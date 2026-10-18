"""Canvas drawing of raster engraving parts, canvas level of detail, and the engraving dialog's
raster output (offscreen Qt)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsSimpleTextItem  # noqa: E402

from mlaser.cam import raster_engraving as R  # noqa: E402
from mlaser.engrave.raster import RasterParams, gcode_from_bitmap, process_image, raster_from_bitmap  # noqa: E402
from mlaser.io.gcode import parse_gcode  # noqa: E402
from mlaser.model import Contour, Document, Line, Part  # noqa: E402

pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _img(h=40, w=60):
    rng = np.random.default_rng(3)
    return rng.integers(0, 256, (h, w), dtype=np.uint8)


def _raster_doc(mode="dither"):
    p = RasterParams(mode=mode, width_mm=10, rho=6)
    bmp = process_image(_img(), p)
    r = raster_from_bitmap(bmp, p.rho, p.power, p)
    return Document(parts=[R.make_raster_part(r)]), r, bmp


def _render(view):
    img = QImage(400, 300, QImage.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    view.render(p)
    p.end()
    return img


def test_canvas_raster_item(app):
    from mlaser.cam.toolpath import CamParams, build_toolpath
    from mlaser.ui.canvas import CanvasView, _RasterItem
    doc, r, bmp = _raster_doc()
    v = CanvasView()
    v.resize(400, 300)
    v.set_document(doc)
    v.set_toolpath(build_toolpath(doc, CamParams(sort_mode=0)))
    rasters = [it for it in v.scene().items() if isinstance(it, _RasterItem)]
    assert len(rasters) == 1
    assert len(v.scene().items()) < 20          # not one item per burn run
    it = rasters[0]
    mask = it._mask
    assert mask.shape[0] == len(np.unique(r.row_y))
    # the mask covers exactly the burn pixels of the engraving bitmap (rows bottom-up)
    assert np.array_equal(mask[:, :bmp.shape[1]], (bmp <= 125)[::-1])
    img = _render(v)
    assert not img.isNull()
    # pick inside the engraving area selects the raster part
    xa, ya, xb, yb = r.frame
    assert v.pick((xa + xb) / 2, (ya + yb) / 2) == 0
    v.select_parts([0])
    assert doc.parts[0].selected
    v.deleteLater()


def test_canvas_raster_follows_frame(app):
    from mlaser.ui import docops
    from mlaser.ui.canvas import CanvasView, _RasterItem
    doc, r, _ = _raster_doc("halftone")
    docops.translate(doc, 50.0, 20.0, list(doc.parts))
    docops.rotate(doc, 90.0)
    v = CanvasView()
    v.set_document(doc)
    it = [i for i in v.scene().items() if isinstance(i, _RasterItem)][0]
    sx, sy, ex, ey = R.segment_arrays(doc.parts[0])
    p = it.sceneTransform().map(it.boundingRect().center())
    x0, y0, x1, y1 = doc.bbox()
    assert abs(p.x() - (x0 + x1) / 2) < 0.2 and abs(p.y() - (y0 + y1) / 2) < 0.2
    v.deleteLater()


def test_canvas_lod_many_contours(app):
    from mlaser.ui.canvas import LOD_CONTOURS, CanvasView, _LodPathItem
    parts = [Part(contours=[Contour([Line((i * 0.01, 0.0), (i * 0.01 + 0.005, 0.0))], False)])
             for i in range(LOD_CONTOURS + 10)]
    v = CanvasView()
    v.resize(400, 300)
    v.set_document(Document(parts=parts))
    items = v.scene().items()
    assert not any(isinstance(i, QGraphicsSimpleTextItem) for i in items)
    assert sum(isinstance(i, _LodPathItem) for i in items) == LOD_CONTOURS + 10
    assert not _render(v).isNull()
    v.set_document(Document(parts=parts[:5]))
    assert sum(isinstance(i, QGraphicsSimpleTextItem) for i in v.scene().items()) == 5
    v.deleteLater()


def test_dialog_raster_output(app, tmp_path):
    from mlaser.engrave.dialog import BitmapDialog
    d = BitmapDialog(out_dir=tmp_path)
    d.set_image(_img(48, 64))
    d.set_mode("dither")
    d.width_mm.setValue(12)
    assert d.result_document() is None
    d.generate()
    assert d.raster_part is not None and len(d.raster) > 0
    doc = d.result_document()
    assert len(doc.parts) == 1 and R.raster_of_part(doc.parts[0]) is not None
    seg = d.result_document(per_segment=True)
    old = parse_gcode(d.gcode_text)
    assert [p.contours[0].segments for p in seg.parts] == [p.contours[0].segments for p in old.parts]
    assert d.gcode_text == gcode_from_bitmap(d.engraving_bitmap(), d.params.rho, d.params.power)
    d.deleteLater()
