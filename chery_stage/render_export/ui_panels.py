"""
CheryStage - Render Export: N-panels (3D Viewport & Compositor)
"""

import bpy
from . import core, compositor


# ---------------------------------------------------------------------------
# Draw functions (shared by every editor)
# ---------------------------------------------------------------------------

def draw_main(layout, context):
    scene = context.scene
    p = scene.chery_render
    r = scene.render

    # Summary card
    box = layout.box()
    col = box.column(align=True)
    kind = "视频" if p.output_type == "VIDEO" else "序列帧"
    fmt = (core.VIDEO_PRESETS[p.video_preset][0] if p.output_type == "VIDEO"
           else core.SEQ_PRESETS[p.seq_format][0])
    col.label(text=f"{kind} · {fmt.split('（')[0]}", icon="FILE_MOVIE" if p.output_type == "VIDEO" else "RENDERLAYERS")
    w = r.resolution_x * r.resolution_percentage // 100
    h = r.resolution_y * r.resolution_percentage // 100
    n = scene.frame_end - scene.frame_start + 1
    col.label(text=f"{w}×{h} · {core.fps_label(r.fps, r.fps_base)} fps · {scene.frame_start}–{scene.frame_end}（{n} 帧）", icon="INFO")

    row = layout.row(align=True)
    row.scale_y = 1.4
    row.operator("chery.render_export_render", text="渲染动画", icon="RENDER_ANIMATION").animation = True
    row.operator("chery.render_export_render", text="渲染当前帧", icon="RENDER_STILL").animation = False


def draw_type(layout, context):
    p = context.scene.chery_render
    r = context.scene.render
    layout.row(align=True).prop(p, "output_type", expand=True)
    col = layout.column(align=True)
    if p.output_type == "SEQUENCE":
        col.prop(p, "seq_format", text="格式")
        col.prop(p, "seq_subfolder")
    else:
        col.prop(p, "video_preset", text="编码")
        col.prop(p, "include_audio")
        if r.film_transparent and not core.video_supports_alpha(p):
            warn = layout.box()
            warn.alert = True
            warn.label(text="此编码不支持透明，背景会变黑", icon="ERROR")
            warn.operator("chery.render_export_use_alpha_codec", icon="LOOP_FORWARDS")


def draw_spec(layout, context):
    scene = context.scene
    p = scene.chery_render
    r = scene.render

    # Resolution
    col = layout.column(align=True)
    col.prop(p, "res_preset", text="分辨率")
    row = col.row(align=True)
    row.prop(r, "resolution_x", text="宽")
    row.prop(r, "resolution_y", text="高")
    col.prop(r, "resolution_percentage", text="缩放 %")

    layout.separator()

    # Frame rate
    col = layout.column(align=True)
    if p.is_stretched:
        base_label = core.fps_label(p.base_fps, p.base_fps_base)
    else:
        base_label = core.fps_label(r.fps, r.fps_base)
    col.label(text=f"制作帧率：{base_label} fps", icon="TIME")
    col.prop(p, "out_fps", text="导出帧率")
    if p.out_fps == "CUSTOM":
        col.prop(p, "custom_fps", text="帧率")

    if p.is_stretched:
        box = layout.box()
        c = box.column(align=True)
        c.label(text="原始帧范围（按制作帧率）", icon="ACTION")
        row = c.row(align=True)
        row.prop(p, "base_start", text="起")
        row.prop(p, "base_end", text="止")
        c.separator()
        c.label(text=f"→ 导出范围 {scene.frame_start}–{scene.frame_end}", icon="FORWARD")
        c.label(text=f"时间伸缩 {r.frame_map_old} → {r.frame_map_new}", icon="MOD_TIME")
        box.operator("chery.render_export_reset_fps", icon="LOOP_BACK")
        tip = box.column(align=True)
        tip.scale_y = 0.8
        tip.label(text="关键帧动画会自动插值；烘焙缓存、视频素材", icon="QUESTION")
        tip.label(text="和音频不会跟着伸缩", icon="BLANK1")
    else:
        row = layout.row(align=True)
        row.prop(scene, "frame_start", text="起")
        row.prop(scene, "frame_end", text="止")


