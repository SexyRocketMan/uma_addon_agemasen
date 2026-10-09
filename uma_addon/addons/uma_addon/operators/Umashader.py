# The Shading operator, as upstream ships it, with three things added. One is unavoidable: the write of the
# outline modifier's "Depth Offset" at the end, where upstream does `geom_mod["Input_15"] = 0.1` - an ID
# property on the modifier, which Blender 5.2 no longer uses for node-group modifier inputs (4.2 through 5.1
# still do). `set_modifier_input()` picks the route the running Blender has and reports instead of raising
# when neither works. Another is optional: it runs only when the scene's "Contain the outline" checkbox in
# the UMA panel is ticked, and `contain_outline()` below is the whole of it.
#
# The third is that none of the names this operator used to carry in the code are here any more: the shader
# file, the collection to append, the part substrings, the two shader materials, the texture node names and
# filename substitutions, the per-part toggles and the outline's own names all come from the manifest of the
# shader being applied (`operators/shaders/<id>.json`, read by `operators/ShaderManifest.py`). Applying a
# second shader is then that file, not an edit here. The behaviour is meant to be *identical* to the shipped
# 2.18.0 one and is measured to be so (`docs/SHADER_INTERFACE.md` section 4, step 1, `shader_apply_fingerprint.py`);
# the three deliberate differences are that a missing manifest is reported instead of raising, that an image
# node with no image is reported instead of failing on `img_node.image.filepath`, and that a shader group
# without a toggle socket is left alone instead of raising a KeyError.

import bpy
import os
from ..config import __addon_name__

from ..utils.Utils import find_file_by_keywords
from . import ShaderManifest

# The central shading control (one place to set the look, per-character overrides) lives in its own file so it can
# be removed on its own; the panel still draws and Shading still runs without it. Same for the face-normal step.
try:
    from .UmaShaderControl import hook as hook_shader_control
    from .UmaShaderControl import TAG as SHADER_CONTROL_TAG
except ImportError:  # pragma: no cover - only when that file is deleted
    hook_shader_control = None
    SHADER_CONTROL_TAG = "look-0"

try:
    from .FaceNormals import apply_face_normals
except ImportError:  # pragma: no cover - only when that file is deleted
    apply_face_normals = None


def set_modifier_input(modifier, name, identifier, value):
    """Set one input of a geometry nodes modifier on Blender 4.2 through 5.2.

    Up to 5.1 a node group modifier keeps its inputs as ID properties on the modifier itself, keyed by the
    node group interface's socket identifier - ``modifier["Input_15"] = 0.1``. Blender 5.2 moved them to
    ``modifier.properties.inputs[identifier]``, and the old path raises there
    ("bpy_struct[key] = val: id properties not supported for this type").

    Returns False and says so rather than raising, so a modifier whose group does not offer the socket
    cannot take the operator down.
    """
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    if inputs is not None:
        for key in _outline_socket_candidates(modifier, name, identifier):
            if key in inputs.keys():
                entry = inputs[key]
                try:
                    entry["value"] = value
                    return True
                except (TypeError, KeyError):
                    pass
                for attribute in ("value", "default_value"):
                    if hasattr(entry, attribute):
                        setattr(entry, attribute, value)
                        return True

    try:
        if identifier in modifier:
            modifier[identifier] = value
            return True
    except TypeError:
        pass

    print(f"[uma_addon] could not set '{name}' ({identifier}) on the '{modifier.name}' modifier")
    return False


def _outline_socket_candidates(modifier, name, identifier):
    """The identifier to look up, plus the socket's current one if the interface has renamed it."""
    candidates = [identifier]
    group = getattr(modifier, "node_group", None)
    interface = getattr(group, "interface", None)
    for item in getattr(interface, "items_tree", None) or []:
        if getattr(item, "item_type", "") == "SOCKET" and item.in_out == 'INPUT' and item.name == name:
            candidates.insert(0, item.identifier)
    return candidates


