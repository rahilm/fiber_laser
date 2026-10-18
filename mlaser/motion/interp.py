"""Time-sampled interpolation (simulator / preview) and cut-time estimation.

Two layers:

1. Direct ports of MotionCtrl.dll's exported single-move interpolators
   (``seg_interp`` = segInterp 0x100101c0, ``seg_interp_time`` = segInterp_time
   0x10010260, ``arc_interp`` = arcInterp 0x10010320; CADModule carries copies of
   segInterp/segInterp_time at 0x101078a0/0x10106e70).  They return the normalised
   path parameter at every interpolation period, which is the DLL's output format
   (vector<double>, every value divided by the last sample, so it ends at 1.0;
   no extra end sample, docs/parity.md D-01).
2. ``simulate``/``estimate_time``: run the look-ahead planner over a MotionItem
   stream (mlaser.toolpath.items) and sample x/y/v every period.  Continuous runs of
   cut moves are planned as one path (the controller blends them); rapids are
   planned as separate stop-to-stop moves plus the JumpAddTime settle time.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field

from ..toolpath.items import (CutArc, CutLine, Delay, Pierce, Rapid, MOVE_TYPES)
from ..model import Arc
from ..parity_ports import active_profile, export_ports
from .planner import PathPoint, PlannerParams, Plan, R_STRAIGHT, plan_velocity
from .profile import make_profile, Profile


# ----------------------------------------------------------------------------
# 1. exported single-move interpolators: mlaser.parity_ports.cadmodule.interp (the parity fence);
#    seg_interp / seg_interp_time / arc_interp below dispatch through the active ParityProfile
# ----------------------------------------------------------------------------
export_ports(globals(), "mlaser.parity_ports.cadmodule.interp",
             ("seg_interp", "seg_interp_time", "arc_interp", "_validate_seg", "_ftol"),
             seg_interp=("motion", "seg_interp"), seg_interp_time=("motion", "seg_interp_time"),
             arc_interp=("motion", "arc_interp"))


# ----------------------------------------------------------------------------
# 2. MotionItem stream simulation
# ----------------------------------------------------------------------------
@dataclass
class MotionConfig:
    """Machine dynamics used by the simulator/time estimate.

    ``cut`` mirrors CVelocityPlanning's parameter block.  ``jump_add_time_ms``
    comes from JumpAddTime.txt [Jump] AddTime (default file value 200, code
    default 100; MainApp FUN_004657e0 adds AddTime*0.001 s to each jump).
    ``corner_tol`` is the deviation allowed when rounding a sharp corner
    (INFERRED: the original's CContoutSmooth turns corners into small curves
    whose radius feeds the curvature speed law).
    ``arc2seg_k`` = [Arc2SegVelK] K_X,K_Y * 0.01 (CADModule FUN_101041d0), applied
    as a factor on arc speed limits.

    CInterpMrg's process block +8..+0x48 (O): MainApp FUN_004572f0 builds it from these
    parameters and hands it to ICADModule slot 0x1fc (FUN_101020b0 copies the 0x40 bytes into
    both CInterpMrg objects); the CInterpMrg constructor 0x10102490 defaults apply only before
    that call.  The fields below use the ParaModule defaults of the named parameters:

    =====  ==============================  =====================================  =======  ====
    off    field                           parameter                              default  ctor
    =====  ==============================  =====================================  =======  ====
    +0x08  ``micro_link_redrill``          GP.IsDrillInMicoLink (pd595_1)         0        0
    +0x09  ``micro_link_decel``            GP.EnableMicroLinkDecc (pd594)         0        0
    +0x10  ``micro_link_vel``              GP.MicoLinkSlowDownVel (pd595)         10       10
    +0x18  ``small_circle_limit``          MP.EnableSmallCircleSpeedLimit (1560)  0        0
    +0x20  ``small_circle_ratio``          MP.SmallCircleSpeedLimitRatio (1561)   0.6      1.0
    +0x28  ``scan_circle_vel_ratio``       FCP.FlycutCircleVelRatio (pd290)       1.0      1.0
    +0x30  ``scan_circle_pwm_delay``       FCP.FlycutCirclePwmDelayTime (pd513)   0        0
    +0x38  ``scan_line_open_pwm``          FCP.FlycutLineOpenPwmForwardCycle (514) -10     0
    +0x40  ``scan_line_close_pwm``         FCP.FlycutLineColsePwmForwardCycle (515) -10    0
    =====  ==============================  =====================================  =======  ====

    Applied here: the small-circle limit (CInterpMrg::Interp 0x10105260 after vt[5]: when +0x18
    is set and the contour has fewer than 4 primitives, the last an arc with r <= 6 and sweep
    > 3 pi/2, every planner point with |R - r| <= min(0.05 r, 0.5) gets R * ratio).  The
    micro-joint deceleration (FUN_10103780, +8/+9/+0x10) and the scan-path entries (Interp
    modes 2/3) are carried but not applied (U: the job loop that uses them is licence-gated).
    """
    cut: PlannerParams = field(default_factory=PlannerParams)
    rapid_vel: float = 500.0
    rapid_acc: float = 5000.0
    rapid_acc_time: float = 0.1
    jump_add_time_ms: float = 200.0
    corner_tol: float = 0.05
    small_circle_limit: bool = False           # CInterpMrg+0x18, MP.EnableSmallCircleSpeedLimit
    small_circle_ratio: float = 0.6            # CInterpMrg+0x20, MP.SmallCircleSpeedLimitRatio
    micro_link_redrill: bool = False           # CInterpMrg+0x08, GP.IsDrillInMicoLink
    micro_link_decel: bool = False             # CInterpMrg+0x09, GP.EnableMicroLinkDecc
    micro_link_vel: float = 10.0               # CInterpMrg+0x10, GP.MicoLinkSlowDownVel (mm/s)
    scan_circle_vel_ratio: float = 1.0         # CInterpMrg+0x28, FCP.FlycutCircleVelRatio
    scan_circle_pwm_delay: float = 0.0         # CInterpMrg+0x30, FCP.FlycutCirclePwmDelayTime
    scan_line_open_pwm: float = -10.0          # CInterpMrg+0x38, FCP.FlycutLineOpenPwmForwardCycle
    scan_line_close_pwm: float = -10.0         # CInterpMrg+0x40, FCP.FlycutLineColsePwmForwardCycle
    arc2seg_k: tuple[float, float] = (1.0, 1.0)
    period_ms: float = 1.0
    smooth: bool = True                # run cut chains through CContoutSmooth (smooth.py, motion.md §9):
    #                                    always on in the original for contours (CADModule FUN_100c6fe0
    #                                    pushes smooth bit 1 for every CGlyContour / group member, 0 for
    #                                    CGlyScan paths; select 0x10102060 stores it at CInterpMrg+0x4c)
    smooth_params: object = None       # smooth.SmoothParams, None = defaults


@dataclass
class Sample:
    t: float
    x: float
    y: float
    v: float
    laser: bool


class _PathGeom:
    """Arc-length lookup over a chain of CutLine/CutArc moves."""

    def __init__(self, moves):
        self.moves = moves
        self.cum = [0.0]
        for m in moves:
            self.cum.append(self.cum[-1] + _move_len(m))

    def at(self, s: float):
        i = max(0, min(bisect.bisect_right(self.cum, s) - 1, len(self.moves) - 1))
        m = self.moves[i]
        L = self.cum[i + 1] - self.cum[i]
        u = 0.0 if L <= 0 else min(max((s - self.cum[i]) / L, 0.0), 1.0)
        return _move_point(m, u), getattr(m, "laser", False)


def _move_len(m) -> float:
    if isinstance(m, CutArc):
        return Arc(m.start, m.end, m.center, m.ccw).length()
    return math.dist(m.start, m.end)


def _move_point(m, u):
    if isinstance(m, CutArc):
        return Arc(m.start, m.end, m.center, m.ccw).point_at(u)
    return (m.start[0] + (m.end[0] - m.start[0]) * u, m.start[1] + (m.end[1] - m.start[1]) * u)


def _tangent(m, at_end: bool):
    if isinstance(m, CutArc):
        p = m.end if at_end else m.start
        rx, ry = p[0] - m.center[0], p[1] - m.center[1]
        t = (-ry, rx) if m.ccw else (ry, -rx)
    else:
        t = (m.end[0] - m.start[0], m.end[1] - m.start[1])
    n = math.hypot(*t) or 1.0
    return (t[0] / n, t[1] / n)


def corner_radius(deflection: float, tol: float) -> float:
    """Equivalent radius of a fillet whose midpoint deviates ``tol`` from the corner.

    deflection = change of direction (rad, 0 = straight).  R = tol*c/(1-c), c=cos(d/2).
    """
    if deflection < 1e-6:
        return R_STRAIGHT
    c = math.cos(min(deflection, math.pi - 1e-6) / 2)
    return max(tol * c / (1 - c), 1e-4)


def path_points(moves, cfg: MotionConfig) -> list[PathPoint]:
    """Build planner input from a chain of cut moves.

    Each move contributes its end point plus interior points (so that long
    straight segments are not limited by the neighbouring corner speeds, like
    the densely sampled output of the original smoother).
    """
    pts = [PathPoint(0.0, R_STRAIGHT, moves[0].feed)]
    s = 0.0
    for i, m in enumerate(moves):
        L = _move_len(m)
        feed = m.feed
        if isinstance(m, CutArc):
            R = Arc(m.start, m.end, m.center, m.ccw).radius
            kx = min(cfg.arc2seg_k)
            feed = feed * kx
        else:
            R = R_STRAIGHT
        nint = 2 if L > 1e-6 else 0
        for k in range(1, nint + 1):
            pts.append(PathPoint(s + L * k / (nint + 1), R, feed))
        s += L
        if i + 1 < len(moves):
            t0, t1 = _tangent(m, True), _tangent(moves[i + 1], False)
            dot = max(-1.0, min(1.0, t0[0] * t1[0] + t0[1] * t1[1]))
            Rc = corner_radius(math.acos(dot), cfg.corner_tol)
            nxt = moves[i + 1]
            if isinstance(nxt, CutArc):
                Rc = min(Rc, Arc(nxt.start, nxt.end, nxt.center, nxt.ccw).radius)
            if isinstance(m, CutArc):
                Rc = min(Rc, R)
            pts.append(PathPoint(s, Rc, min(feed, nxt.feed)))
        else:
            pts.append(PathPoint(s, R, feed))
    # merge duplicate s (zero-length moves)
    out = [pts[0]]
    for p in pts[1:]:
        if p.s - out[-1].s > 1e-9:
            out.append(p)
        else:
            out[-1].radius = min(out[-1].radius, p.radius)
            out[-1].feed = min(out[-1].feed, p.feed)
    return out


def _chains(items):
    """Split the item stream into ('path', [moves]) / ('rapid', Rapid) / ('dwell', s)."""
    cur: list = []
    for it in items:
        if isinstance(it, (CutLine, CutArc)):
            if cur and math.dist(cur[-1].end, it.start) > 1e-3:
                yield ("path", cur)
                cur = []
            cur.append(it)
            continue
        if cur and isinstance(it, (Rapid, Delay, Pierce)):
            yield ("path", cur)
            cur = []
        if isinstance(it, Rapid):
            yield ("rapid", it)
        elif isinstance(it, Delay):
            yield ("dwell", it.seconds)
        elif isinstance(it, Pierce):
            yield ("dwell", it.time_s)
    if cur:
        yield ("path", cur)


def _rapid_profile(r: Rapid, cfg: MotionConfig) -> Profile | None:
    L = math.dist(r.start, r.end)
    if L <= 1e-9:
        return None
    rp = PlannerParams(max_acc=cfg.rapid_acc, acc_time=cfg.rapid_acc_time, max_vel=cfg.rapid_vel)
    return make_profile(L, 0.0, cfg.rapid_vel, 0.0, cfg.rapid_acc, rp.clamped().jerk(), 2)


def lead_in_index(moves, lead_kind: int | None = None, lead_len: float | None = None,
                  tol: float = 1e-6) -> int | None:
    """Index of the first contour move after a **line** lead-in, or None.

    This is where the original's primitive list (CADModule FUN_1006f2d0) carries a type-4
    marker: it is appended after the lead-in primitives only when the last of them is a line
    (lead type +0x1b8 = 1, line; type 3 line+arc ends in an arc and gets none).  CContoutSmooth
    blends the joint before the marker with ``SmoothParams.lead_tol`` (0.001) instead of the
    corner tolerance (motion.md §9.3 step 2).

    Two sources, in this order:

    * item metadata: moves whose attribute ``lead`` is ``"in"`` (the leading run of them is
      the lead-in);
    * the craft: ``lead_kind`` (LeadParams.kind) and ``lead_len`` (LeadParams.length).  A
      kind-1 lead-in is one line of the lead length (cam/leads.py); the moves covering the
      first ``lead_len`` mm (several when the run was split, e.g. by the slow start) must all
      be lines and end within ``tol`` of ``lead_len``.
    """
    if not moves:
        return None
    n = 0
    while n < len(moves) and getattr(moves[n], "lead", None) == "in":
        n += 1
    if n:
        return n if n < len(moves) and isinstance(moves[n - 1], CutLine) else None
    if lead_kind != 1 or not lead_len or lead_len <= 0:
        return None
    acc = 0.0
    for i, m in enumerate(moves):
        if not isinstance(m, CutLine):
            return None
        acc += _move_len(m)
        if abs(acc - lead_len) <= tol:
            return i + 1 if i + 1 < len(moves) else None
        if acc > lead_len + tol:
            return None
    return None


def _limit_small_circle(pts, moves, lead_index, ratio: float) -> bool:
    """CInterpMrg::Interp 0x10105260, small-circle branch (O): the primitive list (lead-in,
    type-4 marker after a line lead-in, glyphs, lead-out; micro-joint splits are not primitives)
    has fewer than 4 entries and ends in an arc with r <= 6 (0x1011fc00) and sweep > 3 pi/2
    (0x1011fb18): every point with |R - r| <= min(0.05 r, 0.5) gets R * ratio (+0x20)."""
    prims = []
    for m in moves:
        if prims and isinstance(m, CutArc) and isinstance(prims[-1], CutArc) and \
                math.dist(prims[-1].center, m.center) <= 1e-6 and prims[-1].ccw == m.ccw and \
                math.dist(prims[-1].end, m.start) <= 1e-6:
            prims[-1] = CutArc(prims[-1].start, m.end, m.center, m.ccw, m.feed)
            continue
        prims.append(m)
    n = len(prims) + (1 if lead_index else 0)
    last = moves[-1]
    if n >= 4 or not isinstance(last, CutArc):
        return False
    r = math.dist(last.start, last.center)
    sweep = 0.0
    for m in reversed(moves):        # the last glyph: the trailing run of moves on its circle
        if not (isinstance(m, CutArc) and math.dist(m.center, last.center) <= 1e-6):
            break
        sweep += Arc(m.start, m.end, m.center, m.ccw).length() / r if r > 0 else 0.0
    if not (r <= 6.0 and sweep > 4.71238898038469):
        return False
    lim = min(r * 0.05, 0.5)
    for p in pts:
        if abs(p.radius - r) <= lim:
            p.radius = ratio * p.radius
    return True


def plan_path(moves, cfg: MotionConfig, return_geom: bool = False, lead_index: int | None = None):
    """Velocity plan for one cut chain; with ``return_geom`` also the geometry (``.at(s)``,
    ``.cum``) the plan's arc length refers to: the smoothed path when ``cfg.smooth`` is set.

    ``lead_index``: index of the first contour move after a line lead-in (see
    :func:`lead_in_index`); the joint before it is blended with ``SmoothParams.lead_tol``
    as at the original's type-4 marker (CInterpMrg::Interp 0x10105260 -> CContoutSmooth load).
    Ignored without smoothing."""
    prm = PlannerParams(**cfg.cut.__dict__)
    prm.max_vel = max(prm.max_vel, max(m.feed for m in moves))
    if cfg.smooth:
        smooth_path_points = active_profile().smoother.smooth_path_points
        pts, geom = smooth_path_points(moves, cfg, lead_index=lead_index)
    else:
        pts, geom = path_points(moves, cfg), None
    if cfg.small_circle_limit:
        _limit_small_circle(pts, moves, lead_index, cfg.small_circle_ratio)
    plan = plan_velocity(pts, prm)
    if not return_geom:
        return plan
    return plan, (geom if geom is not None else _PathGeom(moves))


def estimate_time(items, cfg: MotionConfig | None = None) -> dict:
    """Estimate job time.  Returns {'total','cut','rapid','dwell','cut_length','rapid_length'} (s, mm)."""
    cfg = cfg or MotionConfig()
    res = dict(total=0.0, cut=0.0, rapid=0.0, dwell=0.0, cut_length=0.0, rapid_length=0.0)
    for kind, obj in _chains(items):
        if kind == "path":
            p = plan_path(obj, cfg)
            res["cut"] += p.duration
            res["cut_length"] += p.length
        elif kind == "rapid":
            pr = _rapid_profile(obj, cfg)
            if pr is not None:
                res["rapid"] += pr.duration + cfg.jump_add_time_ms * 0.001
                res["rapid_length"] += pr.L
        else:
            res["dwell"] += obj
    res["total"] = res["cut"] + res["rapid"] + res["dwell"]
    return res


def simulate(items, cfg: MotionConfig | None = None, period_ms: float | None = None) -> list[Sample]:
    """Sample (t, x, y, v, laser) every period over the whole job."""
    cfg = cfg or MotionConfig()
    dt = (period_ms or cfg.period_ms) * 0.001
    out: list[Sample] = []
    t0 = 0.0
    pos = None
    for kind, obj in _chains(items):
        if kind == "path":
            p, g = plan_path(obj, cfg, return_geom=True)
            T = p.duration
            k = 0
            while k * dt <= T:
                s, v = p.state_at_time(k * dt)
                (x, y), las = g.at(s)
                out.append(Sample(t0 + k * dt, x, y, v, las))
                k += 1
            (x, y), las = g.at(g.cum[-1])
            out.append(Sample(t0 + T, x, y, 0.0, las))
            pos = (x, y)
            t0 += T
        elif kind == "rapid":
            pr = _rapid_profile(obj, cfg)
            if pr is None:
                continue
            (sx, sy), (ex, ey) = obj.start, obj.end
            k = 0
            while k * dt <= pr.duration:
                s, v = pr.state(k * dt)
                u = s / pr.L
                out.append(Sample(t0 + k * dt, sx + (ex - sx) * u, sy + (ey - sy) * u, v, False))
                k += 1
            t0 += pr.duration + cfg.jump_add_time_ms * 0.001
            pos = obj.end
            out.append(Sample(t0, ex, ey, 0.0, False))
        else:
            if pos is not None and obj > 0:
                t0 += obj
                out.append(Sample(t0, pos[0], pos[1], 0.0, False))
    return out
