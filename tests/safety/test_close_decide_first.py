"""Closing decides first, then shuts down (UI evaluation F3 / F4).

Before: the plugins' close hooks, ``jobs.shutdown()`` and the settings flush ran from an event
filter *before* ``closeEvent`` asked "A job is running. Stop it and quit?"; answering No left
the window open with the job service closed (no more plans), the plugins closed (Run timer,
pendant, monitor) and ``NormalExit=1`` written (no crash-recovery prompt after a later crash).
Now nothing stops until the close is accepted, and an accepted close still halts the machine.
"""
from __future__ import annotations

import math
import time

import pytest

from safety_helpers import Watch, head_mm, rect_doc, start_and_run, stopped, wait_until

pytest.importorskip("PySide6")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _answer(w, *buttons, seen=None, before_click=None):
    """Click ``buttons`` in order on the close questions as they come up (offscreen, the window-
    modal box runs its own event loop); ``before_click()`` runs right before each click."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox
    todo = list(buttons)
    seen = seen if seen is not None else []
    tries = [0]

    def poll() -> None:
        tries[0] += 1
        for box in w.findChildren(QMessageBox, "mlaser_close_question"):
            if box.isVisible() and todo:
                seen.append({"text": box.text(), "info": box.informativeText(),
                             "modality": box.windowModality(),
                             "buttons": {box.standardButton(b): b.text() for b in box.buttons()}})
                if before_click is not None:
                    before_click()
                box.button(todo.pop(0)).click()
                break
        if todo and tries[0] < 2000:
            QTimer.singleShot(5, poll)
    QTimer.singleShot(0, poll)
    return seen


def _app_file_dir() -> None:
    """The machine's File folder with a softPara.ini from a clean exit (start-up sets 0)."""
    import os
    from pathlib import Path
    d = Path(os.environ["MLASER_APP_DIR"]) / "File"
    d.mkdir(parents=True, exist_ok=True)
    (d / "softPara.ini").write_bytes(b"[SC2000]\r\nNormalExit=1\r\n")


def _normal_exit(w) -> str:
    from mlaser.formats.scfile import read_ini
    ini = w.extension("ext_machine").ops.file_dir / "softPara.ini"
    return read_ini(ini).get("SC2000", {}).get("NormalExit", "")


def _pump(s: float) -> None:
    from PySide6.QtWidgets import QApplication
    t_end = time.monotonic() + s
    while time.monotonic() < t_end:
        QApplication.processEvents()
        time.sleep(0.005)


def test_declining_job_running_keeps_the_app_working_then_accepting_halts(qapp, make_ctl):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    from wiring import real_window
    ctl = make_ctl(process=False)
    _app_file_dir()
    w = real_window(controller=ctl)
    w.set_document(rect_doc())
    w.shell.mark_saved()                        # only the job question here
    closed = []
    w.hooks.add("close", lambda: closed.append(1), "test")
    assert _normal_exit(w) == "0"
    proj = w.extension("ext_project")
    start_and_run(ctl, run_s=0.3)
    w._test_no_dialogs = False
    seen = _answer(w, QMessageBox.No)
    assert w.close() is False
    assert len(seen) == 1 and seen[0]["modality"] == Qt.WindowModal
    # nothing was shut down
    assert closed == [] and not w._shut_down
    assert not w.jobs._closed
    assert _normal_exit(w) == "0", "NormalExit=1 written although the close was declined"
    assert w._timer.isActive() and proj.timer.isActive()
    assert ctl._sup.connected and ctl.sim.state.fifo_running, "the job was stopped although declined"
    # the application still plans and cuts: a new toolpath after an edit, then Start from the
    # window (the job service plans, the controller streams)
    w._test_no_dialogs = True
    assert ctl.stop().wait(10)
    assert wait_until(lambda: ctl.state().mode == "ready" and stopped(ctl.sim), 10) is not None
    # the controller's polled head position has caught up with the stopped card: Start plans
    # from it, and the start gate refuses a plan made from a stale head ("start again", a slow
    # runner's poll still had the decelerating head)
    def head_known() -> bool:
        st, (hx, hy) = ctl.state(), head_mm(ctl.sim)
        return math.dist((float(st.x), float(st.y)), (hx, hy)) < 0.01
    t_end = time.monotonic() + 10
    while not head_known() and time.monotonic() < t_end:
        _pump(0.02)
    assert head_known(), (ctl.state(), head_mm(ctl.sim))
    plans = []
    w.hooks.add("plan", plans.append, "test")
    # a real edit (the arrow keys move the selection; with nothing selected they do nothing and
    # no new plan comes: macOS CI then saw only a re-plan that had arrived before this point)
    w.run("select.all")
    x0 = w.document.parts[0].bbox()[0]
    w.nudge(1, 0)
    assert w.document.parts[0].bbox()[0] > x0, "the nudge did not move the drawing"
    t_end = time.monotonic() + 30
    while not plans and time.monotonic() < t_end:
        _pump(0.02)
    assert plans, "no toolpath after the declined close (the job service was shut down)"
    assert ctl.arm()          # the Stop above disarmed (a stopped cut, UX review finding 1)
    w.run_job("process")
    t_end = time.monotonic() + 30
    while not (ctl.sim.state.fifo_running and ctl.sim.state.laser) and time.monotonic() < t_end:
        _pump(0.02)
    assert ctl.sim.state.fifo_running and ctl.sim.state.laser, "Start from the window did not cut"
    _pump(0.3)
    # accepted (with that job running): halts the machine, then the hooks and NormalExit=1
    sim = ctl.sim
    ctl.disconnect = (lambda orig=ctl.disconnect: (lambda *a, **k: orig(*a, keep_sim=True, **k)))()
    w.shell.mark_saved()                        # the nudge above: only the job question here
    w._test_no_dialogs = False
    # the halt's time and travel count from the operator's Yes: while the question is up the
    # job rightly runs on (decide first), for as long as the runner takes to show and answer it
    # (Windows CI: 24 mm from before the question to the halt, not from the answer)
    watch = []
    _answer(w, QMessageBox.Yes, before_click=lambda: watch.append((Watch(sim), sim.state.fifo_running)))
    assert w.close() is True
    assert len(watch) == 1 and watch[0][1], "the job was not running when Yes was clicked"
    dt, travel = watch[0][0].until_safe(1.0)
    assert dt is not None and dt < 0.5 and travel < 20.0
    assert closed == [1] and w.jobs._closed and _normal_exit(w) == "1"
    sim.stop()


def test_cancelling_unsaved_changes_after_yes_to_job_running_stops_nothing(qapp, make_ctl):
    """Yes to "Stop it and quit?", then Cancel on Save / Don't Save / Cancel: the job goes on."""
    from PySide6.QtWidgets import QMessageBox

    from wiring import real_window
    ctl = make_ctl(process=False)
    w = real_window(controller=ctl)
    w.set_document(rect_doc())                  # an imported drawing, never saved: modified
    assert w.shell.dirty
    start_and_run(ctl, run_s=0.3)
    w._test_no_dialogs = False
    seen = _answer(w, QMessageBox.Yes, QMessageBox.Cancel)
    assert w.close() is False
    assert len(seen) == 2
    assert not w._shut_down and not w.jobs._closed and ctl.sim.state.fifo_running
    w._test_no_dialogs = True
    assert ctl.stop().wait(10)
    ctl.sim.stop()

