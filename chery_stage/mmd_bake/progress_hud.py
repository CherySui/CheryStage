"""
CheryStage - MMD Physics Bake progress HUD
One floating card in every 3D viewport, drawn with the GPU module in the easing library style
(dark tile, subtle grid, cyan glow line, keyframe-style dots).

Three phases (预热 / 烘焙 / 写入) share the card:
  * a 3-node keyframe timeline at the top shows which phase is running,
  * the bar shows the current phase 0..100%,
  * switching phase wipes the old bar out, slides the timeline fill to the next node,
    pops that node, and cross-slides the status text.
The card pops up from the bottom edge and slides back down when done.

Streaming bakes (motion read from Blender while mmdbake simulates it) use another layout:
  * a 2-step header: 读取 + 物理 -> 写入,
  * one bar like a video buffer: a striped, flowing segment for the frames Blender has read,
    a bright segment with a keyframe head for the frames physics has finished chasing it,
    a ring around the head that fills during the physics warm-up,
  * a legend row with both counts and the buffer between them (or "waiting for frames").
"""

import math
import time

import bpy
import blf
import gpu
from gpu_extras.batch import batch_for_shader

# Easing library palette
BG = (0.12, 0.13, 0.15)
GRID = (0.22, 0.24, 0.28)
TRACK = (0.17, 0.18, 0.21)
GLOW = (0.0, 0.75, 1.0)
CORE = (0.1, 0.9, 1.0)
DOT_RIM = (0.95, 0.98, 1.0)
DOT_CORE = (0.1, 0.6, 0.9)
WARN = (0.95, 0.65, 0.25)  # easing handle orange: cancelled / failed
TEXT = (0.85, 0.88, 0.92)

PHASES = ("预热", "烘焙", "写入")
STREAM_STEPS = ("读取 + 物理", "写入")
BUF = (0.35, 0.55, 0.70)   # read-ahead (buffered) frames

# timings (seconds)
ENTER = 0.5       # pop up from bottom
EXIT = 0.4        # slide back down
HOLD = 0.9        # stay visible after finishing
TWEEN = 0.35      # bar value tween
WIPE = 0.28       # old bar wipe-out on phase change
NEW_DELAY = 0.22  # new phase bar starts after the wipe
TEXT_T = 0.32     # status text cross-slide
NODE_T = 0.45     # timeline fill slide
POP_T = 0.4       # node pop


# --------------------------------------------------------------------------- #
# easing
# --------------------------------------------------------------------------- #
def _c(t):
    return min(1.0, max(0.0, t))


def ease_out_cubic(t):
    t = _c(t)
    return 1.0 - (1.0 - t) ** 3


def ease_in_cubic(t):
    t = _c(t)
    return t * t * t


def ease_in_out_cubic(t):
    t = _c(t)
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_out_back(t, s=1.70158):
    t = _c(t)
    return 1 + (s + 1) * (t - 1) ** 3 + s * (t - 1) ** 2


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
class _State:
    active = False
    handle = None
    t_show = 0.0
    t_end = None
    ok = True
    # phase
    phase = 0
    t_phase = -10.0          # time of the last phase switch
    node_from = 0.0          # timeline fill (in phase units) at switch time
    node_to = 0.0
    wipe_value = 0.0         # old bar value being wiped out
    # bar (current phase)
    target = 0.0
    tw_from = 0.0
    tw_start = 0.0
    # text
    text = ""
    prev_text = ""
    t_text = -10.0
    # streaming layout
    stream = False
    stage = 0                # 0: read + physics, 1: write, 2: done
    t_stage = -10.0
    total = 1
    warm = None              # (done, total) while physics warms up
    read = None              # _Tween, frames read (fraction)
    phys = None              # _Tween, frames simulated (fraction)


class _Tween:
    def __init__(self):
        self.a = self.b = 0.0
        self.t = -10.0

    def set(self, v, now):
        if abs(v - self.b) > 1e-6:
            self.a = self.value(now)
            self.b = v
            self.t = now

    def value(self, now):
        return self.a + (self.b - self.a) * ease_out_cubic((now - self.t) / TWEEN)


S = _State()


