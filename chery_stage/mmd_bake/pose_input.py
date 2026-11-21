# SPDX-License-Identifier: GPL-3.0-or-later
"""Sample the evaluated pose of an MMD Tools armature from Blender and write it for mmdbake.

This lets the baker follow whatever animation is in Blender (hand-keyed edits, IK, constraints)
instead of re-reading the original VMD file.

.mbp layout (little endian):
  char[8] "MMDPOSE1"; u32 version(=1); u32 boneCount; u32 frameCount; i32 firstFrame
  per bone: u32 nameLen; utf8 name (MMD Japanese name); f32[3] rest head (Blender armature space)
  per frame, per bone: f32[7] = transform from rest to pose in Blender armature space
                       (qw, qx, qy, qz, tx, ty, tz), i.e. pose_matrix @ rest_matrix^-1
"""

import struct

import bpy
import numpy as np

from . import layers


def _mmd_name(pb):
    mb = getattr(pb, "mmd_bone", None)
    nj = getattr(mb, "name_j", "") if mb is not None else ""
    return nj or pb.name


def _rigid_inv(m):
    out = np.zeros_like(m)
    rt = np.swapaxes(m[..., :3, :3], -1, -2)
    out[..., :3, :3] = rt
    out[..., :3, 3] = -np.einsum("...ij,...j->...i", rt, m[..., :3, 3])
    out[..., 3, 3] = 1.0
    return out


def _mat_to_quat(m):
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


class _Speedup:
    """Temporarily skip work Blender would do on every frame_set that sampling does not need."""

    def __init__(self, scene, arm):
        self.scene, self.arm, self.undo = scene, arm, []

    def __enter__(self):
        rbw = self.scene.rigidbody_world
        if rbw is not None and rbw.enabled:
            rbw.enabled = False
            self.undo.append(lambda: setattr(rbw, "enabled", True))
        for ob in bpy.data.objects:
            if ob.type != "MESH" or ob.find_armature() != self.arm:
                continue
            for md in ob.modifiers:
                if md.show_viewport and md.type in {"ARMATURE", "SURFACE_DEFORM", "CORRECTIVE_SMOOTH"}:
                    md.show_viewport = False
                    self.undo.append(lambda m=md: setattr(m, "show_viewport", True))
        self.frame = self.scene.frame_current
        # sample the motion only: hide the previous physics bake
        self.phys = layers.physics_muted(self.arm)
        self.phys.__enter__()
        self.undo.append(lambda: self.phys.__exit__(None, None, None))
        return self

    def __exit__(self, *exc):
        for fn in reversed(self.undo):
            try:
                fn()
            except Exception:
                pass
        self.scene.frame_set(self.frame)
        return False


def sample_pose(scene, arm, frame_start, frame_end, path, chunk=8):
    """Generator: samples frames [frame_start, frame_end] into `path`.

    Yields ("sample", done, total) every `chunk` frames, then ("sampled", total, None).
    """
    pbs = arm.pose.bones
    bones = arm.data.bones
    names = [_mmd_name(pb) for pb in pbs]
    nb = len(pbs)
    nf = frame_end - frame_start + 1
    rest = np.stack([np.array(bones[pb.name].matrix_local, dtype=np.float64) for pb in pbs])
    rest_inv = _rigid_inv(rest)
    heads = np.array([tuple(bones[pb.name].head_local) for pb in pbs], dtype=np.float32)
    buf = np.empty(nb * 16, dtype=np.float32)

    with open(path, "wb") as f, _Speedup(scene, arm):
        f.write(b"MMDPOSE1")
        f.write(struct.pack("<IIIi", 1, nb, nf, frame_start))
        for n, h in zip(names, heads):
            raw = n.encode("utf-8")
            f.write(struct.pack("<I", len(raw)))
            f.write(raw)
            f.write(struct.pack("<3f", *h))
        out = np.empty((nb, 7), dtype=np.float32)
        for i, fr in enumerate(range(frame_start, frame_end + 1)):
            scene.frame_set(fr)
            pbs.foreach_get("matrix", buf)
            pose = buf.reshape(nb, 4, 4).transpose(0, 2, 1).astype(np.float64)   # column-major -> row-major
            s = pose @ rest_inv
            out[:, 0:4] = _mat_to_quat(s[:, :3, :3])
            out[:, 4:7] = s[:, :3, 3]
            f.write(out.tobytes())
            if (i + 1) % chunk == 0:
                yield ("sample", i + 1, nf)
    yield ("sampled", nf, None)
