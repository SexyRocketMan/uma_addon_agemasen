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
table (name pattern -> rigify type) plus the run options, and `presets/` holds one file per preset - a set of
answers somebody saved, keyed by nothing but its own name. **Nothing picks a preset automatically**: the rules
table is what a run uses until a preset is chosen, so a model's rig is the same on every machine. The presets
keyed by character and costume that this used to autodetect are retired (handoff 9bd).
`docs/PRESETS_AND_RULES.md` is the map; the handoff's section 9aa is how it got that way.
"""
import copy
import json
import math
import os
import re
import shutil
import time

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, StringProperty
from mathutils import Matrix, Vector

# The rig display styles: one JSON file per style in `<config>/uma_addon/styles/`, his own style shipped as the
# default. Display only - the data, the apply, and the capture all live in `RigStyles` (handoff 9al/9am).
from .SkirtReshape import SKIRT_HINGE_SPACING_M
from .SkirtReshape import reshape_skirt_panels
from .RigStyles import (STYLE_NONE, apply_style, capture_style, describe_style, ensure_style_files, forget_styles,
                        find_generated_rig, read_style, save_style, style_dir, style_filename, style_files,
                        style_items)

# The named steps, per-step timings and the status/progress bar (`Progress.py`). Imported the same defensive way the
# shading half imports its optional modules: a rig run must still work with that file deleted.
try:
    from . import Progress
except ImportError:  # pragma: no cover - only when that file is deleted
    Progress = None

# Rigify's per-bone copying, batched (`RigifyCopyBatch.py`): rigify switches object mode twice per copied bone, which
# is 408 mode switches and seconds of every run. Imported defensively like `Progress` - a run must still work if the
# file is missing, and the batcher itself refuses rather than patches when rigify is not what it was written for.
try:
    from .RigifyCopyBatch import Batcher
except ImportError:  # pragma: no cover - only when that file is deleted
    Batcher = None

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
# the `chain_link: local` wiring (handoff 9bk): one constraint per chain bone, named the same whatever its type,
# so a scene converted between the two wirings has exactly one thing to remove
LINK_CONSTRAINT_NAME = "UMA_chain_link"
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
# **The Pumpkin Rig's own version** - what a user sees. Bump it whenever the *rig* changes (a new chain type, a new
# wiring, a different bone layout); it is deliberately separate from `BUILD_TAG` below, which identifies the *build*
# for the trace, the fleet probe and the stale-code guard, and moves on every commit that touches this file. One
# place, so a bump is one edit: this line, and whatever the message being changed happens to say.
RIG_VERSION = "v8"
RIG_TAG = "Pumpkin Rig " + RIG_VERSION   # what a user sees in a message or a panel label

BUILD_TAG = "chains-80"
BUILD = BUILD_TAG + (
    ": 2.33.12 - the static face copy follows the head by bone parenting, and it is one click away (rig version stays "
    "v8). The user: *\"the static copy is not copying the rotation of the head bone properly. The static face copy "
    "retains its pivot point (which is at the uma's feet) - so it doesn't rotate in place, but rather on a huge "
    "sphere. Also, even with the pivot point fixed, the rotation is not matching - maybe the copy rotation constraint "
    "is not configured properly\"*, and separately *\"it's hard to access the static face copy due to how it's hidden "
    "- you can't just unhide it using the ui viewport/render visibility [...] it should be easier to access, since it "
    "can be a source of many issues\"*. Both reports were right, and they were **two** bugs: a `COPY_ROTATION` on an "
    "*object* turns it about that object's own origin, and the copy's origin is the face object's - the model's feet "
    "- so turning the head swept it around the body; and `LOCAL` on both sides reads the *bone's* axes into the "
    "*object's*, so the rotation came out about the wrong ones. `LOCAL`/`LOCAL` had been chosen for a good reason (it "
    "was the only setting that was identity **at rest**, 0.0 deg, and that identity is what keeps the transferred "
    "normals the face's own), so the answer is not a different space but **no constraint at all**: "
    "`operators/FaceNormals.py` `follow_head` bone-parents the copy to the head, which inherits the bone's transform "
    "*as a transform*, turns about the head's own head, and leaves no space to get wrong. **The order of the last two "
    "lines is the trick** - parent fields, then `matrix_parent_inverse`, then `matrix_world`; the other order moves "
    "the copy. Measured: turning the head 30 deg moves the copy **0.00000 m off a rigid turn of the head** (it moves "
    "0.05949 m, about the head rather than the feet), and the transfer still changes nothing at rest (**0.0 deg**). "
    "**Why this had been written off**: the probe that measured the five ways of following the head built its "
    "bone-parented variant in that wrong order, so it read 72.74 deg like the world-space ones and the conclusion "
    "\"LOCAL/LOCAL is the only one that is identity at rest\" was drawn from a candidate that had never been tested. "
    "Corrected and re-run: 0.0 deg for bone parenting, the same as LOCAL/LOCAL, and this one also gets the motion "
    "right. **Accessibility**: `uma.face_normals_reveal`, a third button on the Shading row (\"Find the static face "
    "copy\"), enables the collection, clears an excluded view-layer entry, selects and frames the copy, and a second "
    "press hides it again - the hidden collection stays hidden by default, because nobody wants a second face in the "
    "viewport while posing, but it is no longer lost. Gates: `face_normals_check.py` 16/0, with the old \"a "
    "constraint exists\" check replaced by two that measure the thing itself (it is bone-parented; and posing the "
    "head moves it rigidly). Before that: "
    ": 2.33.11 - the skirt master hangs off the trunk, so it follows the body (rig version stays v8). The user: "
    "*\"the skirt master is not parented to anything right now - it stays in place when you move the body. I'd say it "
    "should be a child of `torso`\"*. It was created as a parentless root, which was invisible in every gate because "
    "nothing tested it: the two constraints that read it use `LOCAL` spaces and only its *rotation*, so a master "
    "that never moves works exactly as well for them - and fails the hand that has to find it. This is the skirt "
    "study's §7 item 2 arriving as a bug report, and the answer is the one the user's own rigs use: "
    "`SKIRT_MASTER_PARENTS = (\"torso\", \"hips\", \"torso_root\")`, first one this rig has (the reference model's "
    "rig is `root` -> `torso_root` -> `torso` -> `hips`/`chest`, and `torso` is what the user asked for). "
    "`use_connect` stays off - a connected bone's head is locked to its parent's tail, and this one hangs at the "
    "ring's centre - and parenting changes no geometry, so the master's own frame, `_skirt_flare_angles`, the lift's "
    "window and every panel copy are untouched. Measured: posing `torso` 20 deg now moves the master **0.01838 m** "
    "(it was 0.00000), and every skirt number is unchanged - 10 of 10 panels still move, per-joint turns still "
    "26.63-30.0 deg, 10 of 10 outward and rising, worst neighbour excess still 0.0 deg. `skirt_master_check.py` "
    "grew two checks for it (the parent, and that posing the trunk actually moves it - the parent is the mechanism, "
    "the motion is the claim), 23 -> 25. Before that: "
    ": 2.33.10 - the rules file in use is brought up to date with this build's table, on a run (rig version stays "
    "v8). The user, after 2.33.9: *\"we should be able to control the default rules between versions 100% - they are "
    "not for the user to change. Should we overwrite/create the default ruleset on install?\"* The problem was real: "
    "`load_rules` uses a rules file's table **instead of** the shipped one, so until now a rule added to "
    "`DEFAULT_RULES` reached **nobody who already had a rules file** - 2.33.9 put four skirt rules in the shipped "
    "table and they did nothing on this machine, whose config table was thirteen rules written a week earlier, and "
    "the answer had to be duplicated in code to reach an install at all. The policy, decided by the user: **a "
    "pattern we ship is ours**. A shipped pattern the file lacks is **added**; a shipped pattern whose value differs "
    "is **refreshed**, with the value it replaced written into the file's own `housekeeping_rules_table` note; "
    "`options` keys we ship and the file lacks are filled in and existing ones are never overwritten (those are the "
    "user's choices - `chain_link`, `resample`, `never_rig`); a pattern the file carries and we do not ship is left "
    "exactly as it is; a backup is written beside the file first. `rules_housekeeping.py` in the DevTools repo has "
    "done this by hand since `chains-29` with exactly this doctrine - what was missing was that it is manual, and "
    "that its list of rules was a hand-copy of the table, so a new rule was invisible to it. This runs on `invoke` "
    "and on `execute`, before the rows and the run read the table, and never from `draw` or at import. Three "
    "deliberate refusals: a file with **no `rules` array** is left alone (adding one would flip a run from the "
    "built-in table to a frozen copy of it), an **explicitly passed `Rules_File`** is never touched (a probe's table "
    "or a user's per-character one), and a file that will not parse is reported rather than repaired. Two bugs this "
    "pass shipped in its first draft and its own edge-case test caught: the `was` value of a refreshed rule was "
    "recorded **by reference** and then cleared, so the note read `which the file had as {}`; and the insertion "
    "point was 'before the first bare skip', which put a new rule ahead of the table's leading `_Handle$` skip - the "
    "exact mistake this table's own comment records (`a handle bone in the ear chain`, `worst 0.01000 m at "
    "Ear_03_L_Handle`), because the resolver only treats the *leading* run of skips as leading. Measured on this "
    "machine: one run took the file from 13 rules to 17, wrote `rigify_chains.before_housekeeping.json`, wrote the "
    "note, and typed the same model's skirt as `spines.basic_tail (the rules table)` where the run before it said "
    "`(the skirt family list)`. Before that: "
    ": 2.33.9 - every skirt family a uma ships is rigged as a skirt, not just `Sp_Hi_MSkirt` (rig version v8). The "
    "rules table had **one** skirt rule, so a model whose plate skirt is named anything else fell through to the "
    "`unmatched_families` fallback: flat `basic.super_copy` stubs, one control per bone, no chain - and **no skirt "
    "master at all**, because the master selects its panels by the *rule's* collection rather than by the group a "
    "family is filed under. Measured: 20 of the 125 models in the pool have a skirt in that state, led by "
    "`Sp_Hi_CSkirt0` (9 models), `Sp_Wa_MSkirt1` (5) and `Sp_Hi_CSkirt1` (2). `Sp_Wa_MSkirt0` shows the shape of it "
    "exactly - the family is *already* in the `Skirt` collection (`GROUP_PREFIXES` knows that name), so a user sees a "
    "Skirt group and assumes it is rigged like one, while the run says `skirt master: skipped - 0 skirt panel(s) with "
    "a control of their own to drive (2 needed)`. Four rules added - `^Sp_Wa_MSkirt`, `^Sp_Hi_CSkirt`, "
    "`^Sp_Si_MSkirt`, `^Sp_Hi_Hem` - all `spines.basic_tail` in the `Skirt` collection with the same "
    "`copy_rotation_axes` and `chain_deform: bbone` the existing skirt rule uses, and the same three prefixes joined "
    "the group map. Measured on `1025_00` with a rules file carrying just that one rule: chains 52 -> 28 (a "
    "`basic_tail` panel is one chain per panel where `super_copy` was one control per *bone*), the families move from "
    "`Extras`/`basic.super_copy` to `Skirt`/`spines.basic_tail`, and the master goes from `skipped` to "
    "`UMA_skirt_master (created) drives 10 panel(s)`. **Deliberately not included**: the `Tp_*`/`Pc_*` families of "
    "the same names (`Tp_Hi_MSkirt0_FL_00`, `Tp_Hi_SkirtRoot0_FL_00`, `Pc_Wa_MSkirt1_FL_00`) are single root bones "
    "with no `_Handle` companions, so a chain rule would invent a panel where the model has an attachment point. The "
    "whole list is patterns rather than one `^Sp_.*[Ss]kirt` for that reason. `operators/SkirtReshape.py` follows "
    "with the same four prefixes (`SKIRT_PREFIXES`) plus a `NOT_A_PANEL` guard on `Root`, since the reshape is the "
    "part a rules file cannot reach - measured on the two `CSkirt` models, **22 panels** over 0.23-0.83 m of cloth "
    "now deforming through 1-4 bones that the module wants 3-5 for. Before that: "
    ": 2.33.8 - the skirt reshape is on by default, and two things the fleet found in it are fixed (rig version "
    "stays v7). It was off for one reason - it rewrites a model's own weights - and that reason is now a "
    "measurement rather than an assumption: `fleet_chains.ps1 -Sample 40` on both labels at `chains-75` found "
    "**262 panels across the 26 models that have the family, 262 measured, 235 reshaped, 27 left alone, and no "
    "panel a measurement failed on**; every one of the 27 skips was \"already deforms through N bone(s), which is "
    "what N m of cloth asks for\"; the **skirt cloth moves 0.00000 m at rest on all 40 models** and `Hip`'s weighted "
    "vertex count changed on **0 of 40**. The two fixes: (1) the module's own check ended with `and bool(reshaped)`, "
    "so a model whose panels were *all* legitimately left alone - `1062_50`, 12 panels, all already deep enough - "
    "reported `every skirt panel deforms through at least 3 bone(s) after the reshape - 0 reshaped, 12 left alone` "
    "as a **failed check**. Leaving every panel alone is the good outcome, so the condition is now `all(...)` over "
    "the panels it did reshape, and the line says which of the two happened. *Do not quote that line with its own "
    "failure marker in this header*: the first version of this note did, and because the header is part of every "
    "trace, every tool that scans a trace for the marker - `fleet_chains_probe.py`, `skirt_master_check.py` - "
    "reported one phantom failed check on every run of every model until it was reworded. The parsers now require "
    "the marker to open a check line rather than appear anywhere, but the header is still not the place for one. "
    "(2) \"no skirt panel had enough cloth to measure\" "
    "was one message for two different nothings and read as a *measurement* failure: 12 of the 40 models name no "
    "skirt bone at all and 2 name it `Sp_Hi_CSkirt0`, which this module does not look for. Those are now two "
    "messages, one naming the prefix it looks for and one giving the panel count it found. The fleet also has the "
    "numbers for a separate, pre-existing gap: a `Sp_Hi_CSkirt0` skirt is rigged as `basic.super_copy` - \"the "
    "fallback for families no rule names\" - in the **Extras** collection rather than as a Skirt chain, because the "
    "rules table's skirt rule and the group map both key on `Sp_Hi_MSkirt`/`Sp_Wa_MSkirt`/`Tp_Hi_MSkirt` alone. "
    "Before that: "
    ": 2.33.7 - the tooltips say what a setting does and stop there. The user: *\"I would trim down the tooltips - "
    "they are a bit too verbose now. Maybe shorten them to a few sentences at most - give a technical user a dry "
    "overview, nothing else.\"* The dropdown *items* were already trimmed to one line each at 2.27.0 (see the note "
    "above `TYPE_CHOICES`), so this is the property and button tooltips: `Reshape short skirt chains` (four "
    "sentences -> two, and the one fact a user must have - that it edits the model's own weights - kept, while the "
    "reasoning behind the default moves into the comment beside it), `Skirt master control`, `Edit per family`, "
    "`Preset`, `Overwrite Existing Types` and `Batch rigify's bone copies`, plus the preset / reload / rules "
    "operator buttons. Nothing but text: no property, default, row or behaviour changed, so the rig is untouched "
    "and RIG_VERSION stays v7. Before that: "
    ": 2.33.6 - the setup panel, slimmed, and the skirt master on by default (rig version stays v7 - the same rig "
    "for a given set of answers, only the answers it opens with changed). The user: *\"the panel is a bit too "
    "overwhelming for the user as it stands\"*. Four changes, and one consequence worth knowing. **`Mode` is now a "
    "checkbox** (`Edit per family`): the two answers were never a mode the user is in, they are how much detail "
    "they want, and `Mode` remains the source of truth that `fallback_mode`, `sync_preset`, `adopt_group_edits` and "
    "the trace all read - the checkbox mirrors it and the click is adopted against a snapshot, exactly the shape "
    "`adopt_group_edits` uses, because a plain mirror would overwrite the click on the next redraw. **The visual "
    "styles are off the dialog** (`Style`, its folder and its save button commented out) - the property, the "
    "operators, `RigStyles.py` and a style's effect on a rig all stay, so a scene, a preset or a script still sets "
    "one and putting the rows back is uncommenting them; with them hidden every rig takes the shipped default "
    "(`coolchou.json`). **Three rows left `Advanced settings`** (renamed from `More settings`, a name it could only "
    "take once `Advanced` stopped meaning the per-family mode): `Batch rigify's bone copies` (pure speed, identical "
    "rig), `Debug` (console verbosity, and it defaulted to on) and `Rules File` (a raw path to the rules JSON, "
    "reachable through the folder button and the `Rigify Chain Rules` operator). **`Skirt master control` defaults "
    "to on** - it was off only because it adds a bone, and that bone is the one-control skirt the feature exists "
    "for - and `Skirt_Reshape` stays off until the 40-model fleet has measured how often its measurement fails on "
    "real models, since it is the one thing here that rewrites a model's own weights. The consequence: `-SkirtMaster` "
    "in `fleet_chains.ps1` and `Skirt_Master=True` in a probe are now no-ops, and anything that needs the rig "
    "*without* the master - `mmr_freedom_check.py`, which pins the whole rig to a SHA, and any fleet label compared "
    "against a pre-2.33.6 one - has to pass `Skirt_Master=False` explicitly. Before that: "
    ": 2.33.5 - the skirt reshape measures the **panel's** cloth, not only the share its root bone owns (rig version "
    "v7). The user, on `silence_suzuka_upgraded.pmx`: *\"the skirt bones generated after the reshape don't cover the "
    "whole span of the skirt [...] the bones cover barely half of the skirt, and the rest of it moves like a slab. I "
    "also expected Suzuka to get more skirt bones after refinement - but she ended up with only two.\"* 2.33.2 "
    "projected onto the root bone only the vertices weighted to that root, which is the whole panel on exactly one "
    "kind of model - Maruzensky, whose leaf carries 0 vertices - and a fraction of it wherever a model spreads its "
    "skirt weight across the panel's bones. Everything downstream comes from that one number: the joint positions, "
    "the bone count, and which vertices are inside the subdivided stretch - and every vertex *below* it is left "
    "alone and then clamped onto the last new bone as one rigid piece. Measured per panel by "
    "`skirt_panel_span_check.py`, before: Suzuka 0.182-0.189 m of a 0.363-0.372 m panel (**50%**), with 48-59% of "
    "the panel's weight past the measured end - the slab - and 3 bones where `round(0.368/0.10)` asks for 4; "
    "Maruzensky 100% (its leaf carries no weight, so the root alone *was* its whole panel, which is why the defect "
    "hid there); the reference model 0.012 m of a 0.218 m panel, left alone by the skip rule for the wrong reason. "
    "After: Suzuka measures each panel's own 0.363-0.372 m, gets **4 bones** and has **0%** of its weight past the "
    "end; Maruzensky is unchanged to the byte (same 0.144-0.207 m, same 3 bones); and the reference now measures "
    "0.218-0.283 m and is still left alone, now because that is what three bones are for. A new `_03` has no "
    "`_03_Handle` beside it - measured, **no `_Handle` group on this model carries any weight at all**, so that is a "
    "bone-list cosmetic and not a deformation. Found by `skirt_panel_span_check.py`, a new probe that asks the "
    "module what it measures and compares it with every vertex the panel actually deforms. Before that: "
    ": 2.33.4 - the skirt reshape splits a panel's weights where it measured them. 2.33.2 measured each panel's "
    "cloth in fractions of its root bone and laid the new joints out on that same basis, but then called "
    "`_reshape_chain` - which shortens that very bone to the first joint - *before* re-reading the bone's length for "
    "the weight split, so `_redistribute` divided every vertex by the shortened length while comparing it against "
    "bone centres still in the original basis, placing each one `f / joints[1]` too far down the panel (1.60x on "
    "Maruzensky). The hip joint the skirt hangs from, `_00`, held **0.2%** of the panel and the hem bone `_02`, "
    "which turns twice as far, held **92.7%**; the `_01`->`_02` handover sat at 0.541 of the bone where the joint "
    "the cloth was measured for is 0.839, and `_00` never led anywhere in the cloth at all. The same mistake is why "
    "the per-panel trace line reported 0.093 m of cloth for a 0.144 m panel - short by exactly `joints[1]`. The hem "
    "itself never moved differently (0.18618 m and 89.2 deg at the hem, before and after alike), so this was a "
    "**skinning** fault rather than a loss of curl, and the symptom would be the top of the panel - the section near "
    "the hips - following the hem bone; the user's report that the skirt bones move a section at her hips is "
    "probably this. After the fix: 7.8/36.3/55.9 across the three bones, the handover back on its joint (0.840 "
    "against 0.839), and a bend that ramps steadily (53.59, 68.66, 89.23 where it was 48.81, 78.14, 89.20). Found "
    "by `skirt_reshape_scale_check.py`, a new probe that reads a vertex's own position along the panel back against "
    "the bone that actually took it over; the prototype this module was rewritten from reads its geometry *before* "
    "its own reshape and so never had the bug. Same bones in the same places, so RIG_VERSION stays v6. Before that: "
    ": 2.33.3 - the reshape gets a row to tick. 2.33.2 shipped the feature with no `draw` row for it, and this "
    "operator's properties *are* the dialog, so a property nobody draws is a property nobody can set: the user ran "
    "the setup with the master on and the reshape was simply never offered. One row in `draw`, beside the master's. "
    "The rig is unchanged, so RIG_VERSION stays v6 - 2.33.2 built the same rig and only failed to ask for it. "
    "Before that: "
    ": 2.33.2 - a skirt panel with too few hinges to curl gets more (rig version v6). Maruzensky deforms each skirt "
    "panel through **one** bone plus a leaf entirely outside the cloth (0 vertices; the chain is 0.351 m where only "
    "0.234 m is cloth), so the panel swings as a slab - and every hem this project quotes is read at the tip of that "
    "leaf, overstating the cloth's own motion by about 1.85x. `operators/SkirtReshape.py` measures each panel's "
    "**cloth** (its own vertices projected onto the root bone: the cloth starts 13-43% of the way *down* it, so a "
    "subdivision over the whole bone would put its top joint above the skirt), then subdivides that stretch into "
    "three bones, reusing the leaf rather than deleting it so nothing that names a bone breaks, and shares each "
    "vertex's **existing** weight in that panel across the new bones by position - nothing invented, nothing else "
    "replaced, so the ring's cross-panel blending and the `Hip`/`Waist` waistband survive, and that blend is where "
    "the panel's only existing curve comes from. Behind *Reshape short skirt chains* (or `options.skirt_reshape`; "
    "`skirt_hinges` forces a count and `skirt_hinge_spacing_m` changes the one-per-this-much rule), off by default "
    "because it edits the model's own weights, and it runs before the chains are collected so the new bones reach "
    "the metarig - measured: the metarig follows a bone that did not exist before the run to 0.0000 m, the model at "
    "rest is unchanged to 2.5e-07 m, and the cloth's local bend goes from falling to rising (89.05 deg at the hem "
    "against 30.90 for the single hinge). `skirt_master_check.py` takes `reshape` as a second argument and is 20/0 "
    "on Maruzensky with it on. Before that: "
    ": 2.33.1 - the skirt master's lift turns each panel about its *own* axis (rig version v5). chou's `TRANSFORM` "
    "writes the owner's raw local X, and that is right for him for the same reason it was wrong for us: his panel "
    "roots are duplicates of the model's skeleton, while ours are rigify's FK controls, whose local X points within "
    "31 degrees of one *common* world direction for all ten panels while the panels' outward directions span a full "
    "turn. The measured symptom, straight from a user posing it: moving the master up moved the back panels outward "
    "and up (+0.0526, +0.0408 m) while the front panels went *inward* and barely rose (-0.0546, +0.0014 m) - a "
    "backwards curl rather than a flare, with the two places where neighbours disagreed being the two places the "
    "cloth stretched. A flare needs a rotation about each panel's own sideways axis, `outward x up`, which is a "
    "combination of the owner's X and Z rather than either alone; the axis is now expressed in the control's own "
    "rest frame and written as the **exact euler decomposition** of a 20 degree rotation about it, on all three of "
    "the owner's euler axes. Euler angles are an exact parameterisation, so at full travel the panel turns by exactly "
    "that rotation about exactly that axis. Measured after: **10 of 10 panels outward, 10 of 10 rising**, and every "
    "neighbouring pair's hem direction turning by exactly its own azimuth step - worst excess **0.0 deg**, where the "
    "raw-axis version had two boundaries at nearly 180. Two things that look like the same fix and are not: writing "
    "the axis's *components* as if they were angles is right only for small angles and ignores the composition "
    "order, and it leaves four of ten panels flaring inward; and a helper bone carrying the same rotation through "
    "`COPY_ROTATION` in either `LOCAL_OWNER_ORIENT` or `WORLD` does not deliver a rotation about the helper's world "
    "axis once the owner is parented - `_B`, whose axis is 0.1 deg from its own X, flips sign against a direct write "
    "of the same angle to the same axis. Before that: "
    ": 2.33.0 - the skirt master is aimed and driven the way chou's is (rig version v4). His master is not a "
    "rotation handle with a lift bolted on: every one of his 17 panel roots carries **two** constraints, and the "
    "shipped build had one of them. The second is a `TRANSFORM` that reads the master's **vertical travel** and "
    "writes it into the panel root's own X rotation - 4 degrees per centimetre over a +-5 cm window, saturating at "
    "+-20 degrees, `mix_mode_rot: ADD` so it stacks on the `COPY_ROTATION` instead of replacing it - and the master "
    "itself is clamped to 5 cm up and 5 mm down. Measured on our rig before it was ported: the master answered "
    "**0.00000 m** on all three of its own axes, and chou's own constraint, transplanted, is worth 0.18-0.19 m of "
    "hem per 5 cm. The frame had to change with it, and *which way* the frame points turned out to matter more than "
    "the direction. A bone's local Y is always its own length and turning a bone about its own axis is the default "
    "grab, so a master standing up made the natural motion the skirt's *twist* (0.11997 m against 0.26608 m on its "
    "local X). Aiming it along the character's front-to-back axis fixes that - but aimed *forward* the master's "
    "local X comes out at -X, and then everything the master drives arrives anti-parallel to the panels' own handles: "
    "measured, the handle's 25 degrees and the master's 30 together gave **5** deg per joint (`|30 - 25|`) where the "
    "shipped frame gives **55** and chou's own rig gives 55.35. Aimed backward the frame keeps local X = +X and "
    "Z = +Z (up), so the handle adds to the master again (55), the lift adds to the turn (30 + 20 -> 50), and chou's "
    "constraint numbers cross over unmodified. The master also moves from the first panel's control to the centre of "
    "the ring, which is only a gesture: both constraints read it in `LOCAL` spaces, so where it sits does not enter "
    "either copy. Before that: "
    ": 2.32.0 - the skirt gets one master control (rig version v3). The user asked for chou's skirt setup, and the "
    "measurement made it much smaller than the design note assumed: a skirt panel is already a rigify tail chain whose "
    "FK control drives a per-segment cascade that turns 30 degrees into a constant 30.0 degrees per joint (chou's own: "
    "~29.2), so there was no cascade to add. What his rig has and ours did not is one bone over every panel, and that "
    "is all this adds: `UMA_skirt_master`, linked to every skirt panel's own control (`COPY_ROTATION`, LOCAL -> "
    "LOCAL_OWNER_ORIENT, BEFORE - the space chou's file uses for the same job), off by default behind *Skirt master "
    "control* (or `options.skirt_master` in a rules file). Measured through the shipped switch "
    "(`skirt_master_check.py`, 11 checks): 10 of 10 panels move, 26.63-30.0 degrees per joint, a panel still answers "
    "its own control with the master turned (25 deg -> 0.222 m at 25.0 deg per joint), a `Wire only` re-run adds "
    "neither a second bone nor a second link, and with the switch off nothing is added. Nothing is added to the "
    "model's bones and no weight is touched. Before that: "
    ": 2.31.1 - the material copies stop climbing and start saying what they are. A model shaded three times used to "
    "end up with `.002`/`.003` on the same slot: each run copied the slot's material again and Blender handed the new "
    "copy the next free suffix while the old one still existed. A slot already wearing this shader's copy is reused "
    "now, and every copy is named `<the model's own name> [<part>]` - derived, so it cannot climb: three runs, "
    "identical names, the file's material count flat (`material_naming_check.py`, 9 checks). The `.001` suffixes "
    "*before* the first run are the model's own - a PMX may carry several materials with one name and `mmd_tools` "
    "disambiguates them at import (measured with `material_copies_check.py`). Before that: "
    ": 2.31.0 - the small requests, and the build tag stops being the rig's version. Pumpkin Rig has its own version "
    "now (`RIG_VERSION = \"v2\"`, right above `BUILD_TAG`, one edit to bump): the panel says `Pumpkin Rig Setup (v2)`, "
    "a message says `[Pumpkin Rig v2]`, and `BUILD_TAG` stays what it always was - the identifier the trace, the fleet "
    "probe and the stale-code guard read. The `Aim the Uma Sun at the key light` *setting* is gone (the user: *\"it's "
    "not used by anyone\"*) and the button that does it on demand stays; Pumpkin Rig moved directly under `Generate "
    "controller for umamusume`, because those two are the same job in two halves; and the community's `Pregnancy "
    "Toggle` sits under `Face Normal Transfer`, wired to nothing at all, as asked. Before that: "
    ": 2.30.1 - the run says where its own report is, and stops drawing Blender's cursor percentage. The user: *\"I "
    "couldn't see the progress bar or the steps logs in the console - is this intentional? I could only see the "
    "cursor change to some numbers.\"* The numbers *were* the progress bar: `wm.progress_begin/update` draws nothing "
    "but a numeric percentage at the mouse cursor, which reads as a glitch rather than as progress. It is off now "
    "(`Progress.CURSOR_BAR`, one constant, and the user's own blendcap add-on documents the same choice at "
    "`operators_retarget.py:787`), the status line carries the step, its note and the same percentage instead, the one "
    "step that cannot show progress says so in that line (\"the long one: no progress inside this step\"), and the "
    "trace opens by saying that the block *is* the whole run and that the same lines are printed to the console "
    "(Window > Toggle System Console) - which is where the missing step logs were, since a GUI-launched Windows "
    "Blender has no console at all. Before that: 2.30.0 - MikuMikuRig is no longer a dependency. The three things a FULL run used to need from it ship with "
    "this add-on now: the metarig template (`operators/MMR_Rig.blend`, byte-identical to MMR's own file in releases "
    "4.5-3.85 and 5.2.2 - sha256 `12374F03DAA8EBA5...`, 285,696 bytes), the 22 settings the rig pipeline reads "
    "(`operators/RigOptions.py`, MMR's own names, types and defaults, on `object.uma_mmr` and `PoseBone.uma_bone` "
    "instead of MMR's own properties so both add-ons can be installed at once), and the one MMR operator it called, "
    "now a function (`fill_default_weight_bone_parent`). Attribution and the list of modifications are in "
    "`THIRD_PARTY.md` (MikuMikuRig by 小峰峰哥 / XiaoFFGe, GPL-3.0 - the same licence as this add-on), and `LICENSE` "
    "ships beside it. Measured with MMR **disabled**: the run finishes and produces the very same rig - fingerprint "
    "`7a6199722a24c832c1e4e49eb3ff67ad`, identical to the 2.29.0 build with MMR installed and used "
    "(`mmr_freedom_check.py`, 16 checks), and the pre-vendoring build re-run from its own tag reproduces that hash "
    "too. The two refusal messages that named an MMR version are gone with the dependency. Before that: 2.29.0 - a "
    "rig run is up to 1.8x faster, with an identical rig, because rigify's per-bone mode switching is batched. "
    "Rigify copies a bone by creating it in edit mode, switching to object mode to copy its pose properties, "
    "and switching back - twice per bone, 204 bones, 408 dependency-graph flushes per run (`rigify/utils/bones.py`, "
    "*\"LEGACY ONLY, DON'T USE\"*, and `utils/__init__.py` binds the plain name every rig type imports to exactly that "
    "function). A flush costs what the file costs: measured, 7.9 ms in a file with no rig and 50-58 ms in one that "
    "already contains a finished rig, which is why a second character cost 3x (`PERFORMANCE.md` 3.1-3.3). "
    "`operators/RigifyCopyBatch.py` keeps the edit session open and copies the properties once per session instead - "
    "measured 408 switches -> 6, first character 21.00s -> **14.21s** (1.48x), second character in the same file "
    "63.89s -> **36.34s** (1.76x), and the two rigs identical by fingerprint (every bone, pose property and "
    "constraint, hashed - also identical for the two-character file at 4 armatures / 3494 bones / 2548 constraints). "
    "It watches for the end of an edit session through a proxy over `bpy.ops.object.mode_set`, because that is the "
    "only signal available - `sys.setprofile` cannot see operator calls at all (measured) - and if that proxy cannot "
    "be installed it patches nothing and says so rather than batching without a boundary. The switch is "
    "`Batch rigify's bone copies` in More settings, on by default, and the trace's `timings:` line carries the count "
    "of copies batched and switches avoided. Before that: 2.28.0 - both long operations now say what they are doing "
    "and how long each part of it took. `Shading` and "
    "`Pumpkin Rig Setup` declare their steps, time them, print a `timings:` line and drive the status bar and the "
    "window manager's progress bar while they run (`operators/Progress.py`), because the sign of life until now was "
    "that the window stopped answering. Measuring first is what made the rest possible: the rig run is 99% one call "
    "into MMR (`generate the rig` 21.2s of 21.5s here, and 64.3s of 64.6s for a second character - 3.0x), while a "
    "shading run was 82% *one Python loop* - the shape-key analysis that decides which morphs a separated face "
    "cannot use, 3.84s of 4.67s. That loop read `ShapeKeyPoint.co` a point at a time; it reads both arrays in bulk "
    "now and compares them with numpy, which is 11x faster on its own and takes a whole `Shading` run from 4.67s to "
    "**1.25s**. Before that: 2.27.0 - the Pumpkin Rig dialog opens on Simple and on the rules table, every time. "
    "Blender hands an operator its last-used property values and this operator's properties *are* the dialog, so a "
    "second model used to open on the first model's mode, with its per-family answers already in the rows - the "
    "user's *\"some settings are retained from the first run, e.g. tentacle type used for the hair and the "
    "extras\"*. A group whose families disagree now reads `Mixed` instead of its most common answer, and a click on "
    "that row writes the chosen type through to every family in it (measured: every group set to "
    "`basic.super_copy` leaves no non-fixed family in those groups on anything else) - which is what let the dialog "
    "stop forcing itself into Advanced, since that switch was the only thing standing between the old display and a "
    "silent flattening. The X-ray row and the preset search box are off the dialog (both properties stay, so a "
    "scene, a preset or a script still sets them), the dropdowns have one-word headers, and every tooltip here is "
    "two sentences or fewer. Before that: 2.18.0 - the chains follow the body the way the model has them, instead "
    "of being cut loose and pulled in "
    "world space. Every chain bone keeps its MMD parent and copies its own control's **local** rotation "
    "(`options.chain_link: local`, the new default; `world` restores the previous wiring), so the character can "
    "be moved by its root empty without the mesh tearing: measured 0.588386 m and 182 bones -> 0.000001 m and 0 "
    "bones, and a head turn moves a chain by the raw MMD rig's own ratio (tip/head 1.509) where the old wiring "
    "over-moved it by 1.53x (2.303). Every `basic.super_copy` and `spines.basic_tail` control moves the mesh by "
    "exactly the same amount as before, and `limbs.spline_tentacle` chains get a positional link instead. A "
    "spline chain's ORG pose sits off the model's rest position (0.015340 m on the reference model - the rig "
    "type not reproducing the chain exactly), so those families are reported in the trace as sitting off their "
    "rest position rather than failing the run's undisturbed-at-rest check. Hair following measured per family "
    "against the raw rig: all five families at ratio 1.000. Before that: 2.17.1 - the trace tells the truth about where the model's own bones are. The line that reports "
    "grouping them into the `MMD` collection said `visible now, switch it off` whatever `Chain Layers` said, "
    "and the default is `Nothing` - so on a default run it said the opposite of the truth about the one "
    "collection holding the user's own skeleton, which is what turned 'where did my face bones go' into a "
    "support question. Measured on the reference model: 106 face bones in, 106 out, none removed, only "
    "`Eye_L`/`Eye_R` carry a constraint, and every rest position unchanged in world space - they are hidden, "
    "not gone, in two places: MMR hides the armature object and the `MMD` collection is off because `Chain "
    "Layers` is `Nothing`. The line now names which of the two states it is in and how to switch it back. "
    "Also measured while answering it: the rigify face rig (Face / Face (Primary) / Face (Secondary) = 88 "
    "bones) is generated but wired to nothing - the model's constraints point at 147 rig bones and none is "
    "face-related, and the mesh has no vertex group named after a rig-only bone - because MMR's mapping table "
    "is 54 body and finger bones plus the eyes. The model's own face bones are still the ones the skin "
    "follows, so posing them by hand works exactly as it did before the run. "
    "Before that: 2.17.0 - a per-bone family keeps the model's own hierarchy, so its controls cascade. With every "
    "family set to `basic.super_copy`, grabbing the first control of a hair chain moved that one bone and "
    "nothing else: a rule's `parent` ('the hair hangs off the head') is a statement about the *family*, and "
    "a per-bone family makes every bone its own one-bone chain, so all six bones took the chain-root path and "
    "were attached to `spine.006` - their controls came out as siblings (measured: turning `Sp_He_Hair2_L_00` "
    "30 degrees moved `_01` by 0.000000 m, while `Sp_Hi_MSkirt0_B` - whose rule names no parent - cascaded "
    "0.061816 m then 0.123632 m in the same run). Nine of the eleven typed shipped rules name a parent. The "
    "model's own hierarchy is now tried first, and only when the model parent is a bone this same run adds, so "
    "a chain root whose model parent is a body part still lands where the rule says. After it, the same turn "
    "moves `_01` 0.041799 m and `_02` 0.071755 m, each model bone following its own ORG. Under the shipped "
    "rules the only thing that changes is four sub-bones (`Sp_Ch_Ribbon0_F_01`, `Sp_He_Hair0_C_01`, "
    "`Sp_He_Hair3_L/R_01`): attached to their own chain instead of the chest or the head, with all 19 of the "
    "run's own checks and all 43 chains identical (attachment_trace_dump.py, A/B). "
    "Before that: 2.16.0 - a preset is keyed to nothing. The 99 presets read out of the animator's own rigify "
    "metarigs (one per character and costume, keyed `1004_00`, `source: Meta_Base_Head_...`) were "
    "autodetected from the model's `Chara_<id>_<costume>_Handle` bone or its hair skeleton, and their "
    "answers beat the rules table - which is the second half of the reported mixed `super_copy` "
    "result: on `1004_00` the preset had already seeded the hair families, and nothing in the panel "
    "said so. Autodetection is gone, the `preset_cross_costume` option is gone with it, and the "
    "derived files are moved whole into `<config>/uma_addon/presets_retired_character/` (nothing "
    "deleted; anything that says `saved in Blender` is left alone). In their place a preset can now "
    "answer by *group* as well as by family, so a fresh install gets two small generic ones - `One "
    "control per bone` (every group `basic.super_copy`, the shape of the raw MMD rig) and `Chains "
    "everywhere` - and the rules table is what a run uses until somebody picks a preset, which is "
    "what the list opens on. Measured in presets_no_character_check.py: 17 of 17 checks, including "
    "that the identity bone still reads `1004_00` and the preset keyed that way is in the list and "
    "is *not* selected for it. Before that: 2.15.0 - a family choice made in the setup now beats a pinned family. `fixed_families` "
    "('the ears are always a chain') used to discard it outright, which is how setting every "
    "family to `basic.super_copy` silently left the ears as a chain - the reported mixed result, "
    "where some bones answer and some do not. With nothing asked for, the pin still decides (proved "
    "in supercopy_precedence_check.py, case A), and an override is named in the trace instead of "
    "being silent. Nothing else changed: a `super_copy` family already types every bone it governs, "
    "one control each (RigifyChains.py:2201-2206), so the per-bone controls the plan needs were "
    "already there - measured 16 of 16 controls moving their own ORG and every control with bones "
    "below it moving them 30.0 degrees. Before that: 2.14.0 - the face-normal step separates the face into its own object before the transfer, "
    "which is the shape Spork's own file has and the fix for the reported cost: +1.4 ms per "
    "evaluation on a separated face against +12.5 ms on the whole 12,450-vertex mesh, where limiting "
    "the transfer with a vertex group (which stays as the fallback) saved 0.9 ms of that. Bit-exact "
    "under a 30 degree head turn, a 15 degree rig turn and a rig move - rest, deformation and "
    "return-to-rest all 0.00e+00 m - and the separated piece keeps the parent, collections, slots, "
    "vertex groups and armature modifier, so the hierarchy and the seam are untouched. Both pieces "
    "also drop the shape keys that cannot move them, which is the reported 'two sets of identical "
    "morphs in the viewer': 118 keys off the body, 36 off the face, no name lost. Before that: "
    "2.13.1 - the Physics & Action panel stops writing scene properties while it draws (Blender "
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
print("[uma_rigify_chains] %s loaded (build %s)" % (RIG_TAG, BUILD_TAG))

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

#: **The skirt families, in code, because the rules table is only a seed** (2.33.9). `load_rules` reads
#: `<config>/uma_addon/rigify_chains.json` and, when that file carries any rules at all, uses **its table instead of
#: the shipped one** - deliberately, so a hand-edited table is never overwritten. The consequence is easy to miss and
#: it bit this build: adding the skirt rules below to `DEFAULT_RULES` changed nothing on any install that already had
#: a rules file. Measured on the development machine, whose config table is 13 rules written before these existed:
#: `Sp_Wa_MSkirt0` stayed a `basic.super_copy` stub in the `Skirt` collection and the run still reported
#: `skirt master: skipped - 0 skirt panel(s) with a control of their own to drive (2 needed)`.
#:
#: So a skirt-named family that *no* rule in the table in use names is answered here instead - and the answer is the
#: same rule the table carries, so a fresh install and an old one build the same rig. The trace names the source, so
#: which of the two answered is never a guess: `the rules table` against `the skirt family list`.
SKIRT_FAMILY_PREFIXES = ("Sp_Hi_MSkirt", "Sp_Wa_MSkirt", "Sp_Si_MSkirt", "Sp_Hi_CSkirt", "Sp_Hi_Hem")
#: The rule a skirt-named family gets when nothing in the table names it. The same type, collection and chain
#: settings as the rules in `DEFAULT_RULES` below - `spines.basic_tail` is what makes it a *chain*, and a chain is
#: what the skirt master needs: it looks for a panel's own `_master` control, and a `basic.super_copy` family has one
#: control per bone and no cascade, so the master finds no panel to drive however the family is grouped.
SKIRT_FAMILY_ANSWER = {
    "type": "spines.basic_tail",
    "collection": "Skirt",
    "min_bones": 2,
    "parameters": {"copy_rotation_axes": [True, True, True]},
    "chain_deform": "bbone",
}
#: The `Tp_*`/`Pc_*` families that share the skirt *names* while being single root bones with no `_Handle`
#: companions. They must not match, and they do not: this list is all `Sp_`.
SKIRT_FAMILY_PREFIX_NOT = ("Tp_", "Pc_")


def skirt_family(family):
    """Is this family a skirt panel, by the name list above? (`Tp_`/`Pc_` roots aside.)"""
    name = family or ""
    if any(name.startswith(prefix) for prefix in SKIRT_FAMILY_PREFIX_NOT):
        return False
    return any(name.startswith(prefix) for prefix in SKIRT_FAMILY_PREFIXES)


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
        # The other skirt families a real uma actually ships (2.33.9). The rule above was the only one, so every
        # model whose plate skirt is named differently fell through to the `unmatched_families` fallback - flat
        # `basic.super_copy` stubs, one control per bone, no chain - and to no skirt master, because the master
        # selects panels by **the rule's** collection rather than by the group a family is filed under. Measured over
        # the 125-model pool: 20 of them have a skirt family in this state (`Sp_Hi_CSkirt0` 9 models, `Sp_Wa_MSkirt1`
        # 5, `Sp_Hi_CSkirt1` 2, and one each of `Sp_Wa_MSkirt0`, `Sp_Si_MSkirt0`, `Sp_Hi_Hem0`, `Tp_Hi_MSkirt0`,
        # `Tp_Hi_SkirtRoot0`, `Tp_Hi_MSkirtRoot0`, `Pc_Wa_MSkirt1`). The four patterns below are the ones that are
        # shaped like panels - a chain of `_NN` deform bones with `_NN_Handle` companions, 2 or more deep:
        #
        #   Sp_Wa_MSkirt1_L   -> _00, _01 (+ handles)      a waisted skirt        (1070_00, 1001_02)
        #   Sp_Hi_CSkirt0_B   -> _00.._03 (+ handles)      a plate / circle skirt (1025_00, 1014_00)
        #   Sp_Si_MSkirt0_B   -> _00, _01 (+ handles)      a side skirt           (1062_90)
        #   Sp_Hi_Hem0_F      -> _00, _01 (+ handles)      a hem                  (1032_90)
        #
        # **Deliberately not included**, and this is why the list is patterns rather than `^Sp_.*[Ss]kirt`: the
        # `Tp_*` and `Pc_*` families of the same name are *roots*, one or two bones with no `_Handle` companions
        # (`Tp_Hi_MSkirt0_FL_00`, `Pc_Wa_MSkirt1_FL_00`, `Tp_Hi_SkirtRoot0_FL_00`), so a chain rule would invent a
        # panel where the model has an attachment point. `min_bones: 2` would refuse most of them anyway, which is
        # exactly the kind of accidental safety this table should not rely on.
        {"pattern": "^Sp_Wa_MSkirt", "type": "spines.basic_tail", "collection": "Skirt", "min_bones": 2,
         "parameters": {"copy_rotation_axes": [True, True, True]}, "chain_deform": "bbone"},
        {"pattern": "^Sp_Hi_CSkirt", "type": "spines.basic_tail", "collection": "Skirt", "min_bones": 2,
         "parameters": {"copy_rotation_axes": [True, True, True]}, "chain_deform": "bbone"},
        {"pattern": "^Sp_Si_MSkirt", "type": "spines.basic_tail", "collection": "Skirt", "min_bones": 2,
         "parameters": {"copy_rotation_axes": [True, True, True]}, "chain_deform": "bbone"},
        {"pattern": "^Sp_Hi_Hem", "type": "spines.basic_tail", "collection": "Skirt", "min_bones": 2,
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
        # How the model's own chain bones are made to follow the rig (handoff 9bk). `local` keeps the model's own
        # MMD hierarchy - nothing is released - and gives each chain bone a copy of its **control's own local
        # rotation**, which is the character-move-safe link: measured, moving the character by its root empty
        # tears the mesh by 0.000001 m against 0.588386 m with the world-space wiring, and the chain follows the
        # body exactly as the raw MMD rig does (tip/head 1.509, the raw rig's own ratio) where the world-space
        # wiring over-moves it by 1.53x. `limbs.spline_tentacle` chains keep a positional link because their
        # motion is positional (the curve re-shapes the chain): they get a POSE-space COPY_TRANSFORMS, which
        # carries that reshaping without letting the object's own transform in - measured, the alternative
        # (rotation only) drops the spline's `_master` control from 0.048272 m to 0.000000 m.
        # `world` is the previous wiring: release every chain bone from its MMD parent and follow the ORG copy in
        # world space. Kept as the escape hatch, and what the fleet was baselined on.
        #
        # **Why `local` is implemented but not yet the default** (handoff 9bl): it is right for every
        # rotation-driven rig type - measured, the stretch under a root-empty move falls from 0.588386 m and 182
        # bones to 0.000001 m and 0 bones, the chain follows the body at the raw rig's own ratio (tip/head 1.509
        # against 2.303 for the world wiring), and every `basic.super_copy` and `spines.basic_tail` control moves
        # the mesh by exactly the same amount as before. A `limbs.spline_tentacle` chain is the exception and it is
        # structural: its motion is positional (the curve re-shapes the ORG chain), and rigify's spline rig does
        # not reproduce the model's chain exactly - the ORG's *pose* sits 0.01534 m from the model's joint on the
        # reference model (up to 0.13624 m on character 1001), which is the rig type's own error, not placement.
        # Carrying that motion needs a link that *copies* the ORG (which then also copies that gap: 8 bones off
        # their rest by 15 mm), while every delta-form link that avoids the gap gains the two armatures' own
        # motion in this Blender and tears the mesh by 2.32 m. Six link variants were measured; the numbers are in
        # the handoff. Until that is decided, the default stays what it was.
        "chain_link": "local",
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


#: The backup a housekeeping pass leaves beside the rules file. Same name the offline `rules_housekeeping.py` uses,
#: so a file brings its own history whichever tool touched it last; a second pass in the same minute gets a stamp.
HOUSEKEEPING_BACKUP = "rigify_chains.before_housekeeping.json"


def housekeep_rules_file(trace=None, path=None):
    """Bring the rules file **in use** up to date with the table this build ships. Returns a summary dict.

    Why this exists (2.33.10): `load_rules` reads `<config>/uma_addon/rigify_chains.json` and, when that file carries
    any rules at all, uses **its table instead of the shipped one** - deliberately, so a hand-edited table is never
    overwritten behind the user's back. The cost was invisible until it bit: a rule added to `DEFAULT_RULES` reached
    **nobody who already had a rules file**. 2.33.9 put four skirt rules in the shipped table and they did nothing on
    the development machine, whose config table was thirteen rules written a week earlier - the draw rows, the chain
    types and the skirt master all still read the old table, and the answer had to be duplicated in code
    (`SKIRT_FAMILY_ANSWER`) to reach an install at all.

    The rule this pass follows, decided by the user: **a pattern we ship is ours**. So:

    * a shipped pattern the file is missing is **added**, in front of the first bare `skip` rule (the `^Sp_`
      catch-all), or the catch-all would claim those bones first;
    * a shipped pattern the file already carries **is refreshed** when the value differs, and the value it replaced
      is written into the note below, so the change is recoverable and explained rather than silent;
    * a pattern the file carries and we do not ship is **left exactly as it is** - that is the user's own rule;
    * `options` keys we ship and the file lacks are **filled in** and existing ones are never overwritten: those are
      the user's choices (`chain_link`, `resample`, `never_rig`, ...), not our defaults;
    * a backup is written beside the file before anything is saved.

    Three deliberate refusals. A file with **no `rules` array at all** is left alone - `load_rules` only uses a file's
    table when it has one, so adding rules there would flip a run from the built-in table to a frozen copy of it.
    An **explicitly passed `Rules_File` is never touched** (the caller checks; a probe's own table or a user's
    per-character one must survive), and neither is a file that will not parse. And it never raises: a rules file
    that cannot be written is a message, not a failed rig run.
    """
    path = path or config_path()
    if not os.path.isfile(path):
        return {"path": path, "changed": False, "why": "no rules file - the built-in table is in use"}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        return {"path": path, "changed": False, "why": "it would not parse (%s)" % error}
    if not isinstance(data, dict):
        return {"path": path, "changed": False, "why": "it is not a JSON object"}
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        return {"path": path, "changed": False,
                "why": "it carries no rules array, so the built-in table is what a run uses"}

    shipped = {rule["pattern"]: rule for rule in DEFAULT_RULES["rules"] if rule.get("pattern")}
    present = {str(rule.get("pattern")): rule for rule in rules if isinstance(rule, dict)}

    added, refreshed = [], []
    for pattern, rule in shipped.items():
        current = present.get(pattern)
        if current is None:
            added.append(pattern)
        elif json.dumps(current, sort_keys=True) != json.dumps(rule, sort_keys=True):
            # `deepcopy` on the *was* value rather than a reference to it: `current` is the very dict the two lines
            # below clear and refill, so a reference recorded here is empty by the time the note is written and the
            # note reads "which the file had as {}" - measured in this pass's own edge-case test, and the same class
            # of aliasing that the `parameters` copy below exists to avoid.
            refreshed.append({"pattern": pattern, "was": copy.deepcopy(current)})
            current.clear()
            current.update(copy.deepcopy(rule))
    if added:
        # **After the leading skips and before the catch-all** - both halves matter, and the first version of this
        # got the first one wrong:
        #
        # * the shipped table opens with a bare `_Handle$` skip whose entire job is to keep handle bones out of the
        #   chains. A new rule inserted ahead of it claims `Sp_Wa_MSkirt0_BBL_00_Handle` for the skirt chain - the
        #   exact mistake this table's own comment records, caught then as `worst 0.01000 m at Ear_03_L_Handle`. The
        #   resolver also only treats the *leading* run of skips as leading, so a non-skip rule at index 0 demotes
        #   `_Handle$` and the resolved rules land in front of it;
        # * a rule inserted after the trailing `^Sp_` catch-all is never reached at all.
        #
        # So: walk past the leading skips, then stop at the first bare skip that comes after a typed rule.
        index = len(rules)
        seen_typed = False
        for position, current in enumerate(rules):
            if not isinstance(current, dict):
                continue
            if current.get("skip") and not current.get("type"):
                if seen_typed:
                    index = position
                    break
            else:
                seen_typed = True
        for pattern in added:
            rules.insert(index, copy.deepcopy(shipped[pattern]))
            index += 1

    filled = [key for key, value in DEFAULT_RULES["options"].items() if key not in (data.get("options") or {})]
    if filled:
        options = data.setdefault("options", {})
        for key in filled:
            options[key] = copy.deepcopy(DEFAULT_RULES["options"][key])

    if not (added or refreshed or filled):
        return {"path": path, "changed": False, "why": "already current", "rules": len(rules)}

    backup = os.path.join(os.path.dirname(path), HOUSEKEEPING_BACKUP)
    if os.path.exists(backup):
        backup = os.path.join(os.path.dirname(path),
                              HOUSEKEEPING_BACKUP.replace(".json", "_%s.json" % time.strftime("%Y%m%d_%H%M%S")))
    try:
        shutil.copy2(path, backup)
    except OSError as error:
        return {"path": path, "changed": False, "why": "the backup could not be written (%s)" % error}

    lines = ["%s: brought up to date with this build's table." % time.strftime("%Y-%m-%d")]
    if added:
        lines.append("Added (a rule this build ships and the file did not have): %s."
                     % ", ".join("`%s`" % pattern for pattern in added))
    for entry in refreshed:
        lines.append("Refreshed `%s`, which the file had as %s."
                     % (entry["pattern"], json.dumps(entry["was"], sort_keys=True)))
    if filled:
        lines.append("Filled in options the file was missing: %s." % ", ".join("`%s`" % key for key in filled))
    lines.append("Nothing else was touched - options already in the file and rules this build does not ship are "
                 "yours and are left as they are. Backup: %s." % os.path.basename(backup))
    data["housekeeping_rules_table"] = " ".join(lines)
    data["version"] = DEFAULT_RULES.get("version", data.get("version"))
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except OSError as error:
        return {"path": path, "changed": False, "why": "it could not be written (%s)" % error}

    summary = {"path": path, "changed": True, "added": added, "refreshed": [e["pattern"] for e in refreshed],
               "filled": filled, "backup": backup, "rules": len(rules)}
    if trace is not None:
        trace.info("rules file: brought up to date (%s) - added %s, refreshed %s, filled %s; backup %s"
                   % (os.path.basename(path), ", ".join(added) or "none", ", ".join(summary["refreshed"]) or "none",
                      ", ".join(filled) or "none", os.path.basename(backup)))
    else:
        print("[rigify_chains] rules file %s brought up to date: added %s, refreshed %s, filled %s (backup %s)"
              % (os.path.basename(path), ", ".join(added) or "none", ", ".join(summary["refreshed"]) or "none",
                 ", ".join(filled) or "none", os.path.basename(backup)))
    return summary


# --------------------------------------------------------------------------------------------------------
# The groups, the families, and the presets (handoff 9y)
#
# The user chooses one rig type per **group** in Simple mode and one per **family** in Advanced mode, and a
# **preset** fills those choices in when it is picked. Nothing else decides anything: the rules table is what the
# `Default` entry is, and a named preset is a set of answers somebody saved. That is the whole model - there is no
# "match his rig, which means his rig or the table depending on the character" left, and since `chains-56` there is
# no autodetection either: the animator's own rigs are no longer read at run time (handoff 9bd).
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
    ("Sp_Si_MSkirt", "Skirt"),   # a side skirt: `Sp_Si_MSkirt0_B_00`, 2 bones deep (1062_90). Paired with the rules
    ("Sp_Hi_CSkirt", "Skirt"),   # a plate / circle skirt: `Sp_Hi_CSkirt0_B_00`, 2-4 bones deep (9 models in the pool)
    ("Sp_Hi_Hem", "Skirt"),      # a hem: `Sp_Hi_Hem0_F_00`, 2 bones deep (1032_90)
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
#
# The tooltips are one line each on purpose (the user: *"trim down the tooltips... keep it dry, two sentences
# max"*); what a type *does* is rigify's own documentation, and what it does *here* is in the module docstring and
# in the trace of a run.
TYPE_CHOICES = [
    (MODE_OFF, "none", "No control of its own: the bones keep MMR's behaviour"),
    (MODE_STUB, "basic.super_copy", "One control per bone, no chain"),
    (MODE_TAIL, "spines.basic_tail", "A chain per root with tweak controls, curling when a control is pushed"),
    (MODE_SPLINE, "limbs.spline_tentacle", "A chain driven by a spline, posed at its `_master` bone"),
]

#: The group rows' own list: the same four types plus `Mixed`, which is what a group shows when its families hold
#: more than one answer. `Mixed` is a *display* state and never a choice - it is deliberately absent from
#: `TYPE_CHOICES`, so a family row cannot be set to it, and `adopt_group_edits` ignores it rather than writing it
#: through to the families. Before this existed a group with two answers was shown as its *most common* one, and the
#: only thing standing between that and a silent flattening was the dialog forcing itself open on Advanced - which
#: is what the user reported ("Advanced mode is always selected whenever pumpkin rig is run").
MODE_MIXED = "MIXED"
GROUP_CHOICES = TYPE_CHOICES + [
    (MODE_MIXED, "Mixed", "This group's families are set individually - `Edit per family` shows them one by one"),
]

SIMPLE_GROUPS = ("Hair", "Skirt", "Chest", "Tail", "Extras", "Other")
PRESET_DEFAULT = "default"
PRESET_CUSTOM = "custom"


# The presets and the rules table are read from disk while the dialog is open - `preset_items` builds the enum
# on every redraw, `sync_preset` and `preset_mismatch` run in `draw` - and reading one JSON file per preset each
# time made clicking the preset list stutter (99 of them, when the character-keyed ones were still here). Both are
# cached against a stamp of the folder (file count + its mtime), and anything that writes a preset clears the
# cache, so the only case the stamp misses is a file edited outside Blender, which `Reload presets` covers.
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


def presets_retired_dir():
    """Where the character-keyed presets were moved, once: next to the presets folder, not inside it.

    Inside would be wrong: `preset_files` reads every `*.json` in the presets folder, so a subfolder of retired
    presets would be one mis-placed file away from being in the list again. It is a sibling, and nothing reads it.
    """
    return os.path.join(bpy.utils.user_resource("CONFIG", path="uma_addon", create=True),
                        "presets_retired_character")


PRESET_README = """\
Presets for the uma_addon Pumpkin Rig Setup
===========================================

