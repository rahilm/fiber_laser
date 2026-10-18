"""Import cleanup: overlap/duplicate removal, joining, small-object removal, arc fitting.

Evidence (docs/cam.md §15). The import post-processor is ``CGly2ContourEng`` (CADModule
``0x10061c90``, called from the CCADModule import ``0x100f0e10`` via ``0x100b1220``) with an
option block that MainApp fills from the "IGP." import parameters (``0x004d35c0``)::

    +0x00 IGP.MegerConnectGraphType  pd367  0 none, 1 direction first, 2 length first, 3 distance first
    +0x08 IGP.ConnectGate            pd368  0.1   (join tolerance, also the contours' closing tol)
    +0x10 IGP.IsFilteOverlapGraph    pd365  1
    +0x18 IGP.OverlapGate            pd366  0.1
    +0x25 IGP.IsAutoFilterMicoGraph  pd363  1
    +0x28 IGP.MicoGraphGate          pd364  0.1
    +0x30 IGP.IsAutoSmoothGraph      pd369  0
    +0x38 IGP.SmoothAccuracy         pd370  0.05

Order in 0x10061c90 (O): overlap removal on the loose glyphs (0x10061190, tolerance
min(ConnectGate, OverlapGate), 20x20 spatial grid) -> chaining (type 0: one contour per
glyph; type 1: 0x1005f7d0; else 0x10076a20 per contour) -> small-object removal
(0x1005c150; gate MicoGraphGate if enabled, else 0.01) -> open-contour split (0x1005c2f0, U)
-> optional smoothing (0x10075820, accuracy) -> orientation / bbox refresh.

* **Small objects** (0x1005c150, O): a contour is deleted when max(bbox w, h) < gate; a
  contour made of a single point glyph is kept. (The lang label says "smallest length" but
  the test is on the bbox extent.)
* **Duplicate contours** (manual "Clear duplicate lines" mf471, vtable +0xd8 -> 0x100c0a30, O):
  contour i is removed when its sample points (decimated so consecutive kept points differ
  by >= 5*gate in x or y) all lie on another contour j whose bbox overlaps i's bbox
  expanded by the gate; closed contours must also match j's length within 10*gate.
* **Joining** (0x10076a20, O): ends match when |dx| <= tol and |dy| <= tol (a square, not a
  circle). With several candidates: type 1 takes the best tangent continuation (max dot of
  directions), type 2 the shortest candidate (I: the x87 compare is inverted in the
  pseudocode; the 1e8 initial value implies a minimum), type 3 the nearest end point.
* **Arc fitting**: CircleFitDLL ``CCircleFit::mainFit(MyPoint*, first, last, CdlFitOut*)``
  (0x10001010 -> 0x100011e0), used by Dxf2Grp ``0x1000eec0`` on runs of line glyphs with
  Ang_CircleFit = pi/10, Err_CircleFit = caller tolerance, Len_CircleFit = 10 (DLL
  defaults 0.02 / pi/10 / 100 / Rmax 5000). Ported statement by statement (O), see
  docs/cam.md §15: duplicate removal (1e-6), point tangents (0x10002420), whole-set circle
  test (0x10002af0), then a loop over runs of monotonically decreasing (fitted on a reversed
  copy, 0x10002620 / 0x100028a0) or increasing tangents whose chord turn stays <= Ang, each
  run of more than 4 points fitted by 0x100019e0 (biarc 0x100020b0 grown while the points
  are within Err, 0x10003100, then shrunk until the chord midpoints are within Err,
  0x10002eb0, radii <= Rmax, ratio <= 50 and the circles nest within Err; else the longest
  line); otherwise a straight run (chord deviation 0x10002680 <= Err, turn <= Ang).
* The CADModule smoother itself (0x10075820 -> 0x1004b8e0) converts runs of short glyphs
  into splines (CEditableSpline); its fit is U. The rebuild uses the CircleFit approach for
  ``smooth_contour`` so the result stays Line/Arc.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ..model import Arc, Contour, Document, Line, Part, Point, Segment

__all__ = ["CleanupParams", "remove_small", "remove_duplicate_contours", "remove_overlaps",
           "merge_connected", "close_gaps", "fit_arcs", "smooth_contour", "cleanup_contours",
           "cleanup_document", "FitParams"]

ERR_CIRCLEFIT = 0.02          # ?Err_CircleFit@@3NA default (CircleFitDLL 0x1000c050)
ANG_CIRCLEFIT = math.pi / 10  # ?Ang_CircleFit@@3NA (0x1000c060; Dxf2Grp sets the same)
RMAX_CIRCLEFIT = 5000.0       # ?Rmax_CircleFit@@3NA (0x1000c068)
RADIUS_RATIO = 50.0           # 0x1000b100
DUP_EPS = 1e-6                # 0x1000b0f8


@dataclass
class CleanupParams:
    """Import parameters "IGP." (ManuPara.xml defaults)."""
    remove_small: bool = True          # pd363
    small_gate: float = 0.1            # pd364  [0.01, 10]
    remove_overlap: bool = True        # pd365
    overlap_gate: float = 0.1          # pd366  [0.01, 0.5]
    merge_type: int = 1                # pd367  0 none, 1 direction, 2 length, 3 distance
    connect_gate: float = 0.1          # pd368  [0.01, 0.5]
    smooth: bool = False               # pd369
    smooth_accuracy: float = 0.05      # pd370  [0.01, 10]
    close_gap: float = 0.0             # rebuild addition: close open contours up to this gap


# --------------------------------------------------------------------------- small helpers
def _bbox_of(c: Contour):
    pts = list(c.points(0.05))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _seg_dist(p: Point, s: Segment) -> float:
    """Distance from a point to a Line or Arc segment."""
    if isinstance(s, Line):
        (x0, y0), (x1, y1) = s.start, s.end
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 <= 0 else max(0.0, min(1.0, ((p[0] - x0) * dx + (p[1] - y0) * dy) / L2))
        return math.hypot(p[0] - (x0 + t * dx), p[1] - (y0 + t * dy))
    r = s.radius
    ang = math.atan2(p[1] - s.center[1], p[0] - s.center[0])
    a0 = math.atan2(s.start[1] - s.center[1], s.start[0] - s.center[0])
    sw = s.sweep()
    d = (ang - a0) % (2 * math.pi) if sw > 0 else (a0 - ang) % (2 * math.pi)
    if d <= abs(sw) + 1e-12:
        return abs(math.dist(p, s.center) - r)
    return min(math.dist(p, s.start), math.dist(p, s.end))


def _contour_dist(p: Point, c: Contour) -> float:
    return min(_seg_dist(p, s) for s in c.segments)


# --------------------------------------------------------------------------- small objects
def remove_small(contours: Sequence[Contour], gate: float) -> list[Contour]:
    """0x1005c150: drop contours whose larger bbox side is below ``gate``."""
    out = []
    for c in contours:
        if not c.segments:
            continue
        x0, y0, x1, y1 = _bbox_of(c)
        if max(x1 - x0, y1 - y0) < gate:
            continue
        out.append(c)
    return out


# --------------------------------------------------------------------------- duplicates
def _samples(c: Contour, gate: float) -> list[Point]:
    pts = list(c.points(min(0.05, gate)))
    if not pts:
        return []
    keep = [pts[0]]
    step = 5.0 * gate                      # 0x100c0a30: decimation with DAT_1011f428 = 5
    for q in pts[1:]:
        if abs(q[0] - keep[-1][0]) >= step or abs(q[1] - keep[-1][1]) >= step:
            keep.append(q)
    if math.dist(keep[-1], pts[-1]) > 1e-9:
        keep.append(pts[-1])
    # also probe segment midpoints so a short duplicate is not judged by its ends only
    for s in c.segments:
        keep.append(s.point_at(0.5) if isinstance(s, Arc) else
                    ((s.start[0] + s.end[0]) / 2, (s.start[1] + s.end[1]) / 2))
    return keep


def remove_duplicate_contours(contours: Sequence[Contour], gate: float = 0.1) -> list[Contour]:
    """0x100c0a30 "Clear Duplicate Lines": remove contours lying on another contour."""
    cs = [c for c in contours if c.segments]
    alive = [True] * len(cs)
    boxes = [_bbox_of(c) for c in cs]
    lens = [c.length() for c in cs]
    for i in range(len(cs) - 1, -1, -1):
        bi = boxes[i]
        samp = None
        for j in range(len(cs) - 1, -1, -1):
            if j == i or not alive[j]:
                continue
            if cs[i].closed and abs(lens[i] - lens[j]) > 10.0 * gate:
                continue
            bj = boxes[j]
            if bj[0] > bi[2] + gate or bj[2] < bi[0] - gate or bj[1] > bi[3] + gate or bj[3] < bi[1] - gate:
                continue
            if lens[i] > lens[j] + 10.0 * gate:
                continue                    # a longer contour cannot lie on a shorter one
            samp = samp or _samples(cs[i], gate)
            if all(_contour_dist(p, cs[j]) <= gate for p in samp):
                alive[i] = False
                break
    return [c for c, a in zip(cs, alive) if a]


# --------------------------------------------------------------------------- overlaps (glyph level)
def _line_key(s: Line, tol: float):
    (x0, y0), (x1, y1) = s.start, s.end
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy)
    ux, uy = dx / L, dy / L
    if ux < -1e-12 or (abs(ux) <= 1e-12 and uy < 0):
        ux, uy = -ux, -uy
    off = -uy * x0 + ux * y0                 # signed distance of the carrier line from origin
    return ux, uy, off


def remove_overlaps(segs: Iterable[Segment], tol: float = 0.1) -> list[Segment]:
    """Glyph-level overlap removal (role of 0x10061190; details I).

    Collinear lines (direction within tol/length, carrier offset within tol) and co-circular
    arcs (centre and radius within tol) are merged so every stretch of geometry occurs once.
    Disjoint pieces stay separate; direction of the first piece of a group is kept.
    """
    lines: list[Line] = []
    arcs: list[Arc] = []
    for s in segs:
        if s.length() <= 1e-9:
            continue
        (lines if isinstance(s, Line) else arcs).append(s)
    out: list[Segment] = []
    # ---- lines
    used = [False] * len(lines)
    keys = [_line_key(s, tol) for s in lines]
    for i, s in enumerate(lines):
        if used[i]:
            continue
        ux, uy, off = keys[i]
        group = [i]
        for j in range(i + 1, len(lines)):
            if used[j]:
                continue
            vx, vy, o2 = keys[j]
            Lj = lines[j].length()
            if abs(ux * vy - uy * vx) * max(Lj, s.length()) <= tol and abs(o2 - off) <= tol:
                group.append(j)
        for j in group:
            used[j] = True
        # project to the carrier and union the intervals
        ref = s.start
        iv = []
        for j in group:
            a = (lines[j].start[0] - ref[0]) * ux + (lines[j].start[1] - ref[1]) * uy
            b = (lines[j].end[0] - ref[0]) * ux + (lines[j].end[1] - ref[1]) * uy
            iv.append((min(a, b), max(a, b)))
        iv.sort()
        merged = []
        for a, b in iv:
            if merged and a <= merged[-1][1] + 1e-9:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        fwd = ((s.end[0] - s.start[0]) * ux + (s.end[1] - s.start[1]) * uy) > 0
        for a, b in merged:
            p = (ref[0] + ux * a, ref[1] + uy * a)
            q = (ref[0] + ux * b, ref[1] + uy * b)
            out.append(Line(p, q) if fwd else Line(q, p))
    # ---- arcs
    used = [False] * len(arcs)
    for i, s in enumerate(arcs):
        if used[i]:
            continue
        group = [i]
        for j in range(i + 1, len(arcs)):
            if not used[j] and math.dist(arcs[j].center, s.center) <= tol and abs(arcs[j].radius - s.radius) <= tol:
                group.append(j)
        for j in group:
            used[j] = True
        if len(group) == 1:
            out.append(s)
            continue
        c, r = s.center, s.radius
        iv = []
        full = False
        for j in group:
            a = arcs[j]
            sw = a.sweep()
            a0 = math.atan2(a.start[1] - c[1], a.start[0] - c[0])
            if abs(abs(sw) - 2 * math.pi) < 1e-9:
                full = True
                break
            lo = a0 if sw > 0 else a0 + sw
            lo %= 2 * math.pi
            iv.append((lo, lo + abs(sw)))
        if full:
            p0 = (c[0] + r, c[1])
            out.append(Arc(p0, p0, c, s.ccw))
            continue
        iv.sort()
        merged = []
        for a, b in iv:
            if merged and a <= merged[-1][1] + 1e-9:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        # wrap-around: the last interval may run past 2pi onto the first one
        while len(merged) > 1 and merged[-1][1] - 2 * math.pi >= merged[0][0] - 1e-9:
            a, b = merged.pop()
            merged[0] = (a, max(b, merged[0][1] + 2 * math.pi))
        res = [(a, min(b, a + 2 * math.pi)) for a, b in merged]
        for a, b in res:
            if b - a >= 2 * math.pi - 1e-9:
                p0 = (c[0] + r, c[1])
                out.append(Arc(p0, p0, c, s.ccw))
                continue
            pa = (c[0] + r * math.cos(a), c[1] + r * math.sin(a))
            pb = (c[0] + r * math.cos(b), c[1] + r * math.sin(b))
            out.append(Arc(pa, pb, c, True) if s.ccw else Arc(pb, pa, c, False))
    return out


# --------------------------------------------------------------------------- joining
def _tangent_start(s: Segment) -> Point:
    if isinstance(s, Line):
        d = (s.end[0] - s.start[0], s.end[1] - s.start[1])
    else:
        rx, ry = s.start[0] - s.center[0], s.start[1] - s.center[1]
        d = (-ry, rx) if s.ccw else (ry, -rx)
    n = math.hypot(*d) or 1.0
    return (d[0] / n, d[1] / n)


def _tangent_end(s: Segment) -> Point:
    if isinstance(s, Line):
        d = (s.end[0] - s.start[0], s.end[1] - s.start[1])
    else:
        rx, ry = s.end[0] - s.center[0], s.end[1] - s.center[1]
        d = (-ry, rx) if s.ccw else (ry, -rx)
    n = math.hypot(*d) or 1.0
    return (d[0] / n, d[1] / n)


def _near(p: Point, q: Point, tol: float) -> bool:
    return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol     # square test (0x10076a20)


def _with_start(s: Segment, p: Point) -> Segment:
    return Line(p, s.end) if isinstance(s, Line) else Arc(p, s.end, s.center, s.ccw)


def _with_end(s: Segment, p: Point) -> Segment:
    return Line(s.start, p) if isinstance(s, Line) else Arc(s.start, p, s.center, s.ccw)


def _pieces(items) -> list[list[Segment]]:
    out = []
    for it in items:
        if isinstance(it, Contour):
            if it.segments:
                out.append(list(it.segments))
        else:
            out.append([it])
    return out


def merge_connected(items: Iterable, tol: float = 0.1, mode: int = 1) -> list[Contour]:
    """Join segments/open contours whose ends meet within ``tol`` (0x10076a20).

    ``items`` may mix Segments and Contours; closed contours pass through unchanged.
    ``mode``: 0 no joining, 1 direction first, 2 length (shortest) first, 3 distance first.
    A chain is closed when its end meets its start within ``tol`` (the new contour's
    closing tolerance is the gate, ctor 0x1006c2d0(tol)).
    """
    items = list(items)
    closed = [it for it in items if isinstance(it, Contour) and it.closed and it.segments]
    pool = _pieces([it for it in items if not (isinstance(it, Contour) and it.closed)])
    if mode == 0:
        return closed + [Contour(p, _near(p[0].start, p[-1].end, tol) and len(p) > 1) for p in pool]
    used = [False] * len(pool)
    plen = [sum(s.length() for s in p) for p in pool]
    out = list(closed)

    def candidates(pt: Point, at_end: bool):
        res = []
        for k, p in enumerate(pool):
            if used[k]:
                continue
            if _near(p[0].start, pt, tol):
                res.append((k, False))            # attach forward
            elif _near(p[-1].end, pt, tol):
                res.append((k, True))             # attach reversed
        return res

    def pick(res, pt: Point, tan: Point, forward: bool):
        if len(res) == 1 or not res:
            return res[0] if res else None
        best, bv = None, None
        for k, rev in res:
            p = pool[k]
            if mode == 1:
                if forward:
                    t = _tangent_start(p[0]) if not rev else tuple(-x for x in _tangent_end(p[-1]))
                else:
                    t = _tangent_end(p[-1]) if not rev else tuple(-x for x in _tangent_start(p[0]))
                v = -(t[0] * tan[0] + t[1] * tan[1])          # minimise -dot = maximise dot
            elif mode == 2:
                v = plen[k]
            else:
                q = (p[-1].end if rev else p[0].start)
                v = math.dist(q, pt)
            if bv is None or v < bv:
                best, bv = (k, rev), v
        return best

    for i, p in enumerate(pool):
        if used[i]:
            continue
        used[i] = True
        chain = list(p)
        # forward
        while not _near(chain[-1].end, chain[0].start, tol) or len(chain) == 1 and isinstance(chain[0], Line):
            res = [r for r in candidates(chain[-1].end, True)]
            sel = pick(res, chain[-1].end, _tangent_end(chain[-1]), True)
            if sel is None:
                break
            k, rev = sel
            used[k] = True
            segs = pool[k] if not rev else [s.reversed() for s in reversed(pool[k])]
            segs = list(segs)
            segs[0] = _with_start(segs[0], chain[-1].end)
            chain.extend(segs)
        # backward
        if not _near(chain[-1].end, chain[0].start, tol):
            while True:
                res = []
                for k, q in enumerate(pool):
                    if used[k]:
                        continue
                    if _near(q[-1].end, chain[0].start, tol):
                        res.append((k, False))
                    elif _near(q[0].start, chain[0].start, tol):
                        res.append((k, True))
                sel = pick(res, chain[0].start, _tangent_start(chain[0]), False)
                if sel is None:
                    break
                k, rev = sel
                used[k] = True
                segs = pool[k] if not rev else [s.reversed() for s in reversed(pool[k])]
                segs = list(segs)
                segs[-1] = _with_end(segs[-1], chain[0].start)
                chain = segs + chain
                if _near(chain[-1].end, chain[0].start, tol):
                    break
        is_closed = (len(chain) > 1 or isinstance(chain[0], Arc)) and _near(chain[-1].end, chain[0].start, tol)
        if is_closed:
            chain[-1] = _with_end(chain[-1], chain[0].start)
        out.append(Contour(chain, is_closed))
    return out


def close_gaps(contours: Sequence[Contour], max_gap: float) -> list[Contour]:
    """Rebuild addition: close open contours whose ends are within ``max_gap`` with a line."""
    out = []
    for c in contours:
        if not c.closed and c.segments and len(c.segments) >= 1:
            g = math.dist(c.segments[-1].end, c.segments[0].start)
            if g <= max_gap and c.length() > 2 * g:
                segs = list(c.segments)
                if g > 1e-9:
                    segs.append(Line(segs[-1].end, segs[0].start))
                out.append(Contour(segs, True, dict(c.extra)))
                continue
        out.append(c)
    return out


# --------------------------------------------------------------------------- CircleFit port
@dataclass
class FitParams:
    err: float = ERR_CIRCLEFIT        # Err_CircleFit (max deviation)
    ang: float = ANG_CIRCLEFIT        # Ang_CircleFit (corner split angle)
    rmax: float = RMAX_CIRCLEFIT      # Rmax_CircleFit
    ratio: float = RADIUS_RATIO       # max ratio between the two radii of a biarc


_TWO_PI = 2 * math.pi
_HALF_PI = math.pi / 2


def _ang(x1: float, y1: float, x2: float, y2: float) -> float:
    """0x10002600: direction angle of (x1,y1)->(x2,y2) (fpatan)."""
    return math.atan2(y2 - y1, x2 - x1)


def _unwrap_pair(a1: float, a2: float) -> tuple[float, float]:
    """The wrap fix used around every chord-turn test in 0x100011e0 / 0x100019e0."""
    if a1 > _HALF_PI and a2 < -_HALF_PI:
        a2 += _TWO_PI
    elif a2 > _HALF_PI and a1 < -_HALF_PI:
        a1 += _TWO_PI
    return a1, a2


class _Pts:
    """MyPoint array (0x58 bytes each): x @0, y @8, tangent angle @0x28 (field 5)."""
    __slots__ = ("x", "y", "t")

    def __init__(self, xs, ys):
        self.x = list(xs)
        self.y = list(ys)
        self.t = [0.0] * len(self.x)

    def reversed_copy(self, a: int, b: int) -> "_Pts":
        """0x10002620: copy of points b..a (reversed), tangents recomputed on the copy."""
        r = _Pts(self.x[a:b + 1][::-1], self.y[a:b + 1][::-1])
        r.tangents(0, b - a)
        return r

    def chord(self, i: int, j: int) -> float:
        return _ang(self.x[i], self.y[i], self.x[j], self.y[j])

    def tangents(self, a: int, b: int) -> None:
        """0x10002420: interior tangent = sum of the normalised neighbouring chords; the end
        tangents mirror the neighbour's about the end chord, normalised to [-pi, pi]."""
        x, y, t = self.x, self.y, self.t
        ln = {}
        for k in range(a, b):
            ln[k] = math.sqrt((x[k + 1] - x[k]) ** 2 + (y[k + 1] - y[k]) ** 2)
        for k in range(a + 1, b):
            sx = (x[k + 1] - x[k]) / ln[k] + (x[k] - x[k - 1]) / ln[k - 1]
            sy = (y[k + 1] - y[k]) / ln[k] + (y[k] - y[k - 1]) / ln[k - 1]
            t[k] = math.atan2(sy, sx)
        for k, v in ((a, 2 * self.chord(a, a + 1) - t[a + 1]), (b, 2 * self.chord(b - 1, b) - t[b - 1])):
            while v > math.pi:
                v -= _TWO_PI
            while v < -math.pi:
                v += _TWO_PI
            t[k] = v

    def dev(self, i: int, j: int) -> float:
        """0x10002680: max distance of points i+1..j-1 from the chord i->j."""
        m = 0.0
        x, y = self.x, self.y
        if j >= len(x):
            return math.inf          # past the end (the DLL reads scratch memory there)
        a = _ang(x[i], y[i], x[j], y[j])
        for k in range(i + 1, j):
            d = math.sqrt((y[i] - y[k]) ** 2 + (x[i] - x[k]) ** 2) * abs(math.sin(abs(_ang(x[i], y[i], x[k], y[k]) - a)))
            if d > m:
                m = d
        return m

    def corner(self, k: int, ang: float) -> bool:
        """0x100029d0: tangent jump between points k-1 and k larger than Ang."""
        a, b = self.t[k - 1], self.t[k]
        if b > _HALF_PI and a < -_HALF_PI:
            return (a + _TWO_PI) - b > ang
        if a > _HALF_PI and b < -_HALF_PI:
            return (b + _TWO_PI) - a > ang
        return abs(b - a) > ang

    def turn(self, k: int) -> float:
        """|chord(k-1,k) - chord(k,k+1)| after the wrap fix."""
        a1, a2 = _unwrap_pair(self.chord(k - 1, k), self.chord(k, k + 1))
        return abs(a1 - a2)


