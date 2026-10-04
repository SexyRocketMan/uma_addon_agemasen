import bpy
from ..config import __addon_name__
from ..operators.AddonOperators import *
from ..operators.Controller import GenerateIK, BakeFKtoIK, ToggleTwistConstraints
# these live in their own files so they can be removed on their own; without them the panel still draws
try:
    from ..operators.CollapseRig import CollapseRig
except ImportError:
    CollapseRig = None
try:
    from ..operators.RigifyChains import RigifyChains, RigifyChainRules
except ImportError:
    RigifyChains = RigifyChainRules = None
try:
    from ..operators.UmaShaderControl import draw_shading_control
except ImportError:
    draw_shading_control = None
from ..operators.Umashader import *
from ..operators.EarConvert import *
from ..operators.Motion import *
from ..operators.AnmiCopy import *

from ....common.i18n.i18n import i18n
from ....common.types.framework import reg_order
from ..utils import compat
from ..utils.Config_handling import get_panel_name
from ..image.ImageManager import get_image_id

class BasePanel(object):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = get_panel_name()

    @classmethod
    def poll(cls, context: bpy.types.Context):
        return True

@reg_order(0)
class ModelProcessPanel(BasePanel, bpy.types.Panel):
    bl_label = "Processing Model"
    bl_idname = "SCENE_PT_umaaddonpanel1"

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        scene_props = context.scene.uma_scene
        prefs = context.preferences.addons[__addon_name__].preferences
        obj_props = getattr(context.active_object, "uma_object", None)

        row = layout.row()
        row.operator("mmd_tools.import_model", text="Import")
        row.operator(SetBoneCollections.bl_idname)
        row.operator(RefineBoneStructure.bl_idname)
        row = layout.row(align=True)
        row.prop(scene_props, "del_handle", toggle=True, translate=False)
        row.prop(scene_props, "del_face", toggle=True, translate=False)
        row.prop(scene_props, "del_others", toggle=True, translate=False)
        row.operator(SimplifyArmature.bl_idname)

        layout.operator(ApplyShader.bl_idname, icon="SHADING_RENDERED")
        # the outline containment is a scene setting, so it is visible before Shading is applied and can be
        # switched off there; the operator reads it at run time (docs/OUTLINES.md)
        layout.prop(scene_props, "fix_outlines")
        layout.prop(scene_props, "shader_control")
        layout.operator(FixMini.bl_idname, icon="SHADERFX")

        layout.label(text="Use shape keys to drive ear bones")
        split = layout.split(factor=0.7, align=True)
        split.prop_search(scene_props, "ear_target", prefs, "ear_targets")
        split.operator(EarConvert.bl_idname, icon="DRIVER")

