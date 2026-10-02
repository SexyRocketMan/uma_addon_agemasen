<!-- superseded -->
> **Note.** Read with `GROUND_TRUTH.md`. The variant map and the mask's channel assignments here still stand. The claim that the port's 22.86% normal difference came from the export was retracted inside this file already; the retraction is the correct reading.

# The face mask, the shader variants, and what the round trip does to the normals

Written after a session that set out to do three things: fix the `-DumpFace` timeout, fingerprint the
Blender-side normal field, and read the `_USEOPTIONMASKMAP_ON` variant of the game's face program.
Two of the three produced results; the third turned out to rest on a false premise, which is itself the
useful finding.

Everything below is measured. Where something is inferred rather than measured it says so.

---

## 1. The premise that was wrong: `_USEOPTIONMASKMAP_ON` is not a variant

The previous session's last fact was that the face material enables the shader keyword
`_USEOPTIONMASKMAP_ON`, and the plan was to read that variant out of the game and compare it with the
port. There is no such variant. `Gallop/3D/Chara/ToonFace/TSER` - the shader the viewer's loaded face
material carries - declares **eight** keywords, and that is not one of them:

```
STEREO_INSTANCING_ON, UNITY_SINGLE_PASS_STEREO, STEREO_MULTIVIEW_ON, STEREO_CUBEMAP_RENDER_ON,
_MAIN_LIGHT_SHADOWS, _ADDITIONAL_LIGHTS, USE_MASK_COLOR, _ADDITIONAL_LIGHT_SHADOWS
```

`Gallop/3D/Chara/DitherToonFace/TSER` declares exactly the same eight. A keyword a material carries but
its shader does not declare selects nothing, so `_USEOPTIONMASKMAP_ON` identifies no branch set. What
the option mask actually is, is a **uniform** branch: `_UseOptionMaskMap = 1` at runtime and
`_OptionMaskMap` bound to the character's `tex_chr1001_00_face_ctrl`, which the port does not implement.
The port is missing the option-mask branch either way - the premise about *how* it is selected was wrong,
not the gap.

## 2. Which of the twelve compiled pixel variants the game runs

The blob region holds the variant information that the typetree does not: for baked shaders
`m_SubPrograms` is empty, and each container is preceded by a record naming the keywords that select it.
Read that way, `ToonFace/TSER`'s 28 containers are 4 vertex programs, **12 full pixel variants** and 12
small programs for the other passes:

| offsets | textures | keywords |
| --- | --- | --- |
| 12614, 22846, 33078, 43334, 65618, 87902 | 7 | none / `_ADDITIONAL_LIGHTS` / `_MAIN_LIGHT_SHADOWS` / both / + `_ADDITIONAL_LIGHT_SHADOWS` |
| 53642, 75902, 98130, 110094, 122058, 134046 | 8 | the six `USE_MASK_COLOR` combinations |

Two things follow.

**`USE_MASK_COLOR` is what adds the eighth texture**, and the live face material does **not** enable it.
Its keyword set is `_USEOPTIONMASKMAP_ON` alone - no `USE_MASK_COLOR`, no `_MAIN_LIGHT_SHADOWS`, no
`_ADDITIONAL_LIGHTS`. So the game runs the plain 7-texture variant, and the port's docstring cites the
wrong one: it was written from `face_pixel_use_mask_color.asm.txt`, the `USE_MASK_COLOR` program. That
program's `_MaskColorTex` path cannot run on this material at all.

**The 7-texture variant's step matches what the port implements.** In the plain program the step is

```
r4.w = (cb1[3].w - cb1[4].x) - t1.r * (0.5 * dot(v4, L) + 0.5)      ; _ToonStep - _ToonFeather - mask*halfLambert
r4.w = saturate(1 + r4.w / cb1[4].x)
```

which is the port's formula, with the same constants. So the port is a faithful reading *of that part*.

## 3. Which texture is the mask: `_TripleMaskMap`, not `_ToonMap`

The shipped containers have **no `RDEF` chunk**, so the `t0..tN` in a disassembly have no names in the
bytecode at all - the previous session's fact table recorded "the operand is `t1.r`" and then labelled
`t1` as `_ToonMap`. That label came from the shader's property list, not from anything that ties a name
to a register. The tie comes from the shader's own declaration order, and the variant register counts
check it: a variant that omits a texture declares one fewer, trailing, register.

```
t0 = _MainTex      t1 = _TripleMaskMap   t2 = _OptionMaskMap   t3 = _ToonMap
t4 = _EnvMap       t5 = _DirtTex         t6 = _EmissiveTex     t7 = _MaskColorTex
```

