"""
CheryStage - Easing Operators
Blender operators for applying presets, flipping handles, copying/pasting, and floating popup palette.
"""

from typing import Optional, Set
import bpy
from .presets import PRESETS
from .core_easing import (
    apply_easing_to_context,
    flip_handles_in_context,
    copy_handles_from_context,
    get_handle_clipboard,
)


class CHERY_OT_apply_easing(bpy.types.Operator):
    """Apply the active or specified easing preset to selected keyframes"""

    bl_idname = "chery.apply_easing"
    bl_label = "Apply Easing (应用缓动)"
    bl_options = {"REGISTER", "UNDO"}

    preset: bpy.props.StringProperty(
        name="Preset ID",
        description="ID of the preset to apply (blank uses active UI preset)",
        default="",
    )
    influence: bpy.props.FloatProperty(
        name="Influence",
        description="Easing blend factor (-1 uses UI setting)",
        default=-1.0,
        min=-1.0,
        max=1.0,
    )
    flip_x: bpy.props.BoolProperty(
        name="Flip In/Out",
        description="Invert acceleration / deceleration",
        default=False,
    )
    flip_y: bpy.props.BoolProperty(
        name="Flip Vertical",
        description="Invert value curve direction",
        default=False,
    )
    mode: bpy.props.EnumProperty(
        name="Mode",
        items=[
            ("PAIRS", "Consecutive Pairs (相邻对)", "Apply between consecutive selected keyframes"),
            ("EXTREMES", "Extremes Only (首尾对)", "Apply between first and last selected keyframes only"),
        ],
        default="PAIRS",
    )

    def execute(self, context: bpy.types.Context) -> Set[str]:
        props = getattr(context.window_manager, "chery_easing", None)

        preset_id = self.preset
        if not preset_id and props:
            preset_id = props.active_preset
        if not preset_id:
            preset_id = "ease_out_cubic"

        # Resolve influence
        inf = self.influence
        if inf < 0.0:
            inf = props.influence if props else 1.0

        # Check for custom coordinates if user tweaked handle sliders
        custom_coords = None
        if props and props.use_custom_bezier and preset_id in PRESETS and PRESETS[preset_id].get("type") == "BEZIER":
            custom_coords = (props.custom_p1_x, props.custom_p1_y, props.custom_p2_x, props.custom_p2_y)

        apply_flip_x = self.flip_x or (props.flip_in_out if props else False)
        apply_flip_y = self.flip_y or (props.flip_vertical if props else False)
        apply_mode = self.mode if self.properties.is_property_set("mode") else (props.apply_mode if props else "PAIRS")

        stats = apply_easing_to_context(
            context,
            preset_id=preset_id,
            custom_coords=custom_coords,
            influence=inf,
            flip_x=apply_flip_x,
            flip_y=apply_flip_y,
            mode=apply_mode,
        )

        pairs_count = stats["pairs_modified"]
        curves_count = stats["curves_modified"]

        if pairs_count == 0:
            self.report(
                {"WARNING"},
                "未检测到选中的关键帧对！请在同一动画曲线上至少选中两个关键帧 (Select at least 2 keyframes)",
            )
            return {"CANCELLED"}

        p_info = PRESETS.get(preset_id, {})
        display_name = p_info.get("name_cn", p_info.get("name", preset_id))
        self.report(
            {"INFO"},
            f"CheryStage: 已成功应用 [{display_name}] 至 {curves_count} 条曲线上的 {pairs_count} 组关键帧",
        )
        return {"FINISHED"}


class CHERY_OT_quick_preset(bpy.types.Operator):
    """Quickly apply a specific preset with a single click"""

    bl_idname = "chery.quick_preset"
    bl_label = "Quick Preset"
    bl_options = {"REGISTER", "UNDO"}

    preset_id: bpy.props.StringProperty(name="Preset ID")

    def execute(self, context: bpy.types.Context) -> Set[str]:
        props = getattr(context.window_manager, "chery_easing", None)
        if props and self.preset_id in PRESETS:
            props.active_preset = self.preset_id
            # Sync custom bezier sliders
            p = PRESETS[self.preset_id]
            if p.get("type") == "BEZIER":
                coords = p.get("coords", (0.33, 1.0, 0.68, 1.0))
                props.custom_p1_x = coords[0]
                props.custom_p1_y = coords[1]
                props.custom_p2_x = coords[2]
                props.custom_p2_y = coords[3]

        return bpy.ops.chery.apply_easing(preset=self.preset_id)


