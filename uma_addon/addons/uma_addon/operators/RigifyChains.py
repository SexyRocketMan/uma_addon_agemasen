"""An alternative controller setup that also rigifies the model's hair / cloth / tail chains.

Self-contained on purpose, like `CollapseRig.py`: the add-on's auto-loader registers any
`bpy.types.Operator` subclass it finds here, and the only other edit is the guarded import and two buttons
in `panels/AddonPanels.py`. Delete this file and those lines and the add-on is exactly as it was.

`Generate controller for umamusume` (MMR's rig, wrapped by the add-on) rigifies the torso, limbs, hands,
head and eyes, and drives the *whole* MMD skeleton through copy constraints. Every other chain in the model
- skirt, jacket, mantle, ribbon, belt, sleeve, tail, hair, ears - ends up with no control of its own, so
nothing can be animated on it. This operator keeps MMR's rig exactly as it is and adds those chains to the
metarig with real rigify rig types:

  * `spines.basic_tail` for chains that want tweak controls and a curl,
  * `limbs.spline_tentacle` for chains driven by a spline,
  * `basic.super_copy` for one control per bone with no chain.

How it gets in without touching `Controller.py`: rigify's generate operator calls
`rigify.generate.generate_rig(context, metarig)` through the module, so this operator temporarily wraps
that function. MMR fits the metarig, then calls rigify; the wrapper runs at exactly that moment - metarig
final, nothing generated yet - adds the chains to it, and hands over to the real function. That timing
matters: MMR moves and scales the metarig while fitting it, so chain bones added *before* fitting end up in
the wrong place and pull their MMD bones off the model (this file's first version did that, and the scene
showed it as hair, tail and cloth dragged downwards).

Because it does not need MMR's two-pass mode, it also avoids that mode's broken resume path in this build
(`Controller.py:1042`, `UnboundLocalError: cannot access local variable 'config'`).

Everything is undoable, nothing is deleted, the rigify function and the MMR settings this touches are
restored in a `finally`, and a trace of every step (with pass/fail checks) is written to a text block so a
user can send it back.

The run is configured by a `Mode` (one rig type per group, or one per family) and a `Preset` that fills those
fields. Two files hold the data, both under `<Blender config>/uma_addon/`: `rigify_chains.json` is the rules
table (name pattern -> rigify type, the fallback for a character with no preset) plus the run options, and
`presets/` holds one file per preset. `docs/PRESETS_AND_RULES.md` is the map; the handoff's section 9aa is how
it got that way.
"""
import json
import os
import re
import time

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, StringProperty
from mathutils import Matrix, Vector

# The rig display styles: one JSON file per style in `<config>/uma_addon/styles/`, his own style shipped as the
# default. Display only - the data, the apply, and the capture all live in `RigStyles` (handoff 9al/9am).
from .RigStyles import (STYLE_NONE, apply_style, capture_style, describe_style, ensure_style_files, forget_styles,
                        find_generated_rig, read_style, save_style, style_dir, style_filename, style_files,
                        style_items)

# rigify's own prefixes, used to recognise the armature it generated
GENERATED_PREFIXES = ("ORG-", "DEF-", "MCH-")
# Rigify types that are **not** chains: the rule is applied to every bone it governs rather than to one chain
# root. This is how the animator's own metarig does it - for cloth and for hair he types `Sp_He_Hair2_00_L`
# *and* `Sp_He_Hair2_01_L` each as `basic.super_copy`, one control per bone, where a chain type is declared once
# on the root and rigify walks the connected children itself (handoff 9r-9s). Kept as an explicit set, so a rule
# naming anything else behaves exactly as it did before this existed.
PER_BONE_RIG_TYPES = {"basic.super_copy", "basic.raw_copy"}
# the collection the model's own bones are gathered into after a run, so they can be switched off as a set
MODEL_COLLECTION_NAME = "MMD"
# where the model's own bones are made to follow the new chains
FOLLOW_CONSTRAINT_NAME = "UMA_chain_follow"
ROTATE_CONSTRAINT_NAME = "UMA_chain_rotate"
BBONE_SEGMENTS = 8
# How close a metarig bone has to be to the model bone's own parent to be used as the chain root's
# attachment. MMR's metarig has no bone for some model parents (Sp_He_Ear0_L_00, which the animator's own
# pipeline deletes), and a fixed fallback put the ears on the spine; the nearest bone inside this radius is
# the head for the ears, which is where his metarig attaches them.
PARENT_MATCH_RADIUS = 0.15      # what rigify gives the deform chain of a basic_tail / spline_tentacle rig
TRACE_TEXT_NAME = "uma_rigify_chains_trace.txt"
# The build tag, and what this build changes in one paragraph. Both are written into every trace header, and the
# fleet plus `double_run_check.py` read the tag from that line - so the tag is *prepended* to BUILD rather than
# typed into it twice: hand-keeping "first word of BUILD == BUILD_TAG" in sync failed the first time the tag was
# bumped (measured: a chains-41 build reported chains-40 for all 125 models of the strip-a fleet, because the
# first word of this string was still the previous build's entry).
#
# The history that used to live here - 227 lines of it, one paragraph per build going back to chains-8 - was also
# printed into every trace, which is a lot of prose in the one line whose job is to say which build ran. It is all
# in `docs/RIGIFY_CHAINS_HANDOFF.md` (9a-9av), which is where it is readable. Bump BUILD_TAG on every change -
# including changes in the *other* files: chains-45 to chains-50 were sections, the shading panel's two views, the
# master switch and resets, the presets fix and the action panel, none of which touch this module, and the tag was
# left at chains-44 until the shipping pack showed it. The tag is what a tester checks a trace against, so it names
# the whole build, not this file.
BUILD_TAG = "chains-53"
BUILD = BUILD_TAG + (
    ": 2.13.1 - the Physics & Action panel stops writing scene properties while it draws (Blender "
    "5.x refuses ID writes in a draw: \"Writing to ID classes in this context is not allowed\", which "
    "is why the action list never arrived, and the fill is a one-shot timer now), the heart button is "
    "disabled through its row instead of by assigning `.enabled` to the object `layout.operator()` "
    "returns (\"'UMA_OT_toggle_favorite' object has no attribute 'enabled'\", which ended the whole "
    "draw), and the face-normal step refreshes its static copy when the face's topology has changed "
    "under it - a separated material slot used to leave a copy on the old topology for ever, with the "
    "transfer erroring on every evaluation and re-running the step unable to repair it. Before that: "
    "2.13.0 - an Uninstall button in the add-on's own preferences panel. Blender draws one in "
    "that list only for a *legacy* add-on (bl_pkg/bl_extension_ui.py:242-247), so an extension install "
    "had none anywhere a user would look; the button runs extensions.package_uninstall for an "
    "extension and preferences.addon_remove for a legacy copy, names the folder in a confirmation "
    "dialog first, and reports a read-only system repository instead of trying. Before that: 2.12.0 - "
    "\"Agemasen edition\" (the manifest, the README and the line this addon prints at load), a "
    "second maintainer, and the face-normal step shipping on by default beside the shading options: a hidden "
    "static copy of the face with its custom normals transferred, so a morph stops dragging the shading with "
    "it, and the scene checkbox turns it off. Before that: the shading control shipped as 2.11.0 - one place to "
    "set the look of every uma (global values on the sun "
    "empty, per-character overrides on her root empty, a driver per setting per material copy), a panel that "
    "follows the selection with sections matching the shader's own layout, a master override switch and resets. "
    "Before that: the rig presets work again (their dropdown's update callback is handed Blender's properties "
    "wrapper, not the operator, so `sync_preset` called a method that was not there), the action library lists and "
    "applies again, a leftover MMR metarig template is refused up front, and the addon is smaller - the Tanuki GIF "
    "shader nodes, Pillow, the pip buttons and the install-time network permission are gone, along with Change Head "
    "(52 registered operators and 4 panels, against 55 and 5 - counted by panel_census_probe.py on the shipped tree, because the 48 that used to be written here stopped being true when the shading control's seven operators landed)")

# What the rules fall back to when no file exists. A type rule matches at the start of a bone name, a skip
# rule matches anywhere, and the first rule that matches a bone wins. Patterns are matched against the
# model's bone names. `min_bones` guards rigify: a chain rig needs at least this many connected bones.
#
# This table is the animator's own baseline, read off his pre-rigged Maruzensky metarig (248 bones, 172
# typed): he chains only these families - ears, two hair families, the head and skirt ribbons, the skirts
# and the tail - and leaves every other cloth bone as a plain `basic.super_copy` stub with no chain. Types
# are his: `spines.basic_tail` where he wants tweak controls that bend the chain, `limbs.spline_tentacle`
# where he wants a spline through the tweak bones (that is the curve visible in his rig's UI, and the tweak
# bones are what moves it - rotating such a chain's master control does nothing on his rig either).
#
# His `spines.basic_tail` roots all override one rigify parameter, and it decides what pushing a control
# does. Measured on his metarig (every one of his 18 basic_tail roots, in mmr_out/rigify_chain_audit.json):
# `copy_rotation_axes = (True, True, True)`, where rigify's default is `(True, False, False)`. The default
# copies only each bone's local X rotation down the chain, so a push about anything else moves the chain
# rigidly: pushing my `Ear_01_R` about its own Z turned all three ear bones by 28.65 deg (no curl at all,
# mesh max 0.05041 m), where the same push on his rig turned them 28.65 / 57.30 / 85.94 deg (mesh 0.09314 m).
# That is the reported "his ears curl like a spline and mine do not". BASIC_TAIL_PARAMETERS below is applied
# to every basic_tail chain root a rule does not say otherwise for.
BASIC_TAIL_PARAMETERS = {"copy_rotation_axes": (True, True, True)}

# Printed once per module load, so the console shows whether a reload actually picked up an edited file.
print("[uma_rigify_chains] %s loaded" % BUILD_TAG)

# ---- is the module in memory still the file on disk? ---------------------------------------------------------
# Blender keeps a loaded add-on in memory: an edited file on disk changes nothing until the extension is
# reloaded or Blender restarts. That single failure mode has cost this workstream the most - a build ran and was
# verified under the *previous* tag three times (handoff 9v, 9w, 9ac), and a round measured a half-written file
# twice - and printing the tag in the trace header was not enough to catch it, because the header is printed *by*
# the stale module. Recording the file's modification time when this module is imported and comparing it when
# the operator runs catches the next run instead, whatever the tag says. The user's "generation fails, then it
# was fine when I ran it again" report is this failure mode too (handoff 9ad).
SOURCE_PATH = os.path.abspath(__file__) if "__file__" in globals() else ""
SOURCE_MTIME = os.path.getmtime(SOURCE_PATH) if SOURCE_PATH and os.path.exists(SOURCE_PATH) else None


def stale_source():
    """`None` when this module is the file on disk, else a sentence saying what is loaded and what the file says."""
    if not SOURCE_PATH or SOURCE_MTIME is None or not os.path.exists(SOURCE_PATH):
        return None
    try:
        changed = abs(os.path.getmtime(SOURCE_PATH) - SOURCE_MTIME) > 1e-6
        with open(SOURCE_PATH, encoding="utf-8") as handle:
            head = handle.read(8192)
    except OSError:
        return None
    match = re.search(r'^BUILD_TAG = "([^"]+)"', head, re.M)
    file_tag = match.group(1) if match else None
    if not changed and (file_tag is None or file_tag == BUILD_TAG):
        return None
    if file_tag is not None and file_tag != BUILD_TAG:
        return ("Blender is running the %s module, which was loaded before the file on disk was replaced; the "
                "file now says %s" % (BUILD_TAG, file_tag))
    return ("Blender is running the %s module, which was loaded before the file on disk was edited (same tag, so "
            "the tag cannot tell you): the code in memory is not the code on disk" % BUILD_TAG)

DEFAULT_RULES = {
    "version": 2,
    "comment": ("Baseline: the chain families and rig types of the animator's own Maruzensky metarig. "
                "A type rule matches at the start of the bone name, a skip rule anywhere, and the first "
                "rule that matches a bone wins."),
    "rules": [
        {"pattern": "_Handle$", "type": "", "skip": True},
        # `basic_tail` roots carry his `copy_rotation_axes` setting explicitly (see BASIC_TAIL_PARAMETERS
        # below); `spline_tentacle` has no such parameter, so its rules stay bare. `parent` names the metarig
        # bone a chain root hangs off: the head-attached families say `Head` because that is what his own
        # metarig does (COOLCHOU_RIG.md 3/5), and leaving it to the nearest-bone fallback attached the ears
        # to their temple bone instead.
        {"pattern": "^Ear_0", "type": "spines.basic_tail", "collection": "Ears", "min_bones": 2,
         "parent": "Head", "parameters": {"copy_rotation_axes": [True, True, True]}},
        {"pattern": "^Sp_He_Hair2", "type": "spines.basic_tail", "collection": "Hair", "min_bones": 2,
         "parent": "Head", "parameters": {"copy_rotation_axes": [True, True, True]}},
        {"pattern": "^Sp_He_Hair4", "type": "limbs.spline_tentacle", "collection": "Hair",
         "min_bones": 2, "parent": "Head"},
        {"pattern": "^Sp_He_Ribbon0", "type": "limbs.spline_tentacle", "collection": "Hair",
         "min_bones": 2, "parent": "Head"},
        {"pattern": "^Sp_Hi_Ribbon0", "type": "limbs.spline_tentacle", "collection": "Extras",
         "min_bones": 2},
        {"pattern": "^Sp_Hi_Tail0", "type": "limbs.spline_tentacle", "collection": "Tail",
         "min_bones": 2, "parent": "Hip"},
        {"pattern": "^Sp_Hi_MSkirt", "type": "spines.basic_tail", "collection": "Skirt", "min_bones": 2,
         "parameters": {"copy_rotation_axes": [True, True, True]}, "chain_deform": "bbone"},
        # One chest bone has no chain to belong to: the front chest ribbon is a single weighted bone, and a
        # chain rig type refuses a one-bone chain (rigify's `min_chain_length` is 2). It gets the animator's
        # own bare stub instead - `basic.super_copy` with the widget off, which is exactly how his metarig
        # types the whole chest cloth - and is pinned to `Chest` the same way, so it is released and wired
        # like the rest. `min_bones: 1` is what lets it through the collector; the weight filter keeps a
        # single-bone chain as it is because it never trims below its own floor of two.
        {"pattern": "^Sp_Ch_Ribbon0_F", "type": "basic.super_copy", "collection": "Chest", "min_bones": 1,
         "parent": "Chest", "parameters": {"make_widget": False}},
        # The chest cloth: jacket, collar, bust, and the front / side chest ribbons. The animator types
        # these as `basic.super_copy` stubs, which this operator cannot place yet, so they are chains here
        # instead - and that is what fixes the user's report "the chest is pulled in the opposite direction
        # when the neck moves" (docs/RIGIFY_CHAINS_HANDOFF.md 9q). Those bones carry no control at all and
        # keep `Chest` as their parent, so they inherit the gross scale MMR's proxy puts on `Chest` when a
        # `neck` translation stretches rigify's spine segment: a 0.1 m push up scales `Chest` to
        # (0.72, 1.91, 0.72) and the same push down to (2.90, 0.12, 2.90), which sent 26 of 40 bones
        # *against* the push. Releasing them and following an `ORG-` copy instead removes it outright
        # (measured: 26 bones against the push -> 0, and the mesh maximum for a 0.1 m push 0.22149 -> 0.13567 m).
        # Two bones per chain is the floor rigify accepts, and *both* bones have to be in the chain: `_00` is
        # the bone that inherits the distortion even where it carries no weight, and the weight filter keeps
        # an unweighted root rather than trimming the chain to one bone.
        {"pattern": "^Sp_Ch_", "type": "spines.basic_tail", "collection": "Chest", "min_bones": 2,
         "parent": "Chest", "parameters": {"copy_rotation_axes": [True, True, True]}},
        # `1057_00`'s knee pads, and the one real failure left on 125 models: the model names `Knee_L`/`Knee_R`
        # as their parent, MMR's metarig has no bone there, and the nearest is `shin.L`/`shin.R` at 0.3418 m - so
        # every run attached them there and then failed its own check, four times, honestly. Naming the metarig
        # bone turns that guess into a decision: `shin.L` *is* MMD's calf, which is the bone a knee pad belongs
        # to, and the trace now says "attached to shin.L by rule" instead of reporting a distance. Two rules, one
        # per side, because a rule's `parent` is one bone and `resolve_rule_modes` resolves per *bone name*, not
        # per family (`Sp_Kn_Pantaloons0` holds both sides); the type is what the `unmatched_families` fallback
        # already gave these families, so the rig they produce is unchanged (handoff 9am).
        {"pattern": "^Sp_Kn_Pantaloons\\d*_L", "type": "basic.super_copy", "parent": "shin.L"},
        {"pattern": "^Sp_Kn_Pantaloons\\d*_R", "type": "basic.super_copy", "parent": "shin.R"},
        # he rigifies every other cloth bone as a plain follow stub; this operator leaves those to MMR, so
        # they get no control of their own (see the stub option in the docs for matching him exactly)
        {"pattern": "^Sp_", "type": "", "skip": True},
    ],
    "options": {
        "parent_fallback": "spine",
        "connect_chain": True,
        "require_weights": True,
        "release_parents": True,
        # His chains deform through a *connected, 8-segment* B-Bone deform chain and his mesh is skinned
        # straight to it, where ours is skinned to the model's own bones. `chain_deform` is per rule:
        # "bbone" keeps the chain's own hierarchy (only the root is released), gives every bone the same
        # 8 segments and drives the children by rotation; "rigid" releases everything and follows the copy
        # outright. Measured on this character: "bbone" is what makes the animator's skirt tip tweaks
        # respond at all (10 dead controls -> 0), while on the ears the same treatment turned a middle-bone
        # push into 2.26x and froze the tip in the pair test - so the skirt rule opts in above and every
        # other family keeps the verified rigid behaviour.
        "chain_deform": "rigid",
        # Families no mode may rig, whatever the setup asks for. His pipeline deletes `Sp_He_Ear0` and no vertex
        # in the model is weighted to it (handoff 9m), so `Setup = Simple` - which chains every soft part - must
        # not bring it back: without this, Simple produced two chains that the weight filter then dropped again
        # ("chains dropped for carrying no weights: 2"). Data rather than a special case in the resolver.
        "never_rig": ["^Sp_He_Ear0"],
        # Families his own rigs never vary, measured over all 241 of them: the ears are a `spines.basic_tail`
        # chain in 241 of 241. Only the ears are fixed. The *tail* was fixed for the same measured reason
        # (241 of 241 splines) until a model turned up whose tail is a 0.08 m chain and whose spline control
        # therefore lands 0.136 m - 1.7x the chain's own length - away from the geometry
        # (`1001_Special Week.pmx`, handoff 9ac). A constant is only kind while it is right, and that one is
        # not right on every export: the tail keeps a field, defaulting to the spline his rigs use.
        "fixed_families": {"Ear": "spines.basic_tail"},
        # What a preset keyed for *another costume* of the same character may decide. The autodetect that picks
        # one compares the model's hair skeleton (`detect_preset`), so when it fires the head bone set agrees -
        # but costumes differ in their physics and accessories (measured: `1001_90` has no `Sp_Hi_Ribbon0` at all,
        # `docs/RIG_HANDOFF.md` §5), and a skeleton match says nothing about those, so by default only the hair
        # answers are taken and the rest stay with the rules table (which has the `unmatched_families` fallback
        # behind it). "all" restores the old behaviour, "exact" refuses a cross-costume preset entirely.
        "preset_cross_costume": "hair",
        # What a family the table says nothing about gets, instead of nothing. The table names the families the
        # animator's Maruzensky metarig carries; his other 154 packed rigs name many more (`Sp_He_Hair0/1/3/5`,
        # `Sp_He_Acc0`, the sleeves, `Sp_Si_Jacket0`, `Sp_Hi_CSkirt0`, belts, pantaloons, accessory ribbons), and
        # every one of them used to fall through the `^Sp_` catch-all in silence - no control, not even a message
        # (measured over the 125-model fleet: 778 families / 3521 bones left to MMR, which is what this answers).
        #
        # The type is his own dominant answer for exactly these families, counted over the 155 packed rigs in
        # `mmr_out/packed_typing_v025.json` (rigs: type): `Sp_He_Hair0` stub 133 / basic_tail 18, `Hair1` 52/9,
        # `Hair3` 71/24/4 (spline), `Hair5` 24/13/8, `Sp_He_Acc0` 37/6/2, `Sp_Wa_Ribbon0` 21, `Sp_Kn_Ribbon0`
        # 11 of 11, `Sp_Th_Acc0` 8 of 8, `Sp_Ko_Acc0`-style accessories all stub. `basic.super_copy` is also the
        # type that cannot invent a chain where the model has none: one control per bone, which is what a
        # family with no rule to describe it deserves. Only the sleeves and `Sp_Hi_Belt0` come close to
        # contesting it (17 basic_tail against 12 stubs for `Sp_El_Sleeve0`), and there the group dropdown is
        # the place to say so per character.
        #
        # No `collection`: the group then comes from the family's own name (`family_group`), so a head family
        # still lands in `Hair` and a waist ribbon in `Extras` instead of all of them in one bucket. The
        # resolver applies this per family, so every one of them keeps its own row in Advanced and its own
        # answer in the Simple group dropdowns.
        "unmatched_families": {"type": "basic.super_copy"},
        # "keep" copies the model's own joint spacing; "even" puts one joint every equal step along the
        # same line; "leaf" keeps the model's joints and extends only the last bone to the longest segment,
        # which is what the animator's own metarig does to the ears (his three ear bones are 0.03571 m each
        # with their heads on the model's own joints, where the model's leaf is 0.01785 m). Measured in
        # docs/RIGIFY_CHAINS_HANDOFF.md 9a-9b; "keep" stays the default because that is the verified
        # baseline and "even"/"leaf" change where the mesh bends.
        "resample": "keep",
    },
    # Empty on purpose: the profile table is per-character data generated from the animator's own rigs
    # (`build_rules_profiles.py`), so the shipped file carries the key and its shape, not 69 entries that
    # would be wrong for the next character. See the comment on `load_profiles`.
    "profiles": {},
}


def config_path():
    """Where the rules file lives: the Blender config, not the (possibly read-only) add-on folder."""
    directory = bpy.utils.user_resource("CONFIG", path="uma_addon", create=True)
    return os.path.join(directory, "rigify_chains.json")


def load_rules(path=None, force=False):
    """Rules from disk, or the built-in defaults if there is no usable file. Returns (rules, options, source).

    The file's `options` are merged *over* the built-in ones rather than replacing them, so a rules file
    written by an older build keeps working and a new option (`never_rig`) has a default it does not have to
    name.

    Cached on the file's `(mtime, size)`: this is read from `draw` as well as from a run, and it is the same
    file every time.
    """
    path = path or config_path()
    try:
        info = os.stat(path)
        stamp = (path, info.st_mtime, info.st_size)
    except OSError:
        stamp = (path, 0.0, 0)
    if not force and _RULES_CACHE["stamp"] == stamp and _RULES_CACHE["rules"] is not None:
        return (_RULES_CACHE["rules"], _RULES_CACHE["options"], _RULES_CACHE["source"])
    options = dict(DEFAULT_RULES["options"])
    rules, source = None, "built-in"
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            # A JSON file can be valid and still be the wrong *shape*: `[]`, `"text"` or `{"rules": {}}` all parse,
            # and `data.get("rules", [])` on them either raises AttributeError or yields something that is not a
            # list of rules. That used to take the whole dialog down with no message - `_refresh_families` calls
            # this from `invoke`, so the panel never opened (found by the beta code review, handoff 9aq). A file
            # that is not an object is reported and ignored, exactly like a file that will not parse.
            if not isinstance(data, dict):
                print("[rigify_chains] %s is not a JSON object (it is %s); using the built-in rules"
                      % (path, type(data).__name__))
                data = {}
            loaded = [rule for rule in (data.get("rules") or []) if isinstance(rule, dict) and rule.get("pattern")]
            if loaded:
                rules = loaded
                extra = data.get("options")
                if isinstance(extra, dict):
                    options.update(extra)
                source = path
        except (OSError, ValueError, AttributeError, TypeError) as error:
            print("[rigify_chains] could not read %s (%s); using the built-in rules" % (path, error))
    if rules is None:
        rules = [dict(rule) for rule in DEFAULT_RULES["rules"]]
    _RULES_CACHE.update({"stamp": stamp, "rules": rules, "options": options, "source": source})
    return rules, options, source


def write_default_rules(path=None):
    path = path or config_path()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(DEFAULT_RULES, handle, indent=2)
    return path


# --------------------------------------------------------------------------------------------------------
# The groups, the families, and the presets (handoff 9y)
#
# The user chooses one rig type per **group** in Simple mode and one per **family** in Advanced mode, and a
# **preset** fills those choices in. Nothing else decides anything: the rules table is what the `Default`
# preset is built from, and the animator's own rigs are what the named presets are built from. That is the whole
# model - there is no "match his rig, which means his rig or the table depending on the character" left.
#
# A *group* is where a family sits on the body, and four of them are the bone collections the rig already shows
# in the viewport (`Hair`, `Skirt`, `Chest`, `Extras`). `Other` is the fifth: the sleeves, gloves, belts,
# pantaloons and accessories that his own rigs stub (measured over 241 rigs: 178 families he never varies, and
# almost all of them are these). They have no collection today because the rules table leaves them to MMR - in
# Advanced they can be given one.
#
# One family is **fixed**, because his rigs never vary it (241 of 241): the ears are always a
# `spines.basic_tail` chain. It is shown as a locked row in Advanced - so nobody has to wonder where the ears
# went - and no mode or preset can change it. The tail used to be fixed too and is not any more (handoff 9ac/9ad):
# it has a `Tail` row like every other group.
GROUP_ORDER = ("Hair", "Skirt", "Chest", "Tail", "Extras", "Other")

# Which group a physics family belongs to. The rules table answers first, where it has a rule for the family,
# so a family does not change group under a user's feet; the prefixes cover the names only his own rigs use.
GROUP_PREFIXES = (
    ("Sp_He_", "Hair"),          # head: hair, head ribbons, head accessories
    ("Tp_He_", "Hair"),
    ("Sp_Ch_", "Chest"),         # chest cloth: jacket, collar, bust, chest ribbons
    ("Sp_Hi_MSkirt", "Skirt"),
    ("Sp_Wa_MSkirt", "Skirt"),
    ("Tp_Hi_MSkirt", "Skirt"),
    ("Sp_Hi_", "Extras"),        # hip ribbons, leaves, pareos, hip accessories
    ("Sp_Ne_", "Extras"),        # neck ribbon
    ("Sp_Wa_", "Extras"),        # waist ribbons
    ("Ex_", "Extras"),           # extra props
)
# anything else `Sp_` / `Tp_` is a limb accessory - sleeves, gloves, watches, belts, pantaloons - and his own
# rigs stub almost all of them (178 families he never varies across 241 rigs)
GROUP_FALLBACK = "Other"

