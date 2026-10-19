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
back to a polygon offset of the flattened contour.  Two fallbacks exist:

``"shapely"`` (default)
    shapely ``buffer`` with round joins; the result is a polyline (arcs lost) that starts
    wherever GEOS starts the ring.  The goldens (``tools/make_goldens.py``, group
    ``jobs_random``) reach this fallback 87 times (76 self-intersecting analytic results,
    11 vanished pieces), so it stays the default to keep them bit-identical.  The Wine parity
    cases (JC-03/04/13) never reach it.
``"clipper"`` (opt-in: ``CamParams.kerf_fallback = "clipper"`` for one job,
    ``compensate(..., fallback="clipper")`` for one call, or ``kerf.DEFAULT_FALLBACK =
    "clipper"`` process-wide)
    pyclipper ``ClipperOffset`` (``JT_ROUND`` / ``ET_CLOSEDPOLYGON``, integer coordinates at
    :data:`CLIPPER_SCALE` per mm, arc tolerance = the flattening chord error
    :data:`CLIPPER_TOL`) on the chord-error-bounded polyline, then the arcs are re-fitted with
    ``cam.cleanup.fit_arcs`` (CircleFitDLL port, err :data:`CLIPPER_FIT_ERR`), so the child has
    Line/Arc segments again.  The child keeps the parent's direction (CCW stays CCW) and starts
    at the point of the offset nearest to where the analytic offset would start (the parent's
    start moved by ``d`` along the right normal), so ``prepare_contour``'s start fraction
    (parity JC-03) lands where it would on an analytic child.
