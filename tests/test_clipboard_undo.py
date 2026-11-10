"""Clipboard, drag and drop, named and coalesced undo, Edit / File / Window menu conventions
(UI evaluation F9 / F10; mlaser/ui/clipboard.py, mlaser/ui/undo_names.py, mlaser/services/undo.py,
mlaser/ui/shell/menu_spec.py, mlaser/ui/shell/keymap.py)."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.ui

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDropEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox  # noqa: E402

from mlaser import ops  # noqa: E402
from mlaser.model import Document  # noqa: E402
from mlaser.services.document import DocumentService  # noqa: E402
from mlaser.services.undo import UndoStack  # noqa: E402
from mlaser.ui import clipboard  # noqa: E402
from mlaser.ui.shell import keymap, menu_spec  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch):
    from wiring import dispose, real_window
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window(user_mode="engineer")
    w.show()
    app.processEvents()
    QApplication.clipboard().clear()
    yield w
    QApplication.clipboard().clear()
    dispose(w)


def _demo(win, select=(0, 1)):
    win.set_document(ops.demo_document())
    win.canvas.select_parts(list(select))
    return [p for p in win.document.parts if p.selected]


def _geometry(parts):
    return [[(c.closed, [(type(s).__name__, tuple(s.start), tuple(s.end)) for s in c.segments]) for c in p.contours]
            for p in parts]


def _shift(parts, dx, dy):
    import copy
    out = [copy.deepcopy(p) for p in parts]
    ops.translate(Document(parts=out), dx, dy, out)
    return out


# --------------------------------------------------------------------------- clipboard
def test_copy_puts_every_representation_on_the_system_clipboard(win, app):
    sel = _demo(win)
    win.run("edit.copy")
    md = QApplication.clipboard().mimeData()
    assert md.hasFormat(clipboard.MIME_PARTS)
    assert md.hasFormat(clipboard.MIME_SVG) and md.text().lstrip().startswith("<svg")
    # the DXF follows from a worker a moment later (ui/clipboard.copy_document)
    from PySide6.QtCore import QDeadlineTimer, QEventLoop

    def has_dxf() -> bool:
        return any(QApplication.clipboard().mimeData().hasFormat(m) for m in clipboard.MIME_DXF)
    deadline = QDeadlineTimer(20000)
    while not has_dxf() and not deadline.hasExpired():
        app.processEvents(QEventLoop.WaitForMoreEvents, 50)
    assert has_dxf(), "no DXF on the clipboard"
    md = QApplication.clipboard().mimeData()
    assert any(md.hasFormat(m) for m in clipboard.MIME_DXF)
    back = clipboard.document_from_mime(md)
    assert len(back.parts) == len(sel)
    # the native format round-trips exactly (geometry, crafts, layers)
    for a, b in zip(sel, back.parts):
        assert _geometry([a]) == _geometry([b]) and a.craft == b.craft and a.name == b.name
    assert set(back.layers) == {p.craft.layer for p in sel}


def test_paste_round_trip_through_qclipboard(win, app):
    sel = _demo(win)
    n0 = len(win.document.parts)
    win.run("edit.copy")
    undo_before = len(win.docs.stack)
    win.run("edit.paste")
    assert len(win.document.parts) == n0 + len(sel)
    pasted = [p for p in win.document.parts if p.selected]
    assert _geometry(pasted) == _geometry(_shift(sel, 10.0, 10.0))     # offset 10 mm, else exact
    assert [p.craft for p in pasted] == [p.craft for p in sel]
    assert len({p.uid for p in win.document.parts}) == len(win.document.parts)
    assert len(win.docs.stack) == undo_before + 1
    assert win.ribbon.actions["edit.undo"].text().replace("&", "") == "Undo Paste"


def test_paste_between_two_windows(win, app):
    from wiring import dispose, real_window
    sel = _demo(win, select=(2,))
    win.run("edit.copy")
    other = real_window(user_mode="engineer")
    try:
        other.set_document(Document())
        other.run("edit.paste")
        assert _geometry(other.document.parts) == _geometry(_shift(sel, 10.0, 10.0))
        assert other.document.parts[0].craft == sel[0].craft
    finally:
        dispose(other)


def test_paste_plain_svg_and_dxf_from_other_programs(win, app):
    from mlaser.cli.export import svg_text
    src = ops.demo_document()
    win.set_document(Document())
    md = QMimeData()
    md.setData("image/svg+xml", svg_text(Document(parts=src.parts[:1])).encode())
    clipboard.set_clipboard(md)          # a Qt-made copy (a Python QMimeData crashes the exit)
    assert win.can_paste()
    win.run("edit.paste")
    assert len(win.document.parts) >= 1
    # DXF bytes only (a CAD program), then SVG as plain text (a text editor)
    win.set_document(Document())
    md = QMimeData()
    md.setData("image/vnd.dxf", clipboard.dxf_bytes(Document(parts=src.parts[:2])))
    clipboard.set_clipboard(md)          # a Qt-made copy (a Python QMimeData crashes the exit)
    win.run("edit.paste")
    assert len(win.document.parts) >= 2
    win.set_document(Document())
    QApplication.clipboard().setText(svg_text(Document(parts=src.parts[:1])))
    win.run("edit.paste")
    assert len(win.document.parts) >= 1
    # ordinary text is nothing to paste
    QApplication.clipboard().setText("hello")
    assert not win.can_paste()


def test_unreadable_clipboard_is_reported_not_raised(win, app):
    md = QMimeData()
    md.setData(clipboard.MIME_PARTS, b"not a project")
    clipboard.set_clipboard(md)          # a Qt-made copy (a Python QMimeData crashes the exit)
    win.set_document(Document())
    assert win.paste() == 0
    assert len(win.document.parts) == 0


def test_cut_is_copy_and_delete_in_one_undo_step(win, app):
    sel = _demo(win)
    n0 = len(win.document.parts)
    steps = len(win.docs.stack)
    win.run("edit.cut")
    assert len(win.document.parts) == n0 - len(sel)
    assert QApplication.clipboard().mimeData().hasFormat(clipboard.MIME_PARTS)
    assert len(win.docs.stack) == steps + 1
    assert win.ribbon.actions["edit.undo"].text().replace("&", "") == "Undo Cut"
    win.run("edit.undo")
    assert len(win.document.parts) == n0
    assert win.ribbon.actions["edit.redo"].text().replace("&", "") == "Redo Cut"
    win.run("edit.paste")
    assert len(win.document.parts) == n0 + len(sel)


_EXIT_SCRIPT = r"""
import os, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, os.path.join(sys.argv[1], "tests"))
from PySide6.QtWidgets import QApplication, QMenu
app = QApplication([])
from mlaser import ops
from wiring import real_window
w = real_window(user_mode="engineer")
w.show(); app.processEvents()
w.set_document(ops.demo_document())
w.canvas.select_parts([0, 1])
w.run("edit.copy"); w.run("edit.paste"); w.run("edit.cut")
w.nudge(1, 0); w.run("edit.undo")
w.part_list.model.mimeData([])                           # a drag's data
for m in w.shell.menubar.findChildren(QMenu):             # access keys, window list, recent
    m.aboutToShow.emit()
