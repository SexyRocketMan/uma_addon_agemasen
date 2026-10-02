<!-- superseded -->
> **Note.** Read with `GROUND_TRUTH.md` for the current state.

# The game's face shader, read from its bytecode

Everything below comes from disassembling `Gallop/3D/Chara/DitherToonFace/TSER` out of the installed game. Slot
*names* are identified by how they are used and by the shader's own property list; the arithmetic, the thresholds
and the channel assignments are read directly from the instructions.

The tooling that produced it is in the viewer repository at `Tools/game_rip/`, and both disassemblies are kept
there (`face_pixel_8tex.asm.txt`, `face_pixel_9tex.asm.txt`) because re-deriving them needs the game installed.

## 1. Structure: three constant buffers, four vertex programs, eleven pixel variants

| buffer | contents |
| --- | --- |
| `cb0` | per-frame and global: camera position (`cb0[20]`), light direction (`cb0[6]`), matrices, colour grading (`cb0[149]`, `cb0[151..153]`) |
| `cb1` | per-material: `_ToonStep`, `_ToonFeather`, specular, rim, environment, and the mask-colour constants |
| `cb2` | per-character and per-renderer: texture tiling (`cb2[0]`), `_NormalizeNormal` (`cb2[16].z`), `_CylinderBlend` (`cb2[23].w`), the head frame (`cb2[24].yzw` up, `cb2[26].xyz` centre) |

The face shader emits four vertex programs (identical in size, so one program serves several passes) and eleven full
pixel variants: eight with 8 textures and `cb1[28]`, three with 9 textures and `cb1[40]`, plus small programs for
simpler passes. The extra texture and the larger material buffer in the 9-texture variants are the `USE_MASK_COLOR`
path - the mask-colour regions.

## 2. The vertex program

```hlsl
float3 worldPos = skin(position);
o1.zw = saturate(f(worldPos, cb0[140..142]));       // fog / distance factor for the pixel stage

mad o1.xy, v1, cb2[0].xy, cb2[0].zw                 // uv with the material's tiling
o3 = vertexColour;                                  // passed straight through

float3 n = normal;
n = (cb2[16].z <= 0.5) ? n : normalize(n);          // _NormalizeNormal selects raw or normalised
n = mul(n, skinRotation);

// --- THE CYLINDER BLEND, in the vertex shader
float3 toAxis = worldPos - cb2[26].xyz;                        // _FaceCenterPos
float  along  = dot(cb2[24].yzw, toAxis);                      // _FaceUp
float3 onAxis = cb2[26].xyz + along * cb2[24].yzw;
float3 radial = normalize(worldPos - onAxis);                  // outwards from the head's axis
float  weight = (1 - cb2[23].w) * vertexColour.z;              // (1 - _CylinderBlend) * vertex colour blue
o4.xyz = mad(weight, n - radial, radial);                      // the blended normal
o6.xyz = mul(o4.xyz, skinRotation);                            // to world space for the pixel stage

o5.xyz = worldPos;
o7.x   = exp(11 - 10 * cb2[3].x);                              // a per-material exponent curve
o8.xy  = (clip terms) * cb0[22].xy * 0.25;                     // a screen-space lookup uv
```

**This is the cylinder blend, exactly as the community ports described it**, and it lives in the vertex shader: the
axis through `_FaceCenterPos` along `_FaceUp`, the radial direction outwards from that axis, and the normal blended
by a weight of `(1 - _CylinderBlend) * vertexColour.b`. The vertex colour's **blue** channel is the weight - the same
channel this project already exports and feeds to the shader.

It also confirms that `_NormalizeNormal` is a real branch, that the vertex colour is passed through untouched, and
that the environment lookup is driven from the blended normal in screen space.

## 3. The pixel program

```hlsl
float4 diff = tex2D(t0, uv);                    // _MainTex        (diffuse)
float4 toon = tex2D(t1, uv);                    // _ToonMap        (*_shad_c)
float3 mask = tex2D(t2, uv).rgb;                // _TripleMaskMap  (*_base)
mask = 0.5 + w * (mask - 0.5);                  // remapped around 0.5, weighted by a constant

float3 view     = normalize(cameraPos - worldPos);
float3 lightDir = normalize(cb0[6].xyz);
lightDir = lerp(lightDir, normalize(slot27.xyz), flag);        // _OriginalDirectionalLightDir, cb1[26].w

float ndl         = dot(normal, lightDir);                     // the cylinder-blended normal
float halfLambert = 0.5 * ndl + 0.5;

// --- the lit/shaded step
float t    = (slot3.w - slot4.x - toon.r * halfLambert) / slot4.x;    // _ToonStep, _ToonFeather
float step = saturate(1 + t);
if (slot4.x <= 0) step = 1 - step;                             // inverted variants
```