def _circle_test(p: _Pts, a: int, b: int, fp: "FitParams"):
    """0x10002af0: all points one circle (no tangent corner, Kasa fit, max radial error of the
    points and chord midpoints < Err, ends closer than sqrt(0.1 Err)) -> (cx, cy, r)."""
    n = b - a + 1
    if n <= 2:
        return None
    for k in range(a + 1, b):
        if p.corner(k, fp.ang):
            return None
    sx = sy = sxx = syy = sxy = sxxx = syyy = sxyy = sxxy = 0.0
    for k in range(a, b + 1):
        x, y = p.x[k], p.y[k]
        sx += x; sy += y
        xx, yy = x * x, y * y
        sxx += xx; syy += yy
        sxxx += xx * x; syyy += yy * y
        sxy += y * x; sxyy += yy * x; sxxy += y * x * x
    A = n * sxx - sx * sx
    B = n * sxy - sy * sx
    C = n * syy - sy * sy
    D1 = (sxyy + sxxx) * n - (syy + sxx) * sx
    E1 = (sxxy + syyy) * n - (syy + sxx) * sy
    den = C * A - B * B
    if den == 0:
        return None
    D = (E1 * B - C * D1) / den
    E = (E1 * A - D1 * B) / -den
    cx, cy = D * -0.5, E * -0.5
    rr = (D * D + E * E) + 4 * ((D * sx + E * sy + syy + sxx) / n)
    if rr < 0:
        return None
    r = math.sqrt(rr) * 0.5
    m = 0.0
    for k in range(a, b + 1):
        m = max(m, abs(math.hypot(p.x[k] - cx, p.y[k] - cy) - r))
    for k in range(a, b):
        mx, my = (p.x[k] + p.x[k + 1]) * 0.5, (p.y[k] + p.y[k + 1]) * 0.5
        m = max(m, abs(math.hypot(mx - cx, my - cy) - r))
    if m < fp.err and fp.err * 0.1 > (p.y[a] - p.y[b]) ** 2 + (p.x[a] - p.x[b]) ** 2:
        return cx, cy, r
    return None


