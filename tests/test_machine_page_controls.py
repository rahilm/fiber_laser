"""Machine workspace controls that did nothing visible, one that deleted without asking, and
settings an operator could not find (UX review 2026-11-09, finding 14).

* "Set origin here" / "Go to origin" in the Float coordinate system (the default): disabled,
  with the reason, on the page and in the menus / ribbon; enabled for a tool system or Manual.
* Alarms > "Clear history": a question, "Clear history" the destructive button, Cancel the
  default (Enter keeps the history).
* Search: "travel" / "travel limit" / "soft limit" find the travel limit ("Maximum stroke"),
  "jog speed" the jog speeds, in Preferences and in Machine > Setup.
Real MainWindow, offscreen, against the card simulator."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/wiring.py


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch, tmp_path):
    from wiring import dispose, real_window

    from mlaser.controller.ui_adapter import UiController
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    ctl = UiController(poll_s=0.02)
    assert ctl.connect_simulator(time_scale=1.0)
    w = real_window(ctl)
    yield w
    ctl.disconnect()
    dispose(w)


def _ws(w):
    return w.extension("ext_machine").workspace


def test_origin_controls_disabled_in_the_float_system(app, win):
    from mlaser.controller import coords as C
    w = win
    ext = w.extension("ext_machine")
    ws = _ws(w)
    ext.select_coord(C.FLOAT)
    ws.tick()
    for b in (ws.b_set_origin, ws.b_return_origin):
        assert not b.isEnabled()
        assert "Float coordinate system" in b.toolTip()
    for cmd in ("coord.set_origin", "coord.return_origin"):
        a = w.ribbon.actions[cmd]
        assert not a.isEnabled() and "Float coordinate system" in a.toolTip()
    ext.select_coord(1)                               # tool coordinate system 1
    ws.tick()
    for b in (ws.b_set_origin, ws.b_return_origin):
        assert b.isEnabled() and "Float" not in b.toolTip()
    assert all(w.ribbon.actions[c].isEnabled() for c in ("coord.set_origin", "coord.return_origin"))
    ext.select_coord(C.FLOAT)
    ws.tick()
    assert not ws.b_set_origin.isEnabled()


def test_clear_alarm_history_asks_and_enter_keeps_it(app, win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from mlaser.services.alarm_history import AlarmRecord
    from mlaser.ui.verb_question import answer_with
    w = win
    ws = _ws(w)
    ws.history.clear()
    for i in range(3):
        ws.history.add(AlarmRecord("alarm", f"servo alarm {i}", time.time() - 60 + i, code=10 + i,
                                   cleared=time.time()))
    seen = []

    def enter(box, *a):
        d = box.defaultButton()
        seen.append({"default": d.text() if d else None, "title": box.windowTitle(), "text": box.text(),
                     "roles": {b.text(): box.buttonRole(b) for b in box.buttons()}})
        d.click()
        return 0
    monkeypatch.setattr(QMessageBox, "exec", enter)
    assert ws._clear_history() is False
    assert len(ws.history.entries()) == 3, "Enter cleared the alarm history"
    q = seen[-1]
    # macOS shows no window title on an alert (Qt drops it there): the bold question stands alone
    assert q["default"] == "Cancel" and "3" in q["text"]
    assert q["title"] == ("" if sys.platform == "darwin" else "Clear alarm history")
    assert q["roles"]["Clear history"] == QMessageBox.DestructiveRole
    monkeypatch.setattr(QMessageBox, "exec", lambda box, *a: answer_with(box, True))
    assert ws._clear_history() is True
    assert ws.history.entries() == []


@pytest.mark.parametrize("query,key", [("travel", "MAC.SoftLimitMaxLen"), ("travel limit", "MAC.SoftLimitMaxLen"),
                                       ("soft limit", "MAC.SoftLimitMaxLen"), ("stroke", "MAC.SoftLimitMaxLen"),
                                       ("jog speed", "MC.JogFastSpeed"), ("jog speed", "MC.JogSlowSpeed")])
def test_preferences_search_finds_operator_words(app, win, query, key):
    from mlaser.ui.settings_dialog import SettingsDialog
    d = SettingsDialog(win)
    try:
        found = [s.key for s, _label in d.elsewhere_matches(query)]
        assert key in found, (query, found)
    finally:
        d.close()
        d.deleteLater()


def test_machine_setup_search_finds_travel(app, win):
    ws = _ws(win)
    sp = ws.settings_panel
    sp.search.setText("travel")
    app.processEvents()
    shown = [sp.tree.topLevelItem(i).child(j).text(2) for i in range(sp.tree.topLevelItemCount())
             for j in range(sp.tree.topLevelItem(i).childCount()) if not sp.tree.topLevelItem(i).child(j).isHidden()]
    assert "MAC.SoftLimitMaxLen" in shown, shown
    sp.search.setText("jog speed")
    app.processEvents()
    shown = [sp.tree.topLevelItem(i).child(j).text(2) for i in range(sp.tree.topLevelItemCount())
             for j in range(sp.tree.topLevelItem(i).childCount()) if not sp.tree.topLevelItem(i).child(j).isHidden()]
    assert {"MC.JogFastSpeed", "MC.JogSlowSpeed"} <= set(shown), shown
