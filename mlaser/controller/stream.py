"""Streaming a compiled :class:`~mlaser.nc.compiler.Job` to the card (docs/re/protocol.md §5, §13, §15).

Original (observed, NCModule.dll):

* fillFifo ``FUN_10052e90``: frame sequence from RO[15]+1, a frame is sent only while RO[16]
  (free bytes) >= frame bytes + 2000, at most 20 frames per call, one retry after 50 ms.
* mcCoreProcess ``FUN_10058030``: each worker tick calls fillFifo (VM +0x1e8) and, the first time
  the job is ready, writes the step-format command ``[9999, 1, mask|0x66800000, 0]``
  (``FUN_10051e70``) and FIFO start 103 := 2 (``FUN_100515e0``), so the original starts after one
  fill of <= 20 frames (about 2000 step items).
* The card itself flags an underrun: RO[7] bit 5 "FIFO starvation" (``EtherCATErrorInfo_2_05``,
  MainApp ``FUN_00598280``), which MainApp treats like any alarm during a job (pause/stop).
* The card's FIFO capacity is not a constant in NCModule; only RO[16] (free bytes) is used.
  The streamer therefore measures it: ``capacity = RO[16]`` right after the clear.

Rebuild (docs/SAFETY.md §6):

* :class:`JobStreamer` (in-thread) keeps at most ``max_buffer_ms`` (default 1200 ms) of motion
  in the card FIFO, prefill included: whatever happens to the PC or the link, the card runs on
  for at most that long.  It keeps the original's 2000-byte margin and reports progress and the
  buffered motion (``min_buffer_ms``).
* **No automatic restart.**  The streamer never starts the FIFO on its own after it has been
  started, except to release its own hold (``hold()`` going False).  An unexpected idle (an
  external stop, an alarm, the FIFO running dry) and a buffer underrun (``watchdog_ms``: the
  laser is switched off and the FIFO stopped before the card runs dry) raise
  :class:`StreamFault`; the controller latches a FAULT and the operator continues through the
  breakpoint with a re-pierce (``nc.resume``).
* :class:`ProcessStreamer` runs the same JobStreamer in a separate OS process (``spawn``) with
  its own socket, so a busy GUI process (GIL) cannot starve the stream.  While it runs, the
  child is the only user of the main card connection (the parent polls nothing; the child
  relays the status).  Supervision both ways:

  - the child halts the card itself (:func:`controller.supervisor.run_halt` on its own short-
    timeout socket) and exits when its parent disappears (``os.getppid()`` changes), when the
    parent's heartbeat counter stops for ``parent_timeout_s``, or when no reply arrived from the
    card for ``link_deadline_ms``; it applies the parent's safety policy to every request it
    sends (no emission unless the job was started armed, nothing after a cancel);
  - the parent raises :class:`StreamFault` when the child dies or stops relaying, and the
    controller's worker ``finally`` halts the card through the supervisor's halt channel.

  Hold and cancel are shared-memory flags, so a pause is visible to the child before the
  program stop reaches the card.
"""
from __future__ import annotations

import bisect
import contextlib
import gc
import multiprocessing as mp
import os
import queue as _queue
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from .. import _gcsafe
from ..protocol import frames as F
from .client import ControllerError, FifoStreamer, MotionController, StreamStats, TransportConfig
from .clock import SYSTEM as _SYSTEM_CLOCK

FAULT_CODE = 9200
DEFAULT_MAX_BUFFER_MS = 1200.0


class StreamFault(ControllerError):
    """Streaming stopped for a safety reason (``kind``): ``unexpected_idle``, ``underrun``,
    ``link_lost``, ``child_died``, ``child_stalled``, ``parent_lost``, ``refused``,
    ``protocol`` (the streaming process is of another mlaser version: :data:`STREAM_PROTO`)."""

    def __init__(self, kind: str, msg: str, exec_index: int = -1):
        super().__init__(f"{kind}: {msg}", FAULT_CODE)
        self.kind = kind
        self.detail = msg
        self.exec_index = exec_index


_gc_lock = threading.Lock()
_gc_depth = [0]
_gc_saved: list = []
_gcsafe.add_full_hold(lambda: _gc_depth[0] > 0)    # quiet_gc open: no full collection
#: generation-2 threshold while a job streams: no automatic full collection (CPython counts gen-1
#: collections against it; 2**30 of them do not happen within any job)
NO_FULL_GC_THRESHOLD = 1 << 30


def full_gc_held_off() -> bool:
    """True while no automatic full collection can pause the process.

    With CPython's generational collector (3.12, 3.13, 3.14.5+) that is the generation-2
    threshold raised by :func:`quiet_gc`.  CPython 3.14.0-3.14.4 shipped the incremental
    collector instead: it has no generation-2 threshold (``gc.get_threshold()[2]`` reads 0 and a
    set is ignored) and no automatic full collection at all: it scans the old generation in
    bounded increments, so the goal holds there by construction.  mlaser replaces that collector's
    automatic collections by young and full ones from a thread (:mod:`mlaser._gcsafe`): its full
    collections wait while :func:`quiet_gc` is open."""
    th2 = gc.get_threshold()[2]
    return th2 >= NO_FULL_GC_THRESHOLD or (th2 == 0 and _incremental_gc()) or _gcsafe.full_held()


def _incremental_gc() -> bool:
    """The incremental collector of CPython 3.14.0-3.14.4 runs this process's automatic
    collections (it ignores the generation-2 threshold); not while :mod:`mlaser._gcsafe`
    replaces them."""
    with _gc_lock:
        return _gcsafe.incremental_collector() and not _gcsafe.active()


@contextlib.contextmanager
def quiet_gc():
    """While a job streams, the cyclic GC never runs a full (generation 2) collection.

    A full collection holds the GIL for the whole scan: 0.4-1.3 s with a large drawing's
    ~2-3 million live objects (review 2 §3.1), and every thread of the process stops meanwhile:
    the in-thread streamer runs the card's FIFO dry, the parent's heartbeat to the streaming
    child stops.  Two measures, both undone afterwards:

    * ``gc.freeze()`` moves the existing objects to the permanent generation, so no collection
      during the job scans them;
    * the generation-2 threshold is raised to :data:`NO_FULL_GC_THRESHOLD`: CPython's automatic
      full collection is triggered by the objects allocated *after* the freeze (an edit of a
      large drawing allocates millions), and after a freeze it is triggered *more* often.  The
      young generations (0 and 1) keep their automatic collections: each one scans only what was
      allocated since the last one (milliseconds), so cyclic garbage stays bounded.

    Used by the in-thread streamer, the streaming child and the parent of a process streamer.
    Nested / concurrent uses share one freeze; the freeze is only undone when it was ours (not
    when e.g. the test harness had frozen the heap already)."""
    with _gc_lock:
        _gc_depth[0] += 1
        if _gc_depth[0] == 1:
            _gc_depth.append(gc.get_freeze_count() == 0)    # ours to undo (nobody else froze)
            gc.freeze()
            th = gc.get_threshold()
            _gc_saved[:] = [th]
            gc.set_threshold(th[0], th[1], max(th[2], NO_FULL_GC_THRESHOLD))
    try:
        yield
    finally:
        with _gc_lock:
            _gc_depth[0] -= 1
            if _gc_depth[0] == 0:
                if _gc_saved:
                    gc.set_threshold(*_gc_saved.pop())
                if _gc_depth.pop():
                    gc.unfreeze()


