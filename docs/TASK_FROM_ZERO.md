<!-- superseded -->
> **Note.** Superseded in places. Read `GROUND_TRUTH.md` first: it states what is established and lists what has been disproved, including two claims made in this file - the "22.86% of the face's normals already differ" elimination, which was an artefact of a histogram, and the reading that the port's ~50-level brightness gap was unmeasured, which turned out to be camera framing. The mask's role is also stated differently there: it biases where the boundary falls rather than drawing it.

# The face shading task, from zero

Everything needed to pick this up cold: what the task is, what has been established, what has been retracted, which
tools exist, and the next concrete step with the exact commands. Written after a long session that read the game's
shader out of its bytecode.

---

## 1. The task

**Goal:** make the Blender render of an exported Umamusume character match the game's own face shading.

**Why it matters:** this fork of the addon, and the viewer it exports from, exist to get game-accurate renders of
Umamusume models in Blender. The face is the visible failure: it does not shade the way the game shades it.

**The specific symptom:** the lit/shaded boundary on the face appears as **hard-edged polygonal patches** — straight
lines following the mesh — instead of the game's smooth result, and it moves wrongly with the light.

**Status: measured as fixed.** The blocker for weeks was that the two renderers frame the head differently, so every
comparison was between different parts of the model. The facial-shadow probe breaks that: rendering the same frame with
the region path off and on marks the face, so the changed pixels' **centroid** is a landmark present in both renderers
and their count gives the scale; window both on it and the two are comparable pixel for pixel. Aligned, in the skin, at
three sun azimuths: the lit level measures **235 in both** renderers, the shaded skin is 3.2% of the face in the viewer
against 2.4% in the port, the transition band 0.74-0.83% against 0.63-0.66%, the boundary 1810-1849 px against
1438-1456 px. Classified pixel by pixel over the 43.5k pixels both call face: **both lit 95.1%, both shaded 0.2%, the
game shades and the port does not 2.6%, the port shades and the game does not 2.0%** — the two one-sided disagreements
equal and opposite, at every azimuth, which is the signature of the same shape in a slightly different place rather
than of a different shape. The boundary is a hair shorter and its transition a fifth narrower than the game's, which is
what a smoother line looks like rather than a facetted one. Full numbers and the figure: `FACE_PIXEL_PROGRAM.md`, "The
boundary itself, measured with the two renders aligned on the face". Instruments:
`_uma_scratch/skin_boundary.py`, `boundary_agreement.py`, `face_interior.py`, `disagreement_regions.py`.

**How success is judged:** rendering the same character, morph and light as **UmaViewer with the game's own shader**
and getting the same picture. That reference is not an opinion; the viewer loads the vendor's compiled shader and
runs it, so it is ground truth for how the game looks.

**A warning about light:** almost every wrong conclusion in this work came from comparing two renders that were not
lit identically. Print the light vector before comparing anything.

---

## 2. The repositories

