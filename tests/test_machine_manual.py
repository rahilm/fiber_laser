"""Manual machine operations against the simulator, wire-exact as the original MainApp sends them
(docs/parity.md section 7, D-20..D-32; evidence in docs/protocol.md section 4 and 11-17).

Every test captures the requests the controller sends (like parity/compare/cmp_machine.py) and
checks the mailbox / register writes value by value.  Card scale: 1000 counts/mm (the shipped
install's cfg +0xbc28)."""
from __future__ import annotations

import threading
import time

import pytest

from mlaser.protocol import frames as F
from mlaser.controller.coords import MANUAL
from mlaser.controller.machine_ops import CO2, MachineOps
from mlaser.controller.simulator import CardSimulator
from mlaser.controller.ui_adapter import UiController
from mlaser.nc.machine import Machine

POLL = {(1000, 36), (2000, 50), (50200, 100), (50000, 26), (60001, 120)}


def _s32(v):
    return v - (1 << 32) if v >= 1 << 31 else v


class Rig:
    def __init__(self, params=None):
        self.machine = Machine()
        self.machine.params.update(params or {})
        self.sim = CardSimulator("127.0.0.1", 0, "udp", ppm=1000)
        self.log, self.lock = [], threading.Lock()
        base = self.sim.handle

        def handle(data):
            with self.lock:
                self.log.append((time.time(), data))
            return base(data)
        self.sim.handle = handle
        self.sim.start()
        self.ctl = UiController(machine=self.machine, stream_process=False, poll_s=0.02)
        self.ctl.sim = self.sim
        self.ctl._log = lambda m, *a: None
        self.ctl.require_homed = False      # the simulator starts unhomed (pre-flight)
        assert self.ctl.connect("127.0.0.1", self.sim.port)
        self.ops = MachineOps(self.ctl, values=dict(self.machine.params))

    def at(self, x_counts, y_counts):
        with self.sim.lock:
            for a, v in enumerate((x_counts, y_counts)):
                ax = self.sim.state.axes[a]
                ax.pos, ax.target, ax.vel, ax.mode = float(v), None, 0.0, 0
        time.sleep(0.15)

    def run(self, fn):
        t0 = time.time()
        fn()
        time.sleep(0.1)
        with self.lock:
            reqs = [F.decode_request(d) for t, d in self.log if t >= t0]
        return [(q.addr, [_s32(v) for v in q.values]) for q in reqs
                if q.func == 0x40 or (q.addr, q.count) not in POLL and q.addr != 59500]

    def close(self):
        self.ctl.disconnect(quiet=True)
        self.sim.stop()


@pytest.fixture
def rig():
    r = Rig()
    yield r
    r.close()


def _writes(reqs):
    return [r for r in reqs if r[1]]


# ------------------------------------------------------------------ D-20 homed bit
def test_homed_bit_15_in_status_and_client(rig):
    st = rig.ctl.mc.read_status()
    assert not st.axes[0].homed and st.homed_mask() == 0
    rig.ctl.home("XY")
    assert rig.sim.wait_idle(5)
    time.sleep(0.15)
    st = rig.ctl.mc.read_status()
    assert st.axes[0].status & 0x8000 and st.axes[1].homed and st.homed_mask() & 3 == 3
    assert rig.ctl.state().homed


# ------------------------------------------------------------------ D-21..D-23 jog
def test_jog_x_is_axis_number_0_and_moves_x_only(rig):
    """Safety: the X jog addresses card axis 0 (the old mask 1 << 0 = 1 meant the original's Y)."""
    rig.at(0, 0)
    reqs = rig.run(lambda: (rig.ctl.jog_start("X", 1), time.sleep(0.2), rig.ctl.jog_stop("X")))
    # MC.JogSlowSpeed 50 mm/s * ppm, acc ftol(ftol(MC.XFastMoveAcc 4000) * 0.1), stroke 1000 mm
    assert _writes(reqs) == [(101, [3, 0, 50000, 400, 4000, 1000000]), (101, [1, 31, 2, 4000, 200000]),
                             (103, [3]), (103, [1])]
    assert rig.sim.state.axes[0].pos > 0 and rig.sim.state.axes[1].pos == 0


