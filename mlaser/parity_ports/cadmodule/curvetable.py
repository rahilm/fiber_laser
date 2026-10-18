"""Curve pieces of CContoutSmooth: sample table, curvature nodes and arc length -> XY.

The original never evaluates a blend (or spline) piece directly.  When a curve piece is built,
CADModule FUN_100f83c0 asks the openNURBS-based ``ISpline2DAnalyer`` (splineAnalyerVc100.dll,
``CSpline2DAnalyer`` vt+0x30 = FUN_100bfe70) for a list of samples and turns it into a table of
0xa8-byte records (piece +0x84).  Everything downstream reads that table:

* the curvature nodes (CADModule vt[5] -> FUN_100f72a0 with FUN_100f5ce0 / FUN_100f5f10 /
  FUN_100f6090 / FUN_100f6840),
* arc length -> XY (vt[8] -> FUN_100f8b60 -> FUN_100f8180): the path between two samples is a
  chord or a circular arc (the record's arc flag +0x58), not the spline itself.

This module ports that chain operation by operation (O, addresses below).  The arithmetic order
of the original is kept because some decisions are borderline by construction: on a symmetric
90 deg corner each knot span turns by exactly pi/4, and FUN_100bf9c0 rounds turn/2deg + 0.5 =
23.0 down or up depending on the last bit (the original gets 24 steps on one span, 22 on the
other).  The x87 code runs with 53-bit precision control (MSVC default), so plain IEEE doubles
reproduce it, with openNURBS' own ``ON_3dVector::Length`` (max-component formula) and the C
runtime's ``acos``/``atan2``/``_hypot`` taken from ``crtmath`` (the reference's msvcr100, bit for
bit).  ``sin``/``cos`` (only in arc length -> XY) use the platform libm (I: last-bit effects).

Record layout (CAD, 0xa8 bytes), as used here (``_Rec``):
  +0x00 u (spline parameter), +0x08/+0x10 point, +0x18 s (arc length on the table),
  +0x20 1/|D1|^2, +0x28/+0x30 unit tangent, +0x38/+0x40 curvature vector K, +0x48 R (smoothed),
  +0x50 length of the interval ending here, +0x58 arc flag, +0x60/+0x68 arc centre, +0x70 arc
  radius, +0x78/+0x80 start/end angle, +0x88.. / +0x98.. interval end points.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

from .. import crtmath as _crt
from .._registry import port

__port__ = {
    "source": "CADModule",
    "functions": {
        "FUN_100f83c0": "CurveTable._build", "FUN_100f72a0": "CurveTable.nodes",
        "FUN_100f5ce0": "_trend_points", "FUN_100f5f10": "_prune_points", "FUN_100f6090": "_select_points",
        "FUN_100f6840": "_thin_and_floor", "FUN_100f8b60": "CurveTable.point", "FUN_100f8180": "CurveTable.point_f",
        "FUN_100bfe70": "analyser_samples (splineAnalyer CSpline2DAnalyer vt+0x30)",
        "FUN_100bf9c0": "_sample_span", "FUN_10097fc0": "on_length (ON_3dVector::Length)",
        "FUN_1004d1d0": "_ev_tangent (ON_Curve::EvTangent)", "FUN_10057270": "_bezier_eval",
    },
    "parity": ["jobcad"],
}

__all__ = ["CurveTable", "ms_hypot", "on_length", "nurbs_eval", "analyser_samples"]

DBL_MIN = 2.2250738585072014e-308       # splineAnalyer DAT_100e0d30
ANGLE_STEP = 0.017453292519943295 * 2.0  # FUN_100bfe70: DAT_100f0470 (deg->rad) * 2.0 (piece +0x118)
SAMPLE_TOL = 0.05                       # piece +0x120 (CNurbsContour ctor), FUN_100bf9c0 param_6
R_LINE = 10000.0                        # CAD DAT_1011f2a0


# ---------------------------------------------------------------------------------------------
# arithmetic of the original
def ms_hypot(x: float, y: float) -> float:
    """msvcr100 ``_hypot`` as the reference run sees it (crtmath.hypot, see there)."""
    return _crt.hypot(x, y)


def on_length(x: float, y: float, z: float = 0.0) -> float:
    """openNURBS ``ON_3dVector::Length`` (splineAnalyer FUN_10097fc0): m = largest |component|
    (y wins ties with x), len = m*sqrt((1 + (o2/m)^2) + (o1/m)^2) with 1/m formed first."""
    fx, fy, fz = abs(x), abs(y), abs(z)
    if fy >= fx and fy >= fz:
        m, o1, o2 = fy, fz, fx
    elif fz >= fx and fz >= fy:
        m, o1, o2 = fz, fx, fy
    else:
        m, o1, o2 = fx, fy, fz
    if m <= DBL_MIN:
        return m if (m > 0.0 and math.isfinite(m)) else 0.0
    r = 1.0 / m
    a, b = o2 * r, o1 * r
    return math.sqrt((1.0 + a * a) + b * b) * m


def _unitize(x: float, y: float):
    """``ON_3dVector::Unitize`` (FUN_10098080): v * (1/|v|)."""
    d = on_length(x, y)
    if d > DBL_MIN:
        r = 1.0 / d
        return x * r, y * r, True
    return 0.0, 0.0, False


def _acos_clamped(c: float) -> float:
    return _crt.acos(1.0 if c > 1.0 else (-1.0 if c < -1.0 else c))


# ---------------------------------------------------------------------------------------------
# openNURBS evaluation (splineAnalyer FUN_1007ccb0 -> FUN_100584d0 -> FUN_10058130)
def _basis(order: int, knot: Sequence[float], t: float) -> list[float]:
    """``ON_EvaluateNurbsBasis`` (FUN_10057820); ``knot`` is the span's local knot slice."""
    d = order - 1
    N = [0.0] * (order * order)
    if knot[d] == knot[d - 1]:
        return N
    N[order * order - 1] = 1.0
    idx = order * order - 1
    t_k = [0.0] * d
    k_t = [0.0] * d
    for j in range(d):
        t_k[j] = t - knot[d - 1 - j]
        k_t[j] = knot[d + j] - t
        prev, idx = idx, idx - (order + 1)
        x = 0.0
        for r in range(j + 1):
            a = t_k[j - r]
            b = k_t[r]
            y = N[prev + r] / (b + a)
            N[idx + r] = y * b + x
            x = a * y
        N[idx + j + 1] = x
    # rounding fix of openNURBS (0x100e0dd8 / 0x100e0dd0)
    lo, hi = 0.9999999850988388, 1.0000000149011612
    if N[0] <= lo:
        if lo < N[d] and N[d] != 1.0 and N[d] < hi and all(N[i] == 0.0 for i in range(d)):
            N[d] = 1.0
    elif N[0] != 1.0 and N[0] < hi and all(N[i] == 0.0 for i in range(1, d + 1)):
        N[0] = 1.0
    return N


