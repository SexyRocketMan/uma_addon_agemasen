"""Face normals that survive morphs: a static copy of the face, and a data transfer from it.

The problem, measured by this project's own research (`docs/MORPH_ARTEFACT.md`): a facial morph deforms the face, so
its *shading normals* move with it - the p90 of the angle between a vertex's normal and its neighbours' goes from
**12° to 18-20°** under a morph - and a toon shader turns that into a visible defect. The mitigation is to stop the
face's normal field from following the morph: keep a **static copy** of the face and transfer its **custom split
normals** onto the real face every evaluation.

The recipe is the user's own, from their manual setup, and is implemented as described rather than reinvented:

1. a **copy of the face object** in the scene;
2. it **follows the head bone**, so it turns with the head - which is what makes the transferred normals keep facing
   the right way when the character looks around. Since 2.33.12 that is a **bone parent** rather than the
   `COPY_ROTATION` constraint the recipe called for: a constraint on an object turns it about that object's own
   origin (the model's feet) and its `LOCAL` spaces read the bone's axes into the object's, so the copy swung around
   the body and turned about the wrong axes. `follow_head` has the reasoning and the measurements;
3. a **Data Transfer modifier** on the real face, `CUSTOM_NORMAL` as loop data, mapping **`TOPOLOGY`** (corner for
   corner, which is exact here because the copy is the same mesh);
4. the copy is **hidden**, and the modifier is left **live** rather than applied.

Measured off the 5.2 RNA rather than assumed (`face_normals_rna_check.py`):

| what the recipe needs | what 5.2 says |
|---|---|
| `data_types_loops` | reported as an enum (`CUSTOM_NORMAL / COLOR_CORNER / UV`), but **assigning a string raises** - the runtime wants a set: `{'CUSTOM_NORMAL'}` |
| `loop_mapping` | `'TOPOLOGY'` is a real value, and the first one |
| `use_loop_data` | **False by default** - must be set, or the modifier does nothing |
| `use_object_transform` | True by default, so the transfer is in world space - which is why the copy's rotation matters |
| `vertex_group` | exists, so the transfer can be **limited to a region** |
| the head bone | is literally `Head` (`Neck`'s child, 79 children of its own) |

That last row is the one that widens the feature: the user's pipeline has the face as its own object, but a plain
mmd_tools import is **one mesh** holding body, face, brows, hair and tail together. Transferring from a copy of the
whole mesh would freeze the *body's* normals too, which would break its shading as the rig deforms it. So the step
finds the face either way:

* **a separate face object** exists (name or materials say `face`/`mayu`) - transfer to it, whole object, exactly as
  the recipe does;
* **the face is a material in a shared mesh** - separate those polygons into their own object first
  (`face_normals_split`, on by default) and transfer to that, which is the shape Spork's own file has. This is not
  only tidier: the transfer's cost is set by the size of the mesh it writes to, and measured on a 12,450-vertex
  model the separated face costs **+1.4 ms** per evaluation against **+12.5 ms** for the whole mesh. Limiting the
  whole-mesh transfer with a vertex group - the previous approach, still the fallback when the separation fails -
  saves 0.9 ms of that, because the group changes which corners are written and not the per-corner work.

A separated piece keeps the whole shape-key list, which is why the step also prunes the keys that cannot move each
piece (`face_normals_prune_morphs`, on by default): the reported *"two sets of identical morphs appearing in the
viewer"* is a split mesh listing every morph twice.
"""

import bpy

# `numpy` for the bulk shape-key comparison (Blender ships it; the `array` fallback is for a stripped build). It is
# the difference between a 3.8 s `Shading` run and a fraction of a second - see `shape_key_effect`.
try:
    import numpy
except ImportError:  # pragma: no cover - every Blender build ships numpy
    numpy = None
from array import array

# The step timings come from the running `Shading` operation, when there is one (`Progress.current()`): this step is
# 94-96% of that run's time, so its own breakdown is the interesting one. Imported defensively - a face-normal run
# must work with that file deleted.
try:
    from . import Progress
except ImportError:  # pragma: no cover - only when that file is deleted
    Progress = None

from bpy.types import Operator

#: The tag reported by the operators and printed when the step runs, so a report can be told from a stale build.
TAG = "face-normals-2"

#: Names the copy and the modifier get, and the custom property that marks the copy as ours.
COPY_SUFFIX = "_normals_static"

#: The collection the static copy lives in - hidden by default, one click from visible in the Outliner.
COPY_COLLECTION_NAME = "UMA Face Normals"
MODIFIER_NAME = "UMA Face Normals"
COPY_MARKER = "uma_face_normals_copy"
FACE_GROUP_NAME = "UMA_Face"

#: What a separated face object is called, and the marker that says the step made it.
FACE_SUFFIX = "_face"
SPLIT_MARKER = "uma_face_normals_split"

#:  Material name fragments that mean "this is the face" - the same vocabulary `Shading` uses to pick materials.
FACE_HINTS = ("face", "mayu")

#: **The eyes travel with the face** (2.33.13). They are their own material (`..._eye`) and their own geometry, sitting
#: on the body, and every blink morph moves the *lid* (a `face` polygon) and the *eyeball* (an `eye` one) - so
#: separating only the face material cut those morphs in two, leaving the same key on both objects with half the
#: geometry on each. Measured over six models with the pieces' own polygons: 18-42 keys straddled the split, every one
#: of them an `Eye_*` blink (`Eye_CloseA/B/C`, `Eye_HalfA/B/C`, `Eye_EyeHideA/B`, `Eye_DereA/B`); with the eye material
#: included, **0** straddle on all six.
#:
#: The hint is `"eye"` alone because that is the shader's own rule - `{"id": "eye", "match": ["eye"], "material":
#: "eye"}` in the shader manifest - so a material is an eye to the separation and to the shading, or to neither.
#: Inventing a wider list here (`瞳`, `iris`, `pupil`) would separate materials the shader would still shade as the
#: default, which is the one combination nobody wants.
EYE_HINTS = ("eye",)

