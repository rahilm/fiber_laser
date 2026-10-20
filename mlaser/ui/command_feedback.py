"""Feedback for commands that cannot act (the UI audit's D-5: "commands say what happened").

A command run from the ribbon, a menu, the palette or a key goes through
:meth:`mlaser.ui.mainwindow.MainWindow._dispatch`, which asks :class:`CommandFeedback`

* **before** the handler: the command's preconditions (:data:`NEEDS`: a drawing with parts, a
  selection, two selected parts, a group, something to paste or redo, a nest result, a manual
  sort in progress).  When one is not met the handler is not called (it would do nothing) and
  the status bar says what is missing ("Select parts first");
* **after** the handler: a short result where the command changes nothing else that shows
  (:data:`AFTER`: "4 parts selected", "3 parts copied", "Stop sent", "No job is running"), and
  "Connect the machine first" for a machine command run while offline (the command still ran:
  the controller refused it and logged why).

**Safety commands are never guarded**: Stop, Hold, disarm, alarm reset and every other
off-command (:func:`is_unguarded`) always reach their handler at once; at most a message is
shown after it ran (docs/SAFETY.md; ``tests/test_command_feedback.py``).

Toolbar, menu and palette entries that the user mode disables say why ("Needs Engineer mode"):
:meth:`CommandFeedback.mode_reasons`, :func:`disabled_reason`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- safety
#: commands that are never wrapped by a precondition (stop / hold / off-commands)
UNGUARDED_IDS = frozenset({
    "run.stop", "run.hold", "sim.stop", "machine.disarm", "run.disarm", "card.clear_alarms",
    "card.stop", "card.break_resume", "machine.stop", "shell.stop", "run.toggle_arm",
})
_UNGUARDED_RE = re.compile(r"(^|[._])(stop|halt|abort|disarm|estop|e_stop|off|reset|clear_alarms?|pause|hold)"
                           r"($|[._])")


def is_unguarded(cmd: str) -> bool:
    """True for a safety command (stop, hold, disarm, alarm reset, an off-command): it always
    runs at once, never behind a precondition."""
    return cmd in UNGUARDED_IDS or bool(_UNGUARDED_RE.search(cmd))


# --------------------------------------------------------------------------- state helpers
def _doc(w: Any):
    return getattr(w, "document", None)


def _parts(w: Any) -> list:
    d = _doc(w)
    return list(getattr(d, "parts", None) or [])


def _selected(w: Any) -> list:
    return [p for p in _parts(w) if getattr(p, "selected", False)]


def _owner(w: Any, cmd: str) -> Any:
    """The object a command's handler is a bound method of (None for a plain function)."""
    reg = getattr(w, "commands", None)
    spec = reg.get(cmd) if reg is not None else None
    return getattr(getattr(spec, "handler", None), "__self__", None)


def _is_group(p: Any) -> bool:
    try:
        from mlaser.cad import arrange as AR
        return bool(AR.is_group(p))
    except Exception:  # noqa: BLE001 - a missing helper never blocks a command
        return False


def _connected(w: Any) -> bool:
    ctl = getattr(w, "ctl", None)
    try:
        return bool(ctl is not None and ctl.state().connected)
    except Exception:  # noqa: BLE001
        return True


def _nest_result(w: Any, cmd: str) -> bool:
    job = getattr(_owner(w, cmd), "job", None)
    res = getattr(job, "result", None)
    return bool(res is not None and getattr(res, "layouts", None))


def _manual_sort(w: Any, cmd: str) -> bool:
    return bool(getattr(_owner(w, cmd), "manual_active", False))


# --------------------------------------------------------------------------- preconditions
@dataclass(frozen=True)
class Need:
    """A precondition: ``ok(window, cmd)`` and the message id (with its English default and
    placeholder values) shown when it fails."""
    ok: Callable[[Any, str], bool]
    msg: str
    default: str
    values: tuple = ()


DOCUMENT = Need(lambda w, c: bool(_parts(w)), "feedback.need_document",
                "The drawing is empty: draw or import parts first")
