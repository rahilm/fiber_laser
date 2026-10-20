"""Guided procedures (mlaser.services.procedures) against the card simulator: every wizard's
step machine, abort -> safe_halt at any step, the edge-seek transform with undo, the
commissioning checklist results."""
import json
import math
import time

import pytest

from mlaser.controller import edgeseek as E
from mlaser.controller.simulator import SimSheet
from mlaser.model import Contour, Document, Line, Part
from mlaser.services.document import DocumentService
from mlaser.services.procedures import commissioning as CM
from mlaser.services.procedures import engine as PE
from mlaser.services.procedures import simrig
from mlaser.services.procedures.edge_seek import EdgeSeekProcedure, apply_to_docs, sheet_outline
from mlaser.services.procedures.focus_home import FocusHomingProcedure
from mlaser.services.procedures.follower_cal import FollowerCalibrationProcedure
from mlaser.services.procedures.laser_test import LaserTestParams, LaserTestProcedure


# ---------------------------------------------------------------------------- helpers
def run_all(p, answers=None, stop_at=None):
    """Drive a procedure like the wizard does: run actions, answer questions, Next."""
    answers = dict(answers or {})
    if p.state == PE.READY:
        p.start()
    for _ in range(60):
        s = p.step
        if p.state in PE.FINAL or s is None or s.id == stop_at:
            return p
        if s.kind == "action" and s.id not in p.done:
            if not p.execute():
                return p
            continue
        if s.kind == "question":
            p.answer(answers.get(s.id, True))
        p.next()
    raise AssertionError("procedure did not finish")


class HaltSpy:
    """Wraps a controller and counts safe_halt calls (the call still goes through)."""

    def __init__(self, ctl):
        self._ctl = ctl
        self.halts = []

    def safe_halt(self, reason=""):
        self.halts.append(reason)
        self._ctl.safe_halt(reason)

    def __getattr__(self, name):
        return getattr(self._ctl, name)


def edge_params(**kw):
    p = E.EdgeSeekParams(board_x=400, board_y=300, current_start=False, start=(130.0, 80.0),
                         move_speed=400.0, timeout_s=20)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


@pytest.fixture
def sheet_ctl():
    ctl = simrig.simulator_controller("sheet", sheet=SimSheet.rect(100.0, 50.0, 1000, 600, 2.0))
    yield ctl
    simrig.close(ctl)


def square_doc():
    sq = Contour([Line((0, 0), (10, 0)), Line((10, 0), (10, 10)), Line((10, 10), (0, 10)), Line((0, 10), (0, 0))])
    return Document(parts=[Part([sq])])


# ---------------------------------------------------------------------------- engine
class _Fake:
    """Minimal MachineAPI for the engine tests."""

    def __init__(self):
        self.halts = []
        self.actions = []

    def safe_halt(self, reason=""):
        self.halts.append(reason)

    def operator_action(self, what=""):
        self.actions.append(what)


class _Proc(PE.Procedure):
    id = "fake"

    def build_steps(self):
        return [PE.Step("a", "t", kind="info"), PE.Step("b", "t", kind="input"),
                PE.Step("c", "t", kind="action", run=lambda p: p.operator_action("go") or 42),
                PE.Step("q", "t", kind="question"),
                PE.Step("d", "t", kind="action", run=self._slow),
                PE.Step("e", "t", kind="info", skip=lambda p: True)]

    def _slow(self, p):
        p.wait(lambda: False, 5.0, "never")


def test_engine_navigation_and_question():
    m = _Fake()
    p = _Proc(m)
    p.start()
    assert p.step.id == "a" and p.can_next() and not p.can_back()
    p.next()
    assert p.can_back()
    p.back()
    assert p.step.id == "a"
    p.next()
    p.next()
    assert p.step.id == "c" and not p.can_next()
    with pytest.raises(RuntimeError):
        p.next()
    assert p.execute() and p.results["c"] == 42 and m.actions == ["fake: go"]
    assert not p.can_back()              # an action ran
    p.next()
    assert p.step.kind == "question" and not p.can_next()
    p.answer(False)
    assert p.answers["q"] is False and p.can_next()
    p.next()
    assert p.step.id == "d"


def test_engine_abort_in_a_running_step_halts():
    m = _Fake()
    p = _Proc(m)
    run_all(p, stop_at="d")
    seen = []
    p.post = lambda fn: (seen.append(1), fn())
    t = p.execute_async()
    time.sleep(0.1)
    assert p.busy()
    p.abort("operator")
    t.join(2.0)
    assert not t.is_alive()
    assert p.state == PE.ABORTED and len(m.halts) >= 1 and seen
    assert p.history[-1].outcome == "aborted"


