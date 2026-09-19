import bpy
import os
from collections import Counter
from mathutils import Vector
from ..config import __addon_name__

from ..utils.Utils import find_file_by_keywords

# What the viewer's exporter writes into a pmx material comment, e.g.
#   uma1 toon_step=0.4 specular=1,0.905,0.59 rim=1,1,1,0.392 outline_width=0.325 option=tex_bdy1001_00_ctrl
# It is the uma shader's own settings - the ones an MMD material has no field for - so the shader applied
# here can use the values the model was exported with instead of this addon's defaults.
UMA_COMMENT_TAG = "uma1"


def parse_uma_comment(text):
    """The exporter's key=value pairs, or {} for a comment that was not written by it."""
    if not text:
        return {}
    parts = text.split()
    if not parts or parts[0] != UMA_COMMENT_TAG:
        return {}
    values = {}
    for part in parts[1:]:
        key, separator, value = part.partition("=")
        if separator and key and value:
            values[key] = value
    return values


def comment_float(values, key, default=None):
    try:
        return float(values[key])
    except (KeyError, ValueError, TypeError):
        return default


def comment_colour(values, key):
    raw = values.get(key)
    if not raw:
        return None
    try:
        parts = [float(component) for component in raw.split(",")]
    except ValueError:
        return None
    if len(parts) == 3:
        parts.append(1.0)
    return tuple(parts) if len(parts) == 4 else None


def apply_uma_parameters(group_node, values):
    """Set a Uma Shader group node's sockets from the exported parameters, and say what was applied.

    The mapping is by meaning, not by name: the game's toon step is this shader's light threshold, its
    rim colour and step are the rim light, and so on. Settings the comment does not carry keep whatever the
    shipped material has, which is what older exports without a comment get.
    """
    if not values or group_node.type != 'GROUP' or group_node.node_tree is None:
        return []

    available = {socket.name for socket in group_node.inputs}
    applied = []

    def put(socket_name, value):
        if value is None or socket_name not in available:
            return
        group_node.inputs[socket_name].default_value = value
        applied.append(f"{socket_name}={value if not isinstance(value, tuple) else tuple(round(v, 3) for v in value)}")

    put("Light Threshold", comment_float(values, "toon_step"))
    put("Diffuse Color", comment_colour(values, "chara"))
    put("Diffuse Saturation", comment_float(values, "saturation"))
    put("Highlight Color", comment_colour(values, "specular"))
    put("Metallic Intensity", comment_float(values, "env_rate"))
    put("Emission Intensity", comment_float(values, "emissive_intensity"))

    rim = comment_colour(values, "rim")
    if rim is not None:
        put("Rimlight Color", rim)
        put("Rimlight Intensity", rim[3])       # the uma rim colour's alpha is how strong the rim is
    put("Rimlight Size", comment_float(values, "rim_step"))

    # the live grade overlays only count when the game actually applied them
    dark = comment_colour(values, "toon_dark")
    if dark is not None and dark[3] > 0.0:
        put("Shaded Color", dark)
    bright = comment_colour(values, "toon_bright")
    if bright is not None and bright[3] > 0.0:
        put("Shaded Color", bright)

    return applied


def comment_texture_path(tex_dir, values, key, fallback):
    """The file the export wrote for one uma map.

    The comment names the texture as the game calls it ("tex_bdy1001_00_ctrl"), which is what the exporter
    wrote next to the model, so nothing has to be guessed from file name patterns. Falls back to the file
    derived from the albedo name for exports made before the comment existed.
    """
    name = values.get(key)
    if name:
        for suffix in (".png", ".tga", ".bmp", ".jpg"):
            candidate = os.path.join(tex_dir, name + suffix)
            if os.path.exists(candidate):
                return candidate
    return fallback if fallback and os.path.exists(fallback) else None


def find_sun_object():
    """The empty the shader reads its sun direction from, appended with the Icosphere of Materials.

    Upstream calls it "sun", this build ships it as "Uma Sun [DO NOT DELETE]" - both are Empties whose
    location is used as a direction vector, so match on the name prefix.
    """
    for obj in bpy.data.objects:
        if obj.type == 'EMPTY' and (obj.name.startswith("Uma Sun") or obj.name == "sun"):
            return obj
    return None


def assign_sun_direction(group, sun, depth=0, limit=4, census=None):
    """Point every Object Info node inside a shader group at the sun empty.

    That node holds a direct object reference, and the reference does not survive appending the material on
    its own: without it the shader reads a zero vector, and the whole model shades flat white - which is
    what a freshly applied Shading looked like. The upstream README is explicit that the sun object must
    not be deleted; nothing was wiring it back up.
    """
    changed = 0
    for node in group.nodes:
        if node.type == 'OBJECT_INFO':
            # set every socket of the node: the object socket is not necessarily the first one
            for socket in node.inputs:
                socket.default_value = sun
            changed += 1
        elif node.type == 'GROUP' and node.node_tree and depth < limit:
            changed += assign_sun_direction(node.node_tree, sun, depth + 1, limit)
    return changed


