"""More hinges in a skirt panel than the model's weights give it (2.33.2).

The problem this exists for, measured on the user's own test model: Maruzensky's skirt panels carry **one**
deforming bone plus a leaf entirely outside the cloth (`_01` has 0 vertices; the chain is 0.351 m where only the
first 0.234 m is cloth). One hinge cannot curl - the panel swings as a slab - and every hem number this project
quotes is read at the tip of that leaf, so it overstates the cloth's own motion by about 1.85x (0.236 m against
0.121 m). Chou's own packed rig of the same character has the same shape: one weighted bone per panel and the leaf
kept, so he ships the slab too.

What this does, per panel, and nothing else:

1. **Measure the cloth**, not the bone. The panels' own vertices are projected onto their root bone's own axis and
   the cloth is read off as `s_min..s_max` along it. Measured: the cloth starts 13-43% of the way *down* the bone -
   the head sits above the skirt, in the hip - so a subdivision over the bone's whole length would put its topmost
   joint outside the cloth, doing nothing.
2. **Reshape** to `count` bones spanning that measured cloth: the root keeps its head (the hip joint the skirt hangs
   from) and is shortened; the existing leaf moves into the cloth and any further bones are created parented and
   connected, so the chain runs hip -> ... -> hem with every joint inside the cloth. No bone is deleted, which keeps
   `_01_Handle` and anything else that names a bone valid.
3. **Redistribute inside the weights that already exist.** For every vertex and every panel it is weighted to, the
   weight it *already* had in that panel is split across that panel's bones by its position along the panel, blending
   between adjacent bone centres. Nothing is invented and nothing else is replaced: the panel's total is the same per
   vertex, so the ring's cross-panel blending and the `Hip`/`Waist` waistband survive by construction. That matters
   more than it sounds - the panel's existing gentle curve comes from exactly that waistband blend (10.34 deg of bend
   in the shipped rig before any B-Bone moved a thing), so flattening it would take away the only curvature there is.

Why not Blender's automatic weights: bone heat needs a clean manifold with the bones inside the mesh, and a ten-panel
ring with legs inside it is its classic failure case. Every input this needs is already in the file.

Measured through the whole pipeline (reshape before the rig is generated): the metarig follows the reshaped *and
newly created* bones to 0.0000 m, the model at rest is unchanged to 2.5e-07 m, and the cloth's local bend goes from
falling to rising - 89.05 deg at the hem against 30.90 deg for the shipped single hinge.

## 2.33.4 - the split was computed on the wrong bone length (a real bug, found by `skirt_reshape_scale_check.py`)

Steps 1-3 above measure the cloth in fractions of the root bone (`s_min..s_max`) and lay the joints out on that
same basis, but per panel this module called `_reshape_chain` **first** - which shortens the root to `joints[1]` -
and only then re-read the bone's length, handing the *shortened* figure to `_redistribute`. There `s` is
`(local - head).dot(axis) / length`, while the `centres` it is compared against stay in the original basis, so
every vertex came out `f / joints[1]` too far down the panel. On Maruzensky's `_B` panel that factor is 1.60, and
the split it produced was:

| | `_00` (the hip joint the skirt hangs from) | `_01` | `_02` (turns twice as far) |
|---|---|---|---|
| before 2.33.4 | **0.2%** | 7.1% | **92.7%** |
| after 2.33.4 | 7.8% | 36.3% | 55.9% |

The handover between `_01` and `_02` sat at 0.541 of the original bone where the joint the cloth was measured for
is at 0.839, and `_01` already outranked `_00` at the topmost cloth vertex, so the hip bone never led anywhere in
the cloth at all. After the fix that handover is 0.840 against the 0.839 joint. The same mistake is why this
module's per-panel trace line reported 0.093 m for a panel whose cloth is 0.144 m - short by exactly `joints[1]`.

What it did *not* change is the hem: measured through the shipped switch at 30 deg on the master, the worst cloth
vertex moves 0.18618 m **both ways**, and the hem's own tangent is 89.20 deg before against 89.23 after - the hem
bone dominated the maximum either way, so the dose this feature is quoted for was never in doubt. What changed is
where the curve is: the tangent profile was 48.81, 78.14, 89.20 (a third of the way to the hem's angle in one
step, then nearly flat - the slab signature of weights piled on the hem bone) and is 53.59, 68.66, 89.23 after, a
steady +15.07 and +20.57. So this was a **skinning** defect rather than a loss of curl: the panel's top, near the
hips, was being driven by the hem bone. The symptom to look for is that section moving with the skirt bones, which
is what the user reported on this model.
"""
import math

