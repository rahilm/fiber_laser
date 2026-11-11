"""The run rules, without Qt (review 2026-11-09, arch A15): which run controls are enabled and
why (:func:`run_enabled`), the machine's run state (:func:`run_state`), the alarm source of both
(:func:`active_alarms`) and what the job is doing (:class:`JobView` from ``ctl.job_phase()``,
review 2026-11-09 arch A1: :func:`job_running`, :func:`job_owns_card`, :func:`job_held`,
:func:`machine_moving`, :func:`machine_idle`).  The run bar, the Run
screen, the state display and the panels all decide from here; :mod:`mlaser.ui.safety_ui`
re-exports every name, so ``from mlaser.ui.safety_ui import run_enabled`` keeps working.

Plain functions of the controller's status (``st``: the safety status, ``ms``: the machine
state, the card's alarms) and the window's catalogue (``window.tr_``); they can be tested
without a widget.
"""
from __future__ import annotations

import logging
from typing import NamedTuple, Optional

from ..machine.jobphase import JobPhase

log = logging.getLogger("mlaser.ui.safety")

JOB_MODES = ("process", "dry_run", "frame", "pause")


def _tr(window, key: str, default: str, **values) -> str:
    """Catalogue text of ``key`` (``default`` without a catalogue), placeholders filled."""
    tr = getattr(window, "tr_", None)
    text = default
    if tr is not None:
        try:
            if tr.has(key):
                return tr.format(key, **values) if values else tr.tr(key)
        except Exception:  # noqa: BLE001 - a broken catalogue never hides a safety text
            pass
    return text.format(**values) if values else text


def _ctl(w):
    return getattr(w, "ctl", None)


def _safety(ctl):
    return ctl.safety() if ctl is not None else None


# ---------------------------------------------------------------------------- the job's phase
#: a job owns the card: it is being started, streams, or is being stopped
OWNS_CARD = frozenset({JobPhase.STARTING, JobPhase.RUNNING, JobPhase.STOPPING})
#: a job is held: the FIFO hold, the breakpoint being taken, or held at a breakpoint
HELD = frozenset({JobPhase.HOLDING, JobPhase.PAUSING, JobPhase.AT_BREAKPOINT})
#: the modes in which the card itself streams a job
_CARD_JOB_MODES = frozenset({"process", "dry_run", "frame"})
#: the modes in which the card moves the axes (a job, homing, a jog or a go-to move)
_MOVING_MODES = _CARD_JOB_MODES | {"origin", "jog"}


class JobView(NamedTuple):
    """One reading of "what is the job doing" (review 2026-11-09, arch A1): THE answer for the
    run controls, the document lock, the run keys, the queue and the panels.  Built from
    ``ctl.job_phase()`` (:mod:`mlaser.machine.jobphase`), ``ctl.job_active()`` (the safety FSM
    still counts a job: the end-of-job steps after the worker's end) and the machine state's
    mode (the card itself reports motion).

    ``phase`` None: the phase cannot be read.  The view is then restrictive, as
    :func:`active_alarms` is for alarms: the run controls are held as for a running job (Start,
    Frame and Dry run off; Hold and Stop on), never the permissive "idle"."""
    phase: Optional[JobPhase]
    active: bool = False
    mode: str = ""
    connected: bool = False

    @property
    def owns_card(self) -> bool:
        """A job owns the card: starting, running or stopping (also: the phase cannot be read,
        or the safety FSM or the card still report a job after the phase went idle)."""
        p = self.phase
        if p is None or p in OWNS_CARD:
            return True
        return p is JobPhase.IDLE and (self.active or self.mode in _CARD_JOB_MODES)

    @property
    def held(self) -> bool:
        """A job is held: Hold (FIFO), the breakpoint being taken, or at a breakpoint."""
        return self.phase in HELD

    @property
    def running(self) -> bool:
        """A job is in progress (it owns the card or is held): the document is locked, the
        queue loads nothing, the run keys are the job's."""
        return self.owns_card or self.held

    @property
    def moving(self) -> bool:
        """The machine moves on its own: a job owns the card, it homes, or it jogs / makes a
        go-to move."""
        return self.owns_card or self.mode in _MOVING_MODES

    @property
    def idle(self) -> bool:
        """Connected, no job (not even a held one) and nothing moving: ready."""
        return self.connected and not self.running and self.mode == "ready"


