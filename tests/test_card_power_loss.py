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
        assert time.monotonic() - t0 < 5.0      # vs ~12 s waiting out timeouts (macOS runners are slow)
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


class _GuiWatch:
    """Samples the GUI thread's stack while it has not come back to the test's loop for over
    0.3 s: where it was stuck, for a failure's message."""

    def __init__(self, gui, beat):
        import sys
        import traceback
        self.where = "(no stall sampled)"
        self._stop = threading.Event()

        def run() -> None:
            worst = 0.3
            while not self._stop.wait(0.05):
                age = time.monotonic() - beat[0]
                if age > worst:
                    frame = sys._current_frames().get(gui.ident)
                    if frame is not None:
                        worst = age
                        self.where = f"after {age:.2f} s:\n" + "".join(traceback.format_stack(frame)[-14:])
        self._th = threading.Thread(target=run, name="gui-watch", daemon=True)
        self._th.start()

    def stop(self) -> None:
        self._stop.set()
        self._th.join(2)


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
        beat = [prev]
        stuck = _GuiWatch(gui, beat)
        try:
            while time.monotonic() < end:
                app.processEvents()
                now = time.monotonic()
                worst, prev = max(worst, now - prev), now
                beat[0] = now
                time.sleep(0.005)
        finally:
            stuck.stop()
        t.stop()
        assert worst < 0.5, f"the GUI thread blocked for {worst:.2f} s in:\n{stuck.where}"
        assert on_gui == [], f"card requests on the GUI thread: {on_gui[:10]}"
        t0 = time.monotonic()
        w.reconnect()                        # the operator presses Reconnect: no freeze either
        assert time.monotonic() - t0 < 0.5
        mon.close()
    finally:
        monkeypatch.setattr(MotionController, "transact", orig)
        dispose(w)


# ---------------------------------------------------------------------------- every panel command
def _pump(app, seconds, stalls):
    end = time.monotonic() + seconds
    prev = time.monotonic()
    while True:
        app.processEvents()
        now = time.monotonic()
        stalls.append(now - prev)
        prev = now
        if now >= end:
            return
        time.sleep(0.005)


def _panel_actions(w):
    """(name, action) for every operator command of the machine panel, the safety banner and
    the window, each as the operator gives it (a button click / press / release)."""
    p = w.control
    jog = p.jog
    b = p.buttons

    def jog_hold():
        jog.step_mm = lambda: 0.0
        b["jog_X+"].pressed.emit()

    def jog_release():
        b["jog_X+"].released.emit()

    def jog_step():
        jog.step_mm = lambda: 1.0
        b["jog_Y+"].pressed.emit()
        b["jog_Y+"].released.emit()

    def toggle(name):
        return lambda: b[f"out_{name}"].toggle()

    return [
        ("jog press", jog_hold), ("jog release", jog_release), ("jog step", jog_step),
        ("gas on", toggle("gas")), ("gas off", toggle("gas")),
        ("red light on", toggle("red_light")), ("red light off", toggle("red_light")),
        ("follow on", toggle("follow")), ("follow off", toggle("follow")),
        ("shutter on", toggle("shutter")), ("shutter off", toggle("shutter")),
        ("laser press", lambda: b["out_laser"].pressed.emit()),
        ("laser release", lambda: b["out_laser"].released.emit()),
        ("laser power", lambda: p.power.setValue(p.power.value() + 5)),
        ("clear alarm", lambda: b["mf21"].click()),
        ("set origin", lambda: b["mf514"].click()),
        ("home", lambda: w.ctl.home("XY")),
        ("home Z", lambda: b["mp30"].click()),
        ("return to zero", lambda: b["mp29"].click() if "mp29" in b else w.ctl.return_zero()),
        ("backward", lambda: b["mp61"].click()),
        ("forward", lambda: b["mp62"].click()),
        ("laser test (refused)", lambda: w.ctl.laser_test(0.05)),
        ("reset fault", w.safety_ui.reset_fault),
        ("arm", lambda: w.ctl.arm()),
        ("disarm", w.safety_ui.disarm),
        ("hold", w.safety_ui.hold),
        ("stop", w.safety_ui.stop),
    ]


