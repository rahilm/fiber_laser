"""The part list in Design and Process (UX review 2026-11, finding 12; mlaser/ui/part_list.py):
reachable as a dock of its own, find / filter (Ctrl/⌘+F) that never changes the drawing,
two-way selection with the canvas, *Assign technology…* for a subset on the per-layer
mechanism (one undo step), the busy state of a large copy, accessibility."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.ui

from PySide6.QtCore import QItemSelectionModel, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from mlaser.model import Contour, Craft, Document, Line, Part  # noqa: E402
from mlaser.ui import part_list as PL  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch):
    from wiring import dispose, real_window
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window(user_mode="engineer")
    w.resize(1400, 900)
    w.show()
    app.processEvents()
    yield w
    QApplication.clipboard().clear()
    dispose(w)


def sq(x, y, s=10.0, layer=1, closed=True, name=""):
    pts = [(x, y), (x + s, y), (x + s, y + s), (x, y + s)]
    segs = [Line(pts[i], pts[(i + 1) % 4]) for i in range(4 if closed else 3)]
    return Part([Contour(segs, closed)], Craft(layer=layer), name=name)


def grid(n, layers=(1, 2, 3)):
    side = max(1, int(n ** 0.5) + 1)
    return Document(parts=[sq((k % side) * 15.0, (k // side) * 15.0, 10.0, layers[k % len(layers)], name=f"p{k}")
                           for k in range(n)])


def pump(app, n=5):
    for _ in range(n):
        app.processEvents()


def shown_parts(panel) -> list[int]:
    m = panel.model
    return [m.index(r, 0).data(PL.PART_ROLE) for r in range(m.rowCount())]


def selected_rows(panel) -> list[int]:
    return sorted(ix.row() for ix in panel.view.selectionModel().selectedRows())


# --------------------------------------------------------------------------- reachable
def test_part_list_is_a_dock_of_design_and_process(win, app):
    """The review found no part list: it was a tab inside the Layer parameters dock, hidden
    behind Properties in Design and not shown at all in Process."""
    dock = win.findChild(type(win.left_dock), "parts")
    assert dock is not None and dock.widget() is win.part_list
    for ws in ("design", "process"):
        win.show_workspace(ws)
        pump(app)
        assert not dock.isHidden(), ws
        dock.raise_()
        pump(app)
        assert win.part_list.isVisible(), ws
    win.show_workspace("run")
    pump(app)
    assert dock.isHidden()
    win.show_workspace("design")
    pump(app)
    assert not dock.isHidden()


def test_find_key_shows_the_list_and_focuses_the_filter(win, app):
    from mlaser.ui.shell import keymap
    assert keymap.WINDOW_KEYS["partlist.find"] == "Ctrl+F"
    act = win.ribbon.actions["partlist.find"]
    assert [s.toString() for s in act.shortcuts()] == ["Ctrl+F"]
    # no other window action has it
    others = [cid for cid, a in win.ribbon.actions.items() if cid != "partlist.find"
              and any(s.toString() == "Ctrl+F" for s in a.shortcuts())]
    assert others == []
    win.set_document(grid(30))
    win.show_workspace("process")
    pump(app)
    win.activateWindow()
    win.run("partlist.find")
    pump(app)
    assert win.parts_dock.isVisible() and win.part_list.view.isVisible()
    assert QApplication.focusWidget() is win.part_list.filter_edit or win.part_list.filter_edit.hasFocus() \
        or not win.isActiveWindow()
    # outside Design / Process it says where the list is and changes nothing
    win.show_workspace("run")
    pump(app)
    win.run("partlist.find")
    assert win.parts_dock.isHidden()


# --------------------------------------------------------------------------- model
def test_model_updates_incrementally(win, app):
    win.set_document(grid(400))
    pump(app)
    m = win.part_list.model
    resets, changed = [], []
    m.modelReset.connect(lambda: resets.append(1))
    m.dataChanged.connect(lambda a, b, *_: changed.append((a.row(), b.row(), a.column(), b.column())))
    win.canvas.select_parts([7])
    win.run("xform.scale_200")                               # part 7's size changes
    pump(app)
    assert resets == []
    # (the cut-order column alone is refreshed whole when a plan is delivered, whenever that is:
    # the plan comes from another process)
    edits = [(a, b) for a, b, c0, c1 in changed if not c0 == c1 == PL.ORDER]
    assert edits and all(a == b == 7 for a, b in edits), changed
    assert m.index(7, PL.SIZE).data() == "20.00×20.00"
    assert m.rowCount() == 400
    # a paste adds rows at the end (inserted, not a reset)
    inserted = []
    m.rowsInserted.connect(lambda _p, a, b: inserted.append((a, b)))
    win.canvas.select_parts([0, 1])
    win.run("edit.copy")
    win.run("edit.paste")
    pump(app)
    assert m.rowCount() == 402 and inserted == [(400, 401)] and resets == []
    # numbers right-aligned in tabular figures
    ix = m.index(0, PL.SIZE)
    assert ix.data(Qt.TextAlignmentRole) & int(Qt.AlignRight)
    assert m.index(0, PL.NAME).data(Qt.TextAlignmentRole) & int(Qt.AlignLeft)


def test_columns_and_sorting(win, app):
    doc = Document(parts=[sq(0, 0, 10, 2, name="b"), sq(20, 0, 30, 1, name="a"), sq(60, 0, 5, 3, name="c")])
    win.set_document(doc)
    pump(app)
    pl = win.part_list
    m = pl.model
    assert [m.headerData(c, Qt.Horizontal) for c in (PL.NUM, PL.NAME, PL.LAYER)] == ["#", "Name", "Layer"]
    assert m.index(1, PL.NAME).data() == "a" and m.index(1, PL.LAYER).data() == "1"
    assert m.index(1, PL.ORDER).data()                      # a cut order is shown
    pl.view.sortByColumn(PL.SIZE, Qt.DescendingOrder)
    assert shown_parts(pl) == [1, 0, 2]
    pl.view.sortByColumn(PL.NAME, Qt.AscendingOrder)
    assert shown_parts(pl) == [1, 0, 2]
    pl.view.sortByColumn(PL.LAYER, Qt.DescendingOrder)
    assert shown_parts(pl) == [2, 0, 1]
    # the selection follows the parts, not the row positions
    win.canvas.select_parts([0])
    assert selected_rows(pl) == [1]
    pl.view.sortByColumn(PL.NUM, Qt.AscendingOrder)
    assert selected_rows(pl) == [0]


# --------------------------------------------------------------------------- filter
def test_filter_never_changes_the_document(win, app):
    doc = grid(60)
    doc.parts[5].contours[0].closed = False
    doc.parts[5].name = "Flange"
    doc.parts[40].name = "flange big"
    win.set_document(doc)
    win.canvas.select_parts([1, 2, 5])
    pump(app)
    before = ([id(p) for p in win.document.parts], [p.selected for p in win.document.parts],
              win.docs.can_undo)
    pl = win.part_list
    pl.model.set_filter(text="flange")
    assert shown_parts(pl) == [5, 40]
    pl.model.set_filter(text="#12")
    assert shown_parts(pl) == [11]
    pl.model.set_filter(text=">9.5")                         # every part is 10 mm
    assert len(shown_parts(pl)) == 60
    pl.model.set_filter(text="<9.5")
    assert shown_parts(pl) == []
    assert pl.stack.currentWidget() is pl.empty and pl.clear_btn.isVisibleTo(pl)
    pl.clear_filter()
    pl.model.set_filter(layer=2)
    assert shown_parts(pl) == [k for k in range(60) if k % 3 == 1]
    pl.model.set_filter(layer=None, warnings=True)
    # the grid's layers have no cut settings (placeholders): every part warns; the open contour too
    assert 5 in shown_parts(pl)
    pl.clear_filter()
    after = ([id(p) for p in win.document.parts], [p.selected for p in win.document.parts], win.docs.can_undo)
    assert after == before
    assert selected_rows(pl) == [1, 2, 5]


def test_selecting_in_a_filtered_list_keeps_hidden_selected_parts(win, app):
    win.set_document(grid(30))
    win.canvas.select_parts([0, 1])
    pump(app)
    pl = win.part_list
    pl.model.set_filter(text="#20")
    pl.view.selectionModel().select(pl.model.index(0, 0), QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
    pl.flush()
    assert win.canvas.selected_indices() == [0, 1, 19]


def test_empty_state(win, app):
    win.set_document(Document())
    pump(app)
    pl = win.part_list
    assert pl.stack.currentWidget() is pl.empty
    assert pl.empty_text.text() == pl.tr_.tr("partlist.empty") and not pl.clear_btn.isVisibleTo(pl)
    win.set_document(grid(3))
    pump(app)
    assert pl.stack.currentWidget() is pl.view


# --------------------------------------------------------------------------- selection sync
def test_selection_both_ways_and_scrolled_into_view(win, app):
    win.set_document(grid(500))
    win.parts_dock.raise_()
    pump(app)
    pl = win.part_list
    win.canvas.select_parts([480])
    pump(app)
    assert selected_rows(pl) == [480]
    rect = pl.view.visualRect(pl.model.index(480, 0))
    assert pl.view.viewport().rect().intersects(rect)       # scrolled into view
    pl.view.selectionModel().select(pl.model.index(3, 0), QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
    pl.view.selectionModel().select(pl.model.index(4, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
    pl.flush()
    assert win.canvas.selected_indices() == [3, 4]
    assert "2 selected" in pl.status.text()
    # Enter / Show on canvas zooms the canvas to them
    zoomed = []
    pl.zoomRequested.connect(zoomed.append)
    pl.show_btn.click()
    assert zoomed == [[3, 4]]
    s = abs(win.canvas.transform().m11())
    assert s > 0 and win.canvas.transform().m22() < 0       # zoomed, the y axis still up


@pytest.mark.flaky(reruns=1)
def test_selection_sync_is_fast_at_2700_parts(win, app):
    """Measured: the list's own share of a canvas select-all / a list select-all at 2,700
    parts is a few milliseconds (model rows, one selection call); bounds are generous for a
    loaded host."""
    from test_large_drawing import big_document
    win.set_document(big_document(2700))
    pump(app)
    pl = win.part_list
    m = pl.model
    parts = win.document.parts

    def idle() -> None:
        # the list's own share: measured once the drawing's plan is over, not against it.  The
        # preview has two stages (toolpath, then the estimate's compile after an idle pause):
        # a wait for "no plan pending, the preview done" ended between them, and the measured
        # sync shared the interpreter with the compile in the plan thread (CI: 1.69 s for the
        # alternate selection on Windows, 1.43 s for select-all under coverage; 0.006 s alone)
        end = time.monotonic() + 120
        while time.monotonic() < end:
            win.jobs.settle(timeout=60.0)
            pump(app)
            tk = win.jobs.preview
            if not win.jobs.pending and (tk is None or tk.done_event.is_set()):
                return

    def timed(sel) -> float:
        """Best of three syncs to ``sel`` (each from no selection, synced first)."""
        best = None
        for _ in range(3):
            for p in parts:
                p.selected = False
            pl.sync_selection()
            for i, p in enumerate(parts):
                p.selected = sel(i)
            idle()
            t = time.perf_counter()
            pl.sync_selection()
            dt = time.perf_counter() - t
            best = dt if best is None else min(best, dt)
        return best

    t_sync = timed(lambda i: True)
    assert len(selected_rows(pl)) == 2700
    t_alt = timed(lambda i: i % 2 == 0)
    assert len(selected_rows(pl)) == 1350
    idle()
    t = time.perf_counter()
    m.update(win.document, {parts[10].uid})
    t_update = time.perf_counter() - t
    idle()
    t = time.perf_counter()
    m.set_filter(text="p12")
    t_filter = time.perf_counter() - t
    assert t_sync < 1.0 and t_alt < 1.0 and t_update < 0.5 and t_filter < 1.0, (t_sync, t_alt, t_update, t_filter)


# --------------------------------------------------------------------------- assign technology
def _tech(win, k=0):
    return win.extension("ext_process").store.all()[k]


def test_plan_assignment():
    class T:
        id, version = 7, 2
    doc = grid(9)
    sel = doc.parts[:2]
    plan = PL.plan_assignment(doc, sel, T())
    assert plan == PL.Assignment(0, assign=True, move=True)        # the lowest free layer
    for p in doc.parts:
        if p.craft.layer == 2:
            p.selected = True
    whole = [p for p in doc.parts if p.craft.layer == 2]
    assert PL.plan_assignment(doc, whole, T()) == PL.Assignment(2, assign=True, move=False)
    assert PL.plan_assignment(doc, sel, T(), max_layers=4) == PL.Assignment(0, assign=True, move=True)
    assert PL.plan_assignment(Document(parts=[sq(0, 0, layer=i) for i in range(3)] + [sq(0, 0, layer=0)]),
                              [sq(0, 0, layer=0)], T(), max_layers=3) is None


def test_assign_technology_to_a_subset_is_one_undo_step(win, app):
    from mlaser.services import technology as T
    win.set_document(grid(60))
    pump(app)
    tech = _tech(win)
    win.canvas.select_parts(range(10, 22))
    before_layers = [p.craft.layer for p in win.document.parts]
    before_defs = sorted(win.document.layers)
    win._test_assign_tech = tech
    win.run("partlist.assign_tech")
    pump(app)
    doc = win.document
    moved = [p.craft.layer for p in doc.parts[10:22]]
    assert len(set(moved)) == 1
    li = moved[0]
    assert li not in before_layers                           # a free layer ...
    ref = T.layer_technology_ref(doc.layers[li])
    assert ref and ref["id"] == tech.id                      # ... that follows the record
    assert [p.craft.layer for i, p in enumerate(doc.parts) if not 10 <= i < 22] == \
        [x for i, x in enumerate(before_layers) if not 10 <= i < 22]
    msg = win.statusBar().currentMessage()
    assert msg.startswith(f"12 parts moved to layer {li}") and tech.key.label() in msg
    # the list says so too
    pl = win.part_list
    from mlaser.ui.workspaces.process import technology_name
    assert pl.model.index(10, PL.TECH).data() == technology_name(doc, li) != ""
    # a second subset with the same technology joins that layer (no new one)
    win.canvas.select_parts([40, 41])
    win.run("partlist.assign_tech")
    assert win.document.parts[40].craft.layer == li and win.document.parts[41].craft.layer == li
    win.undo()
    # one undo step: the first assignment is still there, the second is gone
    assert win.document.parts[40].craft.layer == before_layers[40]
    win.undo()
    assert [p.craft.layer for p in win.document.parts] == before_layers
    assert sorted(win.document.layers) == before_defs


def test_assign_technology_to_a_whole_layer_moves_nothing(win, app):
    from mlaser.services import technology as T
    win.set_document(grid(30))
    pump(app)
    win.canvas.select_parts([i for i, p in enumerate(win.document.parts) if p.craft.layer == 2])
    win._test_assign_tech = _tech(win)
    win.run("partlist.assign_tech")
    assert {p.craft.layer for p in win.document.parts} == {1, 2, 3}
    assert T.layer_technology_ref(win.document.layers[2])
    assert "layer 2 now uses" in win.statusBar().currentMessage()


def test_assign_technology_needs_a_selection(win, app):
    win.set_document(grid(5))
    pump(app)
    pl = win.part_list
    assert not pl.assign_btn.isEnabled() and pl.assign_btn.toolTip()
    win.canvas.select_parts([1])
    assert pl.assign_btn.isEnabled()


# --------------------------------------------------------------------------- copy
def test_large_copy_shows_busy_state_and_writes_dxf_off_the_gui_thread(win, app, monkeypatch):
    import threading

    from mlaser.ui import clipboard
    threads = []
    real = clipboard.dxf_bytes
    monkeypatch.setattr(clipboard, "dxf_bytes", lambda d: (threads.append(threading.current_thread()), real(d))[1])
    win.set_document(grid(256))
    pump(app)
    win.canvas.select_parts(range(256))
    messages = []
    win.statusBar().messageChanged.connect(messages.append)
    win.run("edit.copy")
    assert win.tr_.plural("busy.copy", 256) in messages
    assert QApplication.overrideCursor() is None             # restored
    md = QApplication.clipboard().mimeData()
    assert md.hasFormat(clipboard.MIME_PARTS)
    from PySide6.QtCore import QDeadlineTimer, QEventLoop
    deadline = QDeadlineTimer(30000)
    while not threads and not deadline.hasExpired():
        app.processEvents(QEventLoop.WaitForMoreEvents, 50)
    assert threads and threads[0] is not threading.main_thread()


# --------------------------------------------------------------------------- accessibility
def test_accessible_names_and_row_text(win, app):
    doc = grid(3)
    doc.parts[1].contours[0].closed = False
    win.set_document(doc)
    pump(app)
    pl = win.part_list
    for w in (pl.view, pl.filter_edit, pl.layer_combo, pl.warn_btn, pl.show_btn, pl.assign_btn):
        assert w.accessibleName().strip(), w.objectName()
    row = pl.model.index(1, PL.NAME).data(Qt.AccessibleTextRole)
    assert row.startswith("Part 2, p1") and "Open contour" in row and "layer 2" in row
    # keyboard: Down from the filter field goes into the list; the arrow keys move in the list
    # and never nudge the selected parts (the window's arrow shortcuts), Esc goes back
    from PySide6.QtTest import QTest
    win.parts_dock.raise_()
    win.canvas.select_parts([0])
    boxes = [p.bbox() for p in win.document.parts]
    pl.filter_edit.setFocus()
    QTest.keyClick(pl.filter_edit, Qt.Key_Down)
    assert pl.view.currentIndex().isValid()
    QTest.keyClick(pl.view, Qt.Key_Down)
    QTest.keyClick(pl.view, Qt.Key_Up)
    QTest.keyClick(pl.filter_edit, Qt.Key_Up)
    assert [p.bbox() for p in win.document.parts] == boxes
    QTest.keyClick(pl.view, Qt.Key_Escape)
    assert win.canvas.selected_indices() == [0] or win.canvas.selected_indices() == [pl.view.currentIndex().row()]
