"""
CheryStage - Pure Python Curve Icon Generator
Generates high-contrast, anti-aliased 96x96 PNG thumbnails for easing presets without any external dependencies.
"""

import os
import math
import struct
import zlib
from typing import Tuple, List, Dict, Any

# Pure Python PNG encoder
def save_png_rgba(width: int, height: int, rgba_bytes: bytearray, filepath: str) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))

    raw_lines = bytearray()
    row_bytes = width * 4
    for y in range(height):
        raw_lines.append(0)  # Filter type 0 (None)
        raw_lines.extend(rgba_bytes[y * row_bytes : (y + 1) * row_bytes])

    idat = chunk(b"IDAT", zlib.compress(bytes(raw_lines), 9))
    iend = chunk(b"IEND", b"")

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(header + ihdr + idat + iend)


class CurveCanvas:
    """A lightweight 2D canvas with anti-aliased line and circle drawing."""

    def __init__(self, width: int = 96, height: int = 96):
        self.width = width
        self.height = height
        # Buffer of floats for RGBA (0.0 to 1.0)
        self.buf = [0.0] * (width * height * 4)

    def clear(self, r: float, g: float, b: float, a: float = 1.0):
        for i in range(self.width * self.height):
            idx = i * 4
            self.buf[idx + 0] = r
            self.buf[idx + 1] = g
            self.buf[idx + 2] = b
            self.buf[idx + 3] = a

    def blend_pixel(self, x: int, y: int, r: float, g: float, b: float, a: float):
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        idx = (y * self.width + x) * 4
        # Alpha blending over existing background
        src_a = a
        dst_a = self.buf[idx + 3]
        out_a = src_a + dst_a * (1.0 - src_a)
        if out_a <= 1e-6:
            return
        self.buf[idx + 0] = (r * src_a + self.buf[idx + 0] * dst_a * (1.0 - src_a)) / out_a
        self.buf[idx + 1] = (g * src_a + self.buf[idx + 1] * dst_a * (1.0 - src_a)) / out_a
        self.buf[idx + 2] = (b * src_a + self.buf[idx + 2] * dst_a * (1.0 - src_a)) / out_a
        self.buf[idx + 3] = out_a

    def draw_line_aa(self, x0: float, y0: float, x1: float, y1: float,
                     r: float, g: float, b: float, a: float, thickness: float = 1.0):
        """Draws an anti-aliased line segment with specified thickness using distance field."""
        radius = thickness * 0.5
        min_x = max(0, int(math.floor(min(x0, x1) - radius - 1)))
        max_x = min(self.width - 1, int(math.ceil(max(x0, x1) + radius + 1)))
        min_y = max(0, int(math.floor(min(y0, y1) - radius - 1)))
        max_y = min(self.height - 1, int(math.ceil(max(y0, y1) + radius + 1)))

        dx = x1 - x0
        dy = y1 - y0
        len_sq = dx * dx + dy * dy

        for py in range(min_y, max_y + 1):
            for px in range(min_x, max_x + 1):
                cx, cy = px + 0.5, py + 0.5
                if len_sq == 0:
                    dist = math.hypot(cx - x0, cy - y0)
                else:
                    t = max(0.0, min(1.0, ((cx - x0) * dx + (cy - y0) * dy) / len_sq))
                    proj_x = x0 + t * dx
                    proj_y = y0 + t * dy
                    dist = math.hypot(cx - proj_x, cy - proj_y)

                cov = max(0.0, min(1.0, radius + 0.5 - dist))
                if cov > 0.0:
                    self.blend_pixel(px, py, r, g, b, a * cov)

    def draw_circle_aa(self, cx: float, cy: float, radius: float,
                       r: float, g: float, b: float, a: float = 1.0):
        min_x = max(0, int(math.floor(cx - radius - 1)))
        max_x = min(self.width - 1, int(math.ceil(cx + radius + 1)))
        min_y = max(0, int(math.floor(cy - radius - 1)))
        max_y = min(self.height - 1, int(math.ceil(cy + radius + 1)))

        for py in range(min_y, max_y + 1):
            for px in range(min_x, max_x + 1):
                dist = math.hypot((px + 0.5) - cx, (py + 0.5) - cy)
                cov = max(0.0, min(1.0, radius + 0.5 - dist))
                if cov > 0.0:
                    self.blend_pixel(px, py, r, g, b, a * cov)

    def to_bytes(self) -> bytearray:
        result = bytearray(self.width * self.height * 4)
        for i in range(self.width * self.height):
            idx = i * 4
            result[idx + 0] = int(max(0.0, min(1.0, self.buf[idx + 0])) * 255.0 + 0.5)
            result[idx + 1] = int(max(0.0, min(1.0, self.buf[idx + 1])) * 255.0 + 0.5)
            result[idx + 2] = int(max(0.0, min(1.0, self.buf[idx + 2])) * 255.0 + 0.5)
            result[idx + 3] = int(max(0.0, min(1.0, self.buf[idx + 3])) * 255.0 + 0.5)
        return result


