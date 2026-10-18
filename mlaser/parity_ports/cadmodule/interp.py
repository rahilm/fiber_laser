"""MotionCtrl.dll's exported single-move interpolators, bit for bit (and the CADModule copies).

``seg_interp`` = segInterp 0x100101c0, ``seg_interp_time`` = segInterp_time 0x10010260,
``arc_interp`` = arcInterp 0x10010320; CADModule carries copies of segInterp/segInterp_time at
0x101078a0/0x10106e70.  They return the normalised path parameter at every interpolation period,
which is the DLL's output format (vector<double>, every value divided by the last sample, so it
ends at 1.0; no extra end sample, docs/parity.md D-01).  Moved from ``mlaser.motion.interp``.
"""
from __future__ import annotations

import math

from .._registry import port
from .profile import make_profile

__port__ = {
    "source": "MotionCtrl",
    "functions": {"0x100101c0": "seg_interp", "0x10010260": "seg_interp_time",
                  "0x10010320": "arc_interp", "FUN_10106d40": "_validate_seg",
                  "0x101078a0": "seg_interp (CADModule copy)",
                  "0x10106e70": "seg_interp_time (CADModule copy)"},
    "parity": ["motion", "cad_motion"],
}


# ----------------------------------------------------------------------------
# 1. exported single-move interpolators
# ----------------------------------------------------------------------------
def _validate_seg(L, vmax, vs, ve, acc) -> int:
    """Argument checks of CADModule FUN_10106d40 (MotionCtrl FUN_10010090 does the same checks
    but clips vs/ve after the "too short" test). 0 ok, 1 'too short', -2 invalid."""
    if L < 1e-4:
        return 1
    if not (0.001 <= vmax <= 10000.0):
        return -2
    vs, ve = min(vs, vmax), min(ve, vmax)
    if L < min(vs, ve) * 0.00025:
        return 1
    if vs < 0 or ve < 0 or L > 1e6 or acc < 5.0:
        return -2
    return 0


@port("0x100101c0", "0x101078a0", source="MotionCtrl", parity=("motion", "cad_motion"))
def seg_interp(L: float, vmax: float, vs: float, ve: float, acc: float, jerk: float,
               period_ms: float = 1.0, mode: int = 2) -> list[float]:
    """segInterp: S-curve (mode 2) or trapezoid move of length L sampled every period.

    vs/ve are clipped to vmax as in FUN_10106d40.  Returns [] when the DLL would return an
    error code.  Output: s/L at t = 0, T, ... <= duration, normalised by the last sample.
    """
    if _validate_seg(L, vmax, vs, ve, acc) != 0:
        return []
    vs, ve = min(vs, vmax), min(ve, vmax)
    return make_profile(L, vs, vmax, ve, acc, jerk, mode).sample(period_ms)


@port("0x10010260", "0x10106e70", source="MotionCtrl", parity=("motion", "cad_motion"))
def seg_interp_time(L, vmax, vs, ve, acc, jerk, mode: int = 2) -> float:
    """segInterp_time: duration in **milliseconds** (sum of the 7 phases * 1000)."""
    if _validate_seg(L, vmax, vs, ve, acc) != 0:
        return 0.0
    vs, ve = min(vs, vmax), min(ve, vmax)
    return make_profile(L, vs, vmax, ve, acc, jerk, mode).duration * 1000.0


def _ftol(x: float) -> int:
    """MSVC _ftol2: truncation toward zero."""
    if x != x or abs(x) > 2 ** 62:
        return -(2 ** 63)
    return int(x)