One file per preset, and a preset is nothing but a saved set of answers: a rig type per bone group
and/or per bone family, written down so it can be picked again.

What a preset is
----------------
    "groups": {
      "Hair":  "basic.super_copy",      one control per bone, no chain
      "Skirt": "spines.basic_tail",     a chain that curls when a control is pushed
      "Tail":  "limbs.spline_tentacle"  a chain driven by a spline
    },
    "families": {
      "Sp_He_Hair4": "limbs.spline_tentacle"    one family said on its own
    }

`groups` answers for every family in that group, `families` answers for one family, so a small
generic preset (three lines) and a specific one (twenty) are written the same way. Where both name
the same family, `families` wins - the specific answer is the one somebody bothered to write. The
groups are Hair, Skirt, Chest, Tail, Extras and Other; a family's group is derived from its own
name, and Advanced mode shows every family in the model under those headings.

Picking a preset in Blender fills the rig type fields and stops there - what the panel shows is
what the rig gets. Nothing picks a preset for you: a run uses the rules table in
../rigify_chains.json until you choose one, which is what the "Default" entry at the top of the
list means. Ears have no field, because the animator's own rigs never vary them (241 of 241
measured) - so they are fixed in the operator and no preset can change them.

Making one
----------
Set the fields in Blender and press "Save as preset", or copy a file here and edit it. The file
name is not significant - the `key` inside it is - and a file that will not parse is skipped with
a line on the console rather than breaking the list.