def _basis_derivatives(order: int, knot: Sequence[float], der_count: int, N: list[float]) -> None:
    """``ON_EvaluateNurbsBasisDerivatives`` (FUN_10057c10), in place."""
    d = order - 1
    dk: list[list[float]] = [[]]          # dk[k] for k = 1..der_count (openNURBS' dk--)
    for k in range(der_count):
        j = d - k
        dk.append([1.0 / (knot[k + i + j] - knot[k + i]) for i in range(j)])
    a0 = [0.0] * order
    a1 = [0.0] * order
    for i in range(order):
        a0[0] = 1.0
        for k in range(1, der_count + 1):
            base = order * k
            dN = 0.0
            j = k - i
            if j <= 0:
                a1[0] = a0[0] * dk[k][i - k]
                dN = a1[0] * N[base + i]
                j = 1
            jmax = d - i
            if jmax < k:
                while j <= jmax:
                    a1[j] = (a0[j] - a0[j - 1]) * dk[k][i + j - k]
                    dN = a1[j] * N[base + i + j] + dN
                    j += 1
            else:
                while j < k:
                    a1[j] = (a0[j] - a0[j - 1]) * dk[k][i + j - k]
                    dN = a1[j] * N[base + i + j] + dN
                    j += 1
                a1[k] = -(dk[k][i] * a0[k - 1])
                dN = a1[k] * N[base + i + k] + dN
            N[base + i] = dN
            a0, a1 = a1, a0
    c = dn = float(d)
    p = order
    for _ in range(der_count):
        for i in range(order):
            N[p + i] = N[p + i] * c
        p += order
        dn -= 1.0
        c = c * dn
    return None