def set_geometry_modifier_input(modifier, name, identifier, value):
    """Set one input of a geometry nodes modifier, across Blender versions.

    Blender 4.0 moved these inputs off the modifier's ID properties: `modifier["Input_15"] = 0.1` now
    raises "bpy_struct[key] = val: id properties not supported for this type", and *reading* it raises
    "this type doesn't support IDProperties" - which is what broke the shading operator. Since then they
    live in `modifier.properties.inputs`, keyed by the node group interface's socket identifier, and each
    entry is an ID property group whose number is under "value":

        modifier.properties.inputs["Input_15"]["value"] = 0.1

    The socket also carries the name it was given in the interface, so the name is used when the
    identifier has moved. Returns False and says so instead of raising, so a mismatched Umashaders.blend
    cannot take the whole operator down with it.
    """
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    keys = list(inputs.keys()) if inputs is not None else []

    candidates = [identifier]
    group = getattr(modifier, "node_group", None)
    interface = getattr(group, "interface", None)
    for item in getattr(interface, "items_tree", None) or []:
        if getattr(item, "item_type", "") == "SOCKET" and item.in_out == 'INPUT' and item.name == name:
            candidates.insert(0, item.identifier)

    for key in candidates:
        if key not in keys:
            continue
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

    # Blender 3.x: the socket values were ID properties on the modifier
    try:
        if identifier in modifier:
            modifier[identifier] = value
            return True
    except TypeError:
        pass

    print(f"[uma_addon] could not set '{name}' ({identifier}) on the '{modifier.name}' modifier; "
          f"the node group offers {keys}")
    return False


def part_type_of(material_name):
    """Which part of the model a material belongs to, by name - the classification used throughout this file."""
    name = (material_name or "").lower()
    if "bdy" in name or "body" in name:
        return "bdy"
    if "hair" in name:
        return "hair"
    if "face" in name or "mayu" in name:
        return "face"
    if "tail" in name:
        return "tail"
    if "eye" in name:
        return "eye"
    return None


