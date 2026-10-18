"""Contour editing: explode, merge, split, trim, extend.

* explode (mf85, cmd 0x3413 -> ICADModule+0xe8 -> 0x100b7ce0): every selected contour with
  more than one element (or a polyline with several vertices) is replaced by one open graph
  per element.  [O]
* merge (mf84, cmd 0x3412 -> CGraphParaDlg, tolerance ``GRP.ManualConnectGate`` pd368,
  default 0.1 mm -> ICADModule+0xe4 -> 0x100bf7e0): open contours whose end points are within
  the tolerance are chained into one contour, closed when the chain meets itself.  [O call
  chain, chaining rule I]
* split (mf216, cmd 0x341c -> view mode 12 -> click handler 0x100ea210: nearest point on a
  contour 0x100546e0, split 0x100b6bb0).  [O]
* trim (A250707_12 "Cut", cmd 0x348f -> mode 16 -> 0x100ea620 -> 0x100bebb0) and extend
  (A250707_13, cmd 0x3490 -> mode 17 -> 0x100ea6b0 -> 0x100beff0): CTrim.  The cut piece is
  the one between the intersections that bracket the clicked point; extend prolongs the
  clicked open end to the nearest intersection.  [O entry points, behaviour I]

All functions take and return model objects and never touch Qt.
"""
from __future__ import annotations

import copy
import math
from typing import Iterable, Optional, Sequence

from mlaser.model import Arc, Contour, Line, Part, Point, Segment

from .intersect import (contour_crossings, curve_intersections, is_full_circle, locate_point,
                        position_point, seg_distance, sub_contour, sub_segment)

MERGE_TOL = 0.1     # GRP.ManualConnectGate default (pd368)


def _clone_part(p: Part, contours: list[Contour], suffix: str = "") -> Part:
    q = Part(contours=contours, craft=copy.deepcopy(p.craft), name=p.name + suffix,
             selected=p.selected, extra=copy.deepcopy(p.extra))
    q.extra.pop("group", None)
    return q


def reversed_contour(c: Contour) -> Contour:
    return Contour([s.reversed() for s in reversed(c.segments)], c.closed, copy.deepcopy(c.extra))


# ----------------------------------------------------------------------------- explode
def explode_contour(c: Contour) -> list[Contour]:
    """One contour per element; a lone full circle stays a closed circle."""
    if len(c.segments) <= 1:
        return [c]
    out = []
    for s in c.segments:
        out.append(Contour([copy.deepcopy(s)], is_full_circle(s)))
    return out


def explode_parts(parts: Sequence[Part]) -> list[Part]:
    """0x100b7ce0: replacement parts for ``parts`` (each element its own graph)."""
    out = []
    for p in parts:
        for c in p.contours:
            for k, e in enumerate(explode_contour(c)):
                out.append(_clone_part(p, [e], f"#{k}" if len(c.segments) > 1 else ""))
    return out


# ----------------------------------------------------------------------------- merge
def join_contours(contours: Sequence[Contour], tol: float = MERGE_TOL) -> list[Contour]:
    """Chain open contours end-to-end (either direction) within ``tol``; closed contours are
    returned unchanged.  A chain whose ends meet within ``tol`` is closed."""
    closed = [c for c in contours if c.closed or not c.segments]
    pool = [Contour(list(c.segments), False, dict(c.extra)) for c in contours if not c.closed and c.segments]
    out: list[Contour] = []
    while pool:
        cur = pool.pop(0)
        grown = True
        while grown:
            grown = False
            for i, o in enumerate(pool):
                s, e = cur.segments[0].start, cur.segments[-1].end
                os_, oe = o.segments[0].start, o.segments[-1].end
                if math.dist(e, os_) <= tol:
                    cur.segments += _snap_start(o.segments, e)
                elif math.dist(e, oe) <= tol:
                    cur.segments += _snap_start(reversed_contour(o).segments, e)
                elif math.dist(s, oe) <= tol:
                    cur.segments = _snap_end(o.segments, s) + cur.segments
                elif math.dist(s, os_) <= tol:
                    cur.segments = _snap_end(reversed_contour(o).segments, s) + cur.segments
                else:
                    continue
                pool.pop(i)
                grown = True
                break
        if len(cur.segments) and math.dist(cur.segments[0].start, cur.segments[-1].end) <= tol \
                and cur.length() > 2 * tol:
            cur.segments[-1] = _with_end(cur.segments[-1], cur.segments[0].start)
            cur.closed = True
        out.append(cur)
    return closed + out


