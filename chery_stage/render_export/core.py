"""
CheryStage - Render Export: core logic
Writes the panel settings into Blender's native render settings.
"""

import os
from fractions import Fraction
from typing import Optional, Tuple
import bpy

from . import compositor

# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

# id: (label, fps, fps_base)
FPS_PRESETS = {
    "23.976": ("23.976", 24, 1.001),
    "24": ("24", 24, 1.0),
    "25": ("25", 25, 1.0),
    "29.97": ("29.97", 30, 1.001),
    "30": ("30", 30, 1.0),
    "48": ("48", 48, 1.0),
    "50": ("50", 50, 1.0),
    "59.94": ("59.94", 60, 1.001),
    "60": ("60", 60, 1.0),
    "120": ("120", 120, 1.0),
}

# id: (label, width, height)
RES_PRESETS = {
    "HD720": ("720p  1280×720", 1280, 720),
    "FHD": ("1080p  1920×1080", 1920, 1080),
    "QHD": ("2K  2560×1440", 2560, 1440),
    "UHD": ("4K  3840×2160", 3840, 2160),
    "V1080": ("竖屏  1080×1920", 1080, 1920),
    "V720": ("竖屏  720×1280", 720, 1280),
    "SQ1080": ("方形  1080×1080", 1080, 1080),
}

# id: (label, container, codec, prores_profile, supports_alpha, audio_codec)
VIDEO_PRESETS = {
    "H264_MP4": ("MP4 · H.264（通用，无透明）", "MPEG4", "H264", None, False, "AAC"),
    "H265_MP4": ("MP4 · H.265（体积小，无透明）", "MPEG4", "H265", None, False, "AAC"),
    "PRORES4444_MOV": ("MOV · ProRes 4444（透明，推荐）", "QUICKTIME", "PRORES", "4444", True, "PCM"),
    "PRORES422HQ_MOV": ("MOV · ProRes 422 HQ（无透明）", "QUICKTIME", "PRORES", "422_HQ", False, "PCM"),
    "PNG_MOV": ("MOV · PNG（透明，无损，体积大）", "QUICKTIME", "PNG", None, True, "PCM"),
    "VP9_WEBM": ("WebM · VP9（透明，网页用）", "WEBM", "WEBM", None, True, "OPUS"),
}

# id: (label, file_format, color_depth)
SEQ_PRESETS = {
    "PNG8": ("PNG 8 位", "PNG", "8"),
    "PNG16": ("PNG 16 位", "PNG", "16"),
    "EXR16": ("OpenEXR 半浮点 (16)", "OPEN_EXR", "16"),
    "EXR32": ("OpenEXR 全浮点 (32)", "OPEN_EXR", "32"),
}

_guard = False  # prevents update callbacks from recursing


def _set(obj, attr, value) -> None:
    try:
        if getattr(obj, attr) != value:
            setattr(obj, attr, value)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Output format
# ---------------------------------------------------------------------------

def video_supports_alpha(p) -> bool:
    return VIDEO_PRESETS[p.video_preset][4]


def apply_format(scene: bpy.types.Scene) -> None:
    p = scene.chery_render
    r = scene.render
    ims = r.image_settings

    if p.output_type == "SEQUENCE":
        _label, fmt, depth = SEQ_PRESETS[p.seq_format]
        _set(ims, "media_type", "IMAGE")
        _set(ims, "file_format", fmt)
        _set(ims, "color_depth", depth)
        _set(ims, "color_mode", "RGBA" if r.film_transparent else "RGB")
        if fmt == "OPEN_EXR":
            _set(ims, "exr_codec", "ZIP")
        else:
            _set(ims, "compression", 15)
    else:
        _label, container, codec, prores, alpha, audio = VIDEO_PRESETS[p.video_preset]
        ff = r.ffmpeg
        _set(ims, "media_type", "VIDEO")
        _set(ff, "format", container)
        _set(ff, "codec", codec)
        if prores:
            _set(ff, "ffmpeg_prores_profile", prores)
        if codec in {"H264", "H265", "WEBM"}:
            _set(ff, "constant_rate_factor", "HIGH")
            _set(ff, "ffmpeg_preset", "GOOD")
        _set(ims, "color_mode", "RGBA" if (r.film_transparent and alpha) else "RGB")
        _set(ff, "audio_codec", audio if p.include_audio else "NONE")
        if p.include_audio:
            _set(ff, "audio_channels", "STEREO")


# ---------------------------------------------------------------------------
# Output path
# ---------------------------------------------------------------------------

def build_filepath(p) -> str:
    d = (p.output_dir or "//").replace("\\", "/")
    if not d.endswith("/"):
        d += "/"
    name = p.file_name.strip() or "{scene_name}"
    if p.output_type == "SEQUENCE" and p.seq_subfolder:
        return f"{d}{name}/{name}_"
    return f"{d}{name}_"


def apply_path(scene: bpy.types.Scene) -> None:
    _set(scene.render, "filepath", build_filepath(scene.chery_render))


