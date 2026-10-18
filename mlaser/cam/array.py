"""Array (阵列, mf79) and fill (圆填充 mf100_1 / 直线填充 A250707_10) operations.

Evidence (docs/cam.md §14):

* **Rectangular array**, CADModule ``0x100b92b0`` (CCADModule vtable slot 73 = +0x124,
  thunk ``0x100f2ea0``), called by MainApp ``0x004b29b0`` with the GraphParam block at
  ParaModule struct +0xb3b8 (O, call-site assembly 0x4b2a3a..0x4b2ab0)::

      vt124(rows=GRP.ArrayRowNum, cols=GRP.ArrayColNum,
            x_positive = (GRP.ColDir == 1 "Right"), y_positive = (GRP.RowDir == 1 "Up"),
            spacing = (GRP.OffsetType != 0 "Spacing"),
            x_step = GRP.ColGapVct, y_step = GRP.RowGapVct)

  - step_x = (+1|-1)*ColGap, step_y = (+1|-1)*RowGap; with *spacing* the selection bbox
    width/height is added (so the value is the gap between copies), otherwise the value is
    the pitch ("offset").
  - copies are made row by row; even rows run left->right (col*step_x), odd rows run
    right->left ((cols-1-col)*step_x): a serpentine creation order. Cell (0,0) is the
    original and is not copied.
  - MainApp forces OffsetType = 1 (spacing) every time before opening the dialog.
  - The total element count rows*cols*n is checked by 0x100da250 (memory guard, mf505).
* **Fill circle / linear fill**: CFillCircle (vtable 0x101219a0), driver ``0x100ce2d0``
  (rtype, radius, stock, space, angle_deg, keep_original), per-region setup ``0x1002f390``:
  fill type (+0xc) 0 / 1 = scan-line fill (``0x1002aec0(this, 1|0)``), 2 = along edge
  (``0x1002dd70``), 3 = straight-line hatch rotated by the angle (``0x1002e210``).
  Object fields: +0x10 stock (margin), +0x18 space, +0x20 radius, +0x28 angle (rad).
  Scan lines start at bbox.min + stock + r and advance by 2r + space (0.1 mm when a line
  misses the region). Along each inside interval [a, b] the first and last valid centres are
  searched from a + stock + r forward and b - stock - r backward in steps of 0.2*space; a
  centre is valid if a circle of radius r + 0.5*stock clears the outer contour and every hole
  (0x10026880). The run is then divided evenly (0x100292d0); each centre must also be at least
  2r + 0.7*space from already placed circles (0x10026a10).

**Circular array** and **fill-sheet array** have no counterpart in the original (no lang
key or command found); they are rebuild additions using the same copy semantics.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ..model import Arc, Contour, Document, Line, Part, Point

__all__ = ["ArrayParams", "rect_array", "apply_rect_array", "circular_array",
           "fill_sheet_array", "FillParams", "fill_circles", "linear_fill",
           "transform_part", "parts_bbox"]


# --------------------------------------------------------------------------- helpers
def _map_seg(s, f, flip: bool):
    if isinstance(s, Line):
        return Line(f(s.start), f(s.end))
    return Arc(f(s.start), f(s.end), f(s.center), (not s.ccw) if flip else s.ccw)


def transform_part(p: Part, f, flip: bool = False) -> Part:
    """Deep copy of ``p`` with every point mapped by ``f`` (``flip`` = mirror)."""
    q = copy.deepcopy(p)
    q.selected = False
    for c in q.contours:
        c.segments = [_map_seg(s, f, flip) for s in c.segments]
    return q


def parts_bbox(parts: Sequence[Part]) -> tuple[float, float, float, float]:
    bs = [p.bbox() for p in parts if p.contours]
    if not bs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def _translated(p: Part, dx: float, dy: float) -> Part:
    return transform_part(p, lambda q: (q[0] + dx, q[1] + dy))


# --------------------------------------------------------------------------- rectangular
@dataclass
class ArrayParams:
    """GraphParam array block (ManuPara.xml, defaults from the parameter table)."""
    rows: int = 3                 # pd332 GRP.ArrayRowNum  [1, 10000]
    cols: int = 3                 # pd333 GRP.ArrayColNum  [1, 10000]
    offset_type: int = 1          # pd334 GRP.OffsetType   0 offset (pitch), 1 spacing (gap)
    row_gap: float = 10.0         # pd335 GRP.RowGapVct    (Y)
    col_gap: float = 10.0         # pd336 GRP.ColGapVct    (X)
    row_dir: int = 1              # pd337 GRP.RowDir       0 down, 1 up
    col_dir: int = 1              # pd338 GRP.ColDir       0 left, 1 right

    MAX_ITEMS = 2_000_000         # stand-in for the 0x100da250 memory guard (I)


def array_steps(parts: Sequence[Part], prm: ArrayParams) -> tuple[float, float]:
    """(step_x, step_y) exactly as 0x100b92b0 computes them."""
    sx = 1.0 if prm.col_dir == 1 else -1.0
    sy = 1.0 if prm.row_dir == 1 else -1.0
    step_x, step_y = sx * prm.col_gap, sy * prm.row_gap
    if prm.offset_type != 0:
        x0, y0, x1, y1 = parts_bbox(parts)
        step_x += (x1 - x0) * sx
        step_y += (y1 - y0) * sy
    return step_x, step_y


def rect_array(parts: Sequence[Part], prm: ArrayParams) -> list[Part]:
    """Copies of ``parts`` on a rows x cols grid, in the original's serpentine order.

    The originals stay where they are (cell 0,0) and are not returned.
    """
    parts = [p for p in parts if p.contours]
    if not parts or prm.rows < 1 or prm.cols < 1:
        return []
    if prm.rows * prm.cols * len(parts) > ArrayParams.MAX_ITEMS:
        raise MemoryError("array too large (mf505)")
    step_x, step_y = array_steps(parts, prm)
    out: list[Part] = []
    for r in range(prm.rows):
        for k in range(prm.cols):
            col = k if r % 2 == 0 else prm.cols - 1 - k
            if r == 0 and col == 0:
                continue
            for p in parts:
                q = _translated(p, col * step_x, r * step_y)
                q.extra = dict(q.extra)
                q.extra["array_cell"] = (r, col)
                out.append(q)
    return out


def _targets(doc: Document) -> list[Part]:
    sel = [p for p in doc.parts if p.selected]
    return sel or list(doc.parts)


def apply_rect_array(doc: Document, prm: ArrayParams) -> int:
    """Append a rectangular array of the selection (or of everything) to ``doc``."""
    new = rect_array(_targets(doc), prm)
    doc.parts.extend(new)
    doc.extra.pop("order", None)
    return len(new)


# --------------------------------------------------------------------------- circular (rebuild)
def circular_array(parts: Sequence[Part], count: int, center: Point,
                   total_angle_deg: float = 360.0, rotate_items: bool = True) -> list[Part]:
    """``count`` instances around ``center`` (the original is instance 0, not returned).

    Rebuild addition (no counterpart found in CADModule/MainApp). With a full 360 deg the
    angular pitch is 360/count, otherwise total/(count-1) so the last copy lands on the end
    angle. ``rotate_items=False`` translates copies without turning them.
    """
    parts = [p for p in parts if p.contours]
    if count < 2 or not parts:
        return []
    full = abs(abs(total_angle_deg) - 360.0) < 1e-9
    pitch = math.radians(total_angle_deg) / (count if full else count - 1)
    cx, cy = center
    x0, y0, x1, y1 = parts_bbox(parts)
    bx, by = (x0 + x1) / 2, (y0 + y1) / 2
    out = []
    for i in range(1, count):
        a = pitch * i
        c, s = math.cos(a), math.sin(a)
        if rotate_items:
            f = lambda q, c=c, s=s: (cx + (q[0] - cx) * c - (q[1] - cy) * s,
                                     cy + (q[0] - cx) * s + (q[1] - cy) * c)
        else:
            nx = cx + (bx - cx) * c - (by - cy) * s
            ny = cy + (bx - cx) * s + (by - cy) * c
            f = lambda q, dx=nx - bx, dy=ny - by: (q[0] + dx, q[1] + dy)
        for p in parts:
            q = transform_part(p, f)
            q.extra = dict(q.extra)
            q.extra["array_index"] = i
            out.append(q)
    return out


# --------------------------------------------------------------------------- fill sheet (rebuild)
def fill_sheet_array(parts: Sequence[Part], sheet: tuple[float, float, float, float],
                     gap: float = 5.0, margin: float = 5.0,
                     max_rows: Optional[int] = None, max_cols: Optional[int] = None) -> list[Part]:
    """Fill the sheet rectangle (x0, y0, x1, y1) with copies of the selection's bbox cell.

    Rebuild addition. The group is first moved so its bbox sits at the sheet corner plus
    ``margin``; the grid then uses spacing mode (gap between bboxes) like 0x100b92b0. All
    instances, including the first (moved) one, are returned in serpentine order.
    """
    parts = [p for p in parts if p.contours]
    if not parts:
        return []
    bx0, by0, bx1, by1 = parts_bbox(parts)
    w, h = bx1 - bx0, by1 - by0
    sx0, sy0, sx1, sy1 = sheet
    aw, ah = (sx1 - sx0) - 2 * margin, (sy1 - sy0) - 2 * margin
    if w > aw + 1e-9 or h > ah + 1e-9:
        return []
    cols = int(math.floor((aw + gap) / (w + gap) + 1e-9))
    rows = int(math.floor((ah + gap) / (h + gap) + 1e-9))
    if max_cols:
        cols = min(cols, max_cols)
    if max_rows:
        rows = min(rows, max_rows)
    dx0, dy0 = sx0 + margin - bx0, sy0 + margin - by0
    base = [_translated(p, dx0, dy0) for p in parts]
    out = [copy.deepcopy(b) for b in base]
    for b in out:
        b.extra = dict(b.extra)
        b.extra["array_cell"] = (0, 0)
    out += rect_array(base, ArrayParams(rows=rows, cols=cols, offset_type=1,
                                        row_gap=gap, col_gap=gap, row_dir=1, col_dir=1))
    return out


# --------------------------------------------------------------------------- fill circle / linear fill
@dataclass
class FillParams:
    """CFillCircle parameters (GraphParam, ManuPara.xml)."""
    fill_type: int = 0            # pd725 GRP.FillGlyDir 0 X, 1 Y, 2 along edge; 3 = linear fill
    radius: float = 4.0           # pd726 GRP.FillCircleRadius [0.1, 500]
    stock: float = 10.0           # pd727 GRP.FillCircleStock  [0.1, 300] (margin to the boundary)
    space: float = 10.0           # pd728 GRP.FillCircleSpace  [0.1, 300] (gap between circles)
    angle_deg: float = 0.0        # A250707_3 linear fill line angle
    keep_original: bool = True    # A250707_6


def _region(outer: Contour, holes: Iterable[Contour] = ()):
    from shapely.geometry import Polygon
    shell = list(outer.points(0.01))
    hs = [list(h.points(0.01)) for h in holes]
    poly = Polygon(shell, [h for h in hs if len(h) >= 3])
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly


def _circle_part(c: Point, r: float, layer: Optional[int] = None) -> Part:
    p0 = (c[0] + r, c[1])
    part = Part(contours=[Contour([Arc(p0, p0, c, True)], True)], name="fill circle")
    if layer is not None:
        part.craft.layer = layer
    return part


def fill_circle_centres(outer: Contour, holes: Sequence[Contour], prm: FillParams) -> list[Point]:
    """Centres of the fill circles (types 0, 1 and 2), see module docstring."""
    from shapely.geometry import LineString, Point as SP
    poly = _region(outer, holes)
    if poly.is_empty:
        return []
    r, st, sp = prm.radius, prm.stock, prm.space
    clear = r + 0.5 * st                     # 0x100292d0: local_5c = stock*0.5 + r
    min_dist = 2 * r + 0.7 * sp              # 0x10026a10 neighbour distance
    pitch = 2 * r + sp
    boundary = poly.boundary

    def valid(q: Point) -> bool:
        pt = SP(q)
        return poly.contains(pt) and boundary.distance(pt) >= clear - 1e-9

    placed: list[Point] = []

    def far(q: Point) -> bool:
        return all(math.dist(q, o) >= min_dist - 1e-9 for o in placed)

    if prm.fill_type == 2:
        # along edge (0x1002dd70, I): rings of inward offsets, circles at pitch along each ring
        d = clear
        while True:
            ring = poly.buffer(-d, join_style=2)
            if ring.is_empty:
                break
            geoms = getattr(ring, "geoms", [ring])
            for g in geoms:
                for lr in [g.exterior] + list(g.interiors):
                    L = lr.length
                    n = max(1, int(math.floor(L / pitch + 1e-9)))
                    for k in range(n):
                        q = lr.interpolate(k * L / n)
                        q = (q.x, q.y)
                        if valid(q) and far(q):
                            placed.append(q)
            d += pitch
        return placed

    x0, y0, x1, y1 = poly.bounds
    along_x = prm.fill_type == 1             # type 1: horizontal scan lines (rows)
    lo, hi = (y0, y1) if along_x else (x0, x1)
    v = lo + st + r
    vmax = hi - st - r + 0.1
    span_lo, span_hi = (x0 - 1, x1 + 1) if along_x else (y0 - 1, y1 + 1)
    while v < vmax:
        line = LineString([(span_lo, v), (span_hi, v)] if along_x else [(v, span_lo), (v, span_hi)])
        inter = poly.intersection(line)
        segs = [g for g in getattr(inter, "geoms", [inter]) if g.geom_type == "LineString" and not g.is_empty]
        if not segs:
            v += 0.1
            continue
        for g in segs:
            cs = [c[0] if along_x else c[1] for c in g.coords]
            a, b = min(cs), max(cs)
            if b - a < 2 * (st + r):
                continue

            def pt(u):
                return (u, v) if along_x else (v, u)
            step = max(0.2 * sp, 1e-3)
            first = a + st + r
            while first < b - st - r and not valid(pt(first)):
                first += step
            last = b - st - r
            while last > first and not valid(pt(last)):
                last -= step
            span = last - first
            if span < -0.1 * sp or not valid(pt(first)):
                continue
            n = min(int(round(span / pitch)), int(math.floor(span / min_dist + 1e-9)))
            us = [(first + last) / 2] if n < 1 else [first + span * k / n for k in range(n + 1)]
            for u in us:
                q = pt(u)
                if valid(q) and far(q):
                    placed.append(q)
        v += pitch
    return placed


def fill_circles(outer: Contour, holes: Sequence[Contour] = (), prm: Optional[FillParams] = None,
                 layer: Optional[int] = None) -> list[Part]:
    """CFillCircle (0x100ce2d0, types 0..2): one circle Part per fill position."""
    prm = prm or FillParams()
    return [_circle_part(c, prm.radius, layer) for c in fill_circle_centres(outer, holes, prm)]


def linear_fill(outer: Contour, holes: Sequence[Contour] = (), prm: Optional[FillParams] = None,
                layer: Optional[int] = None) -> list[Part]:
    """Linear fill (type 3, 0x1002e210): hatch lines at ``angle_deg``, ``space`` apart,
    kept ``stock`` away from the boundary. Lines alternate direction (S order, I)."""
    from shapely import affinity
    from shapely.geometry import LineString
    prm = prm or FillParams(fill_type=3)
    poly = _region(outer, holes)
    if poly.is_empty or prm.space <= 0:
        return []
    inner = poly.buffer(-prm.stock, join_style=2) if prm.stock > 0 else poly
    if inner.is_empty:
        return []
    c = inner.centroid
    rot = affinity.rotate(inner, -prm.angle_deg, origin=c)
    x0, y0, x1, y1 = rot.bounds
    out: list[Part] = []
    y = y0 + prm.space / 2
    k = 0
    while y < y1:
        inter = rot.intersection(LineString([(x0 - 1, y), (x1 + 1, y)]))
        segs = sorted((g for g in getattr(inter, "geoms", [inter])
                       if g.geom_type == "LineString" and g.length > 1e-6),
                      key=lambda g: min(p[0] for p in g.coords))
        if k % 2:
            segs.reverse()
        for g in segs:
            g = affinity.rotate(g, prm.angle_deg, origin=c)
            a, b = g.coords[0], g.coords[-1]
            if k % 2:
                a, b = b, a
            part = Part(contours=[Contour([Line((a[0], a[1]), (b[0], b[1]))], False)], name="fill line")
            if layer is not None:
                part.craft.layer = layer
            out.append(part)
        y += prm.space
        k += 1
    return out
