"""
CheryStage - Addon Preferences
Provides global settings, module management, shortcut configurations, and uninstallation helpers.
"""

import os
import shutil
from typing import Set
import bpy


class CHERY_OT_uninstall_self(bpy.types.Operator):
    """Completely uninstall and remove CheryStage extension from Blender"""

    bl_idname = "chery.uninstall_self"
    bl_label = "彻底卸载 CheryStage (Uninstall Extension)"
    bl_options = {"INTERNAL"}

    def execute(self, context: bpy.types.Context) -> Set[str]:
        pkg_id = "chery_stage"
        uninstalled = False
        prefs = context.preferences

        # 1. Try to uninstall via Blender 5.2 Extension system
        if hasattr(prefs, "extensions") and hasattr(prefs.extensions, "repos"):
            for idx, repo in enumerate(prefs.extensions.repos):
                ext_dir = os.path.join(repo.directory, pkg_id)
                if os.path.exists(ext_dir):
                    try:
                        bpy.ops.extensions.package_uninstall(repo_index=idx, pkg_id=pkg_id)
                        uninstalled = True
                        break
                    except Exception:
                        pass

        # 2. Try fallback addon_remove or direct directory removal
        if not uninstalled:
            import addon_utils
            addon_utils.disable(pkg_id)
            try:
                bpy.ops.preferences.addon_remove(module=pkg_id)
                uninstalled = True
            except Exception:
                pkg_dir = os.path.dirname(os.path.abspath(__file__))
                if os.path.exists(pkg_dir) and pkg_dir.endswith(pkg_id):
                    try:
                        shutil.rmtree(pkg_dir)
                        uninstalled = True
                    except Exception:
                        pass

        self.report({"INFO"}, "CheryStage 已成功卸载！")
        return {"FINISHED"}


class CheryStagePreferences(bpy.types.AddonPreferences):
    bl_idname = __package__.split(".")[0] if "." in __package__ else __package__

    enable_easing: bpy.props.BoolProperty(
        name="关键帧缓动模块 (Keyframe Easing)",
        description="启用或禁用关键帧缓动工具模块",
        default=True,
    )

    enable_hotkey: bpy.props.BoolProperty(
        name="启用快捷键 Alt + E 呼出缓动调色板",
        description="在曲线编辑器和动画摄影表中按下 Alt + E 快速呼出缓动预设弹窗",
        default=True,
    )

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout

        # Banner Header
        header = layout.box()
        row = header.row(align=True)
        row.label(text="CheryStage - 一站式 MMD 工作流工具箱", icon="COMMUNITY")
        row.label(text="v1.5.1", icon="INFO")

        # Modules Section
        box_modules = layout.box()
        box_modules.label(text="功能模块设置 (Modules)", icon="MODIFIER")
        box_modules.prop(self, "enable_easing")

        # Shortcut Section
        box_keys = layout.box()
        box_keys.label(text="快捷键与交互 (Hotkeys)", icon="EVENT_ALT")
        box_keys.prop(self, "enable_hotkey")

        col_tips = box_keys.column(align=True)
        col_tips.label(text="• 快捷键 Alt + E: 在曲线编辑器或动画摄影表中快速呼出缓动预设调色板", icon="DOT")
        col_tips.label(text="• 侧边栏常驻: 随时在侧边栏 N 面板的 [CheryStage] 选项卡中直观选用缓动库", icon="DOT")
        col_tips.label(text="• MMD 物理烘焙: 3D 视图 N 面板 [CheryStage] 选项卡 → MMD 物理烘焙（选中 MMD Tools 模型后使用）", icon="DOT")
        col_tips.label(text="• 渲染导出: 3D 视图 / 合成器的 N 面板 [CheryStage] 选项卡 → 渲染导出", icon="DOT")

        # Uninstall Section
        box_uninstall = layout.box()
        box_uninstall.label(text="卸载插件说明 (Uninstall):", icon="TRASH")
        col_un_tips = box_uninstall.column(align=True)
        col_un_tips.label(text="• Blender 5.2 将本插件识别为「扩展 (Extension)」。在左侧切换到「获取扩展 (Extensions)」", icon="INFO")
        col_un_tips.label(text="  搜索 CheryStage，点击右上角小三角下拉菜单中即可找到官方【卸载 (Uninstall)】按钮。", icon="BLANK1")
        col_un_tips.separator()
        row_btn = col_un_tips.row(align=True)
        row_btn.operator("chery.uninstall_self", text="在此一键卸载 CheryStage 扩展", icon="X")


def register():
    bpy.utils.register_class(CHERY_OT_uninstall_self)
    bpy.utils.register_class(CheryStagePreferences)


def unregister():
    bpy.utils.unregister_class(CheryStagePreferences)
    bpy.utils.unregister_class(CHERY_OT_uninstall_self)
