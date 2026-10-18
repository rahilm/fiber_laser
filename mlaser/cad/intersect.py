"""Segment intersections and arc-length parameterisation of contours.

Used by trim / extend / split (CADModule CTrim vtable 0x1011f7a4, 0x100bebb0 / 0x100beff0).
Arcs are handled analytically (no flattening) so trimmed results keep their arcs.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

from mlaser.model import Arc, Contour, Line, Point, Segment

EPS = 1e-9


def _ang(c: Point, p: Point) -> float:
    return math.atan2(p[1] - c[1], p[0] - c[0])


def is_full_circle(s: Segment) -> bool:
    return isinstance(s, Arc) and math.dist(s.start, s.end) < 1e-9


def seg_param(s: Segment, p: Point) -> float:
    """Normalised parameter in [0, 1] of the point of ``s`` closest to ``p`` (unclamped for
    lines is clamped to [0, 1]; arcs measure the angle from the start in the arc direction)."""
    if isinstance(s, Line):
        dx, dy = s.end[0] - s.start[0], s.end[1] - s.start[1]
        L2 = dx * dx + dy * dy
        if L2 < EPS:
            return 0.0
        t = ((p[0] - s.start[0]) * dx + (p[1] - s.start[1]) * dy) / L2
        return max(0.0, min(1.0, t))
    sw = s.sweep()
    a = _ang(s.center, p) - _ang(s.center, s.start)
    if sw > 0:
        a %= 2 * math.pi
    else:
        a = -((-a) % (2 * math.pi))
    t = a / sw if abs(sw) > EPS else 0.0
    if t > 1.0:     # outside the arc: snap to the nearer end (angularly)
        over = (t - 1.0) * abs(sw)
        under = 2 * math.pi - t * abs(sw)
        t = 1.0 if over < under else 0.0
    return max(0.0, min(1.0, t))


def seg_point(s: Segment, t: float) -> Point:
    if isinstance(s, Line):
        return (s.start[0] + (s.end[0] - s.start[0]) * t, s.start[1] + (s.end[1] - s.start[1]) * t)
    return s.point_at(t)


def seg_distance(s: Segment, p: Point) -> float:
    return math.dist(p, seg_point(s, seg_param(s, p)))


def sub_segment(s: Segment, t0: float, t1: float) -> Segment:
    """Piece of ``s`` between normalised parameters t0 < t1 (same direction)."""
    a, b = seg_point(s, t0), seg_point(s, t1)
    if isinstance(s, Line):
        return Line(a, b)
    return Arc(a, b, s.center, s.ccw)


def _on_seg(s: Segment, p: Point, tol: float = 1e-7) -> Optional[float]:
    t = seg_param(s, p)
    return t if math.dist(seg_point(s, t), p) <= tol * max(1.0, s.length()) + 1e-7 else None


def _line_line(a: Line, b: Line) -> list[Point]:
    (x1, y1), (x2, y2) = a.start, a.end
    (x3, y3), (x4, y4) = b.start, b.end
    d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(d) < 1e-12:
        return []
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / d
    return [(x1 + t * (x2 - x1), y1 + t * (y2 - y1))]


def _line_circle(a: Line, c: Point, r: float) -> list[Point]:
    (x1, y1), (x2, y2) = a.start, a.end
    dx, dy = x2 - x1, y2 - y1
    fx, fy = x1 - c[0], y1 - c[1]
    A = dx * dx + dy * dy
    if A < EPS:
        return []
    B = 2 * (fx * dx + fy * dy)
    C = fx * fx + fy * fy - r * r
    disc = B * B - 4 * A * C
    if disc < -1e-9 * A:
        return []
    disc = math.sqrt(max(disc, 0.0))
    ts = {(-B - disc) / (2 * A), (-B + disc) / (2 * A)}
    return [(x1 + t * dx, y1 + t * dy) for t in ts]


def _circle_circle(c1: Point, r1: float, c2: Point, r2: float) -> list[Point]:
    d = math.dist(c1, c2)
    if d < EPS or d > r1 + r2 + 1e-9 or d < abs(r1 - r2) - 1e-9:
        return []
    a = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
    h = math.sqrt(max(r1 * r1 - a * a, 0.0))
    mx = c1[0] + a * (c2[0] - c1[0]) / d
    my = c1[1] + a * (c2[1] - c1[1]) / d
    ox, oy = -h * (c2[1] - c1[1]) / d, h * (c2[0] - c1[0]) / d
    if h < 1e-9:
        return [(mx, my)]
    return [(mx + ox, my + oy), (mx - ox, my - oy)]


def curve_intersections(a: Segment, b: Segment) -> list[Point]:
    """Intersections of the *carrier curves* (infinite line / full circle) of a and b."""
    if isinstance(a, Line) and isinstance(b, Line):
        return _line_line(a, b)
    if isinstance(a, Line):
        return _line_circle(a, b.center, b.radius)
    if isinstance(b, Line):
        return _line_circle(b, a.center, a.radius)
    return _circle_circle(a.center, a.radius, b.center, b.radius)


def intersections(a: Segment, b: Segment, tol: float = 1e-6) -> list[tuple[float, float, Point]]:
    """(ta, tb, point) for every intersection lying on both bounded segments."""
    out = []
    for p in curve_intersections(a, b):
        ta, tb = _on_seg(a, p, tol), _on_seg(b, p, tol)
        if ta is not None and tb is not None:
            out.append((ta, tb, p))
    return out


# ---------------------------------------------------------------- contour parameterisation
def seg_offsets(c: Contour) -> list[float]:
    """Cumulative arc length at the start of every segment (+ total at the end)."""
    acc, out = 0.0, [0.0]
    for s in c.segments:
        acc += s.length()
        out.append(acc)
    return out


def locate_point(c: Contour, p: Point) -> tuple[int, float, float, float]:
    """Nearest point of contour ``c`` to ``p``: (segment index, t, arc-length position, distance)."""
    offs = seg_offsets(c)
    best = (0, 0.0, 0.0, math.inf)
    for i, s in enumerate(c.segments):
        t = seg_param(s, p)
        d = math.dist(seg_point(s, t), p)
        if d < best[3]:
            best = (i, t, offs[i] + t * s.length(), d)
    return best


def position_point(c: Contour, pos: float) -> tuple[int, float]:
    """(segment index, t) of arc-length position ``pos``."""
    offs = seg_offsets(c)
    for i, s in enumerate(c.segments):
        L = s.length()
        if pos <= offs[i + 1] + 1e-12 or i == len(c.segments) - 1:
            return i, (0.0 if L < EPS else max(0.0, min(1.0, (pos - offs[i]) / L)))
    return 0, 0.0


def sub_contour(c: Contour, p0: float, p1: float) -> list[Segment]:
    """Segments between arc-length positions p0 < p1 (no wrap-around)."""
    out: list[Segment] = []
    offs = seg_offsets(c)
    for i, s in enumerate(c.segments):
        a, b = offs[i], offs[i + 1]
        if b <= p0 + 1e-9 or a >= p1 - 1e-9:
            continue
        L = s.length()
        t0 = 0.0 if p0 <= a or L < EPS else (p0 - a) / L
        t1 = 1.0 if p1 >= b or L < EPS else (p1 - a) / L
        if t1 - t0 > 1e-12:
            out.append(s if (t0 == 0.0 and t1 == 1.0) else sub_segment(s, t0, t1))
    return out


def contour_crossings(c: Contour, others: Iterable[Contour], self_too: bool = True) -> list[float]:
    """Sorted arc-length positions on ``c`` where it meets the other contours (and itself)."""
    offs = seg_offsets(c)
    pos: list[float] = []
    for o in others:
        if o is c:
            continue
        for i, s in enumerate(c.segments):
            for t in o.segments:
                for ta, _tb, _p in intersections(s, t):
                    pos.append(offs[i] + ta * s.length())
    if self_too:
        n = len(c.segments)
        for i in range(n):
            for j in range(i + 1, n):
                adjacent = j == i + 1 or (c.closed and i == 0 and j == n - 1)
                for ta, tb, p in intersections(c.segments[i], c.segments[j]):
                    if adjacent and (min(math.dist(p, c.segments[i].start), math.dist(p, c.segments[i].end)) < 1e-7):
                        continue
                    pos.append(offs[i] + ta * c.segments[i].length())
                    pos.append(offs[j] + tb * c.segments[j].length())
    pos.sort()
    out: list[float] = []
    for v in pos:
        if not out or v - out[-1] > 1e-7:
            out.append(v)
    return out