class ApplyShader(bpy.types.Operator):
    '''Apply uma shader to the model'''
    bl_idname = "uma.apply_shader"
    bl_label = "Shading"
    bl_options = {'REGISTER', 'UNDO'}

    outlines: bpy.props.BoolProperty(
        name="Outlines",
        default=False,
        description="Also add the Uma Outlines geometry node modifier. Off by default: on Blender 4.2 and "
                    "newer that node group renders the model inside out - the shading lands on the faces "
                    "facing away from the camera and the whole model comes out washed out white - so the "
                    "shader only looks right without it. Enable it if you want to experiment, and set the "
                    "modifier's Camera input to your camera")

    @classmethod
    def poll(cls, context: bpy.types.Context):
        return context.active_object and context.active_object.type == 'MESH' and len(context.selected_objects) == 1

    def execute(self, context: bpy.types.Context):
        obj = context.active_object
        
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
        outline_widths = []
        for slot_index, slot in enumerate(obj.material_slots):
            old_mat = slot.material
            if not old_mat: 
                continue
            # 判断部位类型
            part_type = part_type_of(old_mat.name)
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
                comment = parse_uma_comment(getattr(getattr(old_mat, "mmd_material", None), "comment", ""))
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

                # the shader reads its sun direction from that empty; assign it before anything else
                sun = find_sun_object()
                if sun is not None:
                    wired = 0
                    for n in nodes:
                        if n.type == 'GROUP' and n.node_tree:
                            wired += assign_sun_direction(n.node_tree, sun)
                    if wired:
                        print(f"[uma_addon] sun direction taken from {sun.name!r} "
                              f"({wired} Object Info node(s) assigned)")
                else:
                    print("[uma_addon] no 'Uma Sun [DO NOT DELETE]' empty in the scene: the shader has no sun "
                          "direction and will shade flat. Re-append the Icosphere of Materials collection.")

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
                        fallback = os.path.join(tex_dir, tex_diff.replace("diff", "ctrl"))
                        if not os.path.exists(fallback):
                            fallback = find_file_by_keywords(tex_dir, [part_type, 'ctrl'])
                        path = comment_texture_path(tex_dir, comment, "option", fallback)
                        if path:
                            node.image = bpy.data.images.load(path, check_existing=True)
                            node.image.colorspace_settings.name = 'Non-Color'
                        else:
                            print(f"{fallback} not found")

                    node = nodes.get("Image Texture.002")
                    if node:
                        fallback = os.path.join(tex_dir, tex_diff.replace("diff", "shad_c"))
                        path = comment_texture_path(tex_dir, comment, "toon", fallback)
                        if path:
                            node.image = bpy.data.images.load(path, check_existing=True)
                            node.image.colorspace_settings.name = 'sRGB'
                        else:
                            print(f"{fallback} not found")

                    node = nodes.get("Image Texture.003")
                    if node:
                        fallback = os.path.join(tex_dir, tex_diff.replace("diff", "base"))
                        if not os.path.exists(fallback):
                            fallback = find_file_by_keywords(tex_dir, [part_type, 'base'])
                        path = comment_texture_path(tex_dir, comment, "triple", fallback)
                        if path:
                            node.image = bpy.data.images.load(path, check_existing=True)
                            node.image.colorspace_settings.name = 'Non-Color'
                        else:
                            print(f"{fallback} not found")

                    node = nodes.get("Image Texture.004")
                    if node:
                        fallback = os.path.join(tex_dir, tex_diff.replace("diff", "emi"))
                        path = comment_texture_path(tex_dir, comment, "emissive", fallback)
                        if path:
                            node.image = bpy.data.images.load(path, check_existing=True)
                            node.image.colorspace_settings.name = 'Non-Color'

                            for n in nodes:
                                if n.type == 'GROUP' and n.node_tree and "Uma Shader" in n.node_tree.name:
                                    n.inputs["Emmission Toggle"].default_value = 1
                        else:
                            print(f"{fallback} not found")

                    if part_type == 'face':
                        for n in nodes:
                            if n.type == 'GROUP' and n.node_tree and "Uma Shader" in n.node_tree.name:
                                n.inputs["Toggle If Face [0=Off,1=On]"].default_value = 1

                    # the values the model was exported with, instead of this addon's defaults
                    if comment:
                        width = comment_float(comment, "outline_width")
                        if width is not None:
                            outline_widths.append(width)
                        for n in nodes:
                            applied = apply_uma_parameters(n, comment)
                            if applied:
                                print(f"[uma_addon] {new_mat.name}: took {len(applied)} shader setting(s) from "
                                      f"the exported material comment: {', '.join(applied)}")


        # 为选中模型加上 Uma Outlines 几何节点
        #
        # Off by default. Measured on Blender 5.2 with a real model: with this modifier the whole model renders
        # inside out - the shading lands on the faces pointing away from the camera and the surface comes out
        # washed out white, which is exactly what a freshly applied shader used to look like. Without it the
        # shader renders correctly. Setting the modifier's camera (which upstream's README asks for) does not
        # change that, and neither does the sun direction, so it is the node group itself.
        if self.outlines:
            if "Uma Outlines" not in obj.modifiers:
                if "Uma Outlines" not in bpy.data.node_groups:
                    with bpy.data.libraries.load(blend_file, link=False) as (data_from, data_to):
                        if "Uma Outlines" in data_from.node_groups:
                            data_to.node_groups.append("Uma Outlines")
                node_group = bpy.data.node_groups.get("Uma Outlines")
                if node_group:
                    geom_mod = obj.modifiers.new(name="Uma Outlines", type='NODES')
                    geom_mod.node_group = node_group
        elif obj.modifiers.get("Uma Outlines") is not None:
            obj.modifiers.remove(obj.modifiers.get("Uma Outlines"))
            print("[uma_addon] removed the 'Uma Outlines' modifier: on Blender 4.2 and newer that node group "
                  "renders the model inside out, so the shader only looks right without it. Re-run Shading "
                  "with Outlines enabled if you want to try it anyway.")
            self.report({'WARNING'}, "Outlines removed: that node group renders the model inside out on this "
                                     "Blender version, so it is off by default")

        geom_mod = obj.modifiers.get("Uma Outlines")
        if geom_mod is not None:
            # the outline's depth offset: it used to be written as a modifier ID property, which Blender
            # 4.0+ refuses, so the operator died here every time it was run
            set_geometry_modifier_input(geom_mod, "Depth Offset", "Input_15", 0.1)

            if outline_widths:
                # the game's outline width and this node group's Thickness are on the same scale (0.35 in the
                # shipped group, 0.325 in the material this was measured on), so the exported value is used as
                # it is. The most common value wins rather than the average, so one part asking for a much
                # thicker outline does not drag the whole model's outline with it.
                width = Counter(outline_widths).most_common(1)[0][0]
                if set_geometry_modifier_input(geom_mod, "Thickness", "Input_11", width):
                    print(f"[uma_addon] outline thickness {width:.4f}, the most common of "
                          f"{len(outline_widths)} exported material(s): {sorted(set(outline_widths))}")

            # the upstream README is explicit that the outline node group needs the camera it renders for,
            # otherwise it draws garbage on parts facing away from it
            camera = bpy.context.scene.camera
            if camera is not None:
                if set_geometry_modifier_input(geom_mod, "Camera", "Input_13", camera):
                    print(f"[uma_addon] outline camera set to {camera.name!r}")
            else:
                print("[uma_addon] no camera in the scene, so the outline node group has none to work with - "
                      "it only looks right from the camera it is given")


        self.report({'INFO'}, "Uma shader applied")
        return {'FINISHED'}
