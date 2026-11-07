"""The job start the real MCC100 needs (owner's captures of the original, 2026-11-07).

On the owner's machine the rebuild's first Frame was rejected with card alarm 9001 "the
interpolation data content length is abnormal": it never sent the step-format command and did
not reset the card's FIFO state.  The original's sequence, decoded from its capture:

    [9999, 16]                      job reset (frame identification back to 0)
    103 := 1                        FIFO clear
    102 := [1, items...], [2, ...]  the prefill frames, numbered from 1
    [9999, 1, 0x66800003, 0]        step format, axes X|Y
    103 := 2                        FIFO start

The simulator now rejects a FIFO start without the step format the same way."""
from __future__ import annotations

import pytest

from mlaser.controller import alarms as A
from mlaser.controller.client import MotionController, TransportConfig
from mlaser.controller.simulator import CardSimulator
from mlaser.controller.stream import JobStreamer, WireJob
from mlaser.machine import safety as S
from mlaser.protocol import frames as F

pytestmark = pytest.mark.sim


class Recording(MotionController):
    """The real client, with the job-start calls recorded in order."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls: list = []

    def fifo_job_reset(self):
        self.calls.append(("reset",))
        super().fifo_job_reset()

    def fifo_clear(self):
        self.calls.append(("clear",))
        super().fifo_clear()

    def fifo_write_frame(self, frame):
        self.calls.append(("frame", frame[0]))
        super().fifo_write_frame(frame)

    def fifo_step_format(self, axis_mask=0x3):
        self.calls.append(("format", (int(axis_mask) & 0xFFFF) | F.STEP_FORMAT_STEP_CARD))
        super().fifo_step_format(axis_mask)

    def fifo_start(self):
        self.calls.append(("start",))
        super().fifo_start()


def _job(n=2000):
    return WireJob([F.encode_step(10, 0, 5000, 0, 5000) for _ in range(n)], period_ms=1.0)


def test_job_start_matches_the_original_on_a_real_card():
    sim = CardSimulator(time_scale=4.0).start()
    try:
        sim.state.fifo_seq = 68                 # a stale frame identification from an earlier job
        with Recording(config=TransportConfig(host="127.0.0.1", port=sim.port)) as mc:
            js = JobStreamer(mc, prefill_ms=200)
            js.run_job(_job(), timeout_s=30)
            assert sim.wait_idle(5) and sim.position(0) == 2000 * 10
            c = mc.calls
            first_start = c.index(("start",))
            head = c[:first_start + 1]
            assert head[0] == ("reset",) and head[1] == ("clear",)
            frames = [x[1] for x in head if x[0] == "frame"]
            assert frames and frames[0] == 1 and frames == list(range(1, len(frames) + 1))
            assert head[-2] == ("format", 0x66800003) and head[-1] == ("start",)
            assert not sim.state.alarm_w2
    finally:
        sim.stop()


def test_simulator_rejects_a_start_without_the_step_format_like_the_card():
    sim = CardSimulator(time_scale=4.0).start()
    try:
        with MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port)) as mc:
            mc.fifo_job_reset()
            mc.fifo_clear()
            for fr in F.build_fifo_frames([F.encode_step(10, 0, 5000, 0, 5000) for _ in range(200)],
                                          mc.read_status().fifo_seq + 1):
                mc.fifo_write_frame(fr)
            mc.fifo_start()                       # no [9999, 1, mask|0x66800000, 0]
            st = mc.read_status()
            assert st.alarm_word2 & (1 << A.AlarmWord2Bit.INTERP_LENGTH)
            assert 9001 in [a.code for a in st.alarms()]          # the owner's alarm
            assert not st.running and sim.position(0) == 0      # nothing moved
    finally:
        sim.stop()


def test_the_job_reset_is_never_an_always_allowed_halt():
    assert S.classify_mailbox([F.OP_SYS, F.SYS_JOB_RESET]) == S.Cmd.MOTION
    assert S.classify_mailbox([F.OP_SYS, F.SYS_AXIS_CFG, 0x66800003, 0]) == S.Cmd.MOTION


def _status(ro19: int):
    from mlaser.controller.client import MCStatus
    st = MCStatus.__new__(MCStatus)
    st.ro = [0] * 36
    st.ro[F.RO_RUNNING] = ro19
    st.axes = []
    return st


@pytest.mark.parametrize("ro19,executing,active,state", [
    (0, False, False, 0),          # idle
    (0x100, False, False, 0),      # frames loaded, not started
    (0x301, True, True, 4),        # running (the owner's card reported 769 during the job)
    (0x001, False, True, 4),       # drained, until the FIFO stop
])
def test_processing_status_is_a_bit_field_like_the_real_card(ro19, executing, active, state):
    """The owner's second Frame was halted as "unexpected_idle" after ~1 s: the rebuild tested
    RO[19] == 1 for "running", but the card reports 0x301 while it runs.  The original reads the
    word as a char (low byte == 1, FUN_10039500)."""
    st = _status(ro19)
    assert st.running is executing and st.program_active is active and st.machine_state() == state


def test_a_job_runs_to_the_end_and_is_stopped_like_the_original():
    sim = CardSimulator(time_scale=4.0).start()
    try:
        with Recording(config=TransportConfig(host="127.0.0.1", port=sim.port)) as mc:
            seen = []
            js = JobStreamer(mc, prefill_ms=200)
            js.on_status = lambda st: seen.append(st.ro[F.RO_RUNNING])
            js.run_job(_job(3000), timeout_s=30)           # no StreamFault on a bit-field status
            assert sim.position(0) == 3000 * 10
            assert 0x301 in seen                           # the card's running pattern was seen
            assert mc.read_status().ro[F.RO_RUNNING] == 0   # 103 := 3 sent at the end
            assert not mc.read_status().program_active
    finally:
        sim.stop()