That is the same formula this project already had from the community ports and from UmaViewer's own older face
shader:

```hlsl
saturate(1 + (_ToonStep - _ToonFeather - mask * halfLambert) / _ToonFeather)
```

With the material's values - `_ToonStep = 0.4`, `_ToonFeather = 0.001` - it is a **hard step at halfLambert = 0.4**,
i.e. shaded where `N·L < -0.2`. The mask multiplying `halfLambert` is **`_ToonMap`'s red channel**, the shaded
texture, not the triple mask.

```hlsl
// --- specular, gated by the triple mask's red
specular = pow(max(ndl, 0), slot3.x * 10 + 1) * mask.r * slot2.xyz;      // _SpecularColor

// --- rim, gated by the triple mask's blue
rimFactor = saturate(1 + (slot8.x - dot(normal, rotate(view, slot10.w))) / slot8.y);
rim       = rimFactor^4 * slot9.w * mask.b;

// --- where the shadow and the region colours fall
float g     = toon.g;
float cheek = (g >= 0.51) ? 2 * (g - 0.51) : 0;                // the cheek region
float nose  = (g <= 0.49) ? 1 - 2 * min(g, 0.49) : 0;          // the nose region

float4 area = tex2D(t8, uv);                                   // _MaskColorTex, the runtime *_area* texture
float3 low  = (0.49 - min(area.rgb, 0.49)) * (1 / 0.49);
float3 high = (max(area.rgb, 0.51) - 0.51) * (1 / 0.49);
colour *= lerp(1, maskColour.rgb, low.rgb);                    // one colour per channel, per side
colour *= lerp(1, maskColour.rgb, high.rgb);

// --- the half-plane test, exactly as the ports described it
sideOfFace  = sign(dot(cross(slot25.xyz, worldPos - slot26.yzx), slot24.yzw));
sideOfLight = sign(dot(cross(slot25.xyz, view),                slot24.yzw));
blend       = (sideOfFace * sideOfLight + 1) * 0.5;

// --- the vertex colour scales the toon colour weights
weight = f(vertexColour.w, slot27.w);                          // _VertexColorToonPower
```

**The 0.51 and 0.49 thresholds appear twice**: once on `_ToonMap`'s green channel for the cheek and nose regions,
and once per channel on the `*_area*` texture for the region colours. They are literals in the bytecode, and they
are the community ports' pretense thresholds, confirmed.

The face frame slots - `cb1[24]`, `cb1[25]`, `cb1[26]` - are used as **up, forward and centre**, with the cross
product and its sign taken, exactly as `sign(dot(cross(F, P - C), U))`.

## 4. Every term, and what drives it

| term | driven by |
| --- | --- |
| cylinder-blended normal | vertex shader: `(1 - _CylinderBlend) * vertexColour.b`, about the axis through `_FaceCenterPos` along `_FaceUp` |
| lit/shaded step | `saturate(1 + (_ToonStep - _ToonFeather - _ToonMap.r * halfLambert) / _ToonFeather)` |
| specular | `pow(max(N·L,0), power)` gated by `_TripleMaskMap.r` |
| rim | a view term raised to the fourth, gated by `_TripleMaskMap.b` |
| cheek region | `_ToonMap.g >= 0.51`, combined with the light through the face frame |
| nose region | `_ToonMap.g <= 0.49`, likewise |
| region colours | the `*_area*` texture against 0.49 / 0.51 per channel, multiplying the diffuse |
| vertex colour | blue is the cylinder weight; alpha scales the toon colour weights |
| toon bright / dark | present, but transparent in this material, so both branches are inert |
| environment | sampled with the blended normal in screen space, added |
| cutoff / dither | `discard` against a texture lookup, so the cutout is its own map |

## 5. What this settles, and what it corrects

