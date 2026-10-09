"""One manifest per shader: every name the shading code used to hardcode, in one JSON file.

The shading half of this add-on was written against exactly one shader library, so its names were in the code:
the file `Umashaders.blend` (`Umashader.py:221`), the collection to append (`:231`), the five material-name
substrings that say what a part is (`:258-263`), the two shader materials (`:280`), the five texture node names and
the filename substitutions (`:300-366`), the two per-part toggles (`:363-371`), the outline group, modifier and
socket names (`:186-194`, `:374-389`), and - on the other half - the 18-row settings table with the shader's own
socket names in the shader's own order (`UmaShaderControl.py:77-146`). A second shader meant editing the code in
nine places, and a shader whose interface has one more socket than ours meant editing the settings table.

So a shader is now **a `.blend` plus a manifest**, and the code asks the manifest for every one of those names:

    operators/shaders/<id>.json      the data
    operators/Umashader.py           `Shading`: append, part, material, textures, outline
    operators/UmaShaderControl.py    the settings table, the sun, the group hints

The JSON is data only - no expressions, no callables, no file references beyond the `.blend` beside it - so a
shader can be added by writing one, and `shader_manifest_check.py` compares a manifest against the behaviour of
the code it came from.

Nothing in here raises. `load()` returns `(manifest, problems)`: a manifest that cannot be read gives
`(None, [reason])`, and one that is readable but incomplete gives the manifest plus what is wrong with it, so the
caller can report a missing socket name instead of dying at import time - the same "report, do not raise" rule the
rest of this add-on follows (`set_modifier_input`, `hook`, the face-normal step). The panel and the operator both
keep working with a shader that is only half described: the parts of the look that are named still apply.
"""
import json
import os

#: Where the manifests live, beside the `.blend` files they describe (`"blend"` is resolved against this folder).
DIRECTORY = os.path.join(os.path.dirname(__file__), "shaders")

#: The shader `Shading` uses when a scene has not chosen one, and the shader a scene that names one which is not
#: installed falls back to.
#:
#: **Since 2.25.0 this is upstream's 0.2.6.** Our own 0.2.4 is no longer shipped: it was a verified subset of 0.2.6
#: everywhere but the outline, and the outline is ours for every shader now that a manifest can name another library
#: (`outline.blend`, 2.24.0). Its manifest is not lost - it is the *second* shader the two-shader probes install for
#: the length of a run, which is what keeps the discriminating test alive: 216 nodes against 225, 18 settings against
#: 20, and two libraries in one scene. The file lives in `UmaViewer-DevTools/fixtures/`.
DEFAULT_ID = "umashader-0.2.6"

#: The ID property that records which shader a datablock belongs to: the library collection the append step created,
#: and the sun empty whose settings were made for one shader. One name, one meaning - "this came from / belongs to
#: <manifest id>" - and it is what makes two shaders in one scene tell each other apart, since both files name their
#: collection and their sun the same.
SOURCE_PROPERTY = "uma_shader_source"

#: Fields a manifest must have as text, a list, and a table of recipes: `parts`/`textures`/`outline` for
#: `Shading`, `settings`/`sun`/`group_hints` for the control. `settings` may be empty (a shader with nothing to
#: tune), so it is checked per row rather than required to be non-empty.
REQUIRED_TEXT = ("id", "name", "blend", "collection", "sun")
REQUIRED_LIST = ("parts",)
REQUIRED_DICT = ("textures",)

_LOADED = {}


def path_for(manifest_id):
    """The file a manifest id names."""
    return os.path.join(DIRECTORY, "%s.json" % manifest_id)


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle), []
    except FileNotFoundError:
        return None, ["no manifest at %s" % path]
    except OSError as error:
        return None, ["could not read %s (%s)" % (path, error)]
    except ValueError as error:
        return None, ["%s is not valid JSON (%s)" % (path, error)]


