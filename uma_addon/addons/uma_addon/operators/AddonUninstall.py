"""Uninstall this add-on from inside its own preferences panel.

**Why this exists.** Blender's *Preferences > Add-ons* list draws an **Uninstall** button only for a *legacy*
add-on. The code is `bl_pkg/bl_extension_ui.py:242-247`: for `ADDON_TYPE_LEGACY_USER` it draws
`preferences.addon_remove`, and for an extension package (`ADDON_TYPE_EXTENSION`) it draws nothing - an
extension's own uninstall lives in the *Get Extensions* page's per-package dropdown menu
(`bl_extension_ui.py:2008-2017`). So a user who installed this add-on as an extension sees no Uninstall where they
would look for one, which is exactly the report this module answers. This add-on is installed all three ways in
practice (an extension repo package, a scratch extension root, a legacy `scripts/addons` folder), so the button
has to do the right thing for whichever one is actually running.

**What each shape gets.** Measured, not remembered - `uninstall_api_check.py` read the RNA of both:

* **extension** (`bl_ext.<repo>.<id>`) -> `bpy.ops.extensions.package_uninstall`, which is what Blender's own
  Get Extensions menu calls. Its RNA carries `repo_directory`, `repo_index` and `pkg_id` in 5.2; `repo_directory`
  is the one that was added later (it exists so a background session, which has no repo list, can be pointed at a
  directory), so only the properties this Blender actually declares are passed.
* **legacy** (`uma_addon` in `scripts/addons`) -> `addon_utils.disable` and then delete the folder: the same two
  steps Blender's own `preferences.addon_remove` takes, minus the line in that operator that dereferences
  `context.area` without checking it (`bl_operators/userpref.py:899`), which cannot run in a background or job
  context at all. See the comment at the removal itself - calling that operator headlessly ends in an
  `EXCEPTION_ACCESS_VIOLATION`, measured.

A copy in a **system** repo is read-only in Blender and there is no operator that can remove one
(`extensions.package_uninstall_system` has an empty RNA beyond `rna_type` and a `poll` that always fails), so that
case is reported as a message rather than attempted.

**Blocking, on purpose.** `extensions.package_uninstall` runs its child process modally only when it is invoked
from the UI; from Python's `EXEC_DEFAULT` - and always in background mode, where `_is_modal()` returns False
(`bl_extension_ops.py:1282-1287`) - it executes blocking. That is what lets `addon_uninstall_check.py` verify the
real uninstall end to end in a throwaway profile. The cost in the UI is one folder being deleted.

**Nothing here raises.** A panel or an operator must never take the add-on down with it, so every failure path is a
report: unknown install shape, missing operator in this Blender, a repository that cannot be found, or a Blender
that refuses the request. The confirmation dialog names the folder it is about to delete, because this is
destructive and there is no undo.

**And the removal never happens inside the operator.** This one is not a style choice, it is a crash that was
measured twice. Blender formats a called operator into a string as soon as `execute` returns
(`WM_operator_call_py -> wm_operator_invoke -> wm_operator_reports -> WM_operator_pystring ->
RNA_pointer_as_string_keywords -> RNA_property_collection_begin`), and an operator whose add-on has just been
unregistered is by then a freed class: `EXCEPTION_ACCESS_VIOLATION`, with the buffered console output lost with it
(`addon_uninstall_check.ps1` lost whole runs to it, and a `--background` run printed nothing at all). Reporting or
not reporting made no difference, so the button **defers**: `execute` registers a one-shot
`bpy.app.timers` callback and returns, and the deletion runs on the next event-loop tick, after this operator has
been formatted and forgotten. A background session has no event loop (measured: timer callbacks never run under
`--background`), which is why the operator says so and does nothing there - a script calls `uninstall()` directly,
which is the same thing the timer calls.
"""
import os
import shutil
import sys

import bpy
import addon_utils

from ..config import __addon_name__

#: Bumped when this module's behaviour changes; printed nowhere, but the same convention as the other modules.
TAG = "uninstall-1"

#: The package folder name, which is also the package id inside an extension repository.
PACKAGE_ID = "uma_addon"


def running_module():
    """The module name this add-on is registered under: `bl_ext.<repo>.uma_addon`, or `uma_addon` for a legacy one."""
    return __addon_name__


