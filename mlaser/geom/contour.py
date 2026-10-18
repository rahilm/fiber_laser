"""Arc-length based segment and contour operations (formerly ``mlaser.cam.geometry``).

Points and tangents at an arc length, sub-paths (overcut wraps closed contours), start
rotation, orientation, point-in-polygon and the polyline bounding box.  ``stored_length`` keeps
the .chf contour length the original computes with (parity JC-11).
"""
from __future__ import annotations

import math

from ..model import Arc, Contour, Line, Point, Segment
from .curves import reverse_contour, signed_area  # re-export

__all__ = ["seg_point", "seg_tangent", "split_seg", "sub_seg", "locate", "point_at",
           "tangent_at", "rotate_start", "sub_path", "is_ccw", "signed_area",
           "reverse_contour", "point_in_contour", "polyline", "contour_bbox", "rot", "turn",
           "stored_length"]


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
