"""AF (RTU slave 1) and EC (RTU slave 2) clients against their simulators."""
import pytest

from mlaser.devices.autofocus import AFSimulator, AutoFocus
from mlaser.devices.codecs import EXT_CARD_MODBUS, join_u32
from mlaser.devices.ethercat import ECSimulator, EtherCATBridge
from mlaser.devices.modbus_rtu import DeviceError
from mlaser.devices.transport import NetTransport


def test_af_status_and_write_loopback():
    sim = AFSimulator()
    for i in range(14):
        sim.set_status(i, i * 1000 - 3)
    af = AutoFocus(sim.loopback())
    assert af.read_status() == [i * 1000 - 3 for i in range(14)]
    assert sim.requests[-1] == [3, 1000, 28]
    af.write_reg(1100, -2500)
    assert sim.get_u32(1100) == -2500 and af.read_reg(1100) == -2500


def test_af_exception_code():
    sim = AFSimulator()
    sim.readable = lambda a: a < 5000
    af = AutoFocus(sim.loopback())
    with pytest.raises(DeviceError) as ei:
        af.read_reg(6000)
    assert ei.value.code == 502


def test_ec_commands_frames():
    sim = ECSimulator()
    ec = EtherCATBridge(sim.loopback())
    ec.rtu.write_delay_ms = 0
    ec.jog(10.0, 2.5)                       # FUN_10037e70: [4, speed*1000, len*1000] -> reg 101
    tx = ec.rtu.t.sent[-1]
    assert tx[:7].hex() == "0210006500060c"
    assert join_u32(EXT_CARD_MODBUS.decode_request(tx)[3:]) == [4, 10000, 2500]
    ec.home(True)
    ec.stop()
    assert sim.commands == [[4, 10000, 2500], [2, 1], [1]]
    assert ec.read_status()[7] == 0


def test_ec_high_addresses_over_udp():
    sim = ECSimulator(high=True)
    srv = sim.serve()
    try:
        ec = EtherCATBridge(NetTransport("127.0.0.1", srv.port, timeout_ms=200), high=True)
        ec.pulse_jog(100, 4000)
        assert sim.requests[-1][1] == 50101
        assert ec.read_status()[7] == 4000
        ec.close()
    finally:
        sim.stop()


def test_af_commands():
    sim = AFSimulator()
    af = AutoFocus(sim.loopback())
    af.move_to(-1.5, 20)                       # [4, pos*1000, speed*1000] -> reg 101
    assert af.position_mm() == -1.5
    af.home()
    assert af.homed() and af.position_mm() == 0
    af.set_origin_offset(1200)
    assert sim.get_u32(2020) == 1200 and sim.get_u32(100) == 8888
    with pytest.raises(ValueError):
        af.set_origin_offset(20000)
