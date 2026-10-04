"""One control for every uma's shading: global settings on the sun empty, per-uma overrides on each root empty.

Why it exists
-------------
The Uma Shader ignores scene lighting: its brightness comes from the `* Intensity` inputs of the `Uma Shader`
node group, and `Shading` makes **one material copy per shader-able slot** (docs/RIGIFY_CHAINS_HANDOFF.md 9as.2).
So a scene with four characters and eight copies each has thirty-two places where "Diffuse Intensity" lives, and
matching them by hand is what this replaces.

How it works
------------
Every setting is a driver on the socket of every copy, resolving **override -> global**:

    nodes["Group"].inputs[i].default_value   <-   local if local >= 0 else glob

* the **global** value is an ID property on the `Uma Sun [DO NOT DELETE]` empty the shader library brings in;
* the **override** is the same property on the character's own root empty (the empty mmd_tools makes for the
  model, named after it), and `-1` means *inherit the global*. A sentinel rather than a switch per character,
  because it lets one character differ in one setting and follow in the rest.

Measured, not assumed (`shader_driver_eval_check.py`, `shader_control_check.py`, `shader_colour_driver_check.py`):

* a driver on a shader group's input socket follows the property it points at - 0.5 -> 0.5, 2.0 -> 2.0;
* so does the `local if local >= 0 else glob` expression, in both directions (inherit 0.7, override 1.4, and the
  global moving under an override leaves it at 1.4);
* **a colour socket takes four drivers, one per channel.** `driver_add` on the 4-float path returns four curves,
  and a single driver whose variable is the whole array reads `[0, 0, 0, 0]` - so colours are driven channel by
  channel and there is no cheaper route;
* a full set is 36 drivers per copy (12 numeric + 6 colours x 4), 252 on a seven-copy model, created in 0.039 s,
  and a frame change costs 0.0008 s with them against 0.0005 s without. It is cheap enough that there is no
  reason to make it an option;
* `id_properties_ui` gives a plain ID property a minimum, a soft range, a description and, for the colours, the
  `COLOR` subtype - so the values read as sliders and swatches in Blender's own Custom Properties panel too.

What is *not* controlled, on purpose: the five texture inputs and the two per-part toggles (`Toggle If Face`,
`Emmission Toggle`). Those say *what a part is*, and `Shading` sets them per material.

A note on the earlier "drivers do not work here" measurement
------------------------------------------------------------
`shader_driver_check.py` and `shader_socket_driver_check.py` found every route invalid, including a driver on a
plain Material custom property. That was the harness, not Blender: a Python write to a custom property does not
tag the datablock, so the dependency graph never re-evaluated the drivers and each kept whatever value it had the
first time. `shader_driver_eval_check.py` shows all five routes working once the source is tagged. The same trap
is why `_settle()` below tags before it verifies, and why `verify()` exists at all.
"""

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator

#: The tag reported by the operators and printed when drivers are hooked up, so a report can be told apart from
#: one produced by a stale copy of this file (the same convention RigifyChains uses with its build tag).
TAG = "look-1"

#: The empty the shader library brings in, and the object the global settings live on.
SUN_NAME = "Uma Sun [DO NOT DELETE]"

#: A group node whose node tree name contains one of these is a shader the control drives. The eye half is
#: defensive: measured, the shipped `Uma Eyes` material carries **no group node at all** (18 nodes of Mix, Math
#: and Image Texture), so an eye copy is not driven by this control and never will be - its look is its own
#: emission setup. The hint stays for files that do have such a group, and the control is driven by the socket
#: names a group actually offers rather than by which group it is.
GROUP_HINTS = ("Uma Shader", "Uma Eyes")

#: The shader library's own materials and the collection its helper objects live in. `Shading` appends an
#: "Icosphere of Materials" collection whose sphere carries the library's master materials as slots - a mesh with
#: a material that has a `Uma Shader` group node, i.e. exactly what a character looks like from the outside. It
#: is not one: it has no textures, no root empty of its own and nothing to override, and counting it as a
#: character gave it a set of drivers and made it the seed for the global look (measured: 288 drivers where 252
#: were expected, and the global seeded from the master material's own defaults).
LIBRARY_MATERIALS = ("Uma Shader", "Uma Eyes", "Uma Outline Material")
LIBRARY_COLLECTION_HINT = "Icosphere"

