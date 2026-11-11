"""Resume helpers for the UI (the Safety-core start gate, review 2).

* :func:`resume` passes the open document to ``ctl.resume(mode, document=...)``, so a
  breakpoint left by another document is refused by the controller (MachineAPI).
* :func:`moving` is "a job owns the card" (starting, streaming or being stopped), from the
  job's phase (:attr:`mlaser.ui.run_rules.JobView.owns_card`, review 2026-11-09 arch A1):
  ``job_active()`` also counts a held job and a pending breakpoint, so it cannot tell a held
  job from a running one.  A phase that cannot be read counts as moving (nothing resumes).
"""
from __future__ import annotations

from typing import Any

RUNNING_MODES = ("process", "dry_run", "frame")


def resume(ctl: Any, mode: str = "exact", document: Any = None) -> Any:
    return ctl.resume(mode, document=document)


def moving(ctl: Any) -> bool:
    from .run_rules import job_view
    return job_view(ctl).owns_card


__all__ = ["resume", "moving", "RUNNING_MODES"]