def _now():
    return time.perf_counter()


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def start(stream=False):
    """stream=True: motion is read from Blender while physics runs (buffer-bar layout)."""
    now = _now()
    S.active, S.ok, S.t_end = True, True, None
    S.stream, S.stage, S.t_stage, S.total, S.warm = stream, 0, -10.0, 1, None
    S.read, S.phys = _Tween(), _Tween()
    S.t_show = now
    S.phase, S.t_phase, S.node_from, S.node_to, S.wipe_value = 0, -10.0, 0.0, 0.0, 0.0
    S.target = S.tw_from = 0.0
    S.tw_start = now
    S.text, S.prev_text, S.t_text = "启动中", "", -10.0
    if S.handle is None:
        S.handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), "WINDOW", "POST_PIXEL")
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=0.0)


def set_phase(index, text=None):
    """Switch to phase `index` (0..2) with the transition animation."""
    if S.stream:
        if index >= 2 and S.stage == 0:   # read + physics done -> write
            now = _now()
            S.wipe_value = S.phys.value(now)
            S.stage, S.t_stage, S.t_phase = 1, now, now
            S.target = S.tw_from = 0.0
            S.tw_start = now + NEW_DELAY
            if text is not None:
                set_text(text, animate=True)
        elif text is not None:
            set_text(text)
        return
    index = max(0, min(len(PHASES) - 1, index))
    if index <= S.phase and S.t_phase > 0:
        if text is not None:
            set_text(text)
        return
    now = _now()
    S.wipe_value = bar_value()
    S.node_from = _node_fill(now)
    S.node_to = float(index)
    S.phase = index
    S.t_phase = now
    S.target = S.tw_from = 0.0
    S.tw_start = now + NEW_DELAY
    if text is not None:
        set_text(text, animate=True)


def stream_update(read, phys, total, warm=None, text=None):
    """Streaming layout: `read` / `phys` frames done out of `total`; warm = (done, total) or None."""
    now = _now()
    S.total = max(1, total)
    S.read.set(_c(read / S.total), now)
    S.phys.set(_c(phys / S.total), now)
    S.warm = warm
    if text is not None:
        set_text(text)


def update(value, text=None):
    """Progress of the current phase (0..1)."""
    if text is not None:
        set_text(text)
    value = _c(value)
    if abs(value - S.target) > 1e-4:
        now = _now()
        S.tw_from = bar_value()
        S.tw_start = max(now, S.tw_start)  # keep the post-wipe delay
        S.target = value


def set_text(text, animate=False):
    if text == S.text:
        return
    if animate:
        S.prev_text = S.text
        S.t_text = _now()
    S.text = text


def finish(ok=True, text=None):
    if not S.active:
        return
    now = _now()
    S.ok = ok
    if ok and S.stream:
        if S.stage == 0:
            set_phase(2)
        S.stage = 2
        update(1.0)
    elif ok:
        S.node_from = _node_fill(now)
        S.node_to = float(len(PHASES))   # all nodes done
        S.t_phase = now
        S.phase = len(PHASES)
        update(1.0)
    if text is not None:
        set_text(text, animate=True)
    S.t_end = now


def bar_value(now=None):
    now = _now() if now is None else now
    k = ease_out_cubic((now - S.tw_start) / TWEEN)
    return S.tw_from + (S.target - S.tw_from) * k


def current():
    if S.stream and S.stage == 0:
        return S.phys.value(_now())
    return bar_value()


def phase_label():
    if S.stream:
        return STREAM_STEPS[min(S.stage, 1)]
    return PHASES[min(S.phase, len(PHASES) - 1)]


def is_active():
    return S.active


def stop():
    S.active = False
    if S.handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(S.handle, "WINDOW")
        except Exception:
            pass
        S.handle = None
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    _redraw()


# --------------------------------------------------------------------------- #
# animation driver
# --------------------------------------------------------------------------- #
def _node_fill(now):
    return S.node_from + (S.node_to - S.node_from) * ease_in_out_cubic((now - S.t_phase) / NODE_T)


def _tick():
    if not S.active:
        return None
    if S.t_end is not None and _now() - S.t_end > HOLD + EXIT:
        stop()
        return None
    _redraw()
    return 1.0 / 60.0


def _redraw():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type in {"VIEW_3D", "STATUSBAR"}:
                area.tag_redraw()


# --------------------------------------------------------------------------- #
# drawing helpers
# --------------------------------------------------------------------------- #
_shader = None


def _sh():
    global _shader
    if _shader is None:
        _shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    return _shader


