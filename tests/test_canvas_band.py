"""The canvas's selection band is painted by the canvas (a QRubberBand under the app's style
sheet was an opaque black box on macOS): translucent, solid for a window (dragged right), dashed
green for a crossing (dragged left), and the drag selects as before."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mlaser.model import Contour, Craft, Document, Line, Part  # noqa: E402


def _square(x, y, s=10.0):
    c = Contour([Line((x, y), (x + s, y)), Line((x + s, y), (x + s, y + s)),
                 Line((x + s, y + s), (x, y + s)), Line((x, y + s), (x, y))], closed=True)
    return Part([c], Craft())


def _mouse(kind, pos):
    p = QPointF(pos)
    return QMouseEvent(kind, p, p, Qt.LeftButton,
                       Qt.LeftButton if kind != QMouseEvent.Type.MouseButtonRelease else Qt.NoButton,
                       Qt.NoModifier)


@pytest.mark.parametrize("rightwards", [True, False])
def test_band_is_translucent_and_shows_the_rule(rightwards):
    from mlaser.ui.canvas import CanvasView, SelectionBand
    app = QApplication.instance() or QApplication([])
    cv = CanvasView()
    cv.resize(400, 300)
    doc = Document(parts=[_square(0, 0), _square(30, 0)])
    cv.set_document(doc)
    cv.show()
    cv.zoom_fit()
    app.processEvents()
    vp = cv.viewport()
    a, b = (QPoint(5, 5), QPoint(395, 295)) if rightwards else (QPoint(395, 295), QPoint(5, 5))
    cv.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, a))
    cv.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, b))
    band = cv._rubber
    assert isinstance(band, SelectionBand) and band.isVisible() and band.crossing is (not rightwards)
    img = band.grab().toImage()
    mid = img.pixelColor(img.width() // 2, img.height() // 2)
    assert mid.alpha() < 128 or mid.lightness() > 128, "the band must not be an opaque dark box"
    cv.mouseReleaseEvent(_mouse(QMouseEvent.Type.MouseButtonRelease, b))
    assert not band.isVisible()
    assert all(p.selected for p in doc.parts)          # both squares inside the band
    assert vp is cv.viewport()
