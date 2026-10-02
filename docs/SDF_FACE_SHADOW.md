# The angle-keyed ("SDF") face shadow

A single checkbox in **Shading options**: *Use SDF for face shading*. Ticking it replaces the face's lit/shaded
**decision** with a baked angle map and leaves every other thing the game face shading does exactly as it was.

---

## What it is

Each texel of the face's own texture stores one number: the light angle at which the face at that texel turns
dark. The shader measures the light's angle in the head bone's frame and compares the two.

    alpha     = atan2(dot(L, right), dot(L, forward))     // in the head bone's frame, world space
    V         = alpha / pi                                // 0 dead ahead, +/-1 directly behind
    side      = frac(V / 2) < 0.5                          // which of the map's two channels
    t         = pingpong(V, 1)                             // == |V| over this range
    u         = the stored threshold at this texel
    shaded    = t >= u

**Nothing in that comparison reads a vertex normal.** That is the whole point, and it is why this exists:

* a `dot(N, L)` terminator is the level set of a field that is linear inside each triangle, so it is one straight
  segment per triangle with a corner at every mesh edge — a polyline by construction, not by aliasing;
* a facial morph makes it worse. Measured on this model, a morph raises the face's normal-field roughness from
  4.6 to 8 degrees and spikes individual vertices by up to **175 degrees** (`docs/MORPH_ARTEFACT.md`).

Neither can happen here. The map is baked from the **rest** pose and lives in uv space; the angle is measured
from the bone. A shape key has nothing to move.

---

## Where the map comes from: the step rule, inverted, on the field the step shades with

**The field matters as much as the algebra.** The map is inverted against the step's rule, so it has to be
inverted against the same *normal field* the step evaluates:
`w * N + (1 - w) * R`, the game's redirected normal, with R the direction out of the head's own axis and w the
vertex colour's blue. Baking it from the mesh's own normals instead - which follow the nose, the lips and the
sockets - makes the stored angle oscillate across the face, and one light angle then cuts that oscillation in many
places at once: a web of patches rather than a boundary. Measured on this model, the change in the stored angle
between adjacent texels is 0.22 degrees on the redirected field against 6.9 on the mesh's own. See
`docs/FRAME_AND_MAP_BUGS.md` section 8.

`apply_game_face_step` builds

    shaded  <=>  halfLambert <= (ToonStep - ToonFeather) / mask.r,   halfLambert = 0.5 * dot(N, L) + 0.5

For a horizontal light `dot(N, L) = cos(alpha - beta)`, with `beta` the surface's own azimuth. Solving for the
light angle at which the texel flips:

    |alpha - beta| >= theta(m),    theta(m) = arccos(2 * (ToonStep - ToonFeather) / m - 1)
    T(p) = (beta(p) + theta(m(p))) / pi                    // light on the character's right
    T(p) = (-beta(p) + theta(m(p))) / pi                   // light on its left

`m <= ToonStep - ToonFeather` makes the right-hand side at least 1, which no cosine can exceed: those texels are
shaded at every angle and are stored as 0 in **both** channels — they are not a left/right question.

The build checks this against the shipped rule numerically, on the source data rather than through a render, and
prints the result, together with the roughness of the field it used. **Note what that check does and does not
prove:** both sides of it are evaluated on the *same* azimuth field that went into the map, so it verifies the
algebra and nothing about the choice of field. It passed at 0.000-0.241% while the render disagreed with the step,
because the field was wrong. For the main face material:

| light angle | where the map disagrees with the shipped rule | shaded fraction of the face |
|---|---|---|
| 0° | 0.123% | 51.2% |
| 45° | 0.008% | 56.2% |
| 90° | 0.000% | 66.4% |
| 135° | 0.001% | 87.5% |
| 180° | 0.241% | 100.0% |
| −45° | 0.007% | 57.6% |

Every disagreement is on texels facing **away** from the light — the one case a single stored number per texel
cannot express. That is the documented structural limit below, not an error.

---

## One map per material, and why

The game's own shader would look up different normals for the same texel depending on which material is being
drawn, so a map shared between two materials that overlap in uv space is wrong on exactly the overlap. Measured
on this model: this face material and the **eyebrows** share **11640 texels** of the 512 map and disagree about
the direction the surface faces there by up to **82 degrees**. So each face material gets its own map, named
`Uma Face Shadow Threshold <material>`, packed into the .blend.

That also means the eyebrows and the 40-triangle face patch (which covers 0.0% of the texture — a tiny separate
uv island the mask shades outright) get their own, correct-by-construction maps rather than a neighbour's.

---

## Using it

The panel offers **one** tick box, *Use SDF for face shading*, and hands the operator:

    op.legacy_shading   = False
    op.game_face_shader = True
    op.game_face_sdf    = shade_face_sdf

The legacy tick box is gone, and so is the scene property behind it: it was the shipped shader group with none of
this fork's face work, the SDF box replaced it, and a panel with two switches where one of them quietly does
nothing is how this ended up being reported as broken. The operator still carries `legacy_shading` for scripts -
the comparison renders set it to show the shipped group beside the other paths - it is simply not offered in the
UI. `game_face_shader` now defaults to on, so a bare `bpy.ops.uma.apply_shader()` gives the fork's face shading
rather than skipping both paths.

### Pressing Shading twice rebuilds