from .._sched import RESPONSIVE_SWITCH_S, responsive_gil  # noqa: E402,F401 - re-exported


def _packed_wire_cls():
    from ..nc.packed import PackedWire
    return PackedWire


@dataclass
class WireJob:
    """The part of a Job the streamer needs (picklable, small).

    ``wire`` is a list of items, or the compiled job's :class:`~mlaser.nc.packed.PackedWire`
    itself (kept packed: no list per item; its ManuItem grouping is the wire's run flags).  For
    the streaming process :meth:`share` puts a packed wire into shared memory: the pickled
    ``WireJob`` then carries only the handle (``shared``) and the child calls :meth:`attach`."""
    wire: Any
    period_ms: float = 1.0
    duration_s: float = 0.0
    groups: Optional[tuple] = None     # list wire: wire index where each ManuItem expansion starts (Job._groups)
    shared: Any = None                 # SharedWireHandle of the packed wire (hand-off to the child)
    _shm: Any = field(default=None, repr=False, compare=False)

    def __getstate__(self):
        d = dict(self.__dict__)
        d["_shm"] = None
        if d.get("shared") is not None:
            d["wire"] = None               # the child attaches to the shared block instead
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)

    @property
    def packed(self) -> bool:
        """The wire is a PackedWire whose run flags are the ManuItem groups."""
        return self.wire is not None and isinstance(self.wire, _packed_wire_cls())

    @classmethod
    def of(cls, job) -> "WireJob":
        if isinstance(job, WireJob):
            return job
        if hasattr(job, "job") and hasattr(job, "toolpath") and not hasattr(job, "wire_bytes"):
            job = job.job                  # a services.jobs.PlannedJob
        m = getattr(job, "machine", None)
        per = float(getattr(m, "period_ms", getattr(job, "period_ms", 1.0)) or 1.0)
        dur = float(getattr(job, "duration_s", 0.0) or 0.0)
        wire = job.wire
        g = getattr(job, "_groups", None)
        if (isinstance(wire, _packed_wire_cls()) and len(wire) and bool(wire.flag[0])
                and g is not None and g[0] is wire and isinstance(g[2], str)):
            # Job.groups() is the wire's own grouping and covers every item: keep it packed
            return cls(wire, per, dur, None)
        starts: Optional[tuple[int, ...]] = None
        groups = getattr(job, "groups", None)           # nc.compiler.Job (list or packed wire)
        if callable(groups):
            firsts: list[int] = []
            k = 0
            for grp in groups():
                firsts.append(k)
                k += len(grp)
            starts = tuple(firsts) if k == len(job.wire) else None
        return cls([list(w) for w in job.wire], per, dur, starts)

    # ------------------------------------------------------------------ shared memory hand-off
    def share(self) -> "WireJob":
        """A copy for the streaming process whose packed wire is in a new shared-memory block
        (``PackedWire.to_shared``).  The caller owns the block: :meth:`release` it on every exit
        path (the process streamer does, as soon as the child has attached and in its
        ``finally``).  A list wire is returned as it is (pickled as before)."""
        if not self.packed:
            return self
        handle, shm = self.wire.to_shared()
        return WireJob(self.wire, self.period_ms, self.duration_s, None, handle, shm)

    def release(self) -> None:
        """Free the shared block of :meth:`share` (idempotent)."""
        shm, self._shm = self._shm, None
        if shm is not None:
            try:
                shm.close()
            finally:
                with contextlib.suppress(FileNotFoundError):
                    shm.unlink()

    def attach(self) -> "WireJob":
        """In the streaming process: copy the packed wire out of the shared block (the parent
        frees it as soon as this process has introduced itself).  Raises ``OSError`` /
        ``FileNotFoundError`` when the block is gone."""
        if self.shared is not None and self.wire is None:
            self.wire = _packed_wire_cls().from_shared(self.shared, copy=True)
            self.shared = None
        return self

    # ------------------------------------------------------------------ frames
    def frames(self, first_seq: int) -> list[list[int]]:
        """The FIFO frames exactly as ``Job.frames()`` builds them: the frame size is tested once
        per ManuItem expansion (FUN_10043c20, parity JN-08), not after every item."""
        if self.packed:
            pf = PackedFrames(self.wire)
            pf.first = first_seq
            return [pf[i] for i in range(len(pf))]
        if not self.groups:
            return F.build_fifo_frames(self.wire, first_seq)
        starts = list(self.groups) + [len(self.wire)]
        return F.build_fifo_frames([self.wire[a:b] for a, b in zip(starts, starts[1:]) if b > a], first_seq)


class PackedFrames:
    """The FIFO frames of a packed wire, built one at a time when they are sent (no list of all
    frames: the frames of a 5-million-step job held about 1 GB as Python lists).  The frame
    boundaries are :meth:`PackedWire.frame_bounds` (the loop of ``PackedWire.iter_frames``), so
    frame ``i`` is ``wire.frames(first)[i]``, item for item.  ``first`` is the sequence number of
    frame 0 (``build_fifo_frames`` numbering, ``& 0xFFFFFFFF``)."""

    def __init__(self, wire, ws=None) -> None:
        self.wire = wire
        self.ws = wire.item_word_starts() if ws is None else ws
        self.begin, self.end = wire.frame_bounds(ws=self.ws)
        self.first = 1
        self._last: Optional[tuple] = None

    def __len__(self) -> int:
        return len(self.begin)

    def __getitem__(self, i: int) -> list[int]:
        last = self._last
        if last is not None and last[0] == i and last[1] == self.first:
            return last[2]
        if not 0 <= i < len(self.begin):
            raise IndexError("frame index out of range")
        fr = [(self.first + i) & 0xFFFFFFFF, *self.wire.stream_words(int(self.begin[i]), int(self.end[i]), self.ws)]
        self._last = (i, self.first, fr)
        return fr

    def payload_bytes(self):
        """Bytes of each frame without its sequence word (numpy array)."""
        return 4 * (self.end - self.begin)

    def end_items(self, cum):
        """Wire index of the last item in each frame (as the list version's bisect of ``cum``)."""
        import numpy as np
        if not len(cum):
            return np.zeros(len(self.begin), dtype=np.int64)
        return np.minimum(np.searchsorted(cum, 4 * (self.end - (self.begin[0] if len(self.begin) else 0)),
                                          side="left"), len(cum) - 1)


