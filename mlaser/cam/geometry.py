"""Segment/contour primitives used by the CAM modules (arc-length based)."""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Optional

from ..model import Arc, Contour, Line, Point, Segment
from ..io.geom import signed_area, reverse_contour  # re-export

__all__ = ["seg_point", "seg_tangent", "split_seg", "sub_seg", "locate", "point_at",
           "tangent_at", "rotate_start", "sub_path", "is_ccw", "signed_area",
           "reverse_contour", "point_in_contour", "polyline", "contour_bbox", "rot", "turn",
           "stored_length", "SplineCurve", "CurveRef"]


def rot(v, a):
    c, s = math.cos(a), math.sin(a)
    return (v[0] * c - v[1] * s, v[0] * s + v[1] * c)


def _norm(v):
    n = math.hypot(*v) or 1.0
    return (v[0] / n, v[1] / n)


def seg_point(s: Segment, u: float) -> Point:
    if isinstance(s, Line):
        if u == 0.0:
            return tuple(s.start)
        if u == 1.0:
            return tuple(s.end)
        return (s.start[0] + (s.end[0] - s.start[0]) * u, s.start[1] + (s.end[1] - s.start[1]) * u)
    return s.point_at(u)


def seg_tangent(s: Segment, u: float) -> Point:
    """Unit direction of travel at parameter u."""
    if isinstance(s, Line):
        return _norm((s.end[0] - s.start[0], s.end[1] - s.start[1]))
    p = s.point_at(u)
    rx, ry = p[0] - s.center[0], p[1] - s.center[1]
    return _norm((-ry, rx) if s.ccw else (ry, -rx))


def sub_seg(s: Segment, u0: float, u1: float) -> Segment:
    p0, p1 = seg_point(s, u0), seg_point(s, u1)
    if isinstance(s, Line):
        return Line(p0, p1)
    if u0 <= 1e-12 and u1 >= 1 - 1e-12:
        return Arc(s.start, s.end, s.center, s.ccw)
    return Arc(p0, p1, s.center, s.ccw)


def split_seg(s: Segment, u: float) -> tuple[Segment, Segment]:
    return sub_seg(s, 0.0, u), sub_seg(s, u, 1.0)


def locate(c: Contour, dist: float) -> tuple[int, float]:
    """(segment index, local parameter) at arc length ``dist`` from the contour start."""
    acc = 0.0
    for i, s in enumerate(c.segments):
        L = s.length()
        if dist <= acc + L + 1e-12 or i == len(c.segments) - 1:
            return i, (0.0 if L <= 0 else min(max((dist - acc) / L, 0.0), 1.0))
        acc += L
    return len(c.segments) - 1, 1.0


def point_at(c: Contour, dist: float) -> Point:
    i, u = locate(c, dist)
    return seg_point(c.segments[i], u)


def tangent_at(c: Contour, dist: float) -> Point:
    i, u = locate(c, dist)
    return seg_tangent(c.segments[i], u)


def rotate_start(c: Contour, dist: float) -> Contour:
    """Closed contour re-started at arc length ``dist`` (splits a segment if needed)."""
    if not c.closed or not c.segments:
        return c
    L = c.length()
    dist %= L if L > 0 else 1.0
    if dist < 1e-9 or abs(dist - L) < 1e-9:
        return Contour(list(c.segments), True)
    i, u = locate(c, dist)
    s = c.segments[i]
    segs = []
    if u >= 1 - 1e-9:
        segs = c.segments[i + 1:] + c.segments[:i + 1]
    elif u <= 1e-9:
        segs = c.segments[i:] + c.segments[:i]
    else:
        a, b = split_seg(s, u)
        segs = [b] + c.segments[i + 1:] + c.segments[:i] + [a]
    return Contour(segs, True)


def sub_path(c: Contour, d0: float, d1: float) -> list[Segment]:
    """Segments between arc lengths d0 < d1 (d1 may exceed the length on closed
    contours: the path wraps, which implements overcut)."""
    L = c.length()
    out: list[Segment] = []
    if L <= 0 or d1 <= d0:
        return out
    base = 0.0
    while base < d1 - 1e-12:
        acc = base
        for s in c.segments:
            sl = s.length()
            a, b = acc, acc + sl
            lo, hi = max(a, d0), min(b, d1)
            if hi - lo > 1e-9 and sl > 0:
                # a range that reaches a segment end keeps that end exactly ((acc + sl) - acc
                # need not be sl in floating point; parity JC-13: (0, 0) -> (38.137167, 0))
                eps = 1e-12 * max(1.0, abs(b))
                u0 = 0.0 if abs(lo - a) <= eps else (lo - a) / sl
                u1 = 1.0 if abs(hi - b) <= eps else (hi - a) / sl
                out.append(sub_seg(s, u0, u1))
            acc = b
        if not c.closed:
            break
        base += L
    return out


