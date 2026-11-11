"""The interactive commissioning checklist (GitHub issue #6): the live status of every check
follows the card while the operator works on the machine, each sensor has a "test it" prompt
that turns to pass when the input changes and the guard refuses, every failing check offers the
fix that leads to the one place that fixes it, and nothing in the live view moves or fires.

Offscreen, against the card simulator (``mlaser.controller.simulator``)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from mlaser.controller.interlocks import InterlockConfig, InterlockMonitor
from mlaser.services.procedures import commissioning_live as CL

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # tests/wiring.py


# ---------------------------------------------------------------------------- Qt-free
def test_sensor_test_phases():
    t = CL.SensorTest()
    assert t.observe(False) == "ready"
    assert t.observe(True, ()) == "ready"                 # active but nothing refused: not a pass
    assert t.observe(True, ("cut",)) == "triggered" and t.seen
    assert t.observe(True, ("cut",)) == "triggered"
    assert t.observe(False) == "tested"
    assert t.observe(True, ("cut",)) == "tested"          # stays tested
    held = CL.SensorTest()
    assert held.observe(True, ("cut",)) == "held"         # active before the test: release first
    assert held.observe(False) == "ready"


def test_guard_refusals_are_the_guards_own_classification():
    mon = InterlockMonitor(InterlockConfig(door_input=2, laser_alarm_input=3))
    mon.active = {"door": "door open"}
    door = CL.guard_refusals(mon, "door", armed=False)
    assert door == tuple(a for a, job, emits in CL.GUARD_ACTIONS
                         if InterlockMonitor.gated("door", job_active=job, emitting=emits, armed=False)) + ("arm",)
    assert "cut" in door and "frame" not in door                       # a door does not stop a frame
    assert set(CL.guard_refusals(mon, "laser", armed=False)) == {"cut", "frame", "arm"}
    mon.active = {}
    assert "arm" not in CL.guard_refusals(mon, "door", armed=False)


def test_compare_and_adopt_scaling_values():
    from mlaser.controller.client import AxisScaling, CardScaling
    from mlaser.nc.machine import Machine
    sc = CardScaling(period_us=1000, ppm=200, axes=[AxisScaling(ppr=10000, lead_mm=50.0) for _ in range(8)])
    bad, vals = CL.compare_scaling(sc, Machine())
    assert bad == ["X", "Y"] and vals["card_x"] == 200.0 and vals["par_x"] == 100.0
    assert CL.card_scaling_values(sc) == {"MAC.WritePluse": 10000, "MAC.SpeedRatio": 50.0, "MAC_1.WritePluse": 10000,
                                          "MAC_1.SpeedRatio": 50.0, "AX.InterpolationCycle": 1000}
    assert CL.compare_scaling(sc, Machine(pulses_per_mm_x=200.0, pulses_per_mm_y=200.0))[0] == []


# ---------------------------------------------------------------------------- the window
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
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _window(app, monkeypatch, tmp_path, **sim_kw):
    from PySide6.QtWidgets import QDialog, QMessageBox
    from wiring import real_window

    from mlaser.controller.ui_adapter import UiController
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    ctl = UiController(poll_s=0.02)
    assert ctl.connect_simulator(time_scale=1.0, **sim_kw)
    return real_window(ctl)


@pytest.fixture
def sim_win(app, monkeypatch, tmp_path):
    w = _window(app, monkeypatch, tmp_path)
    yield w
    w.ctl.disconnect()
    w.close()


@pytest.fixture
def mismatch_win(app, monkeypatch, tmp_path):
    """The card reports 200 p/mm (10000 pulses/rev, 50 mm lead); the PC's parameters say 100."""
    w = _window(app, monkeypatch, tmp_path, ppm=200.0, step_ppm=200.0)
    yield w
    w.ctl.disconnect()
    w.close()


def _wizard(w):
    from mlaser.ui.wizards.procedures import CommissioningWizard
    return CommissioningWizard(w, w.extension("ext_machine"))


