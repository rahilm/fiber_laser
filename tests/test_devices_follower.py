"""On-board follower through the motion card, calibration table, network follower codec."""
import os

import pytest

from mlaser.controller import frames as F
from mlaser.controller.client import MotionController, TransportConfig
from mlaser.devices.follower import CalibrationTable, OnBoardFollower, make_follower_card_simulator

ORIG = "/home/dev/scratch/mlaser/extracted/Mlaser-v0.0.1.51_SP1/Mlaser-v0.0.1.51_SP1"


@pytest.fixture
def rig():
    sim = make_follower_card_simulator().start()
    cfg = TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=200, fifo_time_ms=600, max_recv=2)
    mc = MotionController(config=cfg, pulses_per_mm=1000.0).open()
    yield sim, mc
    mc.close()
    sim.stop()


def test_follow_and_status(rig):
    sim, mc = rig
    zf = OnBoardFollower(mc)
    zf.follow(True, 800)
    assert sim.state.commands[-1] == [118, 4, 1, 800]       # FUN_10041740
    st = zf.status()
    assert st[5] == 800 and st.flag and zf.flag_10004()
    assert len(zf.read_props()) == 41
    zf.signal_correction()
    assert sim.state.commands[-1] == [118, 7]                 # FUN_10041630
    assert st.homed and st.value("signal_height_mm") == pytest.approx(0.8)
    zf.follow(False)
    assert not zf.status().flag


def test_download_func_26(rig):
    sim, mc = rig
    OnBoardFollower(mc).download(0, bytes(range(10)), chunk=4)
    assert [a for a, _ in sim.downloads] == [0, 4, 8]
    assert b"".join(d for _, d in sim.downloads) == bytes(range(10))


def test_calibration_table_roundtrip(tmp_path):
    text = "413023, 0\r\n0, 406507\r\n0, 409718\r\n101, 410429\r\n201, 410740\r\n"
    t = CalibrationTable.parse(text)
    assert t.reference == 413023 and t.points[2] == (101, 410429)
    assert t.dumps() == text
    p = tmp_path / "calib.csv"
    t.save(str(p))
    assert CalibrationTable.load(str(p)).points == t.points
    assert t.height_for(410585) == pytest.approx(151, abs=1)


@pytest.mark.skipif(not os.path.exists(ORIG + "/Help/calib.csv"), reason="original install missing")
def test_shipped_calib_csv():
    t = CalibrationTable.load(ORIG + "/Help/calib.csv")
    assert t.reference == 413023 and len(t.points) == 63 and t.points[-1] == (4201, 412318)


def test_calibration_and_props(rig):
    sim, mc = rig
    zf = OnBoardFollower(mc)
    zf.calibrate()                                            # writeReg(101, 107)
    assert zf.status().calibrating and sim.state.commands[-1] == [107]
    zf.cancel_calibration()
    assert not zf.status().calibrating
    zf.write_prop(3, 250)                                     # 11000 + 2*3, then 150 := 9999
    assert sim.props[6] == 250 and sim.commits == [9999]
    zf.factory_reset()
    assert sim.commits[-1] == 5555


def test_read_calibration_curve(rig, tmp_path):
    sim, mc = rig
    sim.zf.table = CalibrationTable(413023, [(h, 406000 + h) for h in range(0, 50000, 200)])
    t = OnBoardFollower(mc).read_calibration_curve()          # > 200 regs -> chunked reads
    assert t.reference == 413023 and t.points == sim.zf.table.points
    p = tmp_path / "calib.csv"
    t.save(str(p))
    assert p.read_bytes().startswith(b"413023, 0\r\n0, 406000\r\n")