def _biarc_dll(p: _Pts, i: int, k: int):
    """0x100020b0: biarc from point i (tangent t_i) to point k (tangent t_k) with the joint
    tangent along the chord.  Returns (c1, r1, pm, c2, r2, alpha, beta)."""
    ax, ay, ta = p.x[i], p.y[i], p.t[i]
    bx, by, tb = p.x[k], p.y[k], p.t[k]
    d = math.sqrt((by - ay) ** 2 + (bx - ax) ** 2)
    th = _ang(ax, ay, bx, by)
    al = th - ta if (ta <= _HALF_PI or th >= -_HALF_PI) else th - ta + _TWO_PI
    be = tb - th if (th <= _HALF_PI or tb >= -_HALF_PI) else tb - th + _TWO_PI
    s_ab = math.sin((be + al) * 0.5)
    s_a = math.sin(al * 0.5)
    s_b = math.sin(be * 0.5)
    if abs(s_ab) < 1e-6:
        s_ab = 1e-6 if s_ab >= 0 else -1e-6
    if abs(s_a) < 1e-6:
        s_a = 1e-6 if s_a >= 0 else -1e-6
    s_b_den = s_b if abs(s_b) >= 1e-6 else 1e-6     # both signs -> +1e-6 (as in the DLL)
    r1 = abs(math.sin(be * 0.5) * d / (s_a * s_ab + s_a * s_ab))
    r2 = abs(math.sin(al * 0.5) * d / (s_b_den * s_ab + s_b_den * s_ab))
    s0, c0 = math.sin(ta), math.cos(ta)
    s1, c1_ = math.sin(al + ta), math.cos(al + ta)
    pm = ((-s0 - -s1) * r1 + ax, (c0 - c1_) * r1 + ay)
    c1 = (-s0 * r1 + ax, c0 * r1 + ay)
    c2 = (-s1 * r2 + pm[0], c1_ * r2 + pm[1])
    return c1, r1, pm, c2, r2, al, be


