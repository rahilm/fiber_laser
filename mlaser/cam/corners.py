"""Corner treatments: fillet (CArcRound), relief loop (CUnloadAngle), alpha loop (CAlphaRound),
automatic cooling points (CCoolPoint) and the kerf cubic-Bezier corner join.

Evidence (CADModule.dll, dump ``decomp/CADModule.c``; see docs/cam.md section 13):

* **Fillet** ("倒圆角", pd600 radius, pd601 min angle, pd602 max angle; mf400/mf401/mf484).
  Manual, one corner: 0x10075e00 -> 0x10059f70.  Auto, every corner: 0x10075e00 -> 0x1005a650.
  Line/line geometry 0x100579d0 (arc/arc 0x100595d0, line/arc 0x10059b10).
  For the vertex between glyph i and i+1 with t_in = tangent at the end of i and t_out =
  tangent at the start of i+1, ``c = -(t_in . t_out)`` is the cosine of the interior angle
  (180 deg = straight).  Auto filter: -0.999 <= c <= 0.999 (constants 0x1011f968/0x1011f3a8)
  and cos(max) - 0.01 <= c <= cos(min); both glyphs >= 0.05 mm (0x1011d6d0); an arc glyph
  needs radius >= 2r.  The manual variant requires each glyph to be >= r + 0.05.
  Both glyphs are trimmed at the tangent points (split tolerance 0.1, 0x1011f0b0) and the
  arc is inserted between them.
* **Relief / unload loop** ("释放角", pd603 radius, mf404), 0x10076040 -> 0x10058370, arc
  builder 0x10057800.  Line/line corners only, both lines >= 2r, |c| <= 0.97
  (0x1011f950/0x1011f958).  S = P - r*t_in, E = P + r*t_out, an arc of radius r centred on the
  vertex P from S to E, CCW when cross(t_in, t_out) >= 0, else CW (0x10057800 adds/subtracts
  2*pi so the arc goes the long way round, outside the corner wedge).  The two lines are
  trimmed to S / E.
* **Alpha loop** ("倒α角", pd607 max angle, pd608 min edge length, pd609 outside lead length,
  pd614 type 0 auto / 1 outside / 2 inner / 3 both; mf402/mf403/mf485).  0x100761d0 ->
  0x100587a0.  Corner accepted when both glyphs >= min edge and cos(max) <= c <= 0.9998
  (0x1011f960).  Side filter: s = +1 for CCW contours, -1 for CW; mode 0 negates s for even
  depth (holes); mode 2 negates s; mode 3 has no filter; the corner is skipped when
  cross(t_in, t_out)*s < 0.  Corners closer than min(0.35*lead, 0.5) to the start point are
  skipped.  A cubic B-spline (CEditableSpline, knots [0,0,0,0,2,4,4,4,4]) with control points
  P, P + k*t_in, P + k*(t_in - t_out), P - k*t_out, P is inserted at the vertex (the edges are
  not trimmed), k = 0.5*lead/sqrt(X).  X is computed on the x87 stack and was not recovered;
  we use X = |t_in - t_out|^2 (INFERRED).  The model has no spline type, so the loop is
  flattened to lines.
* **Auto cooling points** ("冷却点", pd604 lead point, pd605 sharp corner, pd606 max angle),
  0x1006a220 -> 0x10057c10.  A vertex is a cooling point when c >= cos(max angle); its
  position is the arc length at the start of the outgoing glyph (stored as a fraction at
  contour+0x15c).  With "cool lead position" the start parameter (+0x180) is added unless a
  point already lies within 0.01 of it.
* **Kerf Bezier join** in the offset builder 0x10038130 (called from 0x1003ad00).  At a gap
  (convex) vertex between offset pieces A (end of piece i, tangent ta) and B (start of piece
  i+1, tangent tb): if the two perpendicular distances |ta x AB| and |tb x AB| differ by
  >= 0.01 (0x1011d800) a cubic Bezier (knots [0,0,0,0,1,1,1,1]) with control points
  A, A + |AB|*ta, B - |AB|*tb, B is used; otherwise a tangent arc A->B (0x100313c0).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ..model import Arc, Contour, Line, Point, Segment
from ..geom.contour import seg_tangent

__all__ = ["Corner", "corners", "fillet", "fillet_corner", "unload_loop", "alpha_corners",
           "auto_cool_points", "cubic_bezier_points", "bezier_corner_join", "kerf_corner_join",
           "bspline_points"]

COS_LIMIT = 0.999          # 0x1011f3a8 / 0x1011f968
ANG_EPS = 0.01             # 0x1011d800 subtracted from cos(max)
MIN_GLYPH = 0.05           # 0x1011d6d0
UNLOAD_COS = 0.97          # 0x1011f950 / 0x1011f958
ALPHA_COS = 0.9998         # 0x1011f960
ALPHA_START_K = 0.35       # 0x1011f598
BEZIER_ASYM = 0.01         # 0x1011d800


def _cross(a, b) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _add(p, v, k=1.0) -> Point:
    return (p[0] + v[0] * k, p[1] + v[1] * k)


@dataclass
class Corner:
    """Vertex between segment ``i`` (incoming) and segment ``j`` (outgoing)."""
    i: int
    j: int
    point: Point
    t_in: Point
    t_out: Point
    dist: float          # arc length of the vertex from the contour start

    @property
    def cos_interior(self) -> float:
        """cos of the interior angle, ``-(t_in . t_out)`` (0x10059f70)."""
        return -_dot(self.t_in, self.t_out)

    @property
    def angle_deg(self) -> float:
        """Interior angle in degrees (180 = straight, 90 = square, ~0 = spike)."""
        return math.degrees(math.acos(max(-1.0, min(1.0, self.cos_interior))))

    @property
    def turn(self) -> float:
        """cross(t_in, t_out): > 0 left turn (convex on a CCW contour)."""
        return _cross(self.t_in, self.t_out)


def corners(c: Contour, close_tol: float = 0.1) -> list[Corner]:
    """All vertices of a contour.  A closed contour includes the wrap vertex when its start
    and end meet within ``close_tol`` (0x1005a650 uses 0.1, 0x1011f0b0)."""
    segs = c.segments
    n = len(segs)
    out: list[Corner] = []
    acc = 0.0
    for k in range(n):
        acc += segs[k].length()
        if k + 1 < n:
            j = k + 1
        elif c.closed and n > 1 and math.dist(segs[-1].end, segs[0].start) <= close_tol:
            j = 0
        else:
            continue
        out.append(Corner(k, j, segs[k].end, seg_tangent(segs[k], 1.0), seg_tangent(segs[j], 0.0),
                          acc if j else 0.0))
    return out


# ---------------------------------------------------------------- fillet / unload
def fillet_corner(cn: Corner, radius: float) -> Optional[tuple[float, Arc]]:
    """Line/line fillet at one corner (0x100579d0): (tangent distance, arc) or None."""
    if radius < 1e-5 or abs(cn.turn) < 1e-5:
        return None
    half = math.acos(max(-1.0, min(1.0, cn.cos_interior))) / 2.0
    if half < 1e-9:
        return None
    d = radius / math.tan(half)
    s = _add(cn.point, cn.t_in, -d)
    e = _add(cn.point, cn.t_out, d)
    left = cn.turn > 0
    nrm = (-cn.t_in[1], cn.t_in[0]) if left else (cn.t_in[1], -cn.t_in[0])
    centre = _add(s, nrm, radius)
    return d, Arc(s, e, centre, ccw=left)


def _rebuild(c: Contour, edits: dict[int, tuple[float, float, Segment]]) -> Contour:
    """edits: corner index i -> (trim at end of seg i, trim at start of seg j, insert)."""
    segs = list(c.segments)
    n = len(segs)
    trim0 = [0.0] * n
    trim1 = [0.0] * n
    ins: dict[int, Segment] = {}
    jmap = {}
    for i, (te, ts, seg) in edits.items():
        j = (i + 1) % n
        trim1[i] += te
        trim0[j] += ts
        ins[i] = seg
        jmap[i] = j
    out: list[Segment] = []
    for k, s in enumerate(segs):
        if trim0[k] or trim1[k]:
            L = s.length()
            assert isinstance(s, Line)
            u0, u1 = trim0[k] / L, 1.0 - trim1[k] / L
            a = (s.start[0] + (s.end[0] - s.start[0]) * u0, s.start[1] + (s.end[1] - s.start[1]) * u0)
            b = (s.start[0] + (s.end[0] - s.start[0]) * u1, s.start[1] + (s.end[1] - s.start[1]) * u1)
            if math.dist(a, b) > 1e-9:
                out.append(Line(a, b))
        else:
            out.append(s)
        if k in ins:
            item = ins[k]
            out.extend(item if isinstance(item, list) else [item])
    # a wrap corner inserts after the last segment: fine; snap the joints
    for k in range(1, len(out)):
        out[k] = _snap(out[k], out[k - 1].end)
    if c.closed and out:
        out[0] = _snap(out[0], out[-1].end)
    return Contour(out, c.closed, dict(c.extra))


def _snap(s: Segment, p: Point) -> Segment:
    if math.dist(s.start, p) > 1e-6:
        return s
    if isinstance(s, Line):
        return Line(p, s.end)
    return Arc(p, s.end, s.center, s.ccw)


def fillet(c: Contour, radius: float, min_angle: float = 0.0, max_angle: float = 180.0,
           at: Optional[Iterable[int]] = None, manual: bool = False) -> Contour:
    """Round corners with radius ``radius`` (CArcRound, 0x1005a650 auto / 0x10059f70 manual).

    ``min_angle``/``max_angle`` are the pd601/pd602 limits on the interior angle (degrees);
    ``at`` restricts to corners whose incoming segment index is listed.  Only line/line corners
    are rounded (the original also rounds arc/arc and line/arc: not ported).  Corners whose
    tangent points do not fit on the remaining line lengths are skipped.
    """
    cmin, cmax = math.cos(math.radians(min_angle)), math.cos(math.radians(max_angle)) - ANG_EPS
    sel = set(at) if at is not None else None
    used_start = [0.0] * len(c.segments)
    used_end = [0.0] * len(c.segments)
    edits = {}
    for cn in corners(c):
        if sel is not None and cn.i not in sel:
            continue
        a, b = c.segments[cn.i], c.segments[cn.j]
        if not (isinstance(a, Line) and isinstance(b, Line)):
            continue
        need = radius + MIN_GLYPH if manual else MIN_GLYPH
        if a.length() < need or b.length() < need:
            continue
        cc = cn.cos_interior
        if not (-COS_LIMIT <= cc <= COS_LIMIT):
            continue
        if sel is None and not (cmax <= cc <= cmin):
            continue
        r = fillet_corner(cn, radius)
        if r is None:
            continue
        d, arc = r
        if used_start[cn.i] + d > a.length() + 1e-9 or used_end[cn.j] + d > b.length() + 1e-9:
            continue
        used_start[cn.i] += d       # consumed at the END of segment i
        used_end[cn.j] += d         # consumed at the START of segment j
        edits[cn.i] = (d, d, arc)
    return _rebuild(c, edits) if edits else c


def unload_loop(c: Contour, radius: float, at: Optional[Iterable[int]] = None) -> Contour:
    """Relief loop (CUnloadAngle, 0x10058370): replace line/line corners by an arc of radius
    ``radius`` around the vertex, going round the outside of the corner wedge."""
    sel = set(at) if at is not None else None
    used_s = [0.0] * len(c.segments)
    used_e = [0.0] * len(c.segments)
    edits = {}
    for cn in corners(c):
        if sel is not None and cn.i not in sel:
            continue
        a, b = c.segments[cn.i], c.segments[cn.j]
        if not (isinstance(a, Line) and isinstance(b, Line)):
            continue
        if a.length() < 2 * radius or b.length() < 2 * radius:
            continue
        if not (-UNLOAD_COS <= cn.cos_interior <= UNLOAD_COS):
            continue
        if used_s[cn.i] + radius > a.length() or used_e[cn.j] + radius > b.length():
            continue
        s = _add(cn.point, cn.t_in, -radius)
        e = _add(cn.point, cn.t_out, radius)
        used_s[cn.i] += radius
        used_e[cn.j] += radius
        edits[cn.i] = (radius, radius, Arc(s, e, cn.point, ccw=cn.turn >= 0))
    return _rebuild(c, edits) if edits else c


# ---------------------------------------------------------------- alpha
def bspline_points(ctrl: Sequence[Point], knots: Sequence[float], n: int = 24) -> list[Point]:
    """Sample a clamped cubic B-spline with de Boor's algorithm (n+1 points)."""
    p = 3
    t0, t1 = knots[p], knots[len(knots) - p - 1]
    out = []
    for s in range(n + 1):
        t = t0 + (t1 - t0) * s / n
        k = p
        while k + 1 < len(knots) - p - 1 and t >= knots[k + 1]:
            k += 1
        d = [list(ctrl[j + k - p]) for j in range(p + 1)]
        for r in range(1, p + 1):
            for j in range(p, r - 1, -1):
                den = knots[j + 1 + k - r] - knots[j + k - p]
                al = 0.0 if den == 0 else (t - knots[j + k - p]) / den
                d[j] = [(1 - al) * d[j - 1][0] + al * d[j][0], (1 - al) * d[j - 1][1] + al * d[j][1]]
        out.append((d[p][0], d[p][1]))
    return out


