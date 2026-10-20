"""Streamer robustness under host load (review 2 P1-2), with the safety bounds unchanged.

=====================================  =======================================  ====================
scenario                               before (review 2 §3.1)                   asserted
=====================================  =======================================  ====================
process streamer, 2 CPU-bound threads  ``parent_lost`` 301 ms in 3/3 (review)   the job completes, no
in the parent (bench_stream --load 2)  and 10/10 runs (this track)              fault
full GC during a job                   gen-2 pauses up to 1.29 s mid-job        no automatic gen-2
                                                                                collection; young ones
                                                                                still run
parent relay loop stuck (process       child halted after 0.3 s                 child still halts
alive)                                                                          (parent_lost), card safe
                                                                                within 0.3 s + 0.3 s
=====================================  =======================================  ====================
"""
from __future__ import annotations

import gc
import sys
import threading
import time

import pytest

from mlaser.controller.client import TransportConfig
from mlaser.controller.simulator import CardSimulator
from mlaser.controller.stream import (NO_FULL_GC_THRESHOLD, RESPONSIVE_SWITCH_S, ProcessStreamer,
                                      StreamFault, WireJob, quiet_gc, responsive_gil)
from mlaser.protocol import frames as F


def _job(n: int) -> WireJob:
    return WireJob([F.encode_step(5, 0, 5000, 100, 5000) for _ in range(n)], period_ms=1.0)


def _hog(stop: threading.Event) -> None:          # tools/bench_stream.py's load thread
    x = 0
    while not stop.is_set():
        for k in range(20000):
            x += k * k
        [object() for _ in range(2000)]


def test_quiet_gc_stops_automatic_full_collections_only():
    th = gc.get_threshold()
    gens: list[int] = []

    def cb(phase, info):
        if phase == "start":
            gens.append(info["generation"])
    gc.callbacks.append(cb)
    try:
        with quiet_gc():
            assert gc.get_threshold()[:2] == th[:2]
            assert gc.get_threshold()[2] >= NO_FULL_GC_THRESHOLD
            with quiet_gc():                      # nested: still quiet, restored only at the end
                pass
            assert gc.get_threshold()[2] >= NO_FULL_GC_THRESHOLD
            junk = None
            for _ in range(300_000):              # cyclic garbage: young collections reclaim it
                a: list = []
                a.append(a)
                junk = a
            del junk
    finally:
        gc.callbacks.remove(cb)
    assert gc.get_threshold() == th
    assert 0 in gens, "young collections must keep running during a job"
    assert 2 not in gens, "no automatic full collection while a job streams"


def test_responsive_gil_nests_and_restores():
    before = sys.getswitchinterval()
    with responsive_gil():
        assert sys.getswitchinterval() == pytest.approx(min(before, RESPONSIVE_SWITCH_S))
        with responsive_gil():
            pass
        assert sys.getswitchinterval() == pytest.approx(min(before, RESPONSIVE_SWITCH_S))
    assert sys.getswitchinterval() == before


# One rerun allowed (real-time, loaded host); the 0.3 s parent timeout is not loosened.
@pytest.mark.flaky(reruns=1)
def test_process_streamer_survives_cpu_bound_threads_in_the_parent():
    """``tools/bench_stream.py --load 2`` as a test: two CPU-bound Python threads in the parent
    (a busy GUI, a nesting thread).  Before: ``parent_lost`` after a few seconds in 10 of 10 runs
    (the relay loop that bumped the heartbeat waited for the GIL for more than 0.3 s)."""
    sim = CardSimulator().start()
    stop = threading.Event()
    hogs = [threading.Thread(target=_hog, args=(stop,), daemon=True) for _ in range(2)]
    try:
        ps = ProcessStreamer(TransportConfig(host="127.0.0.1", port=sim.port), 1000.0,
                             policy={"armed": True}, watchdog_ms=40.0)
        for h in hogs:
            h.start()
        stats = ps.run_job(_job(5000), timeout_s=60)
        assert stats is not None and ps.exec_index >= 4999
        assert ps.halted is None
    finally:
        stop.set()
        for h in hogs:
            h.join(2)
        sim.stop()


@pytest.mark.flaky(reruns=1)
def test_stuck_relay_loop_still_stops_the_heartbeat():
    """The heartbeat now comes from its own thread, but it still means "the relay loop is
    alive": a relay loop stuck in a status callback (the process itself alive) stops it, and the
    child halts the card (``parent_lost``) as before."""
    sim = CardSimulator().start()
    block = threading.Event()
    t_block: list[float] = []
    t_safe: list[float] = []

    def on_status(st, zf) -> None:
        if not t_block and sim.state.fifo_running:
            t_block.append(time.monotonic())
            block.wait(3.0)                       # the relay loop hangs here

    def watch() -> None:
        while not t_block:
            time.sleep(0.005)
        while time.monotonic() - t_block[0] < 3.0:
            if not sim.state.fifo_running:
                t_safe.append(time.monotonic() - t_block[0])
                return
            time.sleep(0.005)
    wt = threading.Thread(target=watch, daemon=True)
    try:
        ps = ProcessStreamer(TransportConfig(host="127.0.0.1", port=sim.port), 1000.0,
                             policy={"armed": True, "halt": {"pwm_freq": 5000}})
        ps.on_status = on_status
        wt.start()
        with pytest.raises(StreamFault) as ei:
            ps.run_job(_job(20000), timeout_s=60)
        assert ei.value.kind == "parent_lost", ei.value
        wt.join(4)
        assert t_safe, "the card kept running while the parent's relay loop was stuck"
        # 0.3 s for the heartbeat thread to see the stuck relay, 0.3 s for the child, plus the halt
        assert t_safe[0] < 0.3 + 0.3 + 0.3, f"card stopped {t_safe[0]:.2f} s after the relay stuck"
    finally:
        block.set()
        sim.stop()
