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


S = _State()


def _now():
    return time.perf_counter()


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def start():
    now = _now()
    S.active, S.ok, S.t_end = True, True, None
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
    if ok:
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
    return bar_value()


def phase_label():
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
    H = 104 * u
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
