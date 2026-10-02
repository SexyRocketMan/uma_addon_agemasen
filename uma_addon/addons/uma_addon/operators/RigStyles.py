"""Rig *display* styles: one JSON file per style, applied to the rig a run generated.

Asked for by the user: *"I'd like to queue the task of matching the visualization style of coolchou's rigs (how
the rigify bones are displayed - color, size and other display styles). I figure we could make that into a
selectable setting as well - so a config file holds the available styles, one file per style. Make coolchou's
style the default one, but leave the option of creating more styles to the user."*

This module is the data half of that feature, laid out exactly like the presets (one file each in
`<Blender config>/uma_addon/styles/`, README in the folder, cache keyed on the folder's timestamp). The operator
side - the dropdown, `Save style from this rig`, and the call at the end of a run - lives in `RigifyChains.py`.

**A style is display only.** It never moves a bone, never re-parents, never changes a constraint: it sets what
Blender *draws*. Everything it touches is per-bone or per-armature RNA that the .blend saves:

* the armature: `display_type`, `show_in_front`, `show_axes`, `show_names`, `axes_position`,
  `show_bone_colors`, `show_bone_custom_shapes`;
* the object: `display_type`;
* per control bone: `color.palette` / `color.custom`, and the widget's `use_custom_shape_bone_size` / scale.

The shipped `coolchou.json` is not a guess: it is what `rig_style_dump.py` read out of **14 of his packed rigs**
(7062 bones, `mmr_out/style/rig_style_dump.json`, written up in `RIGIFY_CHAINS_HANDOFF.md` §9al). What that
measurement settled, and why the file looks the way it does:

* **only controls are coloured.** Every `ORG-`/`DEF-`/`MCH-` bone in all 14 rigs is left `DEFAULT`, so the style
  paints controls and leaves internals alone (`"paint": "controls"`).
* **the colours are rigify's roles plus his chain palette.** FK green, IK red, tweaks blue, face yellow, `root`
  mauve - and the part that matters here, his **chain** colours, which are per family rather than one colour:
  hair, ear and tail chains are **white** (the largest single group in his palette, 642 bones), skirts are pink
  (`Sp_Hi_MSkirt0`, 404 bones) and the chest/accessory chains orange (`Sp_Ch_*`, `Sp_Hi_Ribbon0`, 209). Ours came
  out uncoloured, because rigify assigns a colour per *rig type* and `basic.super_copy`, `spines.basic_tail` and
  `limbs.spline_tentacle` ship none - 223 of the 451 controls on a `1004_00` rig were left `DEFAULT`
  (measurement in `our_style_check.py`). The colour list below is therefore keyed by **family**, in his order,
  first match wins.
* **`show_in_front` is `False`**, in all 14 rigs, which is the "not see-through" the user described. Ours is
  `True` because `operators/Controller.py` sets it (`# 在前面`); per the user's decision the style wins and every
  run now turns it off on the rig it generated.
* everything else was already `True`/`OCTAHEDRAL`/`WIRE` on our rigs, and is written into the file anyway, so a
  style stays a complete statement of the look rather than a diff against rigify's current behaviour.
"""
import fnmatch
import json
import os

import bpy

# Bumped when the meaning of a field changes, so a file written by an older version can be recognised
STYLE_VERSION = 1
DEFAULT_STYLE_KEY = "coolchou"
# the value of the `Style` dropdown that means "generate it and leave the display exactly as rigify made it"
STYLE_NONE = "none"

