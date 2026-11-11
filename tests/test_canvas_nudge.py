"""A move of the selection moves the canvas's items (perf review 2026-11 finding 6, the canvas's
part: a select-all nudge of 2,700 parts rebuilt the canvas, 100-140 ms, most of it flattening
every contour again and building the per-contour markers anew).

A rigid move (``DocumentService.changed.shift``) translates the contours' display paths and,
when every part moved, the markers; with a toolpath the overlay is the plan, which the move
does not change until the new plan arrives.  What is on screen is what a full rebuild shows
(the item geometry, and the pixels); a move that is not the shift it was announced as is drawn
from the geometry.  The checks are counts and equality, no clock."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest  # noqa: E402

pytest.importorskip("PySide6")

from mlaser.cad import arrange as AR  # noqa: E402
from mlaser.services.document import DocumentService  # noqa: E402

from test_large_drawing import big_document  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _setup(app, n=60, toolpath=False):
    from mlaser.ui.canvas import CanvasView
    docs = DocumentService(big_document(n))
    v = CanvasView()
    v.resize(640, 480)
    v.show()
    v.set_document(docs.document)
    if toolpath:
        from mlaser.cam.toolpath import CamParams, build_toolpath
        v.set_toolpath(build_toolpath(docs.document, CamParams()))
    v.finish_overlay()
    app.processEvents()

    def changed(kind):
        v.document = docs.document
        v.rebuild(docs.changed)
        v.finish_overlay()
    docs.subscribe(changed)
    return docs, v


def _nudge(docs, parts, dx, dy):
    return docs.edit_geometry(lambda d: AR.translate(parts, dx, dy), "nudge", parts=parts, coalesce=True,
                              shift=(dx, dy))


def _geometry(v):
    """(part, contour, path bounds, points) of every contour item, rounded to 1e-6 mm."""
    def r(x):
        return round(x, 6)
    out = []
    for pi, ci, it, pts in v._items:
        b = it.path().boundingRect()
        out.append((pi, ci, r(b.x()), r(b.y()), r(b.width()), r(b.height()),
                    tuple((r(x), r(y)) for x, y in pts)))
    return out


def _overlay(v):
    """Every overlay item: its kind and where it is drawn (scene bounds, rounded)."""
    out = []
    for it in v._overlay:
        fixed = bool(it.flags() & it.GraphicsItemFlag.ItemIgnoresTransformations)
        b = None if fixed else it.sceneBoundingRect()       # (a fixed-size marker: its place)
        p = it.scenePos()
        out.append((type(it).__name__, it.data(0)) + ((round(p.x(), 6), round(p.y(), 6)) if fixed else ()) +
                   (None if b is None else (round(b.x(), 6), round(b.y(), 6), round(b.width(), 6),
                                            round(b.height(), 6)),))
    return sorted(out, key=repr)


def _fresh(app, doc, toolpath=None):
    """A canvas that built ``doc`` from nothing (the reference)."""
    from mlaser.ui.canvas import CanvasView
    w = CanvasView()
    w.resize(640, 480)
    w.show()
    w.set_document(doc)
    if toolpath is not None:
        w.set_toolpath(toolpath)
    w.finish_overlay()
    app.processEvents()
    return w


def _image(app, v):
    """The view's pixels at a fixed view (fit to the table: the same for both canvases)."""
    v.zoom_fit_table()
    app.processEvents()
    return v.grab().toImage()


def test_select_all_nudge_moves_the_items_and_flattens_nothing(app, monkeypatch):
    import mlaser.ui.canvas as C
    docs, v = _setup(app)
    doc = docs.document
    items = [it for _, _, it, _ in v._items]
    overlay = list(v._overlay)
    calls = []
    real = C._contour_path
    monkeypatch.setattr(C, "_contour_path", lambda c: (calls.append(c), real(c))[1])
    steps = []
    real_steps = C.CanvasView._overlay_steps
    monkeypatch.setattr(C.CanvasView, "_overlay_steps", lambda self: (steps.append(1), real_steps(self))[1])
    for dx, dy in ((1.0, 0.0), (0.1, 0.0), (0.0, -0.3)):
        _nudge(docs, list(doc.parts), dx, dy)
    monkeypatch.undo()
    assert calls == [] and steps == []                      # nothing flattened, no overlay built
    assert [it for _, _, it, _ in v._items] == items         # the same items, moved
    assert v._overlay == overlay
    ref = _fresh(app, doc)
    try:
        assert _geometry(v) == _geometry(ref)
        assert _overlay(v) == _overlay(ref)
        assert _image(app, v) == _image(app, ref)
        # picking and the simulation follow the moved drawing
        x, y = doc.parts[7].contours[0].segments[3].start
        assert v.pick(x, y) == ref.pick(x, y) == 7
        sim_a, sim_b = v.simulation_path(), ref.simulation_path()
        assert len(sim_a) == len(sim_b)
        assert all(abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9 and a[2] == b[2]
                   for a, b in zip(sim_a, sim_b))
        bb = doc.parts[7].bbox()
        assert all(abs(a - b) < 1e-9 for a, b in zip(v.part_bbox(doc.parts[7]), bb))
    finally:
        ref.close()
    # undo of the coalesced step: back to the very start, again without flattening
    docs.undo()
    ref = _fresh(app, doc)
    try:
        assert _geometry(v) == _geometry(ref)
        assert _overlay(v) == _overlay(ref)
    finally:
        ref.close()
        v.close()