The folder next to this one
---------------------------
`../rigify_chains.json` is the *rules table*: bone name patterns -> rig type, plus the options
a run uses. That is what answers for every model nobody has saved a preset for, so it is where a
change that should apply to every model belongs.
"""


def write_preset_readme():
    """A plain-language note in the folder itself, so it explains itself to whoever opens it.

    Rewritten when the text differs rather than only when it is missing: it is this add-on's own documentation
    sitting in the user's folder, and the one that described character-keyed presets was still there after they
    were retired - a folder that documents a feature that no longer exists is worse than no note at all. Anything
    else in the folder is left alone.
    """
    path = os.path.join(preset_dir(), "README.txt")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as handle:
                if handle.read() == PRESET_README:
                    return path
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
        if not isinstance(entry, dict):
            continue
        # a preset answers by group, by family, or both - the shape check has to accept either, or a small
        # generic preset ("three groups, all basic.super_copy") would be skipped as an unreadable file
        if not (isinstance(entry.get("groups"), dict) or isinstance(entry.get("families"), dict)):
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


# The presets a fresh install starts with, written by `ensure_preset_files` the first time an operator runs -
# the same shape as the shipped style (RigStyles), one file each, and the operator works without them.
#
# They are keyed by *group* rather than by family or character, which is what makes them generic: `Hair` means the
# same thing on every model, and a model that has no `Hair` family simply has nothing for that answer to apply to.
# Both of these are things the user asked for by name in the rework - "wrap the physics bones with super_copy so
# the user can control them like they would the mmd rig" is the first one, spelled as a preset.
SHIPPED_PRESETS = {
    "One control per bone": {
        "note": "basic.super_copy for every physics family: one control per bone, no chain - the shape the raw "
                "MMD rig has, with the controls on the rigify rig instead. This is the preset for 'I want to pose "
                "the physics the way I do on the MMD rig'",
        "groups": {group: "basic.super_copy" for group in SIMPLE_GROUPS},
    },
    "Chains everywhere": {
        "note": "spines.basic_tail for every physics family: one chain per family with tweak controls, curling "
                "when a control is pushed. The other end of the same choice",
        "groups": {group: "spines.basic_tail" for group in SIMPLE_GROUPS},
    },
}


def is_derived_preset(entry):
    """Whether this preset is one of the character-keyed ones derived from the animator's own metarigs.

    Measured on the 99 files this machine had: 69 were keyed to a character (`1004_00`, with `character`,
    `costume` and `signature`) and 30 could not be keyed at all - their key is the metarig's own name
    (`Meta_Base_Head_Mejiro_McQueen_Fair_Lady_of_The_Waves`) and they carry a `reason` saying why not
    (`"costume 'fairladyofthewaves' cannot be keyed by name"`, `"character name did not match"`). Both kinds are
    the animator's rigs rather than the user's choices, so both are retired.

    A preset saved in Blender is never caught by this: older builds wrote `"source": "saved in Blender"` and this
    one writes no `source` at all. The point is to put the animator's rigs aside, not the user's own answers.
    """
    if any(entry.get(field) for field in ("character", "costume", "signature", "reason")):
        return True
    source = str(entry.get("source") or "")
    return bool(source) and source != "saved in Blender"


def ensure_preset_files(trace=None):
    """Housekeeping in the presets folder, once per run: write the shipped presets, retire character-keyed ones.

    Called from `execute`, never from `draw`: it writes and moves files, and `preset_items` runs on every redraw.
    Two separate jobs, both of them one-time by construction (each checks the destination before writing):

    * the two generic presets in `SHIPPED_PRESETS` are written if their files are not there, so a fresh install's
      preset list is not empty and the feature is visible without reading a document;
    * presets derived from the animator's own rigs (`is_derived_preset`: keyed by character and costume, or by the
      metarig's own name when no character could be matched) are *moved* out of the list, whole files, into
      `<config>/uma_addon/presets_retired_character/` - rather than left in the list as 99 entries that look like
      choices. Nothing is deleted and nothing is rewritten, so a file moved by mistake can be moved straight back.
    """
    written = 0
    write_preset_readme()
    for key, entry in SHIPPED_PRESETS.items():
        path = os.path.join(preset_dir(), preset_filename(key) + ".json")
        if os.path.exists(path):
            continue
        try:
            save_preset(key, entry)
            written += 1
        except (OSError, ValueError) as error:
            if trace is not None:
                trace.info("could not write the preset %r: %s" % (key, error))
    if written and trace is not None:
        trace.info("%d shipped preset(s) written into %s: %s"
                   % (written, preset_dir(), ", ".join(sorted(SHIPPED_PRESETS))))
    moved = retire_character_presets(trace)
    return written + moved


def retire_character_presets(trace=None):
    """Move the character-keyed presets out of the preset list, once. Returns how many files moved.

    Which files those are is `is_derived_preset`'s answer, and they are moved to a sibling folder rather than
    deleted: the user asked for them gone from the list, not gone from the disk, and a moved file is a copy-paste
    away from being a preset again.
    """
    retired = {}
    for key, entry in preset_files().items():
        if not is_derived_preset(entry):
            continue
        source = entry.get("file")
        if source and os.path.isfile(source):
            retired[key] = source
    if not retired:
        return 0
    folder = presets_retired_dir()
    moved = 0
    for key, source in sorted(retired.items()):
        try:
            if not os.path.isdir(folder):
                os.makedirs(folder)
            shutil.move(source, os.path.join(folder, os.path.basename(source)))
            moved += 1
        except OSError as error:
            if trace is not None:
                trace.info("could not move the preset %r out of the list: %s" % (key, error))
    if moved:
        forget_presets()
        if trace is not None:
            trace.info("%d character-keyed preset(s) moved out of %s into %s - presets are not keyed to a "
                       "character any more, so nothing would have read them; nothing was deleted"
                       % (moved, preset_dir(), folder))
    return moved


def load_profiles(force=False):
    """Every preset, as `{key: entry}`. Read-only, cached, never raises.

    One file per preset in `<Blender config>/uma_addon/presets/`: a set of answers belongs in its own small file
    that can be added, edited, copied to someone else or deleted on its own, rather than as a key inside the rules
    file next to the pattern table and the run options.

    Cached against `preset_stamp()`, because this is called from `draw` (`preset_items`, `sync_preset`,
    `preset_mismatch`) and reading one file per preset on every redraw made the preset list stutter.
    """
    stamp = preset_stamp()
    # `_PRESET_CACHE["loaded"]` rather than a truthiness test on the dict: "no presets yet" is a valid answer that
    # an empty dict also spells, so on a fresh install every redraw re-listed the folder - the stutter this cache
    # exists to prevent (code review, handoff 9ai).
    if not force and _PRESET_CACHE["loaded"] and _PRESET_CACHE["stamp"] == stamp:
        return _PRESET_CACHE["presets"]
    presets = preset_files()
    _PRESET_CACHE["stamp"] = stamp
    _PRESET_CACHE["presets"] = presets
    _PRESET_CACHE["loaded"] = True
    return presets



def preset_label(key, entry):
    """What to call a preset on screen: its own `name`, else its key.

    The key is a human-readable name now (`One control per bone`), because nothing keys a preset to a character
    any more - the derived ones were keyed `1004_00` and needed a `source` metarig name to be readable at all.
    """
    return str(entry.get("name") or key)


def describe_preset(entry):
    """One line about what a preset answers: `3 groups, 2 families`, for the dropdown's tooltip."""
    groups = len(entry.get("groups") or {})
    families = len(entry.get("families") or {})
    parts = []
    if groups:
        parts.append("%d group%s" % (groups, "s" if groups != 1 else ""))
    if families:
        parts.append("%d famil%s" % (families, "y" if families == 1 else "ies"))
    return ", ".join(parts) or "no answers"


