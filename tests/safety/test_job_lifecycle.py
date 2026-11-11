"""The job lifecycle has one owner (deep review 2026-11, arch A1): the phase
(``mlaser.machine.jobphase``), changed through one locked function with a transition table, and
``MachineState.mode`` derived from it, the breakpoint, the connection and the card's status,
never from the mode shown before.

The first half tests the phase object without threads; the second half drives the real
``UiController`` on the unit seam (seam_helpers.py: in-memory simulator, virtual time), so
nothing here waits on the wall clock and the module runs outside the timing groups.
"""
from __future__ import annotations

import threading

import pytest

from mlaser.machine.jobphase import (HOLD, LIVE, TRANSITIONS, JobLifecycle, JobPhase, PhaseState,
                                     derive_mode)
from mlaser.machine.safety import SafetyState

from seam_helpers import mid_job, rect_doc

pytestmark = pytest.mark.safety
P = JobPhase


# ---------------------------------------------------------------------------- the phase object
def test_the_transition_table_is_closed_and_ends_in_idle():
    for src, dst in TRANSITIONS.items():
        assert src is not P.AT_BREAKPOINT and P.AT_BREAKPOINT not in dst   # reported only
        assert dst <= set(TRANSITIONS)
    for ph in TRANSITIONS:                          # every phase can reach IDLE
        seen, todo = {ph}, [ph]
        while todo:
            for n in TRANSITIONS[todo.pop()]:
                if n not in seen:
                    seen.add(n)
                    todo.append(n)
        assert P.IDLE in seen, ph
    assert HOLD <= LIVE and P.RUNNING not in HOLD


def test_a_start_runs_through_its_phases():
    lc = JobLifecycle()
    assert lc.begin_start("process")
    assert lc.state == PhaseState(P.STARTING, "process", True, True)
    assert not lc.begin_start("frame")             # one job at a time
    lc.end_start(True)
    assert lc.state == PhaseState(P.RUNNING, "process", False, True)
    assert lc.go(P.PAUSING, "hold", frm=(P.RUNNING,))
    assert not lc.go(P.HOLDING, "hold", frm=(P.RUNNING,))   # a second Hold finds it taken
    assert not lc.go(P.STOPPING, "stop", frm=(P.STARTING, P.RUNNING, P.HOLDING))
    assert lc.worker_ended() is P.PAUSING           # the pause's end makes it IDLE
    assert lc.state.hold and not lc.state.worker
    assert lc.end_job("breakpoint kept", frm=(P.PAUSING,))
    assert lc.state == PhaseState(P.IDLE, "", False, False)
    assert [h[1:3] for h in lc.history] == [("idle", "starting"), ("starting", "running"),
                                           ("running", "pausing"), ("pausing", "idle")]


def test_a_stop_in_the_start_window_keeps_the_window_for_the_stop_to_wait_on():
    lc = JobLifecycle()
    lc.begin_start("process")
    assert lc.go(P.STOPPING, "stop", frm=(P.STARTING, P.RUNNING, P.HOLDING))
    assert lc.state.starting and lc.state.hold
    seen = []
    t = threading.Thread(target=lambda: seen.append(lc.wait_started(30)))
    t.start()
    lc.end_start(True)                              # the worker was started (with the cancel)
    t.join(30)
    assert seen == [True]
    assert lc.phase is P.STOPPING                   # the Stop's end makes it IDLE
    assert not lc.begin_start("process")            # the next start waits for that
    assert lc.end_job("stopped", frm=(P.STOPPING,))
    assert lc.begin_start("process")


def test_a_failed_start_goes_back_to_idle():
    lc = JobLifecycle()
    lc.begin_start("frame")
    lc.end_start(False)
    assert lc.state == PhaseState(P.IDLE, "", False, False)


def test_the_worker_end_of_a_running_or_held_job_is_idle():
    for ph in (P.RUNNING, P.HOLDING):
        lc = JobLifecycle()
        lc.begin_start("process")
        lc.end_start(True)
        if ph is P.HOLDING:
            assert lc.go(P.HOLDING, frm=(P.RUNNING,))
        assert lc.worker_ended() is P.IDLE


