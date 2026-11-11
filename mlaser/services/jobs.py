"""PlannedJob and JobService: one compiled job that is previewed, cached and streamed.

Before Wave 2 the canvas showed a toolpath built from ``CamParams.origin`` while Start, Dry Cut
and Frame recompiled on the GUI thread with the head position as the sort origin and with the job
placement applied, so the preview could differ from the cut, and a large compile froze the window
(architecture.md §1.7, §4.1, P1-3; ux.md P0 "what you see is what you cut").  Now:

* :class:`PlannedJob` is immutable: the toolpath, the compiled :class:`mlaser.nc.compiler.Job`
  (wire items and frame grouping), bounds, the time estimate and a hash of every input
  (document, machine, CAM parameters, start position, placement, layers, ParityProfile).
* :class:`JobService` compiles in a worker thread (progress, cancel), debounces edits
  (:meth:`JobService.schedule_preview`) and caches plans by input hash.  The toolpath stage is
  published first (the canvas draws it), then the compiled job.
* **Two-stage previews** (``compile_idle_s``, the application's setting; perf review finding 8):
  an edit or a head move plans the toolpath only; the full compile of that preview starts once
  no newer preview came for ``compile_idle_s`` seconds, when :meth:`JobService.settle` asks for
  it, or when Start / Dry Cut / Frame needs a plan of the same inputs (the start path computes
  the inputs hash itself and compiles whatever it does not find, so a stale plan is never
  started).  In the plan process the toolpath of the first stage is kept and compiled as it is.
  The first stage also plans the time estimate (``JobTicket.estimate``, ``on_estimate``; perf
  review finding 8): the numbers the compile's ``PlannedJob.estimate`` gives, without waiting
  for the compile.  A Start or a ``settle`` that waits skips it (the compile gives it).
* **Bounded by bytes** (``cache_bytes``, perf review finding 7): the cache drops its oldest
  plans beyond about ``cache_bytes`` of compiled job (packed wire, toolpath, bookkeeping), and
  beyond ``cache_size`` plans; the newest plan and the current preview plan always stay.
  :meth:`JobService.release_document` empties it (and the plan codec) when the document is
  closed (finding 12).
* Start / Dry Cut / Frame go through :meth:`JobService.run`: the plan for the current inputs
  (normally the cached preview plan) is handed to a :class:`JobSubmitter`; nothing is compiled on
  the GUI thread.  The frames the controller streams are ``PlannedJob.frames()``, i.e.
  ``Job.frames()`` (tests/test_services_jobs.py checks that they are identical).

Qt-free: the application injects ``post`` (run a callable on the GUI thread) and ``schedule``
(a timer); the defaults run inline / on ``threading.Timer``.
"""

from __future__ import annotations

import copy
import hashlib
import io
import logging
import pickle
import threading
import time
from collections import OrderedDict
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

import dataclasses

from mlaser.model import Arc, Contour, Craft, Document, Part
from mlaser.toolpath.points import path_points  # noqa: F401 - re-exported

log = logging.getLogger("mlaser.jobs")

Point = tuple[float, float]
MODES = ("process", "dry_run", "frame")

#: default byte budget of the plan cache: at 300 parts (about 25 MB a plan) it holds the six
#: plans the count limit allows, at 2,700 parts (about 230 MB) the newest one alone
CACHE_BYTES = 256 * 2**20
#: seconds without a newer preview before the application compiles the preview's toolpath
COMPILE_IDLE_S = 2.0


class Cancelled(Exception):
    """Raised inside a worker when its request was superseded or cancelled."""


# --------------------------------------------------------------------------- data
@dataclass(frozen=True)
class JobRequest:
    """Everything a compile depends on, captured on the GUI thread.

    ``document`` must be a private snapshot (``DocumentService.snapshot``) with the job placement
    already applied (``controller.place``: ``extra['job_offset']``); ``source`` is the snapshot
    before placement (what job-start hooks such as autosave receive)."""

    mode: str
    document: Document
    machine: Any
    start: Point = (0.0, 0.0)
    cam_params: Any = None
    layers: Any = None
    dry_run: bool = False
    profile: str = "original"
    source: Optional[Document] = None
    selected_only: bool = False


@dataclass(frozen=True)
class PlannedJob:
    """An immutable compiled job (treat ``job`` and ``toolpath`` as read-only)."""

    key: str  # hash of the inputs (JobRequest)
    mode: str  # process | dry_run | frame
    toolpath: tuple  # the MotionItems that were compiled (placed coordinates)
    job: Any  # mlaser.nc.compiler.Job: wire items + frame grouping
    start: Point
    bounds: tuple[float, float, float, float]  # every move incl. the rapids from the start (travel)
    estimate: dict  # mlaser.motion.estimate_time (planner)
    profile: str  # ParityProfile name
    document: Document  # the placed document the job was compiled from
    source: Optional[Document] = None  # the document before placement
    compile_s: float = 0.0
    selected_only: bool = False
    cut_bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # cut moves and leads

    @property
    def dry_run(self) -> bool:
        return bool(self.job.dry_run)

    @property
    def wire(self) -> list:
        return self.job.wire

    def frames(self, first_seq: int = 1) -> list[list[int]]:
        """The FIFO frames to stream: ``Job.frames()`` (one size test per ManuItem group)."""
        return self.job.frames(first_seq)

    def iter_frames(self, first_seq: int = 1):
        """:meth:`frames` one at a time from the packed wire (``Job.iter_frames``)."""
        return self.job.iter_frames(first_seq)

    @property
    def time_s(self) -> float:
        """Card replay time plus the estimated process time (``Job.total_time_s``)."""
        return float(self.job.total_time_s)

    def summary(self) -> str:
        return self.job.summary()