def module_folder(module=None):
    """The folder Blender loaded this add-on from, or `""` when it cannot be found.

    `addon_utils` knows about *every* install, including ones that are disabled, which is what makes the two
    "other copies" report possible; `sys.modules` is the fallback for a module that is loaded but not listed.
    """
    module = module or running_module()
    found = sys.modules.get(module)
    path = getattr(found, "__file__", None)
    if not path:
        for other in addon_utils.modules():
            if other.__name__ == module:
                path = getattr(other, "__file__", None)
                break
    if not path:
        return ""
    return os.path.dirname(path) if os.path.basename(path) == "__init__.py" else path


def repositories(context=None):
    """`(index, repo)` for every extension repository this Blender knows about; empty when there is no UI."""
    prefs = getattr(context or bpy.context, "preferences", None)
    extensions = getattr(prefs, "extensions", None)
    repos = getattr(extensions, "repos", None)
    if not repos:
        return []
    try:
        return list(enumerate(repos))
    except TypeError:
        return []


def install_info(context=None, module=None):
    """Where this add-on is installed from, as an extension or as a legacy add-on - and what else is installed.

    The returned dict is deliberately flat and JSON-friendly, because the check writes it out:

        {"module", "shape", "path", "repo_module", "repo_index", "repo_directory", "repo_source", "pkg_id",
         "others": [{"module", "path"}]}
    """
    module = module or running_module()
    info = {
        "module": module,
        "shape": "unknown",
        "path": module_folder(module),
        "repo_module": "",
        "repo_index": -1,
        "repo_directory": "",
        "repo_source": "",
        "pkg_id": "",
        "is_git": False,
        "others": [],
    }
    parts = module.split(".")
    if parts[0] == "bl_ext" and len(parts) >= 3:
        info["shape"] = "extension"
        info["repo_module"] = parts[1]
        info["pkg_id"] = ".".join(parts[2:])
        for index, repo in repositories(context):
            if getattr(repo, "module", "") == info["repo_module"]:
                info["repo_index"] = index
                info["repo_directory"] = getattr(repo, "directory", "") or ""
                info["repo_source"] = getattr(repo, "source", "") or ""
                break
    elif parts[0] == PACKAGE_ID:
        info["shape"] = "legacy"
        info["pkg_id"] = parts[0]

    mine = os.path.normcase(os.path.normpath(info["path"] or ""))
    # A checkout is worth knowing about before the folder is deleted, and it is exactly the case this fork's own
    # development install is: the extension directory *is* the git repository, so uninstalling it takes the
    # commits with it - including any that were never pushed anywhere.
    info["is_git"] = bool(mine) and os.path.isdir(os.path.join(info["path"], ".git"))
    for other in addon_utils.modules():
        name = getattr(other, "__name__", "")
        if name == module or PACKAGE_ID not in name.split("."):
            continue
        path = getattr(other, "__file__", None)
        if not path:
            continue
        folder = os.path.dirname(path) if os.path.basename(path) == "__init__.py" else path
        if mine and os.path.normcase(os.path.normpath(folder)) == mine:
            continue
        info["others"].append({"module": name, "path": folder})
    return info


def describe(info):
    """One line for the user: how this copy is installed."""
    if info["shape"] == "extension":
        where = info["repo_module"] or "unknown repository"
        if info["repo_source"] == "SYSTEM":
            return "extension in the read-only '%s' repository" % where
        return "extension in the '%s' repository" % where
    if info["shape"] == "legacy":
        return "legacy add-on (a folder in scripts/addons)"
    return "unrecognised install"