def eval_dynamic_curve(interp: str, easing: str, t: float) -> float:
    """Evaluates dynamic interpolations (Bounce, Elastic) at normalized time t in [0, 1]."""
    t = max(0.0, min(1.0, t))

    def bounce_out(x: float) -> float:
        n1 = 7.5625
        d1 = 2.75
        if x < 1 / d1:
            return n1 * x * x
        elif x < 2 / d1:
            x -= 1.5 / d1
            return n1 * x * x + 0.75
        elif x < 2.5 / d1:
            x -= 2.25 / d1
            return n1 * x * x + 0.9375
        else:
            x -= 2.625 / d1
            return n1 * x * x + 0.984375

    def elastic_out(x: float) -> float:
        if x <= 0.0:
            return 0.0
        if x >= 1.0:
            return 1.0
        c4 = (2.0 * math.pi) / 3.0
        return math.pow(2.0, -10.0 * x) * math.sin((x * 10.0 - 0.75) * c4) + 1.0

    if interp == "BOUNCE":
        if easing == "EASE_OUT":
            return bounce_out(t)
        elif easing == "EASE_IN":
            return 1.0 - bounce_out(1.0 - t)
        else:  # EASE_IN_OUT
            if t < 0.5:
                return (1.0 - bounce_out(1.0 - 2.0 * t)) * 0.5
            return (1.0 + bounce_out(2.0 * t - 1.0)) * 0.5

    elif interp == "ELASTIC":
        if easing == "EASE_OUT":
            return elastic_out(t)
        elif easing == "EASE_IN":
            return 1.0 - elastic_out(1.0 - t)
        else:
            if t < 0.5:
                return (1.0 - elastic_out(1.0 - 2.0 * t)) * 0.5
            return (1.0 + elastic_out(2.0 * t - 1.0)) * 0.5

    return t


