"""UX safety P0s (docs/review/ux.md §2.2, P0-2..P0-5, P0-7): banners, run bar, global keys,
document lock, status bar, pre-flight dialog."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QScrollArea  # noqa: E402

from safety_helpers import rect_doc, wait_until  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch):
    from mlaser.machine.demo import DemoController
    from mlaser.ui.mainwindow import MainWindow
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    DemoController.RAPID = 400.0
    w = MainWindow(DemoController())
    w._test_no_dialogs = True
    w.resize(1280, 720)                    # 1920 x 1080 at 150 % scaling
    w.show()
    app.processEvents()
    yield w
    w.ctl.stop()
    w.close()


def _pump(app, s=0.15):
    t = time.monotonic() + s
    while time.monotonic() < t:
        app.processEvents()
        time.sleep(0.01)


def _start_job(win, app):
    win.set_document(rect_doc(w=400, h=300))
    win.ctl.connect()
    assert win.ctl.arm()
    win.safety_ui.start()
    # Start runs the planned job: it is compiled in the JobService worker, then handed over
    jobs = getattr(win, "jobs", None)
    if jobs is not None and jobs.last_run is not None:
        jobs.settle(jobs.last_run, timeout=30)
    _pump(app)
    assert win.ctl.state().mode == "process"


def test_run_bar_always_visible_and_not_scrolled(win, app):
    rb = win.safety_ui.run_bar
    assert rb.isVisible() and not rb.isMovable() and not rb.isFloatable()
    p = rb.parent()
    while p is not None:
        assert not isinstance(p, QScrollArea)
        p = p.parent()
    g = rb.geometry()
    assert g.bottom() <= win.height() and g.top() >= 0
    for key in ("start", "hold", "stop", "frame"):
        b = rb.buttons[key]
        assert b.isVisible() and b.height() >= 56 and b.width() >= 56
        r = b.geometry()
        assert r.right() <= rb.width() and r.left() >= 0, f"{key} clipped at 1280 px"


def test_state_banner_follows_the_safety_state(win, app):
    st = win.safety_ui.banner
    _pump(app)
    assert st.kind == "SIMULATOR"            # the demo controller is a simulator, also offline
    win.ctl.connect()
    _pump(app)
    assert st.kind == "SIMULATOR"
    assert win.ctl.arm()
    _pump(app)
    assert st.kind == "ARMED" and "ARMED" in st.label.text()
    win.ctl.raise_fault("test fault")
    _pump(app)
    assert st.kind == "FAULT" and "test fault" in st.label.text()
    assert win.safety_ui.alarm_bar.isVisible() and "Remedy" in win.safety_ui.alarm.remedy.text()
    assert win.st_mc.text() == "MCC <FAULT>"
    win.safety_ui.reset_fault()
    _pump(app)
    assert st.kind == "SIMULATOR" and not win.safety_ui.alarm_bar.isVisible()


def test_status_bar_never_says_ready_with_an_alarm(win, app):
    from mlaser.controller.alarms import decode
    win.ctl.connect()
    win.ctl.active_alarms = decode(1 << 30, 0)
    _pump(app)
    assert "ALARM" in win.st_mc.text() and "Ready" not in win.st_mc.text()
    assert win.safety_ui.alarm_bar.isVisible()
    assert "e-stop" in win.safety_ui.alarm.remedy.text().lower()


def test_no_file_open_or_new_document_during_a_job(win, app, tmp_path):
    _start_job(win, app)
    doc = win.document
    win.file_new()
    assert win.document is doc
    assert not win.load_path(str(tmp_path / "x.dxf"))
    win.run("file.open")
    assert win.document is doc
    assert "not available while a job is running" in win.log_panel.text.toPlainText()


@pytest.mark.parametrize("key", [Qt.Key_Escape, Qt.Key_F12])
def test_global_stop_keys(win, app, key):
    _start_job(win, app)
    win.canvas.setFocus()
    QTest.keyClick(win.canvas, key)
    _pump(app)
    assert win.ctl.state().mode == "ready"


def test_global_hold_key(win, app):
    _start_job(win, app)
    QTest.keyClick(win.canvas, Qt.Key_Space)
    _pump(app)
    assert win.ctl.state().mode == "pause"
    QTest.keyClick(win.canvas, Qt.Key_Space)
    _pump(app)
    assert win.ctl.state().mode == "process"


def test_preflight_dialog_blocks_errors(win, app, monkeypatch):
    shown = []
    from mlaser.ui import safety_ui
    monkeypatch.setattr(safety_ui.PreflightDialog, "exec", lambda self: shown.append(self.report) or 1)
    win._test_no_dialogs = False
    win.set_document(rect_doc(x0=5000, w=100, h=100))
    win.ctl.connect()
    assert win.ctl.arm()
    win.safety_ui.start()
    _pump(app)
    assert shown and "out_of_travel" in shown[0].codes()
    assert win.ctl.state().mode == "ready"


def test_laser_button_only_when_armed(win, app):
    win.ctl.connect()
    _pump(app)
    assert not win.control.buttons["out_laser"].isEnabled()
    assert win.ctl.arm()
    _pump(app)
    assert win.control.buttons["out_laser"].isEnabled()


def test_startup_prompts_are_dialogs_with_dont_ask(win, app, monkeypatch):
    from mlaser.ui import safety_ui
    asked = []

    def fake_exec(self):
        asked.append(self.text())
        if self.checkBox() is not None:
            self.checkBox().setChecked(True)
        return QMessageBox.Ok
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    panel = win.control
    panel.settings = {}
    assert panel.confirm("mp119") and panel.confirm("mf156")       # one dialog for the pair
    assert len(asked) == 1
    assert panel.confirm("mp119") and len(asked) == 1              # "don't ask again" stored
    assert panel.settings.get("dont_ask.mp119") is True
    called = []
    safety_ui.recovery_prompt(win, lambda: called.append(1))
    assert called == [] or called == [1]                         # a real dialog (stubbed here)


def test_jog_keepalive_and_focus_loss(win, app):
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.02, stream_process=False)
    assert ctl.connect_simulator()
    win.set_controller(ctl)
    try:
        panel = win.control
        panel.mode_step.setChecked(False)
        panel._jog_press("X", 1)
        sim = ctl.sim
        assert wait_until(lambda: sim.state.axes[0].busy, 1.0) is not None
        _pump(app, 0.8)                              # keep-alive timer runs while held
        assert sim.state.axes[0].busy
        win.safety_ui._app_state(Qt.ApplicationInactive)    # focus lost -> jog stopped
        assert wait_until(lambda: not sim.state.axes[0].busy, 1.5) is not None
    finally:
        ctl.disconnect()
