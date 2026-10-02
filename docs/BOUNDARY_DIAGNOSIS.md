# Why the face's boundary is jagged, and what the game does instead

Written after reading the game's pixel and vertex programs, the viewer's code, the addon's shading code, the
article the task pointed at, and after building an instrument that renders the face alone as a two-colour mask
so the boundary can be measured rather than squinted at. Every claim below is tagged with how it is known:
**[disassembly]**, **[code]**, **[measured]**, or **[inference]**.

---

## 1. The article does not say what the task assumed

The task pointed at the Cygames talk at
<https://game.watch.impress.co.jp/docs/kikaku/1366154.html> as the source for "the developers use a texture mask
to get a clean boundary line, and they mention the jaggedness". Read in full, it does not.

**[measured, against the page]** Searching the complete article text: 境界 (boundary) 0 occurrences, ギザギザ
(jagged) 0, カクカク (angular) 0, ジャギ (jaggy) 0, エッジ (edge) 0, ノーマル (normal) 0. 法線 (normal) appears
nine times, **all of them about the outline**, never about face shading.

What it does say, and it is exactly on point for the *fix*:

> 形状にあった陰影ではアニメ的な表現をするのは難しく、また、**ポリゴン数などが原因で細かな陰影を表現するには限界があるためシャドウマスクを使って制御している**。

"With shading that follows the shape it is hard to achieve anime-style expression, and **because of things like
polygon count there is a limit to expressing fine shading, so we control it with the Shadow Mask**."

and

> **テクスチャ作業の大半がシャドウマスクの調整**にかけている

"**Most of the texture work goes into adjusting the shadow mask.**"

with the channel assignment visible only in the slides: control map #1 is **Shadow (R), Specular (G), Cutoff
(B)**, control map #2 is Environment (G), Rim (B). There is also a head-only **detail mask** for the cheeks and
nose, and two separate normal sets — one for shading, one hand-edited for the outline — because
「アウトライン用の法線調整を行なうとシェーディングやリムが乱れる」 ("adjusting the normals for the outline
disturbs the shading and the rim").

**So: the article supports "the boundary is painted into a texture channel and the polygon count is why", and
it does not support "the developers redirect face normals to fix a jagged line".** The normal work it describes
is for the outline, in the opposite direction.

The one thing it establishes that changes how the problem should be framed: the mask's red channel is not a
detail — it is *the* mechanism, and it was authored by hand at some cost.

---

## 2. What the game's shader does, from the code

**[disassembly]** The lit/shaded factor in the face's pixel program (`face_pixel_8tex.asm.txt`, the same shape in
`face_pixel_use_mask_color.asm.txt`):

```
60: sample_b r1.xyzw, v1.xyxx, t1.xyzw, s1, cb0[5].x    // t1 = _TripleMaskMap = the export's *_base
80: dp3 r1.z, v4.xyzx, r4.xyzx                           // v4 = the shading normal · the light
82: mad r2.w, r1.z, 0.5, 0.5                             // halfLambert = 0.5·N·L + 0.5
83: add r3.w, cb1[3].w, -cb1[4].x                        // _ToonStep - _ToonFeather
84: mad r3.w, -r1.x, r2.w, r3.w                          // - mask.RED · halfLambert
85: div r3.w, r3.w, cb1[4].x                             // / _ToonFeather
86: add_sat r3.w, r3.w, 1.0                              // the factor: 0 lit, 1 shaded
116: mad r7.xyz, r3.wwww, r6.xyzx, r0.xyzx               // lerp(lit, shade, factor)
```

**[disassembly]** The normal the pixel program lights with is `v4`, which the vertex program wrote to `o4`
after redirecting it (`face_vertex.asm.txt`):

```
72-78: normalise N (with a guard)
80-82: or keep the raw N, chosen by a 0/1 toggle (_NormalizeNormal)
83-85: rotate N by the object matrix
86-89: radial = P - footOfPerpendicular(P onto the axis through cb2[26] along cb2[24].yzw)
93-94: R = normalise(radial)
95-97: w = (1 - _CylinderBlend) * vertexColour.BLUE
        out = w * N + (1 - w) * R
98: mov o4.xyz, out
```