def _arc_err(p: _Pts, ba, i: int, k: int, points: bool, err: float) -> bool:
    """0x10003100 (points i+1..k-1, max <= Err) / 0x10002eb0 (chord midpoints of i..k,
    none > Err): distance to arc 1 or arc 2, chosen by the angle around centre 1."""
    c1, r1, pm, c2, r2 = ba[:5]
    a0 = _ang(c1[0], c1[1], p.x[i], p.y[i])
    am = _ang(c1[0], c1[1], pm[0], pm[1])
    if am < a0:
        am += _TWO_PI
    if points:
        qs = [(p.x[m], p.y[m]) for m in range(i + 1, k)]
    else:
        qs = [((p.x[m] + p.x[m + 1]) * 0.5, (p.y[m] + p.y[m + 1]) * 0.5) for m in range(i, k)]
    for qx, qy in qs:
        a = _ang(c1[0], c1[1], qx, qy)
        if a < a0:
            a += _TWO_PI
        if am <= a:
            e = abs(math.hypot(qx - c2[0], qy - c2[1]) - r2)
        else:
            e = abs(math.hypot(qx - c1[0], qy - c1[1]) - r1)
        if e > err:
            return False
    return True


def _fit_ccw(p: _Pts, i: int, j: int, fp: "FitParams", out: list) -> None:
    """0x100019e0: fit a run i..j (turning one way): one line if straight; else the longest
    biarc from i whose points stay within Err, shrunk from its end until the midpoints are
    within Err, both radii <= Rmax, radius ratio <= 50 and the circles nest within Err; else
    the longest line; then recurse on the rest."""
    err = fp.err
    k = i + 1
    if k <= j:
        if p.dev(i, j) < err:
            out.append(("L", i, j))
            return
        while k <= j:
            ba = _biarc_dll(p, i, k)
            if not _arc_err(p, ba, i, k, True, err):
                break
            k += 1
    k -= 1
    if k - i > 3:
        while True:
            ba = _biarc_dll(p, i, k)
            c1, r1, pm, c2, r2 = ba[:5]
            if (_arc_err(p, ba, i, k, False, err) and r1 <= fp.rmax and r2 <= fp.rmax
                    and r2 <= r1 * fp.ratio and r1 <= r2 * fp.ratio
                    and math.dist(c1, c2) - abs(r2 - r1) <= err):
                break
            k -= 1
            if not k - i > 3:
                break
        if k - i > 3:
            out.append(("A", i, k, ba))
            if k < j:
                _fit_ccw(p, k, j, fp, out)
            return
    k = i + 1
    if k <= j and p.dev(i, k) < err:
        while p.dev(i, k) < err:
            k += 1
            if j < k or p.corner(k, fp.ang):
                break
        if j < k or p.dev(i, k) > err or p.turn(k - 1) > fp.ang:
            k -= 1
        if k <= j:
            out.append(("L", i, k))
    if k < j:
        _fit_ccw(p, k, j, fp, out)


