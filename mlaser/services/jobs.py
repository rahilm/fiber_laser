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
        self.error: Optional[BaseException] = None
        self.progress = 0.0
        self.cached = False
        self._delivered: set[str] = set()
        self._lock = threading.Lock()

    def cancel(self) -> None:
        self.cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

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
    ``on_toolpath(ticket)``, ``on_planned(plan, ticket)``, ``on_failed(ticket, exc)``,
    ``on_progress(ticket, fraction)``."""

    def __init__(
        self,
        executor: Optional[Executor] = None,
        post: Callable[[Callable[[], None]], None] = _inline_post,
        schedule: Callable[[float, Callable[[], None]], Callable[[], None]] = _timer_schedule,
        debounce_s: float = 0.15,
        cache_size: int = 6,
        process: Optional[bool] = None,
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
        self.preview: Optional[JobTicket] = None
        self.last_run: Optional[JobTicket] = None  # the ticket of the last run() (Start/Frame)
        self.current: Optional[PlannedJob] = None  # the last completed preview plan
        self.on_toolpath: list[Callable[[JobTicket], None]] = []
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
        self.executor.submit(self._work, t)
        return t

    def plan_sync(self, req: JobRequest) -> PlannedJob:
        """Plan in the calling thread (CLI, tests, tools); uses and fills the cache."""
        t = JobTicket(req)
        self._work(t, deliver=False)
        if t.error is not None:
            raise t.error
        assert t.plan is not None
        return t.plan

    def _work(self, t: JobTicket, deliver: bool = True) -> None:
        try:
            if t.cancelled:
                raise Cancelled()
            t.key = key = inputs_hash(t.request, self.codec)
            with self._lock:
                hit = self.cache.get(key)
                if hit is not None:
                    self.cache.move_to_end(key)
            if hit is not None:
                self.stats["cache_hits"] += 1
                t.cached = True
                t.toolpath, t.placed, t.plan = list(hit.toolpath), hit.document, hit
                t.toolpath_event.set()
                if deliver:
                    self._deliver_toolpath(t)
            else:
                plan = self._compile(t, deliver)
                with self._lock:
                    self.cache[key] = plan
                    while len(self.cache) > self.cache_size:
                        self.cache.popitem(last=False)
                t.plan = plan
            t.done_event.set()
            if deliver:
                self.post(lambda: self._deliver_plan(t))
        except Cancelled:
            self.stats["cancelled"] += 1
            t.toolpath_event.set()
            t.done_event.set()
        except BaseException as exc:  # noqa: BLE001 - reported to the listeners
            t.error = exc
            log.error("planning failed: %s", exc, exc_info=True)
            t.toolpath_event.set()
            t.done_event.set()
            if deliver:
                err: BaseException = exc
                self.post(lambda: self._deliver_error(t, err))

    def _compile(self, t: JobTicket, deliver: bool) -> PlannedJob:
        """Compile ``t``: in the plan process when there is one (``planproc``), else in this
        thread on a private copy of the request with the GIL switch interval lowered."""
        from .planproc import ChildLost, ChildSetupError

        t0 = time.perf_counter()
        pp = self.planproc
        if pp is not None and not self._closed:
            try:
                plan = self._compile_process(t, deliver, pp, t0)
                self.stats["process_compiles"] += 1
                return plan
            except (ChildLost, ChildSetupError) as exc:
                if t.cancelled:
                    raise Cancelled() from exc
                if isinstance(exc, ChildLost) and pp.failures >= 3:
                    log.warning("plan process failed %d times, compiling in a thread from now on: %s",
                                pp.failures, exc)
                    self.planproc = None
                    pp.close()
                else:
                    log.warning("plan process: %s; this plan is compiled in a thread", exc)
        plan = self._compile_thread(t, deliver, t0)
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

    def _compile_thread(self, t: JobTicket, deliver: bool, t0: float) -> PlannedJob:
        from mlaser.controller.stream import responsive_gil

        from .planproc import run_compile

        req = t.request
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

        with responsive_gil():
            items, _placed, start, job = run_compile(r, self._progress_fn(t, deliver), on_toolpath)
        return self._finish(t, job, start, box[0], t0)

    def _compile_process(self, t: JobTicket, deliver: bool, pp, t0: float) -> PlannedJob:
        from .planproc import placed_from_info

        req = t.request
        pp.ensure_ready()
        msg = self.codec.encode(req, have=pp.have)
        if t.cancelled:
            raise Cancelled()
        placed_box: list = []

        def on_toolpath(items: list, info: dict) -> None:
            placed = placed_from_info(req.document, info)
            placed_box.append(placed)
            self._set_toolpath(t, items, placed, deliver)

        info, job = pp.compile(msg, lambda: t.cancelled, self._progress_fn(t, deliver), on_toolpath)
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

    def _deliver_plan(self, t: JobTicket) -> None:
        if t.cancelled or t.plan is None or not t._once("plan"):
            return
        if t is self.preview:
            self.current = t.plan
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
        t = self.plan(req)
        self.last_run = t

        def done(plan: PlannedJob, ticket: JobTicket) -> None:
            if ticket is not t:
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
            if started is False:  # refused by the controller (pre-flight, start gate): it logged why
                on_result(False, "start refused by the controller")
                return
            on_result(True, plan.summary())

        def failed(ticket: JobTicket, exc: BaseException) -> None:
            if ticket is not t:
                return
            self.on_planned.remove(done)
            self.on_failed.remove(failed)
            on_result(False, f"compile failed: {exc}")

        self.on_planned.append(done)
        self.on_failed.append(failed)
        if t.done_event.is_set():  # inline executor: already done, deliver now
            self.settle(t)
        return t

    def cancel(self) -> None:
        if self.preview is not None:
            self.preview.cancel()

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
    process path; tools that patch the compiler in-process need it too)."""
    import os

    return os.environ.get("MLASER_PLAN_PROCESS", "1").strip().lower() not in ("0", "false", "no", "off", "")


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
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f
