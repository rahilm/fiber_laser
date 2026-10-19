"""Streaming: prefill, buffer cap, telemetry, watchdog fault, separate-process streamer."""
from __future__ import annotations

import pytest

from mlaser.protocol import frames as F
from mlaser.controller.client import MotionController, TransportConfig
from mlaser.controller.simulator import CardSimulator
from mlaser.controller.stream import JobStreamer, ProcessStreamer, StreamFault, WireJob, wire_timing

ARMED = {"armed": True}        # policy of a laser job started armed (normally from the supervisor)


def _job(n=3000, dx=20):
    return WireJob([F.encode_step(dx, 0, 5000, 100, 5000) for _ in range(n)], period_ms=1.0)


def test_wire_timing():
    cb, ct = wire_timing([F.encode_step(1, 0), F.sys_item(F.SYS_OUTPUTS, 1, 1), F.encode_step(1, 0)], 0.5)
    assert cb == [12, 28, 40] and ct == [0.5, 0.5, 1.0]


def test_prefill_and_buffer_stats():
    sim = CardSimulator(fifo_capacity=24_000, time_scale=4.0).start()
    try:
        with MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port)) as mc:
            js = JobStreamer(mc)
            js.run_job(_job(), timeout_s=30)
            assert sim.wait_idle(5) and sim.position(0) == 3000 * 20
            b = js.buf
            assert b.capacity == 24_000
            # prefilled up to the motion cap (1.2 s by default), never the whole FIFO
            assert 1000 < b.prefill_ms <= 1200 + 120
            assert b.peak_buffer_ms <= 1200 + 120
            assert b.min_buffer_ms is not None and b.min_buffer_ms > 0
            assert b.watchdog_trips == 0 and b.restarts == 0
    finally:
        sim.stop()


def test_prefill_target():
    sim = CardSimulator(time_scale=4.0).start()
    try:
        with MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port)) as mc:
            js = JobStreamer(mc, prefill_ms=200)
            js.run_job(_job(1000), timeout_s=30)
            assert 200 <= js.buf.prefill_ms < 2000
    finally:
        sim.stop()


def test_watchdog_trip_is_a_fault_without_restart():
    sim = CardSimulator(fifo_capacity=8_000, time_scale=4.0).start()
    try:
        with MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port)) as mc:
            events = []
            # an impossible threshold forces a trip as soon as the job runs
            js = JobStreamer(mc, watchdog_ms=5000)
            js.on_event = lambda *e: events.append(e[0])
            with pytest.raises(StreamFault) as ei:
                js.run_job(_job(2000), timeout_s=60)
            assert ei.value.kind == "underrun" and ei.value.exec_index >= 0
            assert js.buf.watchdog_trips == 1 and "underrun" in events and js.buf.restarts == 0
            assert [9999, 3, 5000, 0, 0] in sim.state.commands     # PWM off on the trip
            assert not sim.state.fifo_running                      # stopped, never restarted
    finally:
        sim.stop()


def test_unexpected_idle_is_a_fault_not_a_restart():
    """A FIFO stop the streamer did not issue (another client, the card) used to be overridden
    by a FIFO start; now the streamer stops with a fault and leaves the card stopped."""
    import threading
    import time
    sim = CardSimulator(fifo_capacity=24_000, time_scale=1.0).start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port)
        with MotionController(config=cfg) as mc, MotionController(config=cfg) as other:
            js = JobStreamer(mc)
            err = []
            t = threading.Thread(target=lambda: err.append(_run(js, _job(6000, 10))))
            t.start()
            time.sleep(0.8)
            other.fifo_stop()
            t.join(10)
            assert err and isinstance(err[0], StreamFault) and err[0].kind == "unexpected_idle"
            x = sim.position(0)
            time.sleep(0.3)
            assert sim.position(0) == x and not sim.state.fifo_running and js.buf.restarts == 0
    finally:
        sim.stop()


def _run(js, job):
    try:
        js.run_job(job, timeout_s=60)
    except Exception as e:  # noqa: BLE001
        return e
    return None


def test_process_streamer_runs_job():
    sim = CardSimulator(time_scale=4.0).start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port)
        ps = ProcessStreamer(cfg, 1000.0, policy=ARMED)
        seen = []
        stats = ps.run_job(_job(1500), progress=lambda f, i: seen.append(f), timeout_s=60)
        assert stats.items == 1500 and stats.frames > 1
        assert seen and seen[-1] == pytest.approx(1.0)
        assert sim.wait_idle(5) and sim.position(0) == 1500 * 20
        assert ps.buf["capacity"] == 64 * 1024
        assert not ps.proc.is_alive()
    finally:
        sim.stop()


