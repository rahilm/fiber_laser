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
import multiprocessing as mp
import os
import queue as _queue
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional, Sequence

from ..protocol import frames as F
from .client import ControllerError, FifoStreamer, MotionController, StreamStats, TransportConfig

FAULT_CODE = 9200
DEFAULT_MAX_BUFFER_MS = 1200.0


class StreamFault(ControllerError):
    """Streaming stopped for a safety reason (``kind``): ``unexpected_idle``, ``underrun``,
    ``link_lost``, ``child_died``, ``child_stalled``, ``parent_lost``, ``refused``."""

    def __init__(self, kind: str, msg: str, exec_index: int = -1):
        super().__init__(f"{kind}: {msg}", FAULT_CODE)
        self.kind = kind
        self.detail = msg
        self.exec_index = exec_index


@dataclass
class WireJob:
    """The part of a Job the streamer needs (picklable, small)."""
    wire: list
    period_ms: float = 1.0
    duration_s: float = 0.0
    groups: Optional[tuple] = None     # wire index where each ManuItem expansion starts (Job._groups)

    @classmethod
    def of(cls, job) -> "WireJob":
        if isinstance(job, WireJob):
            return job
        if hasattr(job, "job") and hasattr(job, "toolpath") and not hasattr(job, "wire_bytes"):
            job = job.job                  # a services.jobs.PlannedJob
        m = getattr(job, "machine", None)
        per = float(getattr(m, "period_ms", getattr(job, "period_ms", 1.0)) or 1.0)
        starts = None
        groups = getattr(job, "groups", None)           # nc.compiler.Job (list or packed wire)
        if callable(groups):
            starts, k = [], 0
            for grp in groups():
                starts.append(k)
                k += len(grp)
            starts = tuple(starts) if k == len(job.wire) else None
        return cls([list(w) for w in job.wire], per, float(getattr(job, "duration_s", 0.0) or 0.0), starts)

    def frames(self, first_seq: int) -> list[list[int]]:
        """The FIFO frames exactly as ``Job.frames()`` builds them: the frame size is tested once
        per ManuItem expansion (FUN_10043c20, parity JN-08), not after every item."""
        if not self.groups:
            return F.build_fifo_frames(self.wire, first_seq)
        starts = list(self.groups) + [len(self.wire)]
        return F.build_fifo_frames([self.wire[a:b] for a, b in zip(starts, starts[1:]) if b > a], first_seq)


def wire_timing(wire: Sequence[Sequence[int]], period_ms: float) -> tuple[list[int], list[float]]:
    """Cumulative bytes and cumulative replay time (ms) after each wire item."""
    cb, ct = [], []
    b = t = 0.0
    for w in wire:
        b += 4 * len(w)
        if w and (w[0] & 0xFFFF) == F.OP_STEP:
            t += period_ms
        cb.append(int(b))
        ct.append(t)
    return cb, ct


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
                 pwm_freq: int = 5000, keep_samples: bool = False, **kw):
        super().__init__(mc, **kw)
        self.executed = 0.0
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

    def prepare(self, job) -> None:
        """Do the host-side work of :meth:`run_job` for ``job`` now (wire copy, timing, FIFO
        frames; about 0.1 s for a 30 000-item job, more on a slow PC), so that it does not
        happen between the last status poll and the first streaming request.  While a job
        streams the link deadline (``link_deadline_ms``, 150 ms) counts from the last reply of
        the card; preparing inside :meth:`run_job` let that time run out with no request
        outstanding and latched a spurious "link lost" at the start of a long job.
        :meth:`run_job` uses the prepared data when it is given the same ``job``."""
        wj = WireJob.of(job)
        cum, ctime = wire_timing(wj.wire, wj.period_ms)
        frames = wj.frames(0)                   # numbered from the card's sequence in run_job
        frame_end = []              # wire index of the last item in each frame
        acc, k = 0, 0
        for fr in frames:
            acc += 4 * (len(fr) - 1)
            k = bisect.bisect_left(cum, acc, lo=k)
            frame_end.append(min(k, len(cum) - 1))
        self._prepared = (job, wj, cum, ctime, frames, frame_end)

    def run_job(self, job, progress: Optional[Callable[[float, int], None]] = None,
                cancel: Optional[threading.Event] = None, hold: Optional[Callable[[], bool]] = None,
                poll_s: float = 0.01, timeout_s: Optional[float] = None) -> StreamStats:
        """Clear, prefill, start, keep filling and wait until the card has executed the job."""
        mc = self.mc
        prep = self._prepared
        if prep is None or prep[0] is not job:
            self.prepare(job)
            prep = self._prepared
        self._prepared = None
        _job, wj, cum, ctime, frames, frame_end = prep
        cancel = cancel or threading.Event()
        hold = hold or (lambda: False)
        total_bytes = cum[-1] if cum else 0
        total_ms = ctime[-1] if ctime else 0.0
        mc.fifo_clear()
        st = mc.read_status()
        capacity = st.fifo_free
        cap_ms = self.max_buffer_ms
        self.buf = BufferStats(capacity=capacity, max_buffer_ms=cap_ms or 0.0)
        # == Job.frames(st.fifo_seq + 1) (what parity verified): frames numbered from the card's
        # next sequence number, as build_fifo_frames numbers them (seq & 0xFFFFFFFF)
        first = st.fifo_seq + 1
        for j, fr in enumerate(frames):
            fr[0] = (first + j) & 0xFFFFFFFF
        frame_bytes = [4 * (len(fr) - 1) for fr in frames]
        self.stats = StreamStats(items=len(wj.wire))
        dur_s = wj.duration_s or total_ms / 1000.0
        limit = timeout_s if timeout_s is not None else max(60.0, 4 * dur_s + 30.0)
        deadline = time.monotonic() + limit
        i = 0
        sent_bytes = 0
        started = False
        held = False
        grace = 0                   # polls to ignore "idle" after a FIFO start
        idle_polls = 0
        t0 = time.monotonic()

        def executed(st) -> tuple[int, int]:
            in_fifo = max(0, capacity - st.fifo_free)
            done = max(0, sent_bytes - in_fifo)
            idx = bisect.bisect_left(cum, done) if cum else -1
            return done, min(idx, len(cum) - 1)

        def sent_ms() -> float:
            return ctime[frame_end[i - 1]] if i > 0 else 0.0

        while True:
            if cancel.is_set():
                break
            if time.monotonic() > deadline:
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
            done_t = ctime[idx] if idx >= 0 and ctime else 0.0
            bms = max(0.0, sent_ms() - done_t)
            h = bool(hold())
            if started and i < len(frames):
                if self.buf.min_buffer_ms is None or bms < self.buf.min_buffer_ms:
                    self.buf.min_buffer_ms = bms
                if self.buf.min_free is None or st.fifo_free < self.buf.min_free:
                    self.buf.min_free = st.fifo_free
            if self.keep_samples:
                self.buf.samples.append((time.monotonic() - t0, bms))
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
                if cap_ms is not None and i > 0 and ctime[frame_end[i]] - done_t > cap_ms:
                    break
                mc.fifo_write_frame(fr)
                free -= 4 * len(fr)
                sent_bytes += frame_bytes[i]
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
                        break
                else:
                    raise StreamFault("unexpected_idle",
                                      f"the card stopped with {len(frames) - i} frames left and "
                                      f"{capacity - st.fifo_free} bytes in the FIFO (external stop, "
                                      "alarm or FIFO underrun); not restarted", idx)
            else:
                idle_polls = 0
            if sent == 0:
                time.sleep(poll_s)
        if not cancel.is_set():
            self.executed = 1.0
            self.exec_index = len(wj.wire) - 1
            if progress is not None:
                progress(1.0, max(0, len(wj.wire) - 1))
        return self.stats


