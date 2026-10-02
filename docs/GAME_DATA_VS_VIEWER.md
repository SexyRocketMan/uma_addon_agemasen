<!-- superseded -->
> **Note.** Read with `GROUND_TRUTH.md`. The value table here stands and is what the exporter was corrected against. `_CylinderBlend` is no longer a viewer override - the game never sets it at runtime, so the export now carries the asset's value.

# The game's data against the viewer's: what each one actually says

The project has been treating UmaViewer's render as ground truth because it runs the game's compiled
shader. That part is true - but the viewer also **overrides** the material and supplies its own light
and post-processing, so its render is the game's shader with the viewer's inputs. This records which
values are the game's, which are the viewer's, what the exporter currently ships, and what that means
for the port.

Everything here was measured this session. The tooling that produced it is named at the end.

## 1. The viewer does run the game's own face shader

`UmaAssetManager.cs:149-158`, inside `if (bundle.name == "shader.a")`:

```csharp
FaceShader = (Shader)bundle.LoadAsset("assets/_gallop/resources/shader/3d/character/charactertoonfacetser.shader");
```

That is a game bundle and a game asset path, and `UmaContainerCharacter.cs:854-858` replaces the face
material's shader with it:

```csharp
case "Gallop/3D/Chara/ToonFace/TSER":
    m.shader = UmaAssetManager.FaceShader;
    m.SetFloat("_CylinderBlend", 0.25f);
    m.SetColor("_RimColor", new Color(0, 0, 0, 0));
    break;
```

So `docs/VIEWER_SHADING.md`, which describes the face as rendering with the Nars replacement shader
`Nars/UmaMusume/Face` and concludes the mask is `_TripleMaskMap.g` from that shader's source, describes
a shader the face does not use. The face runs the game's own program - the one this project
disassembled - and the live material's shader name (`Gallop/3D/Chara/ToonFace/TSER`) confirms it.

## 2. But it overrides three things, and one of them the exporter ships

The game's material asset (from the database, not the viewer's live material) against the viewer's
runtime material:

| property | game's material | viewer's runtime | notes |
| --- | --- | --- | --- |
| `_CylinderBlend` | **0.0** | **0.25** | the viewer sets it on face, eye and hair |
| `_RimColor` | rgb not zero | **(0,0,0,0)** | the viewer zeroes it |
| `_StencilMask` | 100.0 | 1001.0 | viewer sets it to the character id |
| `_CutoffType` | 1.0 | 3.0 | viewer |
| `_ToonStep` | 0.4 | 0.4 | agree |
| `_ToonFeather` | 0.001 | 0.001 | agree |
| `_UseOptionMaskMap` | 1.0 | 1.0 | agree |
| `_NormalizeNormal` | 0.0 | 0.0 | agree |
| `_faceShadowAlpha` | 0.0 | 0.0 | agree, and it is *driven* per expression |
| `_CheekPretenseThreshold`, `_NosePretenseThreshold` | 0.775 | 0.775 | agree |
| `_ToonBrightColor`, `_ToonDarkColor` | (1,1,1,0) | (1,1,1,0) | alpha 0 makes both tint blocks identity |
| all twelve `_MaskColor*` / `_MaskToonColor*` | (1,1,1,1) | (1,1,1,1) | agree, so the mask-colour path is a no-op |

**The exporter reads the live material**, so the pmx carries the viewer's `cylinder_blend=0.25` where
the game's material says 0. That is a divergence from the game's data on the shaded side of the
cylinder blend, and the number of characters it affects is every exported face, eye and hair material.
It is not the artifact - the earlier measurement that switching the blend off entirely changes the
render by ~0 still stands - but it is the wrong number to be carrying, and it is the clearest example
of the viewer's values leaking into the export.

Whether the *game* also sets 0.25 at runtime cannot be settled from the material asset alone: that
would be in the game's character-loading code, and the decompiled sources that ship with these
projects are only 75 files covering facial driven keys, spring bones and the live director - not the
rendering setup. The material asset is the only game-side statement available, and it says 0.

## 3. Checked and cleared: the index sizes

The class of bug that previously broke exported geometry is present in the tree but not in the path
that runs. `PMXWriter.cs:30-35` hard-codes

```csharp
VertexIndexSize = 2, TextureIndexSize = 1, MaterialIndexSize = 1,
BoneIndexSize = 2, MorphIndexSize = 2, RigidBodyIndexSize = 2
```

