"""UX audit safety findings (C1-C4, W3, W5, W6, W12, W17, O9, O13): the real window on the card
simulator (``UiController.connect_simulator``), offscreen."""
from __future__ import annotations

import logging
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from safety_helpers import rect_doc  # noqa: E402

pytestmark = pytest.mark.xdist_group("ux_audit_safety")


def _pump(app, s=0.1):
    t = time.monotonic() + s
    while time.monotonic() < t:
        app.processEvents()
        time.sleep(0.01)


def _until(app, pred, timeout=8.0) -> bool:
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return pred()


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialogs(monkeypatch):
    """Every message box answers OK (it is recorded: icon, text, buttons, default), every other
    dialog is closed (recorded)."""
    seen: list = []

    def box_exec(self, *a):
        default = self.defaultButton()
        ok = self.button(QMessageBox.Ok)
        seen.append(dict(kind="box", text=self.text(), title=self.windowTitle(), icon=self.icon(),
                         info=self.informativeText(),
                         ok_text=ok.text() if ok is not None else "",
                         default_is_cancel=default is not None and default is self.button(QMessageBox.Cancel)))
        return QMessageBox.Ok

    def dlg_exec(self, *a):
        seen.append(dict(kind=type(self).__name__, title=self.windowTitle(), dialog=self))
        return 0
    monkeypatch.setattr(QMessageBox, "exec", box_exec)
    monkeypatch.setattr(QDialog, "exec", dlg_exec)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(
            lambda *a, **k: seen.append(dict(kind="static", text=str(a[2]) if len(a) > 2 else "")) or QMessageBox.Ok))
    return seen


@pytest.fixture
def sim_win(app, monkeypatch, tmp_path):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import dispose, real_window
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.02)
    w = real_window(ctl)
    w.resize(1280, 720)
    w.show()
    assert ctl.connect_simulator(time_scale=1.0)
    _until(app, lambda: ctl.state().connected)
    yield w
    try:
        ctl.sim.clear_faults()
    except Exception:  # noqa: BLE001
        pass
    dispose(w)


def _estop(app, ctl, on: bool) -> None:
    """Press (wait for the alarm) or release the e-stop (the card keeps the alarm until a reset)."""
    ctl.sim.set_estop(on)
    if on:
        assert _until(app, lambda: any(a.kind == "estop" for a in ctl.active_alarms))


def _armed(ctl) -> bool:
    return bool(ctl.safety().armed)


# ----------------------------------------------------------------------------------- C1 / O13
def test_start_while_disarmed_with_estop_never_arms(sim_win, app, dialogs):
    """C1: the pre-flight runs before any arming question: with the e-stop pressed the start is
    refused and the machine is never armed (before: ARMED after the refused start)."""
    w, ctl = sim_win, sim_win.ctl
    w.set_document(rect_doc(w=50, h=40))
    _estop(app, ctl, True)
    w._test_no_dialogs = False
    w.safety_ui.start()
    assert w.safety_ui.wait_preflight(30)        # the pre-flight runs off the GUI thread
    _pump(app, 0.3)
    assert not _armed(ctl)
    assert not [d for d in dialogs if d["kind"] == "box"], "no arming question when the check refuses"
    pf = [d for d in dialogs if d["kind"] == "PreflightDialog"]
    assert pf and pf[0]["title"] == "Before Start" and "alarm" in pf[0]["dialog"].report.codes()


def test_start_while_disarmed_asks_once_with_the_safety_text(sim_win, app, dialogs):
    """C1/O13: one question, the arming safety text, "Arm and start", warning icon, Cancel the
    default; then the job starts armed."""
    w, ctl = sim_win, sim_win.ctl
    w.set_document(rect_doc(w=20, h=20))
    w._test_no_dialogs = False
    w.safety_ui.start()
    assert _until(app, lambda: ctl.job_active() or ctl.state().mode == "process", 20)
    boxes = [d for d in dialogs if d["kind"] == "box"]
    assert len(boxes) == 1
    b = boxes[0]
    assert b["ok_text"] == "Arm and start" and b["default_is_cancel"] and b["icon"] == QMessageBox.Warning
    assert "enclosure is closed" in b["info"]
    assert _armed(ctl)
    ctl.stop(wait=True)