#: The value that means "inherit the global setting". Anything below zero is inherit.
INHERIT = -1.0

#: Every setting the control drives, in the order the panel shows them.
#:
#: `key` is the ID property name on the sun and root empties (so a driver's data path is `'["uma_shader_x"]'`),
#: `input` is the socket on the shader group, and the two are deliberately similar rather than identical: the
#: socket names are upstream's and must match it exactly, the property names are ours and must stay stable, since
#: they are written into drivers that live in the .blend.
SETTINGS = (
    {"key": "uma_shader_light_threshold", "input": "Light Threshold", "kind": "number", "default": 0.2,
     "min": 0.0, "max": 4.0, "soft_min": 0.0, "soft_max": 1.0,
     "description": "How dark a shaded area has to be before it counts as shadow. Lower catches more of the "
                    "model; higher keeps more of the flat colour"},
    {"key": "uma_shader_diffuse_intensity", "input": "Diffuse Intensity", "kind": "number", "default": 1.0,
     "min": 0.0, "max": 8.0, "soft_min": 0.0, "soft_max": 3.0,
     "description": "Strength of the lit (diffuse) side of the character"},
    {"key": "uma_shader_diffuse_saturation", "input": "Diffuse Saturation", "kind": "number", "default": 1.0,
     "min": 0.0, "max": 4.0, "soft_min": 0.0, "soft_max": 2.0,
     "description": "Colour saturation of the lit side. 0 is greyscale, 1 is the texture's own colours"},
    {"key": "uma_shader_shaded_intensity", "input": "Shaded Intensity", "kind": "number", "default": 1.0,
     "min": 0.0, "max": 8.0, "soft_min": 0.0, "soft_max": 3.0,
     "description": "Strength of the shadowed side"},
    {"key": "uma_shader_shaded_saturation", "input": "Shaded Saturation", "kind": "number", "default": 1.0,
     "min": 0.0, "max": 4.0, "soft_min": 0.0, "soft_max": 2.0,
     "description": "Colour saturation of the shadowed side"},
    {"key": "uma_shader_metallic_intensity", "input": "Metallic Intensity", "kind": "number", "default": 1.0,
     "min": 0.0, "max": 8.0, "soft_min": 0.0, "soft_max": 3.0,
     "description": "Strength of the metallic response"},
    {"key": "uma_shader_highlight_intensity", "input": "Highlight Intensity", "kind": "number", "default": 1.0,
     "min": 0.0, "max": 8.0, "soft_min": 0.0, "soft_max": 3.0,
     "description": "Strength of the specular highlight"},
    {"key": "uma_shader_ambient_rimlight_size", "input": "Ambient Rimlight Size", "kind": "number", "default": 0.1,
     "min": 0.0, "max": 4.0, "soft_min": 0.0, "soft_max": 1.0,
     "description": "Width of the ambient rim, the light that wraps the whole silhouette"},
    {"key": "uma_shader_ambient_rimlight_intensity", "input": "Ambient Rimlight Intensity", "kind": "number",
     "default": 1.0, "min": 0.0, "max": 8.0, "soft_min": 0.0, "soft_max": 3.0,
     "description": "Strength of the ambient rim"},
    {"key": "uma_shader_rimlight_size", "input": "Rimlight Size", "kind": "number", "default": 0.1,
     "min": 0.0, "max": 4.0, "soft_min": 0.0, "soft_max": 1.0,
     "description": "Width of the directional rim, the edge light on the lit side"},
    {"key": "uma_shader_rimlight_intensity", "input": "Rimlight Intensity", "kind": "number", "default": 1.0,
     "min": 0.0, "max": 8.0, "soft_min": 0.0, "soft_max": 3.0,
     "description": "Strength of the directional rim"},
    {"key": "uma_shader_emission_intensity", "input": "Emission Intensity", "kind": "number", "default": 1.0,
     "min": 0.0, "max": 8.0, "soft_min": 0.0, "soft_max": 3.0,
     "description": "Strength of the glow on the parts that have an emission texture (the eyes, mostly)"},
    {"key": "uma_shader_diffuse_color", "input": "Diffuse Color", "kind": "colour", "default": (1.0, 1.0, 1.0, 1.0),
     "description": "Tint of the lit side, multiplied over the texture"},
    {"key": "uma_shader_shaded_color", "input": "Shaded Color", "kind": "colour", "default": (1.0, 1.0, 1.0, 1.0),
     "description": "Tint of the shadowed side, multiplied over the texture"},
    {"key": "uma_shader_metallic_color", "input": "Metallic Color", "kind": "colour", "default": (1.0, 1.0, 1.0, 1.0),
     "description": "Tint of the metallic response"},
    {"key": "uma_shader_highlight_color", "input": "Highlight Color", "kind": "colour", "default": (1.0, 1.0, 1.0, 1.0),
     "description": "Tint of the specular highlight"},
    {"key": "uma_shader_ambient_rimlight_color", "input": "Ambient Rimlight Color", "kind": "colour",
     "default": (1.0, 1.0, 1.0, 1.0), "description": "Tint of the ambient rim"},
    {"key": "uma_shader_rimlight_color", "input": "Rimlight Color", "kind": "colour",
     "default": (1.0, 1.0, 1.0, 1.0), "description": "Tint of the directional rim"},
)

