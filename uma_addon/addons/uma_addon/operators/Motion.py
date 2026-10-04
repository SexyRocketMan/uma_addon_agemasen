import bpy
import os
import re
from ..config import __addon_name__
from ..operators.Properties import get_action
from ..utils import compat

class UmaActionMode(bpy.types.Operator):
    bl_idname = "uma.uma_action_mode"
    bl_label = "UMA Action"
    def execute(self, context):
        context.scene.uma_scene.is_uma_acton = False
        return {'FINISHED'}

class AnyActionMode(bpy.types.Operator):
    bl_idname = "uma.any_action_mode"
    bl_label = "Action"
    def execute(self, context):
        context.scene.uma_scene.is_uma_acton = True
        return {'FINISHED'}

class ToggleFavorite(bpy.types.Operator):
    bl_idname = "uma.toggle_favorite"
    bl_label = ""
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene_props = context.scene.uma_scene
        idx = scene_props.action_index
  
        if 0 <= idx < len(scene_props.filtered_actions):
            item = scene_props.filtered_actions[idx]
            item.is_favorite = not item.is_favorite
            bpy.ops.wm.save_userpref()

            # Always, not only for a category called `fav`: that test is a leftover from a three-category design
            # (the categories are ear and tail now), so marking a favourite never refreshed the list - including
            # the `Favorite` level, whose whole content *is* the favourites.
            get_action(scene_props, context)
        return {'FINISHED'}

class ApplyAction(bpy.types.Operator):
    bl_idname = "uma.apply_action"
    bl_label = "Apply Action"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'ARMATURE'

    def execute(self, context):
        scene_props = context.scene.uma_scene
        idx = scene_props.action_index
        actions = scene_props.filtered_actions
        action_name = actions[idx].name

        # 检查是否选择了动作
        if not action_name or action_name == "NONE":
            self.report({'WARNING'}, "No action selected")
            return {'CANCELLED'}

        # 追加源动作
        blend_file = os.path.join(os.path.dirname(__file__), "Umashaders.blend")
        try:
            with bpy.data.libraries.load(blend_file, link=False) as (_, data_to):
                data_to.actions = [action_name]
        except Exception as e:
            self.report({'ERROR'}, f"Failed to load action: {e}")
            return {'CANCELLED'}
        
        if not data_to.actions:
            self.report({'ERROR'}, f"{action_name} not found in library")
            return {'CANCELLED'}
        src_action = data_to.actions[0]

        # 获取目标动作
        obj = context.active_object
        if not obj.animation_data:
            obj.animation_data_create()
        
        target_action = obj.animation_data.action
        # 如果没有动作，创建动作
        if not target_action:
            target_action = bpy.data.actions.new(name=f"{obj.name}Action")
            obj.animation_data.action = target_action

        # 如果没有动作槽，创建并指定动作槽
        # 4.4 起曲线挂在动作槽下，5.0 起只能用这条路线；compat 挑对 4.2-5.2 的写法
        target_slot = compat.ensure_action_slot(obj.animation_data, target_action, obj.name, id_type='OBJECT')

        # 源动作是从 Umashaders.blend 加载的只读数据
        # 4.2-4.5 它的曲线在 action.fcurves 上；5.0+ 只能从它自己的动作槽里取
        src_fcurves = compat.action_fcurves(src_action)
        if not src_fcurves:
            src_slots = getattr(src_action, "slots", None)
            if src_slots:
                src_fcurves = compat.action_fcurves(src_action, slot=src_slots[0])

        for src_fc in src_fcurves:
            # 检查骨骼是否存在
            match = re.match(r'pose\.bones\["([^"]+)"\]', src_fc.data_path)
            if not match:
                continue
            bone_name = match.group(1)
            if not obj.data.bones.get(bone_name):
                print(f"WARNING: '{bone_name}' not found")
                continue

            # 获取目标动作的曲线（4.2-4.5 走 action.fcurves，4.4+/5.0+ 走该数据块的槽）
            target_fc = compat.ensure_fcurve(
                target_action,
                src_fc.data_path,
                index=src_fc.array_index,
                owner=obj,
                anim_data=obj.animation_data,
            )
            if target_fc is None:
                continue

            # 在目标动作中查找或创建同名组
            if src_fc.group:
                target_group = compat.ensure_group(
                    target_action, src_fc.group.name, anim_data=obj.animation_data
                )
                if target_group and target_fc.group is None:
                    target_fc.group = target_group

            # 插入关键帧
            for src_key in src_fc.keyframe_points:
                # 计算偏移后的新时间
                new_time = src_key.co.x + context.scene.frame_current
                new_value = src_key.co.y

                # 在目标曲线插入关键帧
                new_key = target_fc.keyframe_points.insert(
                    frame=new_time,
                    value=new_value,
                    options={'FAST'}
                )
                
                # 复制关键帧的插值类型和手柄类型
                new_key.interpolation = src_key.interpolation
                new_key.handle_left_type = src_key.handle_left_type
                new_key.handle_right_type = src_key.handle_right_type
                new_key.easing = src_key.easing

            target_fc.update()

        bpy.data.actions.remove(src_action)
        # only when there is a UI: a background or scripted session has no area, and raising
        # "AttributeError: 'NoneType' object has no attribute 'tag_redraw'" there is a liability - every probe in
        # this project calls operators headlessly, which is how this was found while verifying this feature
        if context.area is not None:
            context.area.tag_redraw()
        self.report({'INFO'}, f"Applied successfully")
        return {'FINISHED'}