class CHERY_OT_flip_handles(bpy.types.Operator):
    """Flip existing Bezier handles between selected keyframes"""

    bl_idname = "chery.flip_handles"
    bl_label = "Flip Handles (翻转手柄)"
    bl_options = {"REGISTER", "UNDO"}

    direction: bpy.props.EnumProperty(
        name="Direction",
        items=[
            ("HORIZONTAL", "Horizontal (水平反转 In/Out)", "Flip acceleration and deceleration"),
            ("VERTICAL", "Vertical (垂直反转)", "Invert value direction"),
        ],
        default="HORIZONTAL",
    )

    def execute(self, context: bpy.types.Context) -> Set[str]:
        props = getattr(context.window_manager, "chery_easing", None)
        mode = props.apply_mode if props else "PAIRS"

        flip_x = self.direction == "HORIZONTAL"
        flip_y = self.direction == "VERTICAL"

        count = flip_handles_in_context(context, flip_x=flip_x, flip_y=flip_y, mode=mode)
        if count == 0:
            self.report({"WARNING"}, "未选中可翻转的关键帧对 (No keyframe pairs selected)")
            return {"CANCELLED"}

        dir_label = "水平" if flip_x else "垂直"
        self.report({"INFO"}, f"已成功{dir_label}翻转 {count} 组关键帧的手柄")
        return {"FINISHED"}


class CHERY_OT_copy_handles(bpy.types.Operator):
    """Copy normalized handle coordinates from selected keyframe pair to clipboard"""

    bl_idname = "chery.copy_handles"
    bl_label = "Copy Handles (复制手柄)"

    def execute(self, context: bpy.types.Context) -> Set[str]:
        clip = copy_handles_from_context(context)
        if not clip:
            self.report({"WARNING"}, "请先选中至少两个贝塞尔关键帧以复制手柄")
            return {"CANCELLED"}

        self.report(
            {"INFO"},
            f"已复制手柄: P1=({clip['x1']:.2f}, {clip['y1']:.2f}), P2=({clip['x2']:.2f}, {clip['y2']:.2f})",
        )
        return {"FINISHED"}


class CHERY_OT_paste_handles(bpy.types.Operator):
    """Paste copied handle coordinates to currently selected keyframes"""

    bl_idname = "chery.paste_handles"
    bl_label = "Paste Handles (粘贴手柄)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> Set[str]:
        clip = get_handle_clipboard()
        if not clip:
            self.report({"WARNING"}, "剪贴板中没有已复制的缓动手柄数据")
            return {"CANCELLED"}

        coords = (clip["x1"], clip["y1"], clip["x2"], clip["y2"])
        props = getattr(context.window_manager, "chery_easing", None)
        inf = props.influence if props else 1.0
        mode = props.apply_mode if props else "PAIRS"

        stats = apply_easing_to_context(
            context,
            preset_id="linear",
            custom_coords=coords,
            influence=inf,
            mode=mode,
        )

        count = stats["pairs_modified"]
        if count == 0:
            self.report({"WARNING"}, "未找到目标关键帧对进行粘贴")
            return {"CANCELLED"}

        self.report({"INFO"}, f"已成功将手柄形态粘贴至 {count} 组关键帧")
        return {"FINISHED"}


class CHERY_OT_read_handles(bpy.types.Operator):
    """Read actual Bezier handles from currently selected keyframes into the curve editor"""

    bl_idname = "chery.read_handles"
    bl_label = "Read from Keys (从选中帧读取曲线)"

    def execute(self, context: bpy.types.Context) -> Set[str]:
        from .core_easing import read_handles_from_context
        coords = read_handles_from_context(context)
        if not coords:
            self.report({"WARNING"}, "未检测到选中的关键帧对，无法读取手柄")
            return {"CANCELLED"}

        x1, y1, x2, y2 = coords
        props = getattr(context.window_manager, "chery_easing", None)
        if props:
            props.use_custom_bezier = True
            props.custom_p1_x = max(-2.0, min(3.0, x1))
            props.custom_p1_y = max(-3.0, min(4.0, y1))
            props.custom_p2_x = max(-2.0, min(3.0, x2))
            props.custom_p2_y = max(-3.0, min(4.0, y2))

        self.report(
            {"INFO"},
            f"已成功读取曲线手柄: P1=({x1:.2f}, {y1:.2f})  P2=({x2:.2f}, {y2:.2f})",
        )
        return {"FINISHED"}


class CHERY_OT_popup_easing(bpy.types.Operator):
    """Open floating Keyframe Easing visual palette under the mouse cursor"""

    bl_idname = "chery.popup_easing"
    bl_label = "CheryStage - 关键帧缓动库"

    def execute(self, context: bpy.types.Context) -> Set[str]:
        return {"FINISHED"}

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> Set[str]:
        return context.window_manager.invoke_popup(self, width=340)

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = getattr(context.window_manager, "chery_easing", None)
        if not props:
            layout.label(text="CheryStage not initialized")
            return

        from .ui_panels import draw_easing_ui_elements
        draw_easing_ui_elements(layout, context, props, is_popup=True)


classes = (
    CHERY_OT_apply_easing,
    CHERY_OT_quick_preset,
    CHERY_OT_flip_handles,
    CHERY_OT_copy_handles,
    CHERY_OT_paste_handles,
    CHERY_OT_read_handles,
    CHERY_OT_popup_easing,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
