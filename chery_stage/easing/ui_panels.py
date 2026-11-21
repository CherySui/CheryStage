"""
CheryStage - Easing UI Panels
Provides clean, intuitive sidebar N-panels in Graph Editor, Dope Sheet, and 3D Viewport,
plus visual preset gallery rendering without label bugs.
"""

from typing import Any
import bpy
from .presets import PRESETS, CATEGORIES


def draw_easing_ui_elements(layout: bpy.types.UILayout, context: bpy.types.Context, props: Any, is_popup: bool = False) -> None:
    """Shared UI rendering for both Sidebar panels and popup dialog."""
    # 1. Preset Category Tabs
    row_cat = layout.row(align=True)
    row_cat.prop(props, "category", text="")

    # 2. Visual Preset Thumbnail Grid (show_labels=False prevents Latin-1 encoding bugs)
    box_grid = layout.box()
    box_grid.template_icon_view(props, "active_preset", show_labels=False, scale=5.0)

    # 3. Active Preset Details Display Card (Crystal-clear Chinese & English)
    active_id = props.active_preset
    p_info = PRESETS.get(active_id, PRESETS.get("ease_out_cubic", {}))
    p_name_cn = p_info.get("name_cn", "")
    p_name_en = p_info.get("name", active_id)
    p_desc = p_info.get("desc", "")
    p_type = p_info.get("type", "BEZIER")

    info_box = layout.box()
    r1 = info_box.row(align=True)
    r1.label(text=f"当前预设: {p_name_cn}", icon="IPO_BEZIER" if p_type == "BEZIER" else "IPO_BOUNCE")
    r2 = info_box.row(align=True)
    r2.label(text=f"({p_name_en})", icon="INFO")

    if p_type == "BEZIER":
        coords = p_info.get("coords", (0.33, 1.0, 0.68, 1.0))
        r3 = info_box.row(align=True)
        r3.label(
            text=f"P1=({coords[0]:.2f}, {coords[1]:.2f})  P2=({coords[2]:.2f}, {coords[3]:.2f})",
            icon="HANDLE_ALIGNED",
        )
    else:
        interp = p_info.get("interpolation", "BOUNCE")
        easing = p_info.get("easing", "EASE_OUT")
        r3 = info_box.row(align=True)
        r3.label(text=f"物理插值: {interp} ({easing})", icon="PHYSICS")

    # 4. Big Primary Apply Button
    btn_col = layout.column(align=True)
    btn_col.scale_y = 1.3
    apply_op = btn_col.operator("chery.apply_easing", text="应用缓动到关键帧 (Apply Easing)", icon="CHECKMARK")
    apply_op.preset = active_id

    # 5. Quick Operations Row (Flip, Copy, Paste)
    row_ops = layout.row(align=True)
    row_ops.operator("chery.flip_handles", text="水平反转", icon="ARROW_LEFTRIGHT").direction = "HORIZONTAL"
    row_ops.operator("chery.copy_handles", text="复制手柄", icon="COPYDOWN")
    row_ops.operator("chery.paste_handles", text="粘贴", icon="PASTEDOWN")

    # 6. Options & Fine-Tuning
    col_opts = layout.column(align=True)
    row_settings = col_opts.row(align=True)
    row_settings.prop(props, "auto_apply", text="点击即应用")
    row_settings.prop(props, "apply_mode", text="")

    row_inf = col_opts.row(align=True)
    row_inf.prop(props, "influence", text="影响强度", slider=True)

    # 7. Fine-tuning Bezier Sliders (Foldable)
    if p_type == "BEZIER":
        box_custom = layout.box()
        header_row = box_custom.row(align=True)
        header_row.prop(props, "use_custom_bezier", text="微调控制手柄坐标", icon="SETTINGS")

        if props.use_custom_bezier:
            c1 = box_custom.column(align=True)
            r1 = c1.row(align=True)
            r1.prop(props, "custom_p1_x", text="P1 X")
            r1.prop(props, "custom_p1_y", text="P1 Y")
            r2 = c1.row(align=True)
            r2.prop(props, "custom_p2_x", text="P2 X")
            r2.prop(props, "custom_p2_y", text="P2 Y")

            r_reset = box_custom.row(align=True)
            r_reset.operator("chery.quick_preset", text="重置为当前预设值", icon="LOOP_BACK").preset_id = active_id


class CHERY_PT_base:
    """Base panel class for CheryStage"""
    bl_region_type = "UI"
    bl_category = "CheryStage"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return True


class CHERY_PT_easing_graph(bpy.types.Panel, CHERY_PT_base):
    """CheryStage Easing Panel in Graph Editor"""
    bl_idname = "CHERY_PT_easing_graph"
    bl_label = "关键帧缓动库 (Keyframe Easing)"
    bl_space_type = "GRAPH_EDITOR"

    def draw_header(self, context):
        self.layout.label(icon="IPO_BEZIER")

    def draw(self, context):
        props = getattr(context.window_manager, "chery_easing", None)
        if props:
            draw_easing_ui_elements(self.layout, context, props, is_popup=False)


class CHERY_PT_easing_dopesheet(bpy.types.Panel, CHERY_PT_base):
    """CheryStage Easing Panel in Dope Sheet"""
    bl_idname = "CHERY_PT_easing_dopesheet"
    bl_label = "关键帧缓动库 (Keyframe Easing)"
    bl_space_type = "DOPESHEET_EDITOR"

    def draw_header(self, context):
        self.layout.label(icon="IPO_BEZIER")

    def draw(self, context):
        props = getattr(context.window_manager, "chery_easing", None)
        if props:
            draw_easing_ui_elements(self.layout, context, props, is_popup=False)


class CHERY_PT_easing_view3d(bpy.types.Panel, CHERY_PT_base):
    """CheryStage Easing Panel in 3D Viewport"""
    bl_idname = "CHERY_PT_easing_view3d"
    bl_label = "关键帧缓动库 (Keyframe Easing)"
    bl_space_type = "VIEW_3D"

    def draw_header(self, context):
        self.layout.label(icon="IPO_BEZIER")

    def draw(self, context):
        props = getattr(context.window_manager, "chery_easing", None)
        if props:
            draw_easing_ui_elements(self.layout, context, props, is_popup=False)


classes = (
    CHERY_PT_easing_graph,
    CHERY_PT_easing_dopesheet,
    CHERY_PT_easing_view3d,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
