"""Issue #5: the machine's e-stop switches the card off and the application hung.

The Controller monitor (also a panel of the Machine workspace) read a register snapshot on the
GUI thread every 500 ms; with the card gone one snapshot waits out the retry policy (12 s), so
the window never got its event loop back.  Reconnect blocked the GUI for ~17 s the same way.
On macOS a send to a card whose link went down fails at once (EHOSTDOWN), which escaped the
transport as a bare OSError."""
import errno
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

from mlaser.controller.client import ControllerError, MotionController, TransportConfig
from mlaser.protocol import frames as F


def test_a_link_down_send_is_a_no_reply_error_not_an_oserror(monkeypatch):
    mc = MotionController(config=TransportConfig(host="127.0.0.1", port=9, timeout_ms=50)).open()
    try:
        def down(self, data):
            raise OSError(errno.EHOSTDOWN, "Host is down")
        monkeypatch.setattr(MotionController, "_send", down)
        t0 = time.monotonic()
        with pytest.raises(ControllerError) as ei:
            mc.read_regs(F.REG_STATUS_RO, 2)
        assert ei.value.code == F.ERR_HOST_UNREACHABLE
        assert time.monotonic() - t0 < 2.0
    finally:
        mc.close()


def test_the_repeated_halt_keeps_its_reason():
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.05)
    assert ctl.connect_simulator(time_scale=1.0)
    try:
        ctl.sim.stop()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and ctl._sup._retry is None:
            time.sleep(0.1)
        assert ctl._sup._retry is not None              # still retried: the link may come back
        time.sleep(2.5)
        assert "(repeat)" not in ctl._sup._retry[0]
    finally:
        ctl.disconnect()


def test_background_call_delivers_on_the_gui_thread_and_never_overlaps():
    from PySide6.QtWidgets import QApplication
    from mlaser.ui.qtbridge import BackgroundCall
    app = QApplication.instance() or QApplication([])
    bc = BackgroundCall()
    gate = threading.Event()
    got = []
    assert bc.start(lambda: (gate.wait(5), threading.current_thread())[1],
                    lambda res, err: got.append((res, err, threading.current_thread())))
    assert bc.busy and not bc.start(lambda: 1, lambda r, e: got.append("second"))
    gate.set()
    assert bc.wait(5)
    (worker, err, delivered_on), = got
    assert err is None and worker is not threading.main_thread() and delivered_on is threading.main_thread()
    assert not bc.busy
    assert bc.start(lambda: 1 / 0, lambda r, e: got.append(e)) and bc.wait(5)
    assert isinstance(got[-1], ZeroDivisionError)
    app.processEvents()


@pytest.mark.parametrize("link", ["silent", "down"])
def test_the_window_stays_responsive_when_the_card_loses_power(tmp_path, monkeypatch, link):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    from wiring import dispose, real_window
    from mlaser.controller.ui_adapter import UiController
    w = real_window(UiController(poll_s=0.05))
    gui = threading.main_thread()
    on_gui = []
    orig = MotionController.transact

    def transact(self, req):
        if threading.current_thread() is gui:
            on_gui.append(req.addr)
        return orig(self, req)
    try:
        assert w.connect_simulator(time_scale=1.0)
        w.show_workspace("machine")
        mon = w.extension("ext_diag").mon_controller()
        assert mon.io.wait(30)
        settle = time.monotonic() + 2.0      # start-up homing read, first layouts: before the loss
        while time.monotonic() < settle:
            app.processEvents()
            time.sleep(0.01)
        monkeypatch.setattr(MotionController, "transact", transact)
        w.ctl.sim.stop()                     # the e-stop: the card is gone
        if link == "down":
            def down(self, data):
                raise OSError(errno.EHOSTDOWN, "Host is down")
            monkeypatch.setattr(MotionController, "_send", down)
        ticks = []
        t = QTimer()
        t.timeout.connect(lambda: ticks.append(time.monotonic()))
        t.start(20)
        end = time.monotonic() + 4.0
        worst, prev = 0.0, time.monotonic()
        while time.monotonic() < end:
            app.processEvents()
            now = time.monotonic()
            worst, prev = max(worst, now - prev), now
            time.sleep(0.005)
        t.stop()
        assert worst < 0.5, f"the GUI thread blocked for {worst:.2f} s"
        assert on_gui == [], f"card requests on the GUI thread: {on_gui[:10]}"
        t0 = time.monotonic()
        w.reconnect()                        # the operator presses Reconnect: no freeze either
        assert time.monotonic() - t0 < 0.5
        mon.close()
    finally:
        monkeypatch.setattr(MotionController, "transact", orig)
        dispose(w)