def job_view(ctl, ms=None) -> JobView:
    """The :class:`JobView` of the controller ``ctl`` (``ms``: its machine state when already
    read).  No controller: no job.  A read that fails is restrictive: phase None (as for a
    running job), ``job_active`` True."""
    if ctl is None:
        return JobView(JobPhase.IDLE, False, "offline", False)
    phase: Optional[JobPhase]
    try:
        phase = ctl.job_phase()
    except Exception:  # noqa: BLE001 - cannot tell: held as for a running job (restrictive)
        log.warning("the controller's job phase cannot be read: run controls held as for a running job",
                    exc_info=True)
        phase = None
    try:
        active = bool(ctl.job_active())
    except Exception:  # noqa: BLE001 - cannot tell: a job counts (restrictive)
        active = True
    if ms is None:
        try:
            ms = ctl.state()
        except Exception:  # noqa: BLE001 - no state: not connected
            ms = None
    mode = str(getattr(ms, "mode", "") or "") if ms is not None else ""
    connected = bool(getattr(ms, "connected", False)) if ms is not None else False
    return JobView(phase, active, mode, connected)


def job_running(window) -> bool:
    """A job is in progress (starting, running, held or being stopped): the one answer
    (:attr:`JobView.running`)."""
    return job_view(_ctl(window)).running


def job_owns_card(window) -> bool:
    """A job starts, streams or is being stopped, not held (:attr:`JobView.owns_card`)."""
    return job_view(_ctl(window)).owns_card


def job_held(window) -> bool:
    """A job is held: Hold, the breakpoint being taken, at a breakpoint (:attr:`JobView.held`)."""
    return job_view(_ctl(window)).held


def machine_moving(window) -> bool:
    """The machine moves on its own: a job, homing, a jog (:attr:`JobView.moving`)."""
    return job_view(_ctl(window)).moving


def machine_idle(window) -> bool:
    """Connected, no job, nothing moving (:attr:`JobView.idle`)."""
    return job_view(_ctl(window)).idle


def active_alarms(window) -> list:
    """The card's active alarms: THE alarm source of the run state, the banners, the status
    chips and the run controls' enabling (``ctl.active_alarms``, re-read after every status and
    after a reset; UX audit C2)."""
    ctl = _ctl(window)
    try:
        return list(ctl.active_alarms or ()) if ctl is not None else []
    except Exception:  # noqa: BLE001 - cannot tell: assume an alarm (review A14, restrictive)
        log.warning("the controller's alarm list cannot be read: run controls held as for an alarm",
                    exc_info=True)
        return [_unknown_alarm()]


def _unknown_alarm():
    """Stands for alarms that cannot be read: Start, Frame and Dry run stay disabled ("Reset
    the alarm first: alarm state unknown"), never the permissive "no alarm"."""
    from ..controller.alarms import Alarm
    return Alarm(0, "safety.alarm.unknown", "Alarm state unknown: the controller does not report it")


def alarm_text(window, a) -> str:
    """An alarm's text in the UI language."""
    tr = getattr(window, "tr_", None)
    return a.text(tr) if hasattr(a, "text") else str(a)


