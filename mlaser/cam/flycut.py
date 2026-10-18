"""Fly cut / scan cutting (lang ``mf112`` 飞行切割), port of the CADModule scan builders.

Evidence (CADModule.dll unless noted; see docs/cam.md section 11):

* Entry: CCADModule vtable slot 88 (0x100f30d0) -> ``FUN_100c80d0(type, ...)``. MainApp's
  worker ``FUN_00480520`` calls it with the GraphParam block (offsets in the global param
  struct): type (MainApp+0xf368), pd851 0xb2e0, pd707 0xb2d4, pd708 0xb2d8, pd715 0xb2e4,
  pd716 0xb2e8, pd718 0xb2f8, pd717 0xb2f0, pd719_1 0xb398, pd719_2 0xb3a0, pd719_3 0xb3a8.
* The ribbon handler (MainApp ~0x004b6d00) sets the type: cmd 0x34ef/0x34f1 -> 1 (circle),
  0x34f0 -> 2 (line / segment fly), 0x34f2 -> 3 (arc), 0x34f3 -> 4 (scan engraving), 0x34f4 -> 5.
* ``FUN_100c80d0`` collects the type-8 contours of the first selected layer (at least 2),
  creates a CGlyScan (0xe8 bytes, ctor 0x1009b570, +0xe0 = type) and dispatches:
  1 -> 0x100a7d30 (circles), 3 -> 0x1009f220 (concentric arcs), 5 -> 0x100a6580,
  2/4 -> 0x100a9d80 (lines / engraving).  The used contours become the scan's group
  elements and are removed from the drawing; the scan graph takes the first one's slot.

Circles (``circle_fly``, 0x100a7d30 -> 0x100a18e0 sort -> 0x100a1f80 path):

* A contour is a circle if it is one CEditableCircle, or fits a circle whose span is
  2*pi within 0.1 rad (0x1009f0f0 / 0x1009eb40).  A circle overlapping an already
  accepted circle (|c1 c2| < r1 + r2) is rejected and stays a normal contour.
* Sorted by pd707 (0 none, 1..4 CSSort band modes, 5 local optimal 0x100a04a0), then split
  into groups wherever consecutive centres are farther apart than pd708.  A group of one
  circle stays a plain circle; every other group becomes one scan path (+0x114 = 2).
* Each circle is cut as one full turn starting at angle theta_i with direction dir_i.
  First circle: theta = angle of the travel direction rotated by -90 deg, dir = +1, so the
  CCW tangent at the start points to the next circle.  Collinear runs of equal circles
  reuse that angle and direction.  Consecutive circles are joined by a cubic B-spline
  (5 control points, knots 0,0,0,0,.5,1,1,1,1; middle point pulled back by min(r/4, 2)).
* Laser switch fractions (0x100a1f80 tail, constants 0x1011fc00=6, 0x1011f0f0=10,
  0x1011d8f0=1.2, 0x1011da80=3): a = min(6 deg * r, 1.2), b = min(10 deg * r, 3);
  "on" at (s_i - a + b)/L (not for the first circle), "off" at (e_i + a + b)/L (not for the
  last circle).  The path starts with the beam on.

Lines (``line_fly``, 0x100a9d80 -> 0x1009cbe0 / 0x1009c6b0 / 0x100a8970 / 0x100a3f10):

* Only contours made solely of straight edges qualify; edges shorter than 0.5 mm are
  dropped (0x1009cbe0).  Edges are merged into collinear lines (0x1009c6b0): angle
  tolerance min(tol, 0.08*len), offset tolerance tol = pd716.  Gaps longer than
  1.1 * pd717 split a line.
* Ordering (0x100a8970 / 0x100a5c70): lines of one direction are traversed serpentine from
  the start corner pd715 (0 left-down, 1 right-down, 2 left-up, 3 right-up of the bbox);
  the next line is chained when its nearer end is within pd717.
* Path (0x100a3f10): edges in order; between edges a straight linker when they continue
  collinearly (direction within 0.005, lateral offset < 0.035*d), otherwise a cubic Bezier
  (k = min(d/2, 2), 4 when d > 30) or, for a U-turn, a 6-point cubic B-spline (knots
  0,0,0,0,.05,.95,1,1,1,1).  +0x114 = 3; odd glyphs (linkers) are laser off:
  a switch at the start and the end of each linker.  +0x354 = scan type.

Engraving (type 4) paths use the opposite parity (odd glyphs are the cuts and the path
starts with the beam off); ``scan_paths_from_chf`` reads them from .chf files.

Scan engraving (types 4 and 5, agent G phase 3): ``engrave_scan``, ``apply_engrave_scan``,
``parse_scan_gcode`` and ``gcode_scan_part`` (docs/craft.md §6).

Not reimplemented (U): type 3 concentric-arc scan, the link-collision split 0x100a7580, link
types 1/2/3 between non-collinear circles.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from ..model import Arc, Contour, Craft, Document, LayerParams, Line, Part, Point, Segment
from ..geom.contour import contour_bbox, point_in_contour, sub_seg
from ..toolpath.items import (CutArc, CutLine, Follow, LaserOff, LaserOn, LayerChange, Marker,
                    MotionItem, Pierce, Rapid)

__all__ = ["SCAN_CIRCLE", "SCAN_LINE", "SCAN_ARC", "SCAN_ENGRAVE", "CircleFlyParams",
           "LineFlyParams", "ScanPath", "detect_circle", "circle_fly", "line_fly",
           "split_by_switches", "switches_from_spans", "scan_motion_items", "make_scan_part",
           "apply_fly_cut", "scan_paths_from_chf", "scan_paths_of_part", "shift_switches",
           "EngraveScanParams", "engrave_scan", "apply_engrave_scan", "parse_scan_gcode",
           "gcode_scan_part", "SCAN_5"]

SCAN_CIRCLE, SCAN_LINE, SCAN_ARC, SCAN_ENGRAVE, SCAN_5 = 1, 2, 3, 4, 5

DEG = math.pi / 180.0


# --------------------------------------------------------------------------- params
@dataclass
class CircleFlyParams:
    """Circle fly cut (GraphParam block; lang keys in comments)."""
    sort_type: int = 0                  # pd707 GRP.CircleFlySortType (0 none .. 5 local optimal)
    max_fly_len: float = 80.0           # pd708 GRP.CircleFlyMaxLen: max centre distance in a group
    first_single_contour: bool = True   # pd851: circles inside one outer contour first
    collinear_tol: float = 0.01         # scan +0xd8 (I: IGraph closing tolerance, ctor 0.01)
    lag_deg: float = 6.0                # 0x1011fc00
    lag_max: float = 1.2                # 0x1011d8f0
    overlap_deg: float = 10.0           # 0x1011f0f0
    overlap_max: float = 3.0            # 0x1011da80
    velocity_ratio: float = 1.0         # pd290 FCP.FlycutCircleVelRatio (I: applied to arcs)


@dataclass
class LineFlyParams:
    """Line (segment) fly cut."""
    start_corner: int = 0               # pd715 GRP.LineFlyStartPos 0 LD, 1 RD, 2 LU, 3 RU
    collinear_tol: float = 1.0          # pd716 GRP.LineFlyCollineTol (mm)
    max_link_len: float = 80.0          # pd717 GRP.LineFlyMaxLinkLen (mm)
    max_fly_len: float = 40.0           # pd718 GRP.LineFlyMaxLen (passed, use U)
    min_edge_len: float = 0.5           # 0x1009cbe0 (DAT_1011d738)


# --------------------------------------------------------------------------- scan path
@dataclass
class ScanPath:
    """One continuous scan path (a CGlyScan "####Path" contour).

    ``switches`` are the +0x13c laser toggle positions as fractions of ``contour.length()``;
    the beam state at the start is ``initial_on`` and flips at every switch.
    """
    contour: Contour
    switches: list[float] = field(default_factory=list)
    initial_on: bool = True
    kind: int = SCAN_LINE
    pwm_mode: int = 3
    sources: list[int] = field(default_factory=list)   # indices of the consumed contours
    arc_feed_ratio: float = 1.0

    def length(self) -> float:
        return self.contour.length()

    def spans(self) -> list[tuple[Segment, bool]]:
        return split_by_switches(self.contour, self.initial_on, self.switches)

    def on_intervals(self) -> list[tuple[float, float]]:
        """Laser-on [a, b] arc-length intervals (mm)."""
        L = self.length()
        pts = [0.0] + [min(max(s, 0.0), 1.0) * L for s in self.switches] + [L]
        out, on = [], self.initial_on
        for a, b in zip(pts, pts[1:]):
            if on and b > a + 1e-12:
                out.append((a, b))
            on = not on
        return out


def split_by_switches(c: Contour, initial_on: bool, switches: Sequence[float]) -> list[tuple[Segment, bool]]:
    """Split a contour at the switch fractions into (segment, laser_on) pieces."""
    L = c.length()
    cuts = sorted(min(max(s, 0.0), 1.0) * L for s in switches)
    out: list[tuple[Segment, bool]] = []
    on, k, acc = initial_on, 0, 0.0
    for s in c.segments:
        sl = s.length()
        u0 = 0.0
        while k < len(cuts) and cuts[k] <= acc + sl + 1e-12:
            u = (cuts[k] - acc) / sl if sl > 0 else 0.0
            u = min(max(u, 0.0), 1.0)
            if u > u0 + 1e-9:
                out.append((sub_seg(s, u0, u), on))
                u0 = u
            on = not on
            k += 1
        if u0 < 1 - 1e-9:
            out.append((s if u0 == 0.0 else sub_seg(s, u0, 1.0), on))
        acc += sl
    return out


def switches_from_spans(spans: Sequence[tuple[Segment, bool]]) -> tuple[bool, list[float]]:
    """Inverse of ``split_by_switches``: (initial_on, toggle fractions)."""
    L = sum(s.length() for s, _ in spans) or 1.0
    if not spans:
        return True, []
    init = spans[0][1]
    cur, acc, sw = init, 0.0, []
    for s, on in spans:
        if on != cur:
            sw.append(acc / L)
            cur = on
        acc += s.length()
    return init, sw


def shift_switches(path: ScanPath, on_shift: float, off_shift: float) -> ScanPath:
    """Move every switch-on by ``on_shift`` mm and every switch-off by ``off_shift`` mm
    (negative = earlier).  Models the PWM time corrections pd513/pd514/pd515 once they are
    converted to distance (time * feed; I: MainApp passes them to CInterpMrg+0x30..0x40
    through slot 127 / 0x101020b0, the consumer was not located)."""
    L = path.length() or 1.0
    out, on = [], path.initial_on
    for s in path.switches:
        on = not on                     # state after this switch
        out.append(s + (on_shift if on else off_shift) / L)
    p = copy.copy(path)
    p.switches = sorted(min(max(x, 0.0), 1.0) for x in out)
    return p


# --------------------------------------------------------------------------- splines
def _bspline(ctrl: list[Point], knots: list[float], deg: int = 3, n: int = 16) -> list[Point]:
    """Sample a clamped B-spline (de Boor) at n+1 parameters."""
    def point(t: float) -> Point:
        m = len(ctrl)
        k = deg
        while k < m - 1 and knots[k + 1] <= t:
            k += 1
        d = [list(ctrl[j + k - deg]) for j in range(deg + 1)]
        for r in range(1, deg + 1):
            for j in range(deg, r - 1, -1):
                i = j + k - deg
                den = knots[i + deg - r + 1] - knots[i]
                a = 0.0 if den == 0 else (t - knots[i]) / den
                d[j] = [(1 - a) * d[j - 1][0] + a * d[j][0], (1 - a) * d[j - 1][1] + a * d[j][1]]
        return (d[deg][0], d[deg][1])
    t0, t1 = knots[deg], knots[len(ctrl)]
    pts = [point(t0 + (t1 - t0) * i / n) for i in range(n)]
    pts.append(tuple(ctrl[-1]))
    return pts


def _spline_lines(ctrl: list[Point], knots: list[float]) -> list[Segment]:
    est = sum(math.dist(a, b) for a, b in zip(ctrl, ctrl[1:]))
    n = max(8, min(64, int(math.ceil(est / 0.5))))
    pts = _bspline(ctrl, knots, 3, n)
    pts[0] = tuple(ctrl[0])
    return [Line(a, b) for a, b in zip(pts, pts[1:]) if math.dist(a, b) > 1e-12]


def _add(p, v, k=1.0):
    return (p[0] + v[0] * k, p[1] + v[1] * k)


def _unit(v):
    n = math.hypot(*v)
    return (v[0] / n, v[1] / n) if n > 1e-12 else (1.0, 0.0)


# --------------------------------------------------------------------------- circles
@dataclass
class _Circle:
    cx: float
    cy: float
    r: float
    index: int
    theta: float = 0.0
    dir: int = 1

    @property
    def c(self) -> Point:
        return (self.cx, self.cy)

    def at(self, a: float) -> Point:
        return (self.cx + self.r * math.cos(a), self.cy + self.r * math.sin(a))

    def tangent(self, a: float) -> Point:
        return (-math.sin(a) * self.dir, math.cos(a) * self.dir)


def detect_circle(c: Contour, span_tol: float = 0.1) -> Optional[tuple[float, float, float]]:
    """(cx, cy, r) if the contour is a full circle (0x1009f0f0): all arcs on one centre and
    radius whose sweeps add up to 2*pi within ``span_tol`` (0x1011f0b0 = 0.1)."""
    if not c.segments or not all(isinstance(s, Arc) for s in c.segments):
        return None
    c0, r0 = c.segments[0].center, c.segments[0].radius
    tot = 0.0
    for s in c.segments:
        if math.dist(s.center, c0) > 1e-3 or abs(s.radius - r0) > 1e-3:
            return None
        tot += abs(s.sweep())
    if abs(tot - 2 * math.pi) > span_tol or r0 <= 0:
        return None
    return (c0[0], c0[1], r0)


def _sort_circles(cs: list[_Circle], mode: int) -> list[_Circle]:
    if mode <= 0 or len(cs) < 2:
        return cs
    from .ordering import Node, sort_level   # CSSort band modes 1..4 + local optimal 5
    nodes = []
    for k in cs:
        n = Node(Contour([Arc(k.at(0), k.at(0), k.c, True)]), key=k,
                 bbox=(k.cx - k.r, k.cy - k.r, k.cx + k.r, k.cy + k.r))
        n.entry = n.exit = k.c
        nodes.append(n)
    start = min((k.c for k in cs), key=lambda p: (p[0], p[1])) if mode == 5 else nodes[0].entry
    return [n.key for n in sort_level(nodes, mode, start, False)]


def _circle_group_path(group: list[_Circle], prm: CircleFlyParams) -> ScanPath:
    """Path through one group of >= 2 circles (0x100a1f80)."""
    n = len(group)
    # travel directions u_i (from previous centre) -------------------------------------
    u = [None] * n
    for i in range(1, n):
        u[i] = _unit((group[i].cx - group[i - 1].cx, group[i].cy - group[i - 1].cy))
    # collinear equal-radius runs: centres on one line and same radius --------------
    in_run = [False] * n
    for i in range(1, n):
        if i >= 2 and u[i - 1] is not None:
            cross = abs(u[i - 1][0] * u[i][1] - u[i - 1][1] * u[i][0]) * math.dist(group[i].c, group[i - 1].c)
            if cross <= max(prm.collinear_tol, 1e-9) and u[i - 1][0] * u[i][0] + u[i - 1][1] * u[i][1] > 0 \
                    and abs(group[i].r - group[i - 1].r) <= prm.collinear_tol:
                in_run[i] = True
        elif i == 1 and abs(group[1].r - group[0].r) <= prm.collinear_tol:
            in_run[i] = True
    # start angles ---------------------------------------------------------------------
    g0 = group[0]
    g0.dir = 1
    g0.theta = math.atan2(u[1][1], u[1][0]) - math.pi / 2   # tangent (CCW) along u_1
    for i in range(1, n):
        k = group[i]
        k.dir = group[i - 1].dir
        if in_run[i]:
            k.theta = group[i - 1].theta
        else:
            k.theta = math.atan2(u[i][1], u[i][0]) - k.dir * math.pi / 2
    # geometry -------------------------------------------------------------------------
    segs: list[Segment] = []
    arc_ranges: list[tuple[float, float]] = []
    acc = 0.0
    for i, k in enumerate(group):
        if i > 0:
            p = group[i - 1]
            P4 = k.at(k.theta)
            # overtravel (beam off) on the previous circle until its tangent points to the
            # next entry, so the linker never crosses the circle just cut (I: stands in for
            # the arc link types 1/2 of FUN_1009db40, not decoded)
            phi = 0.0
            for step in range(0, 72):
                ph = step * 5 * DEG
                q = p.at(p.theta + p.dir * ph)
                if math.dist(q, P4) < 1e-9:
                    break
                w = _unit((P4[0] - q[0], P4[1] - q[1]))
                t = p.tangent(p.theta + p.dir * ph)
                if t[0] * w[0] + t[1] * w[1] >= math.cos(30 * DEG):
                    phi = ph
                    break
            ex = p.theta + p.dir * phi
            if phi > 1e-9:
                P0s, P0e = p.at(p.theta), p.at(ex)
                over = Arc(P0s, P0e, p.c, p.dir > 0)
                segs.append(over)
                acc += over.length()
            P0 = p.at(ex)
            d = math.dist(P0, P4)
            t0, t1 = p.tangent(ex), k.tangent(k.theta)
            kk = d / 3.0                                   # I: 0x100a1f80 uses r/sqrt(..) (U)
            pull = min(0.25 * k.r, 2.0)                    # 0x1011f0c0 = 0.25, 0x1011d6c0 = 2
            mid = ((P0[0] + P4[0]) / 2, (P0[1] + P4[1]) / 2)
            radial = (math.cos(k.theta), math.sin(k.theta))
            ctrl = [P0, _add(P0, t0, kk), _add(mid, radial, -pull), _add(P4, t1, -kk), P4]
            link = _spline_lines(ctrl, [0, 0, 0, 0, 0.5, 1, 1, 1, 1])
            segs += link
            acc += sum(s.length() for s in link)
        start = k.at(k.theta)
        segs.append(Arc(start, start, k.c, k.dir > 0))
        arc_ranges.append((acc, acc + 2 * math.pi * k.r))
        acc += 2 * math.pi * k.r
    contour = Contour(segs, closed=False)
    L = contour.length()
    switches = []
    for i, (k, (s0, s1)) in enumerate(zip(group, arc_ranges)):
        a = min(prm.lag_deg * DEG * k.r, prm.lag_max)
        b = min(prm.overlap_deg * DEG * k.r, prm.overlap_max)
        if i > 0:
            switches.append((s0 - a + b) / L)
        if i < n - 1:
            switches.append((s1 + a + b) / L)
    switches = sorted(min(max(s, 0.0), 1.0) for s in switches)
    return ScanPath(contour, switches, True, SCAN_CIRCLE, 2, [k.index for k in group],
                    prm.velocity_ratio)


def circle_fly(contours: Sequence[Contour], prm: Optional[CircleFlyParams] = None,
               parents: Optional[Sequence[Contour]] = None) -> tuple[list[ScanPath], list[int]]:
    """Circle fly cut.  Returns (scan paths, indices of the contours they consume).

    ``parents``: optional outer contours; with pd851 the circles inside each parent are
    flown as separate groups first (FUN_100c80d0, type 1 pre-pass)."""
    prm = prm or CircleFlyParams()
    acc: list[_Circle] = []
    for i, c in enumerate(contours):
        k = detect_circle(c)
        if k is None:
            continue
        if any(math.hypot(k[0] - o.cx, k[1] - o.cy) < o.r + k[2] for o in acc):
            continue                                     # overlapping: rejected (0x100a7d30)
        acc.append(_Circle(k[0], k[1], k[2], i))
    if len(acc) < 2:
        return [], []
    buckets: list[list[_Circle]] = []
    if prm.first_single_contour and parents:
        polys = [list(p.points(0.05)) for p in parents]
        rest = list(acc)
        for p, poly in zip(parents, polys):
            inside = [k for k in rest if point_in_contour(k.c, p, poly)]
            if inside:
                buckets.append(inside)
                rest = [k for k in rest if k not in inside]
        if rest:
            buckets.append(rest)
    else:
        buckets = [acc]
    paths, used = [], []
    for b in buckets:
        b = _sort_circles(b, prm.sort_type)
        groups, cur = [], [b[0]]
        for k in b[1:]:
            if math.dist(k.c, cur[-1].c) > prm.max_fly_len:
                groups.append(cur)
                cur = [k]
            else:
                cur.append(k)
        groups.append(cur)
        for g in groups:
            if len(g) >= 2:
                paths.append(_circle_group_path([copy.copy(k) for k in g], prm))
                used += [k.index for k in g]
    return paths, sorted(used)


# --------------------------------------------------------------------------- lines
@dataclass
class _Edge:
    a: Point
    b: Point
    src: int


@dataclass
class _LineGroup:
    d: Point          # unit direction
    o: Point          # origin
    edges: list = field(default_factory=list)   # (t0, t1, src) intervals along d

    def offset(self) -> float:
        return -self.d[1] * self.o[0] + self.d[0] * self.o[1]


def _canon(d: Point) -> Point:
    """Direction with angle in (-45 deg, 135 deg] (0x1009c090 flips (-0.707, 0.707)-ish)."""
    a = math.atan2(d[1], d[0])
    if a <= -math.pi / 4 - 1e-9 or a > 3 * math.pi / 4 + 1e-9:
        return (-d[0], -d[1])
    return d


def _edges_of(c: Contour, idx: int, min_len: float) -> Optional[list[_Edge]]:
    if not c.segments or any(not isinstance(s, Line) for s in c.segments):
        return None                                       # arcs: contour not flown (0x1009cbe0)
    return [_Edge(s.start, s.end, idx) for s in c.segments if s.length() >= min_len]


def _merge_lines(edges: list[_Edge], tol: float) -> list[_LineGroup]:
    lines: list[_LineGroup] = []
    for e in edges:
        v = (e.b[0] - e.a[0], e.b[1] - e.a[1])
        L = math.hypot(*v)
        dv = (v[0] / L, v[1] / L)
        placed = None
        for g in reversed(lines):
            ang_tol = min(tol, 0.08 * L)
            if abs(g.d[0] * v[1] - g.d[1] * v[0]) > ang_tol:
                continue
            if abs((e.a[1] - g.o[1]) * g.d[0] - (e.a[0] - g.o[0]) * g.d[1]) > tol:
                continue
            placed = g
            break
        if placed is None:
            placed = _LineGroup(_canon(dv), e.a)
            lines.append(placed)
        g = placed
        t0 = (e.a[0] - g.o[0]) * g.d[0] + (e.a[1] - g.o[1]) * g.d[1]
        t1 = (e.b[0] - g.o[0]) * g.d[0] + (e.b[1] - g.o[1]) * g.d[1]
        g.edges.append((min(t0, t1), max(t0, t1), e.src))
    for g in lines:                                       # sort + merge overlaps (0x1009b030, I)
        g.edges.sort()
        merged = []
        for t0, t1, s in g.edges:
            if merged and t0 <= merged[-1][1] + 1e-9:
                merged[-1] = (merged[-1][0], max(merged[-1][1], t1), merged[-1][2])
            else:
                merged.append((t0, t1, s))
        g.edges = merged
    return lines


def _split_gaps(lines: list[_LineGroup], max_gap: float) -> list[_LineGroup]:
    out = []
    for g in lines:
        cur = _LineGroup(g.d, g.o, [g.edges[0]])
        for e in g.edges[1:]:
            if e[0] - cur.edges[-1][1] > max_gap:
                out.append(cur)
                cur = _LineGroup(g.d, g.o, [e])
            else:
                cur.edges.append(e)
        out.append(cur)
    return out


def _pt(g: _LineGroup, t: float) -> Point:
    return (g.o[0] + g.d[0] * t, g.o[1] + g.d[1] * t)


def _line_ends(g: _LineGroup, rev: bool) -> tuple[Point, Point]:
    a, b = _pt(g, g.edges[0][0]), _pt(g, g.edges[-1][1])
    return (b, a) if rev else (a, b)


def _line_pieces(g: _LineGroup, rev: bool) -> list[tuple[Point, Point]]:
    pcs = [(_pt(g, t0), _pt(g, t1)) for t0, t1, _ in g.edges]
    if rev:
        pcs = [(b, a) for a, b in reversed(pcs)]
    return pcs


def _order_lines(lines: list[_LineGroup], prm: LineFlyParams, bbox) -> list[list[tuple[_LineGroup, bool]]]:
    """Serpentine chains (0x100a8970): returns a list of chains of (line, reversed)."""
    x = bbox[0] if prm.start_corner % 2 == 0 else bbox[2]
    y = bbox[1] if prm.start_corner < 2 else bbox[3]
    cur = (x, y)
    pool = list(lines)
    chains = []
    while pool:
        best = None
        for i, g in enumerate(pool):
            for rev in (False, True):
                d = math.dist(cur, _line_ends(g, rev)[0])
                if best is None or d < best[0] - 1e-9:
                    best = (d, i, rev)
        _, i, rev = best
        g = pool.pop(i)
        chain = [(g, rev)]
        cur = _line_ends(g, rev)[1]
        while True:                                       # same direction family, serpentine
            fam = [(j, h) for j, h in enumerate(pool)
                   if abs(h.d[0] - g.d[0]) <= 0.01 and abs(h.d[1] - g.d[1]) <= 0.01]
            cand = None
            for j, h in fam:
                for rv in (False, True):
                    d = math.dist(cur, _line_ends(h, rv)[0])
                    if d <= prm.max_link_len and (cand is None or d < cand[0] - 1e-9):
                        cand = (d, j, rv)
            if cand is None:
                break
            _, j, rv = cand
            h = pool.pop(j)
            chain.append((h, rv))
            cur = _line_ends(h, rv)[1]
        chains.append(chain)
    # merge consecutive chains whose link is short enough (0x100a8970 tail, I: angle rule omitted)
    merged = [chains[0]]
    for ch in chains[1:]:
        a = _line_ends(*merged[-1][-1])[1]
        b = _line_ends(*ch[0])[0]
        if math.dist(a, b) <= prm.max_link_len:
            merged[-1] = merged[-1] + ch
        else:
            merged.append(ch)
    return merged


def _linker(P0: Point, t0: Point, P3: Point, t1: Point) -> tuple[list[Segment], bool]:
    """Linker between two edges (0x100a3f10).  Returns (segments, straight)."""
    d = math.dist(P0, P3)
    if d < 1e-9:
        return [], True
    dd = 2.0 if d < 0.5 else d
    lat = abs((P3[1] - P0[1]) * t1[0] - (P3[0] - P0[0]) * t1[1])
    if abs(t0[0] - t1[0]) <= 0.005 and abs(t0[1] - t1[1]) <= 0.005 and lat < dd * 0.035:
        return [Line(P0, P3)], True
    dot = t0[0] * t1[0] + t0[1] * t1[1]
    cr = abs(t1[0] * t0[1] - t1[1] * t0[0])
    if dot >= 0.0 or cr >= 0.14:
        k = 4.0 if dd > 30.0 else min(0.5 * dd, 2.0)
        ctrl = [P0, _add(P0, t0, k), _add(P3, t1, -k), P3]
        return _spline_lines(ctrl, [0, 0, 0, 0, 1, 1, 1, 1]), False
    h = min(dd, 2.0)
    b = min(0.4 * h, 2.0)
    a = 0.5 * b
    D = (P3[0] - P0[0], P3[1] - P0[1])
    e = abs(t1[0] * D[0] + t1[1] * D[1]) - abs(t1[0] * D[1] - t1[1] * D[0])
    if e > 0.0 and D[0] * t0[0] + D[1] * t0[1] <= 0.0:
        a += e
        b += e
    ctrl = [P0, _add(P0, t0, a), _add(P0, t0, a + h), _add(P3, t1, -(b + h)), _add(P3, t1, -b), P3]
    return _spline_lines(ctrl, [0, 0, 0, 0, 0.05, 0.95, 1, 1, 1, 1]), False


def _chain_path(chain: list[tuple[_LineGroup, bool]], sources: list[int]) -> ScanPath:
    pieces: list[tuple[Point, Point]] = []
    for g, rev in chain:
        pieces += _line_pieces(g, rev)
    spans: list[tuple[Segment, bool]] = []
    for i, (a, b) in enumerate(pieces):
        if i > 0:
            pa, pb = pieces[i - 1]
            t0, t1 = _unit((pb[0] - pa[0], pb[1] - pa[1])), _unit((b[0] - a[0], b[1] - a[1]))
            link, _ = _linker(pb, t0, a, t1)
            spans += [(s, False) for s in link]
        spans.append((Line(a, b), True))
    init, sw = switches_from_spans(spans)
    return ScanPath(Contour([s for s, _ in spans], closed=False), sw, init, SCAN_LINE, 3, sources)


def line_fly(contours: Sequence[Contour], prm: Optional[LineFlyParams] = None) -> tuple[list[ScanPath], list[int]]:
    """Line fly cut of straight-edged contours (rectangles, grids).

    Returns (scan paths, indices of the consumed contours)."""
    prm = prm or LineFlyParams()
    edges, used = [], []
    for i, c in enumerate(contours):
        es = _edges_of(c, i, prm.min_edge_len)
        if es:
            edges += es
            used.append(i)
    if len(used) < 1 or not edges:
        return [], []
    lines = _split_gaps(_merge_lines(edges, prm.collinear_tol), 1.1 * prm.max_link_len)
    xs = [p[0] for e in edges for p in (e.a, e.b)]
    ys = [p[1] for e in edges for p in (e.a, e.b)]
    chains = _order_lines(lines, prm, (min(xs), min(ys), max(xs), max(ys)))
    paths = []
    for ch in chains:
        srcs = sorted({s for g, _ in ch for _, _, s in g.edges})
        paths.append(_chain_path(ch, srcs))
    return paths, used


# --------------------------------------------------------------------------- motion items
def _move(seg: Segment, on: bool, feed: float, layer: int) -> MotionItem:
    if isinstance(seg, Line):
        return CutLine(seg.start, seg.end, feed, layer, on)
    return CutArc(seg.start, seg.end, seg.center, seg.ccw, feed, layer, on)


def scan_motion_items(path: ScanPath, feed: float, layer: int = 1, pos: Optional[Point] = None,
                      pierce_time_s: float = 0.0, stages: Optional[list] = None,
                      part: int = -1, index: int = -1, layer_change: bool = True) -> list[MotionItem]:
    """One scan path as a MotionItem block, consistent with ``build_toolpath``::

        Marker, Rapid, [LayerChange], Follow(True), [Pierce], LaserOn,
        CutLine/CutArc (laser=False on switched-off spans), LaserOff, Follow(False)

    The head moves continuously; off spans carry ``laser=False`` exactly like micro-joint
    gaps (per-sample duty 0).  Arcs get ``feed * path.arc_feed_ratio`` (pd290, I).  If the
    path starts with the beam off (engraving), no pierce is emitted (I)."""
    items: list[MotionItem] = [Marker("scan", part, index, {"kind": path.kind})]
    spans = path.spans()
    if not spans:
        return []
    start = spans[0][0].start
    if pos is not None and math.dist(pos, start) > 1e-9:
        items.append(Rapid(pos, start))
    if layer_change:
        items.append(LayerChange(layer))
    items.append(Follow(True))
    if path.initial_on:
        items.append(Pierce(start, layer, pierce_time_s, list(stages or [])))
    items.append(LaserOn(layer))
    for seg, on in spans:
        f = feed * path.arc_feed_ratio if isinstance(seg, Arc) else feed
        items.append(_move(seg, on, f, layer))
    items.append(LaserOff())
    items.append(Follow(False))
    return items


# --------------------------------------------------------------------------- document level
def make_scan_part(paths: list[ScanPath], sources: list[Contour], craft: Optional[Craft] = None,
                   kind: int = SCAN_LINE) -> Part:
    """Wrap scan paths as a Part that saves as a CGlyScan (graph type 11).

    ``Part.contours`` keeps the source contours (the scan's group elements), so a toolpath
    that ignores ``extra['scan_paths']`` still cuts every contour conventionally."""
    from ..formats import chf
    craft = copy.deepcopy(craft) if craft is not None else Craft()
    part = Part(contours=[copy.deepcopy(c) for c in sources], craft=craft, name="scan")
    elems = [chf.chf_from_contour(c) for c in part.contours]
    for e in elems:
        chf._apply_craft(e, craft)
    grp = chf.ChfGroup(elems=elems, layer=craft.layer)
    chf._refresh_group(grp)
    grp.length = sum(e.length for e in elems)
    cpaths = []
    for p in paths:
        cc = chf.chf_from_contour(p.contour)
        cc.layer = craft.layer
        cc.pwm_mode = p.pwm_mode
        cc.pwm_pairs = []
        cc.pwm_switch = list(p.switches)
        cc.seg_kind = p.kind
        cpaths.append(cc)
    scan = chf.ChfScan(group=grp, paths=cpaths, e0=kind)
    part.extra["chf_type"] = chf.G_SCAN
    part.extra["chf"] = scan
    part.extra["_chf_snapshot"] = {"craft": chf._craft_key(part.craft),
                                   "segs": [copy.deepcopy(c.segments) for c in part.contours]}
    part.extra["scan_paths"] = list(paths)
    part.extra["scan_kind"] = kind
    return part


def scan_paths_from_chf(scan, layer: int = 1) -> list[ScanPath]:
    """ScanPaths of a loaded ChfScan record (``Part.extra['chf']``).  Engraving paths
    (+0x354 = 4) start with the beam off; the others start on (0x100a3f10 parity)."""
    from ..formats import chf
    out = []
    for c in getattr(scan, "paths", []):
        segs, bounds, acc = [], [0.0], 0.0
        for gl in c.glys:
            gs = chf.gly_segments(gl)
            segs += gs
            acc += sum(s.length() for s in gs)
            bounds.append(acc)
        ct = Contour(segs, closed=False, extra={"chf": c})
        kind = c.seg_kind if c.seg_kind in (1, 2, 3, 4, 5) else getattr(scan, "e0", SCAN_LINE)
        sw = list(c.pwm_switch)
        if kind in (SCAN_LINE, SCAN_ENGRAVE) and acc > 0:
            sw = _snap_switches(sw, c.length or acc, bounds)
        out.append(ScanPath(ct, sw, kind != SCAN_ENGRAVE, kind, c.pwm_mode))
    return out


def _snap_switches(sw: list[float], stored_len: float, bounds: list[float]) -> list[float]:
    """Line/engraving switches sit on glyph boundaries (0x100a3f10).  Snap each stored fraction
    to the nearest boundary while tracking the accumulated offset.  Since chf.gly_segments
    flattens spline glyphs exactly like the original (D-15) the glyph lengths agree to ~1e-9 mm,
    so only rounding is absorbed (tolerance 1e-3 mm; it was 1 mm while the lengths drifted)."""
    total = bounds[-1]
    off, j, out = 0.0, 0, []
    for s in sw:
        x = s * stored_len + off
        while j + 1 < len(bounds) and bounds[j + 1] <= x:
            j += 1
        k = j if j + 1 >= len(bounds) or x - bounds[j] <= bounds[j + 1] - x else j + 1
        if abs(bounds[k] - x) <= 1e-3:
            off = bounds[k] - s * stored_len
            x = bounds[k]
        out.append(min(max(x / total, 0.0), 1.0))
    return out


def scan_paths_of_part(part: Part) -> list[ScanPath]:
    """Scan paths attached to a part (built here or loaded from .chf); [] for normal parts."""
    if part.extra.get("scan_paths"):
        return list(part.extra["scan_paths"])
    g = part.extra.get("chf")
    if part.extra.get("chf_type") == 11 and g is not None and getattr(g, "paths", None):
        paths = scan_paths_from_chf(g, part.craft.layer)
        part.extra["scan_paths"] = paths
        return list(paths)
    return []


def apply_fly_cut(doc: Document, kind: int = SCAN_CIRCLE, params=None,
                  selected_only: bool = True) -> Optional[Part]:
    """Replace the qualifying contours of the (selected) parts by one scan Part
    (FUN_100c80d0: the used graphs are removed and the scan takes the first one's slot).
    Only parts on the first candidate's layer are considered.  Returns the new part."""
    cand = [i for i, p in enumerate(doc.parts) if (p.selected or not selected_only) and p.contours
            and "scan_paths" not in p.extra]
    if not cand:
        return None
    layer = doc.parts[cand[0]].craft.layer
    cand = [i for i in cand if doc.parts[i].craft.layer == layer]
    flat, owner = [], []
    for i in cand:
        for c in doc.parts[i].contours:
            flat.append(c)
            owner.append(i)
    if len(flat) < 2 and kind == SCAN_CIRCLE:
        return None
    if kind == SCAN_CIRCLE:
        prm = params or CircleFlyParams()
        parents = [c for c in flat if detect_circle(c) is None and c.closed]
        paths, used = circle_fly(flat, prm, parents)
    elif kind == SCAN_LINE:
        paths, used = line_fly(flat, params or LineFlyParams())
    else:
        raise NotImplementedError(f"scan type {kind} (U)")
    if not paths:
        return None
    used_set = set(used)
    used_parts = sorted({owner[k] for k in used})
    # parts whose contours are all consumed go away; others keep their remaining contours
    srcs = [flat[k] for k in sorted(used_set)]
    remap = {k: n for n, k in enumerate(sorted(used_set))}
    for p in paths:
        p.sources = [remap[s] for s in p.sources if s in remap]
    new = make_scan_part(paths, srcs, doc.parts[used_parts[0]].craft, kind)
    slot = used_parts[0]
    keep: list[Part] = []
    for i, p in enumerate(doc.parts):
        if i == slot:
            keep.append(new)
        if i in used_parts:
            rest = [c for c in p.contours if not any(c is flat[k] for k in used_set)]
            if rest:
                q = copy.copy(p)
                q.contours = rest
                q.extra = {k: v for k, v in p.extra.items() if not k.startswith("_chf") and k != "chf"}
                keep.append(q)
        else:
            keep.append(p)
    doc.parts[:] = keep
    return new


# --------------------------------------------------------------------------- scan engraving (types 4 / 5)
@dataclass
class EngraveScanParams:
    """Scan engraving dialog (MainApp FUN_004310c0 / OnInit FUN_00435ff0; agent G, docs/craft.md).

    Mode 0 (mf112_5, type 4): GRP scanSideLineLength pd719_3 (0xb3a8, default 5 mm) and
    scanDirection pd719_4 (0xb3b0, default 0); the worker uses start corner 0 (LD) for
    direction 0, else 2 (LU).  Mode 2 (G-code scan, type 5): GCodeScanSideLineLength
    (0xb340, 5 mm) and GCodeScanDirection (0xb348, 0); the G-code loader orders rows
    bottom-up / left->right for direction 0, else top-down / right->left (dir code 3).
    minHorizontalLineGap (0xb398) and minScanLineLength (0xb3a0) are forced to 0.01."""
    side_len: float = 5.0
    direction: int = 0
    min_gap: float = 0.01
    min_len: float = 0.01
    collinear_tol: float = 0.01


def _flatten_lines(c: Contour, tol: float = 0.01) -> list[tuple[Point, Point]]:
    if all(isinstance(s, Line) for s in c.segments):
        return [(s.start, s.end) for s in c.segments]
    pts = list(c.points(tol))
    return list(zip(pts, pts[1:]))


def engrave_scan(contours: Sequence[Contour], prm: Optional[EngraveScanParams] = None,
                 kind: int = SCAN_ENGRAVE) -> tuple[list[ScanPath], list[int]]:
    """Raster scan path over the burn edges of ``contours`` (types 4 / 5).

    Edges are grouped into collinear rows (0x1009c6b0 rule with tol 0.01); rows are visited
    from the start corner by offset, alternating direction (serpentine: type 5 builder
    0x100a6580 reverses a row that runs the same way as the previous one).  Each row is
    extended by the side-line length before its first and after its last burn edge (the
    G-code loader 0x1000a730 builds rows x_first - L .. x_last + L); extensions, gaps and
    linkers (0x100a3f10) are beam-off, so the path starts with the beam off (engraving
    parity, cam.md §11.3)."""
    prm = prm or EngraveScanParams()
    edges, used = [], []
    for i, c in enumerate(contours):
        es = [_Edge(a, b, i) for a, b in _flatten_lines(c) if math.dist(a, b) >= prm.min_len]
        if es:
            edges += es
            used.append(i)
    if not edges:
        return [], []
    rows = _merge_lines(edges, prm.collinear_tol)
    d0 = rows[0].d
    rows = [g for g in rows if abs(g.d[0] - d0[0]) <= 0.01 and abs(g.d[1] - d0[1]) <= 0.01] + \
        [g for g in rows if not (abs(g.d[0] - d0[0]) <= 0.01 and abs(g.d[1] - d0[1]) <= 0.01)]
    if kind == SCAN_5:
        top_first, rev_first = prm.direction != 0, prm.direction != 0
    else:
        top_first, rev_first = prm.direction != 0, False
    # offset along the row normal; normal points "up" for canonical directions
    rows.sort(key=lambda g: g.offset(), reverse=top_first)
    L = prm.side_len
    spans: list[tuple[Segment, bool]] = []
    prev_end, prev_t = None, None
    rev = rev_first
    for g in rows:
        pcs = _line_pieces(g, rev)
        t = _unit((pcs[0][1][0] - pcs[0][0][0], pcs[0][1][1] - pcs[0][0][1]))
        a = (pcs[0][0][0] - t[0] * L, pcs[0][0][1] - t[1] * L)
        b = (pcs[-1][1][0] + t[0] * L, pcs[-1][1][1] + t[1] * L)
        if prev_end is not None:
            link, _ = _linker(prev_end, prev_t, a, t)
            spans += [(s, False) for s in link]
        if L > 0:
            spans.append((Line(a, pcs[0][0]), False))
        for k, (p, q) in enumerate(pcs):
            if k:
                gap = pcs[k - 1][1]
                if math.dist(gap, p) > 1e-9:
                    spans.append((Line(gap, p), False))
            spans.append((Line(p, q), True))
        if L > 0:
            spans.append((Line(pcs[-1][1], b), False))
        prev_end, prev_t = (b if L > 0 else pcs[-1][1]), t
        rev = not rev
    init, sw = switches_from_spans(spans)
    path = ScanPath(Contour([s for s, _ in spans], closed=False), sw, init, kind, 3, list(range(len(used))))
    return [path], used


def parse_scan_gcode(text: str) -> list[Contour]:
    """G-code -> burn segments in scan mode (CADModule CGCodeFileParse 0x1000bfd0 with the
    scan flag): G0, or G1 with S <= 0, is a move; G1 with S > 0 burns (S is only tested for
    > 0, no per-segment power).  G90/G91 honoured; X/Y/S modal.  Returns one open line contour
    per burn run (consecutive burns on one row), ready for ``engrave_scan(kind=5)``."""
    import re
    x = y = 0.0
    s_val = 0.0
    absolute = True
    motion = 0
    runs: list[list[Segment]] = []
    cur: list[Segment] = []
    for raw in text.splitlines():
        line = raw.split(";", 1)[0]
        line = re.sub(r"\(.*?\)", "", line).upper()
        if not line.strip():
            continue
        words = dict()
        for k, v in re.findall(r"([A-Z])\s*([-+]?\d*\.?\d+)", line):
            words.setdefault(k, []).append(float(v))
        for g in words.get("G", []):
            gi = int(g)
            if gi in (0, 1, 2, 3):
                motion = gi
            elif gi == 90:
                absolute = True
            elif gi == 91:
                absolute = False
        if "S" in words:
            s_val = words["S"][-1]
        if "X" not in words and "Y" not in words:
            continue
        nx = words["X"][-1] if "X" in words else (x if absolute else 0.0)
        ny = words["Y"][-1] if "Y" in words else (y if absolute else 0.0)
        if not absolute:
            nx, ny = x + nx, y + ny
        burn = motion == 1 and s_val > 0
        if burn and math.dist((x, y), (nx, ny)) > 1e-12:
            if cur and (math.dist(cur[-1].end, (x, y)) > 1e-9 or abs(cur[-1].end[1] - ny) > 1e-8):
                runs.append(cur)
                cur = []
            cur.append(Line((x, y), (nx, ny)))
        elif cur:
            runs.append(cur)
            cur = []
        x, y = nx, ny
    if cur:
        runs.append(cur)
    return [Contour(r, closed=False) for r in runs if r]


def apply_engrave_scan(doc: Document, prm: Optional[EngraveScanParams] = None, kind: int = SCAN_ENGRAVE,
                       selected_only: bool = True) -> Optional[Part]:
    """Type 4 scan engraving of the (selected) parts on the first candidate's layer (worker
    FUN_100c80d0): the consumed contours become one scan part in the first one's slot."""
    cand = [i for i, p in enumerate(doc.parts) if (p.selected or not selected_only) and p.contours
            and "scan_paths" not in p.extra]
    if not cand:
        return None
    layer = doc.parts[cand[0]].craft.layer
    cand = [i for i in cand if doc.parts[i].craft.layer == layer]
    flat = [(i, c) for i in cand for c in doc.parts[i].contours]
    if len(flat) < 2:
        return None
    paths, used = engrave_scan([c for _, c in flat], prm, kind)
    if not paths:
        return None
    used_set = {id(flat[k][1]) for k in used}
    slot = flat[used[0]][0]
    new = make_scan_part(paths, [flat[k][1] for k in used], doc.parts[slot].craft, kind)
    keep: list[Part] = []
    for i, p in enumerate(doc.parts):
        if i == slot:
            keep.append(new)
        if i in cand:
            rest = [c for c in p.contours if id(c) not in used_set]
            if rest:
                q = copy.copy(p)
                q.contours = rest
                keep.append(q)
        else:
            keep.append(p)
    doc.parts[:] = keep
    return new


def gcode_scan_part(text: str, prm: Optional[EngraveScanParams] = None, layer: int = 1) -> Optional[Part]:
    """Scan-G-code import (A251104_0 / Bitmap) as one type-5 scan part.  The source contours
    are the extended row lines (0x1000a730: x_first - L .. x_last + L)."""
    prm = prm or EngraveScanParams()
    runs = parse_scan_gcode(text)
    if not runs:
        return None
    paths, used = engrave_scan(runs, prm, SCAN_5)
    if not paths:
        return None
    rows = []
    for r in runs:
        a, b = r.segments[0].start, r.segments[-1].end
        t = _unit((b[0] - a[0], b[1] - a[1]))
        L = prm.side_len
        rows.append(Contour([Line((a[0] - t[0] * L, a[1] - t[1] * L), (b[0] + t[0] * L, b[1] + t[1] * L))], False))
    return make_scan_part(paths, rows, Craft(layer=layer), SCAN_5)
