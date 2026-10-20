"""CContoutSmooth: corner blending of a cut path before velocity planning.

Recovered from MotionCtrl.dll V1.3.22 (``newContourSmooth`` 0x10005e10, vtable
0x100196b0) and cross-checked against the v1.3.23 copy linked into
CADModule.dll (factory 0x100fa680, vtable 0x10123128; CInterpMrg::Interp
0x10105260 drives it).  Tags: (O) observed in code, (I) inferred, (U) unknown.
See docs/motion.md section 9 for the full write-up.

Pipeline of ``load`` (MotionCtrl vt[2] 0x10005f70; CADModule vt[2] 0x100ff000):

1. ``tol`` is clamped to [0.01, 0.3] (O, 0x10019900 / 0x100199e0).
2. Pre-pass FUN_1000a3c0(tol) (O):
   * primitives shorter than 0.005 mm are dropped (markers excepted);
   * arcs with sweep < 30 deg whose sagitta r(1-cos(sweep/2)) < 0.01 become lines;
   * an arc between two lines (direction dot >= 0.05) whose sagitta is
     <= (acos(dot)/pi + 0.5)*tol is replaced by the sharp corner (the two lines
     are extended to their intersection, if it lies within the arc chord box),
     so small fillets are re-blended by the smoother.
3. FUN_100099d0 (O): consecutive collinear lines (dot > 0, |v1 x v2| <
   0.001*L1 and < 0.001*L2) and consecutive co-circular same-direction arcs
   (centre and radius within 0.001) are merged.  A type-4 primitive (marker
   after a line-only lead-in) is removed and the joint before it is remembered:
   that joint is blended with ``params.lead_tol`` (+0x18) instead of ``tol``.
4. FUN_1000ab70(prims, 0.1, 2.0) (O): runs of short lines (length <= 2 mm,
   turn with dot >= 0 and |cross| <= sin 30 deg) are collected as points and
   replaced by one cubic spline (FUN_1000a970 -> FUN_1000d0a0: polyline
   reduction at 3*0.1 mm, FUN_1000e230, then ISpline2DAnalyer interpolation of
   order 4).  Here: Douglas-Peucker at 0.3 mm + chord-length global cubic
   interpolation (I: the reduction and fit algorithms are not decoded).
5. Corner sizing per joint (O, x87 code 0x100063df..0x1000674d), with e = unit
   end tangent of the previous primitive, s = unit start tangent of the next,
   c = -(e.s) and K = -0.984375 (turn angle ~10.15 deg):
   * no blend (the path stops there) if tol_j < 1e-5, or |e x s| < 0.34 and
     c > 0 (a cusp, turn > ~160 deg);
   * L0 = 2*sqrt(2)*tol_j / sqrt(1 + max(c, K)) = 2*tol_j / sin(turn/2): the leg
     length for which the blend apex deviates exactly ``tol_j`` from the corner;
   * V = L0 for turns >= 90 deg; V = L0*P4/8 for turns <= 10.15 deg; linear in
     the turn angle in between: V = L0*(1 - (1 - P4/8)*(acos(c) - pi/2)/(acos(K) - pi/2));
   * on each side d = min(V, primitive length, r*P1*pi/180 for arcs),
     extent kd = k*d with k = P2 (+0x28) on lines, P3 (+0x30) on arcs/splines.
6. Per primitive (O, 0x100067a2..): if kd_start + kd_end > L both are scaled to
   fit and d = kd/f with f = 1.08 (+0.42*(dsum/L - 0.5) when dsum < 2L); if the
   remaining middle is < 0.1 mm it is absorbed into the larger extent.
7. Arc -> line speed factor (O, Arc2SegVelK): when an arc is followed by a line
   with dot(t_arc(L-kd), t_line) > 0.8, the blend gets factor
   w = (1-a)K_Y + a K_X + (1-dot)(1-w)/0.2 clamped to [0, 1],
   a = |t.x|/(|t.x|+|t.y|).
8. Pieces (O): every primitive trimmed to [kd_start, L-kd_end] (line pieces
   type 4, arc pieces type 3, spline pieces), and between two primitives a
   cubic clamped uniform B-spline blend (FUN_10007590, type 2) whose control
   polygon is A-side + [corner vertex] + B-side:
   * line side:   P(kd) and the point at distance d from the corner;
   * arc side:    P(kd), P(kd) + h t, Q - h T, P(d) where Q is at
     0.667*(kd-d) along the arc and h = 1.0667 r tan(theta/2) with
     theta = 0.667*(kd-d)/(2r) (a Bezier handle);
   * spline side: P(kd), P(kd) + t*min(d, kd-d)*c (c = 0.25 if the other side
     is a line, else 0.5), P(d).
   For a symmetric line/line corner the curve at the interior knot is
   V + d(t_B - t_A)/4, i.e. exactly ``tol`` from the corner.  Blends shorter
   than 1e-4 are dropped; arc/arc joints with dot > cos 5 deg get a radius
   floor min(rA, rB) (+0x110).  A joint without a blend is a sentinel (this+0x40).
9. Nodes (CADModule vt[5] 0x100fc150 + sampler 0x100f72a0) (O): line pieces
   {R=10000} and arc pieces {R=r} at s = 0, L/2, L; spline pieces: curvature
   radius samples (merged closer than 0.001 mm, keeping the min), pairs below
   0.05 thinned, R >= 0.001; blend pieces contribute only their minimum-radius
   interior node and their end node.  At a sentinel the neighbouring end nodes
   get R = 0 (full stop).  The last node of each piece carries the piece factor.
   Output node = {radius, s, factor}; CInterpMrg turns it into a planner point
   s = node[1], R = node[0], factor = node[2], feed = CInterpMrg+0xa0.
10. ``point_at(s)`` (vt[8] 0x100f9fb0): arc length on the smoothed path -> XY.
"""
from __future__ import annotations

import bisect
import math
import struct
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from ...model import Arc, Line, Point
from .. import crtmath as _crt
from .curvetable import CurveTable
from .._registry import port

__port__ = {
    "source": "CADModule",
    "class": "CContoutSmooth v1.3.23 (factory 0x100fa680, vtable 0x10123128); MotionCtrl V1.3.22 newContourSmooth 0x10005e10",
    "functions": {
        "0x100ff000": "ContourSmoother.load (vt[2])", "0x100fc150": "ContourSmoother.nodes (vt[5])",
        "0x100f9fb0": "ContourSmoother.point_at (vt[8])", "FUN_1000a3c0": "pre-pass (short / flat arcs)",
        "FUN_100099d0": "merge collinear / co-circular", "FUN_1000ab70": "short-line runs -> spline",
        "FUN_10007590": "blend B-spline", "0x10105260": "smooth_path_points (CInterpMrg::Interp load)",
    },
    "parity": ["jobcad"],
}

try:  # numpy is available in the venv; only needed by the short-line fit
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None  # type: ignore[assignment]

__all__ = ["SmoothParams", "ContourSmoother", "Node", "smooth_nodes", "smooth_path_points",
           "SmoothedPath", "R_LINE"]

R_LINE = 10000.0          # radius of line pieces (CADModule DAT_1011f2a0)
K_CORNER = -0.984375      # DAT_100199d8: turn angle below which the shallow factor applies
CUSP_CROSS = 0.34         # DAT_100199d0
TOL_MIN, TOL_MAX = 0.01, 0.3      # load clamps (DAT_10019900 / DAT_100199e0)
MIN_BLEND_D = 1e-5        # DAT_10019500
MIN_PIECE_LEN = 1e-4      # DAT_100198f8
ARC_HANDLE = 1.0666666666666667   # DAT_10019978
ARC_STEP = 0.667          # DAT_10019980
ARC_ARC_COS = math.cos(math.radians(5.0))   # 0x1001c1a0 * 0x10019808
# B-side sharp-turn threshold: cos(DAT_1001c198 * DAT_100198b0) = cos(57.2958*10 rad) ~ 0.3736
# (observed; looks like an intended cos(10 deg)): below it the B side of a blend onto an arc or
# spline uses the sharp variant of FUN_10007590 (0x1000787f).
SHARP_COS = math.cos(57.29577951308232 * 10.0)
SHORT_LINE = 2.0          # FUN_1000ab70 param_2
FIT_TOL = 0.1 * 3.0       # FUN_1000ab70 param_1 (0.1) * DAT_100197e0 (3.0)


