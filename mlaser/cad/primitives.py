"""Shapes produced by the Draw tab tools (CADModule ``ICreateGlyph`` subclasses).

Tool index = ribbon command id - 0x334a (MainApp FUN_004b2030 -> view FUN_004d1230 ->
ICADModule vtable+0x84 = FUN_100ef350, which instantiates the tool):

=====  ===========  ==================  ============================================
index  command      class (vtable)      geometry (observed in the builder)
=====  ===========  ==================  ============================================
0      draw.point   CCreatePoint        one click (0x100832b0)
1      draw.line    CCreateSegment      two clicks
2      draw.circle  CCreateCircleOR     centre, point on circle (0x1007f850)
3      draw.circle3 CCreateCircle3Pt    three points (0x1007fb50, circumcircle 0x1007f610)
4      draw.arc3    CCreateArc3Pt       start, mid, end (0x1007ef10, 0x100098a0)
5      draw.scan_arc CCreateArcORA      centre, start (radius), end angle (0x1007e910)
6      draw.ellipse CCreateEllipse      centre, major-axis end, minor ratio (0x10080520)
7      draw.rect    CCreateRect         two corners (0x100817b0 / 0x100810e0)
8      draw.round_rect CCreateRoundRect two corners + radius point (0x10082e90)
9      draw.polygon CCreatePolygon(6)   centre, vertex (0x10081c40 / 0x100811e0)
10     draw.star    CCreateStar(6)      centre, outer vertex, inner radius (0x10081ee0 / 0x10081310)
11     draw.pline   CCreateLwploy       vertices (+ bulge arcs) until finished (0x10082900)
12     draw.curve   CCreateSpline       fit points until finished (0x10083b90)
13     draw.text    CGlyText            dialog then placement (see :mod:`mlaser.cad.text`)
=====  ===========  ==================  ============================================

Minimum feature size: a click closer than ``tol`` (tool+0x38) in both x and y to the previous
point is ignored; round-rect radii below 0.02 mm (0x101203e0) are rejected.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

from mlaser.model import Arc, Contour, Line, Point

TWO_PI = 2 * math.pi   # 0x1013ca70
MIN_RADIUS = 0.02      # 0x101203e0


def _rot(p: Point, c: Point, a: float) -> Point:
    ca, sa = math.cos(a), math.sin(a)
    dx, dy = p[0] - c[0], p[1] - c[1]
    return (c[0] + dx * ca - dy * sa, c[1] + dx * sa + dy * ca)


def closed_poly(pts: Sequence[Point]) -> Contour:
    n = len(pts)
    return Contour([Line(tuple(pts[i]), tuple(pts[(i + 1) % n])) for i in range(n)], True)


def point(p: Point) -> Contour:
    """CCreatePoint: a single point graph (CEditablePoint).  Represented as a zero-length open
    contour flagged ``extra['point']`` (the CAM side pierces/marks it)."""
    p = (float(p[0]), float(p[1]))
    return Contour([Line(p, p)], False, {"point": True})


def line(p0: Point, p1: Point) -> Contour:
    return Contour([Line(tuple(p0), tuple(p1))], False)


def bulge_arc(p0: Point, p1: Point, bulge: float) -> Arc | Line:
    """DXF-style bulge (tan(sweep/4), + = CCW) between two vertices (CEditableLwpoly)."""
    if abs(bulge) < 1e-12 or math.dist(p0, p1) < 1e-12:
        return Line(tuple(p0), tuple(p1))
    sweep = 4 * math.atan(bulge)
    chord = math.dist(p0, p1)
    r = chord / (2 * math.sin(abs(sweep) / 2))
    mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
    ux, uy = (p1[0] - p0[0]) / chord, (p1[1] - p0[1]) / chord
    h = r * math.cos(sweep / 2) * (1 if sweep > 0 else -1)   # signed distance mid -> centre
    # centre lies to the left of the chord for a CCW minor arc
    c = (mx - uy * h, my + ux * h)
    return Arc(tuple(p0), tuple(p1), c, sweep > 0)


def polyline(vertices: Sequence[Point], bulges: Optional[Sequence[float]] = None,
             closed: bool = False) -> Contour:
    """CCreateLwploy result: lines, or bulge arcs where ``bulges[i]`` != 0 (segment i -> i+1)."""
    vs = [tuple(map(float, v)) for v in vertices]
    n = len(vs)
    bulges = list(bulges or []) + [0.0] * n
    segs = []
    last = n if closed else n - 1
    for i in range(last):
        a, b = vs[i], vs[(i + 1) % n]
        if math.dist(a, b) < 1e-12:
            continue
        segs.append(bulge_arc(a, b, bulges[i]))
    return Contour(segs, closed and len(segs) > 1)


def tangent_bulge(prev_dir: Point, p0: Point, p1: Point) -> float:
    """Bulge of the arc leaving ``p0`` tangent to ``prev_dir`` and ending at ``p1``
    (arc mode of the polyline tool, mv6; FUN_10080d30 computes it from the previous element)."""
    L = math.hypot(*prev_dir)
    ch = (p1[0] - p0[0], p1[1] - p0[1])
    C = math.hypot(*ch)
    if L < 1e-12 or C < 1e-12:
        return 0.0
    cross = prev_dir[0] * ch[1] - prev_dir[1] * ch[0]
    dot = prev_dir[0] * ch[0] + prev_dir[1] * ch[1]
    half = math.atan2(cross, dot)            # angle between tangent and chord = sweep/2
    return math.tan(half / 2)


def rect(p0: Point, p1: Point) -> Contour:
    """CCreateRect 0x100810e0: vertices (x0,y0) (x1,y0) (x1,y1) (x0,y1), closed, in click order."""
    (x0, y0), (x1, y1) = p0, p1
    return closed_poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def round_rect_radius(p0: Point, p1: Point, p2: Point) -> float:
    """Third click of CCreateRoundRect (0x10082e90): r = min(|p2 - corner2|, 0.5*min(w, h))."""
    w, h = abs(p1[0] - p0[0]), abs(p1[1] - p0[1])
    return min(0.5 * min(w, h), math.dist(p2, p1))


def round_rect(p0: Point, p1: Point, r: float) -> Contour:
    """CCreateRoundRect: 4 lines + 4 quarter arcs (8 elements, ctor 0x10081960), CCW."""
    x0, x1 = sorted((p0[0], p1[0]))
    y0, y1 = sorted((p0[1], p1[1]))
    r = max(0.0, min(r, 0.5 * min(x1 - x0, y1 - y0)))
    if r < 1e-9:
        return closed_poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    segs = [Line((x0 + r, y0), (x1 - r, y0)),
            Arc((x1 - r, y0), (x1, y0 + r), (x1 - r, y0 + r), True),
            Line((x1, y0 + r), (x1, y1 - r)),
            Arc((x1, y1 - r), (x1 - r, y1), (x1 - r, y1 - r), True),
            Line((x1 - r, y1), (x0 + r, y1)),
            Arc((x0 + r, y1), (x0, y1 - r), (x0 + r, y1 - r), True),
            Line((x0, y1 - r), (x0, y0 + r)),
            Arc((x0, y0 + r), (x0 + r, y0), (x0 + r, y0 + r), True)]
    return Contour([s for s in segs if s.length() > 1e-12], True)


def circle(c: Point, r: float) -> Contour:
    """Full circle as one CCW arc starting at angle 0 (CEditableCircle)."""
    s = (c[0] + r, c[1])
    return Contour([Arc(s, s, tuple(c), True)], True)


def circumcircle(a: Point, b: Point, c: Point) -> Optional[tuple[Point, float]]:
    """Circle through three points (0x1007f610); None if (nearly) collinear."""
    ax, ay = a; bx, by = b; cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return (ux, uy), math.dist((ux, uy), a)


def circle_3pt(a: Point, b: Point, c: Point) -> Optional[Contour]:
    cc = circumcircle(a, b, c)
    return None if cc is None else circle(*cc)


def arc_3pt(a: Point, m: Point, b: Point) -> Optional[Contour]:
    """CCreateArc3Pt: arc from ``a`` through ``m`` to ``b`` (0x100098a0)."""
    cc = circumcircle(a, m, b)
    if cc is None:
        return None
    ccw = ((m[0] - a[0]) * (b[1] - a[1]) - (m[1] - a[1]) * (b[0] - a[0])) > 0
    return Contour([Arc(tuple(a), tuple(b), cc[0], ccw)], False)


def arc_center(c: Point, start: Point, end_dir: Point) -> Optional[Contour]:
    """CCreateArcORA ("scan arc", mf49): centre, start point (radius + start angle), then the
    end angle from the direction of ``end_dir``; swept CCW from start to end (0x1007e910)."""
    r = math.dist(c, start)
    if r < MIN_RADIUS:
        return None
    a1 = math.atan2(end_dir[1] - c[1], end_dir[0] - c[0])
    e = (c[0] + r * math.cos(a1), c[1] + r * math.sin(a1))
    if math.dist(e, start) < 1e-9:
        return circle(c, r)
    return Contour([Arc(tuple(start), e, tuple(c), True)], False)


def ellipse(c: Point, major: Point, ratio: float, tol: float = 0.01) -> Optional[Contour]:
    """CCreateEllipse (0x10080520): centre, major-axis end point, minor/major ratio from the
    third click.  The model has no ellipse primitive, so the CEditableEllipsArc is flattened
    to lines with chord error <= ``tol`` (rebuild choice); parameters kept in ``extra``."""
    a = math.dist(c, major)
    if a < MIN_RADIUS:
        return None
    ratio = max(1e-6, min(abs(ratio), 1e6))
    b = a * ratio
    rot = math.atan2(major[1] - c[1], major[0] - c[0])
    R = max(a, b)
    n = max(16, min(4096, int(math.ceil(TWO_PI / (2 * math.acos(max(-1.0, 1 - tol / R)))))))
    pts = []
    for k in range(n):
        t = TWO_PI * k / n
        x, y = a * math.cos(t), b * math.sin(t)
        pts.append((c[0] + x * math.cos(rot) - y * math.sin(rot), c[1] + x * math.sin(rot) + y * math.cos(rot)))
    out = closed_poly(pts)
    out.extra["ellipse"] = {"center": tuple(c), "major": tuple(major), "ratio": ratio}
    return out


def polygon(c: Point, vertex: Point, n: int = 6) -> Contour:
    """CCreatePolygon (ctor 0x10081b60, 3 <= n <= 360, default 6 from FUN_100ef350): vertex k
    = vertex rotated by k*2pi/n about the centre (0x100811e0)."""
    n = max(3, min(360, int(n)))
    return closed_poly([_rot(vertex, c, TWO_PI * k / n) for k in range(n)])


def star_default_inner(c: Point, vertex: Point, n: int = 6) -> float:
    """Inner radius after the 2nd click (0x10082070): R*sin(pi/2 - 2pi/n) / sin(pi/n + pi/2)."""
    R = math.dist(c, vertex)
    return R * math.sin(math.pi / 2 - TWO_PI / n) / math.sin(math.pi / n + math.pi / 2)


def star(c: Point, vertex: Point, n: int = 6, inner: Optional[float] = None) -> Optional[Contour]:
    """CCreateStar (ctor 0x10081df0, n points, 2n vertices; 0x10081310): outer vertices at
    k*2pi/n, inner vertices at radius ``inner`` half a step later."""
    n = max(3, min(360, int(n)))
    R = math.dist(c, vertex)
    if R < 1e-8:
        return None
    if inner is None:
        inner = star_default_inner(c, vertex, n)
    ux, uy = (vertex[0] - c[0]) / R, (vertex[1] - c[1]) / R
    iv = (c[0] + ux * inner, c[1] + uy * inner)
    step = TWO_PI / n
    pts = []
    for k in range(n):
        pts.append(_rot(vertex, c, k * step))
        pts.append(_rot(iv, c, k * step + step / 2))
    return closed_poly(pts)


def spline(points: Sequence[Point], closed: bool = False, tol: float = 0.02) -> Optional[Contour]:
    """CCreateSpline (0x10083b90): interpolating curve through the clicked fit points.  The
    original keeps a NURBS (splineAnalyer.dll); the model stores lines, so a centripetal
    Catmull-Rom spline is flattened (rebuild choice).  Fit points kept in ``extra``."""
    pts = [tuple(map(float, p)) for p in points]
    dedup = [pts[0]] if pts else []
    for p in pts[1:]:
        if math.dist(p, dedup[-1]) > 1e-9:
            dedup.append(p)
    pts = dedup
    if len(pts) < 2:
        return None
    if len(pts) == 2:
        out = line(pts[0], pts[1])
        out.extra["spline_fit"] = pts
        return out
    ext = ([pts[-1]] + pts + [pts[0], pts[1]]) if closed else \
        ([(2 * pts[0][0] - pts[1][0], 2 * pts[0][1] - pts[1][1])] + pts +
         [(2 * pts[-1][0] - pts[-2][0], 2 * pts[-1][1] - pts[-2][1])])
    out_pts = [pts[0]]
    spans = len(pts) if closed else len(pts) - 1
    for i in range(spans):
        p0, p1, p2, p3 = ext[i], ext[i + 1], ext[i + 2], ext[i + 3]
        L = math.dist(p1, p2)
        n = max(4, min(256, int(math.ceil(L / max(tol * 20, 1e-3)))))
        for k in range(1, n + 1):
            out_pts.append(_catmull(p0, p1, p2, p3, k / n))
    if closed:
        out_pts = out_pts[:-1]
        c = closed_poly(out_pts)
    else:
        c = Contour([Line(out_pts[i], out_pts[i + 1]) for i in range(len(out_pts) - 1)], False)
    c.extra["spline_fit"] = pts
    return c


def _catmull(p0, p1, p2, p3, t, alpha=0.5):
    def tj(ti, a, b):
        return ti + max(math.dist(a, b), 1e-9) ** alpha
    t0 = 0.0
    t1 = tj(t0, p0, p1)
    t2 = tj(t1, p1, p2)
    t3 = tj(t2, p2, p3)
    u = t1 + (t2 - t1) * t

    def lerp(a, b, ta, tb):
        w = (u - ta) / (tb - ta)
        return (a[0] + (b[0] - a[0]) * w, a[1] + (b[1] - a[1]) * w)
    A1, A2, A3 = lerp(p0, p1, t0, t1), lerp(p1, p2, t1, t2), lerp(p2, p3, t2, t3)
    B1, B2 = lerp(A1, A2, t0, t2), lerp(A2, A3, t1, t3)
    return lerp(B1, B2, t1, t2)
