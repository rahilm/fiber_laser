"""Unsaved work (UI evaluation F3): the Save / Don't Save / Cancel question before the document
is closed or replaced, atomic writes of the user's files, Save and autosave off the GUI thread.

The window-modal question runs its own event loop offscreen: :func:`_answer` clicks it."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mlaser.model import Contour, Craft, Document, Line, Part  # noqa: E402
from stall_probe import StallProbe  # noqa: E402

# Qt widgets; the two GUI-latency bounds (``timing``) run in a timing group (tests/conftest.py)
pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _square(x=0.0, y=0.0, n=4, r=5.0):
    import math
    pts = [(x + r * math.cos(2 * math.pi * i / n), y + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    return Part([Contour([Line(pts[i], pts[(i + 1) % n]) for i in range(n)], True)], Craft(layer=1))


def _answer(w, *buttons, seen=None):
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
                box.button(todo.pop(0)).click()
                break
        if todo and tries[0] < 2000:
            QTimer.singleShot(5, poll)
    QTimer.singleShot(0, poll)
    return seen


def _app_file_dir() -> Path:
    d = Path(os.environ["MLASER_APP_DIR"]) / "File"
    d.mkdir(parents=True, exist_ok=True)
    (d / "softPara.ini").write_bytes(b"[SC2000]\r\nNormalExit=1\r\n")
    return d


def _normal_exit(w) -> str:
    from mlaser.formats.scfile import read_ini
    ini = w.extension("ext_machine").ops.file_dir / "softPara.ini"
    return read_ini(ini).get("SC2000", {}).get("NormalExit", "")


def _dirty_window():
    from wiring import real_window
    _app_file_dir()
    w = real_window()
    w.set_document(Document([_square(), _square(20)]))     # a drawing never saved: modified
    assert w.shell.dirty and w.isWindowModified()
    closed: list = []
    w.hooks.add("close", lambda: closed.append(1), "test")
    w._test_no_dialogs = False                # the questions are asked (and answered below)
    return w, closed


def _alive(w, closed) -> None:
    assert closed == [] and not w._shut_down and not w.jobs._closed
    assert _normal_exit(w) == "0"
    assert w._timer.isActive() and w.extension("ext_project").timer.isActive()


# ---------------------------------------------------------------------------- closing
def test_close_cancel_keeps_everything(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox
    w, closed = _dirty_window()
    seen = _answer(w, QMessageBox.Cancel)
    assert w.close() is False
    assert len(seen) == 1
    q = seen[0]
    assert "Untitled" in q["text"] and q["modality"] == Qt.WindowModal
    assert set(q["buttons"]) == {QMessageBox.Save, QMessageBox.Discard, QMessageBox.Cancel}
    assert q["buttons"][QMessageBox.Save] == "Save" and q["buttons"][QMessageBox.Cancel] == "Cancel"
    assert q["buttons"][QMessageBox.Discard] in ("Don’t save", "Close without saving")
    _alive(w, closed)
    assert w.isWindowModified() and len(w.document.parts) == 2
    w._test_no_dialogs = True
    w.close()


def test_close_dont_save_closes(qapp):
    from PySide6.QtWidgets import QMessageBox
    w, closed = _dirty_window()
    _answer(w, QMessageBox.Discard)
    assert w.close() is True
    assert closed == [1] and w._shut_down and w.jobs._closed and _normal_exit(w) == "1"


def test_close_save_saves_then_closes(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from mlaser.project import load_project
    w, closed = _dirty_window()
    target = tmp_path / "plate.mlz"
    asked = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (asked.append(1), (str(target), ""))[1]))
    _answer(w, QMessageBox.Save)
    assert w.close() is True
    assert asked == [1]                                   # untitled: Save As
    assert len(load_project(target).document.parts) == 2
    assert closed == [1] and _normal_exit(w) == "1"


def test_close_save_cancelled_or_failed_cancels_the_close(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    w, closed = _dirty_window()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    _answer(w, QMessageBox.Save)
    assert w.close() is False                             # the Save dialog was cancelled
    _alive(w, closed)
    # a save that fails (a folder that cannot be written) keeps the window open too
    bad = tmp_path / "not_a_folder"
    bad.write_bytes(b"")
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(bad / "y.mlz"), "")))
    _answer(w, QMessageBox.Save)
    assert w.close() is False
    _alive(w, closed)
    assert w.isWindowModified()
    w._test_no_dialogs = True
    w.close()


def test_close_save_writes_chf_back_with_the_same_bytes(qapp, tmp_path):
    from PySide6.QtWidgets import QMessageBox

    from mlaser.cam.raster_engraving import expand_document
    from mlaser.formats.chf import dumps_chf, save_chf
    from wiring import real_window
    p = tmp_path / "part.chf"
    save_chf(Document([_square(), _square(20)]), str(p))
    w = real_window()
    assert w.load_path(str(p))
    assert not w.shell.dirty
    w.run("select.all")
    w.nudge(1, 0)
    assert w.shell.dirty
    want = dumps_chf(expand_document(w.document))
    w._test_no_dialogs = False
    _answer(w, QMessageBox.Save)
    assert w.close() is True
    assert p.read_bytes() == want                          # the bytes of the live document


def test_clean_document_closes_without_a_question(qapp):
    from wiring import real_window
    w = real_window()
    w._test_no_dialogs = False
    assert not w.shell.dirty
    assert w.close() is True


# ---------------------------------------------------------------------------- New / Open
def test_new_and_open_ask_first(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from mlaser.formats.chf import save_chf
    other = tmp_path / "other.chf"
    save_chf(Document([_square(50)]), str(other))
    w, closed = _dirty_window()
    before = w.document
    # File > New: Cancel keeps the drawing, Don't Save replaces it
    seen = _answer(w, QMessageBox.Cancel)
    w.file_new()
    assert len(seen) == 1 and w.document is before
    # File > Open: the question comes before the file dialog
    opened = []
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (opened.append(1), (str(other), ""))[1]))
    _answer(w, QMessageBox.Cancel)
    w.file_open()
    assert opened == [] and w.document is before
    # a file from the OS (Finder, the Dock, a recent drawing): load_path asks
    _answer(w, QMessageBox.Cancel)
    assert w.load_path(str(other)) is False and w.document is before
    if w.shell.native is not None:
        _answer(w, QMessageBox.Cancel)
        assert w.shell.native.open_file(str(other)) is False and w.document is before
    # Don't Save: opened, no second question
    seen = _answer(w, QMessageBox.Discard)
    w.file_open()
    assert opened == [1] and len(seen) == 1 and len(w.document.parts) == 1
    assert not w.shell.dirty
    # a clean document: no question
    w.file_new()
    assert w.document.parts == []
    _alive(w, closed)
    w._test_no_dialogs = True
    w.close()


def test_import_merges_without_a_question(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from mlaser.formats.chf import save_chf
    other = tmp_path / "other.chf"
    save_chf(Document([_square(50)]), str(other))
    w, _closed = _dirty_window()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(other), "")))
    w.file_import()                                       # would hang on a question
    assert len(w.document.parts) == 3
    w._test_no_dialogs = True
    w.close()


# ---------------------------------------------------------------------------- atomic writes
_CRASH = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {root!r})
    from mlaser._atomic import atomic_open
    with atomic_open({path!r}, "wb") as f:
        f.write(b"NEW" * 100000)
        f.flush()
        os._exit(3)                       # the power goes in the middle of the write
""")


