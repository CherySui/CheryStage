"""
CheryStage - Core Easing Engine
Handles F-Curve discovery, keyframe selection detection, cubic Bezier handle mathematics,
and dynamic interpolation application compatible with Blender 5.2 layered actions and legacy actions.
"""

from typing import List, Tuple, Dict, Any, Optional, Set, Generator
import bpy
from .presets import PRESETS

# Global clipboard for copying/pasting handle coordinates
_handle_clipboard: Optional[Dict[str, float]] = None


def iter_action_fcurves(action: Any) -> Generator[bpy.types.FCurve, None, None]:
    """
    Safely yields all F-Curves from an Action, supporting both Blender 5.2+ layered actions
    (layers -> strips -> channelbags -> fcurves) and legacy actions (action.fcurves).
    """
    if not action:
        return

    # Blender 5.2 / 4.3+ Layered Actions
    if getattr(action, "is_action_layered", False) and hasattr(action, "layers"):
        for layer in action.layers:
            for strip in getattr(layer, "strips", []):
                for cb in getattr(strip, "channelbags", []):
                    for fc in getattr(cb, "fcurves", []):
                        yield fc
    else:
        # Legacy action or fallback
        if hasattr(action, "fcurves"):
            for fc in action.fcurves:
                yield fc


def get_target_fcurves(context: bpy.types.Context) -> List[bpy.types.FCurve]:
    """
    Retrieves all relevant editable FCurves from the current context.
    Prioritizes context.selected_editable_fcurves when in Graph Editor / Dope Sheet,
    falling back to animated selected objects, pose bones, shape keys, and materials.
    """
    curves: List[bpy.types.FCurve] = []
    seen_ptr: Set[int] = set()

    # 1. If user is in Graph Editor or Dopesheet with explicitly selected editable fcurves
    if hasattr(context, "selected_editable_fcurves") and context.selected_editable_fcurves:
        for fc in context.selected_editable_fcurves:
            ptr = fc.as_pointer()
            if ptr not in seen_ptr:
                seen_ptr.add(ptr)
                curves.append(fc)
        if curves:
            return curves

    # 2. Gather from selected objects and active object
    objects = list(context.selected_objects or [])
    if context.active_object and context.active_object not in objects:
        objects.append(context.active_object)

    for obj in objects:
        # Object-level animation data
        if obj.animation_data and obj.animation_data.action:
            for fc in iter_action_fcurves(obj.animation_data.action):
                ptr = fc.as_pointer()
                if ptr not in seen_ptr:
                    seen_ptr.add(ptr)
                    curves.append(fc)

        # Shape keys animation data
        if getattr(obj, "data", None) and hasattr(obj.data, "shape_keys"):
            sk = obj.data.shape_keys
            if sk and sk.animation_data and sk.animation_data.action:
                for fc in iter_action_fcurves(sk.animation_data.action):
                    ptr = fc.as_pointer()
                    if ptr not in seen_ptr:
                        seen_ptr.add(ptr)
                        curves.append(fc)

        # Active material animation data
        if getattr(obj, "active_material", None):
            mat = obj.active_material
            if mat and mat.animation_data and mat.animation_data.action:
                for fc in iter_action_fcurves(mat.animation_data.action):
                    ptr = fc.as_pointer()
                    if ptr not in seen_ptr:
                        seen_ptr.add(ptr)
                        curves.append(fc)

    return curves


def get_selected_keyframe_pairs(
    fcurve: bpy.types.FCurve, mode: str = "PAIRS"
) -> List[Tuple[bpy.types.Keyframe, bpy.types.Keyframe]]:
    """
    Finds selected keyframes on an F-Curve and forms pairs according to the mode:
    - 'PAIRS': Consecutive pairs [(k0, k1), (k1, k2), ...]
    - 'EXTREMES': Single pair formed by first and last selected keyframes [(k0, kn)]
    """
    selected_kps = [
        kp for kp in fcurve.keyframe_points if getattr(kp, "select_control_point", False)
    ]

    if len(selected_kps) < 2:
        return []

    # Ensure chronological order by frame
    selected_kps.sort(key=lambda k: k.co.x)

    pairs: List[Tuple[bpy.types.Keyframe, bpy.types.Keyframe]] = []
    if mode == "EXTREMES":
        k_start = selected_kps[0]
        k_end = selected_kps[-1]
        if k_end.co.x > k_start.co.x:
            pairs.append((k_start, k_end))
    else:  # "PAIRS" (default)
        for i in range(len(selected_kps) - 1):
            k_a = selected_kps[i]
            k_b = selected_kps[i + 1]
            if k_b.co.x > k_a.co.x:
                pairs.append((k_a, k_b))

    return pairs