ALPHA_KNOTS = (0.0, 0.0, 0.0, 0.0, 2.0, 4.0, 4.0, 4.0, 4.0)   # 0x100587a0


def alpha_loop(cn: Corner, lead: float, n: int = 24) -> list[Line]:
    """The alpha loop inserted at one vertex (flattened B-spline)."""
    a, b = cn.t_in, cn.t_out
    amb = (a[0] - b[0], a[1] - b[1])
    x = math.hypot(*amb)          # INFERRED: sqrt(X) = |t_in - t_out|
    k = 0.5 * lead / max(x, 1e-9)
    P = cn.point
    ctrl = [P, _add(P, a, k), _add(P, amb, k), _add(P, b, -k), P]
    pts = bspline_points(ctrl, ALPHA_KNOTS, n)
    pts[0] = pts[-1] = P
    return [Line(p, q) for p, q in zip(pts, pts[1:]) if math.dist(p, q) > 1e-12]


def alpha_corners(c: Contour, max_angle: float = 90.0, min_edge: float = 1.0, lead: float = 2.0,
                  mode: int = 0, depth: int = 1, start_dist: float = 0.0,
                  ccw: Optional[bool] = None, at: Optional[Iterable[int]] = None) -> Contour:
    """Insert alpha loops (CAlphaRound, 0x100587a0) at sharp corners.

    mode: 0 auto, 1 outside (convex), 2 inner (reflex), 3 both (pd610..pd613).
    depth: containment depth (1 = outermost) for the auto mode; ccw: orientation of the
    contour (computed when None); start_dist: arc length of the start point.
    """
    if ccw is None:
        from ..geom.contour import is_ccw
        ccw = is_ccw(c) if c.closed else True
    s = 1 if ccw else -1
    if mode == 0 and depth % 2 == 0:
        s = -s
    elif mode == 2:
        s = -s
    cmin = math.cos(math.radians(max_angle))
    skip_r = min(ALPHA_START_K * lead, 0.5)
    L = c.length()
    sel = set(at) if at is not None else None
    edits = {}
    for cn in corners(c):
        if sel is not None and cn.i not in sel:
            continue
        a, b = c.segments[cn.i], c.segments[cn.j]
        if a.length() < min_edge or b.length() < min_edge:
            continue
        if not (cmin <= cn.cos_interior <= ALPHA_COS):
            continue
        if mode != 3 and cn.turn * s < 0:
            continue
        dd = abs(cn.dist - start_dist)
        if c.closed and L > 0:
            dd = min(dd, L - dd)
        if dd < skip_r:
            continue
        edits[cn.i] = (0.0, 0.0, alpha_loop(cn, lead))
    return _rebuild(c, edits) if edits else c