but that is the default of the three-argument `Write` overload. `ModelExporter.cs:67-83` builds its own
`PmxConfig` from the counts and passes it:

```csharp
VertexIndexSize = model.Vertices.Length > 65535 ? 4 : (model.Vertices.Length > 255 ? 2 : 1),
TextureIndexSize = model.TextureList.Count > 255 ? 2 : 1,
MaterialIndexSize = model.Parts.Length > 255 ? 2 : 1,
BoneIndexSize = model.Bones.Length > 65535 ? 4 : (model.Bones.Length > 255 ? 2 : 1),
MorphIndexSize = model.Morphs.Length > 65535 ? 4 : (model.Morphs.Length > 255 ? 2 : 1),
RigidBodyIndexSize = model.Rigidbodies.Length > 65535 ? 4 : (model.Rigidbodies.Length > 255 ? 2 : 1)
```

and the exported file's header agrees for this model: 13968 vertices, 22 textures, 8 materials, 437
bones, 192 morphs - every one of them inside the size it was written with. So the sizing is correct
here; the hard-coded copy is a latent hazard for the other overload, not a live cause.

## 4. The exporter's normals, where they come from

`ModelExporter.cs:894-918`, the skinned path:

```csharp
var vertices = mesh.vertices;      // the shared mesh
var normals  = mesh.normals;       // the shared mesh's normals
smr.BakeMesh(bakemesh, true);      // positions come from the bake
...
vertex.Coordinate = root.InverseTransformPoint(smr.transform.TransformPoint(bakemesh.vertices[i]));
vertex.Normal = normals[i];        // ...but the normal is taken verbatim, untransformed
```

Two things follow, and both are worth testing rather than assuming:

- the normal is written **without** the transform that the position gets, so if `smr.transform` is not
  identity relative to `root` the exported normal field is in a different frame from the geometry;
- if the mesh is not readable, `MakeReadableMeshCopy` is used and **it calls
  `meshCopy.RecalculateNormals()`** (line 1023), which would replace the game's custom normals with
  geometric ones. The measured PMX normals sit a mean of 44.39° away from geometric normals, so this
  did *not* happen for this model - but it is one flag away from happening for any model whose meshes
  are not readable.

The instrument for this exists and was used: the exporter records every vertex as it writes it, and
section 8 reports the result - the field is exact, so there is nothing to localise. What the record does
show is that `BakeMesh` is not normal-preserving (median 1.80 degrees against the shared mesh's), which
is why taking the baked mesh's normals would be a mistake rather than a fix.

## 5. Refuted: the shipped node group is not the reason the face is bright

The port implements the lit/shaded step and hands the result to the shipped "Uma Shader" group, which
then adds its own metallic, highlight, rim, ambient and emission terms. Zeroing those five group inputs
on the face materials moved the face crop's mean by **nothing at all** (200 -> 200 at azimuth +90, 199
-> 200 at -90). So whatever the group contributes, it is not what makes our face measure 199 where the
viewer measures 147.

`Tools/blender_render_angles.py --isolate-game-step` is that experiment, kept as a switch.

## 6. That 147-against-199 comparison was framing, and it is now closed

Every "face mean luminance" in this project - the docs' table and this session's reruns alike - comes
from a fixed crop of the frame, `(0.28, 0.08)-(0.72, 0.60)`. That crop is not the face, and the two
renderers framed very differently: the viewer shoots the face at `height * 0.15` and
`blender_render_angles.py` was using `height * 0.30`, at 1280x720 against 900x900.

**Measured.** The face view was rendered at four camera distances and tiled into one sheet against the
viewer's own shot (`grids/framing/framing_sheet.png`, five rows, three sun azimuths). At 0.30 our render
filled the frame with a face where the viewer shows a whole head with shoulders; **0.60 is the factor that
frames the same way.** With the framing matched, the same crop measures:

| render | face crop mean | fraction of the crop on a steep local gradient |
| --- | --- | --- |
| viewer | 150 | 3.88 |
| ours at 0.30 (what the tool used) | 201 | 1.99 |
| ours at 0.60 (matched) | **140** | 4.22 |
| ours at 1.00 (too far) | 125 | 2.55 |

