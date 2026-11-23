# SPDX-License-Identifier: GPL-3.0-or-later
"""CheryStage - MMD Physics Bake module

Runs the bundled mmdbake.exe (saba + Bullet, MMD-style physics) on a PMX model and
a VMD motion, then writes the simulated physics bones of the selected MMD Tools
model as plain keyframes. Blender's own rigid body system is not used.
"""

import os
import struct
import subprocess
import tempfile
import time

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

from . import layers
from . import paths
from . import pose_input
from . import progress_hud

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
EXE_PATH = os.path.join(ADDON_DIR, "bin", "mmdbake.exe")


# --------------------------------------------------------------------------- #
# .mbk reader
# --------------------------------------------------------------------------- #
class BakeData:
    def __init__(self, path):
        with open(path, "rb") as f:
            if f.read(8) != b"MMDBAKE1":
                raise RuntimeError("不是 mmdbake 输出文件")
            version, nb, nf, first = struct.unpack("<IIIi", f.read(16))
            if version != 1:
                raise RuntimeError(f"不支持的 mbk 版本 {version}")
            self.names = []
            self.phys = np.zeros(nb, dtype=np.uint8)
            self.parent = np.zeros(nb, dtype=np.int32)
            self.rest = np.zeros((nb, 3), dtype=np.float64)
            for i in range(nb):
                (n,) = struct.unpack("<I", f.read(4))
                self.names.append(f.read(n).decode("utf-8", "replace"))
                self.phys[i] = f.read(1)[0]
                (self.parent[i],) = struct.unpack("<i", f.read(4))
                self.rest[i] = struct.unpack("<3f", f.read(12))
            data = np.frombuffer(f.read(), dtype=np.float32)
        if data.size != nb * nf * 7:
            raise RuntimeError("mbk 文件不完整")
        self.frames = data.reshape(nf, nb, 7).astype(np.float64)
        self.first_frame = first
        self.index = {n: i for i, n in enumerate(self.names)}


# --------------------------------------------------------------------------- #
# vectorised math helpers
# --------------------------------------------------------------------------- #
def quat_to_mat(q):
    w, x, y, z = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
    m = np.empty(q.shape[:-1] + (3, 3))
    m[..., 0, 0] = 1 - 2 * (y * y + z * z)
    m[..., 0, 1] = 2 * (x * y - z * w)
    m[..., 0, 2] = 2 * (x * z + y * w)
    m[..., 1, 0] = 2 * (x * y + z * w)
    m[..., 1, 1] = 1 - 2 * (x * x + z * z)
    m[..., 1, 2] = 2 * (y * z - x * w)
    m[..., 2, 0] = 2 * (x * z - y * w)
    m[..., 2, 1] = 2 * (y * z + x * w)
    m[..., 2, 2] = 1 - 2 * (x * x + y * y)
    return m


def mat_to_quat(m):
    m00, m01, m02 = m[..., 0, 0], m[..., 0, 1], m[..., 0, 2]
    m10, m11, m12 = m[..., 1, 0], m[..., 1, 1], m[..., 1, 2]
    m20, m21, m22 = m[..., 2, 0], m[..., 2, 1], m[..., 2, 2]
    tr = m00 + m11 + m22

    def safe(v):
        return np.sqrt(np.maximum(v, 1e-12)) * 2.0

    s0 = safe(tr + 1.0)
    q0 = np.stack([0.25 * s0, (m21 - m12) / s0, (m02 - m20) / s0, (m10 - m01) / s0], -1)
    s1 = safe(1.0 + m00 - m11 - m22)
    q1 = np.stack([(m21 - m12) / s1, 0.25 * s1, (m01 + m10) / s1, (m02 + m20) / s1], -1)
    s2 = safe(1.0 + m11 - m00 - m22)
    q2 = np.stack([(m02 - m20) / s2, (m01 + m10) / s2, 0.25 * s2, (m12 + m21) / s2], -1)
    s3 = safe(1.0 + m22 - m00 - m11)
    q3 = np.stack([(m10 - m01) / s3, (m02 + m20) / s3, (m12 + m21) / s3, 0.25 * s3], -1)

    c0 = tr > 0
    c1 = (~c0) & (m00 > m11) & (m00 > m22)
    c2 = (~c0) & (~c1) & (m11 > m22)
    q = np.where(c0[..., None], q0, np.where(c1[..., None], q1, np.where(c2[..., None], q2, q3)))
    return q / np.linalg.norm(q, axis=-1, keepdims=True)


def make_continuous(q):
    """q: (F, K, 4). Flip signs so consecutive frames stay on the same hemisphere."""
    for f in range(1, q.shape[0]):
        d = np.sum(q[f] * q[f - 1], axis=-1)
        q[f][d < 0] *= -1
    return q


def rigid_inv(m):
    """Inverse of rigid transforms (rotation + translation, no scale): much cheaper than np.linalg.inv."""
    out = np.zeros_like(m)
    rt = np.swapaxes(m[..., :3, :3], -1, -2)
    out[..., :3, :3] = rt
    out[..., :3, 3] = -np.einsum("...ij,...j->...i", rt, m[..., :3, 3])
    out[..., 3, 3] = 1.0
    return out


def to_np(mat):
    return np.array([list(row) for row in mat], dtype=np.float64)