SELECTION = Need(lambda w, c: bool(_selected(w)), "feedback.need_selection", "Select parts first")
TWO_SELECTED = Need(lambda w, c: len([p for p in _selected(w) if getattr(p, "contours", None)]) >= 2,
                    "feedback.need_selection_n", "Select at least {n} parts first", (("n", 2),))
TWO_PARTS = Need(lambda w, c: len(_parts(w)) >= 2, "feedback.need_parts", "This needs at least {n} parts in the "
                 "drawing", (("n", 2),))
GROUPS = Need(lambda w, c: any(_is_group(p) for p in _parts(w)), "feedback.need_groups", "The drawing has no groups")
SELECTED_GROUP = Need(lambda w, c: any(_is_group(p) for p in _selected(w)), "feedback.need_selected_group",
                      "Select a group first")
CLIPBOARD = Need(lambda w, c: bool(getattr(w, "_clipboard", None)), "feedback.need_clipboard",
                 "Nothing to paste: copy parts first")
REDO = Need(lambda w, c: bool(getattr(getattr(w, "docs", None), "can_redo", True)),
            "feedback.need_redo", "Nothing to redo")
UNDO = Need(lambda w, c: bool(getattr(getattr(w, "docs", None), "can_undo", True)),
            "feedback.need_undo", "Nothing to undo")
NEST_RESULT = Need(_nest_result, "feedback.need_nest_result", "Run the nest first")
MANUAL_SORT = Need(_manual_sort, "feedback.need_manual_sort", "Manual sort is not running: start it first")

#: command id -> its preconditions (checked in order; the first that fails is reported)
NEEDS: dict[str, tuple[Need, ...]] = {
    # edit
    "edit.copy": (SELECTION,), "edit.delete": (SELECTION,), "edit.paste": (CLIPBOARD,),
    "edit.redo": (REDO,), "edit.undo": (UNDO,),
    # selection (on an empty drawing)
    "select.all": (DOCUMENT,), "select.invert": (DOCUMENT,), "select.unclosed": (DOCUMENT,),
    "select.inner": (DOCUMENT,), "select.outer": (DOCUMENT,), "select.similar": (SELECTION,),
    # geometry and arrangement on the selection
    "geom.explode": (SELECTION,), "xform.align": (TWO_SELECTED,), "xform.size": (DOCUMENT,),
    "tool.group": (TWO_SELECTED,), "tool.group_select_all": (DOCUMENT, GROUPS),
    "tool.group_explode": (SELECTED_GROUP,), "tool.group_explode_all": (DOCUMENT, GROUPS),
    # cutting order
    "sort.first": (SELECTION,), "sort.last": (SELECTION,), "sort.manual": (TWO_PARTS,),
    "sort.manual_end": (MANUAL_SORT,), "sort.manual_prev": (MANUAL_SORT,),
    # technology and checks on the drawing
    "process.apply": (DOCUMENT,), "cam.lead_check": (DOCUMENT,), "cam.tip_check": (DOCUMENT,),
    "parts.review": (DOCUMENT,),
    # nest
    "nest.add_sheet": (SELECTION,), "nest.to_main": (NEST_RESULT,), "nest.export": (NEST_RESULT,),
}


# --------------------------------------------------------------------------- after the handler
def _n_selected(w: Any, cmd: str, result: Any) -> tuple[str, str, dict]:
    return "feedback.selected", "{n} parts selected", {"n": len(_selected(w))}


def _copied(w: Any, cmd: str, result: Any) -> Optional[tuple[str, str, dict]]:
    n = len(getattr(w, "_clipboard", None) or [])
    return ("feedback.copied", "{n} parts copied", {"n": n}) if n else None


def _hold(w: Any, cmd: str, result: Any) -> Optional[tuple[str, str, dict]]:
    ctl = getattr(w, "ctl", None)
    try:
        mode = ctl.state().mode if ctl is not None else ""
        active = bool(ctl.job_active()) if ctl is not None else False
    except Exception:  # noqa: BLE001
        return None
    if active or mode in ("process", "dry_run", "frame", "pause"):
        return None
    return "feedback.hold_idle", "No job is running: nothing to hold", {}