import bpy
from mathutils import Vector

#: One bone per this much cloth. Read off the reference model's own working skirt, whose panels are three bones over
#: 0.298-0.307 m: 0.0993 m each. Applied to a panel's *longest* stretch of cloth, and used for every panel of that
#: skirt so the ring keeps one depth rather than a different one per panel.
SPACING_M = 0.10
#: The same number under the name the operator and a rules file use (`options.skirt_hinge_spacing_m`).
SKIRT_HINGE_SPACING_M = SPACING_M
#: Two hinges is still a slab - measured hem turns of 30.9, 62.6 and 89.1 degrees for one, two and three - so a
#: reshape that stopped at two would rewrite a model's weights without buying the curl it is for.
MIN_BONES = 3
MAX_BONES = 5
#: Below this many vertices a projection onto the bone axis is noise rather than a cloth extent.
MIN_VERTICES = 12
SKIRT_PREFIX = "Sp_Hi_MSkirt"
HANDLE_SUFFIX = "_Handle"


def panels(model):
    """`{panel stem: [bone names, root first]}` for the model's own skirt families."""
    found = {}
    for bone in model.data.bones:
        if not bone.name.startswith(SKIRT_PREFIX) or bone.name.endswith(HANDLE_SUFFIX):
            continue
        found.setdefault(bone.name.rsplit("_", 1)[0], []).append(bone.name)
    return {stem: sorted(names) for stem, names in found.items()}


def skinned_meshes(model):
    """`[(mesh object, {vertex index: {group name: weight}})]` for every mesh this armature deforms."""
    out = []
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        if not any(modifier.type == 'ARMATURE' and modifier.object == model for modifier in obj.modifiers):
            continue
        groups = {group.index: group.name for group in obj.vertex_groups}
        per_vertex = {}
        for vertex in obj.data.vertices:
            here = {}
            for element in vertex.groups:
                name = groups.get(element.group)
                if name is not None and element.weight > 0.0:
                    here[name] = element.weight
            per_vertex[vertex.index] = here
        out.append((obj, per_vertex))
    return out


def cloth_of(model, meshes, root):
    """Where the vertices weighted to `root` sit along that bone's own axis: `(s_min, s_max, vertices)`.

    `s = 0` is the bone's head and `s = 1` its tail, so a panel whose cloth is at `0.42..1.05` is one whose cloth
    starts nearly halfway down the bone and reaches its tip. Returns `None` when there is too little of it to trust.
    """
    bone = model.data.bones.get(root)
    if bone is None:
        return None
    direction = bone.tail_local - bone.head_local
    length = direction.length
    if length < 1e-6:
        return None
    axis = direction.normalized()
    to_armature = model.matrix_world.inverted()
    positions = []
    for obj, per_vertex in meshes:
        for index, weights_here in per_vertex.items():
            if root in weights_here:
                local = to_armature @ (obj.matrix_world @ obj.data.vertices[index].co)
                positions.append((local - bone.head_local).dot(axis) / length)
    if len(positions) < MIN_VERTICES:
        return None
    return min(positions), max(positions), len(positions)


def bones_for_span(span_m, spacing=SPACING_M, minimum=MIN_BONES, maximum=MAX_BONES):
    """How many bones a stretch of cloth wants, by the reference model's own density."""
    if span_m <= 0.0:
        return minimum
    return max(minimum, min(maximum, int(round(span_m / spacing))))


def _joint_positions(s_min, s_max, count):
    """The `count + 1` joints along the root bone: the hip at 0, then equal steps across the measured cloth."""
    step = (s_max - s_min) / float(count)
    return [0.0] + [s_min + step * index for index in range(1, count + 1)]