app.processEvents()
w.close()
app.processEvents()
assert QApplication.clipboard().mimeData().hasFormat("application/x-mlaser-parts")
print("ok", flush=True)
"""


def test_exit_is_clean_after_copy(tmp_path):
    """Regression: a Python-made QMimeData left on the clipboard crashed the interpreter when
    the application was destroyed (SIGSEGV after the last test; the copy is now a Qt-made
    QMimeData, clipboard.native_mime).  Copy, paste, cut, undo, a drag's data, every menu
    opened, the window closed: the process exits with 0, the parts still on the clipboard."""
    import os
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", MLASER_PLAN_PROCESS="0")
    for var in ("MLASER_CONFIG_DIR", "MLASER_APP_DIR", "MLASER_DATA_DIR"):
        env[var] = str(tmp_path / var)
    script = tmp_path / "exit_after_copy.py"
    script.write_text(_EXIT_SCRIPT)
    r = subprocess.run([sys.executable, "-X", "faulthandler", str(script), root], env=env, cwd=str(tmp_path),
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and "ok" in r.stdout, (r.returncode, r.stdout[-2000:], r.stderr[-4000:])


# --------------------------------------------------------------------------- drag and drop
def _drop(target, md, pos=None):
    """A drag entering ``target`` and dropped there (Qt delivers the drop to the widget that
    accepted the enter)."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QDragEnterEvent
    import warnings
    pos = pos or QPoint(target.width() // 2, target.height() // 2)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)      # Qt 6 has only the QPoint form
        enter = QDragEnterEvent(pos, Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(target, enter)
    assert enter.isAccepted()
    ev = QDropEvent(QPointF(pos), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(target, ev)
    return ev


def test_drop_of_a_drawing_file_on_the_canvas_imports_it(win, app, tmp_path):
    from mlaser.cli.export import write_svg
    win.set_document(ops.demo_document())
    n0 = len(win.document.parts)
    f = tmp_path / "drop.svg"
    write_svg(Document(parts=ops.demo_document().parts[:1]), str(f))
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(f))])
    ev = _drop(win.canvas.viewport(), md)
    assert ev.isAccepted()
    for _ in range(5):
        app.processEvents()
    assert len(win.document.parts) > n0                   # imported into the drawing


