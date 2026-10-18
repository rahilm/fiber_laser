"""Segments, glyphs and splines: the flattening, spline and bounding-box helpers shared by the
importers, the .chf codec and the CAM code (formerly ``mlaser.io.geom``).

Bulge arcs, contour area / reversal, segment chaining (``join_segments``), B-spline evaluation
and flattening, glyph expansion (``glyph_segments``) and bounding boxes.

``flatten_bspline_params`` (with ``_span_params`` / ``_bs_eval`` / ``_bs_tangent``) is the
splineAnalyer sample table of the original (FUN_100953d0), bit for bit: the .chf codec and the
spline pieces of the cutting pipeline depend on it, so it changes only with a parity re-run.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional

from ..model import Arc, Contour, Line, Point, Segment

__port__ = {
    "source": "splineAnalyerVc100",
    "functions": {"FUN_100953d0": "flatten_bspline_params"},
    "note": "foundation primitive used by the parity ports; lives in geom because the .chf "
            "codec and the importers flatten splines with it",
}


def bulge_to_arc(p0: Point, p1: Point, bulge: float) -> Segment:
    """DXF polyline bulge (tan(sweep/4), + = CCW) -> Arc (or Line for bulge 0)."""
    if abs(bulge) < 1e-12 or math.dist(p0, p1) < 1e-12:
        return Line(p0, p1)
    chord = math.dist(p0, p1)
    mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
    nx, ny = -(p1[1] - p0[1]) / chord, (p1[0] - p0[0]) / chord   # left normal
    d = (chord / 2) * (1 - bulge * bulge) / (2 * bulge)          # signed offset of centre
    c = (mx + nx * d, my + ny * d)
    return Arc(p0, p1, c, ccw=bulge > 0)


def seg_reversed(s: Segment) -> Segment:
    return s.reversed()


def signed_area(c: Contour, tol: float = 0.01) -> float:
    """Shoelace area of the flattened contour (+ = CCW)."""
    pts = list(c.points(tol))
    a = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        a += x0 * y1 - x1 * y0
    return a / 2.0


def reverse_contour(c: Contour) -> Contour:
    return Contour([s.reversed() for s in reversed(c.segments)], c.closed)


def join_segments(segs: Iterable[Segment], tol: float = 0.05) -> list[Contour]:
    """Chain loose segments into contours; a chain whose ends meet within tol is closed.

    Greedy end-point matching with reversal (the original's DXF path relies on
    Dxf2Grp/CADModule chaining, tolerance UNKNOWN; default 0.05 mm here).
    End points are snapped so consecutive segments share exact coordinates.
    """
    pool = [s for s in segs if s.length() > 1e-9 or (isinstance(s, Arc))]
    contours: list[Contour] = []
    # grid hash of endpoints
    cell = max(tol, 1e-6) * 2

    def key(p):
        return (int(math.floor(p[0] / cell)), int(math.floor(p[1] / cell)))

    index: dict = {}
    for i, s in enumerate(pool):
        for end in (0, 1):
            p = s.start if end == 0 else s.end
            index.setdefault(key(p), []).append((i, end))
    used = [False] * len(pool)

    def find(p, exclude):
        kx, ky = key(p)
        best, bd = None, tol
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i, end in index.get((kx + dx, ky + dy), ()):
                    if used[i] or i == exclude:
                        continue
                    q = pool[i].start if end == 0 else pool[i].end
                    d = math.dist(p, q)
                    if d <= bd:
                        best, bd = (i, end), d
        return best

    for i, s in enumerate(pool):
        if used[i]:
            continue
        used[i] = True
        chain = [s]
        # closed single entity (circle / closed polyline already a loop)
        if isinstance(s, Arc) and math.dist(s.start, s.end) < 1e-9:
            contours.append(Contour(chain, True))
            continue
        # extend forward
        while True:
            nx = find(chain[-1].end, -1)
            if nx is None:
                break
            j, end = nx
            used[j] = True
            t = pool[j] if end == 0 else pool[j].reversed()
            chain.append(_snap_start(t, chain[-1].end))
        # extend backward
        while True:
            nx = find(chain[0].start, -1)
            if nx is None:
                break
            j, end = nx
            used[j] = True
            t = pool[j] if end == 1 else pool[j].reversed()
            chain.insert(0, _snap_end(t, chain[0].start))
        closed = math.dist(chain[0].start, chain[-1].end) <= tol and (len(chain) > 1 or isinstance(chain[0], Arc))
        if closed:
            chain[-1] = _snap_end(chain[-1], chain[0].start)
        contours.append(Contour(chain, closed))
    return contours


def _snap_start(s: Segment, p: Point) -> Segment:
    if isinstance(s, Line):
        return Line(p, s.end)
    return Arc(p, s.end, s.center, s.ccw)


def _snap_end(s: Segment, p: Point) -> Segment:
    if isinstance(s, Line):
        return Line(s.start, p)
    return Arc(s.start, p, s.center, s.ccw)


def polyline_segments(pts: list[Point], closed: bool) -> list[Segment]:
    segs: list[Segment] = []
    for a, b in zip(pts, pts[1:]):
        if math.dist(a, b) > 1e-12:
            segs.append(Line(a, b))
    if closed and len(pts) > 2 and math.dist(pts[-1], pts[0]) > 1e-12:
        segs.append(Line(pts[-1], pts[0]))
    return segs


# --------------------------------------------------------------------------- B-splines
# Flattening of the original (CEditableSpline, CADModule FUN_100953d0 -> splineAnalyer
# vtable+0x2c = FUN_100bf350 / FUN_100befa0, ported in :func:`flatten_bspline`): with the glyph
# tolerance e = 0.1 (+0x10, ctor FUN_10095050 DAT_1011f0b0) and reference radius R = 2.0 (+0x180,
# DAT_1011d6c0) the analyser gets the distance sqrt(2*R*e) = 0.632 mm and the angle
# acos(1 - e/R) = 18.19 deg.
SPLINE_MAX_CHORD = math.sqrt(2 * 2.0 * 0.1)
SPLINE_MAX_ANGLE = math.acos(1 - 0.1 / 2.0)
SPLINE_SPLIT_ANGLE = math.radians(15.0)     # DAT_100f0470 * DAT_100e1040
SPLINE_TOL = 0.01          # kept for callers; the spline flattening follows the original exactly


def bspline_point(ctrl, knots, u: float) -> Point:
    """De Boor evaluation of a non-rational B-spline of order len(knots) - len(ctrl)."""
    n = len(ctrl)
    p = len(knots) - n - 1
    if p < 1:
        return tuple(ctrl[0])
    s = p
    while s < n - 1 and u >= knots[s + 1]:
        s += 1
    d = [list(ctrl[j + s - p]) for j in range(p + 1)]
    for r in range(1, p + 1):
        for j in range(p, r - 1, -1):
            den = knots[j + 1 + s - r] - knots[j + s - p]
            a = 0.0 if den == 0 else (u - knots[j + s - p]) / den
            d[j] = [(1 - a) * d[j - 1][0] + a * d[j][0], (1 - a) * d[j - 1][1] + a * d[j][1]]
    return (d[p][0], d[p][1])


def _spans(ctrl, knots):
    n = len(ctrl)
    p = len(knots) - n - 1
    if n == 0 or p < 1:
        return []
    return [(knots[i], knots[i + 1]) for i in range(p, n) if knots[i + 1] - knots[i] > 1e-12]


def _bs_eval(ctrl, knots, u: float) -> Point:
    if u >= knots[-1] - 1e-15:
        return _end(ctrl, knots, knots[-1])
    return bspline_point(ctrl, knots, u)


def _bs_tangent(ctrl, knots, u: float) -> float:
    lo, hi = knots[len(knots) - len(ctrl) - 1], knots[len(ctrl)]
    h = 1e-6 * max(1.0, abs(hi - lo))
    a, b = max(lo, u - h), min(hi, u + h)
    p, q = _bs_eval(ctrl, knots, a), _bs_eval(ctrl, knots, b)
    return math.atan2(q[1] - p[1], q[0] - p[0])


def _ang(a: float, b: float) -> float:
    d = abs(a - b) % (2 * math.pi)
    return min(d, 2 * math.pi - d)


def _span_params(ctrl, knots, u0: float, u1: float, out: list) -> None:
    """FUN_100befa0: parameters inside [u0, u1].  With A = turning start->mid->end and D = the
    Manhattan length start->mid->end: if A < 15 deg or D < 0.632 the interval is cut into an
    even number n = max(round(A / 18.19 deg), round(D / 0.632), 1) of equal parameter steps,
    otherwise into even(trunc(1.5 A / 15 deg) + 2) sub-intervals that are handled again."""
    L = u1 - u0
    if L < 1e-4:
        return
    um = 0.5 * (u0 + u1)
    t0, tm, t1 = _bs_tangent(ctrl, knots, u0), _bs_tangent(ctrl, knots, um), _bs_tangent(ctrl, knots, u1)
    p0, pm, p1 = _bs_eval(ctrl, knots, u0), _bs_eval(ctrl, knots, um), _bs_eval(ctrl, knots, u1)
    A = _ang(t0, tm) + _ang(tm, t1)
    D = abs(pm[1] - p0[1]) + abs(pm[0] - p0[0]) + abs(pm[0] - p1[0]) + abs(pm[1] - p1[1])
    if A < SPLINE_SPLIT_ANGLE or D < SPLINE_MAX_CHORD:
        n = max(int(A / SPLINE_MAX_ANGLE + 0.5), int(D / SPLINE_MAX_CHORD + 0.5), 1)
        n = ((n + 1) // 2) * 2
        out.extend(u0 + i * L / n for i in range(1, n))
    else:
        m = ((2 - int(-1.5 * A / SPLINE_SPLIT_ANGLE)) // 2) * 2
        for i in range(1, m + 1):
            _span_params(ctrl, knots, u0 + (i - 1) * L / m, u0 + i * L / m, out)


def flatten_bspline_params(ctrl, knots) -> list[tuple[float, Point]]:
    """:func:`flatten_bspline` with the curve parameter of every kept point: the (u, x, y) part
    of the splineAnalyer sample records CEditableSpline keeps at +0x124 (32 bytes: u, x, y,
    cumulative length; FUN_100953d0).  The end point carries the last knot of the last span."""
    ctrl = [tuple(map(float, c[:2])) for c in ctrl]
    knots = [float(k) for k in knots]
    spans = _spans(ctrl, knots)
    if not spans:
        if not ctrl:
            return []
        return [(knots[0] if knots else 0.0, ctrl[0]), (knots[-1] if knots else 1.0, ctrl[-1])]
    params: list = []
    for j, (a, b) in enumerate(spans):
        _span_params(ctrl, knots, a, b, params)
        if j < len(spans) - 1:
            params.append(b)
    out = [(spans[0][0], _bs_eval(ctrl, knots, spans[0][0]))]
    for u in params:
        p = _bs_eval(ctrl, knots, u)
        q = out[-1][1]
        if abs(p[0] - q[0]) > 0.01 or abs(p[1] - q[1]) > 0.01:
            out.append((u, p))
    e = _end(ctrl, knots, spans[-1][1])
    q = out[-1][1]
    if abs(q[0] - e[0]) > 1e-6 or abs(q[1] - e[1]) > 1e-6:
        out.append((spans[-1][1], e))
    return out


def bspline_derivative(ctrl, knots, u: float, order: int = 1) -> Point:
    """d^k/du^k of the B-spline at u (k = ``order`` <= degree), by differentiating the control
    polygon: Q_i = p (P_{i+1} - P_i) / (t_{i+p+1} - t_{i+1}) on the knot vector without its end
    knots."""
    ctrl = [tuple(map(float, c[:2])) for c in ctrl]
    knots = [float(k) for k in knots]
    for _ in range(order):
        n = len(ctrl)
        p = len(knots) - n - 1
        if p < 1:
            return (0.0, 0.0)
        q = []
        for i in range(n - 1):
            den = knots[i + p + 1] - knots[i + 1]
            f = p / den if den else 0.0
            q.append(((ctrl[i + 1][0] - ctrl[i][0]) * f, (ctrl[i + 1][1] - ctrl[i][1]) * f))
        ctrl, knots = q, knots[1:-1]
    if len(knots) - len(ctrl) - 1 < 1:   # degree 0: piecewise constant
        k = 0
        while k < len(ctrl) - 1 and u >= knots[k + 1]:
            k += 1
        return ctrl[k]
    lo, hi = knots[len(knots) - len(ctrl) - 1], knots[len(ctrl)]
    uu = min(max(u, lo), hi - 1e-12 * max(1.0, abs(hi)))
    return bspline_point(ctrl, knots, uu)


def flatten_bspline(ctrl, knots, tol: Optional[float] = None) -> list[Point]:
    """Polyline through a cubic B-spline exactly as the original computes it (D-15).

    splineAnalyer FUN_100bf350 subdivides every knot span with FUN_100befa0
    (:func:`_span_params`), evaluates the curve there and drops a point that is within 0.01 mm
    (DAT_100e0fa0, per axis) of the previous one; the end point is appended.  On the samples
    the polyline length equals the length the original stores in the .chf to 1e-4 mm.
    ``tol`` is accepted for compatibility and ignored."""
    ctrl = [tuple(map(float, c[:2])) for c in ctrl]
    if not _spans(ctrl, [float(k) for k in knots]):
        return list(ctrl[:1]) + list(ctrl[-1:]) if ctrl else []
    return [p for _u, p in flatten_bspline_params(ctrl, knots)]


def _end(ctrl, knots, u):
    # u == last knot: de Boor with the half-open span rule would step past the end
    n = len(ctrl)
    p = len(knots) - n - 1
    if all(abs(k - knots[-1]) < 1e-12 for k in knots[n:]):
        return tuple(ctrl[-1])
    return bspline_point(ctrl, knots, u - 1e-12 * max(1.0, abs(u)))


_GL = [(-0.9602898564975363, 0.1012285362903763), (-0.7966664774136267, 0.2223810344533745),
       (-0.5255324099163290, 0.3137066458778873), (-0.1834346424956498, 0.3626837833783620),
       (0.1834346424956498, 0.3626837833783620), (0.5255324099163290, 0.3137066458778873),
       (0.7966664774136267, 0.2223810344533745), (0.9602898564975363, 0.1012285362903763)]


def bspline_length(ctrl, knots, sub: int = 8) -> float:
    """Arc length (8-point Gauss-Legendre on ``sub`` pieces of every knot span)."""
    ctrl = [tuple(map(float, c[:2])) for c in ctrl]
    knots = [float(k) for k in knots]
    total = 0.0
    for a, b in _spans(ctrl, knots):
        h = (b - a) / sub
        for i in range(sub):
            u0, u1 = a + i * h, a + (i + 1) * h
            hh = 1e-7 * max(1.0, abs(u1 - u0))
            for x, w in _GL:
                u = 0.5 * (u0 + u1) + 0.5 * (u1 - u0) * x
                p, q = bspline_point(ctrl, knots, u - hh), bspline_point(ctrl, knots, u + hh)
                total += w * 0.5 * (u1 - u0) * math.dist(p, q) / (2 * hh)
    return total


# --------------------------------------------------------------------------- glyphs
def _polar(c: Point, r: float, a: float) -> Point:
    return (c[0] + r * math.cos(a), c[1] + r * math.sin(a))


def lwpoly_segments(verts, closed: bool) -> list[Segment]:
    n = len(verts)
    segs: list[Segment] = []
    for i in range(n if closed else n - 1):
        (p, b), (q, _) = verts[i], verts[(i + 1) % n]
        if math.dist(p, q) < 1e-12:
            continue
        segs.append(bulge_to_arc(tuple(p), tuple(q), float(b)))
    return segs


def glyph_segments(g, tol: float = SPLINE_TOL) -> list[Segment]:
    """Model segments of one glyph (model.Glyph or anything with kind/data/dir) in traversal order."""
    from ..model import (GLYPH_ARC, GLYPH_CIRCLE, GLYPH_ELLIPSE, GLYPH_LINE, GLYPH_LWPOLY,
                         GLYPH_POINT, GLYPH_SPLINE)
    d, k = g.data, g.kind
    if k == GLYPH_POINT:
        segs: list[Segment] = [Line(tuple(d["p"]), tuple(d["p"]))]
    elif k == GLYPH_LINE:
        segs = [Line(tuple(d["start"]), tuple(d["end"]))]
    elif k == GLYPH_ARC:
        c, r, a0, a1 = tuple(d["center"]), d["radius"], d["a0"], d["a1"]
        segs = [Arc(_polar(c, r, a0), _polar(c, r, a1), c, ccw=a1 >= a0)]
    elif k == GLYPH_CIRCLE:
        c, r = tuple(d["center"]), d["radius"]
        s = (c[0] + r, c[1])
        segs = [Arc(s, s, c, True)]
    elif k == GLYPH_ELLIPSE:
        segs = polyline_segments(ellipse_points(d, tol), False)
    elif k == GLYPH_LWPOLY:
        segs = lwpoly_segments(d["verts"], bool(d.get("flag")))
    elif k == GLYPH_SPLINE:
        segs = polyline_segments(flatten_bspline(d["ctrl"], d["knots"], tol), False)
    else:
        segs = []
    if g.dir == -1:
        segs = [s.reversed() for s in reversed(segs)]
    return segs


def ellipse_points(d: dict, tol: float = SPLINE_TOL) -> list[Point]:
    """Points of an ellipse-arc glyph (DXF convention: major-axis vector, minor/major ratio,
    parameters t0..t1), at most SPLINE_MAX_CHORD apart and pi/32 in parameter."""
    c, ax, ratio, t0, t1 = d["center"], d["axis"], d["ratio"], d["t0"], d["t1"]
    a = math.hypot(*ax)
    n = max(8, int(math.ceil(abs(t1 - t0) * a / SPLINE_MAX_CHORD)), int(math.ceil(abs(t1 - t0) / (math.pi / 32))))
    bx, by = -ax[1] * ratio, ax[0] * ratio
    return [(c[0] + ax[0] * math.cos(t) + bx * math.sin(t), c[1] + ax[1] * math.cos(t) + by * math.sin(t))
            for t in (t0 + (t1 - t0) * i / n for i in range(n + 1))]


def glyph_length(g) -> float:
    from ..model import GLYPH_SPLINE
    if g.kind == GLYPH_SPLINE:     # the original stores the length of its flattened polyline
        pts = flatten_bspline(g.data["ctrl"], g.data["knots"])
        return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
    return sum(s.length() for s in glyph_segments(g, tol=1e-4))


def glyph_endpoints(g) -> tuple[Point, Point]:
    """(start, end) of the glyph in its own direction (ignoring ``dir``)."""
    from ..model import GLYPH_ARC, GLYPH_CIRCLE, GLYPH_LINE, GLYPH_LWPOLY, GLYPH_POINT, GLYPH_SPLINE
    d, k = g.data, g.kind
    if k == GLYPH_POINT:
        return tuple(d["p"]), tuple(d["p"])
    if k == GLYPH_LINE:
        return tuple(d["start"]), tuple(d["end"])
    if k == GLYPH_ARC:
        return _polar(d["center"], d["radius"], d["a0"]), _polar(d["center"], d["radius"], d["a1"])
    if k == GLYPH_CIRCLE:
        s = (d["center"][0] + d["radius"], d["center"][1])
        return s, s
    if k == GLYPH_LWPOLY:
        vs = d["verts"]
        return tuple(vs[0][0]), tuple(vs[0][0] if d.get("flag") else vs[-1][0])
    if k == GLYPH_SPLINE:
        return tuple(d["ctrl"][0]), _end(d["ctrl"], d["knots"], d["knots"][-1])
    segs = glyph_segments(type(g)(g.kind, g.data, 1))
    return segs[0].start, segs[-1].end


def translate_glyph(g, dx: float, dy: float):
    """A copy of glyph ``g`` moved by (dx, dy)."""
    import copy
    t = lambda p: (p[0] + dx, p[1] + dy)   # noqa: E731
    d = copy.deepcopy(g.data)
    for key in ("p", "start", "end", "center"):
        if key in d:
            d[key] = t(d[key])
    if "verts" in d:
        d["verts"] = [(t(p), b) for p, b in d["verts"]]
    if "ctrl" in d:
        d["ctrl"] = [t(p) for p in d["ctrl"]]
    return type(g)(g.kind, d, g.dir)


def segments_bbox(segs: list[Segment]):
    """Exact bbox of Line/Arc segments ((xmin, ymin), (xmax, ymax))."""
    xs, ys = [], []
    for s in segs:
        xs += [s.start[0], s.end[0]]; ys += [s.start[1], s.end[1]]
        if isinstance(s, Arc):
            c, r = s.center, s.radius
            a0 = math.atan2(s.start[1] - c[1], s.start[0] - c[0])
            sw = s.sweep()
            lo, hi = (a0, a0 + sw) if sw >= 0 else (a0 + sw, a0)
            k = math.ceil(lo / (math.pi / 2) - 1e-12)
            while k * math.pi / 2 <= hi + 1e-12:
                x, y = _polar(c, r, k * math.pi / 2)
                xs.append(x); ys.append(y)
                k += 1
    if not xs:
        return (0.0, 0.0), (0.0, 0.0)
    return (min(xs), min(ys)), (max(xs), max(ys))
