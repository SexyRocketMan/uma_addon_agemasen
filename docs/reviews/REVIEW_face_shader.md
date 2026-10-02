# Review: the game's face shader in the addon

Reviewed against the vendor disassembly in `Tools/game_rip/shader_sources/`. D1 was confirmed by experiment;
everything else by instruction-by-instruction comparison. Line numbers are the addon's
`uma_addon/addons/uma_addon/operators/Umashader.py`.

## D1 (critical, empirically confirmed) — the Mix's colour inputs are inverted

**Disassembly.** `face_pixel_use_mask_color.asm.txt` lines 160-161:

```
add r2.xyz, -r0.xyzx, r1.xyzx
mad r7.xyz, r3.wwww, r2.xyzx, r0.xyzx      ; r7 = lerp(r0, r1, step)
```

`r0` is the `t0` (`*_face*diff`) path and `r1` the toon path. So `step = 1` yields **the toon/shaded colour**, and
`step = 1` happens when `halfLambert` is small, i.e. when the surface faces *away*. The 8-texture variant is the
same form at its line 116 (`add r6, -r0, r5` then `mad r7, r3.w, r6, r0`).

**Code.** Lines 543-548:

```python
mix = new("ShaderNodeMixRGB", "Blend", ...)
links.new(step_final, mix.inputs["Fac"])
links.new(toon, mix.inputs["Color1"])
links.new(diffuse, mix.inputs["Color2"])
```

`MixRGB` computes `Color1*(1-Fac) + Color2*Fac`, so this is `lerp(toon, diffuse, step)` — **the exact reverse**.
`step = 1` shows the *diffuse* where the game shows the *shaded* texture.

**Measured.** In a scratch session I swapped the two links (no repo file touched) and re-rendered the same model,
morph and light: the face's mean luminance went from **178.7 to 195.9**, and the hard-edged patch on the cheek
disappeared. With `_ToonFeather = 0.001` the step is binary, so the inversion paints the *shaded* texture across
the *lit* area, and the boundary where it flips is a hard polygonal edge — the artefact in `mouth_zoom.png`.

**Fix.** Swap the two:

```python
links.new(diffuse, mix.inputs["Color1"])
links.new(toon, mix.inputs["Color2"])
```

(or keep the links and feed `Fac = 1 - step`).

## D2 (critical) — the regions modulate the *step*; the game modulates *colour*

**Disassembly.** The region path operates on colour registers and never touches `r3.w` (the step):

```
278: mad r2.xyz, r1.wwww, r2.xyzx, r0.xyzx   ; r2 = lerp(r0, r1, sideBlend)
285: mad r2.xyz, r3.xxxx, r2.xyzx, r4.xyzx   ; cheek mask picks between r4 and r2
300: mad r0.xyz, r1.xxxx, r0.xyzx, r2.xyzx   ; nose mask picks between r2 and r0
```

**Code.** Lines 539-541:

```python
step_final = math('SUBTRACT', sharp, step, name="Sharp Versus Base", row=22)
step_final = math('MULTIPLY', step_final, region, name="Region Weighted", row=23)
step_final = math('ADD', step_final, step, clamp=True, name="Step With Regions", row=24)
```

This blends between two *thresholds*, which the bytecode never does. Because `sharp` sits at a different place
from `step`, wherever the region turns on a *second* hard edge appears — a plausible source of the extra edges in
the ±90° columns. Measured, bypassing the region changes the lit-case render by only 6 hard-step pixels, so its
current contribution is small; the mechanism is still wrong and must be replaced, not tuned.

**Fix.** Keep the base `lerp` for the whole face, then add the two colour switches as `MixRGB` nodes after it,
per lines 278/285/300, with the masks from D3.

## D3 (major) — the cheek and nose factors use the wrong operands and lose the strength gating

**Disassembly.**

```
272-274: max(g - 0.51, 0) * 2            cheek factor, then × sharp step (274) and × r4.w (275)
279:     ge r3.z, r3.y, 0.51             the cheek gate
280-283: ge factor, (1 - cb1[23].x)      the strength gate, then and, then mul
289-290: 1 - 2*min(g, 0.49)              nose factor, then × (2*(1-|dot(F,L)|)) (286-291)
294:     ge r1.x, 0.49, r3.y             the nose gate
295-298: ge factor, (1 - cb1[23].y); × cb1[23].z
```

**Code.** Lines 496-502 build `cheek_factor` (correct, doubled) and `nose_factor` (correct) but lines 536-538 use
`cheek_test` and `nose_test` instead — the undoubled and differently-scaled intermediates — and hard-code `× 4.0`:

```python
region = math('MAXIMUM', cheek_test, nose_test, name="Region", row=19)
region = math('MULTIPLY', region, blend, name="Region Blend", row=20)
region = math('MULTIPLY', region, 4.0, clamp=True, name="Region Amount", row=21)
```

