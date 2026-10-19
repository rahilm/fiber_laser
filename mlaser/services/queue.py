"""Job queue: ordered jobs (documents) with quantities and status, persisted across restarts.

Qt-free (docs/review/ux.md P1-7, wireframe §4.4).  Two pieces:

* :class:`JobQueue` holds the entries and writes them to ``queue.json`` (atomic replace) after
  every change, so the queue survives a restart or a crash.  An entry that was running when the
  program ended comes back as ``interrupted`` (the operator decides: run again or skip).
* :class:`QueueRunner` runs the queue on the machine: one entry at a time, one sheet per job,
  with an **operator confirmation between sheets** ("load the next sheet": the runner never
  starts a sheet by itself).  It only orchestrates; the application gives it

  - ``load(entry) -> bool``: make the entry's document the open document;
  - ``start() -> bool``: start it through the normal run path (``SafetyUI.start``: arming
    question, pre-flight dialog, ``window.run_job`` -> ``JobService`` -> the controller's
    ``start_job`` with the supervisor), never around it;
  - ``start_allowed() -> (ok, message)``: the production interlock
    (``ProductionService.start_allowed``, planned quantity).

  The job lifecycle comes from the controller's typed events (:mod:`mlaser.machine.events`)
  through :class:`mlaser.diag.jobwatch.JobWatcher` (tracked process jobs; a pause or a fault
  keeps the job open until it is resumed or stopped).

Entry status: ``waiting`` -> ``running`` -> (``waiting`` again while sheets remain) -> ``done``;
``stopped`` (stopped or failed sheet: the operator retries or skips), ``skipped``,
``interrupted`` (program ended mid-sheet).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Optional

FORMAT = "mlaser-queue/1"
STATUSES = ("waiting", "running", "done", "stopped", "skipped", "interrupted")
ACTIVE = ("waiting", "stopped", "interrupted")        # entries the runner may still run

Listener = Callable[[], None]


@dataclass
class QueueEntry:
    """One queued job: a document file run ``quantity`` times (one sheet each)."""

    path: str                                # the document (.chf / .dxf / .svg)
    name: str = ""
    quantity: int = 1                        # sheets to cut
    done: int = 0                            # sheets cut completely
    status: str = "waiting"
    id: str = ""
    material: str = ""
    thickness_mm: float = 0.0
    sheet: str = ""                          # e.g. "3000 x 1500 mm" or a remnant id
    priority: int = 0                        # informational (the order is the list order)
    est_s: float = 0.0                       # estimated time per sheet (planner), 0 = unknown
    actual_s: float = 0.0                    # cutting time of the completed sheets
    added: float = 0.0                       # time.time()
    started: float = 0.0                     # first sheet started
    finished: float = 0.0                    # last sheet completed
    note: str = ""                           # last message (stop reason, pre-flight refusal)
    remnant_id: str = ""                     # the sheet is this remnant (marked used when cut)
    extra: dict = field(default_factory=dict)   # nest metadata: sheet polygon, utilisation ...

    @property
    def remaining(self) -> int:
        return max(0, self.quantity - self.done)

    @property
    def runnable(self) -> bool:
        return self.status in ACTIVE and self.remaining > 0

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "QueueEntry":
        names = {f.name for f in fields(cls)}
        e = cls(**{k: v for k, v in d.items() if k in names})
        if e.status not in STATUSES:
            e.status = "waiting"
        e.quantity = max(1, int(e.quantity))
        e.done = max(0, min(int(e.done), e.quantity))
        return e


class JobQueue:
    """Ordered entries, persisted as JSON (``path``; None keeps them in memory)."""

    def __init__(self, path: Optional[os.PathLike | str] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path is not None else None
        self.clock = clock
        self.entries: list[QueueEntry] = []
        self.listeners: list[Listener] = []
        self.load_error = ""
        if self.path is not None:
            self.load()

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        self.entries = []
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            items = data.get("entries", []) if isinstance(data, dict) else []
            self.entries = [QueueEntry.from_json(d) for d in items if isinstance(d, dict) and d.get("path")]
        except (OSError, ValueError, TypeError) as exc:
            self.load_error = f"{type(exc).__name__}: {exc}"
            try:                                     # keep the unreadable file for inspection
                os.replace(self.path, str(self.path) + ".bad")
            except OSError:
                pass
            self.entries = []
        for e in self.entries:
            if e.status == "running":                 # the program ended during this sheet
                e.status = "interrupted"
                e.note = "the program ended while this sheet was running"

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"format": FORMAT, "saved": self.clock(), "entries": [e.to_json() for e in self.entries]}
        fd, tmp = tempfile.mkstemp(prefix=".queue-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def changed(self) -> None:
        """Persist and tell the listeners (every mutation ends here)."""
        self.save()
        for fn in list(self.listeners):
            fn()

    def subscribe(self, fn: Listener) -> Callable[[], None]:
        self.listeners.append(fn)
        return lambda: self.listeners.remove(fn) if fn in self.listeners else None

    # ------------------------------------------------------------------ access
    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    def get(self, entry_id: str) -> QueueEntry:
        for e in self.entries:
            if e.id == entry_id:
                return e
        raise KeyError(entry_id)

    def index(self, entry_id: str) -> int:
        for i, e in enumerate(self.entries):
            if e.id == entry_id:
                return i
        raise KeyError(entry_id)

    def next_entry(self) -> Optional[QueueEntry]:
        """The first runnable entry in queue order."""
        for e in self.entries:
            if e.runnable:
                return e
        return None

    def totals(self) -> dict:
        sheets = sum(e.quantity for e in self.entries if e.status != "skipped")
        done = sum(e.done for e in self.entries)
        left_s = sum(e.est_s * e.remaining for e in self.entries if e.runnable or e.status == "running")
        return {"entries": len(self.entries), "sheets": sheets, "done": done,
                "remaining": max(0, sheets - done), "remaining_s": left_s}

    # ------------------------------------------------------------------ edits
    def add(self, path: str, quantity: int = 1, name: str = "", position: Optional[int] = None,
            **kw: Any) -> QueueEntry:
        e = QueueEntry(path=str(path), name=name or Path(path).stem, quantity=max(1, int(quantity)),
                       id=kw.pop("id", "") or uuid.uuid4().hex[:12], added=self.clock(), **kw)
        if position is None:
            self.entries.append(e)
        else:
            self.entries.insert(max(0, min(position, len(self.entries))), e)
        self.changed()
        return e

    def remove(self, entry_id: str) -> QueueEntry:
        e = self.entries.pop(self.index(entry_id))
        self.changed()
        return e

    def move(self, entry_id: str, delta: int) -> int:
        """Move an entry up (negative) or down; returns its new index."""
        i = self.index(entry_id)
        j = max(0, min(len(self.entries) - 1, i + delta))
        if j != i:
            self.entries.insert(j, self.entries.pop(i))
            self.changed()
        return j

    def move_to(self, entry_id: str, index: int) -> int:
        i = self.index(entry_id)
        j = max(0, min(len(self.entries) - 1, index))
        if j != i:
            self.entries.insert(j, self.entries.pop(i))
            self.changed()
        return j

    def set_quantity(self, entry_id: str, quantity: int) -> None:
        e = self.get(entry_id)
        e.quantity = max(1, int(quantity), e.done)
        if e.status == "done" and e.remaining:
            e.status = "waiting"
        elif e.status in ACTIVE and not e.remaining:
            e.status = "done"
        self.changed()

    def skip(self, entry_id: str, skipped: bool = True) -> None:
        e = self.get(entry_id)
        if e.status == "running":
            raise ValueError("a running entry cannot be skipped (stop the job first)")
        e.status = "skipped" if skipped else ("done" if not e.remaining else "waiting")
        self.changed()

    def reset(self, entry_id: str) -> None:
        """Run the entry again from its first sheet."""
        e = self.get(entry_id)
        if e.status == "running":
            raise ValueError("a running entry cannot be reset")
        e.done, e.actual_s, e.status, e.note = 0, 0.0, "waiting", ""
        e.started = e.finished = 0.0
        self.changed()

    def clear_finished(self) -> int:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.status not in ("done", "skipped")]
        n = before - len(self.entries)
        if n:
            self.changed()
        return n


# ============================================================================== runner
class QueueRunner:
    """Runs a :class:`JobQueue` sheet by sheet with an operator confirmation between sheets.

    States: ``idle`` (not running the queue), ``confirm`` (waiting for the operator to load the
    next sheet and confirm), ``starting`` (start requested, waiting for the job to start),
    ``running`` (a sheet of the queue is being cut).  ``on_sheet_done(entry, ok)`` listeners run
    after every finished sheet (remnant capture, reports)."""

    def __init__(self, queue: JobQueue, load: Callable[[QueueEntry], bool] = lambda e: True,
                 start: Callable[[], bool] = lambda: True,
                 start_allowed: Callable[[], tuple[bool, str]] = lambda: (True, ""),
                 clock: Callable[[], float] = time.time,
                 start_timeout_s: float = 30.0,
                 disarm: Optional[Callable[[str], None]] = None) -> None:
        self.queue = queue
        # called when the last sheet of the queue is done: the machine does not stay armed
        # unattended after the queue (docs/SAFETY.md §5; review 2 features #8)
        self.disarm = disarm
        self.load = load
        self.start = start
        self.start_allowed = start_allowed
        self.clock = clock
        self.start_timeout_s = start_timeout_s
        self.state = "idle"
        self.current: Optional[QueueEntry] = None
        self.message = ""
        self.listeners: list[Listener] = []
        self.sheet_listeners: list[Callable[[QueueEntry, bool], None]] = []
        self.watcher: Any = None
        self._t_request = 0.0

    # ------------------------------------------------------------------ wiring
    def attach(self, bus: Any) -> None:
        """Follow the controller's job events (``ctl.events``); call again after a controller
        change."""
        from mlaser.diag.jobwatch import JobWatcher
        if self.watcher is None:
            self.watcher = JobWatcher(self.clock)
            self.watcher.start_listeners.append(self._job_started)
            self.watcher.finish_listeners.append(self._job_finished)
        self.watcher.subscribe(bus)

    def subscribe(self, fn: Listener) -> Callable[[], None]:
        self.listeners.append(fn)
        return lambda: self.listeners.remove(fn) if fn in self.listeners else None

    def _notify(self) -> None:
        for fn in list(self.listeners):
            fn()

    def _say(self, msg: str) -> None:
        self.message = msg

    # ------------------------------------------------------------------ operator actions
    def begin(self) -> bool:
        """Start running the queue: wait for the operator to confirm the first sheet."""
        if self.state in ("starting", "running"):
            return False
        e = self.queue.next_entry()
        if e is None:
            self.state, self.current = "idle", None
            self._say("queue.msg.empty")
            self._notify()
            return False
        self.current = e
        self.state = "confirm"
        self._say("queue.msg.load_sheet")
        self._notify()
        return True

    def cancel(self) -> None:
        """Leave queue mode (a running sheet keeps running; stop it with Stop)."""
        if self.state in ("confirm", "starting", "idle"):
            self.state = "idle"
            self.current = None
            self._say("")
            self._notify()

    def confirm_sheet(self) -> bool:
        """The operator confirms that the next sheet is loaded: load the document and start it
        through the normal run path (pre-flight, arming).  False when nothing was started."""
        if self.state == "idle":
            if not self.begin():
                return False
        if self.state != "confirm" or self.current is None:
            return False
        e = self.current
        if not e.runnable:
            nxt = self.queue.next_entry()
            if nxt is None:
                self.state, self.current = "idle", None
                self._say("queue.msg.finished")
                self._notify()
                return False
            self.current = e = nxt
        ok, msg = self.start_allowed()
        if not ok:
            e.note = msg
            self._say(msg)
            self.queue.changed()
            self._notify()
            return False
        try:
            loaded = bool(self.load(e))
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            loaded = False
            e.note = f"load failed: {exc}"
        if not loaded:
            e.status = "stopped"
            e.note = e.note or "the document could not be opened"
            self._say(e.note)
            self.queue.changed()
            self._notify()
            return False
        self.state = "starting"
        self._t_request = self.clock()
        try:
            started = bool(self.start())
        except Exception as exc:  # noqa: BLE001
            started = False
            e.note = f"start failed: {exc}"
        if not started and self.state == "starting":
            # refused (pre-flight, arming declined, interlock): back to the confirmation
            self.state = "confirm"
            self._say(e.note or "queue.msg.start_refused")
            self.queue.changed()
            self._notify()
            return False
        self._notify()
        return True

    def tick(self) -> None:
        """Periodic check: a start request that never produced a job goes back to confirm."""
        if self.state == "starting" and self.clock() - self._t_request > self.start_timeout_s:
            self.state = "confirm"
            self._say("queue.msg.start_refused")
            self._notify()

    # ------------------------------------------------------------------ job events
    def _job_started(self, mode: str, info: str) -> None:
        if mode != "process" or self.state != "starting" or self.current is None:
            return
        e = self.current
        e.status = "running"
        e.note = ""
        if not e.started:
            e.started = self.clock()
        self.state = "running"
        self._say("")
        self.queue.changed()
        self._notify()

    def _job_finished(self, ev: Any) -> None:
        if self.state != "running" or self.current is None or ev.mode != "process":
            return
        e = self.current
        if ev.ok:
            e.done = min(e.quantity, e.done + 1)
            e.actual_s += ev.duration_s
            e.finished = self.clock()
            e.status = "done" if not e.remaining else "waiting"
            e.note = ""
        else:
            e.status = "stopped"
            e.note = ev.info or "stopped"
        self.queue.changed()
        for fn in list(self.sheet_listeners):
            try:
                fn(e, bool(ev.ok))
            except Exception:  # noqa: BLE001 - a listener must not stop the queue
                pass
        if not ev.ok:
            self.state = "confirm"              # the operator retries (confirm) or cancels
            self._say("queue.msg.sheet_stopped")
            self._notify()
            return
        nxt = self.queue.next_entry()
        if nxt is None:
            self.state, self.current = "idle", None
            self._say("queue.msg.finished")
            if self.disarm is not None:
                try:
                    self.disarm("queue finished")
                except Exception:  # noqa: BLE001 - the queue state is already final
                    pass
        else:
            self.current = nxt
            self.state = "confirm"              # always ask before the next sheet
            self._say("queue.msg.load_sheet")
        self._notify()


__all__ = ["QueueEntry", "JobQueue", "QueueRunner", "STATUSES", "FORMAT"]
