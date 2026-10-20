"""Offscreen smoke test for mlaser.ui.ext_devices."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from mlaser.devices.follower import CalibrationTable  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_register_and_dialogs(app, tmp_path):
    from mlaser.ui import ext_devices as X
    from mlaser.ui.mainwindow import MainWindow
    from wiring import real_window
    n = MainWindow().ribbon.count()
    w = real_window()
    assert "dev.follower" in w.handlers
    assert "Devices" in [w.ribbon.tabText(i) for i in range(n, w.ribbon.count())]
    p = tmp_path / "calib.csv"
    CalibrationTable(413023, [(0, 406507), (101, 410429), (201, 410740)]).save(str(p))
    d = X.FollowerDialog(w, str(p))
    assert d.grid.rowCount() == 3 and d.plot.points[1] == (101, 410429)
    d.grab()
    d.refresh()                               # demo controller: no card -> "Not connected"
    assert "Not connected" in d.status_lbl.text()
    X.LaserDialog(w).grab()
    pd = X.PendantDialog(w)
    pd.grab()
    pd.close()
    w.close()


def test_follower_dialog_with_card(app):
    from types import SimpleNamespace
    from mlaser.controller.client import MotionController, TransportConfig
    from mlaser.controller.follower_sim import make_follower_card_simulator
    from mlaser.ui import ext_devices as X
    sim = make_follower_card_simulator().start()
    mc = MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=200,
                                                 fifo_time_ms=600, max_recv=2)).open()
    try:
        win = SimpleNamespace(ctl=SimpleNamespace(card=lambda: mc), log_panel=None)
        d = X.FollowerDialog(win, parent=None)
        d.refresh()
        assert "homed=True" in d.status_lbl.text()
        d._calibrate()
        assert sim.state.commands[-1] == [107]
        d._read_curve()
        assert d.grid.rowCount() == len(sim.zf.table.points)
        d.close()
    finally:
        mc.close()
        sim.stop()