@dataclass
class SmoothParams:
    """CContoutSmooth parameter block (this+0x18..0x3f, setter vt[1] 0x1000a890).

    Defaults are the constructor values (0x10005e10 / CADModule 0x100fa680).
    Meanings are recovered from their single use in load (0x10005f70).
    """
    lead_tol: float = 0.001      # +0x18 double [0, 0.1]: corner tolerance at the lead-in/contour joint
    arc_limit_deg: int = 37      # +0x20 int [7, 30]: blend leg on an arc <= r * this (deg -> rad);
    #                              the ctor default 37 lies outside the setter clamp (O)
    line_ext: float = 4.0        # +0x28 double [2, 8]: blend extent kd = k*d on lines
    arc_ext: float = 2.2         # +0x30 double [2.2, 4]: blend extent factor on arcs / splines
    shallow: float = 1.2         # +0x38 double [1, 8]: shallow-corner leg factor, V = L0*shallow/8

    def clamped(self) -> "SmoothParams":
        """Apply the setter clamps (vt[1] 0x1000a890)."""
        return SmoothParams(
            lead_tol=min(max(self.lead_tol, 0.0), 0.1),
            arc_limit_deg=int(min(max(int(self.arc_limit_deg), 7), 30)),
            line_ext=min(max(self.line_ext, 2.0), 8.0),
            arc_ext=min(max(self.arc_ext, 2.2), 4.0),
            shallow=min(max(self.shallow, 1.0), 8.0),
        )


@dataclass
class Node:
    """Smoother output node {radius, s, factor} (24 bytes in the original)."""
    radius: float
    s: float
    factor: float = 1.0

    def __iter__(self):
        return iter((self.radius, self.s, self.factor))


# ---------------------------------------------------------------------------
# vector helpers
def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def _mul(a, k):
    return (a[0] * k, a[1] * k)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _unit(a):
    n = math.hypot(a[0], a[1])
    return (a[0] / n, a[1] / n) if n > 0 else (1.0, 0.0)


# ---------------------------------------------------------------------------
# geometry pieces (arc-length parametrised)
class _Geom:
    kind = ""
    length = 0.0

    def point(self, s: float) -> Point:
        raise NotImplementedError

    def tangent(self, s: float) -> Point:
        raise NotImplementedError

    def sub(self, s0: float, s1: float) -> "_Geom":
        raise NotImplementedError


class _LineG(_Geom):
    """Line primitive; point/tangent as MotionCtrl FUN_10007b80 / FUN_10007cf0 compute them
    (f = s/L, P = p0 + (p1 - p0) f; T = (p1 - p0) / L with L = hypot)."""
    kind = "line"

    def __init__(self, p0: Point, p1: Point):
        self.p0, self.p1 = tuple(p0), tuple(p1)
        self.length = _crt.hypot(p1[0] - p0[0], p1[1] - p0[1])
        inv = 1.0 / self.length if self.length > 0 else 0.0
        self.dir = ((p1[0] - p0[0]) * inv, inv * (-p0[1] + p1[1])) if self.length > 0 else (1.0, 0.0)

    def point(self, s):
        if self.length <= 0:
            return self.p0
        f = s / self.length
        return (self.p0[0] + (-self.p0[0] + self.p1[0]) * f, f * (self.p1[1] + -self.p0[1]) + self.p0[1])

    def tangent(self, s):
        return self.dir

    def sub(self, s0, s1):
        return _LineG(self.point(s0), self.point(s1))


class _ArcG(_Geom):
    kind = "arc"

    def __init__(self, center: Point, r: float, a0: float, sweep: float):
        self.c, self.r, self.a0, self.sw = tuple(center), r, a0, sweep
        self.length = abs(sweep) * r

    @classmethod
    def from_arc(cls, a: Arc) -> "_ArcG":
        a0 = math.atan2(a.start[1] - a.center[1], a.start[0] - a.center[0])
        return cls(a.center, a.radius, a0, a.sweep())

    def _ang(self, s):
        return self.a0 + math.copysign(s / self.r, self.sw) if self.r > 0 else self.a0

    def point(self, s):
        a = self._ang(s)
        return (self.c[0] + self.r * math.cos(a), self.c[1] + self.r * math.sin(a))

    def tangent(self, s):
        a = self._ang(s)
        t = (-math.sin(a), math.cos(a))
        return t if self.sw > 0 else (-t[0], -t[1])

    def sub(self, s0, s1):
        return _ArcG(self.c, self.r, self._ang(s0), math.copysign((s1 - s0) / self.r, self.sw))

    @property
    def ccw(self):
        return self.sw > 0


class _TableG(_Geom):
    """A spline primitive (type 3) of CAD's smoother: the glyph's own curve as a sample table
    (piece built by FUN_100f92b0(piece, spline, glyph length, 0) -> FUN_100f83c0) with the
    primitive's window [f0, f1] (+0xc0/+0xc8 of the 0xd0 record, fractions of the glyph in
    travel order, FUN_1006f2d0).  Element length +8 = piece length (u1 - u0) * table length
    (FUN_100f6770); point / tangent at distance s go through the piece fraction s / length
    (FUN_100f9430 / FUN_100f95b0 -> FUN_100f5ac0 / FUN_100f5890), O."""
    kind = "spline"

    def __init__(self, tab: CurveTable, prim_len: Optional[float] = None):
        self.tab = tab
        self.length = tab.length
        # the 0xd0 primitive's own length (f1 - f0) * glyph length (FUN_1006f2d0): the load takes
        # the element's end tangent +0x128 at distance prim_len, i.e. at piece fraction
        # prim_len / piece length (FUN_100f95b0 -> FUN_100f5890), which is not 1 because the
        # table is longer than the glyph's polyline (O, 0x100ff000)
        self.prim_len = self.length if prim_len is None else prim_len

    def end_tangent(self):
        return self.tab.tangent_f(self.prim_len / self.length)

    def point(self, s):
        return self.tab.point_f(s / self.length)

    def tangent(self, s):
        return self.tab.tangent_f(s / self.length)

    def point_f(self, f):
        return self.tab.point_f(f)

    def tangent_f(self, f):
        return self.tab.tangent_f(f)


def glyph_table(curve, table_cls: type = CurveTable) -> Optional[CurveTable]:
    """Sample table of a spline glyph (``cam.geometry.SplineCurve``) in travel order, cached on
    the curve.  A backwards glyph is reversed first as FUN_10036cd0 does it: control points in
    reverse order, knots k'[i] = k[last] - k[n-1-i] (O).  ``table_cls``: the CurveTable
    implementation (the port, or the C-backed one of profiles/native_smooth.py, which keeps
    its own cache attribute)."""
    attr = getattr(table_cls, "CACHE_ATTR", "_smooth_table")
    tab = getattr(curve, attr, None)
    if tab is not None:
        return tab
    ctrl, knots = list(curve.ctrl), list(curve.knots)
    if len(ctrl) < 4 or len(knots) != len(ctrl) + 4:
        return None
    if getattr(curve, "dir", 1) == -1:
        ctrl = ctrl[::-1]
        kl = knots[-1]
        knots = [kl - knots[len(knots) - 1 - i] for i in range(len(knots))]
    tab = table_cls(ctrl, knots, 0.0, 1.0, ref=float(curve.length))
    try:
        setattr(curve, attr, tab)
    except Exception:  # pragma: no cover
        pass
    return tab