@pytest.mark.parametrize("link", ["silent", "down"])
def test_no_panel_command_blocks_the_gui_when_the_card_loses_power(tmp_path, monkeypatch, link):
    """F1 of the UI evaluation: with the card switched off every operator command used to
    block the GUI thread for the retry policy (jog release 29 s, Gas 7 s, Clear alarm 6 s,
    Reset 3.5-6.6 s per press, Disconnect 10 s, close 3.6 s).  Each command is given twice:
    right after the loss (the card silent, the link not yet known to be lost) and once the
    supervisor knows; the event loop never stalls and no card request runs on the GUI thread."""
    from PySide6.QtWidgets import QApplication
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    from wiring import dispose, real_window
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.05)
    w = real_window(ctl)
    gui = threading.main_thread()
    on_gui = []
    orig = MotionController.transact

    def transact(self, req):
        if threading.current_thread() is gui:
            on_gui.append(req.addr)
        return orig(self, req)
    stalls: list = []
    timings = {}
    try:
        assert w.connect_simulator(time_scale=1.0)
        w.resize(1400, 900)
        w.show()
        _pump(app, 1.5, [])                       # start-up homing read, first layouts
        assert ctl.wait_commands(30)
        n_gui = ctl._sup.ui_thread_requests       # the connect itself ran here (by design)
        monkeypatch.setattr(MotionController, "transact", transact)
        ctl.sim.stop()                            # the e-stop: the card is gone
        if link == "down":
            def down(self, data):
                raise OSError(errno.EHOSTDOWN, "Host is down")
            monkeypatch.setattr(MotionController, "_send", down)
        for phase in ("silent", "known lost"):
            if phase == "known lost":
                deadline = time.monotonic() + 15
                while not ctl._sup._lost and time.monotonic() < deadline:
                    _pump(app, 0.05, stalls)
                assert ctl._sup._lost
            for name, act in _panel_actions(w):
                t0 = time.monotonic()
                act()
                timings[f"{phase}: {name}"] = time.monotonic() - t0
                _pump(app, 0.05, stalls)
        # the halt still goes out: the supervisor keeps repeating it on the halt channel
        assert ctl._sup.halts and not ctl._sup.halts[-1].ok
        assert ctl._sup._retry is not None
        t0 = time.monotonic()
        w.ctl.disconnect()                        # the operator's Disconnect
        timings["disconnect"] = time.monotonic() - t0
        _pump(app, 0.3, stalls)
        slow = {k: round(v, 3) for k, v in timings.items() if v > 0.25}
        assert not slow, f"commands that blocked the GUI thread: {slow}"
        assert max(stalls) < 0.5, f"the GUI thread blocked for {max(stalls):.2f} s"
        assert on_gui == [], f"card requests on the GUI thread: {on_gui[:10]}"
        assert ctl._sup.ui_thread_requests == n_gui
        assert ctl.wait_commands(60)              # every queued command ended (fast: link lost)
        assert not ctl.state().connected
    finally:
        monkeypatch.setattr(MotionController, "transact", orig)
        dispose(w)


@pytest.mark.parametrize("link", ["silent", "down"])
def test_closing_the_window_with_the_card_off_is_prompt_and_still_halts(tmp_path, monkeypatch, link):
    from PySide6.QtWidgets import QApplication
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    from wiring import real_window
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.05)
    w = real_window(ctl)
    assert w.connect_simulator(time_scale=1.0)
    _pump(app, 1.0, [])
    assert ctl.wait_commands(30)
    ctl.sim.stop()
    if link == "down":
        def down(self, data):
            raise OSError(errno.EHOSTDOWN, "Host is down")
        monkeypatch.setattr(MotionController, "_send", down)
    b = w.control.buttons
    w.control.jog.step_mm = lambda: 0.0
    b["jog_X+"].pressed.emit()                   # a held jog when the window closes
    n0 = len(ctl._sup.halts)
    w._test_no_dialogs = True
    t0 = time.monotonic()
    w.close()
    took = time.monotonic() - t0
    assert took < 0.25, f"close blocked the GUI thread for {took:.2f} s"
    assert ctl.wait_commands(60)
    reasons = [h.reason for h in ctl._sup.halts[n0:]]
    assert "main window closed" in reasons and "disconnect" in reasons, reasons   # attempted
    assert not ctl.state().connected and not ctl._sup.connected
    w.deleteLater()
    app.processEvents()


