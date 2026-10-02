<!-- superseded -->
> **Note.** Read with `GROUND_TRUTH.md`. The extraction chain described here is what produced the disassembly everything else rests on and is still correct.

# The game's own face shader: what it says

Ground truth, extracted from the installed game rather than inferred. The method is in the viewer repository at
`Tools/game_rip/README.md`; the short version is that the game's asset database is an encrypted SQLite file whose
rows give every asset's path, bundle hash and bundle key, and the bundles are UnityFS files XORed from byte 256 on
with a keystream derived from that key.

## The shaders

The character shaders are a family under `Gallop/3D/Chara/`, and the face one is:

```
Gallop/3D/Chara/DitherToonFace/TSER          asset file charactertoonfacetser.shader
Gallop/3D/Chara/DitherToonFace/TSEREm
Gallop/3D/Chara/HologramToonFace/TSERA
Gallop/3D/Chara/NolineToon/TSER … (body, hair, dither and alpha variants)
```

* UmaViewer loads `assets/_gallop/resources/shader/3d/character/charactertoonfacetser.shader` directly
  (`UmaAssetManager.cs`) - so the viewer runs the game's real shaders for game models, and its own
  `NarsShader`/`UmaShader` files are re-implementations for MMD and exported models. That is the same reverse
  engineering this note describes, done earlier and further: the `Gallop` directory in the viewer is not a
  coincidence.

## The face shader's properties

```
_MainTex  _ToonMap  _TripleMaskMap  _OptionMaskMap  _UseOptionMaskMap
_ToonStep  _ToonFeather  _ToonBrightColor  _ToonDarkColor
_CylinderBlend  _VertexColorToonPower  _NormalizeNormal
_FaceUp  _FaceForward  _FaceCenterPos
_MaskColorTex  _MaskColorR1 _MaskColorR2  _MaskColorG1 _MaskColorG2  _MaskColorB1 _MaskColorB2
_MaskToonColorR1 _MaskToonColorR2  _MaskToonColorG1 _MaskToonColorG2  _MaskToonColorB1 _MaskToonColorB2
_Saturation  _CharaColor  _LightProbeColor
_UseOriginalDirectionalLight  _OriginalDirectionalLightDir
_OutlineWidth _OutlineColor  _DirtTex _EmissiveTex  _Rim* (two sets)  _Env* _Specular* _Stencil* _Cutoff
```

## What the real face material says

`mtl_chr1001_00_face`, as stored in the game:

| property | value | note |
| --- | --- | --- |
| `_ToonStep` | **0.4** | our exporter already carries this |
| `_ToonFeather` | **0.001** | essentially a **hard** step - the game does not soften the boundary |
| `_CylinderBlend` | **0.0** | off for the face; the cylinder normal is a *hair* feature |
| `_VertexColorToonPower` | 1.0 | the vertex colour modulates the toon power |
| `_UseOptionMaskMap` | 1.0 | the per-costume option mask is in use |
| `_MaskColorTex` | **null** | the whole `_MaskColor*` / `_MaskToonColor*` machinery is **unused on the face** |
| `_TripleMaskMap` | bound to a real texture | the triple mask *is* used on the face |
| `_ToonMap` | bound to a real texture | the shaded colour texture |
| `_ToonBrightColor`, `_ToonDarkColor` | (1,1,1,0) | alpha 0: disabled |
| `_Saturation` | 1.0 | |
| `_faceShadowAlpha` | 0.0 | |
| `_faceShadowEndY` | 0.0 | face-shadow parameters: a band with a length and an end position |
| `_faceShadowLength` | 0.1 | |

## What this corrects, in order of how much it cost

- **`_ShadowStrength` and `_ViewDirX`/`_ViewDirY` do not exist in the game.** They are UmaViewer's own inventions
  in its Nars re-implementation. Two sessions were spent tuning a parameter the game has never had.