There is no `× 4.0` in the bytecode, no `>= 1 - strength` comparison, and no `× strength.z`.

**Fix.** Use `cheek_factor`/`nose_factor`; add two `Math` `GREATER_THAN` nodes against `1 - strength.x`/`1 - strength.y`;
multiply the nose mask by a `strength.z` constant; drop the `× 4.0`.

## D4 (major) — the half-plane uses the shading normal where the game uses `_FaceForward`

**Disassembly** (lines 247-257): both cross products take `cb1[25]` = **`_FaceForward`** as the first operand:

```
249: mad r5.xyz, cb1[25].yzxy, r5.yzxy, -r7.xyzx     ; cross(F, P - C)
256: mad r5.xyz, cb1[25].yzxy, r6.zxyz, -r5.xyzx     ; cross(F, r6)
```

**Answer to the question asked:** the second cross product is taken with the **light** (`r6`, the light direction),
*not* the view (`r5` is the view, computed at line 93 and used only for the environment lookup). The code has this
right — line 527 uses `light`.

**Code.** Lines 526-527 substitute `geometry.outputs["Normal"]` for `F` in both:

```python
cross_face = vector('CROSS_PRODUCT', geometry.outputs["Normal"], to_centre, ...)
cross_light = vector('CROSS_PRODUCT', geometry.outputs["Normal"], light..., ...)
```

**Also missing:** `dot(cb1[24].yzw, r6)` (line 263), the `_FaceUp` term, and the whole forward/up factor `r4.w`
(lines 258-268) that multiplies the cheek factor at line 275.

**Fix.** See D6 — supply the real frame, then use it in both cross products and add the up/forward factor.

## D5 (major) — three whole terms are absent

1. **The forward/up factor `r4.w`** (lines 258-268): `f(dot(F, -L), dot(U, L))`, multiplied into the cheek factor
   at line 275. Nothing in the graph corresponds to it.
2. **The vertex-colour toon power** (lines 138-143): `(1 - v3.w) * cb1[27].w` against `0.5 + min(N·L*0.5, 0)`,
   driving both toon-colour paths (`_VertexColorToonPower`). Absent; and `vertex_colour_blue` (line 212) reads
   only the blue channel (`.x` of `_UV3`), so the alpha is not even available. On this model both toon colours are
   transparent so the term is inert, but it is part of the mechanism.
3. **The `*_area*` region colours** (lines 62-86 and 117-137): two per-channel factors against 0.49/0.51, each
   driving six colour multipliers on two textures. Absent — and provably inert for character 1001, which has no
   `_area` texture at all, and whose usable one is flat grey with a single corner block.

## D6 (major) — the head frame is a static substitution, not the live uniform

**Disassembly:** `_FaceForward`, `_FaceUp`, `_FaceCenterPos` are `cb1[24]`, `cb1[25]`, `cb1[26]`, per-character
uniforms the game sets from the head bone each frame.

**Code:** line 507 sets `up = (0,0,1)`; lines 508-510 bake `C` once at Shading time from `head_frame` (line 869);
`F` is the shading normal. So the frame does not follow a head turn at all.

**Fix.** A small Geometry Nodes modifier writing three attributes from the armature's Head bone
(`uma_face_up`, `uma_face_forward`, `uma_face_centre`), read in the material with `Attribute` nodes. That fixes
D4 and D5.1 at the same time.

## D7 (minor) — the blended normal is renormalized; the game's is not

**Disassembly** (vertex, lines 111-116): `radial` is normalized and `n` is unit, but the *result*
`weight*(n - radial) + radial` is **not** renormalized, so `N·L` in the pixel stage is damped by the blend angle.

**Code:** line 368 ends with `.normalized()`.

**Fix.** Drop the `.normalized()`.

## D8 (minor) — `_NormalizeNormal` ignored

Vertex lines 100-106: `if (cb2[16].z <= 0.5) n = raw normal else n = normalize(normal)`. The extracted material's
`_NormalizeNormal` is **0**, so the game uses the **raw** normal for this model.

**Code:** `shading_normals()` always normalizes (line 249), and `apply_game_normal_blend` uses that.

**Fix.** Honour the flag; it is not exported yet, so the default matters — for this model the faithful setting is
"raw".

## D9 (minor, but blocks fidelity) — the light source ignores the flag, and four properties are missing from the export

**Disassembly** lines 93-106: `r6 = normalize(cb0[6])` then `r6 = lerp(r6, normalize(cb1[27]), flag)` with
`flag = (cb1[26].w >= 1)` — so the **scene light** (`cb0[6]`, per-frame) with an optional override by
`_OriginalDirectionalLightDir`.

