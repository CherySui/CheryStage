# SPDX-License-Identifier: GPL-3.0-or-later
"""Animation layers for the physics bake, built on NLA.

Blender 5.2 still allows only one layer per Action, so layers are NLA tracks:
- Motion layer: the armature's active action (imported VMD or hand-keyed). Never modified by the bake.
- Physics layer: a separate action holding only the physics bones, on the NLA track PHYS_TRACK.

- Adjust layer (optional): an action on the NLA track ADJ_TRACK above the physics layer, blended
  with COMBINE. The user keys corrections into it (NLA tweak mode) with a few keys instead of
  editing the dense per-frame physics keys; Blender stores each key as an offset so the bone lands
  where it was posed.

The active action is evaluated on top of the NLA stack, but only for channels it contains.
If the motion action also keys physics bones, those fcurves are muted (not deleted) so the
physics layer shows through; deleting the physics layer unmutes them again.
"""

import json
import re

import bpy

PHYS_TRACK = "CheryStage 物理层"
ADJ_TRACK = "CheryStage 调整层"
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
    if ad and ad.use_tweak_mode:   # editing a layer: Blender parks the motion action meanwhile
        return ad.action_tweak_storage
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
    if find_adjust_track(arm) is not None:
        build_adjust_track(arm)   # keep it on top and covering the new frame range
    return mute_conflicts(arm, bone_names)


# --------------------------------------------------------------------------- #
# adjust layer
# --------------------------------------------------------------------------- #
def adjust_action_name(arm):
    return f"{arm.name}.StageAdjust"


def find_adjust_track(arm):
    ad = arm.animation_data
    return ad.nla_tracks.get(ADJ_TRACK) if ad else None


def adjust_action(arm):
    tr = find_adjust_track(arm)
    if tr and len(tr.strips):
        return tr.strips[0].action
    return bpy.data.actions.get(adjust_action_name(arm))


def is_editing_adjust(arm):
    ad = arm.animation_data
    return bool(ad and ad.use_tweak_mode and ad.action is not None and ad.action == adjust_action(arm))


def exit_tweak(arm):
    ad = arm.animation_data
    if ad and ad.use_tweak_mode:
        ad.use_tweak_mode = False


def build_adjust_track(arm):
    """Create the adjust layer, or rebuild its track on top of the stack covering the physics range.
    The adjust action (the user's keys) is kept."""
    exit_tweak(arm)
    ad = arm.animation_data or arm.animation_data_create()
    ad.use_nla = True
    act = adjust_action(arm) or bpy.data.actions.new(adjust_action_name(arm))
    slot = act.slots[0] if len(act.slots) else act.slots.new(id_type="OBJECT", name=arm.name)
    phys = physics_action(arm)
    lo, hi = phys.frame_range if phys is not None else (bpy.context.scene.frame_start, bpy.context.scene.frame_end)
    if act.use_frame_range:   # never shrink below keys the user already set
        lo, hi = min(lo, act.frame_start), max(hi, act.frame_end)
    act.use_frame_range = True
    act.frame_start, act.frame_end = int(lo), max(int(hi), int(lo) + 1)
    old = find_adjust_track(arm)
    mute = old.mute if old is not None else False
    if old is not None:
        ad.nla_tracks.remove(old)
    tr = ad.nla_tracks.new()   # new tracks go on top
    tr.name = ADJ_TRACK
    st = tr.strips.new("调整层", int(act.frame_start), act)
    st.action_slot = slot
    st.blend_type = "COMBINE"
    st.extrapolation = "HOLD"
    tr.mute = mute
    return act


def edit_adjust_layer(arm, on):
    """Enter / leave NLA tweak mode on the adjust strip: keys then go into the adjust action."""
    ad = arm.animation_data
    tr = find_adjust_track(arm)
    if ad is None or tr is None or not len(tr.strips):
        return False
    if not on:
        exit_tweak(arm)
        return True
    exit_tweak(arm)
    for t in ad.nla_tracks:
        t.select = False
        for s in t.strips:
            s.select = False
    tr.mute = False
    ad.nla_tracks.active = tr
    tr.select = True
    tr.strips[0].select = True
    ad.use_tweak_mode = True
    return is_editing_adjust(arm)


def adjust_key_count(arm):
    act = adjust_action(arm)
    if act is None or not len(act.slots):
        return 0
    cb = _channelbag(act, act.slots[0])
    if cb is None:
        return 0
    return len({round(k.co.x, 3) for fc in cb.fcurves for k in fc.keyframe_points})   # keyed frames


def set_adjust_enabled(arm, enabled):
    tr = find_adjust_track(arm)
    if tr is None:
        return
    if not enabled:
        exit_tweak(arm)
    tr.mute = not enabled
    arm.update_tag()


def delete_adjust_layer(arm):
    exit_tweak(arm)
    ad = arm.animation_data
    tr = find_adjust_track(arm)
    act = adjust_action(arm)
    if ad and tr:
        ad.nla_tracks.remove(tr)
    if act is not None and act.users == 0:
        bpy.data.actions.remove(act)
    arm.update_tag()


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
    exit_tweak(arm)
    tr.mute = not enabled
    # layer off = look of the motion action alone: give back the channels we muted for it
    _set_recorded_mute(arm, enabled)
    if not enabled:
        reset_unanimated(arm, physics_bone_names(arm))
    arm.update_tag()


def delete_physics_layer(arm):
    exit_tweak(arm)
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
    """Context manager: hide the physics and adjust layers while sampling the motion, restore afterwards."""

    def __init__(self, arm):
        exit_tweak(arm)
        self.arm = arm
        self.tracks = [t for t in (find_track(arm), find_adjust_track(arm)) if t is not None]
        self.was = []

    def __enter__(self):
        self.was = [t.mute for t in self.tracks]
        for t in self.tracks:
            t.mute = True
        if self.tracks:
            reset_unanimated(self.arm, physics_bone_names(self.arm))
        return self

    def __exit__(self, *exc):
        for t, m in zip(self.tracks, self.was):
            t.mute = m
        return False
