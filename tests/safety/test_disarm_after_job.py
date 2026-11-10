"""A job that fires the laser disarms the machine when it ends, is stopped or fails (UX review
finding 1, docs/SAFETY.md §2).  Exceptions: a hold at a breakpoint keeps the arming for the
resume, and the queue keeps it between its sheets (it disarms after the last one).  A job that
does not fire (frame, dry run) leaves the arming as it was.
"""
from __future__ import annotations

import pytest

from mlaser.machine import events as EV
from mlaser.machine.safety import SafetyState

from safety_helpers import rect_doc, start_and_run, stopped, wait_until

pytestmark = pytest.mark.safety


def _short_doc():
    return rect_doc(w=5.0, h=5.0)


def _wait_idle(ctl, timeout=20.0):
    assert wait_until(lambda: not ctl.job_active() and not ctl._busy(), timeout), "job did not end"


def test_finished_cut_disarms(make_ctl):
    ctl = make_ctl()
    seen = []
    ctl.events.subscribe(EV.Disarmed, lambda e: seen.append(e.reason))
    ctl.start(_short_doc())
    assert wait_until(lambda: ctl.fsm.state == SafetyState.RUNNING or ctl.job_active(), 10)
    _wait_idle(ctl)
    assert ctl.fsm.state == SafetyState.DISARMED
    assert not ctl.fsm.armed and not ctl.state().armed
    assert seen and seen[-1].startswith("disarmed")
    # the next Start fires nothing: the start is refused as not armed
    wait_until(lambda: stopped(ctl.sim), 5)
    ctl.start(_short_doc())
    _wait_idle(ctl)
    assert not ctl.fsm.armed
    rep = ctl.last_preflight
    assert rep is not None and any(i.code in ("not_armed", "start_gate") for i in rep.errors)


def test_stopped_cut_disarms(make_ctl):
    ctl = make_ctl()
    start_and_run(ctl, run_s=0.2)
    ctl.stop(wait=True)
    _wait_idle(ctl)
    assert not ctl.fsm.armed and ctl.fsm.state == SafetyState.DISARMED


def test_hold_keeps_arming_for_resume_and_stop_while_held_disarms(make_ctl):
    ctl = make_ctl()
    start_and_run(ctl, run_s=0.2)
    ctl.pause()
    assert wait_until(lambda: ctl.breakpoint is not None and ctl.fsm.state == SafetyState.PAUSED, 10)
    assert ctl.fsm.armed                       # Resume continues without a new arming question
    ctl.stop(wait=True)
    _wait_idle(ctl)
    assert not ctl.fsm.armed and ctl.fsm.state == SafetyState.DISARMED


def test_laser_off_job_keeps_the_arming(make_ctl):
    ctl = make_ctl()
    wait_until(lambda: stopped(ctl.sim), 5)
    ctl.frame(_short_doc())
    _wait_idle(ctl)
    assert ctl.fsm.armed and ctl.fsm.state == SafetyState.ARMED


def test_queue_sheet_keeps_arming_once(make_ctl):
    ctl = make_ctl()
    ctl.keep_armed_after_next_job()
    ctl.start(_short_doc())
    _wait_idle(ctl)
    assert ctl.fsm.armed and ctl.fsm.state == SafetyState.ARMED   # the next sheet
    wait_until(lambda: stopped(ctl.sim), 5)
    ctl.start(_short_doc())                    # a second job without the request: disarms
    _wait_idle(ctl)
    assert not ctl.fsm.armed


def test_queue_sheet_that_is_stopped_disarms(make_ctl):
    ctl = make_ctl()
    ctl.keep_armed_after_next_job()
    start_and_run(ctl, run_s=0.2)
    ctl.stop(wait=True)
    _wait_idle(ctl)
    assert not ctl.fsm.armed


def test_withdrawn_keep_request_is_not_used(make_ctl):
    ctl = make_ctl()
    ctl.keep_armed_after_next_job()
    ctl.keep_armed_after_next_job(False)
    ctl.start(_short_doc())
    _wait_idle(ctl)
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