def validate(manifest):
    """Everything wrong with a readable manifest, as short sentences. Empty means it is complete."""
    problems = []
    for field in REQUIRED_TEXT:
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            problems.append("'%s' is missing or empty" % field)
    for field in REQUIRED_LIST:
        if not isinstance(manifest.get(field), list):
            problems.append("'%s' is missing" % field)
    for field in REQUIRED_DICT:
        if not isinstance(manifest.get(field), dict):
            problems.append("'%s' is missing" % field)
        elif not manifest[field]:
            problems.append("'%s' is empty" % field)

    materials = manifest.get("materials")
    if not isinstance(materials, dict) or not materials.get("default"):
        problems.append("'materials' has no 'default' entry")
        materials = materials if isinstance(materials, dict) else {}

    parts = manifest.get("parts") if isinstance(manifest.get("parts"), list) else []
    for rule in parts:
        if not isinstance(rule, dict):
            problems.append("a 'parts' entry is not an object: %r" % (rule,))
            continue
        where = "part %r" % rule.get("id")
        if not rule.get("id"):
            problems.append("%s has no 'id' - the part is what a texture keyword is searched with" % where)
        if not isinstance(rule.get("match"), list) or not rule["match"]:
            problems.append("%s has no 'match' substrings" % where)
        if rule.get("material") not in materials:
            problems.append("%s names the material %r, which 'materials' does not declare"
                            % (where, rule.get("material")))

    recipes = manifest.get("textures")
    recipes = recipes if isinstance(recipes, dict) else {}
    if recipes and "default" not in recipes:
        problems.append("'textures' has no 'default' recipe, so a part without one of its own gets no textures")
    for recipe, rules in recipes.items():
        if not isinstance(rules, list):
            problems.append("the %r texture recipe is not a list" % recipe)
            continue
        for rule in rules:
            if not isinstance(rule, dict) or not rule.get("node"):
                problems.append("a %r texture rule has no 'node' name" % recipe)
            elif not rule.get("source") and not (isinstance(rule.get("replace"), list)
                                                 and len(rule["replace"]) == 2):
                problems.append("the %r rule for %r has neither 'source' nor a 2-entry 'replace'"
                                % (recipe, rule["node"]))

    for rule in manifest.get("part_inputs") if isinstance(manifest.get("part_inputs"), list) else []:
        if not isinstance(rule, dict) or not rule.get("input") or "value" not in rule:
            problems.append("a 'part_inputs' rule (%r) has no 'input' or no 'value'" % (rule,))
            continue
        if not any(part.get("id") == rule.get("part") for part in parts):
            problems.append("'part_inputs' names the part %r, which 'parts' does not declare" % rule.get("part"))

    outline = manifest.get("outline")
    if outline is not None and (not isinstance(outline, dict) or not isinstance(outline.get("inputs"), dict)):
        problems.append("'outline' has no 'inputs' table (the socket names and their identifiers)")
    elif isinstance(outline, dict) and "blend" in outline and not (isinstance(outline.get("blend"), str)
                                                                 and outline["blend"]):
        # `outline.blend` is optional and means "take the outline group out of this other library" (2.24.0). A
        # non-text value there would be silently ignored by the apply step, so it is a problem rather than a shrug.
        problems.append("'outline.blend' is present but not a file name")

    seen = set()
    for row in manifest.get("settings") if isinstance(manifest.get("settings"), list) else []:
        if not isinstance(row, dict):
            problems.append("a settings row is not an object: %r" % (row,))
            continue
        key = row.get("key")
        if not key or not row.get("input"):
            problems.append("a settings row has no 'key' or no 'input': %r" % row)
            continue
        if key in seen:
            problems.append("the settings key %r appears twice" % key)
        seen.add(key)
        if row.get("kind") not in ("number", "colour"):
            problems.append("the settings row %r has the kind %r, not 'number' or 'colour'" % (key, row.get("kind")))
        if "default" not in row:
            problems.append("the settings row %r has no 'default'" % key)
    return problems


