import bpy
import os
from bpy.props import IntProperty, BoolProperty, StringProperty, BoolVectorProperty, FloatVectorProperty, FloatProperty, EnumProperty, CollectionProperty, PointerProperty
from ..config import __addon_name__
from math import pi
from ..utils.Utils import UMA_BONES
from ..operators.AnmiCopy import *
from bpy.app.translations import pgettext_iface as _

_is_updating_list = False
_CACHED_ACTION_CATEGORY_ITEMS =[]
_CACHED_LEVEL1_ITEMS =[]
_CACHED_LEVEL2_ITEMS =[]
_CACHED_LEVEL3_ITEMS =[]

EAR_MOTION = {
    "Favorite": {
    },
    "Campaign": {
        # "00": [], "01": [], "02": [], "03": [], "04": [], "05": [], "06": [], "07": [], "08": [], "09": [], "10": [], "11": []
    },
    "Cut-in": {
        "Card": [], "Chara": []#, "Common": []
    },
    "Event": {
        # "body": [], "facial":[]
    },
    "Gacha": {
        # "00": [], "01": [], "02": [], "03": [], "04": [], "05": [], "06": [], "07": []
    },
    "Home ": {
        # "01": [], "02": [], "03": [], "05": []
    },
    "Live": {
    },
    "Mini": {
        "Circle": [], "Event": [], "Job":[], "Mode": [], "Set ": []
    },
    "Outgame": {
        "Factor":[], "Prologue": [], "Rankup": [], "Rival":[]
    },
    "Race Gate": {
        # "01": [], "02": [], "03": [], "04": [], "05": [], "06": [], "07": [], "99": []
    },
    "Race Gate In Demo": {
    },
    "Race Main": {
    },
    "Race Result": {
    },
    "Result SMT": {
    },
    "Rush In": {
    },
    "Set ": {
        "群英联赛": [], 
        "目标！最强团队": [], 
        "凯旋门": [], 
        "UAF": [], 
        "UAF决赛": [], 
        "命运魔盒": [], 
        "机械杯": [], 
        "传奇赛": [], 
        "紫蝶掠影": [], 
        "名牝传承": [], 
        "大师挑战赛": [], 
        "无人岛": [], 
        "温泉乡": [], 
        "育马者杯": []
    },
    "Training": {
        # "01": [], "02": [], "03": [], "04": [], "05": [], "06": [], "07": []
    }
}

EAR_MOTION_MAP = {
    "群英联赛": "_001", 
    "目标！最强团队": "_002", 
    "凯旋门": "_003", 
    "UAF": "_004", 
    "UAF决赛": "_005", 
    "命运魔盒": "_006", 
    "机械杯": "_007", 
    "传奇赛": "_008", 
    "紫蝶掠影": "_009_02", 
    "名牝传承": "_009_01", 
    "大师挑战赛": "_010", 
    "无人岛": "_011", 
    "温泉乡": "_012", 
    "育马者杯": "_013",
    "Campaign": "anm_cap",
    "Event": "anm_eve",
    "Gacha": "anm_gac",
    "Home ": "anm_hom",
    "Live": "anm_liv",
    "Race Gate": "anm_gat",
    "Race Gate In Demo": "anm_gid",
    "Race Main": "anm_rac",
    "Race Result": "anm_res",
    "Result SMT": "anm_smt",
    "Rush In": "anm_smr",
    "Set ": "anm_set",
    "Training": "anm_tra",
    "Card": "crd",
    "Chara": "chr",
    "Common": "cmn",
    "Factor": "fac",
    "Prologue": "prg",
    "Rankup": "rup",
    "Rival": "riv",
}

class EarTargets(bpy.types.PropertyGroup):
    pass

class AllActions(bpy.types.PropertyGroup):
    is_favorite: BoolProperty(default=False, update=lambda self, context: bpy.ops.wm.save_userpref())
    display_name: StringProperty(default="", update=lambda self, context: bpy.ops.wm.save_userpref())

def sync_fav(self, context):
# 如果是在刷新列表，不执行同步
    global _is_updating_list
    if _is_updating_list:
        return 
    
    prefs = context.preferences.addons[__addon_name__].preferences
    source_item = next((x for x in prefs.all_actions if x.name == self.name), None)
    if source_item:
        if source_item.is_favorite != self.is_favorite:
            source_item.is_favorite = self.is_favorite