def test_refused_start_after_arming_disarms_again(sim_win, app, dialogs, monkeypatch):
    """C1: when the start is refused after the arming (here: the controller refuses the planned
    job), the machine is disarmed again and the operator told."""
    w, ctl = sim_win, sim_win.ctl
    w.set_document(rect_doc(w=20, h=20))
    monkeypatch.setattr(ctl, "start_job", lambda *a, **k: False)
    w._test_no_dialogs = False
    w.safety_ui.start()
    assert w.safety_ui.wait_preflight(30)        # the pre-flight runs off the GUI thread
    assert _until(app, lambda: [d for d in dialogs if d["kind"] == "box"], 20)   # armed by this Start
    assert _until(app, lambda: w.safety_ui._arm_token is None, 20)
    _pump(app, 0.2)
    assert not _armed(ctl)
    assert "disarmed again" in w.statusBar().currentMessage()


def test_arm_question_has_the_verb_and_cancel_default(sim_win, app, dialogs):
    w = sim_win
    w._test_no_dialogs = False
    assert w.safety_ui.arm()
    b, = [d for d in dialogs if d["kind"] == "box"]
    assert b["ok_text"] == "Arm laser" and b["default_is_cancel"] and b["icon"] == QMessageBox.Warning
    assert "enclosure is closed" in b["info"]


def test_ask_defaults_to_cancel(app, dialogs):
    from mlaser.ui.safety_ui import ask
    assert ask(None, "x", "Question?")
    assert dialogs[-1]["default_is_cancel"]


# ----------------------------------------------------------------------------------- C2 / W5
def test_reset_with_the_cause_present_is_refused_and_never_ready(sim_win, app, dialogs):
    w, ctl, su = sim_win, sim_win.ctl, sim_win.safety_ui
    _estop(app, ctl, True)
    su.refresh()
    assert su.run_bar.pill.text().strip() == "E-STOP"
    su.reset_fault()
    assert _until(app, lambda: bool(su.last_reset_refusal), 10)
    assert "Reset refused" in su.last_reset_refusal and "still active" in su.last_reset_refusal
    assert "Emergency stop" in su.last_reset_refusal
    for _ in range(20):                              # the pill never says READY meanwhile
        _pump(app, 0.05)
        su.refresh()
        assert su.run_bar.pill.text().strip() != "READY"
        assert [a.code for a in ctl.active_alarms] == [8030]
        assert ctl.state().alarms                   # the state's list follows the card
    _estop(app, ctl, False)
    su.reset_fault()
    assert _until(app, lambda: not ctl.active_alarms and not ctl.state().alarms and not su.last_reset_refusal, 10)
    su.refresh()
    assert su.run_bar.pill.text().strip() == "READY" and not su.last_reset_refusal


def test_clear_alarms_reports_whether_anything_is_left(sim_win, app):
    ctl = sim_win.ctl
    ctl.sim.set_estop(True)
    assert _until(app, lambda: ctl.active_alarms)
    ctl.wait_commands(5.0)
    t = ctl.clear_alarms()
    res = t.result if hasattr(t, "result") and hasattr(t, "dropped") else t
    if hasattr(t, "then"):
        ctl.wait_commands(5.0)
        res = t.result
    assert res is False and ctl.active_alarms
    ctl.sim.set_estop(False)
    ctl.wait_commands(5.0)
    t = ctl.clear_alarms()
    ctl.wait_commands(5.0)
    assert (t.result if hasattr(t, "then") else t) is True and not ctl.active_alarms


