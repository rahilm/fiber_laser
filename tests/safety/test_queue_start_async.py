"""The job queue's sheet start follows the asynchronous check of Start (perf review 2026-11-09,
finding 10, left from wave 1: the queue called ``SafetyUI.wait_preflight()`` and froze the window,
its Stop button included, while a large sheet was checked).

Now "Sheet loaded: start" returns at once and the queue waits in ``starting`` (``checking``)
until the check ended.  Kept: the same refusals (back to the confirmation, the keep-armed request
withdrawn), the one arming question of a queue (asked at the first sheet only, the machine
disarmed after the last), a Stop meanwhile drops the start, and leaving the queue meanwhile
starts nothing."""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from mlaser.services.queue import JobQueue, QueueRunner  # noqa: E402

from safety_helpers import loop_heartbeat  # noqa: E402
from test_ws_run import _save, doc_of, pump, rect, show_run, wait  # noqa: E402

pytestmark = pytest.mark.xdist_group("ux_audit_safety")


# ----------------------------------------------------------------------------- the runner alone
class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _runner(tmp_path, start):
    q = JobQueue(tmp_path / "queue.json")
    q.add("a.chf", quantity=2)
    keep: list = []
    clock = _Clock()
    r = QueueRunner(q, start=start, keep_armed=keep.append, clock=clock, start_timeout_s=30.0)
    assert r.begin()
    return r, keep, clock


def test_a_start_being_checked_waits_and_a_refusal_goes_back_to_confirm(tmp_path):
    r, keep, clock = _runner(tmp_path, lambda: None)
    assert r.confirm_sheet()
    assert r.state == "starting" and r.checking and r.message == "queue.msg.checking"
    clock.t += 300.0
    r.tick()                                    # no start timeout while the check runs
    assert r.state == "starting" and r.checking
    r.start_result(False)                       # refused (pre-flight, arming declined, a Stop)
    assert r.state == "confirm" and not r.checking and r.message == "queue.msg.start_refused"
    assert keep == [True, False]                # asked before the start, withdrawn on refusal


def test_a_start_that_reached_the_runner_waits_for_the_job(tmp_path):
    r, keep, clock = _runner(tmp_path, lambda: None)
    assert r.confirm_sheet()
    clock.t += 100.0
    r.start_result(True)
    assert r.state == "starting" and not r.checking and keep == [True]
    clock.t += 29.0
    r.tick()
    assert r.state == "starting"                # the timeout counts from the hand-off
    clock.t += 2.0
    r.tick()
    assert r.state == "confirm" and keep == [True, False]
    r.start_result(False)                       # a second answer is ignored
    assert r.state == "confirm" and keep == [True, False]


def test_leaving_the_queue_while_checking_withdraws_keep_armed(tmp_path):
    r, keep, _clock = _runner(tmp_path, lambda: None)
    assert r.confirm_sheet()
    r.cancel()
    assert r.state == "idle" and keep == [True, False]
    r.start_result(True)                        # late answer: the queue stays left
    assert r.state == "idle" and r.current is None and not r.checking


def test_synchronous_starts_behave_as_before(tmp_path):
    r, keep, _clock = _runner(tmp_path, lambda: False)
    assert not r.confirm_sheet() and r.state == "confirm" and keep == [True, False]
    (tmp_path / "b").mkdir()
    r2, keep2, _c = _runner(tmp_path / "b", lambda: True)
    assert r2.confirm_sheet() and r2.state == "starting" and not r2.checking and keep2 == [True]


# ----------------------------------------------------------------------------- in the window
@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch, tmp_path):
    from wiring import real_window
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window()
    w.resize(1280, 800)
    yield w
    try:
        w.ctl.stop()
    except Exception:  # noqa: BLE001
        pass
    host = getattr(w.extension("run").screen, "_workspace_window", None)
    if host is not None:
        host.close()
    w.close()


def _queue_of(win, tmp_path, n_sheets: int = 1):
    from mlaser.machine.demo import DemoController
    clock = [0.0]
    ctl = DemoController(clock=lambda: clock[0])
    win.set_controller(ctl)
    ctl.connect()
    ws = win.extension("run")
    path = _save(doc_of(rect(0, 0, 100, 100)), tmp_path / "a.chf")
    ws.queue.add(path, n_sheets)
    win.set_document(doc_of(rect(0, 0, 10, 10)))      # the layer parameters (a .chf has none)
    return ctl, ws, clock


