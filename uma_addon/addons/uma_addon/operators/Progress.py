"""Named steps for a long run: how long each part took, and a bar that says it is still working.

A `Shading` run and a Pumpkin Rig run are both tens of seconds, and the sign of life until now was that the window
stopped answering. The user asked for the other thing: *"maybe we should separate them into logical steps and log
time? We could output it as a progress bar (some addons do that in the lower screen bar somehow) so the user knows
that it's working, and not just frozen."*

Blender has exactly that, and it needs no drawing code from us:

* `context.workspace.status_text_set(text)` - the one-line status bar at the bottom of the window;
* `window_manager.progress_begin/update/end` - the progress bar the file importers use, drawn in that same strip.

Both are **cosmetic and optional**: neither exists in a background run (no window, no workspace), so every call here
is guarded and a failure cannot take a run down. The part that survives into a trace, and into the numbers this
project decides on, is the timing - *what eats the time* is a measurement, not a guess, and both operators now say
so in one format:

    timings: the outline shell 0.3s | the face-normal transfer 2.6s | hook up the drivers 11.4s | total 21.7s

**What it cannot do**: move the bar *during* a long call into C. The rig run's dominant step is a single
`bpy.ops.uma.mmr_rig`, and Blender's event loop is blocked while it runs, so the bar jumps when it returns. The
status line says which step is running instead ("MMR is generating - the long one"), which is what a user needs to
know they are not frozen.

`LAST` holds the finished run for a probe to read (`step_timing_check.py`), because the operator's own report goes to
the console and a background probe cannot read its own stdout.
"""
import time

try:
    import bpy
except ImportError:  # pragma: no cover - only when imported outside Blender
    bpy = None

#: The finished run: `{"title": str, "steps": [(label, seconds)], "total": float, "started": float}`.
LAST = {}

#: The runs in progress. Both operators can return early - a shading run cancels on a missing manifest, a rig run on
#: a stale build - and a progress bar left open covers the status bar for the rest of the session, which is worse
#: than never having shown one. So `begin()` registers the run and the operator's own `finally` calls
#: `finish_running()`: nothing has to remember to close the bar on any of the early exits.
RUNNING = []

#: Set to False by a probe that wants the timings without the console noise.
VERBOSE = True


def begin(title, trace=None, debug=None):
    """Start a run's steps and register it, so an early return still closes the bar."""
    steps = Steps(title, trace=trace, debug=debug)
    RUNNING.append(steps)
    return steps


def finish_running():
    """Close every run still open. Safe to call when there is nothing to close, and never raises."""
    closed = 0
    while RUNNING:
        steps = RUNNING.pop()
        try:
            steps.finish()
            closed += 1
        except Exception:  # noqa: BLE001 - closing a bar is never worth an exception
            pass
    return closed


def current():
    """The innermost run in progress, or None - what a *helper* uses to time its own part of a step.

    `FaceNormals.apply_face_normals` is the case this exists for: it is 95% of a shading run's time (measured in
    `step_timing_check.py`) and it is called from inside it, so the interesting breakdown is *inside* an existing
    step rather than a new top-level one. A helper that is also called on its own - the panel's own "apply face
    normals" button - gets None and simply does not time anything.
    """
    return RUNNING[-1] if RUNNING else None


def _areas():
    """`(workspace, window_manager)` when this run has a UI, else `(None, None)`.

    A background run has no window and no workspace, and an operator can be run from a script while the workspace is
    in a strange state, so this never assumes: it returns what is there and the callers skip what is not.
    """
    if bpy is None:
        return None, None
    try:
        context = bpy.context
    except Exception:  # noqa: BLE001 - a context can be gone during teardown
        return None, None
    return getattr(context, "workspace", None), getattr(context, "window_manager", None)