def _with_end(s: Segment, p: Point) -> Segment:
    return Line(s.start, p) if isinstance(s, Line) else Arc(s.start, p, s.center, s.ccw)


def _with_start(s: Segment, p: Point) -> Segment:
    return Line(p, s.end) if isinstance(s, Line) else Arc(p, s.end, s.center, s.ccw)


def _snap_start(segs: list[Segment], p: Point) -> list[Segment]:
    segs = list(segs)
    segs[0] = _with_start(segs[0], p)
    return segs


def _snap_end(segs: list[Segment], p: Point) -> list[Segment]:
    segs = list(segs)
    segs[-1] = _with_end(segs[-1], p)
    return segs


def merge_parts(parts: Sequence[Part], tol: float = MERGE_TOL) -> list[Part]:
    """0x100bf7e0 on the selection: all contours of ``parts`` joined; one part per result
    (craft of the first source part)."""
    if not parts:
        return []
    contours = [c for p in parts for c in p.contours]
    joined = join_contours(contours, tol)
    base = parts[0]
    return [_clone_part(base, [c], "") for c in joined]


# ----------------------------------------------------------------------------- split
def split_contour(c: Contour, pos: float) -> list[Contour]:
    """Split at arc-length position ``pos``.  Open -> two open contours; closed -> one open
    contour that starts and ends at the split point."""
    L = c.length()
    if c.closed:
        if L <= 1e-9:
            return [c]
        segs = sub_contour(c, pos, L) + sub_contour(c, 0.0, pos)
        if len(c.segments) == 1 and is_full_circle(c.segments[0]):
            s = c.segments[0]
            i, t = position_point(c, pos)
            p = s.point_at(t)
            segs = [Arc(p, p, s.center, s.ccw)]   # full circle re-started; stays one element
            return [Contour(segs, False, dict(c.extra))]
        return [Contour(segs, False, dict(c.extra))]
    if pos <= 1e-7 or pos >= L - 1e-7:
        return [c]
    return [Contour(sub_contour(c, 0.0, pos), False, dict(c.extra)),
            Contour(sub_contour(c, pos, L), False, dict(c.extra))]


def nearest_contour(parts: Sequence[Part], p: Point, tol: float) -> Optional[tuple[int, int, float, float]]:
    """(part index, contour index, arc-length position, distance) of the contour nearest to
    ``p`` within ``tol`` (0x100546e0)."""
    best = None
    for pi, part in enumerate(parts):
        for ci, c in enumerate(part.contours):
            if not c.segments:
                continue
            _i, _t, pos, d = locate_point(c, p)
            if d <= tol and (best is None or d < best[3]):
                best = (pi, ci, pos, d)
    return best


def split_at(parts: list[Part], p: Point, tol: float) -> bool:
    """Split the contour under ``p`` (in place on ``parts``).  The pieces become separate
    parts (0x100b6bb0 creates new graphs).  Returns False if nothing was hit."""
    hit = nearest_contour(parts, p, tol)
    if hit is None:
        return False
    pi, ci, pos, _ = hit
    part = parts[pi]
    pieces = split_contour(part.contours[ci], pos)
    if len(pieces) == 1 and pieces[0] is part.contours[ci]:
        return False
    rest = [c for k, c in enumerate(part.contours) if k != ci]
    new_parts = []
    if rest:
        new_parts.append(_clone_part(part, rest))
    for k, c in enumerate(pieces):
        new_parts.append(_clone_part(part, [c], f"/{k}"))
    parts[pi:pi + 1] = new_parts
    return True


# ----------------------------------------------------------------------------- trim
def trim_contour(c: Contour, click_pos: float, crossings: Sequence[float]) -> Optional[list[Contour]]:
    """Remove the piece of ``c`` between the crossings bracketing ``click_pos``.  None when
    ``c`` has no usable crossing (nothing to cut)."""
    L = c.length()
    xs = [x for x in crossings if -1e-9 <= x <= L + 1e-9]
    if c.closed:
        xs = sorted({round(x % L, 9) if L else 0.0 for x in xs})
        if len(xs) < 2:
            return None
        before = [x for x in xs if x <= click_pos]
        after = [x for x in xs if x > click_pos]
        a = before[-1] if before else xs[-1]
        b = after[0] if after else xs[0]
        # keep the complement: from b forward (cyclically) to a
        if b <= a:     # piece to remove wraps around position 0 ... keep [b, a]
            keep = sub_contour(c, b, a)
        else:
            keep = sub_contour(c, b, L) + sub_contour(c, 0.0, a)
        return [Contour(keep, False, dict(c.extra))] if keep else []
    inner = [x for x in xs if 1e-7 < x < L - 1e-7]
    if not inner:
        return None
    before = [x for x in inner if x <= click_pos]
    after = [x for x in inner if x > click_pos]
    a = before[-1] if before else 0.0
    b = after[0] if after else L
    out = []
    if a > 1e-7:
        out.append(Contour(sub_contour(c, 0.0, a), False, dict(c.extra)))
    if b < L - 1e-7:
        out.append(Contour(sub_contour(c, b, L), False, dict(c.extra)))
    return out


