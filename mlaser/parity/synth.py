"""Synthetic card captures from the rebuild's own simulator traffic (tests, demos).

:func:`record_job` streams a compiled job to an in-process :class:`CardSimulator` exactly as
the rebuild does (connect, scaling read, status read, ``JobStreamer.run_job``) and records every
request and reply the simulator handled.  :func:`to_messages` turns the record into
:class:`~mlaser.parity.pcap.Message` objects with the real machine's addresses (PC
10.1.1.100, card 10.1.1.168:502), ready for :func:`mlaser.parity.pcap.write_capture`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .pcap import Message

PC_ADDR = ("10.1.1.100", 50123)
CARD_ADDR = ("10.1.1.168", 502)


@dataclass
class Record:
    t_req: float
    request: bytes
    reply: Optional[bytes]
    t_rep: float


def record_job(job, start_mm: tuple[float, float] = (0.0, 0.0), time_scale: float = 50.0,
               position_ppm: float = 1000.0, stop_after_s: Optional[float] = None,
               **sim_kw) -> list[Record]:
    """Run ``job`` on a fresh simulator and return the recorded exchanges.

    The simulator's axes start at ``start_mm`` (position counts = mm x ``position_ppm``), its
    step scale and period are the job machine's.  ``stop_after_s`` cancels the stream after
    that many seconds of wall time and sends the rebuild's job stop (``stop_manu``, 103 := 3,
    103 := 1), to produce a stop sequence.
    """
    import threading

    from ..controller.client import MotionController, TransportConfig
    from ..controller.simulator import CardSimulator
    from ..controller.stream import JobStreamer
    m = job.machine
    sim = CardSimulator(period_ms=m.period_ms, time_scale=time_scale, ppm=position_ppm,
                        step_ppm=m.pulses_per_mm_x, ppr=m.ppr, **sim_kw)
    sim.state.axes[0].pos = round(start_mm[0] * position_ppm)
    sim.state.axes[1].pos = round(start_mm[1] * position_ppm)
    log: list[Record] = []
    orig = sim.handle

    def handle(data: bytes):
        t0 = time.time()
        out = orig(data)
        log.append(Record(t0, bytes(data), out, time.time()))
        return out

    sim.handle = handle
    sim.start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port)
        with MotionController(config=cfg, pulses_per_mm=position_ppm) as mc:
            mc.connect()
            mc.read_scaling()
            mc.read_status()
            js = JobStreamer(mc)
            cancel = threading.Event()
            if stop_after_s is not None:
                threading.Timer(stop_after_s, cancel.set).start()
            js.run_job(job, cancel=cancel, timeout_s=120)
            if cancel.is_set():
                from .capture import expected_stop_sequence
                for reg, words in expected_stop_sequence(m):
                    mc.write_regs(reg, words)
            for _ in range(3):
                mc.read_status()
    finally:
        sim.stop()
    return log


def to_messages(records: list[Record], pc=PC_ADDR, card=CARD_ADDR, t0: float = 1_760_000_000.0) -> list[Message]:
    """Records -> request/reply messages with machine addresses and timestamps from ``t0``."""
    if not records:
        return []
    base = records[0].t_req
    out = []
    for r in records:
        out.append(Message(t0 + r.t_req - base, pc, card, r.request, "udp"))
        if r.reply is not None:
            out.append(Message(t0 + max(r.t_rep, r.t_req + 1e-6) - base, card, pc, r.reply, "udp"))
    return out


# ----------------------------------------------------------------------------- fault injection
def _is_fifo_write(m: Message):
    from ..protocol import frames as F
    if m.dst[1] != CARD_ADDR[1]:
        return None
    try:
        req = F.decode_request(m.data)
    except F.FrameError:
        return None
    return req if req.func == F.FUNC_WRITE and req.addr == F.REG_FIFO_DATA else None


def wire_items(messages: list[Message]) -> list[tuple[int, list[int]]]:
    """(frame seq, item) of every FIFO item, first send of each frame sequence only."""
    seen, out = set(), []
    for m in messages:
        req = _is_fifo_write(m)
        if req is None or req.values[0] in seen:
            continue
        seen.add(req.values[0])
        w = req.values[1:]
        i = 0
        while i < len(w):
            k = (w[i] >> 16) // 4
            out.append((req.values[0], list(w[i:i + 1 + k])))
            i += 1 + k
    return out


def edit_wire(messages: list[Message], drop: Optional[int] = None, set_step: Optional[tuple] = None,
              swap: Optional[tuple[int, int]] = None) -> list[Message]:
    """Inject a difference into the FIFO frames of a recorded capture (tests).

    ``drop``: remove wire item i (its frame gets shorter); ``set_step = (i, dict)``: re-encode
    step item i with changed fields (dx, dy, freq, duty, power); ``swap = (i, j)``: exchange
    items i and j (each stays in its frame slot).  Every send of an affected frame is
    re-encoded with its original transaction id; replies are left as they are.
    """
    from dataclasses import replace

    from ..protocol import frames as F
    flat = wire_items(messages)
    if set_step is not None:
        i, ch = set_step
        seq, it = flat[i]
        dx, dy, fq, du, pw = F.decode_step(it[1:3])
        v = dict(dx=dx, dy=dy, freq=fq, duty=du, power=pw)
        v.update(ch)
        flat[i] = (seq, F.encode_step(v["dx"], v["dy"], v["freq"], v["duty"], v["power"]))
    if swap is not None:
        i, j = swap
        (si, a), (sj, b) = flat[i], flat[j]
        flat[i], flat[j] = (si, b), (sj, a)
    if drop is not None:
        del flat[drop]
    frames: dict[int, list[int]] = {}
    for seq, it in flat:
        frames.setdefault(seq, []).extend(it)
    out = []
    for m in messages:
        req = _is_fifo_write(m)
        if req is not None and req.values[0] in frames:
            items = frames[req.values[0]]
            if items != req.values[1:]:
                req.values = [req.values[0], *items]
                req.count = len(req.values)
                m = replace(m, data=F.encode_request(req))
        out.append(m)
    return out