# --------------------------------------------------------------------------- #
# action helpers (Blender 4.4+ slotted actions)
# --------------------------------------------------------------------------- #
def get_channelbag(arm):
    from bpy_extras import anim_utils

    ad = arm.animation_data or arm.animation_data_create()
    action = ad.action
    if action is None:
        action = bpy.data.actions.new(arm.name)
        ad.action = action
    slot = ad.action_slot
    if slot is None:
        slot = action.slots.new(id_type="OBJECT", name=arm.name)
        ad.action_slot = slot
    return anim_utils.action_ensure_channelbag_for_slot(action, slot)


def write_curve(cb, data_path, index, group, xs, ys):
    fc = cb.fcurves.find(data_path, index=index)
    if fc is not None:
        cb.fcurves.remove(fc)
    fc = cb.fcurves.new(data_path, index=index)
    fc.group = group
    n = len(xs)
    fc.keyframe_points.add(n)
    co = np.empty(n * 2, dtype=np.float32)
    co[0::2] = xs
    co[1::2] = ys
    fc.keyframe_points.foreach_set("co", co)
    # Keys are left as Bezier (Blender's default): with one key per frame the curve passes
    # through every sample, and sub-frames (time-stretched 60fps renders) come out smoother
    # than linear. Skipping the per-key interpolation pass also saves ~15% write time.
    fc.update()


# --------------------------------------------------------------------------- #
# model helpers
# --------------------------------------------------------------------------- #
def find_mmd_root(obj):
    while obj is not None and getattr(obj, "mmd_type", "") != "ROOT":
        obj = obj.parent
    return obj


def scene_models(scene):
    """(root, armature) of every MMD Tools model in the scene."""
    out = []
    for ob in scene.objects:
        if getattr(ob, "mmd_type", "") != "ROOT":
            continue
        arm = next((c for c in ob.children if c.type == "ARMATURE"), None)
        if arm is not None:
            out.append((ob, arm))
    return out


def find_armature(context):
    obj = context.active_object
    if obj is None:
        return None, None
    if obj.type == "ARMATURE":
        return find_mmd_root(obj), obj
    root = find_mmd_root(obj)
    if root is not None:
        for c in root.children:
            if c.type == "ARMATURE":
                return root, c
    if obj.parent is not None and obj.parent.type == "ARMATURE":
        return find_mmd_root(obj.parent), obj.parent
    return None, None


def mmd_name(pb):
    mb = getattr(pb, "mmd_bone", None)
    nj = getattr(mb, "name_j", "") if mb is not None else ""
    return nj or pb.name


def detect_frame_offset(arm, scene):
    ad = arm.animation_data
    action = ad.action if ad else None
    if action is not None:
        try:
            lo, _ = action.frame_range
            return int(round(lo))
        except Exception:
            pass
    return scene.frame_current