MODE_OFF = 'OFF'
MODE_STUB = 'STUB'
MODE_TAIL = 'TAIL'
MODE_SPLINE = 'SPLINE'

MODE_TYPES = {
    MODE_OFF: None,
    MODE_STUB: "basic.super_copy",
    MODE_TAIL: "spines.basic_tail",
    MODE_SPLINE: "limbs.spline_tentacle",
}

# The reverse, for reading a preset: the animator only ever uses these four types, so anything else is reported
# rather than guessed at.
TYPE_MODES = {
    None: MODE_OFF,
    "": MODE_OFF,
    "basic.super_copy": MODE_STUB,
    "basic.raw_copy": MODE_STUB,
    "spines.basic_tail": MODE_TAIL,
    "limbs.spline_tentacle": MODE_SPLINE,
}

# The dropdown labels are rigify's own type names, because that is what the animator configuring this reads and
# writes: every one of these is a type he can set in his own metarig, and a preset file stores exactly that string.
# The plain-English sentence stays in the tooltip, so the dropdown is still readable to somebody who does not know
# rigify's taxonomy. Asked for by the user: *"I'd also think we should use the rigify rig type names in the Pumpkin
# Rig menu - that will be clearer to the experienced animators."*
TYPE_CHOICES = [
    (MODE_OFF, "none", "No rig type: the bones keep MMR's behaviour and get no control of their own. This is the "
                       "`^Sp_` catch-all in the rules table"),
    (MODE_STUB, "basic.super_copy", "One control per bone, no chain. Rigify's `basic.super_copy`: the type the "
                                    "animator uses for cloth and hair he does not want to chain"),
    (MODE_TAIL, "spines.basic_tail", "A chain per root with tweak controls, curling when a control is pushed. "
                                     "Rigify's `spines.basic_tail`, which his own metarig uses for the ears and "
                                     "the hair he does chain"),
    (MODE_SPLINE, "limbs.spline_tentacle", "A chain driven by a spline, posed at its `_master` bone. Rigify's "
                                           "`limbs.spline_tentacle`, which his metarig uses for the big hair and "
                                           "the tail"),
]

SIMPLE_GROUPS = ("Hair", "Skirt", "Chest", "Tail", "Extras", "Other")
PRESET_DEFAULT = "default"
PRESET_CUSTOM = "custom"


# The presets and the rules table are read from disk while the dialog is open - `preset_items` builds the enum
# on every redraw, `sync_preset` and `preset_mismatch` run in `draw` - and reading 99 JSON files each time made
# clicking the preset list stutter. Both are cached against a stamp of the folder (file count + its mtime), and
# anything that writes a preset clears the cache, so the only case the stamp misses is a file edited outside
# Blender, which the `Reload presets` button covers.
_PRESET_CACHE = {"stamp": None, "presets": {}, "loaded": False}
_RULES_CACHE = {"stamp": None, "rules": None, "options": None, "source": None}


def preset_stamp():
    """`(directory, how many entries, the directory's mtime)` - one `listdir`, no JSON parsing."""
    directory = preset_dir()
    try:
        entries = os.listdir(directory)
    except OSError:
        return (directory, 0, 0.0)
    try:
        stamp = os.path.getmtime(directory)
    except OSError:
        stamp = 0.0
    return (directory, len(entries), stamp)


def forget_presets():
    """Drop the cache, so the next read comes off disk. Called by anything that writes a preset."""
    _PRESET_CACHE["stamp"] = None
    _PRESET_CACHE["presets"] = {}
    _PRESET_CACHE["loaded"] = False
    _RULES_CACHE["stamp"] = None


def preset_dir():
    """Where one preset lives per file: `<Blender config>/uma_addon/presets/`."""
    return bpy.utils.user_resource("CONFIG", path=os.path.join("uma_addon", "presets"), create=True)


PRESET_README = """\
Presets for the uma_addon Pumpkin Rig Setup
===========================================

One file per preset. The file name is *not* significant - the `key` inside it is - but the
generated ones are named after the character and costume they came from, e.g. 1004_00.json.

What a preset is
----------------
A list of rig types, one per bone family:

    "families": {
      "Sp_He_Hair0": "basic.super_copy",     one control per bone, no chain
      "Sp_He_Hair2": "spines.basic_tail",    a chain that curls when a control is pushed
      "Sp_He_Hair4": "limbs.spline_tentacle" a chain driven by a spline
    }

Picking one in Blender fills the rig type fields and stops there - what the panel shows is
what the rig gets. Ears and the tail have no field: his rigs never vary them (241 of 241
measured), so they are fixed in the operator.

Where these came from
---------------------
Most of them were read out of the animator's own rigify metarigs - one per character and
costume - so `source` names the metarig they came from and `signature` is the hair skeleton
they were checked against. `"source": "saved in Blender"` means somebody made it here.

The folder next to this one
---------------------------
`../rigify_chains.json` is the *rules table*: bone name patterns -> rig type, plus the options
a run uses. It is what answers for a character with no preset, and what the "Default" preset
is built from. Editing it changes every run that has no preset, so it is not where a
per-character preference belongs - that is what a preset file is for.
"""


def write_preset_readme():
    """A plain-language note in the folder itself, so it explains itself to whoever opens it."""
    path = os.path.join(preset_dir(), "README.txt")
    if os.path.isfile(path):
        return path
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(PRESET_README)
    except OSError:
        return None
    return path


def preset_filename(key):
    """A preset key as a file name: `1004_00` stays, a metarig name loses what a file system dislikes."""
    cleaned = "".join(character if (character.isalnum() or character in "._-") else "_" for character in key)
    return cleaned.strip("._") or "preset"


def preset_files():
    """Every preset in the folder, as `{key: entry}`. A file that will not parse is skipped, not fatal."""
    presets = {}
    try:
        names = sorted(os.listdir(preset_dir()))
    except OSError:
        return presets
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(preset_dir(), name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                entry = json.load(handle)
        except (OSError, ValueError) as error:
            print("[rigify_chains] could not read preset %s (%s)" % (path, error))
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("families"), dict):
            continue
        key = entry.get("key") or os.path.splitext(name)[0]
        entry = dict(entry)
        entry["file"] = path
        presets[key] = entry
    return presets


def save_preset(key, entry):
    """Write one preset to its own file. Returns the path."""
    write_preset_readme()
    if not key:
        raise ValueError("a preset needs a key")
    payload = dict(entry)
    payload["key"] = key
    path = os.path.join(preset_dir(), preset_filename(key) + ".json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    forget_presets()
    return path


def legacy_presets(data):
    """The `profiles` / `presets` sections of a rules file, as `{key: entry}`. May be empty.

    Guards the shape of `data` as well as of its sections: a rules file that parses to a list or a string used to
    reach `data.get` and raise from `execute` (found by the beta code review, handoff 9aq).
    """
    if not isinstance(data, dict):
        return {}
    legacy = data.get("presets") or data.get("profiles") or {}
    if not isinstance(legacy, dict):
        legacy = {}
    entries = dict(legacy)
    # the entries that could not be keyed to a character id are a *list* (source name + signature only)
    unmapped = data.get("profiles_unmapped") or []
    if isinstance(unmapped, dict):
        for key, entry in unmapped.items():
            entries.setdefault(key, entry)
    else:
        for entry in unmapped:
            if isinstance(entry, dict) and entry.get("source"):
                entries.setdefault(entry["source"], entry)
    return entries


def ensure_preset_files(trace=None):
    """One-time: move the presets out of the rules file into one file each, then tidy the rules file up.

    Called from `execute`, never from `draw`: it writes files and rewrites the rules file, and `preset_items`
    runs on every redraw. The rules file keeps a timestamped backup next to it before anything is removed, so
    the move is reversible without a version control system.
    """
    if preset_files():
        return 0
    path = config_path()
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return 0
    entries = legacy_presets(data)
    if not entries:
        return 0
    write_preset_readme()
    written = 0
    for key, entry in entries.items():
        if not isinstance(entry, dict) or not entry.get("families"):
            continue
        try:
            save_preset(key, entry)
            written += 1
        except (OSError, ValueError) as error:
            if trace is not None:
                trace.info("could not write preset %s: %s" % (key, error))
    if not written:
        return 0
    backup = os.path.join(os.path.dirname(path), "rigify_chains.before_preset_files.json")
    try:
        if not os.path.isfile(backup):
            with open(backup, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
        for section in ("profiles", "presets", "profiles_unmapped"):
            data.pop(section, None)
        data["presets_live_in"] = "presets/ - one file per preset; this file is the rules table and options"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
    except OSError as error:
        if trace is not None:
            trace.info("could not tidy %s: %s (the presets are written, so this is cosmetic)" % (path, error))
        return written
    if trace is not None:
        trace.info("%d presets moved out of %s into %s, one file each; the rules file keeps the table and the "
                   "options, and the previous version of it is saved as %s"
                   % (written, os.path.basename(path), preset_dir(), os.path.basename(backup)))
    return written


def load_profiles(path=None, force=False):
    """Every preset, as `{key: {"families": {family: rigify type}, ...}}`. Read-only, cached, never raises.

    One file per preset in `<Blender config>/uma_addon/presets/` - a preset is a per-character preference, so it
    belongs in its own small file that can be added, edited, copied to someone else or deleted on its own,
    rather than as a key inside the rules file next to the pattern table and the run options. A rules file that
    still carries the old `profiles` section is read from as a fallback; `ensure_preset_files` moves it out.

    Cached against `preset_stamp()`, because this is called from `draw` (`preset_items`, `sync_preset`,
    `preset_mismatch`) and reading 99 files per redraw made the preset list stutter.
    """
    stamp = preset_stamp()
    # `_PRESET_CACHE["loaded"]` rather than a truthiness test on the dict: "no presets yet" is a valid answer that
    # an empty dict also spells, so on a fresh install every redraw re-listed the folder *and* re-read the rules
    # file for the legacy fallback - the stutter this cache exists to prevent (code review, handoff 9ai).
    if not force and _PRESET_CACHE["loaded"] and _PRESET_CACHE["stamp"] == stamp:
        return _PRESET_CACHE["presets"]
    presets = preset_files()
    if not presets:
        path = path or config_path()
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError) as error:
                print("[rigify_chains] could not read presets from %s (%s)" % (path, error))
                data = {}
            presets = {key: entry for key, entry in legacy_presets(data).items()
                       if isinstance(entry, dict) and entry.get("families")}
    _PRESET_CACHE["stamp"] = stamp
    _PRESET_CACHE["presets"] = presets
    _PRESET_CACHE["loaded"] = True
    return presets


def preset_label(key, entry):
    """What to call a preset on screen: its own name, else the rig it came from, else the key.

    `source` is a metarig name (`Meta_Base_Head_Maruzensky`), which is where the data came from rather than
    anything about the character - so it is shown as a readable name (`Maruzensky`) and the key (`1004_00`) is
    what identifies it. Nothing here is Maruzensky-specific: she is simply the character with the most rigs in
    the pack, and therefore the one most of the derived presets were checked against.
    """
    if entry.get("name"):
        return str(entry["name"])
    source = str(entry.get("source") or "")
    for prefix in ("Meta_Base_Head_", "Meta_", "RIG-"):
        if source.startswith(prefix):
            source = source[len(prefix):]
            break
    return source.replace("_", " ").strip() or key


def preset_modes(entry):
    """A preset's `{family: rigify type}` as `{family: MODE_*}`, reporting anything unmapped."""
    modes, unknown = {}, []
    for family, rig_type in (entry.get("families") or {}).items():
        mode = TYPE_MODES.get(rig_type)
        if mode is None:
            unknown.append("%s=%s" % (family, rig_type))
            mode = MODE_STUB
        modes[family] = mode
    return modes, unknown


def preset_items(self, context):
    """The `Preset` dropdown: the default, the detected character's own, then everything else.

    A callback because the list comes from the rules file, which the user can point elsewhere with `Rules File`.
    Blender requires an *index* default when `items` is a callback, so `default=0` - `preset_items` always
    returns `PRESET_DEFAULT` first, which is the rules table's own answers.

    `Preset_Filter` narrows the list (Blender has no search on an enum): the filter matches anywhere in the key,
    the source name or the signature, so `maru`, `1004` and `Hair4:L,R` all find Maruzensky. The current
    selection is always kept in the list, so narrowing cannot silently change what the run will use.
    """
    items = [(PRESET_DEFAULT, "Default (rules table)", "The rules table's own typing for every family - what "
                                                       "an unrigged character gets"),
             (PRESET_CUSTOM, "Custom", "Whatever the fields below currently say")]
    try:
        rules_file = getattr(self, "Rules_File", "") or ""
        presets = load_profiles(bpy.path.abspath(rules_file) if rules_file else None)
    except Exception as error:  # noqa: BLE001
        # A dropdown must not raise, but it must not be silent either: this is the "the preset list is empty and
        # nothing says why" case, and a line on the console costs nothing (code review, handoff 9ai).
        presets = {}
        print("[rigify_chains] the preset list could not be read (%s: %s); the dropdown shows Default only"
              % (type(error).__name__, error))
    identity = None
    # The active armature first, because that is the one `_find_model_armature` rigifies: with two characters in
    # one file, "selected objects" and "the active object" are not the same set, and the list *and* the run have
    # to be talking about the same character (code review, handoff 9ai).
    active = getattr(context, "active_object", None)
    armatures = ([active] if active is not None and active.type == 'ARMATURE' else [])
    armatures += [obj for obj in getattr(context, "selected_objects", []) if obj.type == 'ARMATURE']
    armatures += [obj for obj in getattr(context, "scene", context).objects if obj.type == 'ARMATURE']
    for armature in armatures:
        identity, _bone = model_identity(armature)
        if identity:
            break
    needle = (getattr(self, "Preset_Filter", "") or "").strip().lower()
    character = identity.split("_")[0] if identity else None
    mine = sorted(key for key in presets if character and key.startswith(character + "_"))
    others = sorted(key for key in presets if key not in mine)
    current = getattr(self, "Preset_Key", None)
    shown = 0
    for key in mine + others:
        entry = presets[key]
        haystack = " ".join([key, preset_label(key, entry), str(entry.get("source") or ""),
                             str(entry.get("signature") or ""), str(entry.get("character") or ""),
                             str(entry.get("costume") or "")]).lower()
        if needle and needle not in haystack and key != current:
            continue
        # the character and costume key first (that is what identifies it), then where the data came from
        label = "%s  %s" % (key, preset_label(key, entry))
        detail = "%d families" % len(entry.get("families") or {})
        if entry.get("signature"):
            detail += "  (%s)" % entry["signature"]
        items.append((key, label, detail))
        shown += 1
    if needle and not shown:
        items.append((PRESET_DEFAULT, "no preset matches '%s'" % needle, "Clear the search box to see them all"))
    return items


class ChainFamilyType(bpy.types.PropertyGroup):
    """One physics family's rig type, filled in from the model and the preset.

    Advanced mode draws one row per one of these. It is a `CollectionProperty` rather than a fixed set of
    properties because the families differ per model (`Hair6` exists on two of his 241 rigs, `Pareo0` on one),
    and the panel has to show the ones this model actually has.
    """

    family: StringProperty(name="Family")
    group: StringProperty(name="Group")
    mode: EnumProperty(name="Type", items=TYPE_CHOICES, default=MODE_OFF)
    default_mode: EnumProperty(name="Table", items=TYPE_CHOICES, default=MODE_OFF,
                               description="What the rules table alone says, so an untouched family keeps the "
                                           "table's own behaviour")
    bones: StringProperty(name="First bone", description="The family's first bone, which is what the rules "
                                                        "table is matched against")
    fixed: StringProperty(name="Fixed", description="The type this family always gets; not editable")


def model_identity(model):
    """The model's own `Chara_<id>_<costume>_Handle` bone, as `"<id>_<costume>"`, or None.

    The one asset-specific bone name in a UMA rig (docs/BONE_STRUCTURE.md), so nothing has to be inferred from
    geometry or from the file name: the model says which character and costume it is.
    """
    if model is None:
        return None, None
    for bone in model.data.bones:
        if bone.name.startswith("Chara_") and bone.name.endswith("_Handle"):
            key = bone.name[len("Chara_"):-len("_Handle")]
            if key:
                return key, bone.name
    return None, None


def profile_family_of(name):
    """`Sp_He_Hair4_L_00` -> `Sp_He_Hair4`; `Ear_01_L` -> `Sp_He_Hair4`-style family key."""
    fields = name.split("_")
    cut = len(fields)
    while cut > 1:
        field = fields[cut - 1]
        if (field.lower() in ("handle", "handle2") or field.isdigit()
                or field in ("L", "R", "C", "F", "B", "BL", "BR", "FL", "FR")):
            cut -= 1
        else:
            break
    return "_".join(fields[:cut])


def profile_signature(names):
    """The model's hair/accessory skeleton: `Hair4:C,L,R|Hair2:L,R|Ear:L,R`, comparable across models."""
    import re as _re
    parts = {}
    for name in names:
        if not any(token in name for token in ("Hair", "Ribbon", "Acc", "Acce", "Cat", "Beard", "Ear")):
            continue
        match = _re.match(r"^Sp_He_(Hair\d|Ribbon\d|Acc\d|Acce\d|Cat\d|Beard\d)", name)
        label = match.group(1) if match else ("Ear" if name.startswith("Ear_") else profile_family_of(name))
        sides = {token for token in name.split("_")
                 if token in ("L", "R", "C", "F", "B", "BL", "BR", "FL", "FR")}
        parts.setdefault(label, set()).update(sides or {"C"})
    return "|".join("%s:%s" % (label, ",".join(sorted(sides))) for label, sides in sorted(parts.items()))


def family_group(family, bones, base_rules):
    """Which group a physics family belongs to: `Hair`, `Skirt`, `Chest`, `Extras` or `Other`.

    The rules table answers first, where it has a rule for the family, because that is the collection a rig
    generated before any of this existed would use - so a family does not change group under a user's feet.
    Measured on Special Week (1001) in `chains-22`: a profile-driven `Ear` family that skipped this step was
    filed under `Hair` while the base `^Ear_0` rule says `Ears`, so that rig had no `Ears` collection at all and
    its ear controls sat inside `Hair`. The rule that wins is the first one whose pattern matches the family's
    first bone, the same first-match-wins order `_collect_chains` uses. Families the table says nothing about -
    the ones only his own rigs name, like `Sp_He_Hair0` or `Sp_He_Acc0` - are placed by name.
    """
    if bones:
        bone = sorted(bones)[0]
        for rule in base_rules:
            if not rule.get("collection") or not rule.get("type"):
                continue
            try:
                if re.match(str(rule["pattern"]), bone):
                    return rule["collection"]
            except re.error:
                continue
    if family.startswith("Ear"):
        return "Ears"
    for prefix, group in GROUP_PREFIXES:
        if family.startswith(prefix):
            return group
    return GROUP_FALLBACK


def physics_families(model):
    """The model's own physics families -> their bones, as `{family: [bone name, ...]}`.

    A *family* is the unit the rules and the animator's rigs are written in (`Sp_He_Hair4`, `Ear`,
    `Sp_Hi_MSkirt0`); it is derived from the bone name by `profile_family_of`. Only the physics side is
    reported: the body's own bones (`Hip`, `Thigh_L`, `Eye_up_01_L`, ...) are MMR's business, and the
    `_Handle` bones are the model's control side, which every skip rule drops.
    """
    present = {}
    for bone in model.data.bones:
        name = bone.name
        if name.endswith("_Handle") or not name.startswith(("Sp_", "Tp_", "Ex_", "Ear")):
            continue
        present.setdefault(profile_family_of(name), []).append(name)
    return present


def resolve_rule_modes(model, rules, family_modes, table_modes, fixed, never, trace, unmatched=None):
    """The rule table this run will use, after the preset and the user's per-group / per-family choices.

    `family_modes` is what the fields say (`{family: MODE_*}`) and `table_modes` what the rules table alone would
    say; a family counts as *chosen* only where the two differ, which is what keeps a family the table splits
    behaving as it does today. `fixed` is `{family: rigify type}` for the families his rigs never vary, which no
    mode may change. `unmatched` is what a family the table says nothing about gets instead of nothing - a
    `{"type": ...}` dict from `options.unmatched_families`, applied per family, so the families his own rigs name
    and the table never learned (`Sp_He_Hair0`, the sleeves, the accessory ribbons) come out with a control
    rather than with a silent skip. Returns `(rules, report)`, where `report` is
    `[(group, family, type or None, source, pattern)]` for the trace and for the check at the end.

    The unit of resolution is **one table rule's worth of bones**, not one family. A family is the animator's
    own naming unit (`Sp_Ch_Ribbon0`), and the table splits it: that family holds a one-bone front stub and two
    two-bone side chains, under `^Sp_Ch_Ribbon0_F` and `^Sp_Ch_`. Resolving per family alone gave the whole
    family the *first* matching rule's type - measured on Maruzensky, `Sp_Ch_Ribbon0` became a stub and the rig
    went from 32 chains to 36, which the check at the end of this function named. Grouping by the rule that
    actually decides each bone keeps every family the behaviour it has today.
    """
    present = physics_families(model)
    typed_rules = [rule for rule in rules if rule.get("type")]
    order = {}
    for index, rule in enumerate(typed_rules):
        order.setdefault(str(rule.get("pattern")), index)

    def decides(bone):
        """The first table rule that matches this bone, first-match-wins like `_collect_chains`."""
        for rule in typed_rules:
            try:
                if re.match(str(rule["pattern"]), bone):
                    return rule
            except (re.error, KeyError):
                continue
        return None

    # group every physics bone under the rule that decides it; bones no rule decides stay grouped by family,
    # which is how the families only his own rig names (`Sp_He_Hair0`, `Sp_He_Acc0`) get their rule
    units = {}
    for family, bones in sorted(present.items()):
        if not family:
            continue
        for bone in sorted(bones):
            base = decides(bone)
            key = str(base["pattern"]) if base is not None else "^" + family
            unit = units.setdefault(key, {"pattern": key, "base": base, "bones": [], "families": set()})
            unit["bones"].append(bone)
            unit["families"].add(family)

    def sort_key(unit):
        if unit["base"] is not None:      # the table's own rules first, in the table's own order
            return (0, order.get(unit["pattern"], 0), unit["pattern"])
        # Families no rule names are ordered longest-name-first. Two of them can be in a prefix relationship -
        # `1022_00` has `Sp_Wa_MSkirt1` and `Sp_Wa_MSkirt1_FLL` - and first-match-wins would let the shorter
        # pattern claim the longer family's bones. Nothing noticed while these families were skipped; the
        # `unmatched_families` fallback types them (chains-31), and this function's own check reported it:
        # "2 shadowed: Sp_Wa_MSkirt1_FLL_00 is claimed by ^Sp_Wa_MSkirt1".
        return (1, -len(unit["pattern"]), unit["pattern"])

    # `never`: families no mode may rig, whatever the user picks. His pipeline deletes `Sp_He_Ear0` and no vertex
    # is weighted to it (handoff 9m), so a preset that chains every soft part must not resurrect it. This is data
    # (`options.never_rig`), not a special case in the code.
    resolved = []
    report = []
    never_regexes = []
    for pattern in never or ():
        try:
            never_regexes.append(re.compile(str(pattern)))
        except re.error as error:
            trace.check("never_rig pattern compiles: %s" % pattern, False, str(error))
    for pattern in never or ():
        resolved.append({"pattern": pattern, "type": "", "skip": True, "never_rig": True})

    for unit in sorted(units.values(), key=sort_key):
        bones, base_rule = unit["bones"], unit["base"]
        families_here = sorted(unit["families"])
        group = family_group(families_here[0], bones, rules)
        if base_rule is None and any(regex.match(bones[0]) for regex in never_regexes):
            for family in families_here:
                report.append((group, family, None,
                               "never rigged: his pipeline deletes it and no vertex is weighted to it",
                               unit["pattern"]))
            continue
        # a fixed family: his rigs never vary it (241 of 241), so no mode can change it - the ears are always a
        # basic_tail chain and the tail always a spline
        fixed_here = [family for family in families_here if family in fixed]
        if fixed_here:
            rig_type, source = fixed[fixed_here[0]], "fixed"
            mode = None
        else:
            # A unit holds the families one table rule decides. The family's field counts as a *choice* only when
            # it differs from what the table alone would say - so a family the table splits keeps both of its
            # rules until someone asks for one answer for the whole family (measured: resolving per family
            # unconditionally turned Maruzensky's two chest side ribbons into stubs, 32 chains -> 36).
            mode = None
            for family in families_here:
                chosen = family_modes.get(family)
                if chosen is not None and chosen != table_modes.get(family):
                    mode = chosen
                    break
            if mode is None:
                if base_rule is not None:
                    rig_type, source = base_rule.get("type"), "the rules table"
                elif (unmatched or {}).get("type"):
                    # a family no table rule names, and the run has a fallback for those: his own rigs type them,
                    # and leaving them to MMR meant no control and no message at all (see the option's comment)
                    rig_type, source = unmatched["type"], "the fallback for families no rule names"
                else:
                    rig_type, source = None, "nothing names it"
            else:
                rig_type = MODE_TYPES.get(mode)
                # the trace names the rigify type, not this build's internal mode letter: it is read by people who
                # know `basic.super_copy` and not `STUB`
                source = "set to %s" % (rig_type or "none")
            # A type the table names that this build does not know (`limbs.spline_tentacles` in a hand-edited
            # file) used to be applied verbatim to `rigify_type` while the panel's row showed it as a stub
            # (`family_defaults` falls back to MODE_STUB silently), so the rig disagreed with the panel and the
            # failure surfaced later, from rigify. `preset_modes` reports the same case; this makes the table
            # path report it too (code review, handoff 9ai).
            if base_rule is not None and str(base_rule.get("type")) not in TYPE_MODES:
                trace.check("every rule's rig type is one this build knows", False,
                            "rule %s names '%s', which is not a rigify type this build recognises; rigify will "
                            "be handed it as it is" % (unit["pattern"], base_rule.get("type")))
        if rig_type is None:
            for family in families_here:
                report.append((group, family, None, source, unit["pattern"]))
            if base_rule is not None and mode is not None and MODE_TYPES.get(mode) is None:
                # an explicit Off has to suppress the table's own rule too, or the bones it names would still be
                # claimed further down the table
                resolved.append({"pattern": unit["pattern"], "type": "", "skip": True,
                                 "collection": group, "setup_off": True})
            continue
        rule = {
            "pattern": unit["pattern"],
            "type": rig_type,
            "collection": group,
            "min_bones": 1 if rig_type in PER_BONE_RIG_TYPES else 2,
            "setup_families": families_here,
            "setup_decides": bones[0],
        }
        # the rule keeps the attachment, parameters and deform mode of the table's rule for these bones, so
        # switching a type by hand cannot silently move where the chain hangs or how it bends
        if base_rule is not None:
            for key in ("parent", "parameters", "chain_deform", "resample", "connect_chain"):
                if key in base_rule:
                    # `parameters` is the one value here that is a *dict*, and `load_rules` caches the table for
                    # the session: copying it by reference meant line 1241's `setdefault("make_widget", ...)`
                    # edited the cached table (and `DEFAULT_RULES` itself) for the rest of the session. Benign
                    # today - only `basic.super_copy` reads that key - but it is the same class of bug as the
                    # options dict the code review caught in chains-35 (handoff 9aq).
                    rule[key] = dict(base_rule[key]) if isinstance(base_rule[key], dict) else base_rule[key]
        elif not mode and (unmatched or {}).get("type"):
            # a family the fallback typed keeps the rest of the fallback's settings (a stricter `min_bones`,
            # parameters of its own), because there is no table rule to inherit from
            for key in ("parent", "parameters", "chain_deform", "resample", "connect_chain", "min_bones"):
                if key in unmatched:
                    rule[key] = dict(unmatched[key]) if isinstance(unmatched[key], dict) else unmatched[key]
        if any(family.startswith(("Sp_He_", "Tp_He_", "Ear")) for family in families_here):
            rule.setdefault("parent", "Head")
        # Everything else the table does not name hangs off the nearest metarig bone to its model parent, the
        # same rule the ears needed (`_metarig_parent_for`, handoff 9o). This used to default to `Hip`, which is
        # right for a hip ribbon and wrong for a sleeve: measured on the fallback path (chains-31), a sleeve
        # attached to the nearest arm bone follows the arm, and one attached to `Hip` ignores it.
        if rig_type in PER_BONE_RIG_TYPES:
            rule.setdefault("parameters", {}).setdefault("make_widget", False)
        resolved.append(rule)
        for family in families_here:
            report.append((group, family, rig_type, source, unit["pattern"]))

    # the table's own rules have to keep working for everything the units above did not claim: the body's bones
    # and the `_Handle`s. The resolved rules sit *ahead* of the table - but after the leading skips, or a
    # `^Sp_He_Hair4` chain would swallow `Sp_He_Hair4_L_04_Handle` (that exact mistake put a handle bone in the
    # ear chain, caught by the metarig drift gate as `worst 0.01000 m at Ear_03_L_Handle`). Only the *leading*
    # skips move up: the table's trailing `^Sp_` catch-all stays last, or it would swallow nothing at all.
    leading_skips = []
    cut = 0
    for rule in rules:
        if rule.get("skip") and not rule.get("type"):
            leading_skips.append(rule)
            cut += 1
        else:
            break
    final = leading_skips + resolved + rules[cut:]

    # verification: every bone this run typed has to be decided by the rule this resolver produced for it, not
    # by something earlier in the table - the whole point of a dropdown is that the rig does what it says
    shadowed = []
    for rule in resolved:
        if not rule.get("type") or not rule.get("setup_decides"):
            continue
        bone = rule["setup_decides"]
        for candidate in final:
            try:
                if not re.match(str(candidate["pattern"]), bone):
                    continue
            except (re.error, KeyError):
                continue
            # A skip counts as shadowing. It used to be skipped over, which made this check blind to the one
            # thing it exists to catch - `_governing_rules` walks skip rules too, first-match-wins, so a skip
            # that matches a chain bone leaves it untyped while this check (and the per-family report) called it
            # typed. Unreachable with the shipped table (the leading `_Handle$` cannot match a chain bone), which
            # is exactly why it went unnoticed (code review, handoff 9ai).
            if candidate.get("skip"):
                shadowed.append("%s is skipped by %s" % (bone, candidate.get("pattern")))
                break
            if candidate is not rule:
                shadowed.append("%s is claimed by %s" % (bone, candidate.get("pattern")))
            break
    trace.check("every family the setup typed is the rule that decides it", not shadowed,
                "%d shadowed%s" % (len(shadowed), (": " + "; ".join(shadowed[:3])) if shadowed else ""))
    return final, report


def show_collection(collection, visible=True):
    """Hide or show one of the collections this operator creates, unless it is a sub-layer.

    `visible=False` is for the `<Family> (FK)` / `<Family> (Tweak)` siblings: measured on a run with all of them
    shown, 282 of the 1259 generated bones are drawn and 99 of those are sub-layers nobody has asked for
    (`Skirt (Tweak)` alone is 30, `Chest (Tweak)` 24). Rigify treats its own tweak layers the same way - its
    `Arm.L (Tweak)` and `Leg.L (Tweak)` are hidden - so hiding ours matches the rig it sits in. See
    `Chain_Layers`.

    **The `is_selectable` half of this does nothing, and never did.** A dead-code audit measured
    `BoneCollection` on 4.2, 4.5 and 5.2 and there is no such property on any of them, so the `hasattr` guard
    skips it every time - the earlier comment here claimed the hidden layers "stay selectable so a click in the
    N-panel brings it back", which was never true. A hidden collection is brought back from the N-panel the way
    Blender does it for any bone collection, and the only thing this function actually does is `is_visible`.
    """
    for attribute, value in (("is_visible", visible), ("is_selectable", True)):
        if hasattr(collection, attribute):
            try:
                setattr(collection, attribute, value)
            except (AttributeError, TypeError):
                pass


def assign_chain_ui_rows(armature, families, trace=None):
    """Give each chain family a rigify UI row of its own, and return {family: (row, collections)}.

    Rigify draws **one button per collection in a row, all the same width** (rigify/ui.py `draw`: every
    collection sharing a `rigify_ui_row` goes into one `grid_flow(columns=len(row_items), even_columns=True)`),
    and it lays its own families out one row each - `Face` row 1, `Torso` row 4, `Fingers` row 7,
    `Arm.L (IK)` + `Arm.R (IK)` row 10, ... (`rigify/metarigs/human.py`). Six chain families with their
    `(FK)` and `(Tweak)` siblings therefore want six rows of three buttons. The first version of this put
    all eighteen collections in one row instead, and rigify's own `Fingers` was in that same row - measured
    on the code tagged `chains-21`: 19 buttons, each about a twentieth of the panel wide, which is what
    "the text is impossible to see" was.

    The rows are picked from the *generated* rig, after rigify has created its own collections, because
    before that there is nothing to measure: the metarig carries no rigify collections yet (the operator
    inserts its chains before calling `generate_rig`), so any row number chosen then is a guess that can
    land on top of rigify's - which is exactly what row 7 was. Rows in use by collections this operator
    does not manage are collected first and the families go after the highest of them, so a rigify row can
    never be stolen, and a re-run lands on the same numbers (its own collections are excluded from the
    measurement).
    """
    ours = set()
    for family in families:
        ours.update((family, family + " (FK)", family + " (Tweak)"))
    in_use = {getattr(collection, "rigify_ui_row", 0) for collection in armature.data.collections_all
              if getattr(collection, "rigify_ui_row", 0) > 0 and collection.name not in ours}
    row = (max(in_use) if in_use else 0) + 1
    rows = {}
    for family in families:
        taken = []
        for suffix, title in (("", ""), (" (FK)", "(FK)"), (" (Tweak)", "(Tweak)")):
            collection = armature.data.collections_all.get(family + suffix)
            if collection is None:
                continue
            # `(FK)` / `(Tweak)` as the button text, not `Hair (FK)`: that is what rigify's own rows show
            # (`human.py` passes `ui_title='(FK)'`), so three buttons stay legible in one row.
            try:
                collection.rigify_ui_row = row
                collection.rigify_ui_title = title
            except (AttributeError, TypeError) as error:
                if trace is not None:
                    trace.check("UI row on %s" % collection.name, False, str(error))
                continue
            taken.append(collection.name)
        if taken:
            rows[family] = (row, taken)
            row += 1
    return rows


def set_collection_refs(pose_bone, collection_name, metarig, trace):
    """Point rigify's own FK / tweak collection parameters at the sibling collections.

    Rigify decides where a chain's FK and tweak controls go from the rig parameters
    `rigify_parameters.fk_coll_refs` / `tweak_coll_refs`, resolved against collections on the metarig
    (`rigify/utils/layers.py`). Creating the collections on their own does nothing: the parameter list stays
    empty, `ControlLayersOption.get()` returns nothing, rigify prints "empty tweak layer list on bone ..."
    and the generated FK / tweak controls end up in a collection that is hidden. That is the residue of the
    "controls I cannot select" report, so the refs are set here explicitly.
    """
    if not collection_name:
        return
    params = getattr(pose_bone, "rigify_parameters", None)
    if params is None:
        return
    for option, suffix in (("fk_coll_refs", " (FK)"), ("tweak_coll_refs", " (Tweak)")):
        refs = getattr(params, option, None)
        collection = metarig.data.collections.get(collection_name + suffix)
        if refs is None or collection is None:
            continue
        try:
            while len(refs) > 0:
                refs.remove(refs[0])
            refs.add().set_collection(collection)
        except (AttributeError, TypeError, RuntimeError) as error:
            trace.check("%s on %s" % (option, pose_bone.name), False, str(error))


class Trace:
    """Collects the debug log. Printed live, and written to a text block for sending back."""

    def __init__(self, debug):
        self.debug = debug
        self.lines = []
        self.failures = []

    def step(self, message):
        self.lines.append("== " + message)
        if self.debug:
            print("[rigify_chains] " + message)

    def info(self, message):
        self.lines.append("   " + message)
        if self.debug:
            print("[rigify_chains] " + message)

    def check(self, label, condition, detail=""):
        """A verification: recorded either way, and failures are surfaced in the operator report."""
        mark = "OK  " if condition else "FAIL"
        self.lines.append("   [%s] %s%s" % (mark, label, (" - " + detail) if detail else ""))
        if self.debug:
            print("[rigify_chains] [%s] %s %s" % (mark, label, detail))
        if not condition:
            self.failures.append(label + ((" - " + detail) if detail else ""))
        return condition

    def save(self):
        text = bpy.data.texts.get(TRACE_TEXT_NAME) or bpy.data.texts.new(TRACE_TEXT_NAME)
        text.clear()
        text.write("rigify chains trace, %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        text.write("build: %s\n" % BUILD)
        text.write("module: %s\n" % __file__)
        try:
            text.write("module modified: %s\n" % time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(__file__))))
        except OSError:
            pass
        text.write("\n".join(self.lines))
        return text.name