**[code]** The game's own C# never touches this at runtime: `Gallop/Live/Director.cs` sets rim, toon and
saturation values, and nothing anywhere in `Gallop/` sets `_CylinderBlend`, so the material asset's value is
the one in force. **[code]** The viewer's loader sets `_faceForward`, `_faceUp`, `_faceCenterPos` and
`_faceShadowHeadMat` on the face material every frame and modifies no mesh data at all.

So the terminator has **two inputs**: a light term whose normal has been redirected towards a cylinder about
the head's axis, and a per-texel bias from the mask's red channel. **[inference]** Because the mask's red is
mostly 0 or 1 (measured below), the light term is what gives the boundary its *shape*, and the mask is what
*places* it.

---

## 3. What the port does

**[code]** `add_game_face_shading` builds the same formula in Blender nodes — halfLambert from the geometry's
normal and a sun vector, multiplied by the mask's red channel, compared against `_ToonStep - _ToonFeather`
over `_ToonFeather`, then a MixRGB of the diffuse and shaded textures. **[measured]** The arithmetic matches the
disassembly instruction for instruction, including the polarity of the blend.

**[code]** Before that, `apply_game_normal_blend` does the vertex program's redirection as a one-off write:
it averages the imported per-corner normals per vertex, computes `radial + weight * (N - radial)` with
`weight = clamp01((1 - _CylinderBlend) * vertexColour.b)`, and writes the result with
`normals_split_custom_set_from_vertices`.

**[measured]** On this model, over the face's 1328 vertices:

| | |
| --- | --- |
| the vertex colour's blue, which is the whole weight at `_CylinderBlend` 0 | min 0.0, median **0.0**, p90 1.0, max 1.0 |
| vertices at weight 0 (the authored normal is replaced by `radial`) | **1010 of 1328** |
| vertices at weight 1 (the authored normal is kept) | 237 |
| resulting angle between the authored normal and the written one | mean **13.4°**, median 2.0°, max **180°** |
| corners that disagree within a vertex, before the write | 0 of 1328 |

The blend's *formula* matches the disassembly exactly. The *data* it acts on is binary — the blue channel is 0
or 1 with nothing in between on this model — so the blend is a hard switch between two normal fields, and at
76% of the face's vertices the authored normal is thrown away entirely.

---

## 4. What the mask texture holds

**[measured]**, read from the image the material's Base Texture resolves to, `tex_chr1001_00_face_base.png`,
512×512, read Non-Color:

| channel | below 0.02 | between 0.05 and 0.95 | above 0.98 |
| --- | --- | --- | --- |
| red | 56.1% | **0.7%** | 42.6% |
| green | 0.0% | — | 0.9% (median 0.502) |
| blue | 5.7% | — | 94.3% |

The red channel is **essentially binary**, with a transition about two texels wide. On a face that occupies
roughly 600 px of an 800 px render, one texel of this mask is about four pixels of the render, so the mask's
own edges are chunky at render scale.

---

## 5. The instrument, and what it showed

The task asked for a pipeline that renders only the face as a two-colour mask. That exists now:
`_uma_scratch/render_face_mask.py`. It hides every material but the face, replaces the face's surface with the
step's own two-way choice painted as flat red and flat blue on a flat yellow background, and renders with a
script-controlled sun. `_uma_scratch/run_mask_controls.ps1` and `run_mask_wired_test.ps1` drive it, and
`build_mask_sheet.py` lays the results out side by side.

**[measured]** Five comparisons, at sun azimuth 0, 800 px, from those renders, counting pixels that differ by
more than 32 of 255:

| comparison | pixels differing |
| --- | --- |
| the shader with **and without** the cylinder normal blend | **12** (0.002%) |
| the mask pinned to 0 / 0.25 / 0.5 / 1 through its socket | **0** |
| the mask texture replaced by **solid white** | 9,567 (1.5%) |
| the mask texture replaced by **solid black** | 15,719 (2.5%) |

and the same question to the game's own shader, through the viewer, with its diffuse and shaded textures forced
to flat red and flat blue:

| the game, `_TripleMaskMap` forced to | what the face shows |
| --- | --- |
| as it is | almost entirely **red**, with a thin blue fringe on the nose bridge, the cheeks' outer edge and the jaw |
| **0** | a **clean vertical half-and-half**, red on the left, blue on the right, split down the nose — a straight, smooth line |
| 0.5 | the same smooth half-and-half at a different angle |

