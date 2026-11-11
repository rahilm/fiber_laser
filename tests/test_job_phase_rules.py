"""The UI asks "what is the job doing" one way (review 2026-11-09, arch A1, UI half):
:class:`mlaser.ui.run_rules.JobView` from ``ctl.job_phase()``.  The predicates per phase, the
restrictive answer when the phase cannot be read, and each UI site whose behaviour changed when
it moved from the ``mode`` string to the phase (most visibly: during a Stop the mode reads
"pause", which made the run controls treat the stopping job as held)."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from mlaser.machine.jobphase import JobPhase as P
from mlaser.ui import run_rules as R

#: the mode the card controller shows in each phase while the worker runs (derive_mode)
MODE = {P.IDLE: "ready", P.STARTING: "process", P.RUNNING: "process", P.HOLDING: "pause",
        P.PAUSING: "pause", P.STOPPING: "pause", P.AT_BREAKPOINT: "pause"}


class Ctl:
    """A controller reduced to what the job view reads."""

    def __init__(self, phase=P.IDLE, mode=None, active=None, breakpoint=None):
        self.phase = phase
        self.mode = MODE[phase] if mode is None and phase is not None else (mode or "ready")
        self.active = (phase not in (P.IDLE, None)) if active is None else active
        self.breakpoint = breakpoint
        self.calls: list = []

    def job_phase(self):
        if self.phase is None:
            raise RuntimeError("the phase cannot be read")
        return self.phase

    def job_active(self):
        return self.active

    def state(self):
        return NS(mode=self.mode, connected=True, homed=True, x=0.0, y=0.0)

    def safety(self):
        return NS(banner="ARMED", armed=True, dry_run=False)

    def pause(self):
        self.calls.append("pause")

    def resume(self, mode="exact", document=None):
        self.calls.append("resume")


def _view(phase, **kw):
    return R.job_view(Ctl(phase, **kw))


@pytest.mark.parametrize("phase, owns, held", [
    (P.IDLE, False, False), (P.STARTING, True, False), (P.RUNNING, True, False),
    (P.STOPPING, True, False), (P.HOLDING, False, True), (P.PAUSING, False, True),
    (P.AT_BREAKPOINT, False, True)])
def test_the_predicates_per_phase(phase, owns, held):
    v = _view(phase)
    assert (v.owns_card, v.held) == (owns, held)
    assert v.running == (owns or held)
    assert v.moving == owns
    assert v.idle == (phase is P.IDLE)


def test_motion_without_a_job_moves_but_is_no_job():
    for mode in ("origin", "jog"):
        v = _view(P.IDLE, mode=mode)
        assert v.moving and not v.running and not v.owns_card and not v.idle


def test_the_end_of_a_job_after_the_phase_went_idle_still_owns_the_card():
    """The worker's end makes the phase IDLE before the safety FSM's job end: until then
    ``job_active()`` is True and Start stays off (as the old ``job_running`` had it)."""
    assert _view(P.IDLE, active=True).owns_card
    assert _view(P.IDLE, mode="process", active=False).owns_card      # the card says it streams


def test_an_unreadable_phase_is_restrictive():
    """As run_rules.active_alarms for alarms: held as for a running job, Hold and Stop on."""
    win = NS(ctl=Ctl(None, mode="process"))
    v = R.job_view(win.ctl)
    assert v.phase is None and v.owns_card and v.running and v.moving and not v.held and not v.idle
    assert R.job_running(win) and R.machine_moving(win) and not R.machine_idle(win)
    on = {k: ok for k, ok, _ in R.run_enabled(win, win.ctl.safety(), win.ctl.state(), v, alarms=[])}
    assert on == dict(start=False, hold=True, stop=True, frame=False, dry_run=False, arm=True)


def test_no_controller_no_job():
    win = NS(ctl=None)
    assert not R.job_running(win) and not R.job_held(win) and not R.machine_moving(win)


def test_run_controls_while_stopping():
    """Changed (run bar and Run screen): during a Stop the mode reads "pause" (the streamer's
    hold), so Start was enabled as "Continue" and Hold disabled as "Held".  From the phase:
    the stopping job owns the card: Start off ("Not while a job runs"), Hold and Stop on."""
    ctl = Ctl(P.STOPPING)
    win = NS(ctl=ctl)
    ms = ctl.state()
    old = {k: ok for k, ok, _ in R.run_enabled(win, ctl.safety(), ms, True, alarms=[])}
    assert old["start"]                                    # the mode-only answer
    new = {k: (ok, why) for k, ok, why in R.run_enabled(win, ctl.safety(), ms, R.job_view(ctl), alarms=[])}
    assert new["start"] == (False, "Not while a job runs")
    assert new["hold"][0] and new["stop"][0] and not new["frame"][0] and not new["dry_run"][0]


def test_hold_during_a_stop_goes_to_the_controller():
    """Changed (SafetyUI.hold): during a Stop the mode reads "pause", so Hold only said "Held:
    Start continues the job".  The Hold now goes out (the controller ignores it while stopping);
    a held job keeps the message."""
    from mlaser.ui.safety_ui import SafetyUI
    msgs: list = []
    bar = NS(showMessage=lambda text, ms=0: msgs.append(text))
    for phase, called in ((P.STOPPING, True), (P.RUNNING, True), (P.HOLDING, False), (P.AT_BREAKPOINT, False),
                          (None, True)):
        ctl = Ctl(phase)
        SafetyUI.hold(NS(w=NS(ctl=ctl, statusBar=lambda: bar)))
        assert (ctl.calls == ["pause"]) == called, phase


def test_start_during_a_stop_is_not_a_resume():
    """Changed (SafetyUI.start): the "pause" mode of a Stop sent Start to ``ctl.resume`` (which
    refused it).  A held job or a breakpoint still resumes; a stopping job is no held job."""
    from mlaser.ui.safety_ui import SafetyUI

    class Self:
        def __init__(self, ctl):
            self.w = NS(ctl=ctl, document=object())
            self.checked: list = []

        def _after(self, _text):
            pass

        def _dialogs(self):
            return False

        def _preflight_async(self, *a, **kw):
            self.checked.append(a[0])

    for phase, bp, resumed in ((P.STOPPING, None, False), (P.HOLDING, None, True),
                               (P.AT_BREAKPOINT, object(), True), (P.IDLE, None, False)):
        ctl = Ctl(phase, breakpoint=bp)
        ctl.laser_enabled = True
        s = Self(ctl)
        SafetyUI.start(s)
        assert ("resume" in ctl.calls) == resumed, phase
        assert bool(s.checked) == (not resumed), phase


def test_the_panel_holds_motion_while_a_job_stops():
    """Changed (the machine panel's jog and motion buttons): enabled during a Stop (the mode
    read "pause", where stepping along the path is allowed); off from the phase."""
    from mlaser.ui.panels import ControlPanel
    got: dict = {}

    class Btn:
        def __init__(self, k):
            self.k = k

        def isEnabled(self):
            return None

        def property(self, _name):
            return None

    import mlaser.ui.panels as PN
    orig = PN.style.set_enabled
    PN.style.set_enabled = lambda b, on, why="": got.__setitem__(b.k, on)
    try:
        for phase, motion in ((P.STOPPING, False), (P.RUNNING, False), (P.AT_BREAKPOINT, True),
                              (P.IDLE, True)):
            got.clear()
            ctl = Ctl(phase)
            fake = NS(ctl=ctl, tr_=NS(tr=lambda k: k), jog=NS(buttons={}, held=lambda: False),
                      buttons={k: Btn(k) for k in ControlPanel.MOTION}, MOTION=ControlPanel.MOTION,
                      OUTPUTS=(), release_jogs=lambda *_: None)
            ControlPanel._dock_enabling(fake, ctl.safety(), ctl.state())
            assert got["mp29"] is motion, phase
    finally:
        PN.style.set_enabled = orig


def test_pulse_jog_refused_while_the_machine_homes():
    """Changed (Diagnostics, pulse jog): refused while a job ran; now also while the machine
    homes or jogs ("System is moving")."""
    from mlaser.ui import ext_diag
    cls = next(c for c in vars(ext_diag).values() if isinstance(c, type) and hasattr(c, "send_pulses"))
    for mode, refused in (("origin", True), ("jog", True), ("ready", False)):
        text: list = []
        sent: list = []
        fake = NS(ext=NS(w=NS(ctl=Ctl(P.IDLE, mode=mode), tr_=None)), _writable=lambda: True,
                  status=NS(setText=text.append),
                  pulse_speed=NS(value=lambda: 1.0), axis=NS(currentIndex=lambda: 0),
                  pulses=NS(value=lambda: 10))
        orig = ext_diag._mc
        ext_diag._mc = lambda _w: NS(jog=lambda *a: sent.append(a))
        try:
            cls.send_pulses(fake)
        finally:
            ext_diag._mc = orig
        assert (text == ["System is moving"]) == refused, mode
        assert bool(sent) == (not refused), mode


def test_hold_feedback_and_the_update_check_follow_the_phase():
    from mlaser.ui import command_feedback as CF
    from mlaser.ui import diag_bundle as DB
    assert CF._hold(NS(ctl=Ctl(P.AT_BREAKPOINT)), "run.hold", None) is None
    assert CF._hold(NS(ctl=Ctl(P.IDLE)), "run.hold", None)[0] == "feedback.hold_idle"
    # unreadable: a job counts (the quiet update check waits); it said "no job" before
    assert DB._job_running(NS(ctl=Ctl(None)))
    assert not DB._job_running(NS(ctl=Ctl(P.IDLE)))
