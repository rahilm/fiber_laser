"""The job-run safety paths on both streamers (review arch.md A5).

A real card gets the process streamer and the 150 ms link deadline; the simulator defaults to
the in-thread streamer and a 1 s deadline, so the real configuration used to be the minority
path in tests.  Here start-to-end, pause + resume and an underrun run on both
(``safety_helpers.REAL_AND_THREAD``); stop, link loss, the alarm mid-job, hold and a second
client's stop are parametrised the same way in test_exit_paths.py / test_link_and_streamer.py.

=====================================  ==============================================
scenario                               asserted on both streamers
=====================================  ==============================================
job start to end                       done, no fault, every planned step executed once,
                                       job reset and step format sent, FIFO stopped and
                                       PWM off at the end, the card idle (RO[19] 0)
pause, then resume                     PAUSED with a breakpoint, card safe, nothing moves;
                                       Resume runs the rest to the end without a fault
the card starved (slow replies)        FAULT (underrun / unexpected idle; process: the
                                       child's silence), card safe, breakpoint kept,
                                       never restarted
=====================================  ==============================================
"""
from __future__ import annotations

import time

import pytest

from mlaser.machine.safety import SafetyState
from mlaser.protocol import frames as F

from safety_helpers import REAL_AND_THREAD, rect_doc, safe, start_and_run, stopped, wait_until

SHORT = dict(w=60.0, h=40.0)          # 200 mm at 100 mm/s: about 2 s of motion


def _steps(wire) -> int:
    return sum(1 for w in wire if (w[0] & 0xFFFF) == F.OP_STEP)


def _executed_steps(sim) -> int:
    return sum(1 for op, _p in list(sim.state.history) if op == F.OP_STEP)


@pytest.mark.flaky(reruns=1)       # process: the real 150 ms deadline (docs/dev/TESTING.md)
@pytest.mark.parametrize("card", REAL_AND_THREAD)
def test_job_runs_from_start_to_end(make_ctl, card):
    ctl = make_ctl(**card)
    sim = ctl.sim
    wait_until(lambda: stopped(sim), 5)
    ctl.start(rect_doc(**SHORT))
    assert wait_until(lambda: sim.state.fifo_running, 20) is not None, "job did not start"
    assert ctl.wait_job(30)
    assert not ctl.fsm.faulted, ctl.fsm.fault
    assert ctl.fsm.state == SafetyState.ARMED and ctl.breakpoint is None
    assert _executed_steps(sim) == _steps(ctl.job.wire)
    cmds = sim.state.commands
    assert [F.OP_SYS, F.SYS_JOB_RESET] in cmds
    assert [F.OP_SYS, F.SYS_AXIS_CFG, F.STEP_FORMAT_STEP_CARD | 0x3, 0] in cmds
    # the end like the original: FIFO stop, then the PWM off (docs/re/protocol.md §15)
    assert wait_until(lambda: safe(sim) and not sim.state.program_active, 2.0) is not None
    assert [F.OP_SYS, F.SYS_PWM, ctl.machine.pt_laser_freq, 0, 0] in cmds
    assert wait_until(lambda: ctl.state().mode == "ready", 2.0) is not None


@pytest.mark.flaky(reruns=1)       # process: the real 150 ms deadline (docs/dev/TESTING.md)
@pytest.mark.parametrize("card", REAL_AND_THREAD)
def test_pause_then_resume_runs_to_the_end(make_ctl, card):
    ctl = make_ctl(**card)
    sim = ctl.sim
    start_and_run(ctl, rect_doc(w=100.0, h=60.0), run_s=0.6)
    ctl.pause()
    assert wait_until(lambda: ctl.breakpoint is not None, 5) is not None
    assert ctl.fsm.state == SafetyState.PAUSED and not ctl.fsm.faulted
    assert wait_until(lambda: safe(sim), 1.0) is not None
    x = sim.position(0), sim.position(1)
    time.sleep(0.3)
    assert (sim.position(0), sim.position(1)) == x, "the head moved while paused"
    assert ctl.wait_job(5)
    wait_until(lambda: stopped(sim), 5)
    ctl.resume()
    assert wait_until(lambda: sim.state.fifo_running, 20) is not None, "resume did not start"
    assert wait_until(lambda: ctl.breakpoint is None and not ctl._busy(), 40) is not None
    assert ctl.wait_job(10)
    assert not ctl.fsm.faulted, ctl.fsm.fault
    assert ctl.fsm.state == SafetyState.ARMED
    assert wait_until(lambda: safe(sim) and not sim.state.program_active, 2.0) is not None


@pytest.mark.flaky(reruns=1)       # process: the real 150 ms deadline (docs/dev/TESTING.md)
@pytest.mark.parametrize("card", REAL_AND_THREAD)
def test_a_starved_card_is_a_fault_never_a_restart(make_ctl, card):
    """Every reply 100 ms late (within the 150 ms deadline): the streamer cannot keep the FIFO
    filled.  The in-thread streamer's watchdog (or the card running dry) ends the job with a
    FAULT; the process streamer's child, waiting for its slow writes, stops relaying first, which
    the parent takes for a lost link.  Either way: FAULT, card safe, never restarted."""
    ctl = make_ctl(**card)
    sim = ctl.sim
    start_and_run(ctl, rect_doc(w=200.0, h=100.0), run_s=0.3)
    sim.inject(latency_ms=100.0)
    assert wait_until(lambda: ctl.fsm.state == SafetyState.FAULT, 10.0) is not None
    kinds = ("underrun", "unexpected_idle") + (("link lost", "child_stalled") if card["process"] else ())
    assert any(k in ctl.fsm.fault for k in kinds), ctl.fsm.fault
    sim.inject(latency_ms=0.0)
    assert ctl.wait_job(10) and ctl.breakpoint is not None
    assert wait_until(lambda: safe(sim), 2.0) is not None
    x = sim.position(0), sim.position(1)
    time.sleep(0.5)
    assert (sim.position(0), sim.position(1)) == x and not sim.state.fifo_running
    assert ctl.stream_stats.get("restarts", 0) == 0