Three conclusions, each of which the pictures show plainly (`grids/face_mask/mask_sheet.png`):

1. **The blend is not what makes the port's boundary jagged.** Removing it changes 12 pixels of 810,000. Whatever
   the blend does to the normals, the boundary barely notices.
2. **The game's boundary is the light term, and it is smooth.** With the mask pinned, the game's terminator is a
   straight vertical line down the face — the contour of `N·L` across a normal field that varies smoothly.
3. **The port's boundary is not.** Its blue region covers the lower face, the cheeks and both sides — a large,
   irregular, hard-edged area, not a contour near a terminator. The game's blue, with the same mask and the same
   light, is a thin fringe.

The reading that fits all three: **in the port the mask is doing the work the light should be doing.** The mask
is binary and texel-quantised, so a boundary that the mask draws is hard-edged and chunky at texel scale —
which is exactly the jaggedness complained about. In the game the mask only nudges a boundary that the light
has already drawn smoothly.

**[inference]** That in turn points at the light term: if `N·L` in the port is near zero or wrong-signed across
most of the face, the mask's per-texel bias dominates the comparison everywhere, and the boundary becomes the
mask's pattern. The most likely cause is the redirected normal field: at 76% of the face's vertices the normal
is `radial`, which points *sideways out of the head's axis* rather than forwards, so a frontal light gives a
small or negative dot product over most of the face, and the field's hard 0-or-1 switch between two normal
sources adds its own discontinuities.

---

## 6. Theories, and their status

| # | theory | status |
| --- | --- | --- |
| 1 | The article describes a face-normal fix for a jagged boundary. | **Refuted by reading the article.** No mention of jaggedness or face normals; its normal work is for the outline. |
| 2 | The export loses the face's normals. | **Refuted.** PMX is per-vertex and Unity's mesh is per-vertex, the exporter copies both 1:1, and mmd_tools assigns per-corner custom normals from exactly those values. The failing mode is normals being *replaced*, not lost. |
| 3 | The boundary is jagged because the shipped node group's step still runs on top of the port's. | **Still open, and weakened.** The group's own `Less Than` is fed a zeroed threshold, which degenerates its condition, and the port hands it one already-decided colour in both texture sockets. Not yet measured directly. |
| 4 | The boundary is jagged because the port's **normal field is broken** by the cylinder blend's hard 0/1 weight. | **Open, and the leading candidate.** The blend changes 87.7% of the face's vertices and flips 8 of them 180°, yet removing it changes the render by 12 pixels — which means the *current* boundary is not normal-driven, and the mask is covering for a light term that is not producing a clean contour. |
| 5 | The boundary is jagged because the **mask is binary** and its texel edges are visible at render scale. | **Confirmed as a contributing mechanism**, but it is only visible because the light term is not drawing a smooth contour underneath it. The game has the same binary mask and a smooth boundary. |
| 6 | The boundary is jagged because of missing anti-aliasing in the addon's graph (no `smoothstep`, no `fwidth`). | **Open.** The game's programs contain no derivative instructions either, and the viewer runs 4× MSAA, so anti-aliasing is not the whole story — but the port's graph has a `_ToonFeather` of 0.001, which is a step, not a ramp. |
| 7 | The port reads the vertex colour's blue from the wrong layer, so the blend's weight is wrong. | **Refuted.** `_UV3.uv.x` is the blue; verified earlier by a multiset comparison, and the export's own uv layout confirms it. |
| 8 | The face's authored normals are being lost on import. | **Refuted.** Measured: the imported face normals are unit length and the per-corner normals agree within 0.000087, so there is no per-corner field being flattened on this mesh. |

---

## 7. What to do about it

The order matters, because each step's result decides whether the next is needed.

**Step 1 — establish what the light term alone draws, in the port.** The mask can be taken out of the
comparison by pinning it inside the addon's own step (not through the group's socket, which is a different
mask — see the note below). With the mask pinned, the port's boundary is the `N·L` contour, and it can be
compared directly against the game's smooth half-and-half. This is the measurement that says whether the
normal field is the problem.

