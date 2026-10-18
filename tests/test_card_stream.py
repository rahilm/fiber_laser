"""Streaming: prefill, buffer telemetry, watchdog, separate-process streamer."""
from __future__ import annotations

import pytest

from mlaser.controller import frames as F
from mlaser.controller.client import MotionController, TransportConfig
from mlaser.controller.simulator import CardSimulator
from mlaser.nc.stream import JobStreamer, ProcessStreamer, WireJob, wire_timing


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
            # prefilled until the FIFO was full: ~ (24000-2000)/12 steps of 1 ms
            assert b.prefill_ms > 1000
            assert b.min_buffer_ms is not None and b.min_buffer_ms > 0
            assert b.watchdog_trips == 0
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


def test_watchdog_holds_laser_off_and_recovers():
    sim = CardSimulator(fifo_capacity=8_000, time_scale=4.0).start()
    try:
        with MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port)) as mc:
            events = []
            # an impossible threshold forces a trip as soon as the job runs
            js = JobStreamer(mc, watchdog_ms=5000, resume_ms=100)
            js.on_event = lambda *e: events.append(e[0])
            js.run_job(_job(2000), timeout_s=60)
            assert sim.wait_idle(5)
            assert sim.position(0) == 2000 * 20          # nothing lost
            assert js.buf.watchdog_trips >= 1 and "underrun" in events and "resumed" in events
            assert [9999, 3, 5000, 0, 0] in sim.state.commands     # PWM off on the trip
    finally:
        sim.stop()


def test_process_streamer_runs_job():
    sim = CardSimulator(time_scale=4.0).start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port)
        ps = ProcessStreamer(cfg, 1000.0)
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
        ps = ProcessStreamer(cfg, 1000.0)
        cancel = threading.Event()
        threading.Timer(1.5, cancel.set).start()
        t0 = time.monotonic()
        ps.run_job(_job(20_000), cancel=cancel, timeout_s=60)
        assert time.monotonic() - t0 < 10
        assert ps.exec_index < 19_999
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