STYLE_README = """\
Rig display styles for the uma_addon Pumpkin Rig Setup
======================================================

One file per style. The file name is the style's key - `coolchou.json` is the one the operator ships
and selects by default; copy it, rename the copy, and edit that to make your own.

What a style is
---------------
How a rig *looks*, and nothing else:

    "armature": {"display_type": "OCTAHEDRAL", "show_in_front": false, "show_axes": false, ...}
    "object":   {"display_type": "WIRE"}
    "control_size": {"bone_size": true, "scale": [1.0, 1.0, 1.0]}
    "colors": [ {"match": ["Sp_He_*", "Ear*"], "color": [1.0, 1.0, 1.0], "about": "hair chains"} ]

A style never moves a bone, never re-parents and never touches a constraint, so applying one cannot change
how the rig behaves at rest. It is applied to the rig at the end of a run, and applying it twice changes
nothing the second time.

Bone colours
------------
`colors` is read top to bottom and the **first rule that matches a bone wins**, so put the specific
patterns above the general ones. A rule matches on either of:

    "match": ["*_fk"]        the bone's name, with * and ? wildcards
    "match": ["class:internal"]   ORG-/DEF-/MCH- bones (what rigify drives the deform with)
    "match": ["class:control"]    everything else - the bones an animator grabs
    "match": ["role:chain"]       a bone of a chain *this run* typed, including its tweak and master controls
    "names": ["Ankle_fk_L"]       exact bone names, for a one-off

A rule with no `color` leaves the bones alone, which is how "internals stay as rigify made them" is written.
`"select"` and `"active"` are optional extra shades; without them the colour is used for all three states.

Where the default came from
---------------------------
`coolchou.json` was read out of 14 of the animator's own packed rigs - 7062 bones - by `rig_style_dump.py`,
and the numbers are in `RIGIFY_CHAINS_HANDOFF.md` 9al: only controls are coloured, his chain controls are
white, FK is green, IK red, tweaks blue, face yellow, and `show_in_front` is off in all 14.

The folder next to this one
---------------------------
`../presets/` holds per-character *rig type* choices (what a chain is made of), and
`../rigify_chains.json` is the rules table. Those decide what the rig is; this folder decides only how it
looks.
"""

# ----------------------------------------------------------------------------------------------------
# The shipped style: his rigs, measured (handoff 9al). Colours are the 3-float `normal` values read off his
# bones; `about` is there because a colour with no name is unreadable six months later.
# ----------------------------------------------------------------------------------------------------
COOLCHOU = {
    "key": DEFAULT_STYLE_KEY,
    "name": "Coolchou's rigs (default)",
    "about": "Read out of 14 of his packed rigs (7062 bones) by rig_style_dump.py - see "
             "RIGIFY_CHAINS_HANDOFF.md 9al. Only control bones are coloured; his hair, ear and tail chains are "
             "white, skirts pink and chest chains orange.",
    "version": STYLE_VERSION,
    "armature": {
        "display_type": "OCTAHEDRAL",
        "show_axes": False,
        "show_names": False,
        "axes_position": 0.0,
        # without these two, colours and widgets do not draw at all (measured, handoff 9al) - written into the
        # file rather than left to rigify, so a style is a complete statement of the look
        "show_bone_colors": True,
        "show_bone_custom_shapes": True,
    },
    # `show_in_front` is the *object*'s, not the armature's - that is where the measurement found it, and where
    # the "not see-through" the user asked for lives. His rigs have it False in 14 of 14; ours was True because
    # `operators/Controller.py` sets it when it builds MMR's controller.
    "object": {"display_type": "WIRE", "show_in_front": False},
    # his widgets are scaled with the bone on 7048 of 7062 bones; `scale` is multiplied into whatever scale a
    # widget already has, so rigify's mirrored (-1, 1, 1) widgets stay mirrored instead of being flattened
    "control_size": {"bone_size": True, "scale": [1.0, 1.0, 1.0]},
    "paint": "controls",
    # The colours are his, written as the exact values his rigs carry: they are 8-bit fractions (measured at six
    # decimals - `0.376471` is 96/255), and the six-decimal form is what a comparison against his rigs needs.
    # His green turns out to be *rigify's own* FK green, which is why the controls rigify colours itself (the
    # eyes, the IK poles) already match without this file saying anything about them.
    "colors": [
        {"match": ["root", "Root"], "color": [0.690196, 0.466667, 0.678431],
         "about": "root - his only mauve, 14 of 14 rigs"},
        {"match": ["*Skirt*"], "color": [1.000000, 0.345098, 0.941177],
         "about": "skirt chains: Sp_Hi_MSkirt0, Sp_Hi_CSkirt0 and their tweaks (404 bones)"},
        {"match": ["Face_Driver_*", "M_*"], "color": [0.980392, 0.901961, 0.239216],
         "about": "face drivers panel and bones (289 bones)"},
        {"match": ["Sp_Ch_*", "Sp_Hi_Ribbon*", "Sp_Wa_*", "Sp_Ne_*", "Sp_So_*", "Sp_El_*", "Sp_Kn_*",
                   "*_01_master"],
         "color": [0.984314, 0.537255, 0.337255],
         "about": "chest, waist and accessory chains, and the finger masters rigify makes (209 bones)"},
        {"match": ["*_ik", "*_ik.*", "*_ik_*", "*_ik_target*", "*_parent", "*_parent_*", "Shoulder*"],
         "color": [0.800000, 0.000000, 0.000000],
         "about": "IK controls, their targets and parents (420 bones)"},
        # `*_fk_*` is not padding: rigify names the FK control of a sided limb `Arm_fk_L`, which `*_fk` alone does
        # not match - it ends in `_L` - and the measurement caught exactly that (70 green controls on a `1004_00`
        # rig kept rigify's own green because the pattern never claimed them)
        {"match": ["tweak_*", "*_tweak", "*_tweak.*", "*_tweak_*"],
         "color": [0.219608, 0.498039, 0.784314],
         "about": "tweak controls (280 bones, shared with the spine FK below)"},
        {"match": ["Chest_fk", "Hip_fk", "Waist_fk", "Neck_fk", "Head_fk", "Torso_fk",
                   "Chest_fk.*", "Hip_fk.*", "Waist_fk.*", "Neck_fk.*", "Head_fk.*", "Torso_fk.*"],
         "color": [0.219608, 0.498039, 0.784314],
         "about": "spine FK is blue in his rigs, where limb FK is green - so this must come first"},
        {"match": ["*_fk", "*_fk.*", "*_fk_*"], "color": [0.376471, 0.780392, 0.207843],
         "about": "limb FK (495 bones, with the fingers and the tail tweaks) - rigify's own green"},
        {"match": ["Index*", "Ring*", "Thumb*", "Middle*", "Pinky*"], "color": [0.376471, 0.780392, 0.207843],
         "about": "fingers"},
        {"match": ["Sp_He_*", "Ear*", "Sp_Hi_Tail0*"], "color": [1.000000, 1.000000, 1.000000],
         "about": "hair, ear and tail chains - white, by name, so the colour does not depend on a run having "
                  "typed them"},
        {"match": ["role:chain"], "color": [1.000000, 1.000000, 1.000000],
         "about": "any other chain this run typed - the catch-all that keeps a new family white"},
        # His 210 uncoloured controls are not reproduced: everything the rules above do not name is left exactly
        # as rigify made it, which is the honest default for a family nobody has looked at yet.
    ],
}