**Settled from the instructions:** the community research - cylinder blend, face-frame uniforms, the half-plane
test, the 0.51/0.49 thresholds - is accurate in detail. UmaViewer's *older* face shader is a faithful copy of the
game's formula, with `0.3`/`50` standing in for `_ToonStep`/`_ToonFeather`. The article's "detail mask aimed at the
nose and cheeks" is `_ToonMap`'s green channel plus the `*_area*` region colours. And the mechanisms this project
implemented by guessing - flat-face normals, a half-plane cheek band, a painted mask - are all genuinely parts of
the game's shader, arrived at from the wrong end and with wrong parameters.

**Corrected, each time from reading rather than reasoning:**

- **Half a program is not a program.** From the first 60 pixel instructions I concluded the toon blend used the
  triple mask's blue channel. The rest shows red driving the toon step (via `_ToonMap.r`), blue gating the rim, and
  the triple mask's red gating specular.
- **`_ToonMap` is not merely a shaded colour texture.** Its red and green channels carry the mask that shapes the
  shadow and the cheek/nose regions, which explains why `*_shad_c` measures 95% bright in red and 0% in green. The
  deviations from those values *are* the painted detail.
- **A stored asset is not the runtime state.** Twice: `_MaskColorTex` is bound at runtime to an `*_area*` texture
  with `USE_MASK_COLOR` enabled, and `_CylinderBlend` is set per part rather than baked.
- **A histogram cannot tell a soft mask from a flat fill.** Treating the absence of structure in a summary as the
  absence of information was the single most expensive habit in this work.

## 6. Still open

- **Slot-to-property names for `cb1`.** Unity strips the reflection chunk, so the blob carries no names.
  `cb1[3].w`/`cb1[4].x` as `_ToonStep`/`_ToonFeather` is consistent with the material's values and the property
  declaration order, and the rim, specular and region constants line up the same way, but it is identification by
  use rather than by name.
- **Which variant the face material runs.** Eleven candidates differ by keyword; the 9-texture `cb1[40]` ones are
  the `USE_MASK_COLOR` path the viewer enables for the face, so that is the one to implement.
- **The `*_area*` texture itself.** Not yet extracted or looked at. It is the detail mask.

## 7. The `*_area*` texture, extracted: the region colours are inert on faces

The last unexamined input. `tex_chr0001_00_face000_0_area`, the mask-colour region map for the base character:

```
r: mean 128.0   0.1% below 0.49   1.1% above 0.51
g: mean 128.5   0.0% below 0.49   0.8% above 0.51
b: mean 128.8   0.7% below 0.49   1.5% above 0.51
```

Rendered and amplified - the thresholds are 0.49 and 0.51, so only the distance from 0.5 matters - it is **flat grey
with a single small block in one corner**. And character 1001's face has **no `*_area*` texture at all**: its head
textures are `_face_diff_wet`, `_face_dirt` and `_face_shad_c_wet`, and the viewer's loader searches for one with
`EndsWith("area")` and finds nothing.

So the region machinery is real, is bound at runtime, and is **effectively inert** for the faces a user is likely
to export: a neutral map means the `0.49`/`0.51` tests almost never fire, and 1001 has no map to fire them with.

**That corrects the correction.** Having found that `_MaskColorTex` is bound at runtime, I wrote that the region
colours "are part of the face shading". They are part of the *shader*; on these faces they are not part of the
*image*. The face's shading comes from the toon step over `_ToonMap.r` and the cheek and nose regions in
`_ToonMap.g`, and those are what an implementation has to reproduce.

## 8. The sources these conclusions came from

Kept in the viewer repository at `Tools/game_rip/shader_sources/`, so the reading can be checked without the game
installed:

| file | what it is |
| --- | --- |
| `face_vertex.asm.txt` | skinning, `_NormalizeNormal` and the cylinder blend |
| `face_pixel_use_mask_color.asm.txt` | the pixel variant with `cb1[40]` - the `USE_MASK_COLOR` path the face runs |
| `face_pixel_8tex.asm.txt` | the smaller pixel variant, for comparison |
| `pass_2tex.asm.txt`, `pass_no_textures.asm.txt` | two small passes, for the shape of the rest |

`Tools/game_rip/README.md` has the from-zero recipe: how to find an asset by name in the game's encrypted
database, the bundle hashes and entry keys for the shader catalogue, the face material and the area texture, and
the extraction and disassembly steps in order.