#: `True` brings back `wm.progress_begin/update/end` - the numeric percentage Blender draws at the mouse cursor.
#: Off by default since 2.30.1: the status line says more (the step, its note, the same percentage) and does not look
#: like a glitch. The blendcap add-on reached the same conclusion on its own (`operators_retarget.py:787`:
#: *"No wm.progress_begin/update/end calls - those cause Blender to show a numeric percentage in the cursor"*).
CURSOR_BAR = False


class Steps:
    """One run's steps: each timed, each reported, and a bar that moves as they pass.

    `plan()` declares what the run intends to do, which is what gives the bar a denominator - without it every mark
    would be a guess at the fraction done. The weights are *relative* costs, so the bar moves roughly in proportion
    to the time rather than in proportion to the number of steps; they come from the measurements in
    `docs/PERFORMANCE.md` and do not have to be exact, because the timings are the part that is checked.

    Every step is a **duration**, whether it came from `mark` (the seconds since the previous mark, which is how
    `RigifyChains` has always reported its phases), from `phase` (a block), or from `record` (timed elsewhere, like
    the MMR call). One list, one meaning, and `timings()` is a straight joining of it.
    """

    def __init__(self, title, trace=None, debug=None):
        self.title = title
        self.trace = trace
        self.debug = VERBOSE if debug is None else debug
        self.started = time.perf_counter()
        self.steps = []                    # [(label, seconds, nested)] - nested ones are part of their parent's
        self.plan_steps = []               # [(label, weight)]
        self.plan_notes = {}               # label -> the note the status line adds while that step runs
        self._last_mark = self.started
        self._open = {}                    # label -> perf_counter at entry
        self._depth = 0                    # how many phases are open, so a nested one can be marked as such
        self._plan_total = 0.0
        self._done_weight = 0.0
        self._bar = False

    # --- the plan ---------------------------------------------------------------------------------------
    def plan(self, *pairs):
        """Declare the run's steps as `(label, weight)` or `(label, weight, note)` triples.

        The weights are what the *status line's* percentage is a fraction of, and the optional note is appended to
        that line while the step runs - it exists for the one step that cannot show progress at all, so a user waiting
        on it is told that instead of watching for movement that will not come.
        """
        self.plan_steps = []
        self.plan_notes = {}
        for entry in pairs:
            label, weight = entry[0], float(entry[1])
            self.plan_steps.append((label, weight))
            if len(entry) > 2 and entry[2]:
                self.plan_notes[label] = str(entry[2])
        self._plan_total = sum(weight for _label, weight in self.plan_steps) or 1.0
        # The window manager's own progress bar is deliberately *not* used: `wm.progress_begin/update` draws nothing
        # but a numeric percentage at the mouse cursor, which is easy to mistake for a glitch and says less than the
        # status line does (measured against the user's own report - *"I could only see the cursor change to some
        # numbers"* - and the same choice the blendcap add-on documents at `operators_retarget.py:787`). The status
        # line below carries the step, its note and the same percentage, and it stays put for the whole step.
        # Set `CURSOR_BAR = True` to bring the cursor readout back.
        if CURSOR_BAR:
            _workspace, wm = _areas()
            if wm is not None:
                try:
                    wm.progress_begin(0, 100)
                    self._bar = True
                except Exception:  # noqa: BLE001 - a bar is never worth an exception
                    self._bar = False
        return self

    def _advance(self, label):
        """Move the status line to wherever `label` sits in the plan, and say what the step is."""
        weight = dict(self.plan_steps).get(label)
        if weight is not None:
            self._done_weight += weight
        fraction = min(1.0, self._done_weight / self._plan_total)
        self.status(label, fraction, self.plan_notes.get(label))
        if not self._bar:
            return
        _workspace, wm = _areas()
        if wm is None:
            return
        try:
            wm.progress_update(int(fraction * 100))
        except Exception:  # noqa: BLE001
            pass

    def status(self, text, fraction=None, note=None):
        """The status bar line: what is happening now, how far along, and why it may look stuck."""
        workspace, _wm = _areas()
        if workspace is None:
            return
        line = "%s: %s" % (self.title, text)
        if fraction is not None:
            line = "%s  [%d%%]" % (line, int(fraction * 100))
        if note:
            line = "%s - %s" % (line, note)
        try:
            workspace.status_text_set(line)
        except Exception:  # noqa: BLE001
            pass

    # --- the steps --------------------------------------------------------------------------------------
    def mark(self, label):
        """Record the step that just finished: the seconds since the previous mark.

        `RigifyChains` used to keep a list of cumulative elapsed times and subtract them when building the `timings:`
        line; recording the difference here is the same arithmetic done once, and it means every entry in
        `self.steps` means the same thing.
        """
        now = time.perf_counter()
        self.record(label, now - self._last_mark)
        self._last_mark = now

    class _Phase:
        """`with steps.phase("name"):` - times the block and reports it on the way out, failure or not."""

        def __init__(self, steps, label):
            self.steps = steps
            self.label = label

        def __enter__(self):
            self.steps.status(self.label)
            self.steps._open[self.label] = time.perf_counter()
            self.steps._depth += 1
            return self.steps

        def __exit__(self, kind, value, tb):
            began = self.steps._open.pop(self.label, None)
            self.steps._depth -= 1
            if began is not None:
                # `nested` because a phase inside another phase is *part of* its parent's seconds: the breakdown is
                # worth having in the trace, but summing every step must not count those twice
                self.steps.record(self.label, time.perf_counter() - began,
                                  nested=self.steps._depth > 0)
            return False

    def phase(self, label):
        """Time one block of work, however it ends. Cheap enough to put around a loop body."""
        return Steps._Phase(self, label)

    def record(self, label, seconds, nested=False):
        """Add a step that was timed elsewhere (the MMR call is timed by its wrapper, not here)."""
        self.steps.append((label, max(0.0, seconds), bool(nested)))
        # A `mark` measures from the last *recorded* step, so a run that mixes `phase` and `mark` (both operators
        # do) cannot double-count the phase it just timed.
        self._last_mark = time.perf_counter()
        self._say(label, seconds)
        self._advance(label)

    def note(self, text):
        """Progress *within* a step - one material of seven, say. Status bar only; no step, no timing."""
        self.status(text)

    def _say(self, label, seconds=None):
        line = "%s: %s" % (label, "%.1fs" % seconds if seconds is not None else "done")
        if self.trace is not None:
            self.trace.info(line)
        elif self.debug:
            print("[uma_addon] " + line)

    # --- the report -------------------------------------------------------------------------------------
    def timings(self):
        """The `timings:` line, in the format this project's traces, docs and handoffs already quote.

        A nested step is indented, because its seconds are *inside* its parent's: the line reads as a breakdown, and
        nobody adds the nested ones to the total by accident.
        """
        parts = ["%s%s %.1fs" % ("  " if nested else "", label, seconds)
                 for label, seconds, nested in self.steps]
        parts.append("total %.1fs" % self.total())
        return "timings: " + " | ".join(parts)

    def total(self):
        """Seconds from the start - the wall clock, so a step that was not recorded is still in the total."""
        return time.perf_counter() - self.started

    def finish(self):
        """Close the bar and hand the run to `LAST` for a probe to read."""
        _workspace, wm = _areas()
        if wm is not None and self._bar:
            try:
                wm.progress_end()
            except Exception:  # noqa: BLE001
                pass
        self._bar = False
        workspace, _wm = _areas()
        if workspace is not None:
            try:
                workspace.status_text_set("")
            except Exception:  # noqa: BLE001
                pass
        LAST.clear()
        LAST.update({"title": self.title, "steps": list(self.steps), "total": self.total(),
                     "started": self.started})
        if self.trace is not None:
            self.trace.info(self.timings())
        elif self.debug:
            print("[uma_addon] " + self.timings())
        return LAST
