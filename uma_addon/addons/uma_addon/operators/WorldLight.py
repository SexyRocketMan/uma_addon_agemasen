"""Aiming the Uma Sun empty at the scene's own key light - and where our shader patch went.

Why this file exists
--------------------
Looper's shader takes its shading *direction* from one object: the `Uma Sun [DO NOT DELETE]` empty the shader library
brings in. It reaches the shader through transform drivers inside the `Dot Creation` node group (`ROT_X/Y/Z` of that
empty), so the empty has to be turned by hand to match wherever the scene's own light points - which is the user's
complaint in `docs/WORLD_LIGHT_INFLUENCE.md`: *"what's the point of reacting to world light if you still have to align
Uma Sun?"*. `aim_at_key_light()` is the answer that does not touch the shader at all: find the scene's brightest lamp
and copy its rotation onto the empty.

What used to be here, and why it is not any more
------------------------------------------------
2.23.0 also **patched** the shader: upstream's world-light option puts the sampled scene light through a
`GREATER_THAN(0.5)`, multiplies that by the sun's own lit/shaded test and adds the result into the picture, and the
patch replaced those two links with a soft band. Measured (`shader_worldlight_patch_check.py`, 13 checks), the band did
what it promised - the boundary became a band with a softness setting, the sun stopped being a factor of the world
branch, and the sun-driven look at `toggle = 0` stayed byte-identical - but two things did not change: the sampled
light still carries *how much* light there is rather than *where* it comes from (the sampling `Diffuse BSDF` ships with
`Weight 0`, an optional socket that cannot be turned up), and the shading it produced was still not good enough to
prefer over upstream's. So 2.23.1 **reverted it**: testers get Looper's shader exactly as he shipped it, so their
feedback is about *his* shader rather than our fork of it. The patch code and its measurements are kept in
`docs/WORLD_LIGHT_INFLUENCE.md` (§5-§10) and in `shader_worldlight_patch_check.py`, which still applies the same two
link changes in memory for anyone who wants to look again.

What is left here is a transform write on an empty. It changes no datablock inside the shader, so a file aimed with it
still holds "the exact shader that's supplied".
"""
import bpy


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

    The shader reads this empty's rotation, so copying the light's rotation makes the two agree. Returns a report;
    changes nothing when there is no lamp to aim at, and says so rather than raising.
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