The plain variant declares `t0-t6` - all but the last, which only `USE_MASK_COLOR` uses - and the
`USE_MASK_COLOR` variant declares `t0-t7`. Both counts agree with the assignment, and independently
`_UseOptionMaskMap` is the scalar that scales `t2`, which is `_OptionMaskMap` by that same order.

So the mask the step multiplies `halfLambert` by is **`_TripleMaskMap.r`** - the export's `*_base` -
and the green channel the cheek and nose gates compare against 0.51 and 0.49 is that same texture.
The port read both from `_ToonMap` (`*_shad_c`), and the two are not interchangeable. Sampled at the
face's own 1284 vertices:

| texture | red mean | red > 0.51 | red < 0.49 | red max |
| --- | --- | --- | --- | --- |
| `_TripleMaskMap` (`*_base`) | 0.611 | 61% | 39% | 1.00 |
| `_ToonMap` (`*_shad_c`) | 0.798 | 85% | 15% | 0.99 |

They differ by 0.297 on average, and 53% of the face's vertices differ by more than 0.05. The base map's
red is a *painted* mask - near-binary across the face - while the shaded colour texture's red is mostly
high. A near-constant mask makes the step a contour of `N·L`, which follows the triangles; a painted one
makes it follow the paint. That is exactly the shape difference the artifact is about, so this looked
like the answer.

## 4. It is not the answer: the mask swap moves 1-2% of pixels

The addon now takes the mask from the group's `Base Texture` input - which already carries
`tex_chr1001_00_face_base` - behind a scriptable `game_face_mask` operator property (`triple` is the
default, `toon` reproduces the earlier reading). `Tools/blender_render_angles.py --face-mask` selects it.

Rendered both ways against the same light and framing (face view, elevation 70, azimuths +90 and -90):