class _Nurbs:
    """Non-rational cubic NURBS as ``CSpline2DAnalyer`` holds it (openNURBS knots = the analyser's
    clamped knot vector without its first and last entry)."""

    def __init__(self, ctrl: Sequence[tuple[float, float]], knots: Sequence[float], order: int = 4):
        self.ctrl = [(float(p[0]), float(p[1])) for p in ctrl]
        self.full_knots = [float(k) for k in knots]
        self.on_knot = self.full_knots[1:-1]
        self.order = order
        self.n = len(self.ctrl)

    def span_index(self, t: float) -> int:
        """``ON_NurbsSpanIndex`` with side 0: knot[i+order-2] <= t < knot[i+order-1]."""
        k, order, n = self.on_knot, self.order, self.n
        last = n - order
        if t >= k[n - 2]:
            return last
        if t < k[order - 1]:
            return 0
        lo, hi = order - 2, n - 1          # search in k[order-2 .. n-1]
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if t < k[mid]:
                hi = mid
            else:
                lo = mid
        return min(max(lo - (order - 2), 0), last)

    def evaluate(self, t: float, der_count: int):
        """``ON_NurbsCurve::Evaluate`` -> list of (x, y) for derivatives 0..der_count."""
        order = self.order
        si = self.span_index(t)
        knot = self.on_knot[si:si + 2 * order - 2]
        cv = self.ctrl[si:si + order]
        if knot[order - 2] == knot[0] and knot[2 * order - 3] == knot[order - 1]:
            return _bezier_eval(cv, knot[order - 2], knot[order - 1], der_count, t)
        der_count = min(der_count, order - 1)
        N = _basis(order, knot, t)
        if der_count:
            _basis_derivatives(order, knot, der_count, N)
        # the analyser's curve is rational (vt[1] 0x100bea60 sets m_is_rat = 1, weights 1.0):
        # ON_EvaluateNurbsSpan -> FUN_10058400 sums (x, y, 0, w) and applies the quotient rule
        v = []
        for j in range(der_count + 1):
            vx = vy = vw = 0.0
            for i in range(order):
                b = N[j * order + i]
                vx = cv[i][0] * b + vx
                vy = cv[i][1] * b + vy
                vw = 1.0 * b + vw
            v.append([vx, vy, vw])
        return _quotient_rule(v, der_count)


def _quotient_rule(v, der_count):
    """``ON_EvaluateQuotientRule`` (FUN_1006cd30) for dim 2 (+ the zero z) and der_count <= 2."""
    w = v[0][2]
    if w == 0.0:
        return [(r[0], r[1]) for r in v]
    for r in v:
        for i in range(3):
            r[i] = r[i] * (1.0 / w)
    if der_count:
        F, F1 = v[0], v[1]
        wp = -F1[2]
        for i in range(2):
            F1[i] = F[i] * wp + F1[i]
        if der_count > 1:
            F2 = v[2]
            w2 = F2[2]
            for i in range(2):
                F2[i] = (wp + wp) * F1[i] + F[i] * -w2 + F2[i]
    return [(r[0], r[1]) for r in v]


def _bezier_eval(cv, t0, t1, der_count, t):
    """Bezier span: ``ON_EvaluateBezier`` (splineAnalyer FUN_10057270, O), reached by
    ON_EvaluateNurbsSpan (FUN_100584d0) when the span's knots are clamped at both ends, i.e. by
    single-span curves such as the DXF spline glyphs (4 control points, knots [0,0,0,0,2,2,2,2]).

    Operation by operation:
    * the homogeneous control points (x, y, w = 1; the analyser curve is rational, z = 0 is
      left out) are copied forwards when t == t0 or (t <= 0.5 (t0 + t1) and t != t1), else
      backwards; then a0 = (t1 - t) d, a1 = (t - t0) d with d = 1/(t1 - t0) and the derivative
      step h = t1 - t (backwards: a1 = (t1 - t) d, a0 = (t - t0) d, h = t0 - t);
    * unless a1 == 0, the full de Casteljau triangle CV[j] = CV[j+1] a1 + CV[j] a0 (j from 0, one
      point fewer per level), which leaves the control points of the sub-curve that starts at t;
    * derivatives: with c = degree, degree-1, ...: CV[i] = (CV[i] - CV[i-1]) (c (1/h)) for i from
      the top down to the level (the factor c * (1/h) is formed once per level, 0x10057602);
    * the quotient rule (FUN_1006cd30) for the weight: w is 1 only up to the last bit after the
      triangle (a0 + a1 need not be 1)."""
    order = len(cv)
    der_count = min(der_count, order - 1)
    fwd = t0 == t or (t <= (t0 + t1) * 0.5 and t1 != t)
    pts = [[float(p[0]), float(p[1]), 1.0] for p in (cv if fwd else reversed(cv))]
    if fwd:
        h = t1 - t
        d = 1.0 / (t1 - t0)
        a0 = h * d
        a1 = (t - t0) * d
    else:
        h = t0 - t
        d = 1.0 / (t1 - t0)
        a1 = (t1 - t) * d
        a0 = (t - t0) * d
    if a1 != 0.0:
        for lvl in range(order - 1, 0, -1):
            for j in range(lvl):
                p, q = pts[j], pts[j + 1]
                for k in range(3):
                    p[k] = q[k] * a1 + p[k] * a0
    if der_count:
        c = float(order)
        inv_h = 1.0 / h
        top = min(order, der_count + 1)
        for lvl in range(top - 1, 0, -1):
            c = c - 1.0
            fac = c * inv_h
            for i in range(top - 1, top - 1 - lvl, -1):
                p, q = pts[i], pts[i - 1]
                for k in range(3):
                    p[k] = (p[k] - q[k]) * fac
    v = [list(pts[i]) for i in range(der_count + 1)]
    return _quotient_rule(v, der_count)


