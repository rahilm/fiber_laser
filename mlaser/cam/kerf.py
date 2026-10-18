"""Kerf (beam width) compensation, port of CADModule FUN_100738b0 -> 10072540 -> 1003ad00.

Gates: closed contour, max(bbox w, h) >= 3*width, 0.01 <= |d| <= 200.

Mode -> sign s (depth 1 = outermost):

====  ===============================  =====================
mode  meaning                          s
====  ===============================  =====================
-1    none
0     outermost shrinks, alternate     -(+1 if depth odd else -1)
1     outermost expands, alternate     +1 if depth odd else -1
2     all shrink                       -1
3     all expand                       +1
====  ===============================  =====================

If the contour is CW, s = -s.  Signed offset d = s*width, where width is the
stored kerf value (contour+0x108) used directly as the offset distance
(whether the UI value is the full beam width or half of it: UNKNOWN).
Positive d offsets to the RIGHT of travel.

Primitive offsets: line endpoints move by d*(uy, -ux); CCW arc r += d, CW arc
r -= d, dropped if r < 0.01.  Joins: nearly tangent (dot > cos 3deg) none;
concave -> trim by intersection; convex with turn < 95deg -> miter; sharper ->
round join (arc of radius |d| around the original vertex).

When the analytic offset fails (self-intersections, vanished pieces) we fall
back to shapely's buffer (arcs are then lost and become polylines).
"""
from __future__ import annotations

import math
from typing import Optional

from ..model import Arc, Contour, Line, Point, Segment
from .geometry import contour_bbox, is_ccw, seg_tangent, turn


def kerf_corner_join(*a):
    from .corners import kerf_corner_join as f   # lazy: corners imports nothing from kerf, keep it so
    return f(*a)

COS3 = math.cos(math.radians(3.0))
MITER_LIMIT = math.radians(95.0)


def kerf_sign(mode: int, depth: int, ccw: bool) -> int:
    if mode == 0:
        s = -(1 if depth % 2 == 1 else -1)
    elif mode == 1:
        s = 1 if depth % 2 == 1 else -1
    elif mode == 2:
        s = -1
    elif mode == 3:
        s = 1
    else:
        return 0
    return s if ccw else -s


def _off_seg(s: Segment, d: float) -> Optional[Segment]:
    if isinstance(s, Line):
        dx, dy = s.end[0] - s.start[0], s.end[1] - s.start[1]
        L = math.hypot(dx, dy)
        if L < 1e-12:
            return None
        nx, ny = dy / L * d, -dx / L * d
        return Line((s.start[0] + nx, s.start[1] + ny), (s.end[0] + nx, s.end[1] + ny))
    r = s.radius
    r2 = r + d if s.ccw else r - d   # right of a CCW arc is outward
    if r2 < 0.01:
        return None
    k = r2 / r
    c = s.center
    p0 = (c[0] + (s.start[0] - c[0]) * k, c[1] + (s.start[1] - c[1]) * k)
    p1 = (c[0] + (s.end[0] - c[0]) * k, c[1] + (s.end[1] - c[1]) * k)
    return Arc(p0, p1, c, s.ccw)


def _line_circle(p, v, c, r):
    """Intersections of line p + t v with circle (c, r) -> list of t."""
    fx, fy = p[0] - c[0], p[1] - c[1]
    a = v[0] ** 2 + v[1] ** 2
    b = 2 * (fx * v[0] + fy * v[1])
    cc = fx * fx + fy * fy - r * r
    disc = b * b - 4 * a * cc
    if disc < 0 or a == 0:
        return []
    sq = math.sqrt(disc)
    return [(-b - sq) / (2 * a), (-b + sq) / (2 * a)]


def _intersections(a: Segment, b: Segment) -> list[Point]:
    """Intersections of the supporting line/circles of a and b."""
    if isinstance(a, Line) and isinstance(b, Line):
        p, r = a.start, (a.end[0] - a.start[0], a.end[1] - a.start[1])
        q, s = b.start, (b.end[0] - b.start[0], b.end[1] - b.start[1])
        den = r[0] * s[1] - r[1] * s[0]
        if abs(den) < 1e-14:
            return []
        t = ((q[0] - p[0]) * s[1] - (q[1] - p[1]) * s[0]) / den
        return [(p[0] + r[0] * t, p[1] + r[1] * t)]
    if isinstance(a, Arc) and isinstance(b, Line):
        a, b = b, a
    if isinstance(a, Line) and isinstance(b, Arc):
        v = (a.end[0] - a.start[0], a.end[1] - a.start[1])
        return [(a.start[0] + v[0] * t, a.start[1] + v[1] * t) for t in _line_circle(a.start, v, b.center, b.radius)]
    # circle-circle
    c0, r0, c1, r1 = a.center, a.radius, b.center, b.radius
    dd = math.dist(c0, c1)
    if dd < 1e-12 or dd > r0 + r1 or dd < abs(r0 - r1):
        return []
    x = (dd * dd + r0 * r0 - r1 * r1) / (2 * dd)
    h = math.sqrt(max(r0 * r0 - x * x, 0))
    ux, uy = (c1[0] - c0[0]) / dd, (c1[1] - c0[1]) / dd
    m = (c0[0] + ux * x, c0[1] + uy * x)
    return [(m[0] - uy * h, m[1] + ux * h), (m[0] + uy * h, m[1] - ux * h)]


def _with_end(s: Segment, p: Point) -> Segment:
    return Line(s.start, p) if isinstance(s, Line) else Arc(s.start, p, s.center, s.ccw)