def preset_table(self, context):
    """The rules file's table and presets, as one call: `(rules, options, source, presets)`.

    `self` is anything with a `Rules_File` - the run's operator, or the save-preset one.
    """
    path = bpy.path.abspath(self.Rules_File) if getattr(self, "Rules_File", "") else None
    rules, options, source = load_rules(path)
    presets = load_profiles(path)
    return rules, options, source, presets


def family_defaults(rules, presets, preset_key, families, identity=None, cross_costume="hair", unmatched=None):
    """What each family should start as: the preset's answer, else the rules table's, else Off.

    Returns `(modes, table_modes, source_label)`.

    * `modes`      - what the field should say (the preset's value where it names the family);
    * `table_modes` - what the rules table alone would say, kept so the resolver can tell "the user chose this"
      from "this is just the default". That distinction is what lets a family the table *splits*
      (`Sp_Ch_Ribbon0` is a one-bone stub in front and two side chains) keep both rules until someone actually
      asks for one answer for the whole family.

    A preset cannot name a family that did not exist when it was made, so anything it does not name falls back
    to the table - stated in the trace rather than guessed at silently.

    `unmatched` is `options.unmatched_families`, and it belongs here for one reason: without it the *panel* lied.
    A family no typed rule names was reported as `none` while the run typed it `basic.super_copy` through that
    fallback - 778 families / 3,521 bones across the 125-model fleet, which is the feature's own promise ("the
    fields decide") being false for a third of what it rigs, and the trace counting them as "left at the rules
    table's own answer". Measured by the beta code review (handoff 9aq) and cheap to get right: the fallback *is*
    the table's answer, so it seeds `table_modes` and the row reads it as `default` rather than as an override.

    `identity` is the model's own `Chara_<id>_<costume>` key, and `cross_costume` says what a preset *keyed for a
    different costume* may decide (`options.preset_cross_costume`):

    * `"hair"` (the default) - only its **hair** families. The autodetect that chooses such a preset compares the
      model's hair *skeleton* (`detect_preset`), so when it fires the head bone set genuinely agrees, and hair
      typing is a statement about which bones are there. Physics and accessory families differ per costume
      (`1001_90` has no `Sp_Hi_Ribbon0` at all, `docs/RIG_HANDOFF.md` §5) and a skeleton match says nothing about
      them, so those stay with the rules table - which now has the `unmatched_families` fallback behind it.
    * `"all"` - every family the preset names, the behaviour before this option existed.
    * `"exact"` - nothing: a preset only applies to the exact character *and* costume it was saved for.
    """
    table_modes = {}
    for item in families:
        for rule in rules:
            if not rule.get("type"):
                continue
            try:
                if re.match(str(rule["pattern"]), item.bones):
                    table_modes[item.family] = TYPE_MODES.get(rule["type"], MODE_STUB)
                    break
            except re.error:
                continue
    fallback_mode = TYPE_MODES.get((unmatched or {}).get("type"))
    modes = {}
    for item in families:
        mode = table_modes.get(item.family, MODE_OFF)
        if item.family not in table_modes and fallback_mode:
            # the fallback is the rules table's answer for this family (see the docstring): the row has to show
            # what the run will do, or the panel and the trace contradict the rig
            mode = fallback_mode
            table_modes[item.family] = fallback_mode
        modes[item.family] = mode
    if preset_key in (None, PRESET_DEFAULT, PRESET_CUSTOM):
        return modes, table_modes, "the rules table"
    entry = presets.get(preset_key)
    if entry is None:
        return modes, table_modes, "the rules table (preset %s not found)" % preset_key
    named, unknown = preset_modes(entry)
    elsewhere = bool(identity) and preset_key != identity
    held_back = []
    if elsewhere and cross_costume in ("hair", "exact"):
        kept = {}
        for item in families:
            if item.family not in named:
                continue
            if cross_costume == "hair" and item.group == "Hair":
                kept[item.family] = named[item.family]
            else:
                held_back.append(item.family)
        named = kept
    changed = 0
    for family, mode in named.items():
        if family in modes:
            changed += 1 if modes[family] != mode else 0
            modes[family] = mode
    label = "preset %s (%s, %d families, %d of them different from the table)" % (
        preset_key, entry.get("source") or "no source", len(named), changed)
    if held_back:
        # Two different things to say, and saying the wrong one is worse than saying nothing: `exact` uses none of
        # a cross-costume preset, `hair` uses part of it. The label is what the panel shows and what the trace
        # keeps, so it has to describe what happened rather than share one sentence between the two.
        if cross_costume == "exact":
            label += ("; another costume's preset and preset_cross_costume is 'exact', so none of it was used "
                      "and all %d family answer(s) came from the rules table" % len(held_back))
        else:
            label += ("; another costume's preset, so only its hair answers were used and %d other family "
                      "answer(s) stayed with the rules table" % len(held_back))
    if unknown:
        label += "; unmapped: " + ", ".join(unknown[:3])
    return modes, table_modes, label
def detect_preset(presets, model, trace=None):
    """Which preset this model is, as `(key, why)`. Autodetection only picks a value for the preset field.

    Two steps, in order of how strong the evidence is: the model's own `Chara_<id>_<costume>_Handle` bone names
    the character and costume outright, and failing that the hair/accessory skeleton is compared - a signature
    match is a strong hint, but only taken when exactly one preset matches it, because a wrong preset is a wrong
    rig rather than a missing one.
    """
    identity, _bone = model_identity(model)
    if identity and identity in presets:
        return identity, "%s is in the presets (%s)" % (identity, presets[identity].get("source") or "no source")
    signature = profile_signature([bone.name for bone in model.data.bones])
    matches = [key for key, entry in presets.items() if entry.get("signature") == signature]
    if len(matches) == 1:
        return matches[0], ("no entry for %s; matched %s by its hair skeleton (%s)"
                            % (identity or "this model", matches[0], signature))
    if not presets:
        return None, "the rules file has no presets"
    return None, ("no entry for %s and %d skeleton matches, so the Default preset is used"
                  % (identity or "this model", len(matches)))


def apply_preset_update(self, context):
    """`Preset` changed: fill the family fields from it. Kept short - the work is in `sync_preset`."""
    sync_preset(self, context)


def find_model_armature(context, trace=None):
    """The MMD armature: a mesh is skinned to it and it is not a generated control rig.

    A **module-level** function rather than only a method, and that is the fix for the preset bug the user
    reported (*"I've never managed to get the presets working"*): an RNA `update` callback is handed the operator's
    *properties wrapper* (`bpy.types.UMA_OT_rigify_chains`, MRO `OperatorProperties`), not the live Python
    operator, so it has no methods at all. Measured in `preset_callback_identity_check.py` - inside the callback,
    `hasattr(self, "_find_model_armature")` is False while the same name is on the class. `sync_preset` is that
    callback's whole body, and it used to ask `self` for the model; the exception came out of an RNA update during
    a dialog and took Blender down with it (`EXCEPTION_ACCESS_VIOLATION`) after printing
    `AttributeError: 'UMA_OT_rigify_chains' object has no attribute '_find_model_armature'`.

    Everything reachable from an `update` callback therefore has to work with RNA properties and module functions
    only. `context` and the operator's `Families`/`Preset`/`Populating` fields are all fine there.
    """
    trace = trace or Trace(False)
    bound = {}
    for obj in context.scene.objects:
        if obj.type != 'MESH':
            continue
        for modifier in obj.modifiers:
            if modifier.type == 'ARMATURE' and modifier.object is not None:
                bound.setdefault(modifier.object.name, []).append(obj.name)
    candidates = [o for o in context.scene.objects
                  if o.type == 'ARMATURE'
                  and not any(bone.name.startswith(GENERATED_PREFIXES) for bone in o.data.bones)]
    # `view_layer.objects.active` rather than `context.active_object`: with two models in one file the two can
    # disagree (a script, or a context copy, can leave `active_object` empty), and then the run silently falls back
    # to "the first armature with a mesh" - which is the *other* model (handoff 9aj).
    active = getattr(context.view_layer.objects, "active", None) or context.active_object
    if active in candidates:
        return active, bound.get(active.name, [])
    with_mesh = [o for o in candidates if bound.get(o.name)]
    if with_mesh:
        return with_mesh[0], bound.get(with_mesh[0].name, [])
    if candidates:
        return candidates[0], []
    trace.check("model armature found", False, "no armature in the scene is skinned to a mesh")
    return None, []


def sync_preset(self, context):
    """Fill the family rows from `Preset`, if they have not already been filled from it.

    This is the whole of what a preset does - it sets the fields and stops. There is no second resolution step
    at run time, so the trace can always say "preset 1004_00, then Chest set to Off" and the answer cannot drift
    from what the panel shows.

    Two things make it a function the panel calls rather than only an `update` callback:

    * **`update` does not fire for every way the value can change.** Measured in `preset_ui_probe.py`: setting
      the property from Python leaves `_preset_chosen` unset, i.e. the callback never ran. A dropdown click in
      the GUI does fire it, but the panel must not depend on that - so `draw` compares the current preset with
      the one the rows were last filled from and calls this itself on the next redraw.
    * each field write fires that field's own callback, so without the `_populating` guard the first family
      written flipped the preset to `Custom` (picking a preset looked like it did nothing) and the group sync
      then **flattened** the per-family values the preset had just set. That was the reported bug: "I expected
      the fields in advanced mode to update after i select a different preset - can't observe that".
    """
    if self.Populating:
        return
    if not getattr(self, "Families", None) or not len(self.Families):
        return
    if self.Preset == PRESET_CUSTOM:
        self.Preset_Key = self.Preset
        return
    # `Preset_Key` is the preset the rows already came from. It is an RNA property and not a Python attribute on
    # purpose: a Python one does not survive a redraw (`AttributeError: ... has no attribute '_model'` was the
    # same fault), so this would re-apply the preset on every redraw and silently revert a hand-edited family.
    if self.Preset_Key == self.Preset:
        return
    self.Preset_Chosen = True        # a choice, so autodetection must not overrule it later in `execute`
    try:
        rules, options, _source, presets = preset_table(self, context)
    except Exception as error:  # noqa: BLE001 - a dialog must not raise
        # Silent here means "picking a preset did nothing, and nothing anywhere says why" - the symptom class of
        # the empty-panel bug (handoff 9ag). The dialog stays up; the reason goes to the console.
        print("[rigify_chains] picking preset '%s' failed (%s: %s); the rows keep what they had"
              % (self.Preset, type(error).__name__, error))
        return
    # A preset keyed for another costume may only decide hair by default (`options.preset_cross_costume`): the
    # autodetect behind such a preset compares hair skeletons, and physics differ per costume. The model's
    # identity is what says whether the preset is this model's or a relative's.
    #
    # The module-level function, not `self._find_model_armature`: this runs inside an RNA `update` callback, where
    # `self` is the properties wrapper and has no methods (see `find_model_armature`).
    model_for_identity, _meshes = find_model_armature(context, Trace(False))
    identity, _bone = model_identity(model_for_identity) if model_for_identity is not None else (None, None)
    modes, table_modes, label = family_defaults(rules, presets, self.Preset, self.Families, identity,
                                                str(options.get("preset_cross_costume", "hair")).lower(),
                                                options.get("unmatched_families") or {})
    self.Populating = True
    try:
        for item in self.Families:
            if item.fixed:
                continue
            item.mode = modes.get(item.family, MODE_OFF)
            item.default_mode = table_modes.get(item.family, MODE_OFF)
        sync_group_fields(self)
        # A preset that gives one group more than one answer cannot be shown as a group row at all: Simple mode
        # has a single dropdown per group, so leaving it selected would invite the user to flatten the character
        # with one click - which is exactly what Maruzensky's rig (Hair0 a stub, Hair2 and Hair4 chains) would
        # lose. Switch to the mode that can show it, and say so in the panel. The fallback type is excluded from
        # that test on purpose: it is not an answer anybody chose (`mixed_groups`).
        mixed = mixed_groups(self, TYPE_MODES.get((options.get("unmatched_families") or {}).get("type")))
        self.Forced_Advanced = ", ".join(mixed)
        if mixed:
            self.Mode = 'ADVANCED'
        self.Preset_Note = label if len(label) < 120 else label[:117] + "..."
        self.Preset_Key = self.Preset
    finally:
        self.Populating = False


def wrap_note(text, width=58):
    """Split a note into lines Blender can actually show.

    A `layout.label` truncates instead of wrapping, so a long note ends up cut in the middle - reported as
    "it the message trimmed in the middle? It's not clear what it means". Wrapping here is crude on purpose:
    it only has to keep every word visible.
    """
    lines, current = [], ""
    for word in str(text).split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return lines or [""]


def detect_step(model, trace=None):
    """`Full` or `Wire only`, decided from what is in the scene. Returns `(step, why)`.

    The user asked whether this can detect itself, and it can - the two states are unmistakable:

    * no generated rig (nothing with `ORG-`/`DEF-`/`MCH-` bones) -> there are no chains to wire to, so `Full`;
    * a generated rig carrying our chain roots, and the model's chain bones with no `UMA_chain_*` constraint on
      them -> this is the state a run that died before wiring leaves behind, which is exactly what `Wire only`
      exists for;
    * everything already wired -> `Wire only`, which for that state means "there is nothing to generate or
      attach; re-apply the display". It used to answer `Full` on the reasoning that "a Full run redoes it from
      scratch" - it does not: the chains were released by the run that wired them, so a second Full run finds 33
      chains where the first found 44 and fails 17 attachment checks (measured, handoff 9aq). `Full` on that state
      is now refused in `execute` rather than silently degrading the model.
    """
    rig = RigifyChains._find_rig_for_chains(model, [bone.name for bone in model.data.bones
                                                    if bone.name.startswith(("Sp_", "Tp_", "Ear"))
                                                    and not bone.name.endswith("_Handle")], trace)
    if rig is None:
        # No rig in this file belongs to *this* model. Another character's rig does not count, however many of
        # this model's chain-bone names it happens to carry: measured on the user's two-model file, the Doto rig
        # carried 13 of King Halo's 15 (handoff 9aj), and a `Wire only` run on that basis wired King Halo to
        # Doto's rig. So this is the state that needs a Full run, not a wiring pass.
        elsewhere = RigifyChains._generated_rig_candidates(model)
        if elsewhere:
            return 'FULL', ("%d generated rig(s) in this file (%s) and none of them is %s's - chain bones share "
                            "their names across characters, so another model's rig is not evidence that this one "
                            "has been rigged" % (len(elsewhere),
                                                 ", ".join(obj.name for obj in elsewhere[:3]), model.name))
        return 'FULL', "no generated rig in the scene, so there is nothing to wire to yet"
    chain_bones = [bone.name for bone in model.data.bones
                   if bone.name.startswith(("Sp_", "Tp_", "Ear")) and not bone.name.endswith("_Handle")]
    generated = [name for name in chain_bones if ("ORG-" + name) in rig.pose.bones]
    if not generated:
        return 'FULL', ("a rig exists (%s) but it carries none of this model's chains, so they have to be "
                        "generated" % rig.name)
    wired = [bone.name for bone in model.pose.bones
             if any(c.name.startswith("UMA_chain") for c in bone.constraints)]
    if wired:
        # A wired model is *not* a Full run's business. `Full` regenerates from the model, and this model's chain
        # bones were released by the run that wired them, so chain discovery would find 33 chains where the first
        # run found 44 and fail 17 attachment checks - measured with `double_run_check.py` (handoff 9aq). The safe
        # answer is `Wire only`, which now means "re-attach nothing, re-apply the display".
        return 'WIRE', ("%d chain(s) already follow %s, so nothing needs generating: `Wire only` re-applies the "
                        "wiring and the display, and a Full run would rebuild only part of the rig (measured: 33 "
                        "chains instead of 44, 17 failed checks)" % (len(wired), rig.name))
    return 'WIRE', ("%s already carries %d of this model's chains and nothing is wired to them yet, so this is "
                    "the state a run that stopped before wiring leaves behind" % (rig.name, len(generated)))


def mixed_groups(operator, fallback_mode=None):
    """Groups that hold **more than one real rig type** - a single dropdown cannot show those.

    What does not count as an answer: `Off`, and the `unmatched_families` fallback type. A family no rule names
    used to be `Off` because nothing named it; it is not Off, it gets `basic.super_copy` from the fallback, and the
    rows now say so (see `family_defaults`). For *this* test it is left out all the same, because a group whose
    only difference is "some of these families fall back to a stub" is still expressible as one answer - and
    counting it would open the dialog on Advanced for nearly every model, since most costumes have one accessory
    family the table never learned. Reporting `Extras (2 different answers across 2 families)` for `Ribbon0` a
    spline and `Ribbon1` a fallback stub was tried and was noise; it buried the group that really is mixed
    (`Hair`, where `Hair2` is a tail and `Hair4` a spline).
    """
    mixed = []
    for group in SIMPLE_GROUPS:
        members = [item.mode for item in operator.Families
                   if item.group == group and not item.fixed and item.mode != MODE_OFF
                   and item.mode != fallback_mode]
        if len(set(members)) > 1:
            mixed.append(group)
    return mixed


