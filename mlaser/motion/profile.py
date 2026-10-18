"""1-D motion profiles ported from MotionCtrl.dll (MotionCtrl_dll_V1.3.22) and the v1.3.23 copy
statically linked into CADModule.dll (the copy MainApp actually uses, see docs/motion.md §1).

Profile record
--------------
Both DLLs keep every profile in a ``double[20]`` (0xA8-byte entries inside the planner; the last
8 bytes hold the two point indices of a planner block)::

    [0] L     length (mm)
    [1] vs    start velocity (mm/s)
    [2] vmax  cruise velocity (mm/s), may be lowered by the solver
    [3] ve    end velocity (mm/s)
    [4] A     max acceleration (mm/s^2), may be changed by the solver
    [5] J     jerk (mm/s^3), may be changed by the solver
    [6..12]   durations t1..t7 of the seven phases (s)
    [13..19]  distances s1..s7 of the seven phases (mm)

Phases: 1 jerk+, 2 const accel, 3 jerk-, 4 cruise, 5 jerk-, 6 const decel, 7 jerk+.

=========================  ===================  ======================  =====================
function                   MotionCtrl           CADModule (v1.3.23)     Python
=========================  ===================  ======================  =====================
S-curve solver             FUN_1000eeb0         FUN_101064b0            ``scurve_profile``
distance v0->v1            FUN_1000ecc0         FUN_101062c0            ``accel_distance``
bisection                  FUN_1000ed30         FUN_10106330            ``_bisect``
  f(v), low side saturated FUN_1000ec70         FUN_10106270            ``_f_sat_lo``
  f(v), jerk-only ramps    FUN_1000ec20         FUN_10106230            ``_f_jerk``
accel-only profile         FUN_100112c0         FUN_10107d20            ``accel_profile``
trapezoid (mode != 2)      FUN_1000f5a0         FUN_10106bb0            ``trapezoid_profile``
fixed-period sampler       FUN_1000f730 (/L)    FUN_101094e0 (planner)  ``Profile.sample_from``
=========================  ===================  ======================  =====================

The two copies of each function are the same code (constants and branch structure compared,
docs/motion.md §2).  All arithmetic follows the x87 code operation by operation; the original runs
with 53-bit precision control, so plain Python floats reproduce it.

Units: mm, mm/s, mm/s^2, mm/s^3; the interpolation period is in **milliseconds** (``period*0.001``
in FUN_1000f730 / FUN_101094e0); ``segInterp_time`` returns the summed durations ``* 1000``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

NAN = float("nan")


def _div(a: float, b: float) -> float:
    """x87 division semantics (no exception): x/0 = +-inf, 0/0 = nan."""
    try:
        return a / b
    except ZeroDivisionError:
        if a == 0 or a != a:
            return NAN
        return math.copysign(math.inf, a) * math.copysign(1.0, b)


def _sqrt(x: float) -> float:
    """MSVC _CIsqrt: negative or nan argument gives nan (no exception)."""
    if x != x or x < 0:
        return NAN
    return math.sqrt(x)


def accel_distance(v0: float, v1: float, a: float, j: float) -> float:
    """Distance needed to go from v0 to v1 (v1 >= v0) with jerk-limited accel.

    Port of FUN_1000ecc0 / CAD FUN_101062c0: if dv < a^2/j the acceleration never saturates and
    the time is 2*sqrt(dv/j); otherwise it is dv/a + a/j.  The distance is the mean speed times
    that time.  Returns 0 for dv < 0.
    """
    s = v0 + v1
    dv = v1 - v0
    if dv < 0:
        return 0.0
    if dv < _div(a * a, j):
        return _sqrt(_div(dv, j)) * s
    return s * (_div(dv, a) + _div(a, j)) * 0.5


def _f_sat_lo(vlo, vhi, A, J, L, x):
    """FUN_1000ec70: d(vlo->x) with saturated acceleration + jerk-only d(vhi->x) - L."""
    return ((_div(A, J) + _div(x - vlo, A)) * (vlo + x) * 0.5 + _sqrt(_div(x - vhi, J)) * (vhi + x)) - L


def _f_jerk(vlo, vhi, A, J, L, x):
    """FUN_1000ec20: jerk-only d(vlo->x) + jerk-only d(vhi->x) - L."""
    return ((vhi + x) * _sqrt(_div(x - vhi, J)) + _sqrt(_div(x - vlo, J)) * (vlo + x)) - L


def _bisect(fn, lo: float, hi: float, tol: float) -> float:
    """FUN_1000ed30 / CAD FUN_10106330: bisection for fn(v)=0 on [lo, hi] with |fn| <= tol.

    If the end values have the same sign the end with the smaller |fn| is returned (the low end
    when |fn(lo)| < |fn(hi)|).  Midpoints are (neg + pos) * 0.5.
    """
    flo, fhi = fn(lo), fn(hi)
    if abs(flo) < tol:
        return lo
    if not abs(fhi) >= tol:          # also true for nan, like the x87 compare
        return hi
    if not fhi * flo <= 0.0:
        return lo if abs(flo) < abs(fhi) else hi
    neg, pos = (hi, lo) if fhi < flo else (lo, hi)
    total = neg + pos
    for _ in range(2000):            # the DLL loops until |f| <= tol; 2000 halvings always end it
        mid = total * 0.5
        fm = fn(mid)
        if fm <= tol and -tol <= fm:
            return mid
        if fm <= 0.0:
            total = mid + pos
            neg = mid
        else:
            total = neg + mid
            pos = mid
    return mid


@dataclass
class Profile:
    L: float
    vs: float
    vmax: float
    ve: float
    A: float
    J: float
    t: list[float] = field(default_factory=lambda: [0.0] * 7)
    s: list[float] = field(default_factory=lambda: [0.0] * 7)
    feasible: bool = True   # False when the solver had to change A/J (return value of FUN_1000eeb0)
    a: int = 0              # planner blocks: first point index (record +0xa0)
    b: int = 0              # planner blocks: last point index (record +0xa4)

    @property
    def duration(self) -> float:
        t = self.t
        return t[0] + t[1] + t[2] + t[3] + t[4] + t[5] + t[6]

    def copy(self) -> "Profile":
        return Profile(self.L, self.vs, self.vmax, self.ve, self.A, self.J, list(self.t), list(self.s),
                       self.feasible, self.a, self.b)

    # --- kinematics -----------------------------------------------------
    def _pos(self, k: int, tt: float, e: list[float], base: list[float]) -> float:
        """Position at time tt inside phase k (formulas of FUN_101094e0 / FUN_1000f730)."""
        J, A, vm, t = self.J, self.A, self.vmax, self.t
        if k == 0:
            return self.vs * tt + (tt * (tt * tt) * J) / 6.0
        if k == 1:
            u = tt - e[0]
            return u * (J * t[0] * t[0] * 0.5 + self.vs) + base[1] + A * u * u * 0.5
        if k == 2:
            u = t[2] - (tt - e[1])
            return (self.s[2] + base[2]) - (vm * u - (J * (u * (u * u))) / 6.0)
        if k == 3:
            return (tt - e[2]) * vm + base[3]
        if k == 4:
            u = tt - e[3]
            return (vm * u + base[4]) - (u * (u * u) * J) / 6.0
        if k == 5:
            u = tt - e[4]
            return (u * (vm - t[4] * J * t[4] * 0.5) + base[5]) - A * u * u * 0.5
        u = t[6] - (tt - (e[4] + t[5]))
        return (self.s[6] + self.s[5] + base[5]) - ((u * (u * u) * J) / 6.0 + self.ve * u)

    def _ends(self):
        e, acc = [], 0.0
        for x in self.t:
            acc = acc + x
            e.append(acc)
        base, b = [0.0], 0.0
        for x in self.s[:6]:
            b = x + b
            base.append(b)
        return e, base

    def _vel(self, k: int, u: float) -> float:
        J, t = self.J, self.t
        if k == 0:
            return self.vs + J * u * u / 2
        if k == 1:
            return self.vs + J * t[0] * t[0] / 2 + self.A * u
        if k == 2:
            r = t[2] - u
            return self.vmax - J * r * r / 2
        if k == 3:
            return self.vmax
        if k == 4:
            return self.vmax - J * u * u / 2
        if k == 5:
            return self.vmax - J * t[4] * t[4] / 2 - self.A * u
        r = t[6] - u
        return self.ve + J * r * r / 2

    def state(self, tt: float) -> tuple[float, float]:
        """(position, velocity) at time tt (continuous evaluation of the sampler formulas)."""
        if tt <= 0:
            return 0.0, self.vs
        if tt >= self.duration:
            return self.L, self.ve
        e, base = self._ends()
        k = 0
        while k < 6 and tt >= e[k]:
            k += 1
        start = e[k - 1] if k else 0.0
        return self._pos(k, tt, e, base), self._vel(k, tt - start)

    def position(self, tt: float) -> float:
        return self.state(tt)[0]

    def velocity(self, tt: float) -> float:
        return self.state(tt)[1]

    def time_at(self, pos: float) -> float:
        """Inverse of position() (monotone), by bisection."""
        if pos <= 0:
            return 0.0
        T = self.duration
        if pos >= self.L:
            return T
        lo, hi = 0.0, T
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if self.position(mid) < pos:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)

    def sample_from(self, t0: float, dt: float) -> tuple[list[float], float]:
        """Positions at t0, t0+dt, ... (accumulated by repeated addition) while t <= T.

        Phase-by-phase loop of FUN_101094e0 (planner block sampler) and FUN_1000f730 (segInterp):
        phases 1..6 are sampled while t < phase end, phase 7 while t <= T.  Returns the positions
        and the last sampled time (or ``None`` when nothing was sampled).
        """
        e, base = self._ends()
        T = self.duration
        out: list[float] = []
        tt = t0
        last = None
        for k in range(7):
            end = e[k]
            if k < 6:
                while tt < end:
                    out.append(self._pos(k, tt, e, base))
                    last = tt
                    tt = tt + dt
            else:
                while tt <= T:
                    out.append(self._pos(k, tt, e, base))
                    last = tt
                    tt = tt + dt
        return out, tt

    def sample(self, period_ms: float) -> list[float]:
        """FUN_1000f730 (segInterp output): s/L at t = 0, dt, 2dt, ... while t <= T, then every
        value is multiplied by 1/(last value), so the last sample is exactly 1.  No extra end
        sample is appended (D-01).  Moves shorter than half a period give no samples at all
        (count estimate trunc(T/period + 0.5) < 1)."""
        n = self.duration / period_ms * 1000.0 + 0.5
        if not n >= 1.0:            # _ftol2(T/period*1000 + 0.5) < 1: the DLL returns no samples
            return []
        dt = period_ms * 0.001
        pos, _ = self.sample_from(0.0, dt)
        if not pos:
            return []
        L = self.L
        out = [_div(p, L) for p in pos]
        r = _div(1.0, out[-1])
        return [x * r for x in out]


def _fill_distances(p: Profile, vlo: float, vhi: float, swapped: bool) -> None:
    """Tail of FUN_1000eeb0 (LAB_1000f44f / LAB_1000f504): s1..s7 from the durations, cruise =
    the remainder, t4 = s4/vmax (may be a tiny negative number, as in the DLL), then the phases
    are mirrored when vs > ve."""
    J, A, vm = p.J, p.A, p.vmax
    t = p.t
    s = [0.0] * 7
    s[0] = t[0] * vlo + (J * t[0] * t[0] * t[0]) / 6.0
    s[1] = t[1] * A * t[1] * 0.5 + (J * t[0] * t[0] * 0.5 + vlo) * t[1]
    s[2] = vm * t[2] - (J * t[2] * t[2] * t[2]) / 6.0
    s[4] = vm * t[4] - (J * t[4] * t[4] * t[4]) / 6.0
    s[5] = (vm - t[4] * J * t[4] * 0.5) * t[5] - A * t[5] * t[5] * 0.5
    s[6] = t[6] * vhi + (J * t[6] * t[6] * t[6]) / 6.0
    s[3] = (((((p.L - s[0]) - s[1]) - s[2]) - s[4]) - s[5]) - s[6]
    t[3] = _div(s[3], vm)
    p.s = s
    if swapped:
        p.t = [t[6], t[5], t[4], t[3], t[2], t[1], t[0]]
        p.s = [s[6], s[5], s[4], s[3], s[2], s[1], s[0]]


def scurve_solve(p: Profile) -> bool:
    """FUN_1000eeb0 / CAD FUN_101064b0 on a profile record (L, vs, vmax, ve, A, J set).

    The lower end speed vlo is planned first (phases are mirrored at the end when vs > ve):

    1. d(vhi->vmax) + d(vlo->vmax) < L: keep vmax, cruise fills the rest.
    2. Else with v3 = A^2/J + vhi (vhi side just saturates the acceleration):
       a. if L > (v3+vhi)*A/J + d(vlo->v3), both ramps saturate: closed form
          vmax = (sqrt(A^4 + 2J(J(vlo^2+vhi^2) - A^2(vlo+vhi) + 2AJL)) - A^2) / 2J.
       b. else if d(vlo->vhi) <= L, bisection (tolerance min(vmax/1000, 0.001)) between
          v4 = max(vhi, A^2/J + vlo) and v3 with the low side saturated, or (when even v4 is too
          far) between vhi and v4 with both ramps jerk-only; for L < 0.01 a constant-acceleration
          closed form vmax = sqrt(A L + vlo^2 + vhi^2).
       c. else (vhi not reachable): A = (vhi-vlo)(vhi+vlo)/L, J = A(vhi+vlo)/L, pure jerk
          ramp vlo->vhi; returns False.
    """
    vs, ve = p.vs, p.ve
    if vs <= ve:
        vlo, vhi = vs, ve
    else:
        vlo, vhi = ve, vs
    swapped = vs > ve
    if p.vmax < vhi:
        p.vmax = vhi
    A, J, L = p.A, p.J, p.L
    AA = A * A
    feasible = True
    t = [0.0] * 7
    d_hi = accel_distance(vhi, p.vmax, A, J)
    d_lo = accel_distance(vlo, p.vmax, A, J)
    if d_lo + d_hi < L:
        vm = p.vmax
        if _div(AA, J) <= vm - vlo:
            t[0] = t[2] = _div(A, J)
            t[1] = _div((vm - vlo) - J * t[0] * t[0], A)
        else:
            t[0] = t[2] = _sqrt(_div(vm - vlo, J))
        if _div(AA, J) <= vm - vhi:
            t[4] = t[6] = _div(A, J)
            t[5] = _div((vm - vhi) - J * t[4] * t[4], A)
        else:
            t[4] = t[6] = _sqrt(_div(vm - vhi, J))
        p.t = t
        _fill_distances(p, vlo, vhi, swapped)
        p.feasible = True
        return True
    v3 = _div(AA, J) + vhi
    if L <= _div((v3 + vhi) * A, J) + accel_distance(vlo, v3, A, J):
        if accel_distance(vlo, vhi, A, J) <= L:
            v2 = _div(A * A, J) + vlo
            v4 = v2 if vhi < v2 else vhi
            tol = p.vmax / 1000.0
            if 0.001 < tol:
                tol = 0.001
            if accel_distance(vhi, v4, A, J) + accel_distance(vlo, v4, A, J) <= L:
                p.vmax = _bisect(lambda x: _f_sat_lo(vlo, vhi, A, J, L, x), v4, v3, tol)
                t[0] = t[2] = _div(A, J)
                t[1] = _div((p.vmax - t[2] * J * t[2] * 0.5) - (0.5 * J * t[0] * t[0] + vlo), A)
            else:
                if L < 0.01:
                    p.vmax = _sqrt(A * L + vlo * vlo + vhi * vhi)
                    t[1] = _div(p.vmax - vlo, A)
                    t[5] = _div(p.vmax - vhi, A)
                    p.t = t
                    _fill_distances(p, vlo, vhi, swapped)
                    p.feasible = True
                    return True
                p.vmax = _bisect(lambda x: _f_jerk(vlo, vhi, A, J, L, x), vhi, v4, tol)
                t[0] = t[2] = _sqrt(_div(p.vmax - vlo, J))
            t[4] = t[6] = _sqrt(_div(p.vmax - vhi, J))
        else:
            A2 = _div((vhi - vlo) * (vhi + vlo), L)
            J2 = _div(A2 * (vhi + vlo), L)
            p.A, p.J = A2, J2
            p.vmax = _div(A2 * A2, J2) + vlo
            t[0] = t[2] = _div(A2, J2)
            feasible = False
    else:
        A4 = AA * AA
        J2x = J * 2.0
        x = ((vhi * vhi + vlo * vlo) * J - (vlo + vhi) * AA) + A * 2.0 * J * L
        p.vmax = _div(_sqrt(A4 + J2x * x) - AA, J2x)
        tj = _div(A, J)
        t = [tj, 0.0, tj, 0.0, tj, 0.0, tj]
        t[1] = _div((p.vmax - vlo) - J * tj * tj, A)
        t[5] = _div((p.vmax - vhi) - J * tj * tj, A)
    p.t = t
    _fill_distances(p, vlo, vhi, swapped)
    p.feasible = feasible
    return feasible


def scurve_profile(L: float, vs: float, vmax: float, ve: float, A: float, J: float) -> Profile:
    """7-phase jerk limited profile (FUN_1000eeb0 / CAD FUN_101064b0), see ``scurve_solve``."""
    p = Profile(L, vs, vmax, ve, A, J)
    if L <= 0:
        p.vmax = max(vmax, vs, ve)
        return p
    scurve_solve(p)
    return p


def accel_profile(p: Profile) -> bool:
    """FUN_100112c0 / CAD FUN_10107d20: one ramp vlo -> vhi that exactly fills L (no cruise).

    Used by the planner's merge and flatten passes.  With S = vhi+vlo, D = vhi-vlo:

    * D < 0.001: constant speed, t4 = L/vlo (A field set to S, as in the DLL).
    * jerk-only ramp longer than L (sqrt(D/J)*S >= L): A = D*S/L, J = A*S/L, no constant phase.
    * otherwise J is halved when sqrt(2D/J)*S < L, then A = J*L/S - J*sqrt((L/S)^2 - D/J),
      t1 = t3 = A/J, t2 = (D - J t1^2)/A.

    Phases 4..7 are zero (mirrored when vs > ve).  Returns True unless D < 0.001.
    """
    vs, ve = p.vs, p.ve
    if ve < vs:
        vlo, vhi, swapped = ve, vs, True
    else:
        vlo, vhi, swapped = vs, ve, False
    if not vhi <= p.vmax:
        p.vmax = vhi
    t = [0.0] * 7
    s = [0.0] * 7
    S = vhi + vlo
    D = vhi - vlo
    L = p.L
    ret = False
    if not 0.001 <= D:
        p.A = S
        t[3] = _div(L, vlo)
        s[3] = L
    else:
        ret = True
        J = p.J
        r = _sqrt(_div(D, J)) * S
        if not (r < L or r != r):          # x87: unordered compares take the "<" branch here
            A = _div(D * S, L)
            p.A = A
            p.J = _div(A * S, L)
            t[0] = t[2] = _div(p.A, p.J)
        else:
            if _sqrt(_div(D, J * 0.5)) * S < L:
                p.J = J * 0.5
            J = p.J
            p.A = _div(J * L, S) - _sqrt(_div(L, S) * _div(L, S) - _div(D, J)) * J
            t[0] = t[2] = _div(p.A, J)
            t[1] = _div(D - t[0] * t[0] * J, p.A)
    J, A = p.J, p.A
    s[0] = _div(J * t[0] * t[0] * t[0], 6.0) + t[0] * vlo
    s[1] = S * 0.5 * t[1]
    s[2] = t[2] * vhi - _div(J * t[2] * t[2] * t[2], 6.0)
    if swapped:
        t = [t[6], t[5], t[4], t[3], t[2], t[1], t[0]]
        s = [s[6], s[5], s[4], s[3], s[2], s[1], s[0]]
    p.t, p.s = t, s
    return ret


def trapezoid_profile(L: float, vs: float, vmax: float, ve: float, A: float) -> Profile:
    """Constant-acceleration profile (segInterp mode != 2): FUN_1000f5a0 / CAD FUN_10106bb0.

    Both DLL copies have two bugs on this path that the rebuild does NOT copy (D-02, accepted):
    the cruise **distance** L-sa-sd is stored in the cruise-time slot t4, and the triangle peak is
    sqrt(A L + vhi^2) instead of sqrt(A L + (vs^2+ve^2)/2).  The path is unreachable in the
    shipped application: MotionCtrl.dll is never loaded and every MainApp call of CADModule's
    segInterp copy (ICADModule vtable slots 0x1ec/0x1f0) passes mode 2 (docs/motion.md §2).
    """
    vlo, vhi = (vs, ve) if vs <= ve else (ve, vs)
    vmax = max(vmax, vhi)
    p = Profile(L, vs, vmax, ve, A, 0.0)
    ta = (vmax - vs) / A
    td = (vmax - ve) / A
    da = (vmax + vs) * ta * 0.5
    dd = (vmax + ve) * td * 0.5
    cruise = L - da - dd
    if cruise < 0:
        need = (vhi + vlo) * (vhi - vlo) / (2 * A)
        if need <= L:
            vp = math.sqrt(A * L + (vs * vs + ve * ve) / 2)
            p.vmax = vp
            ta, td = (vp - vs) / A, (vp - ve) / A
            da, dd = (vp * vp - vs * vs) / (2 * A), (vp * vp - ve * ve) / (2 * A)
            cruise = 0.0
        else:
            # not reachable: the DLL raises A so that vlo->vhi fits exactly in L
            p.feasible = False
            p.vmax = vhi
            T = 2 * L / (vlo + vhi) if vlo + vhi > 0 else 0.0
            p.A = (vhi - vlo) / T if T > 0 else A
            if vs <= ve:
                ta, td, da, dd = T, 0.0, L, 0.0
            else:
                ta, td, da, dd = 0.0, T, 0.0, L
            cruise = 0.0
    p.t = [0.0, ta, 0.0, (cruise / p.vmax) if p.vmax > 0 else 0.0, 0.0, td, 0.0]
    p.s = [0.0, da, 0.0, max(cruise, 0.0), 0.0, dd, 0.0]
    return p


def make_profile(L, vs, vmax, ve, A, J, mode: int = 2) -> Profile:
    """Mode 2 = S-curve, anything else = trapezoid (FUN_10010090 / CAD FUN_10106d40 dispatch)."""
    if mode == 2:
        return scurve_profile(L, vs, vmax, ve, A, J)
    return trapezoid_profile(L, vs, vmax, ve, A)
