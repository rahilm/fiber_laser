"""Original Mlaser job stream from a packet capture, compared with the rebuild's ``compile_job``.

The definitive check of the NC pipeline needs the original's card traffic for a real job, which
the Wine harness cannot produce (the original refuses processing without its hardware
authorisation, docs/parity.md §4.6/§6).  On the user's own licensed machine the original runs
normally; a Wireshark/tcpdump capture of the PC <-> card traffic (``udp port 502``) then holds
the complete FIFO stream.  This module:

1. decodes the card traffic (:func:`parse_traffic`): every request and reply with
   :mod:`mlaser.protocol.frames`, paired by transaction id; identical re-sends with the same
   transaction id are counted as retransmissions (CMCHalAPI retry loop FUN_1001d6d0, protocol.md
   §1), not as new requests;
2. follows the card state (:func:`analyse`): RO block reads (1000 x n: RO[15] last FIFO
   sequence, RO[16] free bytes, RO[19] running), axis positions from the plain axis-RO read
   (2000 x 50, word 10a+2) or the fast read (60001 x n through the 60000 map, FUN_1003ee50 /
   FUN_10052c30, default map when the capture starts after the connect), the system and axis
   parameter blocks (scaling, FUN_10039f20) and the FIFO capacity read (1032);
3. rebuilds each job's FIFO stream (:class:`CapturedJob`): a job starts at a FIFO clear
   (103 := 1) followed by ``write(102, [seq, items...])`` frames (fillFifo FUN_10052e90, frame
   builder FUN_10043c20, protocol.md §5); frames are de-duplicated by sequence number (the 50 ms
   "second fillFifo" retry re-sends a frame with a new transaction id), sequence gaps and
   conflicting re-sends are reported, flow control (RO[16] >= frame bytes + 2000) is checked;
4. compares it with the rebuild (:func:`compare_job`) for the same drawing, parameter files,
   start position (the last position the card reported before the first frame) and job
   placement (estimated from the captured path when the original applied a coordinate-system
   offset): step count, per-step deltas, cumulative position and its deviation, the path
   deviation, duty/frequency/power per step, the process-item sequence with values, the frame
   packing, the job time and any pause/stop sequence.

Everything is pure Python plus numpy.  Nothing is sent to a card.
"""
from __future__ import annotations

import difflib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np

from ..protocol import frames as F
from . import pcap as P

CARD_HOST = "10.1.1.168"        # File/ipAdd.ini CardIP (protocol.md §1)
CARD_PORT = 502
RETRANSMIT_WINDOW_S = 5.0       # same tid + same bytes within this window = retransmission
FLOW_MARGIN = 2000              # fillFifo: RO[16] >= frame bytes + 2000 (FUN_10052e90)

FIFO_CTRL_NAMES = {F.FIFO_CLEAR: "fifo_clear", F.FIFO_START: "fifo_start", F.FIFO_STOP: "fifo_stop"}


# =============================================================================== traffic
@dataclass
class Exchange:
    """One request (first send) with its retransmissions and reply."""
    index: int                         # order of the first send
    ts: float                          # first send
    tid: int
    raw: bytes
    request: Optional[F.Request]
    decode_error: Optional[str] = None
    send_ts: list = field(default_factory=list)
    reply: Optional[F.Response] = None
    reply_ts: Optional[float] = None
    reply_raw: bytes = b""
    extra_replies: int = 0
    packet: int = 0                    # capture packet number of the first send

    @property
    def retransmissions(self) -> int:
        return max(0, len(self.send_ts) - 1)

    @property
    def result(self) -> Optional[int]:
        """Result code as FUN_1001d6d0 (1 ok, 500+exception, 603 mismatch); None = no reply."""
        if self.reply is None or self.request is None:
            return None
        return F.check_reply(self.request, self.reply)

    @property
    def ok(self) -> bool:
        return self.result == F.ERR_OK

    @property
    def data_ts(self) -> float:
        """When the card's answer was produced (reply time, else request time)."""
        return self.reply_ts if self.reply_ts is not None else self.ts


@dataclass
class Traffic:
    card: tuple[str, int]
    exchanges: list[Exchange] = field(default_factory=list)
    clients: set = field(default_factory=set)
    orphan_replies: int = 0
    undecodable_requests: int = 0
    undecodable_replies: int = 0
    transport: str = "udp"
    read_stats: Optional[P.ReadStats] = None

    @property
    def retransmissions(self) -> int:
        return sum(e.retransmissions for e in self.exchanges)

    @property
    def unanswered(self) -> int:
        return sum(1 for e in self.exchanges if e.reply is None)


def _pick_card(messages: Sequence[P.Message], port: int) -> tuple[str, int]:
    counts: dict = {}
    for m in messages:
        if m.dst[1] == port:
            counts[m.dst] = counts.get(m.dst, 0) + 1
    if not counts:
        raise ValueError(f"no traffic to port {port} in the capture")
    return max(counts, key=counts.get)


def parse_traffic(messages: Sequence[P.Message], card_host: Optional[str] = None,
                  card_port: int = CARD_PORT) -> Traffic:
    """Pair requests and replies of the card at ``card_host:card_port``.

    ``card_host`` None picks the address that receives the most messages on ``card_port``.
    """
    card = (card_host, card_port) if card_host else _pick_card(messages, card_port)
    tr = Traffic(card)
    open_by_tid: dict[int, list[Exchange]] = {}
    for m in messages:
        if m.dst == card:
            tr.transport = m.transport
            tr.clients.add(m.src)
            data = m.data
            tid = int.from_bytes(data[:2], "big") if len(data) >= 2 else -1
            lst = open_by_tid.setdefault(tid, [])
            prev = lst[-1] if lst else None
            if prev is not None and prev.raw == data and m.ts - prev.send_ts[-1] <= RETRANSMIT_WINDOW_S:
                prev.send_ts.append(m.ts)          # identical re-send (FUN_1001d6d0 loop)
                continue
            try:
                req, err = F.decode_request(data), None
            except (F.FrameError, Exception) as e:  # noqa: BLE001 - keep going on garbage
                req, err = None, str(e)
                tr.undecodable_requests += 1
            ex = Exchange(len(tr.exchanges), m.ts, tid, data, req, err, [m.ts], packet=m.index)
            tr.exchanges.append(ex)
            lst.append(ex)
            if len(lst) > 4:
                del lst[0]
        elif m.src == card:
            data = m.data
            tid = int.from_bytes(data[:2], "big") if len(data) >= 2 else -1
            lst = open_by_tid.get(tid)
            if not lst:
                tr.orphan_replies += 1
                continue
            ex = lst[-1]
            if ex.reply is not None or ex.reply_raw:
                ex.extra_replies += 1
                continue
            try:
                ex.reply = F.decode_response(data)
            except (F.FrameError, Exception):  # noqa: BLE001
                tr.undecodable_replies += 1
            ex.reply_ts, ex.reply_raw = m.ts, data
    return tr


def load_traffic(path, card_host: Optional[str] = None, card_port: int = CARD_PORT) -> Traffic:
    """Read a pcap/pcapng file and pair its card requests and replies."""
    st = P.ReadStats()
    msgs = P.read_messages(path, card_port, card_host, st)
    tr = parse_traffic(msgs, card_host, card_port)
    tr.read_stats = st
    return tr


# =============================================================================== card state
@dataclass
class Event:
    """A write that matters for a job: FIFO control (103), mailbox (101), FIFO frame (102),
    control register (100)."""
    ts: float
    kind: str                     # fifo_clear / fifo_start / fifo_stop / mailbox / frame / control / write
    reg: int
    words: list
    exchange: Exchange


@dataclass
class RoSample:
    ts: float
    ro: dict                      # index -> value (RO[i] = register 1000 + i)


