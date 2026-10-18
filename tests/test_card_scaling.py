"""Card scaling: parameter-block decode (FUN_10039f20), 16-bit addressing, step vs position units."""
from __future__ import annotations

import pytest

from mlaser.protocol import frames as F
from mlaser.controller.client import (AX_W_LEAD, AX_W_PPR, SYS_W_PERIOD_US, SYS_W_PPM, AxisScaling,
                                      CardScaling, MotionController, TransportConfig,
                                      axis_param_addr, sys_param_addr)
from mlaser.controller.simulator import CardSimulator


def test_default_axis_scaling_is_ppr_over_lead():
    a = AxisScaling()                      # MAC.WritePluse 10000, MAC.SpeedRatio 100
    assert a.step_ppm == pytest.approx(100.0)
    sc = CardScaling()
    assert sc.max_speed(0) == pytest.approx(1270.0)          # 127 p / 100 p/mm / 1 ms
    assert CardScaling(period_us=250).max_speed(0) == pytest.approx(5080.0)


def test_from_words_decode():
    sysw = [0] * 26
    sysw[SYS_W_PERIOD_US] = 250
    sysw[SYS_W_PPM] = 1000
    axw = [0] * 100
    for a in range(5):
        axw[20 * a + AX_W_LEAD] = 10 * 1000          # lead 10 mm * ppm
        axw[20 * a + AX_W_PPR] = 10000
        axw[20 * a + 5] = 50 * 1000                  # home fast 50 mm/s
    sc = CardScaling.from_words(sysw, axw)
    assert sc.period_ms == 0.25 and sc.position_ppm == 1000
    assert sc.axes[0].lead_mm == 10 and sc.step_ppm(1) == 1000
    assert sc.axes[0].home_fast == 50
    assert sc.max_speed(0) == pytest.approx(508.0)
    assert "508" in sc.summary()


def test_param_block_addresses():
    assert sys_param_addr(14) == 50028 and sys_param_addr(18) == 50036     # FUN_1002e500 writes
    assert axis_param_addr(0, 0) == 50200 and axis_param_addr(2, 0) == 50280


@pytest.fixture
def sim_mc():
    sim = CardSimulator(ppm=1000, step_ppm=100, period_ms=0.25).start()
    mc = MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=300))
    mc.open()
    yield sim, mc
    mc.close()
    sim.stop()


def test_read_scaling_from_simulator(sim_mc):
    sim, mc = sim_mc
    sc = mc.read_scaling()
    assert sc.ppm == 1000 and sc.period_us == 250
    assert sc.step_ppm(0) == pytest.approx(100.0) and sc.axes[0].ppr == 10000
    # 16-bit addressing: word 17 of the system block is register 50034
    mc.write_sys_param(SYS_W_PPM, 2000)
    assert sim.state.params[50034] == 2000
    assert mc.read_system_params()[SYS_W_PPM] == 2000


def test_step_pulses_vs_position_counts(sim_mc):
    """Steps are in ppr/lead pulses (MainApp FUN_00462630), positions in ppm counts."""
    sim, mc = sim_mc
    mc.pulses_per_mm = 1000
    items = [F.encode_step(10, -5) for _ in range(100)]       # 1000 x -500 step pulses
    mc.fifo_clear()
    for fr in F.build_fifo_frames(items, mc.read_status().fifo_seq + 1):
        mc.fifo_write_frame(fr)
    mc.fifo_start()
    assert sim.wait_idle(5)
    x, y = mc.position_mm()[:2]
    assert x == pytest.approx(10.0) and y == pytest.approx(-5.0)        # 1000 p / 100 p/mm


def test_ui_controller_adopts_card_scaling():
    from mlaser.controller.ui_adapter import UiController
    from mlaser.nc import Machine
    m = Machine()
    ctl = UiController(machine=m)
    try:
        assert ctl.connect_simulator(time_scale=4.0, ppm=1000, step_ppm=100)
        assert ctl.scaling is not None and ctl.scaling.ppm == 1000
        assert m.pulses_per_mm_x == pytest.approx(100.0) and m.pulses_per_mm_y == pytest.approx(100.0)
        assert ctl.position_ppm == 1000
    finally:
        ctl.disconnect()
