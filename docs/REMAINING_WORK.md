<!-- superseded -->
> **Note.** Superseded in places. This file is the session log of an earlier attempt, and its central conclusion - that the hard-edged boundary is a defect in the port's shading - is right, but the causes it eliminated were eliminated on comparisons made before the two renderers were framed alike and before the viewer's single-azimuth light bug was found. `GROUND_TRUTH.md` has the current position.

# What remains, and the design for each piece

Written at the end of the session that read the game's face shader out of its bytecode and ported it. Everything
below is unstarted or partially done, with the approach that was worked out but not landed. The current state is
commit `4fc33fb` in the addon: verified, with the tree clean.

## 1. The head frame, live (design only, not implemented)

The game sets `_FaceForward`, `_FaceUp` and `_FaceCenterPos` from the head bone every frame. The port uses
substitutes: the shading normal for forward, the world up for up, and the head's rest centre for the centre. That
is the last structural gap, and it is why the region masks are approximate.

Two approaches were tried and neither was verified working:

- **A Geometry Nodes modifier** writing the frame as vector attributes. It cannot work as hoped: geometry nodes
  cannot read a *bone's* pose in Blender 5.2, only an object's transform.
- **Drivers on a Combine XYZ reading the head bone directly.** Drivers do accept a bone target, but for bones only
  the location transform types are available, so a bone's *rotation* cannot be read this way.

**The design that should work**, and is worth implementing with its verification done properly: create an Empty
parented to the Head bone through a **Child Of** constraint, so the Empty's location *is* the head and its rotation
*is* the head's. Drivers can read an object's rotation and location freely - the light already works exactly this
way - so three Combine XYZ nodes fed from the Empty give `up`, `forward` and `centre` live.

The rotation-to-axis expressions for Blender's `'XYZ'` euler (`R = Rz * Ry * Rx`), whose columns are the rotated
basis vectors:

```
X column: (cos(ry)*cos(rz),  cos(ry)*sin(rz),  -sin(ry))
Y column: (cos(rz)*sin(ry)*sin(rx) - sin(rz)*cos(rx),
           sin(rz)*sin(ry)*sin(rx) + cos(rz)*cos(rx),
           cos(ry)*sin(rx))
Z column: (cos(rz)*sin(ry)*cos(rx) + sin(rz)*sin(rx),
           sin(rz)*sin(ry)*cos(rx) - cos(rz)*sin(rx),
           cos(ry)*cos(rx))
```

The head bone's **Y** axis is up. Forward is the armature's facing direction measured in the rest pose, rotated by
the bone's rotation - not the bone's own Z column, which depends on the rig's bone roll.

**Verification that was never done, and must be:** apply the shading, then *rotate the Head bone* and confirm the
driven values change. Numbers, not a render - a render cannot show it on this model, because

## 2. `_faceShadowAlpha` is 0 on the model used for testing

The region path is gated by the material's own `_faceShadowAlpha`, and this model's material has it at **0**. So the
cheek and nose regions are inert here and the face shows the bare step's boundary - which, with `_ToonFeather`
0.001, is a hard polygonal line following the mesh. The game's shader does the same on this material.

Any visual test of the regions therefore needs a character whose `_faceShadowAlpha` is non-zero, or a hand-edited
material comment. Testing against the viewer's own render of the same character is the right comparison, since the
viewer runs the vendor's shader.

## 3. Properties the exporter does not carry yet

Roughly eight lines in `Assets/Scripts/Exporters/ModelExporter.cs`, then a Unity batch run to regenerate:

| property | why it matters |
| --- | --- |
| `_faceShadowAlpha`, `_faceShadowEndY`, `_faceShadowLength` | the region strengths; without them the port uses 0/0/0.1 and the regions are off |
| `_CylinderBlend` | 0 for the face, which is right, but the viewer sets 0.25 for hair and eyes |
| `_NormalizeNormal` | 0 in this material, which means the game uses the *raw* normal, not the normalised one |
| `_MaskColorR/G/B` strengths | the region gates compare against `1 - strength` |
| `_OriginalDirectionalLightDir` | the game lerps to it when a flag is set (`cb1[26].w`) |

## 4. The region's colour endpoints

The reviewer's scope caveat, and it still stands: lines 87-260 of the 9-texture variant - about 170 instructions
covering lighting, the two colour paths, the environment and grading - were never traced. The region's shape here is
right (a colour switch selected by the masks, after the base blend) but the *second colour path* is built from the
sharp step over the same two textures, which is a plausible identity rather than a proven one. Re-derive it from
that section before calling the port faithful rather than merely close.

## 5. Two process traps, both hit more than once

