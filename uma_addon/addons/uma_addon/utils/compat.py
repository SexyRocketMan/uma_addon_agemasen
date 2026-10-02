"""Compatibility helpers so one code base runs on Blender 4.2 - 5.2.

Two things moved in Blender 5.0 and broke the addon's animation and bone tools:

* bone selection and pose-mode visibility moved off ``Bone`` and onto ``PoseBone``
  (``Bone.select`` / ``Bone.select_head`` / ``Bone.select_tail`` were removed, and
  ``Bone.hide`` now only affects Edit-mode display), and
* the legacy ``Action`` API was removed - ``action.fcurves``, ``action.groups`` and
  ``action.id_root`` are gone. Since 4.4 the curves of an action live in a *channelbag*
  that belongs to a slot (the slot says which datablock the action animates), and 5.0
  requires that route.

Everything here is a thin wrapper that picks the right spelling for the running Blender,
so the operators can stay readable and version-free. Nothing raises for a version that
cannot do the thing; the functions return ``None`` and the caller decides.
"""

import bpy

#: Blender 4.4 introduced action slots, layers and channelbags.
SLOTTED_ACTIONS = tuple(bpy.app.version) >= (4, 4, 0)
#: Blender 5.0 removed Bone.select / Bone.select_head / Bone.select_tail.
POSE_BONE_SELECTION = tuple(bpy.app.version) >= (5, 0, 0)
#: Blender 5.0 removed action.fcurves / action.groups / action.id_root.
LEGACY_ACTION_API = tuple(bpy.app.version) < (5, 0, 0)
#: Blender 5.0 moved pose-mode bone visibility to PoseBone.hide.
POSE_BONE_VISIBILITY = tuple(bpy.app.version) >= (5, 0, 0)
#: Blender 5.0 removed 'GRID' from UILayout.template_list's type parameter, leaving only 'DEFAULT' and
#: 'COMPACT'. Passing 'GRID' there raises TypeError and takes the whole panel's draw down with it, which is
#: why the Action list panel failed to open on 5.0 and newer. Older versions keep the grid layout.
TEMPLATE_LIST_GRID = "DEFAULT" if tuple(bpy.app.version) >= (5, 0, 0) else "GRID"


# --------------------------------------------------------------------------------------
# bones: selection and visibility
# --------------------------------------------------------------------------------------

def pose_bone_select(pose_bone, selected=True):
    """Select or deselect a bone from anywhere.

    4.2 - 4.5 keep the selection flag on ``Bone`` (``pose_bone.bone.select``); 5.0 moved it
    to the pose bone. This is what ``bpy.ops.nla.bake(only_selected=True)`` and mmd_tools'
    ``context.selected_pose_bones`` look at.
    """
    if POSE_BONE_SELECTION:
        pose_bone.select = selected
    else:
        pose_bone.bone.select = selected
    return True


def pose_bone_is_selected(pose_bone):
    return pose_bone.select if POSE_BONE_SELECTION else pose_bone.bone.select


def deselect_all_pose_bones(armature_obj):
    for pose_bone in armature_obj.pose.bones:
        try:
            pose_bone_select(pose_bone, False)
        except AttributeError:
            pass


def pose_bone_hide(pose_bone, hidden=True):
    """Hide or show a bone in Object/Pose mode.

    ``Bone.hide`` used to mean this; in 5.0 it only hides the bone in Edit mode, and
    ``PoseBone.hide`` took over. Writing the wrong one is silent, so both callers and the
    setter go through here.
    """
    if POSE_BONE_VISIBILITY:
        pose_bone.hide = hidden
    else:
        pose_bone.bone.hide = hidden
    return True


def pose_bone_visible(pose_bone):
    return not (pose_bone.hide if POSE_BONE_VISIBILITY else pose_bone.bone.hide)


def hide_pose_bone_by_name(armature_obj, bone_name, hidden=True):
    """Same as :func:`pose_bone_hide` for a bone looked up by name. False if it is absent."""
    pose_bone = armature_obj.pose.bones.get(bone_name)
    if pose_bone is None:
        return False
    return pose_bone_hide(pose_bone, hidden)


# --------------------------------------------------------------------------------------
# actions: legacy (<= 4.5) versus slotted (>= 4.4, required from 5.0)
# --------------------------------------------------------------------------------------

def action_slot(anim_data):
    """The slot this owner is animated through, or None where slots do not exist."""
    if not SLOTTED_ACTIONS or anim_data is None:
        return None
    return getattr(anim_data, "action_slot", None)


def ensure_action_slot(anim_data, action, name, id_type='OBJECT'):
    """The owner's slot, created and assigned on first use. None before 4.4."""
    if not SLOTTED_ACTIONS or anim_data is None or action is None:
        return None
    slot = getattr(anim_data, "action_slot", None)
    if slot is None:
        slot = action.slots.new(name=name, id_type=id_type)
        anim_data.action_slot = slot
    return slot