#: The mouth/lip geometries that the same argument applies to, kept out on purpose: a mouth morph moves lip polygons
#: (`Mouth`), which are already `face`-material polygons on every model measured, so there is nothing to add here. If a
#: model turns up whose lips are their own material and whose mouth morphs straddle, this is where it would go.
MOUTH_HINTS = ()

#: Bones that can be the head, in order. Measured on a real skeleton: the bone is `Head` (a child of `Neck`), with
#: `Head_attach`, `Head_height`, `Head_shade_start` and `Head_Handle` as decoys that also match "head".
HEAD_BONE_NAMES = ("Head", "head", "Bip01_Head", "頭", "Head_00")


def is_face_material(material):
    """Is this material part of the face? The shader operator's own naming vocabulary."""
    if material is None:
        return False
    name = material.name.lower()
    return any(hint in name for hint in FACE_HINTS)


def is_face_piece_material(material):
    """Is this material part of the object the face becomes - the face **and the eyes** (2.33.13)?

    Used by the separation and by the vertex group that stands in for it when the separation is off or fails; *not*
    by `face_polygon_count`, which decides whether an object is a face target and keeps its old, narrower meaning.
    """
    if material is None:
        return False
    name = material.name.lower()
    return (any(hint in name for hint in FACE_HINTS) or any(hint in name for hint in EYE_HINTS)
            or any(hint in name for hint in MOUTH_HINTS))


def armature_of(obj):
    """The armature that deforms this object, if any."""
    for modifier in obj.modifiers:
        if modifier.type == 'ARMATURE' and modifier.object is not None:
            return modifier.object
    parent = obj.parent
    while parent is not None:
        if parent.type == 'ARMATURE':
            return parent
        parent = parent.parent
    return next((candidate for candidate in bpy.data.objects if candidate.type == 'ARMATURE'), None)


def head_bone(armature):
    """The head bone of a skeleton: by name first, then the parent of the eyes, then the deepest single chain."""
    if armature is None:
        return None
    for name in HEAD_BONE_NAMES:
        bone = armature.data.bones.get(name)
        if bone is not None:
            return bone
    # the eyes hang off the head, and every uma skeleton has them
    for candidate in armature.data.bones:
        if "eye" in candidate.name.lower() and candidate.parent is not None:
            return candidate.parent
    return None


def face_group_for(mesh_obj, key=FACE_GROUP_NAME):
    """A vertex group holding the face materials' vertices, created or refreshed, or None when there is no face.

    This is the *fallback* way to keep the transfer off the body: it is what the step used before it learned to
    separate the face, and it is still what runs when the separation is switched off or fails. It only limits which
    corners are written - it does not make the transfer any cheaper, which is measured in
    `face_normals_cost_check.py`: on a 12,450-vertex body the group saves 0.9 ms of a 12.5 ms cost.
    """
    vertices = face_vertices(mesh_obj)
    if not vertices:
        return None
    group = mesh_obj.vertex_groups.get(key) or mesh_obj.vertex_groups.new(name=key)
    group.remove(range(len(mesh_obj.data.vertices)))
    group.add(sorted(vertices), 1.0, 'REPLACE')
    return group


def face_vertices(mesh_obj):
    """The vertices of the polygons that *use* a face-piece material (the face and the eyes), or an empty set.

    By polygon, not by material slot, and that is not a detail: separating copies **every** material slot onto both
    pieces, so a slot-based test says the body is still full of face materials after its face polygons have left.
    That is how a separated body would keep getting its own (pointless) transfer with an empty vertex group.

    Widened to the eyes in 2.33.13, together with the separation itself: a blink moves lid *and* eyeball, so the
    group has to cover both or the fallback path keeps half a morph on the body.
    """
    indices = {index for index, slot in enumerate(mesh_obj.material_slots)
               if is_face_piece_material(slot.material)}
    if not indices:
        return set()
    vertices = set()
    for polygon in mesh_obj.data.polygons:
        if polygon.material_index in indices:
            vertices.update(polygon.vertices)
    return vertices


def face_polygon_count(mesh_obj):
    """How many of this object's polygons belong to a face material."""
    indices = {index for index, slot in enumerate(mesh_obj.material_slots)
               if is_face_material(slot.material)}
    if not indices:
        return 0
    return sum(1 for polygon in mesh_obj.data.polygons if polygon.material_index in indices)


def is_whole_face(mesh_obj):
    """Is this object the face and nothing but the face - by the polygons it actually has, not by its slots."""
    total = len(mesh_obj.data.polygons)
    if not total:
        return False
    if "face" in mesh_obj.name.lower() or "mayu" in mesh_obj.name.lower():
        return True
    return face_polygon_count(mesh_obj) == total


def restore_mode(context, obj, mode):
    """Put an object back in the mode it was in, for a step that had to switch. Best effort, never raising.

    Only ever needed on a *failure* path: a step that switches to Edit Mode and back leaves the user where they
    were, but one that raises in between would leave them in Edit Mode while the run carries on and reports success.
    """
    try:
        if obj is not None and obj.mode != mode:
            with context.temp_override(object=obj, active_object=obj, selected_objects=[obj],
                                       selected_editable_objects=[obj]):
                bpy.ops.object.mode_set(mode=mode)
        return obj is not None and obj.mode == mode
    except Exception:  # noqa: BLE001 - a courtesy to the user, not part of the result
        return False