class _SplineG(_Geom):
    """Clamped cubic B-spline with an arc-length table; optional [s_lo, s_hi] window."""
    kind = "spline"

    def __init__(self, ctrl: Sequence[Point], knots: Optional[Sequence[float]] = None,
                 degree: int = 3, _table=None, window: Optional[tuple[float, float]] = None):
        self.ctrl = [tuple(p) for p in ctrl]
        self.p = min(degree, len(self.ctrl) - 1)
        self.knots = list(knots) if knots is not None else _clamped_uniform(len(self.ctrl), self.p)
        self._table = _table or self._build_table()
        full = self._table[1][-1]
        self.lo, self.hi = window if window else (0.0, full)  # type: ignore[var-annotated]
        self.length = self.hi - self.lo

    # -- de Boor ----------------------------------------------------------
    def _span(self, u):
        t, p, n = self.knots, self.p, len(self.ctrl) - 1
        if u >= t[n + 1]:
            return n
        if u <= t[p]:
            return p
        lo, hi = p, n + 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if u < t[mid]:
                hi = mid
            else:
                lo = mid
        return lo

    def _eval(self, u, ctrl=None, knots=None, p=None):
        ctrl = ctrl if ctrl is not None else self.ctrl
        t = knots if knots is not None else self.knots
        p = self.p if p is None else p
        n = len(ctrl) - 1
        if p == 0 or n < 0:
            return ctrl[min(max(int(u * n), 0), n)] if ctrl else (0.0, 0.0)
        # span in this knot vector
        if u >= t[n + 1]:
            k = n
        elif u <= t[p]:
            k = p
        else:
            k = bisect.bisect_right(t, u) - 1
            k = min(max(k, p), n)
        d = [ctrl[j + k - p] for j in range(p + 1)]
        for r in range(1, p + 1):
            for j in range(p, r - 1, -1):
                i = j + k - p
                den = t[i + p - r + 1] - t[i]
                a = 0.0 if den == 0 else (u - t[i]) / den
                d[j] = ((1 - a) * d[j - 1][0] + a * d[j][0], (1 - a) * d[j - 1][1] + a * d[j][1])
        return d[p]

    def _deriv_ctrl(self, ctrl, knots, p):
        out = []
        for i in range(len(ctrl) - 1):
            den = knots[i + p + 1] - knots[i + 1]
            k = p / den if den > 0 else 0.0
            out.append(((ctrl[i + 1][0] - ctrl[i][0]) * k, (ctrl[i + 1][1] - ctrl[i][1]) * k))
        return out, knots[1:-1], p - 1

    def derivs(self, u):
        c1, k1, p1 = self._deriv_ctrl(self.ctrl, self.knots, self.p)
        d1 = self._eval(u, c1, k1, p1) if p1 >= 0 and c1 else (0.0, 0.0)
        if p1 >= 1 and len(c1) > 1:
            c2, k2, p2 = self._deriv_ctrl(c1, k1, p1)
            d2 = self._eval(u, c2, k2, p2) if c2 else (0.0, 0.0)
        else:
            d2 = (0.0, 0.0)
        return d1, d2

    def _build_table(self, per_span: int = 24):
        t = self.knots
        u0, u1 = t[self.p], t[len(self.ctrl)]
        spans = max(1, len(set(t[self.p:len(self.ctrl) + 1])) - 1)
        n = max(32, per_span * spans)
        us = [u0 + (u1 - u0) * i / n for i in range(n + 1)]
        pts = [self._eval(u) for u in us]
        ss = [0.0]
        for a, b in zip(pts, pts[1:]):
            ss.append(ss[-1] + math.dist(a, b))
        return us, ss

    def u_at(self, s):
        us, ss = self._table
        s = min(max(s + self.lo, 0.0), ss[-1])
        i = min(max(bisect.bisect_right(ss, s) - 1, 0), len(ss) - 2)
        ds = ss[i + 1] - ss[i]
        f = 0.0 if ds <= 0 else (s - ss[i]) / ds
        return us[i] + (us[i + 1] - us[i]) * f

    def point(self, s):
        return self._eval(self.u_at(s))

    def tangent(self, s):
        d1, _ = self.derivs(self.u_at(s))
        return _unit(d1)

    def radius(self, s):
        d1, d2 = self.derivs(self.u_at(s))
        v = math.hypot(*d1)
        k = abs(_cross(d1, d2))
        if v <= 0:
            return 0.0
        return math.inf if k <= 1e-15 else v ** 3 / k

    def sub(self, s0, s1):
        return _SplineG(self.ctrl, self.knots, self.p, self._table, (self.lo + s0, self.lo + s1))

    def is_straight(self, eps: float = 1e-9) -> bool:
        a, b = self.ctrl[0], self.ctrl[-1]
        L = math.dist(a, b)
        if L <= 0:
            return False
        dv = _unit(_sub(b, a))
        return all(abs(_cross(dv, _sub(q, a))) <= eps * max(1.0, L) for q in self.ctrl)


def _clamped_uniform(n: int, p: int = 3) -> list[float]:
    """Knots of FUN_10007590: p+1 zeros, interior i/(n-p), p+1 ones."""
    inner = n - p
    return [0.0] * (p + 1) + [i / inner for i in range(1, inner)] + [1.0] * (p + 1)


# ---------------------------------------------------------------------------
@dataclass
class _Prim:
    """One input primitive (the 0xd0-byte element / 0x168-byte work element)."""
    g: _Geom
    src: int = 0
    laser: bool = True
    feed: float = 0.0
    # corner sizing (work element +0xd8 d_start, +0xf0 kd_start, +0x120 d_end, +0x138 kd_end)
    d_s: float = 0.0
    kd_s: float = 0.0
    d_e: float = 0.0
    kd_e: float = 0.0
    factor_after: float = 1.0   # +0x160: factor of the blend that follows

    @property
    def kind(self):
        return self.g.kind

    @property
    def L(self):
        return self.g.length

    def p0(self):
        return self.g.point(0.0)

    def p1(self):
        return self.g.point(self.g.length)

    def t0(self):
        return self.g.tangent(0.0)

    def t1(self):
        if isinstance(self.g, _TableG):
            return self.g.end_tangent()
        return self.g.tangent(self.g.length)


@dataclass
class _Piece:
    g: Optional[_Geom]
    kind: str                    # 'line' (type 4), 'arc' (type 3), 'blend' (type 2), 'spline'
    src: tuple = ()
    laser: tuple = (True,)
    feed: float = 0.0
    factor: float = 1.0
    floor: float = 0.0           # +0x110 radius floor of arc/arc blends
    sentinel: bool = False

    @property
    def length(self):
        return self.g.length if self.g is not None else 0.0