# --- the shader manifest -------------------------------------------------------------------------------
#
# The shader being applied is the *scene's* choice (`scene.uma_scene.shader`, chosen in the shading panel), so it
# is read per run inside `execute` - `ShaderManifest.active(context)` - rather than bound to a module constant here.
# A stale constant is the trap this file does not want: the run would shade with one shader while the panel showed
# another's settings. `execute` reports the problems rather than raising when the manifest is unreadable.


def part_rule(manifest, material_name):
    """The part a material belongs to: the manifest's rules in order, first substring that matches wins.

    This is the if/elif chain upstream wrote, as data - `'bdy' in name or 'body' in name` first, `'eye'` last -
    and a material matching none of them is skipped, exactly as before. The rule's `id` is also what the texture
    keyword search is given (`find_file_by_keywords(tex_dir, [part_type, "ctrl"])`).
    """
    lowered = material_name.lower()
    for rule in manifest.get("parts") or []:
        if any(needle in lowered for needle in rule.get("match") or []):
            return rule
    return None


def set_group_input(nodes, spec):
    """Set one input on every group node whose node tree name contains `spec["group"]`.

    Upstream reached straight into `n.inputs["Emmission Toggle"]` for the emission texture and
    `n.inputs["Toggle If Face [0=Off,1=On]"]` for faces, which raises a KeyError on a material whose group does
    not offer that socket. The socket is looked up instead, so an option a shader does not have is simply not
    set - which is what makes a shader file swapable without touching this file.
    """
    changed = 0
    for node in nodes:
        if node.type != 'GROUP' or node.node_tree is None:
            continue
        if spec.get("group") and spec["group"] not in node.node_tree.name:
            continue
        socket = node.inputs.get(spec.get("input"))
        if socket is None:
            continue
        socket.default_value = spec["value"]
        changed += 1
    return changed


def bind_texture(nodes, rule, tex_dir, tex_diff, source_path, part_id):
    """Put one texture where the manifest says it goes, and return whether a node was given an image.

    `rule` is one entry of a recipe: a node name, and either `"source": true` (the image the material already
    had) or a two-entry `replace` applied to that image's filename - `"diff"` -> `"ctrl"` and the like - plus
    the colour space it wants. `keywords` adds the folder search upstream did when the substituted name was not
    there (`find_file_by_keywords(tex_dir, [part, "ctrl"])`), and `optional` is for the eye highlights, which
    upstream skipped in silence where the part textures print the name they looked for.
    """
    node = nodes.get(rule.get("node"))
    if node is None:
        return False
    if rule.get("source"):
        path = source_path
    else:
        find, replace = rule["replace"]
        path = os.path.join(tex_dir, tex_diff.replace(find, replace))
        if not os.path.exists(path) and rule.get("keywords"):
            path = find_file_by_keywords(tex_dir, [part_id] + list(rule["keywords"]))
        if not path or not os.path.exists(path):
            if not rule.get("optional"):
                print(f"{os.path.join(tex_dir, tex_diff.replace(find, replace))} not found")
            return False

    node.image = bpy.data.images.load(path, check_existing=True)
    if node.image is not None and rule.get("colorspace"):
        node.image.colorspace_settings.name = rule["colorspace"]
    for spec in rule.get("set_inputs") or []:
        set_group_input(nodes, spec)
    return True