From **51 levels apart to 10**, and the structural measure from 1.99 against 3.88 to 4.22 against 3.88.
So the largest unexplained difference in the whole exercise - the thing both handoff documents single out
and neither had checked - was the camera distance. It measured the crop's background-to-subject mix, not
the shading, which is the same lesson as the retracted 22.86%: with the wrong instrument, a difference
appears that is not there. The residual 10 levels is comfortably within what two different tone curves
can account for, so it is a renderer difference until something shows otherwise.

`VIEW_DISTANCE["face"]` is now **0.40**, measured rather than read. The instrument that settled it came later
and from the facial-shadow work: with the region implemented, the change between an alpha-off and an
alpha-on render is a clean mask of the face in both renderers, so its area as a fraction of the frame is a
scale measure needing no background or skin test:

| | change-map area, as a fraction of the frame |
| --- | --- |
| ours at 0.30 (the original value) | tighter than the viewer's |
| ours at 0.60 | 3.36% |
| **ours at 0.40** | **8.27%** |
| viewer | 9.78% |

0.40 is within 15% of the viewer; 0.60 is three times out. The table above was measured at 0.60, so its
"ours" column is at a wider framing than the tool now uses - the direction and size of the confound are what
it establishes, not the digits. **The other three views (`head`, `upper`, `full`) have not been checked
individually**; they carry the same calibration constant, 0.40 / 0.15 = 2.67, applied to the viewer's own
nominal factors.

## 7. The obstacle to finishing the port, and the way around it

To reproduce the whole pixel program rather than the step, each `cb1[i]` in the disassembly has to be
tied to a material property. The shipped containers have **no RDEF chunk**, so the bytecode has no
names, and the per-container records in the blob region list the constant buffers' *names* but not
their variables in layout order (the variable lists they do carry are sorted, and they name only the
mask colours). The shader's own `m_PropInfo` gives declaration order, but Unity's packing is not
declaration order: `_faceShadowAlpha`, `_faceShadowEndY` and `_faceShadowLength` are declared
consecutively and do land consecutively in `cb1[35]` as `.x`, `.y`, `.z`, while `_ToonStep` and
`_ToonFeather` are also declared consecutively but land at `cb1[3].w` and `cb1[4].x`. So the packing
has to be *solved*, not read, and the decompiled sources that would give it are not in these trees.

The way around it, which is what the port should use: **the values are known even where the names are
not.** Every material property's value is in the stored material, and a slot's role is pinned by how
the program uses it - `cb1[35].{x,y,z}` can only be the three `_faceShadow*` scalars in that order,
`cb1[3].w` and `cb1[4].x` are the step and the feather, `cb1[19].z` is compared against the integers
1..5 so it is an enum whose value is 0, `cb1[6]`/`cb1[7]` are the only `(1,1,1,0)` colours, and
`cb1[16].y` is a saturation that must be 1 for the final blend to be identity. Where two candidate
properties have the same value the ambiguity does not affect the render at all. So the program can be
implemented exactly for this material from values alone, and generalised later by carrying the
properties into the pmx comment by name - which is also what makes it work for a character whose
material differs.

## 8. The export path checked vertex by vertex: the colours are right, and a mean cannot show it

`-DumpFace` now also writes the arrays the exporter reads for each face renderer - positions in root
space, `sharedMesh.normals`, `BakeMesh`'s own normals, the colours, the uv sets, `isReadable` and both
matrices - beside the export as `<export>.mesh.txt`, so the pmx can be compared against the game's own
data instead of through a histogram.

**The vertex colour's packing is correct.** `ModelExporter.cs:917` packs it into the pmx's third extra
uv as `(r, g, b, a)`; comparing the two sides as a multiset, which needs no alignment, **119 of the 120
distinct tuples agree under that order**, against 20 for blue-first and 5 for reversed. So
`_UV3.uv.x` is `additional_uv[2].z`, the blue, and the addon's original reading of it was right. An
earlier claim in `FACE_MASK_AND_VARIANTS.md` that it read the wrong component came from comparing
*means* over two different vertex sets - `_UV3.uv.x` 0.2044 against Unity's blue mean 0.1710, with
`UV3.uv.x` at 0.1702 looking like the match - and it is retracted there. Colour *r*'s mean (0.1700)
just happens to sit within 0.001 of colour *b*'s (0.1710) on this model.

