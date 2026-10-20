"""STOP, Hold, Start and Arm keep their touch size and stay visible through every user-mode and
theme switch (UI audit Top 15 #1: after Operator mode, a style-sheet ``min-height`` rule that
stopped matching reset the run bar's and the Run screen's buttons from 56 px to 22 px until the
application restarted).  docs/SAFETY.md §10, docs/dev/RESPONSIVE.md."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton, QWidget  # noqa: E402

TARGET = 56
SAFETY = ("stop", "hold", "start", "arm")


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from wiring import real_window
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    w = real_window()
    w.resize(1512, 875)
    w.show()
    app.processEvents()
    yield w
    from wiring import dispose
    dispose(w)


def _pump(app, n: int = 5) -> None:
    for _ in range(n):
        app.processEvents()


def _check(win, label: str) -> list[str]:
    bad = []
    bar = win.findChild(QWidget, "run_bar")
    for key in SAFETY:
        b = bar.findChild(QPushButton, f"run_{key}")
        if b is None or not b.isVisible():
            bad.append(f"{label}: run bar {key} hidden")
            continue
        r = b.rect().translated(b.mapTo(win, b.rect().topLeft()))
        if b.height() < TARGET or not win.rect().contains(r):
            bad.append(f"{label}: run bar {key} {b.width()}x{b.height()} at {r}")
    if win.shell.host.current == "run":
        scr = win.extension("run").screen
        for key in SAFETY:
            b = scr.buttons[key]
            if not b.isVisible() or b.height() < TARGET:
                bad.append(f"{label}: run screen {key} {b.width()}x{b.height()}")
    return bad


def test_safety_targets_survive_mode_and_theme_switches(win, app):
    bad = []
    for ws in ("design", "run"):
        win.shell.host.activate(ws)
        _pump(app)
        bad += _check(win, f"{ws} start")
        for mode in ("operator", "engineer", "service", "operator", "service"):
            assert win.set_user_mode(mode) or mode == "service"
            _pump(app)
            if win.shell.host.current != ws:
                win.shell.host.activate(ws)       # Operator mode may switch to Run
                _pump(app)
            bad += _check(win, f"{ws} mode {mode}")
        for theme in ("dark", "high_contrast", "light"):
            win.shell.set_theme(theme, persist=False)
            _pump(app)
            bad += _check(win, f"{ws} theme {theme}")
    assert not bad, "\n".join(bad)