def test_process_streamer_cancel():
    import threading
    import time
    sim = CardSimulator(time_scale=1.0).start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port)
        ps = ProcessStreamer(cfg, 1000.0, policy=ARMED)
        cancel = threading.Event()
        threading.Timer(1.5, cancel.set).start()
        t0 = time.monotonic()
        ps.run_job(_job(20_000), cancel=cancel, timeout_s=60)
        assert time.monotonic() - t0 < 10
        assert ps.exec_index < 19_999
    finally:
        sim.stop()


def test_child_treats_a_refusal_after_the_cancel_as_the_cancel(monkeypatch):
    """The parent's cancel flag can reach the child's guard between the streamer's own cancel
    check and its next FIFO write: the refused write is the cancel, reported as a cancelled
    "done", not as an error (macOS runner: test_process_streamer_cancel raised code 9101)."""
    import multiprocessing as mp
    import queue
    from dataclasses import asdict

    from mlaser.controller import stream as S
    from mlaser.controller.supervisor import CommandRefused
    from mlaser.machine.safety import Cmd

    flags = {"hold": mp.Value("b", 0), "cancel": mp.Value("b", 0), "hb": mp.Value("q", 0),
             "halted": mp.Value("b", 0)}

    def run_job(self, job, *a, **kw):
        flags["cancel"].value = 1                  # the cancel arrives ...
        raise CommandRefused("the job was cancelled or halted", Cmd.MOTION)   # ... at the write

    monkeypatch.setattr(S.JobStreamer, "run_job", run_job)
    sim = CardSimulator(time_scale=1.0).start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port)
        evt_q = queue.Queue()
        S._child_main(asdict(cfg), 1000.0, _job(10), {}, queue.Queue(), evt_q, flags,
                      {**ARMED, "parent_timeout_s": 60.0})
        events = []
        while not evt_q.empty():
            events.append(evt_q.get())
        assert events and events[-1][0] == "done" and events[-1][4] is True, events
        assert not any(e[0] in ("error", "fault") for e in events), events
    finally:
        sim.stop()


def test_simulator_wait_and_follower_model():
    import time
    sim = CardSimulator(time_scale=1.0).start()
    try:
        with MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port)) as mc:
            items = [F.fifo_item(104, 2000, 1000), F.wait_zf(3, 500), F.wait_ms(300),
                     F.encode_step(10, 0), F.encode_wait(F.WAIT_INPUT, b=2, c=1, timeout_ms=200),
                     F.encode_step(10, 0)]
            mc.fifo_clear()
            for fr in F.build_fifo_frames(items, mc.read_status().fifo_seq + 1):
                mc.fifo_write_frame(fr)
            t0 = time.monotonic()
            mc.fifo_start()
            time.sleep(0.15)
            assert sim.position(0) == 0                     # held by the 300 ms delay
            assert sim.state.zf_state == 3                  # follow-on reached, zf wait passed
            end = time.monotonic() + 3
            while sim.state.fifo_running and time.monotonic() < end:
                time.sleep(0.01)
            assert not sim.state.fifo_running               # the timeout stopped the program
            assert time.monotonic() - t0 >= 0.45            # 300 ms delay + 200 ms input timeout
            assert sim.position(0) == 10                    # second step never ran
            assert mc.read_status().alarm_word2 & (1 << 4)  # wait timeout -> alarm
    finally:
        sim.stop()


def test_streaming_freezes_the_preexisting_heap():
    """While a job streams, the objects that existed before it are frozen (gc.freeze), so a
    full collection cannot stop the streaming thread for the whole heap; afterwards the freeze
    is undone (when the streamer made it)."""
    import gc
    from mlaser.controller.stream import quiet_gc
    gc.unfreeze()
    seen = []
    sim = CardSimulator(time_scale=4.0).start()
    try:
        with MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port)) as mc:
            JobStreamer(mc).run_job(_job(300), progress=lambda f, i: seen.append(gc.get_freeze_count()))
    finally:
        sim.stop()
    assert seen and min(seen) > 0
    assert gc.get_freeze_count() == 0
    gc.freeze()                                  # frozen by someone else: left frozen
    n = gc.get_freeze_count()
    with quiet_gc():
        pass
    assert gc.get_freeze_count() >= n > 0