def _reshape_chain(model, stem, chain, s_min, s_max, count, trace):
    """Lay `count` bones over the cloth and return the names, root first. Creates bones; never deletes one."""
    for obj in bpy.context.scene.objects:
        obj.select_set(False)
    model.select_set(True)
    bpy.context.view_layer.objects.active = model
    was_edit = model.mode == 'EDIT'
    if not was_edit:
        bpy.ops.object.mode_set(mode='EDIT')
    try:
        edit = model.data.edit_bones
        root = chain[0]
        if root not in edit:
            return None
        head = edit[root].head.copy()
        vector = edit[root].tail - edit[root].head
        length = vector.length
        direction = vector.normalized()
        joints = _joint_positions(s_min, s_max, count)
        names = []
        for index in range(count):
            if index == 0:
                name = root
                edit[name].tail = head + direction * (joints[1] * length)
            else:
                name = "%s_%02d" % (stem, index)
                if index < len(chain):
                    # reuse a bone the model already has before creating one - the leaf was entirely outside the
                    # cloth, and moving it in keeps its name (and its `_Handle`) valid for anything naming it
                    name = chain[index]
                bone = edit.get(name)
                if bone is None:
                    bone = edit.new(name)
                    bone.use_deform = True
                bone.parent = edit[names[index - 1]]
                bone.use_connect = True
                bone.head = head + direction * (joints[index] * length)
                bone.tail = head + direction * (joints[index + 1] * length)
                bone.roll = edit[names[index - 1]].roll
            names.append(name)
        return names
    finally:
        if not was_edit and model.mode == 'EDIT':
            bpy.ops.object.mode_set(mode='OBJECT')


def _redistribute(model, meshes, names, s_min, s_max, head, axis, length, trace):
    """Share each vertex's existing weight in this panel across its bones, by position along the panel."""
    joints = _joint_positions(s_min, s_max, len(names))
    centres = [(joints[index] + joints[index + 1]) / 2.0 for index in range(len(names))]
    writes = 0
    for obj, per_vertex in meshes:
        touched = 0
        for index, weights_here in per_vertex.items():
            total = sum(weights_here.get(name, 0.0) for name in names)
            if total <= 0.0:
                continue
            local = model.matrix_world.inverted() @ (obj.matrix_world @ obj.data.vertices[index].co)
            s = (local - head).dot(axis) / max(length, 1e-9)
            shares = [0.0] * len(names)
            if s <= centres[0]:
                shares[0] = 1.0
            elif s >= centres[-1]:
                shares[-1] = 1.0
            else:
                for position in range(len(centres) - 1):
                    if centres[position] <= s <= centres[position + 1]:
                        span = max(centres[position + 1] - centres[position], 1e-9)
                        t = (s - centres[position]) / span
                        shares[position], shares[position + 1] = 1.0 - t, t
                        break
            for name, share in zip(names, shares):
                group = obj.vertex_groups.get(name)
                if group is None:
                    group = obj.vertex_groups.new(name=name)
                if share <= 0.0:
                    group.remove([index])
                else:
                    group.add([index], total * share, 'REPLACE')
                writes += 1
            touched += 1
        if touched:
            trace.info("skirt reshape: %s: %d vertex weight(s) shared across %s"
                       % (obj.name, touched, ", ".join(names)))
    return writes


