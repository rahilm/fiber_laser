"""Small, UI-level document edits (transform / align / layer / craft / ordering).

These cover the ribbon buttons that only need simple geometry. Anything that needs real
CAM (true kerf offsets, lead geometry, micro-joint splitting, nesting) belongs in
``mlaser.cam`` and is called lazily from the main window when available.
"""
from __future__ import annotations

import copy
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


def _targets(doc: Document) -> list[Part]:
    sel = [p for p in doc.parts if p.selected]
    return sel or list(doc.parts)


def _bbox(parts: list[Part]):
    bs = [p.bbox() for p in parts if p.contours]
    if not bs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def mirror(doc: Document, horizontal: bool) -> None:
    """mf68 Horizontal / mf69 Vertical mirror about the selection's bbox centre."""
    parts = _targets(doc)
    x0, y0, x1, y1 = _bbox(parts)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if horizontal:
        transform_parts(parts, lambda p: (2 * cx - p[0], p[1]), flip=True)
    else:
        transform_parts(parts, lambda p: (p[0], 2 * cy - p[1]), flip=True)


def rotate(doc: Document, degrees: float) -> None:
    """mf62/mf63/mf63_1/mf66: rotate about bbox centre (positive = CCW)."""
    parts = _targets(doc)
    x0, y0, x1, y1 = _bbox(parts)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    a = math.radians(degrees)
    ca, sa = math.cos(a), math.sin(a)
    transform_parts(parts, lambda p: (cx + (p[0] - cx) * ca - (p[1] - cy) * sa,
                                      cy + (p[0] - cx) * sa + (p[1] - cy) * ca))


def scale(doc: Document, factor: float) -> None:
    """mf591 Size presets / mf60 scale: about the selection centre (the original's anchor
    +0xb400 defaults to 5 = centre, FUN_004b20a0; docs/draw.md)."""
    parts = _targets(doc)
    x0, y0, x1, y1 = _bbox(parts)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    transform_parts(parts, lambda p: (cx + (p[0] - cx) * factor, cy + (p[1] - cy) * factor))


def scale_to(doc: Document, size_mm: float) -> None:
    """mf591 "10/50/100/200 mm" presets: scale so the larger bbox side becomes ``size_mm``."""
    x0, y0, x1, y1 = _bbox(_targets(doc))
    m = max(x1 - x0, y1 - y0)
    if m > 0.01:
        scale(doc, size_mm / m)


def translate(doc: Document, dx: float, dy: float, parts=None) -> None:
    transform_parts(parts if parts is not None else _targets(doc), lambda p: (p[0] + dx, p[1] + dy))


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
    for p in _targets(doc):
        for c in p.contours:
            c.segments = [s.reversed() for s in reversed(c.segments)]


def array(doc: Document, rows: int, cols: int, dx: float, dy: float) -> None:
    """mf79 Array (pd332..pd336): copy the selection on a grid; spacing is the gap between bboxes."""
    parts = _targets(doc)
    x0, y0, x1, y1 = _bbox(parts)
    w, h = x1 - x0 + dx, y1 - y0 + dy
    new = []
    for r in range(rows):
        for c in range(cols):
            if r == 0 and c == 0:
                continue
            for p in parts:
                q = copy.deepcopy(p)
                q.selected = False
                translate(doc, c * w, r * h, [q])
                new.append(q)
    doc.parts.extend(new)
    doc.extra.pop("order", None)


def sort_parts(doc: Document, how: str) -> None:
    """mf106..mf110/mf202/mf203/mf204 ordering. Simple bbox-based keys (UI fallback when
    ``mlaser.cam`` has no sorter). Stores the order in ``doc.extra['order']``."""
    idx = list(range(len(doc.parts)))
    bb = {i: doc.parts[i].bbox() for i in idx}
    if how == "left_right":
        idx.sort(key=lambda i: (bb[i][0], bb[i][1]))
    elif how == "right_left":
        idx.sort(key=lambda i: (-bb[i][2], bb[i][1]))
    elif how == "bottom_top":
        idx.sort(key=lambda i: (bb[i][1], bb[i][0]))
    elif how == "top_bottom":
        idx.sort(key=lambda i: (-bb[i][3], bb[i][0]))
    elif how == "small_first":
        idx.sort(key=lambda i: (bb[i][2] - bb[i][0]) * (bb[i][3] - bb[i][1]))
    elif how in ("nearest", "in_out", "out_in"):
        pos, rest, out = (0.0, 0.0), set(idx), []
        while rest:
            i = min(rest, key=lambda k: math.dist(pos, doc.parts[k].contours[0].start_point())
                    if doc.parts[k].contours else 0.0)
            out.append(i)
            rest.discard(i)
            if doc.parts[i].contours:
                pos = doc.parts[i].contours[0].start_point()
        idx = out
    doc.parts = [doc.parts[i] for i in idx]
    doc.extra.pop("order", None)


def apply_lead(doc: Document, lead_in, lead_out, closed_only: bool = True,
               start_mode: int = 0) -> int:
    """引线 (lead) on the selected parts (all parts when none is selected).

    O (parity harness ``app_lead``, the original MainApp on basic_r2000.dxf with the dialog
    defaults):

    * "只对封闭图形有效" (closed graphs only, on by default): open contours keep their lead;
      ``closed_only=False`` applies to every target (used to clear leads);
    * the start point is chosen and **stored** when the lead is applied (``start_param`` of
      the contour, ``Craft.extra['start_param']`` here), with the dialog's start mode
      (default 0, "自动选择起点位置(长边优先)", :func:`mlaser.cam.ordering.choose_start`);
      clearing leads (both kinds 0) leaves the start alone.

    Parts with several contours get per-contour crafts (:func:`mlaser.cam.manual.contour_craft`,
    honoured by the toolpath), so each contour keeps its own start.  Returns the number of
    parts changed.
    """
    from mlaser.cam.ordering import choose_start
    set_start = bool(lead_in.kind or lead_out.kind)
    n = 0
    for p in _targets(doc):
        idx = [i for i, c in enumerate(p.contours) if c.closed or not closed_only]
        if not idx:
            continue
        if len(p.contours) == 1:
            crafts = [(0, p.craft)]
        else:
            from mlaser.cam.manual import contour_craft
            if len(idx) == len(p.contours):     # keep the part craft in step for the .chf writer
                p.craft.lead_in = copy.deepcopy(lead_in)
                p.craft.lead_out = copy.deepcopy(lead_out)
            crafts = [(i, contour_craft(p, i, create=True)) for i in idx]
        for i, cr in crafts:
            cr.lead_in = copy.deepcopy(lead_in)
            cr.lead_out = copy.deepcopy(lead_out)
            c = p.contours[i]
            L = c.length()
            if set_start and c.closed and L > 0:
                cr.extra = dict(cr.extra or {})
                cr.extra["start_param"] = (choose_start(c, start_mode, lead_in.length) % L) / L
        n += 1
    return n


def apply_kerf(doc: Document, kind: int, width: float) -> int:
    n = 0
    for p in _targets(doc):
        p.craft.kerf_comp = kind
        p.craft.kerf_width = width
        n += 1
    return n


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