def wire_timing(wire, period_ms: float):
    """Cumulative bytes and cumulative replay time (ms) after each wire item.

    A list wire gives two lists; a :class:`~mlaser.nc.packed.PackedWire` gives two numpy arrays
    (int64, float64) with the same values, computed from its runs (``np.cumsum`` adds in order,
    so every time is the float the list version accumulates)."""
    if isinstance(wire, _packed_wire_cls()):
        return _packed_timing(wire, period_ms)
    cb, ct = [], []
    b = t = 0.0
    for w in wire:
        b += 4 * len(w)
        if w and (w[0] & 0xFFFF) == F.OP_STEP:
            t += period_ms
        cb.append(int(b))
        ct.append(t)
    return cb, ct


def _packed_timing(wire, period_ms: float, ws=None):
    import numpy as np
    if ws is None:
        ws = wire.item_word_starts()
    cum = 4 * ws[1:]
    ln = np.diff(wire.woff)
    first = np.where(ln > 0, wire.woff[:-1], 0)
    if wire.words.size:
        step = (ln > 0) & ((wire.words[first].astype(np.int64) & 0xFFFF) == F.OP_STEP)
    else:
        step = np.zeros(len(ln), dtype=bool)
    inc = np.repeat(np.where(step, float(period_ms), 0.0), wire.cnt)
    return cum, np.cumsum(inc, dtype=np.float64)


def _search_left(a, x) -> int:
    """``bisect.bisect_left`` on a list or a sorted numpy array."""
    if isinstance(a, list):
        return bisect.bisect_left(a, x)
    import numpy as np
    return int(np.searchsorted(a, x, side="left"))


@dataclass
class BufferStats:
    """Streaming telemetry (what tools/bench_stream.py reports)."""
    capacity: int = 0                    # RO[16] after clear (bytes)
    prefill_ms: float = 0.0              # motion buffered when FIFO start was written
    max_buffer_ms: float = 0.0           # the cap in force
    peak_buffer_ms: float = 0.0          # most motion ever buffered
    min_buffer_ms: Optional[float] = None   # lowest buffered motion while frames remained
    min_free: Optional[int] = None
    watchdog_trips: int = 0
    restarts: int = 0                    # always 0: the streamer never restarts on its own
    polls: int = 0
    samples: list = field(default_factory=list)   # (t, buffered_ms) every poll (optional)