class RigifyChainPresetSave(bpy.types.Operator):
    """Save the current rig type choices as a preset file"""

    bl_idname = "uma.rigify_chain_preset"
    bl_label = "Save as preset"
    bl_description = ("Write what the family fields currently say into one small JSON file in the presets "
                      "folder, so it can be picked again - and so the next run on this character autodetects it. "
                      "A preset is a per-character preference, so it gets its own file rather than a key inside "
                      "the rules file")
    bl_options = {'REGISTER'}

    Name: StringProperty(
        name="Preset name",
        description="Defaults to the model's own <character>_<costume> id, which is what makes it autodetect. "
                    "An existing preset is never overwritten unless Overwrite is ticked - the derived ones "
                    "cannot be regenerated from inside Blender",
        default="",
    )
    Data: StringProperty(name="Choices", default="", options={'HIDDEN'})
    Rules_File: StringProperty(name="Rules File", default="", options={'HIDDEN'})
    Overwrite: BoolProperty(
        name="Overwrite an existing preset",
        description="Replace a preset that is already there. Off by default: the derived presets cannot be "
                    "regenerated from inside Blender, and the name field defaults to one of their keys",
        default=False,
    )

    def invoke(self, context, event):
        if not self.Name:
            model = next((obj for obj in context.scene.objects
                          if obj.type == 'ARMATURE'
                          and not RigifyChains._looks_like_control_rig(obj)), None)
            identity, _bone = model_identity(model) if model is not None else (None, None)
            self.Name = identity or "my-preset"
        return context.window_manager.invoke_props_dialog(self, width=380)

    def execute(self, context):
        families = {}
        for entry in (self.Data or "").split(";"):
            family, _, mode = entry.partition("=")
            if not family:
                continue
            rig_type = MODE_TYPES.get(mode)
            if rig_type:
                families[family] = rig_type
        if not families:
            self.report({'ERROR'}, "Nothing to save: every family is Off.")
            return {'CANCELLED'}
        existing = preset_files().get(self.Name)
        # ...and the *file* that name would be written to, which is not the same question: `preset_filename`
        # sanitises a key, so `1004 00`, `1004-00` and `1004_00` all land on `1004_00.json`. The guard used to
        # be keyed on the key alone, so saving `1004 00` next to a `1004_00` destroyed the other preset and -
        # because the guard saw no conflict - wrote no `replaced` copy either (code review, handoff 9ai).
        target = os.path.join(preset_dir(), preset_filename(self.Name) + ".json")
        if os.path.exists(target) and not self.Overwrite:
            self.report({'ERROR'}, "The name '%s' is saved as %s, which already exists. Tick Overwrite, or save "
                                   "under another name." % (self.Name, os.path.basename(target)))
            return {'CANCELLED'}
        if existing is not None and not self.Overwrite:
            self.report({'ERROR'}, "A preset called '%s' already exists (%s). Tick Overwrite, or save under "
                                   "another name." % (self.Name, existing.get("source") or "no source"))
            return {'CANCELLED'}
        payload = {"source": "saved in Blender", "families": dict(sorted(families.items()))}
        if existing is not None:
            # keep what was there: an overwrite is recoverable without a version control system, and the file
            # it replaces is right next to it
            payload["replaced"] = {key: value for key, value in existing.items() if key != "file"}
        try:
            path = save_preset(self.Name, payload)
        except (OSError, ValueError) as error:
            self.report({'ERROR'}, "Could not write the preset: %s" % error)
            return {'CANCELLED'}
        self.report({'INFO'}, "Saved preset '%s' (%d families) as %s - %d presets in the folder"
                    % (self.Name, len(families), os.path.basename(path), len(preset_files())))
        return {'FINISHED'}


class RigifyChainPresetReload(bpy.types.Operator):
    """Read the preset folder again"""

    bl_idname = "uma.rigify_chain_preset_reload"
    bl_label = "Reload presets"
    bl_description = ("Read the preset folder again. The list is cached because it is read on every redraw and "
                      "99 files made the dropdown stutter; anything saved from Blender refreshes it by itself, "
                      "so this is for a file added or edited on disk")
    bl_options = {'REGISTER'}

    def execute(self, context):
        forget_presets()
        presets = load_profiles(force=True)
        self.report({'INFO'}, "%d preset(s) in %s" % (len(presets), preset_dir()))
        return {'FINISHED'}


class RigifyChainTraceSave(bpy.types.Operator):
    """Write the run's trace out as a text file, for sending back"""

    bl_idname = "uma.rigify_chain_trace"
    bl_label = "Save trace to a file"
    bl_description = ("Write uma_rigify_chains_trace.txt out of the .blend and next to the presets, so it can be "
                      "attached to a message instead of copied out of a text block")
    bl_options = {'REGISTER'}

    def execute(self, context):
        text = bpy.data.texts.get(TRACE_TEXT_NAME)
        if text is None:
            self.report({'ERROR'}, "No trace yet - run Pumpkin Rig Setup first (the trace is always recorded in "
                                   "a text block called %s)." % TRACE_TEXT_NAME)
            return {'CANCELLED'}
        directory = bpy.utils.user_resource("CONFIG", path="uma_addon", create=True)
        path = os.path.join(directory, "trace_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
        try:
            # Written with Python's own writer. `Text.filepath_raw` and `Text.save()` were the obvious pair and
            # are gone in 5.2 - measured: `AttributeError: 'Text' object has no attribute 'filepath_raw'`, the
            # error the user hit (handoff 9af) - and `Text.filepath` alone only *links* the datablock, it does
            # not write it. `create=True` above makes the folder, so no os.makedirs is needed.
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text.as_string())
        except OSError as error:
            self.report({'ERROR'}, "Could not write %s: %s" % (path, error))
            return {'CANCELLED'}
        self.report({'INFO'}, "Trace written to %s" % path)
        return {'FINISHED'}


def group_snapshot(operator):
    """Simple mode's last-seen group values, as `Hair=TAIL;Skirt=OFF`.

    A string in an RNA property rather than a Python dict on the operator: the operator instance is owned by
    Blender and does not reliably keep Python attributes set from inside a callback (measured:
    `AttributeError: 'UMA_OT_rigify_chains' object has no attribute '_preset_chosen'` from a property update),
    so every piece of state that has to survive a redraw is an RNA property.
    """
    return {entry.partition("=")[0]: entry.partition("=")[2]
            for entry in (operator.Group_Snapshot or "").split(";") if entry}


def set_group_snapshot(operator, values):
    operator.Group_Snapshot = ";".join("%s=%s" % (group, values[group]) for group in sorted(values))


def sync_group_fields(operator):
    """Simple mode's group rows follow the families, which are the only place a choice is actually stored.

    One source of truth: a group row the user moves writes through to every family in it
    (`adopt_group_edits`), and a family row the user moves changes what its group row shows (here). The two
    modes therefore always agree, and switching between them cannot lose or hide a choice. The snapshot is what
    makes the diff possible without callbacks.
    """
    values = {}
    for group in SIMPLE_GROUPS:
        members = [item.mode for item in operator.Families if item.group == group and not item.fixed]
        if not members:
            continue
        # a group whose families disagree has no single true value: show the most common one, and `draw` says
        # how many answers there really are
        value = members[0] if len(set(members)) == 1 else max(set(members), key=members.count)
        setattr(operator, "%s_Mode" % group, value)
        values[group] = value
    set_group_snapshot(operator, values)


def adopt_group_edits(operator):
    """Simple mode: if a group row moved since the last redraw, give every family in it that answer.

    Driven from `draw`, against a plain Python snapshot, instead of from an `update` callback. A callback here
    cannot work: this operator's properties are RNA owned by Blender, and the only hook that sees a change is
    `draw` - which is also the only place that is certainly running while the dialog is open. A group whose
    families disagree keeps whatever they say until the user actually moves that row.
    """
    snapshot = group_snapshot(operator)
    if not snapshot:
        return False
    changed = False
    values = dict(snapshot)
    for group in SIMPLE_GROUPS:
        value = getattr(operator, "%s_Mode" % group, MODE_OFF)
        if snapshot.get(group) == value:
            continue
        values[group] = value
        for item in operator.Families:
            if item.group == group and not item.fixed and item.mode != value:
                item.mode = value
                changed = True
    set_group_snapshot(operator, values)
    return changed


def preset_mismatch(operator):
    """How many family rows differ from what the selected preset says - the honest version of "Custom".

    Rather than flipping the preset label on every edit (which needs the callbacks that do not work), the panel
    says what has actually happened: the preset is still selected, and N rows have been changed by hand.
    """
    key = operator.Preset
    if key in (None, PRESET_DEFAULT, PRESET_CUSTOM):
        return 0
    try:
        rules, options, _source, presets = preset_table(operator, None)
    except Exception:  # noqa: BLE001
        return 0
    entry = presets.get(key)
    if entry is None:
        return 0
    wanted, _unknown = preset_modes(entry)
    return sum(1 for item in operator.Families
               if not item.fixed and item.family in wanted and item.mode != wanted[item.family])


def mode_property(group):
    """One `EnumProperty` per group, for Simple mode. `adopt_group_edits` writes it through to the family rows,
    so the two modes cannot drift apart."""
    return EnumProperty(
        name=group,
        description="What every family in %s gets. Advanced mode shows the same choice per family" % group,
        items=TYPE_CHOICES,
        default=MODE_OFF,
    )


