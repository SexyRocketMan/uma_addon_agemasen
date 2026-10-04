"""Face normals that survive morphs: a static copy of the face, and a data transfer from it.

The problem, measured by this project's own research (`docs/MORPH_ARTEFACT.md`): a facial morph deforms the face, so
its *shading normals* move with it - the p90 of the angle between a vertex's normal and its neighbours' goes from
**12° to 18-20°** under a morph - and a toon shader turns that into a visible defect. The mitigation is to stop the
face's normal field from following the morph: keep a **static copy** of the face and transfer its **custom split
normals** onto the real face every evaluation.

The recipe is the user's own, from their manual setup, and is implemented as described rather than reinvented:

1. a **copy of the face object** in the scene;
2. it **copies the rotation of the head bone** through a `COPY_ROTATION` constraint, so it turns with the head -
   which is what makes the transferred normals keep facing the right way when the character looks around;
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
* **the face is a material in a shared mesh** - build a vertex group of the face materials' vertices and point the
  modifier's `vertex_group` at it, so only those corners take the static normals.
"""

import bpy

from bpy.types import Operator

#: The tag reported by the operators and printed when the step runs, so a report can be told from a stale build.
TAG = "face-normals-1"

#: Names the copy and the modifier get, and the custom property that marks the copy as ours.
COPY_SUFFIX = "_normals_static"
MODIFIER_NAME = "UMA Face Normals"
COPY_MARKER = "uma_face_normals_copy"
FACE_GROUP_NAME = "UMA_Face"

#: Material name fragments that mean "this is the face" - the same vocabulary `Shading` uses to pick materials.
FACE_HINTS = ("face", "mayu")

#: Bones that can be the head, in order. Measured on a real skeleton: the bone is `Head` (a child of `Neck`), with
#: `Head_attach`, `Head_height`, `Head_shade_start` and `Head_Handle` as decoys that also match "head".
HEAD_BONE_NAMES = ("Head", "head", "Bip01_Head", "頭", "Head_00")


def is_face_material(material):
    """Is this material part of the face? The shader operator's own naming vocabulary."""
    if material is None:
        return False
    name = material.name.lower()
    return any(hint in name for hint in FACE_HINTS)


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

    This is what makes the step work on a model whose face is not a separate object: the transfer is limited to
    these vertices, so the body's own normals keep following its deformation.
    """
    indices = {index for index, slot in enumerate(mesh_obj.material_slots)
               if is_face_material(slot.material)}
    if not indices:
        return None
    vertices = set()
    for polygon in mesh_obj.data.polygons:
        if polygon.material_index in indices:
            vertices.update(polygon.vertices)
    if not vertices:
        return None
    group = mesh_obj.vertex_groups.get(key) or mesh_obj.vertex_groups.new(name=key)
    group.remove(range(len(mesh_obj.data.vertices)))
    group.add(sorted(vertices), 1.0, 'REPLACE')
    return group


def find_copy(face_obj):
    """The copy this step made for `face_obj` before, if it is still in the file."""
    for obj in bpy.data.objects:
        if obj.get(COPY_MARKER) and obj.get("uma_face_normals_for") == face_obj.name:
            return obj
    return bpy.data.objects.get(face_obj.name + COPY_SUFFIX)


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

    constraint = copy.constraints.new('COPY_ROTATION')
    constraint.name = "UMA face normals: follow the head"
    constraint.target = armature
    if head is not None:
        constraint.subtarget = head.name
    constraint.mix_mode = 'REPLACE'
    constraint.influence = 1.0

    # Hidden the ordinary way, with the flags the Outliner's eye and the render toggle actually drive.
    # `hide_set(True)` is deliberately *not* used: it is a per-view-layer hide that overrides those flags, so the
    # reported "I couldn't get it to appear even after enabling viewport visibility" is exactly what it causes.
    copy.hide_viewport = True
    copy.hide_render = True
    copy.hide_select = False
    if copy.parent is None and face_obj.parent is not None:
        # keep it in the same place as the object it copies, without inheriting what makes that object move
        copy.matrix_world = face_obj.matrix_world
    if trace is not None:
        trace.info("static copy %r: no shape keys, no armature modifier, Copy Rotation from %s.%s, hidden"
                   % (copy.name, armature.name if armature else "?", head.name if head else "?"))
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


def faces_in(context, objects=None):
    """The objects this step should act on, with the reason each was chosen."""
    if objects is None:
        objects = [obj for obj in context.scene.objects if obj.type == 'MESH']
    found = []
    for obj in objects:
        if obj.get(COPY_MARKER):
            continue
        slots = [slot.material for slot in obj.material_slots]
        named_face = "face" in obj.name.lower() or "mayu" in obj.name.lower()
        face_materials = [material for material in slots if is_face_material(material)]
        if named_face:
            found.append((obj, "its name says face"))
        elif face_materials and len(face_materials) == len([material for material in slots if material]):
            found.append((obj, "its materials are all face materials"))
        elif face_materials:
            found.append((obj, "%d of %d material slot(s) are face materials, so the transfer is limited to their "
                                "vertices" % (len(face_materials), len([m for m in slots if m]))))
    return found


def apply_face_normals(context, objects=None, trace=None):
    """Run the whole step, idempotently. Returns a report dict."""
    report = {"tag": TAG, "targets": [], "notes": []}
    for face_obj, why in faces_in(context, objects):
        armature = armature_of(face_obj)
        head = head_bone(armature)
        entry = {"object": face_obj.name, "why": why,
                 "armature": armature.name if armature else None,
                 "head": head.name if head else None}
        if armature is None or head is None:
            entry["skipped"] = "no armature or no head bone to follow"
            report["notes"].append("%s: %s" % (face_obj.name, entry["skipped"]))
            report["targets"].append(entry)
            continue
        # is the face the whole object, or one material among several?
        slots = [slot.material for slot in face_obj.material_slots if slot.material]
        whole_object = ("face" in face_obj.name.lower() or "mayu" in face_obj.name.lower()
                        or len([m for m in slots if is_face_material(m)]) == len(slots))
        group = None if whole_object else face_group_for(face_obj)
        if not whole_object and group is None:
            entry["skipped"] = "no face material found on it"
            report["targets"].append(entry)
            continue
        entry["limited_to_group"] = None if group is None else group.name
        copy = find_copy(face_obj)
        if copy is None:
            copy = make_copy(context, face_obj, armature, head, trace)
            entry["copy"] = "created"
        else:
            # refresh it in place: a re-run must not stack copies
            copy.data = face_obj.data.copy() if copy.data is None else copy.data
            entry["copy"] = "reused"
        modifier = make_modifier(face_obj, copy, group, context)
        entry["modifier"] = modifier.name
        entry["settings"] = {"object": copy.name, "use_loop_data": modifier.use_loop_data,
                             "data_types_loops": modifier.data_types_loops,
                             "loop_mapping": modifier.loop_mapping,
                             "vertex_group": modifier.vertex_group}
        report["targets"].append(entry)
    return report


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
        self.report({'INFO'}, "%s: removed %d transfer(s), %d copy/copies, %d group(s)"
                    % (TAG, removed["modifiers"], removed["copies"], removed["groups"]))
        return {'FINISHED'}