- **The boundary is hard, not feathered**: `_ToonFeather = 0.001`. So the game's clean lit/shaded edge is *not*
  produced by anti-aliasing the step - which retires the `fwidth` stand-in and the wide smoothstep work.
- **`_CylinderBlend = 0` on the face.** The cylinder normal is for hair; the face does not use it. The work that
  implemented it for the face was aimed at the wrong part.
- **The `_MaskColor*` region machinery is unused on the face** (`_MaskColorTex` is null). My most recent
  hypothesis - per-channel colour replacement as "the detail mask" - is wrong for this material.
- **`_FaceUp`, `_FaceForward`, `_FaceCenterPos` are real face-shader uniforms**, so the head frame the community
  ports described is genuine, whatever the game does with it.
- **`_faceShadow*` is a face-specific shadow band** - a length, an end Y and an alpha, all separate from the toon
  step. This is the closest thing in the material to the article's "detail map aiming at nose and cheeks", and it
  is the thing to look at next.

## What is still unknown

The shader has **no source in the build** - only compiled blobs (`m_ShaderIsBaked`, `progVertex`/`progFragment`
with `compressedBlob`). So the property *names and values* are now facts, but the arithmetic behind `_ToonStep`,
the triple mask's three channels and the `_faceShadow*` band is still not read directly. Two ways forward: resolve
each material's texture bindings to actual files and study the textures, or disassemble the blobs.

## The three masks, seen rather than measured

`_TripleMaskMap` is the face's `base` texture - the viewer's runtime code sets it with
`SetTexture("_TripleMaskMap", textures.LoadLast(t => t.name.Contains("_face") && t.name.EndsWith("base")))`, which
is exactly what this project's exporter writes, so the export is faithful. Its three channels are three different
masks, and they only make sense when looked at:

| channel | contents |
| --- | --- |
| R | a hard **face silhouette** - white face, black hair and background, and it is bimodal (43% near 255, 56% near 0) |
| G | a **soft gradient mask** - bright around the eyes, a **dark spot at the nose**, a smooth falloff over the cheeks |
| B | near-white with a **black notch** where the hair overlaps the face - an occlusion or cutoff mask |

`_shad_c` is the shaded colour texture: all three channels near-white, carrying the brows, eyes, nose and mouth
marks.

**The article's "detail mask aimed at the nose and cheeks" is the green channel of this map.** That reconciles
three separate things that had looked contradictory: the article's description, the viewer's shader reading `.g`,
and this project's own measurement of that channel as a mean of 0.52 with no bimodality.

Which was a measurement error worth recording. The histogram was right; the conclusion drawn from it - "flat, so
it carries nothing" - was wrong. A *soft* mask looks exactly like that in a histogram: a mid-grey average with
gentle variation, and no bimodality because it never saturates. Rendering the channel as an image shows the nose
shadow immediately. **A histogram cannot distinguish a soft mask from a flat fill; an image can.** That mistake
sent this project looking for the mask in the red channel, which is the *silhouette*, not the shading.

## Where the boundary actually comes from, so far

Putting the values and the channels together for the face:

- a **hard** toon step (`_ToonStep` 0.4, `_ToonFeather` 0.001) over `NDL`;
- modulated by the **soft detail mask** in the triple mask's green channel, aimed at the nose and cheeks;
- with `_faceShadow*` parameters (a length, an end position, an alpha) forming a separate band;
- and `_CylinderBlend` at 0, so the *normal* the step sees on the face is the mesh's own;
- `_ToonMap` as the shaded colour texture to blend towards.

The arithmetic joining those is still the compiled shader, and is the one remaining unknown.

## The shader bytecode, disassembled

The blobs are DXBC (D3D11), so they can be disassembled rather than merely inspected. The tooling is in the viewer
repository at `Tools/game_rip/` (`dxbc_disasm`, plus the blob extraction and container walker). The face shader's
region holds 28 containers: four vertex programs and the rest pixel, one per multi-compile variant.

