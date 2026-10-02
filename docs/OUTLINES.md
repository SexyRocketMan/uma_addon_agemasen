# Uma Outlines: why the model looked inside out, and what the addon does about it

## The symptom

With the `Uma Outlines` geometry-nodes modifier applied, a model exported by the Agemasen fork rendered
inside out: the shading landed on the faces pointing away from the camera and the whole surface came out
washed out white. Without the modifier it looked right, which is why an earlier build of this fork switched
outlines off and removed them. Restoring the modifier is the default again.

The modifier itself was never the problem.

## What the node group actually does

Read out of the shipped `Umashaders.blend` (71 nodes):

```
Named Attribute ("UV3", FLOAT_VECTOR)
  -> Separate Color.001.Green
     -> Math.006            (multiply)
     -> Combine XYZ.001     (g, g, g)
     -> Vector Math.005     (normalize(Position - Camera.Location) * g)
        -> Vector Math.008  (add)
           -> Set Position.001.Offset
              -> Set Material   (Outline Material)
              -> Flip Faces     (inverted shell)
                 -> Join Geometry (with the original mesh)
```

So the shell is expanded along the **view direction**, scaled by the *green* channel of a per-corner
float-vector attribute named `UV3`. That attribute is the **outline normal set**: the game generates the
outline by expanding an inverted model along normals, and because normals tuned for the outline spoil the
shading, the adjusted normals are moved into a spare UV set of the base model so each pass has the normals it
wants (Cygames' character talk, quoted in `docs/GAME_SHADING_RESEARCH.md` §3).

UmaViewer's own exporter writes that set. **The Agemasen export does not.** With nothing in the channel the
expansion is zero, so the flipped shell lands exactly on the surface and the model renders as its own
backfaces - the inside-out look, with `Outline Material` unset on top of it so the shell draws the
character's own shading rather than a dark line.

## What the addon does now

`operators/Umashader.py` is **upstream's operator**, kept as it ships. The owner asked for exactly that after
an earlier rebuild of this tree carried the fork's own shader work (export-comment parameter parsing, the
game-face shading path, a width attribute for the outline and an automatic outline material) - that version
looked different from the original, so it was reverted. Two things are not upstream's: the version-safe write
of the outline modifier's depth offset, and one optional property, both below. Neither changes what the
operator does unless the property is switched on.

* the `Uma Outlines` modifier is added unconditionally, exactly as upstream adds it;
* the only input it writes is `Depth Offset` (`Input_15`, 0.1). Upstream does `geom_mod["Input_15"] = 0.1`,
  which is an ID property on the modifier; Blender 5.2 no longer stores node-group modifier inputs there, so
  that one write goes through `set_modifier_input()`, which uses `modifier.properties.inputs` on 5.2 and the
  ID property on 4.2 through 5.1. Verified: 0.1 lands on both routes;
* by default it sets nothing else, which is upstream's behaviour and what the measurements below describe.

### The "Contain the outline" setting

The Processing Model panel has a **"Contain the outline"** checkbox, **on by default**, and Shading reads it
when it runs. It is a scene setting rather than a property of the operator on purpose: an operator property
has nowhere to appear until the operator has run at least once (F9, Adjust Last Operation), so the first
version of this toggle existed but could not be found in the UI.

Unticked, Shading is upstream's behaviour exactly. Ticked, it does three things and prints each one:

1. **Points the outline at the scene camera** (`Camera`, the socket next to `Depth Offset`). The group's
   direction term is `normalize(Position - Object Info.Location)`, and that `Object Info` node is wired to
   the camera input. Left unset, geometry nodes falls back to the object itself, so the term measures from the
   model's own origin instead of from the camera - a radial offset rather than a view-ray one. Setting the
   camera field in the modifier by hand does the same thing; this does it for you. (Measured: the shell's
   bounding box barely changes either way - 0.08% of the model's height with the toggle off, 0.16% with it on
   - so this is about *where* the shell sits and what it covers, not about it being geometrically larger.)
2. **Gives the shell a width channel it can use.** When `UV3`'s green channel is zero over most of the mesh
   (the packing above), a neutral width of 0.25 is written to `UMA_OutlineWidth` and the group's
   `Named Attribute` node is pointed at it, leaving `UV3` alone. When the export *does* carry a usable set,
   the group is pointed back at `UV3` and nothing is written.
3. **Gives the shell the shipped `Uma Outline Material`**, so a shell that ends up on the model reads as a
   line rather than as the character's shading flipped.

It is a containment, not a correction: it stops the outline from covering the model, but it does not produce
the game's outline, which needs the real normal set at export.

## What the export decides

`.tools/inspect_exports.py <dir or pmx...>` reports this for any export, which is how the table below was
taken from eleven models in `Desktop\testing_agemasen`:

| export | extra UV layers | `UV3` green non-zero | morphs | material comments |
|---|---|---|---|---|
| `1001_Special Week` | `UVMap, UV1, UV2, UV3, _UV3` | 24% | 192, unified | none |
| `special_week_*` v2, v4 | same | 24% | 192, unified | 8 of 8, `uma1 toon_step=…` |
| `special_week_*` v5 - v9 | `UVMap, UV1, UV2, UV3, _UV2, _UV3` | 24% | 192, unified | 8 of 8, `uma1 toon_step=…` |
| `special_week_*` v3 | `UVMap, UV1, UV2, UV3, _UV3` | 24% | 196, unified | none |
| `matikanefukukitaru_…` (older) | `UVMap, UV1, UV2, UV3, _UV2, _UV3` | **0%** | none | 10 of 10, `uma1 outline_width=2 outline=…` |

