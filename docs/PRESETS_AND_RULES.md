# How the chain data is organised (a 101)

Everything the rig setup needs to know lives in **one folder**, and there are exactly **two kinds of thing** in
it. This is the map.

```
<Blender config>/uma_addon/          e.g. C:\Users\<you>\AppData\Roaming\Blender Foundation\Blender\5.2\config\uma_addon
├── rigify_chains.json               the RULES TABLE + the run options  (~3 KB, 13 rules)
├── presets/                         ONE FILE PER PRESET
│   ├── README.txt                   the same explanation, written for whoever opens the folder
│   ├── One_control_per_bone.json    the shipped generic presets (2 of them)
│   └── Chains_everywhere.json
├── presets_retired_character/       the 99 character-keyed presets, moved aside by chains-56 (see below)
├── trace_<date>.txt                 written by "Save trace to a file"
└── rigify_chains.before_*.json      backups, made automatically before a rewrite
```

The Blender config folder is the right home for this because it is **per user, not per .blend**: a preset you
save here is there for every project afterwards.

## The two kinds of thing

**A preset** is a **saved set of answers** — what the rig type fields said when somebody pressed `Save as
preset`. It answers by *group* and/or by *family*:

```json
{
  "key": "One control per bone",
  "groups": { "Hair": "basic.super_copy", "Skirt": "basic.super_copy", "Tail": "basic.super_copy" }
}
```

```json
{
  "key": "my own",
  "families": { "Sp_He_Hair4": "limbs.spline_tentacle", "Sp_Hi_MSkirt0": "spines.basic_tail" }
}
```

`groups` answers for every family in that group, `families` answers for one family, and where both name the same
family `families` wins. That is what lets a small generic preset (three lines, means the same thing on every
model) and a specific one (twenty families) be written the same way. The groups are `Hair`, `Skirt`, `Chest`,
`Tail`, `Extras` and `Other`; a family's group is derived from its own name (`family_group`).

**Nothing picks a preset automatically.** The list opens on `Default`, which *is* the rules table; a preset
applies because somebody chose it and not otherwise. There is no autodetection, no key based on the character,
and no preset that only applies to one costume — that whole layer was removed in `chains-56` (§ below), because
it silently beat the rules table and made "set every family to `basic.super_copy`" produce a mixed rig.

**The rules table** (`rigify_chains.json`) answers *"which rig type does a bone family get when nobody has said?"*
— by name pattern, for any model, including ones nobody has rigged yet. It is the only thing that decides a run
nobody has configured.

```json
"rules": [
  {"pattern": "_Handle$",       "skip": true},
  {"pattern": "^Ear_0",         "type": "spines.basic_tail", "collection": "Ears", ...},
  {"pattern": "^Sp_He_Hair2",   "type": "spines.basic_tail", "collection": "Hair", ...},
  {"pattern": "^Sp_Hi_MSkirt",  "type": "spines.basic_tail", "collection": "Skirt", ...},
  {"pattern": "^Sp_",           "skip": true}
]
```

plus an `options` section for things that are not per-family at all: `never_rig` (families no mode may touch),
`fixed_families` (the families his rigs never vary — the **ears** only: the tail was fixed too until a model
turned up whose short tail the spline control lands far off, so it has a field again and defaults to the spline
his 241 rigs use), **`unmatched_families`** (what a family *no rule names* gets — one control per bone,
`basic.super_copy`, which is his own dominant answer for exactly those families; without it they fell through the
`^Sp_` catch-all with no control and no message), the default `chain_deform`, and the weight/parent rules.

A family no rule names is also the only case where the group dropdowns do the work rather than the table: it gets
its group from its own name (`family_group`), so `Sp_He_Hair0` lands in `Hair` and a sleeve in `Other`.

## How they work together in a run

1. The rules table answers for every family the model has, and the fields are filled with those answers.
2. If — and only if — a preset is selected, it overwrites the fields for the families it answers for. It stops
   there: what the panel shows is what the rig gets.
3. Any family the preset does not answer for — a `Hair6` it has never heard of — stays with the **rules table**.
4. The run reports every family it decided and where the answer came from, so a wrong rig is a line to read
   rather than a guess.

You never have to edit either by hand: the dropdowns are the front end, `Save as preset` writes a preset file,
and the panel's folder button opens `presets/`.

## Why a preset is keyed to nothing (it used to be keyed to a character)

Until `chains-26` the presets were a `profiles` section *inside* `rigify_chains.json`. `chains-26` moved them into
one file each, but kept what they *were*: **99 presets read out of the animator's own rigify metarigs, one per
character and costume**, keyed `1004_00`, with `source: Meta_Base_Head_Maruzensky` saying where the numbers came
from. `chains-56` removed that layer, for a reason that had already bitten twice:

* the model's own `Chara_<id>_<costume>_Handle` bone (or, failing that, its hair skeleton) **autodetected** a
  preset, and a preset's answers beat the rules table. So "type every family `basic.super_copy`" on `1004_00`
  produced a rig whose hair families were still chains — the preset had seeded them, and nothing in the panel
  said so. The user's report was the mixed result: *some bones answer and some do not*;
* the same file was also *the pack's data* — one entry per character out of a pack of 241 rigs — which is the
  "replicating the animator's rigs" the user asked to let go of: *"just the default ruleset with options of
  overrides ... maybe with a few small presets, but without character based preset search"*.

What `chains-56` did, in order:

| | before | after |
|---|---|---|
| who picks a preset | the model's character bone or hair skeleton | nobody: you do |
| a preset's key | `1004_00`, a metarig name, or a costume id | a name you typed (`my own`) |
| a preset's scope | one character *and* costume; another costume's preset applied to hair only | every model, by group and/or family |
| what a fresh install has | nothing until a run reads the old `profiles` section | two shipped generic presets |
| the 99 derived files | the list | moved to `presets_retired_character/`, whole files, nothing deleted |

The move is one-time and reversible: `retire_character_presets` skips anything that says `"source": "saved in
Blender"` (an older build's own saves) and anything with no `source`, and it names the folder in the trace. Their
answers are still readable as JSON if a character's rig typing is ever wanted as a starting point — but nothing
reads them any more, so a model's rig is now the same on every machine.

## Adding a preset by hand

Copy any file, change `key` (and `groups` / `families`), save it as anything ending `.json`. It shows up in the
preset list on the next redraw. Nothing else about the name matters — `key` is what has to be right.

The rig types a preset may use are the four the animator's own rigs use:

| value in a preset | what it means in the rig |
|---|---|
| `basic.super_copy` | one control per bone, no chain (his "Follow") |
| `spines.basic_tail` | a chain per root with tweak controls; curls when a control is pushed |
| `limbs.spline_tentacle` | a chain driven by a spline: softer, rounder |
| `basic.raw_copy` | a bare copy, treated as `basic.super_copy` here |
| `""` | no rig type: the bones keep MMR's behaviour and get no control (the `^Sp_` catch-all) |
