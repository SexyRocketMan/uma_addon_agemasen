"""The two things that make upstream's "react to world lighting" option usable, and how to take them back out.

The option as 0.2.6 ships it is `Shader to RGB -> GREATER_THAN(0.5) -> MULTIPLY(the sun's own lit/shaded test) ->
Mix.B`: the sample is put through a *hard threshold*, the sun is a *factor of* the world branch, and the two are added
into the picture. Measured (`docs/WORLD_LIGHT_INFLUENCE.md`), that makes the shadow boundary smudged, speckly and
polygon-bound, and it means the Uma Sun empty has to be aligned with the scene's light or the two disagree.

`patch()` replaces exactly two links:

* the sample goes into a `Map Range` band around 0.5 (`From Min/Max = 0.5 -/+ softness`) instead of the
  `GREATER_THAN(0.5)` - Spork's shape, and the transition width becomes a setting;
* `Mix.013.B` takes that band instead of `MULTIPLY(sunTest, thresholded sample)`, so the sun stops being a factor of
  the world branch.

Nothing else is touched, which is why `toggle = 0` (the sun-driven look) comes out byte-identical. `unpatch()` puts
both links back from information **stored on the node group itself** (`bpy.data.node_groups` entries are IDs, so they
take custom properties), not from memory: a file saved patched can be unpatched by a later session, and a patch that
cannot recognise the group it was written for refuses to touch it rather than guessing.

`aim_at_key_light()` answers the other half of the complaint - "what's the point of reacting to world light if you
still have to align Uma Sun". Nothing else in this shader carries a *direction*: the sample is a `Diffuse BSDF` with
`Weight 0`, and that socket is one of Blender's optional ones (`is_unavailable`, so it cannot simply be turned up),
which leaves it carrying how much light there is rather than where it comes from. So the direction is taken from the
scene's own key light and written onto the empty the shader reads.
"""
import bpy

#: The name of the node the patch adds. It is also recorded on the group, so a file can be unpatched later.
BAND_NAME = "UMA World Light Band"
#: The custom property the patch writes on the shader's node group: what to restore, and with which settings.
PATCH_PROPERTY = "uma_world_light_patch"
#: The band the sample is put through, as a half-width around 0.5. 0.15 is Spork's own `Shadow Softness` default.
DEFAULT_SOFTNESS = 0.15


def find_shape(group):
    """The four nodes the patch is written for, found by *behaviour*: `(shape, problems)`.

    A future release may rewire this group, and the only honest answer then is "I do not recognise it, I will not
    touch it" - so the shape is looked for structurally (a `Shader to RGB` whose sample feeds a `GREATER_THAN`, whose
    result feeds a `MULTIPLY`, whose result feeds a `Mix`'s `B`) rather than by hardcoded node names, and the sun's
    comparison is *reported* (it sits a few levels up, behind another `Math`) rather than required.
    """
    shape = {}
    sample = next((node for node in group.nodes if node.bl_idname == "ShaderNodeShaderToRGB"), None)
    shape["sample"] = sample
    if sample is None:
        return shape, ["no Shader to RGB node"]
    threshold = None
    for link in sample.outputs["Color"].links:
        if getattr(link.to_node, "operation", "") == "GREATER_THAN" and link.to_socket.name == "Value":
            threshold = link.to_node
    shape["threshold"] = threshold
    if threshold is None:
        return shape, ["the sample does not feed a GREATER_THAN"]
    multiply = next((link.to_node for link in threshold.outputs["Value"].links
                     if getattr(link.to_node, "operation", "") == "MULTIPLY"), None)
    shape["multiply"] = multiply
    if multiply is None:
        return shape, ["the GREATER_THAN does not feed a MULTIPLY"]
    mix = next((link.to_node for link in multiply.outputs["Value"].links
                if link.to_node.bl_idname == "ShaderNodeMix" and link.to_socket.name == "B"), None)
    shape["mix"] = mix
    if mix is None:
        return shape, ["the MULTIPLY does not feed a Mix's B input"]
    shape["threshold_input"] = threshold.inputs["Value"].links[0].from_socket \
        if threshold.inputs["Value"].is_linked else None
    if shape["threshold_input"] is None:
        return shape, ["the GREATER_THAN takes no input"]
    shape["mix_b_source"] = multiply.outputs["Value"]
    shape["sun_test"] = _comparison_up(multiply, depth=4)
    return shape, []


def _comparison_up(node, depth=4, seen=None):
    """The first comparison above `node` - the sun's `LESS_THAN(NdotL, Light Threshold)` test, for the report."""
    seen = seen if seen is not None else set()
    if node is None or depth < 0 or node.name in seen:
        return None
    seen.add(node.name)
    if getattr(node, "operation", "") in ("LESS_THAN", "GREATER_THAN"):
        return node
    for socket in node.inputs:
        for link in socket.links:
            found = _comparison_up(link.from_node, depth - 1, seen)
            if found is not None:
                return found
    return None


def patched(group):
    """Is this group patched, and with what? Read from the group's own record, so it survives a save."""
    record = group.get(PATCH_PROPERTY)
    if not record:
        return None
    band = group.nodes.get(record.get("band", BAND_NAME))
    if band is None:
        return None
    return {"band": band, "softness": record.get("softness", DEFAULT_SOFTNESS),
            "nodes": record.get("restore", {})}


