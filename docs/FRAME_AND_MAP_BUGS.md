# Four frame bugs, a zeroed map, and what they invalidate

Everything below was **measured**, not reasoned about. Each item is a thing that was silently wrong while
looking exactly like a thing that was silently wrong somewhere else. They are written down together because they
were found together and because three of them invalidate the labels on sheets that were already judged.

The short version: for the whole of this investigation the sun has been **below the horizon when the label said
above it**, the head's forward axis has been **backwards**, the light's azimuth has been **measured from the wrong
axis**, and the threshold map has been **an empty file**. The first three affect every render this project has
produced with its own light placement. The fourth affects every threshold-map render ever made.

Then, after the first five were fixed and the checkbox was in the addon, the user reported a **blocky face that
ignored the light entirely** — and that turned out to be a sixth, much older bug, in the light source itself.
It is section 7, and it is the one that mattered most to anyone actually using this.

---

## 7. The addon's light is an empty, and nothing was reading it (found by the user, not by any of my checks)

**The report:** ticking *Use SDF for face shading* gives a blocky face whose line does not move when the light is
moved.

Both symptoms have one cause, and it is a constant light direction. With the angle pinned, the only texels that
shade are the ones the mask paints as permanently dark, whose edge is a two-texel transition in a 512 map —
blocky — and no light can move it.

**Why the direction was constant.** `find_sun_object` documents the addon's light precisely:

    Upstream calls it "sun", this build ships it as "Uma Sun [DO NOT DELETE]" - both are Empties whose
    location is used as a direction vector, so match on the name prefix.

It returns an **Empty**. The "Uma Sun" empty's location *is* the light direction, and that is what the shipped
shader reads. But every game-face path built since then searched for something else:

    sun_object = next((o for o in bpy.data.objects if o.type == "LIGHT" and o.data.type == "SUN"), None)

A scene set up the addon's way has **no lamp in it at all**. Measured, on a fresh import with the addon's own
collection appended:

    lamps in the scene before shading: []
    lamps in the scene after shading:  []

So `sun_object` is `None`, the drivers are never created, the light vector stays at its default, and the face
shades from the mask alone. The threshold map had exactly the same hole, with `(0, -1, 0)` written in as a
fallback — which is why it looked blocky and deaf rather than obviously broken.

**And the empty could not be read the way the shipped shader tries to read it.** `assign_sun_direction` points
"every Object Info node" at the empty. Measured in Blender 5.2:

    node 'Object Info' type ShaderNodeObjectInfo
       has .object property: False
       inputs:                                    <- nothing at all
       outputs: Location(Vector), Color(Color), Alpha(Float), Object Index(Float), Material Index(Float), Random(Float)

`ShaderNodeObjectInfo` in this version has **no object property and no input sockets** — it can only report the
object being shaded. The loop body never runs, and the function still reports `1 Object Info node(s) assigned`,
because it counts nodes rather than successes. **It has been a no-op for as long as it has existed on this
version**, and its success message said otherwise. The shipped group's "Dot Creation" therefore reads the
*character's own* location, which is the origin — a zero vector.

**The fix.** `find_light_object` resolves the light in order of what a scene can actually offer, and
`light_direction_socket` builds the direction from it with drivers, which do work:

| source | read from | why this order |
|---|---|---|
| a sun lamp | its rotation | its own −Z is the direction it shines along, so towards the light is `Rz·Ry·Rx·(0,0,1)`; verified working |
| any other lamp | its rotation | a compromise, but it is a light a user can see and move |
| the "Uma Sun" empty | its location, normalised | this addon's own convention, and the fallback rather than the first choice |

The game step and the threshold map now **share one light socket** — the map reads the step's own
`Uma Face Light Direction` output — so the two decisions cannot end up keyed on different lights, and the second,
independent lookup that caused this cannot come back.

The empty is read as a **light position**, so the direction towards the light is the normalised location. That is
the convention the shipped "Dot Creation" group is built on (subtract the object's own position, normalise, dot
with the normal), and it makes the empty behave like a light: move it to the other side and the shadow changes
sides.

