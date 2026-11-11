"""Canvas cost with a large drawing (perf review 2026-11 findings 3, 5 and 14; GitHub issue #1).

* A head-marker move, a cut-track point or a simulation step repaints only the live items over
  the cached picture of the drawing: its cost does not grow with the drawing (it was a whole
  repaint, 261 ms at 2,700 parts) nor with the length of the track (+103 ms after an hour).
* A new toolpath does not rebuild the drawing (a full rebuild walked every contour again) and
  its overlay is built in slices of the event loop (75 ms in one go at 2,700 parts).
* A resize in fit mode does not compute the drawing's bounding box again.

Bounds are relative to work measured in the same run (a whole repaint, a short track) with
clear headroom, plus :func:`test_large_drawing.runner_budget` for the absolute ones; the
decisive checks are counts (contour paths recomputed, bounding boxes computed, pictures
painted)."""

from __future__ import annotations

import math
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest  # noqa: E402

pytest.importorskip("PySide6")

from test_large_drawing import big_document, runner_budget  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _canvas(app, n: int):
    from mlaser.cam.toolpath import CamParams, build_toolpath
    from mlaser.ui.canvas import CanvasView
    doc = big_document(n)
    v = CanvasView()
    v.resize(900, 640)
    v.show()
    v.set_document(doc)
    tp = build_toolpath(doc, CamParams())
    v.set_toolpath(tp)
    v.finish_overlay()
    for _ in range(3):
        app.processEvents()
    return v, doc, tp


def _close(v) -> None:
    v.close()
    v.deleteLater()


def _best(fn, n: int = 5) -> float:
    best = math.inf
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def _whole_repaint(v) -> float:
    """A repaint of the whole scene (as every paint was before the cache)."""
    v.layer_cache = False
    try:
        return _best(v.viewport().repaint, 3)
    finally:
        v.layer_cache = True


def test_a_head_move_costs_a_fraction_of_a_whole_repaint(app):
    v, doc, _tp = _canvas(app, 2000)
    try:
        whole = _whole_repaint(v)
        v.viewport().repaint()                       # the cached picture
        k = [0]

        def tick():
            k[0] += 1
            v.set_head_position(10.0 + k[0] * 0.7, 20.0)
            app.processEvents()
            v.viewport().repaint()
        tick()
        made = dict(getattr(v, "paint_stats", {}))
        head = _best(tick, 7)
        assert head < 0.2 * whole, (head, whole)
        assert head < runner_budget(0.015), (head, whole)
        assert v.paint_stats["static"] == made["static"]    # the drawing was not painted again
    finally:
        _close(v)


def test_a_track_tick_does_not_grow_with_the_track(app):
    """One poll of a running job: a track point and the head; with a track of 30,000 points
    about as cheap as with a few hundred."""
    v, doc, _tp = _canvas(app, 300)
    try:
        pts = []
        while len(pts) < 31_000:                     # along the contours, laser off between
            for p in doc.parts:
                ring = list(p.contours[0].points())
                pts.append((*ring[0], False))
                pts.extend((x, y, True) for x, y in ring[1:])
        it = iter(pts)

        def tick():
            x, y, cut = next(it)
            v.add_track_point(x, y, cut)
            v.set_head_position(x, y)
            app.processEvents()
            v.viewport().repaint()
        for _ in range(300):
            v.add_track_point(*next(it))
        tick()
        short = _best(tick, 9)
        for _ in range(30_000 - 320):
            v.add_track_point(*next(it))
        tick()                                       # the finished chunks join the picture once
        long = _best(tick, 9)
        assert long < 2.0 * short + runner_budget(0.004), (long, short)
        assert v.track_points() >= 30_000
    finally:
        _close(v)