def _fill(pts, color):
    if len(pts) < 3 or color[3] <= 0.0:
        return
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    tris = []
    n = len(pts)
    for i in range(n):
        tris += [(cx, cy), pts[i], pts[(i + 1) % n]]
    sh = _sh()
    batch = batch_for_shader(sh, "TRIS", {"pos": tris})
    sh.bind()
    sh.uniform_float("color", color)
    batch.draw(sh)


def _feather(pts_fn, color, steps=3, px=1.0):
    """Cheap anti-aliasing: a few slightly larger, fainter copies under the shape."""
    r, g, b, a = color
    for i in range(steps, 0, -1):
        _fill(pts_fn(i * px / steps), (r, g, b, a * 0.18))
    _fill(pts_fn(0.0), color)


def _rrect(x, y, w, h, r, grow=0.0, seg=6):
    x, y, w, h, r = x - grow, y - grow, w + 2 * grow, h + 2 * grow, r + grow
    r = max(0.0, min(r, w / 2, h / 2))
    pts = []
    for cx, cy, a0 in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180)):
        for i in range(seg + 1):
            a = math.radians(a0 + 90 * i / seg)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _circle(cx, cy, r, seg=20):
    return [(cx + r * math.cos(2 * math.pi * i / seg), cy + r * math.sin(2 * math.pi * i / seg)) for i in range(seg)]


def _line(x0, y0, x1, y1, w, color):
    dx, dy = x1 - x0, y1 - y0
    l = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / l * w / 2, dx / l * w / 2
    _fill([(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)], color)


def _text(x, y, s, size, color, align="L"):
    if not s or color[3] <= 0.0:
        return
    fid = 0
    blf.size(fid, size)
    blf.color(fid, *color)
    w = blf.dimensions(fid, s)[0]
    if align == "R":
        x -= w
    elif align == "C":
        x -= w / 2
    blf.position(fid, x, y, 0)
    blf.draw(fid, s)


def _keyframe_dot(cx, cy, r, rim, core, alpha, u):
    _feather(lambda g: _circle(cx, cy, r + g), (*rim, alpha), px=u)
    _fill(_circle(cx, cy, r * 0.55), (*core, alpha))