# ---------------------------------------------------------------- cooling points
def auto_cool_points(c: Contour, max_angle: float = 90.0, sharp: bool = True,
                     lead_point: bool = False, start_dist: float = 0.0) -> list[float]:
    """Automatic cooling points (0x1006a220 -> 0x10057c10) as arc lengths (mm) from the
    contour start, the form ``Craft.extra['cool_points']`` accepts."""
    out: list[float] = []
    if sharp:
        cmin = math.cos(math.radians(max_angle))
        for cn in corners(c, close_tol=math.inf if c.closed else 0.0):
            if cn.cos_interior >= cmin - 1e-9:
                out.append(cn.dist)
    # the original compares fractions of the length with 0.01 (0x1011d800)
    if lead_point and not any(abs(d - start_dist) < 0.01 * c.length() for d in out):
        out.append(start_dist)
    return sorted(out)


# ---------------------------------------------------------------- kerf joins
def cubic_bezier_points(p0, p1, p2, p3, n: int = 16) -> list[Point]:
    pts = []
    for k in range(n + 1):
        t = k / n
        u = 1 - t
        b0, b1, b2, b3 = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
        pts.append((b0 * p0[0] + b1 * p1[0] + b2 * p2[0] + b3 * p3[0],
                    b0 * p0[1] + b1 * p1[1] + b2 * p2[1] + b3 * p3[1]))
    return pts


