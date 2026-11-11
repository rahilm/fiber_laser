"""Arraying a large selection: progress, Cancel, one undo step (UX review 2026-11: the copies
were made on the GUI thread, the window did not repaint and nothing could stop it).

The copies are made in a worker while the window's event loop runs (a progress dialog with
Cancel after a moment); the result is the same as before and is added in one undo step; Cancel
adds nothing and says so."""
from __future__ import annotations

import copy
import threading

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QProgressDialog, QPushButton

from mlaser import ops
from mlaser.cam import array as A
from mlaser.ops import cam as OC
from wiring import dispose, real_window


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app):
    w = real_window()            # exec_dialogs=False: the tool dialogs apply their stored values
    w.resize(1400, 900)
    w.show()
    app.processEvents()
    yield w
    dispose(w)


def _cam(w):
    ext = w.extension("ext_cam")
    return ext[0] if isinstance(ext, tuple) else ext


def _boxes(parts):
    return [tuple(round(v, 9) for v in p.bbox()) for p in parts]


def test_array_is_the_same_and_one_undo_step(win, app):
    win.set_document(ops.demo_document())
    win.canvas.select_parts([0, 1])
    app.processEvents()
    cam = _cam(win)
    cam.array.rows, cam.array.cols = 3, 4
    expected = copy.deepcopy(win.document)
    OC.rect_array(expected, cam.array)
    n0 = len(win.document.parts)
    cam.array_dialog()
    app.processEvents()
    assert len(win.document.parts) == len(expected.parts) == n0 + 2 * 11
    assert _boxes(win.document.parts) == _boxes(expected.parts)
    assert [p.extra.get("array_cell") for p in win.document.parts] == \
        [p.extra.get("array_cell") for p in expected.parts]
    win.run("edit.undo")
    app.processEvents()
    assert len(win.document.parts) == n0


def test_circular_array_is_the_same(win, app):
    win.set_document(ops.demo_document())
    win.canvas.select_parts([0])
    app.processEvents()
    cam = _cam(win)
    cam.circ.count = 5
    expected = copy.deepcopy(win.document)
    OC.circular_array(expected, cam.circ)
    cam.circular_array_dialog()
    app.processEvents()
    assert _boxes(win.document.parts) == _boxes(expected.parts)
    win.run("edit.undo")
    app.processEvents()
    assert len(win.document.parts) == len(ops.demo_document().parts)


def test_cancel_adds_nothing_and_the_window_runs_meanwhile(win, app, monkeypatch):
    """The worker stops at copy 10 until the GUI thread, running its event loop, presses Cancel
    on the progress dialog: no part is added, no undo step, the status bar says so."""
    win.set_document(ops.demo_document())
    win.run("select.all")
    app.processEvents()
    cam = _cam(win)
    cam.array.rows, cam.array.cols = 20, 20
    n0 = len(win.document.parts)
    undo0 = win.ribbon.actions["edit.undo"].isEnabled(), win.docs.revision
    at_ten, pressed = threading.Event(), threading.Event()
    made = []
    real = A._translated

    def slow(p, dx, dy):
        made.append(1)
        if len(made) == 10:
            at_ten.set()
            pressed.wait(30)
        return real(p, dx, dy)
    monkeypatch.setattr(A, "_translated", slow)
    ticks = []

    def poll() -> None:
        ticks.append(1)
        if at_ten.is_set():
            for d in win.findChildren(QProgressDialog):
                if d.objectName() == "progress_task":
                    d.findChild(QPushButton).click()       # the dialog's Cancel button
                    pressed.set()
                    return
        QTimer.singleShot(5, poll)
    QTimer.singleShot(0, poll)
    assert cam._copies("mf79", "array", lambda pr: A.rect_array(list(win.document.parts), cam.array, pr)) is None
    app.processEvents()
    assert ticks and pressed.is_set()                     # the event loop ran during the work
    assert 10 <= len(made) < 399 * n0
    assert len(win.document.parts) == n0
    assert (win.ribbon.actions["edit.undo"].isEnabled(), win.docs.revision) == undo0
    assert win.statusBar().currentMessage() == "Array cancelled: nothing was added."


def test_rect_array_progress_and_stop():
    doc = ops.demo_document()
    prm = A.ArrayParams(rows=2, cols=3)
    seen = []
    out = A.rect_array(doc.parts, prm, lambda done, total: seen.append((done, total)) or True)
    assert len(out) == seen[-1][0] == seen[-1][1] == 5 * len(doc.parts)
    assert _boxes(out) == _boxes(A.rect_array(doc.parts, prm))
    with pytest.raises(A.ArrayCancelled):
        A.rect_array(doc.parts, prm, lambda done, total: done < 3)