| | |
| --- | --- |
| Blender addon (fork) | `C:\Users\user\Documents\dev\Blender-Uma-Addon`, branch `unified-morph-naming`, local only |
| Viewer (fork) | `C:\Users\user\Documents\dev\UmaViewer-PMX-Export-Patch`, branch `fix/eye-bone-export` |
| Scratch (not a repo) | `C:\Users\user\Documents\dev\_uma_scratch` — probes and one-off scripts |
| Renders | `C:\Users\user\Documents\umaviewer_exports\grids\` |
| Blender | `C:\Program Files\Blender Foundation\Blender 5.2\blender.exe` |
| Unity | `C:\Program Files\Unity\Hub\Editor\2022.3.62f1\Editor\Unity.exe` |

The addon's installed copy is at
`%APPDATA%\Blender Foundation\Blender\5.2\extensions\user_default\uma_addon\uma_addon\addons\uma_addon`.
**After editing any `.py`, copy it there and verify by hash** — a stale installed copy has produced invalid test
results twice, once silently invalidating a round of user testing.

---

## 3. Where the shading is implemented

`uma_addon/addons/uma_addon/operators/Umashader.py`, in two functions:

- **`apply_game_normal_blend`** — the game's cylinder blend, from its *vertex* program, baked as per-vertex custom
  split normals:
  ```
  radial = normalize(P - (C + dot(Up, P - C) * Up))     the direction outwards from the head's axis
  weight = (1 - _CylinderBlend) * vertexColour.b        the vertex colour's BLUE channel
  N'     = radial + weight * (N - radial)               left non-unit, as the game leaves it
  ```
- **`add_game_face_shading`** — the game's lit/shaded step, from its *pixel* program, built as nodes in the material
  and handed to the shipped group as its diffuse texture, with the group's own light threshold zeroed:
  ```
  halfLambert = 0.5 * dot(N, L) + 0.5
  t           = (_ToonStep - _ToonFeather - _TripleMaskMap.r * halfLambert) / _ToonFeather
  step        = saturate(1 + t);   if (_ToonFeather <= 0) step = 1 - step
  colour      = lerp(diffuse, toon, step)
  ```
  The mask is `_TripleMaskMap.r` (the `*_base` texture), not `_ToonMap.r`: with `_ToonFeather` 0.001 the step is hard
  either way, so what decides where the boundary falls is *which texture's channel multiplies halfLambert*, and only
  the painted mask gives the game's shape. `--face-mask toon` reproduces the old behaviour.

  Then the cheek and nose regions as a multiply by `_faceShadowColor`, per the same program:
  ```
  factor = saturate((v8.y - _faceShadowEndY) / _faceShadowLength) * _faceShadowAlpha * _TripleMaskMap.b
  colour = lerp(colour, colour * _faceShadowColor, factor)
  ```
  `v8.y` is the vertex's height inside the head, which the exporter writes into the spare `.z` of the pmx's second
  extra uv and which arrives in Blender as `_UV2.uv.x`. Blue unmoved is the signature of the multiply, and it is what
  the port's output matches the game's on. The port's previous region was a colour switch that was the identity almost
  everywhere and has been removed.

Both are reached only when the panel's single tickbox is **unticked** (`legacy_shading = False`). Legacy is the
default. The panel deliberately has one checkbox; anything else is an operator property, scriptable but not drawn.

---

## 4. The ground truth, and how it was obtained

The shaders ship as compiled **DXBC** in encrypted bundles. The full chain is implemented in the viewer repo at
`Tools/game_rip/`, with a from-zero recipe in its `README.md`:

1. the meta database is SQLite3MC — `sqlite3_key` with a derived key, then `SELECT m,n,h,c,d,e FROM a`;
2. bundles are UnityFS with the first 256 bytes clear, then XOR with a keystream built from the entry key;
3. `UnityPy` extracts the assets;
4. the shader blob is an LZ4 block that expands to an index plus DXBC containers;
5. `dxbc_disasm` disassembles them with `D3DDisassemble`.

**Two traps, both costly:** a DXBC container declares its own length at offset 24 — slicing to the next `DXBC` magic
truncates any container that has a `DXBC` string inside it and every blob then fails with E_FAIL. And `ID3DBlob`
must be read through its vtable: slots 0-2 are QueryInterface/AddRef/Release, so the buffer accessors are 3 and 4.

**The saved disassembly** is in `Tools/game_rip/shader_sources/`: `face_vertex.asm.txt`,
`face_pixel_use_mask_color.asm.txt`, `face_pixel_8tex.asm.txt`, and two small passes. Raw blobs are deliberately not
kept — they are the game's assets and the repo is public. The analysis is kept.

**The write-ups:**
`docs/GAME_FACE_SHADER_READ.md` (the shader, term by term), `docs/GAME_SHADER_GROUND_TRUTH.md` (extracted values and
bindings), `docs/reviews/REVIEW_face_shader.md` (an independent line-by-line review), `docs/REMAINING_WORK.md`
(everything below in more detail).

---

## 5. Known for a fact, with how to re-check

| fact | value | how it is known |
| --- | --- | --- |
| The face shader | `Gallop/3D/Chara/DitherToonFace/TSER` | read from the bundle |
| The step's constants | `_ToonStep 0.4`, `_ToonFeather 0.001` | the material, and the exported comment agrees |
| The step's mask | `_TripleMaskMap`'s **red** channel (the export's `*_base`) | the operand is `t1.r` in the pixel program. The containers ship **no RDEF chunk**, so `t1` has no name in the bytecode; the name comes from the shader's property declaration order (`t0`=`_MainTex`, `t1`=`_TripleMaskMap`, `t2`=`_OptionMaskMap`, `t3`=`_ToonMap`, ...), checked against the variant register counts. Labelling `t1` as `_ToonMap` was a guess from the property list - see `FACE_MASK_AND_VARIANTS.md` |
| The region mask | the same texture's **green** channel | the cheek and nose gates compare `t1.g` against 0.51 and 0.49 |
| The shader's keywords | 8, and `_USEOPTIONMASKMAP_ON` is **not** one of them | `m_ParsedForm.m_KeywordNames`: STEREO_*, `_MAIN_LIGHT_SHADOWS`, `_ADDITIONAL_LIGHTS`, `USE_MASK_COLOR`, `_ADDITIONAL_LIGHT_SHADOWS` |
| The variant the game runs | the plain **7-texture** one; `USE_MASK_COLOR` is off | the live material's enabled keywords are `_USEOPTIONMASKMAP_ON` alone, so `_MaskColorTex` is never sampled |
| The option mask | a **uniform** branch, not a keyword | `_UseOptionMaskMap` = 1, and `_OptionMaskMap` bound to the character's `*_ctrl` texture (`t2`); the port does not implement that branch |
| The face normals in the PMX | **exactly** the game's field, rotated 180 degrees about y with the model | line-by-line against the record the exporter makes as it writes (`ModelExporter.VertexDumpPath`, from `-DumpFace`): worst component difference 0.000000. The earlier "22.86% different" came from the angle-to-the-mean histogram and is retracted |
| The pmx coordinate space | `(-x, y, -z)` of Unity root space, divided by `WorldSizeAmplifier` 0.08, i.e. times 12.5 | `MMDReaderWriteUtil.WriteAmpVector3`; normals go through the same negation but are not scaled |
| Specular / rim gating | `_TripleMaskMap.r` / `.b` | the operands at those two points |
| Cheek and nose thresholds | **0.51** and **0.49** on `_ToonMap.g` | literals in the bytecode |
| Cylinder weight source | vertex colour **blue** (`v3.z`) | the vertex program |
| `_CylinderBlend` at runtime | **0.25** (the *stored* material says 0) | the pipeline dump, from the live material |
| Mesh normals | all exactly unit length | the pipeline dump; so `_NormalizeNormal` is moot |
| `_faceShadowAlpha` | 0 at rest, and **driven** per expression | `Gallop/FaceDrivenKeyTarget` binds it to a `Shade_Ctrl` morph |
| Mask colours | 12, per character, set at runtime | `UmaContainerCharacter.SetMaskColor` |
| Face frame | `_FaceForward`/`_FaceUp`/`_FaceCenterPos` + `_faceShadowHeadMat`, from the head bone every frame | `UmaContainerCharacter` lines 430-434, under the comment "Used to calculate facial shadows" |
| No `fwidth` anywhere | the face shader has no derivatives | opcode inventory of all three programs |
| The face has no `*_area*` texture | so the region path has nothing to switch on for this character | the material's `_MaskColorTex` is null at runtime |

**The database can be trusted; everything after it must be verified.** The stored material is *not* the runtime one —
the viewer overwrites textures and properties when it loads a character.

---

## 6. Retracted conclusions, and why

Each of these was asserted and then disproved. They are listed because the *reasons* are the useful part.

1. **"The triple mask's blue channel drives the toon blend."** From reading the first 60 instructions. The rest of
   the program shows blue gating the *rim* and `_ToonMap.r` driving the step. *Half a program is not a program.*
2. **"`_MaskColorTex` is dead on the face."** From the stored material's null binding. The viewer binds it at
   runtime (though for this character it resolves to nothing). *A stored asset holds what was saved, not what runs.*
3. **"`_ToonMap` is a shaded colour texture."** Its red and green channels carry the painted mask. *A histogram cannot
   tell a soft mask from a flat fill* — the green channel measured 0% bright and I read that as "no information".
4. **"The hard edges are faithful output for this character."** From `_faceShadowAlpha` being 0. That value is not a
   material constant at all, it is expression-driven; and the viewer renders the same model cleanly.
5. **"The artifact is certainly ours."** From a three-way comparison against the viewer — but the two tools were
   lighting the model at different elevations (70° vs 35°). Retracted pending a matched light.

**The lesson that covers all five:** render the value, or read the instruction that produced it. Never reason from a
summary, a stored asset, or a render whose inputs you have not printed.

---

## 7. Eliminated, with numbers

All comparisons are oblique light on the face, face-region mean luminance, character 1001 with `Mouth_OdorokiA=1.0`.

| test | ours | viewer | conclusion |
| --- | --- | --- | --- |
| frontal light | 215.5 (legacy 214.6) | — | agrees with legacy; the face is clean |
| elevation 35 | 195 | 147 | ours patches, the viewer does not |
| elevation 70 (matched to the viewer) | 197 | 147 | elevation was a confound, **not** the cause |
| elevation 70, cylinder blend off | 197 | 147 | **the cylinder blend is eliminated** |
| elevation 70, the mask read from `_TripleMaskMap.r` instead of `_ToonMap.r` | 200 | 147 | the mask source was wrong and is now right, but it **moves 1-2% of pixels** and changes nothing else - **eliminated as the artifact's cause** |

Also eliminated: `_NormalizeNormal` (normals are all unit length), and the step's constants and texture files, which
match the game exactly.

---

## 8. What is left, and the next step

**Candidate 1 — the normal field as it arrives: eliminated.** The exporter was instrumented to record
every vertex as it writes it, and compared line against line the pmx is exact - position, normal, uv and
vertex colour all agree to float32 precision over the face's 1328 vertices. The earlier claim that
22.86% of the face's normal bin assignments already differ inside the pmx is **retracted**: it came from
the angle-to-the-mean histogram, which is hypersensitive because its reference is the mean of the same
data. The field is identical up to the writer's documented `(-x, y, -z)` convention, so the export is
eliminated and the shading difference is not there. See `GAME_DATA_VS_VIEWER.md`.

What is still unexplained on the geometry side, and much smaller: `BakeMesh` perturbs the normals it is
handed (median 1.80 degrees against the shared mesh's over 1330 vertices), though the exporter writes the
shared mesh's normals; and the addon's own custom-normal write moves scores of face vertices.

The next candidate on the export side is therefore not the geometry but the **material comment**. That is
now answered for every value the port reads, and it is answered in the *runtime's* favour wherever the two
disagree: the exporter reads the live material, so it carries `_CylinderBlend` 0.25 - the viewer's own
override, not the asset's 0 - and the rim colour (0,0,0,0), the viewer's override, not the asset's
(1,1,1,0.392). `GAME_DATA_VS_VIEWER.md` has the table and `FACE_PIXEL_PROGRAM.md` the re-check.

**Candidate 2 — the option-mask branch: still not implemented, and a no-op on this character.** At runtime
it is live (`_UseOptionMaskMap` = 1, `_OptionMaskMap` bound to the character's `*_ctrl`), but its red and
green channels measure **exactly 0.000 over every texel** for character 1001 and its blue means 0.036, so
the game multiplies all three gated terms out as well - the port's silence on them is correct here, and only
here. The branch is the difference between a port and a port-for-one-character. Its shape, read out of the
program: red gates the specular, green the `_ToonMap` tint, blue the rim, and the *blue is a lerp weight*
rather than a multiply - the sampled blue becomes `lerp(blue, 0.5, _UseOptionMaskMap)`, which is why a black
option mask still leaves a rim band at the threshold 0.5. This is the one implementable item left in the
face program.

**Candidate 3 — the light: closed.** The two tools now render with the same vector, printed by both
(`blender = (ux, -uz, uy)` from the direction the viewer logs), and the viewer's light is white at intensity
1.0. The remaining difference between the renders is not the light: with the light matched and the two
aligned on the face, 95.1% of the face's pixels are classified the same way by both.

**The unexplained ~50 levels: it was framing, and that is now settled rather than suspected.** Aligning the
two renders on the facial-shadow landmark puts the face's mean luminance at 222.1 for the viewer against
224.2 for the port, a 1% difference, with the same lit level of 235 in both. Section 11 below says the
opposite and is superseded.

**Note on the toon texture:** considered and set aside — the two textures differ by only 16.5/255 on this model, so
the *core* step can only ever be subtle. The visible artifact is the boundary, not the colour.

**See also `FACE_MASK_AND_VARIANTS.md`** for the measurements behind all of the above, including the mask A/B, the
variant map, and the two defects found in this project's own code (the cylinder blend reading the wrong extra-UV
component, and the exporter dropping two face vertices).

---

## 9. The tools

**Viewer side (`Tools/`)**
- `headless_export.ps1` — drives Unity headlessly. Key switches: `-Char <id>`, `-Out <path>`, `-DumpFace` (runtime
  material + mesh facts), `-DumpMaterials`, `-ShotOnly -ShotView face -ShotYaws 0 -ShotAzimuths "0,90,-90"
  -Screenshot <path>` for the viewer's own reference renders.
- `blender_render_angles.py` — the Blender-side renderer. `--pmx`, `--out-dir`, `--yaws`, `--apply-shader`,
  `--legacy` / `--new-face`, `--no-cylinder-blend`, `--morph NAME=VALUE`, `--grid "0,90,-90"`,
  `--light-elevation <deg>`, `--light-from`, `--light-energy`. Prints the sun's direction per azimuth.
- `shading_ab.ps1` — runs both modes and tiles them into `ab.png`.
- `render_stats.py compare`, `montage.py`, `game_rip/` (the extraction and disassembly chain).

**Addon side:** the panel's single tickbox, plus scriptable operator properties (`legacy_shading`,
`game_cylinder_blend`, and the earlier ones still on the class).

**Scratch (`_uma_scratch/`):** `term_walk.py` (walks a chain of shading terms at two light directions),
`game_face_debug.py` (renders one named node as emission), `extract_area.py`, `pmx_normals2.py` (an unfinished PMX
parser — its vertex stride search is a useful start), and various probes.

---

## 10. Constraints

- **Never edit a `.cs` file while a Unity batch run is active.** No Unity process running is the safe state.
- `.cs` files in the viewer carry a **UTF-8 BOM**; preserve it (read and write with `utf-8-sig`).
- `Assets/Scenes/Version2.unity` and the TMP font asset stay uncommitted, as does `meta_plain.sqlite`.
- Raw extracted game assets and bytecode stay out of the public repos; findings are documented instead.
- A headless Unity run needs the Editor closed, and takes about three minutes.
- Long PowerShell-one-liner-plus-Python quoting breaks constantly: use script files.


---

## 11. Two observations that were in plain sight, now both answered

### The mean-luminance comparisons in this document are not sound

The viewer renders through Unity's tone mapping; the Blender tool uses the **Standard** view transform. Those are
different transfer functions, so comparing *mean face luminance* between the two renderers measures the renderers as
much as the shading. The tables in §7 do exactly that.

What survives is the **shape** comparison — whether a hard-edged boundary is present, and where it runs — because a
polygonal edge is a structural feature that tone mapping does not create or remove. That is what the artifact is, so
the conclusion drawn from those comparisons still holds. But the numbers themselves should be read as
"present versus absent", not as measurements of agreement, and any future comparison should use a structural metric
(difference images, edge detection, a boundary's position) rather than a mean.

**Answered, and the caution turns out to be unnecessary once the two are aligned.** Comparing means was never the real
problem; comparing them over *differently-framed crops* was. Put the two renders on the same landmark and the transfer
functions agree closely enough for the numbers to be read directly: the face's mean luminance is 222.1 against 224.2,
the lit skin level is 235 in both, and the mean skin colour is (249.8, 219.1, 194.9) against (251.0, 221.2, 198.7),
with a per-pixel difference whose median is 0.00. See §8 and `FACE_PIXEL_PROGRAM.md`.

### The viewer's face is consistently much darker, and that has never been explained

**It is explained: that was a framing artefact.** The ~50-level gap came from fixed fractional crops of two frames of
different shapes, which cover different parts of the model and mix skin, hair and background in different proportions.
Aligning the two on the facial-shadow landmark replaces it with a 2-level difference, and the lit skin level measures
the same 235 in both renderers. The instrument is `_uma_scratch/skin_boundary.py`; the numbers are in §8.
