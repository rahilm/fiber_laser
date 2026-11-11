"""The jog speed on the jog pad (UX review 2026-11-09 finding 14: the speed was only in the
Service-only Machine › Setup tree).

Every pad shows the speed next to Fast: the slow jog speed, or the fast one while Fast is on,
in the display units.  Changing it changes the same keys Setup edits (``MC.JogSlowSpeed`` /
``MC.JogFastSpeed``), so the next jog uses it; Setup stays their home."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from wiring import dispose, real_window


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog, QMessageBox

    from mlaser.services.config import ConfigService
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    cfg = ConfigService(tmp_path / "cfg" / "config.json", load=False)
    w = real_window(config=cfg, user_mode="operator")
    w.resize(1400, 900)
    w.show()
    w.shell.show_workspace("machine")
    ws = w.extension("ext_machine").workspace
    ws.show_section("motion")
    app.processEvents()
    yield w
    dispose(w)


def _pads(w):
    return w.extension("ext_machine").workspace.jog, w.control.jog


def test_the_pad_shows_the_speed_fast_chooses(win, app):
    pad, dock = _pads(win)
    assert pad.speed_box.isVisibleTo(pad) and pad.speed.isEnabled()
    assert pad.speed.value() == pytest.approx(win.config.get("MC.JogSlowSpeed"))
    assert dock.speed.value() == pytest.approx(win.config.get("MC.JogSlowSpeed"))
    assert "mm/s" in pad.speed.text()
    pad.fast.setChecked(True)
    app.processEvents()
    assert win.config.get("MC.IsFastMode") is True
    assert pad.speed.value() == pytest.approx(win.config.get("MC.JogFastSpeed"))
    assert dock.fast.isChecked() and dock.speed.value() == pytest.approx(win.config.get("MC.JogFastSpeed"))
    assert pad.speed.toolTip() and pad.speed.accessibleName() == "Jog speed"
    assert pad.speed_label.buddy() is pad.speed


def test_changing_the_speed_on_the_pad_changes_the_jog(win, app):
    pad, dock = _pads(win)
    pad.fast.setChecked(False)
    pad.speed.setValue(20.0)
    app.processEvents()
    assert win.config.get("MC.JogSlowSpeed") == pytest.approx(20.0)
    assert win.machine.jog_speed == pytest.approx(20.0)            # the controller's next jog
    assert dock.speed.value() == pytest.approx(20.0)
    dock.fast.setChecked(True)
    dock.speed.setValue(150.0)
    app.processEvents()
    assert win.config.get("MC.JogFastSpeed") == pytest.approx(150.0)
    assert win.machine.jog_fast_speed == pytest.approx(150.0)
    assert win.config.get("MC.JogSlowSpeed") == pytest.approx(20.0)
    # the setting's range: no more than Setup allows
    s = win.config.setting("MC.JogFastSpeed")
    pad.speed.setValue(s.max * 10)
    app.processEvents()
    assert win.config.get("MC.JogFastSpeed") == pytest.approx(s.max)


def test_setup_stays_the_home_of_the_speeds(win, app):
    from mlaser.ui import homes
    pad, dock = _pads(win)
    for p in (pad, dock):
        assert p.speed.property(homes.PROP_ROLE) == homes.SHORTCUT
    h = homes.home_of("cfg:MC.JogFastSpeed")
    assert h is not None and h.workspace == "machine" and h.section == "settings"


def test_imperial_units(win, app):
    pad, _dock = _pads(win)
    win.config.set("units.system", "imperial")
    app.processEvents()
    assert "in/min" in pad.speed.text()
    assert pad.speed.value() == pytest.approx(win.config.get(pad.speed_key()))     # stored in mm/s
