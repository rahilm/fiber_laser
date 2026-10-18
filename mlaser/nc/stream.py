"""Streaming a compiled :class:`~mlaser.nc.compiler.Job` to the card (docs/protocol.md §5, §13).

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

Rebuild:

* :class:`JobStreamer` (in-thread) fills the FIFO as deep as the card allows before the start
  (``prefill_ms``, default: until full or done), keeps it topped up with the same 2000-byte
  margin, reports progress, and tracks how much motion is buffered (``min_buffer_ms``).
* **Watchdog**: if the buffered motion drops below ``watchdog_ms`` while frames remain, it stops
  the FIFO (103 := 3) and switches the PWM off ([9999, 3, f, 0, 0] / [9999, 17, ...]) before the
  card runs dry, refills, and restarts (``auto_resume``) once ``resume_ms`` of motion is buffered
  again (or waits for the caller to resume).  The step items carry the laser duty, so the beam
  comes back with the next step.  INFERRED: whether the card holds the beam during a FIFO stop
  is UNKNOWN, hence the explicit PWM off.
* :class:`ProcessStreamer` runs the same JobStreamer in a separate OS process (``spawn``) with
  its own socket, talking to the caller through queues, so a busy GUI process (GIL) cannot
  starve the stream.  Same ``run_job`` signature; the in-thread path stays for tests.
"""
from __future__ import annotations

import bisect
import multiprocessing as mp
import queue as _queue
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional, Sequence

from ..controller import frames as F
from ..controller.client import (ControllerError, FifoStreamer, MotionController, StreamStats,
                                 TransportConfig)

# card-side bytes per FIFO item: the card stores the item words (header + params); the frame's
# sequence word is not stored (simulator and RO[16] accounting, INFERRED for the real card)


@dataclass
class WireJob:
    """The part of a Job the streamer needs (picklable, small)."""
    wire: list
    period_ms: float = 1.0
    duration_s: float = 0.0

    @classmethod
    def of(cls, job) -> "WireJob":
        if isinstance(job, WireJob):
            return job
        m = getattr(job, "machine", None)
        per = float(getattr(m, "period_ms", getattr(job, "period_ms", 1.0)) or 1.0)
        return cls([list(w) for w in job.wire], per, float(getattr(job, "duration_s", 0.0) or 0.0))


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
    min_buffer_ms: Optional[float] = None   # lowest buffered motion while frames remained
    min_free: Optional[int] = None
    watchdog_trips: int = 0
    restarts: int = 0                    # 103 := 2 after an unexpected idle
    polls: int = 0
    samples: list = field(default_factory=list)   # (t, buffered_ms) every poll (optional)


