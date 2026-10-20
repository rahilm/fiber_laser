"""Resume helpers for the UI (the Safety-core start gate, review 2).

* :func:`resume` passes the open document to ``ctl.resume(mode, document=...)``, so a
  breakpoint left by another document is refused by the controller (MachineAPI).
* :func:`moving` is "a job is actually streaming": ``job_active()`` also counts a pending
  breakpoint (the document lock holds until it is resolved), so it cannot tell a paused or
  stopped job from a running one.
"""
from __future__ import annotations

from typing import Any

RUNNING_MODES = ("process", "dry_run", "frame")


def resume(ctl: Any, mode: str = "exact", document: Any = None) -> Any:
    return ctl.resume(mode, document=document)


def moving(ctl: Any) -> bool:
    try:
        return ctl.state().mode in RUNNING_MODES
    except Exception:  # noqa: BLE001 - a controller that cannot say counts as moving
        return True


__all__ = ["resume", "moving", "RUNNING_MODES"]
