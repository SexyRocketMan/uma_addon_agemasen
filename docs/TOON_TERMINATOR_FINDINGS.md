# Why a `dot(N, L)` terminator on a low-poly face is a polyline, and what the industry does instead

This is a summary of research that is written up in full, with sources, in the viewer repository at
`docs/TOON_TERMINATOR_RESEARCH.md`. What follows is the part that bears on this port, plus the two claims from
that research that I checked myself and one that I had to correct.

---

## 1. The reason a hard threshold produces a polyline — and it is not aliasing

**[established elsewhere, and it follows from two cited facts]** A vertex normal is interpolated *linearly*
across each triangle, so `dot(N, L)` is an **affine function of position within a triangle**. The set
`{dot(N, L) = t}` for an affine function on a triangle is a **straight line segment**. Therefore any hard
threshold on `dot(N, L)` produces, by construction, one straight segment per triangle, with a **corner exactly
at every mesh edge it crosses**.

That is the jaggedness. It is not a sampling error, not an anti-aliasing failure, and not a bug in the port. It
is what the arithmetic produces, and no amount of MSAA, supersampling or feathering removes it — feathering
widens the band but leaves the corners in place.

**[inference]** This settles the argument the work has been going around in circles on. The port's step is
faithful to the game's program; the game's line is nonetheless clean, so the game cannot be getting its line
from this threshold either. Something has to stop one of the three inputs being a function of the mesh: the
normal field, the threshold's *position*, or the threshold's *sharpness*.

## 2. What the game itself does, as far as it can be read

**[measured]** The game's pixel program multiplies the mask's **red** channel into `halfLambert` and compares it
against a constant `_ToonStep`, with `_ToonFeather` 0.001 — so on the game's side too, this term is a hard step
on an interpolated field.

**[established elsewhere]** Two other implementations of a Uma face terminator are vendored in the viewer
repository, and neither lets `dot(N, L)` place the line:

* `Assets/Resources/NarsShader/Shaders/UmaMusume Face.shader` (a decompiled Amplify graph, referenced by no
  material in the repository): `smoothstep(0, mask.G*(1−NDL), NDL*_ShadowStrength*mask.G)`, then
  `saturate((s − 0.26) / fwidth(s))`, then the lerp. The `fwidth` is Amplify's step antialiasing — a
  screen-space derivative.
* `Assets/Resources/Materials/UmaShaderFace.shader` (a hand-written reimplementation):
  `shadowLerp = saturate(1 + ((base.r * 2 * halfLambert − 0.3) * 50))` — the base map's red channel as a
  per-texel bias, then a hard step.

**[measured, correcting the research report]** The report claims the mask algebraically cancels out of the Nars
smoothstep, leaving it active only in a degenerate case. It does not cancel: evaluated over a range of mask
values the result differs substantially (`NDL` = 0.9 gives 0.2258 at `mask.G` = 1.0 and 0.7235 at 0.5). What is
true is subtler and matters more: **the threshold falls at `NDL = 0` for every value of the mask**. The mask
changes the curve's shape and where it saturates, but not where the line is. So even in that shader the mask is
not placing the terminator; `dot(N, L)` is.

## 3. `fwidth` cannot be reproduced in Blender

**[established elsewhere]** Blender exposes no screen-space derivatives to shader nodes — the Utilities and
Shader node indices contain no derivative node, and the only route is an OSL Script node, which is Cycles-only.
So the one mechanism in the vendored shaders that *does* antialias the step is unavailable.

**[inference]** This has a sharp consequence for scope: **a faithful port of the game's shader will produce a
hard-stepped, polygonal line in Blender and no amount of fidelity in the port will change that.** If the goal is
a clean line in Blender, the fix is one of:

* change the **field** the threshold is evaluated on — a smoothed or redirected normal field, or a proxy mesh's
  normals transferred in;
* move the **position** of the line out of `dot(N, L)` and into a texture;
* replace the **sharpness**: substitute a fixed-width ramp for `fwidth`, which will not look identical to the
  game but will not be jagged.

## 4. What the industry actually does, and what it says about the normals route