def bezier_corner_join(a: Point, ta: Point, b: Point, tb: Point, n: int = 16) -> list[Line]:
    """Cubic Bezier join A->B with handle length |AB| (0x10038130), flattened to lines."""
    h = math.dist(a, b)
    pts = cubic_bezier_points(a, _add(a, ta, h), _add(b, tb, -h), b, n)
    pts[0], pts[-1] = a, b
    return [Line(p, q) for p, q in zip(pts, pts[1:])]


def _tangent_arc(a: Point, ta: Point, b: Point) -> Optional[Arc]:
    c = (b[0] - a[0], b[1] - a[1])
    cr = _cross(ta, c)
    if abs(cr) < 1e-12:
        return None
    R = _dot(c, c) / (2 * cr)          # signed: > 0 centre on the left of ta
    centre = (a[0] - ta[1] * R, a[1] + ta[0] * R)
    return Arc(a, b, centre, ccw=R > 0)


def kerf_corner_join(a: Point, ta: Point, b: Point, tb: Point) -> list[Segment]:
    """Join two offset pieces across a convex gap the way 0x10038130 does: tangent arc when
    the corner is symmetric, cubic Bezier when |ta x AB| and |tb x AB| differ by >= 0.01."""
    ab = (b[0] - a[0], b[1] - a[1])
    if math.hypot(*ab) < 1e-9:
        return []
    if abs(abs(_cross(ta, ab)) - abs(_cross(tb, ab))) >= BEZIER_ASYM:
        return list(bezier_corner_join(a, ta, b, tb))
    arc = _tangent_arc(a, ta, b)
    return [arc] if arc is not None else [Line(a, b)]