def trim_at(parts: list[Part], p: Point, tol: float) -> bool:
    """CTrim (0x100bebb0): cut the clicked piece of the contour under ``p`` back to its
    neighbouring intersections with all other contours (and itself)."""
    hit = nearest_contour(parts, p, tol)
    if hit is None:
        return False
    pi, ci, pos, _ = hit
    part = parts[pi]
    c = part.contours[ci]
    others = [o for q in parts for o in q.contours if o is not c]
    res = trim_contour(c, pos, contour_crossings(c, others))
    if res is None:
        return False
    rest = [k for k in part.contours if k is not c]
    new = []
    if rest:
        new.append(_clone_part(part, rest))
    for k, piece in enumerate(res):
        if piece.segments:
            new.append(_clone_part(part, [piece], f"/{k}"))
    parts[pi:pi + 1] = new
    return True


# ----------------------------------------------------------------------------- extend
def _extension_hits(s: Segment, at_end: bool, others: Iterable[Contour], max_len: float) -> Optional[Point]:
    """Nearest point where the prolongation of ``s`` beyond its end (or start) meets the other
    contours."""
    tip = s.end if at_end else s.start
    best, best_d = None, math.inf
    for o in others:
        for t in o.segments:
            for q in curve_intersections(s, t):
                if seg_distance(t, q) > 1e-6:
                    continue
                d = _beyond(s, at_end, q)
                if d is not None and 1e-7 < d < best_d and d <= max_len:
                    best, best_d = q, d
    return best


def _beyond(s: Segment, at_end: bool, q: Point) -> Optional[float]:
    """Distance travelled past the tip of ``s`` along its carrier to reach ``q`` (None if q lies
    behind the tip / on the segment itself)."""
    if isinstance(s, Line):
        dx, dy = s.end[0] - s.start[0], s.end[1] - s.start[1]
        L = math.hypot(dx, dy)
        if L < 1e-12:
            return None
        ux, uy = dx / L, dy / L
        if at_end:
            d = (q[0] - s.end[0]) * ux + (q[1] - s.end[1]) * uy
        else:
            d = -((q[0] - s.start[0]) * ux + (q[1] - s.start[1]) * uy)
        return d if d > 1e-9 else None
    c, r = s.center, s.radius
    a_tip = math.atan2((s.end if at_end else s.start)[1] - c[1], (s.end if at_end else s.start)[0] - c[0])
    a_q = math.atan2(q[1] - c[1], q[0] - c[0])
    forward = s.ccw if at_end else not s.ccw
    da = (a_q - a_tip) % (2 * math.pi) if forward else (a_tip - a_q) % (2 * math.pi)
    free = 2 * math.pi - abs(s.sweep())
    if da < 1e-9 or da > free - 1e-9:
        return None
    return da * r


def extend_at(parts: list[Part], p: Point, tol: float, max_len: float = 1e5) -> bool:
    """Extend (0x100beff0): prolong the open end of the contour under ``p`` that is nearer to
    the click until it meets another contour."""
    hit = nearest_contour(parts, p, tol)
    if hit is None:
        return False
    pi, ci, pos, _ = hit
    c = parts[pi].contours[ci]
    if c.closed or not c.segments:
        return False
    at_end = pos > c.length() / 2
    s = c.segments[-1] if at_end else c.segments[0]
    others = [o for q in parts for o in q.contours if o is not c]
    if len(c.segments) > 1:     # the contour's own other elements count too
        own = Contour(c.segments[:-1] if at_end else c.segments[1:], False)
        others.append(own)
    q = _extension_hits(s, at_end, others, max_len)
    if q is None:
        return False
    new = c.segments
    if at_end:
        new[-1] = _with_end(s, q)
    else:
        new[0] = _with_start(s, q)
    return True