**Verified both ways, with no lamp anywhere in one of them:**

| light driven by | front against side | front against back |
|---|---|---|
| the "Uma Sun" empty (no lamp in the scene) | 10.014% of pixels differ | 35.309% |
| a sun lamp | 9.970% | 35.309% |

The two agree where they should: both were set to the same direction, and at azimuth 0 the lamp's towards-light
is `(0, −0.966, 0.259)` against the empty's `(0, −0.966, 0.259)`.

### Two smaller lies found on the way, both of them mine

* **A sign heuristic that was not measuring.** The first version of the fix decided the empty's sign by dotting
  the face's mean normal against the empty's location, and negated when it came out negative. It printed
  `+0.0000` on a model where the same quantity measures `−0.4096`, so it was silently failing to measure and
  silently choosing "no flip". Removed rather than repaired: the shipped graph's own structure
  (subtract → normalise → dot with the normal) already settles the convention, and a guess layered on an
  ambiguity is worse than no guess.
* **An instrument reading the wrong thing.** Printing the light vector by reading `default_value` off the chain's
  *computed* output sockets showed `(0, 0, 0)` at every light position — output sockets of computed nodes have no
  meaningful `default_value`; only driven inputs do. It nearly produced the conclusion "the light is zero".
  `matrix_world` read at the wrong moment is the same trap, and printed `(0, 0, 0)` for an empty that was at
  `(−2.166, 0, 1.860)`.

---

## 8. The map was baked from the wrong normal field, so the boundary came out as a web

**The report:** the face renders as a blocky, mottled patchwork that ignores the light.

The light part was §7. The mottled part is this, and the user's own scene contained the controlled experiment that
settles it: two copies of the same model, one shaded by the game's face step and one by the map, under one lamp,
side by side in a single render. **The step is clean and the mapped face is a web of pale patches.** Everything
else about them is identical, so the difference is the decision and the field it is taken on.

The step does not shade with the mesh's normals. The game's vertex program redirects them, per vertex, towards the
direction out of the head's own axis:

    R = normalize(P - footOfPerpendicular(P onto the head axis))
    o4 = w * N + (1 - w) * R,     w = (1 - _CylinderBlend) * vertexColour.BLUE

and `_CylinderBlend` is 0 on this material, so the weight is the vertex colour's blue, and 80% of the face's
corners on this model take the radial direction. **That field is monotone around the head.** The mesh's own
normals are not: they follow the nose, the lips, the eye sockets and the cheeks, so the surface azimuth swings
back and forth across one face.

The map stores, per texel, the light angle at which that texel flips, derived by inverting the step's rule. Baked
on the mesh's own normals, the stored angle oscillated between its extremes many times across the face, and a
single light angle then cut that oscillation **in many places at once** — a web of patches instead of one curve.
Measured, as the change in the stored angle between adjacent map texels:

| baked from | median | 90th percentile |
|---|---|---|
| the mesh's own normals | 6.9° | 24.6° |
| the game's redirected normals | **0.22°** | **0.81°** |

**Why this survived every check made up to that point.** The bake's self-check compared the map's decision against
the shipped rule *using the same azimuth field that went into the map*. That is a real check of the algebra — and
it passed at 0.000–0.241% — but it says nothing about whether that field is the one the shader evaluates. A check
that agrees with itself on the wrong input is exactly the kind of instrument this project keeps having to retire.

**The fix:** `bake_shading_normals` reproduces the redirection per loop, in world space, and the rasteriser takes
that field; it falls back to the mesh's own normals only when the head bone or the vertex colour is missing, and
says so loudly in the log, because a map baked from the wrong field is worse than no map. The bake now prints
which field it used and the roughness above, so this cannot regress quietly.

### And the shader's own redirection had a frame error of its own

`head_frame` hands over the head's axis and centre in the **mesh's own space**, and the node graph subtracts that
from `ShaderNodeNewGeometry`'s Position, which is **world** space. On an object at the origin those agree; on any
object that has been moved they do not, and the radial direction comes out wrong. The user's scene is exactly that
case: the second model sits at `x = 0.419`, so its step was computing a radial direction from a mesh-space point
against a world-space one. Both are taken into world space now.