**Step 2 — if the light term is the problem, rebuild the normal handling from the vertex program.** The
disassembly is unambiguous about the formula, and the port matches it; what has to change is what it is applied
to. Two candidates, both testable: keep the authored normals and do the redirection per-pixel where the game
does it per-vertex (which is what the game effectively does, since its weight is per-vertex and its
interpolation is the hardware's), or drop the redirection for the face entirely on the grounds that its weight
is 0 or 1 on this model and therefore cannot reproduce the game's smooth field.

**Step 3 — rebuild the step so the boundary is a ramp, not a step.** `_ToonFeather` is 0.001 on this model, so
the port's own graph produces a hard edge wherever it decides. The game's is hard too — the same 0.001 — but
the game's boundary is smooth because the *field* it crosses is smooth. A ramp would hide a broken field rather
than fix it, so this is third, not first.

**Step 4 — only then revisit the mask.** If the light term produces a clean contour and the mask adds texel
noise on top, the mask is doing its job and the remaining work is about how a 512² mask is sampled onto a
face that is several hundred pixels across: magnification filtering, or a higher-resolution mask, or accepting
the game's own texel scale.

**A note on the controls, for whoever picks this up.** Pinning the mask through the shipped group's
`Base Texture` socket changes nothing at all (measured: 0 pixels). The addon's step reads its mask from its own
`Face Mask Channels` node, which is fed by the image the addon assigned, not by that socket — so a control that
pins the socket is testing the group's mask, not the step's. The controls that work are the ones that change the
image itself (white/black masks: 1.5% and 2.5%) and the ones that pin the step's own input inside the graph.

---

## 8. The light term, confirmed — and a tool bug that had been hiding it

The task left the light term open: "I can't say for sure — let's confirm it." It is confirmed, and finding
out required fixing a bug in the viewer's own shot tool.

**[code, fixed]** `UmaHeadlessExport` applied the light only when several azimuths were requested:

```csharp
foreach (double azimuth in azimuths)
{
    if (azimuths.Length > 1)
        ApplyLightAzimuth(azimuth, options.ShotLightElevation);
```

so a run asking for one azimuth — the natural way to write a light sweep, one shot per run — silently rendered
the scene's own light instead. The tell was a sweep through six azimuths that came back with a **0.98 overlap
between two opposite ones**, which no moving light can produce. Fixed to apply the light for every shot.

**[measured]** With that fixed, rotating the sun through eight azimuths, the mask left as exported, the pose
pinned, the face's two textures forced to flat red (lit) and flat blue (shaded):

| light azimuth | lit fraction | lit centroid | largest lit region |
| --- | --- | --- | --- |
| 0 | 13.2% | (637, 251) | 84% |
| 45 | 13.4% | (638, 254) | 83% |
| 90 | 12.8% | (622, 263) | 78% |
| 135 | 6.1% | (606, 321) | 26% |
| 180 | 5.2% | (605, 342) | 18% |
| 225 | 10.1% | (663, 275) | 64% |
| 270 | 13.7% | (636, 254) | 80% |
| 315 | 13.4% | (636, 251) | 83% |

**The light moves the boundary**: the lit fraction swings from 5.2% to 13.7% and the lit region's centroid
travels 90 px, with adjacent azimuths overlapping 0.86-1.00 and opposite ones 0.40.

**And with the mask forced to 0 — taking the texture out of the shader's comparison entirely, so the step is
a function of the light term alone — the game's terminator is a clean straight line down the middle of the
face** (`grids/game_clean/game_lightterm_only.png`, and `mask_0.png` from the earlier sweep). A straight line
like that can only be the contour of a smooth field. That is the property the port's boundary lacks, and it is
the thing to build for.

## 9. The normal field is already smooth, and the rollback removed the only thing that touched it

**[measured]** On the export, over the face's 1328 vertices, the angle between each vertex's normal and its
neighbours':

| | mean | median | p90 | p99 | max | over 30° |
| --- | --- | --- | --- | --- | --- | --- |
| the face, as imported | **4.58°** | 2.29° | 11.96° | 33.16° | 88.86° | **1.66%** |
| the whole mesh, as imported | 19.82° | 12.51° | 49.50° | 91.89° | 173.10° | 24.79% |

The face's normals are five times smoother than the mesh's as a whole. **The export carries a smooth normal
field, and it is smooth enough to draw the game's straight terminator.** Theory 2 ("the export loses the
normals") is dead a second time, and theory 8 with it.

**[measured]** After the rollback, applying the shader leaves that field **bit-identical** — mean 4.58°, p90
11.96°, 1.66% over 30°, unchanged to two decimals. The rollback removed `apply_game_normal_blend`, which was
the only code that ever rewrote the face's normals, so from here the field is the export's.

**[inference]** This closes theory 4 as a *future* risk and reframes it as a *past* cause: with the normal
field smooth and the mask binary, a boundary that came out jagged cannot have come from either. Whatever
produced it was in the 464 lines of shader code that the rollback removed, which is consistent with the
symptom the task started from — "the current shader does not respond properly".

## 10. What to build, now that the field is known good

The rebuild has a clear target and one open design question.

**[established]** The boundary must be the contour of the light term, on the exported normals, with the mask as
a per-texel bias and not as the thing that draws the line. The reference is
`grids/game_clean/game_lightterm_only.png`: a straight, smooth line.

**[open]** How the mask should bias it, and how the two are combined, is still not pinned down. The mask is
binary (56% at 0, 43% at 1), so `mask.R * halfLambert` is either 0 or `halfLambert` for almost every texel,
and the boundary is wherever the two regimes meet the step — which is a contour of the *light* on one side of
the mask's edge and nothing at all on the other. The measurements do not yet distinguish:

* the mask's role being to *select* which parts of the face are allowed to shade at all (a per-texel on/off
  switch, which would make the game's smooth line the light's contour inside the "on" region), from