A first reading of the pixel program, with `t0 = _MainTex`, `t1 = _ToonMap`, `t2 = _TripleMaskMap`:

```
sample  r0, uv, t6                       ; a flag/mask texture
ge      r0.x, cb1[19].y, r0.x            ; against a constant
sample  r0, uv, t0                       ; _MainTex
sample  r1, uv, t1                       ; _ToonMap
sample  r2, uv, t2                       ; _TripleMaskMap
add     r2.xyz, r2.xyzx, (0, 0, -0.5)    ; mask.B - 0.5          <- the BLUE channel
mad     r2.xyz, r1.zzzz, r2.xyzx, (0,0,0.5)   ; blended with _ToonMap.b
dp3     r1.z, v4, r4                     ; N.L                   (v4 normal, r4 light)
mad     r2.w, r1.z, 0.5, 0.5             ; halfLambert = N.L*0.5 + 0.5
add     r3.w, cb1[3].w, -cb1[4].x        ; threshold - feather
mad     r3.w, -r1.x, r2.w, r3.w          ; threshold - mask*halfLambert    <- the toon step
ge      r4.w, 0.0, cb1[4].x              ; sign of the feather, for inverted variants
add     r4.w, -v3.w, 1.0                 ; 1 - vertex colour
mul     r4.w, r4.w, cb1[27].w            ; * a constant          <- _VertexColorToonPower
mad     r0.xyz, r0.xyzx, r6.xyzx, r7.xyzx     ; final = diffuse * a + b
```

What that establishes, and what it corrects:

- **the face blends towards `_ToonMap` modulated by the triple mask's BLUE channel** - not green, which is what the
  channel looked like rendered as an image, and not red, which was an earlier guess from its histogram;
- the toon step is `threshold - mask * halfLambert`, with a **feather** constant of its own and a sign test that
  admits inverted variants;
- the **vertex colour** enters the shading, scaling through a constant - the `_VertexColorToonPower` property,
  which is the real form of what the community ports called a cylinder weight;
- `_ToonBrightColor` and `_ToonDarkColor` being transparent in the material is consistent: the colour path here is
  `diffuse * a + b`, not a bright/dark mix.

Still to do: map `cb1`'s slots to property names (the RDEF chunk has them, at offsets that need care), and read the
remaining pixel variants, since the multi-compile keywords pick different branches and only one of them is what the
face material actually runs.

## What the game assigns at runtime, and three corrections

The stored material has **null texture bindings** - the game assigns everything when it loads a character. The
viewer does the same thing, and its assignment code is therefore readable evidence for what the game's shader
receives. For the face:

```
_MainTex        <- *_face*diff
_ToonMap        <- *_face*shad_c
_TripleMaskMap  <- *_face*base        (confirms this project's export mapping)
_OptionMaskMap  <- *_face*ctrl
_MaskColorTex   <- *_face*area        and USE_MASK_COLOR is enabled
_FaceForward / _FaceUp / _FaceCenterPos  <- the head bone's forward, up and position, live
```

Three corrections follow, all of the same kind - I concluded from *stored assets* what happens at *runtime*:

- **`_MaskColorTex` is not unused.** I said the `_MaskColor*` / `_MaskToonColor*` machinery was dead because the
  stored material's binding is null. At runtime it is bound to a `*_area*` texture and `USE_MASK_COLOR` is on, so
  the region colour machinery *is* part of the face shading. An `_area_` texture is the best candidate for the
  article's "detail mask aiming at the nose and cheeks" - which makes the earlier guess about it directionally
  right after all.
- **The head frame uniforms are live**, set from the head *bone* every frame, not baked.
- **`_CylinderBlend` is set to 0.25 for other parts, not the face** - the stored face value of 0 stands, so the
  cylinder normal really is a hair and body feature.

The lesson is the same one as the histogram mistake, one level up: a stored asset holds what was saved, not what
runs. Two conclusions in this project were wrong for that reason, and both would have been caught by asking what
assigns the value rather than what the file contains.
