"""A job that fires the laser disarms the machine when it ends, is stopped or fails (UX review
finding 1, docs/SAFETY.md §2).  Exceptions: a hold at a breakpoint keeps the arming for the
resume, and the queue keeps it between its sheets (it disarms after the last one).  A job that
does not fire (frame, dry run) leaves the arming as it was.

On the unit seam under the job runner (seam_helpers.py: in-memory card simulator, virtual time;
deep review 2026-11, arch A11): no real-time waits, so this module runs outside the timing groups.
The arming at the real card's deadlines is covered by the threaded suite (test_ux_audit_safety.py,
test_arming_and_preflight.py).
"""
from __future__ import annotations

import pytest

from mlaser.machine import events as EV
from mlaser.machine.safety import SafetyState

from seam_helpers import mid_job, rect_doc, settle

pytestmark = pytest.mark.safety


def _short_doc():
    return rect_doc(w=5.0, h=5.0)


def _ended(ctl):
    """The job's worker has ended (a thread join, no clock involved)."""
    assert ctl.wait_job(30), "job did not end"
    assert not ctl.job_active() and not ctl._busy()


def _idle_phase(ctl) -> bool:
    """The job phase is back to idle: a Hold has kept its breakpoint, a Stop is complete."""
    return ctl._lc.wait_for(lambda st: st.phase.value == "idle" and not st.starting, 30)


def _stop(ctl, clock):
    done = ctl.stop()
    clock.release()                            # the worker leaves the gate and sees the cancel
    assert done.wait(30), "the stop did not complete"
    _ended(ctl)


def test_finished_cut_disarms(seam_ctl):
    ctl, clock = seam_ctl()
    seen = []
    ctl.events.subscribe(EV.Disarmed, lambda e: seen.append(e.reason))
    ctl.start(_short_doc())
    assert ctl.job_active()
    _ended(ctl)
    assert ctl.fsm.state == SafetyState.DISARMED
    assert not ctl.fsm.armed and not ctl.state().armed
    assert seen and seen[-1].startswith("disarmed")
    # the next Start fires nothing: the start is refused as not armed
    settle(ctl, clock)
    ctl.start(_short_doc())
    _ended(ctl)
    assert not ctl.fsm.armed
    rep = ctl.last_preflight
    assert rep is not None and any(i.code in ("not_armed", "start_gate") for i in rep.errors)


def test_stopped_cut_disarms(seam_ctl):
    ctl, clock = seam_ctl()
    mid_job(ctl, clock, rect_doc(), 0.2)
    assert ctl.fsm.state == SafetyState.RUNNING and ctl.sim.state.laser
    _stop(ctl, clock)
    assert not ctl.fsm.armed and ctl.fsm.state == SafetyState.DISARMED


def test_hold_keeps_arming_for_resume_and_stop_while_held_disarms(seam_ctl):
    ctl, clock = seam_ctl()
    mid_job(ctl, clock, rect_doc(), 0.2)
    ctl.pause()
    clock.release()
    assert ctl.wait_job(30) and _idle_phase(ctl), "the hold did not end"
    assert ctl.breakpoint is not None and ctl.fsm.state == SafetyState.PAUSED
    assert ctl.fsm.armed                       # Resume continues without a new arming question
    assert ctl.stop().wait(30)
    _ended(ctl)
    assert not ctl.fsm.armed and ctl.fsm.state == SafetyState.DISARMED


def test_laser_off_job_keeps_the_arming(seam_ctl):
    ctl, clock = seam_ctl()
    ctl.frame(_short_doc())
    _ended(ctl)
    assert ctl.fsm.armed and ctl.fsm.state == SafetyState.ARMED


def test_queue_sheet_keeps_arming_once(seam_ctl):
    ctl, clock = seam_ctl()
    ctl.keep_armed_after_next_job()
    ctl.start(_short_doc())
    _ended(ctl)
    assert ctl.fsm.armed and ctl.fsm.state == SafetyState.ARMED   # the next sheet
    settle(ctl, clock)
    ctl.start(_short_doc())                    # a second job without the request: disarms
    _ended(ctl)
    assert not ctl.fsm.armed


def test_queue_sheet_that_is_stopped_disarms(seam_ctl):
    ctl, clock = seam_ctl()
    ctl.keep_armed_after_next_job()
    mid_job(ctl, clock, rect_doc(), 0.2)
    _stop(ctl, clock)
    assert not ctl.fsm.armed


def test_withdrawn_keep_request_is_not_used(seam_ctl):
    ctl, clock = seam_ctl()
    ctl.keep_armed_after_next_job()
    ctl.keep_armed_after_next_job(False)
    ctl.start(_short_doc())
    _ended(ctl)
    assert not ctl.fsm.armed


def test_queue_runner_asks_and_withdraws():
    from mlaser.services.queue import JobQueue, QueueRunner
    calls = []
    q = JobQueue(None) if _queue_takes_none() else JobQueue("")
    q.add("a.dxf", quantity=2)
    r = QueueRunner(q, start=lambda: False, keep_armed=calls.append)
    r.begin()
    assert not r.confirm_sheet()
    assert calls == [True, False]              # asked before the start, withdrawn on refusal
    calls.clear()
    r2 = QueueRunner(q, start=lambda: True, keep_armed=calls.append)
    r2.begin()
    assert r2.confirm_sheet()
    assert calls == [True]


def _queue_takes_none() -> bool:
    import inspect

    from mlaser.services.queue import JobQueue
    p = list(inspect.signature(JobQueue.__init__).parameters.values())[1]
    return p.default is None or "Optional" in str(p.annotation)


def test_demo_controller_disarms_after_a_cut():
    from mlaser.machine.demo import DemoController
    t = [0.0]
    ctl = DemoController(clock=lambda: t[0])
    ctl.connect()
    assert ctl.arm()
    ctl.start(_short_doc())
    assert ctl.job_active()
    t[0] += 1000.0
    ctl.state()
    assert not ctl.job_active()
    assert not ctl.fsm.armed
    assert ctl.arm()
    ctl.keep_armed_after_next_job()
    ctl.start(_short_doc())
    t[0] += 2000.0
    ctl.state()
    assert not ctl.job_active() and ctl.fsm.armed
