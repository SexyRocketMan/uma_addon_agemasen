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

Where the settings come from
----------------------------
The list of settings is **the shader's own interface**, so it is not in this file any more: it is the `settings`
table of the shader's manifest (`operators/shaders/<id>.json`, read by `operators/ShaderManifest.py`), which also
names the sun empty and the groups a shader material is recognised by. `TAG` names the version of this file, so a
report can be told apart from one a stale copy produced. Everything *else* here - the drivers, the override
sentinel, the verification, the reorder - is name-driven and did not change.

A note on the earlier "drivers do not work here" measurement
------------------------------------------------------------
`shader_driver_check.py` and `shader_socket_driver_check.py` found every route invalid, including a driver on a
plain Material custom property. That was the harness, not Blender: a Python write to a custom property does not
tag the datablock, so the dependency graph never re-evaluated the drivers and each kept whatever value it had the
first time. `shader_driver_eval_check.py` shows all five routes working once the source is tagged. The same trap
is why `_settle()` below tags before it verifies, and why `verify()` exists at all.
"""

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty
from bpy.types import Menu, Operator

from . import ShaderManifest

#: The tag reported by the operators and printed when drivers are hooked up, so a report can be told apart from
#: one produced by a stale copy of this file (the same convention RigifyChains uses with its build tag). `look-2`
#: stopped this file owning the settings table; `look-3` made the table follow the shader the *scene* chooses.
TAG = "look-3"

# --- the shader's own names, from its manifest ----------------------------------------------------------
#
# `SUN_NAME`, `GROUP_HINTS`, `LIBRARY_MATERIALS` and the settings table were literals in this file. The table is not
# really *our* data: it is the shader's interface - its socket names, in its order, with the defaults and ranges the
# panel shows - so a shader with one socket more than ours meant editing this file as well. It comes from
# `operators/shaders/<id>.json` (`operators/ShaderManifest.py`), and since a scene can now choose its shader the
# names below are **rebound** rather than constants: `use_table(context)` points them at one shader's manifest and
# is called at every entry point (see its own docstring for why that is the design and what it costs).

MANIFEST = None
MANIFEST_PROBLEMS = []
TABLE_ID = ""
SETTINGS = ()
SETTING_BY_KEY = {}
SETTING_BY_INPUT = {}
KEYS = ()
SECTION_ORDER = ()
NUMERIC_KEYS = ()
COLOUR_KEYS = ()
SUN_NAME = "Uma Sun"
GROUP_HINTS = ()
LIBRARY_MATERIALS = ()
LIBRARY_COLLECTION_HINT = ""

#: The value that means "inherit the global setting". Anything below zero is inherit.
INHERIT = -1.0


def table_for(manifest):
    """Everything the rest of this file reads out of one manifest, in one place.

    * `settings`, `by_key` and `by_input`: the table itself. `key` is the ID property name on the sun and root
      empties (so a driver's data path is `'["uma_shader_x"]'`), `input` is the socket on the shader group, and the
      two are deliberately similar rather than identical: the socket names are upstream's and must match it exactly,
      the property names are ours and must stay stable, since they are written into drivers that live in the .blend.
      `section` is the shader's own grouping - it follows its interface order exactly, so a row is where the shader
      puts it, and this order is also the order the properties are created in, which is the order Blender's Custom
      Properties list shows them in (measured: an ID property group keeps its insertion order, and `hook()`
      re-orders a file saved by an earlier build).
    * `keys`/`sections`/`numeric`/`colours`: the derived views the panel and the operators use.
    * `sun`: the empty the shader library brings in, and the object the global settings live on.
    * `group_hints`: a group node whose node tree name contains one of these is a shader the control drives. The eye
      half is defensive: measured, the shipped `Uma Eyes` material carries **no group node at all** (18 nodes of
      Mix, Math and Image Texture), so an eye copy is not driven by this control and never will be - its look is its
      own emission setup. The hint stays for files that do have such a group, and the control is driven by the
      socket names a group actually offers rather than by which group it is.
    * `library_materials` and `collection_hint`: the shader library's own materials and the collection its helper
      objects live in. `Shading` appends an "Icosphere of Materials" collection whose sphere carries the library's
      master materials as slots - a mesh with a material that has a `Uma Shader` group node, i.e. exactly what a
      character looks like from the outside. It is not one: it has no textures, no root empty of its own and nothing
      to override, and counting it as a character gave it a set of drivers and made it the seed for the global look
      (measured: 288 drivers where 252 were expected, and the global seeded from the master material's own
      defaults).
    """
    manifest = manifest or {}
    settings = tuple(manifest.get("settings") or ())
    materials = manifest.get("materials") or {}
    return {
        "id": manifest.get("id") or "",
        "settings": settings,
        "by_key": {row["key"]: row for row in settings},
        "by_input": {row["input"]: row for row in settings},
        "keys": tuple(row["key"] for row in settings),
        "sections": tuple(dict.fromkeys(row["section"] for row in settings)),
        "numeric": tuple(row["key"] for row in settings if row.get("kind") == "number"),
        "colours": tuple(row["key"] for row in settings if row.get("kind") == "colour"),
        "sun": manifest.get("sun") or "Uma Sun",
        "group_hints": tuple(manifest.get("group_hints") or ()),
        "library_materials": tuple(materials.values()),
        "collection_hint": manifest.get("collection") or "",
    }


def use_table(context=None):
    """Point every name above at one shader's table, and return `(manifest, problems)`.

    The names are module globals because about twenty functions read them, and a scene can choose its shader, so
    they are *rebound* at the entry points instead of being constants of the default shader. The rule that makes
    that safe: **every public entry point starts with this call** - `hook`, `verify`, `bake`, `status`,
    `draw_shading_control`, `sun_object` (which is asked which sun a scene's settings live on, i.e. which shader the
    scene chose) and the operators that call them. A stale table would be silent rather than loud, which is why the
    id is kept in `TABLE_ID`, reported by `describe()`, and written onto the sun by `hook()` so the panel can say
    when the drivers in a file were made for a different shader than the one now selected.
    """
    global MANIFEST, MANIFEST_PROBLEMS, TABLE_ID
    global SETTINGS, SETTING_BY_KEY, SETTING_BY_INPUT, KEYS, SECTION_ORDER, NUMERIC_KEYS, COLOUR_KEYS
    global SUN_NAME, GROUP_HINTS, LIBRARY_MATERIALS, LIBRARY_COLLECTION_HINT
    MANIFEST, MANIFEST_PROBLEMS = ShaderManifest.active(context)
    table = table_for(MANIFEST)
    TABLE_ID = table["id"]
    SETTINGS = table["settings"]
    SETTING_BY_KEY = table["by_key"]
    SETTING_BY_INPUT = table["by_input"]
    KEYS = table["keys"]
    SECTION_ORDER = table["sections"]
    NUMERIC_KEYS = table["numeric"]
    COLOUR_KEYS = table["colours"]
    SUN_NAME = table["sun"]
    GROUP_HINTS = table["group_hints"]
    LIBRARY_MATERIALS = table["library_materials"]
    LIBRARY_COLLECTION_HINT = table["collection_hint"]
    return MANIFEST, MANIFEST_PROBLEMS


def describe():
    """One line naming the shader a run used, for a report: an id and how many settings it drives."""
    return "%s (%d setting(s))" % (TABLE_ID or "no manifest", len(SETTINGS))


# The default shader's table, so the names exist for everything defined below (a default argument like
# `keys=KEYS` is bound when the function is defined, which is why those two take `None` and read the global).
use_table()


def section_keys(section):
    """The settings in one section, in the shader's order."""
    return tuple(setting["key"] for setting in SETTINGS if setting["section"] == section)


# --- what is in the scene ------------------------------------------------------------------------------

def library_collection(context=None):
    """The library collection of the shader this scene is set to, when the scene holds it.

    Both shader files name their collection the same ("Icosphere of Materials Collection"), so a scene that shaded
    with one shader and then with another holds two of them, each with its own sphere and its own `Uma Sun`. The
    tag `Shading` writes with the append (`ShaderManifest.SOURCE_PROPERTY`) is what tells them apart; a collection
    with no tag is from a build before that property existed, which could only have been the default shader's.
    """
    hint = LIBRARY_COLLECTION_HINT
    if not hint:
        return None
    wanted = TABLE_ID or ShaderManifest.DEFAULT_ID
    for collection in bpy.data.collections:
        if hint in collection.name and collection.get(ShaderManifest.SOURCE_PROPERTY) == wanted:
            return collection
    return next((collection for collection in bpy.data.collections if hint in collection.name), None)


def sun_object(context=None):
    """The empty the global settings live on: the shader library's own sun, or None if shading has not run.

    Asking for the sun is asking *which shader this scene is set to* - each library brings its own - so this is also
    where the table is pointed at that shader's manifest (`use_table`), which makes the panel, the hook-up and the
    verification all agree with the scene's choice. The sun is looked for inside the selected shader's collection
    first, then by name, then by prefix: the name carries a suffix a user could have edited, and a renamed object
    is still the sun.
    """
    use_table(context)
    collection = library_collection(context)
    if collection is not None:
        sun = next((obj for obj in collection.all_objects
                    if obj.type == 'EMPTY' and obj.name.startswith("Uma Sun")), None)
        if sun is not None:
            return sun
    sun = bpy.data.objects.get(SUN_NAME)
    if sun is None:
        sun = next((obj for obj in bpy.data.objects
                    if obj.type == 'EMPTY' and obj.name.startswith("Uma Sun")), None)
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


def character_materials_at(root, context):
    """The shader materials of the character whose root empty is `root`."""
    if root is None:
        return []
    meshes = [obj for obj in context.scene.objects
              if obj.type == 'MESH' and root_object(obj) is root and not is_library_object(obj)]
    return character_materials(meshes)


def panel_character(context):
    """Which character the *panel* is showing: one of hers has to be **selected**, not merely active.

    The user's report: *"the selection goes back to global only if a non-uma object is selected - not when nothing
    is selected, in this case it's stuck at the uma controls."* That is exactly what keying the panel off
    `context.active_object` does: Blender keeps the active object when everything is deselected (Deselect All,
    Alt+A, the Object menu), so "nothing selected" still had an active mesh and the panel kept showing her values.

    So the panel reads `context.selected_objects`, with the active object only as a *tiebreak* among the selected
    ones - several characters selected is a real thing in a two-character file, and the one Blender marks active is
    the one the operators act on, so the panel should agree with them. Nothing selected, or nothing of a
    character's selected, means `None` and the panel shows the global values.
    """
    active = getattr(context.view_layer.objects, "active", None) or context.active_object
    selected = [obj for obj in getattr(context, "selected_objects", []) if obj is not None]
    ordered = ([active] if active is not None and active in selected else []) + \
        [obj for obj in selected if obj is not active]
    seen = []
    for obj in ordered:
        root = root_object(obj)
        if root is None or any(root is entry for entry in seen):
            continue
        materials = character_materials_at(root, context)
        if materials:
            return root, materials
        seen.append(root)
    return None, []


def resolve_values(context, root=None):
    """What every setting currently comes out as: the global value, or `root`'s override when it has one."""
    use_table(context)
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


def ensure_properties(obj, keys=None, seed=None, inherit=False):
    """Create the settings on `obj` if they are missing, with the UI metadata that makes them usable.

    An ID property is a bare number in Blender's Custom Properties panel until `id_properties_ui` gives it a
    range and a description, and the API needs the property to exist first - hence one function doing both.

    `inherit` is what separates the two hosts: a *global* setting starts at the shader's own default, while a
    character's *override* starts at the sentinel, because "this character follows the global" is the state a
    character is in until somebody says otherwise. Creating overrides at the default instead is a bug worth
    naming, since it looks like nothing is wrong: every character quietly pins itself to 0.2 and the global
    slider appears dead (measured - seven copies stayed at 0.2 with the global at 0.42).

    `keys=None` rather than `keys=KEYS` on purpose: a default argument is bound when the function is *defined*, so
    a table that a scene can change would be frozen into it.

    `obj=None` is a scene with no sun yet - there is nothing to create the settings on, and the caller has nothing
    to do about it either, so this returns an empty list rather than raising (see `reorder_properties`).
    """
    if obj is None:
        return []
    keys = KEYS if keys is None else keys
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


def reorder_properties(obj, keys=None):
    """Put the settings in the shader's own order, which is the order Blender's own list shows them in.

    Measured, because the user asked and it decides what is possible (`shader_property_shape_check.py`): an ID
    property group keeps its **insertion order** (created z, a, m -> that order; created m, a, z -> that order),
    the only handle Blender gives on a property carries `as_dict/clear/update/update_from` and **no grouping or
    order field at all**, so sections are impossible in that list - but its order is ours to choose. A driver
    addresses its target by *name*, so deleting and re-creating a driven property keeps the driver working
    (measured: a socket read 0.42 before, 0.42 after), which makes re-ordering a file saved by an earlier build a
    safe one-off.

    Returns the keys it moved, or an empty tuple when they were already in order - and `()` for `None`, which is
    the state a scene is in before its first Shading run: the sun these settings live on arrives with the shader
    library, so "there is no host yet" is normal rather than a mistake, and a caller that forgets to check must not
    take the operator down with an `AttributeError` (which is exactly what choosing a shader in a fresh scene did).
    """
    if obj is None:
        return ()
    keys = KEYS if keys is None else keys
    ours = [name for name in obj.keys() if name in SETTING_BY_KEY]
    wanted = [key for key in keys if key in ours]
    if ours == wanted:
        return ()
    values = {key: read_value(obj, key, SETTING_BY_KEY[key]) for key in wanted}
    for key in wanted:
        del obj[key]
    for key in wanted:
        write_value(obj, key, values[key])
    ensure_properties(obj, wanted)
    return tuple(wanted)


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
    use_table(context)                      # the scene's shader decides the table, so read it here
    sun = sun_object(context)
    report = {"tag": TAG, "drivers": 0, "copies": 0, "characters": [], "notes": [], "verified": None}
    if sun is None:
        report["notes"].append("no '%s' empty in the scene, so Shading has not run here" % SUN_NAME)
        return report
    if MANIFEST is None:
        report["notes"].append("no readable shader manifest (%s), so there are no settings to drive"
                               % "; ".join(MANIFEST_PROBLEMS))
    elif not SETTINGS:
        report["notes"].append("%s has no settings table, so there is nothing to drive"
                               % (MANIFEST.get("name") or MANIFEST.get("id")))

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
        moved = reorder_properties(root)
        if moved:
            report["notes"].append("%s's settings were moved into the shader's own order" % root.name)
    sun_moved = reorder_properties(sun)
    if sun_moved:
        report["notes"].append("the sun's settings were moved into the shader's own order")

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

    # Which shader made these settings, so the panel can say when a file's drivers are for a shader other than the
    # one now selected - the state a scene is in between choosing another shader and pressing Hook up.
    try:
        sun[ShaderManifest.SOURCE_PROPERTY] = TABLE_ID
    except (TypeError, AttributeError):
        pass

    report["verified"] = verify(context, characters)
    report["shader"] = TABLE_ID
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
    use_table(context)
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
    use_table(context)
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
    use_table(context)
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

class ShaderControlChoose(Operator):
    """Point this scene at one of the shipped shaders"""
    bl_idname = "uma.shader_choose"
    bl_label = "Shader"
    bl_description = ("Which shader library this scene shades with. It applies to the next Shading run - materials "
                      "that were already copied keep the shader they were made from - and the panel's rows become "
                      "that shader's own settings")
    bl_options = {'REGISTER', 'UNDO'}

    shader: StringProperty(name="Shader", description="The id of the manifest to use, from operators/shaders/")

    def execute(self, context):
        scene_props = getattr(context.scene, "uma_scene", None)
        if scene_props is None:
            self.report({'ERROR'}, "this scene has no uma add-on properties, so it cannot choose a shader")
            return {'CANCELLED'}
        manifest, problems = ShaderManifest.load(self.shader)
        if manifest is None:
            self.report({'ERROR'}, "no usable shader manifest for %s: %s"
                        % (self.shader, "; ".join(problems or ["unknown reason"])))
            return {'CANCELLED'}

        previous = getattr(scene_props, "shader", "") or ShaderManifest.DEFAULT_ID
        scene_props.shader = self.shader
        use_table(context)

        # Choosing a shader in a scene that has not been shaded yet is the *normal* order (open a file, pick the
        # shader, then shade), and there is no sun to reorder then: the sun arrives with the library `Shading`
        # appends. Asking `reorder_properties` for it anyway was the bug the user hit -
        # `AttributeError: 'NoneType' object has no attribute 'keys'` - and the `was is None` test below it could
        # never fire, because this function returns `()` or a tuple of keys and never None.
        sun = sun_object(context)
        moved = reorder_properties(sun) if sun is not None else ()

        self.report({'INFO'}, "%s: %d setting(s). Shading applies it to the next character you shade; 'Hook up' "
                              "re-writes the drivers from its table" % (describe(), len(SETTINGS)))
        if previous != TABLE_ID:
            print("[uma_addon] %s: shading is now %s (was %s); rows and drivers follow it from here, and the "
                  "collection and sun of the other shader stay in the scene" % (TAG, TABLE_ID, previous))
        if sun is None:
            print("[uma_addon] %s: no '%s' empty in this scene yet - Shading brings the library, and the sun with "
                  "it" % (TAG, SUN_NAME))
        elif moved:
            print("[uma_addon] %s: the sun's settings were moved into %s's order" % (TAG, TABLE_ID))
        return {'FINISHED'}


class ShaderControlMenu(Menu):
    """The chooser's list: one entry per readable manifest, built from the folder rather than hardcoded."""
    bl_idname = "UMA_MT_shader_choose"
    bl_label = "Shader"

    def draw(self, context):
        layout = self.layout
        current = ShaderManifest.chosen_id(context)
        entries = ShaderManifest.items()
        if not entries:
            layout.label(text="no shader manifests found", icon='ERROR')
            return
        for manifest_id, name, tooltip in entries:
            row = layout.row(align=True)
            entry = row.operator(ShaderControlChoose.bl_idname, text=name,
                                 icon='CHECKMARK' if manifest_id == current else 'NONE')
            entry.shader = manifest_id
            if manifest_id == current:
                row.label(text="in use")
        layout.separator()
        layout.label(text="%d shader(s) in operators/shaders/" % len(entries), icon='INFO')


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


class ShaderControlOverrideAll(Operator):
    """Give this uma her own values (starting from the global), or put her back on the global

    The one switch that replaces per-setting padlocks for the common case: on, every setting becomes this
    character's own - seeded with the global's current numbers, so switching it on changes nothing on screen - and
    off, every setting goes back to following. The per-row padlocks stay for the case of one setting that has to
    differ.
    """
    bl_idname = "uma.shader_override_all"
    bl_label = "This uma has her own values"
    bl_options = {'REGISTER', 'UNDO'}

    on: BoolProperty(name="Own values", default=True)

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        root, _objects = character_objects(context)
        sun = sun_object(context)
        if root is None or sun is None:
            self.report({'ERROR'}, "Select a uma first (her root empty, mesh or armature)")
            return {'CANCELLED'}
        ensure_properties(root, KEYS, inherit=True)
        changed = 0
        for key in KEYS:
            setting = SETTING_BY_KEY[key]
            current = read_value(root, key, setting)
            if self.on:
                if is_inheriting(current, setting):
                    value = read_value(sun, key, setting)
                    write_value(root, key, value if value is not None else setting["default"])
                    changed += 1
            elif not is_inheriting(current, setting):
                write_value(root, key, INHERIT if setting["kind"] == "number" else (INHERIT,) * 4)
                changed += 1
        _settle(context, sun, root)
        if self.on:
            self.report({'INFO'}, "%s now holds the global values as her own (%d setting(s))" % (root.name, changed))
        else:
            self.report({'INFO'}, "%s follows the global everywhere again (%d override(s) removed)"
                        % (root.name, changed))
        return {'FINISHED'}


class ShaderControlResetRow(Operator):
    """Put one setting back: to the global's value here, or to the shader's own default globally"""
    bl_idname = "uma.shader_reset_row"
    bl_label = "Reset this setting"
    bl_options = {'REGISTER', 'UNDO'}

    key: StringProperty(name="Setting")
    local: BoolProperty(name="This uma", default=True)

    def execute(self, context):
        setting = SETTING_BY_KEY.get(self.key)
        if setting is None:
            self.report({'ERROR'}, "Unknown shading setting %r" % self.key)
            return {'CANCELLED'}
        sun = sun_object(context)
        if sun is None:
            self.report({'ERROR'}, "No uma shader in this scene yet")
            return {'CANCELLED'}
        if not self.local:
            write_value(sun, self.key, setting["default"])
            _settle(context, sun)
            self.report({'INFO'}, "%s back to the shader's own default (%s)"
                        % (setting["input"], setting["default"]))
            return {'FINISHED'}
        root, _objects = character_objects(context)
        if root is None:
            self.report({'ERROR'}, "Select a uma first")
            return {'CANCELLED'}
        ensure_properties(root, (self.key,), inherit=True)
        value = read_value(sun, self.key, setting)
        write_value(root, self.key, value if value is not None else setting["default"])
        _settle(context, sun, root)
        self.report({'INFO'}, "%s: %s is back to the global value" % (root.name, setting["input"]))
        return {'FINISHED'}


class ShaderControlResetSection(Operator):
    """Put a whole group back: all of it on the global here, or all of it at the defaults globally"""
    bl_idname = "uma.shader_reset_section"
    bl_label = "Reset this group"
    bl_options = {'REGISTER', 'UNDO'}

    section: StringProperty(name="Group")
    local: BoolProperty(name="This uma", default=True)

    def execute(self, context):
        keys = section_keys(self.section)
        if not keys:
            self.report({'ERROR'}, "Unknown shading group %r" % self.section)
            return {'CANCELLED'}
        sun = sun_object(context)
        if sun is None:
            self.report({'ERROR'}, "No uma shader in this scene yet")
            return {'CANCELLED'}
        if not self.local:
            for key in keys:
                write_value(sun, key, SETTING_BY_KEY[key]["default"])
            _settle(context, sun)
            self.report({'INFO'}, "%d setting(s) in %s back to the shader's own defaults"
                        % (len(keys), self.section))
            return {'FINISHED'}
        root, _objects = character_objects(context)
        if root is None:
            self.report({'ERROR'}, "Select a uma first")
            return {'CANCELLED'}
        ensure_properties(root, keys, inherit=True)
        for key in keys:
            write_value(root, key, INHERIT if SETTING_BY_KEY[key]["kind"] == "number" else (INHERIT,) * 4)
        _settle(context, sun, root)
        self.report({'INFO'}, "%s follows the global again for all %d setting(s) in %s"
                    % (root.name, len(keys), self.section))
        return {'FINISHED'}


class ShaderControlSection(Operator):
    """Show or hide one group of shading settings in the panel"""
    bl_idname = "uma.shader_section"
    bl_label = "Show or hide this group of settings"
    bl_options = {'INTERNAL'}

    index: IntProperty(name="Group", default=0)
    all_uma: BoolProperty(name="All umas", default=True)

    def execute(self, context):
        scene_props = getattr(context.scene, "uma_scene", None)
        if scene_props is None:
            return {'CANCELLED'}
        attribute = "shader_global_sections" if self.all_uma else "shader_uma_sections"
        # a bitmask rather than a dozen booleans: the sections come from the settings table, so the number of them
        # is not a constant this file should have to name twice
        bit = 1 << max(0, min(int(self.index), 30))
        setattr(scene_props, attribute, int(getattr(scene_props, attribute, 0)) ^ bit)
        return {'FINISHED'}


# --- the panel -----------------------------------------------------------------------------------------
#
# The panel is a *view* of the properties, never a second copy of them: every row is `layout.prop(host,
# '["uma_shader_x"]')` on the object the drivers read, so there is nothing to sync and the whole mechanism keeps
# working with the extension switched off. That is also why the grouping and the order live here rather than in the
# data - Blender's own Custom Properties list can neither be grouped nor ordered (it sorts, and the only handle on
# a property offers `as_dict/clear/update/update_from`), so a panel is the only place this can come from.
#
# What the panel shows follows the **selection** (the user's request): with an object of a character's hierarchy
# selected - her root empty, her mesh, her armature - it shows *her* values; otherwise it shows the global ones.
# The two views are never on screen together, and the `This uma` / `Global` row switches between them for the case
# of wanting the global while a character is selected.

def override_count(root):
    """How many of this character's settings are her own rather than following the global."""
    if root is None:
        return 0
    count = 0
    for key in KEYS:
        setting = SETTING_BY_KEY[key]
        if not is_inheriting(read_value(root, key, setting), setting):
            count += 1
    return count


def _differs(value, reference, setting):
    """Does a value differ from a reference one, with a tolerance that suits a slider?"""
    if value is None or reference is None:
        return False
    if setting["kind"] == "colour":
        return any(abs(float(a) - float(b)) > 1e-6 for a, b in zip(value, reference))
    return abs(float(value) - float(reference)) > 1e-6


def _draw_global_row(layout, sun, key):
    """One global row: the setting's own name, its value, and a reset once it is no longer the shader's default."""
    setting = SETTING_BY_KEY[key]
    row = layout.row(align=True)
    row.prop(sun, '["%s"]' % key, text=setting["input"], slider=setting["kind"] == "number")
    if _differs(read_value(sun, key, setting), setting["default"], setting):
        reset = row.operator(ShaderControlResetRow.bl_idname, text="", icon='LOOP_BACK')
        reset.key = key
        reset.local = False


def _draw_override_row(layout, root, sun, key):
    """One row for the character: the follow/override padlock, the name, the value or `global`, and a reset.

    The reset appears only when an override differs from the global it replaced - "I moved this one and want it
    back" - which keeps the common case (following) to a name and the word `global`.
    """
    setting = SETTING_BY_KEY[key]
    value = read_value(root, key, setting)
    inheriting = is_inheriting(value, setting)
    row = layout.row(align=True)
    operator = row.operator(ShaderControlOverride.bl_idname, text="",
                            icon='LOCKED' if inheriting else 'UNLOCKED', emboss=False)
    operator.key = key
    row.label(text=setting["input"])
    if inheriting:
        row.label(text="global")
        return
    row.prop(root, '["%s"]' % key, text="", slider=setting["kind"] == "number")
    if _differs(value, read_value(sun, key, setting), setting):
        reset = row.operator(ShaderControlResetRow.bl_idname, text="", icon='LOOP_BACK')
        reset.key = key
        reset.local = True


def _draw_section_header(layout, scene_props, section, index, all_uma):
    """The group's header: the fold toggle, and a reset for the whole group. Returns whether it is folded."""
    mask = int(getattr(scene_props, "shader_global_sections" if all_uma else "shader_uma_sections", 0))
    collapsed = bool(mask & (1 << index))
    header = layout.row(align=True)
    toggle = header.operator(ShaderControlSection.bl_idname, text=section, emboss=False,
                             icon='TRIA_RIGHT' if collapsed else 'TRIA_DOWN')
    toggle.index = index
    toggle.all_uma = all_uma
    reset = header.operator(ShaderControlResetSection.bl_idname, text="", icon='LOOP_BACK')
    reset.section = section
    reset.local = not all_uma
    return collapsed


def _draw_global_block(layout, sun, scene_props):
    """Every global value, grouped the way the shader groups them."""
    for index, section in enumerate(SECTION_ORDER):
        if _draw_section_header(layout, scene_props, section, index, True):
            continue
        for key in section_keys(section):
            _draw_global_row(layout, sun, key)


def _draw_local_block(layout, root, sun, scene_props):
    """One character's own values: the master switch, then the same groups."""
    overrides = override_count(root)
    row = layout.row(align=True)
    switch = row.operator(ShaderControlOverrideAll.bl_idname, text="This uma has her own values",
                          icon='CHECKBOX_HLT' if overrides else 'CHECKBOX_DEHLT', depress=bool(overrides))
    switch.on = not overrides
    if overrides:
        row.label(text="%d of %d set" % (overrides, len(KEYS)))
    else:
        row.label(text="follows the global")
    for index, section in enumerate(SECTION_ORDER):
        if _draw_section_header(layout, scene_props, section, index, False):
            continue
        for key in section_keys(section):
            _draw_override_row(layout, root, sun, key)


def draw_shading_control(layout, context):
    """The panel: which shader the scene uses, the mode line and its two buttons, then the values."""
    use_table(context)                      # the scene's shader chooses the table this panel draws from
    sun = sun_object(context)
    scene_props = getattr(context.scene, "uma_scene", None)
    mode, drivers = status(context)

    # Which shader this scene shades with - one row, drawn from the manifests that are actually installed, so a
    # shader dropped into `operators/shaders/` appears here without a code change. Shading has to run again for it
    # to affect materials, and Hook up re-writes the drivers from its settings table, so those two sentences are in
    # the tooltip rather than in the panel.
    chooser = layout.row(align=True)
    chooser.label(text="Shader")
    if MANIFEST is None:
        chooser.label(text="no readable manifest", icon='ERROR')
    else:
        chooser.menu(ShaderControlMenu.bl_idname,
                     text="%s (%d setting(s))" % (MANIFEST.get("name") or TABLE_ID, len(SETTINGS)))
    for problem in MANIFEST_PROBLEMS:
        layout.label(text=problem, icon='ERROR')

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

    # The drivers in a file were made for one shader. Choosing another and not hooking up again is a real state -
    # the shared settings keep working (they are resolved by name) and the new shader's own ones do nothing - so it
    # is said out loud rather than left to be discovered.
    made_for = sun.get(ShaderManifest.SOURCE_PROPERTY) if sun is not None else None
    if made_for and made_for != TABLE_ID:
        layout.label(text="The drivers here were made for %s - Hook up to move them to %s"
                     % (made_for, TABLE_ID), icon='INFO')

    if sun is None or scene_props is None:
        return
    ensure_properties(sun, KEYS)
    reorder_properties(sun)

    # Which character the panel is showing: `panel_character` reads the *selection*, so deselecting everything
    # goes back to the global values even though Blender keeps an active object (the user's report).
    character, materials = panel_character(context)
    if character is None:
        box = layout.box()
        box.label(text="All umas in this scene", icon='WORLD')
        _draw_global_block(box, sun, scene_props)
        hint = layout.box()
        # a better sentence than "select a uma" when the active object *is* a uma with no shader yet
        active_root, active_objects = character_objects(context)
        if active_root is not None and active_objects and not character_materials(active_objects):
            hint.label(text="%s has no uma shader yet - run Shading on her" % active_root.name, icon='INFO')
        else:
            hint.label(text="Select a uma (her root empty, mesh or armature) for her own values", icon='INFO')
        return

    box = layout.box()
    box.label(text="Shading for %s" % character.name, icon='OBJECT_DATA')
    ensure_properties(character, KEYS, inherit=True)
    reorder_properties(character)
    if len(materials) != 1:
        box.label(text="%d shader material(s) in this character" % len(materials))
    _draw_local_block(box, character, sun, scene_props)