def apply_bezier_to_pair(
    fcurve: bpy.types.FCurve,
    kp_start: bpy.types.Keyframe,
    kp_end: bpy.types.Keyframe,
    coords: Tuple[float, float, float, float],
    influence: float = 1.0,
    flip_x: bool = False,
    flip_y: bool = False,
) -> bool:
    """
    Calculates and applies cubic Bezier handles to a keyframe interval [kp_start, kp_end].
    Formula:
      H_start.right = K_start + (x1 * dt, y1 * dv)
      H_end.left    = K_start + (x2 * dt, y2 * dv)
    Handles are set to 'FREE' so that tangents outside [kp_start, kp_end] remain unaffected.
    """
    t0, v0 = kp_start.co.x, kp_start.co.y
    t1, v1 = kp_end.co.x, kp_end.co.y
    dt = t1 - t0
    dv = v1 - v0

    if dt <= 0.0:
        return False

    x1, y1, x2, y2 = coords

    # Blend with linear handles (1/3, 1/3, 2/3, 2/3) based on influence
    w = max(0.0, min(1.0, influence))
    x1 = (1.0 - w) * (1.0 / 3.0) + w * x1
    y1 = (1.0 - w) * (1.0 / 3.0) + w * y1
    x2 = (1.0 - w) * (2.0 / 3.0) + w * x2
    y2 = (1.0 - w) * (2.0 / 3.0) + w * y2

    # Horizontal flip (Invert In/Out)
    if flip_x:
        x1, x2 = 1.0 - x2, 1.0 - x1
        y1, y2 = 1.0 - y2, 1.0 - y1

    # Vertical flip
    if flip_y:
        y1 = 1.0 - y1
        y2 = 1.0 - y2

    # Configure handle types
    kp_start.interpolation = "BEZIER"
    kp_start.handle_right_type = "FREE"
    kp_end.handle_left_type = "FREE"

    # Set exact handle coordinates
    kp_start.handle_right.x = t0 + x1 * dt
    kp_start.handle_right.y = v0 + y1 * dv

    kp_end.handle_left.x = t0 + x2 * dt
    kp_end.handle_left.y = v0 + y2 * dv

    return True


def apply_dynamic_to_pair(
    kp_start: bpy.types.Keyframe,
    preset: Dict[str, Any],
) -> bool:
    """
    Applies Blender's native dynamic interpolation (Bounce, Elastic) to kp_start.
    """
    interp = preset.get("interpolation", "BOUNCE")
    easing = preset.get("easing", "EASE_OUT")

    kp_start.interpolation = interp
    kp_start.easing = easing

    if "amplitude" in preset and hasattr(kp_start, "amplitude"):
        kp_start.amplitude = preset["amplitude"]
    if "period" in preset and hasattr(kp_start, "period"):
        kp_start.period = preset["period"]
    if "back" in preset and hasattr(kp_start, "back"):
        kp_start.back = preset["back"]

    return True


