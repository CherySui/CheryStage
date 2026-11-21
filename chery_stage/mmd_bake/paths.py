# SPDX-License-Identifier: GPL-3.0-or-later
"""Find the PMX / VMD files that belong to an MMD Tools model.

Paths are stored on the model (Object.mmd_bake_pmx / Object.mmd_bake_vmd) and
saved with the .blend. MMD Tools does not record them, so they are guessed:
   - PMX: .pmx files near the model's texture folders, matched by file name /
     internal model name.
   - VMD: MMD Tools names the action "<vmd name>_bone", so look for that file
     next to the .blend, the PMX, drive roots and the user's common folders.
"""

import logging
import os
import re
import struct

import bpy

PROP_PMX = "mmd_bake_pmx"
PROP_VMD = "mmd_bake_vmd"


# --------------------------------------------------------------------------- #
# model lookup
# --------------------------------------------------------------------------- #
def find_mmd_root(obj):
    while obj is not None and getattr(obj, "mmd_type", "") != "ROOT":
        obj = obj.parent
    return obj


def model_owner(root, arm):
    """Object that stores the paths: the MMD root if there is one, else the armature."""
    return root if root is not None else arm


def _strip_suffix(name):
    return re.sub(r"\.\d{3}$", "", name or "")


def _model_meshes(root, arm):
    base = root if root is not None else arm
    out = []
    stack = list(base.children) if base else []
    while stack:
        o = stack.pop()
        if o.type == "MESH":
            out.append(o)
        stack.extend(o.children)
    return out


