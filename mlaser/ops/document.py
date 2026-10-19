"""Document operations: transform, align, layer, delete, reverse, selection.

Qt-free domain operations on a :class:`mlaser.model.Document` (moved from ``mlaser.ui.docops`` in
Wave 2, "business logic out of the UI").  Every function edits the document in place; callers
that want undo run them inside ``DocumentService.edit`` (mlaser.services), whose ``parts``
scope is :func:`targets` for the operations that only touch the selection.

The ordering fallback ``sort_parts`` and the bbox-gap ``array`` that used to live here were
shadowed by :mod:`mlaser.cam.ordering` / :mod:`mlaser.cam.array` (the ribbon never reached
them) and were removed.
"""
from __future__ import annotations

import math
from typing import Callable, Iterable

from mlaser.model import Arc, Contour, Document, Line, Part

Xform = Callable[[tuple[float, float]], tuple[float, float]]


def _map_seg(s, f: Xform, flip: bool):
    if isinstance(s, Line):
        return Line(f(s.start), f(s.end))
    return Arc(f(s.start), f(s.end), f(s.center), (not s.ccw) if flip else s.ccw)


def transform_parts(parts: Iterable[Part], f: Xform, flip: bool = False) -> None:
    """Apply point map ``f`` in place. ``flip`` = orientation-reversing (mirror)."""
    for p in parts:
        for c in p.contours:
            c.segments = [_map_seg(s, f, flip) for s in c.segments]


def targets(doc: Document) -> list[Part]:
    """The parts an operation applies to: the selection, or every part when none is selected."""
    sel = [p for p in doc.parts if p.selected]
    return sel or list(doc.parts)


_targets = targets          # old private name (mlaser.ui.docops._targets)


def is_raster(p: Part) -> bool:
    """A raster engraving part (``mlaser.cam.raster_engraving``): its one contour is the frame
    of the image, not geometry to cut, so contour tools leave it alone."""
    from mlaser.cam.raster_engraving import raster_of_part
    return raster_of_part(p) is not None


def contour_targets(doc: Document) -> list[Part]:
    """:func:`targets` without raster engraving parts (for leads, kerf, joints, CAM ops).
    Transforms, alignment and layers still use :func:`targets`, so rasters move with the rest."""
    return [p for p in targets(doc) if not is_raster(p)]


def _bbox(parts: list[Part]):
    bs = [p.bbox() for p in parts if p.contours]
    if not bs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def mirror(doc: Document, horizontal: bool) -> None:
    """mf68 Horizontal / mf69 Vertical mirror about the selection's bbox centre."""
    parts = targets(doc)
    x0, y0, x1, y1 = _bbox(parts)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if horizontal:
        transform_parts(parts, lambda p: (2 * cx - p[0], p[1]), flip=True)
    else:
        transform_parts(parts, lambda p: (p[0], 2 * cy - p[1]), flip=True)


def rotate(doc: Document, degrees: float) -> None:
    """mf62/mf63/mf63_1/mf66: rotate about bbox centre (positive = CCW)."""
    parts = targets(doc)
    x0, y0, x1, y1 = _bbox(parts)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    a = math.radians(degrees)
    ca, sa = math.cos(a), math.sin(a)
    transform_parts(parts, lambda p: (cx + (p[0] - cx) * ca - (p[1] - cy) * sa,
                                      cy + (p[0] - cx) * sa + (p[1] - cy) * ca))


def scale(doc: Document, factor: float) -> None:
    """mf591 Size presets / mf60 scale: about the selection centre (the original's anchor
    +0xb400 defaults to 5 = centre, FUN_004b20a0; docs/re/draw.md)."""
    parts = targets(doc)
    x0, y0, x1, y1 = _bbox(parts)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    transform_parts(parts, lambda p: (cx + (p[0] - cx) * factor, cy + (p[1] - cy) * factor))


def scale_to(doc: Document, size_mm: float) -> None:
    """mf591 "10/50/100/200 mm" presets: scale so the larger bbox side becomes ``size_mm``."""
    x0, y0, x1, y1 = _bbox(targets(doc))
    m = max(x1 - x0, y1 - y0)
    if m > 0.01:
        scale(doc, size_mm / m)


def translate(doc: Document, dx: float, dy: float, parts=None) -> None:
    transform_parts(parts if parts is not None else targets(doc), lambda p: (p[0] + dx, p[1] + dy))