def _mainfit(p: _Pts, first: int, last: int, fp: "FitParams") -> list:
    """0x100011e0 (CCircleFit::mainFit).  Returns records ('L', p0, p1), ('I', c, r) or
    ('A', start, end, ba) in path order (start/end as points)."""
    out: list = []
    if last - first < 2:
        return [("L", (p.x[k], p.y[k]), (p.x[k + 1], p.y[k + 1])) for k in range(first, last)]
    p.tangents(first, last)
    circ = _circle_test(p, first, last, fp)
    if circ is not None:
        return [("I", (circ[0], circ[1]), circ[2])]
    t = p.t
    ang = fp.ang
    p2, p3 = first, last

    def emit(recs, pts: _Pts, rev: bool):
        res = []
        for r in recs:
            if r[0] == "L":
                a, b = (pts.x[r[1]], pts.y[r[1]]), (pts.x[r[2]], pts.y[r[2]])
                res.append(("L", b, a) if rev else ("L", a, b))
            else:
                _, a, b, ba = r
                pa, pb = (pts.x[a], pts.y[a]), (pts.x[b], pts.y[b])
                run = [(pts.x[m], pts.y[m]) for m in range(a, b + 1)]
                rec = ("A", pa, pb, ba, run)
                res.append(("Ar", rec) if rev else rec)
        return res[::-1] if rev else res

    while p2 < p3:
        fitted = False
        k5 = p2 + 1
        # A: run of decreasing tangents (CW): fitted on the reversed copy
        if t[k5] < t[k5 - 1] or (t[k5 - 1] < -_HALF_PI and t[k5] > _HALF_PI):
            k = k5
            while True:
                k4 = k
                if t[k4 - 1] <= t[k4] and (t[k4 - 1] >= -_HALF_PI or t[k4] <= _HALF_PI):
                    break
                if p3 < k4 + 1:
                    k4 = p3
                    break
                if p.turn(k4) > ang:
                    break
                k = k4 + 1
            if k4 < p3:
                t[k4] = 2 * p.chord(k4, k4 + 1) - t[k4 + 1]
            if k4 - p2 > 3:
                rp = p.reversed_copy(p2, k4)
                recs: list = []
                _fit_ccw(rp, 0, k4 - p2, fp, recs)
                out += emit(recs, rp, True)
                fitted = True
                p2 = k4
            k5 = p2 + 1
        # B: run of increasing tangents (CCW)
        if k5 <= p3 and (t[k5 - 1] < t[k5] or (t[k5] < -_HALF_PI and t[k5 - 1] > _HALF_PI)):
            k = k5
            at_end = False
            while True:
                k9 = k
                if t[k9] <= t[k9 - 1] and (t[k9] >= -_HALF_PI or t[k9 - 1] <= _HALF_PI):
                    break
                if p3 < k9 + 1:
                    k9 = p3
                    at_end = True
                    break
                if p.turn(k9) > ang:
                    break
                k = k9 + 1
            if not at_end and k9 < p3:
                t[k9] = 2 * p.chord(k9 - 1, k9) - t[k9 - 1]
            if k9 - p2 > 3:
                recs = []
                _fit_ccw(p, p2, k9, fp, recs)
                out += emit(recs, p, False)
                fitted = True
                p2 = k9
            if k9 < p3:
                t[k9] = 2 * p.chord(k9, k9 + 1) - t[k9 + 1]
            k5 = p2 + 1
        # C: straight line run
        if not fitted and k5 <= p3 and p.dev(p2, k5) < fp.err:
            while True:
                k4 = k5
                k5 = k4 + 1
                if p3 < k5:
                    k4 = p3
                    break
                if not (p.dev(p2, k5) <= fp.err and p.turn(k4) <= ang):
                    break
            if p3 < k4:
                k4 = p3
            out.append(("L", (p.x[p2], p.y[p2]), (p.x[k4], p.y[k4])))
            p2 = k4
            if k4 < p3:
                t[k4] = 2 * p.chord(k4, k4 + 1) - t[k4 + 1]
    return out


