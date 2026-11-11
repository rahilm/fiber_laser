"""The last job's parts: which ones it cut, where it stopped (UX deep review, finding 5).

Qt-free.  :class:`JobPartsTracker` follows the controller's job events (``JobStarted``,
``JobProgress``, ``JobFinished``, :mod:`mlaser.machine.events`) and keeps one
:class:`LastJob`: the parts of the job in cut order (as indices into the document the job was
started from), how many of them were finished, and how the job ended.  The Run screen shows it
("Stopped at part 5 of 6") and offers "Cut remaining parts" / "Start from part..." from it; both
select those parts and use the ordinary selected-parts Start (pre-flight, the arming question
and the compile path unchanged).

What counts as finished.  ``JobProgress.index`` is the job marker (one per contour, in cut
order) of the contour the card is executing.  A part counts as finished once the card executes
a contour of a part that comes after it in the cut order; the part being cut when the job
stopped counts as not finished, also when its last contour was nearly done (a part interrupted
mid-contour is never reported as cut).  A job that ran to its end finished every part.

A held job (Hold, or a fault that kept a breakpoint) is not over: ``JobFinished`` with reason
``paused`` or ``fault...`` leaves it ``held`` and a resume (``JobStarted.resume``) continues it.
:meth:`JobPartsTracker.settle` turns a held job whose breakpoint is gone into a stopped one.

"Cut this contour again" (a failed pierce): a job of single contours of the last job's drawing
(its plan's document carries ``extra["only_contours"]``, see ``services.jobs.build_request``)
does not replace :attr:`JobPartsTracker.last`; its outcome is kept per contour in
:attr:`LastJob.recut` and the rest of the last job's state is unchanged.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

#: the tracked job modes whose parts are counted (a frame or a laser test is not a job of parts)
MODES = ("process", "dry_run")
OUTCOMES = ("running", "held", "finished", "stopped")


@dataclass
class LastJob:
    """One job's parts and its outcome."""
    mode: str                                  # process | dry_run
    name: str = ""                             # the document's name (display)
    doc_key: str = ""                          # nc.resume.document_key of the document it came from
    #: the document part index of each part, in cut order (None: a part of the plan that is not
    #: in the document, a cutting-strategy line say)
    order: tuple = ()
    done: int = 0                              # parts finished, a prefix of ``order``
    outcome: str = "running"                   # one of OUTCOMES
    started: float = 0.0                       # time.time()
    ended: float = 0.0
    reason: str = ""                           # JobFinished.reason of the end
    #: marker index -> 0-based position in ``order`` of its part
    marker_part: tuple = field(default=(), repr=False)
    #: "Cut this contour again" after this job: (document part index, contour index) -> its
    #: outcome (one of OUTCOMES), in the order they were cut
    recut: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.order)

    @property
    def over(self) -> bool:
        return self.outcome in ("finished", "stopped")

    @property
    def duration_s(self) -> float:
        return max(0.0, (self.ended or time.time()) - self.started) if self.started else 0.0

    @property
    def stopped_at(self) -> int:
        """The 1-based cut-order number of the part the job stopped in (0: none)."""
        return self.done + 1 if self.outcome == "stopped" and self.done < self.total else 0

    @property
    def mapped(self) -> bool:
        """Every part of the job is a part of the document (the offers need that)."""
        return bool(self.order) and all(i is not None for i in self.order)

    def done_parts(self) -> list[int]:
        return [i for i in self.order[:self.done] if i is not None]

    def remaining(self) -> list[int]:
        """The document parts the job did not finish, in cut order."""
        return [i for i in self.order[self.done:] if i is not None]

    def from_part(self, n: int) -> list[int]:
        """The document parts from cut-order number ``n`` (1-based) to the end."""
        n = max(1, min(int(n), self.total or 1))
        return [i for i in self.order[n - 1:] if i is not None]

    def number_of(self, doc_index: int) -> int:
        """The 1-based cut-order number of document part ``doc_index`` (0: not in the job)."""
        try:
            return self.order.index(doc_index) + 1
        except ValueError:
            return 0


