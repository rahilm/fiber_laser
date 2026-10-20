"""Where a job ran: on the real machine, the simulator, as a dry run or as a practice lesson.

Review 2 (features P1-7): a simulator sheet used to be recorded as production (29 s actual
against a 398 s estimate gave "actual / estimated 7 %", and it counted towards the device's
processing hours and the planned quantity).  Every job record now carries its **source**, and
the reports, counters and notifications count only ``real`` jobs unless asked otherwise.

=============  ==========================================================================
source         when
=============  ==========================================================================
``practice``   a practice lesson is active (:mod:`mlaser.services.practice`)
``dry_run``    a dry run (laser off: the Dry run button, ``--laser-off``, a disabled laser)
``simulator``  the controller is the in-process card simulator or the demo controller
``real``       everything else: a cut on a real card
=============  ==========================================================================

The order is the precedence: a practice lesson on the simulator is ``practice``, a dry run on
the simulator is ``dry_run``.  An external simulator reached over the network (``mlaser-cardsim``
in another process) looks like a real card to the program and is recorded as ``real``.

Qt-free; the functions only read the controller's public :class:`~mlaser.machine.api.MachineAPI`
members (``is_simulator``, ``laser_enabled``).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

REAL, SIMULATOR, DRY_RUN, PRACTICE = "real", "simulator", "dry_run", "practice"
SOURCES = (REAL, SIMULATOR, DRY_RUN, PRACTICE)
#: what the reports, counters and notifications count by default
PRODUCTION: tuple[str, ...] = (REAL,)


def job_source(ctl: Any, mode: str = "process", practice: bool = False,
               dry_run: Optional[bool] = None) -> str:
    """The source of a job started now in ``mode`` on ``ctl``.  ``dry_run``: the plan's own
    flag when known (a cut compiled laser-off)."""
    if practice:
        return PRACTICE
    if mode == "dry_run" or dry_run:
        return DRY_RUN
    try:
        if not bool(getattr(ctl, "laser_enabled", True)):
            return DRY_RUN
    except Exception:  # noqa: BLE001 - an unknown controller: judge by the rest
        pass
    try:
        fn = getattr(ctl, "is_simulator", None)
        if callable(fn) and fn():
            return SIMULATOR
    except Exception:  # noqa: BLE001
        pass
    return REAL


def practice_active(window: Any) -> bool:
    """True while a practice lesson runs (the practice plugin's ``practice.active`` provider)."""
    hooks = getattr(window, "hooks", None)
    try:
        fn = hooks.provider("practice.active") if hooks is not None else None
        return bool(fn()) if fn is not None else False
    except Exception:  # noqa: BLE001 - no practice plugin, or it failed: not practising
        return False


def window_job_source(window: Any, mode: str = "process") -> str:
    """:func:`job_source` for the application window's controller and practice state."""
    run = getattr(getattr(window, "jobs", None), "last_run", None)
    plan = getattr(run, "plan", None) if run is not None else None
    dry = None
    if plan is not None:
        try:
            dry = bool(plan.dry_run) if mode == "process" else None
        except Exception:  # noqa: BLE001
            dry = None
    return job_source(getattr(window, "ctl", None), mode, practice_active(window), dry)


def counted(source: str, include: Iterable[str] = PRODUCTION) -> bool:
    """Whether a job of ``source`` counts for a report / counter that includes ``include``.
    A record written before sources existed has none and counts as ``real``."""
    return (source or REAL) in tuple(include)


__all__ = ["REAL", "SIMULATOR", "DRY_RUN", "PRACTICE", "SOURCES", "PRODUCTION", "job_source",
           "practice_active", "window_job_source", "counted"]