def preview_path(scene: bpy.types.Scene) -> Tuple[str, str]:
    """Returns (resolved path, error message)."""
    try:
        return scene.render.frame_path(frame=scene.frame_start), ""
    except Exception as e:  # template parse errors
        msg = str(e).strip().splitlines()
        return "", msg[-1] if msg else "路径无效"


def output_folder(scene: bpy.types.Scene) -> Optional[str]:
    path, err = preview_path(scene)
    if err or not path:
        return None
    return os.path.dirname(bpy.path.abspath(path))


# ---------------------------------------------------------------------------
# Frame rate & time stretching
# ---------------------------------------------------------------------------

def _target_fps(p) -> Tuple[int, float]:
    if p.out_fps == "CUSTOM":
        val = max(1.0, p.custom_fps)
        fps = max(1, round(val))
        return fps, fps / val
    _label, fps, base = FPS_PRESETS[p.out_fps]
    return fps, base


def _ratio(src: float, dst: float) -> Fraction:
    """new/old ratio, both terms within Blender's 1..900 frame_map limit."""
    frac = Fraction(dst / src).limit_denominator(900)
    limit = 900
    while frac.numerator > 900 and limit > 1:
        limit //= 2
        frac = Fraction(dst / src).limit_denominator(limit)
    return frac


def _remap_frame(frame: int, old_map: Tuple[int, int], new_map: Tuple[int, int]) -> int:
    """Converts a frame number between two time-stretch mappings (old, new)."""
    t = frame * old_map[0] / old_map[1]
    return round(t * new_map[1] / new_map[0])


def capture_base(scene: bpy.types.Scene) -> None:
    """Remember the scene's original (authoring) timing."""
    global _guard
    p = scene.chery_render
    r = scene.render
    _guard = True
    try:
        p.base_fps = r.fps
        p.base_fps_base = r.fps_base
        p.base_start = scene.frame_start
        p.base_end = scene.frame_end
        p.is_stretched = True
    finally:
        _guard = False


def apply_fps(scene: bpy.types.Scene) -> None:
    global _guard
    p = scene.chery_render
    r = scene.render

    if p.out_fps == "KEEP":
        restore_fps(scene)
        return

    if not p.is_stretched:
        capture_base(scene)

    prev_map = (r.frame_map_old, r.frame_map_new)
    fps, fps_base = _target_fps(p)
    src = p.base_fps / p.base_fps_base
    dst = fps / fps_base
    ratio = _ratio(src, dst)
    new_map = (ratio.denominator, ratio.numerator)  # (old, new)

    _guard = True
    try:
        _set(r, "fps", fps)
        _set(r, "fps_base", fps_base)
        _set(r, "frame_map_old", new_map[0])
        _set(r, "frame_map_new", new_map[1])

        start = round(p.base_start * new_map[1] / new_map[0])
        end = max(start, round(p.base_end * new_map[1] / new_map[0]))
        scene.frame_start = start
        scene.frame_end = end

        if scene.use_preview_range:
            ps = _remap_frame(scene.frame_preview_start, prev_map, new_map)
            pe = _remap_frame(scene.frame_preview_end, prev_map, new_map)
            scene.frame_preview_start = ps
            scene.frame_preview_end = max(ps, pe)
        scene.frame_current = _remap_frame(scene.frame_current, prev_map, new_map)
    finally:
        _guard = False


def restore_fps(scene: bpy.types.Scene) -> None:
    global _guard
    p = scene.chery_render
    r = scene.render
    if not p.is_stretched:
        return
    prev_map = (r.frame_map_old, r.frame_map_new)
    base_map = (100, 100)
    _guard = True
    try:
        _set(r, "fps", p.base_fps)
        _set(r, "fps_base", p.base_fps_base)
        _set(r, "frame_map_old", 100)
        _set(r, "frame_map_new", 100)
        scene.frame_start = p.base_start
        scene.frame_end = p.base_end
        if scene.use_preview_range:
            ps = _remap_frame(scene.frame_preview_start, prev_map, base_map)
            pe = _remap_frame(scene.frame_preview_end, prev_map, base_map)
            scene.frame_preview_start = ps
            scene.frame_preview_end = max(ps, pe)
        scene.frame_current = _remap_frame(scene.frame_current, prev_map, base_map)
        p.is_stretched = False
        p.out_fps = "KEEP"
    finally:
        _guard = False


def fps_label(fps: int, fps_base: float) -> str:
    val = fps / fps_base
    return f"{val:.3f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Glow
# ---------------------------------------------------------------------------

def apply_glow(scene: bpy.types.Scene) -> str:
    p = scene.chery_render
    if p.keep_glow and scene.render.film_transparent:
        return compositor.insert_glow_node(scene)
    compositor.remove_glow_node(scene)
    return ""


# ---------------------------------------------------------------------------
# All
# ---------------------------------------------------------------------------

def apply_all(scene: bpy.types.Scene) -> str:
    apply_format(scene)
    apply_path(scene)
    return apply_glow(scene)