class JobSubmitter(Protocol):
    """What the controller must provide to run a planned job.

    Merge point with the Safety branch (typed MachineAPI): the controller implements
    ``start_job(job)`` itself (pre-flight, arming, then streaming ``job.frames()``); until then
    :class:`ControllerSubmitter` adapts today's ``UiController`` / ``DemoController``."""

    def start_job(self, job: PlannedJob) -> Optional[bool]: ...


# --------------------------------------------------------------------------- hashing
class _KeyPickler(pickle.Pickler):
    """Pickles the inputs for the hash: round-trip records by identity (they are immutable,
    services.undo.RoundTripTable), parts and contours without selection flags and uids."""

    def __init__(self, f, blob_ids: set[int]) -> None:
        super().__init__(f, protocol=5)
        self._blob_ids = blob_ids
        # value-based key: no memo, so equal documents hash equal whether or not sub-objects
        # (a shared Craft, LayerParams ...) are shared; undo restores equal copies, not the same
        # sharing.  The document graph has no cycles; round-trip blobs go by persistent_id.
        self.fast = True

    def persistent_id(self, obj):
        if id(obj) in self._blob_ids:
            return ("blob", id(obj))
        return None

    def reducer_override(self, obj):
        # the opaque ``extra`` data plus the typed fields (mlaser.model), uid as 0
        if type(obj) is Part:
            return (Part, (obj.contours, obj.craft, obj.name, False, dict(dict.items(obj.extra)), 0, obj.raster))
        if type(obj) is Contour:
            return (Contour, (obj.segments, obj.closed, dict(dict.items(obj.extra)), 0, obj.craft, obj.glyphs,
                              obj.glyph_key))
        if type(obj) is Craft:
            # ``extra`` may be a plain dict or the model's LegacyExtra view (after undo or a
            # .chf load); both must hash alike, so reduce every field with extra as its raw items
            vals = tuple(dict(dict.items(v)) if f.name == "extra" else v
                         for f in dataclasses.fields(obj) for v in (getattr(obj, f.name),))
            return (_craft_from_fields, (vals,))
        return NotImplemented


def _craft_from_fields(vals: tuple) -> Craft:
    """Only used as the reduce target of the hash pickler (never unpickled)."""
    return Craft(*vals)


def _blob_ids(doc: Document) -> set[int]:
    from .undo import PART_BLOB_KEYS, contour_blobs

    ids = set()
    for p in doc.parts:
        for k in PART_BLOB_KEYS:
            v = p.extra.get(k) if isinstance(p.extra, dict) else None
            if v is not None:
                ids.add(id(v))
        for c in p.contours:
            for _k, v in contour_blobs(c):
                if v is not None:
                    ids.add(id(v))
    if isinstance(doc.extra, dict) and doc.extra.get("chf") is not None:
        ids.add(id(doc.extra["chf"]))
    return ids


def inputs_hash(req: JobRequest, codec: Any = None) -> str:
    """sha256 of every input of the compile.  Unpicklable inputs fall back to a per-object key
    (no cache hit, still correct).

    The document is hashed per part: the request without its parts, then each part's digest
    (``planproc.part_digest``: the same value-based pickle as before, part by part; the pickler
    keeps no memo, so this carries the same information as one pickle of the whole document).
    ``codec`` (the JobService's ``PlanCodec``) caches the digests of the planner snapshot's
    part copies, which are shared between plans while a part is unchanged: after a one-part
    edit only that part is pickled (review 2 P1-3; 297 ms at 2,000 parts, 1.7 s at 10,000)."""
    from .planproc import part_digest

    h = hashlib.sha256()
    buf = io.BytesIO()
    try:
        doc = req.document
        head = Document(parts=[], layers=doc.layers, version=doc.version, extra=doc.extra)
        _KeyPickler(buf, _blob_ids(head)).dump(
            (
                req.mode,
                head,
                req.machine,
                tuple(req.start),
                _plain(req.cam_params),
                req.layers,
                bool(req.dry_run),
                req.profile,
                len(doc.parts),
            )
        )
        h.update(buf.getvalue())
        for p in doc.parts:
            h.update(codec.digest(p) if codec is not None else part_digest(p))
    except Exception:  # noqa: BLE001 - see docstring
        h.update(repr((id(req), time.time())).encode())
    return h.hexdigest()[:32]


def _plain(cam) -> Any:
    if cam is None or isinstance(cam, dict):
        return cam
    return sorted(vars(cam).items())


def toolpath_bounds(items, kinds=("Rapid", "CutLine", "CutArc")) -> tuple[float, float, float, float]:
    """Bounding box of the moves of ``kinds`` (default: every move, rapids from the start too)."""
    xs: list[float] = []
    ys: list[float] = []
    for m in items:
        name = type(m).__name__
        if name in kinds:
            for p in (m.start, m.end):
                xs.append(float(p[0]))
                ys.append(float(p[1]))
            if name == "CutArc":
                a = Arc(tuple(m.start), tuple(m.end), tuple(m.center), bool(m.ccw))
                for k in range(1, 32):
                    x, y = a.point_at(k / 32)
                    xs.append(x)
                    ys.append(y)
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