def cut_order(plan: Any) -> tuple[list[int], list[int]]:
    """``(plan part indices in cut order, marker index -> position in that order)`` from the
    plan's job markers (``(wire index, part, contour)`` per contour)."""
    markers = list(getattr(getattr(plan, "job", None), "markers", None) or [])
    order: list[int] = []
    pos: dict[int, int] = {}
    marker_part: list[int] = []
    for m in markers:
        p = int(m[1])
        if p not in pos:
            pos[p] = len(order)
            order.append(p)
        marker_part.append(pos[p])
    return order, marker_part


def recut_contours(plan: Any) -> Optional[tuple]:
    """The (plan part, contour) pairs a "Cut this contour again" plan is limited to (None: a
    plan of whole parts)."""
    extra = getattr(getattr(plan, "document", None), "extra", None)
    only = extra.get("only_contours") if isinstance(extra, dict) else None
    return tuple((int(p), int(c)) for p, c in only) if only is not None else None


def map_to_document(plan: Any, plan_parts: list[int], document: Any) -> list[Optional[int]]:
    """The document part index of each plan part (by the parts' ``uid``: the plan's placed
    copy keeps them, also for a selected-parts job); None where a plan part is not in it."""
    pdoc = getattr(plan, "document", None)
    pparts = list(getattr(pdoc, "parts", None) or [])
    where = {getattr(p, "uid", None): i for i, p in enumerate(getattr(document, "parts", None) or [])}
    where.pop(None, None)
    out: list[Optional[int]] = []
    for k in plan_parts:
        uid = getattr(pparts[k], "uid", None) if 0 <= k < len(pparts) else None
        out.append(where.get(uid) if uid is not None else None)
    return out


