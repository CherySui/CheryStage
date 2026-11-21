# SPDX-License-Identifier: GPL-3.0-or-later
"""Animation layers for the physics bake, built on NLA.

Blender 5.2 still allows only one layer per Action, so layers are NLA tracks:
- Motion layer: the armature's active action (imported VMD or hand-keyed). Never modified by the bake.
- Physics layer: a separate action holding only the physics bones, on the NLA track PHYS_TRACK.

The active action is evaluated on top of the NLA stack, but only for channels it contains.
If the motion action also keys physics bones, those fcurves are muted (not deleted) so the
physics layer shows through; deleting the physics layer unmutes them again.
"""

import json
import re

import bpy

PHYS_TRACK = "CheryStage 物理层"
_MUTED_PROP = "chery_physics_muted"          # JSON list of [data_path, index] muted in the motion action
_BONE_RE = re.compile(r'^pose\.bones\["(.+?)"\]\.')


def _channelbag(action, slot, create=False):
    from bpy_extras import anim_utils
    if action is None or slot is None:
        return None
    if create:
        return anim_utils.action_ensure_channelbag_for_slot(action, slot)
    return anim_utils.action_get_channelbag_for_slot(action, slot)


def physics_action_name(arm):
    return f"{arm.name}.StagePhysics"


def find_track(arm):
    ad = arm.animation_data
    return ad.nla_tracks.get(PHYS_TRACK) if ad else None


def physics_action(arm):
    tr = find_track(arm)
    if tr and len(tr.strips):
        return tr.strips[0].action
    return bpy.data.actions.get(physics_action_name(arm))


def motion_action(arm):
    ad = arm.animation_data
    return ad.action if ad else None


def physics_bone_names(arm):
    act = physics_action(arm)
    if act is None or not len(act.slots):
        return set()
    cb = _channelbag(act, act.slots[0])
    if cb is None:
        return set()
    out = set()
    for fc in cb.fcurves:
        m = _BONE_RE.match(fc.data_path)
        if m:
            out.add(m.group(1))
    return out


def prepare_physics_action(arm):
    """Get (or create) the physics action and return its channelbag for writing keys."""
    act = physics_action(arm) or bpy.data.actions.new(physics_action_name(arm))
    slot = act.slots[0] if len(act.slots) else act.slots.new(id_type="OBJECT", name=arm.name)
    return act, slot, _channelbag(act, slot, create=True)


def _motion_channelbag(arm):
    ad = arm.animation_data
    if not ad or not ad.action or ad.action_slot is None:
        return None
    return _channelbag(ad.action, ad.action_slot)


def mute_conflicts(arm, bone_names):
    """Mute motion-action fcurves that drive physics bones so the physics layer shows through."""
    cb = _motion_channelbag(arm)
    if cb is None:
        return 0
    muted = json.loads(arm.get(_MUTED_PROP, "[]"))
    have = {tuple(x) for x in muted}
    n = 0
    for fc in cb.fcurves:
        m = _BONE_RE.match(fc.data_path)
        if m and m.group(1) in bone_names and not fc.mute:
            fc.mute = True
            key = (fc.data_path, fc.array_index)
            if key not in have:
                muted.append(list(key))
                have.add(key)
            n += 1
    arm[_MUTED_PROP] = json.dumps(muted)
    return n


def muted_count(arm):
    return len(json.loads(arm.get(_MUTED_PROP, "[]")))


def _set_recorded_mute(arm, mute):
    cb = _motion_channelbag(arm)
    if cb is None:
        return
    for path, idx in json.loads(arm.get(_MUTED_PROP, "[]")):
        fc = cb.fcurves.find(path, index=idx)
        if fc is not None:
            fc.mute = mute


def unmute_conflicts(arm):
    _set_recorded_mute(arm, False)
    if _MUTED_PROP in arm:
        del arm[_MUTED_PROP]


def finish_physics_layer(arm, act, slot, bone_names):
    """After keys are written: (re)create the NLA strip and mute conflicting motion channels."""
    ad = arm.animation_data or arm.animation_data_create()
    ad.use_nla = True
    tr = find_track(arm)
    if tr is None:
        tr = ad.nla_tracks.new()
        tr.name = PHYS_TRACK
    for st in list(tr.strips):
        tr.strips.remove(st)
    f0, f1 = act.frame_range
    st = tr.strips.new("物理层", int(f0), act)
    st.action_slot = slot
    st.blend_type = "REPLACE"
    st.extrapolation = "HOLD"
    tr.mute = False
    return mute_conflicts(arm, bone_names)


def reset_unanimated(arm, names):
    """Put bones back to rest when nothing animates them any more (Blender keeps the last value)."""
    cb = _motion_channelbag(arm)
    animated = set()
    if cb is not None:
        for fc in cb.fcurves:
            m = _BONE_RE.match(fc.data_path)
            if m and not fc.mute:
                animated.add(m.group(1))
    for n in names:
        pb = arm.pose.bones.get(n)
        if pb is None or n in animated:
            continue
        pb.location = (0, 0, 0)
        pb.rotation_quaternion = (1, 0, 0, 0)
        pb.rotation_euler = (0, 0, 0)
        pb.scale = (1, 1, 1)


def set_physics_enabled(arm, enabled):
    tr = find_track(arm)
    if tr is None:
        return
    tr.mute = not enabled
    # layer off = look of the motion action alone: give back the channels we muted for it
    _set_recorded_mute(arm, enabled)
    if not enabled:
        reset_unanimated(arm, physics_bone_names(arm))
    arm.update_tag()


def delete_physics_layer(arm):
    names = physics_bone_names(arm)
    ad = arm.animation_data
    tr = find_track(arm)
    act = physics_action(arm)
    if ad and tr:
        ad.nla_tracks.remove(tr)
    unmute_conflicts(arm)
    if act is not None and act.users == 0:
        bpy.data.actions.remove(act)
    reset_unanimated(arm, names)
    arm.update_tag()


class physics_muted:
    """Context manager: hide the physics layer while sampling the motion, restore afterwards."""

    def __init__(self, arm):
        self.arm, self.track, self.was = arm, find_track(arm), None

    def __enter__(self):
        if self.track is not None:
            self.was = self.track.mute
            self.track.mute = True
            reset_unanimated(self.arm, physics_bone_names(self.arm))
        return self

    def __exit__(self, *exc):
        if self.track is not None:
            self.track.mute = self.was
        return False