def selection_bbox(doc: Document) -> tuple[float, float, float, float]:
    """Bounding box (x0, y0, x1, y1) of :func:`targets` (properties panel)."""
    return _bbox(targets(doc))


def set_bbox(doc: Document, x: float | None = None, y: float | None = None,
             width: float | None = None, height: float | None = None) -> None:
    """Properties-panel transform of :func:`targets`: move the bounding box's lower-left corner
    to (``x``, ``y``) and/or scale **uniformly** about that corner so the width (or height)
    becomes the given size (arcs stay arcs, so there is no independent width/height)."""
    parts = targets(doc)
    x0, y0, x1, y1 = _bbox(parts)
    w, h = x1 - x0, y1 - y0
    f = 1.0
    if width is not None and w > 1e-9:
        f = float(width) / w
    elif height is not None and h > 1e-9:
        f = float(height) / h
    if f <= 0:
        raise ValueError("size must be positive")
    nx = x0 if x is None else float(x)
    ny = y0 if y is None else float(y)
    if abs(f - 1.0) > 1e-15:
        transform_parts(parts, lambda p: (nx + (p[0] - x0) * f, ny + (p[1] - y0) * f))
    elif nx != x0 or ny != y0:
        translate(doc, nx - x0, ny - y0, parts)


def align(doc: Document, how: str) -> None:
    """mf71..mf77: align selected parts to the selection bbox (left/right/hcenter/top/bottom/vcenter/center)."""
    parts = [p for p in doc.parts if p.selected and p.contours]
    if len(parts) < 2:
        return
    X0, Y0, X1, Y1 = _bbox(parts)
    for p in parts:
        x0, y0, x1, y1 = p.bbox()
        dx = dy = 0.0
        if how == "left":
            dx = X0 - x0
        elif how == "right":
            dx = X1 - x1
        elif how in ("hcenter", "center"):
            dx = (X0 + X1) / 2 - (x0 + x1) / 2
        if how == "bottom":
            dy = Y0 - y0
        elif how == "top":
            dy = Y1 - y1
        elif how in ("vcenter", "center"):
            dy = (Y0 + Y1) / 2 - (y0 + y1) / 2
        translate(doc, dx, dy, [p])


def set_layer(doc: Document, layer: int) -> int:
    n = 0
    for p in doc.parts:
        if p.selected:
            p.craft.layer = layer
            n += 1
    return n


def delete_selected(doc: Document) -> int:
    n = len(doc.parts)
    doc.parts = [p for p in doc.parts if not p.selected]
    doc.extra.pop("order", None)
    return n - len(doc.parts)


def reverse(doc: Document) -> None:
    """mf94 Reverse direction of the selected contours."""
    for p in targets(doc):
        for c in p.contours:
            c.segments = [s.reversed() for s in reversed(c.segments)]


def select_where(doc: Document, pred) -> None:
    for p in doc.parts:
        p.selected = bool(pred(p))


def demo_document() -> Document:
    """Small sample used by tests and the empty-start UI (plate with holes)."""
    def rect(x, y, w, h):
        pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)

    def circle(cx, cy, r):
        s = (cx + r, cy)
        return Contour([Arc(s, s, (cx, cy), True)], True)

    from mlaser.model import Craft
    plate = Part([circle(40, 40, 10), circle(160, 40, 10), rect(70, 25, 60, 30), rect(0, 0, 200, 80)],
                 Craft(layer=1), name="plate")
    disc = Part([circle(260, 40, 8), circle(260, 40, 35)], Craft(layer=2), name="disc")
    slot = Part([Contour([Line((320, 5), (420, 5)), Arc((420, 5), (420, 25), (420, 15), True),
                          Line((420, 25), (320, 25)), Arc((320, 25), (320, 5), (320, 15), True)], True)],
                Craft(layer=3), name="slot")
    open_line = Part([Contour([Line((320, 50), (420, 75))], False)], Craft(layer=4), name="open")
    doc = Document(parts=[plate, disc, slot, open_line])
    return doc


def paste_parts(doc: Document, clip: Iterable[Part], dx: float = 10.0, dy: float = 10.0) -> list[Part]:
    """Paste copies of ``clip`` offset by (dx, dy) and select only them (the original pastes at a
    picked point, menu 134).  Returns the new parts."""
    import copy
    for p in doc.parts:
        p.selected = False
    new = [copy.deepcopy(p) for p in clip]
    translate(doc, dx, dy, new)
    for p in new:
        p.selected = True
    doc.parts.extend(new)
    doc.extra.pop("order", None)
    return new