SETTING_BY_KEY = {setting["key"]: setting for setting in SETTINGS}
SETTING_BY_INPUT = {setting["input"]: setting for setting in SETTINGS}
KEYS = tuple(setting["key"] for setting in SETTINGS)

#: Numeric settings first, then the colours: the panel groups them that way and so does this list.
NUMERIC_KEYS = tuple(setting["key"] for setting in SETTINGS if setting["kind"] == "number")
COLOUR_KEYS = tuple(setting["key"] for setting in SETTINGS if setting["kind"] == "colour")


# --- what is in the scene ------------------------------------------------------------------------------

def sun_object(context=None):
    """The empty the global settings live on: the shader library's own sun, or None if shading has not run."""
    sun = bpy.data.objects.get(SUN_NAME)
    if sun is None:
        # the name carries a suffix a user could have edited, and a renamed object is still the sun
        sun = next((obj for obj in bpy.data.objects if obj.name.startswith("Uma Sun")), None)
    return sun


def root_object(obj):
    """The object that stands for one character: the top of `obj`'s parent chain, or `obj` itself.

    mmd_tools imports a model as a root empty (named after the model) with the armature and the mesh under it,
    which is the host the per-uma overrides want - one per character, and the same one for every part of it.
    """
    if obj is None:
        return None
    top = obj
    seen = set()
    while top.parent is not None and top.parent.name not in seen:
        seen.add(top.name)
        top = top.parent
    return top


def shader_nodes(material):
    """Every group node in `material` that is one of the uma shaders."""
    if material is None or material.node_tree is None:
        return []
    return [node for node in material.node_tree.nodes
            if node.type == 'GROUP' and node.node_tree is not None
            and any(hint in node.node_tree.name for hint in GROUP_HINTS)]


def is_library_object(obj):
    """True for the shader library's own helper objects (the icosphere), which are not characters."""
    if obj is None:
        return True
    return any(LIBRARY_COLLECTION_HINT in collection.name for collection in obj.users_collection)


def is_library_material(material):
    """True for the library's master materials - the ones `Shading` copies *from*, not the copies."""
    return material is None or material.name in LIBRARY_MATERIALS


def character_materials(objects):
    """The materials a set of objects uses that carry a shader group - eyes included, they are copies too."""
    found = []
    for obj in objects:
        if obj is None or is_library_object(obj) or not hasattr(obj, "material_slots"):
            continue
        for slot in obj.material_slots:
            material = slot.material
            if (material is not None and not is_library_material(material) and shader_nodes(material)
                    and material not in found):
                found.append(material)
    return found


def character_objects(context):
    """The objects of the selected character - every mesh sharing the active object's root empty."""
    active = context.active_object
    if active is None:
        return None, []
    root = root_object(active)
    if root is None:
        return None, []
    objects = [obj for obj in context.scene.objects
               if root_object(obj) is root and obj.type == 'MESH' and not is_library_object(obj)]
    if not objects and active.type == 'MESH':
        objects = [active]
    return root, objects


def all_characters(context):
    """Every (root, meshes, materials) triple in the scene that carries a shader."""
    found = []
    for obj in context.scene.objects:
        if obj.type != 'MESH' or is_library_object(obj):
            continue
        root = root_object(obj)
        if root is None or any(root is entry[0] for entry in found):
            continue
        materials = character_materials([obj])
        if materials:
            found.append((root, [obj], materials))
    return found