def test_drop_of_parts_on_the_canvas_pastes_them_there(win, app):
    sel = _demo(win, select=(0,))
    md = clipboard.mime_for(win.document, sel)
    n0 = len(win.document.parts)
    _drop(win.canvas.viewport(), md)
    for _ in range(5):
        app.processEvents()
    assert len(win.document.parts) == n0 + 1
    vp = win.canvas.viewport()
    c = win.canvas.mapToScene(vp.width() // 2, vp.height() // 2)
    x0, y0, x1, y1 = win.document.parts[-1].bbox()
    assert abs((x0 + x1) / 2 - c.x()) < 1e-6 and abs((y0 + y1) / 2 - c.y()) < 1e-6


def test_drop_on_the_window_and_unknown_data_refused(win, app, tmp_path):
    md = QMimeData()
    md.setText("hello")
    assert not win.accepts_drop(md)
    f = tmp_path / "x.dxf"
    f.write_bytes(clipboard.dxf_bytes(Document(parts=ops.demo_document().parts[:1])))
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(f))])
    assert win.accepts_drop(md) and win.acceptDrops()


def test_part_list_drags_the_selected_parts_out(win, app):
    sel = _demo(win, select=(1, 2))
    md = win.part_list.model.mimeData([])
    assert md.hasFormat(clipboard.MIME_PARTS) and md.hasUrls()
    path = md.urls()[0].toLocalFile()
    assert path.endswith(".dxf")
    back = clipboard.document_from_mime(md)
    assert _geometry(back.parts) == _geometry(sel)
    # dropping it back on the list (the window) is no paste; on the canvas it is
    assert not win.accepts_drop(md, source=win.part_list)
    assert win.accepts_drop(md, source=win.part_list, canvas=True)


# --------------------------------------------------------------------------- undo
def test_five_nudges_are_one_named_undo_step(win, app):
    sel = _demo(win, select=(0,))
    x0 = sel[0].bbox()[0]
    steps = len(win.docs.stack)
    for _ in range(5):
        win.nudge(1, 0)
    x5 = win.document.parts[0].bbox()[0]
    assert x5 > x0
    assert len(win.docs.stack) == steps + 1
    assert win.ribbon.actions["edit.undo"].text().replace("&", "") == "Undo Move"
    win.run("edit.undo")
    assert win.document.parts[0].bbox()[0] == pytest.approx(x0)
    win.run("edit.redo")
    assert win.document.parts[0].bbox()[0] == pytest.approx(x5)


