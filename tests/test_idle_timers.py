"""Idle wake-ups (UI evaluation F12): display timers stop while the window is minimised and
start again when it is restored; the machine-watching timers keep running; the job inbox is
watched instead of listed on the GUI thread every 500 ms, and a submission still arrives."""

from __future__ import annotations

import time

import pytest


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import dispose, real_window
    w = real_window()
    w.resize(1100, 760)
    w.show()
    app.processEvents()
    yield w
    dispose(w)


def pump(app, cond=lambda: False, sec=1.0):
    t = time.time()
    while time.time() - t < sec and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


def test_display_timers_pause_when_minimised_and_resume(win, app):
    pause = win.idle_pause
    timers = {"status": win._timer, "run": win.extension("run").tick_timer}
    timers.update({f"other{i}": e.timer for i, e in enumerate(pause._entries) if e.timer not in timers.values()})
    assert all(t.isActive() for t in timers.values())
    diag = win.extension("ext_diag")
    diag = diag[0] if isinstance(diag, tuple) else diag
    watch = getattr(diag, "timer", None)                     # diagnostics: watches the machine
    polls = []
    entry = next(e for e in pause._entries if e.timer is win._timer)
    real_poll = entry.on_resume
    entry.on_resume = lambda: (polls.append(1), real_poll())[1]
    win.showMinimized()
    assert pump(app, lambda: pause.hidden)
    assert not any(t.isActive() for t in timers.values()), {k: t.isActive() for k, t in timers.items()}
    if watch is not None:
        assert watch.isActive()                              # not a display timer: keeps running
    win.showNormal()
    assert pump(app, lambda: not pause.hidden)
    assert all(t.isActive() for t in timers.values())
    assert polls                                             # the status is brought up to date at once
    # a timer that was stopped on purpose stays stopped across a minimise
    win.extension("run").tick_timer.stop()
    win.showMinimized()
    pump(app, lambda: pause.hidden)
    win.showNormal()
    pump(app, lambda: not pause.hidden)
    assert not win.extension("run").tick_timer.isActive()


def test_status_poll_runs_on_during_a_job(win, app):
    pause = win.idle_pause
    win._last_mode = "process"                               # a job runs: the head's track is recorded
    win.showMinimized()
    pump(app, lambda: pause.hidden)
    assert win._timer.isActive()
    win.showNormal()
    pump(app, lambda: not pause.hidden)


def test_hidden_pause_ignores_timers_of_a_window_never_shown(app):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QWidget

    from mlaser.ui.idle import HiddenPause
    w = QWidget()
    p = HiddenPause(w)
    t = QTimer(w)
    t.start(1000)
    p.add(t)
    app.processEvents()
    assert t.isActive() and not p.hidden
    w.show()
    w.hide()
    app.processEvents()
    assert p.hidden and not t.isActive()
    w.show()
    app.processEvents()
    assert t.isActive()
    w.deleteLater()


def test_inbox_is_watched_and_a_submission_arrives(win, app, tmp_path, monkeypatch):
    run = win.extension("run")
    assert run._inbox_watch is not None
    scans = []
    real = run.intake.scan
    monkeypatch.setattr(run.intake, "scan", lambda: (scans.append(1), real())[1])
    pump(app, sec=0.3)
    scans.clear()
    for _ in range(5):                                       # the timer tick no longer lists the inbox
        run._timer_tick()
    assert not scans
    drawing = tmp_path / "part.dxf"
    drawing.write_text("0\nEOF\n")
    import threading
    out = []
    th = threading.Thread(target=lambda: out.append(run.intake.submit({"path": str(drawing), "name": "w1"}, "api")))
    th.start()
    th.join()
    (job,) = out
    assert pump(app, lambda: any(j.id == job.id for j in run.intake.pending), sec=10.0)
    assert scans                                             # picked up by the watcher's scan