def _oriented_arc(p0: Point, p1: Point, c: Point, run: list[Point]) -> Arc:
    """CdlFitOut 'A' keeps centre, radius and the two end angles but no direction (Dxf2Grp
    0x1000eec0 copies them as they are).  Take the sweep whose middle lies on the fitted run."""
    best = None
    for ccw in (True, False):
        a = Arc(p0, p1, c, ccw)
        m = a.point_at(0.5)
        d = min((qx - m[0]) ** 2 + (qy - m[1]) ** 2 for qx, qy in run)
        if best is None or d < best[0]:
            best = (d, a)
    return best[1]


def _records_to_segments(recs: list) -> list[Segment]:
    out: list[Segment] = []
    for r in recs:
        if r[0] == "L":
            if math.dist(r[1], r[2]) > 0:
                out.append(Line(r[1], r[2]))
        elif r[0] == "I":
            (cx, cy), rad = r[1], r[2]
            out.append(("I", (cx, cy), rad))      # resolved by the caller
        else:
            rev = r[0] == "Ar"
            _, pa, pb, ba, run = r[1] if rev else r
            c1, r1, pm, c2, r2, al, be = ba
            a1 = _oriented_arc(pa, pm, c1, run)
            a2 = _oriented_arc(pm, pb, c2, run)
            segs = [a1, a2]
            if rev:
                segs = [a2.reversed(), a1.reversed()]
            out.extend(s for s in segs if math.dist(s.start, s.end) > 0)
    return out