def _row(d, rid):
    return d.update_live()[rid]


def test_live_interlock_follows_the_input_and_the_guard(app, sim_win):
    w, ctl = sim_win, sim_win.ctl
    w.config.set("safety.door_input", 3)                   # reaches the controller (SafetyIOBinding)
    assert ctl.interlocks.cfg.door_input == 3
    d = _wizard(w)
    try:
        r = _row(d, "ilk.door")
        assert r.status == CL.PENDING and r.prompt == "wizard.comm.live.door_prompt"
        row = d.panel.rows["ilk.door"]
        assert row.chip.icon_name() == "circle-dashed" and row.status_text() == "to do" and row.prompt.isVisible() is not None
        assert row.prompt.text() == "Open the door now."
        # the operator opens the door: the card input changes, the poll publishes it
        ctl.sim.set_input(3, True)
        assert wait(app, lambda: _row(d, "ilk.door").status == CL.PASS)
        r = _row(d, "ilk.door")
        assert "cut" in r.values["refused"] and "arm" in r.values["refused"]
        assert row.chip.icon_name() == "circle-check" and row.status_text() == "pass" and "door open seen" in row.detail.text()
        assert "cutting jobs" in row.detail.text() and row.prompt.text() == "Close the door again."
        # the guard really refuses while the door is open (arming is refused by the interlock)
        assert not ctl.arm(reason="test") and not ctl.state().armed
        ctl.sim.set_input(3, False)
        assert wait(app, lambda: d.live.results().get("door") == "tested")
        r = _row(d, "ilk.door")
        assert r.status == CL.PASS and r.detail == "wizard.comm.live.ilk_tested" and not r.prompt
        assert d.proc.live_tests["door"] == "tested"
        # the e-stop: pressed is seen (card alarm), the fix is Clear Alarm
        assert _row(d, "estop").status == CL.PENDING
        ctl.sim.set_estop(True)
        assert wait(app, lambda: _row(d, "estop").status == CL.PASS)
        assert "clear_alarms" in _row(d, "estop").fixes
        ctl.sim.set_estop(False)
        d.panel.rows["estop"].buttons["clear_alarms"].click()        # Clear Alarm through card.clear_alarms
        assert wait(app, lambda: d.live.results().get("estop") == "tested")
        # moving the door switch to another input means testing it again
        w.config.set("safety.door_input", 4)
        assert wait(app, lambda: _row(d, "ilk.door").status == CL.PENDING)
    finally:
        d.done(0)


def test_live_tests_complete_the_interlock_item(app, sim_win):
    from mlaser.services.procedures import commissioning as CM
    from mlaser.services.procedures import engine as PE
    w, ctl = sim_win, sim_win.ctl
    w.config.set("safety.door_input", 3)
    for src in ("gas", "laser"):
        w.config.set(f"safety.{src}_not_fitted", True)
    d = _wizard(w)
    p = d.proc
    try:
        p.netcheck_fn = _netcheck_ok
        while p.step.id != "interlock_test":
            if p.can_next():
                d.next()
            assert wait(app, lambda: not p.busy() and p.state == PE.WAITING)
        assert p.items["interlocks"].status == CM.PENDING            # wired, not tested yet
        ctl.sim.set_input(3, True)
        assert wait(app, lambda: d.live.results().get("door") == "triggered")
        ctl.sim.set_input(3, False)
        assert wait(app, lambda: p.items["interlocks"].status == CM.PASS)
        assert p.items["interlocks"].detail == "wizard.comm.ilk_tested" and p.interlock_results == {"door": "ok"}
    finally:
        d.done(0)


def _netcheck_ok(host="127.0.0.1", port=502, probe=False):
    from mlaser import netcheck
    return netcheck.NetCheckResult(host, port, "test", [netcheck.Check("route", netcheck.OK, "ok")],
                                   local_ip="127.0.0.1")