# ---------------------------------------------------------------------------
@port("0x100fa680", "0x10005e10", source="CADModule", parity=("jobcad",))
class ContourSmoother:
    """Port of CContoutSmooth (IContourSmooth).

    Usage mirrors the vtable: ``set_params`` (vt[1]), ``load`` (vt[2]),
    ``run`` (CADModule vt[9]; MotionCtrl does everything in load), ``nodes``
    (CADModule vt[5]) and ``point_at`` (CADModule vt[8]).
    """

    #: the curve-table implementation of blend and spline pieces (a C-backed subclass in the
    #: native smoother, mlaser/parity_ports/profiles/native_smooth.py)
    table_cls: type = CurveTable

    def __init__(self, params: Optional[SmoothParams] = None):
        self.params = (params or SmoothParams())
        self._input: list = []
        self.prims: list[_Prim] = []
        self.pieces: list[_Piece] = []
        self._cum: list[float] = [0.0]
        self.tol = 0.05
        self.flag = True
        self.kx = self.ky = 1.0
        self.lead_index: Optional[int] = None
        self._done = False

    # -- vt[1] -------------------------------------------------------------
    def set_params(self, params: SmoothParams) -> None:
        self.params = params.clamped()

    # -- vt[2] -------------------------------------------------------------
    def load(self, segments: Iterable, tol: float = 0.05, flag: bool = True, kx: float = 1.0,
             ky: float = 1.0, lead_index: Optional[int] = None, lasers: Optional[Sequence[bool]] = None,
             feeds: Optional[Sequence[float]] = None) -> "ContourSmoother":
        """Load a path.

        ``segments``: model Line/Arc objects (or CutLine/CutArc-like objects
        with start/end[/center/ccw]) or a polyline (sequence of points).
        ``tol``: corner tolerance (CInterpMrg+0x50), clamped to [0.01, 0.3].
        ``flag``: CInterpMrg+0x4c; False = no corner blending (FUN_10009800).
        ``kx``/``ky``: Arc2SegVelK K_X/K_Y * 0.01.
        ``lead_index``: index of the first contour segment after a lead-in
        (the original's type-4 marker); the joint before it uses ``lead_tol``.
        """
        segs = list(segments)
        if segs and isinstance(segs[0], (tuple, list)) and len(segs[0]) == 2 and \
                not hasattr(segs[0], "start") and isinstance(segs[0][0], (int, float)):
            segs = [Line(tuple(a), tuple(b)) for a, b in zip(segs, segs[1:]) if math.dist(a, b) > 0]
        self._input = segs
        self._in_cum = None
        self.tol = min(max(float(tol), TOL_MIN), TOL_MAX)
        self.flag, self.kx, self.ky = bool(flag), float(kx), float(ky)
        self.lead_index = lead_index
        self._lasers = list(lasers) if lasers is not None else None
        self._feeds = list(feeds) if feeds is not None else None
        self._done = False
        return self

    # -- vt[9] -------------------------------------------------------------
    def run(self) -> "ContourSmoother":
        # CInterpMrg::Interp always pushes its block (+0xe0, ctor defaults 0.001/37/4/2.2/1.2)
        # through vt[1] before loading, so the clamped values are what is used (37 -> 30).
        prm = (self.params or SmoothParams()).clamped()
        prims, lead_joint = self._to_prims(self._input)
        if self.flag:
            prims = self._prepass_arcs(prims)
            prims, lead_joint = self._merge(prims, lead_joint)
            prims = self._fit_short_lines(prims)
            self._size_corners(prims, prm, lead_joint)
            self._adjust(prims)
            self._arc_line_factor(prims)
            self.pieces = self._build_pieces(prims)
        else:
            self.pieces = [_Piece(p.g, p.kind if p.kind != "spline" else "spline", (p.src,),
                                  (p.laser,), p.feed) for p in prims if p.L > 0]
        self.prims = prims
        self._cum = [0.0]
        for pc in self.pieces:
            self._cum.append(self._cum[-1] + pc.length)
        # computed once: _locate runs per sample (rebuilding this per call made compile O(samples x pieces))
        self._has_real = any(not pc.sentinel for pc in self.pieces)
        self._done = True
        return self

    def _ensure(self):
        if not self._done:
            self.run()

    # -- input -------------------------------------------------------------
    def _to_prims(self, segs):
        out: list[_Prim] = []
        lead_joint = None
        for i, s in enumerate(segs):
            laser = getattr(s, "laser", True) if self._lasers is None else self._lasers[i]
            feed = getattr(s, "feed", 0.0) if self._feeds is None else self._feeds[i]
            if self.lead_index is not None and i == self.lead_index:
                lead_joint = len(out)       # joint between out[-1] and the next primitive
            ref = getattr(s, "curve", None)
            fr = _ref_fractions(ref)
            if fr is not None:
                # a piece of a spline glyph (cam CutLine.curve): the original's primitive list has
                # ONE type-3 primitive per glyph run, the glyph's curve with the window [f0, f1]
                # (FUN_1006f2d0); consecutive pieces of the same run extend the window
                q = out[-1] if out else None
                if q is not None and q.kind == "spline" and getattr(q, "_curve", None) is ref.curve \
                        and abs(q._f[1] - fr[0]) <= 1e-9 and (lead_joint is None or lead_joint != len(out)) \
                        and not (q.feed and feed and q.feed != feed):
                    q._f = (q._f[0], fr[1])
                    q.g = _TableG(q._tab.windowed(*q._f), (q._f[1] - q._f[0]) * ref.curve.length)
                    q.laser = q.laser and laser
                    continue
                tab = glyph_table(ref.curve, self.table_cls)
                if tab is not None:
                    p = _Prim(_TableG(tab.windowed(*fr), (fr[1] - fr[0]) * ref.curve.length), i, laser, feed)
                    p._curve, p._tab, p._f = ref.curve, tab, fr
                    out.append(p)
                    continue
            if getattr(s, "center", None) is not None:
                a = s if isinstance(s, Arc) else Arc(s.start, s.end, s.center, s.ccw)
                g = _ArcG.from_arc(a)
            else:
                g = _LineG(s.start, s.end)
            p = _Prim(g, i, laser, feed)
            # a micro-joint split (laser change only) is not a primitive boundary in the original:
            # its primitive list (FUN_1006f2d0) has whole glyphs and the joints are a separate
            # switch list, so the pieces are joined back here (laser_at gives the state)
            if out and out[-1].laser != laser and (lead_joint is None or lead_joint != len(out)) \
                    and _continues(out[-1], p):
                q = out[-1]
                q.g = _LineG(q.p0(), p.p1()) if p.kind == "line" else \
                    _ArcG(q.g.c, q.g.r, q.g.a0, q.g.sw + p.g.sw)
                continue
            out.append(p)
        return out, lead_joint

    # -- FUN_1000a3c0 --------------------------------------------------------
    def _prepass_arcs(self, prims: list[_Prim]) -> list[_Prim]:
        tol = self.tol
        prims = [p for p in prims if p.L >= 0.005]
        for p in prims:
            if p.kind == "arc":
                sw = abs(p.g.sw)  # type: ignore[attr-defined]
                if sw < math.radians(30.0) and p.g.r - math.cos(sw * 0.5) * p.g.r < 0.01:  # type: ignore[attr-defined]
                    p.g = _LineG(p.p0(), p.p1())
        i = len(prims) - 2
        while i > 0:
            a, arc, b = prims[i - 1], prims[i], prims[i + 1]
            if arc.kind == "arc" and a.kind == "line" and b.kind == "line":
                da, db = a.g.dir, b.g.dir  # type: ignore[attr-defined]
                dot = _dot(da, db)
                if dot >= 0.05:
                    chord = math.dist(a.p1(), b.p0())
                    sag = arc.g.r - math.cos(abs(arc.g.sw) * 0.5) * arc.g.r  # type: ignore[attr-defined]
                    if sag <= (math.acos(min(1.0, dot)) * 0.5 / (math.pi / 2) + 0.5) * tol:
                        cr = _cross(da, db)
                        if abs(cr) >= 1e-5:
                            # intersection of line a (through a.p1, dir da) and line b (through b.p0)
                            q = b.p0()
                            t = _cross(_sub(q, a.p1()), db) / cr
                            X = _add(a.p1(), _mul(da, t))
                        else:
                            X = _mul(_add(a.p1(), b.p0()), 0.5)
                        if all(abs(u - v) <= chord for u, v in zip(a.p1(), X)) and \
                                all(abs(u - v) <= chord for u, v in zip(b.p0(), X)):
                            a.g = _LineG(a.p0(), X)
                            b.g = _LineG(X, b.p1())
                            del prims[i]
            i -= 1
        return prims

    # -- FUN_100099d0 --------------------------------------------------------
    def _merge(self, prims: list[_Prim], lead_joint):
        out: list[_Prim] = []
        for idx, p in enumerate(prims):
            if out and (lead_joint is None or idx != lead_joint) and self._mergeable(out[-1], p):
                q = out[-1]
                if p.kind == "line":
                    q.g = _LineG(q.p0(), p.p1())
                else:
                    q.g = _ArcG(q.g.c, q.g.r, q.g.a0, q.g.sw + p.g.sw)  # type: ignore[attr-defined]
                q.laser = q.laser and p.laser
                if lead_joint is not None and idx < lead_joint:
                    lead_joint -= 1
                continue
            if lead_joint is not None and idx == lead_joint:
                lead_joint = len(out)
            out.append(p)
        return out, lead_joint

    @staticmethod
    def _mergeable(a: _Prim, b: _Prim) -> bool:
        # (O) FUN_100099d0 compares geometry only: the original's primitives carry no laser state
        # (micro-joints are switch positions on the side, see laser_at).  Different feeds
        # (rebuild-only: compile_items splits slow start/end runs) are kept apart.
        if a.kind != b.kind or (a.feed and b.feed and a.feed != b.feed):
            return False
        if a.kind == "line":
            v1, v2 = _sub(a.p1(), a.p0()), _sub(b.p1(), b.p0())
            c = abs(_cross(v2, v1))
            return _dot(v1, v2) > 0 and c < b.L * 0.001 and c < a.L * 0.001
        if a.kind == "arc":
            # no limit on the merged sweep: a full circle plus its overcut becomes one arc (O)
            return (abs(a.g.c[0] - b.g.c[0]) <= 0.001 and abs(a.g.c[1] - b.g.c[1]) <= 0.001  # type: ignore[attr-defined]
                    and abs(a.g.r - b.g.r) < 0.001 and a.g.sw * b.g.sw > 0)  # type: ignore[attr-defined]
        return False

    # -- FUN_1000ab70 --------------------------------------------------------
    def _fit_short_lines(self, prims: list[_Prim]) -> list[_Prim]:
        sin30 = math.sin(math.radians(30.0))
        out: list[_Prim] = []
        i, n = 0, len(prims)
        while i < n:
            p = prims[i]
            if p.kind != "line" or p.L > SHORT_LINE:
                out.append(p)
                i += 1
                continue
            j = i + 1
            while j < n:
                q = prims[j]
                if q.kind != "line" or q.L > SHORT_LINE or (q.feed and p.feed and q.feed != p.feed):
                    break
                d0, d1 = prims[j - 1].g.dir, q.g.dir  # type: ignore[attr-defined]
                if _dot(d0, d1) < 0.0 or abs(_cross(d0, d1)) > sin30:
                    break
                j += 1
            run = prims[i:j]
            if len(run) >= 2:
                pts = [run[0].p0()] + [r.p1() for r in run]
                g = _fit_polyline(pts, FIT_TOL)
                if g is not None:
                    out.append(_Prim(g, run[0].src, p.laser, min(r.feed for r in run) if run[0].feed else 0.0))
                else:
                    out.extend(run)
            else:
                out.extend(run)
            i = j
        return out

    # -- corner sizing (0x100063df..) -----------------------------------------
    def _size_corners(self, prims: list[_Prim], prm: SmoothParams, lead_joint) -> None:
        n = len(prims)
        f8 = prm.shallow * 0.125
        accK = math.acos(K_CORNER)
        arc_lim = prm.arc_limit_deg * math.pi / 180.0
        for p in prims:
            p.d_s = p.kd_s = p.d_e = p.kd_e = 0.0
        for i in range(1, n):
            a, b = prims[i - 1], prims[i]
            tol = prm.lead_tol if (lead_joint is not None and i == lead_joint) else self.tol
            e, s = a.t1(), b.t0()
            c = -_dot(e, s)
            cr = _cross((-e[0], -e[1]), s)
            if tol < MIN_BLEND_D or (abs(cr) < CUSP_CROSS and c > 0.0):
                continue
            m = max(c, K_CORNER)
            L0 = tol * 2.0 * math.sqrt(2.0) / math.sqrt(1.0 + m)
            if c <= K_CORNER:
                V = L0 * f8
            elif c >= 0.0:
                V = L0
            else:
                r = (math.acos(max(-1.0, min(1.0, c))) - math.pi / 2) / (accK - math.pi / 2)
                V = L0 * (1.0 - (1.0 - f8) * r)
            for side, prim in (("e", a), ("s", b)):
                k = prm.line_ext if prim.kind == "line" else prm.arc_ext
                lim = prim.L
                if prim.kind == "arc":
                    lim = min(lim, prim.g.r * arc_lim)  # type: ignore[attr-defined]
                d = min(lim, V)
                if side == "e":
                    a.d_e, a.kd_e = d, k * d
                else:
                    b.d_s, b.kd_s = d, k * d

    # -- per-primitive adjustment (0x100067a2..) ----------------------------
    @staticmethod
    def _adjust(prims: list[_Prim]) -> None:
        for p in prims:
            L = p.L
            total = p.kd_s + p.kd_e
            if L < total:
                sc = L / total
                p.kd_s *= sc
                p.kd_e *= sc
                dsum = p.d_s + p.d_e
                if L < 1.5 * dsum:
                    f = 1.08
                    if dsum * 0.5 < L:
                        f = (dsum / L - 0.5) * 0.42 + 1.08
                    p.d_s = p.kd_s / f
                    p.d_e = p.kd_e / f
            rem = L - p.kd_s - p.kd_e
            if rem < 0.1 and total > 0.001:
                if p.kd_s <= p.kd_e:
                    p.kd_e += rem
                else:
                    p.kd_s += rem

    # -- Arc2SegVelK (0x10006b31..) ----------------------------------------
    def _arc_line_factor(self, prims: list[_Prim]) -> None:
        for i in range(1, len(prims)):
            a, b = prims[i - 1], prims[i]
            a.factor_after = 1.0     # I: default of the work element's +0x160
            if a.kind == "arc" and b.kind == "line":
                ta = a.g.tangent(max(a.L - a.kd_e, 0.0))
                tb = b.g.tangent(min(b.kd_s, b.L))
                dot = _dot(ta, tb)
                if dot > 0.8:
                    den = abs(ta[0]) + abs(ta[1])
                    w0 = abs(ta[0]) / den if den > 0 else 0.5
                    w = (1.0 - w0) * self.ky + self.kx * w0
                    w = w - (dot - 1.0) * ((1.0 - w) / 0.19999999999999996)
                    a.factor_after = 1.0 if w >= 1.0 else (w if w >= 0.0 else 0.0)

    # -- pieces (0x10006908.. and FUN_10007590) ------------------------------
    def _build_pieces(self, prims: list[_Prim]) -> list[_Piece]:
        pieces: list[_Piece] = []
        n = len(prims)
        for i, p in enumerate(prims):
            if isinstance(p.g, _TableG):
                # the element's piece gets the window u0 + kd_s/total, u1 - kd_e/total (0x100ff000)
                t = p.g.tab
                g = t.windowed(p.kd_s / t.total + t.u0, t.u1 - p.kd_e / t.total)
                if g.length >= MIN_PIECE_LEN:
                    pieces.append(_Piece(g, p.kind, (p.src,), (p.laser,), p.feed))  # type: ignore[arg-type]
            elif p.L - p.kd_e - p.kd_s >= MIN_PIECE_LEN:
                g = p.g.sub(p.kd_s, p.L - p.kd_e)  # type: ignore[assignment]
                pieces.append(_Piece(g, p.kind, (p.src,), (p.laser,), p.feed))  # type: ignore[arg-type]
            if i + 1 < n:
                q = prims[i + 1]
                if p.d_e >= MIN_BLEND_D or q.d_s >= MIN_BLEND_D:
                    g = _blend(p, q, self.table_cls)  # type: ignore[assignment]
                    if g is not None and g.length >= MIN_PIECE_LEN:
                        floor = 0.0
                        if p.kind == "arc" and q.kind == "arc" and _dot(p.t1(), q.t0()) > ARC_ARC_COS:
                            floor = min(p.g.r, q.g.r)  # type: ignore[attr-defined]
                        feeds = [f for f in (p.feed, q.feed) if f]
                        pieces.append(_Piece(g, "blend", (p.src, q.src), (p.laser, q.laser),  # type: ignore[arg-type]
                                             min(feeds) if feeds else 0.0, p.factor_after, floor))
                else:
                    pieces.append(_Piece(None, "sentinel", (p.src, q.src), (p.laser, q.laser),
                                         sentinel=True))
        return pieces

    # -- outputs -------------------------------------------------------------
    @property
    def length(self) -> float:
        self._ensure()
        return self._cum[-1]

    def nodes(self) -> list[Node]:
        """CADModule vt[5] (0x100fc150): concatenated {radius, s, factor} nodes."""
        self._ensure()
        per = [(_piece_nodes(pc) if not pc.sentinel else None) for pc in self.pieces]
        # sentinels: zero radius at both neighbouring ends
        for k, pc in enumerate(self.pieces):
            if not pc.sentinel:
                continue
            for j in range(k + 1, len(per)):
                if per[j]:
                    per[j][0].radius = 0.0  # type: ignore[index]
                    break
            for j in range(k - 1, -1, -1):
                if per[j]:
                    per[j][-1].radius = 0.0  # type: ignore[index]
                    break
        out: list[Node] = []
        first = next((nl for nl in per if nl), None)
        if first is None:
            return out
        out.append(Node(first[0].radius, 0.0, 1.0))
        off = 0.0
        for pc, nl in zip(self.pieces, per):
            if not nl:
                continue
            if nl[0].radius < out[-1].radius:
                out[-1].radius = nl[0].radius
            seg_len = nl[-1].s
            if pc.kind == "blend":
                m = len(nl)
                if m > 1:
                    best = 1
                    for k in range(2, m - 1):
                        if nl[k].radius < nl[best].radius:
                            best = k
                    nd = nl[best]
                    out.append(Node(max(nd.radius, pc.floor), nd.s + off, nd.factor))
                if m > 2:
                    nd = nl[-1]
                    out.append(Node(max(nd.radius, pc.floor), nd.s + off, nd.factor))
            else:
                for nd in nl[1:]:
                    out.append(Node(nd.radius, nd.s + off, nd.factor))
            off += seg_len
        return out

    def point_at(self, s: float) -> Point:
        """CADModule vt[8] (0x100f9fb0): smoothed-path arc length -> XY."""
        pc, u = self._locate(s)
        return pc.g.point(u) if pc is not None else (0.0, 0.0)

    def laser_at(self, s: float) -> bool:
        """Laser state at smoothed arc length ``s``.  The original keeps the micro-joint switch
        list apart from the geometry and places it on the smoothed path by path fraction
        (CInterpMrg::Interp -> FUN_10102770(switch, smoothed length, fractions)); the same
        fraction of the input move chain gives the state here."""
        self._ensure()
        segs = self._input
        if not segs:
            return False
        if self._in_cum is None:
            cum = [0.0]
            for i, sg in enumerate(segs):
                if getattr(sg, "center", None) is not None:
                    a = sg if isinstance(sg, Arc) else Arc(sg.start, sg.end, sg.center, sg.ccw)
                    cum.append(cum[-1] + a.length())
                else:
                    cum.append(cum[-1] + math.dist(sg.start, sg.end))
            self._in_cum = cum  # type: ignore[assignment]
        cum = self._in_cum  # type: ignore[assignment]
        L = self._cum[-1]
        x = s * (cum[-1] / L) if L > 0 else 0.0
        i = min(max(bisect.bisect_right(cum, x) - 1, 0), len(segs) - 1)
        if self._lasers is not None:
            return bool(self._lasers[i])
        return bool(getattr(segs[i], "laser", True))

    def feed_at(self, s: float) -> float:
        pc, _ = self._locate(s)
        return pc.feed if pc is not None else 0.0

    def _locate(self, s: float):
        self._ensure()
        if not self._has_real:
            return None, 0.0
        i = bisect.bisect_right(self._cum, s) - 1
        i = min(max(i, 0), len(self.pieces) - 1)
        # skip sentinels (zero length)
        while i < len(self.pieces) and self.pieces[i].sentinel:
            i += 1
        if i >= len(self.pieces):
            i = len(self.pieces) - 1
            while self.pieces[i].sentinel:
                i -= 1
        pc = self.pieces[i]
        return pc, min(max(s - self._cum[i], 0.0), pc.length)