def _with_start(s: Segment, p: Point) -> Segment:
    return Line(p, s.end) if isinstance(s, Line) else Arc(p, s.end, s.center, s.ccw)


def offset_contour(c: Contour, d: float) -> Optional[Contour]:
    """Analytic offset of a closed contour by signed distance d (+ = right of travel)."""
    segs = [s for s in c.segments if s.length() > 1e-9]
    n = len(segs)
    if n == 0:
        return None
    if n == 1 and isinstance(segs[0], Arc):
        o = _off_seg(segs[0], d)
        return Contour([o], True) if o else None
    off = [_off_seg(s, d) for s in segs]
    if any(o is None for o in off):
        return None
    out: list[Segment] = []
    pieces = list(off)
    # resolve joins: for each vertex i (between seg i and i+1)
    starts = [p.start for p in pieces]
    ends = [p.end for p in pieces]
    joins: list[list[Segment]] = [[] for _ in range(n)]
    for i in range(n):
        j = (i + 1) % n
        t0 = seg_tangent(segs[i], 1.0)
        t1 = seg_tangent(segs[j], 0.0)
        dot = t0[0] * t1[0] + t0[1] * t1[1]
        V = segs[i].end
        if dot > COS3 and math.dist(ends[i], starts[j]) < max(abs(d) * 0.06, 1e-6):
            # tangent join: no join and no trimming, each piece keeps its own offset end points
            # (0x1003ad00).  An arc offset is the glyph's arc with r +- d over the SAME angles, so
            # its length is |a1 - a0| (r +- d) and a line keeps its exact shifted ends; on input
            # whose arc ends come from 6-decimal angles the pieces then stay a few um apart, as in
            # the original (polyline kerf 0.3: 39.7 / 30.473442 = 9.7 * 3.141592, parity JC-04).
            continue
        tr = turn(t0, t1)
        convex = (tr > 0) == (d > 0)   # turning left while offsetting right opens a gap
        if not convex:
            pts = _intersections(pieces[i], pieces[j])
            if not pts:
                return None
            p = min(pts, key=lambda q: math.dist(q, V))
            ends[i], starts[j] = p, p
        elif abs(tr) < MITER_LIMIT and isinstance(pieces[i], Line) and isinstance(pieces[j], Line):
            pts = _intersections(pieces[i], pieces[j])
            if pts:
                ends[i], starts[j] = pts[0], pts[0]
            else:
                joins[i] = [Line(ends[i], starts[j])]
        else:
            # 0x10038130: tangent arc for a symmetric corner (= round join about V),
            # cubic Bezier when the two sides are asymmetric (docs/cam.md §5, §13)
            joins[i] = kerf_corner_join(ends[i], t0, starts[j], t1) or [
                Arc(ends[i], starts[j], V, ccw=(tr > 0))]
    for i in range(n):
        p = pieces[i]
        q = _with_start(_with_end(p, ends[i]), starts[i])
        # a trimmed piece that reversed direction has vanished -> give up (fallback)
        if isinstance(q, Line):
            t_orig = seg_tangent(p, 0.0)
            v = (q.end[0] - q.start[0], q.end[1] - q.start[1])
            if v[0] * t_orig[0] + v[1] * t_orig[1] < -1e-9:
                return None
        else:
            sw0 = p.sweep() if math.dist(p.start, p.end) > 1e-9 else None
            sw1 = q.sweep()
            if sw0 is not None and abs(sw1) > abs(sw0) + math.pi:
                return None
        if q.length() > 1e-9:
            out.append(q)
        out.extend(joins[i])
    return Contour(out, True) if out else None


def _shapely_offset(c: Contour, d: float) -> Optional[Contour]:
    from shapely.geometry import Polygon
    pts = list(c.points(0.005))
    if len(pts) < 3:
        return None
    poly = Polygon(pts)
    if not poly.is_valid:
        poly = poly.buffer(0)
    ccw = is_ccw(c)
    # + d is "right of travel": outward for CW?  right of a CCW loop = outside
    grow = (d > 0) == ccw
    res = poly.buffer(abs(d) if grow else -abs(d), join_style="round", quad_segs=16)
    if res.is_empty:
        return None
    if res.geom_type == "MultiPolygon":
        res = max(res.geoms, key=lambda g: g.area)
    ring = list(res.exterior.coords)[:-1]
    if ccw != (Polygon(ring).exterior.is_ccw):
        ring.reverse()
    segs = [Line(a, b) for a, b in zip(ring, ring[1:] + ring[:1]) if math.dist(a, b) > 1e-9]
    return Contour(segs, True)


def _self_intersects(c: Contour) -> bool:
    from shapely.geometry import LinearRing
    pts = list(c.points(0.01))
    if len(pts) < 4:
        return False
    try:
        return not LinearRing(pts).is_simple
    except Exception:
        return True


def compensate(c: Contour, mode: int, width: float, depth: int = 1) -> Optional[Contour]:
    """Kerf-compensated copy of ``c`` (None if not applicable)."""
    if mode not in (0, 1, 2, 3) or not c.closed or not c.segments:
        return None
    if not 0.01 <= abs(width) <= 200:
        return None
    x0, y0, x1, y1 = contour_bbox(c)
    if max(x1 - x0, y1 - y0) < 3 * abs(width):
        return None
    ccw = is_ccw(c)
    d = kerf_sign(mode, depth, ccw) * width
    res = offset_contour(c, d)
    if res is None or _self_intersects(res):
        res = _shapely_offset(c, d)
    return res
