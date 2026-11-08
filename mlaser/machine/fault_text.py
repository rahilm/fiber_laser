"""Plain-language headlines of latched faults (UX audit W12, docs/SAFETY.md §10).

A fault reason is an internal English string (:meth:`UiController.raise_fault`): "link lost: no
reply from the card for 1502 ms (idle, deadline 1500 ms)", "process aborted: underrun: only 80 ms
of motion buffered at wire 5960".  It stays the fault's detail (the log file, the audit log, the
alarm banner's *Details*); the operator reads :func:`headline`: "Lost connection to the motion
card".  The matching is fixed here (reason prefix or word -> catalogue id ``fault.headline.*``)
and does not depend on the language.
"""
from __future__ import annotations

from .. import i18n as _i18n

#: (words found in the lower-case reason, catalogue id), matched in this order
HEADLINE_IDS = (
    ("link lost", "fault.headline.link_lost"),
    ("interlock door", "fault.headline.interlock_door"),
    ("interlock gas", "fault.headline.interlock_gas"),
    ("interlock laser", "fault.headline.interlock_laser"),
    ("interlock follower", "fault.headline.interlock_follower"),
    ("interlock", "fault.headline.interlock"),
    ("emergency stop", "fault.headline.estop"),
    ("card alarm", "fault.headline.card_alarm"),
    ("underrun", "fault.headline.underrun"),
    ("laser off failed", "fault.headline.laser_off_failed"),
    ("laser_off_failed", "fault.headline.laser_off_failed"),
    ("streaming process", "fault.headline.streaming"),
    ("streaming child", "fault.headline.streaming"),
    ("child_", "fault.headline.streaming"),
    ("refused", "fault.headline.refused"),
    ("unexpected error", "fault.headline.software"),
    ("uncaught exception", "fault.headline.software"),
)
#: any other reason
DEFAULT_ID = "fault.headline.default"


def headline_id(reason: str) -> str:
    """The catalogue id of the headline for ``reason``."""
    low = (reason or "").lower()
    for words, sid in HEADLINE_IDS:
        if words in low:
            return sid
    return DEFAULT_ID


def headline(reason: str, tr=None) -> str:
    """"Lost connection to the motion card" for "link lost: no reply ... deadline 1500 ms";
    ``tr``: the UI translator (``tr(id)``), else the process-wide catalogue."""
    sid = headline_id(reason)
    if tr is not None:
        try:
            s = tr.tr(sid)
            if s and s != sid:
                return s
        except Exception:  # noqa: BLE001 - a broken catalogue never hides a safety text
            pass
    s = _i18n.tr(sid)
    return s if s and s != sid else (reason or sid)


__all__ = ["headline", "headline_id", "HEADLINE_IDS", "DEFAULT_ID"]