@pytest.mark.parametrize("stop", ["a", "b", "c", "q", "d"])
def test_engine_abort_at_every_step_sends_safe_halt(stop):
    m = _Fake()
    p = run_all(_Proc(m), stop_at=stop)
    p.abort()
    assert p.state == PE.ABORTED and m.halts and "aborted" in m.halts[0]


def test_engine_failure_halts():
    m = _Fake()

    class P(PE.Procedure):
        def build_steps(self):
            return [PE.Step("x", "t", kind="action", run=self._boom)]

        def _boom(self, p):
            raise ValueError("bad")
    p = P(m)
    p.start()
    assert not p.execute()
    assert p.state == PE.FAILED and p.error.key == "wizard.error.step" and "bad" in p.error.values["detail"]
    assert m.halts


def test_mode_allows():
    assert PE.mode_allows(None, "service")
    assert PE.mode_allows("service", "operator")
    assert not PE.mode_allows("operator", "engineer")
    assert PE.mode_allows("engineer", "engineer")


# ---------------------------------------------------------------------------- edge seek
@pytest.mark.parametrize("mode,n", [("cap3", 3), ("cap4", 4)])
def test_edge_seek_capacitive_step_machine(sheet_ctl, mode, n):
    p = EdgeSeekProcedure(sheet_ctl, mode, params=edge_params(), values={"ZF.ZFType": 1})
    run_all(p, stop_at="review")
    assert p.state == PE.WAITING and p.step.id == "review"
    res = p.result
    assert len(res.points) == n
    assert res.corner == pytest.approx((100.0, 50.0), abs=0.05)
    assert res.angle_deg == pytest.approx(2.0, abs=0.02)
    out = p.outline()
    poly = out["outline"][0]
    assert poly[0] == pytest.approx(res.corner) and poly[-1] == poly[0] and len(poly) == 5
    # the outline's X edge has the measured angle and the board length
    (x0, y0), (x1, y1) = poly[0], poly[1]
    assert math.degrees(math.atan2(y1 - y0, x1 - x0)) == pytest.approx(2.0, abs=0.02)
    assert math.hypot(x1 - x0, y1 - y0) == pytest.approx(400.0)


def test_edge_seek_apply_with_undo(sheet_ctl):
    docs = DocumentService()
    docs.replace(square_doc(), None, "load")
    p = EdgeSeekProcedure(sheet_ctl, "cap4", params=edge_params(), values={"ZF.ZFType": 1},
                          apply=lambda r: apply_to_docs(docs, r))
    run_all(p)
    assert p.state == PE.DONE and p.applied
    ex = docs.document.extra
    assert ex["rotated"] and ex["rotate_angle"] == pytest.approx(math.radians(2.0), abs=4e-4)
    assert ex["rotate_center"] == pytest.approx(p.result.origin)
    docs.undo()
    assert not docs.document.extra.get("rotated")
    docs.redo()
    assert docs.document.extra["rotated"]


def test_edge_seek_dry_run_never_applies():
    ctl = simrig.simulator_controller("sheet")
    try:
        applied = []
        p = EdgeSeekProcedure(ctl, "cap4", params=edge_params(), apply=applied.append, dry_run=True)
        run_all(p)
        assert p.state == PE.DONE and not applied and "apply" not in p.done
        assert p.result.angle_deg == pytest.approx(simrig.DEMO_SHEET[4], abs=0.02)
    finally:
        simrig.close(ctl)


def test_edge_seek_disc():
    ctl = simrig.simulator_controller("disc")
    try:
        p = EdgeSeekProcedure(ctl, "disc", params=edge_params(), dry_run=True)
        p.disc_guess = (310.0, 190.0)
        run_all(p, stop_at="review")
        assert p.result.centre == pytest.approx(simrig.DEMO_DISC[:2], abs=0.1)
        out = p.outline()
        assert out["radius"] == pytest.approx(simrig.DEMO_DISC[2], abs=0.2)
    finally:
        simrig.close(ctl)


def test_edge_seek_two_point_records_the_head():
    ctl = simrig.simulator_controller("card")
    try:
        p = EdgeSeekProcedure(ctl, "two_point")
        run_all(p, stop_at="p1")
        assert [s.id for s in p.visible_steps()] == ["intro", "check", "p1", "p2", "compute", "review", "apply"]
        assert p.execute()                       # P1 at (0, 0)
        p.next()
        ctl.move_to(100.0, 100.0 * math.tan(math.radians(3.0)))
        assert ctl.wait_job(10.0)
        time.sleep(0.2)
        assert p.execute()
        p.next()
        assert p.execute()                       # compute
        assert p.result.angle_deg == pytest.approx(3.0, abs=0.05)
    finally:
        simrig.close(ctl)


