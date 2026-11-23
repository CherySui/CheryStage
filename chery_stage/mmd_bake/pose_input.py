# SPDX-License-Identifier: GPL-3.0-or-later
"""Sample the evaluated pose of an MMD Tools armature from Blender and write it for mmdbake.

This lets the baker follow whatever animation is in Blender (hand-keyed edits, IK, constraints)
instead of re-reading the original VMD file. The file is flushed every few frames: mmdbake runs at
the same time (--stream) and simulates frames as soon as they are on disk.

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
                f.flush()   # mmdbake reads the file while it is being written
                yield ("sample", i + 1, nf)
    yield ("sampled", nf, None)


# --------------------------------------------------------------------------- #
# scene mode: several armatures + passive rigid body colliders, one pass
# --------------------------------------------------------------------------- #
COLLIDER_SHAPES = {"BOX": 0, "SPHERE": 1, "CAPSULE": 2, "CYLINDER": 3, "CONE": 4,
                   "CONVEX_HULL": 5, "COMPOUND": 5, "MESH": 6}


def _in_mmd_model(ob):
    while ob is not None:
        if getattr(ob, "mmd_type", "NONE") != "NONE":
            return True
        ob = ob.parent
    return False


def scene_colliders(scene):
    """Objects set to Passive in Blender's rigid body panel (MMD Tools' own rigid bodies excluded)."""
    out = []
    for ob in scene.objects:
        rb = ob.rigid_body
        if rb is None or rb.type != "PASSIVE" or not rb.enabled or ob.type != "MESH":
            continue
        if _in_mmd_model(ob) or not ob.visible_get():
            continue
        out.append(ob)
    return out


def _rot_loc(m):
    """(qw,qx,qy,qz,tx,ty,tz) of a world matrix with scale removed."""
    loc, rot, _scale = m.decompose()
    return (rot.w, rot.x, rot.y, rot.z, loc.x, loc.y, loc.z)


def _collider_geometry(ob, depsgraph):
    rb = ob.rigid_body
    shape = COLLIDER_SHAPES.get(rb.collision_shape, 5)
    sc = np.array(ob.matrix_world.to_scale(), dtype=np.float64)
    bb = np.array([tuple(c) for c in ob.bound_box], dtype=np.float64) * sc
    lo, hi = bb.min(0), bb.max(0)
    dims, center = hi - lo, (hi + lo) * 0.5
    if shape == 1:
        size = (float(dims.max()) * 0.5, 0.0, 0.0)
    elif shape in (2, 3, 4):
        size = (float(max(dims[0], dims[1])) * 0.5, 0.0, float(dims[2]))
    else:
        size = tuple(float(x) for x in dims)
    verts = np.zeros((0, 3), dtype=np.float32)
    tris = np.zeros((0, 3), dtype=np.uint32)
    if shape in (5, 6):
        ev = ob.evaluated_get(depsgraph)
        me = ev.to_mesh()
        try:
            co = np.empty(len(me.vertices) * 3, dtype=np.float32)
            me.vertices.foreach_get("co", co)
            verts = (co.reshape(-1, 3) * sc).astype(np.float32)
            if shape == 6:
                me.calc_loop_triangles()
                t = np.empty(len(me.loop_triangles) * 3, dtype=np.int32)
                me.loop_triangles.foreach_get("vertices", t)
                tris = t.reshape(-1, 3).astype(np.uint32)
        finally:
            ev.to_mesh_clear()
        center = np.zeros(3)
    margin = float(rb.collision_margin) if rb.use_margin else 0.0
    return shape, margin, size, tuple(float(x) for x in center), verts, tris


def sample_scene(scene, arms, colliders, frame_start, frame_end, pose_paths, coll_path, chunk=8):
    """Generator: one pass over the frames for every armature (pose file v2) and every collider.

    Yields ("sample", done, total) every `chunk` frames, then ("sampled", total, None).
    """
    import contextlib

    nf = frame_end - frame_start + 1
    per = []
    for arm in arms:
        pbs = arm.pose.bones
        bones = arm.data.bones
        rest = np.stack([np.array(bones[pb.name].matrix_local, dtype=np.float64) for pb in pbs])
        per.append(dict(
            arm=arm, pbs=pbs, nb=len(pbs), names=[_mmd_name(pb) for pb in pbs], rest_inv=_rigid_inv(rest),
            heads=np.array([tuple(bones[pb.name].head_local) for pb in pbs], dtype=np.float32),
            buf=np.empty(len(pbs) * 16, dtype=np.float32), out=np.empty((len(pbs) + 1, 7), dtype=np.float32)))

    with contextlib.ExitStack() as stack:
        for d in per:
            stack.enter_context(_Speedup(scene, d["arm"]))
        files = [stack.enter_context(open(p, "wb")) for p in pose_paths]
        cf = stack.enter_context(open(coll_path, "wb")) if coll_path else None

        scene.frame_set(frame_start)
        for f, d in zip(files, per):
            f.write(b"MMDPOSE1")
            f.write(struct.pack("<IIIi", 2, d["nb"], nf, frame_start))
            for n, h in zip(d["names"], d["heads"]):
                raw = n.encode("utf-8")
                f.write(struct.pack("<I", len(raw)))
                f.write(raw)
                f.write(struct.pack("<3f", *h))
        if cf:
            depsgraph = bpy.context.evaluated_depsgraph_get()
            cf.write(b"MMDCOLL1")
            cf.write(struct.pack("<IIIi", 1, len(colliders), nf, frame_start))
            for ob in colliders:
                shape, margin, size, center, verts, tris = _collider_geometry(ob, depsgraph)
                raw = ob.name.encode("utf-8")
                cf.write(struct.pack("<I", len(raw)))
                cf.write(raw)
                cf.write(struct.pack("<Bf3f3f", shape, margin, *size, *center))
                cf.write(struct.pack("<I", len(verts)))
                cf.write(verts.tobytes())
                cf.write(struct.pack("<I", len(tris)))
                cf.write(tris.tobytes())
        cbuf = np.empty((len(colliders), 7), dtype=np.float32)

        for i, fr in enumerate(range(frame_start, frame_end + 1)):
            scene.frame_set(fr)
            for f, d in zip(files, per):
                d["pbs"].foreach_get("matrix", d["buf"])
                pose = d["buf"].reshape(d["nb"], 4, 4).transpose(0, 2, 1).astype(np.float64)
                s = pose @ d["rest_inv"]
                out = d["out"]
                out[0] = _rot_loc(d["arm"].matrix_world)
                out[1:, 0:4] = _mat_to_quat(s[:, :3, :3])
                out[1:, 4:7] = s[:, :3, 3]
                f.write(out.tobytes())
            if cf:
                for k, ob in enumerate(colliders):
                    cbuf[k] = _rot_loc(ob.matrix_world)
                cf.write(cbuf.tobytes())
            if (i + 1) % chunk == 0:
                for f in files:
                    f.flush()   # mmdbake reads the files while they are being written
                if cf:
                    cf.flush()
                yield ("sample", i + 1, nf)
    yield ("sampled", nf, None)
