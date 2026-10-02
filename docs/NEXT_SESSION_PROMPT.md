<!-- superseded -->
> **Note.** Read with `GROUND_TRUTH.md`. Parts of this handoff predate the rollback of `Umashader.py` and the findings that the normal field is smooth and that the light term draws the boundary.

# Starting prompt for the next session

Copy the block below into a new session. It is written to stand alone.

---

## The task

Make the Blender render of an exported Umamusume character match the game's own face shading. The failure the work
started from was that the lit/shaded boundary on the face rendered as **hard-edged polygonal patches** where the game
shows a smooth result. **That is now measured as fixed** (see "Where it stands" below); what is left is the option-mask
branch and the export-side work that the region path depends on.

**Read, in this order, in `C:\Users\user\Documents\dev\Blender-Uma-Addon`:**

1. `docs/TASK_FROM_ZERO.md` — the goal, the repositories, where the shading is implemented, the ground-truth
   extraction chain with its traps, the table of what is known with how to re-check each item, the retracted
   conclusions, the eliminations with their numbers, the tool inventory and the constraints.
2. `docs/FACE_MASK_AND_VARIANTS.md` — the mask, the variant map, the two eliminations, and two retractions.
3. `docs/GAME_DATA_VS_VIEWER.md` — which values are the game's and which are the viewer's, and the line-by-line
   proof that the export is exact.
4. `docs/FACE_PIXEL_PROGRAM.md` — **which terms of the game's pixel program are live for this character**, the
   facial-shadow region, the driven `_faceShadowAlpha`, and the aligned boundary measurement at the end.

Do not re-derive any of it.

## Where things stand

| repo | branch | tip |
| --- | --- | --- |
| `C:\Users\user\Documents\dev\Blender-Uma-Addon` | `unified-morph-naming` | `310c934` |
| `C:\Users\user\Documents\dev\UmaViewer-PMX-Export-Patch` | `fix/eye-bone-export` | `338d3d3`, **not pushed** |

Both trees are clean apart from `Assets/Scenes/Version2.unity`, the TMP font asset and `meta_plain.sqlite`, which stay
uncommitted by design.

The addon's installed copy **is in sync** with the repo (verified by hash) — but re-check it after every edit, and note
that it is what Blender actually loads.

## The boundary: what is measured, and how far the measurement goes