# ---------------------------------------------------------------------------
def _ref_fractions(ref) -> Optional[tuple[float, float]]:
    """Window [f0, f1] of a cut piece on its spline glyph: travel distances / glyph length."""
    if ref is None:
        return None
    t0, t1 = getattr(ref, "t0", None), getattr(ref, "t1", None)
    L = getattr(ref.curve, "length", 0.0)
    if t0 is None or t1 is None or not L or t1 <= t0:
        return None
    return (t0 / L, t1 / L)


def _continues(a: _Prim, b: _Prim) -> bool:
    """``b`` continues ``a`` on the same line / circle (a split of one glyph)."""
    if a.kind != b.kind or (a.feed and b.feed and a.feed != b.feed):
        return False
    if math.dist(a.p1(), b.p0()) > 1e-6:
        return False
    if a.kind == "line":
        v1, v2 = _sub(a.p1(), a.p0()), _sub(b.p1(), b.p0())
        return _dot(v1, v2) > 0 and abs(_cross(v1, v2)) <= 1e-9 * max(1.0, a.L * b.L)
    return (math.dist(a.g.c, b.g.c) <= 1e-6 and abs(a.g.r - b.g.r) <= 1e-6 and a.g.sw * b.g.sw > 0)  # type: ignore[attr-defined]