# --------------------------------------------------------------------------- #
# draw
# --------------------------------------------------------------------------- #
def _draw():
    if not S.active:
        return
    region = bpy.context.region
    if region is None:
        return
    now = _now()
    u = bpy.context.preferences.system.ui_scale
    glow = GLOW if S.ok else WARN
    core = CORE if S.ok else WARN

    W = min(460 * u, region.width - 40 * u)
    H = (116 if S.stream else 104) * u
    x0 = (region.width - W) / 2
    base_y = 30 * u

    # pop up from bottom (overshoot), slide back down at the end
    enter = ease_out_back((now - S.t_show) / ENTER)
    y0 = base_y - (1.0 - enter) * (H + base_y + 20 * u)
    A = 1.0
    if S.t_end is not None:
        k = ease_in_cubic((now - S.t_end - HOLD) / EXIT)
        y0 -= k * (H + base_y + 20 * u)
        A = 1.0 - k * 0.6
    if y0 + H < 0:
        return

    gpu.state.blend_set("ALPHA")

    # card
    _feather(lambda g: _rrect(x0, y0, W, H, 10 * u, g), (*BG, 0.95 * A), px=1.2 * u)
    _line(x0 + 10 * u, y0 + H - 0.5 * u, x0 + W - 10 * u, y0 + H - 0.5 * u, 1.0 * u, (*GRID, 0.7 * A))

    bx, bw = x0 + 22 * u, W - 44 * u

    if S.stream:
        _draw_stream(now, u, x0, y0, W, H, bx, bw, A, glow, core)
        gpu.state.blend_set("NONE")
        return

    # ---- timeline: 3 keyframe nodes -------------------------------------
    ty = y0 + H - 40 * u
    nodes = [bx + bw * i / (len(PHASES) - 1) for i in range(len(PHASES))]
    fill = _node_fill(now)                                  # 0..3 in phase units
    fill_x = bx + bw * min(fill, len(PHASES) - 1) / (len(PHASES) - 1)
    _line(nodes[0], ty, nodes[-1], ty, 2.0 * u, (*GRID, A))
    if fill_x > nodes[0]:
        _line(nodes[0], ty, fill_x, ty, 5.0 * u, (*glow, 0.18 * A))
        _line(nodes[0], ty, fill_x, ty, 2.2 * u, (*core, 0.95 * A))

    breathe = 0.5 + 0.5 * math.sin(now * 4.0)
    for i, nx in enumerate(nodes):
        reached = fill >= i - 1e-3
        done = fill >= i + 1 - 1e-3 or (S.phase >= len(PHASES))
        current = (i == S.phase) and S.t_end is None
        # pop when the fill arrives at this node
        arrive_t = S.t_phase + NODE_T * (0.6 if i == S.phase else 1.0)
        pop = ease_out_back((now - arrive_t) / POP_T) if (current and S.t_phase > 0) else 1.0
        if current:
            r = 5.2 * u * (0.6 + 0.4 * pop)
            _fill(_circle(nx, ty, 11 * u * pop), (*glow, (0.10 + 0.08 * breathe) * A))
            _keyframe_dot(nx, ty, r, DOT_RIM, DOT_CORE if S.ok else WARN, A, u)
        elif reached or done:
            _keyframe_dot(nx, ty, 4.4 * u, DOT_RIM, core, A, u)
        else:
            _keyframe_dot(nx, ty, 4.2 * u, GRID, BG, A, u)
        # labels: left / center / right aligned under the dots
        align = "L" if i == 0 else ("R" if i == len(PHASES) - 1 else "C")
        lx = nx - 4 * u if align == "L" else (nx + 4 * u if align == "R" else nx)
        col = TEXT
        a_lab = 1.0 if current else (0.75 if done else 0.32)
        _text(lx, ty + 15 * u, PHASES[i], 11 * u, (*(core if current else col), a_lab * A), align=align)

    # ---- status text (cross-slide on phase switch) + phase percent -------
    tyt = y0 + 30 * u
    k = ease_out_cubic((now - S.t_text) / TEXT_T)
    if k < 1.0 and S.prev_text:
        a_old = max(0.0, 1.0 - k * 2.0)                  # old text gone by mid-transition
        _text(x0 + 22 * u, tyt + 14 * u * k, S.prev_text, 12 * u, (*TEXT, a_old * 0.8 * A))
    a_new = min(1.0, max(0.0, (k - 0.5) / 0.5)) if S.prev_text else 1.0   # new text only after that
    _text(x0 + 22 * u, tyt - 8 * u * (1.0 - k), S.text, 12 * u, (*TEXT, a_new * 0.8 * A))

    v = bar_value(now)
    pct = f"{v * 100:.0f}%"
    _text(x0 + W - 22 * u, tyt, pct, 13 * u, (*core, A), align="R")

    # ---- bar for the current phase --------------------------------------
    bh = 6 * u
    by = y0 + 14 * u
    _feather(lambda g: _rrect(bx, by, bw, bh, bh / 2, g), (*TRACK, A), px=u)
    for i in range(1, 10):
        tx = bx + bw * i / 10
        _line(tx, by - 3 * u, tx, by + bh + 3 * u, 1.0 * u, (*GRID, 0.55 * A))

    # old bar wipes out to the right after a phase switch
    wk = (now - S.t_phase) / WIPE
    if 0.0 <= wk < 1.0 and S.wipe_value > 0.0:
        right = bx + bw * max(S.wipe_value, 0.02)
        left = bx + (right - bx) * ease_in_cubic(wk)
        if right - left > 0.5:
            _fill(_rrect(left, by, right - left, bh, bh / 2, 2.5 * u), (*glow, 0.2 * A * (1 - wk)))
            _fill(_rrect(left, by, right - left, bh, bh / 2), (*core, 0.95 * A * (1 - wk * 0.5)))

    if now >= S.tw_start:
        fw = bw * v
        if fw > 0.5:
            _fill(_rrect(bx, by, max(bh, fw), bh, bh / 2, 5 * u), (*glow, (0.07 + 0.04 * breathe) * A))
            _fill(_rrect(bx, by, max(bh, fw), bh, bh / 2, 2.5 * u), (*glow, 0.22 * A))
            _feather(lambda g: _rrect(bx, by, max(bh, fw), bh, bh / 2, g), (*core, 0.95 * A), px=u)
        cy = by + bh / 2
        hx = bx + fw
        _fill(_circle(hx, cy, 9 * u), (*glow, (0.12 + 0.08 * breathe) * A))
        _keyframe_dot(hx, cy, 5.0 * u, DOT_RIM, DOT_CORE if S.ok else WARN, A, u)

    gpu.state.blend_set("NONE")