def test_coalescing_rules():
    doc = ops.demo_document()
    svc = DocumentService(doc)
    a, b = doc.parts[0], doc.parts[1]

    def nudge(parts):
        svc.edit(lambda d: ops.translate(d, 1, 0, parts), "nudge", parts=parts, coalesce=True)
    nudge([a])
    nudge([a])
    assert len(svc.stack) == 1
    nudge([b])                           # another selection: a new step
    assert len(svc.stack) == 2
    svc.edit(lambda d: ops.translate(d, 1, 0, [b]), "nudge", parts=[b])     # not coalescing
    nudge([b])
    assert len(svc.stack) == 4
    svc.undo()
    nudge([b])                           # after an undo: a new step
    assert len(svc.stack) == 4 and not svc.can_redo
    svc.stack.seal()                     # e.g. saved: the next nudge is a step of its own
    nudge([b])
    assert len(svc.stack) == 5
    old = svc.stack.merge_window
    try:
        svc.stack.merge_window = -1.0    # too late: a new step
        nudge([b])
        assert len(svc.stack) == 6
    finally:
        svc.stack.merge_window = old
    xs = [p.bbox()[0] for p in svc.document.parts]
    while svc.can_undo:
        svc.undo()
    assert [p.bbox()[0] for p in svc.document.parts] == [p.bbox()[0] for p in ops.demo_document().parts]
    while svc.can_redo:
        svc.redo()
    assert [p.bbox()[0] for p in svc.document.parts] == xs


def test_undo_is_bounded_by_memory_not_by_count():
    st = UndoStack()
    assert st.limit is None
    svc = DocumentService(ops.demo_document())
    assert svc.stack.limit is None
    for i in range(150):
        svc.edit(lambda d: ops.translate(d, 1, 0), f"m{i}")
    assert len(svc.stack) == 150


def test_steps_are_named_after_their_command(win, app):
    _demo(win)
    win.run("xform.mirror_h")
    label = win.ribbon.actions["edit.undo"].text().replace("&", "")
    assert win.docs.stack.undo_text == "xform.mirror_h"           # recorded: the command
    assert label == "Undo " + win.shell.label("xform.mirror_h").rstrip("…")
    # nothing to undo: the plain word
    win.docs.stack.clear()
    win._update_undo_texts()
    assert win.ribbon.actions["edit.undo"].text().replace("&", "") == "Undo"
    from mlaser.ui.undo_names import display_name
    assert display_name(win, "nudge") == "Move" and display_name(win, "clear micro-joints") == "Clear micro-joints"
    assert display_name(win, "onshape: edit") == "Edit" and display_name(win, "Etch part IDs") == "Etch part IDs"


def test_undo_named_translates_with_the_language(win, app):
    _demo(win, select=(0,))
    win.nudge(1, 0)
    act = win.ribbon.actions["edit.undo"]
    win.set_language("en", persist=False)
    assert act.text().replace("&", "") == "Undo Move"


# --------------------------------------------------------------------------- menus and keys
def test_menu_has_cut_close_and_the_window_list(win, app):
    placed = menu_spec.placement(win.shell.menu_ctl.layout)
    assert placed["edit.cut"] == "edit" and placed["window.close"] == "file"
    edit = menu_spec.layout(["edit.undo", "edit.redo", "edit.cut", "edit.copy", "edit.paste"], menu_spec.LINUX)[1]
    assert edit.items[:6] == ["edit.undo", "edit.redo", menu_spec.SEP, "edit.cut", "edit.copy", "edit.paste"]
    mac = menu_spec.layout(["window.minimize", "window.zoom", "window.bring_all_front"], menu_spec.MACOS)
    wmenu = next(m for m in mac if m.id == "window")
    assert "window.bring_all_front" in wmenu.items
    assert any(isinstance(e, menu_spec.Sub) and e.id == menu_spec.WINDOW_LIST for e in wmenu.items)
    # the window list: the open windows, the active one checked
    qm = win.shell.menubar.findChild(QMenu, "menu_window")
    qm.aboutToShow.emit()
    items = [a for a in qm.actions() if a.objectName() == "window_list_item"]
    assert items and menu_spec.strip_mnemonic(items[0].text()) == win.windowTitle().replace("[*]", "")


