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

#: The shader `Shading` uses when nothing else is chosen. The per-scene choice is step 5 of
#: `docs/SHADER_INTERFACE.md`; until it exists this is the one shader the add-on has.
DEFAULT_ID = "umashader-0.2.4"

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


def active():
    """The manifest `Shading` and the control use, with its problems if it has any."""
    return load(DEFAULT_ID)


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
        if manifest is not None:
            found[manifest.get("id") or name[:-len(".json")]] = manifest
    return found