Three things follow.

* **`UV3` is not an outline normal set in these exports.** Its green channel - the one the Uma Outlines group
  scales its direction offset by - is non-zero on only 24% of corners (mean 0.075), and it looks the same in
  every one of the eleven, so the export iterations did not change it. A stock UmaViewer export puts the real
  outline normal set there. This is the mechanism behind the outline behaving differently with these models,
  and it is why the containment toggle exists.
* **The shader parameters ride in the per-material PMX comments**, not in the model comment:
  `uma1 toon_step=0.4 toon_feather=0.001 specular=1,0.905,0.59,1 env_rate=0.4 rim_step=0.15 …`, and
  `outline_width=` in the older model. Upstream's Shading operator ignores those comments entirely; the fork's
  earlier shader read them to apply the game's own settings (including `outline_width` as the outline
  thickness). That work is reverted, so today the comments are informational only.
* **Two of the eleven carry no comments at all** (`1001_Special Week`, v3 - and v3 is a re-export of a model
  the add-on had already processed, to judge by its `Eye_R(R)` control morphs). Anything that reads those
  comments would silently do nothing on them.

So yes: these exports do change how the shader and the outlines behave, through the extra UV channels and the
material comments. The exporter is where that gets decided - writing the real outline normal set into that
channel is still the fix for a proper outline (see `docs/GAME_SHADING_RESEARCH.md` §3).

## Comparing the versions

`.tools/compare-outlines.ps1` renders the same model with and without that toggle on every installed Blender
and assembles the panels into one labelled strip per mode. Each panel has its Blender version and mode burned
into the picture, so the strip explains itself.

```powershell
pwsh -File .tools/compare-outlines.ps1 -Model "C:\path\to\model.pmx"
```

It builds the package from the tree, gives every Blender a scratch profile with the package and mmd_tools
staged, renders, and prints the same numbers quoted here. `-Resolution`, `-Samples`, `-Yaw` (turn the model
to see the silhouette), `-ExtraBlenders` (a portable copy) and `-SkipContained` are available. The two pieces
can also be run on their own: `.tools/render_outline_compare.py` renders one panel, and
`.tools/build_comparison_strip.py` lays panels side by side.

Everything under `.tools/` is excluded from the built package automatically - Blender's `extension build`
skips dot-prefixed files and directories, which is also how `.git` stays out. (`paths_exclude_patterns` in a
`[build]` manifest section is not honoured by 5.2; tested.)

Typical output on `1002_00.pmx`, 400 px, 25 degrees off the front:

| Blender | upstream | contained |
|---|---|---|
| 4.2.23 | 9.2% of the frame covered by the shell | 0.03% |
| 4.5.14 | 9.2% | 0.03% |
| 5.2.0 | 0.0% | 0.03% |

The contained mode lands in the same place on all three; upstream does not - on 5.2 the shell ends up
somewhere it is not visible at all, while on 4.2/4.5 it covers the model. That difference between versions
is worth knowing before reading anything into one machine's render.

## Measurements on an Agemasen export (1002_00.pmx, 1.65 units tall)

Read from the evaluated geometry and from rendered frames, not from the node graph. These describe what
upstream's own code produces on an Agemasen export, which is why the earlier rebuild tried to change it:

| state | shell displacement | share of frame in the dark outline material |
|---|---|---|
| Outlines off | - | 0.00% (mean luminance 0.593) |
| Outlines on, export's `UV3` | 0.0013 (0.08% of height) | 7.9% - 10% |

`UV3` on that model: 76% of corners have a zero green channel, and its x channel tracks the vertex colour's
red (the packing `vertex_colour_blue()` documents). So the width the group reads is mostly zero, the shell
never leaves the surface, and what the frame shows is the shell's own material lying on the model - which is
what "the model renders inside out / washed out" looked like. Giving the shell a neutral width and its own
material removed that (0.00% coverage), but it also changed how the shading looks overall, so it is not in
this build.

For the record, a width does not by itself draw an outline either: the group's other expansion term pushes
along the view ray *away* from the camera, and its own switches do not help on this export - `Only Depth
Offset = true` deletes the shell outright, and `Enable Distance Scaling` only makes the covering worse (9.4%).

## What is still the exporter's job

A real, visible outline needs the outline normal set in that channel, which is what the game does and what
UmaViewer's own exporter writes: Cygames' talk says the normals adjusted on the outline model are moved into
the base model's UV set, so each pass gets the normals it wants, and the shading normals must stay as they
are. The fix is the one already scoped in `docs/GAME_SHADING_RESEARCH.md`: write that set into the spare
extra UV channel **at export**.

## Checking it

1. Shading a model adds the `Uma Outlines` modifier and writes `Depth Offset` 0.1 into it - through
   `modifier.properties.inputs` on 5.2, through the modifier's ID properties on 4.2 to 5.1. The console says
   `could not set 'Depth Offset' ...` if neither route works, rather than raising.
2. On an Agemasen export the model renders with its flipped shell lying on top of it (about 8% - 10% of the
   frame in the shell's material). That is upstream's behaviour with these exports, left as it is by request.
3. `scene.camera` must be set: the group expands along the view direction, and without a camera it has no
   direction to work with.