def nurbs_eval(ctrl, knots, t: float, der_count: int = 2):
    """Point and derivatives of the analyser curve at ``t`` (exact openNURBS arithmetic)."""
    return _Nurbs(ctrl, knots).evaluate(t, der_count)


# ---------------------------------------------------------------------------------------------
# splineAnalyer sampling (vt+0x30 = FUN_100bfe70, span sampler FUN_100bf9c0)
def _ev_tangent(cur: _Nurbs, t: float):
    """``ON_Curve::EvTangent`` (FUN_1004d1d0): point and unit first derivative."""
    p, d1 = cur.evaluate(t, 1)
    tx, ty, ok = _unitize(d1[0], d1[1])
    return ok, p, (tx, ty)


def _vec_angle(a, b) -> float:
    """FUN_100c42c0: angle between two vectors, 0 when either is shorter than 1e-8."""
    dot = a[0] * b[0] + b[1] * a[1]
    ha = ms_hypot(a[0], a[1])
    hb = ms_hypot(b[0], b[1])
    if ha <= 1e-8 or hb <= 1e-8:
        return 0.0
    return _acos_clamped((dot / ha) / hb)


def _ftol(x: float) -> int:
    return int(x)        # MSVC _ftol2: truncation toward zero


def _sample_span(cur: _Nurbs, out: list[float], u0: float, u1: float, angle: float, tol: float) -> None:
    """FUN_100bf9c0: parameters of the interior samples of [u0, u1] (appended to ``out``)."""
    L = u1 - u0
    if L < 1e-4:
        return
    h = L * 0.01
    if h < 1e-4:
        h = 1e-4
    for i in range(10):
        ok, P0, T0 = _ev_tangent(cur, i * h + u0)
        if ok:
            break
        if i == 9:
            return
    _ok, Pm, Tm = _ev_tangent(cur, (u0 + u1) * 0.5)
    for i in range(10):
        ok, Pe, Te = _ev_tangent(cur, u1 - i * h)
        if ok:
            break
        if i == 9:
            return
    chord = ms_hypot(P0[0] - Pm[0], P0[1] - Pm[1]) + ms_hypot(Pe[0] - Pm[0], Pe[1] - Pm[1])
    turn = _vec_angle(Tm, Te) + _vec_angle(T0, Tm)
    if turn <= 15.0 or chord <= 5.0 * tol:
        n1 = _ftol(turn / angle + 0.5)
        n2 = _ftol(chord / 0.005)
        n = min(n1, n2)
        n3 = _ftol(chord / tol + 0.5)
        n = max(n, n3)
        n = ((n + 1) // 2) * 2
        if n <= 1:
            return
        for i in range(1, n):
            out.append((i * L) / n + u0)
        return
    m = _ftol(turn / 15.0 + 0.001)
    n = ((m + 2) // 2) * 2
    h2 = L / n
    for i in range(1, n + 1):
        _sample_span(cur, out, u0 + (i - 1) * h2, i * h2 + u0, angle, tol)


def analyser_samples(ctrl, knots, angle_deg: float = 2.0, tol: float = SAMPLE_TOL):
    """FUN_100bfe70: sample records [u, x, y, 1/|D1|^2, Tx, Ty, Kx, Ky] of the analyser curve.

    Every knot span of length >= 1e-5 contributes its end parameters and FUN_100bf9c0's interior
    samples; each sample is evaluated (point, D1, D2 -> ON_EvCurvature), then consecutive
    samples within 0.001 mm in both x and y are dropped."""
    cur = _Nurbs(ctrl, knots)
    kn = cur.full_knots
    nk = len(kn)
    if cur.order <= 3 or nk <= 7:
        return []
    ang = 0.017453292519943295 * angle_deg
    spans: list[list[list[float]]] = []
    for i in range(4, nk - 3):
        if kn[i] - kn[i - 1] >= 1e-5:
            us = [kn[i - 1]]
            _sample_span(cur, us, kn[i - 1], kn[i], ang, tol)
            us.append(kn[i])
            spans.append(us)
        else:
            spans.append([])
    recs: list[list[list[float]]] = []
    for us in spans:
        lst = []
        for k, u in enumerate(us):
            (px, py), (d1x, d1y), (d2x, d2y) = cur.evaluate(u, 2)
            ln = on_length(d1x, d1y)
            if ln == 0.0:
                tx = ty = kx = ky = f = 0.0
            else:
                r = 1.0 / ln
                tx, ty = d1x * r, d1y * r
                neg = (-d2x) * tx + (-d2y) * ty
                f = 1.0 / (ln * ln)
                kx = (d2x + tx * neg) * f
                ky = (d2y + ty * neg) * f
            if abs(0.0 - tx) > 1e-8 or abs(0.0 - ty) > 1e-8:
                hh = ms_hypot(tx, ty)
                if abs(hh - 1.0) > 1e-8 and hh >= 1e-8:
                    tx, ty = tx / hh, ty / hh
            elif k < 1:
                tx, ty = 1.0, 0.0           # DAT_100d14c0
            else:
                tx, ty = lst[-1][4], lst[-1][5]
            lst.append([u, px, py, f, tx, ty, kx, ky])
        recs.append(lst)
    out: list[list[float]] = []
    for lst in recs:
        if lst:
            out.append(list(lst[0]))
            break
    for lst in recs:
        for r in lst[1:]:
            last = out[-1]
            if abs(r[1] - last[1]) > 0.001 or abs(r[2] - last[2]) > 0.001:
                out.append(list(r))
        if lst:
            last, e = out[-1], lst[-1]
            if abs(last[1] - e[1]) > 0.001 or abs(last[2] - e[2]) > 0.001:
                out[-1] = list(e)
    return out


# ---------------------------------------------------------------------------------------------
def _r_of_k(kx: float, ky: float) -> float:
    """FUN_100f55f0 / FUN_100f83c0: radius from a curvature vector."""
    if abs(ky) + abs(kx) > 1500.0:
        return 0.001
    q = ky * ky + kx * kx
    if q <= 1e-8:
        return R_LINE
    r = 1.0 / math.sqrt(q)
    return 0.001 if r < 0.001 else r


def _angle_from_x(vx: float, vy: float) -> float:
    """FUN_10009540((1,0), v): atan2(v) - atan2(0,1), folded into (-pi, pi]."""
    if ms_hypot(1.0 - vx, 0.0 - vy) < 1e-8:
        return 0.0
    d = _crt.atan2(vy, vx) - 0.0
    if math.pi < d:
        d -= 6.283185307179586
    elif d <= -3.141592653589793:
        d += 6.283185307179586
    return d


class _Rec:
    __slots__ = ("u", "x", "y", "s", "f", "tx", "ty", "kx", "ky", "R", "len", "arc", "cx", "cy",
                 "rad", "a0", "a1")

    def __init__(self, u, x, y, f, tx, ty, kx, ky):
        self.u, self.x, self.y, self.f = u, x, y, f
        self.tx, self.ty, self.kx, self.ky = tx, ty, kx, ky
        self.s = 0.0
        self.R = _r_of_k(kx, ky)
        self.len = 0.0
        self.arc = False
        self.cx = self.cy = self.rad = self.a0 = self.a1 = 0.0


@port("FUN_100f83c0", "FUN_100f72a0", "FUN_100f8b60", source="CADModule", parity=("jobcad",))
class CurveTable:
    """A curve piece of CContoutSmooth (type 1 spline / type 2 blend) with its record table.

    ``ctrl``/``knots``: the control polygon and clamped knot vector handed to the analyser
    (vt[1] 0x100bea60).  ``u0``/``u1``: the piece window (+0x68/+0x70) as fractions of the
    table's arc length (0, 1 for a blend).  ``ref``: the piece's +0x78 (FUN_100f92b0 param_3):
    the full glyph length for a spline primitive, DAT_1011d8c8 for a blend.  FUN_100f83c0 samples
    with the tolerance min(ref, 1) raised to +0x120 (0.05) when ref > 0, else 0.05: a spline
    glyph of 18 mm is sampled at 1 mm chords (46 steps on a quarter ellipse, O)."""

    kind = "table"

    def __init__(self, ctrl, knots, u0: float = 0.0, u1: float = 1.0, ref: float = 0.0):
        self.ctrl = [tuple(p) for p in ctrl]
        self.knots = list(knots)
        self.u0, self.u1 = float(u0), float(u1)
        self.ref = float(ref)
        if self.ref > 0.0:
            tol = 1.0 if 1.0 < self.ref else self.ref
            self.sample_tol = SAMPLE_TOL if tol < SAMPLE_TOL else tol
        else:
            self.sample_tol = SAMPLE_TOL
        self.recs: list[_Rec] = []
        self._build()
        self.total = self.recs[-1].s if self.recs else 0.0
        self._set_length()

    def _set_length(self) -> None:
        """FUN_100f6770 for a curve piece: length = (u1 - u0) * table length."""
        self.length = (self.u1 - self.u0) * self.total

    def windowed(self, u0: float, u1: float) -> "CurveTable":
        """The same table with another window (+0x68/+0x70), as the smoother's piece loop sets it
        on a spline primitive's piece (CAD load 0x100ff000: u0 + kd_s/total, u1 - kd_e/total)."""
        c = object.__new__(CurveTable)
        c.__dict__.update(self.__dict__)
        c.u0, c.u1 = float(u0), float(u1)
        c._set_length()
        return c

    # -- FUN_100f83c0 ---------------------------------------------------------------------------
    def _build(self) -> None:
        recs: list[_Rec] = []
        for r in analyser_samples(self.ctrl, self.knots, 2.0, self.sample_tol):
            if recs:
                last = recs[-1]
                if not (abs(r[1] - last.x) > 1e-4 or abs(r[2] - last.y) > 1e-4):
                    continue
            recs.append(_Rec(*r))
        for k in range(1, len(recs)):
            p, c = recs[k - 1], recs[k]
            chord = ms_hypot(p.x - c.x, p.y - c.y)
            ravg = (p.R + c.R) * 0.5
            c.arc = False
            if not (500.0 <= ravg or chord > ravg * 0.7853981633974483):
                hc = chord * 0.5
                dx = c.x - p.x
                dy = c.y - p.y
                cp = p.ty * dx - dy * p.tx
                cc = c.ty * dx - dy * c.tx
                direction = 0
                if cp > 0.0:
                    if not cc > 0.0:
                        direction = -1
                elif cc > 0.0:
                    direction = 1
                if direction and not ((0.5 * hc) * hc / ravg < 1e-6):
                    c.arc = True
                    c.rad = ravg
                    h = math.sqrt(ravg * ravg - hc * hc)
                    sgn = 1.0 if direction != -1 else -1.0
                    q = h / chord
                    X = dx * 0.0 - sgn * dy
                    Y = dy * 0.0 + sgn * dx
                    c.cx = X * q + (p.x + c.x) * 0.5
                    c.cy = Y * q + (c.y + p.y) * 0.5
                    c.a0 = _angle_from_x(-c.cx + p.x, -c.cy + p.y)
                    a1 = _angle_from_x(-c.cx + c.x, -c.cy + c.y)
                    if direction == -1:
                        if c.a0 < a1:
                            a1 = a1 - 6.283185307179586
                    elif a1 < c.a0:
                        a1 = a1 + 6.283185307179586
                    c.a1 = a1
            c.len = abs(c.a1 - c.a0) * c.rad if c.arc else chord
        if recs:
            recs[0].len = 0.0
            recs[0].s = 0.0
        for k in range(1, len(recs)):
            recs[k].s = recs[k - 1].s + recs[k].len
        self.recs = recs
        self._smooth_radii()

    # -- FUN_100f6c90 ---------------------------------------------------------------------------
    def _smooth_radii(self) -> None:
        recs = self.recs
        n = len(recs)
        avg = []
        for i in range(n):
            tot = recs[i].R
            cnt = 1
            dist = 0.0
            j = i - 1
            while j >= 0:
                if 2.0 <= dist or j < i - 2:
                    break
                dist = dist + recs[j + 1].len
                tot = recs[j].R + tot
                cnt += 1
                j -= 1
            dist = 0.0
            j = i + 1
            while j < n:
                if 2.0 <= dist or i + 2 < j:
                    break
                dist = dist + recs[j].len
                tot = recs[j].R + tot
                cnt += 1
                j += 1
            avg.append(tot / cnt)
        for i in range(n):
            if 0.01 <= recs[i].R / avg[i]:
                recs[i].R = recs[i].R - (recs[i].R - avg[i]) * 0.75

    # -- lookups ------------------------------------------------------------------------------------
    def _search(self, target: float) -> int:
        """FUN_100f57d0: index k with s[k-1] <= target < s[k] (k = n-1 at the end), -1 if n < 2."""
        n = len(self.recs)
        if n < 2:
            return -1
        lo, hi = 0, n - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if target < self.recs[mid].s:
                hi = mid
            else:
                lo = mid
        return hi

    def radius_at(self, u: float) -> float:
        """FUN_100f6840: radius at the table fraction ``u`` (weights as in the original: the
        record at the far end of the interval gets the near end's weight)."""
        k = self._search(self.total * u)
        if k < 1:
            return 99999.0
        r1, r0 = self.recs[k], self.recs[k - 1]
        d2 = r1.s - r0.s
        d3 = r1.s - self.total * u
        return (_r_of_k(r1.kx, r1.ky) * d3 + (d2 - d3) * _r_of_k(r0.kx, r0.ky)) / d2

    def is_straight(self) -> bool:
        """Analyser vt+0x4c (IsLinear, tolerance 1e-12) (I: collinear control polygon)."""
        a, b = self.ctrl[0], self.ctrl[-1]
        L = math.dist(a, b)
        if L <= 0:
            return False
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        return all(abs(ux * (q[1] - a[1]) - uy * (q[0] - a[0])) <= 1e-12 for q in self.ctrl)

    # -- FUN_100f72a0 (curve branch) ----------------------------------------------------------------
    def nodes(self) -> list[list[float]]:
        """Curvature nodes [[R, s, factor], ...] of the piece (before the per-piece factor)."""
        if self.is_straight() or not self.recs:
            L = self.length
            return [[R_LINE, 0.0, 1.0], [R_LINE, L * 0.5, 1.0], [R_LINE, L, 1.0]]
        recs = self.recs
        idx = _trend_points(recs)
        _prune_points(recs, idx)
        sel = _select_points(recs, idx, self.u0, self.u1, 3.0)
        out = [[self.radius_at(self.u0), 0.0, 1.0]]
        off = self.total * self.u0
        for k in sel:
            s = recs[k].s - off
            R = recs[k].R
            if s - out[-1][1] <= 0.001:
                out[-1][0] = min(out[-1][0], R) if out[-1][0] > R else out[-1][0]
            else:
                out.append([R, s, 1.0])
        r_end = self.radius_at(self.u1)
        s_end = self.total * self.u1 - off
        if len(out) < 2 or 0.05 < s_end - out[-1][1]:
            out.append([r_end, s_end, 1.0])
        else:
            out[-1][1] = s_end
            if r_end < out[-1][0]:
                out[-1][0] = r_end
        _thin_and_floor(out, curve=True)
        return out

    # -- FUN_100f8180 ---------------------------------------------------------------------------
    def point_at_fraction(self, t: float):
        """XY at table fraction ``t`` (piece fraction already mapped through the window)."""
        recs = self.recs
        n = len(recs)
        if n < 2:
            return (recs[0].x, recs[0].y) if recs else (0.0, 0.0)
        target = t * self.total
        k = 1
        while k < n and not (target <= recs[k].s):
            k += 1
        if k >= n:
            k = n - 1
        r, p = recs[k], recs[k - 1]
        d = r.s - self.total * t
        if not r.arc:
            f = d / r.len
            return ((p.x - r.x) * f + r.x, (p.y - r.y) * f + r.y)
        f = d / r.len
        ang = r.a1 - (r.a1 - r.a0) * f
        return (math.cos(ang) * r.rad + r.cx, math.sin(ang) * r.rad + r.cy)

    def point(self, s: float):
        """XY at arc length ``s`` on the piece (FUN_100f8b60 for curve pieces)."""
        t = s / self.length if self.length > 0 else 0.0
        if abs((self.u1 - self.u0) - 1.0) >= 1e-8:
            t = t * (self.u1 - self.u0) + self.u0
        return self.point_at_fraction(t)

    def tangent(self, s: float):
        """Unit tangent at arc length ``s`` (FUN_100f5890 for curve pieces)."""
        return self.tangent_f(s / self.length if self.length > 0 else 0.0)

    def point_f(self, f: float):
        """FUN_100f5ac0 (curve branch): XY at fraction ``f`` of the piece window, as the smoother's
        load evaluates a spline element (corner points +0xf8/+0x140, FUN_100f9430).  t = (u1 - u0)
        f + u0, k from FUN_100f57d0 (s[k-1] <= t total < s[k]); on a chord
        P = r - (r - prev) (s_k - total t)/len_k, on an arc angle a1 - (a1 - a0)(s_k - total t)/len_k."""
        recs = self.recs
        t = (self.u1 - self.u0) * f + self.u0
        k = self._search(self.total * t)
        if k < 1:
            return (self.ctrl[0][0], self.ctrl[0][1]) if self.ctrl else (0.0, 0.0)
        r, p = recs[k], recs[k - 1]
        d = r.s - self.total * t
        if not r.arc:
            g = d / r.len
            return (r.x - (r.x + -p.x) * g, r.y - (-p.y + r.y) * g)
        ang = r.a1 - g_ang(r, d)
        return (math.cos(ang) * r.rad + r.cx, math.sin(ang) * r.rad + r.cy)

    def tangent_f(self, f: float):
        """FUN_100f5890 (curve branch): unit tangent at fraction ``f`` of the piece window."""
        recs = self.recs
        if len(recs) < 2:
            return (1.0, 0.0)
        t = (self.u1 - self.u0) * f + self.u0
        k = self._search(self.total * t)
        r = recs[k]
        if not r.arc:
            inv = 1.0 / r.len
            return ((r.x - recs[k - 1].x) * inv, (r.y - recs[k - 1].y) * inv)
        f = (r.s - self.total * t) / r.len
        if f < 0.2:
            return (r.tx, r.ty)
        if f > 0.8:
            return (recs[k - 1].tx, recs[k - 1].ty)
        dd = r.a1 - r.a0
        sg = 1.0 if dd >= 0.0 else -1.0
        ang = r.a1 - dd * f
        return (-math.sin(ang) * sg, sg * math.cos(ang))


def g_ang(r, d: float) -> float:
    """(a1 - a0) * (d / len) of an arc record (FUN_100f5ac0 0x100f5c49)."""
    return (r.a1 - r.a0) * (d / r.len)


# ---------------------------------------------------------------------------------------------
def _trend(d: float) -> int:
    if d <= 1e-6:
        return 0 if -1e-6 <= d else -1
    return 1


def _trend_points(recs) -> list[int]:
    """FUN_100f5ce0: record indices where the radius trend changes (plus both ends)."""
    n = len(recs)
    if n < 1:
        return []
    idx = [0]
    prev = _trend(recs[1].R - recs[0].R) if n > 1 else 0
    for k in range(1, n - 1):
        tr = _trend(recs[k + 1].R - recs[k].R)
        if tr != prev:
            idx.append(k)
        prev = tr
    idx.append(n - 1)
    return idx


def _prune_points(recs, idx: list[int]) -> None:
    """FUN_100f5f10: drop a b c triples closer than 0.05 mm with similar outer radii."""
    k = len(idx) - 3
    while k >= 2:
        a, b, c = idx[k - 1], idx[k], idx[k + 1]
        if recs[c].s - recs[b].s < 0.05 and recs[b].s - recs[a].s < 0.05:
            r2, r3 = recs[c].R, recs[a].R
            m = r2 if r3 < r2 else r3
            if abs(r3 - r2) < m * 0.1:
                idx[k - 1] = idx[k]
                del idx[k:k + 2]
                k -= 2
        k -= 1


def _select_points(recs, idx: list[int], u0: float, u1: float, gap: float) -> list[int]:
    """FUN_100f6090: node records between the trend points (every ``gap`` mm and where the
    tangent turns: dot < 0.85), then the window [u0, u1] of the table."""
    n = len(recs)
    if n < 2:
        return []
    out = [0]
    for m in range(1, len(idx)):
        cur = idx[m]
        p = out[-1]
        if 0.1 <= recs[cur].s - recs[p].s:
            j = p + 1
            while j < cur:
                dot = recs[j - 1].tx * recs[j].tx + recs[j].ty * recs[j - 1].ty
                if gap < recs[j].s - recs[p].s or dot < 0.85 or 1.00001 < dot:
                    out.append(j)
                    p = j
                j += 1
            out.append(cur)
        elif recs[cur].R < recs[out[-1]].R:
            out[-1] = cur
    if out[-1] < n - 1:
        out.append(n - 1)
    total = recs[-1].s
    lo, hi = total * u0, total * u1
    first = 0
    while first < len(out) and not (lo <= recs[out[first]].s):
        first += 1
    last = len(out) - 1
    while last >= 0 and not (recs[out[last]].s <= hi):
        last -= 1
    return out[first:last + 1]


def _thin_and_floor(nl: list[list[float]], curve: bool) -> None:
    """End of FUN_100f72a0: for curve pieces, of two neighbouring radii below 0.05 the larger
    becomes 0.05 (from the pair (1, 2) on); then every node but the first gets R >= 0.001."""
    n = len(nl)
    if curve and n > 2:
        for k in range(2, n):
            if nl[k][0] < 0.05 and nl[k - 1][0] < 0.05:
                if nl[k][0] <= nl[k - 1][0]:
                    nl[k - 1][0] = 0.05
                else:
                    nl[k][0] = 0.05
    for k in range(n - 1, 0, -1):
        if nl[k][0] < 0.001:
            nl[k][0] = 0.001