- **Sync the installed extension and check it by hash.** A stale installed copy produced invalid test results twice,
  once silently invalidating a whole round of user testing. `Get-FileHash` comparison after every copy.
- **Render the value, do not reason about it.** Every wrong conclusion in this work - the mask channel, the
  `_MaskColorTex` binding, the region's coverage - came from reasoning about a summary. Every correction came from
  rendering the thing and looking at it, or from reading the instruction that produced it.

## 6. A correction: the hard edges are not faithful output, and why `_faceShadowAlpha` misled me

I concluded that the hard-edged patches were the game's own behaviour on this material, on the grounds that
`_faceShadowAlpha` is 0 and gates the region path. That was wrong, and tracing the value through the viewer's own
code shows why.

**`_faceShadowAlpha` is not a material value.** It is a driven shader property of the game's facial driven-key
system: `Gallop/FaceDrivenKeyTarget.cs` binds it as a `BindProperty` of a `FacialOtherMorph` named `Shade_Ctrl`,
driven by that locator's X scale. The 0 in the stored material is its *rest* value. Exporting it as a material
constant was misleading, and the exporter no longer does.

**The viewer does handle the facial-shadow uniforms**, every frame:

```
UmaContainerCharacter.cs:430-432
    //Used to calculate facial shadows
    _FaceForward   <- HeadBone.transform.forward
    _FaceUp        <- HeadBone.transform.up
    _FaceCenterPos <- HeadBone.transform.position
UmaContainerCharacter.cs:434
    _faceShadowHeadMat <- HeadBone.transform.worldToLocalMatrix
```

That comment is the game's own and it names the mechanism: those three vectors *are* the facial shadow. A second,
matrix-valued uniform follows the head too — and a matrix in the constant buffer is the best explanation for the
three consecutive slots I read as `cb2[24]`, `cb2[25]` and `cb2[26]`. So the head frame in the pixel program is very
likely `_faceShadowHeadMat`, not three independent vectors.

**And the region colours are per character**, set at runtime from the character's colour data:
`SetMaskColor` writes `_MaskColor{R,G,B}{1,2}` and `_MaskToonColor{R,G,B}{1,2}` — twelve colours, matching the six
colour constants the pixel program uses. An export that omits them leaves the region path with nothing to switch
between, which is part of why it is inert here. They are in the exporter now.

**What this means for the artifact.** Since every character shares a base model and the game shows no hard edges on
any of them, the boundary is not the game's behaviour. The candidates, in order: the facial-shadow path (driven
alpha, mask colours, head matrix) is absent or inert in the port; the head frame is static where the game's follows
the bone every frame; and `_NormalizeNormal` is 0 in this material, which means the game uses the *raw* vertex
normal while `shading_normals()` always normalises. The last is a one-line difference and worth testing first.

## 7. What the pipeline dump settled, and the two-open-question answer

`-umaDumpFace` in the headless exporter logs the runtime material and the mesh facts, because everything after the
meta database has to be verified: the stored bundle material is not what the shader receives.

**It found a real difference.** The runtime `_CylinderBlend` is **0.25** where the stored material says **0**. The
viewer overwrites it when loading a character, and this fork's exporter did not carry the property at all, so the
port used a default of 0. The exporter carries the runtime value now, verified in the exported file
(`cylinder_blend=0.25`).

**It eliminated a candidate.** The face mesh's normals are all exactly unit length (`min 1.000000 mean 1.000000 max
1.000000`), so `_NormalizeNormal` being 0 changes nothing — there is no raw magnitude for the game's shader to use.
That closes the reviewer's D8 as a cause.

**And it answered the artifact question, which was not a port defect.** The hard-edged patches appear only at
*oblique* light. With a frontal light, the game-side shading and legacy agree to within a point:

```
frontal light, 440px, character 1001
    legacy       face mean 214.6
    game shading face mean 215.5