The blocker for months was that the two renderers frame the head differently (the viewer's output is landscape, the
Blender tool's a 3:4 crop of a square render), so any comparison of the two was between different parts of the model.
The facial-shadow probe was the first thing that made them comparable at all: rendering the same frame with the region
path off and on (`-umaShotFloat _faceShadowAlpha=1` against `--face-shadow-alpha 1`) marks the face, so a landmark can
be derived from it. `_uma_scratch/skin_boundary.py`, `boundary_agreement.py`, `face_interior.py`,
`disagreement_regions.py`, `rigid_face_compare.py` are the instruments.

**The registration is the weak link.** The centroid-and-mass alignment gets the two faces close but is not rigid, and
the headline percentage moves a lot with how it is done: the same landmark, the change mask's own 2nd-98th percentile
box, or a skin-tone region give 83-95% agreement in one framing and a 30-point one-sided disagreement in another. The
viewer's facial-shadow change mask covers **1160x634 px of its 1280x720 frame** at the 2nd-98th percentile against the
port's 279x340 of 900x900, so it is a frame-wide wash in one renderer and a localised region in the other, and any
registration built on it inherits that. What is robust across every method is the lit level (**235 in both**), the mean
skin colour agreeing to a couple of levels, and the absence of a facetted boundary in the port.

Classified pixel by pixel over the ~43.5k pixels both call face, on the closest alignment: **both lit 95.1%, both
shaded 0.2%, the game shades and the port does not 2.6%, the port shades and the game does not 2.0%** — the two
one-sided disagreements the same size and opposite in sign, at every azimuth, which is the signature of the *same
shape in a slightly different place* rather than a different shape. The boundary is also a hair *shorter* and its
transition a fifth *narrower* than the game's, which is what a smoother line looks like rather than a facetted one.

**One number in this document's predecessors was wrong, and it was a wrong measurement rather than a wrong
conclusion.** `tex_chr1001_00_face_shad_c` measures a mean of **232** sRGB with a median texel of (251, 186, 172) — the
"shaded texture ~53" figure came from a region of it that includes the dark border texels — so the split between the
two textures' levels is not 144 and a "shaded fraction below the midpoint" is not a lit/shaded measure. The
lit-versus-shaded difference on this character is a lot smaller than that number implied.

**The remaining difference is a shadow, not the shading.** The viewer's light casts real shadows and Blender's does not
(`m_Shadows.m_Type: 2`, `m_Strength: 1`), so where the hair shades the face the viewer is dark and the port is lit —
and that is what the driven `_faceShadowAlpha` region exists to stand in for. The game's own renderer is therefore not
reproducible from a material shader alone, however exact the port gets.

## The next step that matters most: a rigid registration

`rigid_face_compare.py` is the attempt that showed why the easy landmark is not enough. Before any further per-pixel
claim, build an alignment that is actually rigid: two or three landmark points that are the same point on the face in
both frames (the eye centres are the natural choice - both renders have the eyes, and they are bright and
high-contrast), then solve for the translation, scale and rotation that put the viewer's on the port's, and *check* it
by overlaying the two hair silhouettes. With that the per-pixel result becomes a measurement; without it, only the
robust statements above should be quoted.

A per-pixel comparison of two renders that disagree about a *shadow* will also always show a large disagreement. That
disagreement is the shadow, not the shading, and it should be measured as such - the region's area and its edge -
rather than counted as a boundary error.

**Two older numbers were framing artefacts and are corrected.** "The head is 44.5% shaded in the viewer and 53.5% in
ours" was measured over a window containing the hair as well as the face, and the hair is not shaded by this shader at
all. "Ours is 8% darker on the skin" was a mean over differently-framed crops. Neither was real. **The note in the
previous handoff — that mean face luminance is not comparable across the two renderers because of tone mapping — is
also unnecessary once the two are aligned on the face: the lit skin level is 235 in both.**

## The option mask is implemented, and it is a second step rather than a tint

The last branch the port lacked is built. The step runs **twice**: the first pass (documented in
`FACE_PIXEL_PROGRAM.md`) gives the surface colour, and the second selects between that surface colour and the
*toon* side with the option mask's **green** channel as the selector - green 0 keeps the shaded influence,
green 1 multiplies it out. That is why forcing the mask white in the viewer *brightens* the face. Its red
gates the specular and its blue goes through a lerp towards 0.5 rather than a multiply.

Because `tex_chr1001_00_face_ctrl`'s green measures **exactly 0.0000** over every texel on this character,
the branch is an identity here, and that is verified rather than asserted: rendering the same scene with the
shader module before and after the change gives **0 of 810,000 pixels differing**, at both facial-shadow
alpha 0 and alpha 1. With the mask replaced by solid white the face's mean luminance goes **+10.3 levels**
and shifts towards the toon texture's colour (R +5.3, G +41.6, B +28.1), and the game's own shader answers
the same question with **+9.0 levels** and the same sign on every channel. Renders and the montage:
`grids/option_branch/`, figures in `docs/images/`.

## The exporter is audited, and the defect class the objective names is not present

`Tools/pmx_inspect.py integrity <file>` checks three things and was written for exactly the failure this
project's history contains - an index size chosen without regard to the count it has to hold:
1. the reader consumes the file **byte for byte** with the sizes the header declares (a size that disagrees
   with what was written shows up as a cursor that stops early or runs long);
2. each declared size can address its count (a 2 byte index reaches 65535 entries, a 1 byte index 127);
3. every index read lies inside the range it addresses - surface indices, material face counts summing to
   the surface list, texture and sphere and toon indices, bone parents and tails and ik links, and the bone
   indices each vertex is skinned to.

On `final\1001_00.pmx` it passes every check: 13968 vertices with a **2 byte** vertex index, 79944 surface
indices all inside them, the materials' face counts summing exactly, and 23627 bone references all in range.
The sizes are computed from the counts (`ModelExporter.cs:67-83`: `> 65535 ? 4 : (> 255 ? 2 : 1)` for the
vertex, bone, morph and rigid-body indices, `> 255 ? 2 : 1` for the texture and material ones) and are
written into the header, and `PMXWriter` uses the header's values for every index it writes - so the size and
the data cannot disagree by construction. The hard-coded 2s at `PMXWriter.cs:30-35` belong to an unused
overload.

**The audit's sensitivity is demonstrated, not assumed.** `_uma_scratch/corrupt_pmx_tour.py` injects three
defects into copies of the file - a vertex count raised past what a 2 byte index can address, one surface
index pushed past the last vertex, and a material's face count made not to sum - and the audit catches all
three (the first structurally, the other two by name), while accepting the healthy file.

**Face normals.** They are written exactly as the mesh holds them: `MMDReaderWriteUtil.WriteVector3(writer,
normal, false)` with the amplifier off, so the pmx's normals are unit length (measured: min 1.000000, mean
1.000000, max 1.000000), and the line-by-line comparison against the exporter's own vertex record agrees to
float32 over all 1328 face vertices. `_NormalizeNormal` is 0 on this material, which changes nothing when
there is no raw magnitude for the shader to use.

