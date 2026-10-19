"""Resume helpers for the UI (the Safety-core start gate, review 2).

* :func:`resume` passes the open document to ``ctl.resume(mode, document=...)`` when the
  controller takes it, so a breakpoint left by another document is refused by the controller;
  an older controller gets ``resume(mode)``.  (Merge note: the feature check can go once every
  controller has the ``document`` parameter.)
* :func:`moving` is "a job is actually streaming": ``job_active()`` also counts a pending
  breakpoint (the document lock holds until it is resolved), so it cannot tell a paused or
  stopped job from a running one.
"""
from __future__ import annotations

import inspect
from typing import Any

RUNNING_MODES = ("process", "dry_run", "frame")


def _takes_document(fn: Any) -> bool:
    try:
        return "document" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def resume(ctl: Any, mode: str = "exact", document: Any = None) -> Any:
    fn = ctl.resume
    if document is not None and _takes_document(fn):
        return fn(mode, document=document)
    return fn() if mode == "exact" else fn(mode)      # as the callers did before


def moving(ctl: Any) -> bool:
    try:
        return ctl.state().mode in RUNNING_MODES
    except Exception:  # noqa: BLE001 - a controller that cannot say counts as moving
        return True


__all__ = ["resume", "moving", "RUNNING_MODES"]
