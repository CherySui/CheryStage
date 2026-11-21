"""
CheryStage - Keyframe Easing Module
Initializes properties, submodules, and lifecycle for the easing toolset.
"""

from typing import Any
import bpy
from .presets import PRESETS, CATEGORIES, get_presets_by_category
from .previews_manager import (
    init_previews,
    unregister_previews,
    get_preset_enum_items,
)
from . import operators, ui_panels

_is_updating_preset = False


def on_category_update(self, context: bpy.types.Context) -> None:
    """When user switches category, set active preset to the first one in the category."""
    global _is_updating_preset
    presets_list = get_presets_by_category(self.category)
    if presets_list:
        preset_ids = [p["id"] for p in presets_list]
        if self.active_preset not in preset_ids:
            _is_updating_preset = True
            try:
                self.active_preset = preset_ids[0]
            finally:
                _is_updating_preset = False


def on_preset_update(self, context: bpy.types.Context) -> None:
    """When active preset changes, sync custom bezier handles and trigger auto-apply if enabled."""
    global _is_updating_preset
    if _is_updating_preset:
        return

    pid = self.active_preset
    if pid in PRESETS:
        p = PRESETS[pid]
        if p.get("type") == "BEZIER":
            coords = p.get("coords", (0.33, 1.0, 0.68, 1.0))
            self.custom_p1_x = coords[0]
            self.custom_p1_y = coords[1]
            self.custom_p2_x = coords[2]
            self.custom_p2_y = coords[3]

    # Auto apply to selected keyframes if enabled
    if self.auto_apply and context and hasattr(bpy.ops, "chery"):
        try:
            bpy.ops.chery.apply_easing(preset=pid)
        except Exception:
            pass


class CheryEasingProperties(bpy.types.PropertyGroup):
    """Property group storing active easing parameters and state."""

    category: bpy.props.EnumProperty(
        name="Category",
        description="Filter presets by category",
        items=CATEGORIES,
        default="ALL",
        update=on_category_update,
    )

    active_preset: bpy.props.EnumProperty(
        name="Preset",
        description="Select an easing preset from the visual gallery",
        items=get_preset_enum_items,
        update=on_preset_update,
    )

    influence: bpy.props.FloatProperty(
        name="Influence",
        description="Blend amount between linear (0.0) and the full easing curve (1.0)",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )

    flip_in_out: bpy.props.BoolProperty(
        name="Invert In/Out (反转入出)",
        description="Inverts acceleration / deceleration",
        default=False,
    )

    flip_vertical: bpy.props.BoolProperty(
        name="Invert Value (反转值)",
        description="Inverts vertical curve direction",
        default=False,
    )

    apply_mode: bpy.props.EnumProperty(
        name="Apply Mode",
        description="How to apply easing across multiple selected keyframes",
        items=[
            ("PAIRS", "Consecutive (相邻对)", "Apply between consecutive selected keyframe pairs"),
            ("EXTREMES", "Extremes (首尾对)", "Apply between first and last selected keyframes only"),
        ],
        default="PAIRS",
    )

    auto_apply: bpy.props.BoolProperty(
        name="Auto Apply on Click (点击即应用)",
        description="Immediately apply the easing curve when clicking an icon in the gallery",
        default=True,
    )

    # Fine-tuning sliders
    use_custom_bezier: bpy.props.BoolProperty(
        name="Fine-tune Handles (微调控制手柄)",
        description="Enable custom manual adjustment of Bezier handle control points",
        default=False,
    )

    custom_p1_x: bpy.props.FloatProperty(
        name="P1 X",
        description="Start handle normalized horizontal offset",
        default=0.33,
        min=-2.0,
        max=3.0,
    )
    custom_p1_y: bpy.props.FloatProperty(
        name="P1 Y",
        description="Start handle normalized vertical offset",
        default=1.0,
        min=-3.0,
        max=4.0,
    )
    custom_p2_x: bpy.props.FloatProperty(
        name="P2 X",
        description="End handle normalized horizontal offset",
        default=0.68,
        min=-2.0,
        max=3.0,
    )
    custom_p2_y: bpy.props.FloatProperty(
        name="P2 Y",
        description="End handle normalized vertical offset",
        default=1.0,
        min=-3.0,
        max=4.0,
    )


def register():
    init_previews()
    bpy.utils.register_class(CheryEasingProperties)
    bpy.types.WindowManager.chery_easing = bpy.props.PointerProperty(type=CheryEasingProperties)

    operators.register()
    ui_panels.register()


def unregister():
    ui_panels.unregister()
    operators.unregister()

    if hasattr(bpy.types.WindowManager, "chery_easing"):
        del bpy.types.WindowManager.chery_easing
    bpy.utils.unregister_class(CheryEasingProperties)

    unregister_previews()
