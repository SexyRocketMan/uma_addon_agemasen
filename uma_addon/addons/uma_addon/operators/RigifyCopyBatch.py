"""Batch rigify's per-bone bone copying so a rig run switches object modes a handful of times, not hundreds.

**The problem, measured.** Blender's bundled rigify copies a bone like this - `rigify/utils/bones.py`, its own
comment included:

    def _legacy_copy_bone(obj, bone_name, assign_name=''):
        \"\"\"LEGACY ONLY, DON'T USE\"\"\"
        new_name = copy_bone(obj, bone_name, assign_name, parent=True, bbone=True)
        # Mode switch PER BONE CREATION?!
        bpy.ops.object.mode_set(mode='OBJECT')
        copy_bone_properties(obj, bone_name, new_name)
        bpy.ops.object.mode_set(mode='EDIT')
        return new_name

and `rigify/utils/__init__.py` then binds the plain name every rig type imports to it:

    # Definitions so bad as to make them strictly compatibility only
    from .bones import copy_bone as copy_bone_simple
    from .bones import _legacy_copy_bone as copy_bone

A `Pumpkin Rig` run makes **204** of those calls - 408 mode switches - because MMR's metarig template types its face
bone `faces.super_face` and rigify's SuperFace sample builds itself out of copies. A mode switch is a dependency-graph
flush, so it costs whatever the file costs: measured on the reference model, **7.9 ms** in a file with no rig in it
and **50-58 ms** in one that already contains a finished rig (`docs/PERFORMANCE.md` §3.2). That is 3 s of a first
character's 21 s and 24 s of a second character's 64 s.

**The fix.** Not "call the modern one" - `copy_bone_simple` covers everything the legacy wrapper does *except*
`copy_bone_properties`, which needs object mode, because a bone created inside an edit session has no pose bone until
that session is applied. That round trip is the whole reason for the switches. So this keeps the edit session open,
queues `(object, source, copy)` per call, and copies the properties **once per session** - which cannot change what
anybody sees, because nothing can read a pose property of a bone that does not exist yet. Measured, the whole run goes
from 408 switches to **6**, and the resulting rig is identical by fingerprint (every bone, pose property and
constraint, hashed):

    first character   21.00 s -> 14.21 s   (1.48x)
    second character  63.89 s -> 36.34 s   (1.76x)

**How the session boundary is detected, and why it has to be.** Rigify's `Generator.generate()` opens one edit session
for all the bone creation (`generate.py:563-580`), but the samples open their own around groups of copies -
`super_face.py` does, and after each one it *reads and writes pose data* (it assigns custom shapes), so a property
copy that arrived late would overwrite them. The trigger therefore has to be "the object left edit mode", and there
are only two candidates: `sys.setprofile` **cannot** see operator calls at all (measured: 7 `c_call` events across two
mode switches, none identified as an operator), while `bpy.ops` **is** writable on the `bpy` module - so this installs
a proxy over `object.mode_set` for the duration of the run and flushes after every call. That proxy is why the patch
is guarded: **if it cannot be installed, nothing is patched at all**, because batching without a session-boundary
signal would be fast and wrong.

Callers bind their own reference (`from rigify.utils import copy_bone` at import time - `super_face`, `super_chain` and
`rigify.utils` here), so every module holding the legacy function is rewritten and the module table is re-scanned as we
go, because rigify imports rig types lazily as it meets them. Anything not yet re-scanned simply runs rigify's own
code: slower, never different.

Tags: `copy-batch-1`.
"""
import sys

import bpy

#: What the console lines carry, so a report can say which revision of this file ran.
TAG = "copy-batch-1"


def _is_operator_host(module_name, operator_name):
    """Is there a `bpy.ops.<module>.<operator>` to wrap? Asked without calling it."""
    try:
        return hasattr(getattr(bpy.ops, module_name), operator_name)
    except Exception:  # noqa: BLE001
        return False


class _WrappedOperator(object):
    """One `bpy.ops.x.y`: forwards everything, and calls back after the call.

    Attributes as well as calls are forwarded, so `poll()`, `idname` and friends still work on the wrapped operator.
    """

    def __init__(self, real, after):
        self._real = real
        self._after = after

    def __call__(self, *args, **kwargs):
        result = self._real(*args, **kwargs)
        self._after()
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