def _blend(a: _Prim, b: _Prim, table_cls: type = CurveTable) -> Optional[_SplineG]:
    """FUN_10007590: control polygon A-side + [vertex] + B-side, clamped uniform cubic."""
    V = b.p0()
    tab_a = isinstance(a.g, _TableG)
    if tab_a:
        # spline element: +0x140 / +0x150 = FUN_100f5ac0 / FUN_100f5890(piece, 1 - kd_e / L)
        fa = 1.0 - a.kd_e / a.L
        A: list[Point] = [a.g.point_f(fa)]  # type: ignore[attr-defined]
    else:
        A = [a.g.point(a.L - a.kd_e)]
    if a.kind == "line":
        A.append(_sub(a.p1(), _mul(a.t1(), a.d_e)))
    elif a.kind == "arc":
        A += _arc_handles(a, False)
        A.append(a.g.point(a.L - a.d_e))
    elif tab_a:   # spline primitive (FUN_100fa720): (T m) c + P, then P(L - d_e)
        c = 0.25 if b.kind == "line" else 0.5
        m = a.kd_e - a.d_e
        if not m <= a.d_e:
            m = a.d_e
        T = a.g.tangent_f(fa)  # type: ignore[attr-defined]
        A.append((T[0] * m * c + A[0][0], A[0][1] + T[1] * m * c))
        A.append(a.g.point(a.L - a.d_e))
    else:   # fitted short-line spline
        c = 0.25 if b.kind == "line" else 0.5
        m = min(a.d_e, a.kd_e - a.d_e)
        s0 = a.L - a.kd_e
        A.append(_add(A[0], _mul(a.g.tangent(s0), m * c)))
        A.append(a.g.point(a.L - a.d_e))
    B: list[Point] = []
    PB = b.g.point(b.kd_s)
    TB = b.g.tangent(b.kd_s)
    if b.kind == "line":
        B.append(_add(b.p0(), _mul(b.t0(), b.d_s)))
    else:
        ta, tb = a.t1(), b.t0()
        if ta[1] * tb[1] + tb[0] * ta[0] <= SHARP_COS:
            # sharp turn (0x1000787f): which point comes first depends on the turn side and on
            # the side the arc curves to
            cr = -ta[0] * tb[1] - tb[0] * -ta[1]
            sharp_near = 0.0 < cr
            if TB[1] * tb[0] - tb[1] * TB[0] <= 0.0:
                sharp_near = cr <= 0.0
            if sharp_near:
                B.append(_sub(PB, _mul(TB, b.d_s)))
            else:
                B.append(b.g.point(b.d_s))
                if b.kind == "arc":
                    B += _arc_handles(b, True)
                else:
                    m = min(b.kd_s - b.d_s, b.d_s) * 0.5
                    B.append(_sub(PB, _mul(TB, m)))
        else:
            B.append(b.g.point(b.d_s))
            if b.kind == "arc":
                B += _arc_handles(b, True)
            else:
                B.append(_sub(PB, _mul(TB, (b.kd_s - b.d_s) * 0.5)))
    B.append(PB)
    ctrl = A + [V] + B
    # drop exact duplicates that would make zero-length control legs harmless anyway
    if max(math.dist(ctrl[0], q) for q in ctrl) < 1e-12:
        return None
    # the blend piece is the analyser's sample table (CADModule FUN_100f83c0, curvetable.py)
    return blend_table(table_cls, ctrl, _clamped_uniform(len(ctrl), 3))