# --------------------------------------------------------------------------- tickets
class JobTicket:
    """One request in flight (or answered from the cache)."""

    def __init__(self, request: JobRequest) -> None:
        self.request = request
        self.key: Optional[str] = None
        self.cancel_event = threading.Event()
        self.toolpath_event = threading.Event()
        self.done_event = threading.Event()
        self.toolpath: Optional[list] = None
        self.placed: Optional[Document] = None
        self.plan: Optional[PlannedJob] = None
        # the time estimate of the toolpath stage of a two-stage preview (None: not planned)
        self.estimate: Optional[dict] = None
        self.error: Optional[BaseException] = None
        self.progress = 0.0
        self.cached = False
        self._delivered: set[str] = set()
        self._lock = threading.Lock()
        # two-stage preview: what the compile stage needs (("child",) or ("thread", r, items,
        # placed, start)), whether that compile was queued, and the idle timer's cancel
        self._kept: Optional[tuple] = None
        self._compile_queued = False
        self._idle_cancel: Optional[Callable[[], None]] = None

    def cancel(self) -> None:
        self.cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def toolpath_delivered(self) -> bool:
        """The toolpath went to the ``on_toolpath`` listeners (queued or by :meth:`JobService.settle`)."""
        with self._lock:
            return "toolpath" in self._delivered

    @property
    def deferred(self) -> bool:
        """The toolpath is planned and the compile waits (a two-stage preview)."""
        return self._kept is not None and not self.done_event.is_set()

    def wait(self, timeout: Optional[float] = None) -> Optional[PlannedJob]:
        self.done_event.wait(timeout)
        return self.plan

    def wait_toolpath(self, timeout: Optional[float] = None) -> Optional[list]:
        self.toolpath_event.wait(timeout)
        return self.toolpath

    def _once(self, what: str) -> bool:
        with self._lock:
            if what in self._delivered:
                return False
            self._delivered.add(what)
            return True


def _inline_post(fn: Callable[[], None]) -> None:
    fn()


def _timer_schedule(delay_s: float, fn: Callable[[], None]) -> Callable[[], None]:
    t = threading.Timer(delay_s, fn)
    t.daemon = True
    t.start()
    return t.cancel


