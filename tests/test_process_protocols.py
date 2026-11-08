"""Versioned handshakes of the child processes (UI evaluation F13): the plan process
(services/planproc.py, ``PLAN_PROTO``) and the streaming process (controller/stream.py,
``STREAM_PROTO``).  A different version is refused with a clear message and no undefined
behaviour: the plans compile in a thread; the streaming child never opens a socket and the
job faults (the caller halts the card)."""
from __future__ import annotations

import multiprocessing as mp
import queue

import pytest

from mlaser.controller import stream as S
from mlaser.controller.client import TransportConfig
from mlaser.controller.simulator import CardSimulator
from mlaser.protocol import frames as F
from mlaser.services import planproc as P

ARMED = {"armed": True}


def _job(n=50):
    return S.WireJob([F.encode_step(20, 0, 5000, 100, 5000) for _ in range(n)], period_ms=1.0)


# ------------------------------------------------------------------ plan process
def test_plan_child_answers_its_version_and_ends_on_another():
    a, b = mp.Pipe(duplex=True)
    P._child_main(b, mp.Value("q", 0, lock=False), P.PLAN_PROTO + 1)
    kind, info = a.recv()
    assert kind == "ready" and info["proto"] == P.PLAN_PROTO and "plan protocol" in info["error"]


def test_plan_process_of_another_version_is_refused(monkeypatch):
    monkeypatch.setattr(P, "PLAN_PROTO", P.PLAN_PROTO + 100)      # this side: a newer mlaser
    pp = P.PlanProcess()
    try:
        with pytest.raises(P.ProtocolMismatch, match="protocol"):
            pp.ensure_ready()
        assert pp.failures >= pp.MAX_FAILURES and not pp.alive()
    finally:
        pp.close()


def test_job_service_compiles_in_a_thread_after_a_mismatch(monkeypatch):
    from test_plan_process import demo_doc, req_for, same_plan, thread_svc

    from mlaser.nc.machine import Machine
    from mlaser.services.jobs import InlineExecutor, JobService
    monkeypatch.setattr(P, "PLAN_PROTO", P.PLAN_PROTO + 100)
    svc = JobService(executor=InlineExecutor(), debounce_s=0, process=True)
    try:
        req = req_for(demo_doc(), machine=Machine(smooth=False))
        plan = svc.plan_sync(req)
        same_plan(plan, thread_svc().plan_sync(req))
        assert svc.stats["thread_compiles"] == 1 and svc.stats["process_compiles"] == 0
        assert svc.planproc is None                       # no second try at a foreign child
    finally:
        svc.shutdown()


def test_plan_ready_handshake_is_a_dict():
    a, b = mp.Pipe(duplex=True)
    import threading
    t = threading.Thread(target=P._child_loop, args=(b, mp.Value("q", 0, lock=False)), daemon=True)
    t.start()
    kind, info = a.recv()
    a.send(("quit",))
    t.join(10)
    assert kind == "ready" and info["proto"] == P.PLAN_PROTO and info["pid"] > 0


# ------------------------------------------------------------------ streaming process
def test_stream_child_refuses_another_version_before_any_card_traffic():
    sim = CardSimulator(time_scale=1.0).start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port)
        from dataclasses import asdict
        evt_q: queue.Queue = queue.Queue()
        S._child_main(asdict(cfg), 1000.0, _job(), {}, queue.Queue(), evt_q, S._shared_flags(mp),
                      {**ARMED, "parent_timeout_s": 60.0, "proto": S.STREAM_PROTO + 1})
        events = []
        while not evt_q.empty():
            events.append(evt_q.get())
        assert len(events) == 1 and events[0][0] == "hello"
        assert events[0][1]["ok"] is False and "protocol" in events[0][1]["error"]
        assert sim.requests == 0, "the refusing child talked to the card"
    finally:
        sim.stop()


def test_process_streamer_faults_on_a_child_of_another_version(monkeypatch):
    monkeypatch.setattr(S, "STREAM_PROTO", S.STREAM_PROTO + 100)   # this side: another mlaser
    sim = CardSimulator(time_scale=1.0).start()
    try:
        ps = S.ProcessStreamer(TransportConfig(host="127.0.0.1", port=sim.port), 1000.0, policy=ARMED)
        with pytest.raises(S.StreamFault) as ei:
            ps.run_job(_job(), timeout_s=60)
        assert ei.value.kind == "protocol" and "restart mlaser" in str(ei.value)
        assert sim.requests == 0, "the job reached the card"
        assert not ps.proc.is_alive()
    finally:
        sim.stop()


@pytest.mark.parametrize("first", [("progress", 0.0, 0), ("hello", {"proto": 0, "ok": True}), ("hello", 1)])
def test_streamer_requires_the_hello_first(first):
    ps = S.ProcessStreamer(TransportConfig(), 1000.0)
    ps.flags = S._shared_flags(mp)
    with pytest.raises(S.StreamFault) as ei:
        ps._check_hello(first)
    assert ei.value.kind == "protocol" and ps.flags["cancel"].value == 1
    ps._check_hello(("hello", {"proto": S.STREAM_PROTO, "ok": True, "pid": 1}))     # accepted