**And the values now come from the game's stored material, not the viewer's overrides.** This was the last
outstanding piece of the objective's item (c). The question was `_CylinderBlend`, where the shipped asset
says **0** and the viewer forced **0.25**. The game's own runtime code settles it: `Gallop/Live/Director.cs`
sets the rim, toon and saturation values through a property block (lines 215-258) and **never sets
`_CylinderBlend`**, so the asset's 0 is what the game renders with. The viewer's override is removed, a fresh
export now carries `cylinder_blend=0` - and it is the only value in the comment that ever differed from the
asset, apart from the rim colour the viewer zeroes (the face program has no rim term, so nothing reads it).

The cost of the correction was measured before it was made: rendering the face with the blend and without it,
at three sun azimuths, changes the face crop's mean luminance by **0.04 of a level** and moves 0.2-1.2% of its
pixels by more than 16 levels. Re-running the boundary agreement on the re-exported file gives the same
figures as before (83% agreement, 9.7% against 7.5% one-sided at azimuth +90), so nothing that was working
moved.

**The chain verifier now reports 17 of 18 values identical to the stored asset** with 1 intentional
divergence, and it is one command: `_uma_scratch/verify_chain.py [--pmx PATH] [--no-blender]`.

**The face measurement on the re-exported file**, at three sun azimuths, with the face found by skin tone in
each render (so a misregistration cannot flatter it):

| | mean luminance | shaded | transition band | boundary length |
| --- | --- | --- | --- | --- |
| the game's shader | 222.1-225.4 | 4.0-4.1% | 0.96-1.08% | 0.0458-0.0464 per face pixel |
| the port | 223.8-226.1 | 3.5-3.6% | 0.88-1.03% | 0.0353-0.0363 per face pixel |

The port's boundary is **shorter** than the game's by about a quarter and its transition band the same width,
which is what a smooth line through the same area looks like rather than a facetted one - and it is the
measurement that answers the complaint this task started from.

## What the last sessions settled, so it is not redone

- **The dark area the port lacks is Unity's engine shadow, not the game's material program.** The viewer's scene
  light has `m_Shadows.m_Type: 2` (soft) with `m_Strength: 1` (`Assets/Scenes/Version2.unity`, light component
  926505644), and Blender has nothing under the material shader to shadow anything, so where the hair shades the
  face the viewer is dark and the port is lit. This is the shape of the remaining difference and it is *not* a port
  defect: the material program is matched. It is also what the driven `_faceShadowAlpha` region is for - the game's
  artistic substitute for that shading, driven by the expression, 0 on the player character.
- **The face's shaded colour is bright, and an earlier number in this project was wrong.** `tex_chr1001_00_face_shad_c`
  measures a mean of 232 sRGB with a median texel of (251, 186, 172); the "shaded texture ~53" figure came from a
  region that includes its dark border texels. A light-versus-shade difference of ~120 levels on the cheek is
  therefore far larger than the material program's textured step can produce on this character, which is the
  independent argument that the darkening comes from the engine's shadow pass.
- **The masks are read as data in both renderers** - the port loads `*_base` and `*_ctrl` as Non-Color and the
  program samples them without an sRGB conversion - so the values the two sides multiply by are the same numbers.
- **The `-DumpFace` timeout is fixed.** The dump block ended with `if (!options.DumpMaterials) return;`, which returned
  out of `Tick()` before the export and left the stage machine spinning until the 600 s deadline.
- **The export loses nothing.** `ModelExporter.VertexDumpPath` (`-DumpFace`, written as `<export>.vertices.txt`) records
  every vertex as it is written, so the pmx can be compared line by line: position, normal, uv and vertex colour all
  agree to float32 over the face's 1328 vertices. The "22.86% of normals already differ" claim is **retracted**.
