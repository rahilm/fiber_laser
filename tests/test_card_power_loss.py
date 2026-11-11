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


def test_the_log_and_the_alarm_records_are_written_off_the_gui_thread(tmp_path, monkeypatch):
    """CI (Windows): right after the card lost power the GUI thread stood 0.23-0.75 s in the run
    log's ``open(..., "a")`` (each log line arrives there as an event; the antivirus holds a
    file being written), with the process using no CPU meanwhile.  The run log, the alarm
    history (the fault is recorded) and the production ledger's alarm records are written by
    the file writer thread: a slow file system no longer stalls the caller."""
    from mlaser import _writebehind
    from mlaser.controller.alarms import Alarm
    from mlaser.diag import logs as L
    from mlaser.machine import events as EV
    from mlaser.report import ledger as LG
    from mlaser.services import alarm_history as AH
    caller = threading.current_thread()
    writers = []
    real_open = open

    def slow_open(path, mode="r", *a, **k):
        if "a" in mode or "w" in mode:
            writers.append(threading.current_thread())
            time.sleep(0.3)
        return real_open(path, mode, *a, **k)

    def slow_atomic(path, data, *a, **k):
        writers.append(threading.current_thread())
        time.sleep(0.3)
        return real_atomic(path, data, *a, **k)
    real_atomic = AH.atomic_write_text
    monkeypatch.setattr(L, "open", slow_open, raising=False)
    monkeypatch.setattr(LG, "open", slow_open, raising=False)
    monkeypatch.setattr(AH, "atomic_write_text", slow_atomic)
    log = L.LogFileWriter(str(tmp_path / "Log"))
    hist = AH.AlarmHistory(tmp_path / "alarm_history.jsonl")
    bus = EV.EventBus()
    hist.attach(bus)
    ledger = LG.Ledger(str(tmp_path / "Report"))
    t0 = time.monotonic()
    for i in range(5):
        log.write(f"line {i}")
    a = Alarm(8104, "", "X servo input alarm", 0, "servo")
    bus.publish(EV.AlarmRaised(alarms=(a,), active=(a,)))
    bus.publish(EV.StateChanged("disarmed", "fault", "link lost", "link lost"))
    ledger.alarm_raised("FAULT", "link lost", "fault")
    took = time.monotonic() - t0
    assert took < 0.25, f"the caller waited {took:.2f} s for the files"
    assert [r.code for r in ledger.alarms()] == ["FAULT"]      # a record on its way is read too
    assert _writebehind.flush(30)
    assert writers and caller not in writers
    lines = (tmp_path / "Log" / os.path.basename(log.path())).read_text("utf-8").splitlines()
    assert [ln.split(" -> ")[1] for ln in lines] == [f"line {i}" for i in range(5)]     # in order
    assert [r.kind for r in AH.AlarmHistory(tmp_path / "alarm_history.jsonl").entries(False)] == ["alarm", "fault"]
    assert [r.code for r in ledger.alarms()] == ["FAULT"]
    hist.detach()


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
    """Watches the GUI thread from a thread of its own while a test pumps the event loop.  For
    every time the GUI thread has not come back to the test's loop (``tick``) for over 0.3 s it
    keeps: what the test did last (``label``), the GUI thread's stack sampled while it was
    stuck, the CPU time the process used meanwhile, the garbage collections in it and the
    longest gap of this watcher's own 20 ms ticks.  A watcher that also stood still means the
    whole process did (descheduled: little CPU) or one thread held the GIL (a collection, C
    code: CPU about the stall); a watcher that ran on means the GUI thread itself was busy or
    waiting, where its stack says."""

    LIMIT = 0.3

    def __init__(self, gui):
        import gc
        import sys
        import traceback
        self.label = ""
        self.stalls: list = []
        self._beat = (time.monotonic(), time.process_time(), "")
        self._open = None
        self._gcs: list = []              # (end, duration, generation, thread)
        self._gc_t0 = [0.0]
        self._stop = threading.Event()
        self._lock = threading.Lock()

        def on_gc(phase, info) -> None:
            if phase == "start":
                self._gc_t0[0] = time.perf_counter()
            else:
                self._gcs.append((time.monotonic(), time.perf_counter() - self._gc_t0[0],
                                  info.get("generation"), threading.current_thread().name))
        self._on_gc = on_gc
        gc.callbacks.append(on_gc)

        def run() -> None:
            last = time.monotonic()
            while not self._stop.wait(0.02):
                now = time.monotonic()
                gap, last = now - last, now
                with self._lock:
                    beat = self._beat
                    st = self._open
                    if st is not None and st["beat"] is not beat:
                        self._close(st, beat)
                        st = None
                    if now - beat[0] > self.LIMIT:
                        if st is None:
                            st = self._open = {"beat": beat, "where": "", "own_gap": 0.0, "samples": 0}
                        st["own_gap"] = max(st["own_gap"], gap)
                        frames = sys._current_frames()
                        frame = frames.get(gui.ident)
                        if not st["samples"]:      # what the other threads were doing (a GIL holder)
                            names = {t.ident: t.name for t in threading.enumerate()}
                            st["others"] = "; ".join(
                                f"{names.get(i, i)}@{os.path.basename(f.f_code.co_filename)}:{f.f_lineno}"
                                for i, f in frames.items() if i not in (gui.ident, threading.get_ident()))
                        if frame is not None:
                            st["samples"] += 1
                            st["where"] = (f"sampled after {now - beat[0]:.2f} s:\n"
                                           + "".join(traceback.format_stack(frame)[-16:]))
        self._th = threading.Thread(target=run, name="gui-watch", daemon=True)
        self._th.start()

    def _close(self, st, end) -> None:
        t0, cpu0, label = st["beat"]
        st["wall"] = end[0] - t0
        st["cpu"] = end[1] - cpu0
        st["after"] = label
        st["gc"] = [(round(d, 3), g, th) for t, d, g, th in self._gcs if t0 <= t <= end[0] and d > 0.01]
        self.stalls.append(st)
        self._open = None

    def tick(self) -> None:
        """The GUI thread is back in the test's loop."""
        beat = (time.monotonic(), time.process_time(), self.label)
        with self._lock:
            self._beat = beat

    def stop(self) -> None:
        import gc
        self.tick()
        self._stop.set()
        self._th.join(2)
        with self._lock:
            if self._open is not None:
                self._close(self._open, self._beat)
        if self._on_gc in gc.callbacks:
            gc.callbacks.remove(self._on_gc)

    def report(self) -> str:
        slow_gc = [(round(d, 3), g, th) for t, d, g, th in self._gcs if d > 0.05]
        head = f"collections over 50 ms meanwhile: {slow_gc}\n"
        if not self.stalls:
            return head + "(no stall over 0.3 s was sampled)"
        return head + "\n".join(
            f"stall of {st['wall']:.2f} s after {st['after']!r}: process CPU {st['cpu']:.2f} s, "
            f"collections {st['gc']}, the watcher's own longest tick gap {st['own_gap']:.2f} s, "
            f"{st['samples']} stack samples; other threads then: {st.get('others', '?')}; GUI thread "
            f"{st['where'] or '(no stack)'}" for st in self.stalls)


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
        stuck = _GuiWatch(gui)
        try:
            while time.monotonic() < end:
                app.processEvents()
                now = time.monotonic()
                worst, prev = max(worst, now - prev), now
                stuck.tick()
                time.sleep(0.005)
        finally:
            stuck.stop()
        t.stop()
        assert worst < 0.5, f"the GUI thread blocked for {worst:.2f} s:\n{stuck.report()}"
        assert on_gui == [], f"card requests on the GUI thread: {on_gui[:10]}"
        t0 = time.monotonic()
        w.reconnect()                        # the operator presses Reconnect: no freeze either
        assert time.monotonic() - t0 < 0.5
        mon.close()
    finally:
        monkeypatch.setattr(MotionController, "transact", orig)
        dispose(w)


