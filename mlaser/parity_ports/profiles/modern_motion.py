"""Modern velocity planner: own look-ahead plus Ruckig Community per segment (profile "modern-ruckig").

**Modern planner, NOT validated on hardware; validate against the simulator and real traces
before cutting.**  It is optional, never the default, and has no parity constraint: the default
profile ``"original"`` keeps the bit-exact CVelocityPlanning port
(:mod:`mlaser.parity_ports.cadmodule.planner`).  Select it with::

    from mlaser.parity_ports import use_profile
    with use_profile("modern-ruckig"):
        job = compile_job(doc)

It needs the ``ruckig`` extra (``pip install mlaser[ruckig]``; Ruckig Community, MIT).  Only the
Community features are used: one 1-DOF state-to-state trajectory per segment, offline
(``Ruckig(1).calculate``).  Intermediate waypoints are a Ruckig Pro / cloud-API feature and are
never used.

What it replaces: the ``motion`` slot's ``plan_velocity`` only.  It takes the same input (the
:class:`~mlaser.parity_ports.cadmodule.planner.PathPoint` list the smoother or ``path_points``
builds, and :class:`~mlaser.parity_ports.cadmodule.planner.PlannerParams`) and returns the same
:class:`~mlaser.parity_ports.cadmodule.planner.Plan` (blocks with ``L``, ``duration``,
``state``, ``sample_from``), so ``Plan.sample``, the 1 ms sampling, pulse quantisation and frames
downstream are unchanged.  Rapids, segInterp/arcInterp and the smoother stay the faithful ports.

Algorithm (our own; ideas common to look-ahead planners, nothing copied):

1. Point limits.  Junction and curvature limits come from each point's radius: the smoother
   turns a corner into a curve whose radius it reports, ``path_points`` gives a corner the
   junction-deviation radius ``R = tol c / (1 - c)``, ``c = cos(deflection / 2)``
   (:func:`mlaser.motion.interp.corner_radius`), and an arc its radius.  The centripetal law
   ``v <= sqrt(a_max R)`` turns that into a speed (for the junction radius this is the classic
   junction-deviation formula).  Then the caps of the point (``max_vel``, ``factor``, ``limit``,
   ``feed``); the limit between two points is ``min(feed, max(v_(i-1), v_i))`` as in the
   original.  The path starts and ends at rest.  Slow start / slow end lengths are honoured.
2. Dense backward / forward passes with ``v^2 = v0^2 + 2 a ds`` over every point.
3. Segment boundaries: the valleys of that speed profile (plus the path ends).  A second
   backward / forward pass over the boundaries uses the exact jerk-limited distance of a
   rest-acceleration to rest-acceleration speed change, so every segment can reach its end speed
   (and the path can always stop within the remaining length).
4. Per segment one Ruckig 1-DOF trajectory along the arc length: position 0 -> L, velocity
   v_start -> v_end, acceleration 0 at both ends, ``v_max`` = the peak the passes allow,
   ``a_max = PlannerParams.max_acc``, ``j_max = PlannerParams.jerk()`` (the values the faithful
   planner uses), ``min_velocity = 0``.  The trajectory is checked against every point and
   segment limit inside the segment; a segment that exceeds one is split at its worst point and
   step 3 is repeated.

Limits respected by construction: tangential speed, acceleration and jerk along the path.  Not
modelled: the centripetal jerk and the combination of tangential and centripetal acceleration
(the faithful planner does not model them either).  This is why it needs hardware validation.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from ..cadmodule.planner import PathPoint, Plan, PlannerParams
from ..cadmodule.profile import Profile
from ..profile import ORIGINAL, ParityProfile, register_profile

log = logging.getLogger(__name__)

NAME = "modern-ruckig"
WARNING = ("modern planner, NOT validated on hardware; validate against the simulator and real "
           "traces before cutting")
DESCRIPTION = (f"look-ahead + Ruckig Community (MIT) jerk-limited planner per segment; {WARNING}")

#: relative / absolute slack of the limit checks (mm/s)
_REL_TOL = 1e-6
_ABS_TOL = 1e-6
#: share of a segment the boundary passes leave unused: Ruckig rejects a speed change that needs
#: exactly the whole segment (it solves within ~1e-6 relative), so the passes plan on (1 - this)
_LEN_MARGIN = 1e-5


class RuckigMissing(ImportError):
    """The ``ruckig`` extra is not installed."""


def _ruckig() -> Any:
    try:
        import ruckig
    except ImportError as e:
        raise RuckigMissing(
            f"the {NAME!r} parity profile needs the optional 'ruckig' package "
            "(Ruckig Community, MIT): pip install 'mlaser[ruckig]' or uv sync --extra ruckig") from e
    return ruckig


# --------------------------------------------------------------------------- blocks
@dataclass
class RuckigBlock(Profile):
    """One segment: a Ruckig 1-DOF trajectory along the arc length (0 -> L).

    A :class:`Profile` (so ``Plan`` and its users see the usual fields: ``L``, ``vs``, ``vmax``,
    ``ve``, ``A``, ``J``, the seven phase durations ``t`` and distances ``s``, point indices
    ``a``/``b``), evaluated from Ruckig's own phase polynomials: phase k starts at position
    ``pk[k]``, velocity ``vk[k]``, acceleration ``ak[k]`` and has constant jerk ``jk[k]``.
    """
    jk: list[float] = field(default_factory=list)
    pk: list[float] = field(default_factory=list)
    vk: list[float] = field(default_factory=list)
    ak: list[float] = field(default_factory=list)

    def copy(self) -> "RuckigBlock":
        return RuckigBlock(self.L, self.vs, self.vmax, self.ve, self.A, self.J, list(self.t),
                           list(self.s), self.feasible, self.a, self.b, list(self.jk),
                           list(self.pk), list(self.vk), list(self.ak))

    def _eval(self, tt: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """(position, velocity, acceleration, jerk) at the times ``tt`` (clamped to [0, T])."""
        ends = np.cumsum(self.t)
        T = float(ends[-1])
        tt = np.clip(tt, 0.0, T)
        k = np.minimum(np.searchsorted(ends, tt, side="right"), 6)
        start = np.concatenate(([0.0], ends[:-1]))[k]
        u = tt - start
        j = np.asarray(self.jk)[k]
        a0 = np.asarray(self.ak)[k]
        v0 = np.asarray(self.vk)[k]
        p0 = np.asarray(self.pk)[k]
        p = p0 + u * (v0 + u * (a0 * 0.5 + u * j / 6.0))
        v = v0 + u * (a0 + u * j * 0.5)
        a = a0 + u * j
        return np.clip(p, 0.0, self.L), np.maximum(v, 0.0), a, j

    def state(self, tt: float) -> tuple[float, float]:
        if tt <= 0:
            return 0.0, self.vs
        if tt >= self.duration:
            return self.L, self.ve
        p, v, _, _ = self._eval(np.asarray([tt]))
        return float(p[0]), float(v[0])

    def sample_from(self, t0: float, dt: float) -> tuple[list[float], float]:
        """Positions at t0, t0 + dt, ... while t <= T (the contract of ``Profile.sample_from``
        that ``Plan.sample`` uses); returns them and the next sample time."""
        T = self.duration
        if t0 > T:
            return [], t0
        n = int(math.floor((T - t0) / dt)) + 1
        while n > 0 and t0 + (n - 1) * dt > T:
            n -= 1
        while t0 + n * dt <= T:
            n += 1
        tt = t0 + dt * np.arange(n, dtype=float)
        p, _, _, _ = self._eval(tt)
        return [float(x) for x in p], t0 + n * dt

    def sample(self, period_ms: float) -> list[float]:
        """Normalised samples s/L at 0, dt, ... (``Profile.sample`` contract, ends at 1)."""
        pos, _ = self.sample_from(0.0, period_ms * 0.001)
        if not pos or pos[-1] <= 0:
            return []
        r = 1.0 / pos[-1]
        return [x * r for x in pos]


def _ruckig_block(L: float, vs: float, ve: float, vmax: float, A: float,
                  J: float) -> RuckigBlock | None:
    """The Ruckig trajectory of one segment, or None when Ruckig finds no solution."""
    rk = _ruckig()
    otg = rk.Ruckig(1)
    inp = rk.InputParameter(1)
    inp.current_position = [0.0]
    inp.current_velocity = [vs]
    inp.current_acceleration = [0.0]
    inp.target_position = [L]
    inp.target_velocity = [ve]
    inp.target_acceleration = [0.0]
    inp.max_velocity = [max(vmax, vs, ve, 1e-9)]
    inp.min_velocity = [0.0]
    inp.max_acceleration = [A]
    inp.max_jerk = [J]
    traj = rk.Trajectory(1)
    try:
        res = otg.calculate(inp, traj)
    except rk.RuckigError:
        return None
    if res not in (rk.Result.Working, rk.Result.Finished):
        return None
    prof = traj.profiles[0][0]
    if prof.brake is not None and prof.brake.duration > 0:
        return None                        # start state outside the limits: not expected
    t = [float(x) for x in prof.t]
    p = [float(x) for x in prof.p]
    return RuckigBlock(L, vs, float(max(prof.v)), ve, A, J, t,
                       [p[k + 1] - p[k] for k in range(7)], True, 0, 0,
                       [float(x) for x in prof.j], p, [float(x) for x in prof.v],
                       [float(x) for x in prof.a])


# --------------------------------------------------------------------------- kinematics
def scurve_distance(v0: float, v1: float, A: float, J: float) -> float:
    """Distance of the fastest speed change v0 -> v1 that starts and ends with zero
    acceleration (|a| <= A, |jerk| <= J); symmetric, so distance = mean speed * time."""
    dv = abs(v1 - v0)
    if dv <= A * A / J:
        T = 2.0 * math.sqrt(dv / J)
    else:
        T = dv / A + A / J
    return 0.5 * (v0 + v1) * T


def reachable_speed(v0: float, L: float, A: float, J: float, cap: float) -> float:
    """The largest v1 in [v0, cap] with ``scurve_distance(v0, v1) <= L`` (also the largest
    start speed that can still slow down to v0 within L)."""
    if cap <= v0 or scurve_distance(v0, cap, A, J) <= L:
        return cap
    lo, hi = v0, cap
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if scurve_distance(v0, mid, A, J) <= L:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-12 * max(1.0, hi):
            break
    return lo


# --------------------------------------------------------------------------- limits
@dataclass
class _Limits:
    s: list[float]      # arc length of the points (from 0)
    v: list[float]      # point speed limit
    w: list[float]      # w[i]: limit between point i-1 and i (w[0] unused)


def _limits(points: Sequence[PathPoint], prm: PlannerParams, use_curvature: bool) -> _Limits:
    A = prm.max_acc
    s0 = points[0].s
    s: list[float] = []
    raw: list[float] = []
    v: list[float] = []
    feed: list[float] = []
    for p in points:
        if use_curvature:
            r = math.sqrt(A * max(p.radius, 0.0)) if p.radius > 0 else 0.0
            r = min(r, prm.max_vel)
            lim = min(r * p.factor, p.limit, p.feed)
        else:
            r = min(p.limit, prm.max_vel)
            lim = r
        si = p.s - s0
        if s and si - s[-1] <= 1e-9:            # duplicate point: keep the stricter limits
            raw[-1] = min(raw[-1], r)
            v[-1] = min(v[-1], lim)
            feed[-1] = min(feed[-1], p.feed)
            continue
        s.append(si)
        raw.append(r)
        v.append(max(lim, 0.0))
        feed.append(p.feed)
    w = [0.0] + [max(0.0, min(feed[i], prm.max_vel, max(raw[i - 1], raw[i]) if use_curvature
                             else feed[i])) for i in range(1, len(s))]
    if use_curvature:
        v[0] = v[-1] = 0.0
    return _Limits(s, v, w)


def _insert(lim: _Limits, x: float) -> int:
    """Index of a point at arc length x, inserted (with the limit of its interval) if needed."""
    for i, si in enumerate(lim.s):
        if abs(si - x) <= 1e-6:
            return i
        if si > x:
            lim.s.insert(i, x)
            lim.v.insert(i, lim.w[i])
            lim.w.insert(i, lim.w[i])
            return i
    return len(lim.s) - 1


def _slow_start_end(lim: _Limits, prm: PlannerParams) -> None:
    """p6/p7 (start) and p8/p9 (end) slow zones: speeds within them capped."""
    total = lim.s[-1]
    L0, L1 = prm.start_slow_len, prm.end_slow_len
    if total < L0 + L1:
        L0 = L1 = 0.5 * total
    if L0 >= 0.1:
        k = _insert(lim, L0)
        for i in range(k + 1):
            lim.v[i] = min(lim.v[i], prm.start_slow_vel)
            lim.w[i] = min(lim.w[i], prm.start_slow_vel)
    if L1 >= 0.1:
        k = _insert(lim, lim.s[-1] - L1)
        for i in range(k, len(lim.s)):
            lim.v[i] = min(lim.v[i], prm.end_slow_vel)
            if i > k:
                lim.w[i] = min(lim.w[i], prm.end_slow_vel)


def _dense_passes(lim: _Limits, A: float) -> list[float]:
    n = len(lim.s)
    vp = list(lim.v)
    for i in range(1, n):
        vp[i] = min(vp[i], lim.w[i])
        vp[i - 1] = min(vp[i - 1], lim.w[i])
    for i in range(n - 2, -1, -1):
        ds = lim.s[i + 1] - lim.s[i]
        vp[i] = min(vp[i], math.sqrt(vp[i + 1] ** 2 + 2.0 * A * ds))
    for i in range(1, n):
        ds = lim.s[i] - lim.s[i - 1]
        vp[i] = min(vp[i], math.sqrt(vp[i - 1] ** 2 + 2.0 * A * ds))
    return vp


def _valleys(vp: list[float]) -> list[int]:
    n = len(vp)
    out = [0]
    for i in range(1, n - 1):
        a, b, c = vp[i - 1], vp[i], vp[i + 1]
        if b <= a and b <= c and (b < a or b < c):
            out.append(i)
    if n > 1:
        out.append(n - 1)
    return out


def _boundary_speeds(lim: _Limits, vp: list[float], B: list[int], A: float, J: float) -> list[float]:
    vb = [vp[i] for i in B]
    m = 1.0 - _LEN_MARGIN
    for k in range(len(B) - 2, -1, -1):
        L = (lim.s[B[k + 1]] - lim.s[B[k]]) * m
        vb[k] = min(vb[k], reachable_speed(vb[k + 1], L, A, J, vb[k]))
    for k in range(1, len(B)):
        L = (lim.s[B[k]] - lim.s[B[k - 1]]) * m
        vb[k] = min(vb[k], reachable_speed(vb[k - 1], L, A, J, vb[k]))
    return vb


def _segment_vmax(lim: _Limits, vp: list[float], a: int, b: int, A: float) -> float:
    peak = 0.0
    for i in range(a + 1, b + 1):
        ds = lim.s[i] - lim.s[i - 1]
        top = math.sqrt(0.5 * (vp[i - 1] ** 2 + vp[i] ** 2) + A * ds)
        peak = max(peak, min(lim.w[i], top))
    return peak


def _worst_violation(blk: RuckigBlock, lim: _Limits, a: int, b: int, period_s: float) -> int | None:
    """Interior point of segment a..b where the trajectory exceeds a limit most (None if none)."""
    if b - a < 2:
        return None
    T = blk.duration
    n = max(64, int(T / min(period_s * 0.25, 2.5e-4)) + 2)
    tt = np.linspace(0.0, T, n)
    p, v, _, _ = blk._eval(tt)
    s0 = lim.s[a]
    ss = np.asarray(lim.s[a:b + 1]) - s0
    worst, wi = 0.0, None
    # point limits (velocity at each interior point, interpolated over position)
    vk = np.interp(ss[1:-1], p, v)
    caps = np.asarray(lim.v[a + 1:b])
    over = vk - caps * (1 + _REL_TOL) - _ABS_TOL
    if over.size and over.max() > worst:
        worst, wi = float(over.max()), a + 1 + int(over.argmax())
    # interval limits
    seg = np.clip(np.searchsorted(ss, p, side="right"), 1, b - a)
    wcap = np.asarray(lim.w[a + 1:b + 1])[seg - 1]
    over2 = v - wcap * (1 + _REL_TOL) - _ABS_TOL
    if over2.size and over2.max() > worst:
        k = int(over2.argmax())
        i = a + int(seg[k])                # interval (i-1, i); split at its slower interior end
        cand = [j for j in (i - 1, i) if a < j < b]
        if cand:
            wi = min(cand, key=lambda j: lim.v[j])
    return wi


# --------------------------------------------------------------------------- entry point
def plan_velocity(points: list[PathPoint], params: PlannerParams | None = None,
                  use_curvature: bool = True) -> Plan:
    """Plan a path (same call and result as the faithful ``plan_velocity``; see the module
    docstring).  With curvature limits (the pipeline's call) it starts and ends at rest.

    Modern planner, NOT validated on hardware; validate against the simulator and real traces
    before cutting."""
    prm = (params or PlannerParams()).clamped()
    plan = Plan(period_ms=prm.period_ms)
    if len(points) < 2:
        return plan
    A, J = prm.max_acc, prm.jerk()
    lim = _limits(points, prm, use_curvature)
    if len(lim.s) < 2 or lim.s[-1] <= 1e-9:
        return plan
    _slow_start_end(lim, prm)
    vp = _dense_passes(lim, A)
    B = _valleys(vp)
    period_s = prm.period_ms * 0.001
    for _ in range(2 * len(lim.s) + 64):
        vb = _boundary_speeds(lim, vp, B, A, J)
        blocks: list[Profile] = []
        split: list[int] = []
        failed = False
        for k in range(len(B) - 1):
            a, b = B[k], B[k + 1]
            L = lim.s[b] - lim.s[a]
            vmax = max(_segment_vmax(lim, vp, a, b, A), vb[k], vb[k + 1])
            blk = _ruckig_block(L, vb[k], vb[k + 1], vmax, A, J)
            if blk is None:                 # numerically too tight: lower the faster end a little
                i = a if vb[k] >= vb[k + 1] else b
                vp[i] = 0.999 * min(vb[k] if i == a else vb[k + 1], vp[i])
                failed = True
                break
            blk.a, blk.b = a, b
            bad = _worst_violation(blk, lim, a, b, period_s)
            if bad is not None:
                split.append(bad)
            blocks.append(blk)
        if failed:
            continue
        if not split:
            break
        B = sorted(set(B) | set(split))
    else:                                   # pragma: no cover - every point a boundary ends it
        raise RuntimeError("modern planner did not converge")
    for k, done in enumerate(blocks):
        plan.starts.append(lim.s[B[k]])
        plan.blocks.append(done)
    vel = list(vp)
    for k, i in enumerate(B):
        vel[i] = vb[k]
    plan.point_vel = vel
    return plan


#: the profile: the faithful one with ``motion.plan_velocity`` replaced (registered on import;
#: ``get_profile("modern-ruckig")`` imports this package on demand)
MODERN_RUCKIG: ParityProfile = register_profile(ORIGINAL.derive(
    NAME, DESCRIPTION, warning=WARNING,
    motion=ORIGINAL.motion.with_(plan_velocity=f"{__name__}:plan_velocity")))