class JobStreamer(FifoStreamer):
    """Stream one job with flow control, a motion cap, progress, hold (pause) and cancel."""

    def __init__(self, mc: MotionController, prefill_ms: Optional[float] = None,
                 watchdog_ms: Optional[float] = 40.0, max_buffer_ms: Optional[float] = DEFAULT_MAX_BUFFER_MS,
                 pwm_freq: int = 5000, keep_samples: bool = False, clock=None, **kw):
        super().__init__(mc, **kw)
        # the job's time (controller.clock): the wall clock, or a test's virtual time
        self.clock = clock if clock is not None else getattr(mc, "clock", None) or _SYSTEM_CLOCK
        self.executed = 0.0
        #: axes of the step items (X|Y): the mask of the step-format command (0x66800003 observed)
        self.axis_mask = 0x3
        self.max_buffer_ms = max_buffer_ms
        if prefill_ms is not None and max_buffer_ms is not None:
            prefill_ms = min(prefill_ms, max_buffer_ms)
        self.prefill_ms = prefill_ms if prefill_ms is not None else max_buffer_ms
        self.watchdog_ms = watchdog_ms
        self.pwm_freq = pwm_freq
        self.keep_samples = keep_samples
        self.buf = BufferStats()
        self.exec_index = -1
        self.on_event: Optional[Callable[..., None]] = None
        self.on_status: Optional[Callable[[object], None]] = None
        self.laser_off_errors: list[str] = []
        self._prepared: Optional[tuple] = None      # prepare(job)

    def _emit(self, *ev) -> None:
        if self.on_event is not None:
            try:
                self.on_event(*ev)
            except Exception:  # noqa: BLE001 - telemetry must never break streaming
                pass

    def _safe_laser_off(self) -> bool:
        """FIFO stop and PWM off, each on its own; returns False when one failed (the caller
        turns that into a fault, it is never swallowed)."""
        ok = True
        for fn in (self.mc.fifo_stop,
                   lambda: self.mc.command(F.mailbox_sys(F.SYS_PWM, self.pwm_freq, 0, 0)),
                   lambda: self.mc.command(F.mailbox_sys(F.SYS_PWM_BUS, self.pwm_freq, 0, 0))):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                ok = False
                self.laser_off_errors.append(str(e))
        return ok

    def prepare(self, job, cancel: Optional[threading.Event] = None) -> None:
        """Do the host-side work of :meth:`run_job` for ``job`` now (timing and frame boundaries;
        a compiled job's packed wire stays packed: about 0.1 s per million steps, more on a slow
        PC; a list wire is copied and framed in Python), so that it does not
        happen between the last status poll and the first streaming request.  While a job
        streams the link deadline (``link_deadline_ms``, 150 ms) counts from the last reply of
        the card; preparing inside :meth:`run_job` let that time run out with no request
        outstanding and latched a spurious "link lost" at the start of a long job.
        :meth:`run_job` uses the prepared data when it is given the same ``job``.

        It runs as :meth:`run_job` does, with the GIL switch interval lowered and no cyclic GC:
        the timing and the frames are CPU-bound Python, and with the default 5 ms interval the
        status poller lost the hand-overs meanwhile, so the card's last reply was already
        stale when the streaming deadline took over (macOS CI: "link lost" after 224 ms).

        ``cancel`` (the job's Stop) is tested between the stages: once it is set the preparation
        returns at once and nothing is kept (the caller leaves without streaming)."""
        with quiet_gc(), responsive_gil():
            self._prepare(job, cancel)

    def _prepare(self, job, cancel: Optional[threading.Event] = None) -> None:
        self._prepared = None

        def cancelled() -> bool:
            return cancel is not None and cancel.is_set()
        if cancelled():
            return
        wj = WireJob.of(job)
        if cancelled():
            return
        if wj.packed:
            ws = wj.wire.item_word_starts()
            cum, ctime = _packed_timing(wj.wire, wj.period_ms, ws)
            if cancelled():
                return
            pf = PackedFrames(wj.wire, ws)      # numbered from the card's sequence in run_job
            frames: Any = pf
            frame_end: Any = pf.end_items(cum)
            frame_bytes: Any = pf.payload_bytes()
        else:
            cum, ctime = wire_timing(wj.wire, wj.period_ms)
            if cancelled():
                return
            frames = wj.frames(0)                   # numbered from the card's sequence in run_job
            frame_end = []              # wire index of the last item in each frame
            acc, k = 0, 0
            for fr in frames:
                acc += 4 * (len(fr) - 1)
                k = bisect.bisect_left(cum, acc, lo=k)
                frame_end.append(min(k, len(cum) - 1))
            frame_bytes = [4 * (len(fr) - 1) for fr in frames]
        if cancelled():
            return
        self._prepared = (job, wj, cum, ctime, frames, frame_end, frame_bytes)

    def run_job(self, job, progress: Optional[Callable[[float, int], None]] = None,
                cancel: Optional[threading.Event] = None, hold: Optional[Callable[[], bool]] = None,
                poll_s: float = 0.01, timeout_s: Optional[float] = None) -> StreamStats:
        with quiet_gc(), responsive_gil():
            return self._run_job(job, progress, cancel, hold, poll_s, timeout_s)

    def _run_job(self, job, progress: Optional[Callable[[float, int], None]] = None,
                cancel: Optional[threading.Event] = None, hold: Optional[Callable[[], bool]] = None,
                poll_s: float = 0.01, timeout_s: Optional[float] = None) -> StreamStats:
        """Clear, prefill, start, keep filling and wait until the card has executed the job."""
        mc = self.mc
        prep = self._prepared
        if prep is None or prep[0] is not job:
            self.prepare(job)
            prep = self._prepared
            if prep is None:                # (prepare() without a cancel always keeps it)
                raise StreamFault("refused", "the job was not prepared")
        self._prepared = None
        _job, wj, cum, ctime, frames, frame_end, frame_bytes = prep
        cancel = cancel or threading.Event()
        hold = hold or (lambda: False)
        n_items = len(cum)
        total_bytes = int(cum[-1]) if n_items else 0
        total_ms = float(ctime[-1]) if n_items else 0.0
        # the original's job start on a real MCC100 (owner's capture 2026-11-07):
        # [9999, 16], 103 := 1, the prefill frames from seq 1, [9999, 1, mask|0x66800000, 0], 103 := 2
        if hasattr(mc, "fifo_job_reset"):
            mc.fifo_job_reset()
        mc.fifo_clear()
        st = mc.read_status()
        capacity = st.fifo_free
        cap_ms = self.max_buffer_ms
        self.buf = BufferStats(capacity=capacity, max_buffer_ms=cap_ms or 0.0)
        # == Job.frames(st.fifo_seq + 1) (what parity verified): frames numbered from the card's
        # next sequence number, as build_fifo_frames numbers them (seq & 0xFFFFFFFF)
        first = st.fifo_seq + 1
        if isinstance(frames, PackedFrames):
            frames.first = first                # each frame is built with its number when sent
        else:
            for j, fr in enumerate(frames):
                fr[0] = (first + j) & 0xFFFFFFFF
        self.stats = StreamStats(items=len(wj.wire))
        dur_s = wj.duration_s or total_ms / 1000.0
        limit = timeout_s if timeout_s is not None else max(60.0, 4 * dur_s + 30.0)
        clock = self.clock
        deadline = clock.monotonic() + limit
        i = 0
        sent_bytes = 0
        started = False
        held = False
        grace = 0                   # polls to ignore "idle" after a FIFO start
        idle_polls = 0
        t0 = clock.monotonic()

        def executed(st) -> tuple[int, int]:
            in_fifo = max(0, capacity - st.fifo_free)
            done = max(0, sent_bytes - in_fifo)
            idx = _search_left(cum, done) if n_items else -1
            return done, min(idx, n_items - 1)

        def sent_ms() -> float:
            return float(ctime[frame_end[i - 1]]) if i > 0 else 0.0

        while True:
            if cancel.is_set():
                break
            if clock.monotonic() > deadline:
                raise ControllerError("job timeout", F.ERR_TIMEOUT)
            st = mc.read_status()
            self.buf.polls += 1
            if self.on_status is not None:
                self.on_status(st)
            done, idx = executed(st)
            self.exec_index = idx
            frac = done / total_bytes if total_bytes else 1.0
            self.executed = frac
            if progress is not None:
                progress(min(1.0, frac), max(0, idx))
            if cancel.is_set():
                break
            done_t = float(ctime[idx]) if idx >= 0 and n_items else 0.0
            bms = max(0.0, sent_ms() - done_t)
            h = bool(hold())
            if started and i < len(frames):
                if self.buf.min_buffer_ms is None or bms < self.buf.min_buffer_ms:
                    self.buf.min_buffer_ms = bms
                if self.buf.min_free is None or st.fifo_free < self.buf.min_free:
                    self.buf.min_free = st.fifo_free
            if self.keep_samples:
                self.buf.samples.append((clock.monotonic() - t0, bms))
            self._emit("buffer", bms, st.fifo_free)
            # ---- watchdog: the card is about to run dry with frames left -> laser off + FAULT
            if (started and not h and self.watchdog_ms is not None and i < len(frames)
                    and st.running and bms < self.watchdog_ms):
                ok = self._safe_laser_off()
                self.buf.watchdog_trips += 1
                self._emit("underrun", idx, bms)
                raise StreamFault("underrun", f"only {bms:.0f} ms of motion buffered at wire {idx}"
                                  + ("" if ok else f"; laser off failed: {self.laser_off_errors[-1]}"), idx)
            # ---- fill (never more than cap_ms of motion ahead of the card)
            free = st.fifo_free
            sent = 0
            while i < len(frames) and sent < self.frames_per_fill and not cancel.is_set():
                fr = frames[i]
                if free < 4 * (len(fr) + 3) + self.margin:
                    break
                if cap_ms is not None and i > 0 and float(ctime[frame_end[i]]) - done_t > cap_ms:
                    break
                mc.fifo_write_frame(fr)
                free -= 4 * len(fr)
                sent_bytes += int(frame_bytes[i])
                self.stats.frames += 1
                self.stats.words += len(fr)
                i += 1
                sent += 1
            bms = max(0.0, sent_ms() - done_t)
            self.buf.peak_buffer_ms = max(self.buf.peak_buffer_ms, bms)
            # ---- start (once), and the release of our own hold
            if not started and not h and not cancel.is_set():
                target = self.prefill_ms
                full = sent == 0 or i >= len(frames)
                if full or (target is not None and bms >= target):
                    self.buf.prefill_ms = bms
                    if hasattr(mc, "fifo_step_format"):
                        mc.fifo_step_format(self.axis_mask)     # the data format, then start
                    mc.fifo_start()
                    started = True
                    grace = 2
                    continue
            if started and held and not h and not cancel.is_set():
                mc.fifo_start()             # resume of a FIFO hold (pause_mode="fifo")
                grace = 2
            held = h
            if grace > 0:
                grace -= 1
            elif started and not st.running and not h:
                if i >= len(frames) and st.fifo_free >= capacity:
                    idle_polls += 1
                    if idle_polls >= 2:       # drained and idle: done
                        # the original stops the FIFO at the end of every job (103 := 3): the
                        # card keeps reporting the job as active (status low byte 1) until then
                        # then PWM off as the original does after every job (owner's capture:
                        # 103 := 3, [9999, 3, freq, 0, 0], [9999, 17, freq, 0, 0])
                        if hasattr(mc, "fifo_stop"):
                            if not self._safe_laser_off():
                                raise StreamFault("laser_off_failed",
                                                  "end of job: the FIFO stop / PWM off was not "
                                                  "acknowledged: " + "; ".join(self.laser_off_errors[-3:]), idx)
                        break
                else:
                    raise StreamFault("unexpected_idle",
                                      f"the card stopped with {len(frames) - i} frames left and "
                                      f"{capacity - st.fifo_free} bytes in the FIFO (external stop, "
                                      "alarm or FIFO underrun); not restarted", idx)
            else:
                idle_polls = 0
            if sent == 0:
                clock.sleep(poll_s)
        if not cancel.is_set():
            self.executed = 1.0
            self.exec_index = len(wj.wire) - 1
            if progress is not None:
                progress(1.0, max(0, len(wj.wire) - 1))
        return self.stats


