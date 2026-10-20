"""Safety I/O in the application window (review 2, P0-4): a change of a ``safety.*`` value reaches
the controller, the Run screen names every interlock that is not configured, the pre-flight of a
laser job on a real machine warns, the Safety I/O dialog validates and persists, and the
commissioning checklist offers the dialog and runs the wiring test."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # tests/wiring.py


def pump(app, s=0.15):
    t = time.monotonic() + s
    while time.monotonic() < t:
        app.processEvents()
        time.sleep(0.01)


def wait(app, pred, timeout=10.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def sim_win(app, monkeypatch, tmp_path):
    from wiring import real_window

    from mlaser.controller.ui_adapter import UiController
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    ctl = UiController(poll_s=0.02)
    assert ctl.connect_simulator(time_scale=1.0)
    w = real_window(ctl)
    yield w
    try:
        ctl.disconnect()
    except Exception:  # noqa: BLE001
        pass
    w.close()


def test_config_change_reaches_the_controller(app, sim_win):
    w, ctl = sim_win, sim_win.ctl
    assert w.safety_io.last in ("unchanged", "applied")
    w.config.set("safety.door_input", 6)
    assert ctl.interlocks.cfg.door_input == 6
    w.config.set("safety.heartbeat_output", 8)
    assert ctl.heartbeat_status()["output"] == 8
    # armed: the change waits, then applies once disarmed (the window timer retries)
    assert ctl.arm()
    w.config.set("safety.door_input", 7)
    assert w.safety_io.pending and ctl.interlocks.cfg.door_input == 6
    assert "pending" in w.safety_io.notice_text()
    ctl.disarm("test")
    assert wait(app, lambda: ctl.interlocks.cfg.door_input == 7)
    assert not w.safety_io.pending


def test_controller_swap_gets_the_configuration(app, sim_win):
    from mlaser.controller.ui_adapter import UiController
    w = sim_win
    w.config.set("safety.gas_low_input", 9)
    new = UiController(poll_s=0.02)
    try:
        w.set_controller(new)
        assert new.interlocks.cfg.gas_low_input == 9
    finally:
        new.disconnect()


def test_run_screen_names_unconfigured_interlocks(app, sim_win):
    w = sim_win
    ws = w.extension("run")
    w.commands.run("workspace.run")
    pump(app, 0.2)
    ws.screen.refresh()
    note = ws.screen.safety_notice
    assert note.isVisibleTo(ws.screen)
    for name in ("Gas-low", "Door", "Laser-source alarm", "PC-alive heartbeat"):
        assert name in note.text(), note.text()
    w.config.set("safety.door_input", 3)
    ws.screen.refresh()
    assert "Door" not in note.text() and "Gas-low" in note.text()
    for src, n in (("gas_low", 4), ("laser_alarm", 5)):
        w.config.set(f"safety.{src}_input", n)
    w.config.set("safety.heartbeat_output", 8)
    ws.screen.refresh()
    assert not note.isVisibleTo(ws.screen) and note.text() == ""


def test_preflight_warns_on_a_real_machine_only(app, sim_win, monkeypatch):
    from mlaser.machine.preflight import PreflightReport
    from mlaser.ui.safety_io import augment_preflight
    w = sim_win
    rep = PreflightReport("start")
    assert augment_preflight(w, rep, "start", True) is rep          # simulator: no warning
    monkeypatch.setattr(w.ctl, "is_simulator", lambda: False)
    out = augment_preflight(w, rep, "start", True)
    assert {i.code for i in out.issues} == {"interlock_unconfigured_gas", "interlock_unconfigured_door",
                                            "interlock_unconfigured_laser", "heartbeat_unconfigured"}
    assert out.ok and not rep.issues                                # warnings only; a new report
    assert augment_preflight(w, rep, "frame", False).issues == []
    w.config.set("safety.door_not_fitted", True)
    assert "interlock_unconfigured_door" not in {i.code for i in augment_preflight(w, rep, "start", True).issues}
    # the dialog path of SafetyUI.preflight_ok (no dialogs in tests: warnings do not block)
    w._test_no_dialogs = True
    assert w.safety_ui.preflight_ok("start", w.document, True) in (True, False)


def test_dialog_validates_and_persists(app, sim_win):
    from mlaser.services.config import ConfigService
    from mlaser.ui.safety_io import SafetyIODialog
    w = sim_win
    d = SafetyIODialog(w)
    d.inputs["door"].setValue(5)
    d.levels["door"].setCurrentIndex(1)                     # normally closed
    d.not_fitted["laser"].setChecked(True)
    d.hb_output.setValue(w.ctl.output_map["dedust"])         # a process output: refused
    assert d.save() is False and "process output" in d.message.text()
    assert w.config.get("safety.door_input") == 0           # nothing stored
    d.hb_output.setValue(8)
    assert d.save() is True
    again = ConfigService(w.config.path)
    assert again.get("safety.door_input") == 5 and again.get("safety.door_open_active") is False
    assert again.get("safety.laser_not_fitted") is True and again.get("safety.heartbeat_output") == 8
    assert w.ctl.interlocks.cfg.door_input == 5 and w.ctl.heartbeat_status()["output"] == 8
    # a wired input cannot be declared "not fitted"
    d2 = SafetyIODialog(w)
    d2.inputs["laser"].setValue(2)
    assert not d2.not_fitted["laser"].isChecked() and not d2.not_fitted["laser"].isEnabled()
    d.deleteLater()
    d2.deleteLater()


def test_command_is_engineer_and_service(app, sim_win):
    w = sim_win
    spec = w.commands.get("machine.safety_io")
    assert spec is not None and spec.min_mode == "engineer"
    assert w.shell.allows("machine.safety_io")                  # Service (the test window)
    assert w.set_user_mode("engineer") and w.shell.allows("machine.safety_io")
    assert w.set_user_mode("operator") and not w.shell.allows("machine.safety_io")


def test_commissioning_wizard_configures_and_tests(app, sim_win, monkeypatch):
    from mlaser.services.procedures import commissioning as CM
    from mlaser.services.procedures import engine as PE
    from mlaser.ui.wizards.procedures import CommissioningWizard
    w = sim_win
    d = CommissioningWizard(w, w.extension("ext_machine"))
    d.dry.setChecked(True)                                    # private simulator rig
    p = d.proc

    def drive(until):
        for _ in range(40):
            pump(app, 0.02)
            wait(app, lambda: not p.busy())
            s = p.step
            if p.state in PE.FINAL or s is None or s.id == until:
                return
            if s.kind == "action" and s.id not in p.done:
                d.run()
                wait(app, lambda: not p.busy() and (s.id in p.done or p.state in PE.FINAL))
                continue
            if s.kind == "question":
                d.answer(True)
            if p.can_next():
                d.next()

    drive("interlocks")
    d.run()                                                           # the configuration check
    assert wait(app, lambda: not p.busy() and "interlocks" in p.done)
    assert p.items["interlocks"].status == CM.WARN                    # nothing configured: flagged
    # "Configure safety I/O..." from the checklist: the dialog stores a door switch on input 2
    from mlaser.ui import safety_io as SIO

    def fake_exec(dlg, *a):
        if isinstance(dlg, SIO.SafetyIODialog):
            dlg.inputs["door"].setValue(2)
            dlg.not_fitted["gas"].setChecked(True)
            dlg.not_fitted["laser"].setChecked(True)
            assert dlg.save()
            return 1
        return 0
    monkeypatch.setattr(QDialog, "exec", fake_exec)
    d.configure_safety_io()
    it = p.items["interlocks"]
    assert it.status == CM.PENDING and it.values["wired"] == "door / guard"
    assert d.rig.interlocks.cfg.door_input == 2
    p.interlock_window_s = 3.0
    d.next()
    assert p.step.id == "interlock_test"
    d.run()                                       # the dry run toggles the simulator's input
    assert wait(app, lambda: not p.busy() and "interlock_test" in p.done, 15)
    assert p.items["interlocks"].status == CM.PASS, (p.items["interlocks"].detail, p.interlock_results)
    d.abort()
    d.done(0)
