# One place to set the shading of every uma in the scene

The Uma Shader does not read scene lighting: a character's brightness comes from the `* Intensity` inputs of its
`Uma Shader` node group. `Shading` gives **every shader-able material slot its own copy of that material**, so
"Diffuse Intensity" lives in seven to nine places on a typical character (measured: 6 to 9 copies per model over a
30-model sweep) and in four times as many on a scene with four characters. Matching those by hand is what this
replaces.

## What it does

* the **`Uma Sun [DO NOT DELETE]`** empty the shader library brings in carries **one setting per shader value** -
  these are the *global* values, and they are what every character follows by default;
* each character's **own root empty** (the empty mmd_tools makes for the model, named after it: `1004_00`,
  `1001_00`, ...) carries the same settings as *overrides*;
* every material copy of every character carries **a driver per setting** that resolves one against the other:

      socket  <-  this character's value if it has one, otherwise the global value

Turning one global slider therefore moves every uma in the file at once, and a character that should differ gets
an override on just the rows it differs in.

## Using it

Open the **UMA** panel in the 3D viewport sidebar and look for **Shading - All Umas**.

| control | what it does |
|---|---|
| **Hook up the shading control** | Puts the drivers on every copy of every character. `Shading` already does this for you, so this is for a file that was baked, or made before this feature existed |
| **Bake into materials** | Writes the current values into the materials and removes the drivers - do this before exporting or handing the file on |
| **The panel follows the selection** | Select anything that belongs to a character — her root empty (`1004_00`), her mesh, her armature — and the panel shows *her* values. Select nothing (or something else) and it shows the global ones. The two are never on screen together - to reach the global values while a character is selected, select something that is not hers (the sun empty, or nothing at all) |
| **This uma has her own values** | The master switch for the selected character. On: every setting becomes hers, seeded with the global's current numbers — so switching it on changes nothing on screen, and you can then move what you need. Off: every setting goes back to following the global |
| **All umas in this scene** | The global sliders: 12 numbers and 6 colours. Every character without an override follows these |
| the padlock on a row | For one setting only: `LOCKED` = this character follows the global (the row says `global`). Click to give her her own value, which starts from what she shows right now. Click again to go back to following |
| the **↺** on a row | Only shown once a value differs from what it came from. On a character's row it puts that value back to the global's; on a global row it puts the value back to the shader's own default |
| the **↺** on a group header | The same, for a whole group: a character's group goes back to following the global, a global group goes back to the shader's own defaults |

The line under the two buttons says which mode the file is in:

* *Following the global values (252 driver(s))* - the sliders work;
* *Baked into the materials - the sliders do nothing until Hook up is run again* - the values are plain numbers in
  the materials.

### The rows are grouped the way the shader groups them

