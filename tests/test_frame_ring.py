"""The bounded in-memory ring of recent card frames (review A8 item 2): what it holds, its bound,
that it outlives a disconnect, and its cost on the status tick."""
from __future__ import annotations

import time
from collections import deque

import pytest

from mlaser.controller.client import FRAME_RING, ControllerError, MotionController, TransportConfig
from mlaser.controller.simulator import CardSimulator
from mlaser.controller.supervisor import SafetySupervisor
from mlaser.machine.safety import SafetyFSM
from mlaser.protocol import frames as F

RO = F.REG_STATUS_RO

pytestmark = pytest.mark.sim


@pytest.fixture
def sim():
    s = CardSimulator().start()
    yield s
    s.stop()


def test_each_exchange_is_recorded_after_it_ended(sim):
    mc = MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=300))
    try:
        mc.read_regs(RO, 2)
        mc.read_regs(RO + 4, 1)
    finally:
        mc.close()
    assert len(mc.recent) == 2
    t, func, addr, tx, rx, outcome = mc.recent[-1]
    assert addr == RO + 4 and outcome == "ok" and isinstance(tx, bytes) and isinstance(rx, bytes)
    assert abs(t - time.time()) < 60


def test_no_reply_is_recorded_too():
    # nothing listens on this port: one send, one wait (link_lost), then the record
    mc = MotionController(config=TransportConfig(host="127.0.0.1", port=9, timeout_ms=20))
    mc.link_lost = True
    try:
        with pytest.raises(ControllerError):
            mc.read_regs(0, 1)
    finally:
        mc.close()
    assert mc.recent[-1][4] is None and mc.recent[-1][5].startswith("no reply")


def test_ring_is_bounded(sim):
    mc = MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=300))
    try:
        for _ in range(FRAME_RING + 20):
            mc.read_regs(RO, 1)
    finally:
        mc.close()
    assert len(mc.recent) == FRAME_RING == mc.recent.maxlen


def test_supervisor_frames_outlive_the_disconnect(sim):
    sup = SafetySupervisor(SafetyFSM())
    mc = sup.open(TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=300), 1000.0)
    mc.read_regs(RO, 2)
    sup.close()
    frames = sup.recent_frames()
    assert frames and frames[-1][0] == "main" and frames[-1][3] == RO
    mc2 = sup.open(TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=300), 1000.0)
    mc2.read_regs(RO + 4, 1)
    sup.close()
    got = sup.recent_frames()
    assert [f[3] for f in got] == [RO, RO + 4]                  # oldest first, both connections
    assert all(got[i][1] <= got[i + 1][1] for i in range(len(got) - 1))


def test_ring_cost_per_exchange_is_negligible():
    """The append the status tick pays per request: one tuple and an O(1) bounded append (no
    lock, no file).  Measured here against the tick's own budget: a status read is a UDP round
    trip of 100 µs or more, the poll period is 50 ms."""
    ring: deque = deque(maxlen=FRAME_RING)
    tx, rx = b"\0" * 12, b"\0" * 64
    n = 20000
    t0 = time.perf_counter()
    for _ in range(n):
        ring.append((time.time(), 0x30, 0, tx, rx, "ok"))
    per = (time.perf_counter() - t0) / n
    print(f"frame ring append: {per * 1e9:.0f} ns")
    assert per < 20e-6
