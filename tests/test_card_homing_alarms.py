"""Homing ([2, mask, 0]), alarm words RO[6]/RO[7], alarm reset ([9999, 5, 0, 0])."""
from __future__ import annotations

import time

import pytest

from mlaser.controller import alarms as A
from mlaser.protocol import frames as F
from mlaser.controller.client import (HOME_X, HOME_XY, HOME_XY_W, HOME_XY_ZF, HOME_Y, HOME_ZF,
                                      MotionController, TransportConfig, home_mask)
from mlaser.controller.simulator import CardSimulator


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_home_masks_match_fun_10032da0():
    assert home_mask(HOME_X) == 1 and home_mask(HOME_Y) == 2 and home_mask(HOME_XY) == 3
    assert home_mask(HOME_XY_ZF) == 0x10003 and home_mask(HOME_ZF) == 0x10000
    assert home_mask(HOME_XY_W, 0, 1, 3) == 0b1011
    assert home_mask(HOME_XY, x_axis=1, y_axis=2) == 0b110


def test_decode_alarm_words():
    al = A.decode(1 << 0 | 1 << 30, 1 << 5, [1 << A.AX_HARD_POS | 1 << A.AX_SERVO])
    codes = {a.code for a in al}
    assert codes == {8100, 8104, 8030, 9005}
    by = {a.code: a for a in al}
    assert by[8100].key == "EtherCATAxisErrorInfo_0" and by[8100].text_en == "X hard positive limit alarm"
    assert by[8030].kind == "estop" and by[8030].key == "EtherCATErrorInfo_1_30"
    assert by[9005].kind == "fifo" and by[9005].text_en == "FIFO starvation"
    raised, cleared = A.diff(al[:1], al[1:])
    assert {a.code for a in raised} == codes - {al[0].code} and cleared == [al[0]]


@pytest.fixture
def rig():
    sim = CardSimulator(ppm=1000, home_speed=400.0, time_scale=4.0).start()
    mc = MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=300))
    mc.open()
    yield sim, mc
    mc.close()
    sim.stop()


def test_home_command_and_status(rig):
    sim, mc = rig
    sim.state.axes[0].pos = 50_000.0
    sim.state.axes[1].pos = -20_000.0
    assert mc.home_mode(HOME_XY) == 3
    assert sim.state.commands[-1] == [2, 3, 0]
    st = mc.read_status()
    assert st.homing() and st.machine_state() == 1
    assert _wait(lambda: not mc.read_status().homing())
    assert mc.position_mm()[:2] == (0.0, 0.0)
    assert sim.state.axes[0].homed and sim.state.axes[1].homed


def test_limit_switch_alarm_and_reset(rig):
    sim, mc = rig
    mc.jog(0, 100_000, 100_000, 1_000_000)
    time.sleep(0.05)
    sim.set_limit(0, positive=True, on=True)
    st = mc.read_status()
    assert st.alarm_word1 & 1 and not st.axes[0].busy
    al = st.alarms()
    assert any(a.code == 8100 and a.kind == "limit" for a in al)
    mc.jog(0, 1000, 1000, 1000)                     # motion is refused while the alarm is active
    assert not mc.read_status().axes[0].busy
    mc.reset_alarms()                               # switch still pressed -> alarm stays
    assert sim.state.commands[-1] == [9999, 5, 0, 0]
    assert mc.read_status().alarm_word1 & 1
    sim.set_limit(0, positive=True, on=False)
    mc.reset_alarms()
    assert mc.read_status().alarms() == []


def test_estop_and_servo_alarm(rig):
    sim, mc = rig
    sim.set_estop(True)
    sim.set_servo_alarm(1, True)
    al = mc.read_status().alarms()
    kinds = {(a.code, a.kind) for a in al}
    assert (8030, "estop") in kinds and (8100 + 32 + 4, "servo") in kinds
    sim.set_estop(False)
    sim.set_servo_alarm(1, False)
    mc.reset_alarms()
    assert mc.read_status().alarms() == []


def test_soft_limit():
    sim = CardSimulator(ppm=1000, soft_limits={0: (-1.0, 5.0)}).start()
    try:
        with MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=300)) as mc:
            mc.jog_mm(0, 200, 2000, 20)
            assert _wait(lambda: mc.read_status().alarm_word1 & 1)
            al = mc.read_status().alarms()
            assert al[0].code == 8100 + A.AX_SOFT_POS and al[0].kind == "limit"
            assert mc.position_mm()[0] < 6.0
    finally:
        sim.stop()


def test_ui_home_and_alarms():
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.02)
    seen = []
    ctl.add_alarm_listener(lambda al: seen.append([a.code for a in al]))
    logs = []
    ctl.add_log_listener(logs.append)
    try:
        assert ctl.connect_simulator(time_scale=4.0)
        ctl.sim.state.axes[0].pos = 20 * ctl.position_ppm
        ctl.home("XY")
        assert ctl.state().mode == "origin"
        assert _wait(lambda: ctl.state().homed, 5)
        assert any("homing" in m.lower() for m in logs)
        ctl.sim.set_estop(True)
        assert _wait(lambda: any(8030 in s for s in seen), 3)
        assert any("Emergency stop" in t for t, _ in ctl.state().alarms)
        ctl.sim.set_estop(False)
        ctl.clear_alarms()
        assert _wait(lambda: not ctl.active_alarms, 3)
    finally:
        ctl.disconnect()