- **A fresh export carries the whole chain**, verified end to end on `final\1001_00.pmx`: the material comment holds
  `toon_step=0.4 toon_feather=0.001 cylinder_blend=0 normalize_normal=0 face_shadow_end_y=0 face_shadow_length=0.1
  face_shadow_alpha=0 face_shadow_color=0.6,0.6,1,1 cheek_threshold=0.775 nose_threshold=0.775`, the `_UV2` layer
  exists on import with `_UV2.uv.x` 0.0455 over the face, the mask reads `_TripleMaskMap.r`, and `apply_shader`
  returns `FINISHED`.
- **`_USEOPTIONMASKMAP_ON` is not a shader variant.** The face shader declares eight keywords and that is not one of
  them; the option mask is a *uniform* branch. The game runs the plain 7-texture variant, not `USE_MASK_COLOR`.
- **No container in the game's shader bundle carries an RDEF chunk** (checked all 28 - `Tools/game_rip/
  reflection_search.py`), so no compiled program names its own constants. Every cb register identification in this
  project rests on matching the material's *values* to the slots, not on reflection.
- **`_CylinderBlend` comes from the game's asset, and the export no longer carries a viewer override.** The asset
  stores **0** on the face; the viewer used to force **0.25** as it loaded the face, hair and eye keywords
  (`UmaContainerCharacter.cs`), and because the exporter reads the live material that override ended up in the
  comment. The game's own runtime code never sets this property (`Gallop/Live/Director.cs:215-258` sets the rim, toon
  and saturation values and not this one), so the asset's value is the one the game renders with, the override is
  removed, and the export's comment now says 0. Measured before making the change: the blend moves the face crop's
  mean luminance by 0.04 of a level, and the boundary agreement is unchanged after it.
- **`_faceShadowAlpha`'s driver is not in the export at all.** `Gallop/FaceDrivenKeyTarget.cs` binds the property, with
  no curve, to the **X scale of a locator named `Shade_Ctrl`** (`BindProperty.LocatorPart.ScaX`). That name appears
  nowhere in the export - not among its 192 morphs and not among its 437 bones. So the driven variation cannot be
  reproduced from a static pmx; what the export carries is the rest value (0), and what the port offers is the same
  value as a scriptable override. `Head_shade_start` is a different locator and is not this driver.
- **The region path is implemented and matches the game's own render** with the alpha forced on: ours R −48.2, G −42.6,
  **B 0.0** against the viewer's R −45.4, G −38.3, **B −0.1**. Blue unmoved is the signature of the `_faceShadowColor`
  multiply. The port's previous region was a colour switch that was the identity almost everywhere and is removed.
- **The port's step is verified against the game's own shader** by perturbing textures in the viewer: mask
  `_TripleMaskMap.r`, lit colour `_MainTex`. With the light matched, the aligned windows give a **lit skin level of
  luminance 235 in both renderers**, a face mean luminance of 222.1 against 224.2, and a mean skin colour of
  (249.8, 219.1, 194.9) against (251.0, 221.2, 198.7) - a difference of a couple of levels.
- **The cylinder blend's weight source was never wrong.** `_UV3.uv.x` **is** the vertex colour's blue (the exporter
  packs `(r,g,b,a)` into the third extra uv; mmd_tools maps `_UV{n}` to that slot's `zw`). The change to read the
  `UV3` layer was reverted; a multiset comparison settles it where a mean cannot.
- **The viewer's model animates; `-PinPose 0` is required for any A/B** (it freezes 14 animators and cuts the pose
  difference 3.4×). Spring bones and driven keys may still move.
- **The viewer's light is white, intensity 1.0, and its direction is now matched**: `blender = (ux, -uz, uy)` from the
  vector the viewer logs, passed as `--light-direction=X,Y,Z` (a leading minus needs the `=` form or argparse eats the
  sign). Its light sits at elevation 70.

## The reference that settles arguments

**UmaViewer runs the vendor's compiled shader** — `UmaViewerMain.cs:223` loads the game's own `shader` bundle and the
live face material carries `Gallop/3D/Chara/ToonFace/TSER`. (`docs/VIEWER_SHADING.md` describes a Nars replacement
shader that the face does *not* use; that document is wrong about the face.)

```powershell
cd C:\Users\user\Documents\dev\UmaViewer-PMX-Export-Patch
pwsh -NoProfile -File Tools\headless_export.ps1 -Char 1001 -Costume 00 -ShotView face -ShotYaws 0 `
  -ShotAzimuths "0,90,-90" -PinPose 0 -Screenshot "...\grids\viewer\g.png" -ShotOnly
```