# =============================================================================== process
def _shared_flags(ctx) -> dict:
    """Hold / cancel / heartbeat / halted, shared between the parent and the streaming child,
    **without** a lock (``lock=False``): each flag has one writer, and an aligned load or store
    of a byte / 64-bit integer is atomic.  With the default lock (a process-shared semaphore) a
    parent killed while it held it (it bumps ``hb`` all the time) left it held for good, and the
    child's watchdog blocked on ``flags["hb"].value`` instead of halting the card (macOS CI:
    "orphaned child kept streaming")."""
    return {"hold": ctx.Value("b", 0, lock=False), "cancel": ctx.Value("b", 0, lock=False),
            "hb": ctx.Value("q", 0, lock=False), "halted": ctx.Value("b", 0, lock=False)}


def _child_guard(policy: dict, flags: dict):
    """The parent's safety policy applied to the child's own socket."""
    from ..machine.safety import Cmd, classify_request
    from .supervisor import CommandRefused
    guarded = int(policy.get("guarded", 0))
    armed = bool(policy.get("armed", False))

    def guard(req: F.Request) -> None:
        cls = classify_request(req, guarded)
        if cls == Cmd.READ or cls == Cmd.HALT:
            return
        if (flags["cancel"].value or flags["halted"].value) and cls & (Cmd.MOTION | Cmd.EMISSION):
            raise CommandRefused("the job was cancelled or halted", cls)
        if cls & Cmd.EMISSION and not armed:
            raise CommandRefused("emission refused: the job was not started armed", cls)
        if cls & Cmd.CONFIG:
            raise CommandRefused("configuration write refused while a job is active", cls)
    return guard


#: version of the messages between the parent and the streaming process.  The parent sends it
#: in ``policy["proto"]``; the child answers with its first event, ``("hello", {"proto": ...,
#: "pid": ..., "ok": ...})``, and refuses a different version **before it opens a socket** (no
#: card traffic at all).  The parent faults a job whose child does not introduce itself with
#: this version (``StreamFault("protocol")``: halt, FAULT, a clear message).  A mismatch
#: means two mlaser versions met (an update installed while mlaser ran, a mixed install).
#: Raise it on any change of the event tuples or of the policy's meaning.
#: 2: the job's packed wire comes through shared memory (``WireJob.share`` / ``attach``); the
#: ``hello`` reports a failed attach (``ok`` False, ``kind`` "handoff").
STREAM_PROTO = 2


def _startup_delay() -> None:
    """Tests only: ``MLASER_TEST_STREAM_START_DELAY_S`` seconds of extra start-up in the streaming
    child (a loaded PC spawning a new interpreter), before it opens its sockets."""
    try:
        d = float(os.environ.get("MLASER_TEST_STREAM_START_DELAY_S", "") or 0.0)
    except ValueError:
        d = 0.0
    if d > 0:
        time.sleep(d)


