# Rebuild verification

What was actually run against the rebuilt tree, on which Blender build, and what came out. Everything below
was executed, not inferred. The model used for the real-model runs is `1002_00.pmx` (Silence Suzuka, 423
bones, 240 morphs, 11,114 vertices) imported with mmd_tools; the operator harness uses real actions out of
the shipped `Umashaders.blend`.

## Installs

| check | 4.2.23 LTS | 4.5.14 LTS | 5.2.0 LTS |
|---|---|---|---|
| `--command extension validate` on the built zip | Success parsing TOML | Success parsing TOML | Success parsing TOML |
| `install-file -r user_default -e` | installed, enabled | installed, enabled | installed, enabled |
| `uma.*` operators registered | 41 | 41 | 41 |
| `utils/compat.py` flags | `SLOTTED=False, POSE_SEL_5x=False, LEGACY_ACTION=True` | `SLOTTED=True, POSE_SEL_5x=False, LEGACY_ACTION=True` | `SLOTTED=True, POSE_SEL_5x=True, LEGACY_ACTION=False` |
| `utils/naming.py` on a tagged mesh | `EyeBrow_1_R(WaraiA)[M_Face]`, convention `tagged` | same | same |
| folder-wrapped zip (the shape that failed for users) | installs, enables, no traceback | - | installs, enables, no traceback |

4.5 is the hybrid case: action slots exist (since 4.4) while `Bone.select` / `Bone.hide` are still the 4.x
spelling and the legacy `Action` API is still present. The layer selects exactly that combination.

The folder-wrapped row is the community's failure: a zip whose contents sit inside a folder has no manifest at
the archive root, `validate` refuses it, Blender falls back to the legacy installer, and `auto_load.py` used
to import the nested package under the wrong name (`ModuleNotFoundError: No module named 'uma_addon.addons'`).

## Shipped `Umashaders.blend`

Upstream's file (4,781 actions, 4 materials, 6 node groups, 1 collection), written by **Blender 4.5.91**.
Both older builds read it: `libraries.load` appends the shader material and the `Uma Outlines` node group on
4.2.23 and on 5.2.0, and opening it outright works too. The fork's own 198 KB blend, by contrast, carries
**0 actions**, so its UMA Action list would have been empty.

## Operators that the compatibility layer touches

A harness that calls `ApplyAction`, `ApplyKeyAction` and `BonePositionToZero` directly with real actions:
**19 checks, 0 failures on 4.2.23, 4.5.14 and 5.2.0** - 20/20 pose curves and 8/8 shape-key curves copied
with matching values, times, interpolation and group names, the missing-bone skip intact (`16 curves copied,
expected 16`), and the target's id recorded as `id_root == 'KEY'` on 4.2/4.5 against the slot's
`target_id_type` on 5.2.

A real-model run of the armature operators, from the installed zip, **6 of 6 finishing on all three builds**:
`set_bone_collections`, `refine_bone_structure` (naming-driven eye controls), `generate_ik` (the pose-bone
hiding path), `toggle_twist_constraints`, `group_fcurves_by_bone` (legacy versus slotted F-curves) and
`changehead_pretreat` (the mmd_tools pose-selection poll that used to fail on 5.x).

### The bug the 4.5 run caught

On 4.5 `ApplyKeyAction` copied **0 of 8 curves** while `ApplyAction` copied 20/20. The cause was the order of
the routes in `compat.ensure_fcurve()`: it preferred `action.fcurve_ensure_for_datablock()`, and on 4.4/4.5 -
the only versions that have both APIs - that helper puts the curve into a channelbag of a slot of its own,
which the legacy `action.fcurves` view (what the rest of the addon, the UI and `nla.bake` read there) does not
see. 4.2 and 5.2 both passed because only one route exists on each. The function now takes the legacy
collection first wherever it exists and falls back to the helper, then to the channelbag, only when it is
gone, so every version ends up writing where that version reads.

## The Shading operator's outline inputs

`set_geometry_modifier_input()` has to reach a node-group modifier's inputs, and the route changed:
`modifier.properties.inputs[identifier]` on 5.2, ID properties on the modifier before that. Read back from
the modifier after running Shading on a real model:

| input | expected | 4.5.14 (`has .properties: False`, legacy route) | 5.2.0 (`properties` route) |
|---|---|---|---|
| `Thickness` | 0.35 from the export's comment | 0.35 | 0.35 |
| `Depth Offset` | 0.1 | 0.1 | 0.1 |
| `Camera` | the scene camera | set | set |
| `Outline Material` | the shipped material | set | set |

## The UI, which the operator runs could not see

One of the faults reported after the first ship was `UILayout.template_list(): error with keyword argument
"type" - enum "GRID" not found in ('DEFAULT', 'COMPACT')`, raised while drawing the Action panel. Blender 5.0
removed the `GRID` value; every operator test above called `bpy.ops` directly, and none of them draws a panel,
so nothing here caught it. `compat.TEMPLATE_LIST_GRID` now resolves to `GRID` below 5.0 and `DEFAULT` from
5.0, and there is a harness that draws every UMA panel through a stand-in layout which rejects a `type` the
running Blender does not offer - the same check the real `UILayout` performs.

| | 4.2.23 | 4.5.14 | 5.2.0 |
|---|---|---|---|
| `template_list` types the build offers | `DEFAULT, COMPACT, GRID` | `DEFAULT, COMPACT, GRID` | `DEFAULT, COMPACT` |
| value the addon passes | `GRID` | `GRID` | `DEFAULT` |
| panels drawn (4 panels x 3 action states) | 12 ok, 0 raised | 12 ok, 0 raised | 12 ok, 0 raised |

## Shading, and the outline

`Umashader.py` is upstream's operator with one change: the outline modifier's depth offset is written through
`set_modifier_input()`, because upstream's `geom_mod["Input_15"] = 0.1` is an ID property write that 5.2
rejects. Running it on a real model, from the installed zip:

| | 4.2.23 | 4.5.14 | 5.2.0 |
|---|---|---|---|
| result | FINISHED | FINISHED | FINISHED |
| `Uma Outlines` modifier + node group | added | added | added |
| `Depth Offset` | 0.1 (ID property) | 0.1 (ID property) | 0.1 (`properties.inputs`) |
| material slots switched to a shipped shader group | 7 of 8 | 7 of 8 | 7 of 8 |

The outline's own behaviour on an Agemasen export is measured in `docs/OUTLINES.md`: with the export's own
`UV3` the shell is displaced 0.0013 units on a 1.65 unit tall model and covers 7.9% - 10% of the frame in its
material. An earlier build of this tree worked around that with a neutral width and its own outline material;
the owner asked for upstream's shader back, so the workaround is gone and the measurement stands as a record
of what upstream's code does with these exports.

## Not verified here

* **The exporter-side fix for a visible outline.** Blender-side the shell is now harmless, but a real outline
  needs the outline normal set in that channel at export - see `docs/OUTLINES.md`.
* **The MikuMikuRig integration** (`uma.mmr_rig`): it is guarded against a missing or older MikuMikuRig, but
  no MikuMikuRig was installed in the profiles these runs used.
