# The likely mechanism, after three independent audits

Three subagents audited this work from different directions: one re-derived every numeric claim from the game's
own assets and the PMX, one researched how toon terminators are handled in Blender and in shipped games, and one
measured the model's own data and produced a list of theories with tests. Their reports are in
`_uma_scratch/audit/AUDIT_REPORT.md`, the viewer repo's `docs/TOON_TERMINATOR_RESEARCH.md`, and
`_uma_scratch/theories/THEORIES_jagged_face_terminator.md`.

This file records what they agree on, what they corrected, and the mechanism they point at.

---

## 1. What survived the audit

Every numeric claim that was checkable from source data was re-derived independently and passed:

| claim | verdict |
| --- | --- |
| the factor formula, and that factor 0 selects the lit texture | PASS, re-derived from the game's compiled program |
| `mask.r = 0` forces the shaded branch | PASS — the factor is exactly 1 for every half-Lambert |
| the mask texture is binary | PASS, one digit corrected: **54.62%** exactly zero, 42.61% exactly one, 0.72% between |
| the face's textures must be read through `UVMap`, not `UV1` | PASS exactly — `UV1`'s u is 0 for all 5682 face corners; mask red mean 0.648 under `UVMap`, 0.000 under `UV1` |
| the exported normal field is smooth | PASS exactly — face 4.585° mean / 11.955° p90 / 1.656% over 30°, mesh 19.822° / 24.794% |
| the light vector's magnitude and the mean dot product | PASS — `dot(N, L)` mean ±0.7192 over the face's corners |
| the vertex program redirects the normal, weight from the vertex colour's blue | PASS — `o4 = w·N + (1−w)·radial`, `w = (1−_CylinderBlend)·COLOR.z`, `_CylinderBlend` 0, blue 0/1 at 76%/18% |

And two things I had asserted were corrected:

* **The port reads the right channel.** An earlier claim in this project's own history — that it read the wrong
  extra-uv component — is refuted. `_UV3.uv.x` **is** the blue.
* **My "the light vector is the travel direction" claim was wrong**, and I had already flipped the code once on
  the strength of it. The third column of the sun's world rotation is the direction *towards* the light. What
  makes it confusing is that this model's face normals point at the camera, so the third column happens to give
  a positive dot product — which is why the render looked right with it. The code is now back to the third
  column, with the reason recorded beside it.

## 2. The measurement that changes the picture

**[measured, independently, by two of the three audits]** The face's own data says the light term is barely
involved in the line at all:

* **41.6% of the visible face's area is mask = 0**, which is *unconditionally* shaded — no light term can light
  it.
* On the vertices where the mask is 1, the **game's redirected field leaves 0% shaded** at sun azimuth 0. The
  field's 10th percentile of `dot(N, L)` is **+0.263**, and the step sits at −0.2, so the *entire* lower tail is
  above the threshold.
* The light term's own contribution, swept from azimuth 0 to 180, changes the shaded area from 4.3% to 97.9% —
  but the game's step **with** the mask sits a **constant ~0.41 above it at every azimuth**. The mask's effect
  does not move with the light.

Put together: **at this light the game's face has no light-driven terminator on the mask-1 region.** Its
two-tone appearance is the mask's painted contour — 41.6% of the area, hard-edged, and *smooth by construction*
because it is a contour in texture space rather than a contour of an interpolated field.

**[inference]** This is the mechanism, and it is the opposite of what the port implements. The port draws a
light-driven contour across the whole face; the game shades a painted region and leaves the rest lit. That
explains the three things that have been puzzling:

* why the artifact looks like it follows the mesh — a light-driven contour on 1328 vertices is a polyline with a
  corner at every edge it crosses, which is a mathematical certainty, not an aliasing artefact;
* why the game's line looks smooth — a texture contour has no relationship to the topology at all;
* why the redirection appeared to do nothing in my renders — **41.6% of the face ignores the light entirely, and
  the remaining 59% is saturated lit**, so almost the whole face is insensitive to the normal field. My test was
  measuring a face the field could not affect.

## 3. The theory that is refuted, and one that is confirmed

**Refuted: the port is shading flat, or has lost its custom normals.** Measured on the face: custom split
normals are present, `normals_domain` is `CORNER`, all polygons are smooth, the corner-normal spread *within* a
triangle is a median of 0.65°, the two sides of a shared edge agree in half-Lambert units to a median of
1.7e-4, and **0 of 2482 shared edges step across the threshold** (a per-face-normal control steps at 5.8% of
edges). The field is smooth and continuous.

**Confirmed: the contour is nonetheless a polyline.** The turn between consecutive segments of the contour has
a median of 2–6° and a p90 of 67–117° on the port's own recipe, giving a per-vertex wobble of about 4–5.6 px on
a 400 px face.

**[inference, flagged]** That second measurement is weak evidence on its own, and it is the same trap this work
has fallen into twice: a one-pixel-wide line is jagged when measured at pixel scale whatever it is. What makes
the finding credible is not the wobble figure but the arithmetic in §2 — the field is saturated lit where the
mask is 1, and the shaded area is the mask's zeros, so the painted contour is doing the work.

## 4. The defects the audit found, now fixed

* **The blend weight's layer search was a trap.** The code took the first of `("_UV3", "_UV2", "_UV1")` that
  existed. `_UV2` is the *second* extra uv, where the exporter writes the head-local height the facial-shadow
  region compares against `_faceShadowLength` — so a model with `_UV2` and no `_UV3` would have been shaded by a
  vertex height. Now `_UV3` only, in both places.
