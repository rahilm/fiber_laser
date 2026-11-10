"""The state display, deep review 2026-11:

* UX #3: while the machine homes or makes a go-to move the run-bar pill says HOMING / MOVING
  in the motion style, the banner says the axes move (the laser stays "disarmed"), Start /
  Frame / Dry run are disabled with the reason, and STOP still aborts the homing at once;
* UI #1: an alarm, an e-stop, a fault and a lost link are stated once, in the banner as one
  alarm block (the state with its own icon, the line, the remedy, the laser's state, the one
  Reset alarm), the same in every workspace (compact on Run, whose state block has the remedy);
* UX #9: the simulator is marked wherever the machine state is shown (violet SIMULATOR chip,
  "Simulator" on the run-bar pill), and an address is never called a real card."""
from __future__ import annotations

import colorsys
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _until(app, pred, timeout=8.0) -> bool:
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


@pytest.fixture
def sim_win(app, monkeypatch, tmp_path):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    from wiring import dispose, real_window
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.02)
    w = real_window(ctl)
    w.resize(1280, 800)
    w.show()
    assert ctl.connect_simulator(time_scale=1.0)
    assert _until(app, lambda: ctl.state().connected)
    yield w
    try:
        ctl.sim.clear_faults()
        ctl.sim.set_estop(False)
    except Exception:  # noqa: BLE001
        pass
    dispose(w)


def _st(banner="DISARMED", armed=False, simulator=False):
    return SimpleNamespace(banner=banner, armed=armed, dry_run=False, simulator=simulator,
                           state=SimpleNamespace(value="ready"), fault="")


def _ms(mode="ready"):
    return SimpleNamespace(connected=True, mode=mode, homed=True)


# ------------------------------------------------------------------------ UX #3: motion
def test_homing_and_moves_are_machine_states_not_ready():
    from mlaser.ui.safety_ui import run_state
    from mlaser.ui.state_display import MACHINE_STATES, MOTION_STATES, machine_state
    ctl = SimpleNamespace(breakpoint=None)
    assert run_state(_st(), _ms("origin")) == "homing"
    assert run_state(_st(), _ms("jog")) == "moving"
    assert machine_state(_st(), _ms("origin"), (), ctl, True) == "homing"      # never NOT HOMED
    assert machine_state(_st(), _ms("jog"), (), ctl, False) == "moving"
    assert run_state(_st(), _ms("origin"), [SimpleNamespace(kind="estop")]) == "estop"
    assert {"homing", "moving", "running"} == set(MOTION_STATES) and set(MOTION_STATES) <= set(MACHINE_STATES)


def test_homing_disables_start_frame_dry_run_with_the_reason_stop_stays():
    from mlaser.ui.safety_ui import run_enabled
    for mode, why in (("origin", "Homing in progress"), ("jog", "The machine is moving")):
        got = {k: (on, reason) for k, on, reason in run_enabled(None, _st(), _ms(mode), False, [])}
        for k in ("start", "frame", "dry_run"):
            assert got[k] == (False, why), (mode, k)
        assert got["stop"] == (True, "")
    got = {k: on for k, on, _r in run_enabled(None, _st(), _ms("ready"), False, [])}
    assert got["start"] and got["frame"] and got["dry_run"]


def test_operator_status_says_homing():
    from mlaser.services.operator import KINDS, run_status
    for mode, kind in (("origin", "homing"), ("jog", "moving")):
        s = run_status(SimpleNamespace(connected=True, mode=mode, x=0.0, y=0.0, z=0.0, speed=10.0), _st(), [])
        assert s.kind == kind and kind in KINDS and s.reason == f"run.reason.{kind}"


