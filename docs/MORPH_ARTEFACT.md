# The morph artefact: measured, and where it comes from

The report to take seriously is that **the shading defect appears when facial morphs are used, not on a static
face**, and that a clean line on a static face is the baseline rather than an achievement. This is the same
failure the Hi-Fi RUSH team listed when they abandoned the normals route — "facial animation destroys the shadow
silhouette". This file records what a morph actually does to the face's shading field on this model.

## What was measured

`probe_morph_sweep.py` drives each of the model's 192 shape keys to 1.0 in turn, on the evaluated mesh (the base
mesh carries no shape key, so reading it measures the rest pose twice — the first attempt did exactly that and
reported a change of zero), and reports the angle between each face vertex's shading normal and its neighbours':
the mean, the 90th percentile, and the largest single disagreement.

| pose | mean angle between neighbours | p90 | largest single disagreement |
| --- | --- | --- | --- |
| **at rest** | 4.585° | 11.957° | 88.860° |
| `Mouth_WaraiC` | 6.722° | 18.258° | 88.860° |
| `Mouth_WaraiD` | 7.450° | 20.292° | 93.975° |
| `Eye_CloseA_L` | 7.846° | 18.376° | **168.041°** |
| `Eye_KusyoA_L` | 7.950° | 18.171° | **174.733°** |

So a facial morph **roughly doubles the field's roughness** — the mean neighbour angle goes from 4.6° to 7-8°,
and the p90 from 12° to 18-20° — and it introduces **single vertices whose normals disagree with their
neighbours by up to 175°**, which is a normal pointing very nearly backwards relative to its neighbours.

Those spikes are the artefact. A vertex whose shading normal is 175° from its neighbours' is a discontinuity in
the field, and the terminator of a hard threshold on that field will have a corner there with a kink of up to
that size. The rest pose has one such vertex already (88.9°); a morph turns that into most of a half-turn.

## What is *not* wrong

The morph does not detach the normals from the geometry. Measured: the shading normals turn by a mean of 3.475°
against the geometry's own 3.480° — a ratio of **0.998**. The authored custom split normals follow the
deformation correctly. So the failure is not "the normals stopped tracking the mesh"; it is that **the deformed
field is rougher than the rest field, and it develops spikes**.

Also measured, and worth keeping in proportion: over a single morph, **0 of 1328 vertices flip their lit/shaded
verdict** at a low light, while `dot(N, L)` moves by a mean of 0.018 and at most 0.67. So the effect is
concentrated rather than global — a few vertices move a lot, the majority barely move. That is consistent with
spikes rather than with a field-wide shift.

## The flat-normal attempt, measured, and why it changed almost nothing

The obvious fix for a rough field is to remove the field from the decision: point every face normal the same
way, so `dot(N, L)` is uniform, the step produces one value, and the line comes from the painted mask instead.
That is implemented (`flatten_face_normals`, on by default) and rendered at rest and under three facial morphs,
against the exported field, at two sun angles.

**It changes almost nothing.** At sun elevation 15 / azimuth 90 the splits came out:

| cell | shaded share of the face | shaded regions |
| --- | --- | --- |
| exported field, rest | 45.8% | 2 |
| exported field, `Eye_KusyoA_L` | 44.4% | 3 |
| flat, rest | 44.3% | 2 |
| flat, `Eye_KusyoA_L` | 42.9% | 3 |

and the proper renders agree: between the two modes the luminance range is identical (157..226), the means
differ by about 1.2 levels, and **the pixel differences between a morph and rest are the same in both modes**
(3.32% at `Eye_KusyoA_L`, 1.11% at `Eye_CloseA_L`, 6.7-6.8% at `Mouth_WaraiD`). So flattening the normals
leaves the image essentially as it was.

**[inference] The reason, and it is the same arithmetic as the mask's toggle.** With this material the step is
`saturate(1 + (0.399 - mask.r * halfLambert) / 0.001)`, and the mask is binary. Take a half-Lambert of 0.86,
which is what the face carries at a typical light: where the mask is 1 the argument is `1 + (0.399 - 0.86)/0.001`
= −460, so the branch is **lit**; where the mask is 0 it is +400, so the branch is **shaded**. The comparison
therefore reduces to `mask.r > 0.464 / halfLambert` — and since `halfLambert` sits between 0.5 and 1 over the
whole face, that threshold lands inside the mask's transition window (0.401 to 0.798) **only for a narrow range
of halfLambert values**. Across most of the face the mask decides on its own, and the normal field has nothing
left to influence.

**So the line in these sheets is the mask's contour, not a field contour** — with either normal mode, at both
sun angles. Which also means the artefact under a morph cannot be coming from the normal field at all: a
flattened field has no per-vertex structure to deform, and the image is unchanged.

**That reframes the whole search.** The normal-field theories — flat, cylinder, redirected, proxy-transfer —
are all fighting for control of a term that this material does not let decide anything. What does decide is the
mask: a 512² binary texture magnified onto a face a few hundred pixels across, sampling a contour that is
therefore quantised to the mask's own texel grid and to whatever the bilinear filter makes of a 1-texel
transition. **If the artefact under a morph is a jagged line, the mask's sampling is where to look next**, not
the normals.


1. **Shape keys deform the geometry, and the normals are re-derived from it.** Where the deformation folds or
   crushes a region — an eye closing is a large local deformation on a low-poly face — the recomputed field goes
   through a large angle over one edge. The authored normals follow, but authored normals describe a *shape*,
   and a morphed shape is a different shape.
2. **The vertices that spike are shared between materials or between regions.** The worst vertices sit in a
   narrow numeric band (10256, 10355, 10482-10811) — the eye and mouth region, which is exactly where the morphs
   act.
3. **The morphed geometry is not the geometry the normals were authored for.** Consistent with (1): the game
   authored its normals for the rest shape and then animates the shape, and in the game there is no
   recomputation to do because the vertex program *redirects* the normal towards a field that does not depend on
   the local surface at all.

## What this means for the plan

**It settles the direction.** A clean line at rest is not evidence of anything, because the field at rest is
smooth by construction (mean 4.6°) and the threshold on it behaves. The defect lives in the *deformed* field,
and no amount of tuning the rest-pose step will touch it.

It also explains why every rendering experiment in this work came back ambiguous: the experiments were run on
the rest pose, where there is nothing to find.

**And it makes the case for diverging from the game concrete rather than aesthetic.** The game's approach relies
on the normals pointing where the shading should be shaded, which works when the shape is static and the
redirection is doing the shaping. Blender recomputes the field from the deformed surface, so the same approach
inherits the deformation's roughness. The alternatives that do not depend on the deformed normal field at all
are the ones worth building:

* **Remove the normal field from the decision.** If the face's shading normals are constant in the head's local
  frame — all pointing the same way, or following a smooth authored field that does not change under a morph —
  then `dot(N, L)` is constant across the face and the terminator vanishes, leaving the painted mask to place
  the line. Nothing about that can follow a triangle or a spike.
* **Or key the line to a texture and a light angle** rather than to `dot(N, L)`: the angle-keyed face shadow map
  the industry uses, which is immune to deformation because it is indexed by an object-level angle.
* **Or smooth the field after deformation.** Possible, but it fights the deformation rather than avoiding it,
  and the spikes would need to be suppressed every frame.