# --------------------------------------------------------------------------- service
class JobService:
    """Plan, compile, cache and hand jobs to the controller (see the module docstring).

    Listeners (called through ``post``, i.e. on the GUI thread in the application):
    ``on_toolpath(ticket)``, ``on_estimate(ticket)``, ``on_planned(plan, ticket)``, ``on_failed(ticket, exc)``,
    ``on_progress(ticket, fraction)``."""

    def __init__(
        self,
        executor: Optional[Executor] = None,
        post: Callable[[Callable[[], None]], None] = _inline_post,
        schedule: Callable[[float, Callable[[], None]], Callable[[], None]] = _timer_schedule,
        debounce_s: float = 0.15,
        cache_size: int = 6,
        process: Optional[bool] = None,
        cache_bytes: Optional[int] = CACHE_BYTES,
        compile_idle_s: Optional[float] = None,
    ) -> None:
        if process is None:
            # the plan process by default for the application's own worker; a caller that passes
            # its own executor (tests, tools) compiles in-thread unless it asks for the process
            process = executor is None and process_compile_default()
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlaser-plan")
        self.post = post
        self.schedule = schedule
        self.debounce_s = debounce_s
        self.cache: OrderedDict[str, PlannedJob] = OrderedDict()
        self.cache_size = cache_size
        self.cache_bytes = cache_bytes          # None: no byte bound (count only)
        # two-stage previews (module docstring); None: a preview compiles at once
        self.compile_idle_s = compile_idle_s
        self.preview: Optional[JobTicket] = None
        self.last_run: Optional[JobTicket] = None  # the ticket of the last run() (Start/Frame)
        self.current: Optional[PlannedJob] = None  # the last completed preview plan
        self.on_toolpath: list[Callable[[JobTicket], None]] = []
        self.on_estimate: list[Callable[[JobTicket], None]] = []
        # plan requests that wait for the worker (Start, a settle): the estimate of a toolpath
        # stage in progress gives way to them
        self._waiting = 0
        self.on_planned: list[Callable[[PlannedJob, JobTicket], None]] = []
        self.on_failed: list[Callable[[JobTicket, BaseException], None]] = []
        self.on_progress: list[Callable[[JobTicket, float], None]] = []
        self.start_guards: list[Callable[[str], tuple[bool, str]]] = []
        self.stats = {"compiles": 0, "cache_hits": 0, "cancelled": 0, "last_compile_s": 0.0}
        self._pending: Optional[Callable[[], Optional[JobRequest]]] = None
        self._cancel_timer: Optional[Callable[[], None]] = None
        self._closed = False
        self._lock = threading.Lock()
        from .planproc import PlanCodec, PlanProcess
        self.codec = PlanCodec()
        self.planproc: Optional[PlanProcess] = None
        if process:
            try:
                self.planproc = PlanProcess()
                self.planproc.start()           # spawn now: the first plan does not wait for it
            except Exception as exc:  # noqa: BLE001 - no processes here: compile in-thread
                log.warning("plan process unavailable, compiling in a thread: %s", exc)
                self.planproc = None
        self.stats["process_compiles"] = 0
        self.stats["thread_compiles"] = 0

    @property
    def process_mode(self) -> bool:
        """True while compiles go to the plan process (``planproc``)."""
        return self.planproc is not None

    # ------------------------------------------------------------------ preview (debounced)
    def schedule_preview(
        self, make_request: Callable[[], Optional[JobRequest]], delay_s: Optional[float] = None
    ) -> None:
        """Debounce: ``make_request`` is called (on the scheduling thread) once the edits
        stopped for ``delay_s`` (default ``debounce_s``); the newest call wins."""
        with self._lock:
            self._pending = make_request
            if self._cancel_timer is not None:
                self._cancel_timer()
            d = self.debounce_s if delay_s is None else delay_s
            self._cancel_timer = self.schedule(d, self._flush_timer) if d > 0 else None
        if d <= 0:
            self.flush()

    def _flush_timer(self) -> None:
        self.flush()

    def flush(self) -> Optional[JobTicket]:
        """Run the pending preview request now (returns the preview ticket)."""
        with self._lock:
            make, self._pending = self._pending, None
            if self._cancel_timer is not None:
                self._cancel_timer()
                self._cancel_timer = None
        if make is None:
            return self.preview
        req = make()
        if req is None:
            return self.preview
        return self.request_preview(req)

    def request_preview(self, req: JobRequest) -> JobTicket:
        """Plan ``req`` as the preview; a preview still in flight is cancelled."""
        old = self.preview
        t = self.plan(req, _preview=True)
        if old is not None and old is not t and not old.done_event.is_set():
            old.cancel()
            self._drop_deferred(old)
        return t

    @property
    def pending(self) -> bool:
        return self._pending is not None

    # ------------------------------------------------------------------ planning
    def plan(self, req: JobRequest, _preview: bool = False) -> JobTicket:
        """Plan ``req`` in the worker (answered from the cache when the inputs are known)."""
        if req.mode not in MODES:
            raise ValueError(f"unknown job mode {req.mode!r}")
        t = JobTicket(req)
        if _preview:
            self.preview = t
        if self._closed:
            t.cancel()
            t.toolpath_event.set()
            t.done_event.set()
            return t
        two = _preview and self.compile_idle_s is not None and req.mode != "frame"
        if _preview:
            self.executor.submit(self._work, t, True, "toolpath" if two else "full")
        else:
            self._submit_waiting(t, "full")
        return t

    def _submit_waiting(self, t: JobTicket, stage: str) -> None:
        """Queue ``_work`` for a request someone waits for (counted in ``_waiting`` until it runs)."""
        with self._lock:
            self._waiting += 1
        try:
            self.executor.submit(self._work_waited, t, stage)
        except BaseException:
            with self._lock:
                self._waiting -= 1
            raise

    def _work_waited(self, t: JobTicket, stage: str) -> None:
        with self._lock:
            self._waiting -= 1
        self._work(t, True, stage)

    def _hurry(self) -> bool:
        return self._waiting > 0

    def plan_sync(self, req: JobRequest) -> PlannedJob:
        """Plan in the calling thread (CLI, tests, tools); uses and fills the cache."""
        t = JobTicket(req)
        self._work(t, deliver=False)
        if t.error is not None:
            raise t.error
        assert t.plan is not None
        return t.plan

    def _work(self, t: JobTicket, deliver: bool = True, stage: str = "full") -> None:
        """Plan ``t`` in this thread.  ``stage``: "full" (toolpath and job), "toolpath" (a
        two-stage preview: the toolpath, then the compile waits, :meth:`_arm_compile`) or
        "resume" (the compile of a ticket whose toolpath stage is done)."""
        try:
            if t.cancelled:
                raise Cancelled()
            if stage != "resume":
                t.key = inputs_hash(t.request, self.codec)
            key = t.key or ""
            with self._lock:
                hit = self.cache.get(key)
                if hit is not None:
                    self.cache.move_to_end(key)
            if hit is None and stage == "full":
                hit = self._steal_preview(t, key, deliver)
            if hit is not None:
                self.stats["cache_hits"] += 1
                t.cached = True
                if stage != "resume":
                    t.toolpath, t.placed = list(hit.toolpath), hit.document
                t.plan = hit
                t.toolpath_event.set()
                if deliver and stage != "resume":
                    self._deliver_toolpath(t)
            else:
                plan = self._compile(t, deliver, stage)
                if plan is None:                  # the toolpath stage of a two-stage preview
                    self._arm_compile(t)
                    return
                with self._lock:
                    self.cache[key] = plan
                    self._trim_cache()
                t.plan = plan
            t._kept = None
            t.done_event.set()
            if deliver:
                self.post(lambda: self._deliver_plan(t))
        except Cancelled:
            self.stats["cancelled"] += 1
            t._kept = None
            t.toolpath_event.set()
            t.done_event.set()
        except BaseException as exc:  # noqa: BLE001 - reported to the listeners
            t.error = exc
            t._kept = None
            log.error("planning failed: %s", exc, exc_info=True)
            t.toolpath_event.set()
            t.done_event.set()
            if deliver:
                err: BaseException = exc
                self.post(lambda: self._deliver_error(t, err))

    # ------------------------------------------------------------------ two-stage previews
    def _arm_compile(self, t: JobTicket) -> None:
        """After the toolpath stage of preview ``t``: its compile starts after
        ``compile_idle_s`` without a newer preview (or earlier, :meth:`compile_now`)."""
        delay = self.compile_idle_s or 0.0
        with self._lock:
            if t._compile_queued or t.cancelled or self._closed:
                cancelled = True
            else:
                cancelled = False
                t._idle_cancel = _timer_schedule(delay, lambda: self.compile_now(t, idle=True)) if delay > 0 else None
        if cancelled:
            if t.cancelled or self._closed:
                self._drop_deferred(t)
            return
        if delay <= 0:
            self.compile_now(t, idle=True)

    def compile_now(self, t: Optional[JobTicket] = None, idle: bool = False) -> None:
        """Queue the compile of a two-stage preview whose toolpath is done (default: the
        current preview); nothing when it is queued already, finished or cancelled.  ``idle``:
        the idle timer asked (nobody waits for it: the estimate in progress is finished first)."""
        t = t if t is not None else self.preview
        if t is None:
            return
        with self._lock:
            if t._compile_queued or t._kept is None or t.done_event.is_set():
                return
            t._compile_queued = True
            cancel, t._idle_cancel = t._idle_cancel, None
        if cancel is not None:
            cancel()
        if t.cancelled or self._closed:
            self._drop_deferred(t)
            return
        try:
            if idle:
                self.executor.submit(self._work, t, True, "resume")
            else:
                self._submit_waiting(t, "resume")
        except RuntimeError:                       # the executor was shut down
            self._drop_deferred(t)

    def _drop_deferred(self, t: JobTicket) -> None:
        """A deferred preview that will not be compiled (superseded, closed): it is done."""
        with self._lock:
            if t._kept is None or t.done_event.is_set():
                return
            if t._compile_queued and not (t.cancelled or self._closed):
                return
            t._compile_queued = True
            cancel, t._idle_cancel = t._idle_cancel, None
            t._kept = None
        if cancel is not None:
            cancel()
        t.cancel()
        self.stats["cancelled"] += 1
        t.toolpath_event.set()
        t.done_event.set()

    def _steal_preview(self, t: JobTicket, key: str, deliver: bool) -> Optional[PlannedJob]:
        """A plan request (Start, Dry Cut, an export) whose inputs are those of a deferred
        preview: that preview is compiled now, in this worker call, from its kept toolpath,
        and its plan answers both (``None``: there is no such preview)."""
        p = self.preview
        if p is None or p is t or p.key != key or p.cancelled:
            return None
        with self._lock:
            if p._compile_queued or p._kept is None or p.done_event.is_set():
                return None
            p._compile_queued = True
            cancel, p._idle_cancel = p._idle_cancel, None
        if cancel is not None:
            cancel()
        self._work(p, deliver, "resume")
        with self._lock:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache.move_to_end(key)
        return hit

    # ------------------------------------------------------------------ the cache
    def _trim_cache(self) -> None:
        """Drop the oldest plans beyond ``cache_size`` plans and ``cache_bytes`` bytes; the
        newest plan and the current preview plan stay (call with ``_lock`` held)."""
        cache = self.cache
        if not cache:
            return
        newest = next(reversed(cache))
        cur = self.current.key if self.current is not None else None

        def over() -> bool:
            if len(cache) > self.cache_size:
                return True
            return self.cache_bytes is not None and sum(plan_nbytes(v) for v in cache.values()) > self.cache_bytes
        while len(cache) > 1 and over():
            victim = next((k for k in cache if k != newest and k != cur), None)
            if victim is None:
                break
            del cache[victim]

    @property
    def cache_nbytes(self) -> int:
        """Estimated bytes of the cached plans (:func:`plan_nbytes`)."""
        with self._lock:
            return sum(plan_nbytes(v) for v in self.cache.values())

    def release_document(self) -> None:
        """The open document was closed or replaced: forget its plans and the plan codec's
        part encodings (they hold its part copies) here and in the plan process.  A preview
        in flight is left alone (it plans the new document)."""
        with self._lock:
            self.cache.clear()
            self.current = None
        self.codec.forget()
        if self.planproc is not None and not self._closed:
            try:
                self.executor.submit(self._forget_child)
            except RuntimeError:                   # shut down
                pass

    def _forget_child(self) -> None:
        pp = self.planproc
        if pp is not None:
            pp.forget()

    def _compile(self, t: JobTicket, deliver: bool, stage: str = "full") -> Optional[PlannedJob]:
        """Compile ``t``: in the plan process when there is one (``planproc``), else in this
        thread on a private copy of the request with the GIL switch interval lowered.
        ``stage`` as for :meth:`_work`; None after a "toolpath" stage (``t._kept`` is set)."""
        from .planproc import ChildLost, ChildSetupError

        t0 = time.perf_counter()
        pp = self.planproc
        kept = t._kept
        if pp is not None and not self._closed and not (stage == "resume" and kept is not None and kept[0] != "child"):
            try:
                plan = self._compile_process(t, deliver, pp, t0, stage)
                if plan is not None:
                    self.stats["process_compiles"] += 1
                return plan
            except (ChildLost, ChildSetupError) as exc:
                if t.cancelled:
                    raise Cancelled() from exc
                if isinstance(exc, ChildLost) and pp.failures >= pp.MAX_FAILURES:
                    log.warning("plan process failed %d times, compiling in a thread from now on: %s",
                                pp.failures, exc)
                    self.planproc = None
                    pp.close()
                else:
                    log.warning("plan process: %s; this plan is compiled in a thread", exc)
        plan = self._compile_thread(t, deliver, t0, stage)
        if plan is not None:
            self.stats["thread_compiles"] += 1
        return plan

    def _progress_fn(self, t: JobTicket, deliver: bool) -> Callable[[float], None]:
        def progress(frac: float) -> None:
            if t.cancelled:
                raise Cancelled()
            t.progress = frac
            if deliver and self.on_progress:

                def report(f: float = frac) -> None:
                    for fn in list(self.on_progress):
                        fn(t, f)

                self.post(report)
        return progress

    def _set_toolpath(self, t: JobTicket, items: list, placed: Document, deliver: bool) -> None:
        t.toolpath, t.placed = items, placed
        t.toolpath_event.set()
        if deliver and t.request.mode != "frame":
            self._deliver_toolpath(t)
        if t.cancelled:
            raise Cancelled()

    def _set_estimate(self, t: JobTicket, est: dict, deliver: bool) -> None:
        t.estimate = dict(est)
        if deliver:
            self._deliver_estimate(t)

    def _finish(self, t: JobTicket, job, start, placed: Document, t0: float, bounds=None,
                cut_bounds=None) -> PlannedJob:
        req = t.request
        dt = time.perf_counter() - t0
        self.stats["compiles"] += 1
        self.stats["last_compile_s"] = dt
        return PlannedJob(
            key=t.key or "",
            mode=req.mode,
            toolpath=tuple(job.toolpath),
            job=job,
            start=(float(start[0]), float(start[1])),
            bounds=bounds if bounds is not None else toolpath_bounds(job.toolpath),
            estimate=dict(job.estimate or {}),
            profile=req.profile,
            document=placed,
            source=req.source,
            compile_s=dt,
            selected_only=req.selected_only,
            cut_bounds=cut_bounds if cut_bounds is not None else toolpath_bounds(job.toolpath, ("CutLine", "CutArc")),
        )

    def _compile_thread(self, t: JobTicket, deliver: bool, t0: float, stage: str = "full") -> Optional[PlannedJob]:
        from mlaser._sched import responsive_gil

        from .planproc import run_compile, run_estimate, run_job, run_toolpath

        req = t.request
        kept = t._kept
        if stage == "resume" and kept is not None and kept[0] == "thread":
            _k, r0, items0, placed0, start0 = kept
            with responsive_gil():
                job = run_job(r0, items0, placed0, start0, self._progress_fn(t, deliver))
            return self._finish(t, job, start0, t.placed if t.placed is not None else placed0, t0)
        try:
            # a private copy: the planner snapshot's parts are shared between plans
            # (DocumentService.plan_snapshot) and the compile may cache derived data in them
            r = self.codec.decode_local(self.codec.encode(req))
        except Exception:  # noqa: BLE001 - unpicklable inputs: compile the request itself
            r = {"document": req.document, "mode": req.mode, "machine": req.machine, "start": req.start,
                 "cam_params": req.cam_params, "layers": req.layers, "dry_run": req.dry_run,
                 "profile": req.profile}
        box: list = []

        def on_toolpath(items: list, placed: Document) -> None:
            # as in the plan process: a placement that changed no part gives the request's parts
            # (the private copies may carry derived caches the compile added, e.g. scan paths)
            placed = _placed_in_request(req.document, r["document"], placed)
            box.append(placed)
            self._set_toolpath(t, list(items) if req.mode == "frame" else items, placed, deliver)

        if stage == "toolpath":
            with responsive_gil():
                items, placed_r, start = run_toolpath(r)
            on_toolpath(items, placed_r)
            t._kept = ("thread", r, items, placed_r, start)
            with responsive_gil():
                est = run_estimate(r, items, lambda: t.cancelled or self._hurry())
            if t.cancelled:
                raise Cancelled()
            if est is not None:
                self._set_estimate(t, est, deliver)
            return None
        with responsive_gil():
            items, _placed, start, job = run_compile(r, self._progress_fn(t, deliver), on_toolpath)
        return self._finish(t, job, start, box[0], t0)

    def _compile_process(self, t: JobTicket, deliver: bool, pp, t0: float, stage: str = "full") -> Optional[PlannedJob]:
        from .planproc import ChildSetupError, placed_from_info

        req = t.request
        pp.ensure_ready()
        placed_box: list = []

        def on_toolpath(items: list, info: dict) -> None:
            placed = placed_from_info(req.document, info)
            placed_box.append(placed)
            if t.toolpath is not None:            # a resume that had to plan again: same inputs
                return
            self._set_toolpath(t, items, placed, deliver)

        info: Any = None
        job: Any = None
        if stage == "resume" and t._kept is not None:
            try:                                  # the toolpath the child kept for this key
                info, job = pp.compile(None, lambda: t.cancelled, self._progress_fn(t, deliver), on_toolpath,
                                       kept=t.key)
                placed_box.append(t.placed)
            except ChildSetupError:               # it planned something else since: from the start
                info = job = None
        if job is None:
            msg = self.codec.encode(req, have=pp.have)
            if stage == "toolpath":
                msg["stage"], msg["key"] = "toolpath", t.key
            if t.cancelled:
                raise Cancelled()
            info, job = pp.compile(msg, lambda: t.cancelled, self._progress_fn(t, deliver), on_toolpath,
                                   on_estimate=lambda est: self._set_estimate(t, est, deliver),
                                   hurry=self._hurry)
            if stage == "toolpath":
                t._kept = ("child",)
                return None
        self.stats["child_parallel"] = info.get("parallel", {})   # nc.parallel.STATS of the child
        if info.get("toolpath") is None:
            job.toolpath = list(t.toolpath or [])
        else:
            tp: list = []
            for ch in info["toolpath"]:
                tp.extend(pickle.loads(ch))
            job.toolpath = tp
        return self._finish(t, job, info["start"], placed_box[0], t0, info["bounds"], info["cut_bounds"])

    # ------------------------------------------------------------------ delivery (owner thread)
    def _deliver_toolpath(self, t: JobTicket) -> None:
        def run():
            if t.cancelled or not t._once("toolpath"):
                return
            for fn in list(self.on_toolpath):
                fn(t)

        self.post(run)

    def _deliver_estimate(self, t: JobTicket) -> None:
        def run():
            if t.cancelled or not t._once("estimate"):
                return
            for fn in list(self.on_estimate):
                fn(t)

        self.post(run)

    def _deliver_plan(self, t: JobTicket) -> None:
        if t.cancelled or t.plan is None or not t._once("plan"):
            return
        if t is self.preview:
            self.current = t.plan
            with self._lock:
                self._trim_cache()                # the plan before it may go now
        for fn in list(self.on_planned):
            fn(t.plan, t)

    def _deliver_error(self, t: JobTicket, exc: BaseException) -> None:
        if not t._once("error"):
            return
        for fn in list(self.on_failed):
            fn(t, exc)

    def settle(self, t: Optional[JobTicket] = None, timeout: float = 60.0, toolpath_only: bool = False):
        """Block until ``t`` (default: the preview, flushing a pending debounce) is planned and
        deliver its results now (the queued deliveries become no-ops).  For tests and for
        commands that need the result synchronously (Sim)."""
        if t is None:
            t = self.flush() if self.pending else self.preview
        if t is None:
            return None
        if not toolpath_only and t is self.preview and self.compile_idle_s is not None:
            # a two-stage preview: its compile now, once its toolpath stage has ended
            end = time.monotonic() + timeout
            with self._lock:
                self._waiting += 1            # its estimate gives way (the compile gives it)
            try:
                while (not t.done_event.is_set() and t._kept is None and not t.cancelled
                       and time.monotonic() < end):
                    t.done_event.wait(0.005)
            finally:
                with self._lock:
                    self._waiting -= 1
            self.compile_now(t)
        if toolpath_only:
            t.wait_toolpath(timeout)
            if t.toolpath is not None and not t.cancelled and t._once("toolpath"):
                for fn in list(self.on_toolpath):
                    fn(t)
            return t.toolpath
        t.wait(timeout)
        if t.toolpath is not None and not t.cancelled and t._once("toolpath"):
            for fn in list(self.on_toolpath):
                fn(t)
        if t.plan is not None:
            self._deliver_plan(t)
        elif t.error is not None:
            self._deliver_error(t, t.error)
        return t.plan

    # ------------------------------------------------------------------ running
    def add_start_guard(self, fn: Callable[[str], tuple[bool, str]]) -> None:
        """``fn(mode) -> (allowed, message)`` is asked before a job is handed to the controller
        (the production interlock; Safety's pre-flight belongs in the controller's start_job)."""
        self.start_guards.append(fn)

    def check_start(self, mode: str) -> tuple[bool, str]:
        for g in list(self.start_guards):
            ok, msg = g(mode)
            if not ok:
                return False, msg
        return True, ""

    def run(
        self,
        req: JobRequest,
        submitter: JobSubmitter,
        on_result: Callable[[bool, str], None] = lambda ok, msg: None,
    ) -> Optional[JobTicket]:
        """Plan ``req`` (a cache hit when the preview plan matches) and, on the owner thread,
        hand the PlannedJob to ``submitter.start_job``.  ``on_result(ok, message)`` reports."""
        ok, msg = self.check_start(req.mode)
        if not ok:
            on_result(False, msg)
            return None
        # the listeners are in place before the plan is submitted: with an inline post the worker
        # delivers on its own thread, possibly before ``self.plan`` has returned, and the
        # delivery is one-shot (registering after the submit could lose it, and ``on_result``
        # would never be called).  A delivery before the ticket is known is kept in ``early``
        # and replayed; ``gate`` only orders that hand-over.
        gate = threading.RLock()
        mine: list[JobTicket] = []
        early: list[tuple] = []        # a delivery that came before the ticket was known

        def done(plan: PlannedJob, ticket: JobTicket) -> None:
            with gate:
                if not mine:
                    early.append((_done, plan, ticket))
                    return
                if ticket is not mine[0]:
                    return
            _done(plan, ticket)

        def _done(plan: PlannedJob, ticket: JobTicket) -> None:
            if ticket is not mine[0]:
                return
            self.on_planned.remove(done)
            self.on_failed.remove(failed)
            ok2, msg2 = self.check_start(plan.mode)
            if not ok2:
                on_result(False, msg2)
                return
            try:
                started = submitter.start_job(plan)
            except Exception as exc:  # noqa: BLE001 - reported
                on_result(False, f"start failed: {exc}")
                return
            then = getattr(started, "then", None)
            if callable(then) and hasattr(started, "dropped"):
                # given on the GUI thread: the start (start gate: card reads) runs on the
                # controller's command worker; the result comes when it ended (issue #5)
                then(lambda t: _started(plan, False if t.dropped or t.error is not None else t.result))
                return
            _started(plan, started)

        def _started(plan: PlannedJob, started) -> None:
            if started is False:
                # refused by the controller (pre-flight, start gate): it logged why
                on_result(False, "start refused by the controller")
                return
            on_result(True, plan.summary())

        def failed(ticket: JobTicket, exc: BaseException) -> None:
            with gate:
                if not mine:
                    early.append((_failed, ticket, exc))
                    return
                if ticket is not mine[0]:
                    return
            _failed(ticket, exc)

        def _failed(ticket: JobTicket, exc: BaseException) -> None:
            self.on_planned.remove(done)
            self.on_failed.remove(failed)
            on_result(False, f"compile failed: {exc}")

        self.on_planned.append(done)
        self.on_failed.append(failed)
        t = self.plan(req)             # never under ``gate``: an executor may wait for its worker
        with gate:
            mine.append(t)
            self.last_run = t
            pending = [e for e in early if (e[2] if e[0] is _done else e[1]) is t]
        for fn, *args in pending:      # inline executor: delivered inside ``self.plan``
            fn(*args)
        if t.done_event.is_set() and not pending:
            self.settle(t)             # done, but not delivered yet (a queued post): now
        return t

    def cancel(self) -> None:
        if self.preview is not None:
            self.preview.cancel()
            self._drop_deferred(self.preview)

    def shutdown(self) -> None:
        self._closed = True
        with self._lock:
            self._pending = None
            if self._cancel_timer is not None:
                self._cancel_timer()
                self._cancel_timer = None
        self.cancel()
        try:
            self.executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self.executor.shutdown(wait=False)
        pp, self.planproc = self.planproc, None
        if pp is not None:
            pp.close()