def split_face_into_object(context, mesh_obj, trace=None):
    """Separate the face's polygons into their own object and return it (or None, with the reason).

    This is Spork's shape, and it is the whole point of the option: his static copy feeds a face *object*, so the
    transfer only ever touches the face's corners. On this fork's own pipeline the same model is usually one mesh,
    so the face has to be separated first - with Blender's own operator, chosen deliberately over hand-building a
    mesh: it is exactly what a user doing this by hand gets, which means the vertex groups, the shape keys, the
    material slots, the parenting and the armature modifier come across the way Blender makes them rather than the
    way this add-on guesses.

    What is *not* left as Blender makes it: the new object is named (`.001` is not an answer), it is marked as ours,
    and its shape keys are pruned by `prune_dead_shape_keys` unless that is switched off.

    **The failure path restores the mode, and that is not a nicety.** `mesh.separate` raises
    `RuntimeError: Error: Nothing selected` when nothing is selected - which is the case whenever the face has no
    polygons left to separate, e.g. the face is already its own object and `Shading` is run again - and it raises
    *after* the switch to Edit Mode. Measured before the fix: the shading run finished, reported "Uma shader
    applied", and left the user in `EDIT_MESH` with no hint why; the only sign was a line in the console
    (`shader_side_effects_check.py` reproduces it). So the mode is remembered and put back on every exit.
    """
    before = {obj.name for obj in context.scene.objects}
    # the selection is remembered: separating leaves the new piece selected, and `ApplyShader`'s poll wants exactly
    # one selected object - so a run that ended with two would leave the panel's next operator refusing to run
    # (measured: `bpy.ops.uma.apply_shader.poll() failed, context is incorrect` in face_normals_check.py)
    selection = [obj for obj in context.selected_objects]
    active = context.view_layer.objects.active if getattr(context, "view_layer", None) is not None else None
    mode_before = getattr(mesh_obj, "mode", "OBJECT")
    try:
        with context.temp_override(object=mesh_obj, active_object=mesh_obj, selected_objects=[mesh_obj],
                                   selected_editable_objects=[mesh_obj]):
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='DESELECT')
            bpy.ops.object.mode_set(mode='OBJECT')
            # the piece is the face **and the eyes**: separating the face alone left every blink morph straddling the
            # two objects, because a lid is a `face` polygon and its eyeball is an `eye` one (see `EYE_HINTS`)
            indices = {index for index, slot in enumerate(mesh_obj.material_slots)
                       if is_face_piece_material(slot.material)}
            for polygon in mesh_obj.data.polygons:
                polygon.select = polygon.material_index in indices
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.separate(type='SELECTED')
            bpy.ops.object.mode_set(mode='OBJECT')
    except Exception as error:  # noqa: BLE001 - the caller falls back to the vertex-group transfer
        back = restore_mode(context, mesh_obj, mode_before)
        if trace is not None:
            trace.info("could not separate the face of %r (%s: %s); the transfer stays on the whole mesh, limited "
                       "by a vertex group%s"
                       % (mesh_obj.name, type(error).__name__, error,
                          "" if back else "; and the mode could not be put back either"))
        return None

    new = [obj for obj in context.scene.objects if obj.name not in before]
    if len(new) != 1:
        if trace is not None:
            trace.info("separating %r produced %d object(s), expected 1" % (mesh_obj.name, len(new)))
        return None
    piece = new[0]
    try:
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in selection:
            obj.select_set(True)
        if active is not None:
            context.view_layer.objects.active = active
    except Exception:  # noqa: BLE001 - the selection is a courtesy, not part of the result
        pass
    piece.name = unique_name(mesh_obj.name + FACE_SUFFIX)
    if piece.data is not None:
        piece.data.name = piece.name
    piece[SPLIT_MARKER] = True
    piece["uma_face_normals_split_of"] = mesh_obj.name
    mesh_obj["uma_face_normals_split_into"] = piece.name
    if trace is not None:
        trace.info("separated the face of %r into %r: %d polygon(s), %d vertex/vertices, %d vertex group(s), "
                   "parent %s, collections %s"
                   % (mesh_obj.name, piece.name, len(piece.data.polygons), len(piece.data.vertices),
                      len(piece.vertex_groups), getattr(piece.parent, "name", None),
                      [collection.name for collection in piece.users_collection]))
    return piece


def unique_name(wanted):
    """`wanted`, or `wanted.001` and up, so a second run cannot silently join two different objects."""
    if wanted not in bpy.data.objects:
        return wanted
    index = 1
    while "%s.%03d" % (wanted, index) in bpy.data.objects:
        index += 1
    return "%s.%03d" % (wanted, index)


def shape_key_effect(obj, key, basis):
    """How many vertices this shape key moves on this object, and by how much at most (metres).

    `ShapeKeyPoint.co` is the vertex's **absolute** position for that key, not an offset, so the offset is against
    the `Basis` key. Reading `co` as the offset makes every key look like it moves every vertex, which is how the
    first version of the analysis behind this function convinced itself that no key could ever be pruned.

    **Why it is written in bulk, measured.** Reading `co` one point at a time in Python is 164 keys x ~8 000 points
    x 3 components of attribute lookups on the reference model, and it cost **3.84 s** - 82% of an entire `Shading`
    run, and the largest single cost anywhere in this add-on (`docs/PERFORMANCE.md`, `step_timing_check.py`). Both
    coordinate arrays are now read in one C call each (`foreach_get`) and compared with numpy, which Blender ships;
    the arithmetic is the same, and `step_timing_check.py` checks the two agree key by key on a real model rather
    than trusting that.
    """
    points = len(key.data)
    if points == 0 or len(basis.data) != points:
        return 0, 0.0
    if numpy is not None:
        current = numpy.empty(points * 3, dtype=numpy.float32)
        rest = numpy.empty(points * 3, dtype=numpy.float32)
    else:  # pragma: no cover - Blender ships numpy; the fallback is for a stripped build
        current = array("f", [0.0]) * (points * 3)
        rest = array("f", [0.0]) * (points * 3)
    key.data.foreach_get("co", current)
    basis.data.foreach_get("co", rest)
    if numpy is not None:
        delta = numpy.abs(current - rest).reshape(-1, 3).sum(axis=1)
        moved = int(numpy.count_nonzero(delta > 1e-9))
        return moved, (float(delta.max()) if moved else 0.0)
    moved, biggest = 0, 0.0
    for index in range(0, len(current), 3):
        offset = abs(current[index] - rest[index]) + abs(current[index + 1] - rest[index + 1]) \
            + abs(current[index + 2] - rest[index + 2])
        if offset > 1e-9:
            moved += 1
            biggest = max(biggest, offset)
    return moved, biggest


