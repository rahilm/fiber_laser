"""CircleFitDLL ``CCircleFit::mainFit``: polyline -> lines, arcs and biarcs, statement by statement
(moved from ``mlaser.cam.cleanup``, Wave 1; docs/cam.md §15, docs/parity.md §3 "CircleFitDLL").

Duplicate removal (1e-6), point tangents (0x10002420), whole-set circle test (0x10002af0), runs of
monotonic tangents fitted by 0x100019e0 (biarc 0x100020b0 grown within Err, 0x10003100, shrunk
until the chord midpoints are within Err, 0x10002eb0), else straight runs (0x10002680).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from ...model import Arc, Line, Point, Segment
from .._registry import port

__port__ = {
    "source": "CircleFitDLL",
    "functions": {"0x10001010 / 0x100011e0": "fit_arcs (CCircleFit::mainFit)",
                  "0x10002420": "_Pts (point tangents)", "0x10002af0": "_circle_test",
                  "0x10002620 / 0x100028a0": "_fit_ccw (reversed copy)", "0x100019e0": "_mainfit",
                  "0x100020b0": "_biarc_dll", "0x10003100 / 0x10002eb0": "_arc_err",
                  "0x10002680": "_mainfit (straight run)",
                  "0x1000c050 / 0x1000c060 / 0x1000c068": "ERR_ / ANG_ / RMAX_CIRCLEFIT"},
    "parity": ["circlefit"],
}

ERR_CIRCLEFIT = 0.02          # ?Err_CircleFit@@3NA default (CircleFitDLL 0x1000c050)
ANG_CIRCLEFIT = math.pi / 10  # ?Ang_CircleFit@@3NA (0x1000c060; Dxf2Grp sets the same)
RMAX_CIRCLEFIT = 5000.0       # ?Rmax_CircleFit@@3NA (0x1000c068)
RADIUS_RATIO = 50.0           # 0x1000b100
DUP_EPS = 1e-6                # 0x1000b0f8


def _with_start(s: Segment, p: Point) -> Segment:
    # the same as mlaser.cam.cleanup._with_start (joining); fit_arcs makes its chain continuous
    return Line(p, s.end) if isinstance(s, Line) else Arc(p, s.end, s.center, s.ccw)


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
                res.append(("Ar", rec) if rev else rec)  # type: ignore[arg-type]
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
    return best[1]  # type: ignore[index]


def _records_to_segments(recs: list) -> list[Segment]:
    out: list[Segment] = []
    for r in recs:
        if r[0] == "L":
            if math.dist(r[1], r[2]) > 0:
                out.append(Line(r[1], r[2]))
        elif r[0] == "I":
            (cx, cy), rad = r[1], r[2]
            out.append(("I", (cx, cy), rad))  # type: ignore[arg-type]  # resolved by the caller
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


@port("0x10001010", "0x100011e0", source="CircleFitDLL", parity=("circlefit",))
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
    p = _Pts([q[0] for q in pts], [q[1] for q in pts])  # type: ignore[assignment]
    recs = _mainfit(p, 0, len(pts) - 1, fp)  # type: ignore[arg-type]
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