class RigifyChains(bpy.types.Operator):
    """Rigify the model's hair, cloth and tail chains as well as the body"""

    bl_idname = "uma.rigify_chains"
    bl_label = "Pumpkin Rig Setup (%s)" % BUILD_TAG
    bl_description = ("MMR controller plus rigify chains for hair, cloth and tail. Adds the chains to the "
                      "metarig while rigify is generating, then makes the model follow them. Undoable. [%s]"
                      % BUILD_TAG)
    bl_options = {'REGISTER', 'UNDO'}

    Step: EnumProperty(
        name="Step",
        description="Full runs MMR and adds the chains; Wire only re-attaches the model to chains an earlier "
                    "run already generated (the recovery step for a run that stopped before wiring). The "
                    "detected value and the reason for it are shown above",
        items=[
            ('FULL', "Full", "Run MMR and add the chains"),
            ('WIRE', "Wire only", "Let the model follow the generated chains (after a previous run)"),
        ],
        default='FULL',
    )
    Mode: EnumProperty(
        name="Mode",
        description="Simple asks one question per group; Advanced shows every family in the model and lets each "
                    "one be answered on its own. A preset fills either in",
        items=[
            ('SIMPLE', "Simple", "One rig type per group: Hair, Skirt, Chest, Extras, Other"),
            ('ADVANCED', "Advanced", "One rig type per family: Hair0, Hair2, Hair4, MSkirt0, Jacket0..."),
        ],
        default='SIMPLE',
    )
    Preset: EnumProperty(
        name="Preset",
        description="Fills the rig type fields with a stored set of choices. Autodetected from the model's "
                    "Chara_<id>_<costume>_Handle bone; 'Default' is the rules table, and editing any field "
                    "leaves the preset",
        items=preset_items,
        # an index, not a value: Blender requires that when `items` is a callback. `preset_items` always returns
        # PRESET_DEFAULT first.
        default=0,
        update=apply_preset_update,
    )
    Preset_Filter: StringProperty(
        name="Find a preset",
        description="Narrow the preset list. Matches the key, the source metarig name and the hair skeleton "
                    "signature; Blender has no search box on an enum, so this is what feeds it",
        default="",
    )
    Preset_Key: StringProperty(
        name="Preset key",
        description="The preset the rows were last filled from. A plain string, because reading the `Preset` "
                    "enum from inside its own items callback re-enters the callback until the stack runs out",
        default="",
        options={'HIDDEN'},
    )
    # Everything the dialog has to remember between redraws is an RNA property: Blender owns the operator
    # instance, and a Python attribute set while a callback is running does not survive (measured:
    # `AttributeError: 'UMA_OT_rigify_chains' object has no attribute '_preset_chosen'`).
    Preset_Chosen: BoolProperty(name="Preset chosen", default=False, options={'HIDDEN'})
    Populating: BoolProperty(name="Filling the rows", default=False, options={'HIDDEN'})
    Preset_Note: StringProperty(name="Preset note", default="", options={'HIDDEN'})
    Forced_Advanced: StringProperty(name="Forced Advanced", default="", options={'HIDDEN'})
    Group_Snapshot: StringProperty(name="Last group values", default="", options={'HIDDEN'})
    # How the rig is *drawn* (chains-38). Display only, so it is not part of a preset: a preset says what a chain
    # is made of, a style says what it looks like, and the two are saved in different folders for that reason.
    # The list is the styles folder; the shipped `coolchou.json` is first and is what a fresh install runs.
    Style: EnumProperty(
        name="Style",
        description="How the generated rig is displayed: bone colours, widget sizes and the armature's display "
                    "flags. 'Coolchou's rigs' is the animator's own style, measured out of 14 of his packed rigs; "
                    "'None' leaves everything exactly as rigify made it. Display only - a style cannot change "
                    "what the rig does, and applying one twice changes nothing the second time",
        items=style_items,
        default=0,
    )
    # X-ray, whole rig or nothing. See the note in `execute`: Blender has no per-bone or per-family X-ray, so this
    # is the only form the choice can take - and it is here rather than only in Blender's own Viewport Display
    # panel because a flip made there lasts until the next run, which re-applies the style.
    Xray: EnumProperty(
        name="X-ray",
        description="Whether the model hides the rig. 'From style' takes the style file's answer (the shipped "
                    "one is off, like his rigs); 'On' draws the rig through the model, which is how a control "
                    "inside the clothes can still be clicked - most of them are: on a measured model, 34 of 49 "
                    "chest controls and 159 of 228 body controls sit inside the character. Blender has no "
                    "per-bone or per-family X-ray, so this is the whole rig",
        items=[
            ('STYLE', "From style", "Use the style file's own X-ray setting"),
            ('ON', "On - draw through the model", "The rig is drawn in front of the character; nothing is hidden "
                                                  "behind it"),
            ('OFF', "Off - the model hides the rig", "The controls behind the character are hidden, as in his "
                                                     "rigs"),
        ],
        default='STYLE',
    )
    # Simple mode's own fields, one per group. The per-family rows below are Advanced's, so the two do not
    # fight: switching to Simple overrides them while it is selected and leaves them intact (handoff 9y).
    Hair_Mode: mode_property("Hair")
    Skirt_Mode: mode_property("Skirt")
    Chest_Mode: mode_property("Chest")
    # The tail's own row (chains-31). `Sp_Hi_Tail0`'s group is `Tail`, which was not in `SIMPLE_GROUPS`, so the
    # field `chains-28` gave the tail was only reachable by switching to Advanced - measured in handoff 9ad,
    # where the tail turned out to be the family a user most often needs to answer for (`1001_Special Week.pmx`).
    Tail_Mode: mode_property("Tail")
    Extras_Mode: mode_property("Extras")
    Other_Mode: mode_property("Other")
    # Advanced mode's rows, filled from the model in `invoke`/`execute`: one per physics family it has, with the
    # group it belongs to and the type it currently gets.
    Families: CollectionProperty(type=ChainFamilyType)
    Choices: StringProperty(
        name="Choices",
        description="family=MODE;family=MODE ... applied after the preset, so a script can say what the "
                    "dropdowns would. The panel's own dropdowns write the same thing, so the two cannot disagree",
        default="",
        options={'HIDDEN'},
    )
    Chain_Layers: EnumProperty(
        name="Chain Layers",
        description="Which of the collections this operator creates to switch on. 'Nothing' hides all of them "
                    "including the model's own MMD collection; 'Controls only' shows each family's control "
                    "collection; 'Everything' shows the (FK) and (Tweak) sub-layers as well",
        items=[
            ('NONE', "Nothing", "Hide every collection this operator creates"),
            ('CONTROLS', "Controls only", "Show each family's controls; keep (FK) and (Tweak) off"),
            ('ALL', "Everything", "Show every layer of every family"),
        ],
        default='NONE',
    )
    Rules_File: StringProperty(
        name="Rules File",
        description="JSON file mapping bone name patterns to rigify types. Empty uses the default in the "
                    "Blender config folder",
        subtype='FILE_PATH',
        default="",
    )
    Overwrite_Typed: BoolProperty(
        name="Overwrite Existing Types",
        description="Also re-type bones MMR already rigified (spine, limbs, fingers, head). Off by default "
                    "so the body keeps behaving exactly as it does now",
        default=False,
    )
    Debug: BoolProperty(
        name="Debug Trace",
        description="Print every step and every check to the console as it runs",
        default=True,
    )
    More_Options: BoolProperty(
        name="More settings",
        description="Show the settings that decide what the rig is rather than what it contains: the step, the "
                    "chain joint lengths, whether MMR's own rig types are overwritten, the debug trace and the "
                    "rules file",
        default=False,
    )
    Resample: EnumProperty(
        name="Chain Bone Lengths",
        description="Where the joints of a chain sit. Keep copies the model's own spacing; Even puts one joint "
                    "every equal step along the same line; Leaf keeps the model's joints and extends the last "
                    "bone to the longest segment",
        items=[
            ('FILE', "From rules file", "Use the resample option in the rules file"),
            ('KEEP', "Keep model lengths", "Copy the model's own joint spacing"),
            ('EVEN', "Even out joints", "One joint every equal step along the chain"),
            ('LEAF', "Lengthen the leaf", "Keep the model's joints, extend the last bone to match"),
        ],
        default='FILE',
    )

    # --------------------------------------------------------------------------------------------------
    # discovery

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT', 'POSE'} and any(o.type == 'ARMATURE'
                                                          for o in context.scene.objects)

    @staticmethod
    def _looks_like_control_rig(obj):
        return any(bone.name.startswith(GENERATED_PREFIXES) for bone in obj.data.bones)

    def _find_model_armature(self, context, trace):
        """The MMD armature, found the way the module-level function does (see `find_model_armature`)."""
        return find_model_armature(context, trace)

    @staticmethod
    def _generated_rig_candidates(model):
        """Every rigify-looking armature in the file that is not this model, in `bpy.data.objects` order.

        Deliberately *not* "the generated rig": a .blend can hold several characters, and each keeps the rig an
        earlier run made. Which one belongs to this model is the question `_find_rig_for_chains` answers.
        """
        return [obj for obj in bpy.data.objects
                if obj.type == 'ARMATURE' and obj is not model
                and any(bone.name.startswith(GENERATED_PREFIXES) for bone in obj.data.bones)]

    @staticmethod
    def _rig_belongs_to(model, rig):
        """Is this rig *this model's* rig? Ownership, not shape.

        Chain bones carry the **same names on every character** (`Ear_01_L`, `Sp_He_Hair2_L_00`,
        `Sp_Hi_Tail0_B_00`), so counting how many of a model's chain roots a rig carries cannot tell two
        characters apart: measured on the user's two-model file (`meisho_doto_dot_o_lantern` rigged first, then
        `king_halo_swimsuit_clean_head`), the Doto rig carried **13 of King Halo's 15** chain roots, `WIRE`
        auto-detection concluded "this model already has a rig", and the run wired King Halo to Doto's rig -
        0.936 m joint offsets and six chain bones with no ORG copy (handoff 9aj). What *can* tell them apart is
        what a run wrote down when it generated the rig, or the name MMR gives that rig:

        * `rig["uma_rig_for"] == model.name` / `model["uma_rig"] == rig.name` - stamped by every run from
          `chains-36` on;
        * the rig name containing the model's name (`RIG-meisho_doto_dot_o_lantern_arm` for
          `meisho_doto_dot_o_lantern_arm`) - how a scene rigged by an older build is recognised.
        """
        if rig is None or model is None:
            return False
        try:
            if model.get("uma_rig") == rig.name or rig.get("uma_rig_for") == model.name:
                return True
        except (AttributeError, TypeError):  # not an ID property container: fall through to the name test
            pass
        stem = model.name[:-4] if model.name.endswith("_arm") else model.name
        return bool(stem) and stem in rig.name

    @staticmethod
    def _chain_root_hits(rig, roots):
        """How many of `roots` have an `ORG-` copy on this rig."""
        pose_bones = rig.pose.bones
        return sum(1 for root in roots if ("ORG-" + root) in pose_bones)

    @classmethod
    def _find_rig_for_chains(cls, model, roots, trace=None, preferred=None):
        """The rigify rig that is **this model's** and carries **these** chain roots.

        The old version returned the first armature that was not the model and had any `ORG-`/`DEF-`/`MCH-` bone,
        searched over `bpy.data.objects` - the whole file, not the scene. So a file that also held another
        character's rig wired this character's chains to it: measured on `meisho_doto_dot_o_lantern.pmx` in a file
        that also held Maruzensky's rig, the run reported `generated rig found RIG-maruzensky_フォーミュラオブル
        ージュ_arm`, then 21 of 44 chain roots had an ORG copy there, 38 chain bones had none, every joint
        measured 1.9 m off, and **none of the added controls did anything** (handoff 9af).

        `chains-33` answered that with "how many of this run's chain roots are on it" - decisive when the two
        characters' families differ (44 against 21 there) and useless when they do not, because chain bones share
        their names, so the other character's rig scores highly too. `chains-36` asks for **ownership** first
        (`_rig_belongs_to`) and uses the root count to rank the rigs this model actually owns. With no owned rig
        the answer is `None`: the caller reports it instead of wiring to somebody else's rig.

        `preferred` is the rig this run generated, which is exact by construction and skips the question.
        """
        roots = [root for root in roots or [] if root]
        chosen = bpy.data.objects.get(preferred) if preferred else None
        if chosen is not None and chosen.type == 'ARMATURE' and chosen is not model:
            if trace is not None and roots:
                trace.info("generated rig: %s, the one this run generated (%d of this model's %d chain roots on "
                           "it)" % (chosen.name, cls._chain_root_hits(chosen, roots), len(roots)))
            return chosen

        candidates = cls._generated_rig_candidates(model)
        if not candidates:
            return None
        owned = [obj for obj in candidates if cls._rig_belongs_to(model, obj)]
        if not owned:
            if trace is not None:
                trace.info("%d generated rig(s) in this file (%s), none of them this model's: chain bones share "
                           "their names across characters, so counting them cannot tell two models apart "
                           "(handoff 9aj)" % (len(candidates),
                                              ", ".join(obj.name for obj in candidates[:4])))
                trace.check("a generated rig belongs to this model", False,
                            "nothing here was generated for %s - the rig(s) present are %s. Run Full for this "
                            "model rather than wiring it to another character's rig."
                            % (model.name, ", ".join(obj.name for obj in candidates[:4])))
            return None
        if not roots:
            return owned[0]

        best, best_hits, tied = None, -1, 0
        for obj in owned:
            # Among the rigs this model owns, the one carrying most of these roots wins. An exact tie keeps the
            # first in `bpy.data.objects` (creation) order, because the earlier rig may be the one the model is
            # already wired to - displacing it needs the run to know better. A tie is reported.
            score = cls._chain_root_hits(obj, roots)
            if score == best_hits:
                tied += 1
            if score > best_hits:
                best, best_hits = obj, score
        if trace is not None:
            if len(owned) > 1:
                trace.info("%d rig(s) belong to %s; using %s, which carries %d of these %d chain roots (%s)%s"
                           % (len(owned), model.name, best.name, best_hits, len(roots),
                              ", ".join("%s %d" % (obj.name, cls._chain_root_hits(obj, roots))
                                        for obj in owned[:4]),
                              "; %d tied and the first was kept" % tied if tied else ""))
            if best_hits == 0:
                trace.check("the rig to wire is the one with these chains", False,
                            "none of the %d rig(s) belonging to %s carries any of them (%s)"
                            % (len(owned), model.name, ", ".join(obj.name for obj in owned[:4])))
        return best

    @staticmethod
    def _find_generated_rig(model, preferred=None, chains=None, trace=None):
        """`_find_rig_for_chains` for a run's `(rule, names)` chains - kept as the name the call sites use."""
        return RigifyChains._find_rig_for_chains(
            model, [names[0] for _rule, names in (chains or []) if names], trace, preferred)
    # --------------------------------------------------------------------------------------------------
    # the rules

    @staticmethod
    def _governing_rules(model, rules, trace):
        """First match wins, per bone: {bone name: rule}. Also returns the compiled type and skip lists.

        Skips take part in the ordering like any other rule: a `skip` rule placed after the skirt rules
        leaves the skirts alone, and a catch-all skip at the end of the table only catches what nothing
        else matched. (Applying skips as a pre-filter instead silently removed every chain the table had
        already claimed - the bug that made this baseline produce two chains.)
        """
        ordered = []
        compiled = []
        skips = []
        for rule in rules:
            try:
                regex = re.compile(rule["pattern"])
            except (re.error, KeyError) as error:
                trace.check("rule pattern compiles: %s" % rule.get("pattern"), False, str(error))
                continue
            is_skip = bool(rule.get("skip") or not rule.get("type"))
            ordered.append((rule, regex, is_skip))
            if is_skip:
                skips.append(regex)
            else:
                compiled.append((rule, regex))

        governing = {}
        skipped = 0
        for bone in model.data.bones:
            for rule, regex, is_skip in ordered:
                hit = regex.search(bone.name) if is_skip else regex.match(bone.name)
                if hit:
                    if is_skip:
                        skipped += 1
                    else:
                        governing[bone.name] = rule
                    break

        counts = {}
        for rule in governing.values():
            counts[rule["pattern"]] = counts.get(rule["pattern"], 0) + 1
        for rule, _regex in compiled:
            trace.info("rule %-32s governs %3d bone(s)" % (rule["pattern"], counts.get(rule["pattern"], 0)))
        trace.info("bones left out by skip rules: %d" % skipped)
        return governing, compiled, skips

    def _chains_for_rule(self, model, rule, names, skip_regexes):
        """Split one rule's bones into linear chains, following the model's own hierarchy.

        A bone with several matching children (physics chains carry `_Handle` siblings, and some carry
        branches) continues along the child whose name extends the current one the most; every other child
        starts a chain of its own, so nothing matching the rule is left without a type.
        """
        minimum = int(rule.get("min_bones", 2))
        matched = [name for name in names if name in model.data.bones]

        if str(rule.get("type", "")) in PER_BONE_RIG_TYPES:
            # A stub rule is not a chain: every bone it governs gets its own rig, one control each, because
            # that is what the animator's metarig does for cloth and hair (`Sp_He_Hair2_00_L` *and* `_01_L`
            # typed, not just the root). Each bone is its own single-bone group, so no `min_bones` applies -
            # rigify refuses a one-bone *chain*, but a `basic.super_copy` on one bone is exactly the point.
            return [[name] for name in matched], []

        def usable(child):
            # `matched` already excludes anything a skip rule claimed, so the walk only has to ask whether
            # this child is governed by the same rule - asking the skip patterns again would let a
            # catch-all skip (e.g. "^Sp_") break every chain into single bones
            return child.name in matched

        chains = []
        short = []

        def walk(start_name):
            chain = [start_name]
            current = model.data.bones[start_name]
            while True:
                children = [child for child in current.children if usable(child)]
                if not children:
                    break
                primary = max(children, key=lambda child: len(os.path.commonprefix([current.name,
                                                                                  child.name])))
                for other in children:
                    if other is not primary:
                        walk(other.name)  # a branch is a chain of its own
                current = primary
                chain.append(current.name)
            if len(chain) >= minimum:
                chains.append(chain)
            else:
                short.append((chain[0], len(chain)))

        roots = [name for name in matched
                 if model.data.bones[name].parent is None
                 or model.data.bones[name].parent.name not in matched]
        for root in roots:
            walk(root)
        return chains, short

    def _collect_chains(self, model, rules, trace, weights=None, require_weights=True):
        """Every chain the rules ask for, as (rule, [bone names]), chain root first.

        Chains that no vertex is weighted to are dropped, and a chain whose first bones carry no weight
        starts at the first bone that does. The animator's metarig does the same thing by hand: for the
        ears it keeps `Ear_01_L -> Ear_02_L -> Ear_03_L` (the bones the mesh uses) and leaves the parallel
        `Sp_He_Ear0_*` chain out entirely, which is why a rig that rigifies both shows two sets of ear
        tails and one of them moves nothing.
        """
        governing, compiled, skips = self._governing_rules(model, rules, trace)
        chains = []
        short_total = 0
        dropped_unweighted = 0
        trimmed = []
        trimmed_tails = []
        for rule, _regex in compiled:
            names = [name for name, owner in governing.items() if owner is rule]
            if not names:
                continue
            rule_chains, short = self._chains_for_rule(model, rule, names, skips)
            short_total += len(short)
            if require_weights and weights is not None and str(rule.get("type", "")) in PER_BONE_RIG_TYPES:
                # A per-bone rule keeps every bone it matched. The trimming below exists for *chains* - a
                # chain whose leading bones carry no weight starts at the first that does, and a chain's
                # unweighted leaf is dropped - but a stub is a single bone, so dropping an unweighted one
                # would silently remove a control the animator asked for, and there is no chain to trim.
                rule_chains = [chain for chain in rule_chains if chain]
            elif require_weights and weights is not None:
                kept = []
                minimum = max(int(rule.get("min_bones", 2)), 2)
                for chain in rule_chains:
                    weighted = [name for name in chain if weights.get(name, 0.0) > 0.0]
                    if not weighted:
                        dropped_unweighted += 1
                        continue
                    start = chain.index(weighted[0])
                    # trimming is only worth it when what is left is still a chain rigify accepts;
                    # a two-bone chain with an unweighted root keeps its root instead
                    if start and len(chain) - start >= minimum:
                        trimmed.append((rule["pattern"], chain[0], chain[start]))
                        chain = chain[start:]
                    # Trailing bones with no weight deform nothing, so rigify would give them a control
                    # whose only effect is a stretch that no vertex follows. Measured (rigify_chain_audit):
                    # the model's skirt leaves and its `Sp_He_Hair2_*_02` carry zero weight, and the
                    # animator's own chains stop at the last weighted bone - his hair2 is two bones where
                    # ours was three. Never trim below `min_bones`, which is why the skirts keep their leaf.
                    while len(chain) > minimum and weights.get(chain[-1], 0.0) <= 0.0:
                        trimmed_tails.append((rule["pattern"], chain[0], chain[-1]))
                        chain = chain[:-1]
                    kept.append(chain)
                rule_chains = kept
            chains.extend((rule, chain) for chain in rule_chains)
            trace.info("rule %-32s -> %2d chain(s) of >=%s bones%s"
                       % (rule["pattern"], len(rule_chains), rule.get("min_bones", 2),
                          ("; %d too short, e.g. %s" % (len(short), short[0][0])) if short else ""))
        for pattern, was, now in trimmed[:6]:
            trace.info("chain trimmed to its first weighted bone: %s -> %s (rule %s)" % (was, now, pattern))
        for pattern, root, dropped in trimmed_tails[:6]:
            trace.info("chain %s ends on its last weighted bone: dropped %s (rule %s)"
                       % (root, dropped, pattern))
        if trimmed_tails:
            trace.info("trailing unweighted bones dropped: %d" % len(trimmed_tails))
        if dropped_unweighted:
            trace.info("chains dropped for carrying no weights: %d" % dropped_unweighted)
        trace.info("chains to add: %d (too short, skipped: %d)" % (len(chains), short_total))
        lengths = sorted(len(chain) for _rule, chain in chains)
        if lengths:
            trace.info("chain lengths: min %d, median %d, max %d"
                       % (lengths[0], lengths[len(lengths) // 2], lengths[-1]))
        return chains

    @staticmethod
    def _bone_weights(meshes, trace):
        """How much weight each bone name carries, summed over every vertex of every skinned mesh."""
        weights = {}
        for mesh in meshes:
            groups = {group.index: group.name for group in mesh.vertex_groups}
            for vertex in mesh.data.vertices:
                for element in vertex.groups:
                    name = groups.get(element.group)
                    if name is not None:
                        weights[name] = weights.get(name, 0.0) + element.weight
        weighted = sum(1 for value in weights.values() if value > 0.0)
        trace.info("meshes use %d bone names with weight" % weighted)
        return weights

    @staticmethod
    def _chain_geometry(model, chains, trace):
        """Copy everything needed out of the model's bones as plain data.

        MMR rewrites the model's bone data while fitting, and holding references to `Bone` across that
        gives back stale pointers (they read as garbage, not as a clean ReferenceError), so nothing below
        this point touches the model's bone RNA.
        """
        geometry = {}
        missing = []
        for _rule, names in chains:
            for name in names:
                bone = model.data.bones.get(name)
                if bone is None:
                    missing.append(name)
                    continue
                geometry[name] = {
                    "head": bone.head_local.copy(),
                    "tail": bone.tail_local.copy(),
                    "z": (bone.matrix_local.to_3x3() @ Vector((0.0, 0.0, 1.0))).copy(),
                    "deform": bone.use_deform,
                    "parent": bone.parent.name if bone.parent else None,
                    "parent_head": bone.parent.head_local.copy() if bone.parent else None,
                }
        if missing:
            trace.check("every chain bone exists in the model", False, ", ".join(missing[:5]))
        return geometry

    # --------------------------------------------------------------------------------------------------
    # inserting the chains, at the moment rigify generates

    @staticmethod
    def _space_between(source, target):
        """Matrix taking a point in `source`'s armature space into `target`'s armature space."""
        return target.matrix_world.inverted() @ source.matrix_world

    def _metarig_parent_for(self, geometry, name, metarig, space, options, candidates, rule=None,
                            model=None):
        """Which metarig bone the chain root should hang off.

        MMR fitted the metarig to this model, so the bone it copied this model bone's parent from sits at
        the same place: match by position among the bones that already existed, then fall back to a named
        bone, then to the metarig root. Newly created chain bones are not candidates - some model parents
        sit exactly where the chain root does, and matching one of those would parent a bone to itself.
        """
        parent_head = geometry.get(name, {}).get("parent_head")
        model_parent = geometry.get(name, {}).get("parent")

        def closest(target, tolerance):
            """The best pre-existing metarig bone for a point: nearest first, ancestors before children.

            Several metarig bones can share a head position - MMR's metarig has both `spine.006` and `face`
            exactly at the model's `Head` - and the tie has to go to the higher one, or a chain ends up
            hanging off a child that is itself driven, which is how the ears would double up. Distance is
            compared at 0.1 mm so a real difference still wins.
            """
            candidates_within = []
            for bone in metarig.data.edit_bones:
                if bone.name not in candidates:
                    continue
                distance = (bone.head - target).length
                if distance <= tolerance:
                    depth = 0
                    parent = bone.parent
                    while parent is not None:
                        depth += 1
                        parent = parent.parent
                    candidates_within.append((round(distance / 1e-4), depth, bone.name, distance))
            if not candidates_within:
                return None
            candidates_within.sort()
            best = candidates_within[0]
            return best[3], best[2]

        # A rule can name the attachment. The name may be a metarig bone, or a *model* bone - which is the
        # steadier way to say it, because MMR's metarig does not use the model's names: there is no bone
        # called `Head` in it at all, the model's `Head` sits exactly on its `spine.006`. Resolving through
        # the model also keeps a rules file portable across characters.
        wanted = (rule or {}).get("parent")
        if wanted:
            if wanted in metarig.data.edit_bones:
                return wanted, "rule", 0.0, model_parent
            # `geometry` only holds the chain bones themselves, so the anchor has to come from the model
            anchor_head = None
            if model is not None:
                anchor = model.data.bones.get(wanted)
                if anchor is not None:
                    anchor_head = anchor.head_local
            if anchor_head is None:
                anchor_head = geometry.get(wanted, {}).get("head")
            if anchor_head is not None:
                hit = closest(space @ anchor_head, PARENT_MATCH_RADIUS)
                if hit is not None:
                    return hit[1], "rule:%s" % wanted, hit[0], model_parent
                return (None, "rule-unresolved: no metarig bone within %.2f m of the model bone '%s'"
                        % (PARENT_MATCH_RADIUS, wanted), None, model_parent)
            return (None, "rule-unresolved: the model has no bone called '%s'" % wanted, None,
                    model_parent)
        if parent_head is not None:
            # A `Follow` family is one rig per bone, so every bone in it is a root - including the ones whose
            # model parent is another bone of the same family, which the metarig does not have yet because this
            # run is about to add it. Those hang off that bone, exactly as the model has them: a chain family
            # switched to `Follow` used to fail its attachment check on every bone after the first (measured on
            # Maruzensky's hip ribbon: 8 checks, all "no metarig bone within 0.15 m of the model parent").
            same_run = getattr(self, "_added_bone_names", None) or set()
            if model_parent in same_run and model_parent in metarig.data.edit_bones:
                return model_parent, "same family", 0.0, model_parent
            target = space @ parent_head
            hit = closest(target, 1e-4)
            if hit is not None:
                return hit[1], "exact", hit[0], model_parent
            # Nothing sits exactly where the model's parent does - MMR's metarig simply has no bone for
            # `Sp_He_Ear0_L_00`, which is what the animator's own step 0 deletes. The nearest bone is the
            # right answer far more often than a fixed fallback: for the ears it is the head, which is where
            # his metarig puts the chain. A fixed fallback here is what left the ears parented to the spine,
            # so that moving the head left them behind.
            near = closest(target, PARENT_MATCH_RADIUS)
            if near is not None:
                return near[1], "nearest", near[0], model_parent
            # Nothing is *near*, and the fixed fallback (`spine`) is a worse answer than the closest thing
            # there is: a knee pad attached to the thigh at 0.2 m follows the leg, the same bone attached to
            # the spine does not. Measured on the fleet (chains-31): `1057_00`'s `Sp_Kn_Pantaloons*` have
            # `Knee_L`/`Knee_R` as their model parent and no metarig bone within 0.15 m of it, so all four went
            # to `spine`. The distance comes back with the answer and the run still fails its attachment check,
            # so this is a better rig and an honest report rather than a silent guess.
            far = closest(target, float("inf"))
            if far is not None:
                return far[1], "nearest-far", far[0], model_parent
        fallback = options.get("parent_fallback")
        if fallback and fallback in metarig.data.edit_bones:
            return fallback, "fallback", None, model_parent
        for bone in metarig.data.edit_bones:
            if bone.parent is None:
                return bone.name, "root", None, model_parent
        return None, "none", None, model_parent

    @staticmethod
    def _resample_chain(edit_bones, names, mode):
        """Move a chain's joints along its own line, either evenly or by lengthening the leaf.

        Returns `(root, shortest_before, longest_before, step, worst_shift)`, or None if the chain cannot
        be resampled. Both modes keep the polyline: the joints stay on the line the chain already runs
        along, so the chain still spans the same shape.

        * `even` puts one joint every equal step along the whole line.
        * `leaf` keeps every other joint exactly where the model has it and extends only the last bone to
          the length of the longest segment. This is what the animator's own metarig does to the ears - his
          `Ear_01_L/02_L/03_L` are 0.03571 m each with their heads on the model's own joints, while the
          model's own leaf is 0.01785 m - so it is the mode that reproduces a rig we can measure (9a).
        """
        chain = [edit_bones.get(name) for name in names]
        if len(chain) < 2 or any(bone is None for bone in chain):
            return None
        points = [chain[0].head.copy()] + [bone.tail.copy() for bone in chain]
        lengths = [(b - a).length for a, b in zip(points, points[1:])]
        total = sum(lengths)
        if total < 1e-5 or min(lengths) <= 1e-9:
            return None
        worst_shift = 0.0
        if mode == "leaf":
            step = max(lengths)
            direction = (points[-1] - points[-2]).normalized()
            leaf = chain[-1]
            target = leaf.head + direction * step
            worst_shift = (leaf.tail - target).length
            leaf.tail = target
            return names[0], min(lengths), max(lengths), step, worst_shift
        step = total / len(chain)
        joints = []
        travelled = 0.0
        index = 0
        for number in range(1, len(chain)):
            target = step * number
            while index < len(lengths) - 1 and travelled + lengths[index] < target:
                travelled += lengths[index]
                index += 1
            fraction = min(max((target - travelled) / lengths[index], 0.0), 1.0)
            joints.append(points[index] + (points[index + 1] - points[index]) * fraction)
        for number, joint in enumerate(joints):
            worst_shift = max(worst_shift, (chain[number].tail - joint).length)
            chain[number].tail = joint
            worst_shift = max(worst_shift, (chain[number + 1].head - joint).length)
            chain[number + 1].head = joint
        return names[0], min(lengths), max(lengths), step, worst_shift

    @staticmethod
    def _ensure_editable_active(obj, trace):
        """Make `obj` the active object, showing it first if it is hidden, and verify that it took.

        Two things conspire here. MMR hides the MMD armature at the end of its run - its per-object
        `Hide_mmd_skeleton` flag is off by default, which means "hide" (the panel calls it "No Hide
        skeleton") - and Blender silently refuses to make a hidden or excluded object the active one when
        the assignment is made from Python. The refusal is invisible from here, so the next
        `bpy.ops.object.mode_set()` fails its poll with "Context missing active object", which names
        neither the object nor the reason. Show it, activate it, check, and say what happened.
        """
        previous = (bool(obj.hide_get()), bool(obj.hide_viewport))
        if previous[0]:
            obj.hide_set(False)
        if previous[1]:
            obj.hide_viewport = False
        if bpy.context.view_layer.objects.active is not obj:
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
        bpy.context.view_layer.update()
        if bpy.context.view_layer.objects.active is not obj:
            raise RuntimeError(
                "%s could not be made the active object: it is hidden, or it is not in view layer '%s', "
                "so it cannot be put into Edit mode" % (obj.name, bpy.context.view_layer.name))
        if any(previous):
            trace.info("%s was hidden (hide=%s, excluded=%s); shown so its bones can be edited"
                       % (obj.name, previous[0], previous[1]))
        return previous

    @staticmethod
    def _restore_visibility(obj, previous):
        """Put the eye/monitor flags back the way MMR and the user left them."""
        hidden, excluded = previous
        if hidden and not obj.hide_get():
            obj.hide_set(True)
        if excluded and not obj.hide_viewport:
            obj.hide_viewport = True

    def _add_chains_to_metarig(self, model, metarig, chains, options, trace, geometry):
        """Create the chain bones in the metarig, type the chain roots and file them into collections."""
        space = self._space_between(model, metarig)
        rigid = space.to_3x3().normalized()
        # Run-wide defaults. A rule may override either one: the resolver copies `resample` and `connect_chain`
        # from a table rule into the resolved rule, and until this build nothing ever read them back, so a
        # per-family setting in the rules file was a silent no-op (code review, handoff 9ai). `resample: template`
        # per rule is the agreed shape for per-character joint lengths (handoff 9i), so the keys are honoured now.
        connect_default = bool(options.get("connect_chain", True))
        resample_default = str(options.get("resample", "keep")).lower()
        added = 0
        typed = 0
        resampled = []
        collections = {}
        already_typed = {pose_bone.name: pose_bone.rigify_type
                         for pose_bone in metarig.pose.bones if getattr(pose_bone, "rigify_type", "")}

        previous_active = bpy.context.view_layer.objects.active
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        visibility = None

        try:
            visibility = self._ensure_editable_active(metarig, trace)
            if metarig.mode != 'EDIT':
                bpy.ops.object.mode_set(mode='EDIT')
            edit_bones = metarig.data.edit_bones
            candidates = {bone.name for bone in edit_bones}  # bones that already existed
            # what this run adds, so a `Follow` bone can hang off the bone created for its model parent a
            # moment earlier in the same pass (see `_metarig_parent_for`)
            self._added_bone_names = set()
            # Chain roots whose attachment was not the model's own parent, kept until every chain bone exists:
            # the model parent may be a bone this same run adds *later* (`1032_30`'s `Tp_Hi_liquid0_L_00` hangs
            # off `Sp_Hi_Acc1_L_01`, and the `liquid0` family is added before `Acc1`), and re-trying after the
            # loop costs nothing and turns a fallback into the real parent.
            deferred_attachments = []
            for rule, names in chains:
                if all(name in edit_bones for name in names):
                    trace.info("chain already in the metarig, left alone: %s" % names[0])
                    continue
                # per-rule override, falling back to the run-wide options
                connect = bool(rule.get("connect_chain", connect_default))
                resample = str(rule.get("resample", resample_default)).lower()
                created_here = []
                for index, name in enumerate(names):
                    if name in edit_bones or name not in geometry:
                        continue
                    info = geometry[name]
                    new_bone = edit_bones.new(name)
                    new_bone.head = space @ info["head"]
                    # a connected chain needs every bone to end where the next one begins; the last bone
                    # keeps the model's own tail, so a leaf does not collapse to zero length
                    if connect and index + 1 < len(names):
                        new_bone.tail = space @ geometry[names[index + 1]]["head"]
                    else:
                        new_bone.tail = space @ info["tail"]
                    if (new_bone.tail - new_bone.head).length < 1e-6:
                        new_bone.tail = new_bone.head + (rigid @ Vector((0.0, 0.01, 0.0)))
                    new_bone.align_roll(rigid @ info["z"])
                    new_bone.use_deform = info["deform"]
                    added += 1
                    created_here.append(name)
                    self._added_bone_names.add(name)
                    if index == 0:
                        parent_name, how, distance, model_parent = self._metarig_parent_for(
                            geometry, name, metarig, space, options, candidates, rule, model)
                        if parent_name and parent_name in edit_bones:
                            new_bone.parent = edit_bones[parent_name]
                            detail = ("attached to %s by rule" % parent_name if how == "rule" else
                                      "attached to %s (%s match, %s from the model parent %s)"
                                      % (parent_name, how,
                                         "%.4f m" % distance if distance is not None else "no distance",
                                         model_parent))
                            trace.info("chain %-30s %s" % (name, detail))
                            # a chain root attached by the blind fallback follows the wrong body part:
                            # this is what left the ears on the spine while the head moved
                            if how in ("fallback", "root", "none", "nearest-far"):
                                deferred_attachments.append({
                                    "name": name, "parent": parent_name, "how": how,
                                    "distance": distance, "model_parent": model_parent,
                                    "bone": new_bone})
                        else:
                            trace.check("chain %s got a parent" % name, False,
                                        "no metarig bone matched the model parent %s" % model_parent)
                    else:
                        new_bone.parent = edit_bones[names[index - 1]]
                        new_bone.use_connect = connect
                typed += 1
                if resample in ("even", "leaf") and len(created_here) == len(names):
                    summary = self._resample_chain(edit_bones, names, resample)
                    if summary is None:
                        trace.info("chain %s could not be resampled, kept as it is" % names[0])
                    else:
                        resampled.append(summary)
            # Second pass: a chain root whose model parent this same run added (in a family the resolver reached
            # after it) can be attached to it now that it exists. Everything still unattached is reported as the
            # failure it is, with the distance, because a chain on the wrong body part is what the user sees as
            # "the model moves from somewhere else".
            for entry in deferred_attachments:
                model_parent = entry["model_parent"]
                if model_parent and model_parent in self._added_bone_names and model_parent in edit_bones:
                    entry["bone"].parent = edit_bones[model_parent]
                    trace.info("chain %-30s attached to %s, which this run added for its model parent (was "
                               "'%s' until the second pass)" % (entry["name"], model_parent, entry["parent"]))
                    continue
                if entry["how"] == "nearest-far":
                    trace.check("chain %s is attached where the model says" % entry["name"], False,
                                "%s: no metarig bone within %.2f m of the model parent '%s' - the nearest is "
                                "'%s' at %.4f m, which is where it was attached; add \"parent\": \"...\" to "
                                "the rule to say where it belongs"
                                % (entry["name"], PARENT_MATCH_RADIUS, model_parent, entry["parent"],
                                   entry["distance"] or 0.0))
                else:
                    trace.check("chain %s is attached where the model says" % entry["name"], False,
                                "%s: no metarig bone within %.2f m of the model parent '%s', so it was attached "
                                "to '%s' - give the family a rule naming a bone (\"parent\": \"...\", a metarig "
                                "bone like 'shin.L' or a model bone that is on the metarig)"
                                % (entry["name"], PARENT_MATCH_RADIUS, model_parent, entry["parent"]))
        finally:
            if metarig.mode == 'EDIT':
                bpy.ops.object.mode_set(mode='OBJECT')
            for obj in bpy.context.selected_objects:
                obj.select_set(False)
            if previous_active is not None:
                try:
                    previous_active.select_set(True)
                    bpy.context.view_layer.objects.active = previous_active
                except RuntimeError:
                    pass
            if visibility is not None:
                self._restore_visibility(metarig, visibility)

        # rig types and collections, in object mode
        for rule, names in chains:
            root_name = names[0]
            pose_bone = metarig.pose.bones.get(root_name)
            if pose_bone is None:
                continue
            if not self.Overwrite_Typed and already_typed.get(root_name):
                trace.info("chain %-30s already typed %s, kept" % (root_name, already_typed[root_name]))
                continue
            parameters = dict(rule.get("parameters") or {})
            if rule["type"] == "spines.basic_tail":
                # his setting, unless this rule names its own: without it a push in any direction but the
                # bone's own X leaves the chain rigid instead of curling it (see BASIC_TAIL_PARAMETERS)
                for key, value in BASIC_TAIL_PARAMETERS.items():
                    parameters.setdefault(key, value)
            for key, value in parameters.items():
                try:
                    setattr(pose_bone.rigify_parameters, key, value)
                except (AttributeError, TypeError) as error:
                    trace.check("parameter %s on %s" % (key, root_name), False, str(error))
            trace.info("chain %-30s typed %s%s" % (root_name, rule["type"],
                                                   " (%s)" % ", ".join(sorted(parameters))
                                                   if parameters else ""))
            pose_bone.rigify_type = rule["type"]
            if rule.get("collection"):
                collections.setdefault(rule["collection"], []).extend(names)

        # rigify puts a chain's controls in the collection its bones are in, and its FK / tweak bones in
        # sibling collections named "<collection> (FK)" and "<collection> (Tweak)", and a collection created
        # by a script is hidden by default - which is how the first version of this ended up with 132
        # controls the user could not select. His own metarig does not carry this triple for every family
        # (measured: `(FK)` only for Skirt, Jacket, Tail and Hair, and no cloth family has a `(Tweak)` at
        # all), so this is this fork's layout, not a copy of his - docs/COOLCHOU_RIG.md 3 and 8.
        for collection_name in collections:
            for suffix, targets in (("", collections[collection_name]), (" (FK)", ()), (" (Tweak)", ())):
                name = collection_name + suffix
                collection = metarig.data.collections.get(name)
                if collection is None:
                    collection = metarig.data.collections.new(name)
                # the family's own controls are visible; its FK / tweak sub-layers are selectable but off,
                # unless the run asked for every layer (Chain_Layers)
                show_collection(collection, visible=(not suffix or self.Chain_Layers == 'ALL'))
                for bone_name in targets:
                    bone = metarig.data.bones.get(bone_name)
                    if bone is not None:
                        collection.assign(bone)

        # rigify hides generated collections that have no UI row of their own, so every collection gets a
        # row here; the numbers are re-picked from the generated rig once rigify's own collections exist
        # (see `assign_chain_ui_rows`, called from the wiring step), because at this point there is nothing
        # to measure and a guessed row lands on top of a rigify one.
        assign_chain_ui_rows(metarig, list(collections), trace)

        # with the sibling collections in place, tell rigify to use them for each chain's FK / tweak controls
        for rule, names in chains:
            pose_bone = metarig.pose.bones.get(names[0])
            if pose_bone is not None and rule.get("collection"):
                set_collection_refs(pose_bone, rule["collection"], metarig, trace)

        if resampled:
            trace.info("resampled %d chains (%s): segments were %.5f-%.5f m; joints move up to %.5f m off "
                       "the model's own joints"
                       % (len(resampled), resample, min(entry[1] for entry in resampled),
                          max(entry[2] for entry in resampled),
                          max(entry[4] for entry in resampled)))
            for entry in resampled:
                trace.info("   %-30s segments %.5f-%.5f m -> %.5f m, joints moved up to %.5f m"
                           % (entry[0], entry[1], entry[2], entry[3], entry[4]))
        trace.info("added %d bones, typed %d chain roots, used %d collection(s)"
                   % (added, typed, len(collections)))
        return added, typed, sorted(collections)

    def _generation_wrapper(self, trace):
        """A stand-in for rigify's own generate function that adds the chains first."""
        state = {"done": False, "chains": 0}
        import rigify.generate as rigify_generate

        original = rigify_generate.generate_rig

        def wrapper(context, metarig):
            before = {obj.name for obj in bpy.data.objects if obj.type == 'ARMATURE'}
            if not state["done"]:
                trace.step("rigify is generating: adding the chains to the metarig first")
                _insert_started = time.time()
                # MMR appends its template object inside its own run, so the metarig cannot be known
                # beforehand: recognise it by shape instead, and only ever touch it once
                if self._looks_like_metarig(metarig, self._model):
                    trace.check("metarig recognised", True, "%s (%d bones)"
                                % (metarig.name, len(metarig.data.bones)))
                    try:
                        added, typed, collections = self._add_chains_to_metarig(
                            self._model, metarig, self._chains, self._options, trace, self._geometry)
                    except Exception as error:  # noqa: BLE001
                        # rigify's Generate operator catches whatever this raises and still reports FINISHED
                        # (`rigify/ui.py`), so without this the traceback vanished and the run blamed "rigify never
                        # called generate_rig" - which is the opposite of what happened, and sent the reader
                        # looking in the wrong place (beta code review, handoff 9aq). The insertion's own record is
                        # what `execute` reports from, so keep the real reason and the stack.
                        import traceback
                        state["error"] = "%s: %s" % (type(error).__name__, error)
                        trace.check("the chains were inserted into the metarig", False, state["error"])
                        trace.info(traceback.format_exc())
                        raise
                    state["chains"] = len(self._chains)
                    state["added"] = added
                    state["collections"] = collections
                    # verification: metarig space now matches the model's chain bones
                    drift = self._metarig_drift(self._model, metarig, self._chains)
                    if drift:
                        worst_bone = max(drift, key=lambda name: drift[name])
                        worst = drift[worst_bone]
                        if str(self._options.get("resample", "keep")).lower() in ("even", "leaf"):
                            # intentional: resampled joints do not sit on the model's own joints, so this
                            # is a measurement, not a failure
                            trace.info("chain joints sit off the model's own joints: worst %.5f m at %s "
                                       "(%s resampling, by design)"
                                       % (worst, worst_bone, self._options.get("resample")))
                        else:
                            # name the bone: with a profile in play this check can fail on one family while
                            # every other one is exact, and "worst 0.01000 m" alone does not say which
                            trace.check("chain rest positions match the model", worst < 1e-4,
                                        "worst %.5f m at %s" % (worst, worst_bone))
                    # rigify's Generate operator swallows exceptions from this function and still reports
                    # FINISHED, so remember that the insertion really completed
                    state["done"] = True
                else:
                    trace.check("metarig recognised", False,
                                "rigify was handed %s, which does not look like MMR's metarig"
                                % (metarig.name if metarig else None))
                    state["error"] = "metarig not recognised"
                # how long *this operator's* part of the generation took: the chains into the metarig and the
                # measurement afterwards. Everything else in the MMR call below is MMR's and rigify's own work,
                # and the trace reports both numbers separately (`timings:`), because "generating takes longer
                # with two characters in the file" is a claim about MMR unless this split says otherwise.
                state["insert_seconds"] = time.time() - _insert_started
            result = original(context, metarig)
            # Which rigify rig this run made: whatever armature exists now that did not exist a moment ago.
            # Not `"RIG-" + metarig.name`: measured in a two-character file, MMR's metarig is handed over as
            # `MMR-1004_00_arm` and rigify's rig comes out as `RIG-1004_00_arm`, and MMR renames it *again* after
            # generating (`RIG-MMR-...` -> `RIG-...`), so the name is MMR's business - the object is not. The
            # name lookup stays as a fallback for a rigify that reuses an object.
            fresh = [obj for obj in bpy.data.objects
                     if obj.type == 'ARMATURE' and obj.name not in before and obj is not self._model
                     and not obj.name.startswith("MMR-")]
            if fresh:
                state["rig_object"] = fresh[-1]
                state["rig"] = fresh[-1].name
            elif metarig is not None:
                guessed = bpy.data.objects.get("RIG-%s" % metarig.name)
                if guessed is not None and guessed.type == 'ARMATURE':
                    state["rig_object"] = guessed
                    state["rig"] = guessed.name
            return result

        return rigify_generate, original, wrapper, state

    @staticmethod
    def _looks_like_metarig(obj, model):
        """MMR's metarig: an armature that is neither the model nor a generated rig, with rigify names."""
        if obj is None or obj.type != 'ARMATURE' or obj is model:
            return False
        if RigifyChains._looks_like_control_rig(obj):
            return False
        names = {bone.name for bone in obj.data.bones}
        return "spine" in names and ("upper_arm.L" in names or "thigh.L" in names)

    @staticmethod
    def _metarig_drift(model, metarig, chains):
        """Distance between each chain bone's rest position here and on the model."""
        space = metarig.matrix_world.inverted() @ model.matrix_world
        drift = {}
        for _rule, names in chains:
            for name in names:
                model_bone = model.data.bones.get(name)
                metarig_bone = metarig.data.bones.get(name)
                if model_bone is None or metarig_bone is None:
                    continue
                drift[name] = (metarig_bone.head_local - (space @ model_bone.head_local)).length
        return drift

    # --------------------------------------------------------------------------------------------------
    # the model side: making the model follow the new chains

    @staticmethod
    def _release_wired_bones(model, chain_groups, trace, keep_links=False):
        """Cut the chain roots loose from their MMD parents, and optionally keep the chains linked.

        A chain bone such as `Sp_Ch_Jacket0_L_00` is a child of `Chest`, which MMR already drives from the
        rig. Leaving that parent in place while also following the rig's `ORG-Sp_Ch_Jacket0_L_00` makes the
        bone inherit the same motion twice: moving `torso`, `neck` or `head` moves the cloth, ears and
        skirt roughly twice as far as the rig intends. Clearing the parent changes nothing about the bone's
        rest position (heads and tails are armature-space) - it only stops the double inheritance, leaving
        the follow constraint as the single source of that bone's pose.

        With `keep_links` the release stops at the root and the chain keeps its *own* hierarchy
        (`Ear_02_L <- Ear_01_L`), because that is what lets Blender bend the skin as a B-Bone chain the way
        the animator's rig does - his mesh is skinned to a connected, 8-segment deform chain, ours was three
        parentless rigid bones. The children are then driven by rotation only (see `_wire_model_to_chains`),
        which is what keeps the retained parent from doubling.
        """
        roots = [chain[0] for chain in chain_groups if chain]
        names = [name for chain in chain_groups for name in chain]
        previous_active = bpy.context.view_layer.objects.active
        previous_mode = previous_active.mode if previous_active is not None else 'OBJECT'
        released = []
        relinked = []
        # `not_connected` holds bare bone names, because that is how `_wire_model_to_chains` tests membership;
        # `not_connected_notes` holds the same joints with their gap for the trace. They used to be one list of
        # formatted strings, so `name not in unconnected_joints` could never be true and a bbone chain child at a
        # gapped joint always took the rotation-only path its own comment says it must not (code review, 9ai).
        not_connected = []
        not_connected_notes = []
        linked_ok = []
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        # MMR leaves the MMD armature hidden (see _ensure_editable_active), and a hidden armature cannot be
        # activated from Python, so this is where a FULL run used to die with "Context missing active
        # object" - before a single constraint was added, leaving the rig driving nothing. This is a
        # staticmethod, so the helpers are reached through the class, not through `self`.
        visibility = None
        try:
            visibility = RigifyChains._ensure_editable_active(model, trace)
            if model.mode != 'EDIT':
                bpy.ops.object.mode_set(mode='EDIT')
            edit_bones = model.data.edit_bones
            for name in (roots if keep_links else names):
                bone = edit_bones.get(name)
                if bone is not None and bone.parent is not None:
                    released.append("%s<-%s" % (name, bone.parent.name))
                    bone.parent = None
            if keep_links:
                for chain in chain_groups:
                    for index in range(1, len(chain)):
                        bone = edit_bones.get(chain[index])
                        previous = edit_bones.get(chain[index - 1])
                        if bone is None or previous is None:
                            continue
                        gap = (previous.tail - bone.head).length
                        if gap > 1e-4:
                            # a chain whose joints are not touching would jump when connected, so it stays
                            # as it is and the children are driven by position as well
                            not_connected.append(bone.name)
                            not_connected_notes.append("%s (%.4f m gap)" % (bone.name, gap))
                            bone.parent = previous
                            continue
                        bone.parent = previous
                        bone.use_connect = True
                        linked_ok.append(bone.name)
        finally:
            if model.mode == 'EDIT':
                bpy.ops.object.mode_set(mode='OBJECT')
            for obj in bpy.context.selected_objects:
                obj.select_set(False)
            if previous_active is not None and previous_active.name in bpy.data.objects:
                try:
                    previous_active.select_set(True)
                    bpy.context.view_layer.objects.active = previous_active
                    if previous_mode != 'OBJECT' and previous_active.mode != previous_mode:
                        bpy.ops.object.mode_set(mode=previous_mode)
                except RuntimeError:
                    pass
            if visibility is not None:
                RigifyChains._restore_visibility(model, visibility)
        if released:
            trace.info("released %d chain root(s) from their MMD parents (first: %s)"
                       % (len(released), ", ".join(released[:3])))
        if keep_links:
            trace.info("chain links re-parented: %d connected, %d left unconnected (no touching joints: %s)"
                       % (len(linked_ok), len(not_connected),
                          ", ".join(not_connected_notes[:3]) or "none"))
        return released, not_connected

    def _wire_model_to_chains(self, model, rig, chains, trace, release_parents=True, resample="keep",
                              chain_deform="rigid"):
        """Let the model's own chain bones follow the rig's copies of the same name.

        `CHILD_OF` with the inverse matrix taken from the target bone's *rest* matrix, rather than MMR's
        plain world-space copy-transforms: that moves the model bone by the rig bone's motion and leaves its
        rest alone. A world-space copy instead teleports the model bone onto the rig bone, so any difference
        between the two rest poses becomes a visible pull on the mesh - which is what a mismatched metarig
        produced the first time round.

        In `bbone` mode (his architecture) the chain keeps its own hierarchy, every bone gets the same
        8 B-Bone segments rigify gives its deform chain, and only the root is a `CHILD_OF`: the children are
        set to the rig bone's world rotation with `COPY_ROTATION` in REPLACE mode, so the parent they keep
        supplies their position and the constraint supplies their rotation - once each, not twice.
        """
        groups = [[name for name in chain if ("ORG-" + name) in rig.pose.bones and name in model.pose.bones]
                  for _rule, chain in chains]
        groups = [chain for chain in groups if chain]
        wireable = [name for chain in groups for name in chain]
        all_names = [name for _rule, chain in chains for name in chain]
        unwireable = [name for name in all_names if name not in wireable]
        # `chain_deform` can be set per rule as well as for the whole run. The connected B-Bone chain is what
        # makes the animator's tip tweaks respond, and it is verified on the skirts (their controls answer
        # within 0.999-1.001 of his there); on the ears the same treatment turned a middle-bone push into
        # 2.26x and froze the tip in the pair test, so it is opt-in per family rather than a global switch.
        modes = {}
        for rule, chain in chains:
            mode = str(rule.get("chain_deform", chain_deform)).lower()
            for name in chain:
                modes[name] = mode
        linked_names = {name for name in wireable if modes.get(name) == "bbone"}
        roots = {chain[0] for _rule, chain in chains if chain and modes.get(chain[0]) == "bbone"}
        linked_groups = [[name for name in chain if name in linked_names] for _rule, chain in chains]
        linked_groups = [chain for chain in linked_groups if chain]
        rigid_groups = [[name for name in chain if name not in linked_names] for _rule, chain in chains]
        rigid_groups = [chain for chain in rigid_groups if chain]
        unconnected_joints = []
        if release_parents:
            if linked_groups:
                _released, unconnected_joints = self._release_wired_bones(
                    model, linked_groups, trace, keep_links=True)
            if rigid_groups:
                self._release_wired_bones(model, rigid_groups, trace, keep_links=False)
        linked = bool(linked_names)
        trace.info("chain deform: %d bone(s) as a connected B-Bone chain, %d as rigid followers"
                   % (len(linked_names), len(wireable) - len(linked_names)))

        for name in sorted(linked_names):
            bone = model.data.bones.get(name)
            if bone is not None:
                bone.bbone_segments = BBONE_SEGMENTS
        if linked:
            trace.info("B-Bone segments set to %d on %d chain bones (his deform chain uses the same)"
                       % (BBONE_SEGMENTS, len(linked_names)))

        # Which bone a person actually poses, per rig type. This matters because the answer differs and the
        # names look the same:
        #   * `spines.basic_tail` - the control is the chain's own root bone, named after the model's bone;
        #   * `basic.super_copy`  - the control is the bone itself;
        #   * `limbs.spline_tentacle` - the control is `<stem>_master`. The bone that carries the model's own
        #     name still exists on the rig and is **inert** (rigify drives the ORG chain from the spline and
        #     from the master, not from it), so grabbing it does nothing at all - reported as "added bones
        #     don't have any effect", and measured: pushing `Sp_He_Hair4_L_00` moved the model 0.00000 m,
        #     pushing `Sp_He_Hair4_L_master` moved it 0.05000 m (`spline_control_check.py`, handoff 9ac).
        spline_controls, root_controls = [], 0
        for rule, names in chains:
            root = names[0]
            if str(rule.get("type")) != "limbs.spline_tentacle":
                root_controls += 1
                continue
            stem = root.rsplit("_", 1)[0]
            master = stem + "_master"
            if master in rig.pose.bones:
                spline_controls.append("%s -> pose %s" % (root, master))
            else:
                spline_controls.append("%s -> pose %s (no _master found)" % (root, root))
        if spline_controls:
            trace.info("%d chain(s) are spline chains: their control is the `_master` bone, and the bone named "
                       "after the chain is inert on the rig - %s"
                       % (len(spline_controls), "; ".join(spline_controls[:4])
                          + ("; ..." if len(spline_controls) > 4 else "")))
        trace.info("%d chain(s) are posed at their own root bone (basic_tail / follow), %d with a `_master` "
                   "(spline)" % (root_controls, len(spline_controls)))
        wired = 0
        missing = list(unwireable)
        worst_rest_delta = 0.0
        rest_deltas = {}
        rotation_driven = 0

        # What "no motion" means for the follow constraints: each ORG copy's world matrix **with the rig
        # unposed**. Taking it from the bone's rest matrix is not the same thing, and the difference is
        # model-dependent. MMR bakes rigify's spline pose into the rest pose (`pose.armature_apply`,
        # `Controller.py:1233`), so on a spline family the ORG rest ends up where the spline wanted it -
        # 0.03674 m off the model's own joint on Maruzensky and 0.13624 m on Special Week (character 1001).
        # A rest-matrix inverse then makes the constraint *non*-identity at rest, and the model bone picks up
        # the ORG copy's pose-vs-rest offset plus a lever-arm term, because the rotation swings that whole
        # distance. Measured on 1001: `Sp_He_Ribbon0_L_01` moved 0.003628 m at rest against an ORG pose
        # offset of 0.002758 m - the extra 0.000870 m is the lever arm - which is what tripped the run's own
        # `the model is undisturbed at rest` gate there. Unposing the rig to read the reference makes the
        # constraint exactly identity at rest on any model, whatever the bake moved
        # (docs/RIGIFY_CHAINS_HANDOFF.md 9r).
        references = {}
        if wireable:
            saved_basis = {pose_bone.name: pose_bone.matrix_basis.copy()
                           for pose_bone in rig.pose.bones}
            for pose_bone in rig.pose.bones:
                pose_bone.matrix_basis = Matrix.Identity(4)
            bpy.context.view_layer.update()
            absorbed = {}
            for name in wireable:
                rig_bone = rig.data.bones.get("ORG-" + name)
                rig_pose_bone = rig.pose.bones.get("ORG-" + name)
                if rig_pose_bone is not None:
                    reference = (rig.matrix_world @ rig_pose_bone.matrix).copy()
                    if rig_bone is not None:
                        absorbed[name] = (reference.translation
                                          - (rig.matrix_world @ rig_bone.head_local)).length
                elif rig_bone is not None:
                    reference = rig.matrix_world @ rig_bone.matrix_local
                else:
                    continue
                references[name] = reference
            for pose_bone in rig.pose.bones:
                pose_bone.matrix_basis = saved_basis[pose_bone.name]
            bpy.context.view_layer.update()
            worst_absorbed = max(absorbed.values()) if absorbed else 0.0
            if worst_absorbed > 1e-5:
                worst_name = max(absorbed, key=lambda key: absorbed[key])
                trace.info("follow reference taken with the rig unposed: it differs from the ORG rest pose "
                           "by up to %.5f m (%s) - rigify's spline pose, which MMR baked into the rest pose; "
                           "the wiring absorbs it instead of passing it on to the model"
                           % (worst_absorbed, worst_name))
            else:
                trace.info("follow reference taken with the rig unposed - identical to the ORG rest pose")

        for name in wireable:
            rig_bone = rig.data.bones.get("ORG-" + name)
            pose_bone = model.pose.bones.get(name)
            if rig_bone is None or pose_bone is None:
                missing.append(name)
                continue
            model_bone = model.data.bones.get(name)
            if model_bone is not None:
                delta = ((rig.matrix_world @ rig_bone.head_local)
                         - (model.matrix_world @ model_bone.head_local)).length
                worst_rest_delta = max(worst_rest_delta, delta)
                if delta > 1e-5:
                    rest_deltas[name] = delta
            # a chain root always follows its rig bone outright; a linked child is rotated by its own rig
            # bone and positioned by the chain it belongs to
            if name in linked_names and name not in roots and name not in unconnected_joints:
                follow = pose_bone.constraints.get(FOLLOW_CONSTRAINT_NAME)
                if follow is not None:
                    pose_bone.constraints.remove(follow)
                constraint = pose_bone.constraints.get(ROTATE_CONSTRAINT_NAME)
                if constraint is None:
                    constraint = pose_bone.constraints.new('COPY_ROTATION')
                    constraint.name = ROTATE_CONSTRAINT_NAME
                constraint.target = rig
                constraint.subtarget = "ORG-" + name
                constraint.target_space = 'WORLD'
                constraint.owner_space = 'WORLD'
                constraint.mix_mode = 'REPLACE'
                rotation_driven += 1
            else:
                constraint = pose_bone.constraints.get(FOLLOW_CONSTRAINT_NAME)
                if constraint is None:
                    constraint = pose_bone.constraints.new('CHILD_OF')
                    constraint.name = FOLLOW_CONSTRAINT_NAME
                constraint.target = rig
                constraint.subtarget = "ORG-" + name
                constraint.owner_space = 'WORLD'
                constraint.target_space = 'WORLD'
                reference = references.get(name)
                if reference is None:
                    reference = rig.matrix_world @ rig_bone.matrix_local
                constraint.inverse_matrix = reference.inverted()
            wired += 1
        if linked:
            trace.info("chain children driven by world-space COPY_ROTATION (REPLACE): %d, CHILD_OF: %d"
                       % (rotation_driven, wired - rotation_driven))
        if missing:
            trace.check("every chain bone has an ORG copy to follow", False,
                        "%d missing, e.g. %s" % (len(missing), ", ".join(missing[:5])))
        else:
            trace.check("every chain bone has an ORG copy to follow", True, "%d bones" % wired)
        even = str(resample).lower() in ("even", "leaf")
        # A `limbs.spline_tentacle` chain's ORG bones are driven by a spline, and the spline does not reproduce
        # the rest chain exactly - so those joints sit off the model by an amount that is inherent to the rig
        # type, not a mistake in the placement. The placement itself is already gated exactly where it can be:
        # `chain rest positions match the model` reads the metarig and reports 0.00000 m. This check therefore
        # only fails when the offset is large **relative to the chain's own length**, which is what a genuinely
        # misplaced chain looks like; the plain number goes to an information line either way.
        #
        # Two corrections in chains-30, both from the first fleet run over 125 models (handoff 9ad):
        #
        # * **the denominator was one bone, not the chain.** Walking up the model's hierarchy to sum the chain
        #   stops at the first step, because `_release_wired_bones` has already made every chain bone parentless
        #   by the time this runs - so "that chain's own length" was the worst bone's own length. Measured on
        #   1009_00: 0.19252 m reported, while the chain the bone belongs to is 1.06728 m long. The chain is now
        #   taken from the run's own chain list, so the number in the message is the number it names.
        # * **the thresholds were calibrated against that wrong number.** Over the 125-model fleet the largest
        #   offset of any kind is 0.09953 m (`Sp_He_Hair4_C_04`, 1042_00), all 11 failures were this check, and
        #   every one of them was a spline hair chain sitting 14-86% of a bone off - the bake, not placement.
        #   A genuinely misplaced chain is a different order of magnitude: the reported 1.74168 m at
        #   `Sp_He_Ribbon0_L_01` on a 0.06024 m chain (handoff 1b). So the check fails at *half the chain* or
        #   *0.25 m absolute* - 1.8x above the largest bake measured anywhere, 7x below the smallest known fault.
        worst_name = max(rest_deltas, key=lambda key: rest_deltas[key]) if rest_deltas else None
        worst_len = 0.0            # the whole chain the worst bone belongs to
        worst_bone_len = 0.0       # and the one bone the offset is on
        if worst_name:
            worst_chain = next((names for _rule, names in chains if worst_name in names), [worst_name])
            for name in worst_chain:
                bone = model.data.bones.get(name)
                if bone is not None:
                    worst_len += (bone.tail_local - bone.head_local).length
            bone = model.data.bones.get(worst_name)
            if bone is not None:
                worst_bone_len = (bone.tail_local - bone.head_local).length
        shares = ("%.0f%% of that chain's own %.5f m and %.0f%% of the %.5f m bone it is on"
                  % (100.0 * worst_rest_delta / worst_len if worst_len else 0.0, worst_len,
                     100.0 * worst_rest_delta / worst_bone_len if worst_bone_len else 0.0, worst_bone_len))
        if even:
            trace.check("chain controls sit near the model", worst_rest_delta < 0.10,
                        "worst %.5f m - joints are resampled (%s), so they sit off the model's own joints by "
                        "design" % (worst_rest_delta, resample))
        elif worst_name and (worst_rest_delta > worst_len * 0.5 or worst_rest_delta > 0.25):
            trace.check("chain controls sit near the model", False,
                        "worst %.5f m at %s - %s; a spline chain's own bake measures up to 0.136 m over 125 "
                        "models, so this is placement rather than the bake"
                        % (worst_rest_delta, worst_name, shares))
        else:
            trace.info("chain controls sit up to %.5f m off the model%s - rigify's spline pose, which MMR "
                       "bakes into the rest pose (%s); the wiring absorbs it, and `the model is undisturbed "
                       "at rest` is the gate that matters"
                       % (worst_rest_delta, " at %s" % worst_name if worst_name else "",
                          shares if worst_name else "no joint is off at all"))
        # Where the offset comes from, measured end to end (handoff 9p): the metarig the chains are added to
        # is exact (the check above this one reports 0.00000 m), and so is the generated rig at the end of
        # every one of rigify's stages. `limbs.spline_tentacle` drives its ORG bones with COPY_TRANSFORMS
        # from a chain that follows the spline curve, and that curve does not reproduce the rest chain
        # exactly, so each ORG bone sits a little *posed* off its rest position - 0.036735 m at the worst
        # head-ribbon joint. MMR then runs `bpy.ops.pose.armature_apply(selected=False)`
        # (`Controller.py:1233`), which writes that pose into the rest pose: the same run measures the pose
        # offset at 0.036735 m before that call and the rest offset at 0.036735 m after it. `spines.
        # basic_tail` chains are unaffected (0.000000 m), which is the family split this prints.
        # The animator's rig carries the identical deviation in its *pose* and never bakes it, so his rest
        # joints are exact and the world position of his chains at rest is the same as ours
        # (`his_org_pose_check.py`: 0.036736 m pose offset there, rest offset 0.000000).
        for prefix, delta in sorted(rest_deltas.items(), key=lambda item: -item[1])[:6]:
            trace.info("chain joint %-26s sits %.5f m off the model (rigify's spline pose, baked into the "
                       "rest pose by MMR's pose.armature_apply - the animator's rig has the same deviation "
                       "unbaked)" % (prefix, delta))
        # A bone that keeps its MMD parent AND carries a full follow constraint inherits the same motion
        # twice, so its chain overshoots with a signature of about 2x (this is what the pair test showed for
        # the neck). Independent review measured the mechanism: each level adds its ORG delta on top of the
        # parent-inherited motion. In `bbone` mode the children are meant to keep the chain - what must be
        # free of a parent is the root, and what must not carry both constraints is any bone at all.
        doubled = []
        for name in wireable:
            bone = model.data.bones.get(name)
            pose_bone = model.pose.bones.get(name)
            if bone is None or pose_bone is None:
                continue
            if name in roots and bone.parent is not None:
                doubled.append(name + " (root still parented)")
            if pose_bone.constraints.get(FOLLOW_CONSTRAINT_NAME) and pose_bone.constraints.get(
                    ROTATE_CONSTRAINT_NAME):
                doubled.append(name + " (both constraints)")
        trace.check("no chain bone is driven twice", not doubled,
                    ", ".join(doubled[:5]) if doubled else "%d bones checked" % len(wireable))
        if linked:
            spans = [name for name in sorted(linked_names)
                     if model.data.bones.get(name) is not None
                     and model.data.bones[name].bbone_segments != BBONE_SEGMENTS]
            trace.check("linked chain bones bend like his (B-Bone segments)", not spans,
                        "%d without segments, e.g. %s" % (len(spans), ", ".join(spans[:5]))
                        if spans else "%d bones with %d segments" % (len(linked_names), BBONE_SEGMENTS))
        # the rigid follower mode keeps the old, verified gate: no chain bone may hold a parent
        still_parented = [name for name in wireable
                          if name not in linked_names
                          and model.data.bones.get(name) is not None
                          and model.data.bones[name].parent is not None]
        trace.check("no rigid chain bone still has a parent", not still_parented,
                    "%d doubled, e.g. %s" % (len(still_parented), ", ".join(still_parented[:5]))
                    if still_parented else "%d bones checked" % (len(wireable) - len(linked_names)))
        return wired

    @staticmethod
    def _report_coverage(model, rules, chains, trace):
        """What the run typed, and every physics family it left alone.

        The rules file ends with a catch-all `^Sp_` skip, so a family nothing names gets no control **and no
        message**. Measured over the 125 models in `uma_style_dump`: `Sp_He_Hair0` is in that position on 119 of
        them, `Sp_He_Hair3` on 78, `Hair1` on 51, `Hair5` on 35 - a large, silent gap (handoff 9s). A run that
        says which families it ignored turns that into a line to read, and it is how a profile that is missing a
        family shows up.

        Deliberately an `info` rather than a `check`: leaving a family to MMR is the documented default for
        everything the rules do not name, so failing a run for it would be a false alarm on almost every model.
        """
        typed = {}
        for rule, names in chains:
            for name in names:
                typed[profile_family_of(name)] = rule.get("type", "")
        governing = []
        for rule in rules:
            if rule.get("skip") or not rule.get("type"):
                continue
            try:
                governing.append((rule, re.compile(rule["pattern"])))
            except re.error:
                continue
        skipped = {}
        for bone in model.data.bones:
            name = bone.name
            # the same prefix tuple `physics_families` uses, `Ear` included: without it an `Ear_*` family that ends
            # up with no rule (or whose chain the weight filter drops) could never appear here, and the run would
            # print "every physics family in this model got a rule" while contradicting its own other lines
            # (code review, handoff 9ai)
            if name.endswith("_Handle") or not name.startswith(("Sp_", "Tp_", "Ex_", "Ear")):
                continue
            if profile_family_of(name) in typed:
                continue
            if any(regex.match(name) for _rule, regex in governing):
                continue
            skipped[profile_family_of(name)] = skipped.get(profile_family_of(name), 0) + 1
        trace.info("coverage: %d families typed by the rules (%s)"
                   % (len(typed), ", ".join(sorted(typed)[:8]) + ("..." if len(typed) > 8 else "")))
        if skipped:
            worst = sorted(skipped.items(), key=lambda item: -item[1])[:8]
            trace.info("coverage: %d physics families left to MMR, %d bones - largest: %s"
                       % (len(skipped), sum(skipped.values()),
                          ", ".join("%s(%d)" % item for item in worst)))
            if "Sp_He_Ear0" in skipped:
                trace.info("coverage: Sp_He_Ear0 is skipped on purpose - his pipeline deletes it and no "
                           "vertex is weighted to it (docs/RIGIFY_CHAINS_HANDOFF.md 9m)")
        else:
            trace.info("coverage: every physics family in this model got a rule")

    def _refresh_families(self, context, trace=None, keep=True):
        """Fill the Advanced rows from the model in the scene, and settle what the fields should say.

        Called from `invoke` (so the dialog opens on this model) and from `execute` (so a script that calls
        `bpy.ops.uma.rigify_chains(...)` directly gets the same list, and a redo on another model does not keep
        stale rows). Existing values are kept by family name, so a run does not throw away what the user set.
        """
        rules, options, source, presets = preset_table(self, context)
        model, _meshes = self._find_model_armature(context, trace or Trace(False))
        if model is None:
            return {}, {}, options, source, None
        fixed = dict(options.get("fixed_families") or {})
        present = physics_families(model)
        identity, _bone = model_identity(model)
        previous = {item.family: item.mode for item in self.Families} if keep else {}
        self.Populating = True
        try:
            self.Families.clear()
            for family in sorted(present):
                bones = sorted(present[family])
                item = self.Families.add()
                item.family = family
                item.group = family_group(family, bones, rules)
                item.bones = bones[0]
                item.fixed = fixed.get(family, "")
            modes, table_modes, label = family_defaults(
                rules, presets, self.Preset, self.Families, identity,
                str(options.get("preset_cross_costume", "hair")).lower(),
                options.get("unmatched_families") or {})
            for item in self.Families:
                # a fixed family shows its type and is locked; everything else starts from the preset / table, or
                # from what this run already had for that family
                item.mode = modes.get(item.family, MODE_OFF)
                item.default_mode = table_modes.get(item.family, MODE_OFF)
                if item.family in previous:
                    item.mode = previous[item.family]
            # a script's own choices, applied last: they go through exactly the same field the dialog writes, so
            # there is one place a choice can live and no second code path to keep in step
            overridden = 0
            for entry in (getattr(self, "Choices", "") or "").split(";"):
                family, _, mode = entry.partition("=")
                family, mode = family.strip(), mode.strip().upper()
                if not family or mode not in MODE_TYPES:
                    continue
                for item in self.Families:
                    if item.family == family and not item.fixed:
                        item.mode = mode
                        overridden += 1
            if overridden:
                self.Preset = PRESET_CUSTOM
                sync_group_fields(self)
            # A preset whose families disagree inside one group cannot be shown by a single group dropdown, so
            # the panel opens on Advanced - otherwise the first click on a group row would flatten a character
            # that is deliberately mixed (Maruzensky: Hair0 a stub, Hair2 and Hair4 chains).
            mixed = mixed_groups(self, TYPE_MODES.get((options.get("unmatched_families") or {}).get("type")))
            self.Forced_Advanced = ", ".join(mixed)
            if mixed:
                self.Mode = 'ADVANCED'
            # A preset keyed for another costume of the same character is applied on a skeleton match, and the
            # panel has to say that out loud: the user's report was `preset 1058_00` on a model whose own identity
            # is `1058_40` (the Dot-O-Lantern costume), and nothing in the label said the two were not the same
            # costume (handoff 9af). The choice itself is deliberate (`detect_preset`: one skeleton match is a
            # strong hint), so this informs rather than refuses. This block belongs *here*, where `identity` is
            # read, and not in `sync_preset` - it was put there by mistake first, and `sync_preset` is the first
            # thing `draw` calls, so every redraw raised `NameError: identity` and the dialog came up empty
            # (handoff 9ag).
            if identity and self.Preset not in (PRESET_DEFAULT, PRESET_CUSTOM) and self.Preset != identity:
                # The warning goes *first*: the note is clipped to a single panel line, and measured with the
                # clause appended it read `preset 1058_00 (Meta_Base_Head_Meisho_Doto, 7 families, 5 of them
                # different from the table) - picked for '1058_00': t...` - that is, the half that matters was
                # exactly what fell off the end (handoff 9ag).
                label = ("'%s' is another costume's preset - this model is '%s' (%s)"
                         % (self.Preset, identity, label))
            self.Preset_Note = label if len(label) < 120 else label[:117] + "..."
        finally:
            self.Populating = False
        if trace is not None:
            trace.info("families in this model: %d (%s)" % (len(self.Families), label))
        return modes, table_modes, options, source, model

    def table_modes_from_rows(self):
        """What the rules table alone says per family, as stored on each row when it was filled.

        Kept on the rows rather than in a dict on the operator: the dict would be Python state that does not
        survive a redraw (see `sync_group_fields`), and the rows already carry it.
        """
        return {item.family: item.default_mode for item in self.Families}

    def _chosen_modes(self, trace=None):
        """`{family: mode}` from the family rows - the only place a choice is stored - and the table's answers."""
        return ({item.family: item.mode for item in self.Families}, self.table_modes_from_rows())

    def invoke(self, context, event):
        """Fill the family rows for this model, pick a preset, and open the dialog.

        The panel calls this operator with no properties at all (AddonPanels.py), so everything the run needs has
        to be settled here: which families this model has, and which preset answers for them. Autodetection only
        picks a *value for the preset field* - the preset then fills the fields below and gets out of the way,
        which is the difference between this and the `Profile` dropdown it replaces.
        """
        trace = Trace(self.Debug)
        self._refresh_families(context, trace)
        rules, options, source, presets = preset_table(self, context)
        model, _meshes = self._find_model_armature(context, trace)
        # The dialog *states* a detected step, so it has to be the step the run takes: "Step: Wire only - ...
        # this is the state a run that stopped before wiring leaves behind" printed above a field reading `Full`
        # is a panel promising something it will not do (code review, handoff 9ai). The field stays editable -
        # this only sets its opening value, and a script that passes `Step=` is untouched because it never
        # reaches `invoke`.
        if model is not None:
            try:
                detected, why = detect_step(model)
                self.Step = detected
                trace.info("step detected from the scene: %s - %s" % (detected, why))
            except Exception as error:  # noqa: BLE001 - a panel must never raise
                trace.info("step could not be detected (%s)" % error)
        if model is not None and not self.Preset_Chosen:
            key, why = detect_preset(presets, model, trace)
            if key:
                self.Preset = key
                self.Preset_Chosen = True
                self._refresh_families(context, trace)
            trace.info("autodetect: %s" % why)
        return context.window_manager.invoke_props_dialog(self, width=520)

    def _draw_model(self, context):
        """The model armature for the panel's own use, found from the context.

        Deliberately *not* `self._model`: that is set in `execute`, and Python state on an operator does not
        survive into `draw` - using it there produced
        `AttributeError: 'RigifyChains' object has no attribute '_model'` on every redraw, which aborted the
        panel mid-build (the reported "More settings opens a collapsed empty panel"). Anything the panel needs
        has to be either an RNA property or looked up from the context, and looked up is better here because it
        cannot go stale.
        """
        try:
            model, _meshes = self._find_model_armature(context, Trace(False))
            return model
        except Exception:  # noqa: BLE001 - a panel must never raise
            return None

    def more_settings_lines(self, context):
        """What `More settings` says about `Step`: the detected value, and why.

        A method rather than inline code in `draw` so it can be exercised headlessly (`preset_ui_probe.py`) - the
        panel is the only place this runs, and the panel cannot be drawn without a window.
        """
        # The warning goes *here*, before the button is pressed, not only into the trace after: a stale module
        # writes its own trace header, so the panel is the one place that can tell the user before they run
        lines = []
        stale = stale_source()
        if stale:
            lines += wrap_note("Stale build: %s. Reload the extension or restart Blender first - this run would "
                               "measure the old code." % stale)
        model = self._draw_model(context)
        if model is None:
            return lines + ["Step: Full (no riggable armature in this scene)"]
        try:
            step, why = detect_step(model)
        except Exception as error:  # noqa: BLE001
            return lines + ["Step: could not be detected (%s)" % error]
        return lines + wrap_note("Step: %s - %s" % (step.capitalize(), why))

    def draw(self, context):
        """Mode, preset, and then either one row per group or one row per family.

        `Simple` shows the five group rows; `Advanced` shows every physics family the model has - median 13 of
        them across his 241 rigs, 23 at most - grouped under the same headings, so it reads as the simple view
        expanded rather than a second system. The two families his own rigs never vary are listed and locked, so
        nobody has to wonder where the ears went. Everything that decides what a rig *is* rather than what it
        contains (`Step`, `Resample`, `Overwrite_Typed`, the rules file) stays folded away under `More settings`,
        which is deliberately *not* called "Advanced" - that word belongs to the per-family mode above it.
        """
        layout = self.layout
        column = layout.column()
        # The panel, not a property `update` callback, is what keeps the rows and the preset in step: `update`
        # does not fire for every way the value can change, and one on a `PropertyGroup` item is handed the item
        # rather than the operator. `draw` runs on every redraw while the dialog is open, so both fixes live
        # here - fill the rows from the preset if it changed, then adopt any group row the user moved.
        sync_preset(self, context)
        if self.Mode != 'ADVANCED':
            # adopt first (a group row the user moved wins), then mirror the family rows back into the group
            # rows - unconditionally, so a family edited in Advanced cannot leave a Simple row showing a stale
            # answer. `sync_group_fields` only writes the group properties and the snapshot, never the families,
            # so this cannot flatten a per-family choice (code review, handoff 9ai).
            adopt_group_edits(self)
            sync_group_fields(self)
        top = column.row(align=True)
        top.prop(self, "Mode", expand=True)
        preset = column.row(align=True)
        preset.prop(self, "Preset", text="")
        folder = preset.row(align=True)
        # `EXEC_DEFAULT`: this button carries its action, so it must run it rather than open the operator's own
        # dialog - which it would do now that `RigifyChainRules` has an `invoke` for the panel button's sake
        folder.operator_context = 'EXEC_DEFAULT'
        props = folder.operator(RigifyChainRules.bl_idname, text="", icon='FILE_FOLDER')
        props.Action = 'SHOW'
        preset_search = column.row(align=True)
        preset_search.prop(self, "Preset_Filter", text="", icon='VIEWZOOM')
        reload_row = preset_search.row(align=True)
        reload_row.operator(RigifyChainPresetReload.bl_idname, text="", icon='FILE_REFRESH')
        # One short line at most. The detail (`preset 1004_00 (...), N families different from the table`) is in
        # the trace, where it can be read at leisure; a dialog is not the place for a sentence.
        if self.Forced_Advanced:
            column.label(text="Multiple rig types per group (%s)" % self.Forced_Advanced)
        elif preset_mismatch(self):
            column.label(text="%d famil%s changed" % (preset_mismatch(self),
                                                      "y" if preset_mismatch(self) == 1 else "ies"))
        save = column.row()
        save.operator(RigifyChainPresetSave.bl_idname, icon='FILE_TICK').Data = self._family_data()
        # The style row (chains-38). It sits here rather than under `More settings` for a reason the doctrine
        # above does not cover: `More settings` is for what decides what the rig *is*, and a style decides none
        # of it - its whole effect is visible in the viewport, so hiding it would hide the feature. The folder
        # button is the same one the presets use one row up: one file per style, editable by hand.
        style_row = column.row(align=True)
        style_row.prop(self, "Style", text="")
        style_folder = style_row.row(align=True)
        style_folder.operator(RigifyChainStyleFolder.bl_idname, text="", icon='FILE_FOLDER').Action = 'SHOW'
        # the X-ray row sits with the style, because that is what it is about: how the rig is drawn. It is the
        # whole rig (Blender has no scoped X-ray - see `execute`), and it is here rather than only in Blender's
        # Object Properties panel because that one is overwritten by the style on the next run.
        column.prop(self, "Xray")
        style_save = column.row()
        style_save.operator(RigifyChainStyleSave.bl_idname, icon='FILE_TICK')
        column.separator()
        if self.Mode == 'ADVANCED':
            current = None
            for item in self.Families:
                if item.group != current:
                    current = item.group
                    column.label(text=current)
                row = column.row(align=True)
                row.label(text="  " + item.family)
                if item.fixed:
                    row.label(text="%s (fixed)" % item.fixed, icon='LOCKED')
                else:
                    right = row.row(align=True)
                    right.prop(item, "mode", text="")
                    # `default` = the rules table's own answer for this family; `override` = chosen here or by
                    # the preset. Drawn on every row, one word or the other, so the dropdowns line up.
                    right.label(text="default" if item.mode == item.default_mode else "override")
        else:
            box = column.box()
            box.label(text="What each group gets:")
            for group in SIMPLE_GROUPS:
                if not any(item.group == group and not item.fixed for item in self.Families):
                    continue
                box.prop(self, "%s_Mode" % group)
            locked = [(item.family, item.fixed) for item in self.Families if item.fixed]
            if locked:
                box.label(text="always: %s" % ", ".join("%s = %s" % (family, rig_type)
                                                        for family, rig_type in locked), icon='LOCKED')
            if self.Forced_Advanced:
                box.label(text="Advanced shows the per-family detail", icon='INFO')
        column.separator()
        column.prop(self, "Chain_Layers")
        column.prop(self, "More_Options")
        if self.More_Options:
            more = column.box()
            for index, line in enumerate(self.more_settings_lines(context)):
                more.label(text=line, icon='INFO' if index == 0 else 'BLANK1')
            more.prop(self, "Step")
            more.prop(self, "Resample")
            more.prop(self, "Overwrite_Typed")
            more.prop(self, "Debug")
            more.separator()
            more.prop(self, "Rules_File")
            more.operator(RigifyChainTraceSave.bl_idname, icon='FILE_TICK')

    def _family_data(self):
        """The current family choices as `family=mode;...`, for the save-as-preset button to carry."""
        return ";".join("%s=%s" % (item.family, item.mode) for item in self.Families if not item.fixed)

    @staticmethod
    def _group_model_bones(model, trace, visible=True):
        """Put the model's own bones into one collection so they can be hidden as a set.

        The MMD armature arrives with 3 collections and 438 bones in none of them, and Blender draws a bone
        that is in no collection regardless of which collections are switched on - so the only way to calm that
        armature down is to give the bones a collection to hide. Additive and reversible: nothing is unassigned,
        and the collection is visible, so what is drawn does not change until someone switches it off.
        """
        collection = model.data.collections_all.get(MODEL_COLLECTION_NAME)
        if collection is None:
            collection = model.data.collections.new(MODEL_COLLECTION_NAME)
        # hidden to match the chain collections: the point of the collection is that the model's bones can be
        # switched off, and with `Chain Layers = Nothing` the whole rig should open calm
        show_collection(collection, visible=visible)
        already = set()
        for other in model.data.collections_all:
            if other is collection:
                continue
            try:
                already.update(other.bones.keys())
            except (AttributeError, TypeError):
                continue
        grouped = 0
        for bone in model.data.bones:
            if bone.name in already:
                continue
            try:
                collection.assign(bone)
                grouped += 1
            except (AttributeError, TypeError, RuntimeError) as error:
                trace.info("could not add %s to %s: %s" % (bone.name, MODEL_COLLECTION_NAME, error))
                break
        return grouped

    # --------------------------------------------------------------------------------------------------

    def _apply_display(self, rig, chain_names, trace):
        """The display half of a run: the style, then the X-ray row. Returns `(ok, detail, x_ray)`.

        Split out of `execute` so a run that has nothing left to *rig* can still do this - which is what makes
        `Step = Wire only` on an already-wired model useful instead of an error (`_finish_display_only`). A style
        is display only and idempotent: applying it twice changes nothing, which is the promise the README makes.
        """
        style_entry = read_style(self.Style)
        if style_entry is None and self.Style != STYLE_NONE:
            trace.info("style '%s' is not in %s, so the rig keeps rigify's own display"
                       % (self.Style, style_dir()))
        detail = "no style chosen, so the rig keeps rigify's own display"
        ok = True
        if style_entry is not None:
            try:
                applied = apply_style(rig, style_entry, chain_names=chain_names, trace=trace)
                detail = ("%s: %d control(s) coloured, %d of them chain families, %d left as rigify made them, "
                          "%s" % (style_entry.get("key"), applied["coloured"], applied["chain_coloured"],
                                  applied["left_as_rigify_made_it"], " ".join(applied["flags"][:4])))
                for warning in applied["warnings"]:
                    trace.info("style warning: %s" % warning)
                trace.info("display style: %s" % describe_style(style_entry))
                # a style that was chosen and then could not be applied is a failure, not a silent success: this
                # check used to pass whenever `apply_style` did not raise, while per-bone failures are collected as
                # warnings *inside* it (beta code review, handoff 9aq)
                if applied["warnings"] and not applied["coloured"]:
                    ok = False
                    detail += " | nothing could be applied: %s" % applied["warnings"][0]
                elif not applied["coloured"] and (style_entry.get("colors") or []):
                    ok = False
                    detail += (" | the style has %d colour rule(s) and none of them matched a bone"
                               % len(style_entry.get("colors") or []))
            except Exception as error:  # noqa: BLE001 - a rig that is right but plain beats no rig
                import traceback
                ok = False
                detail = "%s" % error
                trace.info(traceback.format_exc())

        # The one display flag a style cannot scope: X-ray. `show_in_front` is an **Object** property (no `Bone`,
        # `PoseBone` or `BoneCollection` has one - read off the 5.2 RNA) and the armature's draw code reads a
        # custom-shape object only for its *mesh data*, never for the widget's own `show_in_front`, so the rig is
        # drawn through the model as a whole or not at all. The row exists because a manual flip in Blender's
        # Object Properties > Viewport Display > In Front does not survive the next run, which re-applies the style.
        if self.Xray in ('ON', 'OFF'):
            wanted = self.Xray == 'ON'
            before = bool(rig.show_in_front)
            rig.show_in_front = wanted
            turned = ("on: the rig is drawn through the model" if wanted
                      else "off: the model hides the rig again")
            detail += " | x-ray %s (was %s, %s)" % (
                turned, "on" if before else "off",
                "the style's value overridden" if style_entry is not None else "rigify's own value")
            trace.info("x-ray %s - the whole rig or nothing: Blender has no per-bone or per-family X-ray, and %s"
                       % (turned, "the style also says %s" % wanted if style_entry is not None
                          else "no style is in use"))
        return ok, detail, bool(rig.show_in_front)

    def _already_wired(self, model):
        """`(wired bone names, rig)` when this model's chains are already released and following its own rig.

        This is the state a *finished* run leaves behind, and it is what a second run must not walk into blind:
        `_wire_model_to_chains` releases every chain bone from its model parent on purpose (so the chain follows
        the rig instead of MMR's hierarchy), and chain discovery works by walking that hierarchy. So on the next
        run the released bones look like single-bone chains with no model parent - below `min_bones`, so dropped -
        and the survivors are attached by the nearest-bone fallback and fail the attachment check. Measured on
        `meisho_doto_dot_o_lantern` with `double_run_check.py`: run 1 finds 44 chains and fails nothing, run 2 finds
        **33 chains and fails 17 attachment checks**. The handoff had recorded those numbers already (9aj), and the
        panel then recommended repeating it, because `detect_step` answered `Full` for a wired model with the
        reasoning "a Full run redoes it from scratch". It does not (beta code review, handoff 9aq).
        """
        names = [bone.name for bone in model.data.bones
                 if bone.name.startswith(("Sp_", "Tp_", "Ear")) and not bone.name.endswith("_Handle")]
        rig = self._find_rig_for_chains(model, names, Trace(False))
        if rig is None:
            return None, None
        wired = [pose_bone.name for pose_bone in model.pose.bones
                 if any(constraint.name.startswith("UMA_chain") for constraint in pose_bone.constraints)]
        return (wired or None), rig

    def _finish_display_only(self, model, rig, trace, started, mark, chain_names):
        """End a run that had nothing left to rig, having done the display and said so."""
        ok, detail, x_ray = self._apply_display(rig, chain_names, trace)
        trace.check("the rig carries this run's display style", ok, detail)
        mark("the display")
        trace.info("timings: total %.1fs (nothing was generated or wired)" % (time.time() - started))
        trace.info("rig display: x-ray %s" % ("on (drawn through the model)" if x_ray else "off"))
        text_name = trace.save()
        self.report({'INFO'}, "[%s] %s is already rigged - nothing to generate or attach, so the display was "
                              "re-applied. Use Ctrl+Z or re-import the model to rig it again from scratch. "
                              "Trace: %s" % (BUILD_TAG, model.name, text_name))
        return {'FINISHED'}

    # --------------------------------------------------------------------------------------------------

    def execute(self, context):
        trace = Trace(self.Debug)
        trace.step("Pumpkin rig setup %s, step=%s" % (BUILD_TAG, self.Step))
        started = time.time()
        # Where the time went. The user's report - *"the second character did finish generating the rig properly,
        # but it seemed to take much longer"* - could not be answered from the trace at all: it carried one total
        # (`done in 48.2s`) and nothing else, so "MMR is slower with two characters in the file" and "something in
        # this operator scales with the scene" were both consistent with it. These marks cost nothing and make the
        # next slow run explain itself. `MMR_SECONDS` is filled in by the wrapper, which is where rigify's own
        # generation happens, and `INSERT_SECONDS` separates our metarig work from MMR's.
        marks = []
        MMR_SECONDS = {}

        def mark(label):
            marks.append((label, time.time() - started))
        
        # Answer this before anything is measured: a stale module means every number below describes code that is
        # no longer on disk, which is the one thing that makes an entire run worthless (handoff 9ad).
        stale = stale_source()
        if stale:
            trace.info("%s - reload the extension (or restart Blender) and run again" % stale)
        trace.check("the build in memory is the file on disk", stale is None,
                    stale if stale else "%s loaded from %s, unchanged since" % (BUILD_TAG, SOURCE_PATH))

        model, meshes = self._find_model_armature(context, trace)
        if model is None:
            self.report({'ERROR'}, "[%s] No MMD armature found to rig (no armature is skinned to a mesh)." % BUILD_TAG)
            return {'CANCELLED'}
        self._model, self._meshes = model, [o for o in context.scene.objects
                                            if o.type == 'MESH' and o.name in meshes]
        trace.check("model armature", True, "%s (%d bones), meshes: %s"
                    % (model.name, len(model.data.bones), ", ".join(meshes) or "none"))

        # ---- a model that is already rigged cannot be rigged again ---------------------------------------
        # Refused here, before anything is touched, because a `Full` run on this state does not fail loudly - it
        # *succeeds* with a worse rig: measured, 33 chains instead of 44 and 17 failed attachment checks, with the
        # first rig still in the file and the model's old constraints still on it (`_already_wired` has the detail
        # and the numbers). The panel used to recommend exactly that, so the guard is what makes pressing the
        # button twice safe. `Wire only` is allowed through: it has nothing to wire, and `execute` turns that into
        # the display-only pass instead of an error.
        wired_bones, wired_rig = self._already_wired(model)
        if wired_bones and self.Step == 'FULL':
            trace.check("this model can still be rigged", False,
                        "%s is already rigged: %d of its chain bones are released and follow %s, and a Full run "
                        "would rebuild only part of the rig (measured: 33 chains instead of 44 and 17 failed "
                        "attachment checks). Use Step = Wire only to re-apply the wiring and the display, or "
                        "Ctrl+Z / re-import the model to rig it again from scratch"
                        % (model.name, len(wired_bones), wired_rig.name))
            text_name = trace.save()
            self.report({'ERROR'}, "[%s] %s is already rigged (its chain bones follow %s). Nothing was changed: a "
                                   "Full run here would produce a worse rig. Use Step = 'Wire only' to re-apply "
                                   "the wiring and the display, or Ctrl+Z / re-import the model. Trace: %s"
                        % (BUILD_TAG, model.name, wired_rig.name, text_name))
            return {'CANCELLED'}
        if wired_bones:
            trace.info("%s is already rigged: %d chain bone(s) follow %s, so this run has nothing to generate or "
                       "attach and will re-apply the display only" % (model.name, len(wired_bones), wired_rig.name))

        rules, options, source = load_rules(bpy.path.abspath(self.Rules_File) if self.Rules_File else None)
        # `load_rules` hands back the *cached* options dict, and the two lines below write into it - so without
        # this copy they were editing module-level state for the rest of the Blender session. Measured
        # consequence (code review, handoff 9ai): run once with `Resample = Even out joints`, set it back to
        # `From rules file` (whose whole job is to read `options["resample"]`, i.e. to write nothing), run again
        # - and the second run resamples `even` while the field and the trace say otherwise.
        options = dict(options)
        # a rules file from an older build still holds the presets inside it: move them to their own files once,
        # here rather than in the panel (this writes files)
        ensure_preset_files(trace)
        # and the display styles: the shipped `coolchou.json` is written here on a fresh install (never in
        # `draw`, which runs on every redraw and must not write files) - the same shape as the presets, one
        # file per style, and the operator works without it (a missing style leaves rigify's own display)
        ensure_style_files(trace)
        if self.Resample in {'EVEN', 'LEAF'}:
            options["resample"] = self.Resample.lower()
        elif self.Resample == 'KEEP':
            options["resample"] = "keep"
        resample = str(options.get("resample", "keep")).lower()
        trace.info("chain joints: %s (resample %s)" % (source, resample))
        active_rules = [rule for rule in rules if not rule.get("skip") and rule.get("type")]
        trace.check("rules loaded", bool(active_rules), "%d rules from %s" % (len(active_rules), source))

        # ---- what each family gets (handoff 9y). The preset fills the fields; the fields decide. A script that
        # calls this operator directly has an empty family list, so it is filled here too, from the same preset.
        identity, identity_bone = model_identity(model)
        trace.info("model identity: %s%s" % (identity or "no Chara_ bone",
                                            " (%s)" % identity_bone if identity_bone else ""))
        if not self.Preset_Chosen and self.Preset in (None, PRESET_CUSTOM, PRESET_DEFAULT):
            # reached only when nobody has picked a preset - a script calling this operator directly, since the
            # panel path goes through `invoke` where the dialog can show the detected value
            presets = load_profiles(
                bpy.path.abspath(self.Rules_File) if self.Rules_File else None)
            key, why = detect_preset(presets, model, trace)
            trace.info("autodetect: %s" % why)
            if key:
                self.Preset = key
                self.Preset_Chosen = True
        self._refresh_families(context, trace)
        chosen, table_modes = self._chosen_modes(trace)
        fixed = dict(options.get("fixed_families") or {})
        trace.info("mode: %s, preset: %s" % (self.Mode, self.Preset))
        if self.Forced_Advanced:
            trace.info("Advanced was selected because this preset gives one group more than one answer: %s"
                       % self.Forced_Advanced)
        trace.info("fixed: %s" % (", ".join("%s=%s" % (family, rig_type)
                                            for family, rig_type in sorted(fixed.items())) or "none"))
        unchanged = [family for family, mode in chosen.items() if mode == table_modes.get(family)]
        trace.info("families left at the rules table's own answer: %d of %d"
                   % (len(unchanged), len(chosen)))
        rules, family_report = resolve_rule_modes(model, rules, chosen, table_modes, fixed,
                                                 options.get("never_rig") or (), trace,
                                                 options.get("unmatched_families") or {})
        # every family the run decided, named, and where the answer came from: this is the line to read when a
        # rig does not look the way someone expected
        by_group = {}
        for group, family, rig_type, where, pattern in family_report:
            by_group.setdefault(group, []).append((family, rig_type, where, pattern))
        for group in list(GROUP_ORDER) + ["Ears", "-"]:
            entries = by_group.get(group)
            if not entries:
                continue
            typed_here = [entry for entry in entries if entry[1]]
            # a family the table splits (`Sp_Ch_Ribbon0` is a one-bone stub in front and two side chains) appears
            # more than once: name the rule, or the line reads as a contradiction
            repeated = {entry[0] for entry in typed_here
                        if sum(1 for other in typed_here if other[0] == entry[0]) > 1}
            trace.info("%-8s %2d famil%s, %d typed: %s"
                       % (group, len(entries), "y" if len(entries) == 1 else "ies", len(typed_here),
                          ", ".join("%s%s=%s (%s)"
                                    % (family, ("[%s]" % pattern) if family in repeated else "",
                                       rig_type, where)
                                    for family, rig_type, where, pattern in typed_here) or "none"))

        chains = self._collect_chains(model, rules, trace,
                                      weights=self._bone_weights(self._meshes, trace) if self._meshes else None,
                                      require_weights=bool(options.get("require_weights", True)))
        trace.check("chains found in the model", bool(chains), "%d chains" % len(chains))
        mark("find the model, rules, families and chains")
        self._report_coverage(model, rules, chains, trace)
        if not chains and wired_bones:
            # The second way this state is reached: `Wire only` on a model that is already wired. Its chains were
            # released by the run that wired them (they are parentless by design, so they follow the rig instead of
            # MMR's hierarchy), so there is nothing left for chain discovery to walk - and the honest answer is not
            # "no chains matched the rules file" but "there is nothing to wire". What a re-run *can* still usefully
            # do is the display, which is the one part of a run that touches nothing: so that is what happens, and
            # the user gets a styled rig instead of an error for pressing the button twice. See `_already_wired`.
            trace.info("%s is already wired to %s: %d of its chain bones follow ORG copies, and nothing is left "
                       "to generate or attach" % (model.name, wired_rig.name, len(wired_bones)))
            trace.step("nothing to wire: re-applying the display only")
            return self._finish_display_only(model, wired_rig, trace, started, mark,
                                             [name for name in wired_bones])
        if not chains:
            self.report({'ERROR'}, "[%s] No chains matched the rules file - nothing to add." % BUILD_TAG)
            return {'CANCELLED'}
        geometry = self._chain_geometry(model, chains, trace)

        added = typed = wired = 0
        collection_names = []
        rig = None
        # The name of the rig this run generates, so the wiring below cannot pick another character's. Taken as
        # a diff around the whole FULL step rather than inside the wrapper: measured in a two-character file,
        # the rig object already existed when the wrapped `generate_rig` was entered, so the wrapper's own diff
        # found nothing (handoff 9af). `Wire only` has no generation at all, and then the rig is picked by which
        # one carries these chains (`_find_rig_for_chains`).
        generated_rig_name = None
        armatures_before = {obj.name for obj in bpy.data.objects if obj.type == 'ARMATURE'}

        if self.Step == 'FULL':
            mmr = getattr(model, "mmr", None)
            if mmr is None:
                self.report({'ERROR'}, "MikuMikuRig is not installed, so there is no controller to add "
                                       "to. Use the rules with MMR installed, or Step = Wire only. [%s]"
                            % BUILD_TAG)
                return {'CANCELLED'}
            # MMR appends its metarig template only when no object of that name exists (`Controller.py:88`), and
            # rigify consumes the template when a run finishes (`:1034-1037`) - so a finished run leaves nothing
            # behind, which is why running this twice in a row is fine. What a run that *did not finish* leaves
            # behind is the template itself, and measured (`mmr_second_pass_check.py`, mode `leftover`): handing
            # an existing template to MMR fails inside MMR with `AttributeError: 'NoneType' object has no
            # attribute 'name'` while it renames a rig it cannot find, surfacing here as an opaque "Rigify chains
            # failed". Our step always generates from scratch, so a leftover is not something it can use - refuse
            # before touching the model, name the object, and say both ways out.
            leftover = bpy.data.objects.get("MMR_Rig_relative")
            if leftover is not None:
                message = ("This file already contains MMR's metarig template 'MMR_Rig_relative', which is "
                           "normally left behind by an MMR run that did not finish. MMR reuses an existing "
                           "template instead of appending a fresh one and the run then fails inside MMR, so this "
                           "operator stops here rather than failing halfway. Delete that object (or re-import "
                           "the model) and run this again. Nothing was changed. [%s]" % BUILD_TAG)
                trace.check("no leftover MMR metarig template in the file", False,
                            "'MMR_Rig_relative' is already in this file")
                trace.info(message)
                trace.save()
                self.report({'ERROR'}, message)
                return {'CANCELLED'}
            trace.check("no leftover MMR metarig template in the file", True, "nothing to reuse")
            if "rigify" not in context.preferences.addons:
                try:
                    bpy.ops.preferences.addon_enable(module="rigify")
                except RuntimeError as error:
                    self.report({'ERROR'}, "[%s] Could not enable rigify (%s)." % (BUILD_TAG, error))
                    return {'CANCELLED'}

            # state the wrapper needs; nothing is shared through globals
            self._model, self._chains, self._options, self._geometry = model, chains, options, geometry

            saved = {key: getattr(mmr, key, None)
                     for key in ("Only_meta_bones_are_generated", "Hide_mmd_skeleton")}
            saved_active = context.view_layer.objects.active
            visibility = None
            rigify_module = original = wrapper = None
            state = {"done": False}
            try:
                # MMR appends its metarig template, so the object it will hand to rigify can be named
                # ahead of time; the wrapper only proceeds when it sees that object
                rigify_module, original, wrapper, state = self._generation_wrapper(trace)
                for obj in context.selected_objects:
                    obj.select_set(False)
                # MMR's own operator polls for an active armature, and the previous run hid the MMD armature,
                # so a second FULL run in the same session has to show it again before handing over.
                visibility = self._ensure_editable_active(model, trace)
                mmr.Only_meta_bones_are_generated = False

                # rigify's generate operator looks the function up on the module at call time
                rigify_module.generate_rig = wrapper
                trace.info("rigify.generate_rig wrapped for the duration of this run")

                _mmr_started = time.time()
                result = bpy.ops.uma.mmr_rig()
                # the whole MMR pipeline, which is where rigify's generation happens; `INSERT_SECONDS` is how much
                # of it was this operator's own work on the metarig
                MMR_SECONDS["mmr"] = time.time() - _mmr_started
                MMR_SECONDS["insert"] = state.get("insert_seconds")
                trace.check("uma.mmr_rig returned FINISHED", result == {'FINISHED'}, str(result))
                # rigify's Generate operator catches exceptions from generate_rig and still reports
                # FINISHED, so the insertion's own record decides whether wiring may run at all
                if not state.get("done") or state.get("error"):
                    reason = state.get("error") or "rigify never called generate_rig"
                    trace.check("chains were inserted into the metarig", False, reason)
                    text_name = trace.save()
                    self.report({'ERROR'}, "[%s] The chains were not added to the metarig (%s), so nothing was "
                                           "wired. See the %s text block." % (BUILD_TAG, reason, text_name))
                    return {'CANCELLED'}
                trace.check("chains were inserted into the metarig", True,
                            "%d chains, %d bones added" % (state.get("chains", 0),
                                                           state.get("added", 0)))
                mark("generate the rig (MMR + rigify)")
            except Exception as error:  # noqa: BLE001 - everything is restored in the finally block
                import traceback
                trace.check("run completed", False, "%s" % error)
                trace.info(traceback.format_exc())
                trace.save()
                self.report({'ERROR'}, "[%s] Rigify chains failed: %s (see the %s text block)"
                            % (BUILD_TAG, error, TRACE_TEXT_NAME))
                return {'CANCELLED'}
            finally:
                if rigify_module is not None and original is not None:
                    rigify_module.generate_rig = original
                for key, value in saved.items():
                    if value is not None:
                        try:
                            setattr(mmr, key, value)
                        except (AttributeError, TypeError):
                            pass
                if saved_active is not None and saved_active.name in bpy.data.objects:
                    try:
                        for obj in context.selected_objects:
                            obj.select_set(False)
                        saved_active.select_set(True)
                        context.view_layer.objects.active = saved_active
                    except RuntimeError:
                        pass
                # MMR hides the MMD skeleton as its last step; put it back that way if it was hidden when
                # this run started, so a FULL run still ends with the scene MMR intended to leave behind.
                if visibility is not None:
                    self._restore_visibility(model, visibility)
            fresh = [obj.name for obj in bpy.data.objects
                     if obj.type == 'ARMATURE' and obj.name not in armatures_before and obj is not model
                     and not obj.name.startswith("MMR-")]
            rigify_made = [name for name in fresh if name.startswith("RIG-")]
            # The rig object, not its name: MMR renames it after generation (`RIG-MMR-...` -> `RIG-...`), so a name
            # recorded inside the wrapper can be stale a moment later - measured on the user's two-model file,
            # where the stamp silently did nothing because of exactly that (handoff 9aj).
            rig_object = None
            try:
                stored = state.get("rig_object")
                if stored is not None and stored.name in bpy.data.objects:
                    rig_object = stored
            except ReferenceError:      # the object was deleted during the run
                rig_object = None
            if rig_object is None:
                for name in (rigify_made or fresh):
                    candidate = bpy.data.objects.get(name)
                    if candidate is not None and candidate.type == 'ARMATURE' and candidate is not model:
                        rig_object = candidate
                        break
            generated_rig_name = rig_object.name if rig_object is not None else None
            # Write down which model this rig was generated for, on both sides, and keep it in the .blend: it is
            # what tells two characters in one file apart later, because chain bones share their names across
            # every character and nothing about a rig's *shape* can (handoff 9aj, `_rig_belongs_to`).
            if rig_object is not None:
                rig_object["uma_rig_for"] = model.name
                model["uma_rig"] = rig_object.name
                trace.info("rig ownership recorded: %s was generated for %s (kept in the .blend, and how a later "
                           "run tells this model's rig from another character's)" % (rig_object.name, model.name))
            trace.info("this run's generation produced %s%s"
                       % (generated_rig_name or "no new armature",
                          " (as %s)" % state["rig"] if state.get("rig") and state["rig"] != generated_rig_name
                          else "" if not fresh else " (%s in the file now)" % ", ".join(fresh[:3])))

        # ---- wire the model to the chains, and verify -----------------------------------------------
        trace.step("wire the model to the chains")
        rig = self._find_generated_rig(model, preferred=generated_rig_name, chains=chains, trace=trace)
        if rig is None:
            trace.check("generated rig found", False, "no armature with ORG-/DEF-/MCH- bones")
            trace.save()
            self.report({'ERROR'}, "[%s] No generated rig found to wire the chains to (see the %s text block)."
                        % (BUILD_TAG, TRACE_TEXT_NAME))
            return {'CANCELLED'}
        trace.check("generated rig found", True, "%s (%d bones)" % (rig.name, len(rig.data.bones)))

        orgs = [names[0] for _rule, names in chains if ("ORG-" + names[0]) in rig.pose.bones]
        trace.check("chains were generated", len(orgs) == len(chains),
                    "%d of %d chain roots have an ORG copy" % (len(orgs), len(chains)))
        try:
            wired = self._wire_model_to_chains(
                model, rig, chains, trace,
                release_parents=bool(options.get("release_parents", True)),
                resample=resample,
                chain_deform=str(options.get("chain_deform", "rigid")).lower())
        except Exception as error:  # noqa: BLE001 - a half-wired model must not be left silently
            import traceback
            trace.check("wiring the model to the chains", False, "%s" % error)
            trace.info(traceback.format_exc())
            text_name = trace.save()
            self.report({'ERROR'}, "[%s] Wiring the model to the chains failed: %s (see the %s text block)"
                        % (BUILD_TAG, error, text_name))
            return {'CANCELLED'}
        mark("wire the model to the chains")

        wanted = sorted({rule.get("collection") for rule, _ in chains if rule.get("collection")})
        # the metarig was generated before these collections existed on the rig, so the row numbers are
        # settled here, against the rigify rows actually in use - see `assign_chain_ui_rows`
        rows = assign_chain_ui_rows(rig, wanted, trace)
        present = []
        # `Nothing` hides everything this operator creates, so the rig opens on MMR's body controls and the
        # animator turns on what a shot needs; the collections stay *selectable*, so a click in the armature's
        # collection list brings any of them back.
        controls_on = self.Chain_Layers in ('CONTROLS', 'ALL')
        layers_on = self.Chain_Layers == 'ALL'
        hidden_layers, hidden_families = [], []
        for name in wanted:
            collection = rig.data.collections_all.get(name)
            if collection is not None:
                present.append(name)
                show_collection(collection, visible=controls_on)
                if not controls_on:
                    hidden_families.append(collection.name)
                for suffix in (" (FK)", " (Tweak)"):
                    sibling = rig.data.collections_all.get(name + suffix)
                    if sibling is not None:
                        show_collection(sibling, visible=layers_on)
                        if not layers_on:
                            hidden_layers.append(sibling.name)
        if hidden_families:
            trace.info("chain collections hidden so the rig opens on the body controls: %s"
                       % ", ".join(hidden_families))
        if hidden_layers:
            trace.info("sub-layers switched off: %s" % ", ".join(hidden_layers))
        if hidden_families or hidden_layers:
            trace.info("Chain Layers = Controls only / Everything brings them back; they stay selectable "
                       "either way, so a click in the armature's collection list restores any of them")
        if rows:
            trace.info("one UI row per family: %s"
                       % ", ".join("%s=%d" % (family, rows[family][0]) for family in sorted(rows)))
        trace.check("chain collections exist on the generated rig", len(present) == len(wanted),
                    "present: %s" % (", ".join(present) or "none"))
        trace.check("each chain family has a UI row of its own", len(rows) == len(present),
                    "%d row(s) for %d family/families" % (len(rows), len(present)))

        # the deformation must be unchanged at rest: the model's chain bones should not have moved
        # (the depsgraph has to be evaluated first - pose_bone.matrix is stale until then, which is how an
        # earlier version of this check passed regardless of what the wiring did)
        context.view_layer.update()
        displaced = []
        for _rule, names in chains:
            for name in names:
                model_bone = model.data.bones.get(name)
                pose_bone = model.pose.bones.get(name)
                if model_bone is None or pose_bone is None:
                    continue
                moved = ((model.matrix_world @ pose_bone.matrix.translation)
                         - (model.matrix_world @ model_bone.head_local)).length
                if moved > 1e-3:
                    displaced.append((name, round(moved, 5)))
        trace.check("the model is undisturbed at rest", not displaced,
                    "%d bones moved, worst %s" % (len(displaced), displaced[:3] if displaced else ""))

        # Group the model's own bones so they can be switched off as a set. MMR leaves the MMD armature hidden,
        # but a bone in **no** collection is drawn whenever that object is shown, whatever the collection
        # toggles say - so unhiding it to check the wiring currently means 439 bones with no way to hide them
        # in groups. Purely additive: a visible collection changes nothing about what is drawn.
        if self.Step == 'FULL':
            grouped = self._group_model_bones(model, trace, visible=(self.Chain_Layers != 'NONE'))
            if grouped:
                trace.info("model bones grouped into '%s' (%d of them had no collection): %s"
                           % (MODEL_COLLECTION_NAME, grouped,
                              "visible now, switch it off in the armature's collection list"))

        # ---- how the rig is drawn: the display style, last, because it is the only thing that touches nothing --
        style_applied, style_detail, final_xray = self._apply_display(
            rig, [name for _rule, names in chains for name in names], trace)
        mark("collections, UI rows and the style")
        # ---- where the time went, and what the scene looked like ------------------------------------------
        # The two facts a "this run was slow" report needs. `timings:` is this operator's own phases; the MMR
        # figure inside it is the whole `uma.mmr_rig` call, minus this operator's own metarig work, so a slow run
        # says by itself whether it was our code or somebody else's. `scene:` is the context, because the number
        # of *other* armatures, rigs and meshes in the file is the first thing that separates "this model is
        # heavy" from "this file is heavy" (handoff 9ao).
        other_rigs = [obj.name for obj in bpy.context.scene.objects
                      if obj.type == 'ARMATURE' and obj is not model and obj is not rig
                      and any(bone.name.startswith(("ORG-", "DEF-", "MCH-")) for bone in obj.data.bones)]
        other_armatures = [obj.name for obj in bpy.context.scene.objects
                           if obj.type == 'ARMATURE' and obj is not model and obj is not rig]
        scene_meshes = [obj for obj in bpy.context.scene.objects if obj.type == 'MESH']
        phases = []
        previous = 0.0
        for label, when in marks:
            phases.append("%s %.1fs" % (label, when - previous))
            previous = when
        phases.append("everything else %.1fs" % max(0.0, (time.time() - started) - previous))
        mmr_seconds = MMR_SECONDS.get("mmr")
        insert_seconds = MMR_SECONDS.get("insert")
        trace.info("timings: %s | total %.1fs" % (" | ".join(phases), time.time() - started))
        trace.info("timings: the MMR call itself %.1fs%s"
                   % (mmr_seconds if mmr_seconds is not None else -1.0,
                      ", of which this operator's metarig work %.1fs" % insert_seconds
                      if insert_seconds is not None else " (the wrapper never ran)"))
        trace.info("scene: this model %d bones and %d mesh(es), and %d other armature(s) (%s), %d other "
                   "generated rig(s) (%s), %d mesh(es) in the file in total"
                   % (len(model.data.bones), len(meshes or []),
                      len(other_armatures), ", ".join(other_armatures[:4]) or "none",
                      len(other_rigs), ", ".join(other_rigs[:4]) or "none", len(scene_meshes)))
        trace.check("the rig carries this run's display style", style_applied, style_detail)
        # `style_detail` rather than the style entry: `read_style` and the apply both live in `_apply_display` now,
        # and this line referred to a local that no longer exists there - which the verification run after the
        # refactor caught immediately (NameError, handoff 9aq)
        trace.info("rig display: x-ray %s | %s"
                   % ("on (drawn through the model)" if final_xray else "off", style_detail))

        trace.step("done in %.1fs" % (time.time() - started))
        trace.info("chains %d, model bones wired %d, collections %s"
                   % (len(chains), wired, ", ".join(present)))
        text_name = trace.save()
        # The build tag is in this report on purpose: Blender keeps a loaded add-on module in memory, so a
        # file that was edited on disk only takes effect after the add-on is disabled/enabled or Blender
        # restarts. If a change "did nothing", this line says whether the change was even loaded.
        message = ("[%s] %d chains, %d model bones wired, collections: %s. Trace: %s"
                   % (BUILD_TAG, len(chains), wired, ", ".join(present), text_name))
        if trace.failures:
            self.report({'WARNING'}, message + ". Checks failed: " + "; ".join(trace.failures[:4]))
        else:
            self.report({'INFO'}, message)
        return {'FINISHED'}

class RigifyChainStyleSave(bpy.types.Operator):
    """Save how the rig in this scene is displayed as a style file"""

    bl_idname = "uma.rigify_chain_style"
    bl_label = "Save style from this rig"
    bl_description = ("Read the display settings of the generated rig in the scene - bone colours, widget sizes, "
                      "the armature's display flags - and write them into one small JSON file in the styles "
                      "folder, so they can be picked again. This is also how the shipped default was captured, "
                      "so the two cannot drift. A style is display only: nothing it saves can change what the "
                      "rig does")
    bl_options = {'REGISTER'}

    Name: StringProperty(
        name="Style name",
        description="The file is named after this. An existing style is never overwritten unless Overwrite is "
                    "ticked",
        default="",
    )
    Overwrite: BoolProperty(
        name="Overwrite an existing style",
        description="Replace a style that is already there. Off by default, because 'coolchou' is the shipped "
                    "default and the file name field starts empty rather than on top of it",
        default=False,
    )

    def invoke(self, context, event):
        if not self.Name:
            rig = find_generated_rig(context)
            self.Name = "my-style" if rig is None else ("%s-style" % rig.name.split("_arm")[0]).lower()
        return context.window_manager.invoke_props_dialog(self, width=380)

    def execute(self, context):
        rig = find_generated_rig(context)
        if rig is None:
            self.report({'ERROR'}, "No generated rig in this scene: run the Pumpkin Rig Setup first, or select "
                                   "the rigify rig whose display you want to save.")
            return {'CANCELLED'}
        if not self.Name:
            self.report({'ERROR'}, "A style needs a name: it becomes the file name in the styles folder.")
            return {'CANCELLED'}
        target = os.path.join(style_dir(), style_filename(self.Name) + ".json")
        if os.path.exists(target) and not self.Overwrite:
            self.report({'ERROR'}, "The name '%s' is saved as %s, which already exists. Tick Overwrite, or save "
                                   "under another name." % (self.Name, os.path.basename(target)))
            return {'CANCELLED'}
        try:
            entry = capture_style(rig, key=self.Name)
        except (AttributeError, TypeError, ValueError) as error:
            self.report({'ERROR'}, "Could not read the rig's display settings: %s" % error)
            return {'CANCELLED'}
        if isinstance(style_files().get(self.Name), dict) and not self.Overwrite:
            self.report({'ERROR'}, "A style called '%s' already exists. Tick Overwrite, or save under another "
                                   "name." % self.Name)
            return {'CANCELLED'}
        try:
            path = save_style(self.Name, entry)
        except (OSError, ValueError) as error:
            self.report({'ERROR'}, "Could not write the style: %s" % error)
            return {'CANCELLED'}
        counted = entry.get("captured") or {}
        self.report({'INFO'}, "Saved style '%s' from %s: %d control(s) in %d colour rule(s), %d left as rigify "
                              "made them - %s" % (self.Name, rig.name, counted.get("coloured", 0),
                                                  len(entry.get("colors") or []),
                                                  counted.get("uncoloured_controls", 0),
                                                  os.path.basename(path)))
        return {'FINISHED'}


class RigifyChainStyleFolder(bpy.types.Operator):
    """Open the styles folder, or read it again"""

    bl_idname = "uma.rigify_chain_styles"
    bl_label = "Rig Display Styles"
    bl_description = ("Open the folder holding one file per rig display style, or read it again after editing a "
                      "file by hand. Copy the shipped one to make your own")
    bl_options = {'REGISTER'}

    Action: EnumProperty(
        name="Action",
        items=[('SHOW', "Open The Styles Folder", "Open the folder holding one file per style, in the operating "
                                                  "system's file browser"),
               ('RELOAD', "Read It Again", "Read the folder again after a style was added or edited on disk")],
        default='SHOW',
    )

    def execute(self, context):
        ensure_style_files()
        if self.Action == 'RELOAD':
            forget_styles()
            styles = style_files(force=True)
            self.report({'INFO'}, "%d style(s) in %s" % (len(styles), style_dir()))
            return {'FINISHED'}
        try:
            bpy.ops.wm.path_open(filepath=style_dir())
        except RuntimeError as error:
            self.report({'ERROR'}, "Could not open %s: %s" % (style_dir(), error))
            return {'CANCELLED'}
        styles = style_files(force=True)
        self.report({'INFO'}, "%s - %d style file(s)" % (style_dir(), len(styles)))
        return {'FINISHED'}


class RigifyChainRules(bpy.types.Operator):
    """Open the folder the rules and presets live in, or write the rules file"""

    bl_idname = "uma.rigify_chain_rules"
    bl_label = "Rigify Chain Rules"
    bl_description = ("Open the folder the presets and rules live in, write the rules file with the built-in "
                      "defaults so it can be edited, or leave it alone")
    bl_options = {'REGISTER'}

    Action: EnumProperty(
        name="Action",
        items=[('SHOW', "Open The Presets Folder", "Open the folder holding one file per preset, in the "
                                                   "operating system's file browser. The rules table and the run "
                                                   "options are in rigify_chains.json one level up"),
               ('WRITE', "Write Defaults", "Overwrite the rules file with the built-in defaults (a backup of the "
                                           "file it replaces is written beside it)"),
               ('KEEP', "Write Only If Missing", "Leave an existing rules file alone")],
        default='SHOW',
    )

    def invoke(self, context, event):
        """Ask, instead of acting on a default nobody can see.

        The panel draws this operator with no properties at all, and `Action` used to default to `KEEP` with no
        `invoke` - so the button could only ever take the "write it if it is missing" branch, which on any normal
        install reports "Rules file already exists" and does nothing. The two actions that do something were
        unreachable from the UI (the dialog's folder button sets `Action='SHOW'` explicitly, so only that path
        worked). Now every press opens the three choices (beta code review, handoff 9aq).
        """
        return context.window_manager.invoke_props_dialog(self, width=340)

    def execute(self, context):
        path = config_path()
        if self.Action == 'SHOW':
            # the presets folder, not the file: a preset is a per-character preference with its own file now, and
            # that is what someone opening this wants to look at
            folder = preset_dir()
            write_preset_readme()
            try:
                bpy.ops.wm.path_open(filepath=folder)
            except RuntimeError as error:
                self.report({'ERROR'}, "Could not open %s: %s" % (folder, error))
                return {'CANCELLED'}
            self.report({'INFO'}, "%s - %d preset file(s); the rules table is %s"
                        % (folder, len(preset_files()), os.path.basename(path)))
            return {'FINISHED'}
        if self.Action == 'KEEP' and os.path.isfile(path):
            self.report({'INFO'}, "Rules file already exists: %s" % path)
            return {'FINISHED'}
        write_default_rules(path)
        self.report({'INFO'}, "Wrote %s" % path)
        return {'FINISHED'}
