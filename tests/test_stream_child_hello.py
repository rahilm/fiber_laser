"""The streaming child prepares the job (timing, frames) BEFORE its hello.  The parent's stall
and link watch (``ProcessStreamer.stall_s``, ``child_deadline_ms``: 0.5 s) count from the
hello; the start-up before it is bounded by ``startup_s``.  A long job prepared after the hello
on a loaded runner left the parent without a message for 703 ms and latched "link lost" before
the card had any of the job (CI, Windows: test_hold_never_latches_a_fault[True], "job did not
start")."""
from __future__ import annotations

import multiprocessing as mp
import queue
from dataclasses import asdict

from mlaser.controller import stream as S
from mlaser.controller.client import TransportConfig
from mlaser.controller.simulator import CardSimulator
from mlaser.protocol import frames as F


def test_child_prepares_before_its_hello(monkeypatch):
    evt_q: queue.Queue = queue.Queue()
    order = []
    real_prepare = S.JobStreamer.prepare

    def prepare(self, job, cancel=None):
        order.append("prepare")
        real_prepare(self, job, cancel)
    real_put = evt_q.put

    def put(ev, *a, **kw):
        if ev[0] == "hello":
            order.append("hello")
        real_put(ev, *a, **kw)
    monkeypatch.setattr(S.JobStreamer, "prepare", prepare)
    monkeypatch.setattr(evt_q, "put", put)
    job = S.WireJob([F.encode_step(20, 0, 5000, 100, 5000) for _ in range(20)], period_ms=1.0)
    sim = CardSimulator(time_scale=1.0).start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port)
        S._child_main(asdict(cfg), 1000.0, job, {}, queue.Queue(), evt_q, S._shared_flags(mp),
                      {"armed": True, "parent_timeout_s": 60.0, "proto": S.STREAM_PROTO})
        events = []
        while not evt_q.empty():
            events.append(evt_q.get())
    finally:
        sim.stop()
    assert order[:2] == ["prepare", "hello"], order
    assert order.count("prepare") == 1, order        # run_job used the prepared job
    assert events[0][0] == "hello" and events[0][1]["ok"], events[:1]
    assert events[-1][0] == "done", events[-3:]