def sync_display_name(self, context):
    global _is_updating_list
    if _is_updating_list:
        return
        
    prefs = context.preferences.addons[__addon_name__].preferences
    source_item = next((x for x in prefs.all_actions if x.name == self.name), None)
    if source_item:
        # 空字符串恢复成原名
        new_name = self.display_name.strip()
        if not new_name:
            new_name = self.name 
        if source_item.display_name != new_name:
            source_item.display_name = new_name
        if self.display_name != new_name:
            _is_updating_list = True 
            self.display_name = new_name
            _is_updating_list = False

def fill_all_actions(prefs, force=False):
    """Read the action names out of `Umashaders.blend` into `prefs.all_actions`.

    Lives here rather than only in the addon's `__init__.py` because two callers need it: the startup timer, and
    the panel, which has to cope with a session where that timer never ran. Measured: with `--factory-startup`
    (a fresh profile, a background session, or a user who pressed *Clear Data*) `prefs.all_actions` is empty, and
    the panel then had nothing to list under any filter - the same dead end as an empty list with no explanation,
    one layer deeper.

    Returns how many names it added.
    """
    if not force and len(prefs.all_actions) > 0:
        return 0
    blend_file = os.path.join(os.path.dirname(__file__), "Umashaders.blend")
    if not os.path.exists(blend_file):
        print("[uma_addon] %s not found, so the action library is empty" % blend_file)
        return 0
    try:
        with bpy.data.libraries.load(blend_file) as (data_from, _data_to):
            action_names = list(data_from.actions)
    except Exception as error:  # noqa: BLE001
        print("[uma_addon] could not list the actions in %s: %s" % (blend_file, error))
        return 0
    for name in action_names:
        prefs.all_actions.add().name = name
    return len(action_names)


class FilteredActions(bpy.types.PropertyGroup):
    is_favorite: BoolProperty(default=False, update=sync_fav)
    display_name: StringProperty(name="", default="", update=sync_display_name)

def get_action_category(self, context):
    global _CACHED_ACTION_CATEGORY_ITEMS
    _CACHED_ACTION_CATEGORY_ITEMS = [("ear", "Ear", ""), ("tail", _("Tail", "UMA"), "")]
    return _CACHED_ACTION_CATEGORY_ITEMS

def get_level1(self, context):
    items = []
    for key in EAR_MOTION.keys():
        items.append((key, key, ""))
    return items

def get_level2(self, context):
    global _CACHED_LEVEL2_ITEMS
    _CACHED_LEVEL2_ITEMS.clear()
    lvl1 = self.level1
    if lvl1 in EAR_MOTION:
        for key in EAR_MOTION[lvl1].keys():
            _CACHED_LEVEL2_ITEMS.append((key, _(key, "UMA"), ""))
    if _CACHED_LEVEL2_ITEMS == []:
        return [("None", "", "")]
    return _CACHED_LEVEL2_ITEMS
        
def get_level3(self, context):
    items = []
    lvl1 = self.level1
    lvl2 = self.level2
    if lvl1 in EAR_MOTION and lvl2 in EAR_MOTION[lvl1]:
        for key in EAR_MOTION[lvl1][lvl2]:
            items.append((key, key, ""))
    if items == []:
        return [("None", "", "")]
    return items

def is_ear_motion_match(self, context, name, lvl1, lvl2):

    if name.startswith("anm_cti") or lvl1 == "Cut-in":
        if name.startswith("anm_cti") and lvl1 == "Cut-in":
            if EAR_MOTION_MAP[lvl2] in name:
                return True
            else:
                return False
        else:
            return False

    if name.startswith("anm_min") or lvl1 == "Mini":
        if name.startswith("anm_min") and lvl1 == "Mini":
            match lvl2:
                case "Circle":
                    if name.startswith("anm_min_cir"):
                        return True
                case "Event":
                    if name.startswith("anm_min_eve"):
                        return True
                case "Job":
                    if name.startswith("anm_min_job"):
                        return True
                case "Mode":
                    if name.startswith("anm_min_mde"):
                        return True
                case "Set ":
                    if name.startswith("anm_min_set"):
                        return True
                case _:
                    return False
            return False
        else:
            return False
        
    if name.startswith("anm_set") or lvl1 == "Set ":
        if name.startswith("anm_set") and lvl1 == "Set ":
            if EAR_MOTION_MAP[lvl2] in name:
                return True
            else:
                return False
        else:
            return False

    if lvl1 == "Outgame":
        if EAR_MOTION_MAP[lvl2] in name:
            return True
    else:
        if EAR_MOTION_MAP[lvl1] in name:
            return True
    return False