def apply_easing_to_context(
    context: bpy.types.Context,
    preset_id: str,
    custom_coords: Optional[Tuple[float, float, float, float]] = None,
    influence: float = 1.0,
    flip_x: bool = False,
    flip_y: bool = False,
    mode: str = "PAIRS",
    channel_filter: Optional[Tuple[str, int]] = None,
    channel_group: Optional[str] = None,
) -> Dict[str, int]:
    """
    Main controller to apply easing across all selected keyframe pairs in context.
    Supports filtering by specific channel (data_path, array_index) or group ('LOCATION', 'ROTATION', 'SCALE', 'ALL').
    Returns summary stats: {'curves_modified': int, 'pairs_modified': int}
    """
    preset = PRESETS.get(preset_id, PRESETS["ease_out_cubic"])
    p_type = preset.get("type", "BEZIER")

    coords = custom_coords if custom_coords is not None else preset.get("coords", (0.33, 1.0, 0.68, 1.0))

    fcurves = get_target_fcurves(context)
    curves_count = 0
    pairs_count = 0

    for fc in fcurves:
        # Channel filter check
        if channel_filter is not None:
            if fc.data_path != channel_filter[0] or fc.array_index != channel_filter[1]:
                continue
        elif channel_group and channel_group != "ALL":
            if channel_group == "LOCATION" and "location" not in fc.data_path:
                continue
            elif channel_group == "ROTATION" and "rotation" not in fc.data_path:
                continue
            elif channel_group == "SCALE" and "scale" not in fc.data_path:
                continue

        pairs = get_selected_keyframe_pairs(fc, mode=mode)
        if not pairs:
            continue

        modified_on_curve = False
        for kp_a, kp_b in pairs:
            if p_type == "BEZIER":
                success = apply_bezier_to_pair(
                    fc, kp_a, kp_b, coords,
                    influence=influence, flip_x=flip_x, flip_y=flip_y
                )
            else:
                success = apply_dynamic_to_pair(kp_a, preset)

            if success:
                pairs_count += 1
                modified_on_curve = True

        if modified_on_curve:
            fc.update()
            curves_count += 1

    # Force viewport and editor redraws
    for area in context.screen.areas if (context.screen and hasattr(context.screen, "areas")) else []:
        if area.type in ("GRAPH_EDITOR", "DOPESHEET_EDITOR", "VIEW_3D"):
            area.tag_redraw()

    return {"curves_modified": curves_count, "pairs_modified": pairs_count}


def flip_handles_in_context(
    context: bpy.types.Context,
    flip_x: bool = True,
    flip_y: bool = False,
    mode: str = "PAIRS",
) -> int:
    """
    Flips the existing Bezier handles of all selected keyframe pairs in context.
    """
    fcurves = get_target_fcurves(context)
    pairs_count = 0

    for fc in fcurves:
        pairs = get_selected_keyframe_pairs(fc, mode=mode)
        if not pairs:
            continue

        modified = False
        for kp_a, kp_b in pairs:
            t0, v0 = kp_a.co.x, kp_a.co.y
            t1, v1 = kp_b.co.x, kp_b.co.y
            dt = t1 - t0
            dv = v1 - v0
            if dt <= 0.0:
                continue

            # Extract current normalized coords
            hx1 = (kp_a.handle_right.x - t0) / dt
            hy1 = (kp_a.handle_right.y - v0) / dv if dv != 0.0 else 0.0
            hx2 = (kp_b.handle_left.x - t0) / dt
            hy2 = (kp_b.handle_left.y - v0) / dv if dv != 0.0 else 0.0

            if flip_x:
                hx1, hx2 = 1.0 - hx2, 1.0 - hx1
                hy1, hy2 = 1.0 - hy2, 1.0 - hy1
            if flip_y:
                hy1 = 1.0 - hy1
                hy2 = 1.0 - hy2

            kp_a.handle_right_type = "FREE"
            kp_b.handle_left_type = "FREE"
            kp_a.handle_right.x = t0 + hx1 * dt
            kp_a.handle_right.y = v0 + hy1 * dv
            kp_b.handle_left.x = t0 + hx2 * dt
            kp_b.handle_left.y = v0 + hy2 * dv
            pairs_count += 1
            modified = True

        if modified:
            fc.update()

    return pairs_count


def copy_handles_from_context(context: bpy.types.Context) -> Optional[Dict[str, float]]:
    """Copies normalized handle coordinates from the first selected keyframe pair."""
    global _handle_clipboard
    fcurves = get_target_fcurves(context)
    for fc in fcurves:
        pairs = get_selected_keyframe_pairs(fc, mode="PAIRS")
        if pairs:
            kp_a, kp_b = pairs[0]
            t0, v0 = kp_a.co.x, kp_a.co.y
            t1, v1 = kp_b.co.x, kp_b.co.y
            dt = t1 - t0
            dv = v1 - v0
            if dt > 0:
                x1 = (kp_a.handle_right.x - t0) / dt
                y1 = (kp_a.handle_right.y - v0) / dv if dv != 0.0 else 0.0
                x2 = (kp_b.handle_left.x - t0) / dt
                y2 = (kp_b.handle_left.y - v0) / dv if dv != 0.0 else 0.0
                _handle_clipboard = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
                return _handle_clipboard
    return None