def test_one_reset_alarm_name_and_one_banner(sim_win, app):
    w, su = sim_win, sim_win.safety_ui
    assert w.control.buttons["mf21"].text() == "Reset alarm"          # the dock
    assert su.banner.reset.text() == "Reset alarm"
    w.ctl.raise_fault("link lost: no reply from the card for 1502 ms (idle, deadline 1500 ms)")
    _estop(app, w.ctl, True)
    su.refresh()
    # one alarm block (UI review 2026-11 #1): the fault and the alarm in the banner, one Reset
    assert su.banner.kind == "FAULT" and su.banner.btn.isHidden() and su.banner.alarm
    assert not su.banner.reset.isHidden() and "FAULT" in su.banner.title
    assert "deadline" not in su.banner.label.text() and "deadline" not in su.banner.title


# ----------------------------------------------------------------------------------- C3
def test_estop_while_idle_and_armed_disarms_and_rearm_is_required(sim_win, app):
    w, ctl, su = sim_win, sim_win.ctl, sim_win.safety_ui
    assert ctl.arm() and _armed(ctl)
    _estop(app, ctl, True)
    assert _until(app, lambda: not _armed(ctl), 2.0), "an e-stop disarms an idle armed machine"
    assert not ctl.arm(), "no arming while the e-stop alarm is active"
    su.refresh()
    assert su.run_bar.pill.text().strip() == "E-STOP"
    _estop(app, ctl, False)
    su.reset_fault()
    assert _until(app, lambda: not ctl.active_alarms, 10)
    assert not _armed(ctl), "the reset never re-arms"
    assert ctl.arm()


def test_servo_alarm_while_idle_disarms(sim_win, app):
    ctl = sim_win.ctl
    assert ctl.arm()
    ctl.sim.set_servo_alarm(0, True)
    assert _until(app, lambda: not _armed(ctl), 2.0)
    ctl.sim.set_servo_alarm(0, False)


# ----------------------------------------------------------------------------------- C4
def test_start_frame_dry_run_disabled_with_reason_during_an_alarm(sim_win, app):
    w, ctl, su = sim_win, sim_win.ctl, sim_win.safety_ui
    w.set_document(rect_doc(w=20, h=20))
    su.refresh()
    assert su.run_bar.buttons["start"].isEnabled()
    _estop(app, ctl, True)
    su.refresh()
    for k in ("start", "frame", "dry_run", "arm"):
        b = su.run_bar.buttons[k]
        assert not b.isEnabled(), k
        assert b.property("disabled_reason").startswith("Reset the alarm first: "), k
    assert su.run_bar.buttons["stop"].isEnabled()               # STOP always
    _estop(app, ctl, False)


def test_run_enabled_rules_with_an_alarm():
    from types import SimpleNamespace as NS

    from mlaser.controller.alarms import decode
    from mlaser.ui.safety_ui import run_enabled, run_state
    alarms = decode(1 << 30, 0)
    st = NS(banner="DISARMED", armed=False, dry_run=False)
    ms = NS(connected=True, mode="ready")
    rules = {k: (on, why) for k, on, why in run_enabled(None, st, ms, False, alarms)}
    assert not rules["start"][0] and not rules["frame"][0] and not rules["dry_run"][0]
    assert rules["start"][1] == "Reset the alarm first: Emergency stop alarm"
    assert rules["stop"] == (True, "")
    assert run_state(st, ms, alarms) == "estop"
    assert run_state(st, ms, decode(0, 1 << 5)) == "alarm"
    assert run_state(st, ms, []) == "ready"
    assert run_state(st, ms, [], need_home=True) == "not_homed"


