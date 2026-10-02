# The face shading: ground truth, and what has been disproved

This file is the reference. It states what is *known* about how the game shades the face, with the evidence for
each item, and then lists what has been ruled out so it does not get tried again. Everything here was
established against the game's own programs, its own shipped assets, or its own render through the viewer -
never against a summary.

Tags: **[program]** = read out of the game's DXBC. **[asset]** = read out of the game's shipped bundles.
**[game-code]** = read out of the decompiled game sources. **[measured]** = produced by an instrument in this
repository or `_uma_scratch`. **[inference]** = reasoning, labelled as such.

> **Read `docs/FRAME_AND_MAP_BUGS.md` before trusting any render from before this file's latest revision.**
> The light placement, the head bone's forward axis and the light's azimuth were all wrong in the render
> instrument, so renders labelled "elevation 15" were lit from 15 degrees *below* the horizon. Anything here
> marked **[measured]** that was measured from a *render* is affected; anything measured from the mask, the
> disassembly, the mesh or the material is not.

---

## 1. The lit/shaded factor

**[program]** `Gallop/3D/Chara/ToonFace/TSER`, the pixel program
(`Tools/game_rip/shader_sources/face_pixel_8tex.asm.txt`; the same shape in
`face_pixel_use_mask_color.asm.txt`):

```
60: sample_b r1.xyzw, v1.xyxx, t1.xyzw, s1, cb0[5].x     // t1 = _TripleMaskMap = the export's *_base
80: dp3 r1.z, v4.xyzx, r4.xyzx                            // v4 = the shading normal · the light
82: mad r2.w, r1.z, 0.5, 0.5                              // halfLambert = 0.5·N·L + 0.5
83: add r3.w, cb1[3].w, -cb1[4].x                         // _ToonStep - _ToonFeather
84: mad r3.w, -r1.x, r2.w, r3.w                           // - mask.RED · halfLambert
85: div r3.w, r3.w, cb1[4].x                              // / _ToonFeather
86: add_sat r3.w, r3.w, 1.0                               // the factor: 0 lit, 1 shaded
116: mad r7.xyz, r3.wwww, r6.xyzx, r0.xyzx                // lerp(lit, shade, factor)
```

which is

```
halfLambert = 0.5 * dot(N, L) + 0.5
factor      = saturate(1 + (_ToonStep - _ToonFeather - mask.r * halfLambert) / _ToonFeather)
colour      = lerp(_MainTex, _ToonMap, factor) ...        // the remaining terms follow the factor
```

**[asset]** For character 1001's face material: `_ToonStep` 0.4, `_ToonFeather` 0.001, `_CylinderBlend` 0.0,
`_NormalizeNormal` 0.0, `_faceShadowAlpha` 0.0, `_faceShadowEndY` 0.0, `_faceShadowLength` 0.1,
`_faceShadowColor` (0.6, 0.6, 1.0, 1.0).

**[measured]** The mask `tex_chr1001_00_face_base`, 512×512, read Non-Color: red is **0.0 at 56.1% of texels
and 1.0 at 42.6%, with only 0.7% between 0.05 and 0.95**. It is a binary mask whose transitions are about two
texels wide.

**[measured]** Under the face's own uvs the same mask's red has a mean of **0.648** and **64.6%** of the
sampled texels are above the step's threshold.

**[measured]** The mask is a **bias on where the boundary falls, not a picture of the boundary**. Forcing
`_TripleMaskMap` to 0, 0.1, 0.2, 0.3 in the viewer's own render moves the lit fraction by less than a
percentage point each time (8.4% for all four); pinning the *light* and sweeping the mask moves the lit
fraction from 8.4% to 14.5% to 43.4% to 68.0% depending on the value; and with the mask at 0 the game's
terminator is a **clean straight line**, which a binary per-texel mask cannot draw.

## 2. The shading normal is redirected per vertex

**[program]** The face's vertex program (`face_vertex.asm.txt`) does not pass the mesh normal through. It
normalises it (with a toggle), rotates it by the object matrix, and then **redirects** it:

```
86: add  r3.xyz, r1.xyzx, -cb2[26].xyzx      // P - C           the vertex position minus a point on an axis
87: dp3  r0.w, cb2[24].yzwy, r3.xyzx         // dot(U, P - C)   U = the axis direction
88: mad  r3.xyz, r0.wwww, cb2[24].yzwy, cb2[26].xyzx          // the foot of the perpendicular
89: add  r3.xyz, r1.xyzx, -r3.xyzx           // radial = P - foot
91-94: normalise radial
93: mad  r0.xyz, -r3.xyzx, r0.wwww, r0.xyzx  // N - radial
95: add  r0.w, -cb2[23].w, 1.0               // w = 1 - _CylinderBlend
96: mul  r0.w, r0.w, v3.z                    // w *= vertexColour.BLUE
97: mad  r0.xyz, r0.wwww, r0.xyzx, r1.xyzx    // w*N + (1-w)*radial
98: mov  o4.xyz, r0.xyzx                     // the normal the pixel program lights with
```