_STYLE_CACHE = {"stamp": None, "styles": {}, "loaded": False}


def style_dir():
    """Where one style lives per file: `<Blender config>/uma_addon/styles/`."""
    return bpy.utils.user_resource("CONFIG", path=os.path.join("uma_addon", "styles"), create=True)


def style_filename(key):
    """A style key as a file name: `coolchou` stays, anything a file system dislikes becomes `_`."""
    cleaned = "".join(character if (character.isalnum() or character in "._-") else "_" for character in key)
    return cleaned.strip("._") or "style"


def write_style_readme():
    """A plain-language note in the folder itself, so it explains itself to whoever opens it."""
    path = os.path.join(style_dir(), "README.txt")
    if os.path.isfile(path):
        return path
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(STYLE_README)
    except OSError:
        return None
    return path


def style_stamp():
    """A cheap fingerprint of the folder, so the list is only re-read when something changed on disk."""
    try:
        entries = sorted(os.listdir(style_dir()))
    except OSError:
        return None
    return tuple((name, os.path.getmtime(os.path.join(style_dir(), name))) for name in entries)


def forget_styles():
    """Drop the cache, so the next read comes off disk. Called by anything that writes a style."""
    _STYLE_CACHE["stamp"] = None
    _STYLE_CACHE["styles"] = {}
    _STYLE_CACHE["loaded"] = False


def style_files(force=False):
    """Every style in the folder, as `{key: entry}`. A file that will not parse is skipped, not fatal."""
    stamp = style_stamp()
    if not force and _STYLE_CACHE["loaded"] and _STYLE_CACHE["stamp"] == stamp:
        return _STYLE_CACHE["styles"]
    styles = {}
    try:
        names = sorted(os.listdir(style_dir()))
    except OSError:
        names = []
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(style_dir(), name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                entry = json.load(handle)
        except (OSError, ValueError) as error:
            print("[rig_styles] could not read style %s (%s)" % (path, error))
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("colors"), list):
            print("[rig_styles] %s has no `colors` list, skipping" % path)
            continue
        entry = dict(entry)
        entry["file"] = path
        key = entry.get("key") or os.path.splitext(name)[0]
        styles[key] = entry
    _STYLE_CACHE["stamp"] = stamp
    _STYLE_CACHE["styles"] = styles
    _STYLE_CACHE["loaded"] = True
    return styles