def test_jog_y_minus_and_fast(rig):
    rig.at(0, 500000)
    reqs = rig.run(lambda: (rig.ctl.jog_start("Y", -1), time.sleep(0.1), rig.ctl.jog_stop("Y")))
    assert _writes(reqs)[0] == (101, [3, 1, 50000, 400, 4000, -1000000])
    assert rig.sim.state.axes[1].pos < 500000 and rig.sim.state.axes[0].pos == 0
    reqs = rig.run(lambda: (rig.ctl.jog_start("X", 1, fast=True), rig.ctl.jog_stop("X")))
    assert _writes(reqs)[0] == (101, [3, 0, 200000, 4000, 40000, 1000000])   # MC.JogFastSpeed, acc unscaled


def test_step_jog_and_parameters():
    r = Rig({"MC.JogSlowSpeed": 30.0, "MC.XFastMoveAcc": 2500.0, "MAC_1.SoftLimitMaxLen": 1500.0})
    r.machine.jog_speed, r.machine.move_acc = 30.0, 2500.0       # what Machine.from_params reads
    try:
        reqs = r.run(lambda: r.ctl.jog_step("Y", -1, 2.5))
        assert _writes(reqs) == [(101, [3, 1, 30000, 250, 2500, -2500])]      # no stop on release
        reqs = r.run(lambda: (r.ctl.jog_start("Y", 1), r.ctl.jog_stop("Y")))
        assert _writes(reqs)[0] == (101, [3, 1, 30000, 250, 2500, 1500000])   # MAC_1.SoftLimitMaxLen
    finally:
        r.close()


def test_machine_from_params_reads_manual_motion_keys():
    from mlaser.formats.params import MachineParams
    mp = MachineParams.defaults()
    m = Machine.from_params(mp)
    assert (m.jog_speed, m.jog_fast_speed, m.move_speed, m.move_acc) == (50.0, 200.0, 100.0, 4000.0)
    assert (m.jog_stop_factor, m.max_acc, m.pt_laser_freq) == (1, 20000.0, 1234)


# ------------------------------------------------------------------ D-24 mark, D-25 回零位, D-30
def test_mark_and_return_truncates_like_ftol(rig):
    rig.at(199407, 930)
    assert rig.run(lambda: rig.ops.set_mark(0)) == []                  # no card traffic
    rig.at(200407, 1930)
    reqs = rig.run(lambda: rig.ops.return_mark(0))
    # (0.93 - 1.93) * 1000 = -999.9999999999999 -> ftol -999 (the original sends -999 too)
    assert _writes(reqs) == [(101, [5, 3, 100000, 4000, 40000, -1000, -999, 0, 0])]


def test_return_zero_confirms_and_moves_to_origin(rig):
    rig.at(225963, 931)
    asked = []
    assert rig.run(lambda: rig.ctl.return_zero(confirm=lambda k: asked.append(k) or False)) == []
    assert asked == ["mp79"]
    reqs = rig.run(lambda: rig.ctl.return_zero())
    assert _writes(reqs) == [(101, [5, 3, 100000, 4000, 40000, -225963, -931, 0, 0])]


def test_return_tool_origin_acc_is_unscaled(rig):
    rig.ops.select_coord(1)
    rig.at(0, 0)
    rig.ops.set_origin()
    rig.at(1000, 1000)
    reqs = rig.run(rig.ops.return_origin)
    assert _writes(reqs) == [(101, [5, 3, 100000, 4000, 40000, -1000, -1000, 0, 0])]


# ------------------------------------------------------------------ D-26 start-up, D-27 Z home
def test_startup_homing_red_light_home_statistics():
    r = Rig({"LGP.DORedLight": 4})
    try:
        reqs = r.run(r.ctl.startup_homing)
        assert reqs == [(101, [9999, 2, 8, 0]), (1050, []), (101, [2, 3, 0])]
    finally:
        r.close()


