"""Laser command channel: adapter bytes from CIPG/CRaycusProtocolAdapter (NCModule)."""
import pytest

from mlaser.devices.laser import (IpgCommands, IpgLaser, LaserError, LaserSimulator, RaycusCommands,
                                  RaycusLaser, raycus_status_ok)
from mlaser.devices.transport import NetTransport


def test_ipg_command_bytes():
    assert IpgCommands.emission_on() == b"EMON\r"          # FUN_10027260
    assert IpgCommands.emission_off() == b"EMOFF\r"        # FUN_100272b0
    assert IpgCommands.aim_on() == b"ABN\r" and IpgCommands.aim_off() == b"ABF\r"
    assert IpgCommands.set_current(55) == b"SDC55\r"       # FUN_10027880
    assert IpgCommands.external_aim_on() == b"EEABC\r"


def test_raycus_command_bytes():
    assert RaycusCommands.emission_on() == b"\x1bO\r"      # FUN_10027110
    assert RaycusCommands.emission_off() == b"\x1bS\r"     # FUN_10027140
    assert RaycusCommands.set_power(80) == b"\x1bCP\x50\r"  # FUN_10027170, raw byte
    assert RaycusCommands.set_power(101) == b""
    assert RaycusCommands.set_pulse(5000, 50, 50) == bytes(18)
    assert RaycusCommands.set_pulse(49, 50, 50) == b""


def test_raycus_status_frame():
    assert raycus_status_ok(bytes([0xAA, 0x55] + [0] * 10 + [0x55, 0xAA]))
    assert not raycus_status_ok(bytes(14))


def test_ipg_roundtrip_loopback():
    sim = LaserSimulator("ipg")
    las = IpgLaser(sim.loopback())
    las.emission(True)
    assert sim.emission
    las.aiming_beam(True)
    assert las.query("STA") == "STA: 260"
    las.set_current(42.4)
    assert sim.current == 42
    las.emission(False)
    assert not sim.emission


def test_raycus_over_udp_with_retry():
    sim = LaserSimulator("raycus")
    sim.drop = 2
    srv = sim.serve()
    try:
        las = RaycusLaser(NetTransport("127.0.0.1", srv.port, "udp", timeout_ms=100, max_send=5))
        las.emission(True)
        las.set_power(30)
        assert sim.emission and sim.power == 30
        las.close()
    finally:
        sim.stop()


def test_no_reply_raises():
    sim = LaserSimulator("ipg")
    sim.drop = 99
    with pytest.raises(LaserError):
        IpgLaser(sim.loopback()).emission(True)