def _lead_check(w: Any, cmd: str, result: Any) -> Optional[tuple[str, str, dict]]:
    if not isinstance(result, list):
        return None
    n = len({getattr(i, "key", id(i)) for i in result})
    return ("cutstrat.msg.leads", "{n} lead-ins collide", {"n": n}) if n else \
        ("cutstrat.msg.leads_ok", "No lead-in collides", {})


def _tip_check(w: Any, cmd: str, result: Any) -> Optional[tuple[str, str, dict]]:
    if not isinstance(result, list):
        return None
    return "feedback.tip_check", "{n} pieces can tip", {"n": len(result)}


def _orders_to_nest(w: Any, cmd: str, result: Any) -> Optional[tuple[str, str, dict]]:
    if isinstance(result, list) and not result:
        return "feedback.need_orders", "No order lines to load: add orders first", {}
    return None


def _queue_sheet(w: Any, cmd: str, result: Any) -> Optional[tuple[str, str, dict]]:
    scr = _owner(w, cmd)
    runner = getattr(getattr(scr, "ws", None), "runner", None)
    if runner is None or getattr(runner, "state", "") == "running":
        return None
    msg = getattr(runner, "message", "")
    if msg:
        tr = getattr(w, "tr_", None)
        text = tr.tr(msg) if tr is not None and hasattr(tr, "has") and tr.has(msg) else str(msg)
        return "", text, {}
    return "feedback.queue_not_waiting", "The queue is not waiting for a sheet", {}


def _fixed(key: str, default: str) -> Callable[[Any, str, Any], tuple[str, str, dict]]:
    return lambda w, cmd, result: (key, default, {})


#: command id -> a function (window, cmd, handler result) -> (message id, default, values) or None
AFTER: dict[str, Callable[[Any, str, Any], Optional[tuple[str, str, dict]]]] = {
    **{c: _n_selected for c in ("select.all", "select.invert", "select.unclosed", "select.inner", "select.outer",
                                "select.similar", "tool.group_select_all")},
    "edit.copy": _copied,
    "run.hold": _hold,
    "run.stop": _fixed("feedback.stop_sent", "Stop sent"),
    "sim.stop": _fixed("feedback.sim_stopped", "Simulation stopped"),
    "preview.clear": _fixed("feedback.preview_cleared", "Preview cleared"),
    "nest.new": _fixed("feedback.nest_new", "New nest project"),
    "cam.lead_check": _lead_check,
    "cam.tip_check": _tip_check,
    "orders.to_nest": _orders_to_nest,
    "queue.confirm_sheet": _queue_sheet,
}

#: commands that need the machine: run offline they still go to the controller (which refuses
#: and logs why), and the status bar says what to do
MACHINE_PREFIXES = ("machine.home", "coord.return_origin", "coord.float", "coord.manual", "mon.", "hw.upgrade",
                    "card.params", "card.stream_process", "aux.", "machine.laser_")


def needs_machine(cmd: str) -> bool:
    return cmd.startswith(MACHINE_PREFIXES)