@dataclass
class PosSample:
    ts: float
    x: int                        # position counts (axis RO k2)
    y: int


@dataclass
class FrameRec:
    seq: int
    items: list                   # frame words after the sequence word
    ts: float
    tid: int
    ok: bool
    sends: int = 1                # datagrams with this tid (1 + retransmissions)
    resends: int = 0              # later frames with the same seq and the same content
    exchange: Optional[Exchange] = None

    @property
    def nbytes(self) -> int:
        """Frame payload bytes as the card stores them (items only)."""
        return 4 * len(self.items)

    @property
    def request_bytes(self) -> int:
        """``4 * (words + 3)``: the frame size fillFifo compares with RO[16] (rebuild convention,
        JobStreamer)."""
        return 4 * (len(self.items) + 1 + 3)


@dataclass
class CapturedJob:
    """The FIFO stream of one job as the card received it."""
    index: int
    clear_ts: float
    end_window_ts: float
    frames: list = field(default_factory=list)            # FrameRec, unique seqs in send order
    events: list = field(default_factory=list)            # non-frame events in the job window
    conflicts: list = field(default_factory=list)         # (seq, first, other) with different items
    seq_gaps: list = field(default_factory=list)          # (after_seq, next_seq)
    first_seq_expected: Optional[int] = None              # RO[15] + 1 before the first frame
    start_counts: Optional[tuple[int, int]] = None
    start_ts: Optional[float] = None                      # first 103 := 2 after the first frame
    end_ts: Optional[float] = None                        # first RO[19] == 0 after the last frame
    end_counts: Optional[tuple[int, int]] = None
    capacity: Optional[int] = None                        # RO[16] after the clear
    min_flow_margin: Optional[int] = None
    flow_violations: int = 0
    max_frames_per_fill: int = 0
    frames_before_start: int = 0
    unanswered_frames: int = 0
    failed_frames: int = 0

    @property
    def wire(self) -> list[list[int]]:
        """The FIFO items (header + params) in card order, like ``Job.wire``."""
        out: list[list[int]] = []
        for fr in self.frames:
            words = fr.items
            i = 0
            while i < len(words):
                hdr = words[i] & 0xFFFFFFFF
                k = (hdr >> 16) // 4
                out.append([hdr, *[w & 0xFFFFFFFF for w in words[i + 1:i + 1 + k]]])
                i += 1 + k
        return out

    @property
    def first_seq(self) -> Optional[int]:
        return self.frames[0].seq if self.frames else None

    @property
    def steps(self) -> int:
        return sum(1 for w in self.wire if (w[0] & 0xFFFF) == F.OP_STEP)

    @property
    def wall_s(self) -> Optional[float]:
        if self.start_ts is None or self.end_ts is None:
            return None
        return self.end_ts - self.start_ts

    @property
    def retransmissions(self) -> int:
        return sum(f.sends - 1 for f in self.frames)

    @property
    def resends(self) -> int:
        return sum(f.resends for f in self.frames)

    def control_sequence(self) -> list[tuple[float, str, list]]:
        """(ts, kind, words) of the FIFO-control and mailbox writes in the job window."""
        return [(e.ts, e.kind, e.words) for e in self.events]

    def stop_sequence(self) -> list[tuple[int, list]]:
        """The program-stop / pause writes, if the job was stopped: from the first decelerated
        stop ``[1, 0x1f, ...]`` or FIFO stop (103 := 3) after the start, as (reg, words)."""
        out: list[tuple[int, list]] = []
        started = False
        for e in self.events:
            if self.start_ts is not None and e.ts < self.start_ts:
                continue
            if not started:
                if (e.kind == "mailbox" and e.words and e.words[0] == F.OP_STOP_AXES) or e.kind == "fifo_stop":
                    started = True
                else:
                    continue
            out.append((e.reg, list(e.words)))
        return out

    def summary(self) -> str:
        w = self.wire
        nproc = sum(1 for x in w if (x[0] & 0xFFFF) != F.OP_STEP)
        t = f", wall {self.wall_s:.2f} s" if self.wall_s is not None else ""
        st = self.stop_sequence()
        return (f"job {self.index}: {len(self.frames)} frames (seq {self.first_seq}..), "
                f"{self.steps} steps, {nproc} process items, {self.retransmissions} retransmissions, "
                f"{self.resends} re-sent frames{t}" + (", stopped/paused" if st else ""))


@dataclass
class CaptureAnalysis:
    traffic: Traffic
    events: list = field(default_factory=list)
    ro_samples: list = field(default_factory=list)
    positions: list = field(default_factory=list)
    sys_words: dict = field(default_factory=dict)          # system RW word -> value (latest)
    axis_rw: dict = field(default_factory=dict)            # (axis, word) -> value (latest)
    fast_map: list = field(default_factory=list)
    fast_map_from_capture: bool = False
    fifo_capacity_read: Optional[int] = None
    jobs: list = field(default_factory=list)
    x_axis: int = 0
    y_axis: int = 1

    # --------------------------------------------------------------- scaling from the card
    def scaling(self):
        """:class:`mlaser.controller.client.CardScaling` from the parameter blocks the original
        polled (FUN_10039f20), or None when the capture has no system block read."""
        from ..controller.client import CardScaling
        if not self.sys_words:
            return None
        sysw = [self.sys_words.get(i, 0) for i in range(F.N_SYS_RW)]
        axw = [self.axis_rw.get((a, k), 0) for a in range(F.N_AXES) for k in range(F.AXIS_RW_STRIDE)]
        return CardScaling.from_words(sysw, axw, self.x_axis, self.y_axis)

    @property
    def position_ppm(self) -> Optional[float]:
        v = F.s32(self.sys_words.get(17, 0)) if self.sys_words else 0
        return float(v) if v > 0 else None

    def position_at(self, ts: float, before: bool = True) -> Optional[PosSample]:
        """Last position sample at or before ``ts`` (``before``) or first one after it."""
        if before:
            best = None
            for p in self.positions:
                if p.ts <= ts:
                    best = p
                else:
                    break
            return best
        for p in self.positions:
            if p.ts >= ts:
                return p
        return None

    def ro_before(self, ts: float) -> Optional[RoSample]:
        best = None
        for s in self.ro_samples:
            if s.ts <= ts:
                best = s
            else:
                break
        return best