def test_unconfigured_interlock_opens_safety_io_at_its_input(app, sim_win, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from mlaser.ui import safety_io as SIO
    w = sim_win
    d = _wizard(w)
    try:
        r = _row(d, "ilk.gas")
        assert r.status == CL.WARN and r.fixes == ("safety_io:gas",)
        seen = {}

        def fake_exec(dlg, *a):
            if isinstance(dlg, SIO.SafetyIODialog):
                dlg.inputs["gas"].setValue(5)
                assert dlg.save()
                return 1
            return 0
        monkeypatch.setattr(QDialog, "exec", fake_exec)
        monkeypatch.setattr(SIO.SafetyIODialog, "focus_source",
                            lambda self, src: seen.setdefault("source", src) and None)
        d.panel.rows["ilk.gas"].buttons["safety_io:gas"].click()
        assert seen["source"] == "gas" and w.config.get("safety.gas_low_input") == 5
        assert wait(app, lambda: _row(d, "ilk.gas").status == CL.PENDING)     # now: test it
        assert _row(d, "ilk.gas").prompt == "wizard.comm.live.gas_prompt"
    finally:
        d.done(0)


def test_safety_io_dialog_focuses_the_input(app, sim_win):
    from mlaser.ui.safety_io import SafetyIODialog
    dlg = SafetyIODialog(sim_win)
    assert dlg.focus_source("door") is dlg.inputs["door"]
    assert dlg.focus_source("heartbeat") is dlg.hb_output
    dlg.deleteLater()


def test_scaling_mismatch_adopt_the_cards_values(app, mismatch_win):
    w, ctl = mismatch_win, mismatch_win.ctl
    d = _wizard(w)
    try:
        r = _row(d, "scaling")
        assert r.status == CL.FAIL and r.fixes == ("import_params", "adopt_scaling")
        assert r.values["which"] == "X, Y" and r.values["card_x"] == 200.0 and r.values["par_x"] == 100.0
        row = d.panel.rows["scaling"]
        assert row.chip.icon_name() == "circle-x" and row.status_text() == "fail" and "Card and parameters differ" in row.detail.text()
        assert row.buttons["adopt_scaling"].text() == "Adopt the card's values"
        assert row.buttons["import_params"].isEnabled() and row.buttons["adopt_scaling"].isEnabled()
        row.buttons["adopt_scaling"].click()
        cfg = w.config
        assert cfg.get("MAC.WritePluse") == 10000 and cfg.get("MAC.SpeedRatio") == 50.0
        assert cfg.get("MAC_1.SpeedRatio") == 50.0 and cfg.machine().pulses_per_mm_x == 200.0
        assert _row(d, "scaling").status == CL.PASS and not d.panel.rows["scaling"].buttons
        assert d.proc.items["scaling"].status == "pass"
        assert ctl.state().connected                          # nothing was written to the card
    finally:
        d.done(0)


def test_scaling_mismatch_import_parameters(app, mismatch_win, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog

    from mlaser.services.config import ConfigService
    w = mismatch_win
    # an original install's dat folder that describes this machine (10000 pulses/rev, 50 mm lead)
    src = ConfigService(path=tmp_path / "other.json", load=False)
    for k, v in (("MAC.WritePluse", 10000), ("MAC.SpeedRatio", 50.0), ("MAC_1.WritePluse", 10000),
                 ("MAC_1.SpeedRatio", 50.0)):
        src.set(k, v)
    dat = tmp_path / "dat"
    src.export_dat(dat)
    asked = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: (asked.append(a), str(dat))[1]))
    d = _wizard(w)
    try:
        assert _row(d, "scaling").status == CL.FAIL
        d.panel.rows["scaling"].buttons["import_params"].click()   # the existing shell.import_params
        assert asked, "the import asks for the folder (File > Import parameters)"
        # the reviewing import (UX review 2026-11-09 finding 11): a preview, applied on confirm
        imp = w.shell._import_dialog
        assert imp.plan is not None and "MAC.SpeedRatio" in {c.key for c in imp.plan.changes}
        assert "MAC.SpeedRatio" in {c.key for c in imp.plan.safety_changes()}     # marked: scaling
        assert w.config.machine().pulses_per_mm_x == pytest.approx(100.0)       # nothing applied yet
        assert imp.apply(confirm=False) is not None
        imp.close()
        assert w.config.machine().pulses_per_mm_x == pytest.approx(200.0)
        assert _row(d, "scaling").status == CL.PASS
        assert d.proc.items["scaling"].status == "pass"
    finally:
        d.done(0)