def test_homing_in_the_window_and_stop_aborts_it(sim_win, app):
    from mlaser.controller.client import AX_W_HOME_FAST, axis_param_addr
    from mlaser.ui import state_display as SD
    w, ctl, su = sim_win, sim_win.ctl, sim_win.safety_ui
    sim = ctl.sim
    with sim.lock:                                      # far from the switches, slowly
        for a in (0, 1):
            sim.state.params[axis_param_addr(a, AX_W_HOME_FAST)] = int(5 * sim.ppm)
            sim.state.axes[a].pos = 300.0 * sim.ppm
    ctl.home("XY")
    assert _until(app, lambda: ctl.state().mode == "origin")
    su.refresh()
    pill, b = su.run_bar.pill, su.banner
    assert pill.text() == SD.machine_label(w, "homing") == "HOMING"
    assert pill.kind == "RUNNING"                       # the motion style of FRAME / DRY RUN
    assert pill.state == "homing"
    # the banner: the laser's fact stays, the motion is said, never the calm look
    assert b.badge.text() == SD.laser_label(w, "disarmed")
    assert "Homing" in b.label.text() and b.motion_chip.isVisibleTo(b) and b.motion_chip.text() == "HOMING"
    from mlaser.ui import style
    assert style.state_colors("RUNNING")[0] in b.styleSheet()        # the dark slate of motion
    assert style.state_colors("DISARMED")[0] not in b.styleSheet()   # not the calm grey
    assert b.home_btn.isHidden()
    for k in ("start", "frame", "dry_run"):
        btn = su.run_bar.buttons[k]
        assert not btn.isEnabled() and btn.property("disabled_reason") == "Homing in progress", k
    assert su.run_bar.buttons["stop"].isEnabled()
    # STOP aborts the homing at once (the halt path is unchanged)
    t0 = time.monotonic()
    su.stop()
    assert _until(app, lambda: not any(ax.busy for ax in sim.state.axes[:2]), 1.0)
    assert time.monotonic() - t0 < 1.0
    assert _until(app, lambda: ctl.state().mode != "origin", 2.0)
    su.refresh()
    assert su.run_bar.pill.text() != "HOMING" and not b.motion_chip.isVisibleTo(b)


# ------------------------------------------------------------------------ UI #1: one alarm block
def _visible_alarm_bars(w):
    from PySide6.QtWidgets import QToolBar
    return [t for t in w.findChildren(QToolBar) if t.isVisible() and t.objectName() == "alarm_banner"]


def test_estop_is_one_alarm_block_in_every_workspace(sim_win, app):
    from mlaser.ui import a11y, state_display as SD
    from mlaser.ui.icons import STATE_ICONS
    w, ctl, su = sim_win, sim_win.ctl, sim_win.safety_ui
    b = su.banner
    n = a11y.mark()
    ctl.sim.set_estop(True)
    assert _until(app, lambda: any(a.kind == "estop" for a in ctl.active_alarms))
    for ws in ("design", "process", "nest", "machine", "report"):
        w.show_workspace(ws)
        su.refresh()
        app.processEvents()
        # the banner turns to E-STOP (it stayed a calm DISARMED before), with its own icon
        assert b.alarm and b.badge.state == "estop" and b.badge.text() == "E-STOP", ws
        assert b.badge.kind == "E-STOP" and STATE_ICONS["estop"] != STATE_ICONS["alarm"]
        assert "8030" in b.label.text() and "Remedy" in b.remedy.text() and b.remedy.isVisibleTo(b)
        assert b.reset.isVisibleTo(b) and b.details.isVisibleTo(b) and not b.btn.isVisibleTo(b)
        # the laser's fact as a chip, the simulator's chip
        assert b.laser_chip.isVisibleTo(b) and b.laser_chip.text() == SD.laser_label(w, "disarmed")
        assert b.sim_chip.isVisibleTo(b)
        assert not _visible_alarm_bars(w) and b.height() > 0
        assert all(c.isHidden() for c in w.shell.topbar.chips.values())
    # Run: its state block carries the remedy and Reset alarm; the banner keeps the state only
    w.show_workspace("run")
    su.refresh()
    app.processEvents()
    scr = w.extension("run").screen
    assert b.alarm and b.badge.text() == "E-STOP" and b.label.text() == ""
    assert not b.reset.isVisibleTo(b) and not b.remedy.isVisibleTo(b)
    assert scr.reset_btn.isVisibleTo(scr)
    # spoken once, with the state's word, the alarm and its remedy
    said = [t for t, assertive in a11y.since(n) if assertive]
    assert len(said) == 1 and said[0].startswith("E-STOP") and "8030" in said[0] and "Reset" in said[0]
    # reset while the e-stop is still pressed: refused, and the block says why
    w.show_workspace("design")
    su.reset_fault()
    assert _until(app, lambda: bool(su.last_reset_refusal), 5.0)
    su.refresh()
    assert b.alarm and "still active" in b.remedy.text()
    ctl.sim.set_estop(False)
    su.reset_fault()
    assert _until(app, lambda: (su.refresh() or True) and not b.alarm, 5.0)
    assert not b.remedy.isVisibleTo(b) and not b.reset.isVisibleTo(b) and not b.laser_chip.isVisibleTo(b)