```

At ±90 degrees the face's `N·L` crosses the step *across* the face, and the step is hard because `_ToonFeather` is
0.001 — which is the game's own value, in the same shader, on the same base model. So the boundary is what the game's
shader does at that light angle, not something the port invented.

**The one verification still worth doing**, and it is cheap: run the viewer's own grid at the same oblique angles —
`Tools/headless_export.ps1` already rotates a directional light for its shot grid — and see whether it shows the same
boundary. If it does, the port matches the game at every angle and this is closed. If it does not, the difference is
in the light setup rather than the shading, and the next step is to compare the two light vectors rather than the two
shaders.

## 8. The three-way comparison: the artifact is ours, and the viewer proves it

The test that settles the question is the one reference we can trust absolutely: the viewer, which runs the vendor's
own compiled shader on the same model. Rendering it at the same oblique light angles as the port, and putting the
three side by side (`grids/viewer_oblique/compare_three.png`, columns viewer | legacy | game shading, rows light 0,
+90, -90):

```
row 0 (light 0)     viewer 146   legacy 192   game shading 193
row 1 (light +90)   viewer 143   legacy 192   game shading 187
row 2 (light -90)   viewer 145   legacy 192   game shading 187
```

The viewer's face is clean and smooth at **every** angle, with a soft cheek shading and no patches. Ours shows hard
polygonal patches at both oblique angles. So the boundary is **not** the game's behaviour at oblique light — my
earlier conclusion was wrong and the hypothesis that the game would do the same is falsified by the reference itself.

That makes this a defect in the port, and it gives a reliable way to find it: the viewer renders the same model,
textures and light angles, so any parameter can be changed on our side and compared against a known-correct image.
The candidate list is now short and testable, in this order:

1. **The cylinder blend.** Its weight is `(1 - _CylinderBlend) * vertexColour.b` about the head's axis. `_UV3.x` is
   the blue channel as the export packs it (one component per layer), so that part is right - but the blend's
   *effect* on the normal field has never been compared against the viewer's normals.
2. **The head frame.** Static substitutes where the game's comes from the head bone every frame.
3. **The step's constants**, which match the material exactly, and the light vector convention, which has not been
   compared direction-for-direction against Unity's `_WorldSpaceLightPos0`.

A useful next test, and cheap: apply the port with the cylinder blend *skipped* and compare against the viewer. If
the patches vanish, the blend is the culprit; if they remain, it is the step or the light.

## 9. The blend is eliminated, and a flaw in my own comparison

Rendering the port with the cylinder blend switched off and comparing against the same angles:

```
                     viewer   legacy   with blend   no blend
light +90               147      200          195        196
light -90               149      200          194        195
```

The patches are unchanged without the blend, so the cylinder blend is not the cause. That leaves the step, the
light, or the normal field as it arrives from the PMX.

**And the comparison itself has a flaw I should check before trusting it further.** The viewer's shots come from
`headless_export.ps1 -ShotAzimuths`, and ours from `blender_render_angles.py --grid`. Those are two different
programs placing a light, and nothing verifies that "+90" means the same world direction in both. If the two tools
place the light at different angles, then the viewer and the port were never illuminated the same way, and the
conclusion that ours is at fault would need re-checking.

The test is cheap and settles it: have each tool print the world-space direction it gave the light, and compare the
two vectors for the same nominal azimuth. Until that is done, the only safe statement is that the port and the
viewer differ, not which of the shading inputs is responsible.

## 10. What is eliminated, and what is left

Tests run against the viewer, which runs the vendor's own shader on the same model, textures and morph. Face mean,
the two oblique light angles:

| test | ours | viewer | conclusion |
| --- | --- | --- | --- |
| frontal light | 215.5 (legacy 214.6) | - | clean, agrees with legacy |
| oblique, elevation 35 | 195 | 147 | ours shows hard patches, viewer does not |
| oblique, elevation 70 (matched) | 197 | 147 | still differs - elevation was a confound, not the cause |
| oblique, elevation 70, cylinder blend off | 197 | 147 | still differs - **the blend is eliminated** |

So: not the elevation, not the cylinder blend, and not `_NormalizeNormal` (the mesh's normals are all exactly unit
length, so that setting has nothing to act on). The step's constants match the material and the textures are the
same files.

What is left, in the order worth testing:

1. **The normal field as it arrives.** The viewer uses the game's mesh normals directly; ours come from the game's
   normals through the PMX writer, mmd_tools' importer, and this addon's per-vertex custom-normal write. Comparing
   our face's normals against the game's mesh normals - the pipeline dump already reaches the latter - would show
   whether the round trip preserves them.
2. **`_ToonMap`'s channels.** The step is modulated by its red channel and the regions key off its green. The
   texture is the same file, but the channel *ordering* after the round trip has not been checked.
3. **The light, still not matched exactly.** The viewer's azimuth 0 is its scene light's rest orientation
   (`eulerAngles.y` 190) and this tool's azimuth 0 is the model's facing direction. Those are close but not the same
   vector, and until they are, each comparison carries a constant angular offset.

The cheapest next step is 1, because the diagnostic that reaches the game's own normals already exists
(`-umaDumpFace`).

## 11. A note on method, because it cost time twice

Two conclusions in this session were drawn from comparisons that were not like-for-like, and both had to be
retracted: first that the hard edges were faithful output (they were compared at a light angle the viewer never uses),
then that the artifact was certainly ours (the two tools were placing the light at different elevations). The lesson
is narrow and practical: before comparing two renders, print the vectors and the values that produced them. Every
tool in `Tools/` prints its light direction now.