def test_edge_seek_abort_during_measure_halts_the_card(sheet_ctl):
    spy = HaltSpy(sheet_ctl)
    p = EdgeSeekProcedure(spy, "cap4", params=edge_params(out_speed=5.0), values={"ZF.ZFType": 1})
    run_all(p, stop_at="measure")
    t = p.execute_async()
    time.sleep(0.4)
    assert p.busy()
    p.abort("operator")
    t.join(5.0)
    assert not t.is_alive() and p.state == PE.ABORTED
    assert spy.halts and "aborted" in spy.halts[0]
    sim = sheet_ctl.sim
    assert any(c[:2] == [1, 0x1F] for c in sim.state.commands)     # decelerated stop reached the card
    time.sleep(0.5)
    st = sheet_ctl.card().read_status()
    assert not any(a.busy for a in st.axes)


def test_edge_seek_check_refuses_without_follower(sheet_ctl):
    p = EdgeSeekProcedure(sheet_ctl, "cap4", params=edge_params(), values={"ZF.ZFType": 0})
    run_all(p)
    assert p.state == PE.FAILED and p.error.key == "wizard.edge.error.no_follower"


def test_sheet_outline_board_fallback():
    res = E.EdgeSeekResult(mode=4, points=[(10, 0), (60, 0), (0, 10), (0, 40)], corner=(0, 0), angle=0.0)
    out = sheet_outline(res, E.EdgeSeekParams(), "cap4")
    assert out["size"] == (pytest.approx(90.0), pytest.approx(60.0))


# ---------------------------------------------------------------------------- follower
def test_follower_calibration_curve_saved(tmp_path):
    ctl = simrig.simulator_controller("follower")
    try:
        path = tmp_path / "calib.csv"
        path.write_text("1, 0\r\n")
        p = FollowerCalibrationProcedure(ctl, save_path=path)
        run_all(p)
        assert p.state == PE.DONE, p.error
        assert p.grade == "excellent"
        assert p.results["curve"]["points"] == 22 and p.results["curve"]["monotonic"]
        assert path.read_text().startswith("413023, 0") and (tmp_path / "calib.csv.bak").exists()
        assert [107] in ctl.sim.state.commands
    finally:
        simrig.close(ctl)


def test_follower_abort_cancels_calibration():
    ctl = simrig.simulator_controller("follower", calibration_s=30.0)
    try:
        spy = HaltSpy(ctl)
        p = FollowerCalibrationProcedure(spy, dry_run=True)
        run_all(p, stop_at="calibrate")
        t = p.execute_async()
        time.sleep(0.4)
        p.abort()
        t.join(5.0)
        assert p.state == PE.ABORTED and spy.halts
        assert ctl.sim.state.commands[-1] == [101] or [101] in ctl.sim.state.commands
        assert "save" not in p.done
    finally:
        simrig.close(ctl)


# ---------------------------------------------------------------------------- focus
def test_focus_homing_dry_run_axis():
    ctl = simrig.simulator_controller("card")
    try:
        p = FocusHomingProcedure(ctl, dry_run=True)
        run_all(p)
        assert p.state == PE.DONE, p.error
        assert p.results["home"] == {"homed": True, "axis": 4}
    finally:
        simrig.close(ctl)


def test_focus_homing_not_configured():
    ctl = simrig.simulator_controller("card")
    try:
        p = FocusHomingProcedure(ctl, values={"AF.AFType": 0})
        run_all(p)
        assert p.state == PE.FAILED and p.error.key == "wizard.focus.error.none"
    finally:
        simrig.close(ctl)


# ---------------------------------------------------------------------------- laser test
def test_laser_test_arms_fires_and_disarms():
    ctl = simrig.simulator_controller("card")
    try:
        p = LaserTestProcedure(ctl, LaserTestParams([10.0, 40.0], 50.0, 0.1))
        run_all(p, stop_at="arm")
        assert not ctl.safety().armed
        run_all(p, stop_at="results")
        assert [r.duty_pct for r in p.pulses] == [10.0, 40.0] and all(r.fired for r in p.pulses)
        assert ctl.safety().armed                # armed only inside the procedure
        run_all(p)
        assert p.state == PE.DONE and not ctl.safety().armed
    finally:
        simrig.close(ctl)


def test_laser_test_abort_disarms_and_halts():
    ctl = simrig.simulator_controller("card")
    try:
        spy = HaltSpy(ctl)
        p = LaserTestProcedure(spy, LaserTestParams([10.0], 50.0, 0.1))
        run_all(p, stop_at="fire")
        assert ctl.safety().armed
        p.abort()
        assert p.state == PE.ABORTED and spy.halts and not ctl.safety().armed
    finally:
        simrig.close(ctl)


def test_laser_test_refused_in_dry_run_mode():
    ctl = simrig.simulator_controller("card", laser_enabled=False)
    try:
        p = LaserTestProcedure(ctl)
        run_all(p)
        assert p.state == PE.FAILED and p.error.key == "wizard.laser.error.dry_run_mode"
        assert not ctl.safety().armed
    finally:
        simrig.close(ctl)