def stored_length(c: Contour) -> float:
    """The contour length the original works with (CGlyContour +0x48).

    A contour loaded from .chf keeps the length stored in the file: the loader FUN_10072ad0
    reads it and does not recompute it from the (6-decimal) glyphs, and every fraction ->
    position conversion multiplies by it (FUN_10063260, FUN_10063be0, FUN_1008bb90: target =
    +0x48 * t, then the glyph lengths are walked).  E.g. r12_polyline #0 stores 102.831853
    (= 40 + 20 pi) while its glyphs sum to 102.83186 (arc from 6-decimal angles), so a start
    fraction lands 1.4e-6 mm earlier than ``t * c.length()`` (parity JC-11).  Used only while the
    contour still has its source glyphs (``Contour.source_glyphs()``: the segments are unchanged
    since the load) and the stored value agrees with the geometry to 1e-4; otherwise the
    computed length."""
    L = c.length()
    ex = c.extra if isinstance(c.extra, dict) else {}
    rec = ex.get("chf")
    v = getattr(rec, "length", None)
    if v and c.source_glyphs() is not None and abs(float(v) - L) <= 1e-4 * max(1.0, L):
        return float(v)
    return L


def is_ccw(c: Contour) -> bool:
    return signed_area(c) > 0


def polyline(c: Contour, tol: float = 0.02) -> list[Point]:
    return list(c.points(tol))


def contour_bbox(c: Contour):
    pts = polyline(c)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def point_in_contour(p: Point, c: Contour, poly=None) -> bool:
    poly = poly or polyline(c)
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            xi = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if xi > x:
                inside = not inside
    return inside


def turn(t_in, t_out) -> float:
    """Signed turning angle from t_in to t_out (+ = left)."""
    return math.atan2(t_in[0] * t_out[1] - t_in[1] * t_out[0], t_in[0] * t_out[0] + t_in[1] * t_out[1])