"""
from __future__ import annotations

import math
from typing import Optional

from ..model import Arc, Contour, Line, Point, Segment
from ..geom.contour import contour_bbox, is_ccw, seg_tangent, turn


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


#: fallback used when ``compensate`` gets ``fallback=None`` ("shapely": goldens-pinned default)
DEFAULT_FALLBACK = "shapely"
FALLBACKS = ("shapely", "clipper")
CLIPPER_TOL = 0.002       # mm: chord error of the flattening and Clipper's ArcTolerance
CLIPPER_FIT_ERR = 0.005   # mm: CircleFit Err for the arc re-fit
CLIPPER_SCALE = 1e6       # integer units per mm (|x| < 9e12 mm before int64 overflow)


def _flatten(c: Contour, tol: float) -> list[Point]:
    """Closed polyline of ``c`` (last point != first) with chord error <= tol on arcs."""
    pts: list[Point] = [c.segments[0].start]
    for s in c.segments:
        if isinstance(s, Arc):
            r = s.radius
            sw = abs(s.sweep()) if math.dist(s.start, s.end) > 1e-12 else 2 * math.pi
            if r > tol:
                step = 2 * math.acos(1 - tol / r)
                n = max(1, int(math.ceil(sw / step)))
            else:
                n = 4
            n = min(n, 20000)
            for i in range(1, n + 1):
                pts.append(s.point_at(i / n))
        else:
            pts.append(s.end)
        pts[-1] = s.end
    out: list[Point] = []
    for p in pts:
        if not out or math.dist(p, out[-1]) > 1e-9:
            out.append(p)
    if len(out) > 1 and math.dist(out[0], out[-1]) <= 1e-9:
        out.pop()
    return out


def _ring_area(pts: list[Point]) -> float:
    return 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))


def _start_on_ring(ring: list[Point], target: Point) -> list[Point]:
    """``ring`` rotated (and split) so that it starts at its point nearest ``target``."""
    best = (math.inf, 0, 0.0)
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        vx, vy = b[0] - a[0], b[1] - a[1]
        L2 = vx * vx + vy * vy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((target[0] - a[0]) * vx + (target[1] - a[1]) * vy) / L2))
        q = (a[0] + vx * t, a[1] + vy * t)
        dd = math.dist(q, target)
        if dd < best[0]:
            best = (dd, i, t)
    _, i, t = best
    a, b = ring[i], ring[(i + 1) % n]
    q = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    rest = ring[i + 1:] + ring[:i + 1]          # starts at b, ends at a
    out = [q] + rest
    if math.dist(q, rest[0]) < 1e-9:
        out = rest
    elif math.dist(q, rest[-1]) < 1e-9:
        out = [rest[-1]] + rest[:-1]
    return out


def _seg_pts(segs: list[Segment], tol: float) -> list[Point]:
    pts: list[Point] = [segs[0].start]
    for s in segs:
        if isinstance(s, Arc) and s.radius > tol:
            sw = abs(s.sweep()) if math.dist(s.start, s.end) > 1e-12 else 2 * math.pi
            n = max(1, min(20000, int(math.ceil(sw / (2 * math.acos(1 - tol / s.radius))))))
            pts.extend(s.point_at(i / n) for i in range(1, n + 1))
        else:
            pts.append(s.end)
    return pts


def _fits(run: list[Point], segs: list[Segment], err: float) -> bool:
    """Both ways within ``err``: every run vertex near the fit and every fit sample near the run."""
    from shapely.geometry import LineString
    if not segs:
        return False
    fitted = _seg_pts(segs, err / 4)
    if len(fitted) < 2 or len(run) < 2:
        return False
    if math.dist(fitted[0], run[0]) > err or math.dist(fitted[-1], run[-1]) > err:
        return False
    return LineString(run).hausdorff_distance(LineString(fitted)) <= err


def _refit_run(run: list[Point], fp, depth: int = 0) -> list[Segment]:
    """CircleFit an open run; verified, else bisected, else kept as lines."""
    from .cleanup import fit_arcs
    if len(run) > 3:
        segs = [s for s in fit_arcs(run, fp) if s.length() > 1e-9]
        if _fits(run, segs, 2 * fp.err):
            segs[0] = _with_start(segs[0], run[0])
            segs[-1] = _with_end(segs[-1], run[-1])
            return segs
        if depth < 12:
            m = len(run) // 2
            return _refit_run(run[:m + 1], fp, depth + 1) + _refit_run(run[m:], fp, depth + 1)
    return [Line(a, b) for a, b in zip(run, run[1:]) if math.dist(a, b) > 1e-9]


def _refit(ring: list[Point]) -> list[Segment]:
    """Lines/arcs for a closed ring (start = ring[0]): CircleFit on the whole ring first (a
    circle comes back as one full Arc), else per run between sharp corners, each run checked
    against the polyline (the port's biarcs can leave it, e.g. on a tight round join)."""
    from .cleanup import FitParams, fit_arcs, ANG_CIRCLEFIT
    fp = FitParams(err=CLIPPER_FIT_ERR)
    closed = ring + [ring[0]]
    whole = [s for s in fit_arcs(ring, fp, closed=True) if s.length() > 1e-9]
    if whole and _fits(closed, whole, 2 * fp.err):
        return whole
    n = len(ring)
    corners = [0]
    for i in range(1, n):
        a, b, c = ring[i - 1], ring[i], ring[(i + 1) % n]
        a1 = math.atan2(b[1] - a[1], b[0] - a[0])
        a2 = math.atan2(c[1] - b[1], c[0] - b[0])
        t = abs((a2 - a1 + math.pi) % (2 * math.pi) - math.pi)
        if t > ANG_CIRCLEFIT:
            corners.append(i)
    corners.append(n)
    out: list[Segment] = []
    for i0, i1 in zip(corners, corners[1:]):
        out.extend(_refit_run(closed[i0:i1 + 1], fp))
    for k in range(1, len(out)):
        out[k] = _with_start(out[k], out[k - 1].end)
    if out:
        out[-1] = _with_end(out[-1], out[0].start)
    return [s for s in out if s.length() > 1e-9]


def _clipper_offset(c: Contour, d: float) -> Optional[Contour]:
    """pyclipper offset of the flattened contour + CircleFit arc re-fit (see the module doc)."""
    import pyclipper

    try:
        pts = _flatten(c, CLIPPER_TOL)
        if len(pts) < 3 or abs(_ring_area(pts)) < 1e-12:
            return None
        ccw = _ring_area(pts) > 0
        S = CLIPPER_SCALE
        ipts = [(int(round(x * S)), int(round(y * S))) for x, y in pts]
        # self-intersecting input: resolve it to simple polygons first (non-zero fill)
        polys = pyclipper.SimplifyPolygon(ipts, pyclipper.PFT_NONZERO)
        polys = [p for p in polys if len(p) >= 3]
        if not polys:
            return None
        grow = (d > 0) == ccw                 # + d is right of travel: outward on a CCW loop
        co = pyclipper.PyclipperOffset(2.0, CLIPPER_TOL * S)
        co.AddPaths(polys, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
        sol = co.Execute(abs(d) * S if grow else -abs(d) * S)
        outers = [p for p in sol if len(p) >= 3 and pyclipper.Orientation(p)]
        if not outers:
            return None
        best = max(outers, key=lambda p: abs(pyclipper.Area(p)))
        ring: list[Point] = [(x / S, y / S) for x, y in best]
        if (_ring_area(ring) > 0) != ccw:
            ring.reverse()
        s0 = c.segments[0]
        t0 = seg_tangent(s0, 0.0)
        target = (s0.start[0] + d * t0[1], s0.start[1] - d * t0[0])
        ring = _start_on_ring(ring, target)
        segs = _refit(ring)
        if not segs:
            return None
        out = Contour(segs, True)
        if is_ccw(out) != ccw:
            out = Contour([s.reversed() for s in reversed(segs)], True)
        return out
    except (ValueError, ZeroDivisionError, OverflowError, pyclipper.ClipperException):
        return None


def _self_intersects(c: Contour) -> bool:
    from shapely.geometry import LinearRing
    pts = list(c.points(0.01))
    if len(pts) < 4:
        return False
    try:
        return not LinearRing(pts).is_simple
    except Exception:
        return True


def compensate(c: Contour, mode: int, width: float, depth: int = 1,
               fallback: Optional[str] = None) -> Optional[Contour]:
    """Kerf-compensated copy of ``c`` (None if not applicable).

    ``fallback`` picks the polygon offset used when the analytic offset fails: ``"shapely"``
    or ``"clipper"`` (module doc); None means :data:`DEFAULT_FALLBACK`."""
    fb = fallback or DEFAULT_FALLBACK
    if fb not in FALLBACKS:
        raise ValueError(f"unknown kerf fallback {fb!r} (one of {FALLBACKS})")
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
        res = _clipper_offset(c, d) if fb == "clipper" else _shapely_offset(c, d)
    return res