class JobPartsTracker:
    """Keeps :attr:`last`, the last tracked job's :class:`LastJob`.

    ``context()`` returns ``(plan, document, name)`` when a job starts: the plan being run (its
    markers give the cut order), the document it was started from and its display name."""

    def __init__(self, context: Callable[[], tuple[Any, Any, str]], clock: Callable[[], float] = time.time,
                 key: Optional[Callable[[Any], str]] = None):
        self.context = context
        self.clock = clock
        if key is None:
            from mlaser.nc.resume import document_key
            key = document_key
        self.key = key
        self.last: Optional[LastJob] = None
        self.listeners: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._unsub: list = []
        self._unheld_since = 0.0
        #: the (document part, contour) pairs of the "Cut this contour again" job in progress
        self._recut: tuple = ()

    # ------------------------------------------------------------------ events
    def attach(self, bus: Any) -> None:
        """Follow ``bus`` (a controller's ``events``); call again after a controller change."""
        from mlaser.machine import events as EV
        self.detach()
        self._unsub = [bus.subscribe(EV.JobStarted, lambda ev: self.started(ev.mode, ev.resume, ev.tracked)),
                       bus.subscribe(EV.JobProgress, lambda ev: self.progress(ev.mode, ev.index)),
                       bus.subscribe(EV.JobFinished, lambda ev: self.finished(ev.mode, ev.ok, ev.reason,
                                                                              ev.tracked))]

    def detach(self) -> None:
        for un in self._unsub:
            un()
        self._unsub = []

    def _changed(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001 - a listener must not break the tracking
                pass

    def started(self, mode: str, resume: bool = False, tracked: bool = True) -> None:
        if not tracked or mode not in MODES:
            return
        with self._lock:
            last = self.last
            if resume and self._recut and last is not None:
                for k in self._recut:                   # the contour job goes on
                    last.recut[k] = "running"
                resumed = True
            elif resume and last is not None and last.mode == mode and last.outcome != "finished":
                last.outcome, last.ended, last.reason = "running", 0.0, ""     # the same job goes on
                resumed = True
            else:
                resumed = False
                self._recut = ()
        if not resumed:
            try:
                plan, doc, name = self.context()
            except Exception:  # noqa: BLE001 - no context: a job without parts to offer
                plan, doc, name = None, None, ""
            order, marker_part = cut_order(plan)
            try:
                key = self.key(doc) if doc is not None else ""
            except Exception:  # noqa: BLE001 - no document key: the cut-the-rest offer is withdrawn
                key = ""
            only = recut_contours(plan)
            if only is not None:
                parts = map_to_document(plan, [p for p, _c in only], doc)
                keys = tuple((p, c) for p, (_pp, c) in zip(parts, only) if p is not None)
                with self._lock:
                    last = self.last
                    if (keys and last is not None and last.over and last.mode == mode and key
                            and key == last.doc_key):
                        self._recut = keys
                        for k in keys:
                            last.recut.pop(k, None)     # the newest at the end
                            last.recut[k] = "running"
                        recut = True
                    else:
                        recut = False
                if recut:
                    self._changed()
                    return
            job = LastJob(mode, name=name or "", doc_key=key, order=tuple(map_to_document(plan, order, doc)),
                          started=self.clock(), marker_part=tuple(marker_part))
            with self._lock:
                self.last = job
        self._changed()

    def progress(self, mode: str, index: int) -> None:
        with self._lock:
            last = self.last
            if self._recut or last is None or last.outcome != "running" or last.mode != mode:
                return
            if not 0 <= index < len(last.marker_part):
                return
            done = last.marker_part[index]          # the parts before the one being cut
            if done <= last.done:
                return
            last.done = done
        self._changed()

    def finished(self, mode: str, ok: bool, reason: str = "", tracked: bool = True) -> None:
        if not tracked or mode not in MODES:
            return
        with self._lock:
            last = self.last
            if self._recut and last is not None:
                reason = reason or ""
                held = not ok and (reason == "paused" or reason.startswith("fault"))
                for k in self._recut:
                    last.recut[k] = "held" if held else "finished" if ok else "stopped"
                if not held:
                    self._recut = ()
                recut = True
            else:
                recut = False
        if recut:
            self._changed()
            return
        with self._lock:
            last = self.last
            if last is None or last.mode != mode or last.over:
                return
            reason = reason or ""
            if not ok and (reason == "paused" or reason.startswith("fault")):
                last.outcome = "held"               # resumed later, or stopped (settle)
            else:
                last.outcome = "finished" if ok else "stopped"
                if ok:
                    last.done = last.total
            last.ended, last.reason = self.clock(), reason
        self._changed()

    #: seconds a held job must be without its breakpoint before it counts as stopped (a Hold
    #: ends the stream before the controller sets the breakpoint)
    SETTLE_S = 2.0

    def settle(self, held_now: bool) -> None:
        """A held job whose breakpoint is gone (a fault that kept none) is a stopped job, once
        that has lasted :attr:`SETTLE_S`.  (A Stop of a held job ends it with its own event.)"""
        now = self.clock()
        with self._lock:
            last = self.last
            if self._recut and last is not None:
                if held_now or not any(last.recut.get(k) == "held" for k in self._recut):
                    self._unheld_since = 0.0
                    return
                if not self._unheld_since:
                    self._unheld_since = now
                if now - self._unheld_since < self.SETTLE_S:
                    return
                self._unheld_since = 0.0
                for k in self._recut:
                    last.recut[k] = "stopped"
                self._recut = ()
                settled = True
            else:
                settled = False
        if settled:
            self._changed()
            return
        with self._lock:
            last = self.last
            if last is None or last.outcome != "held" or held_now:
                self._unheld_since = 0.0
                return
            if not self._unheld_since:
                self._unheld_since = now
            if now - self._unheld_since < self.SETTLE_S:
                return
            self._unheld_since = 0.0
            last.outcome = "stopped"
        self._changed()

    def clear(self) -> None:
        with self._lock:
            self.last = None
            self._recut = ()
        self._changed()


__all__ = ["LastJob", "JobPartsTracker", "cut_order", "map_to_document", "recut_contours", "MODES",
           "OUTCOMES"]