# ---------------------------------------------------------------------------- every panel command
def _pump(app, seconds, stalls, watch=None):
    end = time.monotonic() + seconds
    prev = time.monotonic()
    if watch is not None:
        watch.tick()
    while True:
        app.processEvents()
        now = time.monotonic()
        stalls.append(now - prev)
        prev = now
        if watch is not None:
            watch.tick()
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
        watch = _GuiWatch(gui)
        try:
            for phase in ("silent", "known lost"):
                if phase == "known lost":
                    watch.label = "waiting for the link-lost verdict"
                    deadline = time.monotonic() + 15
                    while not ctl._sup._lost and time.monotonic() < deadline:
                        _pump(app, 0.05, stalls, watch)
                    assert ctl._sup._lost
                for name, act in _panel_actions(w):
                    watch.label = f"{phase}: {name}"
                    watch.tick()
                    t0 = time.monotonic()
                    act()
                    timings[f"{phase}: {name}"] = time.monotonic() - t0
                    _pump(app, 0.05, stalls, watch)
            # the halt still goes out: the supervisor keeps repeating it on the halt channel
            # (each sequence runs on a thread of its own and is recorded once it ended: on a
            # loaded macOS runner one took 2.4 s with the card off, none was recorded yet)
            watch.label = "the test waiting for the halts on their way"
            assert ctl._sup.wait_halts(15)
            deadline = time.monotonic() + 15
            while not ctl._sup.halts and time.monotonic() < deadline:
                _pump(app, 0.05, [], watch)
            assert ctl._sup.halts and not ctl._sup.halts[-1].ok
            assert ctl._sup._retry is not None
            watch.label = "disconnect"
            watch.tick()
            t0 = time.monotonic()
            w.ctl.disconnect()                    # the operator's Disconnect
            timings["disconnect"] = time.monotonic() - t0
            _pump(app, 0.3, stalls, watch)
        finally:
            watch.stop()
        slow = {k: round(v, 3) for k, v in timings.items() if v > 0.25}
        assert not slow, f"commands that blocked the GUI thread: {slow}\n{watch.report()}"
        assert max(stalls) < 0.5, f"the GUI thread blocked for {max(stalls):.2f} s:\n{watch.report()}"
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
              "is_demo", "restore_kept_breakpoint", "discard_kept_breakpoint",
            # no card I/O: the host's arm refusals (a config.json not loaded, review A3)
            "set_arm_hold", "arm_holds",
            # no card I/O: a flag the next job start consumes (arming across a queue's sheets)
            "keep_armed_after_next_job",
            # no card I/O: reads the job lifecycle's phase (arch A1)
            "job_phase"}
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