**Code** lines 443-464: always the sun lamp. The direction *is* computed correctly (the sun's `+Z` column, which is
the direction toward the light, matching Unity's `_WorldSpaceLightPos0`), and it is live. The flag is ignored.

**Also:** `ModelExporter.cs` carries `toon_step` and `toon_feather` — the two that matter most, already correct —
but **not** `_CylinderBlend` (line 856 defaults 0.0; the viewer sets 0.25 for hair and eyes), `_NormalizeNormal`,
the `_MaskColor{x,y,z}` strengths, or `_OriginalDirectionalLightDir`. Four `Number(...)`/`Vector(...)` lines.

## D10 (informational) — `discard_nz`

Line 60 of both pixel variants discards against a projected-coordinate texture lookup. It is the cutout/dither
pass, not the face shading.

---

# Answer 1 — is anything fundamental?

**From the opcode inventory of all three programs:** `discard` (1), `exp` (1), `log` (1), `rsq` (3), `movc` (8),
`sqrt` (1, vertex). **There is no `deriv_rtx` or `deriv_rty` anywhere in the face shader** — no screen-space
derivatives, no `fwidth`. (The `fwidth` in the earlier work came from the *viewer's* Nars re-implementation, not
from the game.)

`exp`/`log` → `Math` `POWER`; `rsq`/`sqrt` → `Vector Math` `LENGTH`/`NORMALIZE`; `movc` → `Mix` with a 0/1 or a
`Math` multiply. All expressible.

What is **not** directly expressible, with the instruction that needs it:

1. **Screen-space values** — `discard_nz` at line 60 reads `v8.xy/v8.w`, a projected coordinate, and dithering is
   inherently screen-space. Blender's shader nodes expose **no screen coordinate and no camera matrix**. Needs an
   `Attribute` written by a Geometry Nodes modifier (which can read the camera object), or nothing.
2. **Per-frame uniforms from a bone** — `cb1[24..26]` (the face frame) and `cb2[16].z`/`cb2[23].w` per character.
   Shader nodes cannot read an armature's pose; **drivers** or a GN-written attribute are required. The addon
   already uses drivers for the light, so the mechanism exists.
3. **Multi-compile keyword variants** — the bundle holds eleven pixel variants selected by keywords
   (`USE_MASK_COLOR`, `SELFSHADOW_ON/OFF`, …). Blender materials have no keyword system; needs a numeric switch or
   separate materials. This is why the 9-texture and 8-texture programs differ at all.
4. **Unity's ambient/SH and grading** — `_LightProbeColor`, `cb0[139]`, `cb0[151..153]`. Approximable via the world
   colour and `Shader to RGB`.
5. **Stencil** — `_StencilMask`, `_StencilComp`, `_StencilOp` are material state; EEVEE has no stencil buffer. Used
   for the outline and eye masking, not the face shading.

**Conclusion: nothing fundamental.** The two real gaps are screen-space effects and bone-driven uniforms, and the
latter already has a working pattern in this addon.

# Answer 2 — the minimal set of changes to match the mechanism

In value order:

1. **D1**: swap two links at lines 547-548. This is the measured cause of the artefact.
2. **D2 + D3**: replace lines 535-541 with the game's colour switching — two `MixRGB` nodes attached *after* the
   base blend, fed by the correct `cheek_factor`/`nose_factor`, gated by two `Math` `GREATER_THAN` nodes against
   `1 - strength.x`/`y` and a `Math` multiply by `strength.z`. Delete the `× 4.0` and the `step_final` chain.
3. **D6 (and with it D4, D5.1)**: one Geometry Nodes group writing `uma_face_up`/`uma_face_forward`/
   `uma_face_centre` from the Head bone, plus three `Attribute` nodes in the material; use `F` in both cross
   products and add the up/forward factor from lines 258-268.
4. **D7, D8**: drop `.normalized()` at line 368; honour `_NormalizeNormal`.
5. **D5.2**: read the vertex colour's alpha alongside the blue and add the `(1 - v3.w) * cb1[27].w` weight to the
   toon colours. Inert on this model.
6. **D9**: export `_CylinderBlend`, `_NormalizeNormal`, the `_MaskColor{x,y,z}` strengths and
   `_OriginalDirectionalLightDir`; use the flag on the light.

# Scope and what I did not verify

Verified against the disassembly: D1 (and confirmed by experiment), D3, D4, D5 (presence and absence), D6, D7,
D8, D9, and the opcode inventory behind Answer 1. The Mix inversion was confirmed twice independently — once by
reading both variants, once by swapping the links and measuring a **+17/255** shift.

**Not** fully traced: the 9-texture variant's lines 87-260 (~170 instructions covering the lighting, the two
colour paths, the environment and the grading). The colour endpoints the region switches between (`r0`, `r1`, `r2`,
`r4`) are identified only by partial register lineage, so D2's rebuild is correctly *shaped* but its endpoints
should be re-derived from that section before it is called faithful rather than plausible.