@pytest.mark.parametrize("st,kw,mode", [
    (PhaseState(P.IDLE, "", False), dict(connected=False), "offline"),
    (PhaseState(P.IDLE, "", False), dict(connected=True), "ready"),
    (PhaseState(P.IDLE, "", False), dict(connected=True, card_state=1), "origin"),
    (PhaseState(P.IDLE, "", False), dict(connected=True, card_state=2), "jog"),
    (PhaseState(P.IDLE, "", False), dict(connected=True, card_state=4), "process"),
    (PhaseState(P.IDLE, "", False), dict(connected=True, poll_failing=True), "offline"),
    (PhaseState(P.IDLE, "", False), dict(connected=True, breakpoint=True), "pause"),
    (PhaseState(P.STARTING, "dry_run", True, True), dict(connected=True), "dry_run"),
    (PhaseState(P.RUNNING, "frame", False, True), dict(connected=True, worker_alive=True), "frame"),
    (PhaseState(P.RUNNING, "frame", False, True), dict(connected=True, worker_alive=False), "ready"),
    (PhaseState(P.HOLDING, "process", False, True), dict(connected=True, worker_alive=True), "pause"),
    (PhaseState(P.STOPPING, "process", False, True), dict(connected=True, worker_alive=True), "pause"),
    (PhaseState(P.STOPPING, "", False, False), dict(connected=True, worker_alive=True), "ready"),
    (PhaseState(P.PAUSING, "", False, False), dict(connected=True, breakpoint=True), "pause"),
])
def test_the_mode_is_derived_from_the_phase_and_the_card(st, kw, mode):
    kw = dict(dict(worker_alive=False, breakpoint=False), **kw)
    assert derive_mode(st, **kw) == mode


# ---------------------------------------------------------------------------- the controller
def test_phase_and_mode_through_a_job(seam_ctl):
    ctl, clock = seam_ctl()
    assert ctl.job_phase() is P.IDLE and ctl.state().mode == "ready"
    mid_job(ctl, clock, rect_doc(w=20.0, h=20.0))
    assert ctl.job_phase() is P.RUNNING and ctl.state().mode == "process"
    assert not ctl._held()
    clock.release()
    assert ctl.wait_job(30)
    assert ctl.job_phase() is P.IDLE and ctl.state().mode == "ready"
    assert ctl.fsm.state == SafetyState.DISARMED     # the cut disarmed (wave 1)


def test_hold_then_stop_while_held(seam_ctl):
    ctl, clock = seam_ctl()
    mid_job(ctl, clock, rect_doc())
    ctl.pause()
    assert ctl.job_phase() is P.PAUSING and ctl._held()
    ctl.pause()                                     # a second Hold changes nothing
    assert ctl.job_phase() is P.PAUSING
    clock.release()
    assert ctl.wait_job(30)
    assert ctl._lc.wait_for(lambda st: st.phase is P.IDLE, 30)
    assert ctl.job_phase() is P.AT_BREAKPOINT and ctl.state().mode == "pause"
    assert ctl.fsm.state == SafetyState.PAUSED and ctl.fsm.armed
    assert ctl.stop().wait(30)
    assert ctl.job_phase() is P.IDLE and ctl.breakpoint is None and ctl.kept_breakpoint is not None
    assert ctl.state().mode == "ready" and not ctl.fsm.armed


def test_stop_holds_the_streamer_then_ends_the_phase(seam_ctl):
    ctl, clock = seam_ctl()
    mid_job(ctl, clock, rect_doc())
    done = ctl.stop()
    assert ctl.job_phase() is P.STOPPING and ctl._held()   # set inline, before the halt thread
    ctl.pause()                                     # a Hold during the Stop does nothing
    assert ctl.job_phase() is P.STOPPING
    clock.release()
    assert done.wait(30) and ctl.wait_job(30)
    assert ctl.job_phase() is P.IDLE and ctl.state().mode == "ready"
    assert ctl.breakpoint is None and not ctl.job_active()
    halts = {h.reason for h in ctl._sup.halts if h.ok}
    # the Stop's own halt, and the worker's end halt with its off commands, both went out
    assert {"stop", "process ended: stopped by operator"} <= halts, halts


