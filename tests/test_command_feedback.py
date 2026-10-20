"""Commands say what happened (mlaser/ui/command_feedback.py, UI audit D-5).

* the registry walk: every command, run on a fresh window (an empty drawing, then a drawing with
  no selection; offline), shows something: a dialog or message box, a file / input prompt, a
  menu, a status-bar message, a log or alarm line, a checked / mode / workspace / dock / window /
  selection / view change, a document change, or a new window.  The few commands that only bring
  something to the front that is already in front are checked for that state instead
  (:data:`IDEMPOTENT`);
* safety commands (stop, hold, disarm, alarm reset, off-commands) are never guarded: their handler
  always runs at once;
* a command that cannot act is not called, and the status bar says why;
* entries the user mode disables say which mode they need (toolbar tooltip, palette);
* disabled buttons say why (tooltip), in every workspace.
"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import (QAbstractButton, QApplication, QDialog, QDockWidget, QFileDialog, QInputDialog, QMenu,
                               QMessageBox)

from mlaser import ops
from mlaser.model import Document
from mlaser.ui import command_feedback as F
from wiring import dispose, real_window

#: commands not run by the walk
SKIP = {"app.exit": "closes the application"}

#: commands that bring a panel / workspace / mode to the front: run while it is already in front
#: they change nothing, so the walk checks the state they promise instead
IDEMPOTENT = {
    "diag.log": lambda w: w.log_dock.isVisible(),
    "nest.show": lambda w: w.extensions["ext_nest"][0].dock.isVisible(),
    "shell.mode_service": lambda w: w.user_mode == "service",
    "workspace.design": lambda w: w.shell.host.current == "design",
    "view.zoom_fit": lambda w: True,          # the drawing is shown fitted when it is set
    "view.fit_table": lambda w: True,         # an empty drawing is shown fitted to the table
}


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def stubbed(monkeypatch):
    """Dialogs, prompts and menus record that they were shown and return at once."""
    events: list[str] = []

    def rec(kind, ret=None):
        def f(*_a, **_k):
            events.append(kind)
            return ret
        return f
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: (events.append("dialog"), 0)[1])
    monkeypatch.setattr(QDialog, "open", lambda self, *a: events.append("dialog"))
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: (events.append("msgbox"), 0)[1])
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(rec("msgbox", QMessageBox.No)))
    for n in ("getOpenFileName", "getSaveFileName"):
        monkeypatch.setattr(QFileDialog, n, staticmethod(rec("file", ("", ""))))
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(rec("file", ([], ""))))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(rec("file", "")))
    for n in ("getInt", "getDouble", "getText", "getItem"):
        monkeypatch.setattr(QInputDialog, n, staticmethod(rec("input", (None, False))))
    monkeypatch.setattr(QMenu, "exec", lambda self, *a: events.append("menu"))
    monkeypatch.setattr(QMenu, "popup", lambda self, *a: events.append("menu"))
    try:
        from mlaser.controller import nettest
        monkeypatch.setattr(nettest, "network_test", lambda *a, **k: [])
    except ImportError:
        pass
    return events


def _window(app):
    w = real_window(exec_dialogs=True, prompts=True)
    w.control.confirm_fn = lambda key: False
    w.show()
    app.processEvents()
    return w


def _snapshot(w, app):
    return (tuple(a.isChecked() for a in w.ribbon.actions.values()), w.canvas.mode, w.shell.host.current,
            w.log_panel.text.blockCount(), w.log_panel.alarms.rowCount(),
            frozenset(id(x) for x in app.topLevelWidgets() if x.isVisible()),
            tuple(d.isVisible() for d in w.findChildren(QDockWidget)), len(w.document.parts),
            tuple(p.selected for p in w.document.parts), str(w.canvas.transform()), str(w.windowState()),
            bool(w.shell.palette is not None and w.shell.palette.isVisible()), w.user_mode)


def _walk(app, events, with_doc: bool) -> list[str]:
    w = _window(app)
    sb = w.statusBar()
    sb.messageChanged.connect(lambda m: events.append("status") if m else None)
    w.docs.subscribe(lambda _kind: events.append("doc"))
    silent = []
    try:
        for cmd in sorted(w.ribbon.actions):
            if cmd in SKIP:
                continue
            if with_doc:
                w.set_document(ops.demo_document())
                w.canvas.select_parts([])
            else:
                w.set_document(Document())
            sb.clearMessage()
            app.processEvents()
            events.clear()
            before = _snapshot(w, app)
            w.run(cmd)
            app.processEvents()
            if not events and _snapshot(w, app) == before:
                check = IDEMPOTENT.get(cmd)
                if check is None or not check(w):
                    silent.append(cmd)
            if cmd.startswith("shell.mode_"):
                w.set_user_mode("service")
            for x in app.topLevelWidgets():
                if x is not w and x.isVisible() and x.isWindow():
                    x.close()
            if w.shell.host.current != "design":
                w.shell.show_workspace("design")
    finally:
        dispose(w)
    return silent


def test_every_command_gives_feedback_on_an_empty_drawing(app, stubbed):
    assert _walk(app, stubbed, with_doc=False) == []


def test_every_command_gives_feedback_without_a_selection(app, stubbed):
    assert _walk(app, stubbed, with_doc=True) == []


# --------------------------------------------------------------------------- safety
SAFETY_IDS = ("run.stop", "run.hold", "sim.stop", "machine.disarm", "card.clear_alarms", "card.break_resume")


def test_safety_commands_are_never_guarded():
    for cmd in SAFETY_IDS:
        assert F.is_unguarded(cmd), cmd
    for cmd in F.NEEDS:
        assert not F.is_unguarded(cmd), f"{cmd} is a safety command: it must not have preconditions"
    for word in ("stop", "halt", "abort", "disarm", "reset", "off", "pause", "hold"):
        assert F.is_unguarded(f"x.{word}") and F.is_unguarded(f"x.{word}_all") and F.is_unguarded(f"x.y_{word}")


def test_safety_handlers_run_at_once_whatever_the_state(app, stubbed):
    """With nothing to act on (empty drawing, no selection, offline) a stop / hold / disarm /
    alarm-reset still reaches its handler, at once."""
    w = _window(app)
    try:
        w.set_document(Document())
        ran = []
        for cmd in sorted(c for c in w.commands.commands if F.is_unguarded(c)):
            spec = w.commands.get(cmd)
            spec.handler, old = (lambda *a, c=cmd: ran.append(c)), spec.handler
            try:
                w._dispatch(cmd) if not spec.checkable else w._dispatch(cmd, True)
            finally:
                spec.handler = old
            assert ran and ran[-1] == cmd, f"{cmd} did not reach its handler"
        assert {"run.stop", "run.hold"} <= set(ran)
    finally:
        dispose(w)


def test_unmet_precondition_skips_the_handler_and_says_why(app, stubbed):
    w = _window(app)
    try:
        w.set_document(ops.demo_document())
        w.canvas.select_parts([])
        spec = w.commands.get("edit.copy")
        called = []
        spec.handler, old = (lambda: called.append(1)), spec.handler
        try:
            w.run("edit.copy")
        finally:
            spec.handler = old
        assert not called
        assert w.statusBar().currentMessage() == w.tr_.tr("feedback.need_selection")
        w.canvas.select_parts([0, 1])
        w.run("edit.copy")
        assert w.statusBar().currentMessage() == w.tr_.format("feedback.copied", n=2)
        w.run("edit.paste")                       # now there is something to paste
        assert len(w.document.parts) == len(ops.demo_document().parts) + 2
    finally:
        dispose(w)


def test_machine_command_offline_says_connect_first(app, stubbed):
    w = _window(app)
    try:
        assert not w.ctl.state().connected
        w.run("machine.home")
        assert w.command_feedback.last == w.tr_.tr("feedback.need_connection")
    finally:
        dispose(w)


# --------------------------------------------------------------------------- disabled entries
def test_mode_disabled_actions_say_which_mode(app, stubbed):
    w = _window(app)
    try:
        assert w.set_user_mode("operator")
        app.processEvents()
        off = [(c, a) for c, a in w.ribbon.actions.items() if not a.isEnabled()]
        assert off
        for cmd, act in off:
            reason = F.disabled_reason(w, cmd)
            assert reason and reason in act.toolTip(), cmd
            assert act.statusTip() == reason
        cmd, act = off[0]
        assert w.set_user_mode("service")
        app.processEvents()
        assert act.isEnabled() and F.disabled_reason(w, cmd) == ""
        assert w.tr_.tr("shell.mode.engineer") not in act.toolTip() or "mode" not in act.statusTip()
    finally:
        dispose(w)


def test_palette_shows_the_reason(app, stubbed):
    """An entry disabled for a reason (style.set_enabled on its action) says it in the palette."""
    from mlaser.ui.style import set_enabled
    w = _window(app)
    try:
        act = w.ribbon.actions["view.zoom_fit"]
        set_enabled(act, False, "Busy: a job is running")
        assert F.disabled_reason(w, "view.zoom_fit") == "Busy: a job is running"
        w.shell.open_palette()
        pal = w.shell.palette
        pal.edit.setText(act.text().replace("&", "").strip())
        pal.refresh()
        tips = [pal.list.item(i).toolTip() for i in range(pal.list.count())]
        assert any("Busy: a job is running" in t for t in tips), tips
        pal.close_palette()
        set_enabled(act, True)
        assert F.disabled_reason(w, "view.zoom_fit") == ""
    finally:
        dispose(w)


#: disabled buttons without a tooltip in files of other tracks (objectName or class: owner):
#: left for them, to be emptied (see the module docstring)
DISABLED_EXEMPT: dict[str, str] = {}


def test_disabled_buttons_say_why_in_every_workspace(app, stubbed):
    from mlaser.ui.style import walk_disabled_without_reason
    w = _window(app)
    w.resize(1512, 875)
    found = {}
    try:
        for ws_id in list(w.shell.host.specs):
            if not w.shell.show_workspace(ws_id):
                continue
            app.processEvents()
            for b in walk_disabled_without_reason(w, (QAbstractButton,)):
                key = b.objectName() or f"{type(b).__name__}:{b.text() or b.accessibleName()}"
                if key not in DISABLED_EXEMPT:
                    found[key] = (ws_id, type(b.parentWidget()).__name__ if b.parentWidget() else "")
    finally:
        dispose(w)
    assert not found, "\n".join(f"{k}: {v}" for k, v in sorted(found.items()))
