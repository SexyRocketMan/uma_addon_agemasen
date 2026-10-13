# Third-party code and assets in this add-on

This add-on is **GPL-3.0-or-later** (`blender_manifest.toml`, and `LICENSE` beside this file is the verbatim
GPL-3.0 text). It carries work by other people, listed here so the credits travel with the add-on and so it is clear
what is theirs, what is ours, and what was changed.

## MikuMikuRig - 小峰峰哥 / XiaoFFGe

* <https://github.com/XiaoFFGe/MikuMikuRig>
* **GPL-3.0** (GNU General Public License v3.0), the same licence as this add-on, so the two are compatible by design.
* This add-on used to be a *dependency* on MikuMikuRig and no longer is; the parts it needed are vendored here.

**What was vendored, and what was changed.** Every change is named, because GPL-3.0 asks for that:

| what | where | the change |
|---|---|---|
| the metarig template `MMR_Rig.blend` | `uma_addon/addons/uma_addon/operators/MMR_Rig.blend` | **unmodified**: byte-identical to MMR release `4.5-3.85` and to `5.2.2`, sha256 `12374F03DAA8EBA5FD54AD57…`, 285 696 bytes |
| the rig settings | `uma_addon/addons/uma_addon/operators/RigOptions.py` (`RigOptions`) | **modified**: the 22 settings' names, types and default values are MMR's (read out of MMR's own runtime, not retyped from its source), but they live on *this* add-on's own object property (`object.uma_mmr`) instead of MMR's (`object.mmr`), so both add-ons can be installed at once. `presets` is a `StringProperty` here where MMR's is a dropdown over its own preset folder |
| the per-bone group | same file (`BoneOptions`) | **modified**: same story - MMR's fields and defaults, registered as `PoseBone.uma_bone` instead of `PoseBone.mmr_bone`. Only `Set_constraints` is read by this add-on |
| the weight-bone-parent table | same file (`WeightBoneParentItem`, `fill_default_weight_bone_parent`) | **modified**: MMR's six defaults are the same six rows, written by a function here where MMR had an operator (`mmr.import_default_weight_bone_parent`) |
| the rig pipeline | `uma_addon/addons/uma_addon/operators/Controller.py` | **modified fork** of MMR's `operators/RIG.py` (1 975 lines against their 2 022). It has been a fork of this add-on for a long time, not something this vendoring introduced; the refusals that used to name an MMR version were removed when the dependency was |

Nothing else of MMR is included: not its panels, not its physics, retarget, preset or animation-copy modules, not its
other metarig templates, and none of its UI. A FULL `Pumpkin Rig` run works with MikuMikuRig **absent** - measured,
with the same rig fingerprint as when it was installed and used (`mmr_freedom_check.py`, 16 checks) - and if it is
installed anyway, both add-ons coexist because this one never claims a property name of theirs.

## Looper - `Umashaders` 0.2.6 and the bundled `Umashaders.blend`

* `uma_addon/addons/uma_addon/operators/Umashaders-0.2.6.blend` is upstream's released library, **dropped in
  unmodified** (sha256 `97f2aa03e5f581398690bc66f856bd408d23490e9dc08e20bb7075a2d07f97e5`, 198 532 bytes), and
  `Umashaders.blend` is this project's own file built from that line of the shader, carrying the ear/tail action
  library as well. `operators/shaders/umashader-0.2.6.json` is the manifest that describes it.
* The terms are the ones this add-on has shipped these files under since before this note existed; they are not
  restated here because this file only records what the vendoring added. If a licence line is wanted for these two
  files, it belongs in that manifest's `notes`, which is where everything else about them is documented.

## Spork - the second shader

* `uma_addon/addons/uma_addon/operators/SporkShaders.blend`, built from Spork's own project file
  (`Big Samson.blend`) by `build_spork_library.py`, and described by `operators/shaders/spork-1.0.json`.
* Included **with his permission and on the terms he gave**: his shader ships complete, *including the third-party
  parts*, as long as the credits stay in place. The group is 26 nodes and no image is bound (a library carries slots,
  not textures); the credits and the measured details are in that manifest's `notes` and in `docs/SPORK_RIG.md` of
  the development repository.

## This add-on

* `uma_addon` "Agemasen edition" - the fork of UmaViewer's Blender add-on maintained at
  <https://github.com/SexyRocketMan/uma_addon_agemasen>, GPL-3.0-or-later, by the maintainers named in
  `blender_manifest.toml`.
* `mmd_tools` is not bundled: it is required to be installed, and it is what imports the models.
* `rigify` is not bundled either, and is not modified on disk. Since 2.29.0 this add-on replaces one *function* inside
  it at runtime, for the duration of a rig run, to batch rigify's per-bone mode switching
  (`operators/RigifyCopyBatch.py`); it is put back when the run ends, and the switch in *More settings* turns the
  whole thing off.