def run_state(st, ms, alarms=(), need_home: bool = False) -> str:
    """The machine's run state from the safety status ``st``, the machine state ``ms`` and the
    card's active ``alarms``: ``fault`` > ``estop`` (an emergency-stop alarm) > ``alarm`` >
    ``offline`` > ``hold`` > ``running`` > ``homing`` > ``moving`` (a go-to move or a jog) >
    ``not_homed`` > ``ready``.  Never ``ready`` while an alarm is active (UX audit C2/C3/W3),
    nor while the axes move (UX review 2026-11 #3)."""
    if st is not None and st.banner == "FAULT":
        return "fault"
    alarms = list(alarms or ())
    if any(getattr(a, "kind", "") == "estop" for a in alarms):
        return "estop"
    if alarms:
        return "alarm"
    if ms is None or not ms.connected:
        return "offline"
    if ms.mode == "pause":
        return "hold"
    if ms.mode in ("process", "dry_run", "frame"):
        return "running"
    if ms.mode == "origin":
        return "homing"
    if ms.mode == "jog":
        return "moving"
    if need_home:
        return "not_homed"
    return "ready"


def run_enabled(window, st, ms, running: "bool | JobView", alarms=None) -> list[tuple[str, bool, str]]:
    """(run control, enabled, why not) for the run bar and the Run screen: one rule set.
    STOP is always enabled.  ``running``: the :class:`JobView` (a job in progress, held or
    not: :attr:`JobView.running`, :attr:`JobView.held`); a plain bool (a caller with only the
    machine state) takes "held" from the mode.  ``alarms``: the card's active alarms (None: read from the
    controller, :func:`active_alarms`): while one is active Start, Frame and Dry run are
    disabled with "Reset the alarm first: <alarm>" (UX audit C4), and so is Arm while an
    e-stop or servo alarm is active (C3); while the machine homes or makes a go-to move they are
    disabled with "Homing in progress" / "The machine is moving" (UX review 2026-11 #3)."""
    connected = bool(ms is not None and ms.connected)
    fault = st is not None and st.banner == "FAULT"
    if isinstance(running, JobView):
        paused, running = running.held, running.running
    else:
        paused = ms is not None and ms.mode == "pause"
    dry = bool(getattr(st, "dry_run", False))
    if alarms is None:
        alarms = active_alarms(window)
    alarms = list(alarms or ())
    no_conn = _tr(window, "run.why.offline", "Connect the machine or the simulator first")
    why_fault = _tr(window, "run.why.fault", "Reset the fault first")
    why_running = _tr(window, "run.why.running", "Not while a job runs")
    why_alarm = (_tr(window, "run.why.alarm", "Reset the alarm first: {alarm}", alarm=alarm_text(window, alarms[0]))
                 if alarms else "")
    from ..controller.alarms import DISARM_KINDS
    arm_alarm = next((a for a in alarms if getattr(a, "kind", "") in DISARM_KINDS), None)
    mode = getattr(ms, "mode", "") if ms is not None else ""
    why_motion = {"origin": _tr(window, "run.why.homing", "Homing in progress"),
                  "jog": _tr(window, "run.why.moving", "The machine is moving")}.get(mode, "")

    def reason(*checks: tuple[bool, str]) -> tuple[bool, str]:
        for bad, why in checks:
            if bad:
                return False, why
        return True, ""
    out = []
    ok, why = reason((not connected, no_conn), (fault, why_fault), (bool(alarms), why_alarm),
                     (running and not paused, why_running), (bool(why_motion), why_motion))
    out.append(("start", ok, why))
    ok, why = reason((not connected, no_conn), (not running, _tr(window, "run.why.no_job", "No job is running")))
    out.append(("hold", ok, why))
    out.append(("stop", True, ""))
    for k in ("frame", "dry_run"):
        ok, why = reason((not connected, no_conn), (fault, why_fault), (bool(alarms), why_alarm), (running, why_running),
                         (bool(why_motion), why_motion))
        out.append((k, ok, why))
    armed = bool(getattr(st, "armed", False))
    ok, why = reason((not connected, no_conn), (fault, why_fault),
                     (dry, _tr(window, "run.why.laser_off", "Laser output is disabled (dry-run mode)")),
                     (arm_alarm is not None and not armed,           # Disarm always stays possible
                      _tr(window, "run.why.alarm", "Reset the alarm first: {alarm}",
                          alarm=alarm_text(window, arm_alarm)) if arm_alarm is not None else ""))
    out.append(("arm", ok, why))
    return out