def get_action(self, context):
    global _is_updating_list
    _is_updating_list = True 
    try:
        cat = self.action_category
        lvl1 = self.level1
        lvl2 = self.level2

        actions = context.preferences.addons[__addon_name__].preferences.all_actions
        self.filtered_actions.clear()

        for action in actions:
            name = action.name
            is_match = False
            if cat == "ear":
                if lvl1 == "Favorite":
                    if "ear" in name and action.is_favorite:
                        is_match = True
                else:
                    is_match = is_ear_motion_match(self, context, name, lvl1, lvl2)
            elif cat == "tail":
                if "tail" in name:
                    is_match = True
            
            if is_match:
                filtered_action = self.filtered_actions.add()
                filtered_action.name = name
                if action.display_name:
                    filtered_action.display_name = action.display_name
                else:
                    filtered_action.display_name = name
                filtered_action.is_favorite = action.is_favorite 
        self.action_index = 0
    finally:
        _is_updating_list = False

def update_action_category(self, context):
    get_action(self, context)

def update_level1(self, context):
    lvl2_list = list(EAR_MOTION[self.level1].keys())
    if len(lvl2_list) > 0:
        self.level2 = lvl2_list[0]
    else:
        self.level2 = "None"

def update_level2(self, context):
    self.level3 = "None"

def update_level3(self, context):
    get_action(self, context)

def clear_anmi_copy_constraints(arm_obj):
    bones = arm_obj.pose.bones
    for n in UMA_BONES:
        bone = bones.get(n)
        if bone:
            for con in reversed(bone.constraints):
                if con.name in {'BAC_ROT_COPY', 'BAC_ROT_ROLL', 'BAC_LOC_COPY'}:
                    bone.constraints.remove(con)

def update_action_source(self, context):
    uma = context.scene.uma_scene
    if uma.action_target:
        clear_anmi_copy_constraints(uma.action_target)

def update_action_target(self, context):
    uma = context.scene.uma_scene
    if uma.prev_action_target and uma.prev_action_target != uma.action_target:
        clear_anmi_copy_constraints(uma.prev_action_target)
    if uma.action_target:
        clear_anmi_copy_constraints(uma.action_target)
    uma.prev_action_target = uma.action_target

class UmaShaderSections(bpy.types.PropertyGroup):
    """Which shading groups one shader's blocks are folded away on.

    A bitmask per *shader* rather than one for the scene, because a scene can wear more than one shader and each
    draws its own blocks: with a single mask, folding "Diffuse" in one shader's block folds whatever section happens
    to sit at that index in the other one. Two masks, one for the global block and one for a selected character's
    overrides, exactly as before - just per shader now.
    """
    shader: StringProperty(name="Shader", default="")
    global_mask: IntProperty(default=0, description="Shading groups folded away in this shader's global list")
    uma_mask: IntProperty(default=0, description="Shading groups folded away in this shader's character list")


