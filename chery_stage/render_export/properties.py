"""
CheryStage - Render Export: scene properties
Only settings Blender lacks natively are stored here; resolution etc. are drawn from scene.render directly.
"""

import bpy
from . import core, compositor


# ---------------------------------------------------------------------------
# Update callbacks
# ---------------------------------------------------------------------------

def _upd_format(self, context):
    if core._guard:
        return
    scene = self.id_data
    core.apply_format(scene)
    core.apply_path(scene)


def _upd_path(self, context):
    if core._guard:
        return
    core.apply_path(self.id_data)


def _upd_fps(self, context):
    if core._guard:
        return
    core.apply_fps(self.id_data)


def _upd_glow(self, context):
    if core._guard:
        return
    core.apply_glow(self.id_data)


def _upd_glow_values(self, context):
    if core._guard:
        return
    compositor.sync_glow_values(self.id_data)


# ---------------------------------------------------------------------------
# Get/Set wrappers around native settings
# ---------------------------------------------------------------------------

_RES_KEYS = ["CUSTOM"] + list(core.RES_PRESETS.keys())


def _get_res(self):
    r = self.id_data.render
    for i, key in enumerate(_RES_KEYS):
        if key in core.RES_PRESETS:
            _l, w, h = core.RES_PRESETS[key]
            if r.resolution_x == w and r.resolution_y == h:
                return i
    return 0


def _set_res(self, value):
    key = _RES_KEYS[value]
    if key not in core.RES_PRESETS:
        return
    _l, w, h = core.RES_PRESETS[key]
    r = self.id_data.render
    r.resolution_x = w
    r.resolution_y = h
    r.resolution_percentage = 100


def _get_transparent(self):
    return self.id_data.render.film_transparent


def _set_transparent(self, value):
    scene = self.id_data
    scene.render.film_transparent = value
    core.apply_format(scene)
    core.apply_glow(scene)


# ---------------------------------------------------------------------------
# Property group
# ---------------------------------------------------------------------------

def _fps_items():
    items = [("KEEP", "保持制作帧率", "不做时间伸缩", 0)]
    for i, (key, (label, _f, _b)) in enumerate(core.FPS_PRESETS.items(), start=1):
        items.append((key, f"{label} fps", "", i))
    items.append(("CUSTOM", "自定义", "输入任意帧率", 99))
    return items


class CheryRenderExportProperties(bpy.types.PropertyGroup):
    # ---- 导出类型 ----
    output_type: bpy.props.EnumProperty(
        name="导出类型",
        items=[
            ("VIDEO", "视频", "输出为单个视频文件", "FILE_MOVIE", 0),
            ("SEQUENCE", "序列帧", "每帧输出一张图片", "RENDERLAYERS", 1),
        ],
        default="SEQUENCE",
        update=_upd_format,
    )
    seq_format: bpy.props.EnumProperty(
        name="图片格式",
        items=[(k, v[0], "") for k, v in core.SEQ_PRESETS.items()],
        default="PNG8",
        update=_upd_format,
    )
    seq_subfolder: bpy.props.BoolProperty(
        name="放进同名子文件夹",
        description="序列帧单独放进以文件名命名的子文件夹，避免大量图片散在导出目录",
        default=True,
        update=_upd_path,
    )
    video_preset: bpy.props.EnumProperty(
        name="视频编码",
        items=[(k, v[0], "") for k, v in core.VIDEO_PRESETS.items()],
        default="H264_MP4",
        update=_upd_format,
    )
    include_audio: bpy.props.BoolProperty(
        name="包含音频",
        description="把场景中的声音一起写进视频",
        default=True,
        update=_upd_format,
    )

    # ---- 导出规格 ----
    res_preset: bpy.props.EnumProperty(
        name="分辨率预设",
        items=[("CUSTOM", "自定义", "手动输入宽高", 0)]
        + [(k, v[0], "", i) for i, (k, v) in enumerate(core.RES_PRESETS.items(), start=1)],
        get=_get_res,
        set=_set_res,
    )
    out_fps: bpy.props.EnumProperty(
        name="导出帧率",
        description="修改后自动配置时间伸缩和时间轴范围，关键帧本身不会被改动",
        items=_fps_items(),
        default="KEEP",
        update=_upd_fps,
    )
    custom_fps: bpy.props.FloatProperty(
        name="自定义帧率",
        default=30.0, min=1.0, max=240.0, precision=3,
        update=_upd_fps,
    )

    # time-stretch state (authoring timing)
    is_stretched: bpy.props.BoolProperty(default=False, options={"HIDDEN"})
    base_fps: bpy.props.IntProperty(name="制作帧率", default=24, min=1, max=1000, options={"HIDDEN"})
    base_fps_base: bpy.props.FloatProperty(default=1.0, min=0.0001, options={"HIDDEN"})
    base_start: bpy.props.IntProperty(
        name="原始起始帧", description="按制作帧率计算的起始帧", default=1, update=_upd_fps,
    )
    base_end: bpy.props.IntProperty(
        name="原始结束帧", description="按制作帧率计算的结束帧", default=250, update=_upd_fps,
    )

    # ---- 导出功能 ----
    transparent: bpy.props.BoolProperty(
        name="透明背景",
        description="开启胶片透明并输出 RGBA",
        get=_get_transparent,
        set=_set_transparent,
    )
    keep_glow: bpy.props.BoolProperty(
        name="保留辉光",
        description="在合成器输出前插入节点，根据光晕亮度重建 Alpha，使透明背景下的辉光能保存到 PNG / 视频",
        default=False,
        update=_upd_glow,
    )
    glow_source: bpy.props.EnumProperty(
        name="Alpha 来源",
        items=[
            ("MAX", "最大通道", "用 R/G/B 中最大的值，彩色光晕最饱满"),
            ("LUMA", "亮度", "用感知亮度，彩色光晕更通透"),
        ],
        default="MAX",
        update=_upd_glow_values,
    )
    glow_strength: bpy.props.FloatProperty(
        name="强度", description="光晕不透明度倍数，越大越实", default=1.0, min=0.0, max=10.0, soft_max=3.0,
        update=_upd_glow_values,
    )
    glow_threshold: bpy.props.FloatProperty(
        name="阈值", description="低于此亮度的像素不生成 Alpha，可去除暗部噪点", default=0.0, min=0.0, max=0.95,
        update=_upd_glow_values,
    )
    glow_gamma: bpy.props.FloatProperty(
        name="衰减", description="大于 1 时光晕边缘更快变透明，小于 1 时更柔和扩散", default=1.0, min=0.1, max=5.0,
        update=_upd_glow_values,
    )

    # ---- 导出位置 ----
    output_dir: bpy.props.StringProperty(
        name="导出目录",
        description="// 开头表示相对 .blend 文件所在目录",
        default="//render/",
        subtype="DIR_PATH",
        update=_upd_path,
    )
    file_name: bpy.props.StringProperty(
        name="文件名",
        description="可用变量：{blend_name} {scene_name} {camera_name} {fps} {resolution_x} {resolution_y}",
        default="{blend_name}_{scene_name}",
        update=_upd_path,
    )


def register():
    bpy.utils.register_class(CheryRenderExportProperties)
    bpy.types.Scene.chery_render = bpy.props.PointerProperty(type=CheryRenderExportProperties)


def unregister():
    if hasattr(bpy.types.Scene, "chery_render"):
        del bpy.types.Scene.chery_render
    bpy.utils.unregister_class(CheryRenderExportProperties)