def fit_arcs(points: Sequence[Point], fp: Optional[FitParams] = None, closed: bool = False) -> list[Segment]:
    """Fit lines/arcs/biarcs to a polyline: port of CircleFitDLL ``CCircleFit::mainFit``
    (0x10001010 -> 0x100011e0, see the module doc and docs/cam.md §15).

    Returns a continuous segment list from the first to the last point, or a single full
    circle Arc ('I') when the closed polyline is a circle within ``fp.err``.
    """
    fp = fp or FitParams()
    pts: list[Point] = []
    for p in points:   # 0x100011e0: drop a point equal (|dx|, |dy| < 1e-6) to its predecessor
        if not pts or abs(p[0] - pts[-1][0]) >= DUP_EPS or abs(p[1] - pts[-1][1]) >= DUP_EPS:
            pts.append((float(p[0]), float(p[1])))
    if closed and len(pts) > 1 and math.dist(pts[0], pts[-1]) > DUP_EPS:
        pts.append(pts[0])
    if len(pts) < 2:
        return []
    p = _Pts([q[0] for q in pts], [q[1] for q in pts])
    recs = _mainfit(p, 0, len(pts) - 1, fp)
    if len(recs) == 1 and recs[0][0] == "I":
        (cx, cy), r = recs[0][1], recs[0][2]
        area = sum(pts[k][0] * pts[k + 1][1] - pts[k + 1][0] * pts[k][1] for k in range(len(pts) - 1))
        d0 = math.dist(pts[0], (cx, cy)) or 1.0
        p0 = (cx + r * (pts[0][0] - cx) / d0, cy + r * (pts[0][1] - cy) / d0)
        return [Arc(p0, p0, (cx, cy), area > 0)]
    out = _records_to_segments(recs)
    for k in range(1, len(out)):       # make the chain exactly continuous
        out[k] = _with_start(out[k], out[k - 1].end)
    return out