def resolve_values(context, root=None):
    """What every setting currently comes out as: the global value, or `root`'s override when it has one."""
    sun = sun_object(context)
    values = {}
    for setting in SETTINGS:
        key = setting["key"]
        value = read_value(sun, key, setting) if sun is not None else None
        if value is None:
            value = setting["default"]
        if root is not None:
            override = read_value(root, key, setting)
            if override is not None and not is_inheriting(override, setting):
                value = override
        values[key] = value
    return values


# --- reading and writing the properties -----------------------------------------------------------------

def read_value(obj, key, setting):
    """The property as a plain Python value: a float, or a 4-tuple for a colour. None when it is absent."""
    if obj is None or key not in obj.keys():
        return None
    raw = obj[key]
    if setting["kind"] == "colour":
        try:
            values = [float(component) for component in raw]
        except TypeError:
            return None
        while len(values) < 4:
            values.append(1.0)
        return tuple(values[:4])
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def is_inheriting(value, setting):
    """True when a value means "use the global one" - for a colour, when its first channel does."""
    if value is None:
        return True
    if setting["kind"] == "colour":
        return float(value[0]) < 0.0
    return float(value) < 0.0


def ensure_properties(obj, keys=KEYS, seed=None, inherit=False):
    """Create the settings on `obj` if they are missing, with the UI metadata that makes them usable.

    An ID property is a bare number in Blender's Custom Properties panel until `id_properties_ui` gives it a
    range and a description, and the API needs the property to exist first - hence one function doing both.

    `inherit` is what separates the two hosts: a *global* setting starts at the shader's own default, while a
    character's *override* starts at the sentinel, because "this character follows the global" is the state a
    character is in until somebody says otherwise. Creating overrides at the default instead is a bug worth
    naming, since it looks like nothing is wrong: every character quietly pins itself to 0.2 and the global
    slider appears dead (measured - seven copies stayed at 0.2 with the global at 0.42).
    """
    created = []
    for key in keys:
        setting = SETTING_BY_KEY[key]
        if key not in obj.keys():
            if inherit:
                obj[key] = INHERIT if setting["kind"] == "number" else (INHERIT,) * 4
            else:
                obj[key] = seed.get(key, setting["default"]) if seed else setting["default"]
            created.append(key)
    for key in keys:
        setting = SETTING_BY_KEY[key]
        if key not in created and describe_property(obj, key) == setting.get("description", ""):
            # already described: the panel calls this on every redraw, and a write that cannot change anything is
            # a write worth not making
            continue
        update = {"description": setting.get("description", "")}
        if setting["kind"] == "colour":
            update["subtype"] = 'COLOR'
        else:
            update.update(min=INHERIT, max=setting.get("max", 8.0),
                          soft_min=setting.get("soft_min", 0.0), soft_max=setting.get("soft_max", 2.0))
        try:
            obj.id_properties_ui(key).update(**update)
        except (TypeError, AttributeError, KeyError):
            # a Blender without id_properties_ui keeps working, the properties just draw as plain numbers
            pass
    return created


def describe_property(obj, key):
    """The description this property already carries, or None when it has none (or the API is absent)."""
    try:
        return obj.id_properties_ui(key).as_dict().get("description")
    except (TypeError, AttributeError, KeyError):
        return None


def write_value(obj, key, value):
    """Set one setting, in the shape an ID property wants."""
    setting = SETTING_BY_KEY[key]
    if setting["kind"] == "colour":
        if isinstance(value, (int, float)):
            value = (float(value),) * 4
        values = [float(component) for component in value]
        while len(values) < 4:
            values.append(1.0)
        obj[key] = values[:4]
    else:
        obj[key] = float(value)


def seed_global(context, materials):
    """Give the sun its settings, seeded from what the file already shows so nothing changes on hook-up.

    Only when they are missing: the global is one look for the scene, so a second character shaded into the same
    file must not redefine it. A character that should differ gets overrides, not a second global.
    """
    sun = sun_object(context)
    if sun is None:
        return None, []
    missing = [key for key in KEYS if key not in sun.keys()]
    if not missing:
        return sun, []
    seed = {}
    source = next((material for material in sorted(materials, key=lambda item: item.name)
                   if shader_nodes(material)), None)
    if source is not None:
        for key in missing:
            setting = SETTING_BY_KEY[key]
            for node in shader_nodes(source):
                for index, socket in enumerate(node.inputs):
                    if socket.name != setting["input"]:
                        continue
                    if setting["kind"] == "colour":
                        seed[key] = tuple(float(component) for component in socket.default_value)
                    else:
                        seed[key] = float(socket.default_value)
                    break
    created = ensure_properties(sun, missing, seed or None)
    return sun, created


