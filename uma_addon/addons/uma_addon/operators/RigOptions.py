"""The rig options and the metarig template that used to come from MikuMikuRig, shipped with this add-on instead.

`Pumpkin Rig Setup` used to need MikuMikuRig installed for three things, and this module is all three of them:

1. **the metarig template** - MMR's `operators/MMR_Rig.blend`, appended as the object `MMR_Rig_relative` and then
   handed to rigify. The copy shipped here is **byte-identical** to MMR 4.5-3.85's and to 5.2.2's (sha256
   `12374F03DAA8EBA5FD54AD57`, 285 696 bytes, measured against both releases), so the metarig every chain is
   inserted into is the same file it always was.
2. **the settings** our fork of MMR's rig pipeline reads - 21 of them, kept under **MMR's own attribute names,
   types and defaults**, read out of the installed MMR's RNA rather than retyped from its source. The names are
   deliberately unchanged because `operators/Controller.py` reads them in about two dozen places; what changed is
   only *where they live*: an object property of our own (`object.uma_mmr`) instead of MMR's (`object.mmr`), so
   this add-on and MMR can be installed side by side without either one's registration stepping on the other.
   One type did change: MMR's `presets` is a dynamic `EnumProperty` whose items come from a folder of preset files
   that only MMR's UI knows about, and every place this add-on touches it only copies the string to another object.
   Ours is a `StringProperty`.
3. **one MMR operator**, `mmr.import_default_weight_bone_parent`, which our controller called to seed the
   weight-bone-parent table. It clears a collection on the object and adds six fixed key/value pairs, so it is a
   function here (`fill_default_weight_bone_parent`) rather than an operator: same six pairs, same collection, no
   second add-on's namespace involved.

**Where this came from, and the licence.** Everything above is MikuMikuRig by 小峰峰哥 / XiaoFFGe
(<https://github.com/XiaoFFGe/MikuMikuRig>), **GPL-3.0**, vendored here under the same licence as the rest of this
add-on (`SPDX:GPL-3.0-or-later`). The version read for the settings was 4.5-3.85 (the release this project has been
running); the template was checked against 5.2.2 as well. `THIRD_PARTY.md` at the extension root carries the full
attribution, and **this file is a modification** of MMR's property definitions: names, types and defaults are MMR's,
the container is ours, `presets` changed type, and the one operator became a function. The rig pipeline itself
(`operators/Controller.py`) is a fork of MMR's `operators/RIG.py` and carries its own note.

Tags: `rig-options-1`.
"""
import os

import bpy
from bpy.props import (BoolProperty, BoolVectorProperty, CollectionProperty, FloatProperty, PointerProperty,
                       StringProperty)

#: What the console lines carry, so a report can say which revision of this file ran.
TAG = "rig-options-1"

#: The metarig template, beside this file, and the object name inside it that rigify is handed.
TEMPLATE_NAME = "MMR_Rig.blend"
TEMPLATE_OBJECT = "MMR_Rig_relative"





class WeightBoneParentItem(bpy.types.PropertyGroup):
    """One row of `object.uma_weight_bone_parent_fix`: the bone, and the bone its weights move to."""

    key: StringProperty(name="Bone")
    value: StringProperty(name="Parent")


class BoneOptions(bpy.types.PropertyGroup):
    """MMR's per-pose-bone group, which this add-on reads in exactly two places.

    Both are about `Set_constraints`, the three-flag vector MMR uses to decide which of a bone's constraints it may
    touch: `Controller.py` clears flag 1 for the toe bones when *Disable the toe position constraint* is on, and backs
    the whole vector up per bone before the rig is rebuilt. The other eight fields are MMR's damping-tracking and
    keyframe switches, carried so the group is the group MMR's own panel would show - **none of them is read by this
    add-on**. Names, types and defaults are MMR's, taken from the installed MMR's *runtime* (`PoseBone.mmr_bone`;
    asking its RNA instead reported a group with one field, which is wrong - the runtime is the authority here), and
    the container is ours (`PoseBone.uma_bone`) so MMR may stay installed beside us.
    """

    Set_constraints: BoolVectorProperty(
        name="Set constraints",
        description="Which of this bone's constraints MMR may touch. Flag 1 is the toe constraint this add-on "
                    "switches off when asked to",
        size=3,
        default=(True, True, True),
    )
    Constraint_bool: BoolProperty(default=False)
    Constraint_influence: FloatProperty(default=0.0)
    Damping_Tracking_bool: BoolProperty(default=False)
    Damping_Tracking_influence: FloatProperty(default=0.0)
    Disable_keep_transform: BoolProperty(default=False)
    Insert_constraint_keyframe: BoolProperty(default=False)
    Insert_dt_keyframe: BoolProperty(default=False)
    Use_manual_influence: BoolProperty(default=False)


#: The six pairs MMR's `mmr.import_default_weight_bone_parent` seeds, verbatim - the MMD D-bone to its weight parent.
DEFAULT_WEIGHT_BONE_PARENTS = (
    ("足D.R", "足.R"),
    ("足D.L", "足.L"),
    ("ひざD.R", "ひざ.R"),
    ("ひざD.L", "ひざ.L"),
    ("足首D.R", "足首.R"),
    ("足首D.L", "足首.L"),
)