def dead_shape_keys(obj):
    """The names of this object's shape keys that cannot move it: every offset they store is zero here.

    `Basis` is never one of them, and a key that moves *nothing* anywhere is still a name the model has - so the
    caller decides what to do with those.
    """
    keys = getattr(obj.data, "shape_keys", None)
    if keys is None or len(keys.key_blocks) < 2:
        return []
    basis = keys.key_blocks.get("Basis") or keys.key_blocks[0]
    dead = []
    for key in list(keys.key_blocks):
        if key == basis or key.name == "Basis":
            continue
        moved, _biggest = shape_key_effect(obj, key, basis)
        if moved == 0:
            dead.append(key.name)
    return dead


def remove_shape_keys(obj, names, trace=None):
    """Remove these keys from this object, and return how many went."""
    keys = getattr(obj.data, "shape_keys", None)
    if keys is None or not names:
        return 0
    wanted = set(names)
    removed = 0
    for key in list(keys.key_blocks):
        if key.name in wanted and key.name != "Basis":
            # `Object.shape_key_remove` rather than poking the key collection: it is the documented call and it
            # takes the key's driver channel with it
            obj.shape_key_remove(key)
            removed += 1
    if trace is not None and removed:
        trace.info("removed %d shape key(s) from %r that cannot move it (%s%s)"
                   % (removed, obj.name, ", ".join(sorted(wanted)[:5]), ", ..." if len(wanted) > 5 else ""))
    return removed


def prune_split_morphs(body, piece, trace=None):
    """Prune the morphs each piece cannot use, without losing a morph *name* the model has.

    A separated piece keeps the whole shape-key list, so a viewer listing the scene's morphs shows each one twice -
    the reported *"two sets of identical morphs appearing in the viewer"*. The rule:

    * the **face** drops every key that cannot move it (they belong to the body, which keeps them);
    * the **body** drops every key that cannot move it **and** can still move the face - so a key that moves nothing
      anywhere stays on the body once, and no name disappears from the model.

    Measured on a 12,450-vertex model: the face drops 36 keys and the body 114, leaving 157 + 79 = 236 entries
    where a naive split would list 386, with the same 193 names present as before. This step is **82% of a `Shading`
    run** (3.8s of 4.6s on the reference model), so its two halves are timed apart: deciding which keys are dead
    (a walk over every key's influence) and removing them (one call per key, each of which re-evaluates the mesh).
    """
    steps = Progress.current() if Progress is not None else None
    if steps:
        with steps.phase("the face step: work out which morphs are dead"):
            dead_body = set(dead_shape_keys(body))
            dead_face = set(dead_shape_keys(piece))
    else:
        dead_body = set(dead_shape_keys(body))
        dead_face = set(dead_shape_keys(piece))
    if steps:
        with steps.phase("the face step: remove the dead morphs (%d + %d)"
                         % (len(dead_face), len(dead_body - dead_face))):
            removed_face = remove_shape_keys(piece, dead_face, trace)
            removed_body = remove_shape_keys(body, dead_body - dead_face, trace)
    else:
        removed_face = remove_shape_keys(piece, dead_face, trace)
        removed_body = remove_shape_keys(body, dead_body - dead_face, trace)
    return {"face_keys_removed": removed_face, "face_keys_left": len(piece.data.shape_keys.key_blocks)
            if piece.data.shape_keys else 0,
            "body_keys_removed": removed_body, "body_keys_left": len(body.data.shape_keys.key_blocks)
            if body.data.shape_keys else 0,
            "keys_dead_on_both_pieces_left_on_the_body": len(dead_body & dead_face)}


def find_copy(face_obj):
    """The copy this step made for `face_obj` before, if it is still in the file."""
    for obj in bpy.data.objects:
        if obj.get(COPY_MARKER) and obj.get("uma_face_normals_for") == face_obj.name:
            return obj
    return bpy.data.objects.get(face_obj.name + COPY_SUFFIX)


def copy_collection(face_obj):
    """The collection the static copy lives in: hidden by default, one click from being visible.

    Its own collection rather than the model's, because that is the toggle a user will look for, and because it
    keeps the copy out of the way of the character's own collection while still being findable in the Outliner.
    """
    collection = bpy.data.collections.get(COPY_COLLECTION_NAME)
    if collection is None:
        collection = bpy.data.collections.new(COPY_COLLECTION_NAME)
        # linked under the scene, and under the character's own collection when it has one, so it stays with the
        # model when the scene is organised
        parent = face_obj.users_collection[0] if face_obj.users_collection else None
        if parent is not None:
            try:
                parent.children.link(collection)
            except RuntimeError:
                bpy.context.scene.collection.children.link(collection)
        else:
            bpy.context.scene.collection.children.link(collection)
    collection.hide_viewport = True
    collection.hide_render = True
    return collection