def uninstall(context=None, report=None, module=None):
    """Disable and delete this add-on. Returns a dict describing what was done; calls `report` with messages.

    `report` is the operator's own `report` method when there is one, so the messages land in Blender's status bar
    and the info log; without it the messages are only in the returned dict, which is what the check reads.
    """
    info = install_info(context, module)
    result = {"info": info, "operator": "", "status": set(), "removed": False, "error": ""}

    def say(kind, message):
        result.setdefault("messages", []).append((kind, message))
        # `print`, not `report`: see the module docstring - a report issued by the operator that is deleting itself
        # is formatted after `execute` returns, when its class is already unregistered, and that is a crash.
        print("[uma_addon] %s: %s" % (kind, message))
        if report is not None:
            report({kind}, message)

    if info["shape"] == "unknown":
        result["error"] = ("this add-on's install could not be identified (module %r, folder %r)"
                           % (info["module"], info["path"] or "not found"))
        say('ERROR', "Cannot uninstall: %s. Remove it from Blender's Preferences instead." % result["error"])
        return result

    if info["shape"] == "extension" and info["repo_source"] == "SYSTEM":
        result["error"] = "the '%s' repository is read-only" % (info["repo_module"] or "system")
        say('ERROR', "Cannot uninstall: %s. Blender does not allow removing a system extension."
            % result["error"])
        return result

    if info["shape"] == "extension":
        operator = getattr(getattr(bpy.ops, "extensions", None), "package_uninstall", None)
        if operator is None:
            result["error"] = "this Blender has no bpy.ops.extensions.package_uninstall"
            say('ERROR', "Cannot uninstall: %s." % result["error"])
            return result
        called = {"repo_index": info["repo_index"], "pkg_id": info["pkg_id"]}
        # `repo_directory` is newer than `repo_index`/`pkg_id`; pass it only where the RNA declares it
        try:
            declared = {prop.identifier for prop in operator.get_rna_type().properties}
        except Exception:                                                  # noqa: BLE001 - report, never raise
            declared = {"repo_index", "pkg_id"}
        if "repo_directory" in declared and info["repo_directory"]:
            called["repo_directory"] = info["repo_directory"]
        try:
            result["status"] = set(bpy.ops.extensions.package_uninstall(**called))
        except Exception as exc:                                           # noqa: BLE001
            result["error"] = "%s: %s" % (type(exc).__name__, exc)
            say('ERROR', "Uninstalling failed: %s" % result["error"])
            return result
        result["operator"] = "extensions.package_uninstall(%s)" % ", ".join(
            "%s=%r" % item for item in sorted(called.items()))
    else:
        # A legacy add-on is removed here rather than through `bpy.ops.preferences.addon_remove`, and that is a
        # deliberate difference. That operator does exactly two things after disabling the add-on - `rmtree` the
        # folder, then `context.area.tag_redraw()` on the *unguarded* `context.area` (`bl_operators/userpref.py:899`
        # in 5.2) - and in a context with no area the second line raises, which ends in
        # `EXCEPTION_ACCESS_VIOLATION` and takes Blender down with it (measured: `addon_uninstall_check.ps1` lost
        # the whole legacy run to it, after the folder had already been deleted). Disabling and deleting is the
        # same work with a redraw that is guarded, so the button is safe from a background session, a job or a
        # script as well as from the preferences panel it normally lives in.
        try:
            addon_utils.disable(info["module"], default_set=True)
        except Exception as exc:                                           # noqa: BLE001
            result["error"] = "could not disable %s: %s: %s" % (info["module"], type(exc).__name__, exc)
            say('ERROR', "Uninstalling failed: %s" % result["error"])
            return result
        folder = info["path"]
        try:
            if folder and os.path.islink(folder):
                os.remove(folder)
            elif folder and os.path.isdir(folder):
                shutil.rmtree(folder, ignore_errors=True)
            result["status"] = {"FINISHED"}
            result["operator"] = "addon_utils.disable(%r) + rmtree" % info["module"]
        except Exception as exc:                                           # noqa: BLE001
            result["error"] = "%s: %s" % (type(exc).__name__, exc)
            say('ERROR', "Disabled, but deleting %s failed: %s" % (folder, result["error"]))
            return result
        area = getattr(context if context is not None else bpy.context, "area", None)
        if area is not None:
            try:
                area.tag_redraw()
            except Exception:                                              # noqa: BLE001 - a stale area is not fatal
                pass

    # Three outcomes, three messages - and the difference matters. `info["path"]` is empty when the add-on has
    # already been removed and unregistered (the module is still in `sys.modules` but Blender has cleared its
    # `__file__`), which is a different situation from "the folder is still there", and the first version of this
    # said "still on disk" for both.
    statuses = ", ".join(sorted(result["status"])) or "nothing"
    if not info["path"]:
        say('WARNING', "Blender reported %s. The add-on's folder could not be located afterwards - check that it "
                       "is gone." % statuses)
    elif os.path.exists(info["path"]):
        say('WARNING', "Blender reported %s, but %s is still on disk." % (statuses, info["path"]))
    else:
        result["removed"] = True
        say('INFO', "Uninstalled from %s. Restart Blender to finish." % info["path"])
    return result


