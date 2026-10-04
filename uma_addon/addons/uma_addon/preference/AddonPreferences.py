import bpy
import os
from bpy.props import StringProperty, IntProperty, BoolProperty, CollectionProperty
from bpy.types import AddonPreferences
from ..config import __addon_name__
from ..operators.Properties import EarTargets, AllActions
from ..utils.Config_handling import get_config_parameter, set_config_parameter, get_panel_name
from ..panels.AddonPanels import (ModelProcessPanel, ControllerPanel, ChangeHeadPanel, PhysicsandActionsPanel,
                                  ShadingLookPanel)

#: Every panel whose sidebar category the `panel_name` preference rewrites. The list has to be complete: a panel
#: missing from it keeps the old category until Blender restarts, which is how "Shading - All Umas" was left
#: behind in a renamed sidebar (found by the feature inventory, fixed here - it is a top-level panel, so unlike a
#: child panel its category is its own).
UI_PANELS = [ModelProcessPanel, ControllerPanel, ChangeHeadPanel, PhysicsandActionsPanel, ShadingLookPanel]

class AddonPreferences(AddonPreferences):
    # this must match the add-on name (the folder name of the unzipped file)
    bl_idname = __addon_name__

    # https://docs.blender.org/api/current/bpy.props.html
    # The name can't be dynamically translated during blender programming running as they are defined
    # when the class is registered, i.e. we need to restart blender for the property name to be correctly translated.

    def update_panel_name(self, context):
        try:
            for c in UI_PANELS:
                bpy.utils.unregister_class(c)
                c.bl_category = self.panel_name
        except:
            pass

        set_config_parameter("Addon Settings", "panel_name", self.panel_name)

        for c in UI_PANELS:
            bpy.utils.register_class(c)

    panel_name: StringProperty(name="", default=get_panel_name(), update=update_panel_name)
    debug: BoolProperty(default=False)
    ear_targets: CollectionProperty(type=EarTargets)
    all_actions: CollectionProperty(type=AllActions)

    def draw(self, context: bpy.types.Context):
        """绘制偏好设置界面"""
        layout = self.layout

        layout.prop(self, "panel_name")

        row = layout.row()
        row.operator(RefreshData.bl_idname, icon="FILE_REFRESH")
        row.operator(ClearData.bl_idname, icon="TRASH")

class RefreshData(bpy.types.Operator):
    """Refresh addon data"""
    bl_idname = "uma.refresh_data"
    bl_label = "Refresh Data"
    bl_options = {'REGISTER'}

    def execute(self, context):

        prefs = context.preferences.addons[__addon_name__].preferences
        prefs.debug = False   

        # 刷新时保存收藏和昵称
        # 记录当前已经收藏的名字
        # favorites = {item.name for item in prop if item.is_favorite}
        # prop.clear()
        # for n in action_names:
        #     item = prop.add()
        #     item.name = n
        #     if n in favorites:
        #         item.is_favorite = True

        self.report({'INFO'}, "Refresh addon data successfully")
        return {'FINISHED'}

class ClearData(bpy.types.Operator):
    """Clear addon data"""
    bl_idname = "uma.clear_data"
    bl_label = "Clear Data"
    bl_options = {'REGISTER'}

    def execute(self, context):

        prefs = context.preferences.addons[__addon_name__].preferences
        prefs.panel_name = "UMA"
        prefs.debug = False   
        if prefs.ear_targets:
            prefs.ear_targets.clear()
        if prefs.all_actions:
            prefs.all_actions.clear()     
        try:
            bpy.ops.wm.save_userpref()
        except Exception as e:
            self.report({'WARNING'}, str(e))
        scene_props = context.scene.uma_scene
        scene_props.filtered_actions.clear()
        scene_props.action_index = 0

        self.report({'INFO'}, "Clear addon data successfully")
        return {'FINISHED'}
