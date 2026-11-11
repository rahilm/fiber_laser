"""Machine › Connection says what is connected (UX review 2026-11-09 finding 9, the rest of it).

* the Controller choice is "Built-in simulator" on a new profile, "Network address" when a
  card address is stored (imported or typed), and while connected it shows what is connected;
* a first connect to an address is logged as that ("Connected to the card at 127.0.0.1:port,
  not identified"), never "Hardware reconnected"."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _until(app, pred, timeout=10.0) -> bool:
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


def _win(app, monkeypatch, tmp_path, record=None):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    from wiring import real_window

    from mlaser.controller.ui_adapter import UiController
    from mlaser.services.config import ConfigService
    cfg = ConfigService(tmp_path / "cfg" / "config.json", load=False)
    if record is not None:
        cfg.set("ip", record)
    w = real_window(UiController(poll_s=0.02), config=cfg)
    w.resize(1400, 900)
    w.show()
    w.shell.show_workspace("machine")
    mws = w.extension("ext_machine").workspace
    mws.show_section("connection")
    app.processEvents()
    return w, mws


def test_new_profile_offers_the_simulator_a_stored_address_the_card(app, monkeypatch, tmp_path):
    from wiring import dispose
    w, mws = _win(app, monkeypatch, tmp_path / "a")
    try:
        assert mws.r_sim.isChecked() and not mws.r_real.isChecked()
    finally:
        dispose(w)
    w, mws = _win(app, monkeypatch, tmp_path / "b", record={"pd445": "192.168.5.20"})
    try:
        assert mws.r_real.isChecked() and mws.host.text() == "192.168.5.20"
    finally:
        dispose(w)


def test_first_connect_is_logged_as_a_connect_and_the_choice_follows(app, monkeypatch, tmp_path):
    from wiring import dispose

    from mlaser.controller.simulator import CardSimulator
    ext = CardSimulator("127.0.0.1", 0).start()
    w, mws = _win(app, monkeypatch, tmp_path)
    try:
        mws.r_real.setChecked(True)
        mws.host.setText("127.0.0.1")
        mws.port.setValue(ext.port)
        log0 = w.log_panel.text.toPlainText()
        mws.connect()
        # the page's own line comes after the controller's "Connected to MCC ..." line: wait for it
        assert _until(app, lambda: w.ctl.state().connected and f"Connected to the card at 127.0.0.1:{ext.port}"
                      in w.log_panel.text.toPlainText()[len(log0):])
        new = w.log_panel.text.toPlainText()[len(log0):]
        assert f"Connected to the card at 127.0.0.1:{ext.port}" in new and "not identified" in new
        assert "Hardware reconnected" not in new
        mws.tick()
        assert mws.r_real.isChecked()
        # the built-in simulator connected from elsewhere (the menu): the page shows it
        w.ctl.disconnect()
        assert w.ctl.wait_commands(30)
        assert w.run("hw.connect_sim") is not False
        assert _until(app, lambda: w.ctl.state().connected and w.ctl.is_simulator())
        mws.r_real.setChecked(True)
        mws._conn_kind = None              # (as after a disconnect: the next tick follows)
        mws.tick()
        assert mws.r_sim.isChecked()
        # a choice made while connected is not undone by the next tick
        mws.r_real.setChecked(True)
        mws.tick()
        assert mws.r_real.isChecked()
    finally:
        try:
            w.ctl.disconnect()
        except Exception:  # noqa: BLE001
            pass
        dispose(w)
        ext.stop()
