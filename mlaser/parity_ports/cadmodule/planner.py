"""Look-ahead velocity planning: port of CVelocityPlanning.

MainApp uses the v1.3.23 copy statically linked into **CADModule.dll** (factory 0x1010d140,
vtable 0x10123374, driven by CInterpMrg::Interp 0x10105260); MotionCtrl.dll (V1.3.22, vtable
0x10019730) holds an older copy that is never loaded.  This module follows the CADModule copy;
where the two differ that is noted (docs/motion.md §4).  The block algorithm of both copies gives
identical results on 300 random paths (parity work/motionK, CADModule called through its factory).

=================================  =============  ===================  =========================
step                               MotionCtrl     CADModule            Python
=================================  =============  ===================  =========================
plan (vt[1])                       0x10016720     0x1010df60           ``plan_velocity``
point limits, use_curvature=1      0x100169b0     0x1010d1b0           ``_point_limits``
point limits, use_curvature=0      0x10016f60     0x1010d8c0           ``_point_limits``
curvature speed                    0x100170e0     0x1010c9f0           ``curvature_speed``
slow start (and CAD: slow end)     0x10017270     0x1010da50           ``_slow_start_end``
block planner                      0x100114d0     0x1010bbc0           ``_BlockPlanner.run``
  valley split + block loop        0x100116e0     0x1010ba00           ``_split``
  valleys                          0x10014060     0x10109070           ``_valleys``
  valley-pair cap                  0x100144b0     0x10108590           ``_cap_pairs``
  recursive block planning         0x10011870     0x1010a890           ``_plan_range``
  one block (+ infeasible fix-up)  0x10011c30     0x10108920           ``_block``
  fix-up root finder               0x10011b10     0x10107b10           ``_reach_speed``
  violation search                 0x10011fb0     0x10108b80           ``_violation``
    forward / backward check       0x10012220/490 0x10108070/0x101082e0 ``_fwd_check``/``_back_check``
  merge steep (raised-J) blocks    0x10012b20     0x1010b030           ``_merge_steep``
  merge short ramps                0x10012740     0x1010ab40           ``_merge_short``
    ramp class / merged block      0x100146b0/1e80 0x10107930/0x10107f40
  flatten small bumps              0x10014740     0x101087a0           ``_flatten``
sample (vt[4])                     0x10013440     0x1010a0d0           ``Plan.sample``
  block sampler                    0x100136a0     0x101094e0           ``Profile.sample_from``
total time (vt[2])                 0x10011590     0x101079c0           ``Plan.duration``
=================================  =============  ===================  =========================

Internal point record (40 bytes, ``_Pt``): [0] ds = s_i - s_(i-1), [1] v (point speed),
[2] w (limit of the segment i-1 -> i), [3] max_acc (unused), [4] s.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .profile import NAN, Profile, _div, _sqrt, accel_profile, scurve_solve
from .._registry import port

__port__ = {
    "source": "CADModule",
    "class": "CVelocityPlanning v1.3.23 (factory 0x1010d140, vtable 0x10123374); MotionCtrl V1.3.22 copy",
    "functions": {
        "0x1010df60": "plan_velocity", "0x1010d1b0": "_point_limits", "0x1010d8c0": "_point_limits",
        "0x1010c9f0": "curvature_speed", "0x1010da50": "_slow_start_end",
        "0x1010bbc0": "_BlockPlanner.run", "0x1010ba00": "_split", "0x10109070": "_valleys",
        "0x10108590": "_cap_pairs", "0x1010a890": "_plan_range", "0x10108920": "_block",
        "0x10107b10": "_reach_speed", "0x10108b80": "_violation",
        "0x10108070": "_fwd_check", "0x101082e0": "_back_check", "0x1010b030": "_merge_steep",
        "0x1010ab40": "_merge_short", "0x101087a0": "_flatten", "0x1010a0d0": "Plan.sample",
        "0x101079c0": "Plan.duration",
    },
    "parity": ["motion", "cad_motion"],
}

R_STRAIGHT = 1e9  # radius used for straight segments (CContoutSmooth itself uses 10000)


@dataclass
class PlannerParams:
    """CVelocityPlanning parameter block (this+0x28).

    CADModule's copy has ten doubles (defaults 0x1013fcc8 = CInterpMrg ctor 0x10102490 values at
    +0x88); MotionCtrl's has the first eight (defaults 0x1001c288, p7 = 10).
    """
    max_acc: float = 2000.0        # p0 mm/s^2
    acc_time: float = 0.125        # p1 s, clamped to [0.06, 0.25]
    precision: float = 0.05        # p2, shape factor of curvature_speed (0.05 = neutral)
    max_vel: float = 200.0         # p3 mm/s
    min_vel: float = 0.0           # p4 mm/s
    period_ms: float = 1.0         # p5 interpolation period
    start_slow_len: float = 0.0    # p6 mm (active when >= 0.1)
    start_slow_vel: float = 200.0  # p7 mm/s (raised to >= min_vel); MotionCtrl default 10
    end_slow_len: float = 0.0      # p8 mm (CADModule only, active when >= 0.1)
    end_slow_vel: float = 200.0    # p9 mm/s (CADModule only)

    def clamped(self) -> "PlannerParams":
        """Clamps of vt[1] (CAD 0x1010df60): acc_time to [0.06, 0.25], p7 = max(p7, p4)."""
        p = PlannerParams(**self.__dict__)
        if p.acc_time < 0.06:
            p.acc_time = 0.06
        if 0.25 < p.acc_time:
            p.acc_time = 0.25
        p.start_slow_vel = p.start_slow_vel if p.min_vel <= p.start_slow_vel else p.min_vel
        return p

    def jerk(self) -> float:
        """Jerk derived in vt[1]: if max_vel <= A*T/2, J = 4V/T^2, otherwise J = 2A/T."""
        A, T, V = self.max_acc, self.acc_time, self.max_vel
        if V <= A * T * 0.5:
            return V * 4.0 / T / T
        return (A + A) / T


@dataclass
class PathPoint:
    """One 72-byte planner input record.

    Observed fields (CAD 0x1010d1b0, CInterpMrg 0x10105260): [0] s, [1] radius of curvature,
    [2] commanded feed, [3] extra limit, [8] speed factor.  Fields [4..7] are not read.
    """
    s: float
    radius: float = R_STRAIGHT
    feed: float = 1e9
    limit: float = 1e9
    factor: float = 1.0


def curvature_speed(R: float, q: float, p: float) -> float:
    """Empirical speed limit for radius ``R`` (port of CAD 0x1010c9f0 / MotionCtrl 0x100170e0).

    q = 0.5 / acc_time, p = precision.  Piecewise in R (mm)::

        R <= 1.6      v = 8 q R
        1.6 < R <= 6  v = sqrt(R (102.4 q^2 + (R-1.6)(30 (q-3) + 40)))
        6 < R < 12    v = sqrt(R (102.4 q^2 + 132 q - 220 + 150 (R-6)(q-2)))
        R >= 12       v = sqrt(R (102.4 q^2 + 1032 q - 2020))

    (continuous at 1.6, 6, 12) plus a precision correction that is zero for p = 0.05::

        p >= 0.03: v += 73.8 (p-0.05) min(R, 25 q^2 / (73.8 p - 3.69)^2)
        p <  0.03: v += 15 (30 p - 1)  min(R, q^2 / (3 - 90 p)^2)
    """
    if R <= 1.6:
        v = 8.0 * q * R
    else:
        if R <= 6.0:
            g = 102.4 * q * q + (R - 1.6) * (30.0 * (q - 3.0) + 40.0)
        elif R < 12.0:
            g = 102.4 * q * q + 132.0 * q - 220.0 + 150.0 * (R - 6.0) * (q - 2.0)
        else:
            g = 102.4 * q * q + 1032.0 * q - 2020.0
        v = math.sqrt(max(R * g, 0.0))
    if p >= 0.03:
        d = (73.8 * p - 3.69) ** 2      # == 0 at p = 0.05 -> h = inf (x87 semantics)
        h = 25.0 * q * q / d if d > 0 else math.inf
        v += 73.8 * (p - 0.05) * min(R, h)
    else:
        d = (3.0 - 90.0 * p) ** 2
        k = q * q / d if d > 0 else math.inf
        v += 15.0 * (30.0 * p - 1.0) * min(R, k)
    return v


@dataclass
class Plan:
    """Result: consecutive blocks, each one profile (S-curve or accel-only)."""
    blocks: list[Profile] = field(default_factory=list)
    starts: list[float] = field(default_factory=list)   # arc length at block start (relative)
    point_vel: list[float] = field(default_factory=list)
    period_ms: float = 1.0

    @property
    def duration(self) -> float:
        """vt[2] (CAD 0x101079c0): sum of all phase durations."""
        return sum(b.duration for b in self.blocks)

    @property
    def length(self) -> float:
        return sum(b.L for b in self.blocks)

    def state_at_time(self, t: float) -> tuple[float, float]:
        """(s, v) at absolute time t."""
        acc = 0.0
        for s0, b in zip(self.starts, self.blocks):
            d = b.duration
            if t <= acc + d:
                pos, v = b.state(t - acc)
                return s0 + pos, v
            acc += d
        return (self.length, 0.0)

    def sample(self, period_ms: float | None = None) -> list[float]:
        """vt[4] (CAD 0x1010a0d0 -> 0x101094e0): cumulative s every period.

        Each block is sampled from t0 = dt - carry (t0 = 0 when the carry is negative, which is
        also how the first block starts: carry is initialised to -1), phases 1..6 while
        t < phase end and phase 7 while t <= T.  The new carry is T - t_last, or carry - T
        when the block produced no sample.  The offset of every block is the sum of the previous
        block lengths.  No final sample at the exact end is added (D-01): the last value is the
        last tick t <= T.
        """
        dt = (self.period_ms if period_ms is None else period_ms) * 0.001
        out: list[float] = []
        carry = -1.0
        off = 0.0
        for b in self.blocks:
            t0 = dt - carry if (0.0 <= carry or dt <= carry) else 0.0
            pos, tnext = b.sample_from(t0, dt)
            T = b.duration
            carry = T - (tnext - dt) if pos else carry - T
            out.extend(p + off for p in pos)
            off = b.L + off
        return out


class _Pt:
    __slots__ = ("ds", "v", "w", "A", "s")

    def __init__(self, ds, v, w, A, s):
        self.ds, self.v, self.w, self.A, self.s = ds, v, w, A, s

    def copy(self):
        return _Pt(self.ds, self.v, self.w, self.A, self.s)


def _point_limits(points: list[PathPoint], prm: PlannerParams, use_curvature: bool) -> list[_Pt]:
    """Internal point records.

    use_curvature (CAD 0x1010d1b0 = MotionCtrl 0x100169b0):
      v_i = curvature_speed(R_i), w_i = feed_i, s_i; then for i >= 1: w_i = min(max(v_i, v_(i-1)),
      feed_i) and ds_i = s_i - s_(i-1); segment limits <= 0.01 are copied from the previous
      segment (i >= 2) or from the next one (i = 1, when larger) until nothing changes; then
      v_i = min(min(max(min(v_i, p3) * factor_i, p4), limit_i), feed_i); v_0 = v_(n-1) = 0.
      (The CAD copy also appends point speeds to Log\\VelDecc.txt: not reproduced.)
    not use_curvature (CAD 0x1010d8c0 = MotionCtrl 0x10016f60, D-05):
      v_i = limit_i, w_i = feed_i, ds_i; **no** clamps, no fill and the end speeds are kept.
    """
    n = len(points)
    A0 = prm.max_acc
    pts = []
    if not use_curvature:
        for i, p in enumerate(points):
            pts.append(_Pt(0.0, p.limit, p.feed, A0, p.s))
        for i in range(1, n):
            pts[i].ds = pts[i].s - pts[i - 1].s
        return pts
    q = 0.5 / prm.acc_time
    for p in points:
        pts.append(_Pt(0.0, curvature_speed(p.radius, q, prm.precision), p.feed, A0, p.s))
    for i in range(1, n):
        a, b = pts[i - 1], pts[i]
        b.w = a.v if b.v <= a.v else b.v
        if points[i].feed < b.w:
            b.w = points[i].feed
        b.ds = b.s - a.s
    for _ in range(4 * n + 8):          # the DLL loops until no limit < 0.01 is left
        if n < 2:
            break
        done = True
        last = n - 1
        for i in range(1, n):
            r = pts[i]
            if r.w <= 0.01:
                if i < 2:
                    if i < last and r.w < pts[i + 1].w:
                        r.w = pts[i + 1].w
                else:
                    r.w = pts[i - 1].w
                if r.w < 0.01:
                    done = False
        if done:
            break
    for i, p in enumerate(points):
        r = pts[i]
        if prm.max_vel < r.v:
            r.v = prm.max_vel
        r.v = p.factor * r.v
        if r.v < prm.min_vel:
            r.v = prm.min_vel
        if p.limit < r.v:
            r.v = p.limit
        if p.feed < r.v:
            r.v = p.feed
    pts[-1].v = 0.0
    pts[0].v = 0.0
    return pts


def _slow_start_end(pts: list[_Pt], prm: PlannerParams) -> list[_Pt]:
    """CAD 0x1010da50 (MotionCtrl 0x10017270 has the start part only).

    L0 = p6 (start), L1 = p8 (end); when the path is shorter than L0 + L1 both become half the
    path length.  Start (L0 >= 0.1): the first point i >= 1 with s_i + 0.01 > L0 is found; if
    |s_i - L0| > 0.05 a point is inserted at L0 (a copy of point 0 with v = w = min(w_0, p7));
    the speeds and segment limits of points 0..i (i = the inserted point) are capped to p7.
    End (L1 >= 0.1): searching backwards for the first point j with s_end - s_j > L1, a copy of
    point j+1 is inserted at s_end - L1 when it is more than 0.05 away; points j+2..end are
    capped to p9.
    """
    n = len(pts)
    if n < 2:
        return pts
    L1 = prm.end_slow_len
    s_end = pts[-1].s
    L0 = prm.start_slow_len
    if s_end < L1 + L0:
        L1 = 0.5 * s_end
        L0 = L1
    p7, p9 = prm.start_slow_vel, prm.end_slow_vel
    if 0.1 <= L0:
        for i in range(1, len(pts)):
            if pts[i].s + 0.01 <= L0:
                continue
            d = pts[i].s - L0
            if 0.05 < abs(d):
                rec = pts[0].copy()
                if p7 < rec.w:
                    rec.w = p7
                pts[i].ds = d
                rec.ds = L0 - pts[i - 1].s
                rec.v = rec.w
                rec.s = L0
                pts.insert(i, rec)
            for j in range(0, i + 1):
                r = pts[j]
                r.w = p7 if p7 <= r.w else r.w
                r.v = p7 if p7 <= r.v else r.v
            break
    if 0.1 <= L1:
        s_end = pts[-1].s
        for j in range(len(pts) - 2, -1, -1):
            if not s_end - pts[j].s > L1:
                continue
            if 0.05 < abs((s_end - pts[j].s) - L1):
                x = s_end - L1
                rec = pts[j + 1].copy()
                rec.ds = x - pts[j].s
                pts[j + 1].ds = pts[j + 1].s - x
                rec.s = x
                pts.insert(j + 1, rec)
            for k in range(len(pts) - 1, j + 1, -1):
                r = pts[k]
                r.w = p9 if p9 <= r.w else r.w
                r.v = p9 if p9 <= r.v else r.v
            break
    return pts


def _cbrt_pow(x: float) -> float:
    """MSVC pow(x, 1/3): nan for negative x."""
    if x != x or x < 0:
        return NAN
    return math.pow(x, 1.0 / 3.0)


def _gt(a: float, b: float) -> bool:
    """x87 ``fcomp; test $0x41; jp`` taken: a > b or unordered."""
    return a > b or a != a or b != b


class _BlockPlanner:
    """CAD 0x1010bbc0 state: points, A (+0x10), J (+0x18), acc_time (+0x20), period (+0x28),
    max_vel (+0x30)."""

    def __init__(self, pts: list[_Pt], A: float, J: float, acc_time: float, period: float,
                 vmax: float):
        self.p = pts
        self.A, self.J, self.T, self.period, self.Vmax = A, J, acc_time, period, vmax

    # --- helpers --------------------------------------------------------
    def _vmax(self, a: int, b: int) -> float:
        """0x10108720: max(v_a, w_(a+1..b))."""
        m = self.p[a].v
        for i in range(a + 1, b + 1):
            if m < self.p[i].w:
                m = self.p[i].w
        return m

    def _jb(self, vmax: float, vmin: float) -> float:
        """Adaptive block jerk ((vmax - vmin) * 0.6 / max_vel + 0.4) * J."""
        return ((vmax - vmin) * 0.6 / self.Vmax + 0.4) * self.J

    # --- valleys --------------------------------------------------------
    def _valleys(self, a: int, b: int) -> list[int]:
        """0x10109070: indices where the speed trend turns from falling to rising.

        Trend of step i-1 -> i (i = a+1 .. b-1): +1 if dv > 1, 0 if dv >= -1, else -1.  A pair
        (i-1, prev+trend) is recorded when trend*prev < 1 and (prev == -1 or trend == 1); then
        a sum of 0 (falling -> rising) gives index i-1, a sum of -1 (falling -> flat) gives the
        flat start and, when the next record is a rise (or it is the last record), also the next
        record's index.  ``prev`` is the previous trend, flat steps included.
        """
        if not 2 < b - a:
            return []
        idx, tr_sum = [], []
        prev = 0
        for i in range(a + 1, b):
            dv = self.p[i].v - self.p[i - 1].v
            if dv <= 1.0:
                tr = 0 if -1.0 <= dv else -1
            else:
                tr = 1
            if tr * prev < 1 and (prev == -1 or tr == 1):
                idx.append(i - 1)
                tr_sum.append(tr + prev)
            prev = tr
        out = []
        n = len(idx)
        k = 0
        while k < n:
            if tr_sum[k] == 0:
                out.append(idx[k])
            elif tr_sum[k] == -1 and (k == n - 1 or (k < n - 1 and tr_sum[k + 1] == 1)):
                out.append(idx[k])
                if k < n - 1:
                    out.append(idx[k + 1])
                    k += 1
            k += 1
        return out

    def _cap_pairs(self, V: list[int]) -> None:
        """0x10108590: for consecutive split points (a, b): if max(v_a, w_(a+1..b)) <
        1.2 max(v_a, v_b), w_b = min(w_b, max(v_a, v_b)).  (The loops index point b only, so the
        interior points are untouched, as in the DLL.)"""
        if len(V) < 3:
            return
        p = self.p
        for k in range(1, len(V)):
            a, b = V[k - 1], V[k]
            va, vb = p[a].v, p[b].v
            m = va
            for i in range(a + 1, b + 1):
                if m < p[i].w:
                    m = p[i].w
            vhi = vb if va <= vb else va
            if m < vhi * 1.2:
                if vhi < p[b].v and a + 1 < b:
                    p[b].v = vhi
                if vhi < p[b].w:
                    p[b].w = vhi

    # --- one block ------------------------------------------------------
    @staticmethod
    def _reach_speed(v0: float, J: float, L: float, lo: float, hi: float, tol: float) -> float:
        """0x10107b10: bisection for the speed v reachable from v0 over L with jerk-only
        acceleration: (v0+v)(v^2-v0^2) = J L^2."""
        v02 = v0 * v0
        c = L * J * L
        f_lo = (v0 + lo) * (lo * lo - v02) - c
        f_hi = (v0 + hi) * (hi * hi - v02) - c
        if abs(f_lo) < tol:
            return lo
        if not tol <= abs(f_hi):
            return hi
        if 0.0 < f_hi * f_lo:
            return lo if abs(f_hi) <= abs(f_lo) else hi
        if f_lo <= f_hi:
            x, y = hi, lo
        else:
            x, y = lo, hi
        for _ in range(4000):
            lo2 = y
            mid = (x + lo2) * 0.5
            f = (mid + v0) * (mid * mid - v02) - c
            if f <= tol and -tol <= f:
                return mid
            if 0.0 < f:
                x = mid
            else:
                y = mid
        return mid

    def _block(self, a: int, b: int, fixup: bool) -> tuple[bool, Profile]:
        """0x10108920: S-curve block a..b.  L = s_b - s_a, vs = v_a, ve = v_b, vmax =
        max(v_a, w_(a+1..b)), adaptive jerk.  When the solve is infeasible, the raised jerk is
        >= 1.2 J and ``fixup`` (only true for 2-point paths), the higher end speed is lowered to
        what a jerk-only ramp reaches (written back to the point) and the block is re-solved with
        A, J; for a decelerating block the function then returns False (no block is emitted)."""
        p = self.p
        blk = Profile(p[b].s - p[a].s, p[a].v, 0.0, p[b].v, self.A, 0.0)
        blk.vmax = self._vmax(a, b)
        blk.a, blk.b = a, b
        blk.J = self._jb(blk.vmax, blk.vs if blk.vs <= blk.ve else blk.ve)
        blk.A = self.A
        ok = scurve_solve(blk)
        dec = False
        if not ok and 1.2 <= blk.J / self.J and fixup:
            dec = blk.ve < blk.vs
            lo, hi = (blk.ve, blk.vs) if dec else (blk.vs, blk.ve)
            tol = blk.vmax * 0.01
            if 5.0 < tol:
                tol = 5.0
            v = self._reach_speed(lo, blk.J, blk.L, lo, hi, tol)
            if dec:
                blk.vs = v
            else:
                blk.ve = v
            p[a].v = blk.vs
            p[b].v = blk.ve
            blk.A, blk.J = self.A, self.J
            scurve_solve(blk)
        return (not dec), blk

    # --- violation search ------------------------------------------------
    def _fwd_check(self, blk: Profile, a: int, b: int, tol: float) -> int:
        """0x10108070: first point a+1..b-1 inside the acceleration side of the block whose
        limit v_i + max(1, tol v_i) the profile exceeds; -1 if none (scan stops after phase 3)."""
        J, A, vs, vm = blk.J, blk.A, blk.vs, blk.vmax
        t, s = blk.t, blk.s
        v1 = vs + J * t[0] * t[0] * 0.5
        v2 = vm - t[2] * J * t[2] * 0.5
        e1 = s[0]
        e2 = s[1] + e1
        e3 = s[2] + e2
        d = 0.0
        p = self.p
        for i in range(a + 1, b):
            d = p[i].ds + d
            m = p[i].v * tol
            if not 1.0 < m:
                m = 1.0
            lim = p[i].v + m
            if not d >= e1:
                if lim < v1:
                    pp = vs * 6.0 / J
                    qq = d * -6.0 / J
                    D = _sqrt(pp * pp * pp / 27.0 + qq * qq * 0.25)
                    h = qq * 0.5
                    tt = _cbrt_pow(D - h) - _cbrt_pow(h + D)
                    if not 0.0 > tt:
                        v = J * tt * tt * 0.5 + vs
                        if _gt(v, lim):
                            return i
            elif not d >= e2:
                v = _sqrt(v1 * v1 + (A + A) * (d - e1))
                if _gt(v, lim):
                    return i
            else:
                if e3 <= d:
                    return -1
                if vm > lim:
                    v = _sqrt(v2 * v2 + (_div(vm - v2, t[2]) * 2.0) * (d - e2))
                    if _gt(v, lim):
                        return i
        return -1

    def _back_check(self, blk: Profile, a: int, b: int, tol: float) -> int:
        """0x101082e0: mirror of ``_fwd_check`` from the block end (points b-1..a+1, limit
        v_i (1 + tol) + 8).  As in both DLLs, phase 5 uses t6 where t5 is meant."""
        J, A, ve, vm = blk.J, blk.A, blk.ve, blk.vmax
        t, s = blk.t, blk.s
        u1 = ve + J * t[6] * t[6] * 0.5
        u2 = vm - t[4] * J * t[4] * 0.5
        e1 = s[6]
        e2 = s[5] + e1
        e3 = s[4] + e2
        d = 0.0
        p = self.p
        for i in range(b - 1, a, -1):
            d = p[i + 1].ds + d
            lim = tol * p[i].v + p[i].v + 8.0
            if not d >= e1:
                if lim < u1:
                    pp = ve * 6.0 / J
                    qq = d * -6.0 / J
                    D = _sqrt(pp * pp * pp / 27.0 + qq * qq * 0.25)
                    h = qq * 0.5
                    tt = _cbrt_pow(D - h) - _cbrt_pow(h + D)
                    if not 0.0 > tt:
                        v = J * tt * tt * 0.5 + ve
                        if _gt(v, lim):
                            return i
            elif not d >= e2:
                if vm > lim:
                    v = _sqrt(u1 * u1 + (A + A) * (d - e1))
                    if _gt(v, lim):
                        return i
            else:
                if e3 <= d:
                    return -1
                if vm > lim:
                    v = _sqrt(u2 * u2 + (_div(vm - u2, t[5]) * 2.0) * (d - e2))
                    if _gt(v, lim):
                        return i
        return -1

    def _violation(self, blk: Profile, a: int, b: int, tol: float) -> tuple[int, int]:
        """0x10108b80: (code, index).  code 1 = re-plan [a, c] as one block and recurse on
        [c, b]; 3 = recurse on [a, c], one block [c, b]; 2 = recurse on both; 0 = keep.

        Decelerating/flat block: backward check (tol 1.25*precision) -> 3, else when
        vmax - vs > 0.001 the forward check (2*precision) -> 1; accelerating block: forward
        (1.25*precision) -> 1, else when vmax - ve > 0.001 backward (2*precision) -> 3.  Then the
        cruise phase: among points with s within [s1+s2+s3, +s4] whose v_i + max(0.5, tol v_i) <
        vmax, the one with the lowest v -> 2."""
        if b - a < 2:
            return 0, -1
        c = -1
        if blk.ve <= blk.vs:
            c = self._back_check(blk, a, b, tol * 1.25)
            if 0 < c:
                return 3, c
            if 0.001 < blk.vmax - blk.vs:
                c = self._fwd_check(blk, a, b, tol + tol)
            if 0 < c:
                return 1, c
        else:
            c = self._fwd_check(blk, a, b, tol * 1.25)
            if 0 < c:
                return 1, c
            if 0.001 < blk.vmax - blk.ve:
                c = self._back_check(blk, a, b, tol + tol)
            if 0 < c:
                return 3, c
        s = blk.s
        acc = s[1] + s[0] + s[2]
        cr = s[3]
        d = 0.0
        best = -1
        p = self.p
        for i in range(a + 1, b):
            d = p[i].ds + d
            if acc <= d:
                if cr + acc < d:
                    break
                m = 0.5
                if 0.5 <= p[i].v * tol:
                    m = p[i].v * tol
                if p[i].v + m < blk.vmax:
                    if best == -1 or p[i].v < p[best].v:
                        best = i
        if best != -1:
            return 2, best
        return 0, -1

    def _plan_range(self, a: int, b: int, fixup: bool, tol: float) -> list[Profile]:
        """0x1010a890: blocks for points a..b, split recursively at violations.

        code 0 -> [block]; 1 -> [block(a, c)] + plan(c, b); 3 -> plan(a, c) + [block(c, b)];
        2 -> plan(a, c) + plan(c, b).  The DLL recurses; this walks the same tree with an explicit
        stack (long paths would exceed Python's recursion limit) and emits blocks in order.
        """
        out: list[Profile] = []
        todo: list[tuple] = [("range", a, b, fixup)]
        while todo:
            task = todo.pop()
            if task[0] == "block":
                out.append(self._block(task[1], task[2], False)[1])
                continue
            _, lo, hi, fx = task
            if lo == hi:
                continue
            ok, blk = self._block(lo, hi, fx)
            if not ok:
                continue
            code, c = self._violation(blk, lo, hi, tol)
            if code == 0:
                out.append(blk)
            elif code == 1:
                out.append(self._block(lo, c, False)[1])
                todo.append(("range", c, hi, False))
            elif code == 3:
                todo.append(("block", c, hi))
                todo.append(("range", lo, c, False))
            else:
                todo.append(("range", c, hi, False))
                todo.append(("range", lo, c, False))
        return out

    def _split(self, a: int, b: int) -> list[Profile]:
        """0x1010ba00: split a..b at the valleys and plan each range (tolerance 0.05)."""
        if a == b:
            return []
        V = self._valleys(a, b)
        V.append(b)
        self._cap_pairs(V)
        fixup = len(self.p) < 3
        out: list[Profile] = []
        for k in range(len(V)):
            lo = a if k == 0 else V[k - 1]
            out.extend(self._plan_range(lo, V[k], fixup, 0.05))
        return out

    # --- post passes ------------------------------------------------------
    def _merge_dist(self, v0: float, v1: float) -> float:
        dv = v1 - v0
        if not 0.0 <= dv:
            return 0.0
        A, J = self.A, self.J
        if A * A / J <= dv:
            return (v0 + v1) * (A / J + dv / A) * 0.5
        return _sqrt(dv / J) * (v0 + v1)

    def _merge_steep(self, blocks: list[Profile]) -> list[Profile]:
        """0x1010b030: blocks whose solve raised the jerk (J_block >= 1.2 J).

        Backward pass: such a decelerating block absorbs the preceding blocks (taking their start
        speed and vmax) until the start speed drops below its end speed or the distance needed
        from ve to that start speed fits in the merged length; the merged block is re-solved
        with A and the adaptive jerk.  Forward pass: the same for accelerating blocks and the
        following blocks.
        """
        n = len(blocks)
        if n < 2:
            return blocks
        J = self.J
        rev: list[Profile] = []
        k = n - 1
        while k >= 0:
            blk = blocks[k]
            if blk.J < J * 1.2 or blk.vs < blk.ve:
                rev.append(blk)
                k -= 1
                continue
            m = blk.copy()
            ve = m.ve
            j = k - 1
            if j < 0:
                vs_new = m.vs
            else:
                while j >= 0:
                    prv = blocks[j]
                    m.a = prv.a
                    m.L = m.L + prv.L
                    m.vs = prv.vs
                    vs_new = prv.vs
                    m.vmax = prv.vmax
                    if vs_new < ve:
                        break
                    if self._merge_dist(ve, vs_new) < m.L:
                        break
                    j -= 1
            vmin = vs_new if vs_new <= ve else ve
            m.J = self._jb(m.vmax, vmin)
            m.A = self.A
            scurve_solve(m)
            rev.append(m)
            k = j - 1
        blocks = rev[::-1]
        n = len(blocks)
        out: list[Profile] = []
        k = 0
        while k < n:
            blk = blocks[k]
            if blk.J < J * 1.2 or blk.ve < blk.vs:
                out.append(blk)
                k += 1
                continue
            m = blk.copy()
            vs = m.vs
            j = k + 1
            ve_new = m.ve
            while j < n:
                nxt = blocks[j]
                m.b = nxt.b
                m.L = nxt.L + m.L
                m.vmax = nxt.vmax
                m.ve = nxt.ve
                ve_new = nxt.ve
                if ve_new < vs:
                    break
                if self._merge_dist(vs, ve_new) < m.L:
                    break
                j += 1
            vmin = ve_new if ve_new < vs else vs
            m.J = self._jb(m.vmax, vmin)
            m.A = self.A
            scurve_solve(m)
            out.append(m)
            k = j + 1
        return out

    def _ramp_class(self, blk: Profile) -> int:
        """0x10107930: +1 for a short (T <= 1.5 acc_time) almost pure acceleration block
        (accel time / T > 0.75, one side <= 1 ms), -1 for deceleration, else 0."""
        t = blk.t
        ta = t[1] + t[0] + t[2]
        td = t[5] + t[4] + t[6]
        T = t[3] + ta + td
        if T <= self.T * 1.5 and (ta <= 0.001 or td <= 0.001):
            if 0.75 < _div(ta, T):
                return 1
            if 0.75 < _div(td, T):
                return -1
        return 0

    def _merge_short(self, blocks: list[Profile]) -> list[Profile]:
        """0x1010ab40: runs of consecutive blocks with the same non-zero ramp class (runs of
        one included) are replaced by one accel-only block from the first block's start point
        to the last block's end point (0x10107f40: vs = v_a, ve = v_b, vmax = max(vs, ve),
        adaptive jerk)."""
        n = len(blocks)
        cls = [self._ramp_class(b) for b in blocks]
        out: list[Profile] = []
        k = 0
        p = self.p
        while k < n:
            if cls[k] == 0:
                out.append(blocks[k])
                k += 1
                continue
            j = k + 1
            while j < n and cls[j] == cls[k]:
                j += 1
            a, b = blocks[k].a, blocks[j - 1].b
            vs, ve = p[a].v, p[b].v
            vm = vs if ve < vs else ve
            m = Profile(p[b].s - p[a].s, vs, vm, ve, self.A, 0.0)
            m.J = self._jb(vm, vs if vs <= ve else ve)
            m.a, m.b = a, b
            accel_profile(m)
            out.append(m)
            k = j
        return out

    @staticmethod
    def _flatten(blocks: list[Profile]) -> None:
        """0x101087a0: a block whose vmax lies in [max(vs,ve) + 0.01, 1.33 max(vs,ve)] gets
        vmax = max(vs, ve) and is re-solved: S-curve when (the higher end's ramp time + cruise)
        / T >= 0.2 (times of the old profile), otherwise accel-only."""
        for blk in reversed(blocks):
            vhi = blk.ve if blk.vs < blk.ve else blk.vs
            if blk.vmax <= 1.33 * vhi and 0.01 + vhi <= blk.vmax:
                blk.vmax = vhi
                t = blk.t
                ta = t[1] + t[0] + t[2]
                td = t[5] + t[4] + t[6]
                x = td if blk.vs < blk.ve else ta
                if 0.2 <= _div(x + t[3], td + ta + t[3]):
                    scurve_solve(blk)
                else:
                    accel_profile(blk)

    def run(self) -> list[Profile]:
        """0x1010bbc0: split + plan, merge steep blocks, merge short ramps, flatten bumps."""
        n = len(self.p)
        if n < 2:
            return []
        blocks = self._split(0, n - 1)
        blocks = self._merge_steep(blocks)
        blocks = self._merge_short(blocks)
        self._flatten(blocks)
        return blocks


@port("0x1010df60", "0x10016720", source="CADModule", parity=("cad_motion", "motion"))
def plan_velocity(points: list[PathPoint], params: PlannerParams | None = None,
                  use_curvature: bool = True) -> Plan:
    """vt[1] (CAD 0x1010df60): plan a path; with curvature limits it starts and ends at rest."""
    prm = (params or PlannerParams()).clamped()
    if len(points) < 1:
        return Plan(period_ms=prm.period_ms)
    pts = _point_limits(list(points), prm, use_curvature)
    pts = _slow_start_end(pts, prm)
    J = prm.jerk()
    bp = _BlockPlanner(pts, prm.max_acc, J, prm.acc_time, prm.period_ms, prm.max_vel)
    blocks = bp.run()
    plan = Plan(point_vel=[r.v for r in pts], period_ms=prm.period_ms)
    s0 = 0.0
    for blk in blocks:
        plan.starts.append(s0)
        plan.blocks.append(blk)
        s0 = blk.L + s0
    return plan


def seg_time(length: float, vmax: float, acc: float, jerk: float,
             vs: float = 0.0, ve: float = 0.0) -> float:
    """Time (s) of one S-curve move (== segInterp_time / 1000)."""
    from .profile import scurve_profile
    if length <= 0:
        return 0.0
    return scurve_profile(length, vs, vmax, ve, acc, jerk).duration