def load(manifest_id=None):
    """`(manifest, problems)` for one id, read once. `None` only when the JSON cannot be read at all."""
    manifest_id = manifest_id or DEFAULT_ID
    if manifest_id in _LOADED:
        return _LOADED[manifest_id]
    manifest, problems = _read(path_for(manifest_id))
    if manifest is not None and not isinstance(manifest, dict):
        manifest, problems = None, ["%s does not hold a JSON object" % path_for(manifest_id)]
    if manifest is not None:
        problems = validate(manifest)
    _LOADED[manifest_id] = (manifest, problems)
    return manifest, problems


def chosen_id(context=None):
    """The id a scene is set to, or the default when it has not chosen. Read even when it is not installed."""
    scene = getattr(context, "scene", None)
    chosen = getattr(getattr(scene, "uma_scene", None), "shader", "") or ""
    return chosen or DEFAULT_ID


def active(context=None):
    """The manifest of the shader this scene shades with, with its problems.

    The choice is a scene property (`scene.uma_scene.shader`, empty meaning this add-on's default), because a scene
    handed to somebody else should carry the shader it was shaded with - and because the settings table below it is
    the shader's own interface, not ours. A scene that asks for a shader which is not installed (the file was
    removed, or the manifest with it) falls back to the default **and says so** rather than shading with nothing.
    """
    chosen = chosen_id(context)
    if chosen == DEFAULT_ID:
        return load(DEFAULT_ID)
    manifest, problems = load(chosen)
    if manifest is not None:
        return manifest, problems
    fallback, fallback_problems = load(DEFAULT_ID)
    return fallback, list(problems) + ["shading with %s instead" % DEFAULT_ID] + list(fallback_problems)


def items():
    """`(id, name, tooltip)` for every readable manifest, the default first - what a chooser lists.

    Built from the folder rather than from a hardcoded list, which is the whole point of a manifest: dropping a
    shader in makes it selectable, and removing one takes it out of the list without leaving a dead entry behind.
    """
    found = available()
    order = ([DEFAULT_ID] if DEFAULT_ID in found else []) + sorted(key for key in found if key != DEFAULT_ID)
    entries = []
    for manifest_id in order:
        manifest = found[manifest_id]
        entries.append((manifest_id, manifest.get("name") or manifest_id,
                        "%s: %d setting(s), shaded from %s"
                        % (manifest.get("name") or manifest_id, len(manifest.get("settings") or []),
                           manifest.get("blend") or "?")))
    return entries



def looks_like_a_manifest(manifest):
    """True when a readable JSON object has the fields a shader manifest must have.

    `available()` lists what the folder holds, so it has to tell a shader from *any other* JSON that ends up in
    there. The rule used to be "it parsed into an object", and that was wrong the first time something else wrote a
    file into the folder: `shader_manifest_blend_check.py` put its own report beside the manifest it was checking
    (`umashader-0.2.6_blend_check.json`), and the chooser promptly offered it as a shader - `Shading` could be asked
    for it, and its id would have been read from the file name. A shader needs the `.blend` it is drawn from, the
    collection to append and the texture recipes to apply, so a JSON object with none of those is not one; it is
    skipped with no complaint, because a report is not a mistake the user made.

    `active()` deliberately stays tolerant - it is handed an id the user *chose*, and the design is that a manifest
    which is readable but incomplete still applies the parts it names. The folder-level filter below is what keeps a
    file that is not a manifest from being choosable in the first place, which is where the bug actually was.
    """
    if not isinstance(manifest, dict):
        return False
    for field in REQUIRED_TEXT:
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            return False
    for field in REQUIRED_LIST:
        if not isinstance(manifest.get(field), list):
            return False
    for field in REQUIRED_DICT:
        if not isinstance(manifest.get(field), dict) or not manifest[field]:
            return False
    materials = manifest.get("materials")
    return isinstance(materials, dict) and bool(materials.get("default"))


def available():
    """Every readable manifest in the folder, by id - what a shader chooser will list (step 5)."""
    found = {}
    try:
        names = sorted(os.listdir(DIRECTORY))
    except OSError:
        return found
    for name in names:
        if not name.endswith(".json"):
            continue
        manifest, _problems = load(name[:-len(".json")])
        if manifest is not None and looks_like_a_manifest(manifest):
            found[manifest.get("id") or name[:-len(".json")]] = manifest
    return found