def test_dry_run_never_adopts_and_simulates_sensors(app, sim_win):
    w = sim_win
    w.config.set("safety.door_input", 2)
    d = _wizard(w)
    try:
        d.dry.setChecked(True)
        assert d.rig is not None and d.proc.dry_run
        ok, why = d.fix_enabled("adopt_scaling")
        assert not ok and why
        r = _row(d, "ilk.door")
        assert "simulate:door" in r.fixes and r.status == CL.PENDING
        d.panel.rows["ilk.door"].buttons["simulate:door"].click()
        assert wait(app, lambda: _row(d, "ilk.door").status == CL.PASS)
        d.panel.rows["ilk.door"].buttons["simulate:door"].click()
        assert wait(app, lambda: d.live.results().get("door") == "tested")
        assert not w.ctl.interlocks.active                          # the real machine was not touched
    finally:
        d.done(0)


MOTION_OR_EMISSION = ("home", "startup_homing", "jog_start", "jog_step", "move_to", "move_by", "return_zero",
                      "set_origin", "go_origin_point", "set_output", "set_gas_type", "set_laser_power",
                      "laser_press", "laser_test", "start", "start_job", "dry_run", "frame", "resume",
                      "step_backward", "step_forward", "resume_saved", "arm")


def test_live_view_and_checks_send_no_motion_or_emission(app, sim_win, monkeypatch):
    """The live rows, the automatic checks and every fix button that is not a motion step:
    no motion, output, arming or emission call reaches the controller, the head stays put and
    the beam stays off."""
    from mlaser.services.procedures import engine as PE
    w, ctl = sim_win, sim_win.ctl
    w.config.set("safety.door_input", 3)
    calls: list = []
    for name in MOTION_OR_EMISSION:
        if callable(getattr(ctl, name, None)):
            monkeypatch.setattr(ctl, name, lambda *a, _n=name, **k: calls.append(_n))
    x0, y0 = ctl.sim.position(0), ctl.sim.position(1)
    d = _wizard(w)
    p = d.proc
    try:
        p.netcheck_fn = _netcheck_ok
        # the automatic steps (network, scaling, soft limits, interlocks) up to the first motion step
        while p.step.id != "homing":
            if p.step.kind == "action" and p.step.id not in p.done and not p.step.auto:
                break                                       # the wiring test waits for its Run
            if p.can_next():
                d.next()
            assert wait(app, lambda: not p.busy() and p.state == PE.WAITING)
        # the operator works the sensors while the live view runs on its timer
        for on in (True, False, True, False):
            ctl.sim.set_input(3, on)
            ctl.sim.set_estop(on)
            assert wait(app, lambda: d.live.rows and True)
            time.sleep(0.1)
            app.processEvents()
        # every fix button offered now (the Safety I/O dialog is cancelled, Clear Alarm clears)
        clicked = []
        for rid, row in list(d.panel.rows.items()):
            for fx, b in list(row.buttons.items()):
                if b.isEnabled():
                    clicked.append(fx)
                    b.click()
        assert {"safety_io:gas", "safety_io:laser", "clear_alarms"} <= set(clicked), clicked
        assert wait(app, lambda: not p.busy())
        assert calls == [], calls
        assert ctl.sim.position(0) == x0 and ctl.sim.position(1) == y0
        assert not ctl.sim.beam_on and not ctl.state().armed
        assert "homing" not in p.done
    finally:
        d.done(0)