def plan_nbytes(plan: PlannedJob) -> int:
    """Estimated bytes a cached plan keeps alive: the wire (a PackedWire's arrays, else about
    120 bytes an item), the toolpath's MotionItems and the job's bookkeeping lists.  The
    placed document is shared with the planner snapshot and not counted."""
    job = plan.job
    w = getattr(job, "wire", None)
    nb = getattr(w, "nbytes", None)
    n = int(nb) if isinstance(nb, int) else 120 * len(w or ())
    n += 240 * len(plan.toolpath)
    for name in ("manu", "markers", "cut_blocks"):
        n += 100 * len(getattr(job, name, None) or ())
    return n


def _placed_in_request(req_doc: Document, copy_doc: Document, placed: Document) -> Document:
    """``placed`` (compiled from ``copy_doc``, a private copy of ``req_doc``) with the request's
    own part objects and ``extra`` where the placement kept those of the copy."""
    if copy_doc is req_doc:
        return placed
    same = placed.parts is copy_doc.parts or (
        len(placed.parts) == len(copy_doc.parts) and all(a is b for a, b in zip(placed.parts, copy_doc.parts)))
    if not same and placed.extra is not copy_doc.extra:
        return placed
    return Document(parts=list(req_doc.parts) if same else placed.parts, layers=placed.layers,
                    version=placed.version, extra=req_doc.extra if placed.extra is copy_doc.extra else placed.extra)