def render_preset_icon(preset: Dict[str, Any], filepath: str, size: int = 96) -> None:
    """Renders a single preset curve icon and saves as PNG."""
    canvas = CurveCanvas(size, size)

    # 1. Dark theme background
    canvas.clear(0.12, 0.13, 0.15, 1.0)

    # Coordinate mapping: Unit square [0, 1] x [0, 1] maps to image coords
    # Padding: 18px from edges to allow overshoot/back curves
    pad_x = 18.0
    pad_y = 20.0
    plot_w = size - pad_x * 2.0
    plot_h = size - pad_y * 2.0

    def to_canvas(u: float, v: float) -> Tuple[float, float]:
        cx = pad_x + u * plot_w
        cy = (size - pad_y) - v * plot_h
        return cx, cy

    p0 = to_canvas(0.0, 0.0)
    p3 = to_canvas(1.0, 1.0)

    # 2. Reference grid box and diagonal
    # Subtle box boundary
    b_min = to_canvas(0.0, 0.0)
    b_max = to_canvas(1.0, 1.0)
    canvas.draw_line_aa(b_min[0], b_min[1], b_max[0], b_min[1], 0.22, 0.24, 0.28, 0.6, 1.0)
    canvas.draw_line_aa(b_min[0], b_max[1], b_max[0], b_max[1], 0.22, 0.24, 0.28, 0.6, 1.0)
    canvas.draw_line_aa(b_min[0], b_min[1], b_min[0], b_max[1], 0.22, 0.24, 0.28, 0.6, 1.0)
    canvas.draw_line_aa(b_max[0], b_min[1], b_max[0], b_max[1], 0.22, 0.24, 0.28, 0.6, 1.0)

    # Diagonal reference line (dashed effect or dim line)
    canvas.draw_line_aa(p0[0], p0[1], p3[0], p3[1], 0.25, 0.27, 0.32, 0.5, 1.0)

    # 3. Curve points calculation
    curve_points: List[Tuple[float, float]] = []
    p_type = preset.get("type", "BEZIER")

    if p_type == "BEZIER":
        coords = preset.get("coords", (0.0, 0.0, 1.0, 1.0))
        x1, y1, x2, y2 = coords
        p1 = to_canvas(x1, y1)
        p2 = to_canvas(x2, y2)

        # Draw handle lines
        canvas.draw_line_aa(p0[0], p0[1], p1[0], p1[1], 0.8, 0.5, 0.2, 0.45, 1.2)
        canvas.draw_line_aa(p3[0], p3[1], p2[0], p2[1], 0.8, 0.5, 0.2, 0.45, 1.2)
        # Handle end dots
        canvas.draw_circle_aa(p1[0], p1[1], 2.2, 0.95, 0.65, 0.25, 0.8)
        canvas.draw_circle_aa(p2[0], p2[1], 2.2, 0.95, 0.65, 0.25, 0.8)

        # Compute cubic Bezier points
        steps = 64
        for step in range(steps + 1):
            t = step / steps
            mt = 1.0 - t
            mt2 = mt * mt
            mt3 = mt2 * mt
            t2 = t * t
            t3 = t2 * t
            bx = mt3 * 0.0 + 3.0 * mt2 * t * x1 + 3.0 * mt * t2 * x2 + t3 * 1.0
            by = mt3 * 0.0 + 3.0 * mt2 * t * y1 + 3.0 * mt * t2 * y2 + t3 * 1.0
            curve_points.append(to_canvas(bx, by))

    else:
        # Dynamic curves (Bounce / Elastic)
        interp = preset.get("interpolation", "BOUNCE")
        easing = preset.get("easing", "EASE_OUT")
        steps = 80
        for step in range(steps + 1):
            t = step / steps
            val = eval_dynamic_curve(interp, easing, t)
            curve_points.append(to_canvas(t, val))

    # 4. Render main curve with bright vibrant cyan and soft glow
    # Soft glow layer (thickness 3.5, lower alpha)
    for i in range(len(curve_points) - 1):
        pt0 = curve_points[i]
        pt1 = curve_points[i + 1]
        canvas.draw_line_aa(pt0[0], pt0[1], pt1[0], pt1[1], 0.0, 0.75, 1.0, 0.25, 4.0)

    # Core curve layer (thickness 2.2, bright cyan-white)
    for i in range(len(curve_points) - 1):
        pt0 = curve_points[i]
        pt1 = curve_points[i + 1]
        canvas.draw_line_aa(pt0[0], pt0[1], pt1[0], pt1[1], 0.1, 0.9, 1.0, 0.95, 2.2)

    # 5. Keyframe start and end dots
    canvas.draw_circle_aa(p0[0], p0[1], 3.2, 0.95, 0.98, 1.0, 1.0)
    canvas.draw_circle_aa(p0[0], p0[1], 1.8, 0.1, 0.6, 0.9, 1.0)

    canvas.draw_circle_aa(p3[0], p3[1], 3.2, 0.95, 0.98, 1.0, 1.0)
    canvas.draw_circle_aa(p3[0], p3[1], 1.8, 0.1, 0.6, 0.9, 1.0)

    # Save to PNG
    save_png_rgba(size, size, canvas.to_bytes(), filepath)


def generate_all_preset_icons(presets_dict: Dict[str, Dict[str, Any]], output_dir: str) -> None:
    """Generates all preset curve icons into the specified directory if not already cached."""
    os.makedirs(output_dir, exist_ok=True)
    for pid, preset in presets_dict.items():
        filepath = os.path.join(output_dir, f"{pid}.png")
        if not os.path.exists(filepath):
            render_preset_icon(preset, filepath)