class _OperatorProxy(object):
    """A `bpy.ops.<module>` with one operator wrapped."""

    def __init__(self, real, operator_name, after):
        self._real = real
        self._operator_name = operator_name
        self._after = after

    def __getattr__(self, name):
        target = getattr(self._real, name)
        if name == self._operator_name:
            return _WrappedOperator(target, self._after)
        return target

    def __dir__(self):
        return dir(self._real)


class _OpsProxy(object):
    """`bpy.ops`, with one module's one operator wrapped; everything else is handed straight through."""

    def __init__(self, real, module_name, operator_name, after):
        self._real = real
        self._module_name = module_name
        self._operator_name = operator_name
        self._after = after

    def __getattr__(self, name):
        target = getattr(self._real, name)
        if name == self._module_name:
            return _OperatorProxy(target, self._operator_name, self._after)
        return target

    def __dir__(self):
        return dir(self._real)


class Batcher(object):
    """Installs the batched copy for the duration of a run, and puts everything back afterwards.

    Usage is a `with` block. `install()` returns False with a reason instead of raising when rigify is not the rigify
    this was written against, and every caller is expected to carry on with rigify's own path - a performance patch
    must never be able to fail a run.
    """

    #: The one operator whose completion means "that edit session has been applied".
    MODE_MODULE = "object"
    MODE_OPERATOR = "mode_set"

    def __init__(self):
        self.copies = 0
        self.flushes = 0
        self.holders = []
        self.pending = []
        self.reason = "not installed"
        self.installed = False
        self.error = None
        self._legacy = None
        self._modern = None
        self._properties = None
        self._patched = None
        self._original_ops = None
        self._start_copies = 0
        self._start_flushes = 0

    # --- lifecycle ----------------------------------------------------------------------------------------
    def install(self):
        """Wrap rigify's legacy copy. Returns True on success, False with `reason` set otherwise."""
        try:
            import rigify.utils.bones as bones
        except Exception as error:  # noqa: BLE001
            self.reason = "rigify is not importable (%s)" % error
            return False
        for attribute in ("_legacy_copy_bone", "copy_bone", "copy_bone_properties"):
            if not hasattr(bones, attribute):
                self.reason = ("this rigify has no %s.%s - it is not the implementation this was written "
                               "against" % (bones.__name__, attribute))
                return False
        if not _is_operator_host(self.MODE_MODULE, self.MODE_OPERATOR):
            self.reason = "bpy.ops.%s.%s is not there to watch for the end of an edit session" % (
                self.MODE_MODULE, self.MODE_OPERATOR)
            return False
        # The proxy is installed first and deliberately: without a session-boundary signal, batching would be fast
        # and wrong, so nothing is patched unless the signal is in place.
        if not self._install_proxy():
            return False

        self._legacy = bones._legacy_copy_bone
        self._modern = bones.copy_bone
        self._properties = bones.copy_bone_properties
        self._patched = self._make_patched()
        self.installed = True
        self.reason = "installed"
        self._start_copies, self._start_flushes = self.copies, self.flushes
        self._rescan()
        return True

    def uninstall(self):
        """Put every module back, flush whatever is left, and restore `bpy.ops`."""
        if self._patched is not None:
            try:
                self.flush()
            except Exception as error:  # noqa: BLE001
                self.error = "%s: %s" % (type(error).__name__, error)
            for name in list(self.holders):
                module = sys.modules.get(name)
                if module is None:
                    continue
                if getattr(module, "copy_bone", None) is self._patched:
                    try:
                        setattr(module, "copy_bone", self._legacy)
                    except Exception:  # noqa: BLE001
                        pass
            try:
                import rigify.utils.bones as bones
                if getattr(bones, "_legacy_copy_bone", None) is self._patched:
                    bones._legacy_copy_bone = self._legacy
            except Exception:  # noqa: BLE001
                pass
        if self._original_ops is not None:
            try:
                setattr(bpy, "ops", self._original_ops)
            except Exception as error:  # noqa: BLE001
                self.error = "%s: %s" % (type(error).__name__, error)
            self._original_ops = None
        self.installed = False
        return self.report()

    def __enter__(self):
        self.install()
        return self

    def __exit__(self, *_exception):
        self.uninstall()
        return False

    # --- the patch itself ---------------------------------------------------------------------------------
    def _make_patched(self):
        def patched(obj, bone_name, assign_name=''):
            """`_legacy_copy_bone`, without the round trip.

            The one behavioural difference from rigify's version is that this returns with the object still in edit
            mode, exactly as rigify's does (it re-enters edit mode before returning) - so a caller cannot tell.
            """
            if obj.mode != 'EDIT':
                # A caller that left the session and came back: the previous session has been applied, so anything
                # still owed can be copied now rather than being lost.
                self.flush()
            name = self._modern(obj, bone_name, assign_name, parent=True, bbone=True)
            self.pending.append((obj, bone_name, name))
            self.copies += 1
            self._rescan()
            return name

        return patched

    def _rescan(self):
        """Rewrite every module that holds its own reference - rigify imports rig types lazily, so this runs often."""
        for name, module in list(sys.modules.items()):
            if module is None or name in self.holders:
                continue
            try:
                holder = getattr(module, "copy_bone", None)
            except Exception:  # noqa: BLE001
                continue
            if holder is self._legacy:
                try:
                    setattr(module, "copy_bone", self._patched)
                except Exception:  # noqa: BLE001
                    continue
                self.holders.append(name)
        try:
            import rigify.utils as utils
            import rigify.utils.bones as bones
            if getattr(bones, "_legacy_copy_bone", None) is self._legacy:
                bones._legacy_copy_bone = self._patched
            if getattr(utils, "copy_bone", None) is self._legacy:
                utils.copy_bone = self._patched
                if utils.__name__ not in self.holders:
                    self.holders.append(utils.__name__)
        except Exception:  # noqa: BLE001
            pass

    def _install_proxy(self):
        try:
            real = bpy.ops
            setattr(bpy, "ops", _OpsProxy(real, self.MODE_MODULE, self.MODE_OPERATOR, self.after_mode_set))
        except Exception as error:  # noqa: BLE001
            self.reason = "bpy.ops cannot be wrapped in this build (%s)" % error
            return False
        if getattr(bpy, "ops", None) is real:
            self.reason = "setting bpy.ops had no effect in this build"
            return False
        self._original_ops = real
        return True

    # --- the batching -------------------------------------------------------------------------------------
    def after_mode_set(self):
        """Called after every `bpy.ops.object.mode_set`: if the object is no longer editing, the session is over."""
        if self.pending:
            self.flush()

    def flush(self):
        """Copy the queued properties - but only for objects that are actually out of edit mode.

        A bone created in an edit session has no pose bone until the session is applied, so a pending entry whose
        object is still in EDIT mode is left alone; `copy_bone_properties` would raise on it (its own guard:
        `MetarigError("Cannot copy bone properties in edit mode")`).
        """
        if not self.pending:
            return 0
        copied = 0
        remaining = []
        for obj, source, target in self.pending:
            if obj.mode not in ('OBJECT', 'POSE'):
                remaining.append((obj, source, target))
                continue
            try:
                self._properties(obj, source, target)
                copied += 1
            except Exception as error:  # noqa: BLE001
                # Reported, never raised: a bone that cannot be copied must not take the run down. It stays queued
                # for the next boundary, and `error` says what happened.
                self.error = "%s: %s" % (type(error).__name__, error)
                remaining.append((obj, source, target))
        self.pending = remaining
        if copied:
            self.flushes += 1
        return copied

    # --- reporting ----------------------------------------------------------------------------------------
    def report(self, since=None):
        copies = self.copies - (since or {}).get("copies", 0)
        flushes = self.flushes - (since or {}).get("flushes", 0)
        return {"installed": self.installed, "reason": self.reason, "copies": copies, "flushes": flushes,
                "switches_avoided": (copies * 2) - (flushes * 2), "pending": len(self.pending),
                "holders": list(self.holders), "error": self.error}

    def mark(self):
        """A point to measure a single run from, when one batcher covers several runs."""
        return {"copies": self.copies, "flushes": self.flushes}

    def describe(self, since=None):
        """One line for the console and the run's trace."""
        counts = self.report(since)
        if not counts["installed"]:
            return "%s: skipped (%s)" % (TAG, counts["reason"])
        return ("%s: %d bone copy(ies) batched, %d property pass(es), %d mode switch(es) avoided%s"
                % (TAG, counts["copies"], counts["flushes"], counts["switches_avoided"],
                   " [%s]" % counts["error"] if counts["error"] else ""))
