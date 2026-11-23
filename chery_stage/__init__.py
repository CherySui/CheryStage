"""
CheryStage - All-in-one MMD workflow toolkit for Blender (formerly CheryTools)
Features:
- Visual Keyframe Easing library with support for Blender 5.2 layered actions.
- MMD Physics Bake: MMD-style Bullet physics baked by an external solver into keyframes.
- Render Export panel: output type, resolution / frame rate (with time stretching),
  transparent background with glow preservation, output location.
"""

bl_info = {
    "name": "CheryStage",
    "author": "Chery",
    "version": (1, 8, 0),
    "blender": (5, 0, 0),
    "location": "Graph Editor / Dope Sheet / 3D Viewport / Compositor > Sidebar > CheryStage",
    "description": "All-in-one MMD workflow toolkit for Blender (Keyframe Easing, MMD Physics Bake & Render Export)",
    "warning": "",
    "doc_url": "",
    "category": "Animation",
}

from typing import List, Tuple
import bpy
from . import preferences
from . import easing
from . import mmd_bake
from . import render_export

addon_keymaps: List[Tuple[bpy.types.KeyMap, bpy.types.KeyMapItem]] = []


def register_keymaps() -> None:
    """Registers Alt+E shortcut to invoke the CheryStage easing palette in Graph and Dope Sheet."""
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    # Graph Editor
    km_graph = kc.keymaps.new(name="Graph Editor", space_type="GRAPH_EDITOR")
    kmi_graph = km_graph.keymap_items.new("chery.popup_easing", type="E", value="PRESS", alt=True)
    addon_keymaps.append((km_graph, kmi_graph))

    # Dopesheet Editor
    km_dope = kc.keymaps.new(name="Dopesheet", space_type="DOPESHEET_EDITOR")
    kmi_dope = km_dope.keymap_items.new("chery.popup_easing", type="E", value="PRESS", alt=True)
    addon_keymaps.append((km_dope, kmi_dope))


def unregister_keymaps() -> None:
    """Safely removes all registered addon keymaps."""
    for km, kmi in addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    addon_keymaps.clear()


def register():
    preferences.register()
    easing.register()
    mmd_bake.register()
    render_export.register()
    register_keymaps()


def unregister():
    unregister_keymaps()
    render_export.unregister()
    mmd_bake.unregister()
    easing.unregister()
    preferences.unregister()


if __name__ == "__main__":
    register()