def preset_answers(entry, families):
    """A preset's answers for *this* model's families: `({family: MODE_*}, [unmapped names])`.

    `groups` answers for every family in that group and `families` for one family, so a small generic preset
    (three groups) and a specific one (twenty families) are written the same way and applied by the same code.
    Where both name the same family, `families` wins: the specific answer is the one somebody bothered to write.

    Only families the model actually has are answered - a preset cannot name a family that did not exist when it
    was made, and an answer for a family that is not here is not a change. A rig type no mode maps to is reported
    rather than guessed at, and answered as a stub, which is the type that cannot invent a chain where the model
    has none.
    """
    def mode_of(where, rig_type):
        mode = TYPE_MODES.get(rig_type)
        if mode is None:
            unknown.append("%s=%s" % (where, rig_type))
            return MODE_STUB
        return mode

    unknown = []
    by_family = {family: mode_of(family, rig_type)
                 for family, rig_type in (entry.get("families") or {}).items()}
    by_group = {group: mode_of(group, rig_type)
                for group, rig_type in (entry.get("groups") or {}).items()}
    answers = {}
    for item in families:
        if item.family in by_family:
            answers[item.family] = by_family[item.family]
        elif item.group in by_group:
            answers[item.family] = by_group[item.group]
    return answers, unknown


def preset_items(self, context):
    """The `Preset` dropdown: `Default` (the rules table), `Custom`, then every preset in the folder.

    A callback because the list is the folder's, and the folder can change while the dialog is open. Blender
    requires an *index* default when `items` is a callback, so `default=0` - `Default` is always first and it is
    the rules table's own answers.

    No preset is sorted to the front or picked by the model: the list is alphabetical and the run uses `Default`
    until somebody chooses otherwise (`chains-56` took the character-keyed autodetection out - handoff 9bd).

    `Preset_Filter` narrows the list (Blender has no search on an enum), matching anywhere in the name or the key.
    The current selection is always kept in the list, so narrowing cannot silently change what the run will use.
    """
    items = [(PRESET_DEFAULT, "Default (rules table)", "The rules table's own typing for every family - what a "
                                                       "model gets until you choose otherwise"),
             (PRESET_CUSTOM, "Custom", "Whatever the fields below currently say")]
    try:
        presets = load_profiles()
    except Exception as error:  # noqa: BLE001
        # A dropdown must not raise, but it must not be silent either: this is the "the preset list is empty and
        # nothing says why" case, and a line on the console costs nothing (code review, handoff 9ai).
        presets = {}
        print("[rigify_chains] the preset list could not be read (%s: %s); the dropdown shows Default only"
              % (type(error).__name__, error))
    needle = (getattr(self, "Preset_Filter", "") or "").strip().lower()
    current = getattr(self, "Preset_Key", None)
    shown = 0
    for key in sorted(presets):
        entry = presets[key]
        label = preset_label(key, entry)
        if needle and needle not in ("%s %s" % (label, key)).lower() and key != current:
            continue
        # what it answers, and what it is for: a preset is a choice somebody made, so its own note is the useful
        # tooltip when it has one
        detail = entry.get("note") or describe_preset(entry)
        items.append((key, "%s  %s" % (label, describe_preset(entry)), detail))
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
        # basic_tail chain and the tail always a spline. **Unless the setup asks for something else**: the pin was
        # absolute, which is how "set every family to `basic.super_copy`" silently left the ears as a chain - the
        # user's own report, and the one place where a choice made in the dialog could not take effect. The pin is
        # still the default (nothing asked for means his answer), and an override is reported rather than silent.
        fixed_here = [family for family in families_here if family in fixed]
        if fixed_here:
            rig_type, source = fixed[fixed_here[0]], "fixed"
            mode = None
            for family in fixed_here:
                chosen = family_modes.get(family)
                # compared as a *type*: the setup's answer is a mode (`STUB`, `TAIL`, ...) and the pin is a rig
                # type, so comparing them directly made every answer look like a change - asking for the pinned
                # type itself was reported as an override, which is exactly the kind of line that makes a trace
                # untrustworthy
                chosen_type = MODE_TYPES.get(chosen, chosen)
                if chosen is not None and chosen != table_modes.get(family) and chosen_type != rig_type:
                    mode = chosen
                    rig_type = chosen_type
                    source = "fixed (%s), overridden by the setup" % fixed[family]
                    trace.info("family %-22s is pinned to %s, but the setup asks for %s - using %s"
                               % (family, fixed[family], chosen, rig_type))
                    break
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
            carries = None      # whatever typed this family when the table did not, so its settings come along
            if mode is None:
                if base_rule is not None:
                    rig_type, source = base_rule.get("type"), "the rules table"
                elif skirt_family(families_here[0]):
                    # a skirt-named family the table *in use* does not name - see `SKIRT_FAMILY_PREFIXES`: the
                    # shipped table names these, but a rules file on disk replaces that table wholesale, so this is
                    # the path that reaches an install which already has one, and without it the family stays a
                    # `basic.super_copy` stub and the skirt master finds no panel to drive
                    rig_type, source = SKIRT_FAMILY_ANSWER["type"], "the skirt family list"
                    carries = SKIRT_FAMILY_ANSWER
                elif (unmatched or {}).get("type"):
                    # a family no table rule names, and the run has a fallback for those: his own rigs type them,
                    # and leaving them to MMR meant no control and no message at all (see the option's comment)
                    rig_type, source = unmatched["type"], "the fallback for families no rule names"
                    carries = unmatched
                else:
                    rig_type, source = None, "nothing names it"
                    carries = None
            else:
                rig_type = MODE_TYPES.get(mode)
                # the trace names the rigify type, not this build's internal mode letter: it is read by people who
                # know `basic.super_copy` and not `STUB`
                source = "set to %s" % (rig_type or "none")
            # A type the table names that this build does not know (`limbs.spline_tentacles` in a hand-edited
            # file) used to be applied verbatim to `rigify_type` while the panel's row showed it as a stub
            # (`family_defaults` falls back to MODE_STUB silently), so the rig disagreed with the panel and the
            # failure surfaced later, from rigify. `preset_answers` reports the same case; this makes the table
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
        elif not mode and carries:
            # a family typed by something other than the table - the `unmatched_families` fallback, or the skirt
            # family answer - keeps that source's own settings (a stricter `min_bones`, parameters of its own, the
            # `chain_deform` that makes a skirt a chain), because there is no table rule to inherit from
            for key in ("parent", "parameters", "chain_deform", "resample", "connect_chain", "min_bones"):
                if key in carries:
                    rule[key] = dict(carries[key]) if isinstance(carries[key], dict) else carries[key]
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
        # Where to find this report, said in the report itself: a Windows Blender launched from the GUI has no
        # console, so the same lines that are printed are invisible unless the user opens one - and the user went
        # looking for them and found neither (2.30.1).
        text.write("this block is the whole run; the same lines are printed to the console "
                   "(Window > Toggle System Console)\n")
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
    presets = load_profiles()
    return rules, options, source, presets