def test_fifo_hold_is_holding_and_a_stop_ends_it(seam_ctl):
    ctl, clock = seam_ctl(controller_kw=dict(pause_mode="fifo"))
    mid_job(ctl, clock, rect_doc())
    ctl.pause()
    assert ctl.job_phase() is P.HOLDING and ctl.state().mode == "pause"
    assert ctl.fsm.state == SafetyState.PAUSED
    done = ctl.stop()
    assert ctl.job_phase() is P.STOPPING
    clock.release()                                 # the held worker keeps polling the card
    assert done.wait(30) and ctl.wait_job(30)
    assert ctl._lc.wait_for(lambda st: st.phase is P.IDLE, 30)
    assert ctl.state().mode == "ready"


def test_after_a_link_fault_is_reset_the_mode_is_ready_again(seam_ctl):
    """Wave-1 leftover of arch A1: the poll's "offline" outlived the blackout and the reset
    (``elif s.mode != "offline"``), so the status chip showed the card offline and the idle
    replan stopped until the next jog, home or job."""
    ctl, clock = seam_ctl(arm=False)
    ctl.sim.link_down(True)
    for _ in range(3):                              # each read waits out the retry policy
        assert not ctl._poll_once()
    assert ctl.fsm.state == SafetyState.FAULT and "link lost" in ctl.fsm.fault
    assert ctl.state().mode == "offline" and not ctl.state().link_ok
    ctl.sim.link_down(False)
    assert ctl._poll_once()
    clock.advance(0.2)                              # the monitor's next tick: link restored
    assert ctl.state().link_ok and ctl.state().mode == "ready"
    assert ctl.reset_fault()
    assert ctl.state().mode == "ready" and ctl.fsm.state == SafetyState.DISARMED
    assert ctl._poll_once() and ctl.state().mode == "ready"


def test_fault_reset_survives_an_os_error_from_the_follower_read(seam_ctl, monkeypatch):
    """Wave-1 leftover: an OSError from the follower read escaped from ``reset_fault``."""
    ctl, clock = seam_ctl(arm=False)
    monkeypatch.setattr(ctl, "_zf_type", lambda: 1)
    ctl.raise_fault("test fault")
    real = ctl._mc.read_regs

    def read_regs(addr, n):
        from mlaser.protocol import frames as F
        if addr == F.REG_ONBZF:
            raise OSError(9, "Bad file descriptor")
        return real(addr, n)
    monkeypatch.setattr(ctl._mc, "read_regs", read_regs)
    assert ctl.interlocks.cfg.follower
    assert ctl.reset_fault() is True                # no follower alarm was held: unknown passes
    assert ctl.fsm.state == SafetyState.DISARMED


def test_a_held_follower_alarm_still_blocks_the_reset_when_the_read_fails(seam_ctl, monkeypatch):
    """Unknown is not clear: a follower alarm the interlock holds keeps holding."""
    ctl, clock = seam_ctl(arm=False)
    monkeypatch.setattr(ctl, "_zf_type", lambda: 1)
    ctl.interlocks.active["follower"] = "follower alarm 0x00000001 (test)"
    ctl.raise_fault("follower alarm")

    from mlaser.protocol import frames as F
    real = ctl._mc.read_regs

    def read_regs(addr, n):
        if addr == F.REG_ONBZF:
            raise OSError(9, "Bad file descriptor")
        return real(addr, n)
    monkeypatch.setattr(ctl._mc, "read_regs", read_regs)
    assert ctl.reset_fault() is False
    assert ctl.fsm.state == SafetyState.FAULT