def read_handles_from_context(
    context: bpy.types.Context,
    channel_filter: Optional[Tuple[str, int]] = None,
) -> Optional[Tuple[float, float, float, float]]:
    """
    Reads the actual normalized Bezier handle coordinates (x1, y1, x2, y2)
    from the target selected keyframe pair in context, optionally filtered by channel.
    """
    fcurves = get_target_fcurves(context)
    for fc in fcurves:
        if channel_filter is not None:
            if fc.data_path != channel_filter[0] or fc.array_index != channel_filter[1]:
                continue
        pairs = get_selected_keyframe_pairs(fc, mode="PAIRS")
        if pairs:
            kp_a, kp_b = pairs[0]
            t0, v0 = kp_a.co.x, kp_a.co.y
            t1, v1 = kp_b.co.x, kp_b.co.y
            dt = t1 - t0
            dv = v1 - v0
            if dt > 0:
                x1 = (kp_a.handle_right.x - t0) / dt
                y1 = (kp_a.handle_right.y - v0) / dv if dv != 0.0 else 0.0
                x2 = (kp_b.handle_left.x - t0) / dt
                y2 = (kp_b.handle_left.y - v0) / dv if dv != 0.0 else 0.0
                return (x1, y1, x2, y2)

    # Fallback to any selected pair if filter had no keys
    if channel_filter is not None:
        return read_handles_from_context(context, channel_filter=None)
    return None


def get_animated_channels(context: bpy.types.Context) -> List[Dict[str, Any]]:
    """
    Returns list of animated channels discovered in context, with human-readable labels and colors.
    """
    fcurves = get_target_fcurves(context)
    channels = []
    seen = set()

    for fc in fcurves:
        key = (fc.data_path, fc.array_index)
        if key in seen:
            continue
        seen.add(key)

        dp = fc.data_path
        idx = fc.array_index
        axis = ["X", "Y", "Z", "W"][idx] if idx < 4 else str(idx)

        # Determine label and theme color
        if dp == "location":
            name = f"位移 {axis}"
            group = "LOCATION"
            color = (1.0, 0.35, 0.35, 1.0) if axis == "X" else ((0.35, 0.9, 0.4, 1.0) if axis == "Y" else (0.35, 0.65, 1.0, 1.0))
        elif "rotation" in dp:
            name = f"旋转 {axis}"
            group = "ROTATION"
            color = (1.0, 0.55, 0.25, 1.0) if axis == "X" else ((0.85, 0.85, 0.3, 1.0) if axis == "Y" else (0.5, 0.85, 0.95, 1.0))
        elif dp == "scale":
            name = f"缩放 {axis}"
            group = "SCALE"
            color = (0.85, 0.45, 0.95, 1.0) if axis == "X" else ((0.95, 0.65, 0.85, 1.0) if axis == "Y" else (0.7, 0.5, 0.95, 1.0))
        else:
            name = f"{dp}[{axis}]"
            group = "OTHER"
            color = (0.75, 0.75, 0.8, 1.0)

        channels.append({
            "data_path": dp,
            "array_index": idx,
            "name": name,
            "axis": axis,
            "group": group,
            "color": color,
            "has_selected_keys": len(get_selected_keyframe_pairs(fc)) > 0,
        })

    # Sort channels by standard order: Location X,Y,Z, Rotation X,Y,Z, Scale X,Y,Z
    group_order = {"LOCATION": 0, "ROTATION": 1, "SCALE": 2, "OTHER": 3}
    channels.sort(key=lambda c: (group_order.get(c["group"], 9), c["data_path"], c["array_index"]))
    return channels


def get_handle_clipboard() -> Optional[Dict[str, float]]:
    return _handle_clipboard

