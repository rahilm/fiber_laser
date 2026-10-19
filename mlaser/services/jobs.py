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

    def start_job(self, job: PlannedJob) -> None: ...


# --------------------------------------------------------------------------- hashing
class _KeyPickler(pickle.Pickler):
    """Pickles the inputs for the hash: round-trip records by identity (they are immutable,
    services.undo.RoundTripTable), parts and contours without selection flags and uids."""

    def __init__(self, f, blob_ids: set[int]) -> None:
        super().__init__(f, protocol=5)
        self._blob_ids = blob_ids

    def persistent_id(self, obj):
        if id(obj) in self._blob_ids:
            return ("blob", id(obj))
        return None

    def reducer_override(self, obj):
        # the opaque ``extra`` data plus the typed fields (mlaser.model), uid as 0
        if type(obj) is Part:
            return (Part, (obj.contours, obj.craft, obj.name, False, dict(dict.items(obj.extra)), 0, obj.raster))
        if type(obj) is Craft:
            # the lazily created ``extra`` (mlaser.model) must not change the key: a craft whose
            # extra was never read pickles like one with an empty dict
            import dataclasses
            state = tuple(dict(dict.items(obj.extra)) if f.name == "extra" else getattr(obj, f.name)
                          for f in dataclasses.fields(obj))
            return (_craft_from_state, (state,))
        if type(obj) is Contour:
            return (Contour, (obj.segments, obj.closed, dict(dict.items(obj.extra)), 0, obj.craft, obj.glyphs,
                              obj.glyph_key))
        return NotImplemented


def _craft_from_state(state: tuple) -> "Craft":   # (key pickles are never loaded)
    return Craft(*state)


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


def inputs_hash(req: JobRequest) -> str:
    """sha256 of every input of the compile.  Unpicklable inputs fall back to a per-object key
    (no cache hit, still correct)."""
    h = hashlib.sha256()
    buf = io.BytesIO()
    try:
        _KeyPickler(buf, _blob_ids(req.document)).dump(
            (
                req.mode,
                req.document,
                req.machine,
                tuple(req.start),
                _plain(req.cam_params),
                req.layers,
                bool(req.dry_run),
                req.profile,
            )
        )
        h.update(buf.getvalue())
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
    ) -> None:
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
            t.key = key = inputs_hash(t.request)
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
        from mlaser.nc.compiler import compile_items, frame_items, placed_document, plan_toolpath
        from mlaser.parity_ports import active_profile, use_profile

        req = t.request
        t0 = time.perf_counter()

        def progress(frac: float) -> None:
            if t.cancelled:
                raise Cancelled()
            t.progress = frac
            if deliver and self.on_progress:

                def report(f: float = frac) -> None:
                    for fn in list(self.on_progress):
                        fn(t, f)

                self.post(report)

        ctx = use_profile(req.profile) if active_profile().name != req.profile else _null()
        with ctx:
            if req.mode == "frame":
                placed = placed_document(req.document)
                items = frame_items(placed.bbox(), req.machine.frame_speed)
                start = (float(req.start[0]), float(req.start[1]))
                t.toolpath, t.placed = list(items), placed
                t.toolpath_event.set()
                job = compile_items(items, req.machine, start, dry_run=True, progress=progress)
            else:
                placed, prm, items = plan_toolpath(req.document, req.cam_params, req.start, req.layers)
                start = prm.origin
                t.toolpath, t.placed = items, placed
                t.toolpath_event.set()
                if deliver:
                    self._deliver_toolpath(t)
                if t.cancelled:
                    raise Cancelled()
                job = compile_items(items, req.machine, start, placed, req.dry_run, progress=progress)
        dt = time.perf_counter() - t0
        self.stats["compiles"] += 1
        self.stats["last_compile_s"] = dt
        return PlannedJob(
            key=t.key or "",
            mode=req.mode,
            toolpath=tuple(job.toolpath),
            job=job,
            start=(float(start[0]), float(start[1])),
            bounds=toolpath_bounds(job.toolpath),
            estimate=dict(job.estimate or {}),
            profile=req.profile,
            document=placed,
            source=req.source,
            compile_s=dt,
            selected_only=req.selected_only,
            cut_bounds=toolpath_bounds(job.toolpath, ("CutLine", "CutArc")),
        )

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
                submitter.start_job(plan)
            except Exception as exc:  # noqa: BLE001 - reported
                on_result(False, f"start failed: {exc}")
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
    source = docs.snapshot(doc)
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
    """:class:`JobSubmitter` for today's controllers (merge point: the Safety branch gives the
    controller its own ``start_job``; then this adapter only forwards).

    * a controller with ``start_job(planned)`` (DemoController, the future MachineAPI): forwarded;
    * ``UiController``: the checks of ``_start_doc`` (connected, idle, no alarms), its job-start
      hooks with the unplaced document, then ``_run_job(plan.job, ...)`` - no recompile."""

    def __init__(self, ctl, log: Callable[[str], None] = lambda m: None) -> None:
        self.ctl = ctl
        self.log = log

    def start_job(self, plan: PlannedJob) -> None:
        ctl = self.ctl
        fn = getattr(ctl, "start_job", None)
        if callable(fn):
            fn(plan)
            return
        run = getattr(ctl, "_run_job", None)
        if not callable(run):
            raise TypeError(f"{type(ctl).__name__} cannot run a planned job")
        motion_ok = getattr(ctl, "_motion_ok", None)
        busy = getattr(ctl, "_busy", lambda: False)
        if callable(motion_ok) and not motion_ok("frame" if plan.mode == "frame" else "start"):
            return
        if busy():
            ctl._log("a job is already running")
            return
        refuse = getattr(ctl, "_refuse", lambda rep: False)
        preflight = getattr(ctl, "preflight", None)
        if plan.mode == "frame":
            if callable(preflight) and refuse(preflight("frame", job=plan.job, emits=False)):
                return
            x0, y0, x1, y1 = plan.document.bbox()
            run(plan.job, "frame", f"frame {x1 - x0:.1f} x {y1 - y0:.1f} mm", track=False)
            return
        if getattr(ctl, "active_alarms", None):
            ctl._log("mp74: cannot start while alarms are active (Clear Alarm first)")
            return
        src = plan.source if plan.source is not None else plan.document
        ctl.document = src
        for hook in list(getattr(ctl, "job_start_hooks", [])):
            try:
                hook(src)
            except Exception as e:  # noqa: BLE001 - a hook must not stop the job (as _start_doc)
                ctl._log(f"job start hook failed: {e}")
        if not plan.job.wire:
            ctl._log("mp74: nothing to cut")
            return
        job_emits = getattr(ctl, "job_emits", None)
        emits = bool(job_emits(plan.job)) if callable(job_emits) else not plan.job.dry_run
        # the controller's pre-flight on the planned job (travel, homed, armed, layers, alarms)
        if callable(preflight) and refuse(preflight("start" if emits else "dry_run", src, plan.selected_only,
                                                    job=plan.job, emits=emits)):
            return
        if plan.job.dry_run and plan.mode == "process":
            ctl._log("laser disabled: running the job as a dry run")
        try:
            run(plan.job, plan.mode, plan.job.summary(), emits=emits)
        except TypeError:                                  # a controller without the emits flag
            run(plan.job, plan.mode, plan.job.summary())


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