class ApplyKeyAction(bpy.types.Operator):
    bl_idname = "uma.apply_key_action"
    bl_label = "Apply Action"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH' and context.active_object.data.shape_keys

    def execute(self, context):
        scene_props = context.scene.uma_scene
        idx = scene_props.action_index
        actions = scene_props.filtered_actions             
        action_name = actions[idx].name

        # 检查是否选择了动作
        if not action_name or action_name == "NONE":
            self.report({'WARNING'}, "No action selected")
            return {'CANCELLED'}

        # 从库文件中追加源动作
        blend_file = os.path.join(os.path.dirname(__file__), "Umashaders.blend")
        try:
            with bpy.data.libraries.load(blend_file, link=False) as (_, data_to):
                data_to.actions = [action_name]
        except Exception as e:
            self.report({'ERROR'}, f"Failed to load action: {e}")
            return {'CANCELLED'}
        
        if not data_to.actions:
            self.report({'ERROR'}, f"{action_name} not found in library")
            return {'CANCELLED'}
        src_action = data_to.actions[0]

        # 获取目标对象和形态键数据块
        obj = context.active_object
        kb = obj.data.shape_keys

        # 确保形态键数据块有动画数据
        if not kb.animation_data:
            kb.animation_data_create()
        
        target_action = kb.animation_data.action

        # 如果没有动作，创建形态键动作
        if not target_action:
            target_action = bpy.data.actions.new(name=f"{obj.name}Action")
            # 4.2-4.5 记录在 action.id_root 上；5.0+ 该属性已移除，类型记录在动作槽上
            compat.set_action_id_root(target_action, 'KEY')
            kb.animation_data.action = target_action

        # 如果没有动作槽，创建并指定动作槽
        # 形态键动作的槽必须用 id_type='KEY'，否则 5.0+ 找不到曲线所属的数据块
        target_slot = compat.ensure_action_slot(kb.animation_data, target_action, obj.name, id_type='KEY')

        # 源动作是从 Umashaders.blend 加载的只读数据
        # 4.2-4.5 它的曲线在 action.fcurves 上；5.0+ 只能从它自己的动作槽里取
        src_fcurves = compat.action_fcurves(src_action)
        if not src_fcurves:
            src_slots = getattr(src_action, "slots", None)
            if src_slots:
                src_fcurves = compat.action_fcurves(src_action, slot=src_slots[0])

        for src_fc in src_fcurves:

            # 获取目标动作的曲线（4.2-4.5 走 action.fcurves，4.4+/5.0+ 走 Key 数据块的槽）
            target_fc = compat.ensure_fcurve(
                target_action,
                src_fc.data_path,
                index=src_fc.array_index,
                owner=kb,
                anim_data=kb.animation_data,
            )
            if target_fc is None:
                continue

            # 复制并插入关键帧
            for src_key in src_fc.keyframe_points:
                # 计算偏移后的新时间
                new_time = src_key.co.x + context.scene.frame_current
                new_value = src_key.co.y

                # 在目标曲线插入关键帧
                new_key = target_fc.keyframe_points.insert(
                    frame=new_time,
                    value=new_value,
                    options={'FAST'}
                )
                
                # 复制关键帧属性
                new_key.interpolation = src_key.interpolation
                new_key.easing = src_key.easing
                new_key.handle_left_type = src_key.handle_left_type
                new_key.handle_right_type = src_key.handle_right_type

            target_fc.update()

        bpy.data.actions.remove(src_action)
        # only when there is a UI: a background or scripted session has no area, and raising
        # "AttributeError: 'NoneType' object has no attribute 'tag_redraw'" there is a liability - every probe in
        # this project calls operators headlessly, which is how this was found while verifying this feature
        if context.area is not None:
            context.area.tag_redraw()
        self.report({'INFO'}, f"Applied successfully")
        return {'FINISHED'}
