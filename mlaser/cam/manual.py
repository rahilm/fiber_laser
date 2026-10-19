"""Manual craft tools (lang ``mf87``..``mf97``, ``mf101``, ``mf103_1``, ``mf406``, ``mf475``,
``mf472``, ``mf604``): the geometry behind the Process-tab commands that act on one clicked
contour or on the selection.  UI in ``mlaser/ui/ext_craft.py``; evidence in docs/re/craft.md.

Conventions (shared with ``cam.toolpath``):

* ``Craft.extra['start_param']``: start position as a fraction 0..1 of the contour length
  (original contour +0x180, setter CADModule 0x10071f30; forced to 0 on open contours).
* ``Craft.micro_joints``: joint centres in mm of arc length from the contour's first
  segment, or ``(pos, len)`` pairs (original +0x118 fractions / +0x128 widths).
* ``Craft.extra['cool_points']``: cooling positions in mm (or points); original +0x15c.
* ``Craft.extra['overcut']``: signed overcut (+) / gap (-) in mm (original +0x198).

The original keeps these per contour; the rebuild keeps them per ``Part`` (one contour per
part after import).  For a part with several contours the per-contour value goes to
``Contour.extra['craft']`` (a full ``Craft``); ``contour_craft`` returns the craft that
applies to one contour.  (``cam.toolpath`` reads the override once the lead applies the
patch listed in docs/re/craft.md §9.)
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ..geom.curves import reverse_contour, signed_area
from ..model import Arc, Contour, Craft, Document, LeadParams, Line, Part, Point, Segment
from ..geom.contour import (contour_bbox, is_ccw, point_at, polyline, rotate_start, seg_point,
                       seg_tangent, sub_path, tangent_at)
from .microjoint import DEFAULT_JOINT_LEN, joint_intervals

__all__ = [
    "Hit", "project_on_segment", "nearest_on_contour", "hit_test", "contour_craft",
    "start_distance", "set_start_at", "toggle_joint", "joint_list", "add_cool_point",
    "toggle_cool_point", "place_cool_point", "snap_to_vertex", "split_margin", "explode_joints", "split_contour_at", "set_gap_seal",
    "GAP", "SEAL", "OVERCUT",
]

GAP, SEAL, OVERCUT = -1, 0, 1           # GraphParam 0xb230 modes (MainApp FUN_004b5ad0)


# --------------------------------------------------------------------------- projection
def project_on_segment(s: Segment, p: Point) -> tuple[float, Point, float]:
    """(local parameter u, nearest point, distance) of ``p`` on segment ``s`` (exact)."""
    if isinstance(s, Line):
        dx, dy = s.end[0] - s.start[0], s.end[1] - s.start[1]
        L2 = dx * dx + dy * dy
        u = 0.0 if L2 <= 0 else max(0.0, min(1.0, ((p[0] - s.start[0]) * dx + (p[1] - s.start[1]) * dy) / L2))
        q = (s.start[0] + dx * u, s.start[1] + dy * u)
        return u, q, math.dist(p, q)
    cx, cy = s.center
    r = s.radius
    sw = s.sweep()
    a0 = math.atan2(s.start[1] - cy, s.start[0] - cx)
    ap = math.atan2(p[1] - cy, p[0] - cx)
    d = ap - a0
    if sw > 0:
        d %= 2 * math.pi
    else:
        d = -((-d) % (2 * math.pi))
    u = d / sw if sw else 0.0
    if 0.0 <= u <= 1.0:
        q = (cx + r * math.cos(ap), cy + r * math.sin(ap))
        return u, q, math.dist(p, q)
    d0, d1 = math.dist(p, s.start), math.dist(p, s.end)
    return (0.0, s.start, d0) if d0 <= d1 else (1.0, s.end, d1)


def nearest_on_contour(c: Contour, p: Point) -> tuple[float, Point, float, int]:
    """(arc length from the contour start, nearest point, distance, segment index)."""
    best = (0.0, c.segments[0].start if c.segments else p, math.inf, -1)
    acc = 0.0
    for i, s in enumerate(c.segments):
        L = s.length()
        u, q, d = project_on_segment(s, p)
        if d < best[2]:
            best = (acc + u * L, q, d, i)
        acc += L
    return best


@dataclass
class Hit:
    part: int
    contour: int
    dist: float            # arc length along the contour (mm)
    point: Point           # nearest contour point
    distance: float        # distance of the click from the contour


def hit_test(doc: Document, p: Point, tol: float, parts: Optional[Iterable[int]] = None) -> Optional[Hit]:
    """Nearest contour within ``tol`` of ``p`` (the view's pick, CADModule hit tolerance in
    model units = pixels / zoom)."""
    best: Optional[Hit] = None
    idx = range(len(doc.parts)) if parts is None else parts
    for pi in idx:
        part = doc.parts[pi]
        for ci, c in enumerate(part.contours):
            if not c.segments:
                continue
            x0, y0, x1, y1 = contour_bbox(c)
            if p[0] < x0 - tol or p[0] > x1 + tol or p[1] < y0 - tol or p[1] > y1 + tol:
                continue
            s, q, d, _ = nearest_on_contour(c, p)
            if d <= tol and (best is None or d < best.distance):
                best = Hit(pi, ci, s, q, d)
    return best


# --------------------------------------------------------------------------- per-contour craft
def contour_craft(part: Part, ci: int, create: bool = False) -> Craft:
    """Craft that applies to contour ``ci`` of ``part``.

    Single-contour parts use ``part.craft``.  For several contours a per-contour override in
    ``Contour.extra['craft']`` is returned (created as a copy of the part craft when
    ``create``)."""
    if len(part.contours) <= 1:
        return part.craft
    c = part.contours[ci]
    ov = c.extra.get("craft") if isinstance(c.extra, dict) else None
    if isinstance(ov, Craft):
        return ov
    if not create:
        return part.craft
    ov = copy.deepcopy(part.craft)
    c.extra = dict(c.extra or {})
    c.extra["craft"] = ov
    return ov


def start_distance(c: Contour, craft: Craft) -> float:
    """Current start position in mm (``start_param`` fraction, 0 when unset)."""
    if not c.closed:
        return 0.0
    return float(craft.extra.get("start_param", 0.0) or 0.0) * c.length()


# --------------------------------------------------------------------------- start point
def set_start_at(part: Part, ci: int, dist: float) -> float:
    """Path start at arc length ``dist`` of contour ``ci`` (tool 6, setter 0x10071f30:
    fraction of the length, 0 for open contours).  Returns the stored fraction."""
    c = part.contours[ci]
    craft = contour_craft(part, ci, create=True)
    L = c.length()
    f = 0.0 if (not c.closed or L <= 0) else (dist % L) / L
    craft.extra["start_param"] = f
    return f


# --------------------------------------------------------------------------- micro-joints
def joint_list(craft: Craft) -> list[tuple[float, float]]:
    """``Craft.micro_joints`` normalised to (pos_mm, len_mm)."""
    jl = float(craft.extra.get("micro_joint_len", DEFAULT_JOINT_LEN))
    out = []
    for j in craft.micro_joints:
        if isinstance(j, (tuple, list)):
            out.append((float(j[0]), float(j[1])))
        else:
            out.append((float(j), jl))
    return out


def toggle_joint(craft: Craft, c: Contour, pos: float, length: float) -> str:
    """Manual micro-joint click (CADModule 0x10071940): a click inside an existing joint
    removes it; otherwise a joint of ``length`` centred at ``pos`` is added unless it would
    overlap another one.  Returns 'removed', 'added' or 'rejected'."""
    L = c.length()
    joints = joint_list(craft)
    for k, (jp, jlen) in enumerate(joints):
        if _arc_gap(jp, pos, L, c.closed) <= jlen / 2:
            del joints[k]
            craft.micro_joints = [list(j) for j in joints]
            return "removed"
    if length <= 0 or L <= 0:
        return "rejected"
    for jp, jlen in joints:
        if _arc_gap(jp, pos, L, c.closed) < (jlen + length) / 2:
            return "rejected"
    if not c.closed and (pos - length / 2 < 0 or pos + length / 2 > L):
        return "rejected"
    joints.append(((pos % L) if c.closed else pos, length))
    joints.sort()
    craft.micro_joints = [list(j) for j in joints]
    return "added"


def _arc_gap(a: float, b: float, L: float, closed: bool) -> float:
    d = abs(a - b)
    if closed and L > 0:
        d %= L
        d = min(d, L - d)
    return d


def explode_joints(c: Contour, craft: Craft) -> list[Contour]:
    """Micro-joint explode (mf103_1): the joint spans become real gaps, i.e. the contour is
    cut into open contours covering the laser-on parts only.  Returns [c] when there are no
    joints."""
    joints = joint_list(craft)
    L = c.length()
    if not joints or L <= 0:
        return [c]
    iv = joint_intervals(L, joints, closed=c.closed)
    if not iv:
        return [c]
    keep: list[tuple[float, float]] = []
    if c.closed:
        # on-spans between consecutive joints, wrapping across the start
        for k, (a, b) in enumerate(iv):
            na = iv[(k + 1) % len(iv)][0] + (L if k + 1 == len(iv) else 0.0)
            if na - b > 1e-9:
                keep.append((b, na))
    else:
        prev = 0.0
        for a, b in iv:
            if a - prev > 1e-9:
                keep.append((prev, a))
            prev = b
        if L - prev > 1e-9:
            keep.append((prev, L))
    out = []
    for a, b in keep:
        segs = sub_path(c, a, b)
        if segs:
            out.append(Contour(segs, closed=False))
    return out


# --------------------------------------------------------------------------- cooling points
def add_cool_point(craft: Craft, pos: float) -> None:
    pts = [p for p in craft.extra.get("cool_points", [])]
    pts.append(float(pos))
    craft.extra["cool_points"] = sorted(pts, key=lambda v: v if isinstance(v, (int, float)) else 0.0)


def snap_to_vertex(c: Contour, p: Point, tol: float) -> Optional[float]:
    """Arc length of a segment start/end inside the 2*tol box around ``p`` (CCoolPoint
    FUN_10057320: segment start first, then end), else None."""
    acc = 0.0
    for s in c.segments:
        L = s.length()
        for q, d in ((s.start, acc), (s.end, acc + L)):
            if abs(q[0] - p[0]) <= tol and abs(q[1] - p[1]) <= tol:
                return d
        acc += L
    return None


def place_cool_point(craft: Craft, c: Contour, pos: float, tol: float) -> str:
    """Manual cooling point (tool 15, FUN_10069fd0): every existing point closer than
    max(tol, 0.5) mm to ``pos`` is removed and ``pos`` is inserted (sorted).  Clicking an
    existing point therefore moves it.  Returns 'added' or 'moved'."""
    L = c.length()
    lim = max(tol, 0.5)
    pts = list(craft.extra.get("cool_points", []))
    keep = []
    for p in pts:
        d = nearest_on_contour(c, p)[0] if isinstance(p, (tuple, list)) else float(p)
        if _arc_gap(d, pos, L, False) >= lim:
            keep.append(p)
    craft.extra["cool_points"] = keep
    add_cool_point(craft, pos)
    return "moved" if len(keep) < len(pts) else "added"


def toggle_cool_point(craft: Craft, c: Contour, pos: float, tol: float) -> str:
    """Rebuild convenience (not in the original): remove a point within ``tol``, else add."""
    pts = list(craft.extra.get("cool_points", []))
    L = c.length()
    for k, p in enumerate(pts):
        d = nearest_on_contour(c, p)[0] if isinstance(p, (tuple, list)) else float(p)
        if _arc_gap(d, pos, L, c.closed) <= tol:
            del pts[k]
            craft.extra["cool_points"] = pts
            return "removed"
    add_cool_point(craft, pos)
    return "added"


# --------------------------------------------------------------------------- segmentation
def split_margin(length: float, tol: float) -> float:
    """Tool 12 margin (FUN_100b6bb0): m = max(3*tol, 0.2)/length, capped at 0.3 (fraction)."""
    if length <= 0:
        return 0.3
    return min(max(3.0 * tol, 0.2) / length, 0.3)


def split_contour_at(c: Contour, dist: float, tol: float = 0.0) -> list[Contour]:
    """Curve segmentation (mf475 / mf216, tool 12, FUN_100b6bb0 -> FUN_100752c0): the contour
    becomes the two open pieces [0, u] and [u, 1] (a closed contour is therefore also cut at
    its first segment's start).  Clicks within the margin of either end do nothing."""
    L = c.length()
    if L <= 0:
        return [c]
    m = split_margin(L, tol) if tol > 0 else 1e-9
    u = dist / L
    if not (m < u < 1 - m):
        return [c]
    a = sub_path(c, 0.0, dist)
    b = sub_path(c, dist, L)
    return [Contour(a, closed=False), Contour(b, closed=False)]


# --------------------------------------------------------------------------- gap / seal / overcut
def set_gap_seal(craft: Craft, c: Optional[Contour], mode: int, value: float) -> float:
    """Gap / seal / overcut (FUN_004b5ad0 -> CCAD slot 90): stores the signed value in
    ``Craft.extra['overcut']``: gap -> -value, seal -> 0, overcut -> +value.  Open contours
    keep only gaps (cam.md §4).  Returns the stored value."""
    v = 0.0
    if mode == GAP:
        v = -abs(value)
    elif mode == OVERCUT:
        v = abs(value)
        if c is not None and not c.closed:
            v = 0.0
    craft.extra["overcut"] = v
    return v


# --------------------------------------------------------------------------- manual lead (tool 5)
def manual_lead(part: Part, ci: int, q: Point, dist: float, depth: int = 1,
                invert: bool = False) -> Optional[LeadParams]:
    """Manual lead line (tool 5, FUN_100be7d0 -> FUN_10064760).  First click ``q``, second
    click on a closed contour at arc length ``dist``: the start moves to the clicked point and
    the lead-in becomes a straight line from ``q`` to it: type 1, length |S - q|, angle = the
    angle between the start tangent and S - q (may exceed 90 deg); |S - q| < 0.01 -> no lead.

    The original stores the side in the lead flag (+0x1f8 = angle <= 0); the rebuild's toolpath
    derives the side from depth / orientation (``leads.side_flag``), so the angle is stored
    signed for that side so ``leads.lead_in`` reproduces the line q -> S exactly."""
    from .leads import side_flag
    c = part.contours[ci]
    if not c.closed:
        return None
    set_start_at(part, ci, dist)
    craft = contour_craft(part, ci, create=True)
    S = point_at(c, dist % c.length())
    d = tangent_at(c, dist % c.length())
    v = (S[0] - q[0], S[1] - q[1])
    L = math.hypot(*v)
    lp = copy.deepcopy(craft.lead_in)
    if L < 0.01:
        lp.kind = 0
        craft.lead_in = lp
        return lp
    a = math.atan2(d[0] * v[1] - d[1] * v[0], d[0] * v[0] + d[1] * v[1])
    flag = side_flag(depth, is_ccw(c), invert)
    lp.kind = 1
    lp.length = L
    lp.angle_deg = math.degrees(-a if flag else a)
    craft.lead_in = lp
    return lp


def contour_depths(doc: Document) -> dict[tuple[int, int], int]:
    """Nesting depth (outermost = 1) of every contour, keyed (part, contour)."""
    from .ordering import build_tree
    keys, conts = [], []
    for pi, p in enumerate(doc.parts):
        for ci, c in enumerate(p.contours):
            if c.segments:
                keys.append((pi, ci))
                conts.append(c)
    return {n.key: n.depth for n in build_tree(conts, keys)}


# --------------------------------------------------------------------------- lines x contours
def _line_seg_hits(a: Point, b: Point, s: Segment) -> list[tuple[float, float]]:
    """Intersections of segment a-b with ``s`` as (t on a-b, u on s)."""
    out = []
    dx, dy = b[0] - a[0], b[1] - a[1]
    if isinstance(s, Line):
        ex, ey = s.end[0] - s.start[0], s.end[1] - s.start[1]
        den = dx * ey - dy * ex
        if abs(den) < 1e-15:
            return out
        wx, wy = s.start[0] - a[0], s.start[1] - a[1]
        t = (wx * ey - wy * ex) / den
        u = (wx * dy - wy * dx) / den
        if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
            out.append((t, min(max(u, 0.0), 1.0)))
        return out
    cx, cy = s.center
    r = s.radius
    fx, fy = a[0] - cx, a[1] - cy
    A = dx * dx + dy * dy
    B = 2 * (fx * dx + fy * dy)
    C = fx * fx + fy * fy - r * r
    disc = B * B - 4 * A * C
    if A <= 0 or disc < 0:
        return out
    sq = math.sqrt(disc)
    for t in ((-B - sq) / (2 * A), (-B + sq) / (2 * A)):
        if -1e-9 <= t <= 1 + 1e-9:
            p = (a[0] + dx * t, a[1] + dy * t)
            u, q, dd = project_on_segment(s, p)
            if dd < 1e-6:
                out.append((t, u))
    return out


def line_contour_hits(a: Point, b: Point, c: Contour) -> list[tuple[float, Point]]:
    """(arc length on c, point) of every crossing of segment a-b with contour c."""
    res, acc = [], 0.0
    for s in c.segments:
        L = s.length()
        for t, u in _line_seg_hits(a, b, s):
            res.append((acc + u * L, seg_point(s, u)))
        acc += L
    return res


def _nearest_hit(hits, p: Point):
    return min(hits, key=lambda h: math.dist(h[1], p)) if hits else None


def _piece(c: Contour, d0: float, d1: float) -> list[Segment]:
    """Path of closed contour c from arc length d0 forward to d1 (wrapping)."""
    L = c.length()
    if d1 < d0:
        d1 += L
    return sub_path(c, d0, d1)


def _rev(segs: list[Segment]) -> list[Segment]:
    return [s.reversed() for s in reversed(segs)]


# --------------------------------------------------------------------------- bridge (tool 11)
def bridge_lines(A: Point, B: Point, width: float):
    """The two cut lines of CBridge (FUN_1001a7f0): parallel to AB at +-h (h = width/2),
    extended by 2h beyond both points."""
    h = width / 2.0
    L = math.dist(A, B)
    if L < 1e-9:
        return None
    u = ((B[0] - A[0]) / L, (B[1] - A[1]) / L)
    n = (-u[1], u[0])
    L1 = ((A[0] + h * (n[0] - 2 * u[0]), A[1] + h * (n[1] - 2 * u[1])),
          (B[0] + h * (n[0] + 2 * u[0]), B[1] + h * (n[1] + 2 * u[1])))
    L2 = ((A[0] + h * (-n[0] - 2 * u[0]), A[1] + h * (-n[1] - 2 * u[1])),
          (B[0] + h * (-n[0] + 2 * u[0]), B[1] + h * (-n[1] + 2 * u[1])))
    return L1, L2


def _keep_long(c: Contour, p1: float, p2: float) -> Optional[tuple[list[Segment], bool]]:
    """Longer portion between the two cut parameters (d = (p2 - p1) mod 1 must be >= 0.6 or
    <= 0.4).  Returns (segments, runs_from_p1)."""
    L = c.length()
    d = ((p2 - p1) / L) % 1.0
    if 0.4 < d < 0.6:
        return None
    if d >= 0.6:
        return _piece(c, p1, p2), True      # p1 -> p2 is the long way
    return _piece(c, p2, p1), False         # p2 -> p1


def bridge_two(c1: Contour, A: Point, c2: Contour, B: Point, width: float) -> Optional[Contour]:
    """Bridge between two closed contours (FUN_1001a7f0): the short pieces between the cut
    lines are removed and the rest is joined by two straight edges into one closed contour."""
    lines = bridge_lines(A, B, width)
    if lines is None or not (c1.closed and c2.closed):
        return None
    (a1, b1), (a2, b2) = lines
    h11 = _nearest_hit(line_contour_hits(a1, b1, c1), A)
    h12 = _nearest_hit(line_contour_hits(a2, b2, c1), A)
    h21 = _nearest_hit(line_contour_hits(a1, b1, c2), B)
    h22 = _nearest_hit(line_contour_hits(a2, b2, c2), B)
    if None in (h11, h12, h21, h22):
        return None
    k1 = _keep_long(c1, h11[0], h12[0])
    k2 = _keep_long(c2, h21[0], h22[0])
    if k1 is None or k2 is None:
        return None
    s1, fwd1 = k1
    s2, fwd2 = k2
    if not fwd1:              # make piece 1 run L1 -> L2
        s1 = _rev(s1)
    if fwd2:                  # piece 2 must run L2 -> L1
        s2 = _rev(s2)
    if not s1 or not s2:
        return None
    segs = list(s1) + [Line(s1[-1].end, s2[0].start)] + list(s2) + [Line(s2[-1].end, s1[0].start)]
    out = Contour(segs, True)
    if signed_area(out) * signed_area(c1) < 0:
        out = reverse_contour(out)
    return out


def bridge_same(c: Contour, A: Point, B: Point, width: float) -> Optional[list[Contour]]:
    """Bridge with both points on one contour (FUN_1001af80): the strip between the two cut
    lines is cut out, leaving two closed contours, each closed by one bridge edge."""
    lines = bridge_lines(A, B, width)
    if lines is None or not c.closed:
        return None
    (a1, b1), (a2, b2) = lines
    hits = []
    for (pa, pb), side in (((a1, b1), 1), ((a2, b2), 2)):
        hs = line_contour_hits(pa, pb, c)
        for P, tag in ((A, "A"), (B, "B")):
            h = _nearest_hit(hs, P)
            if h is None:
                return None
            hits.append((h[0], side, tag, h[1]))
    L = c.length()
    out = []
    for side in (1, 2):
        ha = next(h for h in hits if h[1] == side and h[2] == "A")
        hb = next(h for h in hits if h[1] == side and h[2] == "B")
        others = [h[0] for h in hits if h[1] != side]
        # the portion between ha and hb that does not contain the other line's hits
        for d0, d1 in ((ha[0], hb[0]), (hb[0], ha[0])):
            span = (d1 - d0) % L
            if all(((o - d0) % L) > span for o in others):
                segs = _piece(c, d0, d1)
                if segs:
                    segs.append(Line(segs[-1].end, segs[0].start))
                    out.append(Contour(segs, True))
                break
    return out if len(out) == 2 else None


# --------------------------------------------------------------------------- lead check (mf89)
def _seg_poly(segs: Sequence[Segment], tol: float = 0.02) -> list[Point]:
    if not segs:
        return []
    return list(Contour(list(segs), closed=False).points(tol))


def _poly_cross(p: list[Point], q: list[Point]) -> list[tuple[float, Point]]:
    """Crossings of polyline p with polyline q as (arc length along p, point)."""
    out, acc = [], 0.0
    for a, b in zip(p, p[1:]):
        L = math.dist(a, b)
        for c, d in zip(q, q[1:]):
            for t, _u in _line_seg_hits(a, b, Line(c, d)):
                out.append((acc + t * L, (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)))
        acc += L
    return out


def lead_in_path(c: Contour, craft: Craft, depth: int, invert: bool = False) -> tuple[list[Point], Point]:
    """Lead-in polyline from the contour start point P outwards (P first), and P."""
    from .leads import lead_in, side_flag
    from .ordering import choose_start
    if "start_param" in craft.extra:
        s = float(craft.extra["start_param"]) * c.length()
    else:
        s = choose_start(c, 0, craft.lead_in.length)
    P = point_at(c, s)
    d = tangent_at(c, s)
    segs = lead_in(P, d, craft.lead_in, side_flag(depth, is_ccw(c), invert))
    pts = _seg_poly(segs)
    return list(reversed(pts)), P


def check_leads(doc: Document, invert: bool = False) -> list[tuple[int, int]]:
    """Check lead line (mf89, CCAD 0x100d1f70 -> 0x10020e50), all graphs, >= 2 needed.

    The lead-in of every closed contour, extended past its free end by 0.5 x lead length, is
    intersected with its own contour (crossings other than at the start point) and with the
    other contours; for other contours the distance counts x0.7 unless the lead is a line
    (type 1).  On a hit the lead-in is shortened to the crossing distance d (I: scaled by
    1/1.5 when the hit lies on the 0.5 x extension so the clearance is kept) and the error
    mark (+0x1f9, rebuild ``Craft.extra['lead_error']``) is set.  Returns the marked
    (part, contour) keys."""
    from ..geom.contour import contour_bbox as cbb
    marked: list[tuple[int, int]] = []
    if len(doc.parts) < 1 or sum(len(p.contours) for p in doc.parts) < 2:
        return marked
    depths = contour_depths(doc)
    polys = {}
    for pi, p in enumerate(doc.parts):
        for ci, c in enumerate(p.contours):
            if c.segments:
                pts = polyline(c)
                polys[(pi, ci)] = (pts + [pts[0]] if c.closed else pts, cbb(c))
    for (pi, ci), (own, _bb) in polys.items():
        part = doc.parts[pi]
        c = part.contours[ci]
        craft = contour_craft(part, ci)
        lp = craft.lead_in
        if not c.closed or lp.kind == 0 or lp.length < 0.01:
            continue
        path, P = lead_in_path(c, craft, depths.get((pi, ci), 1), invert)
        if len(path) < 2:
            continue
        a, b = path[-2], path[-1]
        v = (b[0] - a[0], b[1] - a[1])
        n = math.hypot(*v) or 1.0
        ext = path + [(b[0] + v[0] / n * 0.5 * lp.length, b[1] + v[1] / n * 0.5 * lp.length)]
        d = 10000.0
        for s, _q in _poly_cross(ext, own):
            if s > 1e-6:
                d = min(d, s)
        xs = [q[0] for q in ext]
        ys = [q[1] for q in ext]
        lb = (min(xs), min(ys), max(xs), max(ys))
        for key, (poly, bb) in polys.items():
            if key == (pi, ci) or bb[0] > lb[2] or bb[2] < lb[0] or bb[1] > lb[3] or bb[3] < lb[1]:
                continue
            for s, _q in _poly_cross(ext, poly):
                d = min(d, s if lp.kind == 1 else 0.7 * s)
        if d >= 10000.0:
            continue
        cr = contour_craft(part, ci, create=True)
        cr.lead_in = copy.deepcopy(cr.lead_in)
        new_len = d if d <= lp.length else d / 1.5
        cr.lead_in.length = max(0.0, min(lp.length, new_len))
        cr.extra["lead_error"] = True
        marked.append((pi, ci))
    return marked


def clear_lead_marks(doc: Document) -> int:
    """Clear error mark (mf91, 0x100adfe0): every contour, no undo in the original; the
    shortened lengths stay."""
    n = 0
    for p in doc.parts:
        crafts = [p.craft] + [c.extra["craft"] for c in p.contours
                              if isinstance(c.extra, dict) and isinstance(c.extra.get("craft"), Craft)]
        for cr in crafts:
            if cr.extra.pop("lead_error", None):
                n += 1
    return n


# --------------------------------------------------------------------------- replace circle (mf472)
def circle_of(c: Contour, tol: float = 0.05) -> Optional[tuple[float, float, float]]:
    """Circle test of 0x1009a4f0: closed, bbox |w - h| <= 0.05, centre = bbox centre,
    r = (w + h)/4; every vertex / sample with |d^2 - r^2| <= 0.2 r; arcs need radius and
    centre within 0.1.  Returns (cx, cy, r) or None (already a single full circle -> None)."""
    if not c.closed or not c.segments:
        return None
    if len(c.segments) == 1 and isinstance(c.segments[0], Arc) and c.segments[0].start == c.segments[0].end:
        return None
    x0, y0, x1, y1 = contour_bbox(c)
    w, h = x1 - x0, y1 - y0
    if abs(w - h) > tol or w <= 0:
        return None
    cx, cy, r = (x0 + x1) / 2, (y0 + y1) / 2, (w + h) / 4
    for s in c.segments:
        if isinstance(s, Arc):
            if abs(s.radius - r) > 0.1 or math.dist(s.center, (cx, cy)) > 0.1:
                return None
        for q in (s.start, s.end, seg_point(s, 0.5)):
            if abs((q[0] - cx) ** 2 + (q[1] - cy) ** 2 - r * r) > 0.2 * r:
                return None
    if len(c.segments) < 3 and not any(isinstance(s, Arc) for s in c.segments):
        return None
    return cx, cy, r


def replace_circles(doc: Document) -> int:
    """Replace circle (mf472, 0x100b8e00): over all graphs, every contour that passes
    ``circle_of`` becomes one CEditableCircle (a full-circle Arc) in its slot, selected.
    The new contour keeps only the layer of the craft (I: default craft)."""
    n = 0
    for p in doc.parts:
        for ci, c in enumerate(p.contours):
            r = circle_of(c)
            if r is None:
                continue
            cx, cy, rr = r
            s = (cx + rr, cy)
            ccw = is_ccw(c)
            p.contours[ci] = Contour([Arc(s, s, (cx, cy), ccw)], True)
            if len(p.contours) == 1:
                p.craft = Craft(layer=p.craft.layer)
            p.selected = True
            n += 1
    return n


# --------------------------------------------------------------------------- adjust start (mf604)
def adjust_starts(parts: Sequence[Part]) -> int:
    """Adjust Start Pt (mf604, 0x100c9b00 -> CUniformStartPos 0x10051070, corner 1): for every
    closed contour with >= 2 segments the segment list is rotated so that the segment start
    nearest (Manhattan) to the bbox lower-left corner + offset comes first.  The offset is
    (0, 0) for the first contour and then (chosen vertex - corner) of that first contour, so
    all later contours start at the same relative position.  Geometry is reordered; the
    start parameter is not touched."""
    off: Optional[Point] = None
    n = 0
    for p in parts:
        for ci, c in enumerate(p.contours):
            if not c.closed or len(c.segments) < 2:
                continue
            x0, y0, _x1, _y1 = contour_bbox(c)
            o = off or (0.0, 0.0)
            tx, ty = x0 + o[0], y0 + o[1]
            k = min(range(len(c.segments)),
                    key=lambda i: abs(c.segments[i].start[0] - tx) + abs(c.segments[i].start[1] - ty))
            if off is None:
                v = c.segments[k].start
                off = (v[0] - x0, v[1] - y0)
            if k:
                p.contours[ci] = Contour(c.segments[k:] + c.segments[:k], True, dict(c.extra or {}))
                n += 1
    return n