| mask source | face mean, az+90 | face mean, az-90 |
| --- | --- | --- |
| `_ToonMap.r` (earlier) | 199 | 199 |
| `_TripleMaskMap.r` (the game's) | 200 | 199 |
| viewer (reference) | 147 | 147 |

The two render sets are **not** identical - a pixel-by-pixel difference over all three azimuths gives a
mean of 0.17 to 0.50 per channel, a maximum of 32-42, and 0.95-2.14% of pixels differing by more than 2,
confined to the face's bounding box. So the swap takes effect; it simply does not move the result. The
reason is the feather: with `_ToonFeather = 0.001` the step is binary, so changing the mask *moves* the
boundary and changes which texels fall each side, but does not change how many do, and does not soften
it. The face mean stays at 199.

**Conclusion: the mask source was wrong and is now right, and it is eliminated as the cause of the
artifact.** The boundary is hard in both readings. Neither reading reaches the viewer's 147.

## 5. Retracted: the normal field is *not* perturbed, and the instrument that said so was the problem

This section first reported that the face's normal field is already 22.86% different from Unity's
*inside the pmx*, from a measurement that binned the angle from each normal to the normalised mean
normal, 5 degrees per bin, on both sides. **That number is wrong.** The exporter was instrumented to
record every vertex as it writes it, and compared line against line the pmx is exact: position worst
component difference **0.000002**, normal **0.000000** (100% of the 1328 face vertices within 0.5
degrees, max 0.000002), uv **0.000000**, vertex colour **0.000000**. The normal field in the pmx *is*
the game's field, rotated 180 degrees about y along with the rest of the model by
`MMDReaderWriteUtil.WriteAmpVector3`, which negates x and z on every vector it writes. See
`GAME_DATA_VS_VIEWER.md` section 8 for the record format and the convention.

Why the histogram could not see that: its bins are narrow, its distribution is spiky (one bin holds 322
of the 1330), and the reference direction is the *mean of the same data*, so the two fields differing in
mean direction by 1.43 degrees is enough on its own to move hundreds of vertices across bin edges. It
was comparing two summaries, not two fields, and it invented a difference.

What survives from this section, and was measured independently: normal **length** is exactly 1.000000
everywhere (Unity, the pmx, the import, and after the addon's shading), scores of face vertices do move
during the addon's own custom-normal write, and **`BakeMesh` perturbs the normals it is given** -
against the shared mesh's, index aligned over 1330 vertices, median 1.80 degrees with only 3.3% within
1 degree. The exporter writes the shared mesh's normals, so that last one is a mechanism to watch rather
than a live defect; it is the reason a path that took the baked normals would silently perturb the
field.

## 6. One defect in the export, and one claim of a defect that was wrong

**The cylinder blend's weight source: it was right, and this document first said it was wrong.** The
first version of this section claimed `vertex_colour_blue()` read the wrong extra-UV component. It does
not. The exporter packs the vertex colour into the pmx's third extra uv as `(r, g, b, a)`
(`ModelExporter.cs:917`), and mmd_tools maps `UV{n}` to `additional_uv[n-1].xy` and `_UV{n}` to
`additional_uv[n-1].zw`, so `_UV3.uv.x` **is** `additional_uv[2].z`, the blue - which is what the game's
vertex program reads as `v3.z`. The original code was correct.

What misled that reading was a mean taken over two *different* vertex sets: `_UV3.uv.x` measures 0.2044
over the face's 1328 vertices where the Unity-side blue mean is 0.1710, while `UV3.uv.x` measures 0.1702
and looked like the match. Comparing the two sides as a **multiset** instead, which needs no alignment,
settles it: of the 120 distinct `(r,g,b,a)` tuples in the pmx's face materials, **119 agree** under
`(r,g,b,a)`, while the permutations that put the blue first agree on 20 and the reversed order on 5. The
coincidence is that colour *r*'s mean on this model (0.1700) sits within 0.001 of colour *b*'s (0.1710).
A mean cannot separate them; a multiset can. See `GAME_DATA_VS_VIEWER.md`.

**The exporter drops two face vertices.** Unity's `M_Face` has 1330; the exported face materials carry
1328. The arithmetic closes on it: `1328 * 0.1702 + 2 * 0.70 = 227.42`, and `227.42 / 1330 = 0.17100`,
exactly Unity's measured blue mean. So the two missing vertices carry a blue of about 0.70.

## 7. A correction to the reference assumption

`docs/VIEWER_SHADING.md` describes the viewer's face as rendering with a Nars replacement shader
(`Nars/UmaMusume/Face`, with `smoothstep` and `fwidth`), and an earlier line of work concluded from that
shader's source that the mask is `_TripleMaskMap.g`. That document is describing a shader the face does
not use. `UmaViewerMain.cs:223` loads the *game's* `shader` bundle into `Builder.ShaderList`, and
`UmaContainerCharacter.cs:601-608` only substitutes for `Noline` costume variants; the live face
material's shader name is `Gallop/3D/Chara/ToonFace/TSER`. **The viewer does run the vendor's compiled
shader**, so it remains valid ground truth - and the `_TripleMaskMap` conclusion was right about the
texture for the wrong reason.

## 8. What is left

The mask is fixed and eliminated. The normal field is measured as perturbed at the source and is the best
remaining candidate. The two things that would settle it, cheapest first:

1. **Find out why the exporter's face normals differ from the mesh's.** `-umaDumpFace` already reaches the
   game's mesh; dumping the same fingerprint *inside the exporter*, from `BakeMesh`, and comparing it
   against the renderer's `sharedMesh.normals`, localises the loss to one of the two in a single run. If
   `BakeMesh` is the source, writing the shared mesh's normals (rotated by the skinning that the bake
   applied) is the fix.
2. **The ~50-level global difference is still unexplained, and the mask did not close it.** The viewer
   measures 147 in every matched comparison where we measure 199. The project's own advice is to
   neutralise tone mapping on both sides before drawing a conclusion; that has not been done.
3. **Render the same comparison with the option-mask branch implemented** (`_UseOptionMaskMap`,
   `_OptionMaskMap` = `*_ctrl`), which the port still does not have, and with the mask read from `t1`
   as it now is.

## 9. Reproducing the measurements

```powershell
# the variant map and the texture-register order, straight from the game's bundle
cd Tools/game_rip
python variant_map.py <shader_bundle.unity3d> "Gallop/3D/Chara/ToonFace/TSER" <scratch dir>

# disassemble every container of the runtime shader
dxbc_disasm <scratch>/Gallop_3D_Chara_ToonFace_TSER_region.bin <scratch>/dxbc

# the mask A/B, same light and framing both ways
blender --background --factory-startup --python Tools/blender_render_angles.py -- `
    --pmx <model.pmx> --out-dir <dir> --view face --apply-shader --new-face `
    --grid "0,+90,-90" --light-elevation 70 --yaws 0 --face-mask triple
```

Scratch scripts that produced the numbers in this document live in `_uma_scratch/`:
`variant_map`-feeding `region_keywords.py`, `face_mask_uv_probe.py` (samples the candidate masks at the
face's own UVs), `mask_ab_diff.py` (pixel diff of the two render sets), `boundary_shape.py`,
`face_normal_fingerprint.py` and `pmx_face_probe.py` (the normal field, with Blender out of the loop).