# --------------------------------------------------------------------------- #
# streaming layout
# --------------------------------------------------------------------------- #
def _arc(cx, cy, r, frac, w, color, seg=32):
    """Clockwise arc from 12 o'clock covering `frac` of the circle."""
    n = max(1, int(seg * frac))
    pts = [(cx + r * math.sin(2 * math.pi * frac * i / n), cy + r * math.cos(2 * math.pi * frac * i / n))
           for i in range(n + 1)]
    for (ax, ay), (bx_, by_) in zip(pts, pts[1:]):
        _line(ax, ay, bx_, by_, w, color)


def _stripes(left, right, y, h, u, now, color):
    """Diagonal stripes flowing to the right inside [left, right] (vertices clamped to the segment)."""
    if right - left < 1.0:
        return
    sw, gap, sk = 4 * u, 9 * u, 5 * u
    off = (now * 28 * u) % gap
    x = left - sk - gap + off
    while x < right:
        quad = [(x, y), (x + sw, y), (x + sw + sk, y + h), (x + sk, y + h)]
        quad = [(min(right, max(left, px)), py) for px, py in quad]
        if quad[1][0] - quad[0][0] > 0.2 or quad[2][0] - quad[3][0] > 0.2:
            _fill(quad, color)
        x += gap


def _glow_bar(bx, by, fw, bh, u, A, glow, core, breathe):
    if fw > 0.5:
        _fill(_rrect(bx, by, max(bh, fw), bh, bh / 2, 5 * u), (*glow, (0.07 + 0.04 * breathe) * A))
        _fill(_rrect(bx, by, max(bh, fw), bh, bh / 2, 2.5 * u), (*glow, 0.22 * A))
        _feather(lambda g: _rrect(bx, by, max(bh, fw), bh, bh / 2, g), (*core, 0.95 * A), px=u)