def process_compile_default() -> bool:
    """Whether the application's JobService compiles in the plan process: on, unless the
    environment says ``MLASER_PLAN_PROCESS=0`` (the test suite does, except where it tests the
    process path; tools that patch the compiler in-process need it too) or the main script has
    no ``__main__`` guard."""
    import os

    from mlaser.nc.parallel import _main_is_safe

    if os.environ.get("MLASER_PLAN_PROCESS", "1").strip().lower() in ("0", "false", "no", "off", ""):
        return False
    # a spawned child imports the main script again: one without an ``if __name__ ==
    # "__main__":`` guard would run again in the plan process (the per-chain pool's rule)
    return _main_is_safe()


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --------------------------------------------------------------------------- requests
def build_request(mode: str, docs, ctl, machine, cam_params, selected_only: bool = False) -> JobRequest:
    """The JobRequest for the open document and the current controller, on the GUI thread: the
    head position is the start (and the sort origin, as the controller compiled before), the
    controller's placement (``place``) is applied, the selection is taken when
    ``selected_only``, and a disabled laser makes any cut a dry run."""
    from mlaser.parity_ports import active_profile

    doc = docs.document
    if selected_only:
        doc = Document(
            parts=[p for p in doc.parts if p.selected],
            layers=doc.layers,
            version=doc.version,
            extra=doc.extra,
        )
    try:
        st = ctl.state()
        start = (float(st.x), float(st.y))
    except Exception:  # noqa: BLE001 - no controller state: machine origin
        start = (0.0, 0.0)
    snap = getattr(docs, "plan_snapshot", None) or docs.snapshot
    source = snap(doc)
    place = getattr(ctl, "place", None)
    placed = place(source) if callable(place) else source
    if placed is not source:
        placed = Document(
            parts=placed.parts, layers=placed.layers, version=placed.version, extra=copy.copy(placed.extra)
        )
    laser_on = bool(getattr(ctl, "laser_enabled", True))
    from mlaser.cam.toolpath import CamParams

    cam = CamParams(**vars(cam_params)) if cam_params is not None else None
    return JobRequest(
        mode=mode,
        document=placed,
        machine=copy.deepcopy(machine),
        start=start,
        cam_params=cam,
        layers=getattr(ctl, "layers", None),
        dry_run=(mode != "process") or not laser_on,
        profile=active_profile().name,
        source=source,
        selected_only=selected_only,
    )