# =============================================================================== process
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


def _child_main(cfg: dict, position_ppm: float, wj: WireJob, kw: dict, cmd_q, evt_q,
                flags: Optional[dict] = None, policy: Optional[dict] = None) -> None:
    """Entry point of the streaming process (spawned): own socket, own GIL, self-supervised."""
    from .supervisor import HaltParams, run_halt
    try:  # best effort: a little more priority for the real-time-ish loop (often not permitted)
        os.nice(-5)
    except Exception:  # noqa: BLE001
        pass
    policy = dict(policy or {})
    flags = flags or {"hold": mp.Value("b", 0), "cancel": mp.Value("b", 0),
                      "hb": mp.Value("q", 0), "halted": mp.Value("b", 0)}
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
    last = [0.0, 0.0]
    started = [False]

    def put(ev) -> None:
        try:
            evt_q.put(ev)
        except Exception:  # noqa: BLE001 - parent gone
            pass

    def self_halt(kind: str, why: str) -> None:
        if fault:
            return
        fault.update(kind=kind, why=why)
        flags["halted"].value = 1
        cancel.set()
        put(("fault", kind, why))           # tell the parent first: the halt may take a while
        res = run_halt(hmc, halt, f"streaming child: {why}", channel="child")
        put(("halted", kind, why, res.ok, [(n, ok) for n, ok, _e in res.steps]))

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
        mc.open()
        hmc.open()
        mc.last_ok = time.monotonic()
        wd.start()
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
        if fault:
            put(("error", f"{fault['kind']}: {fault['why']}", FAULT_CODE, js.exec_index, fault["kind"]))
        else:
            self_halt("error", f"{type(e).__name__}: {e}")
            put(("error", str(e), getattr(e, "code", 0), js.exec_index, "error"))
    finally:
        cancel.set()
        mc.close()
        hmc.close()


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
        self.proc = None
        self.cmd_q = None
        self.evt_q = None
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
        from .client import AxisStatus, MCStatus
        wj = WireJob.of(job)
        cancel = cancel or threading.Event()
        hold = hold or (lambda: False)
        ctx = self.ctx
        self.cmd_q, self.evt_q = ctx.Queue(), ctx.Queue()
        self.flags = {"hold": ctx.Value("b", 0), "cancel": ctx.Value("b", 0), "hb": ctx.Value("q", 0),
                      "halted": ctx.Value("b", 0)}
        policy = {**self.policy, "ppid": os.getpid()}
        self.proc = ctx.Process(target=_child_main, name="mlaser-stream", daemon=True,
                                args=(self._cfg_dict(), self.position_ppm, wj, self.kw,
                                      self.cmd_q, self.evt_q, self.flags, policy))
        self.proc.start()
        dur = wj.duration_s or wire_timing(wj.wire, wj.period_ms)[1][-1] / 1000.0 if wj.wire else 0.0
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else max(60.0, 4 * dur + 60.0))
        t_start = time.monotonic()
        last_msg = None
        try:
            while True:
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
            if self.proc is not None:
                self.flags["cancel"].value = 1
                self.proc.join(timeout=5.0)
                if self.proc.is_alive():  # pragma: no cover - should not happen
                    self.proc.terminate()


__all__ = ["JobStreamer", "ProcessStreamer", "WireJob", "BufferStats", "StreamFault",
           "wire_timing", "DEFAULT_MAX_BUFFER_MS", "FAULT_CODE"]