def test_a_crash_mid_write_leaves_the_old_file(tmp_path):
    p = tmp_path / "drawing.chf"
    p.write_bytes(b"OLD contents")
    root = str(Path(__file__).resolve().parents[1])
    r = subprocess.run([sys.executable, "-c", _CRASH.format(root=root, path=str(p))], timeout=60)
    assert r.returncode == 3
    assert p.read_bytes() == b"OLD contents"


@pytest.mark.parametrize("what", ["chf", "nest", "task", "ini", "mlz", "csv", "gcode"])
def test_user_files_are_replaced_atomically(tmp_path, monkeypatch, what):
    """Every writer goes through mlaser._atomic: a failure before the rename (a crash, a full
    disk) leaves the old file as it was, and no temporary file."""
    import mlaser._atomic as A
    from mlaser.formats.chf import save_chf
    p = tmp_path / f"user.{what}"
    p.write_bytes(b"OLD")
    if os.name == "posix":
        os.chmod(p, 0o640)
    doc = Document([_square()])

    def write() -> None:
        if what == "chf":
            save_chf(doc, str(p))
        elif what == "nest":
            from mlaser.nest.model import NestJob
            from mlaser.nest.project import save_project
            save_project(NestJob(), p)
        elif what == "task":
            from mlaser.formats.backup import save_task
            save_task(p.with_suffix(".aut"), doc)
        elif what == "ini":
            from mlaser.formats.scfile import write_ini
            write_ini({"SC2000": {"NormalExit": "1"}}, p)
        elif what == "mlz":
            from mlaser.project import save_project
            save_project(p, doc)
        elif what == "csv":
            from mlaser.report.ledger import write_csv
            write_csv(str(p), ["a"], [[1]])
        elif what == "gcode":
            from mlaser.io.gcode import write_gcode
            write_gcode(str(p), doc)
    if what == "task":
        p = p.with_suffix(".aut")
        p.write_bytes(b"OLD")
        if os.name == "posix":
            os.chmod(p, 0o640)

    def boom(tmp, target):
        raise KeyboardInterrupt("crash before the rename")
    monkeypatch.setattr(A, "_replace", boom)
    with pytest.raises(KeyboardInterrupt):
        write()
    assert p.read_bytes() == b"OLD"
    assert [f.name for f in tmp_path.iterdir() if f.name.endswith(".tmp")] == []
    monkeypatch.undo()
    write()
    assert p.read_bytes() != b"OLD"
    if os.name == "posix":
        assert stat.S_IMODE(p.stat().st_mode) == 0o640       # the old file's permissions kept


