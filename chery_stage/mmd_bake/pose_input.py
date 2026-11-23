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


def sample_pose(scene, arm, frame_start, frame_end, path, chunk=8, fields=None):
    """Generator: samples frames [frame_start, frame_end] into `path`.

    Yields ("sample", done, total) every `chunk` frames, then ("sampled", total, None).
    fields: optional FieldWriter, sampled on the same frames.
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
            if fields is not None:
                fields.frame()
            if (i + 1) % chunk == 0:
                f.flush()   # mmdbake reads the file while it is being written
                if fields is not None:
                    fields.flush()
                yield ("sample", i + 1, nf)
    if fields is not None:
        fields.close()
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


def sample_scene(scene, arms, colliders, frame_start, frame_end, pose_paths, coll_path, chunk=8, fields=None):
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
            if fields is not None:
                fields.frame()
            if (i + 1) % chunk == 0:
                for f in files:
                    f.flush()   # mmdbake reads the files while they are being written
                if cf:
                    cf.flush()
                if fields is not None:
                    fields.flush()
                yield ("sample", i + 1, nf)
    if fields is not None:
        fields.close()
    yield ("sampled", nf, None)


# --------------------------------------------------------------------------- #
# force fields (wind, force, vortex, turbulence) -> .mbf for mmdbake
# --------------------------------------------------------------------------- #
FIELD_TYPES = {"WIND": 0, "FORCE": 1, "VORTEX": 2, "TURBULENCE": 3}


def scene_fields(scene):
    """Force fields that act on rigid bodies: visible, supported type, in the rigid body world's
    effector collection if one is set."""
    rbw = scene.rigidbody_world
    coll = rbw.effector_weights.collection if rbw is not None else None
    allowed = set(coll.all_objects) if coll is not None else None
    out = []
    for ob in scene.objects:
        fd = ob.field
        if fd is None or fd.type not in FIELD_TYPES or not ob.visible_get():
            continue
        if allowed is not None and ob not in allowed:
            continue
        out.append(ob)
    return out


class FieldWriter:
    """Writes the .mbf file: static field settings, then one record per sampled frame.

    space: armature whose object space the fields are written in (single mode), or None for world.
    """

    def __init__(self, scene, fields, path, frames, first, space=None, mult=1.0):
        self.fields, self.space = fields, space
        rbw = scene.rigidbody_world
        ew = rbw.effector_weights if rbw is not None else None
        self.weights = [mult * (ew.all * getattr(ew, ob.field.type.lower(), 1.0) if ew else 1.0) for ob in fields]
        self.f = open(path, "wb")
        self.f.write(b"MMDFILD1")
        self.f.write(struct.pack("<IIIi", 1, len(fields), frames, first))
        for ob in fields:
            fd = ob.field
            flags = ((1 if fd.use_min_distance else 0) | (2 if fd.use_max_distance else 0)
                     | (4 if fd.use_radial_min else 0) | (8 if fd.use_radial_max else 0)
                     | (16 if fd.use_global_coords else 0))
            zdir = {"POSITIVE": 1, "NEGATIVE": -1}.get(fd.z_direction, 0)
            self.f.write(struct.pack("<BBBbB", FIELD_TYPES[fd.type], 1 if fd.shape == "PLANE" else 0,
                                     {"TUBE": 1, "CONE": 2}.get(fd.falloff_type, 0), zdir, flags))
            self.f.write(struct.pack("<8f", fd.falloff_power, fd.distance_min, fd.distance_max,
                                     fd.radial_falloff, fd.radial_min, fd.radial_max, fd.size,
                                     fd.inflow if fd.type == "VORTEX" else 0.0))
        self.buf = np.empty((len(fields), 8), dtype=np.float32)

    def frame(self):
        inv = self.space.matrix_world.inverted() if self.space is not None else None
        for k, ob in enumerate(self.fields):
            m = ob.matrix_world if inv is None else inv @ ob.matrix_world
            self.buf[k, :7] = _rot_loc(m)
            self.buf[k, 7] = ob.field.strength * self.weights[k]
        self.f.write(self.buf.tobytes())

    def flush(self):
        self.f.flush()

    def close(self):
        if self.f is not None:
            self.f.close()
            self.f = None


def write_static_fields(scene, fields, path, space=None, mult=1.0):
    """Fields as they are on the current frame (VMD input: nothing is sampled per frame)."""
    w = FieldWriter(scene, fields, path, 1, scene.frame_current, space, mult)
    w.frame()
    w.close()
