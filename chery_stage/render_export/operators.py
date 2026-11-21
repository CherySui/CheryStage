"""
CheryStage - Render Export: operators
"""

import os
import subprocess
import sys
import bpy
from . import core, compositor


class CHERY_OT_render_export_render(bpy.types.Operator):
    """同步导出设置后开始渲染"""

    bl_idname = "chery.render_export_render"
    bl_label = "渲染导出"
    bl_options = {"REGISTER"}

    animation: bpy.props.BoolProperty(name="渲染动画", default=True)

    def execute(self, context):
        scene = context.scene
        err = core.apply_all(scene)
        if err:
            self.report({"WARNING"}, f"辉光未生效：{err}")

        path, perr = core.preview_path(scene)
        if perr:
            self.report({"ERROR"}, f"导出路径无效：{perr}")
            return {"CANCELLED"}

        p = scene.chery_render
        if scene.render.film_transparent and p.output_type == "VIDEO" and not core.video_supports_alpha(p):
            self.report({"WARNING"}, "当前视频编码不支持透明，背景会变成黑色")

        folder = core.output_folder(scene)
        if folder:
            os.makedirs(folder, exist_ok=True)

        if self.animation:
            bpy.ops.render.render("INVOKE_DEFAULT", animation=True)
        else:
            write = p.output_type == "SEQUENCE"
            bpy.ops.render.render("INVOKE_DEFAULT", write_still=write)
        return {"FINISHED"}


class CHERY_OT_render_export_open_dir(bpy.types.Operator):
    """在资源管理器中打开导出目录（不存在时自动创建）"""

    bl_idname = "chery.render_export_open_dir"
    bl_label = "打开导出目录"
    bl_options = {"REGISTER"}

    def execute(self, context):
        scene = context.scene
        core.apply_path(scene)
        folder = core.output_folder(scene)
        if not folder:
            _p, err = core.preview_path(scene)
            self.report({"ERROR"}, f"导出路径无效：{err}")
            return {"CANCELLED"}
        if folder.startswith("//") or not os.path.isabs(folder):
            self.report({"ERROR"}, "相对路径需要先保存 .blend 文件")
            return {"CANCELLED"}
        try:
            os.makedirs(folder, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            self.report({"ERROR"}, f"无法打开目录：{e}")
            return {"CANCELLED"}
        return {"FINISHED"}


class CHERY_OT_render_export_reset_fps(bpy.types.Operator):
    """取消时间伸缩，恢复制作帧率和原始帧范围"""

    bl_idname = "chery.render_export_reset_fps"
    bl_label = "还原制作帧率"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        core.restore_fps(context.scene)
        return {"FINISHED"}


class CHERY_OT_render_export_new_compositor(bpy.types.Operator):
    """新建合成节点树：渲染层 → 辉光 (Bloom) → 输出"""

    bl_idname = "chery.render_export_new_compositor"
    bl_label = "新建合成节点（含辉光）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        compositor.create_compositor_with_glare(scene)
        err = core.apply_glow(scene)
        if err:
            self.report({"WARNING"}, err)
        return {"FINISHED"}


class CHERY_OT_render_export_insert_glow(bpy.types.Operator):
    """把辉光透明节点插入当前合成节点树"""

    bl_idname = "chery.render_export_insert_glow"
    bl_label = "插入辉光透明节点"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        err = compositor.insert_glow_node(context.scene)
        if err:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        return {"FINISHED"}


class CHERY_OT_render_export_use_alpha_codec(bpy.types.Operator):
    """切换到支持透明的 ProRes 4444"""

    bl_idname = "chery.render_export_use_alpha_codec"
    bl_label = "改用 ProRes 4444"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        context.scene.chery_render.video_preset = "PRORES4444_MOV"
        return {"FINISHED"}


classes = (
    CHERY_OT_render_export_render,
    CHERY_OT_render_export_open_dir,
    CHERY_OT_render_export_reset_fps,
    CHERY_OT_render_export_new_compositor,
    CHERY_OT_render_export_insert_glow,
    CHERY_OT_render_export_use_alpha_codec,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