def family_defaults(rules, presets, preset_key, families, unmatched=None):
    """What each family should start as: the preset's answer, else the rules table's, else Off.

    Returns `(modes, table_modes, source_label)`.

    * `modes`      - what the field should say (the preset's value where it answers for the family);
    * `table_modes` - what the rules table alone would say, kept so the resolver can tell "the user chose this"
      from "this is just the default". That distinction is what lets a family the table *splits*
      (`Sp_Ch_Ribbon0` is a one-bone stub in front and two side chains) keep both rules until someone actually
      asks for one answer for the whole family.

    A preset cannot answer for a family that did not exist when it was made, so anything it does not answer falls
    back to the table - stated in the trace rather than guessed at silently. `preset_answers` is what decides
    which families a preset answers for (by group, by family, or both).

    `unmatched` is `options.unmatched_families`, and it belongs here for one reason: without it the *panel* lied.
    A family no typed rule names was reported as `none` while the run typed it `basic.super_copy` through that
    fallback - 778 families / 3,521 bones across the 125-model fleet, which is the feature's own promise ("the
    fields decide") being false for a third of what it rigs, and the trace counting them as "left at the rules
    table's own answer". Measured by the beta code review (handoff 9aq) and cheap to get right: the fallback *is*
    the table's answer, so it seeds `table_modes` and the row reads it as `default` rather than as an override.
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
        return modes, table_modes, ("preset %s is not in the folder any more, so the rules table answered"
                                    % preset_key)
    answers, unknown = preset_answers(entry, families)
    changed = 0
    for family, mode in answers.items():
        changed += 1 if modes[family] != mode else 0
        modes[family] = mode
    label = "preset %s (%d famil%s, %d of them different from the table)" % (
        preset_label(preset_key, entry), len(answers), "y" if len(answers) == 1 else "ies", changed)
    if unknown:
        label += "; unmapped: " + ", ".join(unknown[:3])
    return modes, table_modes, label


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


def adopt_rig_into_model(model, rig, trace=None):
    """Put the generated rig inside the model's own object hierarchy, so moving the character moves the rig too.

    The reported fault: *"after running pumpkin rig, I had to move the uma using the armature object - if i used the
    root empty, the mesh would get stretched between the empty and the armature."* Reproduced and measured
    (`rigify_chains_parenting_check.py`): mmd_tools imports a model as `root empty -> armature -> mesh`, and the
    generated rig was parented to **nothing**, so moving the empty moved `1004_00 -> 1004_00_arm -> 1004_00_mesh`
    while `RIG-1004_00_arm` stayed where it was. The model's chain bones are all constrained to follow that rig's
    bones, so the chain-driven parts of the mesh stayed behind with it: a 0.5 m translation plus a 20 degree turn
    stretched the mesh by up to **0.608 m** at its far corner.

    The rig therefore becomes a child of the model's armature with its world transform preserved through
    `matrix_parent_inverse` (set from the armature's world matrix, and the world matrix written back afterwards so
    nothing jumps). The character is one tree again: the empty moves the armature, and the armature moves the rig.

    This direction cannot be a dependency cycle: the armature's *bones* are constrained to the rig's bones, while
    what the rig's transform depends on is the armature's *object* transform, which does not depend on the pose.
    """
    if rig is None or model is None:
        return None
    if rig.parent is not None:
        if trace is not None:
            trace.info("the generated rig %r is already a child of %r, so its place in the hierarchy is left alone"
                       % (rig.name, rig.parent.name))
        return rig.parent
    world = rig.matrix_world.copy()
    rig.parent = model
    rig.parent_type = 'OBJECT'
    rig.matrix_parent_inverse = model.matrix_world.inverted()
    rig.matrix_world = world
    if trace is not None:
        trace.info("the generated rig %r is now a child of the model armature %r, so moving the character by its "
                   "root empty moves the rig with it (without this the rig stayed behind and the mesh stretched: "
                   "measured 0.608 m on a 0.5 m move with a 20 degree turn)" % (rig.name, model.name))
    return model


def sync_preset(self, context):
    """Fill the family rows from `Preset`, if they have not already been filled from it.

    This is the whole of what a preset does - it sets the fields and stops. There is no second resolution step
    at run time, so the trace can always say "preset One control per bone, then Chest set to Off" and the answer
    cannot drift from what the panel shows.

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
    try:
        rules, options, _source, presets = preset_table(self, context)
    except Exception as error:  # noqa: BLE001 - a dialog must not raise
        # Silent here means "picking a preset did nothing, and nothing anywhere says why" - the symptom class of
        # the empty-panel bug (handoff 9ag). The dialog stays up; the reason goes to the console.
        print("[rigify_chains] picking preset '%s' failed (%s: %s); the rows keep what they had"
              % (self.Preset, type(error).__name__, error))
        return
    modes, table_modes, label = family_defaults(rules, presets, self.Preset, self.Families,
                                                options.get("unmatched_families") or {})
    self.Populating = True
    try:
        for item in self.Families:
            if item.fixed:
                continue
            item.mode = modes.get(item.family, MODE_OFF)
            item.default_mode = table_modes.get(item.family, MODE_OFF)
        sync_group_fields(self)
        # A preset that gives one group more than one answer cannot be shown as a group row at all: Simple mode has
        # a single dropdown per group, so the row says `Mixed` and the line above it names the groups. This used to
        # switch the dialog to Advanced as well, which is the user's "Advanced mode is always selected whenever
        # pumpkin rig is run" - and it fired on nearly every model, because the rules table itself splits hair
        # (`Hair0` a stub, `Hair2`/`Hair4` chains). `Mixed` closes the hole the switch was guarding: a row that
        # says `Mixed` cannot be read as one answer, and only a real click rewrites the families in it
        # (`adopt_group_edits`). The fallback type is excluded from that test on purpose: it is not an answer
        # anybody chose (`mixed_groups`).
        mixed = mixed_groups(self, TYPE_MODES.get((options.get("unmatched_families") or {}).get("type")))
        self.Mixed_Groups = ", ".join(mixed)
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