# --------------------------------------------------------------------------- submitter
class ControllerSubmitter:
    """:class:`JobSubmitter` for the controllers: forwards the plan to the controller's own
    ``start_job(plan, generation=...)`` (UiController, DemoController: pre-flight, the start
    gate, then streaming), with the start generation read when this submitter was made (at the
    operator's request)."""

    def __init__(self, ctl, log: Callable[[str], None] = lambda m: None) -> None:
        self.ctl = ctl
        self.log = log
        # the controller's start generation when the operator asked for the start (made with
        # the request, see MainWindow.run_job): a Stop or a halt while the plan compiles makes
        # the controller's start gate refuse it (review 2 P0-2)
        gen = getattr(ctl, "start_generation", None)
        self.generation: Optional[int] = gen() if callable(gen) else None

    def start_job(self, plan: PlannedJob) -> Optional[bool]:
        """Returns False when the controller refused the start (None: not known)."""
        ctl = self.ctl
        fn = getattr(ctl, "start_job", None)
        if callable(fn):
            if self.generation is not None:
                return fn(plan, generation=self.generation)
            return fn(plan)
        raise TypeError(f"{type(ctl).__name__} cannot run a planned job (no start_job)")


@dataclass
class InlineExecutor(Executor):
    """Runs submissions immediately (tests, CLI)."""

    calls: int = field(default=0)

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future

        self.calls += 1
        f: Future = Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001 - carried by the Future, as a real executor does
            f.set_exception(e)
        return f
