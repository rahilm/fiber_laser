"""Opening files off the GUI thread (mlaser.ui.import_worker): progress dialog, Cancel, the
GUI keeps running, and a refused file is a clear message (offscreen)."""
from __future__ import annotations

import os
import threading
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mlaser.io import limits as LIM  # noqa: E402
from mlaser.io.limits import ImportCancelled  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def windows():
    """Main windows a test builds, closed and deleted after it: a window's background compile
    (JobService, under ``responsive_gil``) must not run on into the next test."""
    made: list = []
    yield made
    from wiring import dispose
    for w in made:
        dispose(w)


def _window(tmp_path, made=None):
    from mlaser.services.config import ConfigService
    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import EXTENSIONS, MainWindow

    cfg = ConfigService()
    cfg.set("app_dir", str(tmp_path))
    w = MainWindow(DemoController(), extensions=EXTENSIONS, config=cfg)
    w._test_no_dialogs = True
    if made is not None:
        made.append(w)
    return w


def _slow(stop_after_s: float = 30.0):
    """A loader that counts forever (like a huge drawing) until Cancel or the timeout."""
    def fn():
        b = LIM.Budget(10**12, "entities", "{limit} {what}")
        t0 = time.monotonic()
        while time.monotonic() - t0 < stop_after_s:
            b.add(64)
            time.sleep(0.0005)
        return "finished"
    return fn


def test_cancel_stops_the_import_and_the_gui_keeps_running(qapp):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QProgressDialog

    from mlaser.ui.import_worker import run_import
    ticks = []
    t = QTimer()
    t.timeout.connect(lambda: ticks.append(time.monotonic()))
    t.start(20)
    seen = {}

    def press_cancel():
        dlgs = [w for w in QApplication.topLevelWidgets() if isinstance(w, QProgressDialog) and w.isVisible()]
        seen["dialog"] = bool(dlgs)
        if dlgs:
            seen["label"] = dlgs[0].labelText()
            from PySide6.QtWidgets import QPushButton
            [b for b in dlgs[0].findChildren(QPushButton) if b.text() == "Cancel"][0].click()
        else:
            QTimer.singleShot(100, press_cancel)
    QTimer.singleShot(700, press_cancel)
    before = {th.name for th in threading.enumerate()}
    t0 = time.monotonic()
    with pytest.raises(ImportCancelled):
        run_import(None, _slow(), "Opening big.dxf...")
    dt = time.monotonic() - t0
    t.stop()
    assert seen.get("dialog"), "the progress dialog was shown"
    assert "entities" in seen.get("label", "")
    assert dt < 5
    assert len(ticks) > 10, "the GUI event loop ran while the file was read"
    deadline = time.monotonic() + 5
    while any(th.name == "mlaser-import" and th.is_alive() for th in threading.enumerate()) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not any(th.name == "mlaser-import" and th.is_alive() for th in threading.enumerate()
                   if th.name not in before), "the worker stopped at its next budget check"


def test_result_errors_and_one_import_at_a_time(qapp):
    from mlaser.ui.import_worker import ImportBusy, run_import
    assert run_import(None, lambda: 42) == 42

    def bad():
        raise ValueError("broken file")
    with pytest.raises(ValueError, match="broken file"):
        run_import(None, bad)

    def nested():
        return run_import(None, lambda: 1)
    with pytest.raises(ImportBusy):
        run_import(None, nested)


def test_load_path_refuses_a_dxf_bomb_with_a_message(qapp, tmp_path, windows):
    ezdxf = pytest.importorskip("ezdxf")
    from PySide6.QtWidgets import QMessageBox

    d = ezdxf.new()
    b = d.blocks.new("B0")
    b.add_line((0, 0), (1, 0))
    for k in range(1, 9):
        b = d.blocks.new(f"B{k}")
        for i in range(10):
            b.add_blockref(f"B{k - 1}", (i * 2.0 ** k, 0))
    d.modelspace().add_blockref("B8", (0, 0))
    p = tmp_path / "bomb.dxf"
    d.saveas(p)
    w = _window(tmp_path, windows)
    logged = []
    orig = w.log_panel.log
    w.log_panel.log = lambda m, *a, **k: (logged.append(str(m)), orig(m, *a, **k))
    before = w.document
    w._test_no_dialogs = False
    t0 = time.monotonic()
    assert w.load_path(str(p)) is False
    assert time.monotonic() - t0 < 10
    assert w.document is before
    assert any("more than 2,000,000 entities" in m for m in logged)
    boxes = [c for c in w.findChildren(QMessageBox) if c.isVisible()]
    assert boxes and "bomb.dxf" in boxes[0].text()
    for bx in boxes:
        bx.close()
    w._test_no_dialogs = True


def test_load_path_still_opens_files(qapp, tmp_path, windows):
    from mlaser.model import Contour, Document, Line, Part
    from mlaser.project import save_project
    w = _window(tmp_path, windows)
    pts = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    doc = Document([Part([Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)])])
    p = tmp_path / "ok.mlz"
    save_project(p, doc)
    assert w.load_path(str(p)) is True
    assert len(w.document.parts) == 1


def test_gui_wait_lowers_the_switch_interval(qapp):
    """While the progress dialog waits for the worker, the GIL switch interval is 0.5 ms (the
    GUI thread starved on macOS CI with the default 5 ms: 3 ticks of a 20 ms timer in 1 s), and
    it is restored afterwards."""
    import sys

    from mlaser._sched import RESPONSIVE_SWITCH_S, responsive_holders
    from mlaser.ui.import_worker import run_import
    # the interval is process-wide and reference-counted (mlaser._sched): another thread's
    # responsive_gil (a window's compile, a stream) keeps it lowered until the last one ends,
    # so measure "restored" from a moment when nobody else holds it (Windows CI: a compile of
    # an earlier test's window was still running, 0.0005 after the import)
    t0 = time.monotonic()
    while responsive_holders() and time.monotonic() - t0 < 30:
        qapp.processEvents()
        time.sleep(0.01)
    assert responsive_holders() == 0
    before = sys.getswitchinterval()
    seen = []

    def fn():
        time.sleep(0.3)                     # past the first join: the dialog loop is waiting
        seen.append(sys.getswitchinterval())
        return 1
    assert run_import(None, fn) == 1
    assert seen and seen[0] <= RESPONSIVE_SWITCH_S + 1e-9
    assert responsive_holders() == 0
    assert sys.getswitchinterval() == before