* the mask's role being to *offset* the threshold per texel (which the arithmetic literally says, and which
  on a binary mask degenerates to the first case).

Either way, for this character the visible boundary is the light's contour where the mask is 1, and the mask's
own hard edges never appear as boundary because a multiplication by 0 cannot move a step that is already
saturated. That reading fits every measurement taken: lit fraction falling monotonically as the pinned mask
rises (68% as exported, 43% at 0, 14% at 0.5 — though the middle of that range measured flat, which is one
remaining loose end), the boundary moving with the light, and no texel-scale jaggedness in the game's own
render.

**The first thing to build**, then, is the step exactly as the disassembly gives it — `saturate((_ToonStep -
mask.R * halfLambert) / _ToonFeather)` — fed by the exported normals, the scene's sun, and the mask the
exporter names, with nothing else in the path. Everything the rollback removed was additional machinery on
top of that formula; the formula itself is nine instructions and is what the game runs.

## 11. The tools this produced

In `_uma_scratch`, all runnable:

| tool | what it does |
| --- | --- |
| `render_face_mask.py` | renders the face alone as a two-colour mask: other materials blanked, the step's two choices painted flat, a third colour behind. `--flat-mask` pins the step's mask, `--ambient` pins the light term, `--subdivide N` tessellates without moving vertices, `--raw-mask` paints the mask texture itself. `--no-blend` refers to the pre-rollback code and no longer does anything. |
| `run_game_mask.ps1`, `run_game_light_sweeps.ps1`, `run_game_clean_compare.ps1` | ask the game's own shader, through the viewer, for the same picture: its diffuse and shaded textures forced to flat red and flat blue, its mask forceable to any value. These are the reference renders. |
| `run_game_light_sweeps.ps1` | the eight-azimuth light sweep and the seven-value mask sweep |
| `analyse_game_light_sweeps.py` | the lit fraction, centroid, component count and overlap for each render of a sweep |
| `measure_game_light_term.py` | the same for the first three game renders, with the shader's own arithmetic printed beside the measurement |
| `probe_mask_texture.py` | what the mask's channels hold and how sharp its transitions are (uses numpy from Blender's Python, which is the only numpy available here) |
| `probe_normal_field.py` | whether the face's normal field is smooth: the angle between each vertex's normal and its neighbours', before and after the addon touches it |
| `probe_normal_blend.py`, `probe_blend_weights.py` | how far the pre-rollback blend moved the normals, and the distribution of its weight. Both describe code the rollback removed; keep them for the record. |
| `build_mask_sheet.py` | the side-by-side sheet of port masks and game masks |
| `boundary_jaggedness.py`, `boundary_roughness.py`, `compare_boundary_smoothness.py` | three attempts at a numeric jaggedness. **None is fit to report yet**: the first is a radial profile, which is blind to tangential jaggedness; the second's straight-edge test fails on rasterisation rather than on jaggedness; the third needs a reference boundary at a known-scale match, which is what the game's flat-colour renders now provide. The metric is owed. |