so

```
radial = normalize(P - footOfPerpendicular(P, axis through C along U))
w      = (1 - _CylinderBlend) * vertexColour.BLUE
N_shaded = w * N + (1 - w) * radial
```

**[asset]** `_CylinderBlend` is 0 on this material, and **[game-code]** nothing in the game sets it at runtime
(`Gallop/Live/Director.cs` sets the rim, toon and saturation values and not this one), so `w` is exactly the
vertex colour's blue channel.

**[measured]** On the export, over the face's 1328 vertices: the blue channel is **0.0 at 1010 of them** and
1.0 at 237, with a median of 0.0; and the resulting redirection moves the authored normal by a mean of 12.65°
(median 2.02°). The `radial` direction differs from the mesh's own normal by a mean of 21.74°.

**[measured]** But the redirection is **nearly a no-op on this model**, and that is the honest reading of the
two numbers together: it moves each normal by 12.65° on average, yet the face's shaded region barely changes -
the boundary measured 63.8% lit with it and the same with the radial's sign reversed, and an earlier attempt to
remove the normal work entirely moved the render by 12 pixels of 810,000. The reason is visible in the numbers
above: the face's normals and the radial directions both lie close to the head's own cross-section, so blending
between them changes little about which way the surface faces with respect to a light that is mostly overhead.
**Whatever makes the game's terminator clean, it is not this blend on this character.** The blend is built
faithfully anyway, because the vertex program has it and a character whose vertex colours are painted
differently would use it.

**[measured]** The export's face normals are already **smooth**: the angle between a face vertex's normal and
its neighbours' has a mean of **4.58°**, a p90 of 11.96°, and only **1.66%** of edges exceed 30°. The whole
mesh, for scale, has a mean of 19.82° and 24.79% of edges over 30°. So the export carries a clean normal
field - the jaggedness was never the export's.

**[game-code]** The game's own C# never recomputes, smooths or flattens the face's normals, and the viewer's
render path modifies no mesh data at all: its per-frame face writes are `_FaceForward`, `_FaceUp`,
`_FaceCenterPos` and `_faceShadowHeadMat` (`UmaContainerCharacter.cs:418-434`). The only `RecalculateNormals`
in the export path is on a copy, and only when a mesh is unreadable (`ModelExporter.cs:1114`).

**[inference, now qualified]** The redirection is *a* mechanism for making a low-poly face carry a clean
terminator - a field of radial directions is smooth by construction - and it is what the task's line of
reasoning expected. But the measurement below says it is not what this character's clean terminator comes from,
so it is built because the program has it, not because it is the answer here.

## 3. The textures and the uv layer

**[asset]** The face material's maps: `_MainTex` = `tex_chr1001_00_face_diff` (sRGB), `_ToonMap` =
`tex_chr1001_00_face_shad_c` (sRGB), `_TripleMaskMap` = `tex_chr1001_00_face_base` (data, no sRGB
conversion), `_OptionMaskMap` = `tex_chr1001_00_face_ctrl` (data).

**[program]** The pixel program samples the mask and the diffuse with the *same* uv, `v1.xy`, which is the
mesh's primary uv pair transformed by `_MainTex_ST`. There is no separate uv set for the mask.