def fill_default_weight_bone_parent(obj):
    """Clear the table on `obj` and refill it with MMR's defaults. Returns how many rows were written."""
    table = getattr(obj, "uma_weight_bone_parent_fix", None)
    if table is None:
        return 0
    table.clear()
    for key, value in DEFAULT_WEIGHT_BONE_PARENTS:
        item = table.add()
        item.key = key
        item.value = value
    return len(table)


class RigOptions(bpy.types.PropertyGroup):
    """The 22 settings this add-on's rig pipeline reads.

    Every `default` here is MMR's own, so a fresh install behaves exactly as a fresh MMR install did. The comments
    say what each one is for where this project's own notes establish it; the rest are MMR's switches, carried
    unchanged so the fork keeps reading the same names.

    `of()` is a staticmethod rather than a module function so the call sites read `RigOptions.of(obj)` - the class is
    the only thing `operators/Controller.py` imports from here. (Plain methods are fine on a `PropertyGroup`;
    Blender only inspects the *annotated* attributes.)
    """

    @staticmethod
    def of(obj):
        """The rig options of an object, or None if there is no such object.

        A registered `PointerProperty` is never None for a real object, so this is only a guard for the None object a
        cancelled operator can carry - and it is what replaces the old "MikuMikuRig is not installed" refusal: there
        is no second add-on to be missing any more.
        """
        return getattr(obj, "uma_mmr", None) if obj is not None else None

    @staticmethod
    def template_path():
        """The metarig template's full path. `bpy.ops.wm.append` takes the three parts separately at the call site."""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), TEMPLATE_NAME)

    # --- what to build ---------------------------------------------------------
    Generate_controllers: BoolProperty(
        name="Generate controllers",
        description="MMR's own switch for generating its controller set. This add-on sets it before handing over",
        default=False,
    )
    Only_meta_bones_are_generated: BoolProperty(
        name="Only meta bones",
        description="MMR generates only the metarig. This add-on clears it so the full rig is built",
        default=False,
    )
    MMR_Arm: BoolProperty(
        name="MMR arm",
        description="MMR's marker for the arm it generated",
        default=False,
    )
    mmd_Armature: PointerProperty(
        name="MMD armature",
        description="The MMD armature this run is rigging - what the rest of the settings are copied onto",
        type=bpy.types.Object,
    )

    # --- the shape of the generated rig ----------------------------------------
    Bend_the_bones: BoolProperty(
        name="Bend the bones",
        description="Let MMR bend the generated bones the way the MMD skeleton bends",
        default=False,
    )
    Bend_the_leg_bones: BoolProperty(
        name="Bend the leg bones",
        description="The same, for the legs only",
        default=False,
    )
    Bend_angle_arm: FloatProperty(
        name="Arm bend angle",
        description="How far a bent arm bone is bent, in degrees",
        default=6.0,
    )
    Bend_angle_leg: FloatProperty(
        name="Leg bend angle",
        description="How far a bent leg bone is bent, in degrees",
        default=2.5,
    )

    # --- hands and feet --------------------------------------------------------
    Enable_finger_IK: BoolProperty(
        name="Finger IK",
        description="Generate IK for the fingers, which is what the animator's hand controls drive",
        default=True,
    )
    f_pin: BoolProperty(
        name="Pin the fingers",
        description="MMR's finger pinning pass, run while the hand rolls are aligned",
        default=True,
    )
    Disable_hand_fix: BoolProperty(
        name="Disable the hand fix",
        description="Skip MMR's hand-correction pass",
        default=False,
    )
    Disable_toe_position_constraint: BoolProperty(
        name="Disable the toe position constraint",
        description="Skip the constraint that keeps a toe where the foot points",
        default=False,
    )
    Thumb_twist_aligns_with_the_world_Z_axis: BoolProperty(
        name="Thumb twist on world Z",
        description="Align the thumb's twist axis to the world Z axis instead of the world -Y axis",
        default=False,
    )
    Polar_target: BoolProperty(
        name="Polar target",
        description="Give the arm and leg IK a pole target",
        default=False,
    )
    Use_ITASC_solver: BoolProperty(
        name="Use the iTaSC solver",
        description="Set the IK chains to Blender's iTaSC solver instead of the default one",
        default=False,
    )

    # --- the model ------------------------------------------------------------
    Weight_bone_parent_fix: BoolProperty(
        name="Weight bone parent fix",
        description="Re-parent the weights of the MMD D-bones onto the bones the table below names",
        default=False,
    )
    ORG_mode: BoolProperty(
        name="ORG mode",
        description="Leave the metarig's ORG bones as the deform bones instead of generating a separate set",
        default=False,
    )
    Hide_mmd_skeleton: BoolProperty(
        name="Hide the MMD skeleton",
        description="MMR's own last step: hide the model's skeleton once the rig is on it",
        default=False,
    )

    # --- presets (MMR's UI owns the item list; this add-on only copies the value) ---
    Import_presets: BoolProperty(
        name="Import presets",
        description="Let MMR apply a stored preset while it builds the rig",
        default=False,
    )
    presets: StringProperty(
        name="Preset",
        description="The preset MMR would apply. MMR's own property is a dropdown over its preset folder; this is "
                    "the string, because this add-on only copies it from one object to another",
        default="",
    )
    json_filepath: StringProperty(
        name="Preset file",
        description="The preset file that goes with `Import presets`",
        default="",
    )
    Upper_body_linkage: BoolProperty(
        name="Upper body linkage",
        description="Let the torso's control leave the neck and head following it. Off keeps them following the "
                    "torso, which is MMR's own default",
        default=False,
    )
