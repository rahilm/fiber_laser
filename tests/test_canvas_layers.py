"""The canvas's cached static picture (perf review 2026-11 finding 3).

The canvas paints the drawing, the toolpath and the other scene items once into a picture and
draws the live items (head marker, cut track, simulation trail, tool previews) over a copy of it
(``mlaser/ui/canvas.py``, "live layer").  The decisive check is pixel equality with the way
the canvas painted before: every item in the one scene, the whole scene on every paint.  It is
made at several zooms and pan positions, with the grid on, at device pixel ratio 1 here and 2
in a child process (``QT_SCALE_FACTOR=2``)."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest  # noqa: E402

pytest.importorskip("PySide6")


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def make_canvas(n: int = 48, inside: bool = False):
    """A shown canvas with a drawing, its real toolpath (leads, kerf, rapids, markers) and every
    kind of live item: head marker, a cut track of several chunks, a simulation trail, a tool
    preview with a snap marker.  ``inside``: in a window, at an odd offset (as in the main
    window: the viewport is not at the window's origin), compared on the screen itself."""
    sys.path.insert(0, str(Path(__file__).parent))
    from test_large_drawing import big_document
    from mlaser.cam.toolpath import CamParams, build_toolpath
    from mlaser.model import LeadParams
    from mlaser.ui.canvas import CanvasView, _Trail
    app = _app()
    doc = big_document(n)
    for k, p in enumerate(doc.parts):
        if k % 3 == 0:
            p.craft.lead_in = LeadParams(kind=1, length=2.0, angle_deg=30.0)
        if k % 5 == 0:
            p.craft.kerf_comp, p.craft.kerf_width = 1, 0.2
    if inside:
        from PySide6.QtWidgets import QWidget
        win = QWidget()
        win.resize(663, 501)
        v = CanvasView(win)
        v.move(7, 5)
        v.resize(640, 480)
        win.show()
        v._test_window = win                     # kept alive with the canvas
    else:
        v = CanvasView()
        v.resize(640, 480)
        v.show()
    v.set_document(doc)
    v.set_toolpath(build_toolpath(doc, CamParams()))
    v.finish_overlay()
    for _ in range(3):
        app.processEvents()
    old = (_Trail.CHUNK, _Trail.CHUNK_MAX)
    _Trail.CHUNK, _Trail.CHUNK_MAX = 40, 160      # several closed chunks
    try:
        for p in doc.parts[: n // 2]:
            pts = list(p.contours[0].points())
            v.add_track_point(pts[0][0], pts[0][1], False)
            for x, y in pts[1:]:
                v.add_track_point(x, y, True)
        v.set_head_position(47.0, 31.0)
        v.start_simulation(path=[(30.0 + 2 * k, 20.0 + 9 * math.sin(k / 3), k % 17 != 0) for k in range(90)])
        for _ in range(12):
            v._sim_step()
        v.stop_simulation()
    finally:
        _Trail.CHUNK, _Trail.CHUNK_MAX = old
    v.set_preview(polylines=[[(5, 5), (60, 30), (90, 5)]], markers=[(60, 30)])
    for _ in range(3):
        app.processEvents()
    return v


def _grab(v):
    """What the screen shows: the view painted on its window's backing store, read back (the
    offscreen platform's screen grab).  Not ``grab()``: a grab paints on a pixmap of the
    grabbed widget, and the paint engine starts a dashed line that leaves the view at that
    pixmap's edge, so its dashes can differ from the screen's (the canvas then paints the whole
    scene for the grab, as before)."""
    v.viewport().repaint()
    win = v.window()
    return win.screen().grabWindow(win.winId()).toImage()


def cached_image(v):
    """The canvas as it paints now (the cached picture plus the live items)."""
    _app().processEvents()
    v.viewport().repaint()                       # the window's update request makes the picture
    before = dict(v.paint_stats)
    img = _grab(v)
    assert v.paint_stats["cached"] == before["cached"] + 1, v.paint_stats
    return img


def reference_image(v):
    """The canvas painted as before the cache: the live items in the canvas scene itself, the
    whole scene on every paint."""
    from PySide6.QtCore import Qt
    live = v.overlay_scene().items(Qt.AscendingOrder)
    for it in live:
        v.overlay_scene().removeItem(it)
        v.scene().addItem(it)
    v.layer_cache = False
    try:
        return _grab(v)
    finally:
        v.layer_cache = True
        for it in live:
            v.scene().removeItem(it)
            v.overlay_scene().addItem(it)
        _app().processEvents()


def _views(v):
    """(name, set-up) of the compared views: fitted, zoomed in about a point, zoomed out and
    panned, with the grid."""
    def fit():
        v.zoom_fit()

    def zoom_in():
        v.zoom_fit()
        v.zoom(2.7)
        v.centerOn(40.0, 35.0)

    def zoom_out_pan():
        v.zoom_fit()
        v.zoom(0.45)
        v.centerOn(50.0, 40.0)
        v.horizontalScrollBar().setValue(v.horizontalScrollBar().value() + 37)
        v.verticalScrollBar().setValue(v.verticalScrollBar().value() - 23)

    def grid():
        v.options.show_grid = True
        v.zoom_fit()
        v.zoom(1.3)
        v.centerOn(45.0, 40.0)
    def resized():
        v.options.show_grid = False
        v.resize(v.width() - 37, v.height() - 21)
        v.zoom_fit()
    return [("fit", fit), ("zoom_in", zoom_in), ("zoom_out_pan", zoom_out_pan), ("grid", grid),
            ("resized", resized)]


def compare_all(v) -> dict:
    """{view name: (equal, differing pixels)} of the cached against the reference paint."""
    out = {}
    for name, setup in _views(v):
        _app().processEvents()
        v.viewport().repaint()                   # a clean picture of the previous view
        setup()                                  # only the view changes
        a = cached_image(v)
        b = reference_image(v)
        diff = 0
        if a != b:
            diff = sum(1 for y in range(a.height()) for x in range(a.width()) if a.pixel(x, y) != b.pixel(x, y))
        out[name] = (a == b, diff, a.devicePixelRatio(), a.width())
    return out


def _close(v) -> None:
    win = v.window()
    win.close()
    win.deleteLater()


@pytest.mark.parametrize("inside", [False, True], ids=["own_window", "in_a_window"])
def test_cached_paint_is_pixel_identical_to_the_whole_paint(inside):
    v = make_canvas(inside=inside)
    try:
        res = compare_all(v)
        assert all(r[0] for r in res.values()), res
        assert all(r[2] == 1.0 for r in res.values()), res
    finally:
        _close(v)


DPR2_SCRIPT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import test_canvas_layers as T
out = {}
for inside in (False, True):
    v = T.make_canvas(inside=inside)
    out.update({f"{k}/{inside}": list(r) for k, r in T.compare_all(v).items()})
    T._close(v)
print(json.dumps(out))
"""


def test_cached_paint_is_pixel_identical_at_device_pixel_ratio_2(tmp_path):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR="2",
               MLASER_CONFIG_DIR=str(tmp_path / "cfg"), MLASER_APP_DIR=str(tmp_path / "app"),
               MLASER_DATA_DIR=str(tmp_path / "data"))
    env.pop("_MLASER_CONFIG_AUTO", None)
    r = subprocess.run([sys.executable, "-c", DPR2_SCRIPT, str(Path(__file__).parent.resolve())], env=env,
                       capture_output=True, text=True, timeout=600, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr[-3000:]
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert all(x[0] for x in res.values()), res
    assert all(x[2] == 2.0 for x in res.values()), res


def test_a_head_move_draws_from_the_cached_picture():
    """A head move, a track point and a preview change repaint only the live items: the static
    picture is not painted again.  A change of the drawing or of the view is painted whole (a
    zoom or an edit costs what it did), and the picture is made again at the next live-only
    frame."""
    app = _app()
    v = make_canvas()

    def frame():
        app.processEvents()
        v.viewport().repaint()
    try:
        frame()
        frame()
        made, full = v.paint_stats["static"], v.paint_stats["full"]
        for k in range(5):
            v.set_head_position(10.0 + k, 12.0)
            v.add_track_point(10.0 + k, 12.0, True)
            v.set_preview(polylines=[[(0, 0), (k, k)]])
            frame()
        assert (v.paint_stats["static"], v.paint_stats["full"]) == (made, full)
        for change in (lambda: v.select_parts([0]), lambda: v.zoom(1.25)):   # an item, the view
            change()
            v.viewport().repaint()               # painted whole, no picture made
            assert (v.paint_stats["static"], v.paint_stats["full"]) == (made, full + 1)
            v.set_head_position(3.0, 4.0)
            frame()                              # a live-only frame: the picture again
            v.set_head_position(4.0, 4.0)
            frame()
            assert v.paint_stats["static"] == made + 1
            made, full = v.paint_stats["static"], v.paint_stats["full"]
    finally:
        _close(v)


def test_a_changed_item_never_shows_a_stale_picture():
    """A scene change followed at once by a synchronous paint (no event-loop turn in between)
    shows the change: the scene's queued notifications are delivered before the paint."""
    from PySide6.QtGui import QPen, QColor
    v = make_canvas()
    try:
        v.viewport().repaint()
        it = v._items[0][2]
        it.setPen(QPen(QColor("#123456"), 4))
        a = _grab(v)
        b = reference_image(v)
        assert a == b
    finally:
        _close(v)


def test_track_chunks_draw_what_one_path_drew():
    """The cut track in chunks (closed at laser-off moves) paints the same pixels as the one
    growing path it was before, for a track that does not cross itself."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPainterPath
    from PySide6.QtWidgets import QGraphicsPathItem
    from mlaser.ui.canvas import _Trail, LIVE_Z_MIN
    app = _app()
    v = make_canvas(8)
    try:
        v.clear_track()
        v.stop_simulation(clear=True)
        v.clear_preview()
        old = (_Trail.CHUNK, _Trail.CHUNK_MAX)
        _Trail.CHUNK, _Trail.CHUNK_MAX = 25, 10 ** 6
        pts = []
        for k in range(400):                     # a spiral, a laser-off move every 30 points
            a = k * 0.21
            r = 3 + 0.35 * a
            pts.append((60 + r * math.cos(a), 40 + r * math.sin(a), k % 30 != 0))
        try:
            for x, y, cut in pts:
                v.add_track_point(x, y, cut)
        finally:
            _Trail.CHUNK, _Trail.CHUNK_MAX = old
        assert len(v._track.items) > 5
        v.zoom_fit()
        a = cached_image(v)
        # the old way: one path, one item in the scene, the whole scene painted
        path, last = QPainterPath(), None
        for x, y, cut in pts:
            if last is None or not cut:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
            last = (x, y)
        one = QGraphicsPathItem(path)
        one.setPen(v._track.pen)
        one.setZValue(LIVE_Z_MIN)
        live = v.overlay_scene().items(Qt.AscendingOrder)
        for it in live:
            v.overlay_scene().removeItem(it)
        v.scene().addItem(one)
        for it in live:
            if it not in v._track.items:
                v.scene().addItem(it)
        v.layer_cache = False
        b = _grab(v)
        assert a == b
        app.processEvents()
    finally:
        _close(v)


def test_the_main_window_canvas_paints_from_its_picture():
    """In the main window (the canvas in a splitter, the window resized, another workspace and
    back) a head move is drawn from the cached picture, not by a whole repaint."""
    from mlaser.ops import demo_document
    from wiring import dispose, real_window
    app = _app()
    w = real_window()
    try:
        w.resize(1300, 820)
        w.show()
        w.set_document(demo_document())
        for size in ((1280, 800), (1350, 860)):
            w.resize(*size)
            for _ in range(5):
                app.processEvents()
        w.shell.host.activate("process")
        for _ in range(5):
            app.processEvents()
        w.shell.host.activate("design")
        for _ in range(5):
            app.processEvents()
        cv = w.canvas
        cv.viewport().repaint()
        before = dict(cv.paint_stats)
        for k in range(4):
            cv.set_head_position(5.0 + k, 5.0)
            app.processEvents()
            cv.viewport().repaint()
        assert cv.paint_stats["cached"] >= before["cached"] + 4, cv.paint_stats
        assert cv.paint_stats["full"] == before["full"], cv.paint_stats
    finally:
        dispose(w)
