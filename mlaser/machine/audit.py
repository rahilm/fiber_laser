"""Structured audit trail (docs/SAFETY.md §7).

Every safety-relevant fact goes to the ``mlaser.audit`` logger as one record with an
``audit`` dict (event name plus fields).  :func:`mlaser.machine.logsetup.setup_logging` writes
them as JSON lines into ``audit.log`` (rotating, in the user log directory) and also into the
main log.  Events:

* ``command``: every outgoing card command except status reads and FIFO data frames (class,
  register, words); refused commands carry ``refused``;
* ``fifo``: a summary per job (frames, frames with emission);
* ``arm`` / ``disarm``, ``state`` (safety state change), ``fault``, ``fault_reset``;
* ``alarm`` (raised / cleared), ``link`` (lost / restored), ``halt`` (each safe_halt with the
  result of every command), ``interlock``, ``preflight`` (refused actions), ``crash``;
* ``material_check`` (a cut refused or let through by an override because the sheet does not
  match the technology) and ``material_override`` (the operator's confirmation, with the
  reason and user mode; :mod:`mlaser.services.material_check`).
"""
from __future__ import annotations

import logging
from typing import Any

AUDIT = logging.getLogger("mlaser.audit")


def audit(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one audit record (cheap when nobody listens)."""
    if not AUDIT.isEnabledFor(level):
        return
    rec = {"event": event, **fields}
    AUDIT.log(level, "%s %s", event, _short(fields), extra={"audit": rec})


def _short(fields: dict) -> str:
    parts = []
    for k, v in fields.items():
        s = repr(v) if not isinstance(v, str) else v
        if len(s) > 120:
            s = s[:117] + "..."
        parts.append(f"{k}={s}")
    return " ".join(parts)


__all__ = ["AUDIT", "audit"]