It used to **refuse**, silently, the moment the `Uma Face *` nodes existed - which is indistinguishable from the
option being broken, and it meant a graph built by an older build could never be repaired. That mattered here: the
older builds wrote a *constant* light direction whenever the scene had no lamp in it, so a model shaded by one of
those would stay deaf to the light no matter what was ticked afterwards. The step now removes its own previous
nodes and builds again, and says so:

    [uma_addon] <material>: removed 45 previous 'Uma Face' node(s) and is building the face shading again from scratch

### If a change does not appear in Blender

**Blender imports an add-on once per session.** Editing the files on disk changes nothing for a Blender that is
already running - the module is in memory. After any update, restart Blender, or toggle the extension off and on
in Preferences, Add-ons.

The files live in two places, and they are kept identical by hash:

    repo:  C:\Users\user\Documents\dev\Blender-Uma-Addon\uma_addon\addons\uma_addon\
    loaded: %APPDATA%\Blender Foundation\Blender\5.2\extensions\user_default\uma_addon\uma_addon\addons\uma_addon\

That is the **only** copy Blender can load: there is no `uma_addon` under `scripts\addons`, the extension folder
is a real directory and not a link, and the `__pycache__` beside it is built from the current source rather than
an older one. So a stale panel means a stale session, not a stale file.

Two things to look for, to tell which build is running:

* the Shading options box shows **one** tick box, not two;
* pressing Shading prints `[uma_addon] <material>: the light is read from ...`, naming the object the face
  follows. If that line is missing, the running code is older than this document.

### The light it follows

**The face shading needs a light object, and it will tell you which one it found.** In order of preference:

| source | read from |
|---|---|
| a **sun lamp** | its rotation |
| any other **lamp** | its rotation, as a compromise |
| the **"Uma Sun [DO NOT DELETE]" empty** | its location, normalised — this addon's own convention |

If the scene has none of those, the addon prints `NO LIGHT SOURCE` and skips the face step rather than baking a
constant that would look plausible and never move. **Move or rotate that object to move the shadow.**

This is where the first version failed, and it failed in the way this project keeps failing: the game-face paths
searched for a `LIGHT` object of type `SUN`, while the addon's own light is an *empty* whose location is the
direction vector. A scene set up the addon's way has no lamp in it, so nothing was found, the light vector stayed
at its default, and the face shaded from the mask alone — **blocky**, because the mask's edge is a two-texel
transition in a 512 map, and **deaf to the light**. The empty also cannot be read through an Object Info node,
which is what the shipped shader tries: in Blender 5.2 that node has no object property and no input sockets at
all, so `assign_sun_direction` has always been a no-op on this version. See `docs/FRAME_AND_MAP_BUGS.md` §7.

Verified with **no lamp anywhere in the scene**, moving only the empty:

| light driven by | front against side | front against back |
|---|---|---|
| the "Uma Sun" empty | 10.014% of pixels differ | 35.309% |
| a sun lamp, same directions | 9.970% | 35.309% |

Both the step and the map read the *same* light socket, so they cannot disagree about which way the light comes
from.

Verified end to end by running the operator as the panel would and asking the scene what happened: three maps
baked and packed, all three face materials' blend factor coming from `Uma Face SDF Shaded`, and a render that
follows the light. A checkbox that silently does nothing is the failure mode this project keeps meeting, so none
of that is taken on trust.

---

## Limits, stated rather than discovered later

* **Horizontal light only.** One number per texel is one switch: the representable set is "shaded once the light
  passes this angle", never "shaded, lit, shaded again". Elevation is not represented at all.
* **No occlusion.** Hair over the forehead, a hand in front of the face: nothing here evaluates a shadow map.
  The mask's permanently shaded texels are where the artist painted that in, and that is all.
* **A drawn boundary still stretches with an extreme expression**, because it lives in uv space. It stays smooth
  and stays in the same place relative to the *texture*; a morph that moves the surface under the texture moves
  the drawn line with it. That is the price of being immune to the normal field.
* **8-bit is not the limit**: the map is written at 16 bits, and `T` moves about one 8-bit level per texel
  anyway.

---

## What is measured, and what is not

**Measured:** the bake agrees with the shipped step rule to 0.000–0.241% of face texels; the shaded fraction is a
clean monotone sweep across the half turn; the operator wires the decision in and the render responds to the sun.

**Not measured, and not claimable:** that it *looks* better under a morph. Over whole frames the morph changes
the image by about the same number of pixels either way — for `Mouth_OdorokiB` at azimuth 45, 6.391% of pixels
under the map against 6.534% under the step — because the morph moves geometry as well as shading. So the claim
here is about the *shape and quality of the line*, which is a judgement for eyes, not for a pixel count. The
sheets in `umaviewer_exports\sheets\mouth` exist for exactly that judgement.

---

## The failures found while building it

Three were in the instruments and are written up in `docs/FRAME_AND_MAP_BUGS.md`. Two were in this code and are
recorded here because both produced plausible-looking output:

* the bake's result was assigned to `image` while the code below read `mask_image`, so the texture node silently
  got `None` and the operator raised;
* an edit dedented `owner[window] = ...` out of the rasteriser's inner loop, so ownership was only recorded for
  the last triangle — which made the overlap report name material index `-1`, displayed as the *last* material
  in the table (the tail). The fix was indentation; the lesson is that the report needed to resolve `-1` to
  "no owner" rather than to a name, and it now counts covered texels with no owner explicitly.