def test_fault_is_said_once_and_link_lost_has_its_own_icon(sim_win, app):
    w, ctl, su = sim_win, sim_win.ctl, sim_win.safety_ui
    b = su.banner
    ctl.raise_fault("process aborted: card alarm while running")
    su.refresh()
    assert b.alarm and b.badge.state == "fault" and b.badge.kind == "FAULT"
    assert "FAULT (latched)" not in b.label.text() and b.label.text().count("FAULT") == 1
    assert not _visible_alarm_bars(w)
    ctl.reset_fault()
    ctl.wait_commands(5.0)
    assert _until(app, lambda: not ctl.safety().fault)
    su.refresh()
    ctl.raise_fault("link lost: no reply from the card for 1502 ms (idle, deadline 1500 ms)")
    su.refresh()
    assert b.alarm and b.badge.state == "link_lost" and b.badge.text() == "FAULT"
    assert "deadline" not in b.label.text() and "deadline" in b.label.toolTip()
    ctl.reset_fault()


@pytest.mark.parametrize("theme", ("light", "dark"))
def test_alarm_block_colours_follow_the_state(sim_win, app, theme):
    from mlaser.ui import style
    w, ctl, su = sim_win, sim_win.ctl, sim_win.safety_ui
    w.shell.set_theme(theme, persist=False)
    ctl.sim.set_estop(True)
    assert _until(app, lambda: any(a.kind == "estop" for a in ctl.active_alarms))
    su.refresh()
    assert style.state_colors("E-STOP")[0] in su.banner.styleSheet()
    ctl.sim.set_estop(False)
    ctl.raise_fault("test fault")
    su.refresh()
    assert style.state_colors("FAULT")[0] in su.banner.styleSheet()
    ctl.reset_fault()


# ------------------------------------------------------------------------ UX #9: simulator
def _hsv(c: str):
    q = QColor(c)
    return colorsys.rgb_to_hsv(q.redF(), q.greenF(), q.blueF())


@pytest.mark.parametrize("theme", ("light", "dark", "high_contrast"))
def test_simulator_colour_is_its_own(theme):
    from mlaser.ui.shell import theme as T
    from mlaser.ui.shell.contrast import contrast
    t = T.STATE_TOKENS[theme]
    bg, fg = t["SIMULATOR"]
    h, s, _v = _hsv(bg)
    assert contrast(bg, fg) >= 4.5
    assert 0.6 <= h <= 0.8 and s >= 0.18, (theme, bg)     # violet: not red, not amber, not grey
    assert bg not in (t["ALARM"][0], t["ARMED"][0], t["READY"][0], t["E-STOP"][0], t["FAULT"][0])


def test_simulator_marked_in_banner_and_run_bar(sim_win, app):
    from mlaser.ui import style
    ctl, su = sim_win.ctl, sim_win.safety_ui
    b, pill = su.banner, su.run_bar.pill
    su.refresh()
    assert b.sim_chip.isVisibleTo(b) and b.sim_chip.text() == "SIMULATOR" and b.sim_chip.state == "simulator"
    assert b.sim_chip.accessibleName() and b.sim_chip.toolTip()
    assert "simulator" not in b.label.text()             # the grey "· simulator" text is gone
    assert pill.caption.text() == "Simulator" and pill.accessibleName().startswith("Simulator")
    assert style.state_colors("SIMULATOR")[0] in pill.styleSheet()
    assert ctl.arm()
    su.refresh()
    assert b.badge.kind == "ARMED" and b.sim_chip.isVisibleTo(b)


def test_network_address_is_never_called_a_real_card(sim_win, app):
    from mlaser.controller.simulator import CardSimulator
    from mlaser.controller.ui_adapter import UiController
    from mlaser.ui.workspaces.machine import controller_kind_text
    w = sim_win
    assert controller_kind_text(w, w.ctl, None) == "Built-in simulator (not a machine)"
    ext = CardSimulator("127.0.0.1", 0).start()
    c2 = UiController(machine=w.machine, cam_params=w.cam_params)
    try:
        w.set_controller(c2)
        assert c2.connect("127.0.0.1", ext.port)
        text = controller_kind_text(w, c2, None)
        assert "Real motion card" not in text and f"127.0.0.1:{ext.port}" in text
        assert "not identified" in text and "mlaser-cardsim" in text
        mws = w.extension("ext_machine").workspace
        assert mws.r_sim.text() == "Built-in simulator" and mws.r_real.text() == "Network address"
        su = w.safety_ui
        su.refresh()
        # not identified as the simulator: no SIMULATOR marker, and no claim either way
        assert not su.banner.sim_chip.isVisibleTo(su.banner) and su.run_bar.pill.caption.text() == "Machine"
    finally:
        c2.disconnect()
        ext.stop()
