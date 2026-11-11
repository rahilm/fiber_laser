"""The streaming process's start-up (a new interpreter on Windows and macOS: imports, the job's
hand-off and preparation, its sockets) is not link silence.  Until the child's hello the card
has nothing of the job and the parent keeps polling it (watched as when idle); the child says
hello just before its first request, and from then on the streaming deadline counts.  A child
that never says hello faults at the start-up bound.  (CI, Windows: "link lost: no reply from
the card for 563 ms (streaming, deadline 500 ms)" right after the job start,
test_hold_never_latches_a_fault[True].)  ``MLASER_TEST_STREAM_START_DELAY_S`` delays the
child's start-up (mlaser.controller.stream._startup_delay)."""
from __future__ import annotations

import time

import pytest

from mlaser.controller import stream as S
from mlaser.controller.client import TransportConfig
from mlaser.controller.simulator import CardSimulator
from mlaser.machine.safety import SafetyState
from mlaser.protocol import frames as F

from safety_helpers import rect_doc, start_and_run, wait_until

DELAY_ENV = "MLASER_TEST_STREAM_START_DELAY_S"


def test_a_slow_child_start_is_not_a_lost_link(make_ctl, monkeypatch):
    monkeypatch.setenv(DELAY_ENV, "1.0")         # twice the 0.5 s streaming deadline
    ctl = make_ctl(process=True)
    start_and_run(ctl, run_s=0.3)
    assert not ctl.fsm.faulted, ctl.safety().fault
    ctl.pause()
    assert wait_until(lambda: ctl.breakpoint is not None, 5) is not None
    assert not ctl.fsm.faulted, ctl.safety().fault
    assert ctl.stop().wait(10)
    assert not ctl.fsm.faulted, ctl.safety().fault


def test_a_dead_link_during_the_child_start_is_noticed(make_ctl, monkeypatch):
    monkeypatch.setenv(DELAY_ENV, "6.0")         # the child is still starting throughout
    ctl = make_ctl(process=True)
    ctl.start(rect_doc())
    assert wait_until(lambda: ctl._sup.streaming == "process", 5) is not None
    time.sleep(0.2)
    assert not ctl.fsm.faulted, ctl.safety().fault
    sim = ctl.sim
    sim.link_down(True)
    try:
        # the parent's own polling sees it: the idle deadline (1.5 s) and a poll period or two,
        # long before the child would say hello
        t = wait_until(lambda: ctl.fsm.state == SafetyState.FAULT, 3.0)
        assert t is not None, "a dead link during the child's start-up went unnoticed"
        assert "link lost" in ctl.fsm.fault
    finally:
        sim.link_down(False)
        ctl.stop().wait(15)


def test_a_child_that_never_says_hello_faults_at_the_start_up_bound(monkeypatch):
    monkeypatch.setenv(DELAY_ENV, "30")
    sim = CardSimulator(time_scale=1.0).start()
    try:
        ps = S.ProcessStreamer(TransportConfig(host="127.0.0.1", port=sim.port), 1000.0,
                               policy={"armed": True}, startup_s=1.5)
        job = S.WireJob([F.encode_step(20, 0, 5000, 100, 5000) for _ in range(50)], period_ms=1.0)
        t0 = time.monotonic()
        with pytest.raises(S.StreamFault) as ei:
            ps.run_job(job, timeout_s=60)
        assert ei.value.kind == "child_stalled" and "did not start" in str(ei.value)
        assert time.monotonic() - t0 < 1.5 + 6.0       # the bound, plus a spawn's join
        if ps.proc is not None and ps.proc.is_alive():
            ps.kill()
    finally:
        sim.stop()