def follow_head(copy, armature, head):
    """Make the static copy follow the head bone **rigidly**. Returns a short note for the trace.

    Three properties are needed, and the constraint this replaces got one right and two wrong:

    * **the pivot is the head bone's head**, not the object's origin. A `COPY_ROTATION` on an *object* turns it about
      that object's own origin, and the copy's origin sits wherever the face object's does - the model's feet - so
      turning the head swept the copy around the whole body. The user: *"it doesn't rotate in place, but rather on a
      huge sphere"*.
    * **the frame is the bone's own**, so the copy turns the way the head turns. `COPY_ROTATION` with `LOCAL` on both
      sides was picked because it is the only setting that is identity at rest (0.00 deg, measured by
      `face_normals_rest_check.py`, and that identity is what keeps the transferred normals the face's own). But
      `LOCAL` reads the *bone's* axes and writes them into the *object's* axes: same angle, wrong axes. The user:
      *"even with the pivot point fixed, the rotation is not matching"*.
    * **identity at rest**, which is the property the whole feature exists for.

    A bone parent gives all three, because it inherits the bone's transform *as a transform* instead of replacing an
    orientation - and it needs no constraint at all, so there is no space left to get wrong.

    **The order of the last two lines is the whole trick**, and this is what `face_normals_rest_check.py` variant 5
    got wrong: assigning `matrix_world` and *then* overwriting `matrix_parent_inverse` moves the copy somewhere else,
    which is why that variant measured the same 72.74 deg as the world-space constraints and why bone parenting was
    written off. Parent fields, then the inverse, then the world matrix.
    """
    # a copy made by an older build still carries the constraint this replaces - remove it either way, so a re-run
    # upgrades the copy in place instead of stacking a parent on top of a constraint
    for constraint in list(copy.constraints):
        if constraint.type == 'COPY_ROTATION':
            copy.constraints.remove(constraint)
    if armature is None or head is None:
        copy.parent = None
        return "no head bone in this rig, so it does not follow one"
    world = copy.matrix_world.copy()
    # `pose.bones[...].matrix` is only as fresh as the last evaluation
    bpy.context.view_layer.update()
    copy.parent = armature
    copy.parent_type = 'BONE'
    copy.parent_bone = head.name
    copy.matrix_parent_inverse = (armature.matrix_world @ armature.pose.bones[head.name].matrix).inverted()
    copy.matrix_world = world
    return "bone-parented to the head bone %s" % head.name


def make_copy(context, face_obj, armature, head, trace=None):
    """The static copy: same mesh, no shape keys, not skinned, turning with the head, hidden."""
    copy = face_obj.copy()
    copy.data = face_obj.data.copy()
    copy.name = face_obj.name + COPY_SUFFIX
    copy.data.name = copy.name
    for collection in face_obj.users_collection:
        collection.objects.link(copy)
    copy[COPY_MARKER] = True
    copy["uma_face_normals_for"] = face_obj.name
    # the copy inherits every custom property of the face, including the markers of the split that made it - which
    # is how a copy came to look like a separated face piece to anything scanning for `SPLIT_MARKER`
    for key in (SPLIT_MARKER, "uma_face_normals_split_of", "uma_face_normals_split_into"):
        if key in copy.keys():
            del copy[key]

    # static with respect to *morphs*: the shape keys go, so nothing moves the copy's geometry.
    copy.shape_key_clear()
    # **every** modifier goes, not only the armature one. The copy is made *after* `Shading` has put its own
    # `Uma Outlines` geometry-nodes modifier on the face, so a copy that kept it would be the *shelled* mesh - twice
    # the face corners - while the destination at the transfer's position is the plain one. That is the reported
    # error word for word:
    #   ERROR Source and destination meshes do not have the same number of face corners, 'Topology' mapping cannot
    #   be used in this case
    # and it is why the failure only appeared in a shaded scene: on a plain import there is no outline modifier to
    # inherit, and the two sides measure equal (76,584 corners each).
    for modifier in list(copy.modifiers):
        copy.modifiers.remove(modifier)

    # **It follows the head by being bone-parented, not by a constraint** (2.33.12). This is where a
    # `COPY_ROTATION` used to be made, in `LOCAL` spaces on both sides: chosen because that was the only setting that
    # was identity at rest (0.00 deg, measured by `face_normals_rest_check.py`, against 72.74 deg for the world-space
    # variants), and that identity is what keeps the transferred normals the face's own. Two things it cost, both
    # reported by the user afterwards: the pivot was the *object's* origin - the model's feet - so the copy swept
    # around the body instead of turning in place, and `LOCAL` reads the bone's axes into the object's axes, so the
    # rotation came out about the wrong ones. `follow_head` above has the reasoning and the fix.
    follow = follow_head(copy, armature, head)

    # Hidden in the **collection**, not on the object, and this is the second reported bug: neither the object's
    # eye (`hide_set`, a per-view-layer hide that overrides the eye) nor `hide_viewport` (the Outliner's *monitor*
    # icon) is the flag a user reaches for first, and setting either one leaves "I couldn't get it to appear even
    # after enabling viewport visibility" true. The copy goes into its own collection whose viewport and render
    # flags are off, so it is out of the way by default.
    #
    # But finding it again was *also* hard - the user, 2.33.12: *"it's hard to access the static face copy due to how
    # it's hidden - you can't just unhide it using the ui viewport/render visibility... it should be easier to
    # access, since it can be a source of many issues"*. A disabled collection is a monitor-icon in one specific
    # Outliner row, in whatever collection the model happens to live under, so `FaceNormalsReveal` now does it from
    # the Shading panel: one click enables the collection, selects the object and shows it. The default stays hidden
    # - it is a copy nobody wants to see while posing - but it is no longer *lost*.
    collection = copy_collection(face_obj)
    for other in list(copy.users_collection):
        if other is not collection:
            other.objects.unlink(copy)
    if copy.name not in collection.objects:
        collection.objects.link(copy)
    if copy.parent is None and face_obj.parent is not None:
        # keep it in the same place as the object it copies, without inheriting what makes that object move
        copy.matrix_world = face_obj.matrix_world
    if trace is not None:
        trace.info("static copy %r: no shape keys, no armature modifier, %s, hidden (FaceNormalsReveal shows it)"
                   % (copy.name, follow))
    return copy


