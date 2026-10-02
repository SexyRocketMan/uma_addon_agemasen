# The Shading operator, as upstream ships it, with two things added. One is unavoidable: the write of the
# outline modifier's "Depth Offset" at the end, where upstream does `geom_mod["Input_15"] = 0.1` - an ID
# property on the modifier, which Blender 5.2 no longer uses for node-group modifier inputs (4.2 through 5.1
# still do). `set_modifier_input()` picks the route the running Blender has and reports instead of raising
# when neither works. The other is optional: it runs only when the scene's "Contain the outline" checkbox in
# the UMA panel is ticked, and `contain_outline()` below is the whole of it.

import bpy
import os
from ..config import __addon_name__

from ..utils.Utils import find_file_by_keywords


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
#: not an outline set.
OUTLINE_ATTRIBUTE = "UV3"
OUTLINE_WIDTH_ATTRIBUTE = "UMA_OutlineWidth"
#: The width written into it. 0.25 is the smallest value measured to stop the shell covering the model.
OUTLINE_WIDTH = 0.25


def outline_width_coverage(mesh_obj, attribute_name=OUTLINE_ATTRIBUTE):
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


def contain_outline(operator, modifier, mesh_obj, blend_file):
    """The optional containment described above: camera, width, material. Reports what it did."""
    group = getattr(modifier, "node_group", None)

    # 1. the camera the right_offset term measures from
    camera = bpy.context.scene.camera
    if camera is not None:
        if set_modifier_input(modifier, "Camera", "Input_13", camera):
            print(f"[uma_addon] outline camera set to {camera.name!r}: without it the outline offsets away "
                  f"from the model's own origin instead of along the view ray, which is what makes it huge")
    else:
        print("[uma_addon] no scene camera, so the outline keeps offsetting from the model's origin - "
              "set a camera and run Shading again for a sane outline size")

    # 2. a width the group can actually use
    if group is not None:
        coverage = outline_width_coverage(mesh_obj)
        if coverage is not None and coverage >= 0.25:
            point_outline_group_at(group, OUTLINE_ATTRIBUTE)
            print(f"[uma_addon] {OUTLINE_ATTRIBUTE} carries a usable outline width "
                  f"({coverage:.0%} of the mesh), left as it is")
        else:
            attribute = mesh_obj.data.attributes.get(OUTLINE_WIDTH_ATTRIBUTE)
            if attribute is None:
                attribute = mesh_obj.data.attributes.new(OUTLINE_WIDTH_ATTRIBUTE, 'FLOAT_VECTOR', 'CORNER')
            attribute.data.foreach_set("vector", [0.0, OUTLINE_WIDTH, 0.0] * len(mesh_obj.data.loops))
            mesh_obj.data.update()
            point_outline_group_at(group, OUTLINE_WIDTH_ATTRIBUTE)
            if coverage is None:
                print(f"[uma_addon] {mesh_obj.name} has no {OUTLINE_ATTRIBUTE} outline normal set; gave the "
                      f"shell a neutral width of {OUTLINE_WIDTH} instead")
            else:
                print(f"[uma_addon] {OUTLINE_ATTRIBUTE}'s green channel is zero over {(1 - coverage):.0%} of "
                      f"{mesh_obj.name} (an Agemasen export packs the vertex colour there, not an outline "
                      f"normal set); gave the shell a neutral width of {OUTLINE_WIDTH} and left "
                      f"{OUTLINE_ATTRIBUTE} alone")

    # 3. the shell's own material, so it reads as a line rather than the character's shading flipped
    material = bpy.data.materials.get("Uma Outline Material")
    if material is None:
        with bpy.data.libraries.load(blend_file, link=False) as (data_from, data_to):
            if "Uma Outline Material" in data_from.materials:
                data_to.materials.append("Uma Outline Material")
        material = bpy.data.materials.get("Uma Outline Material")
    if material is not None:
        operator.report({'INFO'}, "outline camera, width and material set")
        if set_modifier_input(modifier, "Outline Material", "Input_18", material):
            print(f"[uma_addon] outline shell uses {material.name!r}")
    else:
        print("[uma_addon] Umashaders.blend has no 'Uma Outline Material', so the shell keeps the "
              "character's own material")


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
        
        # 定位同文件夹下的 Umashaders.blend
        blend_file = os.path.join(os.path.dirname(__file__), "Umashaders.blend")

        if not os.path.exists(blend_file):
            self.report({'ERROR'}, f"{blend_file} not found")
            return {'CANCELLED'}

        # 追加 Icosphere of Materials Collection
        col_name = None
        with bpy.data.libraries.load(blend_file, link=False) as (data_from, data_to):
            for name in data_from.collections:
                if "Icosphere" in name:
                    col_name = name
                    break
            if col_name:
                # 只在当前工程没有该集合时追加
                if col_name not in bpy.data.collections:
                    data_to.collections.append(col_name)
            else:
                self.report({'ERROR'}, "Icosphere of Materials collection not found in Umashaders.blend")
                return {'CANCELLED'}
        
        # 链接到当前场景的视图层
        coll = bpy.data.collections.get(col_name)
        if coll:
            if coll.name not in context.scene.collection.children:
                context.scene.collection.children.link(coll)
        else:
            self.report({'ERROR'}, "Failed to load Icosphere of Materials collection")
            return {'CANCELLED'}

        # 遍历选中模型的材质槽
        for slot in obj.material_slots:
            old_mat = slot.material
            if not old_mat: 
                continue
            # 判断部位类型
            part_type = None
            mat_name_lower = old_mat.name.lower()
            if 'bdy' in mat_name_lower or 'body' in mat_name_lower: part_type = 'bdy'
            elif 'hair' in mat_name_lower: part_type = 'hair'
            elif 'face' in mat_name_lower or 'mayu' in mat_name_lower: part_type = 'face'
            elif 'tail' in mat_name_lower: part_type = 'tail'
            elif 'eye' in mat_name_lower: part_type = 'eye'
            if not part_type:
                continue
            
            # 推导 Texture2D 贴图名
            if old_mat.node_tree:
                # 寻找原有材质里的图像节点来推导
                img_node = next((n for n in old_mat.node_tree.nodes if n.name == 'mmd_base_tex' or n.name == 'Image Texture' and n.image), None)
                if img_node:
                    img_path = bpy.path.abspath(img_node.image.filepath)
                    tex_dir = os.path.dirname(img_path)
                    tex_diff = os.path.basename(img_path)
                else:
                    self.report({'ERROR'}, f"Cannot find a valid image node in material {old_mat.name} to determine texture path")
                    return {'CANCELLED'}

                # 找到 Uma Shader
                target_shader_name = "Uma Eyes" if part_type == 'eye' else "Uma Shader"
                uma_shader = bpy.data.materials.get(target_shader_name)
                if not uma_shader:
                    with bpy.data.libraries.load(blend_file, link=False) as (data_from, data_to):
                        if target_shader_name in data_from.materials:
                            data_to.materials.append(target_shader_name)
                            uma_shader = bpy.data.materials.get(target_shader_name)
                        else:
                            self.report({'ERROR'}, f"{target_shader_name} not found in Umashaders.blend")
                            return {'CANCELLED'}

                # 对材质进行深拷贝并替换
                new_mat = uma_shader.copy()
                old_mat_name = old_mat.name
                if old_mat.users <= 1:
                    bpy.data.materials.remove(old_mat)
                new_mat.name = old_mat_name
                slot.material = new_mat
                nodes = new_mat.node_tree.nodes

                if part_type == 'eye':
                    for i, suffix in enumerate([None, "eyehi00", "eyehi01", "eyehi02"]):
                        node_name = "Image Texture" if i == 0 else f"Image Texture.{i:03d}"
                        node = nodes.get(node_name)
                        if node:
                            if i == 0:
                                node.image = bpy.data.images.load(img_path, check_existing=True)
                                node.image.colorspace_settings.name = 'sRGB'
                            else:
                                p = os.path.join(tex_dir, tex_diff.replace("eye0", suffix))
                                if os.path.exists(p):
                                    node.image = bpy.data.images.load(p, check_existing=True)
                                    node.image.colorspace_settings.name = 'Non-Color'

                else:
                    node = nodes.get("Image Texture")
                    if node:
                        node.image = bpy.data.images.load(img_path, check_existing=True)
                        node.image.colorspace_settings.name = 'sRGB'

                    node = nodes.get("Image Texture.001")
                    if node:
                        img_path = os.path.join(tex_dir, tex_diff.replace("diff", "ctrl"))
                        if not os.path.exists(img_path):
                            img_path = find_file_by_keywords(tex_dir, [part_type, 'ctrl'])  
                        if img_path and os.path.exists(img_path):
                            node.image = bpy.data.images.load(img_path, check_existing=True)
                            node.image.colorspace_settings.name = 'Non-Color'
                        else:
                            img_path = os.path.join(tex_dir, tex_diff.replace("diff", "ctrl"))
                            print(f"{img_path} not found")

                    node = nodes.get("Image Texture.002")
                    if node:
                        img_path = os.path.join(tex_dir, tex_diff.replace("diff", "shad_c"))
                        if os.path.exists(img_path):
                            img = bpy.data.images.load(img_path, check_existing=True)
                            node.image = img
                            node.image.colorspace_settings.name = 'sRGB'
                        else:
                            print(f"{img_path} not found")

                    node = nodes.get("Image Texture.003")
                    if node:
                        img_path = os.path.join(tex_dir, tex_diff.replace("diff", "base"))
                        if not os.path.exists(img_path):
                            img_path = find_file_by_keywords(tex_dir, [part_type, 'base'])  
                        if img_path and os.path.exists(img_path):
                            node.image = bpy.data.images.load(img_path, check_existing=True)
                            node.image.colorspace_settings.name = 'Non-Color'
                        else:
                            img_path = os.path.join(tex_dir, tex_diff.replace("diff", "base"))
                            print(f"{img_path} not found")

                    node = nodes.get("Image Texture.004")
                    if node:
                        img_path = os.path.join(tex_dir, tex_diff.replace("diff", "emi"))
                        if os.path.exists(img_path):
                            img = bpy.data.images.load(img_path, check_existing=True)
                            node.image = img
                            node.image.colorspace_settings.name = 'Non-Color'

                            for n in nodes:
                                if n.type == 'GROUP' and n.node_tree and "Uma Shader" in n.node_tree.name:
                                    n.inputs["Emmission Toggle"].default_value = 1
                        else:
                            print(f"{img_path} not found")

                    if part_type == 'face':
                        for n in nodes:
                            if n.type == 'GROUP' and n.node_tree and "Uma Shader" in n.node_tree.name:
                                n.inputs["Toggle If Face [0=Off,1=On]"].default_value = 1

        # 为选中模型加上 Uma Outlines 几何节点
        if "Uma Outlines" not in obj.modifiers:
            if "Uma Outlines" not in bpy.data.node_groups:
                with bpy.data.libraries.load(blend_file, link=False) as (data_from, data_to):
                    if "Uma Outlines" in data_from.node_groups:
                        data_to.node_groups.append("Uma Outlines")
            node_group = bpy.data.node_groups.get("Uma Outlines")
            if node_group:
                geom_mod = obj.modifiers.new(name="Uma Outlines", type='NODES')
                geom_mod.node_group = node_group

        geom_mod = obj.modifiers.get("Uma Outlines")
        if geom_mod is not None:
            # was: geom_mod["Input_15"] = 0.1
            set_modifier_input(geom_mod, "Depth Offset", "Input_15", 0.1)
            if fix_outlines:
                contain_outline(self, geom_mod, obj, blend_file)

        self.report({'INFO'}, "Uma shader applied")
        return {'FINISHED'}