def _child_main(cfg: dict, position_ppm: float, wj: WireJob, kw: dict, cmd_q, evt_q,
                flags: Optional[dict] = None, policy: Optional[dict] = None) -> None:
    """Entry point of the streaming process (spawned): own socket, own GIL, self-supervised."""
    from .supervisor import HaltParams, run_halt
    hello: dict[str, object] = {"proto": STREAM_PROTO, "pid": os.getpid(), "ok": True}
    their = (policy or {}).get("proto")
    if their != STREAM_PROTO:
        hello.update(ok=False, error=f"the parent speaks streaming protocol {their!r}, "
                                     f"this streaming process {STREAM_PROTO}")
    else:
        try:
            wj = wj.attach()                    # the packed wire out of the parent's shared block
        except Exception as e:  # noqa: BLE001 - no job data: refuse before any socket is opened
            hello.update(ok=False, kind="handoff",
                         error=f"the streaming process did not receive the job ({type(e).__name__}: {e})")
    if not hello["ok"]:
        try:
            evt_q.put(("hello", hello))
        except Exception:  # noqa: BLE001 - parent gone
            pass
        return                                  # nothing was opened: the card never heard of us
    try:  # best effort: a little more priority for the real-time-ish loop (often not permitted)
        os.nice(-5)
    except Exception:  # noqa: BLE001
        pass
    try:  # macOS: no App Nap for the streaming process (timer coalescing, lowered priority)
        from mlaser._power import app_activity
        app_activity("mlaser streams a laser job")
    except Exception:  # noqa: BLE001
        pass
    policy = dict(policy or {})
    flags = flags or _shared_flags(mp)
    ppid0 = int(policy.get("ppid", os.getppid()))
    parent_timeout = float(policy.get("parent_timeout_s", 0.3))
    link_deadline = float(policy.get("link_deadline_ms", 150.0)) / 1000.0
    status_every = float(policy.get("status_every_s", 0.04))
    zf_regs = policy.get("follower_regs")          # (addr, n) to relay, or None
    cancel = threading.Event()
    fault: dict = {}
    tcfg = TransportConfig(**cfg)
    mc = MotionController(config=tcfg, pulses_per_mm=position_ppm)
    mc.guard = _child_guard(policy, flags)
    hcfg = TransportConfig(**{**cfg, "timeout_ms": int(policy.get("halt_timeout_ms", 40)),
                              "fifo_time_ms": 2 * int(policy.get("halt_timeout_ms", 40)), "max_recv": 1})
    hmc = MotionController(config=hcfg, pulses_per_mm=position_ppm)
    halt = HaltParams(**policy.get("halt", {}))
    stats_kw = {k: v for k, v in kw.items()}
    js = JobStreamer(mc, **stats_kw)
    # the host-side preparation (timing, frames) before the hello: the parent's stall and link
    # watch (stall_s, child_deadline_ms: 0.5 s) count from the hello, and the card has nothing
    # of this job before the first request, so the preparation is start-up (startup_s), not
    # silence (CI, Windows: a 28 000-item job prepared after the hello on a loaded runner, no
    # message for 703 ms, "link lost").  A preparation error is raised again by run_job below
    # and reported as before.
    try:
        js.prepare(wj)
    except Exception:  # noqa: BLE001 - run_job prepares again and raises it there
        pass
    last = [0.0, 0.0]
    started = [False]
    # the hello goes just before the job's first request (or before any earlier message): until
    # then the parent keeps polling the card itself and the start-up bound (startup_s) applies;
    # from the hello its stall and link watch count, and the next message is the card's status
    # (CI, Windows: the sockets and the rest of the set-up after an early hello took 563 ms)
    hello_lock = threading.Lock()
    said = [False]

    def put(ev) -> None:
        try:
            with hello_lock:
                if not said[0]:
                    said[0] = True
                    evt_q.put(("hello", hello))
            evt_q.put(ev)
        except Exception:  # noqa: BLE001 - parent gone
            pass

    def say_hello() -> None:
        with hello_lock:
            if not said[0]:
                said[0] = True
                try:
                    evt_q.put(("hello", hello))
                except Exception:  # noqa: BLE001 - parent gone (the watchdog halts)
                    pass

    # --- ci-fixes: the self-halt runs to completion (begin) ---------------------------------
    # The watchdog (a daemon thread) runs the halt, and ``cancel.set()`` makes the main thread
    # return from the job at once: before, its ``finally`` closed the halt socket and the process
    # exited, killing the daemon thread after the first halt write (CI, 3.13: only "stop motion"
    # reached the card, the beam stayed on).  ``halt_lock`` is held for the whole halt and the
    # main thread takes it before it closes the sockets and returns.  With the parent gone nobody
    # else will halt the card, so that halt is repeated until every step is acknowledged.
    halt_lock = threading.Lock()
    closed = [False]
    orphan_halt_s = float(policy.get("orphan_halt_s", 3.0))

    def self_halt(kind: str, why: str) -> None:
        with halt_lock:
            if fault or closed[0]:
                return
            fault.update(kind=kind, why=why)
            flags["halted"].value = 1
            cancel.set()
            put(("fault", kind, why))           # tell the parent first: the halt may take a while
            res = run_halt(hmc, halt, f"streaming child: {why}", channel="child")
            t_end = time.monotonic() + orphan_halt_s
            while kind == "parent_lost" and not res.ok and time.monotonic() < t_end:
                time.sleep(0.02)
                res = run_halt(hmc, halt, f"streaming child: {why} (retry)", channel="child")
            put(("halted", kind, why, res.ok, [(n, ok) for n, ok, _e in res.steps]))

    def wait_halt_and_close() -> None:
        got = halt_lock.acquire(timeout=orphan_halt_s + 5.0)   # bounded: never hang the child
        try:
            closed[0] = True
            mc.close()
            hmc.close()
        finally:
            if got:
                halt_lock.release()
    # --- ci-fixes (end) ------------------------------------------------------------------------

    # A re-send (no reply in time) passes the guard again: never after the cancel or a halt.
    # And never once the card has been silent for the parent's streaming deadline
    # (child_deadline_ms): the parent latches "link lost" at that age and halts, but its watch
    # runs in the GUI's process and can come late (CI, Windows: at 656 ms, after the 600 ms
    # FIFO timeout, so the frame went out again before the halt).  The child halts the card
    # itself then, as its own link watch does, and the frame is not sent again.
    resend_limit = float(policy.get("child_deadline_ms", 500.0)) / 1000.0

    def resend_guard(req, g=mc.guard):
        def again() -> None:
            g(req)                              # reads and the halt always pass
            from ..machine.safety import Cmd, classify_request
            from .supervisor import CommandRefused
            cls = classify_request(req, int(policy.get("guarded", 0)))
            age = time.monotonic() - mc.last_ok
            if cls & (Cmd.MOTION | Cmd.EMISSION) and mc.last_ok and age >= resend_limit:
                why = f"no reply from the card for {age * 1000:.0f} ms"
                self_halt("link_lost", why)
                raise CommandRefused(f"not sent again: {why}", cls)
        return again
    mc.resend_guard = resend_guard

    def watchdog() -> None:
        hb, hb_t = flags["hb"].value, time.monotonic()
        while not cancel.is_set():
            time.sleep(0.02)
            now = time.monotonic()
            try:
                pp = os.getppid()
            except Exception:  # noqa: BLE001
                pp = ppid0
            if pp != ppid0:
                self_halt("parent_lost", f"parent process {ppid0} is gone")
                return
            v = flags["hb"].value
            if v != hb:
                hb, hb_t = v, now
            elif now - hb_t > parent_timeout:
                self_halt("parent_lost", f"no heartbeat from the parent for {(now - hb_t) * 1000:.0f} ms")
                return
            if started[0] and mc.last_ok and now - mc.last_ok > link_deadline:
                self_halt("link_lost", f"no reply from the card for {(now - mc.last_ok) * 1000:.0f} ms")
                return

    def on_status(st) -> None:
        started[0] = True
        if flags["cancel"].value:
            cancel.set()
        now = time.monotonic()
        if now - last[0] >= status_every:
            last[0] = now
            zf = None
            if zf_regs:
                try:
                    zf = mc.read_regs(int(zf_regs[0]), int(zf_regs[1]))
                except ControllerError:
                    zf = None
            put(("status", list(st.ro), [list(a.raw) for a in st.axes], zf))

    def progress(frac, idx):
        # legacy command queue (tests / tools may still use it)
        while True:
            try:
                c = cmd_q.get_nowait()
            except _queue.Empty:
                break
            if c == "cancel":
                flags["cancel"].value = 1
            elif c == "pause":
                flags["hold"].value = 1
            elif c == "resume":
                flags["hold"].value = 0
        if flags["cancel"].value:
            cancel.set()
        now = time.monotonic()
        if now - last[1] >= 0.02 or frac >= 1.0:
            last[1] = now
            put(("progress", frac, idx))

    js.on_event = lambda *ev: put(ev) if ev[0] != "buffer" else None
    js.on_status = on_status
    wd = threading.Thread(target=watchdog, name="mlaser-stream-watchdog", daemon=True)
    try:
        _startup_delay()                    # tests: a slow start-up (MLASER_TEST_STREAM_START_DELAY_S)
        mc.open()
        hmc.open()
        mc.last_ok = time.monotonic()
        wd.start()
        say_hello()
        stats = js.run_job(wj, progress, cancel, lambda: bool(flags["hold"].value))
        b = asdict(js.buf)
        b.pop("samples", None)
        if fault:
            put(("error", f"{fault['kind']}: {fault['why']}", FAULT_CODE, js.exec_index, fault["kind"]))
        else:
            put(("done", asdict(stats), b, js.exec_index, bool(flags["cancel"].value)))
    except StreamFault as e:
        if not fault:
            self_halt(e.kind, e.detail)
        put(("error", str(e), FAULT_CODE, js.exec_index, e.kind))
    except Exception as e:  # noqa: BLE001
        from .supervisor import CommandRefused
        if isinstance(e, CommandRefused) and flags["cancel"].value and not fault:
            # the parent's cancel reached the guard between the streamer's cancel check and its
            # next FIFO write: that is the cancel, not an error (as the in-thread job worker treats
            # a refusal after its own cancel); the parent halts the card as on any cancel
            b = asdict(js.buf)
            b.pop("samples", None)
            put(("done", asdict(js.stats), b, js.exec_index, True))
        elif fault:
            put(("error", f"{fault['kind']}: {fault['why']}", FAULT_CODE, js.exec_index, fault["kind"]))
        else:
            self_halt("error", f"{type(e).__name__}: {e}")
            put(("error", str(e), getattr(e, "code", 0), js.exec_index, "error"))
    finally:
        cancel.set()
        wait_halt_and_close()               # ci-fixes: never cut a running self-halt short