def ensure_style_files(trace=None):
    """Put the shipped style and the README in the folder, once. Returns the keys written."""
    write_style_readme()
    written = []
    target = os.path.join(style_dir(), style_filename(DEFAULT_STYLE_KEY) + ".json")
    if not os.path.isfile(target):
        try:
            save_style(DEFAULT_STYLE_KEY, COOLCHOU)
            written.append(DEFAULT_STYLE_KEY)
        except OSError as error:
            if trace is not None:
                trace.info("could not write the shipped style: %s" % error)
    if written and trace is not None:
        trace.info("shipped style written: %s" % ", ".join(written))
    return written


def save_style(key, entry):
    """Write one style to its own file. Returns the path."""
    write_style_readme()
    if not key:
        raise ValueError("a style needs a key")
    payload = {name: value for name, value in entry.items() if name != "file"}
    payload["key"] = key
    payload.setdefault("version", STYLE_VERSION)
    path = os.path.join(style_dir(), style_filename(key) + ".json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    forget_styles()
    return path


def read_style(key):
    """One style by key, or `None`. `STYLE_NONE` and an empty key both return `None` (nothing to apply).

    The shipped style is **built in**: if the folder has no `coolchou.json` - a fresh install, before the first
    run has written one - the built-in copy answers, so the default is available on the very first run and a
    script can say `Style=coolchou` before any file exists. A file with that name always wins, which is what
    makes the copy in the folder something the user can edit.
    """
    if not key or key == STYLE_NONE:
        return None
    entry = style_files().get(key)
    if entry is not None:
        return entry
    if key == DEFAULT_STYLE_KEY:
        entry = dict(COOLCHOU)
        entry["built_in"] = True
        entry["file"] = None
        return entry
    return None


def style_label(entry, key=None):
    """One line for the dropdown: the key, then whatever the file says it is."""
    name = entry.get("name") or ""
    key = key or entry.get("key") or "?"
    if name and name != key:
        return "%s (%s)" % (key, name)
    return key


def style_items(self, context):
    """The `Style` dropdown: the default first, the rest sorted, `None` last.

    The shipped default is listed whether or not its file is in the folder: it is built into the operator
    (`COOLCHOU`), the file is a copy the user can edit, and a fresh install has to be able to pick it on the
    first run - a dropdown that only offered what happened to be on disk would offer `none` and nothing else.
    Nothing is written here: `draw` runs on every redraw, and files are written from `execute`.
    """
    try:
        styles = style_files()
    except Exception:  # noqa: BLE001 - a dropdown must never raise
        styles = {}
    items = []
    default = styles.get(DEFAULT_STYLE_KEY) or dict(COOLCHOU)
    items.append((DEFAULT_STYLE_KEY, style_label(default, DEFAULT_STYLE_KEY) + " - default",
                  default.get("about") or "The animator's own display style"))
    for key in sorted(styles):
        if key == DEFAULT_STYLE_KEY:
            continue
        items.append((key, style_label(styles[key], key), styles[key].get("about") or ""))
    items.append((STYLE_NONE, "None (as rigify made it)",
                  "Generate the rig and leave every display setting exactly as rigify left it"))
    return items


# ----------------------------------------------------------------------------------------------------
# applying one
# ----------------------------------------------------------------------------------------------------

def bone_class(name):
    """`internal` for the bones rigify drives the deform with, `control` for everything else."""
    for prefix in ("ORG-", "DEF-", "MCH-"):
        if name.startswith(prefix):
            return "internal"
    return "control"


def chain_prefixes(chain_names):
    """The families a run typed, from the names of their bones: `Sp_He_Hair2_00_master` -> `Sp_He_Hair2_00`.

    A run knows which families it typed and a name alone does not, which is the whole reason `role:chain`
    exists as a token rather than a pattern: `Sp_Ch_Bust0` and `Sp_He_Hair2` are both `Sp_*` chains, and his
    rigs give them different colours.
    """
    prefixes = set()
    for name in chain_names or ():
        stem = os.path.splitext(name)[0]
        prefixes.add(stem)
        for marker in ("_master", "-twist"):
            if stem.endswith(marker):
                prefixes.add(stem[: -len(marker)])
    return prefixes


def is_chain_bone(name, prefixes):
    """True for a chain's own controls, its masters and the `tweak_` copies rigify makes for each of them."""
    for stem in (name, os.path.splitext(name)[0]):
        for prefix in prefixes:
            if stem == prefix or stem.startswith(prefix + "_") or stem.startswith(prefix + "."):
                return True
            for wrapper in ("tweak_", "tweak_base_"):
                if stem.startswith(wrapper + prefix):
                    return True
    return False


def rule_matches(rule, name, klass, prefixes):
    """Does one rule claim this bone? `names` first (exact), then `match` patterns."""
    for exact in rule.get("names") or ():
        if name == exact or os.path.splitext(name)[0] == exact:
            return True
    for pattern in rule.get("match") or ():
        if pattern == "class:internal":
            if klass == "internal":
                return True
        elif pattern == "class:control":
            if klass == "control":
                return True
        elif pattern == "role:chain":
            if klass == "control" and is_chain_bone(name, prefixes):
                return True
        elif fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(os.path.splitext(name)[0], pattern):
            return True
    return False


def colour_for(name, klass, entry, prefixes):
    """The first rule that claims this bone, or `None` so the bone keeps whatever rigify gave it."""
    for rule in entry.get("colors") or ():
        if not isinstance(rule, dict):
            continue
        if rule_matches(rule, name, klass, prefixes):
            return rule
    return None


def _rgb(rule):
    """A rule's colour as three floats. Raises `ValueError` for a file that says something else."""
    raw = rule.get("color")
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise ValueError("a colour in a style must be three numbers (r, g, b), got %r" % (raw,))
    return tuple(max(0.0, min(1.0, float(value))) for value in raw)


def apply_style(rig, entry, chain_names=(), trace=None):
    """Apply one style to one generated rig. Display only, idempotent, and never fatal.

    Returns a summary dict - what was set and what was left alone - so the run can put the numbers in its trace
    rather than claiming a style "was applied" without saying what it did.
    """
    summary = {"style": entry.get("key") if entry else None, "flags": [], "coloured": 0, "chain_coloured": 0,
               "matched_unpainted": 0, "left_as_rigify_made_it": 0, "warnings": []}
    if not entry or rig is None:
        return summary

    def note(message):
        summary["warnings"].append(message)
        if trace is not None:
            trace.info("style %s: %s" % (summary["style"], message))

    # ---- what the armature and the object draw ------------------------------------------------------
    for key, value in (entry.get("armature") or {}).items():
        try:
            setattr(rig.data, key, value)
            summary["flags"].append("armature.%s=%s" % (key, value))
        except (AttributeError, TypeError) as error:
            note("could not set armature.%s to %r (%s)" % (key, value, error))
    for key, value in (entry.get("object") or {}).items():
        try:
            setattr(rig, key, value)
            summary["flags"].append("object.%s=%s" % (key, value))
        except (AttributeError, TypeError) as error:
            note("could not set %s to %r (%s)" % (key, value, error))

    # ---- how big the widgets are drawn ---------------------------------------------------------------
    size = entry.get("control_size") or {}
    if size:
        bone_size = size.get("bone_size")
        scale = size.get("scale")
        scaled = 0
        for pose_bone in rig.pose.bones:
            if pose_bone.custom_shape is None:
                continue
            try:
                if bone_size is not None:
                    pose_bone.use_custom_shape_bone_size = bool(bone_size)
                if scale:
                    # *multiplied* into what is there, not written over it: rigify mirrors a widget on the other
                    # side with a negative scale (-1, 1, 1) - 103 of the 451 controls on a `1004_00` rig - and
                    # writing (1, 1, 1) over that would silently un-mirror them
                    current = list(pose_bone.custom_shape_scale_xyz)
                    pose_bone.custom_shape_scale_xyz = [current[index] * float(scale[index]) for index in range(3)]
                    scaled += 1
            except (AttributeError, TypeError, IndexError) as error:
                note("could not size the widget of %s (%s)" % (pose_bone.name, error))
                break
        summary["flags"].append("widgets: bone_size=%s scale=%s (%d scaled)" % (bone_size, scale, scaled))

    # ---- the colours --------------------------------------------------------------------------------
    paint = str(entry.get("paint") or "controls").lower()
    prefixes = chain_prefixes(chain_names)
    painted_families = set()
    for bone in rig.data.bones:
        klass = bone_class(bone.name)
        if klass == "internal" and paint == "controls":
            summary["left_as_rigify_made_it"] += 1
            continue
        try:
            rule = colour_for(bone.name, klass, entry, prefixes)
            if rule is None:
                summary["left_as_rigify_made_it"] += 1
                continue
            colour = _rgb(rule)
            if colour is None:
                # an explicit "leave this alone" rule: `{"match": ["class:internal"]}` with no colour
                summary["matched_unpainted"] += 1
                continue
            bone.color.palette = 'CUSTOM'
            # `select` and `active` are optional extra shades; a rule that gives only `color` uses it for all
            # three states, which is what his rigs do
            select = _rgb({"color": rule["select"]}) if rule.get("select") else colour
            active = _rgb({"color": rule["active"]}) if rule.get("active") else colour
            # three floats, not four: writing RGBA raises `sequences of dimension 0 should contain 3 items`
            bone.color.custom.normal = colour
            bone.color.custom.select = select
            bone.color.custom.active = active
            summary["coloured"] += 1
            if klass == "control" and is_chain_bone(bone.name, prefixes):
                summary["chain_coloured"] += 1
                painted_families.add(rule.get("about") or "chain")
        except (AttributeError, TypeError, ValueError) as error:
            note("could not colour %s (%s)" % (bone.name, error))
    summary["chain_families"] = sorted(painted_families)
    return summary


def describe_style(entry):
    """One line for a report or a trace: what this style will do, without opening it."""
    if not entry:
        return "no style"
    colours = [rule for rule in (entry.get("colors") or []) if isinstance(rule, dict) and rule.get("color")]
    armature = entry.get("armature") or {}
    obj = entry.get("object") or {}
    # `show_in_front` is the object's, so it is read from there; asking the armature block printed `None` in a
    # trace until this probe's first run caught it
    return ("%s: %d colour rule(s), display %s, show_in_front=%s, widgets scaled by bone=%s"
            % (entry.get("key"), len(colours), armature.get("display_type"),
               obj.get("show_in_front"), (entry.get("control_size") or {}).get("bone_size")))


# ----------------------------------------------------------------------------------------------------
# reading a style back out of a rig - what `Save style from this rig` does
# ----------------------------------------------------------------------------------------------------

def find_generated_rig(context):
    """The rigify rig in this scene: an armature with ORG-/DEF-/MCH- bones that is not the model.

    Preferred in the same order the run itself uses - whatever is active first, so "save the style of the rig I
    just made" works in a file that holds two characters.
    """
    def generated(obj):
        return (obj.type == 'ARMATURE'
                and any(bone.name.startswith(("ORG-", "DEF-", "MCH-")) for bone in obj.data.bones))

    active = getattr(context.view_layer.objects, "active", None)
    if active is not None and generated(active):
        return active
    for obj in context.scene.objects:
        if generated(obj):
            return obj
    return None


def _common_pattern(names):
    """The shortest `prefix*` that covers most of a group, or `None` if there is no shared stem.

    Deliberately crude: a captured file is a starting point for a human to tidy, so it favours a readable
    pattern plus an explicit list of the names it misses over a clever expression nobody can edit.
    """
    tokens = [os.path.splitext(name)[0].split("_") for name in names]
    if not tokens:
        return None
    shared = []
    for index in range(min(len(row) for row in tokens)):
        if len({row[index] for row in tokens}) == 1:
            shared.append(tokens[0][index])
        else:
            break
    if not shared:
        tails = {row[-1] for row in tokens}
        if len(tails) == 1:
            return "*" + tokens[0][-1]
        return None
    return "_".join(shared) + "*"


def capture_style(rig, key=None):
    """Read one rig's display settings into a style entry.

    Colours are grouped by value and each group is written as a pattern where one exists, plus the exact names
    the pattern misses - so the file reproduces the rig it came from and can be edited afterwards, which is the
    same trade the preset capture makes (a list a human can read beats a perfect compression).
    """
    import collections

    def rgb(colour):
        # a *tuple*, so it can key the grouping below - a list is unhashable, which is how this line first ran
        return tuple(round(float(value), 4) for value in colour)

    entry = {
        "key": key or "my-style",
        "name": "Captured from %s" % (rig.name if rig else "a rig"),
        "about": "Read out of the rig in the scene by 'Save style from this rig'",
        "version": STYLE_VERSION,
        "armature": {
            "display_type": rig.data.display_type,
            "show_axes": bool(rig.data.show_axes),
            "show_names": bool(rig.data.show_names),
            "axes_position": round(float(getattr(rig.data, "axes_position", 0.0)), 4),
            "show_bone_colors": bool(rig.data.show_bone_colors),
            "show_bone_custom_shapes": bool(rig.data.show_bone_custom_shapes),
        },
        # on the object, where Blender keeps it - see COOLCHOU above
        "object": {"display_type": rig.display_type, "show_in_front": bool(rig.show_in_front)},
        "control_size": {"bone_size": False, "scale": [1.0, 1.0, 1.0]},
        "paint": "controls",
        "colors": [],
    }

    shaped = [pose_bone for pose_bone in rig.pose.bones if pose_bone.custom_shape is not None]
    if shaped:
        entry["control_size"]["bone_size"] = all(pb.use_custom_shape_bone_size for pb in shaped)
        scales = {tuple(round(float(value), 4) for value in pb.custom_shape_scale_xyz) for pb in shaped}
        if len(scales) == 1:
            entry["control_size"]["scale"] = list(scales.pop())
        else:
            # mixed, which means somebody sized them one by one: the most common scale, and a note saying so
            counts = collections.Counter(tuple(round(float(v), 4) for v in pb.custom_shape_scale_xyz)
                                         for pb in shaped)
            entry["control_size"]["scale"] = list(counts.most_common(1)[0][0])
            entry["control_size"]["about"] = ("%d of %d widgets are at this scale; the rest were sized "
                                              "individually" % (counts.most_common(1)[0][1], len(shaped)))

    groups = collections.defaultdict(list)
    internals = []
    for bone in rig.data.bones:
        if bone_class(bone.name) == "internal":
            internals.append(bone.name)
            continue
        if bone.color.palette != 'CUSTOM':
            continue
        groups[rgb(bone.color.custom.normal)].append(bone.name)
    for colour, names in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        names = sorted(names)
        pattern = _common_pattern(names)
        # `colour` is a tuple for grouping and a list in the file, because JSON has no tuples
        rule = {"color": list(colour), "bones": len(names)}
        if pattern:
            rule["match"] = [pattern]
            missed = [name for name in names if not fnmatch.fnmatchcase(name, pattern)
                      and not fnmatch.fnmatchcase(os.path.splitext(name)[0], pattern)]
            if missed:
                rule["names"] = missed
        else:
            rule["names"] = names
        entry["colors"].append(rule)
    entry["captured"] = {"bones": len(rig.data.bones), "controls": len(rig.data.bones) - len(internals),
                         "coloured": sum(len(names) for names in groups.values()),
                         "uncoloured_controls": len(rig.data.bones) - len(internals)
                                                - sum(len(names) for names in groups.values()),
                         "internals_left_alone": len(internals)}

    # Verify the compression against the rig itself, and put the exceptions **in front**. A pattern derived from a
    # big group (`Sp_*`) can claim a bone that belongs to a smaller one, and `apply_style` is first-match-wins, so
    # without this the captured file silently gives those bones the wrong colour - measured: 10 of 451 bones on a
    # `1004_00` rig. Exact `names` are checked before `match` inside a rule, so one leading rule per colour fixes
    # it without touching the readable patterns behind.
    corrections = {}
    probe = {"colors": entry["colors"]}
    for name, colour in ((bone.name, rgb(bone.color.custom.normal)) for bone in rig.data.bones
                         if bone_class(bone.name) == "control"):
        if colour not in groups:
            continue
        rule = colour_for(name, "control", probe, set())
        assigned = tuple(round(float(value), 4) for value in rule["color"]) if rule and rule.get("color") else None
        if assigned != colour:
            corrections.setdefault(colour, []).append(name)
    if corrections:
        fixed = [{"names": sorted(names), "color": list(colour), "bones": len(names),
                  "about": "captured exactly: a pattern below would have claimed these bones"}
                 for colour, names in sorted(corrections.items(), key=lambda item: item[0])]
        entry["colors"] = fixed + entry["colors"]
        entry["captured"]["corrected"] = sum(len(names) for names in corrections.values())
    return entry