def patch(group, softness=None):
    """Put the sample through a soft band, or report why not. Idempotent, and safe to call on any group."""
    softness = DEFAULT_SOFTNESS if softness is None else max(0.005, min(0.5, float(softness)))
    existing = patched(group)
    if existing is not None:
        _set_band(existing["band"], softness)
        return {"applied": True, "again": True, "softness": softness, "band": existing["band"].name}

    shape, problems = find_shape(group)
    if problems:
        return {"applied": False, "problems": problems}
    band = group.nodes.new("ShaderNodeMapRange")
    band.name = BAND_NAME
    band.label = "world light: soft band (patched by the UMA addon)"
    band.location = (shape["threshold"].location.x, shape["threshold"].location.y - 200.0)
    band.clamp = True
    _set_band(band, softness)
    group.links.new(shape["threshold_input"], band.inputs["Value"])
    group.links.new(band.outputs["Result"], shape["mix"].inputs["B"])
    # what to restore, recorded *on the group*: this is what makes the patch reversible from a later session, since a
    # file saved patched has no memory of the links that were there before
    group[PATCH_PROPERTY] = {
        "version": 1,
        "band": band.name,
        "softness": softness,
        "restore": {"threshold_node": shape["threshold"].name,
                    "multiply_node": shape["multiply"].name,
                    "mix_node": shape["mix"].name}}
    return {"applied": True, "again": False, "softness": softness, "band": band.name,
            "decoupled_from": shape["multiply"].name,
            "sun_test": shape["sun_test"].name if shape["sun_test"] is not None else None}


def _set_band(band, softness):
    band.inputs["From Min"].default_value = max(0.0, 0.5 - softness)
    band.inputs["From Max"].default_value = min(1.0, 0.5 + softness)
    band.inputs["To Min"].default_value = 0.0
    band.inputs["To Max"].default_value = 1.0


def unpatch(group):
    """Put the graph back exactly as upstream had it, using the record the patch left on the group."""
    record = group.get(PATCH_PROPERTY)
    if not record:
        return {"reverted": False, "reason": "this group was never patched by the add-on"}
    restore = record.get("restore", {})
    threshold_node = group.nodes.get(restore.get("threshold_node", ""))
    multiply_node = group.nodes.get(restore.get("multiply_node", ""))
    mix_node = group.nodes.get(restore.get("mix_node", ""))
    band = group.nodes.get(record.get("band", BAND_NAME))
    if threshold_node is None or multiply_node is None or mix_node is None:
        return {"reverted": False, "reason": "the nodes the patch recorded are not all in this group any more"}
    source = threshold_node.inputs["Value"].links[0].from_socket if \
        threshold_node.inputs["Value"].is_linked else None
    if source is None:
        # the band is what feeds it now; the original source is the sample node
        sample = next((node for node in group.nodes if node.bl_idname == "ShaderNodeShaderToRGB"), None)
        source = sample.outputs["Color"] if sample is not None else None
    if source is not None:
        group.links.new(source, threshold_node.inputs["Value"])
    group.links.new(multiply_node.outputs["Value"], mix_node.inputs["B"])
    if band is not None:
        group.nodes.remove(band)
    del group[PATCH_PROPERTY]
    return {"reverted": True}


def shader_groups(materials):
    """The distinct shader node groups a set of material copies drives - the shared trees a patch belongs to."""
    groups = []
    for material in materials:
        tree = getattr(material, "node_tree", None)
        if tree is None:
            continue
        for node in tree.nodes:
            if node.bl_idname == "ShaderNodeGroup" and node.node_tree is not None:
                if all(node.node_tree is not entry for entry in groups):
                    groups.append(node.node_tree)
    return groups


def key_light(context):
    """The scene's own key light: the brightest lamp that has a direction, or None.

    Sun lamps first, because a sun lamp *is* a direction; then any other lamp, whose direction is where it points.
    """
    lamps = [obj for obj in context.scene.objects
             if obj.type == 'LIGHT' and obj.data is not None and not obj.hide_render]
    if not lamps:
        return None
    suns = [obj for obj in lamps if obj.data.type == 'SUN']
    pool = suns if suns else lamps
    return max(pool, key=lambda obj: float(obj.data.energy))


def aim_at_key_light(context, host):
    """Turn the sun empty onto the scene's key light, so the shader's direction agrees with the scene's.

    The shader reads this empty's rotation (`ROT_X/Y/Z`, through transform drivers inside `Dot Creation`), so copying
    the light's rotation makes the two agree. Returns a report; changes nothing when there is no lamp to aim at.
    """
    if host is None:
        return {"aimed": False, "reason": "no sun empty in this scene"}
    light = key_light(context)
    if light is None:
        return {"aimed": False, "reason": "the scene has no light to aim at"}
    before = list(host.rotation_euler)
    after = list(light.rotation_euler)
    if all(abs(a - b) < 1e-5 for a, b in zip(before, after)):
        return {"aimed": True, "changed": False, "light": light.name, "rotation": after}
    host.rotation_euler = after
    host.update_tag()
    return {"aimed": True, "changed": True, "light": light.name, "was": before, "rotation": after}