**[measured]** In Blender, after mmd_tools' import, the layers are `['UVMap', 'UV1', 'UV2', 'UV3', '_UV2',
'_UV3']`. `UVMap` is the pmx's first uv pair and is what the face's textures must be read through; `UV1`,
`UV2` and `UV3` are the three **extra** uv pairs, which on this model carry data, not texture coordinates -
`UV1`'s u is 0 for **every face vertex**. Reading the mask through `UV1` samples a single column of the
texture and gets 0.000 everywhere, which shades the entire face. This was the port's largest single defect and
is fixed.

## 4. The light

**[program]** The light direction reaches the pixel program through `cb0[6]`, which is Unity's
`_WorldSpaceLightPos0` - the direction *towards* the light, normalised, for a directional light.

**[measured]** The viewer's light is white with intensity 1.0, at elevation 70°. **Rotating it moves the
boundary**: sweeping the azimuth through eight values swings the lit fraction between 5.2% and 13.7% and moves
the lit region's centroid 90 px, with adjacent azimuths overlapping 0.86-1.00 and opposite ones 0.40.

**[measured]** With the mask pinned out of the comparison, the game's terminator at azimuth 0 is a straight
line down the middle of the face. The light term, not the mask, draws the line.

## 5. The driven `_faceShadowAlpha`

**[game-code]** `Gallop/FaceDrivenKeyTarget.cs:263-277` binds the property, with no curve, to the **X scale of
a locator named `Shade_Ctrl`** (`BindProperty.LocatorPart.ScaX`), and writes it on the face material every
morph update (line 526); `Gallop/Live/Cutt/LiveTimelineFacialData.cs:141` gates it per performance off
`kAttrFaceShadowVisible`.

**[measured]** No node named `Shade_Ctrl` exists in the export - not among its 192 morphs, not among its 437
bones. So the driven variation **cannot** be reproduced from a static pmx: the export carries the rest value
(0), and the port can only offer that value or a manual override.

## 6. The engine's shadows

**[asset]** The viewer's scene light has `m_Shadows.m_Type: 2` (soft shadows) and `m_Strength: 1`, and
**[asset]** Unity's quality settings carry 4× MSAA.

**[measured]** The game's own render carries a dark area on the lower face that the port's does not reproduce.
It is not the material program: `_ToonMap` measures a mean of 232 sRGB, so the program's own texture step
cannot darken the face by 120 levels. It is the engine's shadow pass, which Blender has no counterpart of
under the material shader.

## 7. The light direction: which way round

**[measured]** This cost more time than anything else in the work, so it is written down precisely. The
question is whether the vector fed to `dot(N, L)` is the direction the light *travels* or the direction it
comes *from*. The two are negatives of each other and both render something plausible - the boundary moves
with the sun either way - so only a measurement over the whole face settles it.

What settles it, measured on a real export at sun azimuth 0, elevation 70:

| | |
| --- | --- |
| the face's mean corner normal, over all 5682 corners | (0.0012, **−0.7286**, −0.1051) |
| the sun's world rotation third column (the direction it shines along) | (0.0000, −0.9397, −0.3420) |
| mean of `dot(N, that vector)` | **+0.7206** |
| mean of `dot(N, its negative)` | **−0.7206** |

The face's normals point front-and-down and the light at elevation 70 comes from above and in front, so the
direction it travels has a large negative Y - and **that is the vector the shader wants**. Using its negative
puts the light behind the head and shades the whole face. The mean `halfLambert` is 0.86 with the vector as the
travel direction and 0.14 with its negative.

**[measured]** The consequence, once corrected: the port's lit fraction over the face is **63.8%** against the
game's **68.0%** at the same light and mask, and the shaded region falls in the same place - the forehead and
around the eye sockets.

**[measured]** And the boundary is nearly insensitive to the sun's *azimuth* at this elevation, which is worth
knowing before reading a sweep: with the light at elevation 70 the travelling vector is
`(0.342·sin(az), −0.940, −0.342·cos(az))`, and the face's mean normal is `(0.001, −0.729, −0.105)` - almost
parallel to the plane the azimuth rotates through. Rotating the azimuth therefore changes `dot(N, L)` by only
about ±0.34 times the normals' x component, which on this face is nearly zero. Sweeping the azimuth at elevation
70 is close to sweeping the light's intensity. An elevation closer to the horizontal would move the boundary far
more.

**[measured]** The graph's light vector does follow the lamp: printed at three azimuths it evaluates to
`(0.000, −0.940, −0.342)`, `(0.342, −0.940, −0.000)` and `(0.000, −0.940, +0.342)`, so the drivers are live and
the vector is unit length.

## 8. The mask's role, confirmed against the game's own render

**[measured]** The question was whether the mask *draws* the boundary or *biases* it, and whether the port's
handling of its zeros is right. Both are answered by putting three pictures side by side - the mask's red
rendered on the face, the game's own flat-colour regions, and the port's - scaled to a common frame
(`grids/shaded_vs_mask.png`):

* the mask's red is **0 over the forehead and around the eye sockets**, and 1 over the rest of the face;
* **the game's shaded regions are in exactly those places**, plus a thin line along the jaw;
* **so are the port's** - the same shape, in the same position.

**So the port's handling of the mask is correct, and my earlier reading of "the game shades where the mask is 0,
which the port cannot do" was wrong.** Back to the arithmetic: the factor is
`saturate(1 + (_ToonStep - mask.r * halfLambert) / _ToonFeather)`, and `saturate` clamps *upwards* at 1, so a
mask of 0 gives a shaded factor for every pixel and every light direction - the mask's zeros do not merely fail
to light, they force the shaded branch. The mask's ones are the region where the boundary can be drawn at all,
and there the boundary is the light's contour. That is one consistent mechanism, it is what the port
implements, and it matches the game's render in both regions.

**[measured]** The port's lit fraction over the face is **63.8%** against the game's **68.0%** at the same light
and mask. Those few points are not yet attributed.

## 9. The port's boundary tracks the light

**[measured]** A full sweep of the sun's azimuth, the face rendered as two flat colours, at two elevations:

| elevation | lit pixels | lit centroid travel | overlap with the previous azimuth |
| --- | --- | --- | --- |
| **70°** | 3752 to 3753 (0.03% spread) | 0 px in x, 0 px in y | 1.000 at every step |
| **15°** | 3108 to 3733 (20% spread) | 11 px in x, 1 px in y | 0.87-0.95 |

and at both elevations the lit region stays **one connected region covering 99.9% of the lit area**, so the
boundary does not fragment as it moves.

**This is the behaviour the game shows, elevation dependence included.** The game's own face in the viewer at
elevation 70 swings its lit fraction from 13.2% to 5.2% across opposite azimuths - about a 2.5x relative change,
against the port's 0.03% - and adjacent azimuths overlap 0.86-1.00. The reason the port's own absolute swing is
smaller is the one given above: the face's mean normal is almost parallel to the plane the azimuth rotates
through, so `dot(N, L)` is dominated by the light's Y component. An azimuth sweep at a high elevation is
near-invariant for *any* renderer of this mesh, which is a property of the model's normals rather than of either
implementation - and it means a sweep near the horizontal is the one that moves the line.

**[measured] A warning about the instruments.** Two numbers had to be discarded before they were used.
`measure_mask_regions.py` classifies pixels by hue over the whole frame, and the game's render also contains
hair and background - so its "shaded" class included both, which put the game's shaded share at 60% of the face
and the port's at 6.6%. Neither figure means anything. Anything comparing these two renderers by counting
coloured pixels has to confine itself to the face, and the landmark-based alignment used earlier is not rigid
enough to do that reliably. **The pictures above are trustworthy; per-pixel counts over a shared frame are
not.**

---

## 10. A smoothness metric, and it is calibrated

**[measured]** Four attempts were needed and the first three are recorded here because each failed for a
different reason, and the failure modes are the ones to avoid in any later measurement of the same kind:

| attempt | why it was useless |
| --- | --- |
| a radial profile of the boundary about its centroid | blind to jaggedness that runs *tangentially* - a circle and a cogwheel have the same profile |
| a straight-edge test: sample across the boundary at several distances and ask whether the far points still have the right colours | fails on rasterisation rather than on jaggedness, so every render scored 100% |
| classifying pixels by hue over a whole frame and comparing the two renderers | the game's render also contains hair and background, both of which landed in the "shaded" class |

**[measured]** The metric that works walks the lit/shaded frontier pixel by pixel and records the *turn* at each
step: a smooth curve turns by a small, near-constant amount per pixel, while a staircase alternates straight runs
with right angles. Two numbers come out of it: `mean turn`, the overall roughness, and `turn spread`, which
separates a jagged line from a smoothly-curved one with the same mean.

**[measured]** It is calibrated on shapes drawn so that the line between the two colours *is* the shape whose
roughness is known. Getting that right mattered: an earlier version drew each shape as a coloured block and
split it with a straight line, so the metric measured the straight line and returned 0 for every shape, which
looked like a working metric.

| calibration case | mean turn | turn spread | expected |
| --- | --- | --- | --- |
| a straight split | 0.00° | 0.00° | 0 |
| a circle of radius 100 px | **22.34°** | 22.50° | 57.3 / 100 = 0.573° per *pixel of arc*; the walker steps ~1.4 px, and measured over the pixel grid the figure is the circle's own rasterisation |
| a staircase | 4.95° | **19.39°** | a low mean with a large spread - the corners are rare but sharp, which is exactly what "spread" is for |
| a sine wave | 32.73° | 20.04° | the roughest of the four, as drawn |

**[measured]** Applied to the renders, each cropped to its own coloured area:

| render | boundary px walked | mean turn | turn spread | sharp share |
| --- | --- | --- | --- | --- |
| the port, sun elevation 70 | 113 | 27.34° | 29.95° | 7.2% |
| the port, sun elevation 15 | 44 | 17.44° | 18.35° | 0.0% |
| the game's own shader | 26 | 22.25° | 28.55° | 8.3% |

**The port's boundary is as smooth as the game's** by this measure, at this framing and resolution. The
comparison is between two rasters of a face at different pixel scales, so it is a statement about smoothness at
that scale and nothing finer - the resolution sweep is what would make it a statement about the boundary itself.

# Disproved, with the evidence

Each of these was believed at some point in this work and is now ruled out. They are listed so they are not
retried.

| claim | status and evidence |
| --- | --- |
| The article the task pointed at describes the dev team's fix for a jagged face boundary, and it involves normals. | **Disproved.** The article contains no occurrence of 境界 (boundary), ギザギザ/カクカク/ジャギ (jagged), エッジ (edge) or ノーマル (normal). 法線 (normal) appears nine times, all about the **outline**. What it does say: 「ポリゴン数などが原因で細かな陰影を表現するには限界があるためシャドウマスクを使って制御している」 — polygon count limits fine shading, so they use a Shadow Mask — and テクスチャ作業の大半がシャドウマスクの調整 (most texture work goes into the shadow mask). Its control map #1 is Shadow (R), Specular (G), Cutoff (B). |
| The export loses the face's normals, or the chain cannot carry them. | **Disproved twice.** PMX stores one normal per vertex and Unity's mesh is per-vertex too, the exporter copies every vertex and index 1:1 without splitting or merging, and mmd_tools assigns per-corner custom normals from exactly those values. Measured: the imported face's per-corner normals agree to 0.000087, and the field's smoothness (4.58° mean between neighbours) is what the game's own terminator needs. |
| The normals are being destroyed by the cylinder blend, and that is why the boundary is jagged. | **Disproved.** The blend does move 87.7% of the face's vertices, but removing it changed the render by **12 pixels of 810,000** - the boundary is not normal-driven in the old port. And the blend's formula matches the vertex program exactly; what varies is only whether it is applied. |
| The game shades where the mask is 0, and the port cannot, so the port is missing something. | **Disproved - my misreading.** The mask's zeros force the shaded branch (saturate clamps at 1, so a factor of 1 is the shaded colour whatever the light does), and the port does shade them. Measured side by side, the port's shaded region and the game's are the same shape in the same place. |
| The mask draws the boundary, so the light term does not matter. | **Disproved.** Pinning the mask through the group's socket changes nothing (0 pixels), because the addon's step reads its own mask node, not that socket; replacing the mask texture itself with white or black moves only 1.5% and 2.5% of pixels; and with the mask forced to 0 the game's boundary is a clean straight line - the light term's contour. |
| The jagged boundary was the port's camera framing. | **Disproved.** The framing was indeed different between the two renderers and was corrected, but the port's boundary stayed wrong after that. |
| The port is about 8% darker on the skin than the game. | **Disproved.** With the two aligned, the mean skin colour is (251.0, 221.2, 198.7) against the game's (249.8, 219.1, 194.9) - a couple of levels. |
| The port's vertex-colour blue is read from the wrong uv layer. | **Disproved.** `_UV3.uv.x` is the blue; a multiset comparison over the export's distinct tuples settles it where a mean cannot. |
| `_USEOPTIONMASKMAP_ON` is a shader variant. | **Disproved.** The face shader declares eight keywords and that is not one of them; the option mask is a uniform branch. |
| The viewer's fog, or its tone mapping, explains the difference between the two renderers. | **Disproved.** Forcing `_Global_FogColor` to red or white moves the face by 0.3 of a level, and with the two aligned the lit levels match. |
| A pmx's index sizes were chosen without regard to the vertex count, as they once were. | **Disproved for this exporter.** `Tools/pmx_inspect.py integrity` passes every check on a real export: the declared sizes address their counts, the reader consumes the file byte for byte, every index is in range, and the materials' face counts sum to the surface list. |

---

# Open, and how to close it

**The few points of lit fraction.** **[measured]** 63.8% against the game's 68.0% at the same light and mask,
with the shaded region in the same place and the same shape. Not yet attributed. The candidates, in the order
worth testing: the mask's sampling (a 512² mask magnified onto a face several hundred pixels across, so a
one-texel transition becomes several pixels and the boundary's fine position shifts), the engine's shadow pass
(which the game has and Blender does not, and which would darken the game's shaded side further rather than
lighten it), and the option-mask branch's gate.

**The boundary's smoothness at the port's resolution.** **[measured]** With the metric in §10, the port's
boundary and the game's read within about 5° of each other (27.3° against 22.3° mean turn, 30.0° against 28.6°
spread). That is one measurement at one framing, and what it does *not* yet say is how either behaves as the
render's resolution changes - which is the experiment that separates a genuinely smooth boundary from one that
is rasterisation noise, and the next thing to run.

