"""Start readiness: what must be true before Start, shown before Start is pressed (UX audit W3).

:func:`readiness` returns one :class:`ReadinessItem` per prerequisite, in the order an operator
works through them:

========== ============================================================ ====================
id         ok when                                                       action id (fix)
========== ============================================================ ====================
connected  the controller is connected (a card or the simulator)        ``hw.reconnect``
alarms     no card alarm / e-stop is active and no fault is latched      ``card.clear_alarms``
homed      X and Y are homed, or the machine does not require homing     ``machine.home``
technology every layer the drawing uses has cutting parameters           ``workspace.process``
interlocks every interlock and the heartbeat is configured or declared    ``machine.safety_io``
           not fitted (a warning: Start still runs, the pre-flight asks)
armed      the machine is armed (Start asks to arm when it is not)        ``run.arm``
========== ============================================================ ====================

The checks are the pre-flight's own (:mod:`mlaser.machine.preflight`): readiness only shows them
earlier; the pre-flight check at Start stays the gate.  Pure reads of the controller, no I/O, no
Qt: the Run screen's readiness strip, the run bar's NOT HOMED state, the API and the tests use
it.  ``blocking`` items stop a start; the others are information (the pre-flight asks).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from .. import i18n as _i18n

log = logging.getLogger("mlaser.readiness")

#: the item ids in display order
ITEMS = ("connected", "alarms", "homed", "technology", "interlocks", "armed")


@dataclass(frozen=True)
class ReadinessItem:
    id: str                    # one of ITEMS
    ok: bool
    label: str                 # "Homed" (translated)
    reason: str = ""           # why not ok, plain language ("" when ok)
    action: str = ""           # the command id that fixes it ("" when ok or nothing to do)
    blocking: bool = True      # False: Start still runs (the pre-flight asks)


def _t(sid: str, **kw) -> str:
    return _i18n.tr(sid, **kw)


def _alarm_text(a: Any) -> str:
    return getattr(a, "text_en", None) or str(a)


def homing_needed(ctl: Any) -> bool:
    """Connected, the policy requires homing and X/Y are not homed (the NOT HOMED run state)."""
    if ctl is None:
        return False
    try:
        ms = ctl.state()
        if not ms.connected or not bool(getattr(ctl, "require_homed", False)):
            return False
        homed = ctl.homed_xy() if callable(getattr(ctl, "homed_xy", None)) else ms.homed
    except Exception:  # noqa: BLE001 - a controller without the query: unknown, not "needed"
        return False
    return homed is False


def undefined_layers(ctl: Any, document: Any) -> list[int]:
    """The layers ``document`` uses that have no cutting parameters (the pre-flight's
    ``layer_undefined``)."""
    if document is None or not getattr(document, "parts", None):
        return []
    from ..machine import preflight as PF
    table = None
    try:
        table = ctl.layer_table() if ctl is not None and callable(getattr(ctl, "layer_table", None)) else None
    except Exception:  # noqa: BLE001 - no card layer table: judged from the document (logged)
        log.debug("readiness: the layer table cannot be read", exc_info=True)
        table = None
    return sorted(li for li in PF.used_layers(document) if not PF.layer_is_defined(document, li, table))


def readiness(ctl: Any, document: Any = None, safety_io: Optional[Any] = None) -> list[ReadinessItem]:
    """The start prerequisites of ``ctl`` for ``document`` (see the module doc).
    ``safety_io``: a :class:`mlaser.services.safety_io.SafetyIO` (None: read from ``ctl``)."""
    out: list[ReadinessItem] = []
    try:
        ms = ctl.state() if ctl is not None else None
    except Exception:  # noqa: BLE001 - cannot read the state: shown as not connected (restrictive)
        ms = None
    connected = bool(ms is not None and ms.connected)
    out.append(ReadinessItem("connected", connected, _t("ready.connected"),
                             "" if connected else _t("ready.why.offline"), "" if connected else "hw.reconnect"))
    st = ctl.safety() if ctl is not None and callable(getattr(ctl, "safety", None)) else None
    alarms = list(getattr(ctl, "active_alarms", None) or ()) if ctl is not None else []
    fault = getattr(st, "fault", "") if st is not None and getattr(st, "banner", "") == "FAULT" else ""
    if alarms:
        why = _t("ready.why.alarm", alarm=_alarm_text(alarms[0]))
    elif fault:
        from ..machine.fault_text import headline
        why = _t("ready.why.fault", fault=headline(fault))
    else:
        why = ""
    out.append(ReadinessItem("alarms", not why, _t("ready.alarms"), why, "card.clear_alarms" if why else ""))
    need_home = homing_needed(ctl)
    out.append(ReadinessItem("homed", not need_home, _t("ready.homed"),
                             _t("ready.why.not_homed") if need_home else "", "machine.home" if need_home else ""))
    missing = undefined_layers(ctl, document)
    out.append(ReadinessItem("technology", not missing, _t("ready.technology"),
                             _i18n.plural("ready.why.technology", len(missing),
                                          layers=", ".join(str(li) for li in missing)) if missing else "",
                             "workspace.process" if missing else ""))
    sio = safety_io
    if sio is None and ctl is not None:
        try:
            from . import safety_io as S
            sio = S.current(ctl)
        except Exception:  # noqa: BLE001 - no interlock notices; the controller's pre-flight still checks them (logged)
            log.debug("readiness: the safety I/O configuration cannot be read", exc_info=True)
            sio = None
    if sio is not None:
        from . import safety_io as S
        open_ = [n for n in S.notices(sio) if not n.declared]
        out.append(ReadinessItem("interlocks", not open_, _t("ready.interlocks"),
                                 _i18n.plural("ready.why.interlocks", len(open_)) if open_ else "",
                                 "machine.safety_io" if open_ else "", blocking=False))
    armed = bool(getattr(st, "armed", False))
    dry = bool(getattr(st, "dry_run", False)) or not bool(getattr(ctl, "laser_enabled", True))
    if dry:
        out.append(ReadinessItem("armed", True, _t("ready.armed"), _t("ready.why.laser_off"), "", blocking=False))
    else:
        out.append(ReadinessItem("armed", armed, _t("ready.armed"), "" if armed else _t("ready.why.not_armed"),
                                 "" if armed else "run.arm", blocking=False))
    return out


def blockers(items: list[ReadinessItem]) -> list[ReadinessItem]:
    """The items that stop a start."""
    return [i for i in items if not i.ok and i.blocking]


__all__ = ["ReadinessItem", "ITEMS", "readiness", "blockers", "homing_needed", "undefined_layers"]