---

## 1. The sun's rotation was solved wrongly, so "elevation 15" was −15

A sun shines along its own local **−Z**. With `rotation_euler = (rx, 0, rz)` the shine direction is therefore

    Rz(rz) . Rx(rx) . (0, 0, -1)  =  ( -sin rz . sin rx ,  cos rz . sin rx ,  -cos rx )

The renderer used `rx = acos(travel.z)` and `rz = atan2(-travel.x, -travel.y)`. Feeding a wanted travel of
`(0, -0.2588, -0.9659)` (elevation 15, azimuth 0) that gives `rx = acos(-0.9659) = 165°`, whose actual shine is

    (0, +0.2588, +0.9659)

— the wanted direction with **Y and Z both negated**: the light was in front of the character but *below* it,
rising, instead of above it, falling. Its horizontal direction happened to come out right, which is exactly why
nobody noticed: a front light that is 15 degrees below the horizon still looks like a front light.

Measured, in `probe_sdf_frame.py`, by reading the direction back out of `sun.matrix_world`:

| wanted travel | achieved shine | verdict |
|---|---|---|
| `(0.0000, -0.2588, -0.9659)` | `(0.0000, +0.2588, +0.9659)` | Y and Z negated |
| `(-0.6830, -0.2588, -0.6830)` | `(-0.6830, +0.2588, +0.6830)` | Y and Z negated |
| `(-0.9659, -0.2588, 0.0000)` | `(-0.9659, +0.2588, 0.0000)` | Y and Z negated |

**What it invalidates.** Every statement of the form "at elevation 15, the boundary does X" describes a light
15° *below* the horizon. The user's own reading of the earlier sheet — that a low sun traces the cheek cleanly at
azimuth 90/270 and that the lip and under-lip region appears at 180 — was made from below-lit renders, which is
consistent with a boundary sitting low on the face. The mask-dominance measurements are unaffected: those came
from the mask texture and the formula, not from a render.

**The fix.** Solve for the shine direction properly. With `towards = (+cos e . sin a, -cos e . cos a, +sin e)`
(azimuth 0 = straight in front of a face looking −Y, positive azimuth swinging towards +X):

    travel = -towards
    rx = acos(-travel.z)
    rz = atan2(-travel.x / sin rx, travel.y / sin rx)

and then **read the direction back out of the object's world matrix and refuse to continue if it is off by more
than 0.01°**. `render_sdf_face.py` does this and prints the residual on every run. After the fix the residual is
`0.0000°` at every azimuth tested.

---

## 2. Azimuth 0 meant "behind the head"

With the corrected solver and the old direction vector, azimuth 0 put the light at `+Y` — **behind** a character
that faces `−Y` — so a "front" light shaded 100% of the face. The convention now is stated in the function that
implements it and checked in the render:

    azimuth 0    -> light straight in front of the face, alpha = 0, progress t = 0
    azimuth 90   -> a full side light, alpha = 90, progress t = 0.5
    azimuth 180  -> directly behind, alpha = 180, progress t = 1

The renderer prints the angle between the light and the face's forward axis and says in words whether the light
is in front of or behind the face. At azimuth 90 it reads 90.6° and at 180 it reads 167.3°.

---

## 3. The head bone's forward axis is `+Z`, not `−Z`

Measured from the bone's rest matrix in world space:

    x (col 0)   (+1.0000, +0.0000, +0.0000)   azimuth  +90.00
    y (col 1)   (-0.0000, -0.0396, +0.9992)   azimuth -180.00
    z (col 2)   (+0.0000, -0.9992, -0.0396)   azimuth +180.00

`col[2]` points along `−Y`, and `−col[2]` points `+Y`. The face material's own normals and the render camera
both put the face towards `−Y`, so **the character's forward is `+col[2]`**, and both the map builder and the
renderer had it negated. The rig is right-handed and consistent: `col[0] × col[1] = col[2]`, with the bone's local
Y along the bone (up) and its local Z the direction the character faces.

