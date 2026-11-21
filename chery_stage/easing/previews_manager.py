"""
CheryStage - Easing Previews and Icon Manager
Manages preview collection, lazy icon caching, and EnumProperty items for template_icon_view.
"""

import os
from typing import Dict, Any, List, Tuple
import bpy
import bpy.utils.previews
from .presets import PRESETS, CATEGORIES, get_presets_by_category
from .icon_generator import generate_all_preset_icons

_preview_collections: Dict[str, Any] = {}
_cached_items: Dict[str, List[Tuple[str, str, str, int, int]]] = {}


def get_icons_dir() -> str:
    """Returns the absolute directory path where preset icons are cached."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(current_dir, "icons_cache")


def ensure_icons_generated() -> None:
    """Generates all preset icons if not present."""
    icons_dir = get_icons_dir()
    generate_all_preset_icons(PRESETS, icons_dir)


def init_previews() -> Any:
    """Initializes the preview collection and loads all preset icons."""
    global _preview_collections
    if "main" in _preview_collections:
        return _preview_collections["main"]

    ensure_icons_generated()
    icons_dir = get_icons_dir()

    pcoll = bpy.utils.previews.new()
    for pid in PRESETS.keys():
        icon_path = os.path.join(icons_dir, f"{pid}.png")
        if os.path.exists(icon_path):
            pcoll.load(pid, icon_path, "IMAGE")

    _preview_collections["main"] = pcoll
    return pcoll


def get_preview_collection() -> Any:
    """Gets the active preview collection, initializing it if needed."""
    if "main" not in _preview_collections:
        return init_previews()
    return _preview_collections.get("main")


def get_icon_id(preset_id: str) -> int:
    """Returns the Blender icon ID for a preset."""
    pcoll = get_preview_collection()
    if pcoll and preset_id in pcoll:
        return pcoll[preset_id].icon_id
    return 0


def clear_items_cache() -> None:
    global _cached_items
    _cached_items.clear()


def get_preset_enum_items(self, context) -> List[Tuple[str, str, str, int, int]]:
    """
    EnumProperty callback returning items filtered by active category with visual icon IDs.
    Uses clean English identifier for the internal label and full bilingual info in tooltip.
    """
    pcoll = get_preview_collection()
    category = getattr(self, "category", "ALL")

    presets_list = get_presets_by_category(category)
    if not presets_list:
        presets_list = list(PRESETS.values())

    items = []
    for idx, p in enumerate(presets_list):
        pid = p["id"]
        # Short clean name prevents Blender internal C string truncation issues
        name = p["name"]
        cn = p.get("name_cn", "")
        desc = f"{cn} - {p.get('desc', '')}" if cn else p.get("desc", "")
        icon_id = pcoll[pid].icon_id if (pcoll and pid in pcoll) else 0
        items.append((pid, name, desc, icon_id, idx))

    return items


def unregister_previews() -> None:
    """Cleans up preview collection on unregister."""
    global _preview_collections, _cached_items
    for pcoll in _preview_collections.values():
        try:
            bpy.utils.previews.remove(pcoll)
        except Exception:
            pass
    _preview_collections.clear()
    _cached_items.clear()