class UmaScene(bpy.types.PropertyGroup):
    dummy_coll: CollectionProperty(type=FilteredActions)
    dummy_idx: IntProperty(default=0)

    #: One entry per shader whose blocks have been folded somewhere (see UmaShaderSections).
    shader_sections: CollectionProperty(type=UmaShaderSections)

    del_handle: BoolProperty(name="handle", default=True)
    del_face: BoolProperty(name="face", default=True)
    del_others: BoolProperty(name="others", default=True)

    fix_outlines: BoolProperty(
        name="Contain the outline",
        default=True,
        description="Point the Uma Outlines modifier at the scene camera and give the shell a usable width. "
                    "Off applies upstream's shading exactly as it ships")

    shader_control: BoolProperty(
        name="Unified shader control",
        default=True,
        description="Drive every material copy from one setting per value on the sun empty, with per-character "
                    "overrides. Off leaves the values per material, to be matched by hand")

    # Which shader library this scene shades with: the id of a manifest in `operators/shaders/`, empty meaning the
    # add-on's default. A scene property rather than a preference, because the shader travels with the scene - and a
    # **StringProperty** rather than an EnumProperty, deliberately: an enum whose `items` is a callback cannot have a
    # string default ("'default' can only be an integer when 'items' is a function" - the same rule the action
    # category in this file documents), so it would store an *index* into a list that changes when a manifest is
    # added or removed. The id is what the file and the drivers mean, so the id is what is stored; the panel draws a
    # menu from `ShaderManifest.items()` instead.
    shader: StringProperty(
        name="Shader",
        default="",
        description="Which shader library this scene shades with; empty is the add-on's default. Applies to the next "
                    "Shading run - materials already copied keep their shader until they are shaded again")

    # --- aiming the Uma Sun at the scene's own light ---------------------------------------------------------
    #
    # Nothing in Looper's shader carries a *direction* except the Uma Sun empty: the sample it takes is a Diffuse BSDF
    # with weight 0, an optional socket that cannot simply be turned up, so it carries how much light there is rather
    # than where it comes from (measured: docs/WORLD_LIGHT_INFLUENCE.md). Aiming the empty at the scene's key light is
    # what makes the shader's direction agree with the scene's, so nobody has to align it by hand. It touches no
    # shader datablock, which is why it survived the revert of our patch to the shader itself.
    sun_follow_key_light: BoolProperty(
        name="Aim the Uma Sun at the key light",
        default=False,
        description="Turn the Uma Sun empty onto the scene's brightest lamp when Shading runs, so the shading "
                    "direction follows the scene. Off by default, because it moves the empty the file came with")

    face_normals: BoolProperty(
        name="Face normal transfer",
        default=True,
        description="Keep a hidden static copy of the face and transfer its custom normals onto the live face, so a "
                    "facial morph stops dragging the shading with it. Separates the face into its own object first, "
                    "and drops the morphs that cannot move each piece")

    # Which shading groups the panel has folded away, as a bitmask over the groups in the settings table, one for
    # the global settings and one for the selected character's overrides. Here rather than on the objects
    # themselves, because a UI state has no business appearing in their Custom Properties list.
    shader_global_sections: IntProperty(default=0, description="Shading groups folded away in the global list")
    shader_uma_sections: IntProperty(default=0, description="Shading groups folded away in the character's list")

    ear_target: StringProperty(name="")
    is_uma_acton: BoolProperty(default=True)
    # `default=1` is not a preference, it is the fix for a reported dead end, and it has to be an **index**: with
    # `items` given as a callback Blender refuses an identifier ("'default' can only be an integer when 'items' is
    # a function" - the same rule the preset dropdown documents). Index 1 is `tail` in `get_action_category`'s
    # two-item list. With no default at all the enum opened on its first item (`ear`) *and* `level1` opened on its
    # first item (`Favorite`), whose filter needs `action.is_favorite` - so the panel greeted every user with an
    # empty action list and no explanation, which is the "I've never managed to get the ear/tail actions presets
    # working" report. `tail` is a plain substring test over the 49 `*tail*` actions the library holds, so the
    # panel lists something the moment it is opened.
    action_category: EnumProperty(name="", items=get_action_category, update=update_action_category,
                                  default=1)
    level1: EnumProperty(name="", items=get_level1, update=update_level1)
    level2: EnumProperty(name="", items=get_level2, update=update_level2)
    level3: EnumProperty(name="", items=get_level3, update=update_level3)
    filtered_actions: CollectionProperty(type=FilteredActions)
    action_index: IntProperty(name="", default=0, options={'HIDDEN'})
    #: The action list is only built when a filter *changes*, so a scene that was never touched had an empty list
    #: under a good default too - the panel fills it once on first draw and marks this. See `ensure_action_list`.
    action_list_filled: BoolProperty(default=False)

    action_source: PointerProperty(name="Source", type=bpy.types.Object, poll=lambda self, obj: obj.type == 'ARMATURE' and obj != self.action_target, update=update_action_source)
    action_target: PointerProperty(name="Target", type=bpy.types.Object, poll=lambda self, obj: obj.type == 'ARMATURE' and obj != self.action_source, update=update_action_target)
    prev_action_target: PointerProperty(type=bpy.types.Object)
    editing_type: IntProperty(default=0)
    preview: BoolProperty(default=True)

class UmaObject(bpy.types.PropertyGroup):
    auto_twist_bones: BoolProperty(default=False)
    ik_generated: BoolProperty(default=False)