def make_modifier(face_obj, copy, group=None, context=None):
    """The transfer: corner custom normals, topology mapping, from the copy.

    **It goes at the top of the stack**, and that is not cosmetic. `TOPOLOGY` mapping maps corner for corner and
    requires that source and destination have the same topology - and `Shading` puts the **`Uma Outlines`
    geometry-nodes modifier** on the mesh *before* this step runs, which **adds a shell** (it duplicates the geometry
    to draw the line). A transfer appended after it sees twice the loops the copy has, which is the reported
    *"data transfer modifier reports an error with mismatched topology after I change the value of the morph"*. At
    the top of the stack the destination is still the plain mesh, so the counts match: measured on a plain import,
    76,584 loops on both sides and the transfer takes.
    """
    modifier = face_obj.modifiers.get(MODIFIER_NAME)
    if modifier is None:
        modifier = face_obj.modifiers.new(MODIFIER_NAME, 'DATA_TRANSFER')
        # `object` stays unset until after the move: a new modifier is *appended*, and with a source set it is
        # evaluated once down there - after Shading's outline modifier, on twice the corners - which prints
    # `ERROR Source and destination meshes do not have the same number of face corners` before the move fixes it.
    # With no source the modifier does nothing, so moving it is silent.
    modifier.use_loop_data = True
    modifier.data_types_loops = {'CUSTOM_NORMAL'}
    modifier.loop_mapping = 'TOPOLOGY'
    modifier.mix_mode = 'REPLACE'
    modifier.mix_factor = 1.0
    modifier.use_max_distance = False
    modifier.use_object_transform = True
    # `vertex_group` is what lets a shared mesh keep its body normals: measured to exist on 5.2, and empty means
    # the whole object
    modifier.vertex_group = group.name if group is not None else ""
    modifier.invert_vertex_group = False
    if context is not None and face_obj.modifiers[0].name != MODIFIER_NAME:
        try:
            with context.temp_override(object=face_obj, active_object=face_obj,
                                       selected_objects=[face_obj],
                                       selected_editable_objects=[face_obj]):
                bpy.ops.object.modifier_move_to_index(modifier=MODIFIER_NAME, index=0)
        except Exception as error:  # noqa: BLE001 - neither the panel nor Shading may die for this
            print("[uma_addon] %s: could not move %r to the top of the stack (%s: %s); a transfer that reports "
                  "mismatched topology is what that costs" % (TAG, MODIFIER_NAME, type(error).__name__, error))
    # the source, last: see the note above about the evaluated-once-in-the-wrong-place error
    modifier.object = copy
    return modifier


def copy_topology_mismatch(copy, face_obj):
    """Why the existing copy can no longer feed a `TOPOLOGY` transfer, or `""` when it still can.

    `TOPOLOGY` mapping needs the same topology on both sides, and a mesh is edited all the time: separating a
    material slot, joining another object into it, deleting faces, running a decimate. The step used to reuse the
    copy's mesh data as it was, so a copy made *before* such an edit stayed on the old topology for ever and the
    transfer kept writing wrong normals - and pressing **Apply face normals** again did not repair it, because the
    copy was simply "reused". Measured (`shader_black_check.py`): separating one material slot took the destination
    from 76,584 corners to 75,420 while the copy stayed at 76,584, which is the reported
    *"Source and destination meshes do not have the same number of face corners"* and, for a toon shader, a face
    whose shading no longer means anything.
    """
    if copy.data is None:
        return "the copy has no mesh"
    if len(copy.data.vertices) != len(face_obj.data.vertices):
        return "%d copy vertices against %d on the face" % (len(copy.data.vertices),
                                                            len(face_obj.data.vertices))
    if len(copy.data.loops) != len(face_obj.data.loops):
        return "%d copy corners against %d on the face" % (len(copy.data.loops), len(face_obj.data.loops))
    if copy.data.shape_keys is not None:
        return "the copy has shape keys again"
    return ""


def refresh_copy_data(copy, face_obj):
    """Give the copy the face's *current* mesh and keep everything else about it: constraint, collection, name.

    The same three things `make_copy` does to a new copy - fresh data, no shape keys, no modifiers - applied to an
    existing one, so a re-run after a topology edit repairs the transfer instead of leaving it broken.
    """
    copy.data = face_obj.data.copy()
    copy.data.name = copy.name
    copy.shape_key_clear()
    for modifier in list(copy.modifiers):
        copy.modifiers.remove(modifier)
    return copy


def faces_in(context, objects=None):
    """The objects this step should act on, with the reason each was chosen.

    The test is the polygons the object actually has, not its material slots: separating a mesh copies **every**
    slot onto both pieces, so a slot-based test keeps calling the body a face object long after its face polygons
    have left, and the body then gets a transfer with an empty vertex group - cost with no effect.
    """
    if objects is None:
        objects = [obj for obj in context.scene.objects if obj.type == 'MESH']
    found = []
    for obj in objects:
        # the static copy is ours and is not a target; the *separated face* is a target like any other - it is
        # excluded from being split again by `is_whole_face`, not by its marker, so that a second run still sets its
        # transfer up
        if obj.get(COPY_MARKER):
            continue
        polygons = len(obj.data.polygons)
        face_polygons = face_polygon_count(obj)
        named_face = "face" in obj.name.lower() or "mayu" in obj.name.lower()
        if named_face:
            found.append((obj, "its name says face"))
        elif polygons and face_polygons == polygons:
            found.append((obj, "every polygon of it uses a face material"))
        elif face_polygons:
            # the count is the face material's alone (this decides whether the object *is* a face target, and that
            # meaning has not changed); say how much the *piece* will take, eyes included, because that is what the
            # run then reports and the two numbers differ by the eye material - measured 1946 against 1994 on the
            # reference model, and a reader comparing them would otherwise think something was lost
            piece_polygons = sum(1 for polygon in obj.data.polygons
                                 if polygon.material_index < len(obj.material_slots)
                                 and is_face_piece_material(obj.material_slots[polygon.material_index].material))
            found.append((obj, "%d of its %d polygon(s) use a face material, %d with the eyes"
                          % (face_polygons, polygons, piece_polygons)))
    return found