def _merge_axis_ro(axro: dict, j: int, v: int) -> None:
    axro[(j // F.AXIS_RO_STRIDE, j % F.AXIS_RO_STRIDE)] = v


def analyse(traffic: Traffic, x_axis: int = 0, y_axis: int = 1) -> CaptureAnalysis:
    """Follow the card state through the capture and cut the FIFO traffic into jobs."""
    from ..controller.startup import FAST_MAP, FAST_MAP_REG, unpack_fast_map
    an = CaptureAnalysis(traffic, fast_map=list(FAST_MAP), x_axis=x_axis, y_axis=y_axis)
    for ex in traffic.exchanges:
        rq = ex.request
        if rq is None:
            continue
        if rq.func == F.FUNC_WRITE:
            if rq.addr == FAST_MAP_REG:     # VM+0x1204 is set before the write (FUN_1003ee50)
                an.fast_map = unpack_fast_map(rq.values) or list(FAST_MAP)
                an.fast_map_from_capture = True
            kind = {F.REG_FIFO_CTRL: FIFO_CTRL_NAMES.get(rq.values[0] if rq.values else -1, "fifo_ctrl"),
                    F.REG_MAILBOX: "mailbox", F.REG_FIFO_DATA: "frame",
                    F.REG_CONTROL: "control"}.get(rq.addr)
            if kind is not None:
                an.events.append(Event(ex.ts, kind, rq.addr, list(rq.values), ex))
            continue
        if rq.func != F.FUNC_READ or ex.reply is None or ex.reply.is_exception:
            continue
        vals = ex.reply.values
        a0, ts = rq.addr, ex.data_ts
        axro: dict = {}
        if F.REG_STATUS_RO <= a0 < F.REG_STATUS_RO + 64:
            ro = {a0 - F.REG_STATUS_RO + i: v for i, v in enumerate(vals)}
            an.ro_samples.append(RoSample(ts, ro))
            if a0 == 1032 and len(vals) == 1:          # FUN_10054e70: FIFO capacity
                an.fifo_capacity_read = vals[0]
        elif F.REG_AXIS_RO <= a0 < F.REG_AXIS_RO + F.N_AXES * F.AXIS_RO_STRIDE:
            for i, v in enumerate(vals):
                _merge_axis_ro(axro, a0 - F.REG_AXIS_RO + i, v)
        elif a0 == F.REG_SYS_RW:
            for i, v in enumerate(vals):
                an.sys_words[i] = v
        elif F.REG_AXIS_RW <= a0 < F.REG_AXIS_RW + 200:
            base = (a0 - F.REG_AXIS_RW) // 2
            for i, v in enumerate(vals):
                w = base + i
                an.axis_rw[(w // F.AXIS_RW_STRIDE, w % F.AXIS_RW_STRIDE)] = v
        elif F.REG_FAST <= a0 < F.REG_FAST + 144:
            for i, v in enumerate(vals):
                k = a0 - F.REG_FAST + i
                if k >= len(an.fast_map):
                    break
                reg = an.fast_map[k]
                if F.REG_AXIS_RO <= reg < F.REG_AXIS_RO + 2 * F.N_AXES * F.AXIS_RO_STRIDE:
                    _merge_axis_ro(axro, (reg - F.REG_AXIS_RO) // 2, v)
                elif F.REG_AXIS_RW <= reg < F.REG_AXIS_RW + 200:
                    off = reg - F.REG_AXIS_RW
                    an.axis_rw[(off // 40, (off % 40) // 2)] = v
                elif F.REG_SYS_RW <= reg < F.REG_AXIS_RW:
                    an.sys_words[(reg - F.REG_SYS_RW) // 2] = v
        if (x_axis, F.AX_POSITION) in axro and (y_axis, F.AX_POSITION) in axro:
            an.positions.append(PosSample(ts, F.s32(axro[(x_axis, F.AX_POSITION)]),
                                          F.s32(axro[(y_axis, F.AX_POSITION)])))
    an.ro_samples.sort(key=lambda s: s.ts)
    an.positions.sort(key=lambda s: s.ts)
    an.jobs = _cut_jobs(an)
    return an


def _cut_jobs(an: CaptureAnalysis) -> list[CapturedJob]:
    # segments start at every FIFO clear; a segment with frames is a job, a segment without
    # frames (e.g. the clear at the end of a stop) belongs to the previous job's window
    segs: list[list[Event]] = [[]]
    for e in an.events:
        if e.kind == "fifo_clear":
            segs.append([e])
        else:
            segs[-1].append(e)
    jobs_ev: list[list[Event]] = []
    for s in segs:
        has_frames = any(e.kind == "frame" for e in s)
        if has_frames or not jobs_ev:
            if has_frames:
                jobs_ev.append(list(s))
        else:
            jobs_ev[-1].extend(s)
    end_ts = an.traffic.exchanges[-1].ts if an.traffic.exchanges else 0.0
    jobs: list[CapturedJob] = []
    for k, evs in enumerate(jobs_ev):
        nxt = jobs_ev[k + 1][0].ts if k + 1 < len(jobs_ev) else end_ts + 1.0
        job = CapturedJob(len(jobs), evs[0].ts, nxt)
        _fill_job(an, job, evs)
        jobs.append(job)
    return jobs


def _fill_job(an: CaptureAnalysis, job: CapturedJob, evs: list[Event]) -> None:
    by_seq: dict[int, FrameRec] = {}
    for e in evs:
        if e.kind != "frame":
            job.events.append(e)
            continue
        if not e.words:
            continue
        seq, items = e.words[0], list(e.words[1:])
        ex = e.exchange
        if ex.reply is None:
            job.unanswered_frames += 1
        elif not ex.ok:
            job.failed_frames += 1
        rec = FrameRec(seq, items, e.ts, ex.tid, ex.ok, len(ex.send_ts), exchange=ex)
        old = by_seq.get(seq)
        if old is None:
            by_seq[seq] = rec
            job.frames.append(rec)
        elif old.items == items:
            old.resends += 1                      # application-level retry of the same frame
            if not old.ok and rec.ok:
                old.ok = True
        else:
            job.conflicts.append((seq, old, rec))
            if not old.ok and rec.ok:              # keep the version the card acknowledged
                idx = job.frames.index(old)
                job.frames[idx] = rec
                by_seq[seq] = rec
    if not job.frames:
        return
    t_first = job.frames[0].ts
    for a, b in zip(job.frames, job.frames[1:]):
        if b.seq != (a.seq + 1) & 0xFFFFFFFF:
            job.seq_gaps.append((a.seq, b.seq))
    ro = an.ro_before(t_first)
    if ro is not None and ro.ts >= job.clear_ts and 15 in ro.ro:
        job.first_seq_expected = (ro.ro[15] + 1) & 0xFFFFFFFF
    for s in an.ro_samples:
        if s.ts >= job.clear_ts and 16 in s.ro:
            if s.ts <= t_first:
                job.capacity = s.ro[16]
            break
    pos = an.position_at(t_first)
    if pos is not None:
        job.start_counts = (pos.x, pos.y)
    t_last = job.frames[-1].ts
    for e in job.events:
        if e.kind == "fifo_start" and e.ts >= t_first:
            job.start_ts = e.ts
            break
    if job.start_ts is not None:
        job.frames_before_start = sum(1 for f in job.frames if f.ts < job.start_ts)
        for s in an.ro_samples:
            if s.ts > max(t_last, job.start_ts) and s.ts < job.end_window_ts and s.ro.get(19) == 0:
                job.end_ts = s.ts
                break
        if job.end_ts is not None:
            p = an.position_at(job.end_ts, before=False)
            if p is not None and p.ts < job.end_window_ts:
                job.end_counts = (p.x, p.y)
    # flow control: frames between two RO reads, free bytes minus what was sent since
    samples = [s for s in an.ro_samples if 16 in s.ro and job.clear_ts <= s.ts < job.end_window_ts]
    si, cur, sent, nfill = -1, None, 0, 0
    for fr in job.frames:
        while si + 1 < len(samples) and samples[si + 1].ts <= fr.ts:
            si += 1
            cur, sent, nfill = samples[si], 0, 0
        if cur is None:
            continue
        margin = cur.ro[16] - sent - fr.request_bytes
        job.min_flow_margin = margin if job.min_flow_margin is None else min(job.min_flow_margin, margin)
        if margin < FLOW_MARGIN:
            job.flow_violations += 1
        sent += fr.nbytes
        nfill += 1
        job.max_frames_per_fill = max(job.max_frames_per_fill, nfill)


# =============================================================================== wire helpers
@dataclass
class StepStream:
    """Decoded steps and process items of a wire item list."""
    dx: np.ndarray
    dy: np.ndarray
    freq: np.ndarray
    duty: np.ndarray
    power: np.ndarray
    step_wire_index: np.ndarray
    process: list                     # (wire index, steps before, op, params tuple)

    @property
    def n(self) -> int:
        return int(self.dx.size)

    def cumulative(self, start: tuple[int, int]) -> np.ndarray:
        """(n + 1) x 2 positions in step pulses, starting at ``start``."""
        out = np.zeros((self.n + 1, 2), dtype=np.int64)
        out[0] = start
        out[1:, 0] = start[0] + np.cumsum(self.dx, dtype=np.int64)
        out[1:, 1] = start[1] + np.cumsum(self.dy, dtype=np.int64)
        return out


def step_stream(wire: Sequence[Sequence[int]]) -> StepStream:
    cols: list[list[int]] = [[], [], [], [], [], []]
    proc = []
    for i, w in enumerate(wire):
        op = w[0] & 0xFFFF
        if op == F.OP_STEP and len(w) >= 3:
            dx, dy, fq, du, pw = F.decode_step(w[1:3])
            for c, v in zip(cols, (dx, dy, fq, du, pw, i)):
                c.append(v)
        else:
            proc.append((i, len(cols[0]), op, tuple(x & 0xFFFFFFFF for x in w[1:])))
    arr = [np.asarray(c, dtype=np.int64) for c in cols]
    return StepStream(*arr, proc)


def describe(op: int, params: Sequence[int]) -> str:
    """One-line description of a FIFO item (frames.describe_item)."""
    d = F.describe_item(op, list(params))
    kind = d.pop("kind", "?")
    d.pop("op", None)
    if kind == "step":
        return f"step dx={d['dx']} dy={d['dy']} freq={d['freq']} duty={d['duty']} power={d['power']}"
    vals = ", ".join(f"{k}={v}" for k, v in d.items())
    return f"op {op} {kind}" + (f" ({vals})" if vals else "") + f" raw={list(params)}"


def wait_ms_total(wire: Sequence[Sequence[int]]) -> int:
    """Sum of the timed waits (op 2001 mode 0) in a wire."""
    t = 0
    for w in wire:
        if (w[0] & 0xFFFF) == F.OP_WAIT and len(w) >= 3:
            d = F.decode_wait(w[1:3])
            if d["mode"] == F.WAIT_TIME:
                t += d["ms"]
    return t


def is_dry_run(wire: Sequence[Sequence[int]]) -> bool:
    """True when no step has the laser byte set and no PWM item switches the beam on."""
    for w in wire:
        op = w[0] & 0xFFFF
        if op == F.OP_STEP and (w[1] >> 16) & 0xFF:
            return False
        if op == F.OP_SYS and len(w) >= 4 and w[1] in (F.SYS_PWM, F.SYS_PWM_BUS) and w[3]:
            return False
    return True


# =============================================================================== rebuild side
def load_drawing(path):
    """.chf (the original's own file, best) or .dxf (imported like the original's import)."""
    p = Path(path)
    if p.suffix.lower() == ".chf":
        from ..formats.chf import load_chf
        return load_chf(str(p))
    if p.suffix.lower() == ".dxf":
        from ..io.dxf_import import import_dxf
        return import_dxf(str(p))
    raise ValueError(f"unsupported drawing type {p.suffix!r} (need .chf or .dxf)")


@dataclass
class RebuildInputs:
    drawing: str
    params_dir: Optional[str] = None
    ini: Optional[str] = None
    jump_add_time: Optional[str] = None
    cam: dict = field(default_factory=dict)
    kind: str = "job"                    # "job" (Start / Dry Cut) or "frame" (边框)
    dry_run: Optional[bool] = None       # None: from the capture
    start_mm: Optional[tuple[float, float]] = None   # None: from the capture
    offset_mm: Optional[tuple[float, float]] = None  # None: auto (estimated), (0, 0) = none
    scaling: str = "card"                # "card": adopt the card's scaling when polled, "xml"


def make_machine(inp: RebuildInputs, card_scaling=None):
    """``Machine.from_dat_dir`` on the parameter XMLs (defaults without a directory), with the
    card's step scale and period adopted like ``UiController._read_scaling``."""
    from ..nc import Machine
    m = Machine.from_dat_dir(inp.params_dir, inp.ini, inp.jump_add_time) if inp.params_dir else Machine()
    note = "parameter files" if inp.params_dir else "Machine() defaults"
    if inp.scaling == "card" and card_scaling is not None and card_scaling.ppm > 0:
        sx, sy = card_scaling.step_ppm(card_scaling.x_axis), card_scaling.step_ppm(card_scaling.y_axis)
        if sx > 0 and sy > 0 and card_scaling.period_us > 0:
            if (abs(sx - m.pulses_per_mm_x) > 1e-9 or abs(sy - m.pulses_per_mm_y) > 1e-9
                    or abs(card_scaling.period_ms - m.period_ms) > 1e-9):
                note += (f"; card scaling adopted ({sx:g}/{sy:g} pulses/mm, {card_scaling.period_us} µs"
                         f" instead of {m.pulses_per_mm_x:g}/{m.pulses_per_mm_y:g}, {m.period_ms * 1000:g} µs)")
            m.pulses_per_mm_x, m.pulses_per_mm_y, m.period_ms = sx, sy, card_scaling.period_ms
            m.position_ppm = card_scaling.position_ppm
    return m, note


def compile_rebuild(inp: RebuildInputs, machine, start_mm: tuple[float, float], dry_run: bool,
                    offset_mm: tuple[float, float] = (0.0, 0.0), doc=None):
    """The rebuild's job for the inputs: ``compile_job`` (or the Frame items) as UiController
    would stream it, with ``doc.extra["job_offset"]`` = ``offset_mm``."""
    from ..cam.toolpath import CamParams
    from ..model import Document
    from ..nc import compile_items, compile_job, frame_items
    from ..nc.compiler import placed_document
    doc = doc if doc is not None else load_drawing(inp.drawing)
    extra = dict(doc.extra)
    if abs(offset_mm[0]) > 1e-12 or abs(offset_mm[1]) > 1e-12:
        extra["job_offset"] = (float(offset_mm[0]), float(offset_mm[1]))
    else:
        extra.pop("job_offset", None)
    doc = Document(parts=doc.parts, layers=doc.layers, version=doc.version, extra=extra)
    layers = None
    if inp.params_dir:
        from ..formats.params import MachineParams
        layers = MachineParams.load_dir(inp.params_dir)
    if inp.kind == "frame":
        bb = placed_document(doc).bbox()
        return compile_items(frame_items(bb, machine.frame_speed), machine, start_mm, dry_run=True)
    cam = {k: v for k, v in (inp.cam or {}).items() if k in CamParams.__dataclass_fields__}
    return compile_job(doc, cam, machine, start=start_mm, dry_run=dry_run, layers=layers)


def _drawing_part_start(ss: StepStream, dwell: int = 10) -> Optional[int]:
    """Step index where the path after the first rapid begins: the first dwell (``dwell`` or more
    zero-motion steps in a row, e.g. the JumpAddTime settle time) or the first process item after
    motion started.  Isolated zero steps inside the rapid's S-curve ramp do not count."""
    moving = (ss.dx != 0) | (ss.dy != 0)
    if not moving.any():
        return None
    first = int(np.argmax(moving))
    cand = [p[1] for p in ss.process if p[1] > first][:1]
    still = (~moving[first:]).astype(np.int8)
    if still.size >= dwell:
        win = np.convolve(still, np.ones(dwell, dtype=np.int8), mode="valid")
        hit = np.nonzero(win == dwell)[0]
        if hit.size:
            cand.append(first + int(hit[0]))
    return min(cand) if cand else 0


def offset_candidates(cap: StepStream, cap_start, reb: StepStream, reb_start,
                      ppm: tuple[float, float]) -> list[tuple[float, float]]:
    """Placement differences (mm) between the captured and the rebuild path, two estimates:
    the centre of the bounding box of the path after the first rapid, and the point where that
    part begins (the first pierce / cut start).  The same rule runs on both sides."""
    out = []
    ka, kb = _drawing_part_start(cap), _drawing_part_start(reb)
    if ka is None or kb is None:
        return out
    ca, cb = cap.cumulative(cap_start), reb.cumulative(reb_start)
    pa, pb = ca[ka + 1:] if ka + 1 < len(ca) else ca, cb[kb + 1:] if kb + 1 < len(cb) else cb
    out.append(((pa[:, 0].min() + pa[:, 0].max() - pb[:, 0].min() - pb[:, 0].max()) / 2.0 / ppm[0],
                (pa[:, 1].min() + pa[:, 1].max() - pb[:, 1].min() - pb[:, 1].max()) / 2.0 / ppm[1]))
    out.append(((ca[ka, 0] - cb[kb, 0]) / ppm[0], (ca[ka, 1] - cb[kb, 1]) / ppm[1]))
    return out


def estimate_offset(cap: StepStream, cap_start, reb: StepStream, reb_start,
                    ppm: tuple[float, float]) -> tuple[float, float]:
    """Bounding-box estimate of the placement difference (mm), see :func:`offset_candidates`."""
    c = offset_candidates(cap, cap_start, reb, reb_start, ppm)
    return c[0] if c else (0.0, 0.0)


def match_fraction(cap_cum: np.ndarray, reb_cum: np.ndarray, tol: float, max_points: int = 4000) -> float:
    """Share of the captured path points within ``tol`` pulses of the rebuild path (robust score
    for choosing the placement: a dropped step leaves the part before it matching exactly, a
    wrong offset matches nothing)."""
    stride = max(1, len(cap_cum) // max_points)
    d = _seg_distances(cap_cum[::stride], reb_cum)
    return float(np.mean(d <= tol)) if d.size else 0.0


def expected_stop_sequence(machine, pause: bool = False) -> list[tuple[int, list]]:
    """The writes the rebuild sends for 停止 (or a breakpoint pause) while a job runs
    (``UiController.stop`` / ``pause``: ``stop_manu`` + FIFO stop/clear), as (reg, words)."""
    from types import SimpleNamespace

    from ..controller.client import MotionController
    from ..controller.ui_adapter import DEFAULT_OUTPUT_MAP, UiController

    class _Rec(MotionController):
        def __init__(self):
            super().__init__()
            self.log: list = []

        def transact(self, req):
            self.log.append((req.addr, list(req.values)))
            return F.Response(req.func, req.tid, req.addr, req.count,
                              [0] * req.count if req.func == F.FUNC_READ else [])

    try:
        om = machine.process.output_map()
    except Exception:  # noqa: BLE001
        om = dict(DEFAULT_OUTPUT_MAP)
    ns = SimpleNamespace(machine=machine, output_map=om)
    rec = _Rec()
    rec.stop_manu(UiController._decel_counts(ns), int(machine.pt_laser_freq),
                  UiController._process_outputs_mask(ns))
    if not pause:
        rec.fifo_stop()
    rec.fifo_clear()
    return rec.log


# =============================================================================== comparison
PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    core: bool = True                 # part of the cutting-job verdict


@dataclass
class Settings:
    tol_pulses: float = 1.0           # time-aligned cumulative deviation tolerance
    path_tol_pulses: float = 2.0      # spatial path deviation tolerance
    context: int = 5                  # steps / items shown around a divergence


@dataclass
class Comparison:
    verdict: str = ""
    checks: list = field(default_factory=list)
    info: dict = field(default_factory=dict)
    first_divergence: Optional[dict] = None
    step_divergence: Optional[dict] = None
    laser_divergence: dict = field(default_factory=dict)
    process_diff: list = field(default_factory=list)
    frame_diff: Optional[dict] = None
    arrays: dict = field(default_factory=dict)       # for plots (numpy arrays)

    def check(self, name: str) -> Optional[Check]:
        return next((c for c in self.checks if c.name == name), None)

    def to_dict(self) -> dict:
        def conv(o):
            if isinstance(o, dict):
                return {str(k): conv(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [conv(v) for v in o]
            if isinstance(o, np.generic):
                return o.item()
            if isinstance(o, float) and not math.isfinite(o):
                return None
            return o
        return conv({"verdict": self.verdict,
                     "checks": [c.__dict__ for c in self.checks],
                     "info": self.info, "first_divergence": self.first_divergence,
                     "step_divergence": self.step_divergence,
                     "laser_divergence": self.laser_divergence,
                     "process_diff": self.process_diff, "frame_diff": self.frame_diff})


def _seg_distances(points: np.ndarray, path: np.ndarray, cell: float = 64.0) -> np.ndarray:
    """Distance of each point to the polyline ``path`` (pulses), via a uniform grid."""
    pts = points.astype(float)
    pth = path.astype(float)
    if len(pth) == 1:
        return np.hypot(pts[:, 0] - pth[0, 0], pts[:, 1] - pth[0, 1])
    a, b = pth[:-1], pth[1:]
    keep = np.ones(len(a), dtype=bool)
    a, b = a[keep], b[keep]
    lo = np.floor(np.minimum(a, b) / cell).astype(np.int64)
    hi = np.floor(np.maximum(a, b) / cell).astype(np.int64)
    grid: dict = {}
    for i in range(len(a)):
        for gx in range(lo[i, 0], hi[i, 0] + 1):
            for gy in range(lo[i, 1], hi[i, 1] + 1):
                grid.setdefault((gx, gy), []).append(i)
    grid_np = {k: np.asarray(v, dtype=np.int64) for k, v in grid.items()}
    ab = b - a
    ab2 = (ab ** 2).sum(axis=1)
    ab2[ab2 == 0] = 1.0

    def dist_to(p, idx):
        ap = p - a[idx]
        t = np.clip((ap * ab[idx]).sum(axis=1) / ab2[idx], 0.0, 1.0)
        q = a[idx] + ab[idx] * t[:, None]
        return float(np.min(np.hypot(q[:, 0] - p[0], q[:, 1] - p[1])))

    out = np.empty(len(pts))
    uniq, inv = np.unique(pts, axis=0, return_inverse=True)
    res = np.empty(len(uniq))
    span = int(max(np.ptp(pth[:, 0]), np.ptp(pth[:, 1]), np.ptp(pts[:, 0]), np.ptp(pts[:, 1])) / cell) + 3
    for u, p in enumerate(uniq):
        cx, cy = int(math.floor(p[0] / cell)), int(math.floor(p[1] / cell))
        best = math.inf
        r = 0
        while r <= span:
            cand = []
            for gx in range(cx - r, cx + r + 1):
                for gy in (range(cy - r, cy + r + 1) if abs(gx - cx) == r else (cy - r, cy + r)):
                    v = grid_np.get((gx, gy))
                    if v is not None:
                        cand.append(v)
            if cand:
                best = min(best, dist_to(p, np.concatenate(cand)))
            if best <= r * cell:
                break
            r += 1
        if not math.isfinite(best):
            best = dist_to(p, np.arange(len(a)))
        res[u] = best
    out[:] = res[np.asarray(inv).reshape(-1)]
    return out


def _context_steps(ss: StepStream, cum: np.ndarray, k: int, n: int) -> list[dict]:
    out = []
    for i in range(max(0, k - n), min(ss.n, k + n + 1)):
        out.append({"step": i, "wire": int(ss.step_wire_index[i]), "dx": int(ss.dx[i]), "dy": int(ss.dy[i]),
                    "duty": int(ss.duty[i]), "freq": int(ss.freq[i]), "power": int(ss.power[i]),
                    "x": int(cum[i + 1, 0]), "y": int(cum[i + 1, 1])})
    return out


def _context_items(wire, i: int, n: int) -> list[dict]:
    return [{"index": j, "text": describe(wire[j][0] & 0xFFFF, wire[j][1:])}
            for j in range(max(0, i - n), min(len(wire), i + n + 1))]


def _nearest_process(ss: StepStream, step: int) -> Optional[dict]:
    best = None
    for wi, sb, op, prm in ss.process:
        if best is None or abs(sb - step) < abs(best[1] - step):
            best = (wi, sb, op, prm)
    if best is None:
        return None
    return {"wire": best[0], "after_steps": best[1], "text": describe(best[2], best[3])}


def _runs(on: np.ndarray) -> list[tuple[int, int]]:
    if on.size == 0:
        return []
    d = np.diff(np.concatenate([[0], on.astype(np.int8), [0]]))
    starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
    return list(zip(starts.tolist(), ends.tolist()))


def compare_job(cap_job: CapturedJob, cap_start: tuple[int, int], reb_job, reb_start: tuple[int, int],
                ppm: tuple[float, float], period_ms: float, settings: Optional[Settings] = None,
                expected_stop: Optional[list] = None) -> Comparison:
    """Compare the captured FIFO stream with a rebuild :class:`~mlaser.nc.compiler.Job`.

    ``cap_start`` / ``reb_start`` are the start positions in step pulses.  Returns a
    :class:`Comparison` with the checks, the first divergence (item level and step level) with
    context, statistics and the arrays for the plots.
    """
    s = settings or Settings()
    cw, rw = cap_job.wire, list(reb_job.wire)
    cs, rs = step_stream(cw), step_stream(rw)
    cc, rc = cs.cumulative(cap_start), rs.cumulative(reb_start)
    cmp = Comparison()
    info = cmp.info
    info.update(capture_steps=cs.n, rebuild_steps=rs.n, capture_items=len(cw), rebuild_items=len(rw),
                capture_process_items=len(cs.process), rebuild_process_items=len(rs.process),
                capture_start=list(cap_start), rebuild_start=list(reb_start), ppm=list(ppm),
                period_ms=period_ms)

    # ---- item level: identical?  first divergence
    identical = cw == rw
    if not identical:
        i = next((j for j, (a, b) in enumerate(zip(cw, rw)) if a != b), min(len(cw), len(rw)))
        r0 = i
        if i < len(rw):
            while r0 > 0 and rw[r0 - 1] == rw[i] and r0 - 1 < len(cw) and cw[r0 - 1] == rw[i]:
                r0 -= 1
        cmp.first_divergence = {
            "item": i,
            # a dropped / extra item inside a run of identical items shows up at the run's end:
            # the difference lies somewhere in items run_start..item
            "run_start": r0,
            "capture_steps_before": int(np.searchsorted(cs.step_wire_index, i)),
            "rebuild_steps_before": int(np.searchsorted(rs.step_wire_index, i)),
            "capture": _context_items(cw, i, s.context), "rebuild": _context_items(rw, i, s.context)}

    # ---- steps
    n = min(cs.n, rs.n)
    cmp.checks.append(Check("step count", PASS if cs.n == rs.n else FAIL,
                            f"capture {cs.n}, rebuild {rs.n} (Δ {cs.n - rs.n:+d})"))
    dmis = np.nonzero((cs.dx[:n] != rs.dx[:n]) | (cs.dy[:n] != rs.dy[:n]))[0]
    k0 = int(dmis[0]) if dmis.size else (n if cs.n != rs.n else None)
    if k0 is not None:
        r0 = k0
        if k0 < rs.n:
            same = (rs.dx == rs.dx[k0]) & (rs.dy == rs.dy[k0])
            while r0 > 0 and same[r0 - 1] and r0 - 1 < cs.n and cs.dx[r0 - 1] == rs.dx[k0] and cs.dy[r0 - 1] == rs.dy[k0]:
                r0 -= 1
        cmp.step_divergence = {
            "step": k0, "run_start": r0, "capture_wire": int(cs.step_wire_index[k0]) if k0 < cs.n else None,
            "rebuild_wire": int(rs.step_wire_index[k0]) if k0 < rs.n else None,
            "capture_pos": [int(v) for v in cc[min(k0, cs.n)]], "rebuild_pos": [int(v) for v in rc[min(k0, rs.n)]],
            "capture": _context_steps(cs, cc, k0, s.context), "rebuild": _context_steps(rs, rc, k0, s.context),
            "nearest_process_capture": _nearest_process(cs, k0),
            "nearest_process_rebuild": _nearest_process(rs, k0)}
    cmp.checks.append(Check("step deltas", PASS if k0 is None else FAIL,
                            "identical" if k0 is None else
                            f"{dmis.size} of {n} aligned steps differ; first at step {k0}", core=False))
    # time-aligned cumulative deviation (the shorter stream holds its last position)
    m = max(cs.n, rs.n) + 1
    cpad = np.vstack([cc, np.repeat(cc[-1:], m - len(cc), axis=0)]) if len(cc) < m else cc
    rpad = np.vstack([rc, np.repeat(rc[-1:], m - len(rc), axis=0)]) if len(rc) < m else rc
    dev = np.hypot(*(cpad - rpad).T.astype(float))
    kmax = int(np.argmax(dev)) if dev.size else 0
    info.update(max_deviation_pulses=float(dev.max()) if dev.size else 0.0, max_deviation_step=kmax,
                max_deviation_mm=float(dev.max() / min(ppm)) if dev.size else 0.0,
                mean_deviation_pulses=float(dev.mean()) if dev.size else 0.0,
                max_dev_x=int(np.abs(cpad[:, 0] - rpad[:, 0]).max()), max_dev_y=int(np.abs(cpad[:, 1] - rpad[:, 1]).max()))
    cmp.checks.append(Check("cumulative position (time aligned)",
                            PASS if info["max_deviation_pulses"] <= s.tol_pulses else FAIL,
                            f"max {info['max_deviation_pulses']:.1f} pulses ({info['max_deviation_mm']:.3f} mm) "
                            f"at step {kmax}; |Δx| ≤ {info['max_dev_x']}, |Δy| ≤ {info['max_dev_y']}; "
                            f"tolerance {s.tol_pulses:g}"))
    # spatial deviation (order independent)
    d_cr = _seg_distances(cc, rc)
    d_rc = _seg_distances(rc, cc)
    hd = float(max(d_cr.max(), d_rc.max()))
    info.update(path_capture_to_rebuild_max=float(d_cr.max()), path_capture_to_rebuild_p99=float(np.percentile(d_cr, 99)),
                path_capture_to_rebuild_mean=float(d_cr.mean()), path_rebuild_to_capture_max=float(d_rc.max()),
                path_hausdorff=hd, path_hausdorff_mm=hd / min(ppm))
    cmp.checks.append(Check("path deviation (spatial)", PASS if hd <= s.path_tol_pulses else FAIL,
                            f"Hausdorff {hd:.1f} pulses ({hd / min(ppm):.3f} mm); capture→rebuild max "
                            f"{d_cr.max():.1f} / p99 {np.percentile(d_cr, 99):.1f} / mean {d_cr.mean():.2f}; "
                            f"rebuild→capture max {d_rc.max():.1f}; tolerance {s.path_tol_pulses:g}"))
    end_c, end_r = cc[-1], rc[-1]
    cmp.checks.append(Check("end position", PASS if (end_c == end_r).all() else FAIL,
                            f"capture ({end_c[0]}, {end_c[1]}), rebuild ({end_r[0]}, {end_r[1]}) pulses"))
    # ---- laser fields per step
    for name, a, b, mask in (
            ("duty", cs.duty[:n], rs.duty[:n], None),
            ("frequency", cs.freq[:n], rs.freq[:n], (cs.duty[:n] > 0) | (rs.duty[:n] > 0)),
            ("power", cs.power[:n], rs.power[:n], None)):
        mm = a != b
        if mask is not None:
            mm_all = int(mm.sum())
            mm = mm & mask
        bad = np.nonzero(mm)[0]
        det = (f"{bad.size} of {n} aligned steps differ" if bad.size else f"identical on {n} aligned steps")
        if name == "frequency":
            det += f" (laser-on steps; {mm_all} incl. laser-off steps)"
        if bad.size:
            k = int(bad[0])
            cmp.laser_divergence[name] = {"step": k, "capture": int(a[k]), "rebuild": int(b[k]),
                                          "capture_context": _context_steps(cs, cc, k, s.context),
                                          "rebuild_context": _context_steps(rs, rc, k, s.context)}
            det += f"; first at step {k}: capture {int(a[k])}, rebuild {int(b[k])}"
        cmp.checks.append(Check(f"{name} per step", PASS if not bad.size else FAIL, det))
    cr_, rr_ = _runs(cs.duty > 0), _runs(rs.duty > 0)
    info.update(capture_laser_runs=len(cr_), rebuild_laser_runs=len(rr_),
                capture_laser_steps=int((cs.duty > 0).sum()), rebuild_laser_steps=int((rs.duty > 0).sum()))
    # ---- process items
    ck = [(op, prm) for _wi, _sb, op, prm in cs.process]
    rk = [(op, prm) for _wi, _sb, op, prm in rs.process]
    sm = difflib.SequenceMatcher(None, ck, rk, autojunk=False)
    first_pd = None
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        ent = {"tag": tag, "capture_range": [i1, i2], "rebuild_range": [j1, j2],
               "capture": [{"n": i, "wire": cs.process[i][0], "after_steps": cs.process[i][1],
                            "text": describe(*ck[i])} for i in range(i1, i2)],
               "rebuild": [{"n": j, "wire": rs.process[j][0], "after_steps": rs.process[j][1],
                            "text": describe(*rk[j])} for j in range(j1, j2)]}
        cmp.process_diff.append(ent)
        first_pd = first_pd or ent
    pfirst = next((i for i, (a, b) in enumerate(zip(ck, rk)) if a != b),
                  None if len(ck) == len(rk) else min(len(ck), len(rk)))
    info["process_first_index"] = pfirst
    if pfirst is not None:
        info["process_first"] = {
            "index": pfirst,
            "capture": (describe(*ck[pfirst]) + f" (wire {cs.process[pfirst][0]}, after step {cs.process[pfirst][1]})")
            if pfirst < len(ck) else None,
            "rebuild": (describe(*rk[pfirst]) + f" (wire {rs.process[pfirst][0]}, after step {rs.process[pfirst][1]})")
            if pfirst < len(rk) else None}
    moved = [(i, j) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag == "equal"
             for i, j in zip(range(i1, i2), range(j1, j2)) if cs.process[i][1] != rs.process[j][1]]
    det = (f"{len(ck)} items identical in order and values" if not cmp.process_diff else
           f"{len(cmp.process_diff)} differing block(s); first different item #{pfirst}: capture "
           f"{info['process_first']['capture']}, rebuild {info['process_first']['rebuild']}")
    cmp.checks.append(Check("process items (sequence and values)", PASS if not cmp.process_diff else FAIL, det))
    info["process_items_at_other_step"] = len(moved)
    if moved:
        i, j = moved[0]
        info["process_first_moved"] = {"capture_item": i, "capture_after_steps": cs.process[i][1],
                                       "rebuild_after_steps": rs.process[j][1], "text": describe(*ck[i])}
    cmp.checks.append(Check("process item positions", PASS if not moved else FAIL,
                            "every matching item after the same step" if not moved else
                            f"{len(moved)} matching items placed after a different step; first: capture item "
                            f"{moved[0][0]} after step {cs.process[moved[0][0]][1]}, rebuild after "
                            f"{rs.process[moved[0][1]][1]}", core=False))
    info["capture_wait_ms"], info["rebuild_wait_ms"] = wait_ms_total(cw), wait_ms_total(rw)

    # ---- frames
    seq0 = cap_job.first_seq if cap_job.first_seq is not None else 1
    cap_lens = [len(f.items) for f in cap_job.frames]
    reb_frames = F.build_fifo_frames(rw, seq0)
    reb_lens = [len(fr) - 1 for fr in reb_frames]
    repack = [len(fr) - 1 for fr in F.build_fifo_frames(cw, seq0)]
    fd = next((i for i, (a, b) in enumerate(zip(cap_lens, reb_lens)) if a != b),
              None if len(cap_lens) == len(reb_lens) else min(len(cap_lens), len(reb_lens)))
    cmp.frame_diff = {"capture_frames": len(cap_lens), "rebuild_frames": len(reb_lens), "first_different": fd,
                      "capture_words": cap_lens[fd] if fd is not None and fd < len(cap_lens) else None,
                      "rebuild_words": reb_lens[fd] if fd is not None and fd < len(reb_lens) else None,
                      "capture_packing_rule": repack == cap_lens}
    cmp.checks.append(Check("frame packing", PASS if fd is None else FAIL,
                            f"capture {len(cap_lens)} frames, rebuild {len(reb_lens)}"
                            + ("" if fd is None else f"; first different frame {fd}: {cmp.frame_diff['capture_words']}"
                               f" vs {cmp.frame_diff['rebuild_words']} words"), core=False))
    cmp.checks.append(Check("capture packing = 1200-byte rule", PASS if repack == cap_lens else WARN,
                            "re-packing the captured items with frames.build_fifo_frames gives the same frames"
                            if repack == cap_lens else
                            f"re-packed: {len(repack)} frames; the original packs differently (frames.build_fifo_frames)",
                            core=False))
    seq_ok = not cap_job.seq_gaps and (cap_job.first_seq_expected in (None, cap_job.first_seq))
    cmp.checks.append(Check("frame sequence numbers", PASS if seq_ok else WARN,
                            f"first {cap_job.first_seq} (RO[15]+1 = {cap_job.first_seq_expected}), "
                            f"{len(cap_job.seq_gaps)} gaps, {len(cap_job.conflicts)} conflicting re-sends, "
                            f"{cap_job.retransmissions} retransmissions, {cap_job.resends} re-sent frames",
                            core=False))
    cmp.checks.append(Check("flow control (RO[16] ≥ frame + 2000)", PASS if not cap_job.flow_violations else WARN,
                            f"min margin {cap_job.min_flow_margin} bytes, {cap_job.flow_violations} frames below "
                            f"{FLOW_MARGIN}, up to {cap_job.max_frames_per_fill} frames per fill, "
                            f"{cap_job.frames_before_start} frames before 103 := 2", core=False))
    # ---- time
    t_c, t_r = cs.n * period_ms / 1000.0, rs.n * period_ms / 1000.0
    info.update(capture_step_time_s=t_c, rebuild_step_time_s=t_r,
                rebuild_total_time_s=float(getattr(reb_job, "total_time_s", t_r)),
                capture_wall_s=cap_job.wall_s)
    wall = f", capture wall clock {cap_job.wall_s:.2f} s (103 := 2 → RO[19] = 0)" if cap_job.wall_s is not None else ""
    cmp.checks.append(Check("job time", PASS if cs.n == rs.n and info["capture_wait_ms"] == info["rebuild_wait_ms"] else FAIL,
                            f"step replay capture {t_c:.3f} s, rebuild {t_r:.3f} s; timed waits "
                            f"{info['capture_wait_ms']} / {info['rebuild_wait_ms']} ms; rebuild estimate "
                            f"{info['rebuild_total_time_s']:.2f} s{wall}", core=False))
    # ---- pause / stop
    stop = cap_job.stop_sequence()
    if stop:
        exp = expected_stop or []
        ok = [tuple([r, tuple(w)]) for r, w in stop[:len(exp)]] == [tuple([r, tuple(w)]) for r, w in exp]
        cmp.info["capture_stop_sequence"] = stop
        cmp.info["rebuild_stop_sequence"] = exp
        cmp.checks.append(Check("pause/stop sequence", PASS if ok else FAIL,
                                f"capture {_fmt_writes(stop)}; rebuild {_fmt_writes(exp)}", core=False))
    else:
        cmp.checks.append(Check("pause/stop sequence", INFO, "none in this job (it ran to the end)", core=False))
    # ---- verdict
    core_fail = [c for c in cmp.checks if c.core and c.status == FAIL]
    other = [c for c in cmp.checks if not c.core and c.status in (FAIL, WARN)]
    if identical:
        cmp.verdict = "IDENTICAL: the rebuild's FIFO stream equals the original's item for item"
    elif not core_fail:
        cmp.verdict = ("EQUIVALENT: same path, laser fields and process items within tolerance; "
                       f"{len(other)} secondary difference(s)")
    else:
        cmp.verdict = (f"DIFFERENT: {len(core_fail)} core check(s) failed ("
                       + ", ".join(c.name for c in core_fail) + ")")
    cmp.arrays = {"cap_cum": cc, "reb_cum": rc, "dev": dev, "path_dev": d_cr,
                  "cap_duty": cs.duty, "reb_duty": rs.duty, "cap_freq": cs.freq, "reb_freq": rs.freq,
                  "ppm": ppm}
    return cmp


def _fmt_writes(seq: Sequence[tuple[int, list]]) -> str:
    return "; ".join(f"{r} := {w if len(w) != 1 else w[0]}" for r, w in seq) or "-"


# =============================================================================== one call
@dataclass
class Result:
    analysis: CaptureAnalysis
    job: CapturedJob
    rebuild_job: object
    comparison: Comparison
    notes: list = field(default_factory=list)
    start_mm: tuple = (0.0, 0.0)
    offset_mm: tuple = (0.0, 0.0)
    dry_run: bool = False
    machine: object = None


def select_job(an: CaptureAnalysis, which: Optional[int] = None) -> CapturedJob:
    """Job ``which`` (index, negative from the end) or the one with the most steps."""
    if not an.jobs:
        raise ValueError("no FIFO job in the capture (no write(102) frames after a 103 := 1)")
    if which is None:
        return max(an.jobs, key=lambda j: (j.steps, j.index))
    return an.jobs[which]


def run(capture_path, inp: RebuildInputs, card_host: Optional[str] = None, card_port: int = CARD_PORT,
        job_index: Optional[int] = None, settings: Optional[Settings] = None, x_axis: int = 0,
        y_axis: int = 1, position_ppm: Optional[float] = None, traffic: Optional[Traffic] = None) -> Result:
    """Capture + inputs -> :class:`Result` (see the module doc for the steps)."""
    tr = traffic or load_traffic(capture_path, card_host, card_port)
    an = analyse(tr, x_axis, y_axis)
    job = select_job(an, job_index)
    notes: list[str] = []
    sc = an.scaling()
    machine, mnote = make_machine(inp, sc)
    notes.append(f"machine: {mnote}")
    pppm = position_ppm or an.position_ppm or machine.position_ppm
    notes.append(f"position counts/mm: {pppm:g} ("
                 + ("CLI" if position_ppm else "card sys word 17" if an.position_ppm else "Machine default, INFERRED")
                 + ")")
    ppm = (machine.pulses_per_mm_x, machine.pulses_per_mm_y)
    if inp.start_mm is not None:
        start_mm = tuple(map(float, inp.start_mm))
        notes.append(f"start position {start_mm} mm (CLI)")
    elif job.start_counts is not None:
        start_mm = (job.start_counts[0] / pppm, job.start_counts[1] / pppm)
        notes.append(f"start position from the card's last position read before the first frame: "
                     f"{job.start_counts} counts = ({start_mm[0]:.4f}, {start_mm[1]:.4f}) mm")
    else:
        start_mm = (0.0, 0.0)
        notes.append("no position read before the job: start position (0, 0) assumed (use --start)")
    cap_wire = job.wire
    dry = is_dry_run(cap_wire) if inp.dry_run is None else bool(inp.dry_run)
    if inp.dry_run is None:
        notes.append(f"dry run {'detected (no laser-on step or PWM item)' if dry else 'off (the capture switches the laser on)'}")
    cap_start = (round(start_mm[0] * ppm[0]), round(start_mm[1] * ppm[1]))
    doc = load_drawing(inp.drawing)
    offset = (0.0, 0.0) if inp.offset_mm is None else tuple(map(float, inp.offset_mm))
    reb = compile_rebuild(inp, machine, start_mm, dry, offset, doc=doc)
    if inp.offset_mm is None and inp.kind == "job":
        cs = step_stream(cap_wire)
        cap_cum = cs.cumulative(cap_start)
        tol = (settings or Settings()).path_tol_pulses
        best = (match_fraction(cap_cum, step_stream(reb.wire).cumulative(tuple(reb.start_pulses)), tol), offset, reb)
        tried = [offset]
        for cand in offset_candidates(cs, cap_start, step_stream(reb.wire), tuple(reb.start_pulses), ppm):
            if all(abs(cand[0] - t[0]) * ppm[0] < 0.5 and abs(cand[1] - t[1]) * ppm[1] < 0.5 for t in tried):
                continue
            o = cand
            r = compile_rebuild(inp, machine, start_mm, dry, o, doc=doc)
            for _ in range(2):          # refine: the sort / start point may change with the placement
                d = estimate_offset(cs, cap_start, step_stream(r.wire), tuple(r.start_pulses), ppm)
                if abs(d[0]) * ppm[0] < 0.5 and abs(d[1]) * ppm[1] < 0.5:
                    break
                o = (o[0] + d[0], o[1] + d[1])
                r = compile_rebuild(inp, machine, start_mm, dry, o, doc=doc)
            tried.append(o)
            f = match_fraction(cap_cum, step_stream(r.wire).cumulative(tuple(r.start_pulses)), tol)
            if f > best[0] + 1e-3:
                best = (f, o, r)
        _f, offset, reb = best
        if offset != (0.0, 0.0):
            notes.append(f"placement offset estimated from the captured path: ({offset[0]:.4f}, {offset[1]:.4f}) mm "
                         f"({best[0]:.0%} of the path within tolerance; doc.extra['job_offset']; "
                         "use --offset to fix it, --offset 0,0 to disable)")
    elif inp.offset_mm is not None and offset != (0.0, 0.0):
        notes.append(f"placement offset {offset} mm (CLI)")
    try:
        exp_stop = expected_stop_sequence(machine)
    except Exception as e:  # noqa: BLE001 - informative only
        exp_stop = []
        notes.append(f"rebuild stop sequence unavailable: {e}")
    cmp = compare_job(job, cap_start, reb, tuple(reb.start_pulses), ppm, machine.period_ms, settings, exp_stop)
    cmp.info["position_ppm"] = pppm
    if job.end_counts is not None and job.start_counts is not None and job.end_ts is not None:
        cs = step_stream(cap_wire)
        k = pppm / ppm[0], pppm / ppm[1]
        exp = (job.start_counts[0] + int(cs.dx.sum()) * k[0], job.start_counts[1] + int(cs.dy.sum()) * k[1])
        d = (job.end_counts[0] - exp[0], job.end_counts[1] - exp[1])
        cmp.info["card_end_minus_integrated_counts"] = d
        ok = abs(d[0]) <= max(1.0, k[0]) and abs(d[1]) <= max(1.0, k[1])
        cmp.checks.append(Check("card-reported end position", PASS if ok else WARN,
                                f"card {job.end_counts} counts after the job, start + Σsteps = "
                                f"({exp[0]:.0f}, {exp[1]:.0f}); Δ ({d[0]:.0f}, {d[1]:.0f})", core=False))
    return Result(an, job, reb, cmp, notes, start_mm, offset, dry, machine)
