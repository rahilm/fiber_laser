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


def test_frame_speed_comes_from_the_machine_setting():
    """MC.BoundSpeed (pd528): the owner's original framed at its 133.3 mm/s setting while the
    rebuild always used 100 mm/s."""
    from mlaser.formats.params import MachineParams, param_defs
    from mlaser.nc.machine import Machine
    mp = MachineParams.defaults()
    assert Machine.from_params(mp).frame_speed == 100.0                 # the parameter's default
    d = next(x for x in param_defs() if x.key == "MC.BoundSpeed")
    st = mp.hard if d.file == "HardPara.xml" else mp.manu
    st.set(d, 133.333)
    assert abs(Machine.from_params(mp).frame_speed - 133.333) < 1e-3


def test_frame_raises_the_process_signal_and_ends_like_the_original():
    """Owner's capture of the original's Frame: output 'process signal' (DO.ManuSignal) on while
    the job runs, PWM off with the configured frequency after the FIFO stop, the signal off 0.2 s
    after the end."""
    import time
    from mlaser import ops
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.02, output_map={"process": 1, "red_light": 6})
    assert ctl.connect_simulator(time_scale=4.0)
    sim = ctl.sim
    try:
        n0 = len(sim.state.commands)
        seen_on = []
        ctl.frame(ops.demo_document())
        t = time.monotonic()
        while time.monotonic() - t < 30:
            seen_on.append(bool(sim.state.outputs & 1))
            if ctl.wait_job(0.05):
                break
        cmds = sim.state.commands[n0:]
        # the job reset leads the FIFO sequence (the process signal may come first: the rebuild
        # switches it just before the job, the original ~60 ms after the FIFO start)
        assert [c for c in cmds if c[1] != F.SYS_OUTPUTS][0] == [F.OP_SYS, F.SYS_JOB_RESET]
        assert [F.OP_SYS, F.SYS_AXIS_CFG, 0x66800003, 0] in cmds
        assert any(seen_on)                                   # on while the frame ran
        pwm = [c for c in cmds if c[:2] in ([F.OP_SYS, F.SYS_PWM], [F.OP_SYS, F.SYS_PWM_BUS])]
        assert pwm and all(c[3] == 0 for c in pwm)             # duty 0 at the end
        assert not sim.state.outputs & 1                       # process signal off afterwards
    finally:
        ctl.disconnect()


def _steps(job):
    out = []
    for it in job.wire:
        if (it[0] & 0xFFFF) == F.OP_STEP:
            dx, dy = it[1] & 0xFF, (it[1] >> 8) & 0xFF
            out.append((dx - 256 if dx > 127 else dx, dy - 256 if dy > 127 else dy))
    return out


def test_frame_stops_at_every_corner_with_the_original_s_curve():
    """Owner's capture of the original's Frame (221.8 x 158.6 mm, MC.BoundSpeed 133.3 mm/s,
    MC.XFastMoveAcc 6000, MC.EmptyMoveAccTime 250 ms, period 0.25 ms): four single-axis runs,
    a stop at each corner, 24 512 steps; the rebuild matches it to 1 pulse.  It used to plan the
    rectangle as a cut with rounded corners at the default layer's acceleration."""
    import dataclasses
    from mlaser.nc import compile_frame
    from mlaser.nc.machine import Machine
    m = dataclasses.replace(Machine(), pulses_per_mm_x=8000 / 31.079, pulses_per_mm_y=257.57761135402114,
                            period_ms=0.25, frame_speed=400 / 3, move_acc=6000.0, frame_acc_time=0.25)
    assert m.frame_machine().rapid_jerk == pytest.approx(48000.0)
    st = _steps(compile_frame((0, 0, 57092 / m.pulses_per_mm_x, 40864 / m.pulses_per_mm_y), m))
    assert len(st) == 24512
    assert not any(dx and dy for dx, dy in st)                  # never both axes: sharp corners
    runs, cur = [], None
    for dx, dy in st:
        d = (dx > 0) - (dx < 0), (dy > 0) - (dy < 0)
        if d != (0, 0) and d != cur:
            runs.append(d)
            cur = d
    assert runs == [(1, 0), (0, 1), (-1, 0), (0, -1)]
    assert abs(sum(dx for dx, _ in st)) <= 1 and abs(sum(dy for _, dy in st) - 1) <= 1


def test_the_gas_pressure_da_goes_to_zero_after_a_job():
    """Owner's capture: [9999, 4, 1, 0] (DA port 2 = MGP.RatioO2's proportional valve) right
    after the end-of-job FIFO stop.  A zero DA is a halt-class command."""
    import dataclasses
    from mlaser import ops
    from mlaser.controller.ui_adapter import UiController
    from mlaser.machine.safety import Cmd, classify_mailbox
    from mlaser.nc.machine import Machine
    assert classify_mailbox(F.mailbox_sys(F.SYS_DA, 1, 0)) == Cmd.HALT
    m = Machine()
    m.process = dataclasses.replace(m.process, ratio_da=(0, 2, 0))
    ctl = UiController(machine=m, poll_s=0.02)
    assert ctl.connect_simulator(time_scale=4.0)
    try:
        n0 = len(ctl.sim.state.commands)
        ctl.frame(ops.demo_document())
        assert ctl.wait_job(30)
        assert [F.OP_SYS, F.SYS_DA, 1, 0] in ctl.sim.state.commands[n0:]
    finally:
        ctl.disconnect()