def split_off_face(context, face_obj, trace=None):
    """Separate `face_obj`'s face polygons into their own object and prune both pieces' dead morphs.

    Both halves used to be options (`face_normals_split`, `face_normals_prune_morphs`) and both are always on now
    (2.23.1): the separation is what makes the transfer cost the *face's* corners rather than the whole body's, and the
    pruning is what stops a viewer listing every morph twice, so neither was a choice worth offering.

    The two are timed separately when a `Shading` run is going on, and the split is worth having: together they are
    ~92% of that run's whole cost (4.0s of 4.4s), and `step_timing_check.py` reads which half. `bpy.ops.mesh.separate`
    duplicates every shape key of the source mesh into the new object before either side is pruned, so the two are
    expected to be of the same order - the measurement says which, and the answer is in `docs/PERFORMANCE.md`.
    """
    steps = Progress.current() if Progress is not None else None
    if steps:
        with steps.phase("the face step: separate (the operator, with every morph)"):
            piece = split_face_into_object(context, face_obj, trace)
    else:
        piece = split_face_into_object(context, face_obj, trace)
    if piece is None:
        return None, "the face could not be separated"
    if steps:
        with steps.phase("the face step: prune the dead morphs"):
            morphs = prune_split_morphs(face_obj, piece, trace)
    else:
        morphs = prune_split_morphs(face_obj, piece, trace)
    return piece, morphs


def apply_face_normals(context, objects=None, trace=None):
    """Run the whole step, idempotently. Returns a report dict.

    Timed from the inside when a shading run is going on (`Progress.current()`), because this step is **94-96% of a
    `Shading` run's time** - 4.2s of 4.4s on the reference model, measured by `step_timing_check.py` - and "the face
    step is slow" is only actionable once it says *which* part: the separation (an operator call on the whole face),
    the static copy (a full mesh copy plus every shape key), or the transfer modifier. A standalone call - the panel's
    own `Apply face normals` button - gets None and simply records nothing.
    """
    report = {"tag": TAG, "targets": [], "notes": []}
    steps = Progress.current() if Progress is not None else None

    def phase(label):
        return steps.phase(label) if steps else _null_phase()

    for face_obj, why in faces_in(context, objects):
        entry = {"object": face_obj.name, "why": why}
        if not is_whole_face(face_obj):
            # Spork's shape: the face becomes its own object, so the transfer only ever touches the face's corners.
            # Measured (+12.5 ms against +1.4 ms per evaluation) - see `split_face_into_object`.
            with phase("the face step: separate the face and prune its morphs"):
                piece, note = split_off_face(context, face_obj, trace)
            entry["split"] = note if piece is None else {
                "into": piece.name, "polygons": len(piece.data.polygons),
                "vertices": len(piece.data.vertices),
                "parent": getattr(piece.parent, "name", None),
                "parent_type": piece.parent_type,
                "collections": [collection.name for collection in piece.users_collection],
                "vertex_groups": len(piece.vertex_groups),
                "morphs": note,
            }
            if piece is None:
                report["notes"].append("%s: %s" % (face_obj.name, note))
            else:
                face_obj = piece
                entry["object"] = piece.name
        armature = armature_of(face_obj)
        head = head_bone(armature)
        entry["armature"] = armature.name if armature else None
        entry["head"] = head.name if head else None
        if armature is None or head is None:
            entry["skipped"] = "no armature or no head bone to follow"
            report["notes"].append("%s: %s" % (face_obj.name, entry["skipped"]))
            report["targets"].append(entry)
            continue
        # is the face the whole object, or one material among several?
        whole_object = is_whole_face(face_obj)
        group = None if whole_object else face_group_for(face_obj)
        if not whole_object and group is None:
            entry["skipped"] = "no face material found on it"
            report["targets"].append(entry)
            continue
        entry["limited_to_group"] = None if group is None else group.name
        with phase("the face step: the static copy"):
            copy = find_copy(face_obj)
            if copy is None:
                copy = make_copy(context, face_obj, armature, head, trace)
                entry["copy"] = "created"
            else:
                # reuse it, but not blindly: if the face's topology has changed since the copy was made (a separated
                # slot, a join, deleted faces), the copy's mesh is refreshed from the face so the transfer maps again
                mismatch = copy_topology_mismatch(copy, face_obj)
                if mismatch:
                    refresh_copy_data(copy, face_obj)
                    entry["copy"] = "reused, mesh refreshed (%s)" % mismatch
                    report["notes"].append("%s: the copy's mesh was refreshed (%s)" % (face_obj.name, mismatch))
                    if trace is not None:
                        trace.info("refreshed the static copy of %r: %s" % (face_obj.name, mismatch))
                else:
                    entry["copy"] = "reused"
                # ...and how it follows the head is re-applied every run, because a copy made by an older build
                # follows it through a constraint 2.33.12 replaced. This also repairs a copy whose parent was lost.
                follow_head(copy, armature, head)
        with phase("the face step: the transfer modifier"):
            modifier = make_modifier(face_obj, copy, group, context)
        entry["modifier"] = modifier.name
        entry["settings"] = {"object": copy.name, "use_loop_data": modifier.use_loop_data,
                             "data_types_loops": modifier.data_types_loops,
                             "loop_mapping": modifier.loop_mapping,
                             "vertex_group": modifier.vertex_group}
        report["targets"].append(entry)
    return report


