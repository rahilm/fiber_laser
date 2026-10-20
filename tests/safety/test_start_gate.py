"""Review 2: the controller's start gate (docs/SAFETY.md §5a).

P0-2  Stop while a planned Start still compiles must cancel it (it used to start 1.37 s after
      the Stop).
P0-3  A plan made from a stale head position (a move during the compile) must not run offset
      (the head used to reach X 1150 mm with a travel of 0..1000).
P1-3  Backward/Forward after a move while paused replays from the breakpoint (head reached
      X 1250 mm); it now rapids back from the real head first and pre-flights that extent.
P1-4  Start / Dry run / Frame / moves while a jog is still running are refused.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from safety_helpers import head_mm, rect_doc, start_and_run, stopped, wait_until

from mlaser.machine import events as EV


class SlowExecutor(ThreadPoolExecutor):
    """Stands in for a large compile (seconds on a big nest)."""

    def __init__(self, delay_s: float = 1.0):
        super().__init__(max_workers=1)
        self.delay_s = delay_s

    def submit(self, fn, *a, **kw):
        def slow():
            time.sleep(self.delay_s)
            return fn(*a, **kw)
        return super().submit(slow)


@pytest.fixture
def ctl():
    from mlaser.controller.ui_adapter import UiController
    c = UiController(poll_s=0.02, stream_process=False, require_homed=False, jog_deadman_s=None)
    assert c.connect_simulator(time_scale=1.0)
    started: list = []
    c.events.subscribe(EV.JobStarted, lambda e: started.append((time.monotonic(), e.mode)))
    c.started = started            # type: ignore[attr-defined]
    yield c
    c.stop(wait=True)
    c.wait_job(10)
    c.disconnect()


def _run_planned(ctl, doc, mode="dry_run", delay_s=1.0):
    from mlaser.services.document import DocumentService
    from mlaser.services.jobs import ControllerSubmitter, JobService, build_request
    docs = DocumentService(doc)
    jobs = JobService(executor=SlowExecutor(delay_s))
    results: list = []
    req = build_request(mode, docs, ctl, ctl.machine, None)
    jobs.run(req, ControllerSubmitter(ctl), lambda ok, m: results.append((ok, m)))
    return jobs, req, results


def test_stop_while_the_plan_compiles_cancels_the_start(ctl):
    jobs, _req, results = _run_planned(ctl, rect_doc(0, 0, 200, 100))
    time.sleep(0.2)
    ctl.stop(wait=True)                     # the operator presses Stop while "compiling..."
    assert wait_until(lambda: results, 10) is not None
    time.sleep(0.3)
    assert ctl.started == [], "the job started after Stop"
    assert results[0][0] is False
    assert not ctl._busy() and not ctl.sim.state.fifo_running
    assert ctl.last_preflight is not None and "start_gate" in ctl.last_preflight.codes()
    jobs.shutdown()


@pytest.mark.parametrize("what", ["disarm", "fault", "halt"])
def test_disarm_fault_or_halt_while_compiling_cancels_the_start(ctl, what):
    jobs, _req, results = _run_planned(ctl, rect_doc(0, 0, 200, 100), delay_s=0.6)
    time.sleep(0.1)
    if what == "disarm":
        ctl.disarm("test")
    elif what == "fault":
        ctl.raise_fault("test fault")
        assert ctl.reset_fault()
    else:
        ctl._sup.safe_halt("test halt")
    assert wait_until(lambda: results, 10) is not None
    time.sleep(0.2)
    assert ctl.started == [] and results[0][0] is False
    jobs.shutdown()


def test_generation_advances_on_stop_halt_fault_disarm_connect(ctl):
    g = ctl.start_generation()
    for act in (lambda: ctl.stop(wait=True), lambda: ctl._sup.safe_halt("x"), lambda: ctl.disarm("x"),
                lambda: (ctl.raise_fault("x"), ctl.reset_fault())):
        act()
        g2 = ctl.start_generation()
        assert g2 != g
        g = g2


def test_stop_between_the_gate_and_the_worker_is_honoured(ctl):
    """The last window: a Stop after the gate passed but before the worker exists."""
    from mlaser.nc import compile_job
    gen = ctl.start_generation()
    job = compile_job(rect_doc(0, 0, 50, 50), None, ctl.machine, start=head_mm(ctl.sim), dry_run=True)
    ctl.stop()
    assert ctl._run_job(job, "dry_run", "test", generation=gen) is False
    assert not ctl._busy() and ctl.started == []


def test_plan_from_a_stale_head_is_refused_never_run_offset(ctl):
    lim = ctl.travel_limits()
    x_max = lim.x[1]
    doc = rect_doc(x_max - 300 - 50, 0, 300, 100)        # fits from (0, 0): ends 50 mm before X max
    jobs, req, results = _run_planned(ctl, doc, delay_s=1.0)
    assert req.start == pytest.approx((0.0, 0.0), abs=0.01)
    ctl.move_by(200.0, 0.0, speed=2000.0, acc=20000.0)  # the head moves during the compile
    assert wait_until(lambda: results, 10) is not None
    peak = 0.0
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.5:
        peak = max(peak, head_mm(ctl.sim)[0])
        time.sleep(0.01)
    assert results[0][0] is False and ctl.started == []
    assert peak <= x_max + 1e-6
    assert "start_gate" in ctl.last_preflight.codes() or not ctl.last_preflight.ok
    jobs.shutdown()


def test_preflight_extent_is_translated_to_the_head():
    from mlaser.machine import preflight as PF
    from mlaser.nc import Machine, compile_job
    job = compile_job(rect_doc(600, 0, 300, 100), None, Machine(), start=(0.0, 0.0), dry_run=True)
    travel = PF.TravelLimits((0.0, 1000.0), (0.0, 1000.0))
    assert PF.check("dry_run", job=job, travel=travel, require_homed=False, head=(0.0, 0.0)).ok
    rep = PF.check("dry_run", job=job, travel=travel, require_homed=False, head=(200.0, 0.0))
    assert "out_of_travel" in rep.codes() and rep.extent[2] == pytest.approx(1100.0, abs=0.5)


@pytest.mark.parametrize("action", ["dry_run", "start", "frame", "move_to", "move_by", "laser_test"])
def test_starts_refused_while_jogging(ctl, action):
    assert ctl.arm()
    ctl.jog_start("X", +1, fast=True)
    assert wait_until(lambda: abs(ctl.sim.state.axes[0].vel) > 0, 2) is not None
    doc = rect_doc(0, 0, 100, 100)
    {"dry_run": lambda: ctl.dry_run(doc), "start": lambda: ctl.start(doc), "frame": lambda: ctl.frame(doc),
     "move_to": lambda: ctl.move_to(10.0, 10.0), "move_by": lambda: ctl.move_by(-5.0, 0.0),
     "laser_test": lambda: ctl.laser_test(0.05)}[action]()
    time.sleep(0.1)
    assert not ctl._busy() and ctl.started == []
    assert ctl.sim.state.axes[0].vel > 0                 # still the jog, nothing superimposed
    assert "start_gate" in ctl.last_preflight.codes()
    ctl.jog_stop("X")


def test_start_refused_while_the_card_still_moves_after_a_jog_step(ctl):
    ctl.jog_step("X", +1, 200.0, fast=True)
    assert wait_until(lambda: abs(ctl.sim.state.axes[0].vel) > 0, 2) is not None
    ctl.dry_run(rect_doc(0, 0, 100, 100))
    assert not ctl._busy() and ctl.started == []
    assert wait_until(lambda: stopped(ctl.sim), 10) is not None
    ctl.dry_run(rect_doc(0, 0, 100, 100))
    assert ctl._busy()


def test_backward_forward_after_a_move_while_paused(ctl):
    assert ctl.arm()
    start_and_run(ctl, rect_doc(0, 0, 900, 100), 1.0)     # cutting along y = 0 towards x = 900
    ctl.pause()
    assert wait_until(lambda: ctl.breakpoint is not None and not ctl._busy(), 10) is not None
    bp = ctl.breakpoint
    assert wait_until(lambda: stopped(ctl.sim), 5) is not None
    ctl.move_by(950.0 - head_mm(ctl.sim)[0], 0.0, speed=2000.0, acc=20000.0)
    assert wait_until(lambda: abs(head_mm(ctl.sim)[0] - 950) < 0.5 and stopped(ctl.sim), 15) is not None
    assert ctl.breakpoint is bp
    ctl.step_forward(300.0)
    assert ctl.last_preflight.ok, ctl.last_preflight.summary()
    assert ctl.last_preflight.extent[2] == pytest.approx(950.0, abs=1.0)   # from the real head
    peak = 0.0
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10 and (ctl._busy() or time.monotonic() - t0 < 0.3):
        peak = max(peak, head_mm(ctl.sim)[0])
        time.sleep(0.005)
    assert peak <= ctl.travel_limits().x[1]
    nb = ctl.breakpoint
    assert nb is not None and nb.index > bp.index
    # the head ends on the path: the breakpoint 300 mm further on (rapid back first)
    assert head_mm(ctl.sim)[0] == pytest.approx(nb.x, abs=0.5)


def test_forward_out_of_travel_is_refused_by_its_real_extent(ctl):
    """A forward move whose rapid from the head plus the path leaves the travel is refused."""
    from mlaser.nc import resume as R
    assert ctl.arm()
    start_and_run(ctl, rect_doc(0, 0, 900, 100), 0.6)
    ctl.pause()
    assert wait_until(lambda: ctl.breakpoint is not None and not ctl._busy(), 10) is not None
    bp = ctl.breakpoint
    seg, _new = R.along_path(bp.job, bp.index, 50.0, (950.0, 0.0))
    assert seg.start_mm == (950.0, 0.0)
    from mlaser.machine import preflight as PF
    ext = PF.job_extent_mm(seg)
    assert ext[2] == pytest.approx(950.0, abs=0.5) and ext[0] <= bp.x + 0.5


def test_a_new_job_is_refused_while_a_breakpoint_is_pending(ctl):
    from mlaser.services.jobs import ControllerSubmitter, JobService, build_request
    from mlaser.services.document import DocumentService
    assert ctl.arm()
    start_and_run(ctl, rect_doc(0, 0, 900, 100), 0.4)
    ctl.pause()
    assert wait_until(lambda: ctl.breakpoint is not None and not ctl._busy(), 10) is not None
    n = len(ctl.started)
    for mode in ("dry_run", "frame"):
        docs = DocumentService(rect_doc(0, 0, 40, 40))
        jobs = JobService(executor=SlowExecutor(0.0))
        res: list = []
        jobs.run(build_request(mode, docs, ctl, ctl.machine, None), ControllerSubmitter(ctl),
                 lambda ok, m: res.append(ok))
        assert wait_until(lambda: res, 10) is not None
        assert res == [False]
        jobs.shutdown()
    ctl.dry_run(rect_doc(0, 0, 40, 40))
    assert len(ctl.started) == n and ctl.breakpoint is not None


def test_active_door_interlock_refuses_a_cut_but_not_a_dry_run(ctl):
    from mlaser.controller.interlocks import InterlockConfig
    assert ctl.configure_safety(interlocks=InterlockConfig(door_input=3))
    assert ctl.arm()
    ctl.sim.state.inputs |= 1 << 2                      # door open, before the start (P2 S-12)
    time.sleep(0.15)
    ctl.start(rect_doc(0, 0, 50, 50))
    assert not ctl._busy() and ctl.started == []
    assert "interlock" in ctl.last_preflight.summary()
    ctl.dry_run(rect_doc(0, 0, 50, 50))                 # laser off: the door does not gate it
    assert wait_until(lambda: ctl.started, 2) is not None
    ctl.wait_job(30)


def test_demo_controller_honours_the_generation():
    from mlaser.machine.demo import DemoController
    from mlaser.services.document import DocumentService
    from mlaser.services.jobs import JobService, build_request, ControllerSubmitter
    d = DemoController()
    d.connect()
    sub = ControllerSubmitter(d)
    d.stop()
    jobs = JobService()
    plan = jobs.plan_sync(build_request("dry_run", DocumentService(rect_doc(0, 0, 50, 50)), d, None, None))
    assert sub.start_job(plan) is False
    assert ControllerSubmitter(d).start_job(plan) is True
    jobs.shutdown()


def test_concurrent_stop_never_lets_a_start_through(ctl):
    """Stop racing with Start from another thread: whichever wins, no job keeps running."""
    for _ in range(5):
        assert wait_until(lambda: stopped(ctl.sim), 5) is not None
        t = threading.Thread(target=lambda: ctl.dry_run(rect_doc(0, 0, 300, 300)))
        t.start()
        time.sleep(0.002)
        ctl.stop(wait=True)
        t.join(10)
        ctl.wait_job(10)
        time.sleep(0.05)
        assert not ctl._busy() and not ctl.sim.state.fifo_running


# ----------------------------------------------------------------------------- simulator fidelity
def test_simulator_reports_a_jog_in_progress_and_the_live_head(ctl):
    ctl.jog_start("X", +1, fast=True)
    assert wait_until(lambda: abs(ctl.sim.state.axes[0].vel) > 0, 2) is not None
    card = ctl.card()
    a = card.read_status()
    time.sleep(0.05)
    b = card.read_status()
    assert a.machine_state() == 2 and b.machine_state() == 2            # manual motion
    assert b.axes[0].velocity > 0 and b.axes[0].position > a.axes[0].position
    assert b.axes[0].position / b.pulses_per_mm == pytest.approx(head_mm(ctl.sim)[0], abs=5.0)
    ctl.jog_stop("X")
    assert wait_until(lambda: stopped(ctl.sim), 5) is not None
    assert card.read_status().machine_state() == 0


def test_pessimistic_simulator_decelerates_a_jog_after_the_stop(make_ctl):
    ctl = make_ctl(arm=False, jog_deadman_s=None)       # pessimistic simulator
    ctl.jog_start("X", +1, fast=True)
    assert wait_until(lambda: abs(ctl.sim.state.axes[0].vel) > 0, 2) is not None
    time.sleep(0.2)
    ctl.card().stop_motion(200)                         # a gentle deceleration: it coasts on
    ctl._jog_active, ctl._jogging = False, None         # the controller thinks the jog is over
    assert not stopped(ctl.sim)
    ctl.dry_run(rect_doc(0, 0, 50, 50))                 # refused: the card still moves
    assert not ctl._busy() and "start_gate" in ctl.last_preflight.codes()
    assert wait_until(lambda: stopped(ctl.sim), 10) is not None