class UninstallAddon(bpy.types.Operator):
    """Delete this add-on from the disk"""

    bl_idname = "uma.uninstall_addon"
    bl_label = "Uninstall"
    bl_description = ("Disable this add-on and delete its folder from the disk, the same way Blender's own "
                      "Uninstall does. There is no undo; Blender has to be restarted afterwards")
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, _context):
        # Never greys the button out: an install this module cannot identify gets an explanation when it is
        # pressed, which is more useful than a button that refuses to say why it is dead.
        return True

    def invoke(self, context, event):
        # destructive and irreversible, so the folder is named before anything happens
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        layout = self.layout
        info = install_info(context)
        layout.label(text="This deletes the add-on from the disk:", icon='ERROR')
        layout.label(text=info["path"] or "folder not found", translate=False)
        layout.label(text="Installed as %s" % describe(info), translate=False)
        if info["is_git"]:
            layout.label(text="This folder is a git checkout: its commits are deleted with it",
                         icon='ERROR')
        if info["others"]:
            layout.label(text="%d other copy/copies of this add-on are also installed"
                              % len(info["others"]), icon='INFO')
            for other in info["others"]:
                layout.label(text=other["path"], translate=False)
        layout.label(text="Blender has to be restarted afterwards. Preferences and scene data are untouched.")

    def execute(self, context):
        """Queue the removal for the next event-loop tick - never perform it inside this operator.

        Measured, twice, in `addon_uninstall_check.ps1`: an operator that unregisters its own add-on and *then*
        returns is formatted by Blender as the call unwinds (`WM_operator_pystring -> RNA_pointer_as_string_keywords`),
        and by then its class is gone, so that read is a use-after-free ending in `EXCEPTION_ACCESS_VIOLATION`.
        Reporting or not reporting made no difference - the first version reported, the second printed only, and
        both crashed on the extension shape. Deleting the files is therefore deferred to a timer, which runs after
        this operator has been formatted and forgotten.

        In a background session there is no event loop to defer into (measured: `bpy.app.timers` callbacks never run
        under `--background`), and doing the work inline is exactly the crash above - so the operator explains
        itself and does nothing. A script should call `uninstall()` directly, which is what the timer calls too.
        """
        info = install_info(context)
        target = info["path"] or "the add-on"
        if bpy.app.background:
            print("[uma_addon] uninstall: deferred to the event loop, which a background session does not have. "
                  "Call AddonUninstall.uninstall() from a script, or use the button in the interface.")
            self.report({'WARNING'}, "Uninstall needs the interface; from a script call uninstall() instead")
            return {'CANCELLED'}
        schedule_uninstall()
        # safe to report here: the add-on - and with it this class - is still registered, because the removal has
        # not happened yet
        self.report({'INFO'}, "Uninstalling %s - Blender has to be restarted afterwards" % target)
        return {'FINISHED'}


def schedule_uninstall():
    """Register the one-shot timer that performs the removal, and return the callback it registered.

    Returned rather than registered anonymously so the check can ask Blender whether that exact function is
    registered (`bpy.app.timers.is_registered`) and then run it by hand - a background session never fires timers,
    so without this the interface's half of the button could not be measured at all.

    The callback takes no arguments on purpose. The `context` an operator is handed is only valid for the duration
    of the call, so capturing it into a timer would be handing the timer a freed struct; the callback reads
    `bpy.context` instead, which is what a running timer does have.
    """
    callback = _deferred_uninstall
    bpy.app.timers.register(callback, first_interval=0.0)
    return callback


def _deferred_uninstall():
    """The timer callback: the removal itself, now that no operator of ours is being formatted any more."""
    context = bpy.context
    try:
        result = uninstall(context, report=None)
    except Exception as exc:                                                # noqa: BLE001 - a timer must not raise
        print("[uma_addon] ERROR: uninstalling failed: %s: %s" % (type(exc).__name__, exc))
        return None
    try:
        workspace = getattr(context, "workspace", None) or getattr(getattr(context, "window", None), "workspace",
                                                                  None)
        if workspace is not None:
            workspace.status_text_set("UMA Addon removed - restart Blender" if not result["error"]
                                      else "UMA Addon could not be uninstalled - see the console")
    except Exception:                                                       # noqa: BLE001 - a hint is not essential
        pass
    return None