**[established elsewhere]** The clearest account is the GDC 2024 talk on Hi-Fi RUSH's 3D toon rendering, which
treats the face as an exception: every other part is shaded by thresholding `dot(N, L)`, and the face is not.
They tried the normals route first and abandoned it, listing three failures — smooth curves are hard to
generate, **facial animation destroys the shadow silhouette**, and artists cannot fix it in the model settings.
Their replacement stores a *light-angle threshold* per texel, sampled by the angle between the light and the
face's forward direction in the **head bone's** coordinate system, explicitly *not* a dot product.

**[established elsewhere]** The same shape of solution appears independently in Genshin-derived shaders,
lilToon, and Project Sekai implementations; Unity's Toon Shader ships a related "Position Map" whose documented
property is that maps "allow you to set shadows of any shape and in any place you like, **regardless of geometry
or vectors**".

**[established elsewhere, and worth flagging]** Uma Musume itself is **not** an SDF face-shadow system. Neither
vendored shader is one, and nothing in the game's own program is. The relevant Uma technique is
flattened/cylinder-blended normals plus a painted per-texel detail mask for the cheeks and nose. So an SDF port
would be a reasonable engineering choice for producing a clean line in Blender, but it would **diverge from the
game**, not reproduce it — and that trade-off is the user's to make, not the port's to assume.

## 5. Telling geometry from aliasing — the sharp test

**[established elsewhere]** MSAA adds *coverage* samples but not *colour* samples, so a boundary computed in the
shader is **not** antialiased by it, while supersampling and TAA are. That gives a decisive test:

| observation | conclusion |
| --- | --- |
| raising MSAA changes nothing, SSAA/TAA smooths the staircase | the boundary is shader-computed — geometry, not coverage |
| supersampling at 400% leaves the same polyline in the same object-space place | geometry, confirmed |
| the steps are one pixel and do not line up with triangle edges | aliasing |

**[inference]** Both rows of the first case describe this port, and both are consistent with §1.

## 6. The techniques Blender itself documents

**[established elsewhere]** The official Normal Edit modifier manual names the use case directly: it can "fix
shading of toon-like rendering by partially bending default normals". The related toolbox — `Mesh → Normals →
Point to Target` (Align / Rotate / Spherize / Average / Smooth Vectors), the Normal Edit and Weighted Normal
modifiers, Data Transfer of custom normals, Laplacian normal smoothing, Geometry Nodes' `Set Mesh Normal` — is
the documented way to change the *field*. Third-party documentation from a custom-normals vendor states the
underlying problem plainly: the shading a shape produces is often not the shading it *should* produce, so the
normals are authored separately.

**The three documented face-normal shapes** are Flat (constant normal — **no terminator exists on the face at
all**, so nothing can follow a triangle), Cylinder (a clean vertical curve that sweeps as the light turns), and
Two Halves (two planes and a soft crease). **[inference]** For a port whose defect is a polygonal terminator, the
Flat and Cylinder shapes are the two worth rendering and looking at: Flat removes the line entirely and lets a
painted mask place it, which is the Hi-Fi RUSH conclusion reached from the other direction.

---

## 7. What this means for the port, stated plainly

The regenerated step is faithful and produces a polygonal line **by construction**, so the remaining work is not
in the step's formula. Given that, the honest options are:

1. **Keep the normals and accept the polyline** — faithful to the game's arithmetic, wrong to the eye, and not
   what the game looks like.
2. **Change the field.** Apply the game's own normal redirection (built and currently switched on), or go further
   with Flat / Cylinder shaping or a proxy-mesh normal transfer. This is Blender-documented practice and it is
   what the Hi-Fi RUSH team tried first.
3. **Move the line into a mask** so its position is a texture contour rather than a field contour. This needs the
   mask to be the *dominant* input, not a multiplier against a threshold that still varies across the face.
4. **Substitute a fixed ramp for the missing `fwidth`.** Cheapest, least faithful, and it hides corners rather
   than removing them.

The one thing this research rules out is continuing to tune the step's constants. The line is polygonal because
of what it is a contour of, not because of where the threshold sits.
