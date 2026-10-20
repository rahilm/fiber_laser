"""Part list: bounding boxes cached by part uid (ui/panels.PartListPanel)."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mlaser import ops  # noqa: E402
from mlaser.model import Contour, Craft, Document, Line, Part  # noqa: E402
from mlaser.services.document import DocumentService  # noqa: E402


def sq(x, y, s=10.0, closed=True):
    pts = [(x, y), (x + s, y), (x + s, y + s), (x, y + s)]
    segs = [Line(pts[i], pts[(i + 1) % 4]) for i in range(4 if closed else 3)]
    return Part([Contour(segs, closed)], Craft(layer=1))


@pytest.fixture
def panel():
    from mlaser.ui.i18n import translator
    from mlaser.ui.panels import PartListPanel
    QApplication.instance() or QApplication([])
    return PartListPanel(translator())


def sizes(panel):
    return [panel.topLevelItem(i).text(4) for i in range(panel.topLevelItemCount())]


def test_only_changed_parts_are_measured(panel):
    docs = DocumentService(Document(parts=[sq(i * 20, 0) for i in range(50)]))
    panel.set_document(docs.document)
    assert panel.bbox_misses == 50
    panel.refresh()
    assert panel.bbox_misses == 50 and panel.bbox_hits == 50
    target = docs.document.parts[3]
    docs.edit(lambda d: ops.scale(Document(parts=[target]), 2.0), "scale", parts=[target])
    panel.set_document(docs.document)
    assert panel.bbox_misses == 51                     # only the scaled part
    assert sizes(panel)[3] == "20.00×20.00" and sizes(panel)[4] == "10.00×10.00"
    docs.undo()                                       # the old object comes back (same uid)
    panel.set_document(docs.document)
    assert sizes(panel)[3] == "10.00×10.00"
    docs.redo()
    panel.set_document(docs.document)
    assert sizes(panel)[3] == "20.00×20.00"


def test_rows_follow_removals_and_open_contours(panel):
    doc = Document(parts=[sq(0, 0), sq(20, 0, closed=False), sq(40, 0, 5)])
    panel.set_document(doc)
    assert panel.topLevelItem(1).foreground(1).color().name() == "#d00000"
    doc.parts.pop(0)
    doc.parts[0].selected = True
    panel.refresh()
    assert panel.topLevelItemCount() == 2
    assert sizes(panel) == ["10.00×10.00", "5.00×5.00"]
    assert panel.topLevelItem(0).isSelected() and not panel.topLevelItem(1).isSelected()
    doc.parts[0].contours[0].segments[1] = Line((30, 0), (35, 10))     # an in-list replacement
    panel.refresh()
    assert sizes(panel)[0] == "15.00×10.00"


def _selected_rows(panel):
    return sorted(panel.indexOfTopLevelItem(it) for it in panel.selectedItems())


def test_sync_selection_follows_the_part_flags(panel):
    """One batched selection (review 2 P1-8): the rows match the parts' flags for every pattern,
    and the panel's own selection signal does not echo back to the canvas."""
    doc = Document(parts=[sq(i * 20, 0) for i in range(40)])
    panel.set_document(doc)
    echoed = []
    panel.selectionToCanvas.connect(echoed.append)
    for pattern in (lambda i: True, lambda i: False, lambda i: i % 2 == 0, lambda i: i in (0, 39),
                    lambda i: 10 <= i < 20 or i == 25, lambda i: i == 7):
        for i, p in enumerate(doc.parts):
            p.selected = pattern(i)
        panel.sync_selection()
        assert _selected_rows(panel) == [i for i in range(40) if pattern(i)]
    assert echoed == []
    doc.parts[3].selected = True                    # refresh applies the flags as well
    doc.parts.pop(0)
    panel.refresh()
    assert _selected_rows(panel) == [i for i, p in enumerate(doc.parts) if p.selected]
    # a click in the list still reaches the canvas
    panel.topLevelItem(5).setSelected(True)
    assert echoed and 5 in echoed[-1]


def _grid_doc(n):
    import math
    cols = int(math.ceil(math.sqrt(n)))
    return Document(parts=[sq((k % cols) * 15.0, (k // cols) * 15.0) for k in range(n)])


@pytest.mark.flaky(reruns=1)
def test_rubber_band_selection_at_10000_parts_is_linear():
    """Rubber band and select-all on 10,000 parts with the part list attached (canvas selection
    -> PartListPanel.sync_selection, the application's wiring).  Before: 222-223 s for the full
    rubber band and 77 s for a small one right after (QTreeWidgetItem.setSelected per row)."""
    import time

    from PySide6.QtCore import QRectF

    from mlaser.ui.canvas import CanvasView
    from mlaser.ui.i18n import translator
    from mlaser.ui.panels import PartListPanel
    QApplication.instance() or QApplication([])
    n = 10_000
    doc = _grid_doc(n)
    cv = CanvasView()
    pl = PartListPanel(translator())
    cv.selectionChanged.connect(pl.sync_selection)
    pl.selectionToCanvas.connect(lambda idx: cv.select_parts(idx))
    cv.set_document(doc)
    pl.set_document(doc)
    x0, y0, x1, y1 = doc.bbox()
    t = time.perf_counter()
    cv._rubber_select(QRectF(x0 - 1, y0 - 1, x1 - x0 + 2, y1 - y0 + 2), True, False)
    t_all = time.perf_counter() - t
    assert all(p.selected for p in doc.parts)
    assert len(pl.selectedItems()) == n
    t = time.perf_counter()
    cv._rubber_select(QRectF(-1, -1, 12, 12), False, False)        # just the first square
    t_small = time.perf_counter() - t
    assert [i for i, p in enumerate(doc.parts) if p.selected] == [0]
    assert _selected_rows(pl) == [0]
    t = time.perf_counter()
    cv.select_parts(range(0, n, 2))
    t_alt = time.perf_counter() - t
    assert _selected_rows(pl) == list(range(0, n, 2))
    # generous bounds for a loaded host (measured 0.2-0.6 s each); quadratic took minutes
    assert t_all < 8.0 and t_small < 8.0 and t_alt < 8.0, (t_all, t_small, t_alt)