class BoneMapping(bpy.types.PropertyGroup):
    def update_target(self, context):
        if self.get_owner():
            action_source = context.scene.uma_scene.action_source
            action_target = context.scene.uma_scene.action_target

            if self.get_target():
                euler_offset = ((action_source.matrix_world @ self.get_target().matrix).inverted() @ (action_target.matrix_world @ self.get_owner().matrix)).to_euler()
                for i in range(3):
                    if euler_offset[i] < 0.1 and euler_offset[i] > -0.1:
                        euler_offset[i] = 0
                    self.offset[i] = euler_offset[i]
                
            cons = self.get_owner().constraints
            for con in reversed(cons):
                if con.name == 'BAC_ROT_COPY':
                    cons.remove(con)

            cr = cons.new(type='COPY_ROTATION')
            cr.name = 'BAC_ROT_COPY'
            cr.target = action_source
            cr.subtarget = self.target
            cr.target_space = 'WORLD'
            cr.owner_space = 'WORLD'
            cr.show_expanded = False

            self.has_rotoffs = True
            self.has_loccopy = False

    def update_rotoffs(self, context):
        cons = self.get_owner().constraints
        for con in reversed(cons):
            if con.name == 'BAC_ROT_ROLL':
                cons.remove(con)

        if self.has_rotoffs:
            rr = cons.new(type='TRANSFORM')
            rr.name = 'BAC_ROT_ROLL'
            rr.map_to = 'ROTATION'
            rr.owner_space = 'CUSTOM'
            rr.to_min_x_rot = self.offset[0]
            rr.to_min_y_rot = self.offset[1]
            rr.to_min_z_rot = self.offset[2]
            rr.target = rr.space_object = context.scene.uma_scene.action_source
            rr.subtarget = rr.space_subtarget = self.target
            rr.show_expanded = False
            self.reorder()

    def update_loccopy(self, context):
        cons = self.get_owner().constraints
        for con in reversed(cons):
            if con.name == 'BAC_LOC_COPY':
                cons.remove(con)

        if self.has_loccopy:
            cp = cons.new(type='COPY_LOCATION')
            cp.name = 'BAC_LOC_COPY'
            cp.use_x = self.loc_axis[0]
            cp.use_y = self.loc_axis[1]
            cp.use_z = self.loc_axis[2]
            cp.target = context.scene.uma_scene.action_source
            cp.subtarget = self.target
            cp.show_expanded = False
            self.reorder()

    def reorder(self):
        if self.get_owner():
            cons = self.get_owner().constraints
            target_order = ['BAC_ROT_COPY', 'BAC_ROT_ROLL', 'BAC_LOC_COPY']
            for name in target_order:
                con = cons.get(name)
                if con:
                    cons.move(cons.find(name), len(cons) - 1)

    def get_owner(self):
        if bpy.context.scene.uma_scene.action_target:
            return bpy.context.scene.uma_scene.action_target.pose.bones.get(self.owner)
        else:
            return None

    def get_target(self):
        if bpy.context.scene.uma_scene.action_source:
            return bpy.context.scene.uma_scene.action_source.pose.bones.get(self.target)
        else:
            return None
        
    def is_valid(self):
        return self.get_owner() and self.get_target()

    target: StringProperty(name="Source", override={'LIBRARY_OVERRIDABLE'}, update=update_target)
    owner: StringProperty(name="Target")
    has_rotoffs: BoolProperty(default=False, override={'LIBRARY_OVERRIDABLE'}, update=update_rotoffs)
    has_loccopy: BoolProperty(default=False, override={'LIBRARY_OVERRIDABLE'}, update=update_loccopy)
    offset: FloatVectorProperty(default=(0.0, 0.0, 0.0), min=-pi, max=pi, override={'LIBRARY_OVERRIDABLE'}, subtype='EULER', update=update_rotoffs)
    loc_axis: BoolVectorProperty(default=[True, True, True], override={'LIBRARY_OVERRIDABLE'}, subtype='XYZ', update=update_loccopy)

class UmaArmature(bpy.types.PropertyGroup):
    def update_active_mapping(self, context):
        if context.scene.uma_scene.action_source and context.scene.uma_scene.action_target:
            bones = context.scene.uma_scene.action_source.data.bones
            bones.active = bones.get(self.mappings[self.active_mapping].target)
            bones = context.scene.uma_scene.action_target.data.bones
            bones.active = bones.get(self.mappings[self.active_mapping].owner)

    mappings: CollectionProperty(type=BoneMapping, override={'LIBRARY_OVERRIDABLE', 'USE_INSERTION'})
    active_mapping: IntProperty(name="", default=0, override={'LIBRARY_OVERRIDABLE'}, options={'HIDDEN'} ,update=update_active_mapping)
    