# --- optional: contain the outline ---------------------------------------------------------------------
#
# Off by default, so shading behaves exactly as upstream unless it is switched on. What it is for:
#
# The Uma Outlines group offsets the shell with two terms. One is the mesh normal times Thickness (the real
# width); the other is normalize(Position - Object Info.Location) scaled by the green channel of the
# attribute the group reads, i.e. a direction offset. That Object Info node is wired to the group's Camera
# input, and when that input is left unset geometry nodes falls back to the object itself - so the offset
# points radially away from the model's origin instead of along the view ray, and the outline comes out far
# larger than it should. Setting the camera field in the modifier by hand fixes that; this does it for you.
#
# The second half deals with what is *in* that attribute. UmaViewer's exporter writes the outline normal set
# there; an Agemasen export packs the vertex colour (r, g, b, a) into the same UV pair instead, so the green
# the group reads is the vertex colour's green - zero over most of the mesh (measured: 76% of corners on
# 1002_00.pmx). The term is then scaled by the wrong data. Where that is the case the shell is given a
# neutral width in an attribute of its own, and the group is pointed at it, leaving the export's own channels
# alone. Measured on that model: with no width the shell lies on the surface and covers 7.9% - 10% of the
# frame; at 0.25 it covers 0.00% and the render matches an un-outlined one. A contained outline, not a
# correct one - the correct fix is the exporter writing the real set (docs/OUTLINES.md).

#: The outline normal set the group reads by default, and what it is pointed at instead when that channel is
#: not an outline set - `outline.source_attribute` and `outline.width_attribute` in the manifest, read per run.
#: `outline.width` is the width written into it: 0.25 is the smallest value measured to stop the shell covering
#: the model. The 0.25 *coverage* below is a different number and stayed in the code, because it is a measured
#: threshold rather than a name the shader file decides, and it is passed in rather than read from a constant.


def outline_width_coverage(mesh_obj, attribute_name):
    """Share of the mesh whose green channel of ``attribute_name`` is non-zero, or None when it is absent."""
    mesh = mesh_obj.data
    layer = mesh.uv_layers.get(attribute_name)
    if layer is not None:
        data, accessor, components = layer.data, "uv", 2
    else:
        attribute = mesh.attributes.get(attribute_name)
        if attribute is None:
            return None
        kind = attribute.data_type
        if "COLOR" in kind:
            data, accessor, components = attribute.data, "color", 4
        elif kind == "FLOAT2":
            data, accessor, components = attribute.data, "vector", 2
        else:
            data, accessor, components = attribute.data, "vector", 3

    values = [0.0] * (len(data) * components)
    try:
        data.foreach_get(accessor, values)
    except (AttributeError, TypeError, RuntimeError):
        return None
    channel = values[1::components]
    if not channel:
        return None
    return sum(1 for value in channel if abs(value) > 1e-6) / len(channel)


def point_outline_group_at(group, attribute_name, depth=0, limit=4):
    """Point the attribute-reading nodes inside the outline group at ``attribute_name``."""
    changed = 0
    for node in group.nodes:
        if node.type == "INPUT_ATTRIBUTE":
            name_socket = node.inputs.get("Name")
            if name_socket is not None and name_socket.default_value != attribute_name:
                name_socket.default_value = attribute_name
                changed += 1
        elif node.type == "GROUP" and node.node_tree is not None and depth < limit:
            changed += point_outline_group_at(node.node_tree, attribute_name, depth + 1, limit)
    return changed