# --------------------------------------------------------------------------- the window side
class CommandFeedback:
    """Checks a command before it runs and reports after it ran (see the module docstring)."""

    MESSAGE_MS = 5000

    def __init__(self, window: Any) -> None:
        self.w = window
        self.last: str = ""                     # the last message shown (tests)

    def _text(self, key: str, default: str, values: dict) -> str:
        tr = getattr(self.w, "tr_", None)
        if not key:
            return default
        if tr is not None and tr.has(key):
            return tr.format(key, **values) if values else tr.tr(key)
        return default.format(**values) if values else default

    def say(self, key: str, default: str, values: Optional[dict] = None) -> str:
        text = self._text(key, default, dict(values or {}))
        self.last = text
        sb = getattr(self.w, "statusBar", None)
        if callable(sb):
            sb().showMessage(text, self.MESSAGE_MS)
        return text

    def unmet(self, cmd: str) -> Optional[Need]:
        """The first precondition of ``cmd`` that is not met (None: it may run)."""
        if is_unguarded(cmd):
            return None
        for need in NEEDS.get(cmd, ()):
            try:
                if not need.ok(self.w, cmd):
                    return need
            except Exception:  # noqa: BLE001 - a precondition that fails to evaluate never blocks
                continue
        return None

    def before(self, cmd: str) -> bool:
        """True when the handler should run; else the reason is in the status bar."""
        need = self.unmet(cmd)
        if need is None:
            return True
        self.say(need.msg, need.default, dict(need.values))
        return False

    def after(self, cmd: str, result: Any = None) -> None:
        fn = AFTER.get(cmd)
        out = None
        if fn is not None:
            try:
                out = fn(self.w, cmd, result)
            except Exception:  # noqa: BLE001 - feedback never breaks a command
                out = None
        if out is None and needs_machine(cmd) and not _connected(self.w):
            out = ("feedback.need_connection", "Connect the machine first", {})
        if out is not None:
            self.say(*out)

    # -------------------------------------------------------------- disabled entries
    def mode_reasons(self, *_args) -> None:
        """Give every action the user mode disables the reason as its tooltip and status tip,
        and restore the normal tooltip when it is enabled again."""
        ribbon = getattr(self.w, "ribbon", None)
        if ribbon is None:
            return
        for cmd, act in ribbon.actions.items():
            reason = disabled_reason(self.w, cmd)
            normal = act.property("tooltip_enabled")
            if reason and not act.isEnabled():
                if normal is None:
                    act.setProperty("tooltip_enabled", act.toolTip())
                    normal = act.toolTip()
                tip = f"{normal}\n{reason}" if normal and reason not in str(normal) else reason
                if act.toolTip() != tip:
                    act.setToolTip(tip)
                    act.setStatusTip(reason)
            elif normal is not None and act.isEnabled():
                act.setToolTip(str(normal))
                act.setStatusTip("")
                act.setProperty("tooltip_enabled", None)


def disabled_reason(w: Any, cmd: str) -> str:
    """Why ``cmd`` cannot run now ("" when it can): the user mode it needs, else the reason its
    action was disabled with (:func:`mlaser.ui.style.set_enabled`)."""
    shell = getattr(w, "shell", None)
    modes = getattr(shell, "modes", None)
    ribbon = getattr(w, "ribbon", None)
    act = ribbon.actions.get(cmd) if ribbon is not None else None
    own = str(act.property("disabled_reason") or "") if act is not None and not act.isEnabled() else ""
    if modes is None:
        return own
    spec = w.commands.get(cmd) if getattr(w, "commands", None) is not None else None
    try:
        if modes.allows(cmd, spec):
            return own
        from mlaser.services import usermodes as um
        need = um.min_mode_of(cmd, spec)
    except Exception:  # noqa: BLE001
        return ""
    tr = getattr(w, "tr_", None)
    name = tr.tr(f"shell.mode.{need}") if tr is not None else need.title()
    if tr is not None and tr.has("feedback.needs_mode"):
        return tr.format("feedback.needs_mode", mode=name)
    return f"Needs {name} mode"


def install(window: Any) -> CommandFeedback:
    """Attach the feedback to ``window`` (MainWindow) and keep the disabled entries' reasons in
    step with the user mode."""
    fb = CommandFeedback(window)
    window.command_feedback = fb
    hooks = getattr(window, "hooks", None)
    if hooks is not None:
        hooks.add("user_mode_changed", fb.mode_reasons, "command_feedback")
    fb.mode_reasons()
    return fb


__all__ = ["AFTER", "NEEDS", "Need", "CommandFeedback", "disabled_reason", "install", "is_unguarded",
           "needs_machine", "UNGUARDED_IDS"]