def test_a_stop_cancels_queued_motion_commands(tmp_path, monkeypatch):
    """A command queued behind a slow one (the card slow to answer) and a Stop given meanwhile:
    the Stop goes out at once and the queued motion is dropped, never sent after it."""
    from PySide6.QtWidgets import QApplication
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    from wiring import dispose, real_window
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.05)
    w = real_window(ctl)
    try:
        assert w.connect_simulator(time_scale=1.0)
        _pump(app, 1.0, [])
        assert ctl.wait_commands(30)
        gate = threading.Event()
        ctl.submit_command("slow", lambda: gate.wait(10))     # the card busy answering
        sent = []
        orig = MotionController.transact

        def transact(self, req):
            sent.append((req.addr, list(req.values[:2])))
            return orig(self, req)
        monkeypatch.setattr(MotionController, "transact", transact)
        t = ctl.jog_step("X", 1, 5.0)                          # queued behind it
        assert t is not None and not t.done()
        w.safety_ui.stop()                                     # Stop: at once
        gate.set()
        assert ctl.wait_commands(30)
        assert t.dropped, "the jog given before the Stop must not run after it"
        assert not any(a == 101 and v[:1] == [3] for a, v in sent), sent   # no [3, ...] jog
    finally:
        dispose(w)


def test_every_operator_command_is_queued_or_a_reviewed_direct_path():
    """Static walk: every MachineAPI method of the UI controller is a queued card command (runs
    on the command worker when given on the GUI thread), a direct halt path (Stop, Hold,
    halt, disconnect: immediate threads) or sends nothing to the card; a new operator command
    cannot silently block the GUI thread again."""
    from mlaser.controller.ui_adapter import UiController
    from mlaser.machine.api import ControllerFacade
    queued = {n for n in dir(UiController) if getattr(getattr(UiController, n, None), "card_command", None)}
    assert {"home", "jog_start", "jog_step", "jog_stop", "move_to", "move_by", "return_zero", "set_origin",
            "set_output", "laser_press", "laser_release", "laser_off", "laser_test", "set_laser_power",
            "clear_alarms", "reset_fault", "start", "dry_run", "frame", "start_job", "resume",
            "resume_saved", "step_backward", "step_forward", "startup_homing"} <= queued
    direct = {"stop", "pause", "safe_halt", "disconnect", "arm", "disarm", "connect", "operator_action",
              "heartbeat_hold", "jog_keepalive", "set_gas_type", "go_origin_point", "place", "preflight",
              "wait_job", "job_active", "state", "snapshot", "is_simulator", "card", "endpoint", "safety",
              "set_plugin_policy", "plugin_policy_check", "start_generation", "heartbeat_status",
              "add_log_listener", "add_alarm_listener", "travel_limits", "homed_xy", "layer_table",
              "laser_enabled", "require_homed", "raise_fault", "commands_pending", "wait_commands",
              # no card I/O: the kept resume point is controller state (UX audit W17)
              "is_demo", "restore_kept_breakpoint", "discard_kept_breakpoint"}
    api = {n for n, v in vars(ControllerFacade).items() if callable(v) and not n.startswith("_")}
    assert not api - queued - direct, f"MachineAPI commands neither queued nor reviewed: {api - queued - direct}"


def test_the_channels_never_close_under_a_halt_still_on_its_way():
    """CI (Windows): the window's "main window closed" halt and the disconnect's own halt run
    from their own threads; the disconnect used to wait only for its own, so the sockets could
    close (and the disconnect end) before the first one was sent.  Every halt started before
    the disconnect is sent and recorded before the channels close."""
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.05)
    assert ctl.connect_simulator(time_scale=1.0)
    sent = []
    try:
        gate = threading.Event()

        def slow_halt():
            gate.wait(5)
            sent.append(ctl._sup.connected)          # still connected when it goes out
        ev = ctl._sup.send_soon(slow_halt, "mlaser-halt")
        threading.Timer(0.3, gate.set).start()
        ctl.disconnect()
        assert ev.is_set() and sent == [True]
        assert not ctl._sup.connected
    finally:
        gate.set()
        ctl.disconnect()