def test_laser_test_params_validated():
    ctl = simrig.simulator_controller("card")
    try:
        p = LaserTestProcedure(ctl, LaserTestParams([150.0]))
        run_all(p)
        assert p.state == PE.FAILED and p.error.key == "wizard.laser.error.level"
    finally:
        simrig.close(ctl)


# ---------------------------------------------------------------------------- commissioning
def _netcheck_ok(host, port, probe=False):
    from mlaser import netcheck
    return netcheck.NetCheckResult(host, port, "test", [netcheck.Check("route", netcheck.OK, "ok")],
                                   local_ip="127.0.0.1")


def test_commissioning_checklist_all_pass(tmp_path):
    ctl = simrig.simulator_controller("card", heartbeat_output=7)
    try:
        p = CM.CommissioningProcedure(ctl, report_dir=tmp_path, netcheck_fn=_netcheck_ok)
        run_all(p)
        assert p.state == PE.DONE, p.error
        st = {k: it.status for k, it in p.items.items()}
        # no interlock is configured on the simulator: flagged, not failed (tests/safety/test_safety_io.py)
        assert st == {"network": "pass", "scaling": "pass", "soft_limits": "warn", "interlocks": "warn",
                      "homing": "pass", "heartbeat": "pass", "jog_x": "pass", "jog_y": "pass"}
        assert p.items["heartbeat"].values["toggles_per_s"] == pytest.approx(10.0, rel=0.3)
        assert p.items["jog_x"].values["moved"] == pytest.approx(10.0, abs=0.01)
        rep = json.loads(p.report_path.read_text())
        assert rep["summary"]["ok"] and len(rep["items"]) == 8
        assert ctl.heartbeat_status()["hold"] is False
    finally:
        simrig.close(ctl)


def test_commissioning_failures_and_fixes(tmp_path):
    from mlaser.services.config import ConfigService
    cfg = ConfigService(tmp_path / "c.json")
    ctl = simrig.simulator_controller("card")              # no heartbeat output configured
    try:
        cfg.set("A0.PulseEquivalent", 1.0)                   # untouched machine: scaling still matches
        p = CM.CommissioningProcedure(ctl, config=cfg, report_dir=tmp_path, netcheck_fn=_netcheck_ok)
        run_all(p, answers={"jog_x_dir": False, "jog_y_dir": True})
        assert p.items["heartbeat"].status == CM.FAIL and p.items["heartbeat"].detail == "wizard.comm.hb_off"
        assert "heartbeat_relay" not in p.answers                    # skipped without a heartbeat
        x = p.items["jog_x"]
        assert x.status == CM.FAIL and x.fix == "wizard.comm.fix_reverse"
        assert p.fix_axis_direction("X") is True and cfg.get("MAC.AxisReverse") is True
        assert p.items["jog_x"].status == CM.WARN
        assert not p.summary()["ok"]
    finally:
        simrig.close(ctl)


def test_commissioning_scaling_mismatch(tmp_path):
    from mlaser.nc.machine import Machine

    class Cfg:
        def machine(self):
            return Machine(pulses_per_mm_x=50.0)

        def get(self, k, d=None):
            return d
    ctl = simrig.simulator_controller("card")
    try:
        p = CM.CommissioningProcedure(ctl, config=Cfg(), report_dir=tmp_path, netcheck_fn=_netcheck_ok)
        run_all(p, stop_at="soft_limits")
        it = p.items["scaling"]
        assert it.status == CM.FAIL and it.values["which"] == "X" and it.values["card_x"] == 100.0
    finally:
        simrig.close(ctl)


def test_commissioning_relay_answer_no_fails(tmp_path):
    ctl = simrig.simulator_controller("card", heartbeat_output=5)
    try:
        # the default 1 s measuring window (0.6 s left 6 edges to count, and on the macOS VMs
        # one late edge put the rate under the 60 % the check requires)
        p = CM.CommissioningProcedure(ctl, report_dir=tmp_path, netcheck_fn=_netcheck_ok)
        run_all(p, stop_at="heartbeat_relay")
        p.heartbeat_hold(True)
        # an edge already being written when the hold came, and the one "off" write after it,
        # complete first (on the macOS runners a write to the simulator takes tens of ms)
        time.sleep(0.25)
        n = ctl.heartbeat_status()["toggles"]
        time.sleep(0.5)
        assert ctl.heartbeat_status()["toggles"] == n              # held: no more edges
        assert not ctl.sim.state.outputs >> (ctl.heartbeat_status()["output"] - 1) & 1   # and off
        p.answer(False)
        assert p.items["heartbeat"].status == CM.FAIL and not ctl.heartbeat_status()["hold"]
    finally:
        simrig.close(ctl)