The settings appear in the shader's own order, under the same six headings, each with a header you can fold away
(the fold is remembered per file, per group, separately for the global list and the character's list):

| group | settings |
|---|---|
| **Light & Shadow** | Light Threshold, Diffuse Intensity, Diffuse Saturation, Diffuse Color, Shaded Intensity, Shaded Saturation, Shaded Color |
| **Metallic** | Metallic Intensity, Metallic Color |
| **Highlight** | Highlight Intensity, Highlight Color |
| **Ambient Rimlight** | Ambient Rimlight Size, Ambient Rimlight Intensity, Ambient Rimlight Color |
| **Rimlight** | Rimlight Size, Rimlight Intensity, Rimlight Color |
| **Emission** | Emission Intensity |

The values are also visible in Blender's own *Properties → Object → Custom Properties* list, in that same order -
but **that list cannot be sectioned**. The only handle Blender offers on a custom property
(`IDPropertyUIManager`) carries `as_dict`, `clear`, `update` and `update_from`, and no grouping, ordering or tab
field of any kind (measured, `shader_property_shape_check.py`). What it does have is insertion order, so the
properties are created in the shader's order and a file saved before that was true is moved into it automatically
the next time *Hook up* runs - which is safe because a driver points at its target by name, not by position.
Grouping is therefore a feature of this panel, not of the properties.

## What it controls, and what it does not

Controlled (18 settings, named after the shader's own inputs):

`Light Threshold`, `Diffuse Intensity`, `Diffuse Saturation`, `Shaded Intensity`, `Shaded Saturation`,
`Metallic Intensity`, `Highlight Intensity`, `Ambient Rimlight Size`, `Ambient Rimlight Intensity`,
`Rimlight Size`, `Rimlight Intensity`, `Emission Intensity`, and the six colours that go with them
(`Diffuse`, `Shaded`, `Metallic`, `Highlight`, `Ambient Rimlight`, `Rimlight`).

Not controlled, on purpose:

* the **five texture inputs** - they are per material by definition, that is what the copies are for;
* **`Toggle If Face`** and **`Emmission Toggle`** - they say *what a part is*, and `Shading` sets them per
  material when it finds the face, emission and control textures;
* **eye materials.** `Shading` gives a material whose name contains "eye" a copy of the library's `Uma Eyes`
  material instead of `Uma Shader`, and that material has **no node group at all** (18 nodes of Mix, Math and
  Image Texture) - so there is no socket for a driver to write to. An eye copy is its own emission setup and the
  control leaves it alone; it is also not counted as a copy, so the driver counts here are for the parts that are
  driven.

## Where the values live, and what travels

The settings are plain custom properties on the objects, so they are saved in the .blend and visible in
*Properties → Object → Custom Properties* (they also carry a proper range and description there, and the colours
are real colour fields). Because they are plain properties and not addon-registered ones, the drivers keep working
even if the extension is switched off: nothing about the mechanism needs the addon once it is in place.

What does **not** travel is a driver. Export to FBX/glTF, or send the .blend to somebody who will not have the
`Uma Sun` empty, and the values have to be in the materials rather than driven - so **Bake into materials** first.
Baking keeps each character's own look, overrides included.

## Things worth knowing

* **the global is one look for the scene.** It is seeded once, from the values the file already shows, and shading
  a second character does not reseed it - otherwise whoever was shaded last would redefine everybody's look.
* **a material shared by two characters can only follow one of them.** `Shading` makes a copy per slot per object,
  so this does not happen in practice, but if a material is shared the control says so in the console and it takes
  the first character's overrides.
* **it costs 36 drivers per copy** (12 numbers + 6 colours × 4 channels - a colour socket is four scalar drivers,
  measured: a single driver on the 4-float socket evaluates to `[0, 0, 0, 0]`). On the models measured that is 216
  to 324 drivers each; creating them takes 0.039 s, and a frame change costs 0.0008 s with them against 0.0005 s
  without.
* **if the sliders do nothing**, check the mode line first: a baked file needs *Hook up*. If the mode says driven
  and a value still does not move, the console names the material and the setting - `uma.shader_hook` reports any
  socket that did not follow its driver instead of failing quietly.

## Where this was measured

`shader_driver_eval_check.py`, `shader_control_check.py`, `shader_colour_driver_check.py`, `shader_look_check.py`,
`shader_look_edges_check.py` and `shader_look_fleet.ps1` in the `UmaViewer-DevTools` repository; the results and the
bugs they caught are written up in `docs/RIGIFY_CHAINS_HANDOFF.md` §9av. The short version: a driver on a shader
group's input socket follows an ID property (0.5 → 0.5, 2.0 → 2.0), the override/inherit expression resolves both
ways, and the reason two earlier probes reported that drivers do not work at all is that a Python write to a custom
property does not tag its datablock - so nothing was ever evaluated. Over a 30-model sweep every model hooked up
clean: 6 to 11 copies each, 216 to 396 drivers, one global slider reaching all of them.

## Face normals that survive morphs

A facial morph deforms the face, and the face's **shading normals** come with it: the field stops being the one the
model was authored with, which a toon shader shows as a shifting terminator and odd shading on the face. The
optional step below keeps the normals still:

* a **hidden static copy** of the face is kept in the scene - same mesh, **no shape keys** and **no armature
  modifier**, so nothing deforms it;
* it **copies the rotation of the head bone** (`Head`) through a `COPY_ROTATION` constraint, so it turns with the
  head and the transferred normals keep facing the right way when the character looks around;
* a **Data Transfer modifier** on the real face takes **corner custom normals** from that copy with **`TOPOLOGY`**
  mapping - corner for corner, which is exact because the copy is the same mesh;
* the modifier is left **live**, not baked, so the copy stays the thing that decides;
* **the face is separated into its own object first** ("Separate the face first", on by default) when it is not
  already one, so the transfer only ever runs over the face's corners. This is Spork's own setup, and it is not
  cosmetic: the transfer's cost is set by the size of the mesh it writes to, and the same model measures **+1.4 ms**
  per evaluation with the face as its own object against **+12.5 ms** for the whole mesh. Limiting the whole-mesh
  transfer with the old `UMA_Face` vertex group saved 0.9 ms of that - a group changes which corners are *written*,
  never the work done per corner - so the group is now only the fallback for when the separation fails;
* shapes are separated as Blender separates them, not rebuilt: the piece keeps the parent (and parent type), the
  collections, every material slot, every vertex group and the same armature modifier, and its vertices come out at
  exactly the coordinates they had. Measured with the head turned 30 degrees and the whole rig turned and moved:
  rest, the deformation and the return to rest are all identical to **0.00e+00 m**, and the two pieces' vertices
  together are exactly the original mesh's vertices.

**It is on by default** ("Face normals that survive morphs" in the Processing Model panel, next to the other
shading options) because the manual setup it automates is the usual one. Untick it to opt out. The row's two
buttons are for doing it by hand: the driver icon re-runs it, the bin removes the copy and the transfer again.
Beside them, two options:

* **"Separate the face first"** - as above. Untick it and the step falls back to the vertex group on one mesh;
* **"No duplicated morphs"** (on by default) - a separated piece keeps the whole shape-key list, so a viewer
  listing the scene's morphs shows every one of them twice: that is the reported *"two sets of identical morphs
  appearing in the viewer"*. The step removes from each piece the keys whose vertex offsets are all zero **there**
  and therefore cannot change it under any value, while keeping every key that moves nothing anywhere on the body so
  no morph name is lost. Measured on a 12,450-vertex model: the body drops 118 keys and the face 36, 236 entries
  where a plain split would list 386, with all 193 names still present.

Three things worth knowing:

* **re-run the step after editing the mesh's topology.** `TOPOLOGY` mapping needs the same corner count on both
  sides, so separating a slot, joining an object in or deleting faces under a live transfer leaves it writing
  garbage - the copy is refreshed automatically on the next run (it is compared with the face and rebuilt when they
  no longer match), but the *next run* is what fixes it;
* **"Remove face normal transfer" leaves the separated face where it is.** Joining it back is one keystroke (select
  the body and the face, `Ctrl`+`J`) and it is reported rather than done, because a silent rejoin would change what
  your export sees;
* **the copy is hidden from the viewport and from renders**, but it is a real object in the file: export with the
  option off, or remove it first, if your export step does not skip hidden objects. Whether a live modifier's
  normals reach an exported PMX/FBX is worth checking per exporter - baking the modifier is the safe answer if it
  does not.

The visual result is the acceptance test here, not a number: this project's normal-field statistic
(`docs/MORPH_ARTEFACT.md`) *was* used to describe the problem, but it ultimately failed as a metric, so there is no
measurement in this repository that says the step helps - look at a morphed face with the option on and off.