def _draw_stream(now, u, x0, y0, W, H, bx, bw, A, glow, core):
    breathe = 0.5 + 0.5 * math.sin(now * 4.0)
    stage_k = ease_in_out_cubic((now - S.t_stage) / NODE_T) if S.stage >= 1 else 0.0

    # ---- header: two steps joined by a connector that fills on the switch
    hy = y0 + H - 30 * u
    x = bx
    for i, label in enumerate(STREAM_STEPS):
        current_step = i == min(S.stage, 1) and S.t_end is None
        done = S.stage > i
        dcx, dcy = x + 5 * u, hy + 4 * u
        if current_step:
            pop = ease_out_back((now - S.t_stage) / POP_T) if i == 1 else 1.0
            _fill(_circle(dcx, dcy, 10 * u * pop), (*glow, (0.10 + 0.08 * breathe) * A))
            _keyframe_dot(dcx, dcy, 5.0 * u * (0.6 + 0.4 * pop), DOT_RIM, DOT_CORE if S.ok else WARN, A, u)
        elif done:
            _keyframe_dot(dcx, dcy, 4.4 * u, DOT_RIM, core, A, u)
        else:
            _keyframe_dot(dcx, dcy, 4.2 * u, GRID, BG, A, u)
        tx = x + 19 * u
        a_lab = 1.0 if current_step else (0.75 if done else 0.35)
        _text(tx, hy, label, 11.5 * u, (*(core if current_step else TEXT), a_lab * A))
        blf.size(0, 11.5 * u)
        x = tx + blf.dimensions(0, label)[0] + 12 * u
        if i == 0:
            cw = 34 * u
            _line(x, dcy, x + cw, dcy, 2.0 * u, (*GRID, A))
            if stage_k > 0:
                _line(x, dcy, x + cw * stage_k, dcy, 2.2 * u, (*core, 0.95 * A))
            x += cw + 12 * u

    _text(x0 + W - 22 * u, hy, f"{current() * 100:.0f}%", 13 * u, (*core, A), align="R")

    # ---- status text (cross-slide)
    tyt = y0 + 60 * u
    k = ease_out_cubic((now - S.t_text) / TEXT_T)
    if k < 1.0 and S.prev_text:
        _text(bx, tyt + 14 * u * k, S.prev_text, 12 * u, (*TEXT, max(0.0, 1.0 - k * 2.0) * 0.8 * A))
    a_new = min(1.0, max(0.0, (k - 0.5) / 0.5)) if S.prev_text else 1.0
    _text(bx, tyt - 8 * u * (1.0 - k), S.text, 12 * u, (*TEXT, a_new * 0.8 * A))

    # ---- legend row: counts + buffer (fades out when writing starts)
    r = S.read.value(now)
    p = min(S.phys.value(now), r)
    la = (1.0 - stage_k) * A
    ly = y0 + 34 * u
    if la > 0.01:
        n_read, n_phys = round(S.read.b * S.total), round(S.phys.b * S.total)
        lab1 = f"已读取 {n_read}"
        _fill(_rrect(bx, ly + 1 * u, 9 * u, 7 * u, 2 * u), (*BUF, 0.85 * la))
        _text(bx + 14 * u, ly, lab1, 10.5 * u, (*TEXT, 0.7 * la))
        blf.size(0, 10.5 * u)
        x2 = bx + 14 * u + blf.dimensions(0, lab1)[0] + 16 * u
        _fill(_rrect(x2, ly + 1 * u, 9 * u, 7 * u, 2 * u), (*core, 0.95 * la))
        lab2 = f"物理预热 {S.warm[0]}/{S.warm[1]}" if S.warm else f"已计算 {n_phys} / {S.total} 帧"
        _text(x2 + 14 * u, ly, lab2, 10.5 * u, (*TEXT, 0.7 * la))
        if S.warm:
            info, col, a_info = "", TEXT, 0.0
        elif n_read >= S.total:
            info, col, a_info = "读取完成", TEXT, 0.55
        elif n_read - n_phys <= 1 and n_phys > 0:
            info, col, a_info = "等待读取…", WARN, 0.55 + 0.35 * breathe
        else:
            info, col, a_info = f"缓冲 {n_read - n_phys} 帧", TEXT, 0.55
        _text(x0 + W - 22 * u, ly, info, 10.5 * u, (*col, a_info * la), align="R")

    # ---- bar
    bh = 8 * u
    by = y0 + 14 * u
    cy = by + bh / 2
    _feather(lambda g: _rrect(bx, by, bw, bh, bh / 2, g), (*TRACK, A), px=u)
    for i in range(1, 10):
        tx = bx + bw * i / 10
        _line(tx, by - 3 * u, tx, by + bh + 3 * u, 1.0 * u, (*GRID, 0.55 * A))

    if S.stage == 0:
        rx, px = bx + bw * r, bx + bw * p
        if rx - px > 0.5:   # buffered frames: soft fill + flowing stripes
            _fill(_rrect(px, by, rx - px, bh, min(bh / 2, (rx - px) / 2)), (*BUF, 0.28 * A))
            _stripes(px, rx, by, bh, u, now, (*BUF, 0.55 * A))
        if rx > bx + 0.5:   # read head caret
            _line(rx, by - 4 * u, rx, by + bh + 4 * u, 1.6 * u, (*DOT_RIM, 0.75 * A))
        _glow_bar(bx, by, px - bx, bh, u, A, glow, core, breathe)
        _fill(_circle(px, cy, 9 * u), (*glow, (0.12 + 0.08 * breathe) * A))
        if S.warm:          # warm-up ring around the physics head
            _arc(px, cy, 11 * u, 1.0, 1.4 * u, (*GRID, 0.9 * A))
            frac = S.warm[0] / max(1, S.warm[1])
            if frac > 0:
                _arc(px, cy, 11 * u, frac, 2.0 * u, (*core, 0.95 * A))
        _keyframe_dot(px, cy, 5.0 * u, DOT_RIM, DOT_CORE if S.ok else WARN, A, u)
        return

    # write stage: the physics bar wipes out, then the write bar fills
    wk = (now - S.t_stage) / WIPE
    if 0.0 <= wk < 1.0 and S.wipe_value > 0.0:
        right = bx + bw * max(S.wipe_value, 0.02)
        left = bx + (right - bx) * ease_in_cubic(wk)
        if right - left > 0.5:
            _fill(_rrect(left, by, right - left, bh, bh / 2, 2.5 * u), (*glow, 0.2 * A * (1 - wk)))
            _fill(_rrect(left, by, right - left, bh, bh / 2), (*core, 0.95 * A * (1 - wk * 0.5)))
    if now >= S.tw_start:
        fw = bw * bar_value(now)
        _glow_bar(bx, by, fw, bh, u, A, glow, core, breathe)
        _fill(_circle(bx + fw, cy, 9 * u), (*glow, (0.12 + 0.08 * breathe) * A))
        _keyframe_dot(bx + fw, cy, 5.0 * u, DOT_RIM, DOT_CORE if S.ok else WARN, A, u)
