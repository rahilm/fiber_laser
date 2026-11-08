"""Remedy texts for alarms and faults shown in the alarm banner (docs/SAFETY.md §10).

``remedy(alarm)`` returns a short "what to check, then what to press" text for a card alarm
(:class:`mlaser.controller.alarms.Alarm`: by code, then by kind) or a fault reason.  The texts
are catalogue ids (``mlaser/i18n/en.json``): ``remedy.<code>``, ``remedy.<kind>`` and
``remedy.fault.<reason>``, so they are translated like every other string.  The matching (which
text answers which alarm or fault) is fixed here and does not depend on the language.
"""
from __future__ import annotations

from typing import Optional

from mlaser import i18n as _cat

# by alarm kind (controller.alarms.Alarm.kind) -> id
KIND_IDS = {
    "estop": "remedy.estop",
    "limit": "remedy.limit",
    "servo": "remedy.servo",
    "fifo": "remedy.fifo",
    "overspeed": "remedy.overspeed",
    "axis": "remedy.axis",
    "card": "remedy.card",
}

# by alarm code (controller.alarms numbering) -> id
CODE_IDS = {
    8025: "remedy.8025",
    8026: "remedy.8026",
    8029: "remedy.8029",
    8030: "remedy.8030",
    9000: "remedy.9000",
    9001: "remedy.9001",
    9003: "remedy.9003",
    9005: "remedy.9005",
    9007: "remedy.9007",
}

# by fault reason prefix (SafetySupervisor / UiController.raise_fault; internal English reason
# strings, matched in this order) -> id
FAULT_IDS = (
    ("link lost", "remedy.fault.link_lost"),
    ("interlock follower", "remedy.fault.interlock_follower"),
    ("interlock gas", "remedy.fault.interlock_gas"),
    ("interlock laser", "remedy.fault.interlock_laser"),
    ("interlock door", "remedy.fault.interlock_door"),
    ("card alarm", "remedy.fault.card_alarm"),
    ("uncaught exception", "remedy.fault.uncaught_exception"),
    ("laser off failed", "remedy.fault.laser_off_failed"),
)
#: any other fault reason
DEFAULT_ID = "remedy.fault.default"


def _en(sid: str) -> str:
    return _cat.catalogue("en").strings[sid]


# the English texts (compatibility views of the catalogue)
BY_KIND = {k: _en(sid) for k, sid in KIND_IDS.items()}
BY_CODE = {c: _en(sid) for c, sid in CODE_IDS.items()}
BY_FAULT = tuple((prefix, _en(sid)) for prefix, sid in FAULT_IDS)


def _text(tr, sid: str) -> str:
    """``sid`` in the current language: through ``tr`` (the UI translator) when given, else the
    process-wide lookup (:func:`mlaser.i18n.tr`); English when neither has a translation."""
    if tr is not None:
        try:
            s = tr.tr(sid)
            if s and s != sid:
                return s
        except Exception:  # noqa: BLE001
            pass
        return _en(sid)
    s = _cat.tr(sid)
    return s if s and s != sid else _en(sid)


def _has(tr, key: str) -> bool:
    if tr is None:
        return key in _cat.catalogue("en").strings
    try:
        return bool(tr.has(key))
    except Exception:  # noqa: BLE001
        return False


def _tr(tr, key: str) -> Optional[str]:
    if not _has(tr, key):
        return None
    if tr is None:
        return _text(None, key)
    try:
        s = tr.tr(key)
        return s if s and s != key else None
    except Exception:  # noqa: BLE001
        return None


def remedy(alarm, tr=None) -> str:
    """Remedy for a card alarm object (code/kind) or a fault reason string."""
    if isinstance(alarm, str):
        low = alarm.lower()
        for prefix, sid in FAULT_IDS:
            if prefix in low:
                return _text(tr, sid)
        for prefix, sid in FAULT_IDS:
            if low.startswith(prefix.split()[0]):
                return _text(tr, sid)
        return _text(tr, DEFAULT_ID)
    code = getattr(alarm, "code", None)
    kind = getattr(alarm, "kind", "card")
    t = _tr(tr, f"remedy.{code}") or _tr(tr, f"remedy.{kind}")
    if t:
        return t
    if code in CODE_IDS:
        return _text(tr, CODE_IDS[code])
    return _text(tr, KIND_IDS.get(kind, KIND_IDS["card"]))


__all__ = ["remedy", "BY_KIND", "BY_CODE", "BY_FAULT", "KIND_IDS", "CODE_IDS", "FAULT_IDS", "DEFAULT_ID"]