# --------------------------------------------------------------------------- exact splines
class SplineCurve:
    """The exact curve of a spline glyph (CEditableSpline, glyph kind 7) next to the polyline the
    contour's segments flatten it to.

    The original keeps the spline through the whole cutting pipeline (parity JC-07): the glyph's
    length +0x48 is the length of the splineAnalyer polyline, whose samples (u, x, y, s) sit at
    +0x124 (FUN_100953d0, ported as ``io.geom.flatten_bspline_params``); a contour fraction is
    turned into a curve parameter by FUN_10093390 (target = t * length; the first sample k >= 1
    with target <= s_k; u linear in s between samples k-1 and k) and the point / tangent are
    then evaluated on the TRUE curve (vtable +0x40 FUN_10093b40 -> analyser +0x28, +0x44
    FUN_10093dc0 -> analyser +0x3c).  Arc lengths ``s`` here are always in the glyph's own
    direction; :meth:`travel` converts a travel-order distance."""

    def __init__(self, ctrl, knots, gdir: int = 1):
        from ..io.geom import flatten_bspline_params
        self.ctrl = [tuple(map(float, c[:2])) for c in ctrl]
        self.knots = [float(k) for k in knots]
        self.dir = -1 if gdir == -1 else 1
        tab = flatten_bspline_params(self.ctrl, self.knots)
        self.u = [u for u, _p in tab]
        self.pts = [p for _u, p in tab]
        self.s = [0.0]
        for a, b in zip(self.pts, self.pts[1:]):
            self.s.append(self.s[-1] + math.dist(a, b))
        self.length = self.s[-1]

    @classmethod
    def from_glyph(cls, g) -> Optional["SplineCurve"]:
        d = getattr(g, "data", None) or {}
        if "ctrl" not in d or "knots" not in d:
            return None
        return cls(d["ctrl"], d["knots"], getattr(g, "dir", 1))

    def u_at(self, s: float) -> float:
        """FUN_10093390: curve parameter at own arc length ``s`` of the sample polyline."""
        n = len(self.s)
        if n < 2:
            return self.u[0] if self.u else 0.0
        k = bisect.bisect_left(self.s, s, 1, n - 1) if s > self.s[1] else 1
        while k < n - 1 and not (s <= self.s[k]):
            k += 1
        ds = self.s[k] - self.s[k - 1]
        f = (s - self.s[k - 1]) / ds if ds > 0 else 0.0
        return self.u[k - 1] + (self.u[k] - self.u[k - 1]) * f

    def travel(self, s_travel: float) -> float:
        """Own arc length of a travel-order distance (the glyph may be run backwards)."""
        return s_travel if self.dir == 1 else self.length - s_travel

    def point(self, u: float) -> Point:
        from ..io.geom import _bs_eval
        lo, hi = self.u[0], self.u[-1]
        if u <= lo + 1e-12:
            return self.pts[0]
        if u >= hi - 1e-12:
            return self.pts[-1]
        return _bs_eval(self.ctrl, self.knots, u)

    def derivative(self, u: float, order: int = 1) -> Point:
        from ..io.geom import bspline_derivative
        return bspline_derivative(self.ctrl, self.knots, u, order)

    def tangent(self, u: float) -> Point:
        """Unit tangent in the glyph's own direction: the analyser's EvTangent (FUN_10093dc0 ->
        analyser +0x3c -> ON_Curve::EvTangent FUN_1004d1d0): first derivative of the openNURBS
        evaluation (a single-span glyph takes the Bezier path FUN_10057270) unitized with
        ON_3dVector::Unitize, as ``motion.curvetable`` ports them bit for bit (parity JC-07: the
        lead-in start then matches to the last bit)."""
        from ..motion.curvetable import _Nurbs, _ev_tangent
        if len(self.knots) == len(self.ctrl) + 4 and len(self.ctrl) >= 4:
            cur = getattr(self, "_on_curve", None)
            if cur is None:
                cur = self._on_curve = _Nurbs(self.ctrl, self.knots)
            ok, _p, t = _ev_tangent(cur, u)
            if ok:
                return t
        d = self.derivative(u)
        n = math.hypot(*d) or 1.0
        return (d[0] / n, d[1] / n)

    def curvature(self, u: float) -> float:
        """Signed curvature (1/mm, + = left turn in the glyph's own direction)."""
        d1, d2 = self.derivative(u), self.derivative(u, 2)
        n = math.hypot(*d1)
        return 0.0 if n < 1e-15 else (d1[0] * d2[1] - d1[1] * d2[0]) / n ** 3

    def point_at_travel(self, s_travel: float) -> Point:
        return self.point(self.u_at(self.travel(s_travel)))

    def tangent_at_travel(self, s_travel: float) -> Point:
        t = self.tangent(self.u_at(self.travel(s_travel)))
        return t if self.dir == 1 else (-t[0], -t[1])


@dataclass
class CurveRef:
    """The exact geometry of one cut piece that the contour flattens: the piece runs on
    ``curve`` from parameter ``u0`` to ``u1`` (in travel order; u decreases on a backwards
    glyph).  Consumers that understand curves (the planner's curvature, sample positions on
    the true curve) use it; everything else keeps using the piece's Line/Arc chord geometry."""
    curve: SplineCurve
    u0: float
    u1: float
    # the same range as travel distances on the glyph (0 .. curve.length): the original's
    # primitive window is t / glyph length (FUN_1006f2d0 +0xc0/+0xc8, used by motion.smooth)
    t0: Optional[float] = None
    t1: Optional[float] = None

    def sub(self, f0: float, f1: float) -> "CurveRef":
        """The part from fraction ``f0`` to ``f1`` of this piece."""
        t0 = t1 = None
        if self.t0 is not None and self.t1 is not None:
            t0 = self.t0 + (self.t1 - self.t0) * f0
            t1 = self.t0 + (self.t1 - self.t0) * f1
        return CurveRef(self.curve, self.u0 + (self.u1 - self.u0) * f0,
                        self.u0 + (self.u1 - self.u0) * f1, t0, t1)

    def point(self, f: float) -> Point:
        """Point at fraction ``f`` of the piece (parameter linear in arc length within the
        piece, as FUN_10093390 maps between samples)."""
        return self.curve.point(self.u0 + (self.u1 - self.u0) * f)