# ---------------------------------------------------------------------------
# value memo of blend tables (performance only): a CurveTable is a pure function of its
# control polygon and knots, and is never changed after it is built.  The compiler plans every
# cut chain twice, once for the job and once more, without the lead-in joint and the slow
# start / end split, for ``Job.estimate`` (mlaser.motion.interp.estimate_time); all blends but
# the lead-in joint's are the same tables.  Inside ``table_memo()`` (per thread) a table
# whose class and inputs are bit for bit equal to one built before is that same object.
_MEMO = threading.local()


@contextmanager
def table_memo():
    """Memoise blend tables by value (exact bits) for the ``with`` block, in this thread."""
    prev = getattr(_MEMO, "d", None)
    _MEMO.d = {}
    try:
        yield _MEMO.d
    finally:
        _MEMO.d = prev


def blend_table(table_cls: type, ctrl: list, knots: list) -> Any:
    """``table_cls(ctrl, knots)``, or the equal table built before inside :func:`table_memo`."""
    memo = getattr(_MEMO, "d", None)
    if memo is None:
        return table_cls(ctrl, knots)
    flat = [v for p in ctrl for v in p]
    flat += knots
    if not all(type(v) is float for v in flat):   # an int computes differently: no memo
        return table_cls(ctrl, knots)
    key = (table_cls, len(ctrl), struct.pack(f"<{len(flat)}d", *flat))
    tab = memo.get(key)
    if tab is None:
        tab = memo[key] = table_cls(ctrl, knots)
    return tab


def _arc_handles(p: _Prim, b_side: bool) -> list[Point]:
    """FUN_1000a240: two Bezier-handle control points along the arc."""
    r = p.g.r  # type: ignore[attr-defined]
    if b_side:
        delta = (p.kd_s - p.d_s) * ARC_STEP
        sQ = p.kd_s - delta
        s0 = p.kd_s
    else:
        delta = (p.kd_e - p.d_e) * ARC_STEP
        s0 = p.L - p.kd_e
        sQ = s0 + delta
    th = delta / r * 0.5 if r > 0 else 0.0
    h = r * ARC_HANDLE * (1.0 - math.cos(th)) / math.sin(th) if abs(th) > 1e-12 else 0.0
    P0, t0 = p.g.point(s0), p.g.tangent(s0)
    Q, T = p.g.point(sQ), p.g.tangent(sQ)
    if b_side:
        return [_add(Q, _mul(T, h)), _sub(P0, _mul(t0, h))]
    return [_add(P0, _mul(t0, h)), _sub(Q, _mul(T, h))]


def _piece_nodes(pc: _Piece) -> list[Node]:
    """Per-piece node sampler (CADModule FUN_100f72a0).

    Line pieces (type 4) give R = 10000 and arc pieces (type 3) R = r at s = 0, L/2, L.  Blend
    and spline pieces read their sample table (curvetable.CurveTable.nodes: trend points
    FUN_100f5ce0/FUN_100f5f10, node selection FUN_100f6090 with a 3 mm gap, end radii
    FUN_100f6840, thinning of radii < 0.05 and the 0.001 floor).  The floor skips node 0 (O)."""
    g = pc.g
    L = pc.length
    if isinstance(g, CurveTable):
        nl = [Node(r, s_) for r, s_, _f in g.nodes()]
    else:
        if pc.kind in ("line", "arc") or (isinstance(g, _SplineG) and g.is_straight()):
            R = g.r if pc.kind == "arc" else R_LINE  # type: ignore[union-attr]
            nl = [Node(R, 0.0), Node(R, L * 0.5), Node(R, L)]
        else:
            nl = _spline_nodes(g, L)
        for nd in nl[1:]:
            if nd.radius < 0.001:
                nd.radius = 0.001
    nl[-1].factor = pc.factor
    return nl


def _spline_nodes(g, L: float) -> list[Node]:
    """Nodes of a fitted short-line spline (_SplineG; I: sampled every 0.02 mm, the original
    samples its own table as for blends)."""
    n = min(max(64, int(L / 0.02)), 800)
    nl: list[Node] = []
    for k in range(n + 1):
        s = L * k / n
        R = g.radius(s)
        if not math.isfinite(R):
            R = R_LINE
        R = min(R, R_LINE)
        if nl and s - nl[-1].s <= 0.001:
            nl[-1].radius = min(nl[-1].radius, R)
        else:
            nl.append(Node(R, s))
    if len(nl) == 1:
        nl.append(Node(nl[0].radius, L))
    nl[-1].s = L
    for k in range(2, len(nl)):
        if nl[k].radius < 0.05 and nl[k - 1].radius < 0.05:
            if nl[k].radius <= nl[k - 1].radius:
                nl[k - 1].radius = 0.05
            else:
                nl[k].radius = 0.05
    return nl


def _dp(pts: list[Point], tol: float) -> list[Point]:
    """Douglas-Peucker reduction (I: stands in for FUN_1000e230)."""
    if len(pts) < 3:
        return list(pts)
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        a, b = pts[i], pts[j]
        L = math.dist(a, b)
        best, bk = -1.0, -1
        for k in range(i + 1, j):
            if L > 0:
                d = abs(_cross(_sub(b, a), _sub(pts[k], a))) / L
            else:
                d = math.dist(a, pts[k])
            if d > best:
                best, bk = d, k
        if best > tol and bk > 0:
            keep[bk] = True
            stack += [(i, bk), (bk, j)]
    return [p for p, k in zip(pts, keep) if k]