@pytest.fixture
def slow_check(monkeypatch):
    """The cutting-strategy part of the check waits for ``release`` (a large sheet)."""
    from mlaser.ui.workspaces import cutting_strategy as CS
    entered, release = threading.Event(), threading.Event()
    real = CS.augment_preflight

    def slow(*a, **k):
        if threading.current_thread() is threading.main_thread():
            pytest.fail("the queue's sheet was checked on the GUI thread")
        entered.set()
        release.wait(20)
        return real(*a, **k)
    monkeypatch.setattr(CS, "augment_preflight", slow)
    yield entered, release
    release.set()


def test_sheet_start_returns_at_once_and_stop_drops_it(win, app, tmp_path, slow_check):
    entered, release = slow_check
    ctl, ws, _clock = _queue_of(win, tmp_path)
    assert ctl.arm()
    show_run(win, app)
    scr = ws.screen
    scr.toggle_queue()
    assert ws.runner.state == "confirm"
    t0 = time.monotonic()
    scr.confirm_sheet()
    assert time.monotonic() - t0 < 1.0, "the queue's start blocked the GUI thread"
    assert ws.runner.state == "starting" and ws.runner.checking
    assert wait(app, entered.is_set, 30) and win.safety_ui.preflight_busy
    # the event loop keeps turning while the check is in flight: no turn held up, and no stretch
    # without a timer beat longer than two consecutive turns (safety_helpers)
    longest, gap, two_turns, beats = loop_heartbeat(app)
    assert entered.is_set() and not release.is_set() and ws.runner.checking   # still in flight
    assert longest < 0.3, f"the event loop was frozen while the sheet was checked ({longest:.3f} s)"
    assert gap <= two_turns, (f"the event loop was frozen while the sheet was checked: no beat for "
                              f"{gap * 1000:.0f} ms, two turns {two_turns * 1000:.0f} ms ({beats} beats)")
    assert not scr.buttons["confirm_sheet"].isEnabled()           # one start at a time
    win.safety_ui.stop()                                           # the on-screen Stop
    release.set()
    assert win.safety_ui.wait_preflight(30)
    assert wait(app, lambda: ws.runner.state == "confirm", 10), ws.runner.state
    pump(app, 0.2)
    assert not ctl.job_active() and not ws.runner.checking
    assert ws.runner.message == "queue.msg.start_refused"
    assert not ctl._keep_armed_next                               # withdrawn


def test_leaving_the_queue_while_the_sheet_is_checked_starts_nothing(win, app, tmp_path, slow_check):
    entered, release = slow_check
    ctl, ws, _clock = _queue_of(win, tmp_path)
    assert ctl.arm()
    show_run(win, app)
    scr = ws.screen
    scr.toggle_queue()
    scr.confirm_sheet()
    assert wait(app, entered.is_set, 30)
    scr.toggle_queue()                                             # Leave queue
    assert ws.runner.state == "idle" and not ctl._keep_armed_next
    release.set()
    assert win.safety_ui.wait_preflight(30)
    pump(app, 0.3)
    assert not ctl.job_active() and ws.runner.state == "idle"


def test_one_arming_question_for_the_whole_queue(win, app, tmp_path, monkeypatch):
    """Disarmed: the first sheet asks once ("Arm and start"), after the check; the next sheet
    starts without a question; the machine is disarmed after the last sheet."""
    ctl, ws, clock = _queue_of(win, tmp_path, n_sheets=2)
    assert not ctl.safety().armed
    asked: list = []
    monkeypatch.setattr(win.safety_ui, "ask_arm_and_start", lambda: asked.append(1) or True)
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 1)    # past the "material unverified" warning
    win._test_no_dialogs = False
    show_run(win, app)
    scr = ws.screen
    scr.toggle_queue()
    for sheet in range(2):
        scr.confirm_sheet()
        assert wait(app, lambda: ws.runner.state == "running", 30), (sheet, ws.runner.message)
        assert ctl.safety().armed
        clock[0] += 10000.0                        # the demo machine finishes the path
        ctl.state()
        assert wait(app, lambda: ws.runner.state != "running", 10)
    assert asked == [1]
    assert ws.runner.state == "idle" and ws.runner.message == "queue.msg.finished"
    assert not ctl.safety().armed