@pytest.mark.flaky(reruns=1)            # single slices timed on a shared runner (one rerun)
def test_a_new_toolpath_does_not_rebuild_the_drawing(app, monkeypatch):
    """Issue #1: delivering a new toolpath to a 3,200-contour drawing recomputes no contour
    path, and no single step on the GUI thread takes the whole overlay build."""
    import mlaser.ui.canvas as C
    v, doc, tp = _canvas(app, 3200)
    try:
        calls = [0]
        real = C._contour_path

        def counting(c):
            calls[0] += 1
            return real(c)
        monkeypatch.setattr(C, "_contour_path", counting)
        old_items = [it for _, _, it, _ in v._items]
        whole = _best(lambda: (setattr(v, "_tp_paths", None), v.rebuild(), v.finish_overlay()), 2)
        calls[0] = 0
        slices = []                                  # each event-loop turn of the overlay build
        real_run = v._overlay_run

        def timed_run(gen):
            t0 = time.perf_counter()
            real_run(gen)
            slices.append(time.perf_counter() - t0)
        monkeypatch.setattr(v, "_overlay_run", timed_run)
        t0 = time.perf_counter()
        v.set_toolpath(list(tp))                     # a new list: a new plan's toolpath
        sync = time.perf_counter() - t0              # (includes the first slice)
        while v.overlay_pending:
            app.processEvents()
        assert calls[0] == 0
        assert [it for _, _, it, _ in v._items] == old_items
        assert v.toolpath_items("cut") or v.toolpath_items("kerf")
        # a slice ends after the step that crosses OVERLAY_SLICE_S (a few hundred toolpath
        # items); the synchronous part adds the toolpath's cut order and a check of the drawing
        assert max(slices) < C.OVERLAY_SLICE_S + runner_budget(0.010), (slices, whole)
        # (CI, Ubuntu / Python 3.12, twice: 20 and 35 ms for the cut order and the check where
        # the reference work said 15 and 23; the rebuild this delivery replaces took 160 and
        # 350 ms there.  The bound is also a fifth of that rebuild on the same runner: a
        # delivery that rebuilds, the regression, costs all of it)
        assert sync < C.OVERLAY_SLICE_S + max(runner_budget(0.015), whole / 5), (sync, whole, slices)
    finally:
        _close(v)


def test_the_new_toolpath_shows_what_a_rebuild_showed(app):
    """The overlay after a delivery without the rebuild is the overlay a full rebuild makes."""
    from mlaser.cam.toolpath import CamParams, build_toolpath
    v, doc, tp = _canvas(app, 400)
    try:
        doc.parts[3].craft.lead_in.kind = 1
        doc.parts[3].craft.lead_in.length = 3.0
        tp2 = build_toolpath(doc, CamParams())

        def picture():
            out = []
            for it in v._overlay:
                d = it.data(0)
                path = it.path() if hasattr(it, "path") else None
                out.append((type(it).__name__, d, it.zValue(), it.pos().toTuple(),
                            path.elementCount() if path is not None else None,
                            path.boundingRect().getRect() if path is not None else None))
            return out
        v.set_toolpath(tp2)
        v.finish_overlay()
        fast = (picture(), list(v.toolpath_marks), list(v._order))
        v.rebuild()
        v.finish_overlay()
        assert (picture(), list(v.toolpath_marks), list(v._order)) == fast
    finally:
        _close(v)


def test_a_resize_in_fit_mode_does_not_walk_the_drawing(app, monkeypatch):
    """Finding 14: the fit that follows the view size used the drawing's bounding box computed
    again in Python on every resize event."""
    from mlaser.model import Document
    v, doc, _tp = _canvas(app, 2000)
    try:
        v.zoom_fit()
        calls = [0]
        real = Document.bbox

        def counting(self):
            calls[0] += 1
            return real(self)
        monkeypatch.setattr(Document, "bbox", counting)
        t0 = time.perf_counter()
        for k in range(10):
            v.resize(900 + 16 * k, 640)
        per = (time.perf_counter() - t0) / 10
        assert calls[0] <= 1
        assert v.fit_mode == "drawing"
        r = v.fit_rect()
        bx = real(doc)
        assert r.left() < bx[0] and r.right() > bx[2]
        assert per < runner_budget(0.010), per
        v.document.parts[0].contours[0].segments[0] = type(v.document.parts[0].contours[0].segments[0])(
            (-50.0, -50.0), v.document.parts[0].contours[0].segments[0].end)
        v.rebuild()                                 # an edit: the box follows
        assert v.fit_rect().left() < -50.0
    finally:
        _close(v)