def contain_outline(operator, modifier, mesh_obj, blend_file, manifest, masters=None):
    """The optional containment described above: camera, width, material. Reports what it did.

    Every name it writes through comes from `manifest["outline"]` - the socket names with the legacy identifiers
    Blender 4.2-5.1 needs, the two attributes, and the outline material, which comes from `masters` (this shader's
    own copy of the library) rather than from `bpy.data.materials` by name, since two shaders in one scene have two
    materials called `Uma Outline Material`.
    """
    outline = (manifest or {}).get("outline") or {}
    inputs = outline.get("inputs") or {}
    group = getattr(modifier, "node_group", None)

    # 1. the camera the right_offset term measures from
    camera = bpy.context.scene.camera
    spec = inputs.get("camera") or {}
    if camera is not None:
        if spec and set_modifier_input(modifier, spec["name"], spec["identifier"], camera):
            print(f"[uma_addon] outline camera set to {camera.name!r}: without it the outline offsets away "
                  f"from the model's own origin instead of along the view ray, which is what makes it huge")
    else:
        print("[uma_addon] no scene camera, so the outline keeps offsetting from the model's origin - "
              "set a camera and run Shading again for a sane outline size")

    # 2. a width the group can actually use
    source_attribute = outline.get("source_attribute") or ""
    width_attribute = outline.get("width_attribute") or ""
    width = outline.get("width", 0.0)
    if group is not None and source_attribute and width_attribute:
        coverage = outline_width_coverage(mesh_obj, source_attribute)
        if coverage is not None and coverage >= 0.25:
            point_outline_group_at(group, source_attribute)
            print(f"[uma_addon] {source_attribute} carries a usable outline width "
                  f"({coverage:.0%} of the mesh), left as it is")
        else:
            attribute = mesh_obj.data.attributes.get(width_attribute)
            if attribute is None:
                attribute = mesh_obj.data.attributes.new(width_attribute, 'FLOAT_VECTOR', 'CORNER')
            attribute.data.foreach_set("vector", [0.0, width, 0.0] * len(mesh_obj.data.loops))
            mesh_obj.data.update()
            point_outline_group_at(group, width_attribute)
            if coverage is None:
                print(f"[uma_addon] {mesh_obj.name} has no {source_attribute} outline normal set; gave the "
                      f"shell a neutral width of {width} instead")
            else:
                print(f"[uma_addon] {source_attribute}'s green channel is zero over {(1 - coverage):.0%} of "
                      f"{mesh_obj.name} (an Agemasen export packs the vertex colour there, not an outline "
                      f"normal set); gave the shell a neutral width of {width} and left "
                      f"{source_attribute} alone")

    # 3. the shell's own material, so it reads as a line rather than the character's shading flipped
    material = (masters or {}).get(outline.get("material"))
    material_input = inputs.get("material") or {}
    if material is not None and material_input:
        operator.report({'INFO'}, "outline camera, width and material set")
        if set_modifier_input(modifier, material_input["name"], material_input["identifier"], material):
            print(f"[uma_addon] outline shell uses {material.name!r}")
    else:
        print(f"[uma_addon] the library has no {outline.get('material')!r} material, so the shell keeps the "
              "character's own material")


# --- the library in the scene --------------------------------------------------------------------------
#
# A scene can hold two shaders at once, and the reason it needs care is that both files name everything the same:
# the collection, the sphere, the sun empty, the master materials, the outline node group. So "is the library in
# the scene?" cannot be answered by name - it would reuse the first library's `Uma Shader` for the second shader,
# and the copy would look right while shading with the old group. What answers it is the tag
# `ShaderManifest.SOURCE_PROPERTY`, written on everything an append brings in, which is also what lets
# `UmaShaderControl.sun_object` pick the sun of the shader the *scene* has selected.

def library_masters(collection, manifest):
    """`{role: material}` for the copy of the library that came with *this* collection.

    The sphere in the library collection carries the master materials as slots (measured: four, the three the
    manifest names plus a placeholder), so they are looked up *there* rather than with `bpy.data.materials.get`,
    which would hand back the first library's material whose name matches - Blender renames the second library's to
    `Uma Shader.001`, and that suffix is the only thing that tells the two apart by name.
    """
    names = manifest.get("materials") or {}
    found = {}
    for obj in collection.all_objects:
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue
            for role, name in names.items():
                if role in found or not name:
                    continue
                if material.name == name or material.name.startswith(name + "."):
                    found[role] = material
    return found