`-umaShotTexture`, `-umaShotGlobal`, `-umaShotFloat`, `-umaShotMaterial` force values on the live game material, which
is how the shader's branches are identified rather than guessed. **Print the light before comparing anything.**

## Eliminated already, do not repeat

- the **camera framing** (it was the source of the two artefact numbers above; aligned, the two agree)
- the **elevation** (matched at 70), the **cylinder blend** (off: unchanged), **`_NormalizeNormal`** (the mesh's
  normals are all exactly unit length, so it has nothing to act on)
- the **step's constants and texture files** (they match the game exactly), and the **mask source** (verified)
- **the export**, line by line to float32 — the geometry is not where anything is lost
- the **shipped node group's** metallic/highlight/rim/ambient/emission (zeroing all five moves the face crop by nothing)
- the **textures are not V-flipped** (flipping them all scrambles the face)
- the **viewer's fog** (forcing `_Global_FogColor` to red or white moves the face 0.3 of a level)

## The next steps, in order

1. **A rigid registration**, as above. It is the one measurement still standing between this work and a
   per-pixel verdict on the face, and it is what the remaining difference should be attributed with (the
   engine shadow, the pose, or the shader).
2. **The two option-mask channels that gate the specular and the rim** - the branch's red and blue. What is
   built is the green, which is the selector that changes the *surface* colour and therefore the face's
   shading. The red gates the specular and the blue gates the rim, and both are terms the port's face path
   does not compute at all: they live in the shipped node group's specular and rim sections, whose nodes are
   generically named, so wiring them is a graph-reading job before it is a coding one. Neither can change
   character 1001 (red and green are 0 there, and the face's rim colour is 0), so this is the difference
   between a port and a port-for-one-character *for other characters*, not a defect in what this one renders.
   The blue is also the one uniform the disassembly shows being *lerped* rather than multiplied - towards 0.5
   by `_UseOptionMaskMap` - and nothing in the port consumes the option mask's blue, so there is no wiring
   for it yet. `_USEOPTIONMASKMAP_ON` is not one of the shader's keywords, so there is no variant that turns
   the multiply into a bypass; the uniform is the only switch.
3. **Nothing else in the face program is unaccounted for.** The remaining terms are scene state: two light-direction
   constants (`cb0[64..66]`, which read 0,0,0 in the viewer so the second light contributes nothing via a *max*, not a
   multiply), the lightmap (`cb0[139]`, 0,0,0), and the final saturation/contrast pair. If a future comparison shows a
   systematic difference the aligned instruments cannot explain, that is where to look, and the way to look is
   `-umaShotGlobal`.

## Method constraints that cost the most time here

- **Sync the installed extension and check it by hash.** A stale installed copy produced invalid test results twice.
- **Render the value, do not reason about it.** Every wrong conclusion here came from a summary, a stored asset, or a
  render whose inputs were not printed; every correction came from looking at the thing itself.
- **Align before comparing.** Two crops of two differently-shaped frames are not the same region, and every summary
  statistic taken over them — mean, exposure, coverage, centroid — is partly a measure of the framing. This produced
  two false conclusions that stood for weeks.
- **A two-sided disagreement of equal size is a shape match with an offset; a one-sided one is a shape difference.**
  That is worth checking before concluding anything from a disagreement percentage.
- **Vision only where a number cannot decide.** Facetting is a claim about a *pattern*; position, length and width do
  not distinguish a smooth curve from a chain of straight segments of the same length. Everything else here is a
  number.
- **Never edit a `.cs` file while a Unity batch run is active**, and preserve the UTF-8 BOM (`utf-8-sig`).
- A headless Unity run needs the Editor closed and takes about three minutes.
- Long PowerShell-plus-Python one-liners break on quoting constantly: write script files. PowerShell has no heredoc,
  and `pwsh -File` does not deliver a `[string[]]` parameter intact (pass one `;`-separated string).
- **No numpy in this Python.** The 3.14 install has Pillow only; write the arithmetic out or use `get_flattened_data()`
  rather than `getdata()`.

## Carrying the scratch work forward

`_uma_scratch/` holds the scripts behind every number in the docs. The boundary instruments
(`skin_boundary.py`, `boundary_agreement.py`, `face_interior.py`, `disagreement_regions.py`, `skin_colour.py`,
`zoom_face_pair.py`) and the chain verifier (`verify_chain.py`) are the ones worth keeping; the reusable rip tooling
(`variant_map.py`, `reflection_search.py`) lives in the viewer repo under `Tools/game_rip/`, which is where the game
extraction tools belong.
