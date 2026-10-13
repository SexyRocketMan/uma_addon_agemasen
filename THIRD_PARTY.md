# Third-party code in this add-on

This add-on is GPL-3.0-or-later (`LICENSE` beside this file).

| what | author | licence | link |
|---|---|---|---|
| MikuMikuRig - the rig pipeline, the metarig template and the rig settings | 小峰峰哥 / XiaoFFGe | GPL-3.0 | <https://github.com/XiaoFFGe/MikuMikuRig> |
| `Umashaders` 0.2.6 - the default shader | Looper | the upstream project's | shipped unmodified; terms in `uma_addon/addons/uma_addon/operators/shaders/umashader-0.2.6.json` |
| Spork's shader - the second shader | Spork | used with his permission, credits to stay in place | terms in `uma_addon/addons/uma_addon/operators/shaders/spork-1.0.json` |
| `mmd_tools`, `rigify` | their authors / Blender | not bundled - required, unmodified | `rigify` is patched in memory only, for the duration of a rig run (`operators/RigifyCopyBatch.py`) |

**Modified**: MikuMikuRig is vendored, not bundled whole. `operators/MMR_Rig.blend` is their file unchanged;
`operators/RigOptions.py` carries their setting names and defaults on this add-on's own properties; and
`operators/Controller.py` is a long-standing fork of their `operators/RIG.py`. Everything else here is ours.
