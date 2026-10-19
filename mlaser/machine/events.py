"""Typed machine events and a small thread-safe event bus (docs/SAFETY.md §4).

Every controller (:class:`~mlaser.machine.api.MachineAPI`) owns one :class:`EventBus` as
``ctl.events``.  Producers (poll thread, job worker, safety supervisor) publish frozen event
dataclasses; consumers subscribe by event type (or to every event with ``Event``).

Delivery: without a dispatcher, handlers run synchronously on the publishing thread.  A GUI
installs a dispatcher with :meth:`EventBus.set_dispatcher` (``ui.qtbridge.QtDispatcher``) that
runs each delivery on its own thread; this replaces the Qt signal bridge the UI adapter used to
carry (and with it the import-linter ignore ``controller.ui_adapter -> PySide6``).

A failing handler never breaks the publisher or the other handlers: the exception is logged
(``mlaser.events``) and counted in :attr:`EventBus.handler_errors`.  Safety-relevant consumers
must therefore not rely on raising; the safety supervisor itself does not consume events.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypeVar

log = logging.getLogger("mlaser.events")

E = TypeVar("E", bound="Event")


@dataclass(frozen=True)
class Event:
    """Base class; ``t`` is ``time.time()`` at creation."""
    t: float = field(default_factory=time.time, compare=False, kw_only=True)


# ------------------------------------------------------------------------------ job
@dataclass(frozen=True)
class JobStarted(Event):
    mode: str                       # process | dry_run | frame | pause (backward/forward) | test
    info: str = ""
    resume: bool = False            # continuing from a breakpoint (not a new job)
    tracked: bool = True            # False: frame, laser test, along-path moves


@dataclass(frozen=True)
class JobProgress(Event):
    mode: str
    fraction: float
    index: int = -1                 # contour index (job markers), -1 unknown


@dataclass(frozen=True)
class JobFinished(Event):
    mode: str
    ok: bool                        # True: executed to the end
    reason: str = ""                # "done" | "stopped" | "paused" | "fault: ..." | error text
    resume: bool = False
    tracked: bool = True
    duration_s: float = 0.0


# ------------------------------------------------------------------------------ alarms
@dataclass(frozen=True)
class AlarmRaised(Event):
    alarms: tuple = ()              # the new alarms (controller.alarms.Alarm or text)
    active: tuple = ()              # every alarm active now


@dataclass(frozen=True)
class AlarmCleared(Event):
    alarms: tuple = ()
    active: tuple = ()


# ------------------------------------------------------------------------------ link
@dataclass(frozen=True)
class LinkLost(Event):
    reason: str = ""
    during_job: bool = False


@dataclass(frozen=True)
class LinkRestored(Event):
    pass


# ------------------------------------------------------------------------------ safety
@dataclass(frozen=True)
class Armed(Event):
    pass


@dataclass(frozen=True)
class Disarmed(Event):
    reason: str = ""


@dataclass(frozen=True)
class StateChanged(Event):
    """Safety state change (``machine.safety.SafetyState`` values as strings)."""
    old: str
    new: str
    reason: str = ""
    fault: str = ""                 # latched fault text while ``new == "fault"``


@dataclass(frozen=True)
class Log(Event):
    message: str
    level: int = logging.INFO


@dataclass(frozen=True)
class BreakpointChanged(Event):
    breakpoint: Any = None          # nc.resume.Breakpoint or None


@dataclass(frozen=True)
class HomingDone(Event):
    ok: bool = True


Handler = Callable[[Any], None]


class EventBus:
    """Publish/subscribe by event class (subclasses included)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[tuple[type, Handler]] = []
        self._dispatcher: Optional[Callable[[Callable[[], None]], None]] = None
        self.handler_errors = 0
        self.history: list = []            # last events (diagnostics, tests)
        self.history_len = 200

    # ------------------------------------------------------------------ subscribe
    def subscribe(self, kind: type, fn: Handler) -> Callable[[], None]:
        """Call ``fn(event)`` for every event that is an instance of ``kind``.  Returns an
        unsubscribe function."""
        with self._lock:
            self._subs.append((kind, fn))

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subs.remove((kind, fn))
                except ValueError:
                    pass
        return unsubscribe

    def set_dispatcher(self, dispatch: Optional[Callable[[Callable[[], None]], None]]) -> None:
        """``dispatch(thunk)`` runs ``thunk()`` where handlers must run (e.g. the GUI thread);
        None = synchronous delivery on the publishing thread."""
        self._dispatcher = dispatch

    @property
    def dispatcher(self):
        return self._dispatcher

    # ------------------------------------------------------------------ publish
    def publish(self, ev: Event) -> None:
        with self._lock:
            subs = [fn for kind, fn in self._subs if isinstance(ev, kind)]
            self.history.append(ev)
            del self.history[:-self.history_len]
        if not subs:
            return
        d = self._dispatcher
        if d is None:
            self._deliver(subs, ev)
        else:
            try:
                d(lambda: self._deliver(subs, ev))
            except Exception:  # noqa: BLE001 - dispatcher gone (app closing): deliver here
                log.debug("dispatcher failed, delivering synchronously", exc_info=True)
                self._deliver(subs, ev)

    def _deliver(self, subs, ev) -> None:
        for fn in subs:
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 - a consumer must not break the producer
                self.handler_errors += 1
                log.exception("event handler %r failed on %s", fn, type(ev).__name__)

    def last(self, kind: type[E]) -> Optional[E]:
        """Most recent event of ``kind`` (diagnostics, tests)."""
        with self._lock:
            for ev in reversed(self.history):
                if isinstance(ev, kind):
                    return ev
        return None


__all__ = [
    "Event", "JobStarted", "JobProgress", "JobFinished", "AlarmRaised", "AlarmCleared",
    "LinkLost", "LinkRestored", "Armed", "Disarmed", "StateChanged", "Log", "BreakpointChanged",
    "HomingDone", "EventBus",
]
