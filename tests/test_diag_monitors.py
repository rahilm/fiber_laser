"""Controller / follower monitors and the device report against the card simulator."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from mlaser.controller.client import MotionController, TransportConfig  # noqa: E402
from mlaser.controller.simulator import CardSimulator  # noqa: E402
from mlaser.diag import devicereport as DR, registers as R  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def card():
    sim = CardSimulator("127.0.0.1", 0, ppm=1000.0).start()
    mc = MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=200,
                                                 fifo_time_ms=600, max_recv=2), pulses_per_mm=1000.0).open()
    yield sim, mc
    mc.close()
    sim.stop()


def test_label_tables():
    assert len(R.RO_KEYS) == 36 and len(R.RO_EN) == 36
    assert len(R.AXIS_RO_KEYS) == 10 and len(R.SYS_RW_KEYS) == 26 and len(R.AXIS_RW_KEYS) == 14
    # protocol facts line up with the label order (RO[4] inputs, RO[15]/[16] FIFO, RO[23]/[24])
    assert R.RO_EN[4] == "Input Status" and R.RO_EN[16] == "FIFO space margin"
    assert R.RO_EN[23] == "Profile Status" and R.RO_EN[24] == "Contour Index"
    assert R.AXIS_RO_EN[2] == "Pulse position" and R.AXIS_RO_EN[6] == "Total mileage"
    assert R.SYS_RW_KEYS[11:13] == ["RWRegName_4", "RWRegName_5"]


def test_snapshot_reads_card(card):
    sim, mc = card
    sim.set_input(3, True)
    sim.state.outputs = 0b101
    sim.state.axes[0].pos = 12345
    snap = R.read_snapshot(mc)
    assert not snap.errors, snap.errors
    assert snap.inputs()[2] and not snap.inputs()[0]
    assert snap.outputs()[0] and snap.outputs()[2] and not snap.outputs()[1]
    assert R.s32(snap.axis_ro[0][2]) == 12345
    assert len(snap.sys_rw) == 26 and len(snap.axis_rw) == 5 and len(snap.run_info) == 3
    assert snap.sys_rw[17] == 1000                       # counts/mm word (protocol.md §11)
    pages = R.table_rows(snap)
    ro = dict((a, (n, v)) for a, n, v in pages["RO"])
    assert ro["1001"] == ("Program Version", str(sim.version))
    assert ro["1004"][1] == "0x00000004"
    assert pages["SystemRW"][17][0] == "50034"
    assert pages["AxisRW"][14][0] == str(50200 + 40)     # axis 1 word 0
    assert any(a == "2002" and v == "12345" for a, _n, v in pages["AxisRO"])


def test_translated_labels(card, tmp_path):
    from pathlib import Path
    from mlaser.ui.i18n import Translator
    _sim, mc = card
    # Chinese comes from an original install; tests/data/Lang is a synthetic one
    tr = Translator(lang_dir=str(Path(__file__).parent / "data" / "Lang"), language="zh",
                    import_dir=str(tmp_path))
    rows = R.table_rows(R.read_snapshot(mc, params=False), tr)["RO"]
    assert rows[4][1] == "输入状态"


def test_device_report_card_info(card):
    sim, mc = card
    sim.state.axes[0].pos = 0
    info, mileage = DR.read_card_info(mc, (0, 1), None)
    assert info == [0, 0, 0] and set(mileage) == {"x", "y"}
    c = DR.DeviceCounters()
    c.add_card_info([0, 0, 7200], {"x": 2_000_000, "y": 500_000, "z": 1000}, 1000.0)
    assert c.laser_on_h == 2.0 and c.x_move_m == 2.0 and c.y_move_m == 0.5 and c.z_move_m == 0.001
    c.add_job(90)
    assert c.process_count == 1 and c.last_process_s == 90 and abs(c.total_process_h - 0.025) < 1e-9
    s = {}
    c.to_settings(s)
    assert s["SOP.DeviceTotalManuCount"] == 1
    c2 = DR.DeviceCounters.from_settings(s)
    assert c2.x_move_m == 2.0
    c2.reset("x_move_m")
    assert c2.x_move_m == 0 and c2.laser_on_h == 2.0
    c2.reset()
    assert c2.process_count == 0 and isinstance(c2.process_count, int)
    t = iter([0.0, 3600.0])
    u = DR.UseTimer(c2, clock=t.__next__)
    u.tick(connected=True)
    assert c2.software_use_h == 1.0 and c2.controller_use_h == 1.0


def test_controller_monitor_ui(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from mlaser.ui import ext_diag as X
    from mlaser.ui.mainwindow import MainWindow
    w = MainWindow()
    ext = X.register(w)
    try:
        assert w.connect_simulator(time_scale=1.0)
        sim = w.ctl.sim
        sim.set_input(1, True)
        mon = ext.mon_controller()
        mon.refresh(True)
        assert mon.tables["RO"].rowCount() == 36 and mon.tables["AxisRW"].rowCount() == 70
        assert "background" in mon.di[0].styleSheet()
        # read-only: the DO button does not switch the output
        mon._set_do(2, True)
        assert not (sim.state.outputs >> 1) & 1
        mon.ro_mode.setChecked(True)
        mon._set_do(2, True)                 # DO2 = a gas valve: allowed disarmed (manual gas)
        assert (sim.state.outputs >> 1) & 1
        assert w.ctl.arm()                   # the DA / PWM test below fires: needs arming
        mon.da1.setValue(5000)
        mon.pwm_f.setValue(2000)
        mon.pwm_d.setValue(40)
        mon.apply_da_pwm()
        mon.pulses.setValue(500)
        mon.send_pulses()
        assert sim.wait_idle(5.0)
        assert sim.state.axes[0].pos == pytest.approx(500, abs=1)
        mon.close()
        # FTC monitor without an on-board follower: shows the card error, no exception
        fm = ext.mon_ftc()
        fm.refresh()
        assert "not available" in fm.info.text()
        fm.close()
        # device report reads 1050 + mileage and stores the counters
        monkeypatch.setattr(X.DeviceReportDialog, "exec", lambda self: 0)
        d = ext.device_report()
        assert d.table.rowCount() == 9
        d.zero_all(confirm=False)
        assert ext.counters.process_count == 0
    finally:
        w.close()


def test_follower_monitor_with_card(app, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mlaser.controller.follower_sim import make_follower_card_simulator
    from mlaser.ui import ext_diag as X
    sim = make_follower_card_simulator().start()
    mc = MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=200,
                                                 fifo_time_ms=600, max_recv=2)).open()
    try:
        win = SimpleNamespace(ctl=SimpleNamespace(card=lambda: mc, state=lambda: None), log_panel=None, tr_=None)
        ext = SimpleNamespace(w=win, settings={}, device_settings=None)
        m = X.DeviceMonitor(ext, "zf")
        m.refresh()
        names = [m.table.item(i, 1).text() for i in range(m.table.rowCount())]
        assert "signal_height_mm" in names and "homed" in names
        m.read_props()
        assert m.props.rowCount() == 41
        p = str(tmp_path / "zf.zpf")
        assert m.export_zpf(p) == p
        text = open(p, newline="").read()
        assert text.startswith("41\r\n") and text.endswith("eof")
        assert m.import_zpf(p)
        m.calibrate()
        assert sim.state.commands[-1] == [107]
        m.close()
    finally:
        mc.close()
        sim.stop()