**`BakeMesh` does perturb the normals.** `sharedMesh.normals` against `BakeMesh`'s own normals, index
aligned, 1330 vertices: median **1.80°**, mean 1.78°, only 3.3% within 1°. The exporter writes the
*shared mesh's* normals, which is the right array - but this is the mechanism by which any path that
took the baked mesh's normals would silently perturb the field, and it is worth knowing that the bake
is not normal-preserving here.

**Still open on this front:** aligning the pmx against the dump by position did not work - the two are
not in the same space, so the pmx coordinates are not simply
`root.InverseTransformPoint(renderer.transform.TransformPoint(bakedVertex))`, and something between that
expression and the file applies a conversion. Finding it is the next step, and it is the one that would
let the pmx's normals and the game's be compared exactly rather than distributionally.

**Resolved: the export loses nothing.** The instrument was moved inside the exporter - `ModelExporter`
records every vertex as it appends it, and each renderer's block carries the pmx index its first vertex
landed at, so pmx vertex *k* maps to a known record line by arithmetic: no geometry matching, and no
dependence on when the mesh was read. It lives behind `ModelExporter.VertexDumpPath`, set from
`-DumpFace`, written as `<export>.vertices.txt`. The record covers all 13968 vertices in 5 renderer
blocks - M_Body at 0, M_Face at 9598, M_Hair at 10928, M_Mayu at 13625, M_Tail at 13687 - and comparing
the 1328 face vertices against it gives:

| quantity | worst difference between the pmx and what the exporter recorded writing |
| --- | --- |
| position, through the writer's convention | **0.000002** |
| normal, through the writer's convention | **0.000000** (100% within 0.5 degrees, max 0.000002) |
| uv | **0.000000** |
| vertex colour (the third extra uv) | **0.000000** |

The convention is `MMDReaderWriteUtil.WriteAmpVector3`, which negates x and z for **every** vector it
writes, and divides by the amplifier only when asked:

```csharp
writer.Write(-vector.x / amp);
writer.Write(vector.y / amp);
writer.Write(-vector.z / amp);
```

`WriteVertexBasic` writes the coordinate with the amplifier on - so the pmx holds `(-x, y, -z) / 0.08`,
which is where the factor of 12.5 comes from - and the normal with it **off**
(`WriteVector3(writer, normal, false)`), which is why the pmx's normals measure exactly 1.000000 in
length. Applied as written, both sides agree to float32 precision: the normal field in the pmx *is* the
game's normal field, rotated 180 degrees about y along with the rest of the model. So the face's
geometry, normals, uvs and vertex colours all survive the export exactly, and **the export is
eliminated** as the source of the lit/shaded boundary. (Unexamined on this front: the bones and
skinning, and the morphs.)

**Which retracts the 22.86%.** `FACE_MASK_AND_VARIANTS.md` says the face's normal field is already
22.86% different from Unity's *inside the pmx*, and `NEXT_SESSION_PROMPT.md` and `TASK_FROM_ZERO.md`
both build on it. It is wrong. That number comes from the angle-to-the-mean histogram, which is
hypersensitive - the bins are narrow, one bin holds 322 of 1330, and the mean is computed from the same
data - so the 1.43 degree difference in mean direction alone moves hundreds of vertices across bin
edges. Measured against the exporter's own record, the field is identical. The lesson is the one this
project keeps relearning: an instrument that compares two *summaries* of two data sets can invent a
difference that a line-by-line comparison does not find, and a histogram of angles to a data-dependent
mean is one of the worst of them.

## 9. Reproducing this session's measurements

```powershell
# the stored face material, from the game's bundle (not the viewer's live material)
python _uma_scratch/stored_material.py

# the variant map, the keyword sets and the texture-register order
python Tools/game_rip/variant_map.py <shader_bundle.unity3d> "Gallop/3D/Chara/ToonFace/TSER" <scratch>

# what the per-container records say, whole rather than windowed
python _uma_scratch/record_layout.py <region.bin> <shader json>

# the pmx's own uv means, for comparing against what Blender imported
python _uma_scratch/pmx_uv_convention.py <model.pmx>

# the group-isolation experiment
blender --background --factory-startup --python Tools/blender_render_angles.py -- `
    --pmx <model.pmx> --out-dir <dir> --view face --apply-shader --new-face --isolate-game-step `
    --grid "0,+90,-90" --light-elevation 70 --yaws 0
```