# --- the drivers ---------------------------------------------------------------------------------------

def _sockets_for(node, setting):
    """The indices of `node`'s inputs carrying this setting - a name, not a position, since the two shader
    groups do not have to agree on an order."""
    return [index for index, socket in enumerate(node.inputs) if socket.name == setting["input"]]


def _own_driver_paths(material):
    """The driver paths this control owns, so it can clear and re-create them without touching anything else."""
    paths = set()
    for node in shader_nodes(material):
        for index, socket in enumerate(node.inputs):
            if socket.name in SETTING_BY_INPUT:
                paths.add('nodes["%s"].inputs[%d].default_value' % (node.name, index))
    return paths


def unhook(materials):
    """Remove this control's drivers, leaving any other animation on those node trees alone."""
    removed = 0
    for material in materials:
        tree = material.node_tree
        if tree is None or tree.animation_data is None:
            continue
        owned = _own_driver_paths(material)
        doomed = [curve for curve in tree.animation_data.drivers if curve.data_path in owned]
        for curve in doomed:
            tree.animation_data.drivers.remove(curve)
            removed += 1
    return removed


def _add_driver(tree, path, index, expression, targets):
    """Add one driver, with one `SINGLE_PROP` variable per (name, id, data path) in `targets`.

    `driver_add` returns a *list* when the path names a whole array, which is how a colour socket answers: it
    makes four curves at once. Those four drivers all see the same variable, and a scalar curve handed a 4-float
    variable evaluates to zero, so colours are driven channel by channel with a scalar data path instead.
    """
    added = tree.driver_add(path, index)
    curves = added if isinstance(added, list) else [added]
    for curve in curves:
        driver = curve.driver
        driver.type = 'SCRIPTED'
        for name, id_object, data_path in targets:
            variable = driver.variables.new()
            variable.name = name
            variable.type = 'SINGLE_PROP'
            variable.targets[0].id = id_object
            variable.targets[0].data_path = data_path
        driver.expression = expression
    return curves


def hook(context, roots=None, materials=None):
    """Put the drivers on every copy of every character, resolving override -> global.

    Returns a report dictionary: how many drivers were made, per character, and anything that had to be said -
    a material shared by two characters, a copy whose shader group lacks a setting, or a driver that did not take.
    """
    sun = sun_object(context)
    report = {"tag": TAG, "drivers": 0, "copies": 0, "characters": [], "notes": [], "verified": None}
    if sun is None:
        report["notes"].append("no '%s' empty in the scene, so Shading has not run here" % SUN_NAME)
        return report

    if roots is None or materials is None:
        characters = all_characters(context)
    else:
        characters = [(root, [], character_materials(materials)) for root in roots]
    if not characters:
        report["notes"].append("no character in the scene carries a uma shader")
        return report

    every_material = []
    for root, _objects, copies in characters:
        every_material.extend(copies)
    seed_global(context, every_material)
    for root, _objects, copies in characters:
        ensure_properties(root, KEYS, inherit=True)

    # a material used by two characters can only follow one of them
    users = {}
    for root, _objects, copies in characters:
        for material in copies:
            users.setdefault(material.name, []).append(root.name)
    for name, owners in users.items():
        if len(set(owners)) > 1:
            report["notes"].append("%s is used by more than one character (%s), so it takes the first one's "
                                   "overrides" % (name, ", ".join(sorted(set(owners)))))

    for root, _objects, copies in characters:
        entry = {"root": root.name, "materials": len(copies), "drivers": 0, "missing": []}
        for material in copies:
            tree = material.node_tree
            unhook([material])          # idempotent: hooking twice leaves one driver per setting, not two
            for node in shader_nodes(material):
                for setting in SETTINGS:
                    key = setting["key"]
                    indices = _sockets_for(node, setting)
                    if not indices:
                        entry["missing"].append("%s / %s" % (material.name, setting["input"]))
                        continue
                    path = 'nodes["%s"].inputs[%d].default_value'
                    targets = [("local", root, '["%s"]' % key), ("glob", sun, '["%s"]' % key)]
                    expression = "local if local >= 0 else glob"
                    for index in indices:
                        if setting["kind"] == "colour":
                            for component in range(4):
                                targets_with_component = [
                                    (name, id_object, '%s[%d]' % (data_path, component))
                                    for name, id_object, data_path in targets]
                                curves = _add_driver(tree, path % (node.name, index), component, expression,
                                                     targets_with_component)
                                entry["drivers"] += len(curves)
                        else:
                            curves = _add_driver(tree, path % (node.name, index), -1, expression, targets)
                            entry["drivers"] += len(curves)
            report["copies"] += 1
        report["characters"].append(entry)
        report["drivers"] += entry["drivers"]

    report["verified"] = verify(context, characters)
    return report


