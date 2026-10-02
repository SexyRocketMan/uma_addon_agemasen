<!-- superseded -->
> **Note.** Superseded in part. The face does not use the replacement shader described here; the viewer loads the game's own compiled face shader (`Gallop/3D/Chara/ToonFace/TSER`) through `UmaAssetManager.cs`, which `GROUND_TRUTH.md` and `BOUNDARY_DIAGNOSIS.md` both rely on.

# What the viewer's face shader actually does

Written twice. The first version of this note described `Assets/Resources/Materials/UmaShaderFace.shader`, which
is a leftover - its `float3 lightDirection = float3(0, -1, 0)` is a constant, so a face shaded by it could never
respond to the sun, and building on it cost the face its shading entirely. The shader the viewer actually renders
with is **`Assets/Resources/NarsShader/Shaders/UmaMusume Face.shader`** (`Nars/UmaMusume/Face`, Amplify-authored).
This note describes that one.

## The shader

Properties, which are also the material parameters the effect needs:

| property | meaning | default |
| --- | --- | --- |
| `_MainTex` | the lit skin | |
| `_ToonMap` | "SSS" - the **shadowed** skin. Everywhere below where "ShadowTexture" appears, this is it | |
| `_TripleMaskMap` | "HardShadows" - the painted mask; the **green** channel is what this shader uses | |
| `_ShadowStrength` | how far the shadow goes | 3 |
| `_ViewDirX`, `_ViewDirY` | rotate the light direction about the camera - what the viewer's right-drag adjusts | 0 |
| `_FallBackBrightness1` | brightness when the scene has no light | 0.6 |
| `_Outlinewidth` | the outline pass' width | 0 |

The shading itself, from the forward pass:

```hlsl
float  NDL    = dot(lightDir, worldNormal);                        // the scene's light, or the view-rotated one
float  g      = tex2D(_TripleMaskMap, uv).g;
float  shadow = smoothstep(0.0, g * (1.0 - NDL), NDL * _ShadowStrength * g);
float  t      = shadow - 0.26;
float4 colour = lerp(_ToonMap, _MainTex, saturate(t / fwidth(t)));  // shadowed -> lit
c.rgb         = globalLightColour * colour.rgb;
```

Read out loud, that is:

* **The face is a blend of two textures**, `_ToonMap` (shadowed) and `_MainTex` (lit), chosen per texel. Not a
  darkening, not a tint - a straight `lerp` between the two.
* **The mask is the green channel of `_TripleMaskMap`**, and it does two things: it scales the shading term
  (`NDL * _ShadowStrength * g`) and it sets where the blend begins (`smoothstep(0, g * (1 - NDL), …)`). So the
  *material itself* carries which parts of the face shadow and how strongly - painted in texture space, which is
  why the boundary never follows a triangle edge and no morph can drag it.
* **`saturate(t / fwidth(t))` is a one-pixel anti-aliased edge.** `fwidth` is the screen-space derivative, so the
  boundary is always about one pixel wide whatever the geometry is doing: crisp, but never stair-stepped. This is
  the piece with no direct equivalent in Blender's shader nodes, and it needs a stand-in.
* **The light direction is the scene's**, so the face *does* follow the sun. `_ViewDirX/Y` rotate it about the
  camera, which is how the viewer's drag control works.
* The outline is a **second pass in the same shader** (`Cull Front`, `v.vertex.xyz += v.normal * outlineVar` with
  `v.color.r` as the width) - not only the geometry-nodes group.

## What this means for the addon

* The mask and the shadow texture come from the export: `ModelExporter.cs` already writes `Map("toon", "_ToonMap")`
  and `Map("triple", "_TripleMaskMap")` into the material comment, so no exporter change is needed for them.
* `_ShadowStrength` and `_FallBackBrightness1` are not in that comment yet. `_ShadowStrength` matters - its
  default of 3 is what makes the shadow strong - so it should be added there.
* The blend belongs in the *material*, where the textures are, and can then be handed to the shader group as its
  diffuse texture, with the group's own toon step held out of the way since this shader does its own.
* `fwidth` has to be replaced. A narrow `smoothstep` on the same term gives the same look without the screen-space
  derivative; the difference is that the edge will not automatically stay one pixel wide, which is worth watching
  on a face whose mask is a low-resolution texture.
* The game-side mechanisms that were tried and reverted (the cylinder normal blend, the face normals pointed where
  the character looks, the half-plane cheek band) are **not** in this shader. They are the retail game's, they are
  off by default now, and the reason the face shading broke was one of them: with every face normal pointed the
  same way, `NDL` is a constant and no mask can vary.
