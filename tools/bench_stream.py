#!/usr/bin/env python3
"""Streaming headroom benchmark: how close does the card's FIFO come to running dry?

The card simulator runs in its own process (so only the PC side is loaded).  The job is streamed
either in a thread of this process (``thread``) or by :class:`mlaser.nc.stream.ProcessStreamer`
in a spawned process (``process``), while ``--load`` CPU-busy Python threads run in this
process (a busy GUI).  Reported per mode:

* ``min fill``  - lowest motion buffered in the card FIFO while frames remained, in ms of
  motion (measured by the simulator itself, from its FIFO byte count and the item size);
* ``pc view``   - the same seen by the streamer from RO[16] polls;
* watchdog trips, simulator underruns (FIFO empty while running), wall time / real time.

Examples::

    .venv/bin/python tools/bench_stream.py                      # both modes, 2 load threads
    .venv/bin/python tools/bench_stream.py --mode process --load 4 --seconds 30
    .venv/bin/python tools/bench_stream.py --capacity 16384 --period-ms 0.25

Real-time factor 1.0 (``--scale 1``) is the honest test; larger scales stress the PC harder.
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mlaser.controller import frames as F  # noqa: E402

ITEM_BYTES = 12            # one op-3000 step item on the card (header + 2 params)


def _sim_proc(conn, capacity: int, period_ms: float, scale: float) -> None:
    from mlaser.controller.simulator import CardSimulator
    sim = CardSimulator(fifo_capacity=capacity, period_ms=period_ms, time_scale=scale).start()
    conn.send(sim.port)
    samples: list = []          # (t, fifo bytes) while running
    last_frame_t = 0.0
    last_seq = None
    while True:
        if conn.poll(0.002):
            msg = conn.recv()
            if msg == "reset":
                samples, last_frame_t, last_seq = [], 0.0, None
                with sim.lock:
                    sim.state.underruns = 0
                conn.send("ok")
            elif msg == "report":
                # only the time before the last frame arrived counts: afterwards the FIFO drains
                # by design at the end of the job
                mins = [b for t, b in samples if t < last_frame_t]
                with sim.lock:
                    conn.send((min(mins) if mins else None, sim.state.underruns, sim.position(0)))
            elif msg == "quit":
                sim.stop()
                return
        now = time.monotonic()
        with sim.lock:
            st = sim.state
            if st.fifo_seq != last_seq:
                last_seq, last_frame_t = st.fifo_seq, now
            if st.fifo_running:
                samples.append((now, st.fifo_bytes))


def _job(seconds: float, period_ms: float):
    from mlaser.nc.stream import WireJob
    n = int(seconds * 1000 / period_ms)
    # a circle-ish pattern: varying small deltas, laser on
    import math
    wire = []
    for k in range(n):
        a = 2 * math.pi * k / 2000.0
        wire.append(F.encode_step(int(round(20 * math.cos(a))), int(round(20 * math.sin(a))), 5000, 100, 5000))
    return WireJob(wire, period_ms, n * period_ms / 1000.0)


def _hog(stop: threading.Event) -> None:
    x = 0
    while not stop.is_set():
        for k in range(20000):
            x += k * k
        [object() for _ in range(2000)]


def run(mode: str, args, conn) -> dict:
    from mlaser.controller.client import MotionController, TransportConfig
    from mlaser.nc.stream import JobStreamer, ProcessStreamer
    conn.send("reset")
    conn.recv()
    cfg = TransportConfig(host="127.0.0.1", port=args.port)
    job = _job(args.seconds, args.period_ms)
    stop = threading.Event()
    hogs = [threading.Thread(target=_hog, args=(stop,), daemon=True) for _ in range(args.load)]
    for h in hogs:
        h.start()
    t0 = time.perf_counter()
    try:
        if mode == "process":
            ps = ProcessStreamer(cfg, 1000.0, watchdog_ms=args.watchdog_ms)
            ps.run_job(job, timeout_s=args.seconds * 20 + 60)
            buf = ps.buf
        else:
            with MotionController(config=cfg) as mc:
                js = JobStreamer(mc, watchdog_ms=args.watchdog_ms)
                js.run_job(job, timeout_s=args.seconds * 20 + 60)
                buf = js.buf.__dict__
    finally:
        stop.set()
    wall = time.perf_counter() - t0
    conn.send("report")
    min_bytes, underruns, _pos = conn.recv()
    per = args.period_ms
    return {"mode": mode, "wall": wall, "motion": job.duration_s / args.scale,
            "min_fill_ms": None if min_bytes is None else min_bytes / ITEM_BYTES * per,
            "pc_min_ms": buf.get("min_buffer_ms"), "trips": buf.get("watchdog_trips", 0),
            "restarts": buf.get("restarts", 0), "underruns": max(0, underruns - 1),   # the end of the job drains it once
            "capacity_ms": buf.get("capacity", 0) / ITEM_BYTES * per,
            "prefill_ms": buf.get("prefill_ms", 0.0)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mode", choices=("thread", "process", "both"), default="both")
    ap.add_argument("--load", type=int, default=2, help="CPU-busy threads in the caller process")
    ap.add_argument("--seconds", type=float, default=20.0, help="job length in card time")
    ap.add_argument("--scale", type=float, default=1.0, help="simulator time scale (1 = real time)")
    ap.add_argument("--period-ms", type=float, default=1.0)
    ap.add_argument("--capacity", type=int, default=64 * 1024, help="simulated FIFO bytes")
    ap.add_argument("--watchdog-ms", type=float, default=40.0)
    args = ap.parse_args(argv)
    ctx = mp.get_context("spawn")
    a, b = ctx.Pipe()
    sp = ctx.Process(target=_sim_proc, args=(b, args.capacity, args.period_ms, args.scale), daemon=True)
    sp.start()
    args.port = a.recv()
    modes = ("thread", "process") if args.mode == "both" else (args.mode,)
    print(f"job {args.seconds:.0f} s at {args.period_ms} ms/step, sim x{args.scale}, FIFO "
          f"{args.capacity} B, {args.load} load threads")
    try:
        for m in modes:
            r = run(m, args, a)
            fmt = lambda v: "n/a" if v is None else f"{v:.0f} ms"  # noqa: E731
            print(f"{m:8} min fill {fmt(r['min_fill_ms'])} (pc view {fmt(r['pc_min_ms'])}, capacity "
                  f"{r['capacity_ms']:.0f} ms, prefill {r['prefill_ms']:.0f} ms); watchdog trips "
                  f"{r['trips']}, restarts {r['restarts']}, sim underruns {r['underruns']}; "
                  f"wall {r['wall']:.1f} s for {r['motion']:.1f} s of motion")
    finally:
        a.send("quit")
        sp.join(3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