def _settle(context, *objects):
    """Make the dependency graph evaluate the drivers, so a socket can be read back.

    The tag is the whole trick: writing an ID property from Python does not tag its datablock, so without this
    the drivers are never re-evaluated and every one of them keeps the value it had when it was created. This is
    the mistake that made the first two probes report that drivers do not work at all.

    `update_tag()` and `view_layer.update()` are **not** enough on their own, and that was measured the hard way:
    with them alone a material's node tree keeps its old socket values (a global of 0.42 left all seven copies
    reading 0.2), while writing an override on a root *empty* appeared to work, which is what made it look like a
    seeding problem rather than a missing evaluation. A frame change is what re-evaluates animation on data that
    is not part of the view layer's object graph, so that is what this does - at the current frame, so nothing
    moves.
    """
    for obj in objects:
        if obj is not None:
            obj.update_tag()
    scene = getattr(context, "scene", None)
    try:
        if scene is not None:
            scene.frame_set(scene.frame_current)
        context.view_layer.update()
    except (AttributeError, RuntimeError):
        pass


def verify(context, characters=None, settle=True):
    """Check that every driven socket really follows the properties, and say where it does not.

    A driver that fails to evaluate leaves the socket at whatever it was, which looks like "the control is
    ignoring me" rather than an error - so this reads the value back and compares it with the resolved one.

    Resolved **per character**: a character with an override is supposed to differ from the global, and comparing
    every character's copies against the global reported seven failures on a two-character file whose control was
    doing exactly what it had been asked to do.
    """
    if characters is None:
        characters = all_characters(context)
    sun = sun_object(context)
    if settle:
        _settle(context, sun, *[root for root, _objects, _copies in characters])
    global_values = resolve_values(context)
    wrong = []
    checked = 0
    for root, _objects, copies in characters:
        resolved = resolve_values(context, root) if root is not None else global_values
        for material in copies:
            for node in shader_nodes(material):
                for setting in SETTINGS:
                    key = setting["key"]
                    expected = resolved[key]
                    for index in _sockets_for(node, setting):
                        checked += 1
                        socket = node.inputs[index]
                        if setting["kind"] == "colour":
                            actual = tuple(float(component) for component in socket.default_value)
                            if any(abs(a - b) > 1e-4 for a, b in zip(actual, expected)):
                                wrong.append({"material": material.name, "setting": setting["input"],
                                              "expected": list(expected), "actual": list(actual)})
                        else:
                            actual = float(socket.default_value)
                            if abs(actual - float(expected)) > 1e-4:
                                wrong.append({"material": material.name, "setting": setting["input"],
                                              "expected": expected, "actual": actual})
    return {"checked": checked, "wrong": wrong[:20], "wrong_count": len(wrong)}


def bake(context, roots=None):
    """Write the resolved values into the copies and take the drivers out.

    What a character needs before it is exported or handed to somebody who will not have the sun empty: the
    values stop following anything, and every socket becomes editable by hand again.

    Each character is written with *its own* resolved values, overrides included - baking the global into a
    character that had an override would quietly throw that character's look away, which is the one thing a bake
    must not do.
    """
    if roots is None:
        characters = all_characters(context)
    else:
        characters = [(root, [], character_materials([obj for obj in context.scene.objects
                                                      if root_object(obj) is root and obj.type == 'MESH']))
                      for root in roots]
    global_values = resolve_values(context)
    written = 0
    materials = []
    for root, _objects, copies in characters:
        resolved = resolve_values(context, root) if root is not None else global_values
        materials.extend(copies)
        for material in copies:
            for node in shader_nodes(material):
                for setting in SETTINGS:
                    for index in _sockets_for(node, setting):
                        socket = node.inputs[index]
                        value = resolved[setting["key"]]
                        if setting["kind"] == "colour":
                            socket.default_value = tuple(value)
                        else:
                            socket.default_value = float(value)
                        written += 1
    removed = unhook(materials)
    return {"written": written, "drivers_removed": removed, "materials": len(materials), "tag": TAG}