class UMA_UL_Action(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        row = layout.row(align=True)
        fav_icon = 'FUND' if item.is_favorite else 'NONE'
        row.prop(item, "display_name", text="", icon=fav_icon, emboss=False)

class UMA_UL_Mappings(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        # 无效时警告
        layout.alert = not item.is_valid()
        row  = layout.row(align=True)

        def mapping():
            source_arm = context.scene.uma_scene.action_source
            if source_arm:
                row.prop_search(item, 'target', source_arm.data, 'bones', text='', translate=False, icon='BONE_DATA')
                row.label(icon='FORWARD')
                row.label(text=item.owner, translate=False, icon='BONE_DATA' if context.scene.uma_scene.action_target.data.bones.get(item.owner) else 'CANCEL')
            else:
                row.label(text=' ', icon='CANCEL')
                row.label(icon='FORWARD')
                row.label(text=item.owner, translate=False, icon='BONE_DATA' if context.scene.uma_scene.action_target.data.bones.get(item.owner) else 'CANCEL')
        def rotation():
            row.prop(item, 'has_rotoffs', icon='CON_ROTLIKE', icon_only=True)
            layout.label(text=item.owner, translate=False)
            if item.has_rotoffs:
                layout.prop(item, 'offset', text='')
        def location():
            row.prop(item, 'has_loccopy', icon='CON_LOCLIKE', icon_only=True)
            layout.label(text=item.owner, translate=False)
            if item.has_loccopy:
                layout.row().prop(item, 'loc_axis', text='', toggle=True)

        draw = {0: mapping, 1: rotation, 2: location}
        draw[context.scene.uma_scene.editing_type]()

from ..operators.Properties import fill_all_actions, get_action


def ensure_action_list(scene_props, context):
    """Build the action list the first time the panel is drawn, once per scene.

    The list is otherwise only written when a dropdown *changes* (`update_action_category`,
    `update_level1/2/3`) or when a favourite is toggled, so a scene nobody had touched showed an empty list under
    a perfectly good default - the second half of the reported *"I've never managed to get the ear/tail actions
    presets working"*.

    It also fills the library itself when it is empty: `prefs.all_actions` is read by a **startup timer**, and a
    session where that has not run (a fresh profile, `--factory-startup`, a background run, or a user who pressed
    *Clear Data*) otherwise has nothing to list under any filter. Both fills happen once - the flag is what keeps
    them out of every redraw.

    A module-level function so a probe can call exactly what `draw` calls; the panel is not drawable headlessly.
    """
    if scene_props.action_list_filled:
        return False
    prefs = context.preferences.addons[__addon_name__].preferences
    if len(prefs.all_actions) < 1:
        added = fill_all_actions(prefs)
        print("[uma_addon] action library was empty; read %d name(s) from Umashaders.blend" % added)
    get_action(scene_props, context)
    scene_props.action_list_filled = True
    return True


def action_list_hint(scene_props):
    """Why the action list is empty, in one sentence, or None when it is not empty.

    A module-level function because the panel is not drawable headlessly, and this is the part of the panel worth
    asserting on: an empty list that says nothing is what made the ear/tail actions look broken. The two cases are
    different and deserve different words - `Favorite` with nothing favourited is a normal first state, while "no
    actions match" means the level choice is wrong.
    """
    if scene_props.filtered_actions:
        return None
    if scene_props.action_category == "ear" and scene_props.level1 == "Favorite":
        return "No favourites yet - pick a level above, then the heart marks one"
    return "No actions match %s / %s" % (scene_props.action_category, scene_props.level1)


@reg_order(1)
class PhysicsandActionsPanel(BasePanel, bpy.types.Panel):
    bl_label = "Physics & Action"
    bl_idname = "SCENE_PT_umaaddonpanel3"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        scene_props = context.scene.uma_scene
        obj_props = getattr(context.active_object, "uma_object", None)
        
        layout.label(text="Physics", icon="PHYSICS")
        row = layout.row(align=True)
        # if obj_props and :
        row.prop(context.scene.damped_track, "ear_enable", toggle=True, text="Ear")
        row.prop(context.scene.damped_track, "bust_enable", toggle=True, text="Bust")
        row.prop(context.scene.damped_track, "tail_enable", toggle=True, text="Tail", text_ctxt="UMA")

        row = layout.row()
        row.alignment = 'LEFT' 
        if scene_props.is_uma_acton:
            row.operator(UmaActionMode.bl_idname, icon="ANIM", emboss=False)
            cat = scene_props.action_category
            if cat == "tail":
                row = layout.row(align=True)
                row.prop(scene_props, "action_category", text_ctxt="UMA")
            elif cat == "ear":
                row = layout.row(align=True)
                row.prop(scene_props, "action_category")
                row.prop(scene_props, "level1")
                if scene_props.level2 != "None":
                    row.prop(scene_props, "level2")
                    if scene_props.level3 != "None":
                        row.prop(scene_props, "level3")

            # `action_index` is a scene property, so it outlives the list it pointed into: switch from a level with
            # 1156 rows to one with a single row and the old index is past the end. Reading it unguarded raised
            # `IndexError: bpy_prop_collection[index]: index 40 out of range, size 1` out of `draw`, and Blender
            # stops a panel where it stopped - which is the reported "the list of actions is gone now, just the
            # dropdown selectors are rendered". Clamped here, and the list is only read when it has rows.
            def selected_action():
                if not len(scene_props.filtered_actions):
                    return None
                index = min(max(scene_props.action_index, 0), len(scene_props.filtered_actions) - 1)
                if index != scene_props.action_index:
                    scene_props.action_index = index
                return scene_props.filtered_actions[index]

            current = selected_action()
            fav_icon = 'FUND' if (current is not None and current.is_favorite) else 'HEART'
            # the list is built by the filter updates, so a scene nobody has touched has none: fill it here, once.
            # A panel must never raise (Blender truncates it at the exception), so a failing fill is reported and
            # the panel carries on with whatever the list holds.
            if not scene_props.action_list_filled:
                try:
                    ensure_action_list(scene_props, context)
                except Exception as error:  # noqa: BLE001
                    print("[uma_addon] could not build the action list (%s: %s); the panel keeps what it has"
                          % (type(error).__name__, error))
                current = selected_action()
                fav_icon = 'FUND' if (current is not None and current.is_favorite) else 'HEART'
            row = layout.row()
            col = row.column(align=True)
            box = col.box()
            box_row = box.row()
            if cat == 'ear':
                box_row.operator(ApplyKeyAction.bl_idname)
            elif cat == 'tail':
                box_row.operator(ApplyAction.bl_idname)
            # Nothing to apply means nothing to apply: the buttons go dead rather than failing on a click, and the
            # reason is written down. `action_list_hint` is the whole of that sentence, as a function so a probe can
            # read it without a UI (the reported dead end was an empty list that said nothing).
            box_row.enabled = bool(scene_props.filtered_actions)
            col.template_list(
                "UMA_UL_Action", "", 
                scene_props, "filtered_actions", 
                scene_props, "action_index", 
                type=compat.TEMPLATE_LIST_GRID, 
                columns=4, rows=14
            )
            hint = action_list_hint(scene_props)
            if hint:
                col.label(text=hint, icon='INFO')
            heart = box_row.operator(ToggleFavorite.bl_idname, text="", icon=fav_icon)
            heart.enabled = bool(scene_props.filtered_actions)
        else:
            row.operator(AnyActionMode.bl_idname, icon="ANIM", emboss=False)
            row = layout.row(align=True)
            row.prop(scene_props, 'action_source', text='', icon='ARMATURE_DATA', translate=False)
            row.label(icon='FORWARD')
            row.prop(scene_props, 'action_target', text='', icon='ARMATURE_DATA', translate=False)

            row = layout.row()
            left = row.column(align=True)
            box = left.box()
            box_row = box.row()

            # 编辑模式切换
            box_row.operator(SelectEditType.bl_idname, text='Map', icon='PRESET', emboss=True, depress=scene_props.editing_type==0).selected_type = 0
            box_row.operator(SelectEditType.bl_idname, text='Rot', icon='CON_ROTLIKE', emboss=True, depress=scene_props.editing_type==1).selected_type = 1
            box_row.operator(SelectEditType.bl_idname, text='Pos', icon='CON_LOCLIKE', emboss=True, depress=scene_props.editing_type==2).selected_type = 2
            box_row.separator()
            if scene_props.editing_type==2:
                box_row.operator(BonePositionToZero.bl_idname)
            box_row.operator(Umapping.bl_idname, icon_value=get_image_id("umapping"), text='')

            # 映射列表
            if scene_props.action_target:
                arm = scene_props.action_target.data.uma_armature
                left.template_list(
                    'UMA_UL_Mappings', '', 
                    arm, 'mappings', 
                    arm, 'active_mapping',
                    rows=14
                )
            else:
                left.template_list(
                    'UMA_UL_Mappings', '', 
                    scene_props, 'dummy_coll', 
                    scene_props, 'dummy_idx',
                    rows=14
                )

            # 预设菜单
            # box = left.box().row(align=True)
            # box.menu(BAC_MT_presets.__name__, text=BAC_MT_presets.bl_label, translate=False)
            # box.operator(AddPresetBACMapping.bl_idname, icon='ADD')
            # box.separator()
            # box.operator('kumopult_bac.open_preset_folder', text="", icon='FILE_FOLDER')

            row = layout.row()
            pre_icon = 'HIDE_OFF' if scene_props.preview else 'HIDE_ON'
            row.operator(TogglePreview.bl_idname, icon=pre_icon, depress=scene_props.preview)
            row.operator(BakeAnm.bl_idname, icon='NLA')

@reg_order(2)
class ControllerPanel(BasePanel, bpy.types.Panel):
    bl_label = "Controller"
    bl_idname = "SCENE_PT_umaaddonpanel4"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        obj_props = getattr(context.active_object, "uma_object", False)
        if obj_props:
            layout.operator(ToggleTwistConstraints.bl_idname, text="Auto Twist", depress=obj_props.auto_twist_bones)
        else:
            layout.operator(ToggleTwistConstraints.bl_idname, text="Auto Twist")
        layout.operator(GenerateIK.bl_idname, icon='BONE_DATA')
        layout.operator(BakeFKtoIK.bl_idname, icon='SNAP_ON')
        if RigifyChains is not None:
            # two ways to build the controller, and both buttons are called "Generate controller for
            # umamusume" (MMR's own in its panel, the fork's appended next to it). This one is the fork's
            # plus the hair / cloth / tail chains.
            layout.label(text="Controller with hair/cloth/tail chains (not the MMR button above):")
            layout.operator(RigifyChains.bl_idname, icon='MOD_ARMATURE')
            if RigifyChainRules is not None:
                layout.operator(RigifyChainRules.bl_idname, icon='PRESET')
        if CollapseRig is not None:
            layout.operator(CollapseRig.bl_idname, icon='ARMATURE_DATA')

@reg_order(3)
class ShadingLookPanel(BasePanel, bpy.types.Panel):
    """One place to set the look of every uma in the scene, with per-character overrides.

    Open by default, unlike the panels above: the values live on objects as custom properties, and Blender's own
    Custom Properties list can neither be grouped nor ordered to match the shader (measured - the only handle,
    `IDPropertyUIManager`, carries `as_dict/clear/update/update_from` and nothing else, and the list is sorted,
    which is why the user's properties never appeared in the shader's order there). This panel is where both the
    order and the grouping come from, so it should be the first thing a user sees rather than something to find.
    """
    bl_label = "Shading - All Umas"
    bl_idname = "SCENE_PT_umaaddonpanel5"

    def draw(self, context: bpy.types.Context):
        if draw_shading_control is None:
            self.layout.label(text="UmaShaderControl.py is missing", icon='ERROR')
            return
        draw_shading_control(self.layout, context)
    