class _null_phase:
    """`with phase(...)` when there is no run to record into - a no-op that still reads like the timed version."""

    def __enter__(self):
        return None

    def __exit__(self, kind, value, tb):
        return False


# --- operators -----------------------------------------------------------------------------------------

class FaceNormalsApply(Operator):
    """Set up the static face copy and its normal transfer on every face in the scene"""
    bl_idname = "uma.face_normals_apply"
    bl_label = "Set up face normal transfer"
    bl_description = ("Keeps a static copy of the face, turning with the head, and transfers its custom split "
                      "normals onto the face so morphs stop changing the shading. Shading does this for you when "
                      "the option is on; this button is for doing it again by hand")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        report = apply_face_normals(context)
        if not report["targets"]:
            self.report({'ERROR'}, "No face found: no object named face/mayu and no face material on any mesh")
            return {'CANCELLED'}
        done = [entry for entry in report["targets"] if not entry.get("skipped")]
        if not done:
            self.report({'WARNING'}, "Nothing to transfer: %s" % "; ".join(
                entry.get("skipped", "") for entry in report["targets"]))
            return {'CANCELLED'}
        self.report({'INFO'}, "%s: %s" % (TAG, "; ".join(
            "%s %s a copy, transfer on %s%s" % (entry["object"], entry["copy"], entry["modifier"],
                                                "" if not entry.get("limited_to_group")
                                                else " limited to %s" % entry["limited_to_group"])
            for entry in done)))
        for note in report["notes"]:
            print("[uma_addon] %s: %s" % (TAG, note))
        return {'FINISHED'}


def layer_collection(context, name):
    """The view layer's own entry for a collection, which is where `exclude`/`hide_viewport` actually live.

    `collection.hide_viewport` is the *data* flag (the monitor icon); a collection can also be excluded from a view
    layer, and a collection that is excluded has no eye to click at all. Both have to be cleared for the collection's
    contents to appear, and a user hunting for the copy hits whichever one is in the way.
    """
    def walk(layer):
        if layer.collection.name == name:
            return layer
        for child in layer.children:
            found = walk(child)
            if found is not None:
                return found
        return None
    return walk(context.view_layer.layer_collection)


class FaceNormalsReveal(Operator):
    """Show, select and frame the static face copy - the thing that decides face shading and is easy to lose"""
    bl_idname = "uma.face_normals_reveal"
    bl_label = "Find the static face copy"
    bl_description = ("Reveal the static face copy: enable the collection it is hidden in, select it, and show it in "
                      "the viewport. Run it again to hide it back the way it was. The copy is what the transfer reads "
                      "its normals from, so it is the first thing to look at when face shading is wrong")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        copies = [obj for obj in bpy.data.objects if obj.get(COPY_MARKER)]
        if not copies:
            self.report({'WARNING'}, "%s: there is no static copy in this file - set the transfer up first" % TAG)
            return {'CANCELLED'}
        collection = bpy.data.collections.get(COPY_COLLECTION_NAME)
        # the collection's own state is the toggle: showing it again is a second click on the same button
        hidden = collection is not None and collection.hide_viewport
        if collection is not None:
            collection.hide_viewport = not hidden
            layer = layer_collection(context, collection.name)
            if layer is not None:
                layer.exclude = False
                layer.hide_viewport = False
        if hidden:
            for obj in copies:
                obj.hide_set(False)
                obj.hide_viewport = False
                obj.hide_render = True
            for obj in context.scene.objects:
                obj.select_set(False)
            copies[0].select_set(True)
            context.view_layer.objects.active = copies[0]
            self.report({'INFO'}, "%s: showing %s (%d cop%s) - press it again to hide it"
                        % (TAG, copies[0].name, len(copies), "y" if len(copies) == 1 else "ies"))
        else:
            for obj in copies:
                obj.select_set(False)
            self.report({'INFO'}, "%s: hid %d cop%s again" % (TAG, len(copies), "y" if len(copies) == 1 else "ies"))
        return {'FINISHED'}


class FaceNormalsRemove(Operator):
    """Take the copies and the transfer out again"""
    bl_idname = "uma.face_normals_remove"
    bl_label = "Remove face normal transfer"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        removed = {"modifiers": 0, "copies": 0, "groups": 0}
        for obj in list(bpy.data.objects):
            modifier = obj.modifiers.get(MODIFIER_NAME) if obj.type == 'MESH' else None
            if modifier is not None:
                obj.modifiers.remove(modifier)
                removed["modifiers"] += 1
            if obj.get(COPY_MARKER):
                bpy.data.objects.remove(obj, do_unlink=True)
                removed["copies"] += 1
        for mesh_obj in [obj for obj in bpy.data.objects if obj.type == 'MESH']:
            group = mesh_obj.vertex_groups.get(FACE_GROUP_NAME)
            if group is not None and mesh_obj.modifiers.get(MODIFIER_NAME) is None:
                mesh_obj.vertex_groups.remove(group)
                removed["groups"] += 1
        # A separated face is **left** where it is. Joining it back would silently change what the user's export
        # sees, and it is one keystroke away - so it is reported rather than done.
        split = [obj.name for obj in bpy.data.objects if obj.get(SPLIT_MARKER)]
        note = ("; %d face object(s) the step separated are left in place (%s) - select the body and the face and "
                "press Ctrl+J to make one mesh again" % (len(split), ", ".join(split[:3]))) if split else ""
        self.report({'INFO'}, "%s: removed %d transfer(s), %d copy/copies, %d group(s)%s"
                    % (TAG, removed["modifiers"], removed["copies"], removed["groups"], note))
        return {'FINISHED'}