def status(context):
    """Which mode the selected character is in: `driven`, `baked`, or `none` when no shader copy has drivers."""
    _root, objects = character_objects(context)
    materials = character_materials(objects)
    if not materials:
        return "none", 0
    driven = 0
    for material in materials:
        tree = material.node_tree
        if tree is None or tree.animation_data is None:
            continue
        owned = _own_driver_paths(material)
        driven += len([curve for curve in tree.animation_data.drivers if curve.data_path in owned])
    if driven:
        return "driven", driven
    return "baked", 0


# --- operators -----------------------------------------------------------------------------------------

class ShaderControlHook(Operator):
    """Connect the shader settings of every uma to the sun empty and to each character's own overrides"""
    bl_idname = "uma.shader_hook"
    bl_label = "Hook up the shading control"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sun = sun_object(context)
        if sun is None:
            self.report({'ERROR'}, "No '%s' empty in this scene: run Shading on a character first" % SUN_NAME)
            return {'CANCELLED'}
        report = hook(context)
        if not report["copies"]:
            self.report({'ERROR'}, "No character in this scene carries a uma shader (%s)"
                        % (report["notes"][0] if report["notes"] else "nothing to hook up"))
            return {'CANCELLED'}
        verification = report["verified"] or {}
        if verification.get("wrong_count"):
            first = verification["wrong"][0]
            self.report({'WARNING'}, "%s [%s]: %s did not follow the control (%s instead of %s, %d driver(s) "
                        "made)" % (TAG, first["material"], first["setting"], first["actual"],
                                   first["expected"], report["drivers"]))
        else:
            self.report({'INFO'}, "%s: %d driver(s) over %d cop(y/ies), %d value(s) checked and all follow"
                        % (TAG, report["drivers"], report["copies"], verification.get("checked", 0)))
        for note in report["notes"]:
            print("[uma_addon] %s: %s" % (TAG, note))
        return {'FINISHED'}


class ShaderControlBake(Operator):
    """Write the current shading into the materials and remove the drivers, for export or for handing on"""
    bl_idname = "uma.shader_bake"
    bl_label = "Bake into materials"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        result = bake(context)
        if not result["written"]:
            self.report({'ERROR'}, "Nothing to bake: no character here carries a uma shader")
            return {'CANCELLED'}
        self.report({'INFO'}, "%s: %d value(s) written into %d material(s), %d driver(s) removed - the sliders "
                    "do nothing until Hook up is run again" % (TAG, result["written"], result["materials"],
                                                               result["drivers_removed"]))
        return {'FINISHED'}


class ShaderControlOverride(Operator):
    """Follow the global value for this setting, or give this character its own"""
    bl_idname = "uma.shader_override"
    bl_label = "Override this setting for this character"
    bl_options = {'REGISTER', 'UNDO'}

    key: StringProperty(name="Setting")

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        setting = SETTING_BY_KEY.get(self.key)
        if setting is None:
            self.report({'ERROR'}, "Unknown shading setting %r" % self.key)
            return {'CANCELLED'}
        root, _objects = character_objects(context)
        if root is None:
            self.report({'ERROR'}, "No character selected")
            return {'CANCELLED'}
        ensure_properties(root, (self.key,), inherit=True)
        current = read_value(root, self.key, setting)
        if is_inheriting(current, setting):
            # start the override from what the character shows now, so switching it on changes nothing
            global_value = read_value(sun_object(context), self.key, setting) or setting["default"]
            write_value(root, self.key, global_value)
            self.report({'INFO'}, "%s now overrides %s (at %s)" % (root.name, setting["input"], global_value))
        else:
            write_value(root, self.key, INHERIT if setting["kind"] == "number" else (INHERIT,) * 4)
            self.report({'INFO'}, "%s follows the global %s again" % (root.name, setting["input"]))
        _settle(context, sun_object(context), root)
        return {'FINISHED'}