def test_bring_all_to_front_on_a_simulated_mac(app, monkeypatch):
    """macOS only: registered, in the Window menu, and with one window it leaves it in front
    (the feedback walk checks that state: tests/test_command_feedback.py IDEMPOTENT)."""
    from mlaser.ui import native
    from wiring import dispose, real_window
    from test_command_feedback import IDEMPOTENT
    monkeypatch.setattr(native, "platform_id", lambda plat=None: native.MACOS)
    monkeypatch.setattr(native, "mac_arrange_in_front", lambda: False)
    w = real_window(user_mode="engineer")
    try:
        w.show()
        app.processEvents()
        assert "window.bring_all_front" in w.commands
        assert menu_spec.placement(w.shell.menu_ctl.layout)["window.bring_all_front"] == "window"
        w.run("window.bring_all_front")
        app.processEvents()
        assert IDEMPOTENT["window.bring_all_front"](w)
    finally:
        dispose(w)


def test_close_closes_the_active_window(win, app, monkeypatch):
    from PySide6.QtWidgets import QWidget
    other = QWidget(None, Qt.Window)
    other.setWindowTitle("Other")
    other.show()
    app.processEvents()
    monkeypatch.setattr(QApplication, "activeWindow", staticmethod(lambda: other))   # offscreen: no activation
    win.run("window.close")
    app.processEvents()
    assert not other.isVisible() and win.isVisible()
    other.deleteLater()
    # the main window active: Close closes it (the application's close path)
    monkeypatch.setattr(QApplication, "activeWindow", staticmethod(lambda: win))
    win.run("window.close")
    app.processEvents()
    assert not win.isVisible()


def test_item_mnemonics_are_unique_per_menu():
    texts = ["Undo Move", "Redo", "Cut", "Copy", "Paste", "Delete", "Select all", "Invert selection", "Exit",
             "Save as…", "Q&&A", "&Close"]
    out = menu_spec.item_mnemonics(texts)
    keys = [menu_spec._mnemonic_of(t) for t in out]
    assert all(keys)
    assert len(set(keys)) == len(keys)
    d = dict(zip(texts, out))
    assert d["Undo Move"] == "&Undo Move" and d["Cut"] == "Cu&t" and d["Exit"] == "E&xit"
    assert d["&Close"] == "&Close" and d["Copy"] == "C&opy"       # an item's own access key is kept
    assert d["Save as…"] == "Save &as…" and menu_spec.strip_mnemonic(d["Q&&A"]) == "Q&A"
    assert [menu_spec.strip_mnemonic(t) for t in out] == [menu_spec.strip_mnemonic(t) for t in texts]


def test_every_windows_style_menu_has_unique_access_keys(win, app):
    """Every menu and submenu of the real menu bar (Windows / Linux style): each item has an
    access key, no two alike in one menu when letters allow."""
    from mlaser.ui.shell.menubar import MenuBar
    mb = MenuBar(win.shell, plat=menu_spec.WINDOWS)
    try:
        menus = [m for m in mb.bar.findChildren(QMenu)]
        assert len(menus) > 10
        for qm in menus:
            qm.aboutToShow.emit()
            texts = [a.text() for a in qm.actions() if not a.isSeparator() and a.isVisible()]
            keys = [menu_spec._mnemonic_of(t) for t in texts]
            got = [k for k in keys if k]
            assert len(got) == len(set(got)), (qm.objectName(), texts)
            # an item without one has no letter left (all its letters are other items' keys)
            for t, k in zip(texts, keys):
                if not k:
                    assert {ch.lower() for ch in menu_spec.strip_mnemonic(t) if ch.isalnum()} <= set(got), \
                        (qm.objectName(), t)
            assert len(got) >= min(len(texts), 6) or qm.objectName() == "menu_xform.size", (qm.objectName(), texts)
    finally:
        mb.bar.deleteLater()
        win.shell.menu_ctl.build()