def fallback_mode(operator, context):
    """The rules table's own type for a family no rule names - what `mixed_groups` has to leave out of its count.

    Read here rather than passed in, because `draw` needs it: the "set per family" line has to be recomputed on
    every redraw (see `draw`), and the rules table is cached against the folder's stamp, so this is a dict lookup in
    the common case. A dialog must never raise, so a rules file that cannot be read simply means no fallback.
    """
    try:
        _rules, options, _source, _presets = preset_table(operator, context)
    except Exception:  # noqa: BLE001 - a dialog must not raise
        return None
    return TYPE_MODES.get((options.get("unmatched_families") or {}).get("type"))


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
    bl_description = "Write the current rig type answers into one JSON file in the presets folder"
    bl_options = {'REGISTER'}

    Name: StringProperty(
        name="Preset name",
        description="What to call this preset in the list. An existing preset is never overwritten unless "
                    "Overwrite is ticked",
        default="my-preset",
    )
    Data: StringProperty(name="Choices", default="", options={'HIDDEN'})
    Rules_File: StringProperty(name="Rules File", default="", options={'HIDDEN'})
    Overwrite: BoolProperty(
        name="Overwrite an existing preset",
        description="Replace a preset that is already there. Off by default, so a mistyped name cannot quietly "
                    "replace one of your own presets",
        default=False,
    )

    def invoke(self, context, event):
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
                                   "another name." % (self.Name, os.path.basename(existing.get("file") or
                                                                                 self.Name)))
            return {'CANCELLED'}
        payload = {"families": dict(sorted(families.items()))}
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
    bl_description = "Read the preset folder again, for a file added or edited outside Blender"
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
    bl_description = ("Write the run's trace out as uma_rigify_chains_trace.txt, beside the presets. This is the "
                      "file to attach to a message")
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

    A group whose families disagree shows **`Mixed`** rather than one of those answers. It used to show the most
    common one, which read as "this group gets that type" while it did not - and the only reason it was not a trap
    was that the dialog forced itself open on Advanced whenever any group was mixed (the user's *"Advanced mode is
    always selected"*). `Mixed` says what is true, the row stops being a lie, and a click on it still gives every
    family in the group that one answer.
    """
    values = {}
    for group in SIMPLE_GROUPS:
        members = [item.mode for item in operator.Families if item.group == group and not item.fixed]
        if not members:
            continue
        value = members[0] if len(set(members)) == 1 else MODE_MIXED
        setattr(operator, "%s_Mode" % group, value)
        values[group] = value
    set_group_snapshot(operator, values)


def edit_per_family(operator):
    """Mirror `Mode` into the `Per_Family` checkbox, and adopt a click on it back into `Mode`.

    Same shape as `adopt_group_edits`, and for the same reason: a redraw cannot tell the user's click from its own
    mirror, so what the user moved is compared against the snapshot taken when it was last written. `Mode` is still
    the one place the choice is stored - this only gives it a checkbox row instead of a two-button dropdown.
    """
    wanted = 'ADVANCED' if operator.Per_Family else 'SIMPLE'
    if operator.Per_Family_Snapshot and wanted != operator.Per_Family_Snapshot:
        operator.Mode = wanted
    operator.Per_Family = (operator.Mode == 'ADVANCED')
    operator.Per_Family_Snapshot = 'ADVANCED' if operator.Per_Family else 'SIMPLE'


def adopt_group_edits(operator):
    """Simple mode: if a group row moved since the last redraw, give every family in it that answer.

    Driven from `draw`, against a plain Python snapshot, instead of from an `update` callback. A callback here
    cannot work: this operator's properties are RNA owned by Blender, and the only hook that sees a change is
    `draw` - which is also the only place that is certainly running while the dialog is open. A group whose
    families disagree keeps whatever they say until the user actually moves that row, and `Mixed` is not a move:
    it is what the row shows while they disagree, so selecting it again changes nothing.
    """
    snapshot = group_snapshot(operator)
    if not snapshot:
        return False
    changed = False
    values = dict(snapshot)
    for group in SIMPLE_GROUPS:
        value = getattr(operator, "%s_Mode" % group, MODE_OFF)
        if snapshot.get(group) == value or value == MODE_MIXED:
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
    wanted, _unknown = preset_answers(entry, operator.Families)
    return sum(1 for item in operator.Families
               if not item.fixed and item.family in wanted and item.mode != wanted[item.family])


def reset_dialog_state(operator):
    """Put the dialog's own fields back to how it opens, before the rows are filled for *this* model.

    The user's report: *"I've tried running pumpkin rig on a second model in a file - I've used the default
    settings/rules for the first one, but used simple mode with all the groups set to `basic.super_copy` - but for
    some reason, some settings are retained from the first run, e.g. tentacle type used for the hair and the
    extras."* And: *"Advanced mode is always selected whenever pumpkin rig is run."* Both are the same thing.

    Blender remembers an operator's last-used property values and seeds the next invocation of that operator with
    them - it is why `Add Mesh > Cube` remembers the size you typed - and this operator's properties *are* the
    dialog: `Mode`, `Preset`, the six group fields, and `Families`, a `CollectionProperty` whose rows hold one rig
    type per physics family. So a fresh dialog for a second model opened on the first model's `Mode` and with the
    first model's answers already in rows whose families happen to share a name (hair families are called `Hair0`,
    `Hair2`, `Hair4` on every model), which `_refresh_families(keep=True)` then treated as "what the user set" and
    kept. Nothing was written to disk: `rules` and `presets` are read-only for a run, which was checked first.

    A background session cannot reproduce Blender handing the values over, so `rigify_dialog_state_check.py` copies
    them in by hand and asserts this clears them - that is the whole of the fix, and it makes the dialog's opening
    state a property of the code rather than of what ran before it.

    What is reset is what describes *this run's answers*. What stays is the user's own workflow: `Step` (detected
    from the scene anyway), `Style`, `Rules_File`, `Chain_Layers`, `Resample`, `Overwrite_Typed`, `Debug` and
    `More_Options` - a rules file or a style is chosen once and meant to stick, while "what the hair gets" is a
    question about the model in front of you.
    """
    operator.Mode = 'SIMPLE'
    # the checkbox mirrors `Mode`, so it is carried-over state too: a second model must open on the group rows even
    # if the first was edited per family (the same report the rest of this function answers)
    operator.Per_Family = False
    operator.Per_Family_Snapshot = ""
    operator.Preset = PRESET_DEFAULT
    operator.Preset_Filter = ""
    operator.Preset_Key = ""
    operator.Preset_Note = ""
    operator.Choices = ""
    operator.Group_Snapshot = ""
    operator.Mixed_Groups = ""
    operator.Families.clear()
    for group in SIMPLE_GROUPS:
        setattr(operator, "%s_Mode" % group, MODE_OFF)


def mode_property(group):
    """One `EnumProperty` per group, for Simple mode. `adopt_group_edits` writes it through to the family rows,
    so the two modes cannot drift apart."""
    return EnumProperty(
        name=group,
        description="What every family in %s gets" % group,
        items=GROUP_CHOICES,
        default=MODE_OFF,
    )


class RigifyChains(bpy.types.Operator):
    """Rigify the model's hair, cloth and tail chains as well as the body"""

    bl_idname = "uma.rigify_chains"
    bl_label = "Pumpkin Rig Setup (%s)" % RIG_VERSION
    bl_description = ("MMR controller plus rigify chains for hair, cloth and tail, on the metarig the model is then "
                      "made to follow. Undoable")
    bl_options = {'REGISTER', 'UNDO'}

    Step: EnumProperty(
        name="Step",
        description="Full runs MMR and adds the chains; Wire only re-attaches the model to chains an earlier run "
                    "generated",
        items=[
            ('FULL', "Full", "Run MMR and add the chains"),
            ('WIRE', "Wire only", "Let the model follow the generated chains (after a previous run)"),
        ],
        default='FULL',
    )
    Mode: EnumProperty(
        name="Mode",
        description="Simple asks one question per group; Advanced answers every family on its own",
        items=[
            ('SIMPLE', "Simple", "One rig type per group: Hair, Skirt, Chest, Tail, Extras, Other"),
            ('ADVANCED', "Advanced", "One rig type per family: Hair0, Hair2, Hair4, MSkirt0, Jacket0..."),
        ],
        default='SIMPLE',
    )
    # The per-family switch, drawn in place of the `Mode` dropdown (2.33.6). `Mode` stays the *source of truth*:
    # `fallback_mode`, `sync_preset`, `adopt_group_edits`, `sync_group_fields`, `execute`'s `mode:` trace line and
    # `rigify_dialog_state_check.py` all read it, and a scene, a preset or a script can still set it. This checkbox
    # mirrors it, and the *edit* is detected against a snapshot - the same shape as `adopt_group_edits`, because a
    # plain mirror would overwrite the user's click on the very next redraw.
    Per_Family: BoolProperty(
        name="Edit per family",
        description="Answer every physics family on its own instead of one answer per group",
        default=False,
    )
    Per_Family_Snapshot: StringProperty(name="Last per-family switch", default="", options={'HIDDEN'})
    Preset: EnumProperty(
        name="Preset",
        description="Fill the rig type rows from a stored set of answers. Nothing is chosen automatically - "
                    "`Default` is the rules table",
        items=preset_items,
        # an index, not a value: Blender requires that when `items` is a callback. `preset_items` always returns
        # `PRESET_DEFAULT` first.
        default=0,
        update=apply_preset_update,
    )
    Preset_Filter: StringProperty(
        name="Find a preset",
        description="Narrow the preset list by name",
        default="",
    )
    Preset_Key: StringProperty(
        name="Preset key",
        description="The preset the rows were last filled from",
        default="",
        options={'HIDDEN'},
    )
    # Everything the dialog has to remember between redraws is an RNA property: Blender owns the operator
    # instance, and a Python attribute set while a callback is running does not survive (measured:
    # `AttributeError: 'UMA_OT_rigify_chains' object has no attribute '_preset_chosen'`).
    Populating: BoolProperty(name="Filling the rows", default=False, options={'HIDDEN'})
    Preset_Note: StringProperty(name="Preset note", default="", options={'HIDDEN'})
    #: Groups whose families hold more than one rig type, as `"Hair, Extras"`. Reported above the rows; it no longer
    #: changes the mode (`Mixed` in the group row says the same thing in place - see `sync_group_fields`).
    Mixed_Groups: StringProperty(name="Mixed groups", default="", options={'HIDDEN'})
    Group_Snapshot: StringProperty(name="Last group values", default="", options={'HIDDEN'})
    # How the rig is *drawn* (chains-38). Display only, so it is not part of a preset: a preset says what a chain
    # is made of, a style says what it looks like, and the two are saved in different folders for that reason.
    # The list is the styles folder; the shipped `coolchou.json` is first and is what a fresh install runs.
    Style: EnumProperty(
        name="Style",
        description="How the generated rig is displayed: bone colours, widget sizes and display flags. Display "
                    "only - a style cannot change what the rig does",
        items=style_items,
        default=0,
    )
    # X-ray, whole rig or nothing. See the note in `execute`: Blender has no per-bone or per-family X-ray, so this
    # is the only form the choice can take - and it is here rather than only in Blender's own Viewport Display
    # panel because a flip made there lasts until the next run, which re-applies the style.
    #
    # The row is commented out of `draw` at the user's request ("X-ray dropdown can be removed"): the style
    # already carries the answer, and the property stays so a scene, a script or a preset that sets it still works.
    Xray: EnumProperty(
        name="X-ray",
        description="Whether the rig is drawn through the model. Blender has no per-bone X-ray, so this is the "
                    "whole rig",
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
        description="family=MODE;family=MODE ... applied after the preset, so a script can say what the dropdowns "
                    "would",
        default="",
        options={'HIDDEN'},
    )
    Chain_Layers: EnumProperty(
        name="Chain Layers",
        description="Which of the collections this operator creates to switch on. `Everything` also shows the "
                    "(FK) and (Tweak) sub-layers",
        items=[
            ('NONE', "Nothing", "Hide every collection this operator creates"),
            ('CONTROLS', "Controls only", "Show each family's controls; keep (FK) and (Tweak) off"),
            ('ALL', "Everything", "Show every layer of every family"),
        ],
        default='NONE',
    )
    Rules_File: StringProperty(
        name="Rules File",
        description="JSON file mapping bone name patterns to rigify types. Empty uses the default in the Blender "
                    "config folder",
        subtype='FILE_PATH',
        default="",
    )
    Overwrite_Typed: BoolProperty(
        name="Overwrite Existing Types",
        description="Also re-type the bones MMR already rigified (spine, limbs, fingers, head)",
        default=False,
    )
    Debug: BoolProperty(
        name="Debug Trace",
        description="Print every step and every check to the console as it runs",
        default=True,
    )
    # On by default from 2.33.6. It was off because it adds a bone, and the fleet therefore had to ask for it
    # (`-SkirtMaster`); but the bone *is* the one-control skirt the feature exists for, every gate covers it
    # (`skirt_master_check.py` 20/0 both on and off), and a user who never opens the settings panel should still
    # get it. Pass `Skirt_Master=False` to ask for the rig without it - `fleet_chains.ps1`'s switch still does, for
    # the labels that are compared against pre-2.33.6 fleet runs.
    Skirt_Master: BoolProperty(
        name="Skirt master control",
        description="Add one control that swings every skirt panel at once. Each panel keeps its own control on top",
        default=True,
    )
    # On by default from 2.33.8. It was off for exactly one reason - it rewrites a model's own weights - and that is
    # now measured rather than assumed: the 40-model fleet (both labels, `chains-75`) found 262 panels, 262 of them
    # measured, 235 reshaped, 27 left alone for being already deep enough, **no panel a measurement failed on**, a
    # **0.00000 m** rest-pose delta on the skirt cloth of all 40 models, and the `Hip` blend count unchanged on all
    # 40. Pass `Skirt_Reshape=False` to ask for the rig without it - `fleet_chains.ps1` has `-SkirtNoReshape` for the
    # labels that are compared against a pre-2.33.8 fleet run.
    Skirt_Reshape: BoolProperty(
        name="Reshape short skirt chains",
        description="Give a panel more hinges when its cloth has length for them: subdivides the panel's bones and "
                    "shares its existing weight across them. Edits the model's own weights",
        default=True,
    )
    Batch_Rigify_Copies: BoolProperty(
        name="Batch rigify's bone copies",
        description="Copy rigify's bones with one mode switch per edit session instead of two per bone. Same rig, "
                    "about 1.5x faster",
        default=True,
    )
    # Renamed from "More settings" at 2.33.6. The word "Advanced" used to be *taken* - it was the name of the
    # per-family mode beside this toggle, and the panel deliberately avoided it for that reason. The mode is now the
    # `Edit per family` checkbox, so the name is free for the panel it actually describes.
    More_Options: BoolProperty(
        name="Advanced settings",
        description="The settings that decide what the rig is rather than what it contains: the step, the bone "
                    "lengths and MMR's own types",
        default=False,
    )
    Resample: EnumProperty(
        name="Chain Bone Lengths",
        description="Where the joints of a chain sit. `Keep` copies the model's own spacing; `Even` puts one joint "
                    "every equal step",
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

        # The model's own hierarchy first, when it points at a bone this run is adding (handoff 9be).
        #
        # A rule's `parent` is a statement about the *family* - "the hair hangs off the head" - and for a chain
        # family it is only ever read for the group's index 0, which is the chain root, whose model parent is a
        # body part. For a per-bone family (`basic.super_copy`: `_chains_for_rule` returns one single-bone group
        # per bone, so every bone is an index-0 root) the same sentence was applied to every bone, and the whole
        # chain was flattened onto the body part: measured (all families `super_copy`, one run, 30 degree turn)
        # `Sp_He_Hair2_L` came out with all three controls parented to `ORG-spine.006` and turning the first one
        # moved the next by 0.000000 m, while `Sp_Hi_MSkirt0_B` - whose rule names no parent at all, so it fell
        # through to this branch - cascaded properly (0.061816 m, then 0.123632 m). Nine of the eleven typed
        # shipped rules name a `parent`, so this was most of the all-`super_copy` answer, and it is the difference
        # between "one control per bone" and "one control per bone, and the chain still bends as a chain".
        #
        # This is the same test the no-rule-parent path below has always made; it is only *earlier* now, and only
        # for a model parent that this same run has already created. A bone whose model parent is a body part
        # (`Head`, `Knee_L`) never matches it, so a rule that names an attachment still decides that, and a chain
        # root still lands where the rule says.
        same_run = getattr(self, "_added_bone_names", None) or set()
        if model_parent and model_parent in same_run and model_parent in metarig.data.edit_bones:
            return model_parent, "same family", 0.0, model_parent

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
            #
            # The case this branch could not reach - a rule *does* name a parent, and the bone's model parent is a
            # bone this run adds later - is handled where that bone is actually missing: the branch at the top of
            # this function fires only once the parent exists, and `_add_chains_to_metarig` defers the ones that
            # arrive before it, so the second pass re-parents them (handoff 9be).
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
                            #
                            # ...and a bone whose model parent is a bone this run adds *later* is deferred for the
                            # same reason, whichever branch answered: `_metarig_parent_for` can only hand back the
                            # model's own parent once that bone exists, and `model_parent in geometry` with the
                            # bone not in the metarig yet means exactly "this run is about to add it". The second
                            # pass below re-parents it, so a per-bone family keeps the model's hierarchy even when
                            # the bones are created child-first (handoff 9be).
                            later = (model_parent in geometry and model_parent not in edit_bones
                                     and model_parent != name)
                            if how in ("fallback", "root", "none", "nearest-far") or later:
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

    #: The skirt master's own names, kept here because a re-run has to find what a previous run made.
    SKIRT_MASTER_NAME = "UMA_skirt_master"
    SKIRT_MASTER_CONSTRAINT = "UMA_skirt_master_link"
    #: chou's master is a short bone lying **horizontally**, at the skirt's own height, aimed along the character's
    #: front-to-back axis. Ours used to stand up, which put the world vertical on the one axis an animator turns by
    #: default; ours is aimed *backward* along that axis rather than forward, and that is a measured requirement
    #: rather than a taste - the sense decides whether the panels' own handles add to the master or fight it. See the
    #: frame note in `_add_skirt_master`.
    SKIRT_MASTER_LENGTH = 0.10
    #: chou's **second** channel on the same bone, read off his file by `skirt_master_channels_probe.py`: every one of
    #: his 17 panel roots carries a `TRANSFORM` (`map_from: LOCATION`, `map_to: ROTATION`) that turns the master's
    #: vertical travel into that root's own X rotation - 4 degrees per centimetre over a +-5 cm window, saturating at
    #: +-20 degrees - with `mix_mode_rot: ADD`, so it stacks on the `COPY_ROTATION` rather than replacing it. His own
    #: master is limited to 5 cm up and 5 mm down, and the window is enforced by both his constraint and that limit;
    #: the numbers below are his, verbatim. Measured on our rig: 0.18-0.19 m of hem across all ten panels per 5 cm,
    #: against 0.00000 m for the shipped rotation-only master.
    SKIRT_LIFT_CONSTRAINT = "UMA_skirt_master_lift"
    SKIRT_TRAVEL_CONSTRAINT = "UMA_skirt_master_travel"
    SKIRT_LIFT_WINDOW_M = 0.05
    SKIRT_LIFT_DEGREES = 20.0
    SKIRT_LIFT_UP_M = 0.05
    SKIRT_LIFT_DOWN_M = 0.005
    #: What the master hangs off, in order of preference - the user's answer (*"it should be a child of `torso`"*)
    #: and then the trunk controls a differently-built spine rig might have instead. The master is a *control*, so
    #: it never contributes deformation itself; this only decides what it follows when the body moves.
    SKIRT_MASTER_PARENTS = ("torso", "hips", "torso_root")

    def _skirt_master_forward(self, model):
        """Which way the character faces, in armature space - the direction the master's own length points.

        Taken from the toes, the same measurement `AddonOperators.gaze_direction` uses for the eyes (the toe sits in
        front of the ankle on every humanoid rig), so it does not depend on which way an exporter happened to aim
        anything. The fallback is the armature's own `-Y`, which is the direction `mmd_tools` leaves a model facing.
        """
        for side in ("L", "R"):
            toe = model.data.bones.get("Toe_" + side)
            ankle = model.data.bones.get("Ankle_" + side)
            if toe is None or ankle is None:
                continue
            direction = toe.head_local - ankle.head_local
            direction.z = 0.0
            if direction.length > 1e-5:
                return direction.normalized()
        return Vector((0.0, -1.0, 0.0))

    def _skirt_flare_angles(self, rig, control, last_name, centre, trace):
        """The XYZ euler angles, in the control's own rest frame, of a `SKIRT_LIFT_DEGREES` rotation about that
        panel's flare axis - the rotation that swings its hem outward.

        The axis is sideways to the panel: `outward x up`, where `outward` is the hem's direction away from the
        master. With the hem hanging along `-up`, a rotation of +theta about it moves the hem by
        `-L((outward x up) x up) = +L*outward`, so a positive turn flares that panel outwards - for every panel,
        whatever way it faces, which is the whole point on a ring. Taken from the bones (the chain's last bone tail
        is the hem, or lies beyond it along the same direction) rather than from the mesh, so this costs nothing and
        works on any model, reshaped or not.

        Falls back to the owner's X alone when the panel's outward direction is degenerate - a hem directly below
        the master has no sideways axis to speak of - and says so rather than writing a rotation about nothing.
        """
        fallback = (math.radians(-self.SKIRT_LIFT_DEGREES), 0.0, 0.0)
        bone = rig.data.bones.get(control)
        hem = rig.data.bones.get(last_name)
        if bone is None or hem is None:
            return fallback
        outward = hem.tail_local - centre
        outward.z = 0.0
        if outward.length < 1e-5:
            trace.info("skirt flare: %s's hem sits directly below the master, so it has no sideways direction to "
                       "flare about; its lift stays on the control's own X" % control)
            return fallback
        axis = outward.normalized().cross(Vector((0.0, 0.0, 1.0)))
        if axis.length < 1e-5:
            return fallback
        local_axis = (bone.matrix_local.to_3x3().inverted() @ axis.normalized()).normalized()
        angles = Matrix.Rotation(math.radians(self.SKIRT_LIFT_DEGREES), 3, local_axis).to_euler('XYZ')
        return (angles.x, angles.y, angles.z)

    def _add_skirt_master(self, model, rig, chains, trace):
        """One control over every skirt panel - chou's `skirt_master`, built on what rigify already generates.

        The measurement that made this small (`skirt_master_probe.py`, `docs/SKIRT_RIG_STUDY.md` 8b): a skirt panel is
        a rigify `spines.basic_tail` chain, and its FK control (`<panel>_00_master`) already drives a *per-segment
        cascade* - each segment copies the one above it - which turns 30 degrees into a constant **30.0 degrees per
        joint** and 0.261 m of hem on this model's three-segment panels. chou's own rig does exactly that (a constant
        ~29.2 degrees per joint, 0.271 m over twelve), so the cascade does not have to be added at all. What his rig
        has and ours did not is **one bone over every panel**: that is what this builds.

        **Two channels, because chou's has two** (2.33.0). `skirt_master_channels_probe.py` read his file at the
        channel level for the first time and found that every one of his 17 panel roots carries *two* constraints on
        the master, not one:

        * a `COPY_ROTATION` in `LOCAL` -> `LOCAL_OWNER_ORIENT` with `BEFORE` - the space that lets one rotation be
          read in each panel's own orientation, so a single turn swings a ring of differently-aimed panels;
        * a `TRANSFORM` (`map_from: LOCATION`, `map_to: ROTATION`, `mix_mode_rot: ADD`) that turns the master's
          **vertical travel** into the panel root's own X rotation - 4 deg/cm over a +-5 cm window, saturating at
          +-20 degrees - so the master answers being *moved*, not only turned. His master is clamped to 5 cm up and
          5 mm down (`LIMIT_LOCATION`, `LOCAL`), which is the half of the mechanism a port is most likely to drop.

        Measured on our rig before porting it: the shipped rotation-only master answered **0.00000 m** on all three of
        its own translation axes, and his `TRANSFORM` transplanted is worth 0.18-0.19 m of hem per 5 cm across all ten
        panels.

        **The frame is part of the feature, and so is its handedness.** chou's master is a short bone lying
        horizontally; ours used to stand up, and since a bone's local Y is its own length, that put the world vertical
        on the axis an animator turns by default - so the *natural* motion was the skirt's twist (0.11997 m, against
        0.26608 m on its local X), which is what "it doesn't respond to rotation properly" was. Aiming it along the
        character's front-to-back axis fixes the direction. **Which way along that axis is not cosmetic**: a panel's
        own handle turns its raw local X and the master reaches the same bone through `LOCAL_OWNER_ORIENT`, so the
        master's own sense decides whether the two add. Aimed forward (tail toward -Y, local X = -X) they came out
        anti-parallel - the handle's 25 degrees and the master's 30 together gave **5** deg per joint, where the
        shipped 2.32.0 frame gives **55** and chou's own rig gives 55.35. Aimed backward the frame keeps local X = +X
        and Z = +Z (up); then the handle adds to the master (55), the lift adds to the turn (30 + 20 -> 50), and chou's
        constraint numbers are right as they stand.

        Measured through the shipped switch: 4 of 4 panels moved in the first probe (24.9-29.1 degrees each, the same
        spread chou's ring shows), 10 of 10 in `skirt_master_check.py`, and a panel keeps its own authority afterwards
        (its own control still turns it 25 degrees). Nothing is added to the model's bones and no weight is touched,
        so this cannot change how the mesh deforms - only what moves the panels.
        """
        panels = []
        for rule, chain in chains:
            if str(rule.get("collection", "")).lower() != "skirt" or not chain:
                continue
            control = chain[0]
            if control not in rig.pose.bones:
                continue
            # The bone a user turns is rigify's own per-panel master, not the chain root: that root is *driven* by
            # `COPY_ROTATION` from `<root>_master`, so assigning a rotation to it measures nothing (handoff 9cb).
            master = None
            for constraint in rig.pose.bones[control].constraints:
                if constraint.type == 'COPY_ROTATION' and getattr(constraint, "subtarget", ""):
                    master = constraint.subtarget
                    break
            if master:
                # The chain's last bone carries the hem, and the hem's direction away from the master is the
                # direction this panel faces - which is what the lift has to turn the panel about. Taken from the
                # bones rather than the mesh so this works whether or not the cloth has been reshaped.
                panels.append((control, master, chain[-1]))
        if len(panels) < 2:
            trace.info("skirt master: skipped - %d skirt panel(s) with a control of their own to drive (2 needed)"
                       % len(panels))
            return {"panels": len(panels), "linked": 0, "lifted": 0}
        active = bpy.context.view_layer.objects.active
        try:
            for obj in bpy.context.scene.objects:
                obj.select_set(False)
            rig.select_set(True)
            bpy.context.view_layer.objects.active = rig
            bpy.ops.object.mode_set(mode='EDIT')
            edit = rig.data.edit_bones
            master_bone = edit.get(self.SKIRT_MASTER_NAME)
            created = master_bone is None
            if created:
                master_bone = edit.new(self.SKIRT_MASTER_NAME)
                master_bone.use_deform = False
            # **The frame, and its handedness** (2.33.0). Two things have to hold, and the second one was found the
            # hard way - by measuring the wrong one first and shipping it into a probe run.
            #
            # *First, the direction.* A bone's local Y is always its own length and turning a bone about its own axis
            # is the default grab, so a master standing up put the world vertical on the one axis an animator reaches
            # for - measured, 0.11997 m of hem against 0.26608 m on its local X. That is the "it doesn't respond to
            # rotation properly" report, and aiming the master along the character's front-to-back axis fixes it: the
            # same grab now swings the skirt.
            #
            # *Second, the sense along that axis*, which is not cosmetic. A panel's own handle turns its raw local X;
            # the master's rotation reaches that same bone through `COPY_ROTATION` in `LOCAL_OWNER_ORIENT`, and which
            # sense arrives depends on the master's own frame. Aimed forward - tail toward -Y, which puts local X at
            # -X - the two come out **anti-parallel**: measured on all ten panels, the handle's 25 degrees and the
            # master's 30 together gave **5** deg per joint, which is `|30 - 25|`. The shipped 2.32.0 frame (standing,
            # local X = +X) gives **55**, and chou's own rig gives 55.35 for the same pair - so anti-parallel is a
            # regression, not a taste. Aimed *backward* the frame keeps local X = +X and Z = +Z (up), and then the
            # handle adds to the master again, the lift's source axis is still "up", and chou's own constraint
            # numbers cross over unmodified.
            forward = self._skirt_master_forward(model)
            # The ring's centre, not the first panel's control: the panels surround the hip, and starting from
            # `panels[0]` made the master's home depend on the order the chains happened to come out in. Only the
            # gesture is affected - both constraints read the master in `LOCAL` spaces, so where it sits does not
            # enter either copy.
            centre = Vector((0.0, 0.0, 0.0))
            for _control, master, _last in panels:
                centre += edit[master].head
            centre /= float(len(panels))
            master_bone.head = centre
            master_bone.tail = centre - forward * self.SKIRT_MASTER_LENGTH
            # **Hang it off the trunk** (2.33.11, the user: *"the skirt master is not parented to anything right now -
            # it stays in place when you move the body. I'd say it should be a child of `torso`"*). It was created as
            # a parentless root, so it stayed where it was while the body moved: fine for the two constraints, which
            # read it in `LOCAL` spaces and only ever use its *rotation*, but wrong for the hand that has to find it.
            # Parenting changes no geometry - `edit_bone.parent` only adds the transform chain, and head/tail above
            # are in armature space - so the master's own frame, and therefore `_skirt_flare_angles`, the lift's
            # window and every panel copy, are untouched. `use_connect` stays off: a connected bone's head is locked
            # to its parent's tail, and this one hangs at the ring's centre, not on `torso`'s tail.
            #
            # `torso` is what the user asked for and it is the trunk control rigify's spine rig makes (`root` ->
            # `torso_root` -> `torso` -> `hips`/`chest` on the reference model). The fallbacks are there because the
            # rig is MMR's metarig passed through rigify: a model with a different spine setup may name the trunk
            # something else, and a master that is parentless is still better than a run that fails.
            for parent_name in self.SKIRT_MASTER_PARENTS:
                parent = edit.get(parent_name)
                if parent is not None:
                    master_bone.parent = parent
                    master_bone.use_connect = False
                    break
            else:
                trace.info("skirt master: none of %s is in this rig, so %s stays a root bone and will not follow "
                           "the body" % (", ".join(self.SKIRT_MASTER_PARENTS), self.SKIRT_MASTER_NAME))
            bpy.ops.object.mode_set(mode='OBJECT')
            # Show it with the panels rather than in whatever collection happens to be active.
            try:
                collections = [collection for collection in rig.data.collections
                               if rig.data.bones.get(panels[0][1]) in collection.bones]
                if collections:
                    collections[0].assign(rig.data.bones[self.SKIRT_MASTER_NAME])
            except Exception:  # noqa: BLE001 - a bone in the wrong collection is cosmetic
                pass
            linked = 0
            lifted = 0
            for control, master, _last in panels:
                holder = rig.pose.bones.get(master)
                if holder is None:
                    continue
                existing = holder.constraints.get(self.SKIRT_MASTER_CONSTRAINT)
                if existing is not None:
                    holder.constraints.remove(existing)
                constraint = holder.constraints.new('COPY_ROTATION')
                constraint.name = self.SKIRT_MASTER_CONSTRAINT
                constraint.target = rig
                constraint.subtarget = self.SKIRT_MASTER_NAME
                constraint.owner_space = 'LOCAL'
                constraint.target_space = 'LOCAL_OWNER_ORIENT'
                constraint.mix_mode = 'BEFORE'
                linked += 1
                # **His second channel, aimed at each panel's own axis** (2.33.1). chou's `TRANSFORM` writes the
                # owner's raw local X, and that is right for him for the same reason it is wrong for us: his panel
                # roots are duplicates of the model's skeleton. Ours are rigify's FK controls, whose local X points
                # within 31 degrees of one *common* world direction for all ten panels while the panels' outward
                # directions span a full turn - so the same rotation tips the whole ring about one world axis. His
                # wiring measured on the model he was tested with: the back panels moving outward and up (+0.0526,
                # +0.0408 m) while the front panels moved *inward* and barely rose (-0.0546, +0.0014 m). The skirt
                # does not flare; its back lifts and its front sinks, which is a backwards curl, and the two places
                # where neighbours disagreed were the two places the cloth stretched.
                #
                # What a flare actually needs is a rotation about each panel's *own* sideways axis, which is
                # `outward x up` with the hem hanging along -up: rotating by +theta about it moves the hem by
                # -L((outward x up) x up) = +L*outward, i.e. outwards, for every panel. That axis is a combination of
                # the owner's X and Z rather than either alone, and a `TRANSFORM` can write all three of the owner's
                # euler axes - so the axis is expressed in the control's own rest frame and written as the **exact
                # euler decomposition** of a SKIRT_LIFT_DEGREES rotation about it. Euler angles are an exact
                # parameterisation, so at full travel the panel turns by exactly that rotation about exactly that
                # axis. (Writing the axis's *components* as if they were angles is not the same thing: it is right
                # only for small angles and ignores the composition order, and it left four of ten panels flaring
                # inward.) Measured with the decomposition: 10 of 10 panels outward and 10 of 10 rising, and every
                # neighbouring pair's hem direction turning by exactly its own azimuth step - worst excess 0.0 deg,
                # where the raw-axis version had two boundaries at nearly 180.
                stale = holder.constraints.get(self.SKIRT_LIFT_CONSTRAINT)
                if stale is not None:
                    holder.constraints.remove(stale)
                lift = holder.constraints.new('TRANSFORM')
                lift.name = self.SKIRT_LIFT_CONSTRAINT
                lift.target = rig
                lift.subtarget = self.SKIRT_MASTER_NAME
                lift.map_from = 'LOCATION'
                lift.map_to = 'ROTATION'
                # every output axis reads the master's vertical travel; the source's other two windows stay
                # degenerate (0 -> 0) exactly as his are, so only the lift drives it
                for suffix in ("x", "y", "z"):
                    setattr(lift, "map_to_%s_from" % suffix, 'Z')
                    live = suffix == "z"
                    setattr(lift, "from_min_%s" % suffix,
                            -self.SKIRT_LIFT_WINDOW_M if live else 0.0)
                    setattr(lift, "from_max_%s" % suffix,
                            self.SKIRT_LIFT_WINDOW_M if live else 0.0)
                flare = self._skirt_flare_angles(rig, control, _last, centre, trace)
                for suffix, angle in zip(("x", "y", "z"), flare):
                    setattr(lift, "to_min_%s_rot" % suffix, -angle)
                    setattr(lift, "to_max_%s_rot" % suffix, angle)
                lift.to_euler_order = 'XYZ'
                lift.mix_mode_rot = 'ADD'
                lift.owner_space = 'LOCAL'
                lift.target_space = 'LOCAL'
                lifted += 1
            # ...and the master's own travel, clamped the way his is: a lift channel that can be driven past its own
            # window is a control whose last centimetre does nothing, so the limit and the window are sized together.
            master_pose = rig.pose.bones.get(self.SKIRT_MASTER_NAME)
            if master_pose is not None:
                travel = master_pose.constraints.get(self.SKIRT_TRAVEL_CONSTRAINT)
                if travel is None:
                    travel = master_pose.constraints.new('LIMIT_LOCATION')
                    travel.name = self.SKIRT_TRAVEL_CONSTRAINT
                travel.owner_space = 'LOCAL'
                travel.use_min_x = travel.use_max_x = False
                travel.use_min_y = travel.use_max_y = False
                travel.use_min_z = travel.use_max_z = True
                travel.min_z = -self.SKIRT_LIFT_DOWN_M
                travel.max_z = self.SKIRT_LIFT_UP_M
                travel.use_transform_limit = True
            bpy.context.view_layer.update()
        finally:
            if active is not None and active.name in bpy.data.objects:
                for obj in bpy.context.scene.objects:
                    obj.select_set(False)
                active.select_set(True)
                bpy.context.view_layer.objects.active = active
        trace.check("the skirt panels answer one master control", linked == len(panels),
                    "%d of %d panel control(s) linked to %s%s"
                    % (linked, len(panels), self.SKIRT_MASTER_NAME, "" if created else " (the bone was already there)"))
        trace.check("every linked panel also answers the master's lift", lifted == linked,
                    "%d of %d linked panel control(s) carry %s (%.1f deg over +-%.0f cm, clamped to +%.0f/%.0f cm)"
                    % (lifted, linked, self.SKIRT_LIFT_CONSTRAINT, self.SKIRT_LIFT_DEGREES,
                       self.SKIRT_LIFT_WINDOW_M * 100.0, self.SKIRT_LIFT_UP_M * 100.0,
                       self.SKIRT_LIFT_DOWN_M * 100.0))
        trace.info("skirt master: %s (%s) drives %d panel(s) - turn it to swing every skirt panel at once, and move "
                   "it up (%.0f cm) or down (%.0f cm) to flare or settle them; each panel keeps its own control on top"
                   % (self.SKIRT_MASTER_NAME, "created" if created else "reused", linked,
                      self.SKIRT_LIFT_UP_M * 100.0, self.SKIRT_LIFT_DOWN_M * 100.0))
        return {"panels": len(panels), "linked": linked, "lifted": lifted, "created": created}

    def _wire_model_to_chains(self, model, rig, chains, trace, release_parents=True, resample="keep",
                              chain_deform="rigid", chain_link="world"):
        """Let the model's own chain bones follow the rig's copies of the same name.

        Two wirings, chosen by `chain_link` (`options.chain_link`, handoff 9bk):

        **`local`** - the model's own MMD hierarchy is **kept** (nothing is released) and each chain bone copies
        its control's **local** rotation, so the only thing that reaches it from the rig is the delta the animator
        adds on top of the body's motion, which arrives once through the hierarchy. Measured on the reference
        model, moving the character by its root empty (0.5 m and 20 degrees): mesh off by **0.000001 m, 0 bones
        shifted**, against **0.588386 m and 182 bones** with the world-space wiring - and turning the head moves a
        hair chain by the raw MMD rig's own ratio (tip/head 1.509) where the world-space wiring over-moves it by
        1.53x (2.303). A `limbs.spline_tentacle` chain gets a **POSE-space `COPY_TRANSFORMS`** instead, because its
        motion is positional: the curve re-shapes the ORG chain, and pose space carries that reshaping while
        excluding both objects' own transforms. Rotation only would drop its `_master` control from 0.048272 m to
        0.000000 m - measured - so the two rig types genuinely need two links.

        **`world`** - the previous wiring, kept as the escape hatch: `CHILD_OF` with the inverse matrix taken from
        the target bone's *rest* matrix, rather than MMR's plain world-space copy-transforms: that moves the model
        bone by the rig bone's motion and leaves its rest alone. A world-space copy instead teleports the model
        bone onto the rig bone, so any difference between the two rest poses becomes a visible pull on the mesh -
        which is what a mismatched metarig produced the first time round. In `bbone` mode (his architecture) the
        chain keeps its own hierarchy, every bone gets the same 8 B-Bone segments rigify gives its deform chain,
        and only the root is a `CHILD_OF`: the children are set to the rig bone's world rotation with
        `COPY_ROTATION` in REPLACE mode, so the parent they keep supplies their position and the constraint
        supplies their rotation - once each, not twice.
        """
        local_link = str(chain_link).lower() == "local"
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
        if local_link:
            # nothing is released: the model keeps the hierarchy MMD gave it, which is what makes the chains
            # follow the body once (and what lets a damping-track chain or the raw rig be posed by hand)
            trace.info("chain link: local - the model's own hierarchy is kept (no parent is released) and each "
                       "chain bone copies its control's own local rotation; spline chains get a pose-space "
                       "transform copy")
        elif release_parents:
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
        local_rotation, local_pose, local_from_control, local_from_org = 0, 0, 0, 0
        # A spline chain's motion is positional - the curve re-shapes its ORG chain - so it takes the pose-space
        # transform link rather than the rotation one (handoff 9bk)
        spline_names = set()
        for rule, names in chains:
            if str(rule.get("type")) == "limbs.spline_tentacle":
                spline_names.update(names)

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
        # ...and the same reference in **pose space** (the ORG bone's own matrix, i.e. without either object's
        # world transform), which is what the `local` link's spline half needs: see the `CHILD_OF` below. `POSE`
        # on both sides keeps the object transforms out, and the inverse matrix keeps the baked spline rest offset
        # out as well - without it the model's tail bones jumped 0.01535 m at rest (measured on the reference
        # model: 8 bones off their rest position, which the run's own `the model is undisturbed at rest` check
        # then failed on).
        pose_references = {}
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
                    pose_references[name] = rig_pose_bone.matrix.copy()
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
            if local_link:
                # one constraint per chain bone, whatever its rig type: the rotation link for everything whose
                # motion is a rotation, the pose-space transform link for a spline chain. Any of the three names
                # this operator has ever used is cleared first, so re-running over either wiring leaves exactly
                # one link per bone.
                for constraint_name in (FOLLOW_CONSTRAINT_NAME, ROTATE_CONSTRAINT_NAME, LINK_CONSTRAINT_NAME):
                    existing = pose_bone.constraints.get(constraint_name)
                    if existing is not None:
                        pose_bone.constraints.remove(existing)
                if name in spline_names:
                    # A spline chain's motion is positional, so it needs the transform, not just the rotation -
                    # in POSE space, so neither object's own transform enters (which is what makes the character
                    # movable by its root empty without tearing). Measured on the design model: the spline's
                    # `_master` control keeps working at 0.048272 m, and the stretch is 0.000240 m against
                    # 0.588386 m for the world-space wiring.
                    constraint = pose_bone.constraints.new('COPY_TRANSFORMS')
                    constraint.name = LINK_CONSTRAINT_NAME
                    constraint.target = rig
                    constraint.subtarget = "ORG-" + name
                    constraint.owner_space = 'POSE'
                    constraint.target_space = 'POSE'
                    constraint.mix_mode = 'REPLACE'
                    local_pose += 1
                else:
                    subtarget = name if name in rig.pose.bones else "ORG-" + name
                    constraint = pose_bone.constraints.new('COPY_ROTATION')
                    constraint.name = LINK_CONSTRAINT_NAME
                    constraint.target = rig
                    constraint.subtarget = subtarget
                    # LOCAL on both sides: relative to each bone's own rest orientation, so neither object's
                    # transform enters the constraint at all - which is the whole reason the character can be
                    # moved by its root empty without the mesh tearing
                    constraint.owner_space = 'LOCAL'
                    constraint.target_space = 'LOCAL'
                    constraint.mix_mode = 'REPLACE'
                    local_rotation += 1
                    if subtarget == name:
                        local_from_control += 1
                    else:
                        local_from_org += 1
            elif name in linked_names and name not in roots and name not in unconnected_joints:
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
        if local_link:
            trace.info("chain link: %d rotation link(s) (%d from a control of the same name, %d from the ORG "
                       "copy) and %d pose-space transform link(s) for spline chains"
                       % (local_rotation, local_from_control, local_from_org, local_pose))
            if not local_from_control and local_rotation:
                trace.info("chain link: no chain bone has a control of its own name on the rig, so every link "
                           "points at the ORG copy")
        elif linked:
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
                       "bakes into the rest pose (%s); %s"
                       % (worst_rest_delta, " at %s" % worst_name if worst_name else "",
                          shares if worst_name else "no joint is off at all",
                          "under `chain_link: local` the spline families that copy it are reported below, and "
                          "everything else stays on its own rest position"
                          if local_link else
                          "the wiring absorbs it, and `the model is undisturbed at rest` is the gate that "
                          "matters"))
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
        #
        # **Under `chain_link: local` this is inverted, and these two checks are the reason to remember why.**
        # The doubling above is a property of the *world-space* link: it applies the ORG's absolute transform,
        # which already contains the body's motion, on top of the parent-inherited motion. A LOCAL rotation link
        # applies only the delta the animator adds, so the parent is exactly what makes the body's motion arrive
        # once - measured on the reference model, keeping the parents moves a chain by 0.042500 m for a 30 degree
        # head turn (the raw MMD rig's own 0.042500 m) where the released world-space wiring moved it 0.064852 m.
        # So in this mode the check asserts the opposite property, and the one that actually protects the user:
        # every chain link must be in a space that cannot see either object's transform, because that is what
        # makes moving the character by its root empty safe (0.000001 m against 0.588386 m).
        doubled = []
        if local_link:
            world_space = []
            for name in wireable:
                pose_bone = model.pose.bones.get(name)
                if pose_bone is None:
                    continue
                for constraint in pose_bone.constraints:
                    if not constraint.name.startswith("UMA_chain"):
                        continue
                    if 'WORLD' in (constraint.owner_space, constraint.target_space):
                        world_space.append("%s (%s)" % (name, constraint.name))
            trace.check("no chain link reads world space (so the character can be moved safely)", not world_space,
                        ", ".join(world_space[:5]) if world_space
                        else "%d link(s) in local or pose space" % len(wireable))
            kept = [name for name in wireable
                    if model.data.bones.get(name) is not None and model.data.bones[name].parent is not None]
            trace.check("the chains kept the model's own hierarchy", bool(kept),
                        "%d of %d chain bone(s) still have their MMD parent - which is what makes the body's "
                        "motion arrive once" % (len(kept), len(wireable)))
        else:
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
        # the rigid follower mode keeps the old, verified gate: no chain bone may hold a parent. It belongs to
        # the world-space wiring and to it alone - see the note above.
        if not local_link:
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
                rules, presets, self.Preset, self.Families,
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
            # A preset whose families disagree inside one group cannot be shown by a single group dropdown, so the
            # group row says `Mixed` and this names the groups for the line above the rows. It used to switch the
            # dialog to Advanced as well, which is what the user reported ("Advanced mode is always selected
            # whenever pumpkin rig is run") - and it fired on nearly every model, since the rules table itself
            # splits hair (`Hair0` a stub, `Hair2`/`Hair4` chains). `Mixed` closes the hole the switch was guarding:
            # a group row that shows `Mixed` cannot be mistaken for one answer, and only a real click rewrites the
            # families in it (`adopt_group_edits`).
            mixed = mixed_groups(self, TYPE_MODES.get((options.get("unmatched_families") or {}).get("type")))
            self.Mixed_Groups = ", ".join(mixed)
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
        """Fill the family rows for *this* model and open the dialog on a known state.

        The panel calls this operator with no properties at all (AddonPanels.py), so everything the run needs has
        to be settled here: which families this model has, and what the rules table says each of them gets. No
        preset is picked for the user - the list opens on `Default`, which *is* the rules table, and a preset only
        ever applies because somebody chose it (handoff 9bd).

        **`reset_dialog_state` first, and the fill is `keep=False`.** Blender seeds a new invocation with the
        operator's last-used property values, which for this operator means the *previous model's* dialog - its
        mode, its preset and one rig type per family, in rows whose families share names across models. That is the
        user's report in two halves ("Advanced mode is always selected", "some settings are retained from the first
        run"), and it is why the opening state is now set here rather than inherited.
        """
        reset_dialog_state(self)
        trace = Trace(self.Debug)
        # The rules file is brought up to date *before* the rows are filled from it, so the dialog shows what the run
        # will do. Skipped for an explicitly chosen `Rules_File`: that is somebody else's table (a probe's, or a
        # user's per-character one) and this pass has no business editing it. Never in `draw`, and never at import.
        if not self.Rules_File:
            housekeep_rules_file(trace)
        self._refresh_families(context, trace, keep=False)
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
        """What `Advanced settings` says about `Step`: the detected value, and why.

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
        """Preset, then either one row per group or one row per family - behind one checkbox.

        `Edit per family` (off) shows the group rows; on, it shows every physics family the model has - median 13
        of them across chou's 241 rigs, 23 at most - grouped under the same headings, so it reads as the group view
        expanded rather than a second system. It is a checkbox over `Mode` (see `edit_per_family`) because the two
        answers are not a *mode* the user is in, they are how much detail they want right now. The two families
        chou's own rigs never vary are listed and locked, so nobody has to wonder where the ears went.

        `Advanced settings` holds what decides what a rig *is* rather than what it contains (`Step`, the bone
        lengths, MMR's own re-typing). It is folded away and its rare rows are not drawn at all - `Debug`,
        `Batch rigify's bone copies` and `Rules File` are commented out below, with their properties intact, the
        same treatment the `Xray` and `Preset_Filter` rows already have.
        """
        layout = self.layout
        column = layout.column()
        # The panel, not a property `update` callback, is what keeps the rows and the preset in step: `update`
        # does not fire for every way the value can change, and one on a `PropertyGroup` item is handed the item
        # rather than the operator. `draw` runs on every redraw while the dialog is open, so both fixes live
        # here - fill the rows from the preset if it changed, then adopt any group row the user moved.
        sync_preset(self, context)
        # The checkbox is adopted *before* the branch below, so ticking it draws the family rows in the same frame
        # rather than one redraw later. `sync_preset` runs first because applying a preset can set `Mode` itself.
        edit_per_family(self)
        if self.Mode != 'ADVANCED':
            # adopt first (a group row the user moved wins), then mirror the family rows back into the group
            # rows - unconditionally, so a family edited per family cannot leave a group row showing a stale
            # answer. `sync_group_fields` only writes the group properties and the snapshot, never the families,
            # so this cannot flatten a per-family choice (code review, handoff 9ai).
            adopt_group_edits(self)
            sync_group_fields(self)
        # Each dropdown gets a one-word header, which is what the user asked for ("a few header texts would be nice
        # to clarify what each dropdown does - Preset, Style") instead of the tooltip carrying it.
        #
        # The `Mode` dropdown is gone (2.33.6): it was two buttons in a row whose whole content was "how much
        # detail do you want", which is a checkbox. `edit_per_family` mirrors `Mode` into it and adopts the click,
        # so every reader of `Mode` is untouched.
        column.prop(self, "Per_Family")
        preset = column.row(align=True)
        preset.label(text="Preset")
        preset.prop(self, "Preset", text="")
        folder = preset.row(align=True)
        # `EXEC_DEFAULT`: this button carries its action, so it must run it rather than open the operator's own
        # dialog - which it would do now that `RigifyChainRules` has an `invoke` for the panel button's sake
        folder.operator_context = 'EXEC_DEFAULT'
        props = folder.operator(RigifyChainRules.bl_idname, text="", icon='FILE_FOLDER')
        props.Action = 'SHOW'
        # The preset search box is commented out at the user's request ("search bar for the rules can be
        # removed"); the reload button that used to sit inside it stays, now beside the folder. `Preset_Filter` is
        # still a property - `preset_items` still reads it, so a scene or a script that sets it still narrows the
        # list - it just has no row.
        #
        # preset_search = column.row(align=True)
        # preset_search.prop(self, "Preset_Filter", text="", icon='VIEWZOOM')
        # reload_row = preset_search.row(align=True)
        folder.operator(RigifyChainPresetReload.bl_idname, text="", icon='FILE_REFRESH')
        # Which groups hold more than one answer, recomputed here rather than read from `Mixed_Groups`: that
        # property is only written when the rows are *filled* (`sync_preset` returns early once they came from the
        # current preset), so a hand edit that makes a group uniform again - or splits another - left this line
        # saying the old thing. Measured: after the Hair row was set to one answer, the line still named Hair.
        mixed = mixed_groups(self, fallback_mode(self, context))
        self.Mixed_Groups = ", ".join(mixed)
        # One short line at most. The detail (`preset <name> (...), N families different from the table`) is in
        # the trace, where it can be read at leisure; a dialog is not the place for a sentence.
        if mixed:
            column.label(text="Set per family: %s" % ", ".join(mixed))
        elif preset_mismatch(self):
            column.label(text="%d famil%s changed" % (preset_mismatch(self),
                                                      "y" if preset_mismatch(self) == 1 else "ies"))
        save = column.row()
        save.operator(RigifyChainPresetSave.bl_idname, icon='FILE_TICK').Data = self._family_data()
        # The style row (chains-38), hidden from the dialog at 2.33.6 to free the panel up ("the visual styles are a
        # nice feature, but I'm thinking of hiding it from the ui for now - we'll put it back when there are enough
        # users to justify it. I'd just comment it out for now"). The `Style` property, the styles folder, the
        # `RigifyChainStyleSave`/`RigifyChainStyleFolder` operators and everything in `RigStyles.py` all stay: a
        # scene, a preset or a script can still set a style, the folder can still be opened from Blender's own
        # operator search, and putting the rows back is uncommenting three statements. With them hidden every rig
        # takes the shipped default style (`coolchou.json`, first in the list), which is what a fresh install runs
        # anyway.
        #
        # style_row = column.row(align=True)
        # style_row.label(text="Style")
        # style_row.prop(self, "Style", text="")
        # style_folder = style_row.row(align=True)
        # style_folder.operator(RigifyChainStyleFolder.bl_idname, text="", icon='FILE_FOLDER').Action = 'SHOW'
        #
        # The X-ray row is commented out at the user's request ("X-ray dropdown can be removed"). The property and
        # everything that reads it stay - a style still carries the answer, and a scene, a preset or a script can
        # still set it - so this only takes the row off the dialog.
        #
        # column.prop(self, "Xray")
        #
        # style_save = column.row()
        # style_save.operator(RigifyChainStyleSave.bl_idname, icon='FILE_TICK')
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
            if mixed:
                box.label(text="Edit per family sets %s family by family" % ", ".join(mixed), icon='INFO')
        column.separator()
        column.prop(self, "Chain_Layers")
        column.prop(self, "Skirt_Master")
        # The reshape needs its own row: the operator's properties *are* the dialog, and a property nobody draws is a
        # property nobody can set. It was missing here at 2.33.2 and the feature was unreachable from the UI - the
        # user's own report (*"I ran the pumpkin rig setup, and it seems the skirt reshape didn't run"*), with the
        # master on and the reshape simply not offered.
        column.prop(self, "Skirt_Reshape")
        column.prop(self, "More_Options")
        if self.More_Options:
            more = column.box()
            for index, line in enumerate(self.more_settings_lines(context)):
                more.label(text=line, icon='INFO' if index == 0 else 'BLANK1')
            more.prop(self, "Step")
            more.prop(self, "Resample")
            more.prop(self, "Overwrite_Typed")
            # Three rows are off the panel from 2.33.6, with their properties and every reader intact - the same
            # treatment `Xray` and `Preset_Filter` already have above:
            #
            # * `Batch_Rigify_Copies` - pure speed, on by default, and measured to produce a byte-identical rig. It
            #   is not a decision a user makes; a probe or a script can still turn it off to compare.
            # * `Debug` - console verbosity only. The trace text block is written either way, so nothing a user
            #   needs is behind it, and it defaulted to *True*, which is a strange thing to offer in a dialog.
            # * `Rules_File` - a raw path to the JSON mapping bone patterns to rigify types. The folder button in
            #   the preset row and the `Rigify Chain Rules` operator (which can also write the defaults out) are the
            #   way in for a user; the path row is for whoever is editing the file by hand, and `Rigify Chain Rules`
            #   is still in Blender's operator search.
            #
            # more.prop(self, "Batch_Rigify_Copies")
            # more.prop(self, "Debug")
            more.separator()
            # more.prop(self, "Rules_File")
            more.operator(RigifyChainTraceSave.bl_idname, icon='FILE_TICK')
            # Where a run's own record is, said where a user goes looking for it. The status bar carries the current
            # step while a run is going; everything else - every step, every check, the `timings:` line - is in this
            # text block, which the button above writes out as a file. Blender's window-manager progress bar is
            # deliberately *not* used since 2.30.1: it draws a bare percentage at the mouse cursor, which reads as a
            # glitch rather than as progress (`operators/Progress.py`, `CURSOR_BAR`).
            more.label(text="Progress is on the status bar while it runs; the report is this block: %s"
                            % TRACE_TEXT_NAME, icon='INFO')

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
                              "Trace: %s" % (RIG_TAG, model.name, text_name))
        return {'FINISHED'}

    # --------------------------------------------------------------------------------------------------

    def execute(self, context):
        """Run the setup, and close the progress bar however it ends.

        The body is `_run` and this wrapper exists for one reason: `_run` has twenty-odd `return {'CANCELLED'}`
        paths (no model, a stale build, a refused full run on an already-rigged model), and a bar left open covers
        the status bar for the rest of the session. `Progress.finish_running` also prints the `timings:` line, so
        the numbers are reported on every path out - including the ones that report a refusal.
        """
        try:
            return self._run(context)
        finally:
            if Progress is not None:
                Progress.finish_running()

    def _run(self, context):
        trace = Trace(self.Debug)
        trace.step("Pumpkin rig setup %s, step=%s" % (RIG_TAG, self.Step))
        started = time.time()
        # Where the time went. The user's report - *"the second character did finish generating the rig properly,
        # but it seemed to take much longer"* - could not be answered from the trace at all: it carried one total
        # (`done in 48.2s`) and nothing else, so "MMR is slower with two characters in the file" and "something in
        # this operator scales with the scene" were both consistent with it. These marks cost nothing and make the
        # next slow run explain itself. `MMR_SECONDS` is filled in by the wrapper, which is where rigify's own
        # generation happens, and `INSERT_SECONDS` separates our metarig work from MMR's.
        #
        # Since 2.28.0 the same marks also drive the status bar and the progress bar (`Progress.Steps`), because the
        # user asked for a run that does not look frozen: *"separate them into logical steps and log time? We could
        # output it as a progress bar ... so the user knows that it's working"*. The weights are relative costs from
        # `docs/PERFORMANCE.md` - MMR's generation is by far the largest, and it is a *single* call, so the bar jumps
        # when it returns rather than creeping; the status line names the step instead.
        steps = Progress.begin("Pumpkin Rig", trace=trace, debug=self.Debug) if Progress else None
        MMR_SECONDS = {}
        if steps:
            steps.plan(("the stale check", 0.2),
                       ("find the model, rules, families and chains", 1.0),
                       ("generate the rig (MMR + rigify)", 20.0, "the long one: no progress inside this step"),
                       ("wire the model to the chains", 3.0),
                       ("collections, UI rows and the style", 1.0))
        marks = []

        def mark(label):
            if steps:
                steps.mark(label)
            else:
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
            self.report({'ERROR'}, "[%s] No MMD armature found to rig (no armature is skinned to a mesh)." % RIG_TAG)
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
                        % (RIG_TAG, model.name, wired_rig.name, text_name))
            return {'CANCELLED'}
        if wired_bones:
            trace.info("%s is already rigged: %d chain bone(s) follow %s, so this run has nothing to generate or "
                       "attach and will re-apply the display only" % (model.name, len(wired_bones), wired_rig.name))

        # ...and the rules file this run is about to read is brought up to date first, so the table a run uses is the
        # one this build ships wherever the file does not say otherwise. An explicit `Rules_File` is left alone.
        if not self.Rules_File:
            housekeep_rules_file(trace)
        rules, options, source = load_rules(bpy.path.abspath(self.Rules_File) if self.Rules_File else None)
        # `load_rules` hands back the *cached* options dict, and the two lines below write into it - so without
        # this copy they were editing module-level state for the rest of the Blender session. Measured
        # consequence (code review, handoff 9ai): run once with `Resample = Even out joints`, set it back to
        # `From rules file` (whose whole job is to read `options["resample"]`, i.e. to write nothing), run again
        # - and the second run resamples `even` while the field and the trace say otherwise.
        options = dict(options)
        # a rules file from an older build still holds the presets inside it: the presets are one file each now, so
        # this writes the shipped ones and retires the character-keyed ones - here rather than in the panel
        # (this writes and moves files)
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
        # calls this operator directly has an empty family list, so it is filled here too. No preset is picked
        # here either: `Preset` is whatever the caller set, and `Default` is the rules table (handoff 9bd).
        identity, identity_bone = model_identity(model)
        trace.info("model identity: %s%s" % (identity or "no Chara_ bone",
                                            " (%s)" % identity_bone if identity_bone else ""))
        # A group row the user moved is flushed to the families here as well as in `draw`: the dropdown writes the
        # group property and `draw` does the adopting, so pressing OK in the same event as the last change (a
        # keyboard edit, a script) would otherwise run with the old per-family answers. It is a no-op when the
        # snapshot is empty (a plain `bpy.ops` call) or when every row already agrees.
        adopt_group_edits(self)
        self._refresh_families(context, trace)
        chosen, table_modes = self._chosen_modes(trace)
        fixed = dict(options.get("fixed_families") or {})
        trace.info("mode: %s, preset: %s" % (self.Mode, self.Preset))
        if self.Mixed_Groups:
            trace.info("these groups hold more than one answer, so their row reads `Mixed`: %s"
                       % self.Mixed_Groups)
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

        # The skirt reshape (2.33.2): a panel that deforms through fewer bones than its cloth has length for cannot
        # curl, so it gets the missing hinges here - *before* the chains are collected, so the new bones are part of
        # the model when the chains, the metarig and the rig are all built from it. Measured that way: the metarig
        # follows a bone that did not exist before the run to 0.0000 m, and the model at rest is unchanged to
        # 2.5e-07 m. Opt-in, because it rewrites the model's own weights.
        if self.Step == 'FULL' and (bool(self.Skirt_Reshape) or bool(options.get("skirt_reshape", False))):
            trace.step("the skirt reshape: hinges where the cloth asks for them")
            try:
                self._skirt_reshape = reshape_skirt_panels(
                    model, trace, forced=options.get("skirt_hinges") or None,
                    spacing=float(options.get("skirt_hinge_spacing_m", SKIRT_HINGE_SPACING_M)))
            except Exception as error:  # noqa: BLE001 - a model whose cloth cannot be measured is still a model
                trace.check("the skirt reshape ran", False, "%s: %s" % (type(error).__name__, error))
                trace.info("skirt reshape: could not run (%s: %s); the rest of the run is unaffected"
                           % (type(error).__name__, error))

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
            self.report({'ERROR'}, "[%s] No chains matched the rules file - nothing to add." % RIG_TAG)
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
            # The metarig template and the settings used to come from MikuMikuRig, and this step used to refuse
            # without it ("MikuMikuRig is not installed, so there is no controller to add to"). Both ship with this
            # add-on now - `operators/RigOptions.py` and `operators/MMR_Rig.blend` - so there is nothing to be
            # missing; what is left to check is the leftover below, which is about a *file*, not an install.
            mmr = getattr(model, "uma_mmr", None)
            if mmr is None:
                self.report({'ERROR'}, "The rig options that ship with this add-on are not on this object, so "
                                       "there is no controller to add to. Nothing was changed. [%s]" % RIG_TAG)
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
                           "the model) and run this again. Nothing was changed. [%s]" % RIG_TAG)
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
                    self.report({'ERROR'}, "[%s] Could not enable rigify (%s)." % (RIG_TAG, error))
                    return {'CANCELLED'}

            # state the wrapper needs; nothing is shared through globals
            self._model, self._chains, self._options, self._geometry = model, chains, options, geometry

            saved = {key: getattr(mmr, key, None)
                     for key in ("Only_meta_bones_are_generated", "Hide_mmd_skeleton")}
            saved_active = context.view_layer.objects.active
            visibility = None
            rigify_module = original = wrapper = None
            state = {"done": False}
            # Rigify's own bone copying switches object mode twice per bone - 408 switches in this run, at 7.9 ms
            # each in a file with no rig and 50-58 ms in one that has a rig, which is seconds of the wait. The
            # batcher keeps the edit session open and copies the properties once per session instead; it is
            # installed for exactly the window below and always taken back out, and it reports rather than raises
            # if this rigify is not the one it was written against (`operators/RigifyCopyBatch.py`).
            batcher = Batcher() if Batcher is not None else None
            batch_mark = None
            if batcher is None:
                trace.info("rigify's bone copying was left alone: RigifyCopyBatch.py is missing")
            elif self.Batch_Rigify_Copies:
                if batcher.install():
                    batch_mark = batcher.mark()
                else:
                    # One line, at the point of refusal, naming why - and then rigify's own copying runs, which is
                    # slower but never different. The run's own line goes to the trace below, with the counts.
                    trace.info("rigify's bone copying was left alone: %s" % batcher.reason)
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
                # No `MMR_SECONDS["copy_batch"]`: `MMR_SECONDS` is a local of this function (it is created just
                # above the step plan and read by the timings lines below), so nothing outside `_run` could ever
                # read a key put in it - measured, and it cost a probe two runs to notice. The trace line is the
                # record, and it is the channel a report already reads.
                if batcher is not None:
                    trace.info(batcher.describe(batch_mark))
                trace.check("uma.mmr_rig returned FINISHED", result == {'FINISHED'}, str(result))
                # rigify's Generate operator catches exceptions from generate_rig and still reports
                # FINISHED, so the insertion's own record decides whether wiring may run at all
                if not state.get("done") or state.get("error"):
                    reason = state.get("error") or "rigify never called generate_rig"
                    trace.check("chains were inserted into the metarig", False, reason)
                    text_name = trace.save()
                    self.report({'ERROR'}, "[%s] The chains were not added to the metarig (%s), so nothing was "
                                           "wired. See the %s text block." % (RIG_TAG, reason, text_name))
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
                            % (RIG_TAG, error, TRACE_TEXT_NAME))
                return {'CANCELLED'}
            finally:
                # first, because it puts `bpy.ops` and rigify's own function back: everything below runs through
                # both, including the visibility restore and any operator the caller uses after this block.
                # `uninstall()` is unconditional and a no-op when nothing was installed.
                if batcher is not None:
                    batcher.uninstall()
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
                        % (RIG_TAG, TRACE_TEXT_NAME))
            return {'CANCELLED'}
        trace.check("generated rig found", True, "%s (%d bones)" % (rig.name, len(rig.data.bones)))
        # ...and it goes inside the model's hierarchy, or moving the character by its root empty leaves it behind
        adopt_rig_into_model(model, rig, trace)

        orgs = [names[0] for _rule, names in chains if ("ORG-" + names[0]) in rig.pose.bones]
        trace.check("chains were generated", len(orgs) == len(chains),
                    "%d of %d chain roots have an ORG copy" % (len(orgs), len(chains)))
        try:
            wired = self._wire_model_to_chains(
                model, rig, chains, trace,
                release_parents=bool(options.get("release_parents", True)),
                resample=resample,
                chain_deform=str(options.get("chain_deform", "rigid")).lower(),
                chain_link=str(options.get("chain_link", "world")).lower())
        except Exception as error:  # noqa: BLE001 - a half-wired model must not be left silently
            import traceback
            trace.check("wiring the model to the chains", False, "%s" % error)
            trace.info(traceback.format_exc())
            text_name = trace.save()
            self.report({'ERROR'}, "[%s] Wiring the model to the chains failed: %s (see the %s text block)"
                        % (RIG_TAG, error, text_name))
            return {'CANCELLED'}
        mark("wire the model to the chains")

        # The skirt master (2.32.0, `docs/SKIRT_RIG_STUDY.md` 8b): one control over every skirt panel, built on the
        # per-panel cascade rigify already generates. Off by default - it adds a bone - and it can also be asked for
        # from a rules file (`options.skirt_master`), the way the other rig-shaping choices can.
        if bool(self.Skirt_Master) or bool(options.get("skirt_master", False)):
            try:
                self._add_skirt_master(model, rig, chains, trace)
            except Exception as error:  # noqa: BLE001 - a rig without the master is still a rig
                trace.check("the skirt panels answer one master control", False, "%s" % error)
                trace.info("skirt master: could not be added (%s: %s)" % (type(error).__name__, error))

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
        # the spline families, for the offset note below (`_wire_model_to_chains` keeps its own copy for the link)
        spline_names = {name for rule, names in chains
                        if str(rule.get("type")) == "limbs.spline_tentacle" for name in names}
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
        # A spline chain is expected to sit off the model's rest position, and the reason is the rig type rather
        # than the wiring: `limbs.spline_tentacle` reproduces the metarig chain through its curve, and does not
        # reproduce it exactly - measured 0.015340 m on the reference model's tail, 0.13624 m on character 1001
        # (9r). Under `chain_link: local` the model bone copies that ORG pose, so the offset becomes visible on the
        # model; under `world` it was absorbed by the constraint's inverse matrix and invisible. It is reported as
        # an information line with the number, per family, and the check itself only fails on everything else -
        # silently passing a real displacement is worse than a named one, and failing on a rig type's own error
        # every run would train the reader to ignore the line.
        spline_displaced = [entry for entry in displaced if entry[0] in spline_names]
        other_displaced = [entry for entry in displaced if entry[0] not in spline_names]
        if spline_displaced:
            worst = max(spline_displaced, key=lambda entry: entry[1])
            families = sorted({name.rsplit("_", 1)[0] for name, _value in spline_displaced})
            trace.info("spline chains sit off their model rest position by up to %.5f m (%s, %d bone(s)): that is "
                       "`limbs.spline_tentacle` not reproducing the metarig chain through its curve, and the "
                       "`chain_link: local` link carries it - families %s"
                       % (worst[1], worst[0], len(spline_displaced), ", ".join(families[:6])))
        trace.check("the model is undisturbed at rest", not other_displaced,
                    "%d bones moved, worst %s" % (len(other_displaced),
                                                  other_displaced[:3] if other_displaced else ""))

        # Group the model's own bones so they can be switched off as a set. MMR leaves the MMD armature hidden,
        # but a bone in **no** collection is drawn whenever that object is shown, whatever the collection
        # toggles say - so unhiding it to check the wiring currently means 439 bones with no way to hide them
        # in groups. Purely additive: a visible collection changes nothing about what is drawn.
        if self.Step == 'FULL':
            model_visible = self.Chain_Layers != 'NONE'
            grouped = self._group_model_bones(model, trace, visible=model_visible)
            if grouped:
                # The line used to read "visible now, switch it off" whatever `Chain_Layers` said, and the
                # default is `Nothing` - so on a default run it told the reader the opposite of the truth about
                # the one collection that holds their model's own bones. That is what turned "where did my face
                # bones go" into a support question: they are all still there (measured: 106 face bones in, 106
                # out, none removed, only `Eye_L`/`Eye_R` carry a constraint, rest positions unchanged in world
                # space) but MMR hides the armature object and this collection is off by default.
                if model_visible:
                    detail = "visible now, switch it off in the armature's collection list"
                else:
                    detail = ("hidden now, because Chain Layers is 'Nothing' - switch '%s' on in the model "
                              "armature's Bone Collections to pose the model's own bones by hand (they are all "
                              "still here); note MMR hides the armature object itself too, so unhide that as "
                              "well, or tick MMR's 'Hide mmd skeleton' before a run" % MODEL_COLLECTION_NAME)
                trace.info("model bones grouped into '%s' (%d of them had no collection): %s"
                           % (MODEL_COLLECTION_NAME, grouped, detail))

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
        if steps:
            # The steps carry their own durations, so the line is built where they are (`steps.timings()`), which is
            # the format this trace has used since chains-40. The old `marks` arithmetic is kept for the case where
            # `Progress.py` is missing, since that is the only other way this line is built.
            timing_line = steps.timings()
        else:
            phases = []
            previous = 0.0
            for label, when in marks:
                phases.append("%s %.1fs" % (label, when - previous))
                previous = when
            phases.append("everything else %.1fs" % max(0.0, (time.time() - started) - previous))
            timing_line = "timings: %s | total %.1fs" % (" | ".join(phases), time.time() - started)
        mmr_seconds = MMR_SECONDS.get("mmr")
        insert_seconds = MMR_SECONDS.get("insert")
        trace.info(timing_line)
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
                   % (RIG_TAG, len(chains), wired, ", ".join(present), text_name))
        if trace.failures:
            self.report({'WARNING'}, message + ". Checks failed: " + "; ".join(trace.failures[:4]))
        else:
            self.report({'INFO'}, message)
        return {'FINISHED'}

class RigifyChainStyleSave(bpy.types.Operator):
    """Save how the rig in this scene is displayed as a style file"""

    bl_idname = "uma.rigify_chain_style"
    bl_label = "Save style from this rig"
    bl_description = ("Save how the generated rig is displayed - bone colours, widget sizes, display flags - as a "
                      "style file. Display only: it cannot change what the rig does")
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
    bl_description = ("Open the folder holding one file per rig display style, or read it again after editing one "
                      "by hand")
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
    bl_description = ("Open the folder holding the presets and the rules file, or write the rules file out with "
                      "the built-in defaults so it can be edited")
    bl_options = {'REGISTER'}

    Action: EnumProperty(
        name="Action",
        items=[('SHOW', "Open The Presets Folder", "Open the folder holding one file per preset. The rules table and "
                                                   "run options are in rigify_chains.json one level up"),
               ('WRITE', "Write Defaults", "Overwrite the rules file with the built-in defaults, keeping a backup of "
                                           "what it replaces"),
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
            # the presets folder, not the file: a preset has its own file now, and that is what someone opening
            # this wants to look at
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