class ShaderControlCopyGlobal(Operator):
    """Give this character every global value, as a known starting point for its own look"""
    bl_idname = "uma.shader_copy_global"
    bl_label = "Copy the global values to this character"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        root, _objects = character_objects(context)
        sun = sun_object(context)
        if root is None or sun is None:
            self.report({'ERROR'}, "Select a character and run Shading first")
            return {'CANCELLED'}
        ensure_properties(root, KEYS, inherit=True)
        for key in KEYS:
            setting = SETTING_BY_KEY[key]
            value = read_value(sun, key, setting)
            if value is None:
                value = setting["default"]
            write_value(root, key, value)
        _settle(context, sun, root)
        self.report({'INFO'}, "%s now holds the global values as its own" % root.name)
        return {'FINISHED'}


class ShaderControlFollowGlobal(Operator):
    """Put every one of this character's settings back to following the global"""
    bl_idname = "uma.shader_follow_global"
    bl_label = "Follow the global everywhere"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        root, _objects = character_objects(context)
        if root is None:
            self.report({'ERROR'}, "Select a character")
            return {'CANCELLED'}
        ensure_properties(root, KEYS, inherit=True)
        for key in KEYS:
            setting = SETTING_BY_KEY[key]
            write_value(root, key, INHERIT if setting["kind"] == "number" else (INHERIT,) * 4)
        _settle(context, sun_object(context), root)
        self.report({'INFO'}, "%s follows the global shading everywhere again" % root.name)
        return {'FINISHED'}


# --- the panel -----------------------------------------------------------------------------------------

def _draw_global(layout, sun):
    """All umas: the sliders everything follows unless a character overrides them."""
    box = layout.box()
    box.label(text="All umas in this scene", icon='WORLD')
    for key in NUMERIC_KEYS:
        box.prop(sun, '["%s"]' % key, text=SETTING_BY_KEY[key]["input"], slider=True)
    box.label(text="Colours")
    grid = box.grid_flow(columns=2, align=True)
    for key in COLOUR_KEYS:
        grid.prop(sun, '["%s"]' % key, text=SETTING_BY_KEY[key]["input"])


def _draw_override(layout, root, key, icon_locked='LOCKED', icon_free='UNLOCKED'):
    """One row: the follow/override switch, the setting's name, and its value or the word `global`."""
    setting = SETTING_BY_KEY[key]
    value = read_value(root, key, setting)
    inheriting = is_inheriting(value, setting)
    row = layout.row(align=True)
    operator = row.operator(ShaderControlOverride.bl_idname, text="",
                            icon=icon_locked if inheriting else icon_free, emboss=False)
    operator.key = key
    row.label(text=setting["input"])
    if inheriting:
        row.label(text="global")
    else:
        sub = row.row(align=True)
        sub.prop(root, '["%s"]' % key, text="", slider=setting["kind"] == "number")


def draw_shading_control(layout, context):
    """The panel section: what mode the scene is in, the global settings, then the selected character's."""
    sun = sun_object(context)
    root, objects = character_objects(context)
    mode, drivers = status(context)

    row = layout.row(align=True)
    row.operator(ShaderControlHook.bl_idname, icon='DRIVER')
    row.operator(ShaderControlBake.bl_idname, icon='FREEZE')
    if mode == "driven":
        layout.label(text="Following the global values (%d driver(s))" % drivers, icon='CHECKMARK')
    elif mode == "baked":
        layout.label(text="Baked into the materials - the sliders do nothing until Hook up is run again",
                     icon='ERROR')
    else:
        layout.label(text="No uma shader here yet: run Shading on a character", icon='INFO')

    if sun is None:
        return
    ensure_properties(sun, KEYS)
    _draw_global(layout, sun)

    if root is None or not objects:
        box = layout.box()
        box.label(text="Select a character to give it its own values", icon='INFO')
        return

    materials = character_materials(objects)
    box = layout.box()
    box.label(text="%s only" % root.name, icon='OBJECT_DATA')
    ensure_properties(root, KEYS, inherit=True)
    if len(materials) != 1:
        box.label(text="%d shader material(s) in this character" % len(materials))
    row = box.row(align=True)
    row.operator(ShaderControlCopyGlobal.bl_idname, icon='COPYDOWN')
    row.operator(ShaderControlFollowGlobal.bl_idname, icon='LOOP_BACK')
    for key in NUMERIC_KEYS:
        _draw_override(box, root, key)
    box.label(text="Colours")
    grid = box.grid_flow(columns=1, align=True)
    for key in COLOUR_KEYS:
        _draw_override(grid, root, key)