def test_macos_menus_have_no_access_keys(win, app):
    from mlaser.ui.shell.menubar import MenuBar
    mb = MenuBar(win.shell, plat=menu_spec.MACOS)
    try:
        edit = mb.menus["edit"]
        edit.aboutToShow.emit()
        assert not any("&" in a.text().replace("&&", "") for a in edit.actions() if a.data() == "edit.cut")
    finally:
        mb.bar.deleteLater()
        win.shell.menu_ctl.build()


@pytest.mark.parametrize("plat,full,undo,close", [
    ("darwin", "Meta+Ctrl+F", ["Ctrl+Z"], "Ctrl+W"),
    ("win32", "F11", ["Ctrl+Z", "Alt+Backspace"], "Ctrl+W"),
    ("linux", "F11", ["Ctrl+Z", "Alt+Backspace"], "Ctrl+W"),
])
def test_platform_shortcuts(plat, full, undo, close):
    keys = keymap.window_keys(plat)
    assert keys["window.fullscreen"] == full
    assert keymap.sequences(keys["edit.undo"]) == undo           # ⌥⌫ is no Undo on macOS
    assert keys["window.close"] == close and keys["edit.cut"] == "Ctrl+X"
    assert not keymap.conflicts(plat)
    if plat == "darwin":
        assert keymap.key_for("window.fullscreen", plat) == "⌃⌘F" and keymap.key_for("window.close", plat) == "⌘W"
        assert keymap.key_for("edit.undo", plat) == "⌘Z"


def test_workspace_window_uses_the_platform_full_screen_key(app, monkeypatch):
    from PySide6.QtGui import QShortcut
    from PySide6.QtWidgets import QLabel
    from mlaser.ui import workspaces
    monkeypatch.setattr(keymap.sys, "platform", "darwin")
    w = workspaces.WorkspaceWindow("t", QLabel("x"))
    try:
        seqs = [s.key().toString() for s in w.findChildren(QShortcut)]
        assert "F11" not in seqs and seqs
    finally:
        w.deleteLater()


# --------------------------------------------------------------------------- Esc in dialogs during a job
def test_escape_in_a_dialog_while_a_job_runs_stops_and_still_cancels(win, app, monkeypatch):
    """Esc = Stop while a job runs is kept everywhere (docs/SAFETY.md); in a dialog the key
    also reaches the dialog, whose Esc = Cancel keeps working."""
    from PySide6.QtTest import QTest
    from mlaser.ui import safety_ui
    stops = []
    monkeypatch.setattr(safety_ui, "job_running", lambda w: True)
    monkeypatch.setattr(safety_ui, "keep_awake", lambda on: None)
    monkeypatch.setattr(win.safety_ui, "stop", lambda *a, **k: stops.append(1))
    # the global keys are an application event filter installed while a job runs (by the job's
    # start event, else the 100 ms refresh); install it now, through the refresh, instead of
    # hoping a refresh tick falls before the key press (it did not on the macOS runners)
    win.safety_ui.refresh()
    assert win.safety_ui._filtering
    dlg = QDialog(win)
    dlg.show()
    dlg.activateWindow()
    dlg.setFocus()
    app.processEvents()
    QTest.keyClick(dlg, Qt.Key_Escape)
    app.processEvents()
    assert stops == [1]
    assert not dlg.isVisible() and dlg.result() == QDialog.Rejected
    # in the main window Esc only stops (the canvas' deselect does not run)
    stops.clear()
    QTest.keyClick(win.canvas, Qt.Key_Escape)
    app.processEvents()
    assert stops == [1]
    dlg.deleteLater()