# ----------------------------------------------------------------------------------- W17
def test_stop_while_held_keeps_the_resume_point(sim_win, app):
    w, ctl, su = sim_win, sim_win.ctl, sim_win.safety_ui
    w.set_document(rect_doc(w=400, h=300))
    assert ctl.arm()
    su.start()
    assert _until(app, lambda: ctl.state().mode == "process", 20)
    _pump(app, 0.4)
    su.hold()
    assert _until(app, lambda: ctl.breakpoint is not None, 10)
    bp = ctl.breakpoint
    t0 = time.monotonic()
    su.stop()                                    # immediate: no question
    assert time.monotonic() - t0 < 0.5
    assert _until(app, lambda: ctl.breakpoint is None and ctl.kept_breakpoint is not None, 10)
    assert ctl.kept_breakpoint is bp
    su.refresh()
    assert not su.kept_bar.isHidden() and "resume point kept" in su.kept.label.text()
    su.resume_kept()
    assert ctl.breakpoint is bp and ctl.kept_breakpoint is None
    su.refresh()
    assert su.kept_bar.isHidden()
    su.stop()
    assert _until(app, lambda: ctl.kept_breakpoint is not None, 10)
    su.discard_kept()
    assert ctl.kept_breakpoint is None and ctl.breakpoint is None


# ----------------------------------------------------------------------------------- W3
def test_readiness_and_not_homed_state(sim_win, app, monkeypatch):
    from mlaser.services.readiness import ITEMS, blockers
    w, ctl, su = sim_win, sim_win.ctl, sim_win.safety_ui
    from mlaser.model import Document
    from safety_helpers import rect_doc as rd
    doc = rd(w=20, h=20, define=False)
    w.set_document(Document(parts=doc.parts, layers={}))
    items = {i.id: i for i in su.readiness()}
    assert list(items) == [i for i in ITEMS if i in items]
    assert items["connected"].ok and items["alarms"].ok
    assert not items["technology"].ok and items["technology"].action == "workspace.process"
    assert not items["armed"].ok and not items["armed"].blocking
    monkeypatch.setattr(ctl, "_require_homed", True)
    monkeypatch.setattr(ctl, "homed_xy", lambda: False)
    items = {i.id: i for i in su.readiness()}
    assert not items["homed"].ok and items["homed"].action == "machine.home"
    assert {i.id for i in blockers(list(items.values()))} >= {"homed", "technology"}
    su.refresh()
    assert su.run_bar.pill.text().strip() == "NOT HOMED"
    assert not su.banner.home_btn.isHidden()
    _estop(app, ctl, True)
    items = {i.id: i for i in su.readiness()}
    assert not items["alarms"].ok and items["alarms"].reason.startswith("Reset the alarm first")
    _estop(app, ctl, False)


# ----------------------------------------------------------------------------------- W6
def test_preflight_dialog_groups_titles_and_fixes(app):
    from mlaser.machine.preflight import ERROR, WARNING, PreflightReport, demote_cutting_only
    from mlaser.ui.safety_ui import PreflightDialog
    rep = PreflightReport("start")
    for li in (1, 2, 3, 4):
        rep.add("layer_undefined", ERROR, f"layer {li} has no cutting parameters", (li,))
    rep.add("open_contour", WARNING, "1 open contour", (0, 0))
    d = PreflightDialog(rep)
    assert d.windowTitle() == "Before Start"
    rows = [d.list.item(i).text() for i in range(d.list.count())]
    assert rows[0] == "Error: 4 layers have no technology (layers 1, 2, 3, 4)"
    assert rows[1].startswith("Warning: ")
    assert d.fixes["layer_undefined"].text() == "Choose technology…"
    d.deleteLater()
    dry = PreflightReport("dry_run")
    dry.add("layer_undefined", WARNING, "layer 1 has no cutting parameters", (1,))
    dry.add("lead_collision", WARNING, "lead-in of part 1 touches part 2")
    shown = demote_cutting_only(dry)
    assert not shown.warnings and len(shown.infos) == 2 and shown.ok
    dry.add("out_of_travel", WARNING, "x")
    assert demote_cutting_only(dry).warnings                    # not cutting-only: stays
    err = PreflightReport("frame")
    err.add("layer_undefined", ERROR, "e")
    assert demote_cutting_only(err).errors                       # errors are never demoted
    go = PreflightReport("dry_run")
    go.add("out_of_travel", WARNING, "x")
    d = PreflightDialog(go)
    assert d.windowTitle() == "Before Dry run" and d.go.text() == "Dry run anyway" and not d.go.isDefault()
    d.deleteLater()