def test_z_home_without_follower_sends_nothing(rig):
    assert rig.run(lambda: rig.ctl.home("Z")) == []
    r = Rig({"ZF.ZFType": 1})
    try:
        assert _writes(r.run(lambda: r.ctl.home("Z"))) == [(101, [2, 0x10000, 0])]
    finally:
        r.close()


# ------------------------------------------------------------------ D-28 laser button
def test_laser_button_gate_closed_sends_pwm_off_pair(rig):
    assert rig.ctl.arm()                     # firing needs the machine armed (docs/SAFETY.md)
    reqs = rig.run(lambda: (rig.ctl.laser_press(), rig.ctl.laser_release()))
    assert _writes(reqs) == [(101, [9999, 3, 1234, 0, 0]), (101, [9999, 17, 1234, 0, 0])]


def test_laser_button_gate_open_fires_until_release(rig):
    assert rig.ctl.arm()
    rig.ctl.output_map["shutter"] = 5
    rig.ctl.set_output("shutter", True)
    reqs = rig.run(rig.ctl.laser_press)
    assert _writes(reqs) == [(101, [9999, 3, 1234, 50, 1])] * 2        # MC.LaserPointPower 50
    reqs = rig.run(rig.ctl.laser_release)
    assert _writes(reqs) == [(101, [9999, 3, 1234, 0, 0]), (101, [9999, 17, 1234, 0, 0])]


def test_laser_button_timed_shot_release_sends_nothing():
    r = Rig({"MC.PtLaserTime_ms": 50})
    try:
        assert r.ctl.arm()
        r.ctl.output_map["shutter"] = 5
        r.ctl.set_output("shutter", True)
        r.run(r.ctl.laser_press)
        assert r.ctl.wait_job(10)
        assert r.run(r.ctl.laser_release) == []
    finally:
        r.close()


# ------------------------------------------------------------------ D-29 stop
def test_idle_stop_is_cancel_decel_and_fifo_reset(rig):
    reqs = rig.run(rig.ctl.stop)
    assert _writes(reqs) == [(101, [101]), (101, [1, 31, 2, 4000, 200000]), (103, [3]), (103, [1])]


def test_stop_decel_follows_parameters():
    r = Rig()
    r.machine.move_acc, r.machine.jog_stop_factor = 4000.0, 3     # 12000, within FCP.MaxAcc
    try:
        assert _writes(r.run(r.ctl.stop))[1] == (101, [1, 31, 2, 12000, 200000])
        r.machine.jog_stop_factor = 0                              # below the 2000 floor
        assert _writes(r.run(r.ctl.stop))[1] == (101, [1, 31, 2, 2000, 200000])
    finally:
        r.close()


# ------------------------------------------------------------------ D-31 manual coordinates
def test_manual_coordinate_axis_words_and_soft_limits(rig):
    reqs = rig.run(lambda: rig.ops.select_coord(MANUAL))
    w0 = rig.ops.card_params().axes[0].limits_word()
    assert w0 == 131088                                            # SampleType bit 4 | +0xbc76 bit 17
    assert reqs == [(50200, [w0]), (50240, [w0]), (50280, [w0]), (50360, [w0])]   # ZF axis 3 skipped
    reqs = rig.run(rig.ops.set_origin)
    assert reqs == [(101, [9999, 8, 3]), (50202, [-500, 1000000]), (50242, [-500, 1000000])]


# ------------------------------------------------------------------ D-32 laser type
def test_laser_type_switch_sequence():
    r = Rig({"LGP.DORedLight": 4})
    try:
        reqs = r.run(lambda: r.ops.switch_laser_type(CO2))
        assert reqs == [(101, [9999, 3, 1234, 0, 0]), (101, [9999, 17, 1234, 0, 0]),
                        (101, [9999, 2, 8, 0]), (50202, [-500, 1000000]), (50242, [-500, 1000000])]
        reqs = r.run(lambda: r.ops.switch_laser_type(0))
        assert reqs == [(101, [9999, 3, 1234, 0, 0]), (101, [9999, 17, 1234, 0, 0]),
                        (50202, [-500, 1000000]), (50242, [-500, 1000000])]
    finally:
        r.close()