# --------------------------------------------------------------------------- #
# file helpers
# --------------------------------------------------------------------------- #
def read_pmx_name(path):
    """Internal model name of a PMX file, or '' on failure."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"PMX ":
                return ""
            f.read(4)
            hl = f.read(1)[0]
            header = f.read(hl)
            enc = "utf-16-le" if header[0] == 0 else "utf-8"
            (n,) = struct.unpack("<i", f.read(4))
            if n < 0 or n > 4096:
                return ""
            return f.read(n).decode(enc, "replace")
    except Exception:
        return ""


def vmd_has_bone_keys(path):
    try:
        with open(path, "rb") as f:
            if not f.read(30).startswith(b"Vocaloid Motion Data"):
                return False
            f.read(20)
            (n,) = struct.unpack("<I", f.read(4))
            return n > 0
    except Exception:
        return False


def _listdir_lower(d):
    try:
        return {n.lower(): n for n in os.listdir(d)}
    except OSError:
        return {}


def _ancestors(d, levels):
    out = []
    d = os.path.abspath(d)
    for _ in range(levels + 1):
        out.append(d)
        p = os.path.dirname(d)
        if p == d:
            break
        d = p
    return out


# --------------------------------------------------------------------------- #
# guessing
# --------------------------------------------------------------------------- #
def guess_pmx(root, arm):
    dirs = []
    for mesh in _model_meshes(root, arm):
        for slot in mesh.material_slots:
            mat = slot.material
            if mat is None or not mat.node_tree:
                continue
            for node in mat.node_tree.nodes:
                img = getattr(node, "image", None)
                if img is None or not img.filepath:
                    continue
                try:
                    p = bpy.path.abspath(img.filepath, library=img.library)
                except Exception:
                    continue
                d = os.path.dirname(os.path.normpath(p))
                for a in _ancestors(d, 2):
                    if a not in dirs:
                        dirs.append(a)
    if bpy.data.filepath:
        d = os.path.dirname(bpy.data.filepath)
        if d not in dirs:
            dirs.append(d)

    want_file = _strip_suffix((root or arm).name).lower()
    want_model = getattr(getattr(root, "mmd_root", None), "name", "") if root else ""

    best, best_score = "", 0
    for d in dirs:
        for low, real in _listdir_lower(d).items():
            if not low.endswith((".pmx", ".pmd")):
                continue
            path = os.path.join(d, real)
            score = 1
            if os.path.splitext(low)[0] == want_file:
                score += 10
            if want_model and low.endswith(".pmx") and read_pmx_name(path) == want_model:
                score += 5
            if score > best_score:
                best, best_score = path, score
    return best


def _motion_names(root, arm):
    names = []
    for ob in (arm, root):
        ad = getattr(ob, "animation_data", None) if ob else None
        if not ad:
            continue
        acts = [ad.action] if ad.action else []
        for tr in ad.nla_tracks:
            acts += [s.action for s in tr.strips if s.action]
        for act in acts:
            n = _strip_suffix(act.name)
            n = re.sub(r"_(bone|display|facial)$", "", n)
            if n and n not in names:
                names.append(n)
    return names


def guess_vmd(root, arm, pmx_path=""):
    names = _motion_names(root, arm)
    if not names:
        return ""
    dirs = []

    def add(d, levels=0):
        if not d:
            return
        for a in _ancestors(d, levels):
            if a not in dirs and os.path.isdir(a):
                dirs.append(a)

    if bpy.data.filepath:
        add(os.path.dirname(bpy.data.filepath), 1)
    if pmx_path:
        add(os.path.dirname(pmx_path), 3)
    home = os.path.expanduser("~")
    for sub in ("Desktop", "Downloads", "Documents", "Videos", "桌面", "下载"):
        add(os.path.join(home, sub))
    for d in list(dirs):  # drive roots
        drive = os.path.splitdrive(d)[0]
        if drive:
            add(drive + os.sep)

    for n in names:
        target = (n + ".vmd").lower()
        for d in dirs:
            real = _listdir_lower(d).get(target)
            if real:
                path = os.path.join(d, real)
                if vmd_has_bone_keys(path):
                    return path
    return ""


def autofill(root, arm, force=False):
    """Fill empty paths on the model. Returns (pmx, vmd) after filling."""
    owner = model_owner(root, arm)
    if owner is None:
        return "", ""
    pmx = getattr(owner, PROP_PMX, "")
    vmd = getattr(owner, PROP_VMD, "")
    if force or not pmx or not os.path.isfile(bpy.path.abspath(pmx)):
        g = guess_pmx(root, arm)
        if g:
            pmx = g
            setattr(owner, PROP_PMX, g)
    if force or not vmd or not os.path.isfile(bpy.path.abspath(vmd)):
        g = guess_vmd(root, arm, bpy.path.abspath(pmx) if pmx else "")
        if g:
            vmd = g
            setattr(owner, PROP_VMD, g)
    return pmx, vmd


# --------------------------------------------------------------------------- #
# capture paths from MMD Tools' import log
#
# MMD Tools logs through the root logger while importing. A handler on the
# root logger looks up the call stack for the running import operator and
# reads its filepath. Nothing in MMD Tools is modified. Data is only written
# later from a timer, after the import operator has finished.
# --------------------------------------------------------------------------- #
_IMPORT_OPS = {"mmd_tools.import_model": "pmx", "mmd_tools.import_vmd": "vmd"}
_pending = []      # (kind, filepath, [target object names])
_seen = set()      # (id(operator), filepath)


def _find_import_operator():
    import sys

    f = sys._getframe(2)
    depth = 0
    while f is not None and depth < 60:
        op = f.f_locals.get("self") if "self" in f.f_code.co_varnames else None
        if op is not None:
            kind = _IMPORT_OPS.get(getattr(type(op), "bl_idname", ""))
            if kind:
                return kind, op
        f = f.f_back
        depth += 1
    return None, None


class _ImportLogHandler(logging.Handler):
    def emit(self, record):
        try:
            if "mmd_tools" not in (record.pathname or ""):
                return
            kind, op = _find_import_operator()
            if op is None:
                return
            path = getattr(op, "filepath", "")
            if not path:
                return
            key = (id(op), path)
            if key in _seen:
                return
            _seen.add(key)
            targets = []
            if kind == "vmd":
                objs = getattr(op, "_ImportVmd__target_objects", None) or []
                targets = [o.name for o in objs if o is not None]
            _pending.append((kind, os.path.abspath(bpy.path.abspath(path)), targets))
            if not bpy.app.timers.is_registered(_apply_pending):
                bpy.app.timers.register(_apply_pending, first_interval=0.2)
        except Exception:
            pass  # never break MMD Tools' import


def _apply_pending():
    items = list(_pending)
    _pending.clear()
    for kind, path, targets in items:
        try:
            if kind == "pmx":
                _assign_pmx(path)
            else:
                _assign_vmd(path, targets)
        except Exception as e:
            print("[CheryStage mmd_bake] path capture failed:", e)
    if len(_seen) > 1000:
        _seen.clear()
    return None


def _assign_pmx(path):
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    roots = [o for o in bpy.data.objects if getattr(o, "mmd_type", "") == "ROOT"]
    exact = [r for r in roots if _strip_suffix(r.name).lower() == stem]
    # MMD Tools trims long names; fall back to prefix match
    cands = exact or [r for r in roots if stem.startswith(_strip_suffix(r.name).lower()) and r.name]
    # prefer models that don't have a PMX yet (the one just imported)
    fresh = [r for r in cands if not getattr(r, PROP_PMX, "")]
    for r in (fresh or cands[-1:]):
        setattr(r, PROP_PMX, path)


def _assign_vmd(path, targets):
    if not vmd_has_bone_keys(path):
        return  # facial / camera only: keep the body motion we already know
    owners = []
    for name in targets:
        o = bpy.data.objects.get(name)
        if o is None:
            continue
        root = find_mmd_root(o)
        owner = root if root is not None else (o if o.type == "ARMATURE" else None)
        if owner is not None and owner not in owners:
            owners.append(owner)
    for owner in owners:
        setattr(owner, PROP_VMD, path)


_log_handler = None


def register():

    global _log_handler
    bpy.types.Object.mmd_bake_pmx = bpy.props.StringProperty(
        name="PMX 模型", subtype="FILE_PATH", description="这个模型对应的 PMX 文件（自动检测，可手动改）")
    bpy.types.Object.mmd_bake_vmd = bpy.props.StringProperty(
        name="VMD 动作", subtype="FILE_PATH", description="身体动作 VMD（自动检测，可手动改）")
    _log_handler = _ImportLogHandler(level=logging.NOTSET)
    logging.getLogger().addHandler(_log_handler)


def unregister():

    global _log_handler
    if _log_handler is not None:
        logging.getLogger().removeHandler(_log_handler)
        _log_handler = None
    if bpy.app.timers.is_registered(_apply_pending):
        bpy.app.timers.unregister(_apply_pending)
    del bpy.types.Object.mmd_bake_vmd
    del bpy.types.Object.mmd_bake_pmx
