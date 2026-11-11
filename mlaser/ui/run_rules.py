"""The run rules, without Qt (review 2026-11-09, arch A15): which run controls are enabled and
why (:func:`run_enabled`), the machine's run state (:func:`run_state`), the alarm source of both
(:func:`active_alarms`) and whether a job runs (:func:`job_running`).  The run bar, the Run
screen, the state display and the panels all decide from here; :mod:`mlaser.ui.safety_ui`
re-exports every name, so ``from mlaser.ui.safety_ui import run_enabled`` keeps working.

Plain functions of the controller's status (``st``: the safety status, ``ms``: the machine
state, the card's alarms) and the window's catalogue (``window.tr_``); they can be tested
without a widget.
"""
from __future__ import annotations

import logging

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


def job_running(window) -> bool:
    ctl = _ctl(window)
    if ctl is None:
        return False
    if ctl.job_active():
        return True
    return ctl.state().mode in ("process", "dry_run", "frame")


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


def run_enabled(window, st, ms, running: bool, alarms=None) -> list[tuple[str, bool, str]]:
    """(run control, enabled, why not) for the run bar and the Run screen: one rule set.
    STOP is always enabled.  ``alarms``: the card's active alarms (None: read from the
    controller, :func:`active_alarms`): while one is active Start, Frame and Dry run are
    disabled with "Reset the alarm first: <alarm>" (UX audit C4), and so is Arm while an
    e-stop or servo alarm is active (C3); while the machine homes or makes a go-to move they are
    disabled with "Homing in progress" / "The machine is moving" (UX review 2026-11 #3)."""
    connected = bool(ms is not None and ms.connected)
    fault = st is not None and st.banner == "FAULT"
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
