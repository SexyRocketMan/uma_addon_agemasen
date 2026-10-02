<!-- superseded -->
> **Note.** Superseded. This file describes earlier re-implementations of the face shader, including a Nars port with a mask-gated smoothstep. None of that is in the addon now: `Umashader.py` was rolled back to the commit before this fork's shader work, and the face shading is being rebuilt from the game's own program. Its statement that the viewer's shader ends in `saturate(t / fwidth(t))` is also wrong - the game's programs contain no derivative instructions at all.

# The face shading: where it started, what the target is, and what we changed

Written after a long detour, because the goal and the evidence got tangled. Short version: the artefact we set
out to fix is an **anti-aliasing** problem and a **mask** problem, in that order, and the mask is the one we do
not yet have.

## The goal

The face in Blender should show the same lit/shaded (diffuse/shaded) boundary as UmaViewer, and it should stay
clean when morphs deform the face. Nothing else about the face was ever wrong.

## Why the boundary was jagged

The shipped `Uma Shader` group decides lit from shaded with a **hard comparison**: a `Less Than` against the
`Light Threshold` socket. A binary test on a per-pixel value has no anti-aliasing, so the boundary is aliased -
and on a low-poly face it is worse than aliased, because the value being compared is computed from *interpolated
vertex normals*, so the boundary lands wherever those normals cross the threshold. That is the line users see
following the triangles.

Under a morph it gets worse still: the morph moves vertices but not the authored normals, so the normal field and
the geometry disagree, and the boundary can step across triangles that the deformation just moved.

UmaViewer's face shader ends differently:

```hlsl
colour = lerp(_ToonMap, _MainTex, saturate(t / fwidth(t)));   // t = shadow - 0.26
```

`saturate(t / fwidth(t))` is a **one-pixel-wide anti-aliased edge**: `fwidth` is the screen-space derivative, so
the boundary is always about a pixel wide whatever the geometry does. Crisp, never stair-stepped. That is the
mechanism Blender's shader is missing, and it is the whole of the "jagged line" complaint.

## What else the viewer's shader has that the Blender group does not

| the viewer | the shipped Blender group |
| --- | --- |
| `saturate(t / fwidth(t))` - one-pixel anti-aliased edge | a hard `Less Than`, no anti-aliasing |
| the mask is a **texture**, `_TripleMaskMap.g` - the shadow's shape is painted, in texture space | none: the shadow's shape comes from the lighting term, i.e. from the geometry |
| blends two textures, `lerp(_ToonMap, _MainTex, …)` | decides between them with a step |
| the light direction is the scene's (and `_ViewDirX/Y` for the viewer's drag) | a manual sun empty, and the group's own threshold |
| an outline pass in the same shader (`Cull Front`, `v.normal` offset, `v.color.r` width) | a separate geometry-nodes group that reads an outline normal set we do not export |

The second row is why morphs still hurt: with no painted mask, `t` is driven by the lighting term, which is driven
by the normal field, which is what a morph disturbs. The viewer is not immune to that either - it is that its
shadow *shape* comes from the texture, so the geometry only moves the boundary slightly, within a painted region.

## What we changed, in order

1. **Fixed the operator for Blender 5.2** (geometry-nodes socket names, the node-group interface changes,
   `meta_info` handling). Kept. This is what made the addon usable on 5.2 at all.
2. **Cylinder normal blending and face normals pointed where the character looks** - built from the *retail
   game's* decompiled shader. This does clean the boundary, and it is why the face looked good in the middle of
   the session. But it makes `NDL` a constant, which kills all response to the light, and it is not what the
   viewer does. **Off by default now; the honest verdict is that it was a plausible wrong turn.**
3. **A half-plane "cheek band"** - also the retail game's (`UMA_Face.fx`), also not in the viewer. Tuned against
   the game's own screenshots before we knew they were the viewer's. **Off.**
4. **Fade shading based on a *leftover* viewer shader** (`Resources/Materials/UmaShaderFace.shader`, whose light
   direction is a constant `(0,-1,0)` and whose mask is `base.r`). This cost the face its shading entirely.
   **Reverted.**
5. **The Nars port** - the shader the viewer actually renders with: mask-gated `smoothstep`, `lerp(_ToonMap,
   _MainTex, …)`, a live light direction driven from the sun lamp, with a narrow `smoothstep` standing in for
   `fwidth`. **This is the right mechanism**, and it is what the grids above show working, but it is not yet
   better than legacy for two reasons below.

## Why the Nars port still loses to legacy

* **We do not have the mask.** The shader reads `_TripleMaskMap.g`, and that texture is not among the exported
  maps: measured, `_base.green` is a flat 0.52 and `_ctrl.green` is 0 everywhere, while `_base.red` is bimodal
  (56% near 0, 43% near 255) - a mask, but the *wrong* mask. So our shadow shape is still driven by `NDL`, i.e.
  by geometry, i.e. by whatever a morph does to the normal field. That is why the morph artefacts survive.
* **The anti-aliasing is a stand-in.** A narrow `smoothstep` instead of `fwidth` does not scale with the pixel,
  so it can be too soft at some angles and too hard at others.

And one measurement that says the balance is off as well: at ±60 degrees of sun our face is majority shaded where
the viewer's is majority diffuse, which is `_ShadowStrength` and light energy, not mechanism.

## What to do next

1. **Default the face to legacy until the Nars port beats it.** The user's own comparison is the verdict, and a
   shader that ignores the sun is not shippable.
2. **Get the real `_TripleMaskMap` out of the viewer.** `ModelExporter.cs` already has `Map("triple",
   "_TripleMaskMap")`; the question is why the texture does not reach the export - likely the viewer's material
   binds it somewhere the exporter skips, or not at all for this costume. Without that map, the mask is guesswork
   and the morph artefacts cannot be fixed properly.
3. **Then** tune `_ShadowStrength` and the light energy against the viewer's own grid, and only then decide
   whether the stand-in for `fwidth` is good enough.

## Correction: which shader this model's face actually uses

The exported material comment - written by the viewer's own exporter, and a list of the properties the material
actually has - settles it:

    mtl_chr1001_00_face:  triple=tex_chr1001_00_face_base  option=tex_chr1001_00_face_ctrl
                          toon=tex_chr1001_00_face_shad_c   toon_step=0.4 ...

So `_TripleMaskMap` **is** the base map, and the shader's `g = _base.green` - which measures a flat 0.52 on this
model. A flat mask means the Nars shader's shadow shape comes entirely from `NDL`, i.e. from the normal field,
i.e. from the geometry - which is precisely the morph artefact we are trying to get rid of, and precisely what
the viewer's face does *not* do (its shadow is painted and clean).

That points the other way: for this model the face is very likely shaded by
`Assets/Resources/Materials/UmaShaderFace.shader` after all - the file originally dismissed as a leftover - whose
mask is `base.RED` (bimodal, 56% near 0, 43% near 255: a painted mask), giving a shadow whose shape is painted in
texture space and therefore immune to both morphs and triangle edges.

Which means the reverted `f4f1d41` was closer to right than the port that replaced it, and the belief that it was
"built on a leftover" was based on the Nars shader's property list rather than on which shader this material is
assigned.

The tension to decide, because it cannot be resolved by fidelity alone: that shader's light direction is the
constant `(0, -1, 0)`, so a face shaded by it does not follow the sun. Fidelity says the face should not respond;
the user's expectation says it should. Reference and preference have to be separated here, deliberately.