# ----------------------------------------------------------------------------------- W12
def test_plain_fault_headlines_and_raw_detail_in_the_log_file(sim_win, app):
    from mlaser.machine.fault_text import headline
    assert headline("link lost: no reply from the card for 1502 ms (idle, deadline 1500 ms)") == \
        "Lost connection to the motion card"
    assert headline("interlock door: door open") == "A door or guard was opened"
    assert headline("process aborted: underrun: only 80 ms of motion buffered at wire 5960") == \
        "The PC could not send the job fast enough"
    ctl = sim_win.ctl
    plain, raw = [], []
    ctl.add_log_listener(plain.append)
    ctl.add_log_listener(raw.append, detail=True)
    ctl.raise_fault("link lost: no reply from the card for 1502 ms (idle, deadline 1500 ms)")
    assert plain[-1] == "Lost connection to the motion card"
    assert "deadline 1500 ms" in raw[-1] and raw[-1].startswith("Lost connection")
    ctl.sim.set_estop(True)
    _until(app, lambda: ctl.active_alarms)
    ctl.wait_commands(5.0)
    ctl.clear_alarms()
    ctl.wait_commands(5.0)
    assert not any("mf21" in m or "[9999" in m for m in plain)
    assert any("[9999, 5, 0, 0]" in m for m in raw)


def test_hold_log_line_has_no_engineering_words(sim_win, app):
    w, ctl, su = sim_win, sim_win.ctl, sim_win.safety_ui
    plain = []
    ctl.add_log_listener(plain.append)
    w.set_document(rect_doc(w=400, h=300))
    assert ctl.arm()
    su.start()
    assert _until(app, lambda: ctl.state().mode == "process", 20)
    _pump(app, 0.3)
    su.hold()
    assert _until(app, lambda: ctl.breakpoint is not None, 10)
    assert _until(app, lambda: any(m.startswith("Held: resume point") for m in plain), 5)
    held = [m for m in plain if m.startswith("Held: resume point")]
    assert held and not any("manu cmd" in m or "wire" in m for m in held)
    ctl.stop(wait=True)


# ----------------------------------------------------------------------------------- O9
def test_connection_page_texts(app):
    from types import SimpleNamespace as NS

    from mlaser.controller.ui_adapter import UiController
    from mlaser.machine.demo import DemoController
    from mlaser.machine.safety import SafetyState
    from mlaser.ui.workspaces.machine import controller_kind_text, safety_state_text
    off = NS(connected=False)
    assert controller_kind_text(None, UiController(), off) == "Not connected"
    assert controller_kind_text(None, DemoController(), off) == "Demo (no card)"
    assert safety_state_text(None, NS(state=SafetyState.OFFLINE, fault="")) == "Not connected"
    assert safety_state_text(None, NS(state=SafetyState.DISARMED, fault="")) == "Disarmed: the laser cannot fire"
    t = safety_state_text(None, NS(state=SafetyState.FAULT, fault="link lost: x (deadline 1500 ms)"))
    assert t == "Fault (latched): Lost connection to the motion card"


def test_stop_and_halt_unchanged(sim_win, app, caplog):
    """Hard rules: Stop stays immediate with no question; a disarm on an alarm never blocks a halt."""
    w, ctl, su = sim_win, sim_win.ctl, sim_win.safety_ui
    w._test_no_dialogs = False
    caplog.set_level(logging.WARNING)
    t0 = time.monotonic()
    su.stop()
    assert time.monotonic() - t0 < 0.5
