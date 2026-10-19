"""Detect job start / completion from the public controller interface.

The original runs its job bookkeeping in the status timer FUN_00598280 when the machine
leaves the processing state (process statistics, TotalReport.txt, production counter, device
counters).  The watcher follows the controller's typed events (:meth:`subscribe`,
:mod:`mlaser.machine.events`): ``JobStarted`` / ``JobFinished`` of tracked jobs; laser tests,
frames and Backward/Forward moves are untracked, and a resume (``resume=True``) continues the
job that is already open.

Without a bus (old scripts) it falls back to what it did before the event bus existed:

* log messages (:meth:`feed_log`): ``"<mode>: <what>"`` when a job starts, ``"done: <mode>..."``
  on normal completion, ``"<mode> stopped"`` / ``"stopped"`` / ``"<mode> aborted"`` otherwise;
* :meth:`poll` with ``ctl.state()`` (``mode`` leaves ``process``; ``progress`` >= 0.999 means
  completed).

Once subscribed, :meth:`feed_log` and :meth:`poll` are no-ops.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

JOB_MODES = ("process", "dry_run", "frame")
_START = re.compile(r"^(process|dry_run|frame): (.*)$")
_DONE = re.compile(r"^done: (process|dry_run|frame)\b")
_END = re.compile(r"^(?:(process|dry_run|frame) (?:stopped|aborted)|stopped)\b")


@dataclass
class JobEvent:
    mode: str
    ok: bool
    start: float                      # time.time()
    end: float
    info: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end - self.start)


class JobWatcher:
    """Calls ``on_start(mode, info)`` and ``on_finish(JobEvent)`` listeners."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.start_listeners: list[Callable[[str, str], None]] = []
        self.finish_listeners: list[Callable[[JobEvent], None]] = []
        self.mode: Optional[str] = None
        self.started_at = 0.0
        self.info = ""
        self._paused_s = 0.0
        self._log_driven = False
        self._last_mode = "offline"
        self._unsub: list = []
        self.bus = None

    # ------------------------------------------------------------------ events
    def subscribe(self, bus) -> None:
        """Follow ``bus`` (a controller's ``events``); replaces the log/state heuristics."""
        from mlaser.machine import events as EV
        for un in self._unsub:
            un()
        self.bus = bus
        self._unsub = [bus.subscribe(EV.JobStarted, self._on_started),
                       bus.subscribe(EV.JobFinished, self._on_finished)]

    def _on_started(self, ev) -> None:
        if not ev.tracked or ev.mode not in JOB_MODES:
            return
        if ev.resume and self.mode is not None:
            return
        self.start(ev.mode, ev.info)

    def _on_finished(self, ev) -> None:
        if not ev.tracked or self.mode is None or ev.mode not in JOB_MODES:
            return
        if ev.reason in ("paused",) or ev.reason.startswith("fault"):
            return                         # the job continues after a pause / fault reset
        self.finish(ev.ok, ev.reason)

    # ------------------------------------------------------------------ inputs
    def feed_log(self, msg: str) -> None:
        if self.bus is not None:
            return
        m = _START.match(msg)
        if m:
            what = m.group(2)
            if "laser test" in what:
                return
            self._log_driven = True
            if "resume from" in what and self.mode == m.group(1):
                return
            self.start(m.group(1), what)
            return
        m = _DONE.match(msg)
        if m and self.mode is not None:
            self._log_driven = True
            self.finish(True, msg)
            return
        m = _END.match(msg)
        if m and self.mode is not None and (m.group(1) in (None, self.mode)):
            self._log_driven = True
            self.finish(False, msg)

    def poll(self, state) -> None:
        """Fallback edge detection on ``MachineState.mode`` (used only when no log events)."""
        mode = getattr(state, "mode", "offline")
        if self.bus is not None:
            self._last_mode = mode
            return
        if not self._log_driven:
            if mode in JOB_MODES and self._last_mode not in JOB_MODES + ("pause",):
                self.start(mode, "")
            elif self.mode is not None and mode not in JOB_MODES + ("pause",):
                self.finish(getattr(state, "progress", 0.0) >= 0.999, "")
        self._last_mode = mode

    # ------------------------------------------------------------------ events
    def start(self, mode: str, info: str = "") -> None:
        self.mode, self.info = mode, info
        self.started_at = self.clock()
        for fn in list(self.start_listeners):
            try:
                fn(mode, info)
            except Exception:  # noqa: BLE001 - a listener must not break the others
                pass

    def finish(self, ok: bool, info: str = "") -> Optional[JobEvent]:
        if self.mode is None:
            return None
        ev = JobEvent(self.mode, ok, self.started_at, self.clock(), info or self.info)
        self.mode = None
        for fn in list(self.finish_listeners):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001
                pass
        return ev

    @property
    def running(self) -> bool:
        return self.mode is not None