def draw_features(layout, context):
    scene = context.scene
    p = scene.chery_render
    r = scene.render

    layout.prop(p, "transparent", icon="IMAGE_RGB_ALPHA")

    col = layout.column(align=True)
    col.active = r.film_transparent
    col.prop(p, "keep_glow", icon="LIGHT_SUN")

    if not (p.keep_glow and r.film_transparent):
        if p.keep_glow and not r.film_transparent:
            layout.label(text="需先开启透明背景", icon="INFO")
        return

    box = layout.box()
    tree = compositor.get_scene_tree(scene)
    node = compositor.find_glow_node(tree)
    if tree is None:
        box.label(text="场景还没有合成节点", icon="ERROR")
        box.operator("chery.render_export_new_compositor", icon="NODETREE")
        return
    if node is None:
        box.label(text="辉光透明节点未插入", icon="ERROR")
        box.operator("chery.render_export_insert_glow", icon="NODE_INSERT_OFF")
        return

    box.label(text="已插入合成器输出前", icon="CHECKMARK")
    c = box.column(align=True)
    c.prop(p, "glow_source", text="来源")
    c.prop(p, "glow_strength", slider=True)
    c.prop(p, "glow_threshold", slider=True)
    c.prop(p, "glow_gamma", slider=True)

    if p.output_type == "SEQUENCE" and p.seq_format.startswith("EXR"):
        tip = box.column(align=True)
        tip.scale_y = 0.8
        tip.label(text="EXR 本身能保留辉光（预乘），后期用", icon="INFO")
        tip.label(text="「相加」叠加时可关闭此功能", icon="BLANK1")


def draw_location(layout, context):
    scene = context.scene
    p = scene.chery_render
    col = layout.column(align=True)
    col.prop(p, "output_dir", text="")
    col.prop(p, "file_name", text="文件名")

    path, err = core.preview_path(scene)
    box = layout.box()
    c = box.column(align=True)
    c.scale_y = 0.8
    if err:
        c.alert = True
        c.label(text=err, icon="ERROR")
    else:
        abs_path = bpy.path.abspath(path)
        c.label(text="输出预览：", icon="FILE_FOLDER")
        # split long paths into readable chunks
        chunk = 42
        for i in range(0, len(abs_path), chunk):
            c.label(text=abs_path[i:i + chunk])
        if path.startswith("//") and not bpy.data.filepath:
            c.alert = True
            c.label(text="文件未保存，相对路径以当前工作目录为准", icon="ERROR")

    row = layout.row()
    row.scale_y = 1.2
    row.operator("chery.render_export_open_dir", icon="FILEBROWSER")


# ---------------------------------------------------------------------------
# Panel classes, generated per editor
# ---------------------------------------------------------------------------

SECTIONS = (
    # suffix, label, icon, draw_fn, default_closed
    ("type", "导出类型", "FILE_MOVIE", draw_type, False),
    ("spec", "导出规格", "PROPERTIES", draw_spec, False),
    ("features", "导出功能", "SHADERFX", draw_features, False),
    ("location", "导出位置", "FILE_FOLDER", draw_location, False),
)

SPACES = (
    # tag, space_type, poll
    ("view3d", "VIEW_3D", None),
    ("compositor", "NODE_EDITOR", lambda ctx: getattr(ctx.space_data, "tree_type", "") == "CompositorNodeTree"),
)


def _make_draw(fn):
    def draw(self, context):
        fn(self.layout, context)
    return draw


def _make_header(icon):
    def draw_header(self, context):
        self.layout.label(icon=icon)
    return draw_header


def _make_poll(fn):
    def poll(cls, context):
        return fn(context)
    return classmethod(poll)


def _make_classes():
    out = []
    for tag, space, poll in SPACES:
        parent_id = f"CHERY_PT_render_export_{tag}"

        def _parent_draw(self, context):
            draw_main(self.layout, context)

        def _parent_header(self, context):
            self.layout.label(icon="RENDER_RESULT")

        attrs = {
            "bl_idname": parent_id,
            "bl_label": "渲染导出 (Render Export)",
            "bl_space_type": space,
            "bl_region_type": "UI",
            "bl_category": "CheryStage",
            "bl_order": 10,
            "bl_options": {"DEFAULT_CLOSED"},
            "draw": _parent_draw,
            "draw_header": _parent_header,
        }
        if poll:
            attrs["poll"] = _make_poll(poll)
        out.append(type(parent_id, (bpy.types.Panel,), attrs))

        for order, (suffix, label, icon, fn, closed) in enumerate(SECTIONS):
            _draw = _make_draw(fn)
            _header = _make_header(icon)
            sub_id = f"{parent_id}_{suffix}"
            sub_attrs = {
                "bl_idname": sub_id,
                "bl_label": label,
                "bl_space_type": space,
                "bl_region_type": "UI",
                "bl_category": "CheryStage",
                "bl_parent_id": parent_id,
                "bl_order": order,
                "draw": _draw,
                "draw_header": _header,
            }
            if closed:
                sub_attrs["bl_options"] = {"DEFAULT_CLOSED"}
            out.append(type(sub_id, (bpy.types.Panel,), sub_attrs))
    return tuple(out)


classes = _make_classes()


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