class ProcessStreamer:
    """Run :class:`JobStreamer` in a separate OS process (multiprocessing ``spawn``).

    ``run_job`` blocks the calling thread like ``JobStreamer.run_job`` but only relays events;
    the timing-critical loop runs in the child with its own socket to the card.  ``policy``
    (from the safety supervisor): ``armed``, ``guarded`` (output mask), ``halt``
    (:class:`~controller.supervisor.HaltParams` fields), ``link_deadline_ms``,
    ``parent_timeout_s``, ``follower_regs``.  ``on_status(MCStatus, follower_words)`` receives the
    relayed card status; ``on_alive()`` is called for every message from the child.
    """

    def __init__(self, cfg: TransportConfig, position_ppm: float = 1000.0, policy: Optional[dict] = None,
                 stall_s: float = 0.5, startup_s: float = 20.0, **streamer_kw):
        self.cfg = cfg
        self.position_ppm = position_ppm
        self.policy = dict(policy or {})
        self.stall_s = stall_s
        self.startup_s = startup_s
        self.kw = streamer_kw
        self.ctx = mp.get_context("spawn")
        self.proc: Optional[mp.process.BaseProcess] = None
        self.cmd_q: Optional[mp.queues.Queue] = None
        self.evt_q: Optional[mp.queues.Queue] = None
        self.flags: Optional[dict] = None
        self.executed = 0.0
        self.exec_index = -1
        self.buf: dict = {}
        self.stats = StreamStats()
        self.halted: Optional[tuple] = None
        self.on_event: Optional[Callable[..., None]] = None
        self.on_status: Optional[Callable[..., None]] = None
        self.on_alive: Optional[Callable[[], None]] = None
        self.on_fault: Optional[Callable[[str, str], None]] = None

    def _cfg_dict(self) -> dict:
        return {k: getattr(self.cfg, k) for k in TransportConfig.__dataclass_fields__}

    def set_hold(self, on: bool) -> None:
        """Shared-memory hold flag: visible to the child at once (before any stop command)."""
        if self.flags is not None:
            self.flags["hold"].value = 1 if on else 0

    def set_cancel(self) -> None:
        if self.flags is not None:
            self.flags["cancel"].value = 1

    def kill(self) -> None:
        """Terminate the child (the caller halts the card)."""
        if self.proc is not None and self.proc.is_alive():
            self.proc.terminate()

    def run_job(self, job, progress: Optional[Callable[[float, int], None]] = None,
                cancel: Optional[threading.Event] = None, hold: Optional[Callable[[], bool]] = None,
                poll_s: float = 0.02, timeout_s: Optional[float] = None) -> StreamStats:
        with quiet_gc(), responsive_gil():
            return self._run_job(job, progress, cancel, hold, poll_s, timeout_s)

    #: period of the parent heartbeat thread (the child halts after ``parent_timeout_s`` 0.3 s)
    HEARTBEAT_S = 0.02

    def _heartbeat(self, flags: dict, relay: list, stop: threading.Event, cancel: threading.Event,
                   hold: Callable[[], bool]) -> None:
        """Bump the heartbeat counter the child watches from a thread of its own (review 2 P1-2):
        the relay loop unpickles and dispatches every status message, so under GUI load (GIL
        convoy) it reached the child's 0.3 s far more often than this small loop does.

        The counter still means "the relay loop is alive": it is only bumped while the relay
        loop has iterated within ``parent_timeout_s`` (the child's own bound), so a parent whose
        relay loop is stuck stops the heartbeat as before.  Cancel and hold are mirrored into
        the shared flags here too, so a Stop reaches the child without waiting for the relay."""
        limit = float(self.policy.get("parent_timeout_s", 0.3))
        while not stop.wait(self.HEARTBEAT_S):
            if cancel.is_set():
                flags["cancel"].value = 1
            try:
                flags["hold"].value = 1 if hold() else 0
            except Exception:  # noqa: BLE001 - the relay loop reads it too
                pass
            if time.monotonic() - relay[0] <= limit:
                flags["hb"].value += 1

    def _check_hello(self, ev) -> None:
        """The child's first event must be its ``hello`` with :data:`STREAM_PROTO`; anything else
        cancels the child and faults the job (the caller halts the card)."""
        hello = ev[1] if ev and ev[0] == "hello" and len(ev) > 1 and isinstance(ev[1], dict) else None
        if hello is not None and hello.get("proto") == STREAM_PROTO and hello.get("ok"):
            return
        if self.flags is not None:
            self.flags["cancel"].value = 1
        if hello is not None and hello.get("kind") == "handoff":
            raise StreamFault("handoff", str(hello.get("error")), self.exec_index)
        if hello is None:
            why = "the streaming process did not introduce itself (a different mlaser version)"
        else:
            why = str(hello.get("error") or f"the streaming process speaks protocol "
                      f"{hello.get('proto')!r}, this mlaser {STREAM_PROTO}")
        raise StreamFault("protocol", f"{why}; restart mlaser", self.exec_index)

    def _run_job(self, job, progress: Optional[Callable[[float, int], None]] = None,
                cancel: Optional[threading.Event] = None, hold: Optional[Callable[[], bool]] = None,
                poll_s: float = 0.02, timeout_s: Optional[float] = None) -> StreamStats:
        wj = WireJob.of(job)
        dur = wj.duration_s or (float(wire_timing(wj.wire, wj.period_ms)[1][-1]) / 1000.0 if len(wj.wire) else 0.0)
        # the packed wire goes to the child through shared memory (a handle is pickled, not the
        # items); the block is freed once the child has attached (its hello) and in the finally
        # below on every other exit; if this process dies, multiprocessing's resource tracker
        # (POSIX) or the OS (Windows: the last handle) frees it
        try:
            shared = wj.share()
        except OSError:                   # no shared memory: the packed arrays are pickled
            shared = wj
        try:
            return self._run_child(shared, dur, progress, cancel, hold, poll_s, timeout_s)
        finally:
            shared.release()

    def _run_child(self, wj: WireJob, dur: float, progress, cancel, hold, poll_s, timeout_s) -> StreamStats:
        from .client import AxisStatus, MCStatus
        cancel = cancel or threading.Event()
        hold = hold or (lambda: False)
        ctx = self.ctx
        self.cmd_q, self.evt_q = ctx.Queue(), ctx.Queue()
        self.flags = _shared_flags(ctx)
        policy = {**self.policy, "ppid": os.getpid(), "proto": STREAM_PROTO}
        self.proc = ctx.Process(target=_child_main, name="mlaser-stream", daemon=True,
                                args=(self._cfg_dict(), self.position_ppm, wj, self.kw,
                                      self.cmd_q, self.evt_q, self.flags, policy))
        self.proc.start()
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else max(60.0, 4 * dur + 60.0))
        t_start = time.monotonic()
        last_msg = None
        greeted = False                     # the child's "hello" (STREAM_PROTO) came first
        relay = [time.monotonic()]          # the relay loop's last iteration (heartbeat thread)
        hb_stop = threading.Event()
        hb = threading.Thread(target=self._heartbeat, args=(self.flags, relay, hb_stop, cancel, hold),
                              name="mlaser-stream-heartbeat", daemon=True)
        hb.start()
        try:
            while True:
                relay[0] = time.monotonic()
                self.flags["hb"].value += 1
                if cancel.is_set():
                    self.flags["cancel"].value = 1
                self.flags["hold"].value = 1 if hold() else 0
                try:
                    ev = self.evt_q.get(timeout=poll_s)
                except _queue.Empty:
                    now = time.monotonic()
                    if not self.proc.is_alive():
                        # drain what it sent before it died
                        try:
                            ev = self.evt_q.get(timeout=0.05)
                        except _queue.Empty:
                            raise StreamFault("child_died", f"streaming process died (exit code "
                                              f"{self.proc.exitcode})", self.exec_index) from None
                    elif last_msg is None and now - t_start > self.startup_s:
                        self.flags["cancel"].value = 1
                        raise StreamFault("child_stalled", "streaming process did not start", self.exec_index)
                    elif last_msg is not None and now - last_msg > self.stall_s:
                        self.flags["cancel"].value = 1
                        raise StreamFault("child_stalled", f"no message from the streaming process for "
                                          f"{(now - last_msg) * 1000:.0f} ms", self.exec_index)
                    elif now > deadline:
                        self.flags["cancel"].value = 1
                        raise ControllerError("job timeout", F.ERR_TIMEOUT)
                    else:
                        continue
                last_msg = time.monotonic()
                if self.on_alive is not None:
                    self.on_alive()
                kind = ev[0]
                if not greeted:
                    self._check_hello(ev)       # raises StreamFault("protocol" / "handoff")
                    greeted = True
                    wj.release()                # the child has its copy of the wire
                    continue
                if kind == "progress":
                    self.executed, self.exec_index = ev[1], ev[2]
                    if progress is not None:
                        progress(ev[1], ev[2])
                elif kind == "status":
                    if self.on_status is not None:
                        st = MCStatus(ev[1], [AxisStatus(r) for r in ev[2]], self.position_ppm)
                        self.on_status(st, ev[3])
                elif kind == "done":
                    self.stats = StreamStats(**ev[1])
                    self.buf, self.exec_index = ev[2], ev[3]
                    return self.stats
                elif kind == "fault":
                    if self.on_fault is not None:
                        self.on_fault(ev[1], ev[2])
                elif kind == "halted":
                    self.halted = ev[1:]
                elif kind == "error":
                    self.exec_index = ev[3]
                    fk = ev[4] if len(ev) > 4 else "error"
                    if ev[2] == FAULT_CODE or fk != "error":
                        raise StreamFault(fk, ev[1].split(": ", 1)[-1], ev[3])
                    raise ControllerError(ev[1], ev[2] or 0)
                elif self.on_event is not None:
                    self.on_event(*ev)
        finally:
            hb_stop.set()
            if self.proc is not None:
                self.flags["cancel"].value = 1
                self.proc.join(timeout=5.0)
                if self.proc.is_alive():  # pragma: no cover - should not happen
                    self.proc.terminate()


__all__ = ["JobStreamer", "ProcessStreamer", "WireJob", "BufferStats", "StreamFault",
           "wire_timing", "DEFAULT_MAX_BUFFER_MS", "FAULT_CODE", "STREAM_PROTO"]