class JobStreamer(FifoStreamer):
    """Stream one job with flow control, progress, hold (pause), cancel and a watchdog."""

    def __init__(self, mc: MotionController, prefill_ms: Optional[float] = None,
                 watchdog_ms: Optional[float] = 40.0, resume_ms: float = 500.0,
                 auto_resume: bool = True, pwm_freq: int = 5000, keep_samples: bool = False,
                 **kw):
        super().__init__(mc, **kw)
        self.executed = 0.0
        self.prefill_ms = prefill_ms
        self.watchdog_ms = watchdog_ms
        self.resume_ms = resume_ms
        self.auto_resume = auto_resume
        self.pwm_freq = pwm_freq
        self.keep_samples = keep_samples
        self.buf = BufferStats()
        self.exec_index = -1
        self.on_event: Optional[Callable[..., None]] = None

    def _emit(self, *ev) -> None:
        if self.on_event is not None:
            try:
                self.on_event(*ev)
            except Exception:  # noqa: BLE001 - telemetry must never break streaming
                pass

    def _safe_laser_off(self) -> None:
        mc = self.mc
        mc.fifo_stop()
        try:
            mc.command(F.mailbox_sys(F.SYS_PWM, self.pwm_freq, 0, 0))
            mc.command(F.mailbox_sys(F.SYS_PWM_BUS, self.pwm_freq, 0, 0))
        except ControllerError:
            pass

    def run_job(self, job, progress: Optional[Callable[[float, int], None]] = None,
                cancel: Optional[threading.Event] = None, hold: Optional[Callable[[], bool]] = None,
                poll_s: float = 0.01, timeout_s: Optional[float] = None) -> StreamStats:
        """Clear, prefill, start, keep filling and wait until the card has executed the job."""
        mc = self.mc
        wj = WireJob.of(job)
        cancel = cancel or threading.Event()
        hold = hold or (lambda: False)
        cum, ctime = wire_timing(wj.wire, wj.period_ms)
        total_bytes = cum[-1] if cum else 0
        total_ms = ctime[-1] if ctime else 0.0
        mc.fifo_clear()
        st = mc.read_status()
        capacity = st.fifo_free
        self.buf = BufferStats(capacity=capacity)
        frames = F.build_fifo_frames(wj.wire, st.fifo_seq + 1)
        frame_bytes = [4 * (len(fr) - 1) for fr in frames]
        frame_end = []              # wire index of the last item in each frame
        acc, k = 0, 0
        for nb in frame_bytes:
            acc += nb
            k = bisect.bisect_left(cum, acc, lo=k)
            frame_end.append(min(k, len(cum) - 1))
        self.stats = StreamStats(items=len(wj.wire))
        dur_s = wj.duration_s or total_ms / 1000.0
        limit = timeout_s if timeout_s is not None else max(60.0, 4 * dur_s + 30.0)
        deadline = time.monotonic() + limit
        i = 0
        sent_bytes = 0
        started = False
        wd_hold = False             # watchdog pause in effect
        idle_polls = 0
        t0 = time.monotonic()

        def executed(st) -> tuple[int, int]:
            in_fifo = max(0, capacity - st.fifo_free)
            done = max(0, sent_bytes - in_fifo)
            idx = bisect.bisect_left(cum, done) if cum else -1
            return done, min(idx, len(cum) - 1)

        def buffered_ms(exec_idx: int) -> float:
            if i == 0:
                return 0.0
            sent_t = ctime[frame_end[i - 1]]
            done_t = ctime[exec_idx] if exec_idx >= 0 else 0.0
            return max(0.0, sent_t - done_t)

        while True:
            if cancel.is_set():
                break
            if time.monotonic() > deadline:
                raise ControllerError("job timeout", F.ERR_TIMEOUT)
            st = mc.read_status()
            self.buf.polls += 1
            done, idx = executed(st)
            self.exec_index = idx
            frac = done / total_bytes if total_bytes else 1.0
            self.executed = frac
            if progress is not None:
                progress(min(1.0, frac), max(0, idx))
            bms = buffered_ms(idx)
            if started and i < len(frames):
                if self.buf.min_buffer_ms is None or bms < self.buf.min_buffer_ms:
                    self.buf.min_buffer_ms = bms
                if self.buf.min_free is None or st.fifo_free < self.buf.min_free:
                    self.buf.min_free = st.fifo_free
            if self.keep_samples:
                self.buf.samples.append((time.monotonic() - t0, bms))
            self._emit("buffer", bms, st.fifo_free)
            # ---- watchdog: hold the laser off and stop before the card runs dry
            if (started and not wd_hold and self.watchdog_ms is not None and i < len(frames)
                    and st.running and bms < self.watchdog_ms and not hold()):
                self._safe_laser_off()
                wd_hold = True
                self.buf.watchdog_trips += 1
                self._emit("underrun", idx, bms)
            # ---- fill
            free = st.fifo_free
            sent = 0
            while i < len(frames) and sent < self.frames_per_fill and not cancel.is_set():
                fr = frames[i]
                if free < 4 * (len(fr) + 3) + self.margin:
                    break
                mc.fifo_write_frame(fr)
                free -= 4 * len(fr)
                sent_bytes += frame_bytes[i]
                self.stats.frames += 1
                self.stats.words += len(fr)
                i += 1
                sent += 1
            bms = buffered_ms(idx)
            # ---- start / restart
            if not started and not hold():
                target = self.prefill_ms
                full = sent == 0 or i >= len(frames)
                if full or (target is not None and bms >= target):
                    self.buf.prefill_ms = bms
                    mc.fifo_start()
                    started = True
                    continue
            if wd_hold and not hold():
                if i >= len(frames) or bms >= self.resume_ms or sent == 0:
                    if self.auto_resume:
                        mc.fifo_start()
                        wd_hold = False
                        self._emit("resumed", idx, bms)
                        continue
            if started and not st.running and not hold() and not wd_hold:
                if i >= len(frames) and st.fifo_free >= capacity:
                    idle_polls += 1
                    if idle_polls >= 2:       # drained and idle: done
                        break
                elif i < len(frames) or st.fifo_free < capacity:
                    mc.fifo_start()           # underrun or stopped with data left (INFERRED)
                    self.buf.restarts += 1
                    idle_polls = 0
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
def _child_main(cfg: dict, position_ppm: float, wj: WireJob, kw: dict, cmd_q, evt_q) -> None:
    """Entry point of the streaming process (spawned): own socket, own GIL."""
    import os
    try:  # best effort: a little more priority for the real-time-ish loop
        os.nice(-5)
    except Exception:  # noqa: BLE001
        pass
    cancel = threading.Event()
    state = {"hold": False}
    mc = MotionController(config=TransportConfig(**cfg), pulses_per_mm=position_ppm)
    js = JobStreamer(mc, **kw)
    last = [0.0]

    def progress(frac, idx):
        # drain commands (cheap, non-blocking) on every poll
        while True:
            try:
                c = cmd_q.get_nowait()
            except _queue.Empty:
                break
            if c == "cancel":
                cancel.set()
            elif c == "pause":
                state["hold"] = True
                try:
                    mc.fifo_stop()
                except ControllerError:
                    pass
            elif c == "resume":
                state["hold"] = False
                try:
                    mc.fifo_start()
                except ControllerError:
                    pass
        now = time.monotonic()
        if now - last[0] >= 0.02 or frac >= 1.0:
            last[0] = now
            evt_q.put(("progress", frac, idx))

    js.on_event = lambda *ev: evt_q.put(ev) if ev[0] != "buffer" else None
    try:
        mc.open()
        stats = js.run_job(wj, progress, cancel, lambda: state["hold"])
        b = asdict(js.buf)
        b.pop("samples", None)
        evt_q.put(("done", asdict(stats), b, js.exec_index, cancel.is_set()))
    except Exception as e:  # noqa: BLE001
        evt_q.put(("error", str(e), getattr(e, "code", 0), js.exec_index))
    finally:
        mc.close()