@port("0x10010320", source="MotionCtrl", parity=("motion",))
def arc_interp(a0: float, a1: float, radius: float, vmax: float, vs: float, ve: float,
               acc_x: float, acc_y: float, jerk_x: float, jerk_y: float,
               period_ms: float = 1.0, mode: int = 2) -> list[float]:
    """arcInterp (MotionCtrl 0x10010320): normalised parameter samples along an arc a0->a1 (rad).

    Checks: vmax, vs, ve, R >= 0, acc >= 5, jerk >= 2, period >= 0.01 ms, L/vmax <= 1e6
    (otherwise the output is empty).

    mode 2: one S-curve over L = |a1-a0| R with A = min(acc_x, acc_y), J = min(jerk_x, jerk_y)
    (vs/ve are not clipped here; the solver raises vmax to max(vs, ve)), sampled like segInterp.

    other modes (D-03), all in per-period units (v in mm/period, a in mm/period^2):
      N = trunc(L/vmax + 0.5) angle steps.  N < 5: constant acceleration with
      N2 = trunc(2L/(vs+ve) + 0.5) samples, s_i = (i^2 a/2 + vs i)/L, a = (ve-vs)/N2, and a last
      value 1.0.  Otherwise, at theta_k = a0 + k (a1-a0)/N (k = 0..N): limit
      v_k = min(vmax, sqrt(R min(acc_y/|sin|, acc_x/|cos|))) (an axis is ignored when its
      |sin|/|cos| < 0.001), v_0 <= vs, v_N <= ve.  A forward and a backward pass limit the
      change of each axis velocity (vx = v|sin|, vy = v|cos|) between neighbouring steps: when
      the axis that needs more time cannot make the change within the step's axis distance
      (|dcos| R or |dsin| R), v_k = sqrt(v_prev_axis^2 + 2 a_axis d_axis)/|axis factor|.
      Step times dt_k = 2 (L/N)/(v_(k-1)+v_k); samples at integer periods 0, 1, 2, ... < total
      with constant acceleration inside a step; finally every value is divided by the last.
    """
    if (vmax < 0 or vs < 0 or ve < 0 or acc_x < 5.0 or acc_y < 5.0 or jerk_x < 2.0
            or jerk_y < 2.0 or radius < 0 or period_ms < 0.01):
        return []
    d = a1 - a0
    L = abs(d) * radius
    if L <= 0 or vmax <= 0 or L / vmax > 1e6:
        return []
    if mode == 2:
        return make_profile(L, vs, vmax, ve, min(acc_x, acc_y), min(jerk_x, jerk_y), 2).sample(period_ms)
    T = period_ms
    axp = acc_x / 1e6 * T * T
    ayp = acc_y / 1e6 * T * T
    vmp = vmax / 1000.0 * T
    vsp = vs / 1000.0 * T
    vep = ve / 1000.0 * T
    if L / vmp > 1e6:
        return []
    n = _ftol(L / vmp + 0.5)
    if n < 5:
        sv = vsp + vep
        if sv <= 0:
            return []
        n2 = _ftol((L + L) / sv + 0.5)
        if n2 < 1:
            return []
        a = (vep - vsp) / n2
        h = a * 0.5
        return [(i * h * i + vsp * i) / L for i in range(n2)] + [1.0]
    dth = d / n
    R = radius
    sa, ca, c, s, lim = [], [], [], [], []
    for k in range(n + 1):
        th = k * dth + a0
        sn, cs = math.sin(th), math.cos(th)
        s.append(sn)
        c.append(cs)
        asn, acs = abs(sn), abs(cs)
        sa.append(asn)
        ca.append(acs)
        if 0.001 <= asn:
            if 0.001 <= acs:
                x = ayp / asn
                if axp / acs < x:
                    x = axp / acs
            else:
                x = ayp / asn
        else:
            x = axp / acs
        lim.append(x)
    v = []
    for k in range(n + 1):
        x = math.sqrt(lim[k] * R)
        v.append(x if x < vmp else vmp)
    if vsp < v[0]:
        v[0] = vsp
    if vep < v[n]:
        v[n] = vep
    dx = [0.0] + [abs((c[k] - c[k - 1]) * R) for k in range(1, n + 1)]
    dy = [0.0] + [abs((s[k] - s[k - 1]) * R) for k in range(1, n + 1)]

    def relax(k, j, dxk, dyk):
        vx0 = sa[j] * v[j]
        vy0 = ca[j] * v[j]
        tx = (sa[k] * v[k] - vx0) / axp
        ty = (ca[k] * v[k] - vy0) / ayp
        if not (tx <= 0.0 or tx <= ty or tx * vx0 + tx * axp * tx * 0.5 <= dxk):
            v[k] = math.sqrt(vx0 * vx0 + (axp + axp) * dxk) / sa[k]
        elif 0.0 < ty and tx < ty and dyk < ty * vy0 + ty * ayp * ty * 0.5:
            v[k] = math.sqrt(vy0 * vy0 + (ayp + ayp) * dyk) / ca[k]

    for k in range(1, n + 1):
        relax(k, k - 1, dx[k], dy[k])
    for k in range(n - 1, -1, -1):
        relax(k, k + 1, dx[k + 1], dy[k + 1])
    ds = L / n
    dt = [0.0] + [(ds + ds) / (v[k - 1] + v[k]) for k in range(1, n + 1)]
    out = [0.0]
    S = 0.0
    idx = 1
    tcur = 1.0
    tk = 0.0
    for k in range(1, n + 1):
        tprev = tk
        tk = tprev + dt[k]
        acc = (v[k] - v[k - 1]) / dt[k]
        if tcur < tk:
            while True:
                u = tcur - tprev
                out.append((u * v[k - 1] + S + acc * u * u * 0.5) / L)
                idx += 1
                tcur = float(idx)
                if not tcur < tk:
                    break
        S = (v[k - 1] + v[k]) * dt[k] * 0.5 + S
    r = 1.0 / out[-1]
    return [x * r for x in out]