def ensure_library(manifest, blend_file, context):
    """Make sure this scene holds *this* shader's library. Returns `(collection, {role: material}, notes)`.

    Three cases, and the third is the one that matters: the library for this shader is already here (tagged with its
    id, so two shaders can be in one scene); a library is here with no tag at all, which can only be a file shaded
    before the tag existed - and then only the *default* shader's library could have been appended, so it is adopted
    and tagged; or nothing matches, and the file's own collection is appended, which Blender renames to
    "... Collection.001" when the other shader's is already there.
    """
    wanted = manifest["collection"]
    for collection in bpy.data.collections:
        if wanted not in collection.name:
            continue
        source = collection.get(ShaderManifest.SOURCE_PROPERTY)
        if source == manifest["id"]:
            return collection, library_masters(collection, manifest), []
        if source is None and manifest["id"] == ShaderManifest.DEFAULT_ID:
            collection[ShaderManifest.SOURCE_PROPERTY] = manifest["id"]
            return collection, library_masters(collection, manifest), \
                ["adopted the untagged %r as %s's library" % (collection.name, manifest["id"])]

    before = set(bpy.data.collections)
    file_collection = None
    with bpy.data.libraries.load(blend_file, link=False) as (data_from, data_to):
        file_collection = next((name for name in data_from.collections if wanted in name), None)
        if file_collection is not None:
            data_to.collections.append(file_collection)
    if file_collection is None:
        return None, {}, ["%s has no collection matching %r" % (manifest["blend"], wanted)]

    fresh = [collection for collection in bpy.data.collections if collection not in before]
    for collection in fresh:
        collection[ShaderManifest.SOURCE_PROPERTY] = manifest["id"]
    collection = fresh[0] if fresh else None
    if collection is None:
        return None, {}, ["%s was appended but is not in bpy.data.collections" % file_collection]
    if collection.name not in context.scene.collection.children:
        context.scene.collection.children.link(collection)

    masters = library_masters(collection, manifest)
    notes = []
    if len(masters) < len(manifest.get("materials") or {}):
        notes.append("%r carries %d of the %d materials the manifest names (%s); the sphere is what holds them, so "
                     "a scene whose sphere was deleted shades from what is left"
                     % (collection.name, len(masters), len(manifest.get("materials") or {}),
                        ", ".join(sorted(masters))))
    return collection, masters, notes


def ensure_node_group(blend_file, group_name, manifest_id):
    """The shader file's own copy of a node group, tagged like the collection, or None when the file has none.

    Both shaders name their outline group `Uma Outlines`, so the same by-name trap applies: a mesh shaded with one
    shader and a mesh shaded with the other would otherwise be given the same group. The tag is what separates them,
    and a group that is already there with no tag is adopted when it is the default shader asking.
    """
    for group in bpy.data.node_groups:
        if group.name == group_name and group.get(ShaderManifest.SOURCE_PROPERTY) == manifest_id:
            return group
    if manifest_id == ShaderManifest.DEFAULT_ID:
        untagged = [group for group in bpy.data.node_groups
                    if group.name == group_name and not group.get(ShaderManifest.SOURCE_PROPERTY)]
        if untagged:
            untagged[0][ShaderManifest.SOURCE_PROPERTY] = manifest_id
            return untagged[0]

    before = set(bpy.data.node_groups)
    with bpy.data.libraries.load(blend_file, link=False) as (data_from, data_to):
        if group_name not in data_from.node_groups:
            return None
        data_to.node_groups.append(group_name)
    fresh = [group for group in bpy.data.node_groups if group not in before]
    for group in fresh:
        group[ShaderManifest.SOURCE_PROPERTY] = manifest_id
    return fresh[0] if fresh else None