* **Two citations were wrong.** `face_pixel_8tex.asm.txt` and `face_vertex.asm.txt` are
  `DitherToonFace/TSER`'s programs, not `ToonFace/TSER`'s. The formulas are identical in both, so the
  conclusions stand, but the files named in this project's documents are not the ones the viewer runs. The
  audit re-extracted the real ones.
* **A number was off by a tolerance band.** "~56% of texels at zero" is 54.62% exactly zero; 56.14% is the
  ±0.02 band. The distinction matters only because the argument is about how binary the mask is.

## 6. The first sheet, as read by a human, and what it settles

**[measured - read from the rendered sheets]** `sheet1_mask_pinned.png` and `sheet3_light_sweep.png`, read by
the user:

* the step's mask pinned to 0.25 and 0.4 gives an **entirely shaded (blue) face**; 0.6, 0.8 and 1.0 give an
  **entirely lit (red) one**. Nothing in between.
* the two normal fields produce **one visible difference: the nose** - and the nose is a hole in the mesh, the
  usual anime-model technique of letting the outline pass behind it.
* the elevation-70 row is **identical across all four azimuths** — the face fully lit. The elevation-15 row shows
  the boundary, and it **traces the cheek cleanly** at azimuth 90 and 270, mirrored as expected; at azimuth 180
  there is a lit lip and under-lip region.

**Why the mask acts as a toggle rather than placing a line** — and this is the arithmetic that explains the
whole confusion. The line sits where `mask.r · halfLambert = _ToonStep − _ToonFeather`, so at
`halfLambert = 0.399 / mask.r`:

| mask | the line sits at half-Lambert | what a face spanning 0.5…1.0 does |
| --- | --- | --- |
| 0.25 | 1.596 | unreachable — the whole face is shaded |
| 0.40 | 0.998 | at the very top edge of the face's range |
| 0.60 | 0.665 | inside the range, so a line exists |
| 0.80 | 0.499 | below the face — the whole face is lit |
| 1.00 | 0.399 | below the face — the whole face is lit |

So **the mask does not place the terminator; it multiplies the light term's range**, and on a face whose
half-Lambert spans a narrow band it is a switch: below a mask of about 0.4 everything shades, above about 0.8
everything lights. A line only exists in the window between, and for this material that window is mask ≈ 0.4 to
0.8. That is a design consequence of using the mask as a *multiplier against a constant threshold* rather than as
a *position*.

**And the elevation result is the opposite of what §2 predicted, in a useful way.** At elevation 70 the face is
fully lit at every azimuth, so `dot(N, L)` is comfortably positive everywhere across the whole face and there is
no terminator to be jagged; the earlier "the light term barely matters" reading was an artefact of testing at an
angle where the light is nearly along the face's average normal. At elevation 15 the light is closer to the
face's own plane, `dot(N, L)` sweeps a wide range across it, and a boundary **appears and traces the cheek
cleanly**. Two consequences:

* **the cleanest result so far comes from a low sun** — the field has enough resolution for a good line when the
  light actually varies across the face, which is the configuration to use from here;
* **the mask is still visible as forced shading** — the lip and under-lip region at azimuth 180 is the mask's
  zeros, not a contour of the light.

**[inference]** So the port has two shading contributions where the game effectively has one-and-a-bit: a
light-driven contour that is good at low elevation, and a mask whose zeros force shading in fixed places. Both
are faithful to the game's program. What the game does *not* do is let the two fight — and on this material they
do not, because the mask's transition window (0.4…0.8) is narrow enough that most of the mask is either fully on
or fully off.

## 7. Where that leaves the `fwidth` question

**[measured, from the sheet]** At low elevation the port's own line traces the cheek cleanly, with the mesh's
normals and with the game's redirected ones alike. So the missing screen-space derivative is **not** what makes
the line look wrong at a good light angle - the hard step is already acceptable when the field varies enough
across the face.

That reframes the last open question rather than answering it. What is still unexplained is the original report:
a boundary that looked like it snapped to the mesh at a light angle the game never uses. Given the elevation
result, the first thing to check is whether that report was made at a high elevation, where the terminator is
compressed into a narrow band and every wobble is magnified.


## 8. What to do next

The sheet above answered the question it was built to answer, so what remains is shorter than it was:

1. **Use a low sun.** The boundary appears and traces the cheek cleanly at elevation 15 and is absent at
   elevation 70, where the face is simply lit. Every future sheet should run near the horizontal, and the
   earlier high-elevation comparisons should be treated as having tested very little.
2. **Check which texture the port blends toward.** One audit reads the pixel program's lerp target as the
   environment map rather than _ToonMap, which would mean the port has the wrong lit/shaded pair. That is a
   register-mapping question in a program with no reflection data, so it needs a render to settle rather than
   an argument - painting both branches with an obviously different flat colour would make it visible at once.
3. **Decide what the shading is meant to be**, because the game's own answer and "a clean terminator that
   moves with the light" are different designs. The game's line is the light term's contour where the mask is 1,
   plus forced shading wherever the mask is 0; at a low sun that is a clean line, which is what the sheet
   shows. If that is enough, the work is to keep the two contributions from fighting and the mask's transition
   window (0.4 to 0.8) in a range where it behaves as a bias. If a modern anime-game face is wanted - smooth
   at any light angle - that is a different technique and should be chosen deliberately.
4. **Not anti-aliasing, and not the step's constants.** width step antialiasing cannot be reproduced in
   Blender's node graph, and at a low sun the hard step is already acceptable, so the missing derivative is
   not the blocker it looked like.
