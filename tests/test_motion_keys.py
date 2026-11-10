"""Motion-starting shortcuts act only on the Run workspace; Space only holds (deep review
2026-11, UX findings 7 and 8; ui/shell/motion_keys.py).

A stray F5 / F6 / Shift+F6 / Space / Enter in any workspace or in a text field starts nothing;
Stop (F12, Esc while a job runs) still stops from everywhere, text fields included."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.ui

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QDialog, QLineEdit, QMessageBox,  # noqa: E402
                               QPushButton)

from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part  # noqa: E402


def rect_doc(w=400.0, h=300.0, layer=1):
    pts = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]
    return Document(parts=[Part([Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)],
                                Craft(layer=layer))], layers={layer: LayerParams(layer, speed=100.0, power_pct=50)})


STRAY = [(Qt.Key_F5, Qt.NoModifier), (Qt.Key_F6, Qt.NoModifier), (Qt.Key_F6, Qt.ShiftModifier),
         (Qt.Key_Space, Qt.NoModifier), (Qt.Key_Return, Qt.NoModifier), (Qt.Key_Enter, Qt.KeypadModifier)]


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch):
    from wiring import dispose, real_window
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window(user_mode="engineer")
    w.resize(1280, 800)
    w.show()
    w.activateWindow()
    app.processEvents()
    yield w
    w.ctl.stop()
    dispose(w)


@pytest.fixture
def calls(win, monkeypatch):
    """Every run action that would start motion, recorded instead of run."""
    out: list[str] = []
    su = win.safety_ui
    for name in ("start", "frame", "dry_run", "start_selection", "run_queue", "resume_kept"):
        monkeypatch.setattr(su, name, lambda *a, _n=name, **k: out.append(_n))
    ctl = win.ctl
    for name in ("start", "frame", "dry_run", "resume", "resume_saved"):
        if hasattr(ctl, name):
            monkeypatch.setattr(ctl, name, lambda *a, _n=name, **k: out.append("ctl." + _n))
    return out


def _pump(app, s=0.05):
    t = time.monotonic() + s
    while time.monotonic() < t:
        app.processEvents()
        time.sleep(0.005)


def _press(target, key, mod=Qt.NoModifier):
    QTest.keyClick(target, key, mod)


def _workspaces(win) -> list[str]:
    host = win.shell.host
    return [ws for ws in host.visible_ids() if ws in host.specs]


def _idle_focus(win, app):
    """Focus on something that is not a text field: the canvas when shown, else the page."""
    c = getattr(win, "canvas", None)
    if c is not None and c.isVisible():
        c.setFocus()
    else:
        win.shell.host.pages[win.shell.host.current].setFocus()
    _pump(app)
    fw = QApplication.focusWidget() or win
    return fw


def _search_field(win) -> QLineEdit:
    """The top bar's "Search commands" field (the reviewer's F6-in-the-search-field case)."""
    f = win.findChild(QLineEdit, "shell_search")
    assert f is not None and f.isVisible()
    return f


def _text_fields(win) -> list:
    """The search field and the first editable field of the workspace page, if any."""
    out = [_search_field(win)]
    page = win.shell.host.pages.get(win.shell.host.current)
    for e in (page.findChildren(QAbstractSpinBox) + page.findChildren(QLineEdit)) if page is not None else []:
        if e.isVisible() and e.isEnabled() and e.objectName() != "qt_spinbox_lineedit" and \
                not getattr(e, "isReadOnly", lambda: False)():
            out.append(e)
            break
    return out


def _close_popups(win, app):
    pal = getattr(win.shell, "palette", None)
    if pal is not None and pal.isVisible():
        pal.hide()
    _pump(app)


# ------------------------------------------------------------------------- finding 7
def test_motion_actions_do_not_auto_repeat(win):
    for cmd in ("run.start", "run.frame", "run.dry_run"):
        assert not win.shell.action(cmd).autoRepeat(), cmd


@pytest.mark.parametrize("key,mod,kind", [(Qt.Key_F5, Qt.NoModifier, "start"), (Qt.Key_F6, Qt.NoModifier, "frame"),
                                          (Qt.Key_F6, Qt.ShiftModifier, "dry_run")])
def test_motion_key_elsewhere_switches_to_run_and_starts_nothing(win, app, calls, key, mod, kind):
    win.ctl.connect()
    win.set_document(rect_doc(w=100, h=100))
    _pump(app, 0.3)
    for ws in _workspaces(win):
        if ws == "run":
            continue
        win.shell.show_workspace(ws)
        _pump(app)
        target = _idle_focus(win, app)
        _press(target, key, mod)
        _pump(app)
        assert calls == [], f"{ws}: {calls}"
        assert win.shell.host.current == "run", ws
        assert win.shell.motion_keys.last == "switched"
        fw = QApplication.focusWidget()
        assert isinstance(fw, QPushButton) and fw.property("run_kind") == kind, (ws, fw)
        assert win.statusBar().currentMessage()
    assert win.ctl.state().mode == "ready"


@pytest.mark.parametrize("key,mod", STRAY)
def test_stray_key_in_a_text_field_starts_nothing(win, app, calls, key, mod):
    win.ctl.connect()
    win.set_document(rect_doc(w=100, h=100))
    seen = 0
    for ws in _workspaces(win):
        win.shell.show_workspace(ws)
        _pump(app)
        for f in _text_fields(win):
            _close_popups(win, app)
            f.setFocus()
            _pump(app)
            fw = QApplication.focusWidget()
            assert fw is f or (fw is not None and f.isAncestorOf(fw)), (ws, f, fw)
            _press(fw, key, mod)
            _pump(app)
            seen += 1
            assert calls == [], f"{ws} {f.objectName()}: {calls}"
            assert win.shell.host.current == ws     # typing in a field never switches
    _close_popups(win, app)
    assert seen > len(_workspaces(win))             # some page fields were tried too
    assert win.ctl.state().mode == "ready"


@pytest.mark.parametrize("key,mod", [k for k in STRAY if k[0] in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter)])
def test_stray_space_or_enter_in_each_workspace_starts_nothing(win, app, calls, key, mod):
    win.ctl.connect()
    win.set_document(rect_doc(w=100, h=100))
    assert win.ctl.arm()                         # the worst case: armed, a Start would fire
    for ws in _workspaces(win):
        win.shell.show_workspace(ws)
        _pump(app)
        _press(_idle_focus(win, app), key, mod)
        _pump(app)
        assert calls == [], f"{ws}: {calls}"


@pytest.mark.parametrize("kind", ["start", "frame", "dry_run"])
@pytest.mark.parametrize("key,mod", [(Qt.Key_Space, Qt.NoModifier), (Qt.Key_Return, Qt.NoModifier),
                                     (Qt.Key_Enter, Qt.KeypadModifier)])
def test_space_or_enter_on_a_focused_motion_button_starts_nothing(win, app, calls, kind, key, mod):
    """The shortcut leaves the focus on the button: a stray Space / Enter then must not press it."""
    win.ctl.connect()
    win.set_document(rect_doc(w=100, h=100))
    assert win.ctl.arm()
    win.shell.show_workspace("design")
    _pump(app)
    _press(_idle_focus(win, app), *{"start": (Qt.Key_F5, Qt.NoModifier), "frame": (Qt.Key_F6, Qt.NoModifier),
                                    "dry_run": (Qt.Key_F6, Qt.ShiftModifier)}[kind])
    _pump(app)
    b = QApplication.focusWidget()
    assert isinstance(b, QPushButton) and b.property("run_kind") == kind
    _press(b, key, mod)
    _pump(app)
    assert calls == []
    b.click()                                    # the mouse still works
    assert calls == [kind]


@pytest.mark.parametrize("key,mod,kind", [(Qt.Key_F5, Qt.NoModifier, "start"), (Qt.Key_F6, Qt.NoModifier, "frame"),
                                          (Qt.Key_F6, Qt.ShiftModifier, "dry_run")])
def test_motion_key_on_the_run_workspace_acts(win, app, calls, key, mod, kind):
    win.shell.show_workspace("run")
    _pump(app)
    _press(_idle_focus(win, app), key, mod)
    _pump(app)
    assert calls == [kind]
    f = _search_field(win)
    f.setFocus()
    _pump(app)
    _press(f, key, mod)                          # ... but not while typing, even there
    _pump(app)
    assert calls == [kind]


def test_menu_and_palette_still_run_the_commands(win, app, calls):
    """Only the keyboard shortcut is gated: the menu / palette / run bar are deliberate."""
    win.shell.show_workspace("design")
    _pump(app)
    win.shell.action("run.frame").trigger()
    win.run("run.dry_run")
    assert calls == ["frame", "dry_run"]


# ------------------------------------------------------------------------- Stop stays global
def _start_job(win, app):
    win.set_document(rect_doc(w=400, h=300))
    win.ctl.connect()
    assert win.ctl.arm()
    win.safety_ui.start()
    jobs = getattr(win, "jobs", None)
    if jobs is not None and jobs.last_run is not None:
        jobs.settle(jobs.last_run, timeout=30)
    t = time.monotonic() + 10
    while win.ctl.state().mode != "process" and time.monotonic() < t:
        _pump(app)
    assert win.ctl.state().mode == "process"


@pytest.mark.parametrize("key", [Qt.Key_F12, Qt.Key_Escape])
@pytest.mark.parametrize("where", ["design", "run", "text"])
def test_stop_keys_stop_from_everywhere(win, app, key, where):
    _start_job(win, app)
    if where == "text":
        f = _search_field(win)
        f.setFocus()
        target = f
    else:
        win.shell.show_workspace(where)
        _pump(app)
        target = _idle_focus(win, app)
    t0 = time.monotonic()
    _press(target, key)
    _pump(app, 0.2)
    assert win.ctl.state().mode == "ready", where
    assert time.monotonic() - t0 < 2.0


# ------------------------------------------------------------------------- finding 8
def test_space_only_holds(win, app):
    _start_job(win, app)
    win.shell.show_workspace("design")
    _pump(app)
    target = _idle_focus(win, app)
    t0 = time.monotonic()
    _press(target, Qt.Key_Space)
    _pump(app, 0.1)
    assert win.ctl.state().mode == "pause"
    assert time.monotonic() - t0 < 1.0                   # Hold is instant
    for _ in range(3):                                   # nervous second presses, 0.2 .. 1.5 s later
        _pump(app, 0.5)
        _press(_idle_focus(win, app), Qt.Key_Space)
        _pump(app, 0.1)
        assert win.ctl.state().mode == "pause"
    win.safety_ui.hold()                                 # the Hold button / menu: never continues
    _pump(app, 0.2)
    assert win.ctl.state().mode == "pause"
    # F5 outside the Run workspace does not continue either: it shows the Run workspace
    _press(_idle_focus(win, app), Qt.Key_F5)
    _pump(app, 0.2)
    assert win.ctl.state().mode == "pause" and win.shell.host.current == "run"
    # Continue is deliberate: the Start / Continue control (F5 on the Run workspace, or the button)
    win.shell.host.pages["run"].setFocus()
    _pump(app)
    _press(QApplication.focusWidget() or win, Qt.Key_F5)
    _pump(app, 0.3)
    assert win.ctl.state().mode == "process"


def test_keyboard_sheet_says_where_the_motion_keys_act(win):
    rows = win.shell.shortcut_rows()
    by_label = {(r[1], r[3]) for r in rows}
    tr = win.shell.tr.tr
    for cmd in ("run.start", "run.frame", "run.dry_run"):
        assert (win.shell.label(cmd), tr("shell.key.ctx_run")) in by_label
    assert (win.shell.label("run.stop"), tr("shell.key.ctx_window")) in by_label
    assert (tr("shell.key.hold"), tr("shell.key.ctx_job")) in by_label
    assert "continue" not in tr("shell.key.hold").lower().replace("never continues", "")