def _fit_polyline(pts: list[Point], tol: float) -> Optional[_Geom]:
    """Short-line run -> one smooth piece (FUN_1000a970 / FUN_1000d0a0; method I)."""
    red = _dp(pts, tol)
    if len(red) == 2:
        return _LineG(red[0], red[1])
    if len(red) < 4 or _np is None:
        return None
    # global cubic interpolation, chord-length parameters, averaged knots (Piegl & Tiller 9.2.1)
    P = _np.asarray(red, float)
    d = _np.sqrt(((P[1:] - P[:-1]) ** 2).sum(1))
    if (d <= 0).any():
        return None
    u = _np.concatenate([[0.0], _np.cumsum(d) / d.sum()])
    n, p = len(P) - 1, 3
    knots = [0.0] * (p + 1) + [float(u[j:j + p].mean()) for j in range(1, n - p + 1)] + [1.0] * (p + 1)
    N = [[_basis(i, p, float(u[k]), knots, n) for i in range(n + 1)] for k in range(n + 1)]
    C = _solve(N, [(float(x), float(y)) for x, y in P])
    if C is None:
        return None
    return _SplineG(C, knots, 3)


def _solve(A: list[list[float]], B: list[tuple[float, float]]) -> Optional[list[tuple[float, float]]]:
    """``A X = B`` for the two columns of ``B`` (Gaussian elimination, partial pivoting), in
    Python floats with a fixed operation order, so every platform gets the same bits.

    It replaces ``numpy.linalg.solve``, whose LAPACK differs per platform: numpy's macOS arm64
    wheels use Apple's Accelerate, the Linux / Windows ones OpenBLAS, and the solutions differed
    in the last bits (17 golden records on macOS, ``tools/golden_libm.py``).  ``A`` is a B-spline
    collocation matrix (at most ``p + 1`` non-zeros per row), so only the non-zero span of each
    row is eliminated.  None when ``A`` is singular (a zero pivot)."""
    n = len(A)
    a = [list(r) for r in A]
    b = [list(r) for r in B]
    hi = [max((j for j, v in enumerate(r) if v != 0.0), default=-1) for r in a]   # last non-zero
    for k in range(n):
        piv = k
        for i in range(k + 1, n):
            if abs(a[i][k]) > abs(a[piv][k]):
                piv = i
        if a[piv][k] == 0.0 or a[piv][k] != a[piv][k]:
            return None
        if piv != k:
            a[k], a[piv] = a[piv], a[k]
            b[k], b[piv] = b[piv], b[k]
            hi[k], hi[piv] = hi[piv], hi[k]
        ak, bk, d = a[k], b[k], a[k][k]
        for i in range(k + 1, n):
            ai = a[i]
            if ai[k] == 0.0:
                continue
            f = ai[k] / d
            ai[k] = 0.0
            for j in range(k + 1, hi[k] + 1):
                ai[j] = ai[j] - f * ak[j]
            hi[i] = max(hi[i], hi[k])
            bi = b[i]
            bi[0] = bi[0] - f * bk[0]
            bi[1] = bi[1] - f * bk[1]
    x = [(0.0, 0.0)] * n
    for k in range(n - 1, -1, -1):
        ak = a[k]
        sx, sy = b[k]
        for j in range(k + 1, hi[k] + 1):
            if ak[j] != 0.0:
                sx = sx - ak[j] * x[j][0]
                sy = sy - ak[j] * x[j][1]
        x[k] = (sx / ak[k], sy / ak[k])
    if not all(math.isfinite(v) for xy in x for v in xy):
        return None
    return x


def _basis(i, p, u, t, n):
    """Cox-de Boor basis N_{i,p}(u); u == 1 belongs to the last basis."""
    if p == 0:
        if t[i] <= u < t[i + 1]:
            return 1.0
        return 1.0 if (u == t[-1] and i == n and t[i] < t[i + 1]) else 0.0
    a = 0.0
    den = t[i + p] - t[i]
    if den > 0:
        a = (u - t[i]) / den * _basis(i, p - 1, u, t, n)
    den2 = t[i + p + 1] - t[i + 1]
    if den2 > 0:
        a += (t[i + p + 1] - u) / den2 * _basis(i + 1, p - 1, u, t, n)
    return a


# ---------------------------------------------------------------------------
# planner-facing helpers
def smooth_nodes(segments, params: Optional[SmoothParams] = None, tol: float = 0.05,
                 flag: bool = True, kx: float = 1.0, ky: float = 1.0,
                 lead_index: Optional[int] = None) -> list[tuple[float, float, float]]:
    """Run the smoother and return [(s, R, factor), ...] (planner order: s first).

    ``s`` is arc length on the smoothed path (use ``ContourSmoother.point_at`` to
    map it back to XY).  CInterpMrg feeds these to CVelocityPlanning as
    ``PathPoint(s=node.s, radius=node.R, factor=node.factor, feed=<contour feed>)``.
    """
    sm = ContourSmoother(params).load(segments, tol, flag, kx, ky, lead_index).run()
    return [(n.s, n.radius, n.factor) for n in sm.nodes()]


class SmoothedPath:
    """Arc-length geometry of a smoothed move chain (drop-in for interp._PathGeom)."""

    def __init__(self, smoother: ContourSmoother):
        self.sm = smoother
        self.cum = [0.0, smoother.length]

    def at(self, s: float):
        return self.sm.point_at(s), self.sm.laser_at(s)


@port("0x10105260", source="CADModule", parity=("jobcad",), note="CInterpMrg::Interp -> CContoutSmooth -> planner points")
def smooth_path_points(moves, cfg=None, params: Optional[SmoothParams] = None,
                       lead_index: Optional[int] = None):
    """Planner input for a chain of CutLine/CutArc moves through CContoutSmooth.

    Returns ``(points, geom)``: ``points`` is a list of ``planner.PathPoint``,
    ``geom`` a :class:`SmoothedPath` whose ``at(s)`` gives ((x, y), laser) on
    the smoothed path.  ``cfg`` (an ``interp.MotionConfig``) supplies
    ``corner_tol`` (-> tol) and ``arc2seg_k`` (-> K_X, K_Y); optional attributes
    ``smooth_params`` (SmoothParams) and ``smooth`` (bool flag) are honoured.
    ``lead_index``: first contour move after a line lead-in (the original's type-4 marker);
    the joint before it is blended with ``SmoothParams.lead_tol``.
    """
    return run_smoother(ContourSmoother, SmoothedPath, moves, cfg, params, lead_index)


def run_smoother(smoother_cls: type, path_cls: type, moves, cfg=None, params: Optional[SmoothParams] = None,
                 lead_index: Optional[int] = None):
    """:func:`smooth_path_points` with the given smoother and path classes (the port's, or the
    C-backed subclasses of mlaser/parity_ports/profiles/native_smooth.py)."""
    from .planner import PathPoint   # local import: planner is not modified
    tol = getattr(cfg, "corner_tol", 0.05) if cfg is not None else 0.05
    kx, ky = getattr(cfg, "arc2seg_k", (1.0, 1.0)) if cfg is not None else (1.0, 1.0)
    params = params or (getattr(cfg, "smooth_params", None) if cfg is not None else None)
    flag = bool(getattr(cfg, "smooth", True)) if cfg is not None else True
    sm = smoother_cls(params).load(moves, tol, flag, kx, ky, lead_index).run()
    pts = []
    for nd in sm.nodes():
        feed = sm.feed_at(min(nd.s, sm.length)) or (moves[0].feed if moves else 1e9)
        pts.append(PathPoint(nd.s, nd.radius, feed, 1e9, nd.factor))
    # planner needs strictly increasing s
    out = []  # type: ignore[var-annotated]
    for p in pts:
        if out and p.s - out[-1].s <= 1e-9:
            out[-1].radius = min(out[-1].radius, p.radius)
            out[-1].factor = min(out[-1].factor, p.factor)
            continue
        out.append(p)
    return out, path_cls(sm)