class ApplyShader(bpy.types.Operator):
    '''Apply uma shader to the model'''
    bl_idname = "uma.apply_shader"
    bl_label = "Shading"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context: bpy.types.Context):
        return context.active_object and context.active_object.type == 'MESH' and len(context.selected_objects) == 1

    def execute(self, context: bpy.types.Context):
        obj = context.active_object

        # The outline containment is a scene setting, drawn under the Shading button in the UMA panel, and it
        # is read here rather than kept as a property of this operator: an operator property has nowhere to
        # appear until the operator has run at least once (F9, Adjust Last Operation), which is exactly how
        # the first version of this toggle ended up invisible in the UI.
        fix_outlines = bool(getattr(getattr(context.scene, "uma_scene", None), "fix_outlines", False))
        
        # The shader this run applies, and everything it is made of - the .blend, the collection to append, the
        # part rules, the materials, the texture recipes, the per-part toggles and the outline. The *scene* decides
        # which shader that is (`scene.uma_scene.shader`, chosen in the shading panel), so the context goes in. A
        # manifest that is missing cannot be worked around, so it cancels; one that is readable but incomplete is
        # reported and the run carries on, applying every part of the look that *was* described.
        manifest, problems = ShaderManifest.active(context)
        if manifest is None:
            self.report({'ERROR'}, "no shader manifest: %s" % "; ".join(problems or ["unknown reason"]))
            return {'CANCELLED'}
        for problem in problems:
            print(f"[uma_addon] the manifest of {manifest.get('id')}: {problem}")

        # 定位 manifest 指定的 shader 库文件
        blend_file = os.path.join(os.path.dirname(__file__), manifest["blend"])

        if not os.path.exists(blend_file):
            self.report({'ERROR'}, f"{blend_file} not found")
            return {'CANCELLED'}

        # 追加 Icosphere of Materials Collection
        library, masters, problems_here = ensure_library(manifest, blend_file, context)
        for problem in problems_here:
            print(f"[uma_addon] {problem}")
        if library is None:
            self.report({'ERROR'}, "%s not found in %s" % (manifest["collection"], manifest["blend"]))
            return {'CANCELLED'}

        # 遍历选中模型的材质槽
        source_nodes = manifest.get("source_image_nodes") or []
        recipes = manifest.get("textures") or {}
        for slot in obj.material_slots:
            old_mat = slot.material
            if not old_mat: 
                continue
            # 判断部位类型
            rule = part_rule(manifest, old_mat.name)
            if rule is None:
                continue
            part_type = rule["id"]
            
            # 推导 Texture2D 贴图名
            if old_mat.node_tree:
                # 寻找原有材质里的图像节点来推导: one of the manifest's source node names, with an image if
                # there is one (a node carrying no image has no path to derive anything from)
                img_node = next((n for n in old_mat.node_tree.nodes if n.name in source_nodes and n.image), None) \
                    or next((n for n in old_mat.node_tree.nodes if n.name in source_nodes), None)
                if img_node is None or img_node.image is None:
                    self.report({'ERROR'}, f"Cannot find a valid image node in material {old_mat.name} to determine texture path")
                    return {'CANCELLED'}
                img_path = bpy.path.abspath(img_node.image.filepath)
                tex_dir = os.path.dirname(img_path)
                tex_diff = os.path.basename(img_path)

                # 找到该部位对应的 shader 材质：from *this* shader's library copy, not by name
                role = rule.get("material")
                uma_shader = masters.get(role)
                if uma_shader is None:
                    self.report({'ERROR'}, "the %s library carries no %r material (%s)"
                                % (manifest["blend"], role,
                                   (manifest.get("materials") or {}).get(role)))
                    return {'CANCELLED'}

                # 对材质进行深拷贝并替换
                new_mat = uma_shader.copy()
                old_mat_name = old_mat.name
                if old_mat.users <= 1:
                    bpy.data.materials.remove(old_mat)
                new_mat.name = old_mat_name
                slot.material = new_mat
                nodes = new_mat.node_tree.nodes

                # 贴图：该部位自己的配方，没有就用手册里的 default
                recipe = recipes.get(part_type) or recipes.get("default") or []
                for texture_rule in recipe:
                    bind_texture(nodes, texture_rule, tex_dir, tex_diff, img_path, part_type)

                # 部位自己的开关（脸部的 Toggle If Face，等等）
                for spec in manifest.get("part_inputs") or []:
                    if spec.get("part") == part_type:
                        set_group_input(nodes, spec)

        # 为选中模型加上 shader 手册里说的那个 outline 几何节点
        outline = manifest.get("outline") or {}
        modifier_name = outline.get("modifier")
        group_name = outline.get("group")
        if outline and modifier_name and group_name:
            if modifier_name not in obj.modifiers:
                node_group = ensure_node_group(blend_file, group_name, manifest["id"])
                if node_group:
                    geom_mod = obj.modifiers.new(name=modifier_name, type='NODES')
                    geom_mod.node_group = node_group

            geom_mod = obj.modifiers.get(modifier_name)
            if geom_mod is not None:
                # was: geom_mod["Input_15"] = 0.1
                depth = (outline.get("inputs") or {}).get("depth_offset") or {}
                if depth:
                    set_modifier_input(geom_mod, depth["name"], depth["identifier"], depth["value"])
                if fix_outlines:
                    contain_outline(self, geom_mod, obj, blend_file, manifest, masters)

        # The central shading control: one property per setting on the sun empty, one override per character on
        # its root empty, and a driver per setting per copy resolving override -> global. Hooked up here so a
        # freshly shaded character is connected without a second click; switchable in the UMA panel, because a
        # scene that is exported or handed on wants the values baked into the materials instead (see
        # docs/SHADER_LOOK.md). This reads the scene setting rather than operator input for the same reason
        # `fix_outlines` does: an operator property has nowhere to be seen until the operator has run.
        control = bool(getattr(getattr(context.scene, "uma_scene", None), "shader_control", True))
        if control and hook_shader_control is not None:
            report = hook_shader_control(context)
            verification = report.get("verified") or {}
            if verification.get("wrong_count"):
                first = verification["wrong"][0]
                self.report({'WARNING'}, "%s: %d driver(s) made, but %s / %s still reads %s instead of %s"
                            % (SHADER_CONTROL_TAG, report.get("drivers", 0), first["material"],
                               first["setting"], first["actual"], first["expected"]))
            else:
                print("[uma_addon] %s: %d driver(s) over %d cop(y/ies), %d value(s) verified"
                      % (SHADER_CONTROL_TAG, report.get("drivers", 0), report.get("copies", 0),
                         verification.get("checked", 0)))
            for note in report.get("notes", []):
                print("[uma_addon] %s: %s" % (SHADER_CONTROL_TAG, note))


        # The face normals step (see operators/FaceNormals.py): a hidden static copy of the face plus a data
        # transfer of its custom split normals, so a facial morph does not drag the face's shading normals
        # with it. On by default and switchable in the panel - the manual setup it automates is the usual
        # one - and it must never take the shading run down with it, so anything it raises is reported and
        # the run carries on.
        if bool(getattr(getattr(context.scene, "uma_scene", None), "face_normals", False)) \
                and apply_face_normals is not None:
            try:
                face_report = apply_face_normals(context)
                done = [entry for entry in face_report["targets"] if not entry.get("skipped")]
                for entry in done:
                    print("[uma_addon] %s: %s %s a static copy, transfer on %s%s"
                          % (face_report["tag"], entry["object"], entry["copy"], entry["modifier"],
                             "" if not entry.get("limited_to_group")
                             else " limited to %s" % entry["limited_to_group"]))
                for note in face_report["notes"]:
                    print("[uma_addon] %s: %s" % (face_report["tag"], note))
                if not done:
                    print("[uma_addon] %s: no face found, so no normal transfer was set up"
                          % face_report["tag"])
            except Exception as error:  # noqa: BLE001 - never take the shading run down
                print("[uma_addon] face normal transfer failed (%s: %s); the shading is applied without "
                      "it" % (type(error).__name__, error))
        self.report({'INFO'}, "Uma shader applied")
        return {'FINISHED'}