def reshape_skirt_panels(model, trace, spacing=SPACING_M, forced=None):
    """Give every skirt panel as many hinges as its cloth wants. Returns a report for the trace and for a probe.

    `forced` is a bone count to use instead of the derived one (`options.skirt_hinges` in a rules file), which is
    what an override exists for: the derivation is one bone per SPACING_M of the *longest* panel's cloth, so a model
    whose skirt is shorter than that gets the MIN_BONES floor and nothing finer.
    """
    meshes = skinned_meshes(model)
    if not meshes:
        trace.info("skirt reshape: no mesh is skinned to %s, so there is no cloth to measure - nothing changed"
                   % model.name)
        return {"panels": 0, "reshaped": [], "skipped": {}, "writes": 0}
    geometry = {}
    for stem, chain in sorted(panels(model).items()):
        root = chain[0]
        extent = cloth_of(model, meshes, root)
        if extent is None:
            continue
        geometry[stem] = {"chain": chain, "root": root, "s_min": extent[0], "s_max": extent[1],
                          "vertices": extent[2]}
    if not geometry:
        trace.info("skirt reshape: no skirt panel had enough cloth to measure - nothing changed")
        return {"panels": 0, "reshaped": [], "skipped": {}, "writes": 0}

    longest = max(entry["s_max"] - entry["s_min"] for entry in geometry.values())
    lengths = {stem: (entry["s_max"] - entry["s_min"]) * model.data.bones[entry["root"]].length
               for stem, entry in geometry.items()}
    want = int(forced) if forced else bones_for_span(max(lengths.values()), spacing)
    trace.info("skirt reshape: %d panel(s), cloth %.3f-%.3f m, one bone per %.2f m -> %d bone(s) each (floor %d)"
               % (len(geometry), min(lengths.values()), max(lengths.values()), spacing, want, MIN_BONES))

    reshaped, skipped = [], {}
    for stem, entry in sorted(geometry.items()):
        chain = entry["chain"]
        root = entry["root"]
        bone = model.data.bones[root]
        span = entry["s_max"] - entry["s_min"]
        # how deep the panel deforms *now*, which is what decides whether there is anything to do
        weights = {}
        for obj, per_vertex in meshes:
            for weights_here in per_vertex.values():
                for name in chain:
                    if name in weights_here:
                        weights[name] = weights.get(name, 0) + 1
        have = sum(1 for name in chain if weights.get(name, 0) > 0)
        if span * bone.length < spacing and have >= MIN_BONES:
            skipped[stem] = "cloth %.3f m is short and it already deforms through %d bone(s)" % (
                span * bone.length, have)
            continue
        if have >= want:
            skipped[stem] = "already deforms through %d bone(s), which is what %.3f m of cloth asks for" % (
                have, span * bone.length)
            continue
        # The cloth was measured (`s_min`/`s_max`) as fractions of the root bone as it stands **now**, and
        # `_joint_positions` lays the joints out on that same basis, so the weight split has to be computed on it
        # too. `_reshape_chain` is about to shorten that bone to `joints[1]` of its length, so the basis is captured
        # *first*: re-reading the bone afterwards (which is what this did until 2.33.4) divides every vertex by the
        # shortened length while the centres it is compared against stay in the original basis, making `s` come out
        # `f / joints[1]` too far down the panel. Measured on Maruzensky's `_B` with `skirt_reshape_scale_check.py`,
        # whose joints were 0.000/0.627/0.839/1.051 and whose centres were 0.314/0.733/0.945: the split it produced
        # put **0.2%** of the panel's weight on `_00` (the hip joint the skirt hangs from) and **92.7%** on `_02`
        # (the bone that turns twice as far), with the `_01`->`_02` handover at 0.541 instead of at the joint the
        # cloth was measured for (0.839) - so the panel swung from its middle rather than curling from the hip.
        original = model.data.bones[root]
        head = original.head_local.copy()
        direction = original.tail_local - original.head_local
        length = direction.length
        axis = direction.normalized()
        names = _reshape_chain(model, stem, chain, entry["s_min"], entry["s_max"], want, trace)
        if not names:
            skipped[stem] = "its root bone %s is not in the model" % root
            continue
        writes = _redistribute(model, meshes, names, entry["s_min"], entry["s_max"], head, axis, length, trace)
        reshaped.append({"stem": stem, "bones": names, "writes": writes,
                         "cloth_m": round(span * length, 5),
                         "deforming_before": have, "deforming_after": len(names)})
        # `span * length` with `length` from *this* basis is the cloth this panel actually has: the two numbers
        # agreed with the summary line above only after the basis was fixed (before it, this line reported 0.093 m
        # for a 0.144 m cloth - short by exactly `joints[1]`).
        trace.info("skirt reshape: %s now has %d hinge(s) inside %.3f m of cloth (%s); %d weight write(s)"
                   % (stem, want, span * length, " -> ".join(names), writes))
    report = {"panels": len(geometry), "reshaped": reshaped, "skipped": skipped,
              "bones": want, "writes": sum(entry["writes"] for entry in reshaped)}
    for stem, why in sorted(skipped.items()):
        trace.info("skirt reshape: %s left alone - %s" % (stem, why))
    trace.check("every skirt panel deforms through at least %d bone(s) after the reshape" % MIN_BONES,
                all(entry["deforming_after"] >= MIN_BONES for entry in reshaped) and bool(reshaped),
                "%d reshaped, %d left alone" % (len(reshaped), len(skipped)))
    return report