def smooth_contour(c: Contour, accuracy: float = 0.05, min_lines: int = 3,
                   fp: Optional[FitParams] = None) -> Contour:
    """Replace runs of >= ``min_lines`` consecutive Lines by fitted lines/arcs (CircleFit
    approach used by Dxf2Grp 0x1000eec0; Err = ``accuracy``). Arcs are kept as they are."""
    fp = fp or FitParams(err=accuracy)
    segs = list(c.segments)
    if not segs:
        return c
    all_lines = all(isinstance(s, Line) for s in segs)
    if all_lines and c.closed and len(segs) >= min_lines:
        pts = [segs[0].start] + [s.end for s in segs]
        new = fit_arcs(pts, fp, closed=True)
        return Contour(new, True, dict(c.extra)) if new else c
    out: list[Segment] = []
    run: list[Line] = []

    def flush():
        if len(run) >= min_lines:
            out.extend(fit_arcs([run[0].start] + [s.end for s in run], fp))
        else:
            out.extend(run)
        run.clear()
    for s in segs:
        if isinstance(s, Line):
            run.append(s)
        else:
            flush()
            out.append(s)
    flush()
    for k in range(1, len(out)):
        out[k] = _with_start(out[k], out[k - 1].end)
    if c.closed and out:
        out[-1] = _with_end(out[-1], out[0].start)
    return Contour(out, c.closed, dict(c.extra))


# --------------------------------------------------------------------------- pipeline
def cleanup_contours(contours: Sequence[Contour], prm: Optional[CleanupParams] = None) -> list[Contour]:
    """Run the 0x10061c90 pipeline on a contour list (open contours are exploded and re-joined)."""
    prm = prm or CleanupParams()
    closed = [c for c in contours if c.closed and c.segments]
    loose: list[Segment] = [s for c in contours if not c.closed for s in c.segments]
    if prm.remove_overlap:
        tol = min(prm.connect_gate, prm.overlap_gate)
        loose = remove_overlaps(loose, tol)
        closed = remove_duplicate_contours(closed, prm.overlap_gate)
    joined = merge_connected(closed + loose, prm.connect_gate, prm.merge_type)
    if prm.remove_overlap:
        joined = remove_duplicate_contours(joined, prm.overlap_gate)
    if prm.close_gap > 0:
        joined = close_gaps(joined, prm.close_gap)
    joined = remove_small(joined, prm.small_gate if prm.remove_small else 0.01)
    if prm.smooth:
        joined = [smooth_contour(c, prm.smooth_accuracy) for c in joined]
    return joined


def cleanup_document(doc: Document, prm: Optional[CleanupParams] = None,
                     only_selected: bool = False) -> dict:
    """Clean the document in place: one Part per resulting contour (craft of the first source
    part is reused). Returns counts {'before', 'after'}."""
    prm = prm or CleanupParams()
    parts = [p for p in doc.parts if p.selected] if only_selected else list(doc.parts)
    if not parts:
        return {"before": 0, "after": 0}
    keep = [p for p in doc.parts if p not in parts]
    before = sum(len(p.contours) for p in parts)
    import copy as _copy
    craft = _copy.deepcopy(parts[0].craft)
    res = cleanup_contours([c for p in parts for c in p.contours], prm)
    new = []
    for c in res:
        q = Part(contours=[c], craft=_copy.deepcopy(craft))
        new.append(q)
    doc.parts = keep + new
    doc.extra.pop("order", None)
    return {"before": before, "after": len(res)}
