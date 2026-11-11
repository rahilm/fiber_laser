"""The UI's one answer to "what is the job doing" (review 2026-11-09, arch A1, UI half):
:class:`mlaser.ui.run_rules.JobView` from ``ctl.job_phase()``.  Both controllers give the same
answers for the same situation: the demo table and the card controller (on the unit seam: the
in-memory card simulator, a manual clock) are walked through start / hold / resume / stop /
fault and their predicates and run controls compared at every step."""
from __future__ import annotations

from types import SimpleNamespace as NS

from mlaser.machine.demo import DemoController
from mlaser.machine.jobphase import JobPhase as P
from mlaser.ui import run_rules as R

from seam_helpers import mid_job, rect_doc, settle


def _answers(ctl) -> dict:
    """The predicates and the run controls the UI derives from ``ctl``."""
    v = R.job_view(ctl)
    ms = ctl.state()
    win = NS(ctl=ctl)
    on = {k: ok for k, ok, _ in R.run_enabled(win, ctl.safety(), ms, v, alarms=[])
          if k in ("start", "hold", "stop", "frame", "dry_run")}
    if v.held and on["hold"]:
        on["hold"] = False                  # the run bar's "Held: Start continues the job"
    return dict(owns_card=v.owns_card, held=v.held, running=v.running, moving=v.moving, idle=v.idle,
                **{f"btn_{k}": ok for k, ok in on.items()})


class _Demo:
    """The demo table on a clock that moves only when the test says so."""

    def __init__(self) -> None:
        self.t = [100.0]
        self.ctl = DemoController(clock=lambda: self.t[0])
        assert self.ctl.connect()
        assert self.ctl.arm()


def test_demo_and_card_controller_answer_alike_through_a_job(seam_ctl):
    doc = rect_doc(w=200.0, h=200.0)
    demo = _Demo()
    d = demo.ctl
    ctl, clock = seam_ctl()
    settle(ctl, clock)
    assert _answers(d) == _answers(ctl), "idle"
    assert _answers(ctl)["idle"] and _answers(ctl)["btn_start"] and not _answers(ctl)["btn_hold"]

    # start: a job owns the card
    d.start(doc)
    mid_job(ctl, clock, doc)
    assert d.job_phase() is P.RUNNING and ctl.job_phase() is P.RUNNING
    a = _answers(ctl)
    assert a == _answers(d), "running"
    assert a["owns_card"] and a["moving"] and not a["held"] and not a["idle"]
    assert not a["btn_start"] and a["btn_hold"] and a["btn_stop"] and not a["btn_frame"]

    # hold: the demo holds in place (HOLDING), the card controller takes a breakpoint
    d.pause()
    ctl.pause()
    clock.release()
    assert ctl.wait_job(30)
    assert ctl._lc.wait_for(lambda st: st.phase is P.IDLE, 30)
    settle(ctl, clock)
    assert d.job_phase() is P.HOLDING and ctl.job_phase() is P.AT_BREAKPOINT
    a = _answers(ctl)
    assert a == _answers(d), "held"
    assert a["held"] and a["running"] and not a["owns_card"] and not a["moving"]
    assert a["btn_start"] and not a["btn_hold"] and a["btn_stop"] and not a["btn_dry_run"]

    # resume: running again
    d.resume()
    clock.hold_after(0.05)
    ctl.resume("exact", doc)
    assert clock.wait_parked(), "the resumed job did not reach the gate"
    assert ctl._lc.wait_for(lambda st: st.phase is P.RUNNING, 30)
    assert d.job_phase() is P.RUNNING
    assert _answers(ctl) == _answers(d), "resumed"

    # stop: the card controller's STOPPING is a job owning the card, never "held"
    d.stop()
    done = ctl.stop()
    assert ctl.job_phase() is P.STOPPING
    a = _answers(ctl)
    assert a["owns_card"] and not a["held"] and not a["btn_start"] and a["btn_hold"] and a["btn_stop"]
    clock.release()
    assert done.wait(30) and ctl.wait_job(30)
    settle(ctl, clock)
    assert d.job_phase() is P.IDLE and ctl.job_phase() is P.IDLE
    assert _answers(ctl) == _answers(d), "stopped"
    assert _answers(ctl)["idle"]

    # fault during a job: the job ends, nothing is held, Start is off (the fault)
    assert d.arm() and ctl.arm()
    d.start(doc)
    mid_job(ctl, clock, doc)
    d.raise_fault("test fault")
    ctl.raise_fault("test fault")
    clock.release()
    assert ctl.wait_job(30)
    assert ctl._lc.wait_for(lambda st: st.phase is P.IDLE, 30)
    settle(ctl, clock)
    # the card controller keeps a resume point at a fault ("stopped at a fault": AT_BREAKPOINT,
    # held); the demo table has none (IDLE).  Not the same situation, but the same run
    # controls: nothing owns the card or moves, Start / Hold / Frame / Dry run off, Stop on
    assert d.job_phase() is P.IDLE and ctl.job_phase() in (P.IDLE, P.AT_BREAKPOINT)
    a, b = _answers(ctl), _answers(d)
    same = [k for k in a if k.startswith("btn_")] + ["owns_card", "moving"]
    assert {k: a[k] for k in same} == {k: b[k] for k in same}, "fault"
    assert not a["owns_card"] and not a["btn_start"] and not a["btn_hold"] and a["btn_stop"]
    assert not a["btn_frame"] and not a["btn_dry_run"]


def test_a_restored_resume_point_counts_as_a_held_job(seam_ctl):
    """Changed with arch A1: a resume point restored after a Stop (UX audit W17) is held "as
    after a Hold", but the safety FSM is idle, so ``job_active()`` is False and the old
    ``job_running`` said no job: the document was not locked and Frame / Dry run were
    offered.  The phase says AT_BREAKPOINT: held, like any other breakpoint."""
    ctl, clock = seam_ctl()
    doc = rect_doc(w=200.0, h=200.0)
    mid_job(ctl, clock, doc)
    ctl.pause()
    clock.release()
    assert ctl.wait_job(30) and ctl._lc.wait_for(lambda st: st.phase is P.IDLE, 30)
    assert ctl.stop().wait(30)
    settle(ctl, clock)
    assert ctl.kept_breakpoint is not None and ctl.restore_kept_breakpoint()
    assert ctl.job_phase() is P.AT_BREAKPOINT and not ctl.job_active()
    win = NS(ctl=ctl)
    assert R.job_running(win) and R.job_held(win) and not R.job_owns_card(win)
    on = {k: ok for k, ok, _ in R.run_enabled(win, ctl.safety(), ctl.state(), R.job_view(ctl), alarms=[])}
    assert on["start"] and on["stop"] and not on["frame"] and not on["dry_run"]