Consequence: every stored angle was keyed 180° away from the face's forward.

**The fix.** `forward = matrix.col[2]` in `face_frame()` and `head_axes()`, with the reason written next to it.

---

## 4. The azimuth was measured from the right axis

`alpha = atan2(dot(L, right), dot(L, forward))` is the angle from the **forward** axis. The graph was built as
`ARCTAN2(forward, right)`, which is the angle from the *right* axis, so a light dead ahead read as **90°** and
`t = 0.5` — half the face shaded with the light straight on. Fixed by feeding `ARCTAN2` the right-axis dot as its
first input.

---

## 5. `colorspace_settings` after `foreach_set` writes an empty file

The map was written the ordinary way:

```python
result = bpy.data.images.new(name, width, height)
result.pixels.foreach_set(out.reshape(-1))
result.colorspace_settings.name = 'Non-Color'      # <- this discards the buffer
result.filepath_raw = str(target); result.file_format = 'PNG'; result.save()
```

Read back, `face_shadow_sdf.png` was **512×512 with red min 0.0000, mean 0.0000, max 0.0000 and 100% of texels
exactly zero**, while the array handed to it had mean 0.287 and max 1.000. A generated image rebuilds its buffer
when its colorspace is assigned, and the pixels written a line earlier are gone. On disk the file was a
perfectly plausible 3.7 KB PNG.

**Why this is the most dangerous of the five:** an all-zero threshold map means "this texel is shaded at every
light angle", so the face renders **uniformly shaded at every sun angle** — which is a *stable, plausible-looking*
result. It produced four bit-identical images at four azimuths, and the first reading of that was "the driver is
not evaluating", which sent an hour into the driver chain. The driver was innocent; it was removed for nothing.

**The fix.** Write the PNG bytes directly (`struct` + `zlib`, 16-bit truecolour), and **read the file back and
print its statistics** as part of the build, failing loudly if the maximum is zero. The array's row 0 is the
bottom and PNG rows run top down, so the array is walked backwards when writing. After the fix:

    read back face_shadow_sdf.png: 512x512, mean 0.2486, max 1.0000,
                                   R mean 0.3812, G mean 0.3647, 38.9% of red exactly zero

which is consistent with the face covering 75.2% of the texture and 51.2% of its texels being pinned to zero
(0.752 × 0.512 = 38.5%).

---

## 6. Node-socket drivers are not evaluated by a background render (probably not the cause)

The first threshold-map sheet had drivers on a `CombineXYZ`'s three component sockets, built the way the
community does it. The four azimuths produced bit-identical images. The conclusion drawn at the time — "drivers
do not evaluate here" — is **not established**, because the map was empty and *every* configuration produces
identical images in that state. What is established is only this: with drivers removed and the angle written into
the sockets from Python, the render does respond, so the question is moot for the sheet.

`render_sdf_face.py --driver` reattaches the drivers so the question can be settled on its own, with a map that
is known to be non-empty. Not done yet.

---

## What the corrections do to the numbers

The bake was always arithmetic on the mask and the rest-pose normals, so it is checked against the shipped
formula directly rather than through a render. With the forward axis corrected, the agreement improved by an
order of magnitude and the shaded fraction became a clean monotone sweep, which is the signature of a threshold
map that is actually keyed to the light:

| light angle | disagreement with the shipped formula | shaded fraction of the face |
|---|---|---|
| 0° | 0.123% | 51.2% |
| 45° | 0.008% | 56.2% |
| 90° | 0.000% | 66.4% |
| 135° | 0.001% | 87.5% |
| 180° | 0.241% | 100.0% |
| −45° | 0.007% | 57.6% |

Every disagreement is on texels facing *away* from the light, which is the one case a single stored number per
texel cannot express — the documented structural limit, not an error.

Before the forward-axis fix the same table read 0.303–0.539% disagreement and 69.8% shaded at 0°, which is what
a map keyed to the back of the head looks like.