class ProcessStreamer:
    """Run :class:`JobStreamer` in a separate OS process (multiprocessing ``spawn``).

    ``run_job`` blocks the calling thread like ``JobStreamer.run_job`` but only relays events;
    the timing-critical loop runs in the child with its own socket to the card.
    """

    def __init__(self, cfg: TransportConfig, position_ppm: float = 1000.0, **streamer_kw):
        self.cfg = cfg
        self.position_ppm = position_ppm
        self.kw = streamer_kw
        self.ctx = mp.get_context("spawn")
        self.proc = None
        self.cmd_q = None
        self.evt_q = None
        self.executed = 0.0
        self.exec_index = -1
        self.buf: dict = {}
        self.stats = StreamStats()
        self.on_event: Optional[Callable[..., None]] = None

    def _cfg_dict(self) -> dict:
        return {k: getattr(self.cfg, k) for k in TransportConfig.__dataclass_fields__}

    def run_job(self, job, progress: Optional[Callable[[float, int], None]] = None,
                cancel: Optional[threading.Event] = None, hold: Optional[Callable[[], bool]] = None,
                poll_s: float = 0.02, timeout_s: Optional[float] = None) -> StreamStats:
        wj = WireJob.of(job)
        cancel = cancel or threading.Event()
        hold = hold or (lambda: False)
        self.cmd_q, self.evt_q = self.ctx.Queue(), self.ctx.Queue()
        self.proc = self.ctx.Process(target=_child_main, name="mlaser-stream", daemon=True,
                                     args=(self._cfg_dict(), self.position_ppm, wj, self.kw,
                                           self.cmd_q, self.evt_q))
        self.proc.start()
        held = False
        cancelled = False
        dur = wj.duration_s or wire_timing(wj.wire, wj.period_ms)[1][-1] / 1000.0 if wj.wire else 0.0
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else max(60.0, 4 * dur + 60.0))
        try:
            while True:
                if cancel.is_set() and not cancelled:
                    self.cmd_q.put("cancel")
                    cancelled = True
                h = bool(hold())
                if h != held:
                    self.cmd_q.put("pause" if h else "resume")
                    held = h
                try:
                    ev = self.evt_q.get(timeout=poll_s)
                except _queue.Empty:
                    if not self.proc.is_alive():
                        raise ControllerError("streaming process died", F.ERR_TIMEOUT)
                    if time.monotonic() > deadline:
                        self.cmd_q.put("cancel")
                        raise ControllerError("job timeout", F.ERR_TIMEOUT)
                    continue
                kind = ev[0]
                if kind == "progress":
                    self.executed, self.exec_index = ev[1], ev[2]
                    if progress is not None:
                        progress(ev[1], ev[2])
                elif kind == "done":
                    self.stats = StreamStats(**ev[1])
                    self.buf, self.exec_index = ev[2], ev[3]
                    return self.stats
                elif kind == "error":
                    self.exec_index = ev[3]
                    raise ControllerError(ev[1], ev[2] or 0)
                elif self.on_event is not None:
                    self.on_event(*ev)
        finally:
            if self.proc is not None:
                self.proc.join(timeout=5.0)
                if self.proc.is_alive():  # pragma: no cover - should not happen
                    self.proc.terminate()
