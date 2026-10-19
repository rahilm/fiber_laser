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