def action_channelbags(action, slot):
    """Every channelbag of ``action`` that belongs to ``slot`` (4.4+)."""
    if not SLOTTED_ACTIONS or action is None or slot is None:
        return []
    found = []
    for layer in getattr(action, "layers", []) or []:
        for strip in getattr(layer, "strips", []) or []:
            channelbag = getattr(strip, "channelbag", None)
            if channelbag is None:
                continue
            try:
                bag = channelbag(slot)
            except (TypeError, RuntimeError):
                bag = None
            if bag is not None:
                found.append(bag)
    return found


def action_channelbag(action, anim_data=None, slot=None):
    """The channelbag holding this owner's curves, or None."""
    slot = slot if slot is not None else action_slot(anim_data)
    bags = action_channelbags(action, slot)
    return bags[0] if bags else None


def ensure_action_channelbag(action, anim_data=None, slot=None):
    """The owner's channelbag, creating the layer/strip/channelbag if the action is empty."""
    if not SLOTTED_ACTIONS or action is None:
        return None
    existing = action_channelbag(action, anim_data, slot)
    if existing is not None:
        return existing
    slot = slot if slot is not None else action_slot(anim_data)
    if slot is None:
        return None
    layer = action.layers[0] if len(action.layers) else action.layers.new("Base Layer")
    strip = layer.strips[0] if len(layer.strips) else layer.strips.new(type='KEYFRAME')
    return strip.channelbag(slot, ensure=True)


def action_fcurves(action, anim_data=None, slot=None):
    """Every F-curve of ``action`` for this owner - legacy list or channelbag contents."""
    if action is None:
        return []
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        return list(legacy)
    bag = action_channelbag(action, anim_data, slot)
    return list(bag.fcurves) if bag is not None else []


def find_fcurve(action, data_path, index=None, anim_data=None, slot=None):
    """Find an F-curve by data path, with the optional index handled per version."""
    if action is None:
        return None
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        try:
            return legacy.find(data_path, index=index)
        except TypeError:
            return legacy.find(data_path)
    bag = action_channelbag(action, anim_data, slot)
    if bag is None:
        return None
    try:
        return bag.fcurves.find(data_path, index=index)
    except TypeError:
        return bag.fcurves.find(data_path)


def ensure_fcurve(action, data_path, index=0, owner=None, anim_data=None, slot=None):
    """Find or create an F-curve on ``action`` that belongs to ``owner``.

    The legacy collection comes first wherever it exists. Blender 4.4 and 4.5 have *both* APIs, and
    ``action.fcurve_ensure_for_datablock()`` there puts the curve into a channelbag of a slot of its own,
    which the legacy ``action.fcurves`` view - the one the rest of the addon, the UI and the bake operators
    read on those versions - does not see. Measured on 4.5.14: preferring the helper copied 0 of 8 curves
    for a shape-key action, while the legacy route copies all 8. From 5.0 the collection is gone, and that
    helper, then the channelbag route, are the only ways in.
    """
    if action is None:
        return None

    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        try:
            found = legacy.find(data_path, index=index)
        except TypeError:
            found = legacy.find(data_path)
        if found is not None:
            return found
        try:
            return legacy.new(data_path=data_path, index=index)
        except (TypeError, RuntimeError):
            return None

    ensure = getattr(action, "fcurve_ensure_for_datablock", None)
    if ensure is not None and owner is not None:
        try:
            curve = ensure(datablock=owner, data_path=data_path, index=index)
        except (TypeError, RuntimeError):
            curve = None
        if curve is not None:
            return curve

    bag = ensure_action_channelbag(action, anim_data, slot)
    if bag is None:
        return None
    try:
        return bag.fcurves.new(data_path, index=index)
    except (TypeError, RuntimeError):
        return None


def action_groups(action, anim_data=None, slot=None):
    """The collection of curve groups, legacy or channelbag (may be None)."""
    if action is None:
        return None
    legacy = getattr(action, "groups", None)
    if legacy is not None:
        return legacy
    bag = action_channelbag(action, anim_data, slot) or ensure_action_channelbag(action, anim_data, slot)
    return getattr(bag, "groups", None) if bag is not None else None


def ensure_group(action, name, anim_data=None, slot=None):
    """Find or create a named curve group for this owner."""
    groups = action_groups(action, anim_data, slot)
    if groups is None:
        return None
    existing = groups.get(name)
    if existing is not None:
        return existing
    try:
        return groups.new(name)
    except (TypeError, RuntimeError):
        return None


def set_action_id_root(action, id_type):
    """Set an action's target datablock type where the API still has ``id_root``.

    From 5.0 the type lives on the slot instead, so it is passed to
    :func:`ensure_action_slot` rather than written here.
    """
    if action is None or not hasattr(action, "id_root"):
        return False
    try:
        action.id_root = id_type
        return True
    except (TypeError, AttributeError):
        return False


def action_for_owner(owner):
    """The action an object / key datablock is animated by, or None."""
    anim_data = getattr(owner, "animation_data", None)
    return anim_data.action if anim_data is not None else None