def test_save_chf_bytes_unchanged(tmp_path):
    from mlaser.formats.chf import dumps_chf, save_chf
    doc = Document([_square(), _square(20, n=7)])
    save_chf(doc, str(tmp_path / "a.chf"))
    assert (tmp_path / "a.chf").read_bytes() == dumps_chf(doc)


def test_new_file_gets_the_usual_permissions(tmp_path):
    from mlaser._atomic import _UMASK, atomic_write
    atomic_write(tmp_path / "n.bin", b"x")
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "n.bin").stat().st_mode) == 0o666 & ~_UMASK


def test_autosave_tickets_never_let_an_older_state_win(tmp_path):
    from mlaser.project import Autosave, load_project
    st = Autosave(tmp_path)
    old = st.reserve()
    new = st.reserve()
    assert st.save(Document([_square(), _square(20)]), ticket=new)
    assert st.save(Document([_square()]), ticket=old) == []          # older snapshot: skipped
    assert len(load_project(st.project_path).document.parts) == 2
    assert st.save(Document([_square()]))                             # now: written
    assert len(load_project(st.project_path).document.parts) == 1


# ---------------------------------------------------------------------------- off the GUI thread
def _big_document(n: int = 2700) -> Document:
    cols = 60
    return Document([_square((i % cols) * 12.0, (i // cols) * 12.0, n=24) for i in range(n)])


def _max_gap_while(qapp, busy, timeout_s: float = 60.0, probe=None) -> float:
    """The longest the GUI thread went without running a 5 ms timer while ``busy()``
    (``probe``: a :class:`stall_probe.StallProbe` given the timer's beats, diagnostics only)."""
    from PySide6.QtCore import QTimer
    stamps = [time.perf_counter()]
    t = QTimer()
    t.setInterval(5)
    t.timeout.connect(lambda: (stamps.append(time.perf_counter()), probe and probe.beat()))
    t.start()
    t_end = time.monotonic() + timeout_s
    while busy() and time.monotonic() < t_end:
        qapp.processEvents()
        time.sleep(0.001)
    t.stop()
    stamps.append(time.perf_counter())
    return max(b - a for a, b in zip(stamps, stamps[1:]))


def _big_window(qapp):
    """A window with the large drawing, idle: its preview plan computed and drawn (the plan's
    snapshot and the canvas rebuild run on the GUI thread, UI evaluation F2, not measured here)."""
    from wiring import real_window
    w = real_window()
    plans: list = []
    w.hooks.add("plan", plans.append, "test")
    w.set_document(_big_document())
    t_end = time.monotonic() + 120
    while not plans and time.monotonic() < t_end:
        qapp.processEvents()
        time.sleep(0.005)
    _settle(qapp)
    return w


def _settle(qapp, quiet_s: float = 1.0, timeout_s: float = 60.0) -> None:
    last = time.perf_counter()
    quiet_since = last
    t_end = time.monotonic() + timeout_s
    while time.monotonic() < t_end:
        qapp.processEvents()
        time.sleep(0.005)
        now = time.perf_counter()
        if now - last > 0.05:
            quiet_since = now
        last = now
        if now - quiet_since >= quiet_s:
            return


@pytest.mark.timing
def test_autosave_does_not_block_the_gui_thread(qapp):
    file_dir = _app_file_dir()
    w = _big_window(qapp)
    proj = w.extension("ext_project")
    proj._dirty = True
    assert proj.autosave_async() is True
    with StallProbe() as probe:
        gap = _max_gap_while(qapp, lambda: proj._autosave_busy, probe=probe)
    assert not proj._autosave_busy, "the autosave did not finish"
    if gap >= 0.2:                            # the best of two (see the Save test)
        proj._dirty = True
        assert proj.autosave_async() is True
        with StallProbe() as probe2:
            gap2 = _max_gap_while(qapp, lambda: proj._autosave_busy, probe=probe2)
        if gap2 < gap:
            gap, probe = gap2, probe2
    assert gap < 0.2, f"the GUI thread stalled {gap * 1000:.0f} ms during the autosave\n{probe.report()}"
    assert not proj._dirty
    rec = proj.autosave_store().recover()                    # crash recovery reads it back
    assert rec is not None and len(rec.document.parts) == 2700
    assert (file_dir / "autosave.chf").exists()
    # an edit while the snapshot is taken is the next autosave's
    proj._dirty = True
    assert proj.autosave_async() is True
    qapp.processEvents()
    w.run("select.all")
    w.nudge(1, 0)
    _max_gap_while(qapp, lambda: proj._autosave_busy)
    assert proj._dirty
    w.close()


@pytest.mark.timing
def test_save_mlz_does_not_block_the_gui_thread(qapp, tmp_path):
    from mlaser.project import load_project
    from PySide6.QtCore import QTimer
    w = _big_window(qapp)
    proj = w.extension("ext_project")

    def save_gap(name: str) -> float:
        stamps: list = []
        t = QTimer()
        t.setInterval(5)
        probe = StallProbe()
        t.timeout.connect(lambda: (stamps.append(time.perf_counter()), probe.beat()))
        with probe:
            t.start()
            t0 = time.perf_counter()
            assert proj.save(str(tmp_path / name))
            stamps = [t0] + stamps + [time.perf_counter()]
            t.stop()
        return max(b - a for a, b in zip(stamps, stamps[1:])), probe
    # the best of two: a loaded CI machine can stall any process for a moment; a Save that
    # serialised on the GUI thread stalls it 0.8 s every time
    gap, probe = min(save_gap("big.mlz"), save_gap("big2.mlz"), key=lambda r: r[0])
    assert gap < 0.2, f"the GUI thread stalled {gap * 1000:.0f} ms during Save\n{probe.report()}"
    assert len(load_project(tmp_path / "big.mlz").document.parts) == 2700
    assert not w.shell.dirty
    w.close()


def test_saving_seals_the_undo_stack(qapp, tmp_path):
    """After Save, the next edit is a new undo step (UndoStack.seal, where it exists)."""
    from wiring import real_window
    w = real_window()
    w.set_document(Document([_square()]))
    sealed = []
    w.docs.stack.seal = lambda: sealed.append(1)
    assert w.extension("ext_project").save(str(tmp_path / "s.mlz"))
    assert sealed == [1]
    w.close()


def test_document_json_written_per_part_is_byte_identical():
    from mlaser.project.codec import Blobs, Encoder
    from mlaser.project.format import _dump, _dump_document
    for doc in (Document(), Document([_square()]), _big_document(50)):
        dd = Encoder(Blobs()).document(doc)
        assert _dump_document(dd) == _dump(dd)