# --------------------------------------------------------------------------- #
# core: apply baked data
# --------------------------------------------------------------------------- #
def apply_bake(arm, data, frame_offset, report):
    pbones = arm.pose.bones
    bones = arm.data.bones

    # map Blender bones -> mbk indices
    bl2idx = {}
    for pb in pbones:
        i = data.index.get(mmd_name(pb))
        if i is None:
            i = data.index.get(pb.name)
        if i is not None:
            bl2idx[pb.name] = i
    if not bl2idx:
        raise RuntimeError("骨骼名一个都对不上，PMX 是不是选错了？")

    # scale (Blender units per MMD unit), least squares on rest heads
    names = list(bl2idx.keys())
    b = np.array([tuple(bones[n].head_local) for n in names])
    m = data.rest[[bl2idx[n] for n in names]]
    bc, mc = b - b.mean(0), m - m.mean(0)
    denom = float(np.sum(mc * mc))
    scale = float(np.sum(bc * mc) / denom) if denom > 0 else 0.08
    resid = float(np.sqrt(np.mean(np.sum((bc - mc * scale) ** 2, axis=1)))) / max(abs(scale), 1e-9)
    if scale <= 0 or resid > 0.5:
        report({"WARNING"}, f"模型和 PMX 的骨骼位置不太吻合（比例 {scale:.4f}，误差 {resid:.2f}），结果可能不对")

    targets = [n for n in names if data.phys[bl2idx[n]] != 0]
    if not targets:
        raise RuntimeError("这个模型没有物理骨骼")

    nf = data.frames.shape[0]
    xs = (np.arange(nf) + data.first_frame + frame_offset).astype(np.float32)

    # bones whose pose we need: targets + their Blender parents
    need = set(targets)
    for n in targets:
        p = bones[n].parent
        if p is not None:
            need.add(p.name)
    need = sorted(need)
    col = {n: k for k, n in enumerate(need)}

    rest = np.stack([to_np(bones[n].matrix_local) for n in need])  # (K,4,4)
    pose = np.empty((nf, len(need), 4, 4))
    for k, n in enumerate(need):
        if k % 25 == 0:
            yield ("prep", k, len(need) * 2)
        i = bl2idx.get(n)
        if i is None:
            pose[:, k] = rest[k]  # parent not in PMX: assume rest pose
            continue
        S = np.zeros((nf, 4, 4))
        S[:, :3, :3] = quat_to_mat(data.frames[:, i, 0:4])
        S[:, :3, 3] = data.frames[:, i, 4:7] * scale
        S[:, 3, 3] = 1.0
        pose[:, k] = S @ rest[k]

    tk = np.array([col[n] for n in targets])
    pose_t = pose[:, tk]  # (F,T,4,4)
    basis = np.empty_like(pose_t)
    rest_inv = rigid_inv(rest)
    pose_inv = {}
    for j, n in enumerate(targets):
        if j % 25 == 0:
            yield ("prep", len(need) + j * len(need) // max(1, len(targets)), len(need) * 2)
        p = bones[n].parent
        if p is None:
            basis[:, j] = rest_inv[col[n]] @ pose_t[:, j]
        else:
            pk = col[p.name]
            if pk not in pose_inv:
                pose_inv[pk] = rigid_inv(pose[:, pk])
            rel_inv = rest_inv[col[n]] @ rest[pk]          # (rest_p^-1 rest_n)^-1
            basis[:, j] = rel_inv @ pose_inv[pk] @ pose_t[:, j]
    del pose, pose_inv

    loc = basis[..., :3, 3]
    quat = make_continuous(mat_to_quat(basis[..., :3, :3]))
    del basis


    # physics goes to its own NLA layer; the motion action is never modified
    act, slot, cb = layers.prepare_physics_action(arm)
    for j, n in enumerate(targets):
        if j % 4 == 0:
            yield ("write", j, len(targets))
        pb = pbones[n]
        if pb.rotation_mode != "QUATERNION":
            pb.rotation_mode = "QUATERNION"
        grp = cb.groups.get(n) if hasattr(cb.groups, "get") else None
        if grp is None:
            grp = next((g for g in cb.groups if g.name == n), None) or cb.groups.new(n)
        # drop stale euler/axis-angle curves for this bone
        for dp in (f'pose.bones["{n}"].rotation_euler', f'pose.bones["{n}"].rotation_axis_angle'):
            for ax in range(4):
                fc = cb.fcurves.find(dp, index=ax)
                if fc is not None:
                    cb.fcurves.remove(fc)
        for ax in range(3):
            write_curve(cb, f'pose.bones["{n}"].location', ax, grp, xs, loc[:, j, ax])
        for ax in range(4):
            write_curve(cb, f'pose.bones["{n}"].rotation_quaternion', ax, grp, xs, quat[:, j, ax])

    muted = layers.finish_physics_layer(arm, act, slot, set(targets))
    yield ("done", (len(targets), scale, int(xs[0]), int(xs[-1]), muted), None)


# --------------------------------------------------------------------------- #
# UI / operator
# --------------------------------------------------------------------------- #
class CheryMMDBakeSettings(bpy.types.PropertyGroup):
    tab: EnumProperty(
        name="页签",
        items=[("SINGLE", "单人", "单个模型的物理烘焙", "USER", 0),
               ("SCENE", "场景", "多个模型、场景碰撞体一起烘焙", "SCENE_DATA", 1),
               ("CONFIG", "配置", "物理参数与高级设置", "PREFERENCES", 2)],
        default="SINGLE")
    inter_model: BoolProperty(name="模型之间碰撞", default=False,
                              description="不同模型的刚体互相碰撞（双人舞身体贴近时可能把头发、裙子挤爆，默认关闭）")
    use_ground: BoolProperty(name="默认地面", default=True,
                             description="在 Blender 世界 Z=0 处放一个无限大的地面")
    source: EnumProperty(
        name="动作来源",
        items=[("BLENDER", "Blender 动作", "按 Blender 里当前的动作烘焙（包括手 K 的修改、IK、约束）"),
               ("VMD", "VMD 文件", "按原始 VMD 文件烘焙（更快，但看不到 Blender 里的修改）")],
        default="BLENDER")
    auto_offset: BoolProperty(name="自动对齐帧", default=True,
                              description="按骨架现有动作的起始帧对齐；没有动作时用当前帧")
    frame_offset: IntProperty(name="帧偏移", default=0,
                              description="Blender 帧 = VMD 帧 + 偏移")
    warmup: IntProperty(name="预热过渡帧", default=60, min=1, max=600,
                        description="从初始姿势过渡到第一帧姿势所用的帧数")
    settle: IntProperty(name="预热静置帧", default=60, min=0, max=600,
                        description="过渡后保持第一帧姿势让布料稳定的帧数")
    fps: FloatProperty(name="物理步频", default=120.0, min=30.0, max=1000.0,
                       description="物理内部每秒步数，越高越稳越慢")
    substeps: IntProperty(name="最大子步", default=10, min=1, max=100)
    threads: IntProperty(name="物理线程数", default=0, min=0, max=64,
                         description="0 = 自动（最多 8 线程，更多反而更慢）；1 = 单线程，结果和旧版完全一致")
    use_range: BoolProperty(name="指定帧范围", default=False,
                            description="Blender 动作：默认用场景帧范围；VMD：默认用整个 VMD")
    start: IntProperty(name="起始帧", default=0, min=0)
    end: IntProperty(name="结束帧", default=1000, min=0)
    clean_rig: BoolProperty(name="清除 MMD Tools 物理", default=True,
                            description="若模型已 Build 物理，先 Clean，否则刚体会覆盖烘焙结果")
    keep_mbk: BoolProperty(name="保留中间文件", default=False)


class _Job:
    """State of the running bake (one at a time)."""
    running = False
    factor = 0.0
    text = ""
    last = None   # timings of the last finished bake, shown in the panel


def _fmt_s(sec):
    return f"{sec:.1f} s" if sec < 60 else f"{int(sec // 60)} 分 {sec % 60:.0f} 秒"


def _redraw_ui():
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type in {"VIEW_3D", "STATUSBAR"}:
                area.tag_redraw()


class CHERY_OT_mmd_bake(bpy.types.Operator):
    bl_idname = "chery.mmd_bake"
    bl_label = "烘焙物理"
    bl_description = "用外置 MMD 物理引擎烘焙，并把物理骨骼写成关键帧（Esc 取消）"
    bl_options = {"REGISTER", "UNDO"}

    scene_mode: BoolProperty(name="场景模式", default=False, options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        if _Job.running:
            return False
        return find_armature(context)[1] is not None or bool(scene_models(context.scene))

    # ---- progress helpers ----
    def _set(self, context, factor, text, phase=None, pv=0.0):
        """factor: overall 0..1 (status bar / cursor); phase + pv: HUD phase and its 0..1 progress."""
        _Job.factor = max(0.0, min(1.0, factor))
        _Job.text = text
        try:
            context.workspace.status_text_set(f"MMD 物理烘焙：{text}（{_Job.factor * 100:.0f}%）  Esc 取消")
        except Exception:
            pass
        context.window_manager.progress_update(int(_Job.factor * 1000))
        if phase is not None:
            progress_hud.set_phase(phase, text)
            progress_hud.update(pv, text)
        _redraw_ui()

    def _finish(self, context, cancelled=False, msg=None):
        progress_hud.finish(ok=not cancelled, text=msg or ("已取消" if cancelled else "完成"))
        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
        wm.progress_end()
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass
        if self._sampler is not None:
            self._sampler.close()   # restores modifiers / rigid body world / current frame
            self._sampler = None
        if not self._settings.keep_mbk:
            for f in [self._out, self._pose] + list(self._tmp):
                if f and os.path.isfile(f):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
        _Job.running = False
        _Job.text = ""
        _redraw_ui()
        return {"CANCELLED"} if cancelled else {"FINISHED"}

    def _reader(self):
        for raw in iter(self._proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                if self._t_bake_start is None and line.startswith("PROGRESS "):
                    self._t_bake_start = time.time()   # warm-up done, per-frame physics starts
                self._lines.append(line)

    # ---- start ----
    def invoke(self, context, event):
        return self.execute(context)

    def _clean_rig(self, context, root):
        if self._settings.clean_rig and root is not None and getattr(root.mmd_root, "is_built", False):
            try:
                with context.temp_override(active_object=root, object=root,
                                           selected_objects=[root], selected_editable_objects=[root]):
                    bpy.ops.mmd_tools.clean_rig()
            except Exception as e:
                self.report({"WARNING"}, f"自动 Clean 物理失败，请手动点 MMD Tools 的 Clean：{e}")

    def _common_args(self, s):
        args = ["--warmup", str(s.warmup), "--settle", str(s.settle),
                "--fps", str(s.fps), "--substeps", str(s.substeps)]
        if s.threads > 0:
            args += ["--threads", str(s.threads)]
        return args

    def _execute_scene(self, context):
        s, scene = self._settings, context.scene
        models = [(r, a) for r, a in scene_models(scene) if r.mmd_bake_include]
        if not models:
            self.report({"ERROR"}, "场景里没有勾选参与烘焙的 MMD 模型")
            return None
        pmxs = []
        for root, arm in models:
            pmx, _ = paths.autofill(root, arm)
            pmx = bpy.path.abspath(pmx) if pmx else ""
            if not os.path.isfile(pmx):
                self.report({"ERROR"}, f"{root.name}：没找到 PMX 模型文件，请在单人页签里手动选择")
                return None
            pmxs.append(pmx)
        for root, _arm in models:
            self._clean_rig(context, root)
        f0, f1 = (s.start, max(s.start, s.end)) if s.use_range else (scene.frame_start, scene.frame_end)
        tmp = tempfile.gettempdir()
        stamp = int(time.time())
        poses = [os.path.join(tmp, f"mmdbake_{stamp}_{k}.mbp") for k in range(len(models))]
        outs = [os.path.join(tmp, f"mmdbake_{stamp}_{k}.mbk") for k in range(len(models))]
        colliders = pose_input.scene_colliders(scene)
        coll = os.path.join(tmp, f"mmdbake_{stamp}.mbc") if colliders else None
        spec = os.path.join(tmp, f"mmdbake_{stamp}_scene.txt")
        lines = [f"model\t{pm}\t{po}\t{ou}" for pm, po, ou in zip(pmxs, poses, outs)]
        if coll:
            lines.append(f"colliders\t{coll}")
        lines.append(f"inter_model\t{1 if s.inter_model else 0}")
        lines.append(f"ground\t{1 if s.use_ground else 0}")
        with open(spec, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        scene_fps = scene.render.fps / (scene.render.fps_base or 1.0)
        self._args = [EXE_PATH, "--scene", spec] + self._common_args(s) + [
            "--dt", repr(1.0 / scene_fps), "--stream", str(os.getpid())]
        self._tmp = poses + outs + [spec] + ([coll] if coll else [])
        self._scene_arms = [a for _r, a in models]
        self._scene_outs = outs
        self._pose = poses[0]          # "from Blender" flag for offsets / timing
        self._sampler = pose_input.sample_scene(scene, self._scene_arms, colliders, f0, f1, poses, coll)
        self._sample_total = f1 - f0 + 1
        self.report({"INFO"}, f"场景烘焙：{len(models)} 个模型，{len(colliders)} 个碰撞体")
        return True

    def _apply_scene(self):
        """Write every model's result into its own physics layer (one generator, progress across models)."""
        n = len(self._scene_arms)
        bones = muted = 0
        scale = f0 = f1 = 0
        for k, (arm, out) in enumerate(zip(self._scene_arms, self._scene_outs)):
            for kind, a, b in apply_bake(arm, BakeData(out), 0, self.report):
                if kind == "done":
                    nb_, scale, f0, f1, m_ = a
                    bones += nb_
                    muted += m_
                else:
                    yield (kind, k * max(1, b) + a, n * max(1, b))
        yield ("done", (bones, scale, f0, f1, muted), None)

    def execute(self, context):
        self._timer = None
        self._proc = None
        self._out = None
        self._pose = None
        self._sampler = None
        self._tmp = []
        self._scene_arms = None
        s = self._settings = context.scene.chery_mmd_bake
        if self.scene_mode:
            if not os.path.isfile(EXE_PATH):
                self.report({"ERROR"}, f"找不到 {EXE_PATH}")
                return {"CANCELLED"}
            if self._execute_scene(context) is None:
                return {"CANCELLED"}
            self._root = self._arm = None
            return self._start_modal(context, from_blender=True)
        self._root, self._arm = find_armature(context)
        pmx, vmd = paths.autofill(self._root, self._arm)
        pmx = bpy.path.abspath(pmx) if pmx else ""
        vmd = bpy.path.abspath(vmd) if vmd else ""
        from_blender = s.source == "BLENDER"
        if not os.path.isfile(EXE_PATH):
            self.report({"ERROR"}, f"找不到 {EXE_PATH}")
            return {"CANCELLED"}
        if not os.path.isfile(pmx):
            self.report({"ERROR"}, "没找到 PMX 模型文件，请在面板里手动选择")
            return {"CANCELLED"}
        if not from_blender and not os.path.isfile(vmd):
            self.report({"ERROR"}, "没找到 VMD 动作文件，请在面板里手动选择")
            return {"CANCELLED"}

        stamp = int(time.time())
        self._out = os.path.join(tempfile.gettempdir(), f"mmdbake_{stamp}.mbk")
        args = [EXE_PATH, pmx, "-" if from_blender else vmd, self._out,
                "--warmup", str(s.warmup), "--settle", str(s.settle),
                "--fps", str(s.fps), "--substeps", str(s.substeps)]
        if s.threads > 0:
            args += ["--threads", str(s.threads)]
        scene = context.scene
        if from_blender:
            # MMD Tools rigid bodies would fight the sampled pose: clean them first
            self._clean_rig(context, self._root)
            f0, f1 = (s.start, max(s.start, s.end)) if s.use_range else (scene.frame_start, scene.frame_end)
            self._pose = os.path.join(tempfile.gettempdir(), f"mmdbake_{stamp}.mbp")
            scene_fps = scene.render.fps / (scene.render.fps_base or 1.0)
            # --stream: mmdbake starts while the poses are still being read and follows the file
            args += ["--pose", self._pose, "--dt", repr(1.0 / scene_fps), "--stream", str(os.getpid())]
            self._sampler = pose_input.sample_pose(scene, self._arm, f0, f1, self._pose)
            self._sample_total = f1 - f0 + 1
        elif s.use_range:
            args += ["--start", str(s.start), "--end", str(max(s.start, s.end))]
        self._args = args
        return self._start_modal(context, from_blender)

    def _start_modal(self, context, from_blender):
        self._lines = []
        self._t0 = time.time()
        self._t_sampled = self._t0
        self._t_proc = self._t0
        self._t_bake_start = None
        self._t_sim_end = None
        self._t_sim = 0.0
        self._gen = None
        self._phase = "sample" if from_blender else "sim"
        _Job.running = True
        progress_hud.start(stream=from_blender)
        if not from_blender and not self._start_proc():
            return self._finish(context, cancelled=True, msg="无法运行烘焙程序")

        wm = context.window_manager
        wm.progress_begin(0, 1000)
        self._timer = wm.event_timer_add(0.01, window=context.window)  # no idle gaps between work slices
        wm.modal_handler_add(self)
        self._set(context, 0.0, "启动中")
        return {"RUNNING_MODAL"}

    def _start_proc(self):
        import threading

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._t_proc = time.time()
        try:
            self._proc = subprocess.Popen(self._args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          creationflags=flags)
        except OSError as e:
            self.report({"ERROR"}, f"无法运行 mmdbake.exe：{e}")
            return False
        threading.Thread(target=self._reader, daemon=True).start()
        return True

    # ---- loop ----
    def modal(self, context, event):
        if event.type == "ESC":
            self.report({"WARNING"}, "已取消物理烘焙")
            return self._finish(context, cancelled=True, msg="已取消")
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        try:
            if self._phase == "sample":
                return self._tick_sample(context)
            if self._phase == "sim":
                return self._tick_sim(context)
            return self._tick_write(context)
        except Exception as e:
            self.report({"ERROR"}, f"写入关键帧失败：{e}")
            return self._finish(context, cancelled=True, msg="写入关键帧失败")

    # reading the action from Blender (Blender source only). mmdbake starts after the first chunk
    # is on disk and simulates while the rest is read (pipelined).
    def _tick_sample(self, context):
        if self._proc is not None and self._proc.poll() not in (None, 0):
            return self._tick_sim(context)   # baker failed early: report it
        deadline = time.time() + 0.05
        while time.time() < deadline:
            kind, a, b = next(self._sampler)
            if self._proc is None and not self._start_proc():
                return self._finish(context, cancelled=True, msg="无法运行烘焙程序")
            if kind == "sample":
                self._stream_progress(context, a, b)
            elif kind == "sampled":
                self._sampler = None
                self._t_sampled = time.time()
                self._phase = "sim"
                return {"RUNNING_MODAL"}
        return {"RUNNING_MODAL"}

    def _phys_state(self):
        """(frames simulated, frames total, (warm-up done, total) or None) from mmdbake's output."""
        for line in reversed(self._lines[-8:]):
            if line.startswith("PROGRESS "):
                a, b = line.split()[1].split("/")
                return int(a), int(b), None
            if line.startswith("WARMUP "):
                a, b = line.split()[1].split("/")
                return 0, 0, (int(a), int(b))
        return 0, 0, None

    def _stream_progress(self, context, read, total):
        """Blender source: reading and physics run together (buffer-bar HUD)."""
        done, _n, warm = self._phys_state()
        elapsed = time.time() - self._t0
        if read < total:
            text = f"读取动作并计算物理  {elapsed:.0f}s"
        elif warm:
            text = f"读取完成 · 物理预热中  {elapsed:.0f}s"
        else:
            text = f"读取完成 · 物理计算中  {elapsed:.0f}s"
        progress_hud.stream_update(read, done, total, warm=warm, text=text)
        pv = done / max(1, total)
        self._set(context, 0.70 * pv, f"已读取 {read}/{total} · 已计算 {done}/{total} 帧")

    # physics simulation (external process): 0% .. 70%
    def _tick_sim(self, context):
        last = self._lines[-1] if self._lines else ""
        elapsed = time.time() - self._t0
        if self._pose:
            self._stream_progress(context, self._sample_total, self._sample_total)
        elif last.startswith("WARMUP "):
            a, b = last.split()[1].split("/")
            base = 0.5 if self._pose else 0.0
            self._set(context, 0.05 * int(a) / max(1, int(b)), f"物理预热 {a}/{b} 帧  {elapsed:.0f}s",
                      0, base + (1 - base) * int(a) / max(1, int(b)))
        elif last.startswith("PROGRESS "):
            a, b = last.split()[1].split("/")
            self._set(context, 0.05 + 0.65 * int(a) / max(1, int(b)), f"物理计算 {a}/{b} 帧  {elapsed:.0f}s",
                      1, int(a) / max(1, int(b)))
        else:
            self._set(context, _Job.factor, f"加载模型和动作  {elapsed:.0f}s", 0, 0.5 if self._pose else 0.0)

        rc = self._proc.poll()
        if rc is None:
            return {"RUNNING_MODAL"}
        time.sleep(0.05)  # let the reader thread drain
        outs = self._scene_outs if self._scene_arms else [self._out]
        if rc != 0 or not all(os.path.isfile(o) for o in outs):
            msg = " | ".join(l for l in self._lines[-6:] if not l.startswith(("PROGRESS", "WARMUP")))
            self.report({"ERROR"}, f"物理烘焙失败（代码 {rc}）：{msg[-400:]}")
            return self._finish(context, cancelled=True, msg="物理计算失败")
        self._t_sim_end = time.time()
        self._t_sim = self._t_sim_end - self._t0

        s, root, arm = self._settings, self._root, self._arm
        if self._scene_arms:
            self._set(context, 0.70, "读取烘焙结果", 2, 0.0)
            self._gen = self._apply_scene()
            self._phase = "write"
            return {"RUNNING_MODAL"}
        if s.clean_rig and root is not None and getattr(root.mmd_root, "is_built", False):
            self._set(context, 0.70, "清除 MMD Tools 物理", 2, 0.0)
            try:
                with context.temp_override(active_object=root, object=root,
                                           selected_objects=[root], selected_editable_objects=[root]):
                    bpy.ops.mmd_tools.clean_rig()
            except Exception as e:
                self.report({"WARNING"}, f"自动 Clean 物理失败，请手动点 MMD Tools 的 Clean：{e}")

        self._set(context, 0.70, "读取烘焙结果", 2, 0.0)
        data = BakeData(self._out)
        if self._pose:
            self._offset = 0
        else:
            self._offset = detect_frame_offset(arm, context.scene) if s.auto_offset else s.frame_offset
        self._gen = apply_bake(arm, data, self._offset, self.report)
        self._phase = "write"
        return {"RUNNING_MODAL"}

    # keyframe writing inside Blender: 70% .. 100%, time-sliced so the UI stays alive
    def _tick_write(self, context):
        deadline = time.time() + 0.05  # ~20 fps HUD while writing, barely any idle time
        while time.time() < deadline:
            kind, a, b = next(self._gen)
            if kind == "prep":
                self._set(context, 0.70 + 0.10 * a / max(1, b), "计算骨骼姿势", 2, 0.25 * a / max(1, b))
            elif kind == "write":
                self._set(context, 0.80 + 0.20 * a / max(1, b), f"写入关键帧 {a}/{b} 根骨骼",
                          2, 0.25 + 0.75 * a / max(1, b))
            elif kind == "done":
                n, scale, f0, f1, muted = a
                if muted:
                    self.report({"INFO"}, f"原动作里有 {muted} 条物理骨骼的通道，已静音（删除物理层时会恢复）")
                s = self._settings
                if s.auto_offset and not self._pose:
                    s.frame_offset = self._offset
                context.view_layer.update()
                self._report_timing(n, f1 - f0 + 1, f0, f1, scale)
                return self._finish(context, msg=f"完成 · {n} 根物理骨骼 · {time.time() - self._t0:.1f}s")
        return {"RUNNING_MODAL"}

    def _report_timing(self, bones, frames, f0, f1, scale):
        t_end = time.time()
        bake_start = self._t_bake_start or self._t_sim_end
        sample = self._t_sampled - self._t0
        prep = bake_start - self._t_proc
        bake = self._t_sim_end - bake_start
        imp = t_end - self._t_sim_end
        total = t_end - self._t0
        _Job.last = dict(sample=sample, prep=prep, bake=bake, imp=imp, total=total, frames=frames, bones=bones,
                         source="Blender 动作" if self._pose else "VMD")
        lines = [
            "[CheryStage] MMD 物理烘焙完成（动作来源：" + _Job.last["source"] + "）",
        ] + ([f"  读取动作：{_fmt_s(sample)}（从 Blender 逐帧读取 {frames} 帧姿势，与物理计算同时进行）"]
             if self._pose else []) + [
            f"  准备阶段：{_fmt_s(prep)}（加载模型与动作、物理预热）",
            f"  烘焙阶段：{_fmt_s(bake)}（{frames} 帧物理计算" + ("，含等待读取动作）" if self._pose else "）"),
            f"  导入阶段：{_fmt_s(imp)}（写入 {bones} 根物理骨骼的关键帧）",
            f"  总计：{_fmt_s(total)}　Blender 帧 {f0}-{f1}，比例 {scale:.4f}",
        ]
        print("\n".join(lines))                       # system console
        for line in lines[1:]:                         # Info editor log
            self.report({"INFO"}, line.strip())
        # last report wins the status bar: keep it a one-line summary
        read = f"读取 {_fmt_s(sample)} · " if self._pose else ""
        self.report({"INFO"}, f"烘焙完成：{read}准备 {_fmt_s(prep)} · 烘焙 {_fmt_s(bake)} · 导入 {_fmt_s(imp)} · 总计 {_fmt_s(total)}")

    def cancel(self, context):
        self._finish(context, cancelled=True)


class CHERY_PT_mmd_bake(bpy.types.Panel):
    bl_label = "MMD 物理烘焙 (Physics Bake)"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "CheryStage"
    bl_order = 5
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.label(icon="PHYSICS")

    def draw(self, context):
        s = context.scene.chery_mmd_bake
        lay = self.layout
        lay.row().prop(s, "tab", expand=True)
        if _Job.running:
            shown = progress_hud.current() if progress_hud.is_active() else _Job.factor
            col = lay.column()
            if hasattr(col, "progress"):
                col.progress(factor=shown, type="BAR", text=f"{progress_hud.phase_label()} · {_Job.text}  {shown * 100:.0f}%")
            else:
                col.label(text=f"{_Job.text}  {shown * 100:.0f}%", icon="TIME")
            col.label(text="按 Esc 取消", icon="CANCEL")
            return
        if s.tab == "SINGLE":
            self._draw_single(context, lay, s)
        elif s.tab == "SCENE":
            self._draw_scene(context, lay, s)
        else:
            self._draw_config(lay, s)

    @staticmethod
    def _draw_range(context, lay, s, allow_offset):
        box = lay.box()
        if allow_offset and s.source == "VMD":
            row = box.row()
            row.prop(s, "auto_offset")
            sub = row.row()
            sub.enabled = not s.auto_offset
            sub.prop(s, "frame_offset")
        elif not s.use_range:
            box.label(text=f"帧范围：场景 {context.scene.frame_start} – {context.scene.frame_end}", icon="TIME")
        box.prop(s, "use_range")
        if s.use_range:
            r = box.row(align=True)
            r.prop(s, "start")
            r.prop(s, "end")

    def _draw_single(self, context, lay, s):
        root, arm = find_armature(context)
        if arm is None:
            lay.label(text="先选中 MMD 模型（根/骨架/网格均可）", icon="INFO")
            return
        lay.label(text=f"骨架：{arm.name}", icon="ARMATURE_DATA")
        if root is not None and getattr(root.mmd_root, "is_built", False):
            lay.label(text="模型已 Build 物理，烘焙时会自动 Clean", icon="ERROR")
        owner = paths.model_owner(root, arm)
        if not owner.mmd_bake_pmx or not owner.mmd_bake_vmd:
            ad = arm.animation_data
            act = ad.action.name if ad and ad.action else ""
            _schedule_autofill(owner.name, act)
        lay.prop(s, "source", expand=True)
        col = lay.column(align=True)
        col.prop(owner, "mmd_bake_pmx")
        if s.source == "VMD":
            col.prop(owner, "mmd_bake_vmd")
            if not owner.mmd_bake_vmd:
                col.label(text="没检测到 VMD：先用 MMD Tools 导入动作，或手动选择", icon="INFO")
        col.operator(CHERY_OT_mmd_detect_paths.bl_idname, icon="VIEWZOOM")
        self._draw_range(context, lay, s, True)
        lay.operator(CHERY_OT_mmd_bake.bl_idname, icon="PHYSICS").scene_mode = False
        _draw_layers(lay, arm)

    def _draw_scene(self, context, lay, s):
        scene = context.scene
        models = scene_models(scene)
        box = lay.box()
        box.label(text=f"模型（{len(models)}）", icon="ARMATURE_DATA")
        if not models:
            box.label(text="场景里没有 MMD 模型")
        for root, arm in models:
            row = box.row(align=True)
            row.prop(root, "mmd_bake_include", text="")
            pmx = bpy.path.abspath(root.mmd_bake_pmx) if root.mmd_bake_pmx else ""
            ok = bool(pmx) and os.path.isfile(pmx)
            row.label(text=root.name, icon="CHECKMARK" if ok else "ERROR")
            tr = layers.find_track(arm)
            if tr is not None:
                op = row.operator(CHERY_OT_mmd_layer_toggle.bl_idname, text="", emboss=False,
                                  icon="PHYSICS" if not tr.mute else "HIDE_ON")
                op.arm_name = arm.name
            if not ok:
                _schedule_autofill(root.name, "")
        cols = pose_input.scene_colliders(scene)
        box = lay.box()
        box.label(text=f"碰撞体（{len(cols)}）", icon="MESH_CUBE")
        if not cols:
            box.label(text="把物体的刚体类型设为「被动」即可作为碰撞体", icon="INFO")
        for ob in cols[:12]:
            box.label(text=f"{ob.name}　{ob.rigid_body.collision_shape}")
        if len(cols) > 12:
            box.label(text=f"…… 还有 {len(cols) - 12} 个")
        box = lay.box()
        box.prop(s, "inter_model")
        box.prop(s, "use_ground")
        self._draw_range(context, lay, s, False)
        op = lay.operator(CHERY_OT_mmd_bake.bl_idname, text="烘焙全部", icon="PHYSICS")
        op.scene_mode = True
        lay.label(text="使用 Blender 动作；每个模型的结果写入各自的物理层", icon="INFO")

    @staticmethod
    def _draw_config(lay, s):
        box = lay.box()
        box.label(text="物理参数", icon="PHYSICS")
        box.prop(s, "warmup")
        box.prop(s, "settle")
        box.prop(s, "fps")
        box.prop(s, "substeps")
        box.prop(s, "threads")
        box = lay.box()
        box.label(text="其他", icon="PREFERENCES")
        box.prop(s, "clean_rig")
        box.prop(s, "keep_mbk")

def _draw_layers(lay, arm):
    box = lay.box()
    box.label(text="动画层", icon="NLA")
    tr = layers.find_track(arm)
    row = box.row(align=True)
    if tr is None:
        row.label(text="物理层：尚未烘焙", icon="PHYSICS")
    else:
        on = not tr.mute
        op = row.operator(CHERY_OT_mmd_layer_toggle.bl_idname, text="", icon="CHECKBOX_HLT" if on else "CHECKBOX_DEHLT", emboss=False)
        act = layers.physics_action(arm)
        row.label(text=f"物理层  {act.name if act else ''}", icon="PHYSICS")
        row.operator(CHERY_OT_mmd_layer_delete.bl_idname, text="", icon="TRASH")
    row = box.row(align=True)
    mot = layers.motion_action(arm)
    row.label(text="", icon="CHECKBOX_HLT")
    row.label(text=f"动作层  {mot.name if mot else '（无）'}", icon="ACTION")
    n = layers.muted_count(arm)
    if n and tr is not None and not tr.mute:
        box.label(text=f"动作层里 {n} 条物理骨骼通道已静音", icon="INFO")


class CHERY_OT_mmd_layer_toggle(bpy.types.Operator):
    bl_idname = "chery.mmd_layer_toggle"
    bl_label = "开关物理层"
    bl_description = "打开或关闭物理层（关闭后裙子等物理骨骼回到原动作 / 静止姿势）"
    bl_options = {"REGISTER", "UNDO"}

    arm_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        arm = bpy.data.objects.get(self.arm_name) if self.arm_name else find_armature(context)[1]
        tr = layers.find_track(arm) if arm is not None else None
        if tr is None:
            return {"CANCELLED"}
        layers.set_physics_enabled(arm, tr.mute)
        return {"FINISHED"}


class CHERY_OT_mmd_layer_delete(bpy.types.Operator):
    bl_idname = "chery.mmd_layer_delete"
    bl_label = "删除物理层"
    bl_description = "删除物理层（NLA 轨道和烘焙的物理动作），并恢复原动作里被静音的通道"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        arm = find_armature(context)[1]
        return arm is not None and layers.find_track(arm) is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        layers.delete_physics_layer(find_armature(context)[1])
        self.report({"INFO"}, "已删除物理层")
        return {"FINISHED"}


_autofill_tried = set()


def _schedule_autofill(obj_name, action_name=""):
    """Panels may not write data while drawing, so detect paths from a timer.
    Retried whenever the model's action changes (e.g. after importing a VMD)."""
    key = (obj_name, action_name)
    if key in _autofill_tried:
        return
    _autofill_tried.add(key)

    def run():
        obj = bpy.data.objects.get(obj_name)
        if obj is not None:
            root = paths.find_mmd_root(obj)
            arm = None
            if root is not None:
                arm = next((c for c in root.children if c.type == "ARMATURE"), None)
            elif obj.type == "ARMATURE":
                arm = obj
            try:
                paths.autofill(root, arm)
            except Exception as e:
                print("[CheryStage mmd_bake] autofill failed:", e)
            _redraw_ui()
        return None

    bpy.app.timers.register(run, first_interval=0.01)


class CHERY_OT_mmd_detect_paths(bpy.types.Operator):
    bl_idname = "chery.mmd_detect_paths"
    bl_label = "重新检测路径"
    bl_description = "根据模型贴图位置和导入的动作名，重新查找 PMX 和 VMD"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return find_armature(context)[1] is not None

    def execute(self, context):
        root, arm = find_armature(context)
        pmx, vmd = paths.autofill(root, arm, force=True)
        self.report({"INFO"}, f"PMX：{pmx or '未找到'}；VMD：{vmd or '未找到'}")
        return {"FINISHED"}


classes = (CheryMMDBakeSettings, CHERY_OT_mmd_bake, CHERY_OT_mmd_detect_paths,
           CHERY_OT_mmd_layer_toggle, CHERY_OT_mmd_layer_delete, CHERY_PT_mmd_bake)


def register():
    paths.register()
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.chery_mmd_bake = PointerProperty(type=CheryMMDBakeSettings)
    bpy.types.Object.mmd_bake_include = BoolProperty(name="参与烘焙", default=True,
                                                    description="场景烘焙时包含这个模型")


def unregister():
    progress_hud.stop()
    del bpy.types.Object.mmd_bake_include
    del bpy.types.Scene.chery_mmd_bake
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    paths.unregister()