def test_a_partial_nudge_moves_the_parts_and_rebuilds_the_markers(app):
    docs, v = _setup(app)
    doc = docs.document
    _nudge(docs, doc.parts[3:9], 2.5, -1.25)
    ref = _fresh(app, doc)
    try:
        assert _geometry(v) == _geometry(ref)
        assert _overlay(v) == _overlay(ref)
    finally:
        ref.close()
        v.close()


def test_with_a_toolpath_the_plan_stays_until_the_new_one(app, monkeypatch):
    import mlaser.ui.canvas as C
    docs, v = _setup(app, toolpath=True)
    doc = docs.document
    tp = v.toolpath
    overlay = list(v._overlay)
    assert v.toolpath_items("pierce") or v.toolpath_items("cut") or v.toolpath_items("kerf")
    steps = []
    real_steps = C.CanvasView._overlay_steps
    monkeypatch.setattr(C.CanvasView, "_overlay_steps", lambda self: (steps.append(1), real_steps(self))[1])
    _nudge(docs, list(doc.parts), 1.0, 2.0)
    assert steps == [] and v._overlay == overlay             # the stale plan, as a rebuild drew it
    monkeypatch.undo()
    ref = _fresh(app, doc, tp)
    try:
        assert _geometry(v) == _geometry(ref)
        assert _overlay(v) == _overlay(ref)
    finally:
        ref.close()
    # the parts' boxes follow the parts: with them shown the overlay is built again
    v.options.show_bbox = True
    v.rebuild()
    v.finish_overlay()
    _nudge(docs, list(doc.parts), -1.0, 0.0)
    ref = _fresh(app, doc, tp)
    ref.options.show_bbox = True
    ref.rebuild()
    ref.finish_overlay()
    try:
        assert _overlay(v) == _overlay(ref)
    finally:
        ref.close()
        v.close()


def test_a_shift_that_does_not_match_is_drawn_from_the_geometry(app):
    """A view hint that does not match the geometry (a move by another amount) never shows a
    wrong drawing: the contours are made from their segments."""
    docs, v = _setup(app)
    doc = docs.document
    parts = list(doc.parts)
    docs.edit_geometry(lambda d: AR.translate(parts, 3.0, 0.0), "nudge", parts=parts, shift=(1.0, 0.0))
    ref = _fresh(app, doc)
    try:
        assert _geometry(v) == _geometry(ref)
        assert _overlay(v) == _overlay(ref)
    finally:
        ref.close()
        v.close()


def test_a_nudge_while_the_markers_are_still_built(app, monkeypatch):
    """A large drawing's markers are built in event-loop slices; nudges that come before they
    are complete (key repeat) move what is built and place the rest moved too."""
    import mlaser.ui.canvas as C
    monkeypatch.setattr(C, "OVERLAY_SLICE_S", 0.0)          # one step per slice: always pending
    docs = DocumentService(big_document(400))
    doc = docs.document
    v = C.CanvasView()
    v.resize(640, 480)
    v.show()
    v.options.show_bbox = True
    v.set_document(doc)
    assert v.overlay_pending
    docs.subscribe(lambda kind: (setattr(v, "document", docs.document), v.rebuild(docs.changed)))
    steps = []
    real_steps = C.CanvasView._overlay_steps
    monkeypatch.setattr(C.CanvasView, "_overlay_steps", lambda self: (steps.append(1), real_steps(self))[1])
    for dx, dy in ((1.0, 0.0), (0.5, -2.0)):
        app.processEvents()                                  # a few more items built
        _nudge(docs, list(doc.parts), dx, dy)
        assert v.overlay_pending
    assert steps == []
    v.finish_overlay()
    monkeypatch.setattr(C, "OVERLAY_SLICE_S", 0.012)
    ref = C.CanvasView()
    ref.resize(640, 480)
    ref.options.show_bbox = True
    ref.set_document(doc)
    ref.finish_overlay()
    try:
        assert _geometry(v) == _geometry(ref)
        assert _overlay(v) == _overlay(ref)
    finally:
        ref.close()
        v.close()
