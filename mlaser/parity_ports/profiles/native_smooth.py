"""CContoutSmooth's arithmetic on the C kernel (docs/dev/NATIVE_KERNEL.md, "Smoother").

The smoother of a default compile was about 70 % of the time (Review 2 P1-5): the openNURBS
sample tables of the blend and spline pieces (``CurveTable``), the musl ``acos`` / ``atan2`` /
``hypot`` they use (``crtmath``) and the per-sample arc length -> XY lookup.  This module puts
those three on ``mlaser._native._emitter`` (``_geom.c``), operation for operation:

* :func:`hypot`, :func:`atan`, :func:`atan2`, :func:`acos`: ``crtmath`` of two / one floats in
  C (any other argument type: the port);
* :class:`NativeCurveTable`: a :class:`~..cadmodule.curvetable.CurveTable` whose records are
  built and read in C (``ct_build``); a table the C code declines (an int control point, a
  division by zero the port would raise, ...) is the port's ``CurveTable``, and every lookup
  the C code declines runs the port's method on the same records;
* :class:`NativeContourSmoother` / :class:`NativeSmoothedPath`: the port's smoother with those
  tables, and ``points(ss)``, every sample of a chain in one call (``sample_path``) as a flat
  ``array('d')`` x0, y0, x1, y1, ... (the emitter kernel takes it as is);
* :func:`smooth_path_points`: the ``smoother`` slot of the default profile ``"original"``.  It
  runs the native classes when the default selection chose the kernel (``MLASER_NATIVE``, the
  self-check of :mod:`.native`, which also checks this module's pieces against the port), else
  the port's ``smooth_path_points``.

The Python port stays the reference: ``"original-python"`` and ``MLASER_NATIVE=0`` run it.
"""
from __future__ import annotations

from array import array
from typing import Any, Optional

from ... import _native as _PKG
from .. import crtmath as CRT
from ..cadmodule import curvetable as CT
from ..cadmodule import smooth as SM

_K = _PKG._emitter   # None when the extension is not built

__port__ = {
    "source": "CADModule",
    "functions": {"FUN_100f83c0": "NativeCurveTable (C)", "0x100f9fb0": "NativeSmoothedPath.points (C)",
                  "acos": "hypot / atan / atan2 / acos (msvcr100, C)"},
    "parity": ["jobcad"],
    "profile": "original (kernel selected)",
}

_F = float

#: C calls / hand-backs to the port, per piece (what tools/native_parity.py reports): curve
#: tables built in C / by the port, lookups of a C table that ran the port's method, chains
#: sampled in C / by the port (path points, plan samples)
STATS: dict[str, int] = {"table": 0, "table_port": 0, "lookup_port": 0, "path": 0, "path_port": 0,
                         "plan": 0, "plan_port": 0}


# ----------------------------------------------------------------------------- crtmath
def hypot(x: float, y: float) -> float:
    """:func:`~..crtmath.hypot` (C for two floats)."""
    if _K is not None and type(x) is _F and type(y) is _F:
        return _K.crt_hypot(x, y)
    return CRT.hypot(x, y)


def atan2(y: float, x: float) -> float:
    """:func:`~..crtmath.atan2` (C for two floats)."""
    if _K is not None and type(x) is _F and type(y) is _F:
        return _K.crt_atan2(y, x)
    return CRT.atan2(y, x)


def atan(x: float) -> float:
    """:func:`~..crtmath.atan` (C for a float)."""
    if _K is not None and type(x) is _F:
        return _K.crt_atan(x)
    return CRT.atan(x)


def acos(x: float) -> float:
    """:func:`~..crtmath.acos` (C for a float)."""
    if _K is not None and type(x) is _F:
        return _K.crt_acos(x)
    return CRT.acos(x)


# ----------------------------------------------------------------------------- CurveTable
def _rec(vals: tuple) -> Any:
    r = object.__new__(CT._Rec)
    (r.u, r.x, r.y, r.s, r.f, r.tx, r.ty, r.kx, r.ky, r.R, r.len, r.arc, r.cx, r.cy, r.rad,
     r.a0, r.a1) = vals
    return r


class NativeCurveTable(CT.CurveTable):
    """:class:`~..cadmodule.curvetable.CurveTable` with its record table in C.

    Constructed with the port's arguments; when the C build declines, the object made is the
    port's ``CurveTable`` (bit-identical by definition).  ``recs`` (the port's record objects)
    is materialised on first use only: by a lookup the C code declines, and by tests."""

    #: glyph_table's cache attribute on a spline curve (the port's is ``_smooth_table``)
    CACHE_ATTR = "_smooth_table_native"
    _t: Any

    def __new__(cls, ctrl, knots, u0: float = 0.0, u1: float = 1.0, ref: float = 0.0):
        self = object.__new__(cls)
        self.ctrl = [tuple(p) for p in ctrl]
        self.knots = list(knots)
        self.u0, self.u1 = float(u0), float(u1)
        self.ref = float(ref)
        if self.ref > 0.0:
            tol = 1.0 if 1.0 < self.ref else self.ref
            self.sample_tol = CT.SAMPLE_TOL if tol < CT.SAMPLE_TOL else tol
        else:
            self.sample_tol = CT.SAMPLE_TOL
        t = _K.ct_build(self.ctrl, self.knots, self.sample_tol) if _K is not None else None
        if t is None:
            STATS["table_port"] += 1
            return CT.CurveTable(ctrl, knots, u0, u1, ref)
        STATS["table"] += 1
        self._t = t
        self.total = t.total
        self._set_length()
        return self

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass                                  # built in __new__

    def __getattr__(self, name: str) -> Any:
        if name == "recs":
            recs = [_rec(v) for v in self._t.records()]
            self.__dict__["recs"] = recs
            return recs
        raise AttributeError(name)

    def windowed(self, u0: float, u1: float) -> "NativeCurveTable":
        c = object.__new__(type(self))
        c.__dict__.update(self.__dict__)
        c.u0, c.u1 = float(u0), float(u1)
        c._set_length()
        return c

    # -- lookups (C, else the port's method on the same records) ----------------------------
    def _search(self, target: float) -> int:
        return self._t.search(target)

    def radius_at(self, u: float) -> float:
        r = self._t.radius_at(u)
        if r is None:
            STATS["lookup_port"] += 1
            return super().radius_at(u)
        return r

    def nodes(self) -> list[list[float]]:
        if self.is_straight() or not self._t.n:
            L = self.length
            return [[CT.R_LINE, 0.0, 1.0], [CT.R_LINE, L * 0.5, 1.0], [CT.R_LINE, L, 1.0]]
        out = self._t.nodes(self.u0, self.u1)
        if out is None:
            STATS["lookup_port"] += 1
            return super().nodes()
        return out

    def point_at_fraction(self, t: float):
        p = self._t.point_at_fraction(t)
        if p is None:
            STATS["lookup_port"] += 1
            return super().point_at_fraction(t)
        return p

    def point(self, s: float):
        p = self._t.point(self.u0, self.u1, self.length, s)
        if p is None:
            STATS["lookup_port"] += 1
            return super().point(s)
        return p

    def point_f(self, f: float):
        p = self._t.point_f(self.u0, self.u1, f)
        if p is None:
            STATS["lookup_port"] += 1
            return super().point_f(f)
        return p

    def tangent_f(self, f: float):
        p = self._t.tangent_f(self.u0, self.u1, f)
        if p is None:
            STATS["lookup_port"] += 1
            return super().tangent_f(f)
        return p


# ----------------------------------------------------------------------------- smoother
class NativeContourSmoother(SM.ContourSmoother):
    """The port's ContourSmoother with C-backed blend / spline tables."""
    table_cls = NativeCurveTable


def _fl(v: Any) -> Any:
    """An int coordinate of a line / arc piece (a drawing made of int points) as a float.
    Exact for |v| < 2^52: the port's arithmetic on it (``p0 + (p1 - p0) * f``, ``c + r cos a``,
    ``s / length``) converts it to the same float or, for ``p1 - p0`` of two such ints, gets
    the same exact integer.  Anything else is passed on as is (the C code then declines)."""
    if type(v) is int and -(1 << 52) < v < (1 << 52):
        return float(v)
    return v


def _fl48(v: Any) -> Any:
    """An int value of a plan block (a profile built from int arguments) as a float: exact for
    |v| < 2^48, also the int sums of up to seven of them the sampler forms (_ends)."""
    if type(v) is int and -(1 << 48) < v < (1 << 48):
        return float(v)
    return v


def _piece_spec(pc: Any) -> tuple:
    if pc.sentinel:
        return (0,)
    g = pc.g
    t = type(g)
    if t is SM._LineG:
        return (1, _fl(pc.length), _fl(g.p0[0]), _fl(g.p0[1]), _fl(g.p1[0]), _fl(g.p1[1]))
    if t is SM._ArcG:
        return (2, _fl(pc.length), _fl(g.c[0]), _fl(g.c[1]), _fl(g.r), _fl(g.a0), _fl(g.sw))
    if t is NativeCurveTable:
        return (3, pc.length, g._t, g.u0, g.u1)
    return (4, pc.length, g.point)


class NativeSmoothedPath(SM.SmoothedPath):
    """:class:`~..cadmodule.smooth.SmoothedPath` plus :meth:`points`."""

    _spec: Optional[list] = None

    def points(self, ss) -> Any:
        """``[self.at(s)[0] for s in ss]`` in one C call, as a flat ``array('d')`` x0, y0, x1,
        y1, ...; the list of tuples when the C code declines (a piece with int coordinates,
        an exception the port would raise, ...)."""
        sm = self.sm
        if self._spec is None:
            sm._ensure()
            self._spec = [_piece_spec(pc) for pc in sm.pieces]
        b = _K.sample_path(self._spec, sm._cum, ss) if _K is not None else None
        if b is None:
            STATS["path_port"] += 1
            return [self.at(s)[0] for s in ss]
        STATS["path"] += 1
        out = array("d")
        out.frombytes(b)
        return out

    @staticmethod
    def sample_plan(plan: Any, period_ms: Optional[float] = None) -> Any:
        """``plan.sample(period_ms)`` (planner.Plan.sample / Profile.sample_from) in one C call,
        as an ``array('d')``; the list of ``plan.sample`` when the C code declines."""
        from ..cadmodule.planner import Plan
        from ..cadmodule.profile import Profile
        blocks = None
        # only the port's own Plan / Profile (another planner's blocks sample differently, e.g.
        # profiles/modern_motion.RuckigBlock)
        if type(plan) is Plan and all(type(b) is Profile for b in plan.blocks):
            try:
                blocks = [tuple(_fl48(v) for v in (b.L, b.vs, b.vmax, b.ve, b.A, b.J, *b.t, *b.s))
                          for b in plan.blocks]
            except (AttributeError, TypeError):
                blocks = None
        dt = (plan.period_ms if period_ms is None else period_ms) * 0.001
        r = _K.sample_plan(blocks, dt) if _K is not None and blocks is not None and type(dt) is _F else None
        if r is None:
            STATS["plan_port"] += 1
            return plan.sample(period_ms)
        STATS["plan"] += 1
        out = array("d")
        out.frombytes(r)
        return out


def native_smooth_path_points(moves, cfg=None, params: Optional[SM.SmoothParams] = None,
                              lead_index: Optional[int] = None):
    """The port's smooth_path_points on the native classes (always; no selection: raises
    :class:`.native.NativeKernelMissing` when the kernel is not built)."""
    from .native import require
    require()
    return SM.run_smoother(NativeContourSmoother, NativeSmoothedPath, moves, cfg, params, lead_index)


def smooth_path_points(moves, cfg=None, params: Optional[SM.SmoothParams] = None,
                       lead_index: Optional[int] = None):
    """The default profile's smoother: :func:`native_smooth_path_points` when the default
    selection chose the C kernel (:func:`.native.native_selected`), else the port's."""
    from .native import native_selected
    if native_selected():
        return native_smooth_path_points(moves, cfg, params, lead_index)
    return SM.smooth_path_points(moves, cfg, params, lead_index)


# the same original function as the port's (a plain attribute, not an @port registration)
smooth_path_points.__port__ = SM.smooth_path_points.__port__  # type: ignore[attr-defined]


# ----------------------------------------------------------------------------- self-check
def _lcg(seed: int):
    x = seed
    while True:
        x = (x * 0x5DEECE66D + 11) & ((1 << 48) - 1)
        yield x / float(1 << 48)


def _hx(v: Any) -> Any:
    if isinstance(v, float):
        return v.hex()
    if isinstance(v, (list, tuple)):
        return [_hx(x) for x in v]
    return v


#: control polygons of the self-check's curve tables: blends as _blend builds them (an exact
#: symmetric 90 deg corner, where FUN_100bf9c0's step count depends on the last bit of acos), a
#: shallow and a sharp corner, an arc-like spline, and a single-span (Bezier) DXF glyph
_KAT_TABLES = (
    ([(0.0, 0.0), (0.15, 0.0), (0.2, 0.0), (0.2, 0.05), (0.2, 0.2)], None),
    ([(0.0, 0.0), (1.2, 0.03), (2.0, 0.1), (2.6, 0.31), (3.1, 0.9), (3.3, 1.7)], None),
    ([(0.0, 0.0), (0.4, 0.0), (0.5, 0.0), (0.45, 0.1), (0.1, 0.12), (-0.2, 0.14)], None),
    ([(5.0, 0.0), (5.0, 2.7), (3.5, 4.6), (1.2, 5.0), (0.0, 5.0), (-1.3, 4.8), (-2.9, 4.1)], None),
    ([(0.0, 0.0), (6.0, 0.0), (12.0, 4.0), (12.0, 10.0)], [0.0, 0.0, 0.0, 0.0, 2.0, 2.0, 2.0, 2.0]),
)


def selfcheck() -> str:
    """Known-answer vector of this module's C pieces against the Python ports: crtmath on
    special and random arguments, curve tables (every record, the nodes, the lookups), the
    smoother of a short chain (planner points, plan samples and path points).  "" when they
    agree, else what differs."""
    if _K is None:
        return "kernel not importable"
    saved = dict(STATS)
    for k in STATS:
        STATS[k] = 0
    try:
        r = _lcg(0x736D6F6F7468)
        args = [0.0, -0.0, 1.0, -1.0, 0.5, -0.5, 1e-300, -1e-300, 5e-324, 1e300, 2.0 ** 512 * 3,
                2.0 ** -460, 0.9999999999999999, -0.9999999999999999, 1.0000000000000002,
                float("inf"), float("-inf"), float("nan"), 0.7853981633974483, 0.70710678118654757]
        args += [(next(r) - 0.5) * 10.0 ** int(next(r) * 12 - 6) for _ in range(120)]
        for x in args:
            if _hx(_K.crt_acos(x)) != _hx(CRT.acos(x)) or _hx(_K.crt_atan(x)) != _hx(CRT.atan(x)):
                return f"crtmath acos / atan differ from the Python port at {x!r}"
            for y in args[::7]:
                if (_hx(_K.crt_hypot(x, y)) != _hx(CRT.hypot(x, y))
                        or _hx(_K.crt_atan2(y, x)) != _hx(CRT.atan2(y, x))):
                    return f"crtmath hypot / atan2 differ from the Python port at {x!r}, {y!r}"
        fr = [0.0, 1.0, -0.1, 1.1, 0.5] + [next(r) for _ in range(12)]
        for ctrl, knots in _KAT_TABLES:
            kn = knots or SM._clamped_uniform(len(ctrl), 3)
            for ref in (0.0, 18.16):
                a = CT.CurveTable(ctrl, kn, 0.0, 1.0, ref)
                b = NativeCurveTable(ctrl, kn, 0.0, 1.0, ref)
                if type(b) is not NativeCurveTable:
                    return "a self-check curve table was not built in C"
                ra = [[getattr(x, f) for f in CT._Rec.__slots__] for x in a.recs]
                if _hx(ra) != _hx([list(v) for v in b._t.records()]) or not ra:
                    return f"curve table records differ from the Python port ({len(ctrl)} control points)"
                for u0, u1 in ((0.0, 1.0), (0.13, 0.91)):
                    wa, wb = a.windowed(u0, u1), b.windowed(u0, u1)
                    if _hx(wa.nodes()) != _hx(wb.nodes()):
                        return "curve table nodes differ from the Python port"
                    for f in fr:
                        if (_hx(wa.point_f(f)) != _hx(wb.point_f(f)) or _hx(wa.tangent_f(f)) != _hx(wb.tangent_f(f))
                                or _hx(wa.point(f * wa.length)) != _hx(wb.point(f * wb.length))
                                or _hx(wa.radius_at(f)) != _hx(wb.radius_at(f))):
                            return f"curve table lookups differ from the Python port at {f!r}"
        # a chain: lead-in line, square corners, a hole-like arc, a short-line run
        from ..cadmodule.planner import PlannerParams, plan_velocity

        class _Mv:
            def __init__(self, a: Any, b: Any, c: Any = None, ccw: bool = True) -> None:
                self.start, self.end, self.center, self.ccw = a, b, c, ccw
                self.feed, self.laser = 80.0, True

        mv = [_Mv((-2.0, 0.0), (0.0, 0.0)), _Mv((0.0, 0.0), (10.0, 0.0)), _Mv((10.0, 0.0), (10.0, 6.0)),
              _Mv((10.0, 6.0), (6.0, 10.0), (6.0, 6.0)), _Mv((6.0, 10.0), (0.5, 10.0)),
              _Mv((0.5, 10.0), (0.3, 9.6)), _Mv((0.3, 9.6), (0.2, 9.0)), _Mv((0.2, 9.0), (0.0, 0.0))]
        cfg = type("Cfg", (), {"corner_tol": 0.05, "arc2seg_k": (1.0, 0.9), "smooth": True,
                               "smooth_params": None})()
        pa, ga = SM.smooth_path_points(mv, cfg, lead_index=1)
        pb, gb = SM.run_smoother(NativeContourSmoother, NativeSmoothedPath, mv, cfg, None, 1)
        if _hx([(p.s, p.radius, p.feed, p.factor) for p in pa]) != _hx([(p.s, p.radius, p.feed, p.factor) for p in pb]):
            return "smoother planner points differ from the Python port"
        plan = plan_velocity(pa, PlannerParams())
        ss = plan.sample(1.0)
        sb = gb.sample_plan(plan, 1.0)
        if type(sb) is not array or _hx(list(sb)) != _hx(ss):
            return "plan samples differ from the Python port"
        pts = gb.points(sb)
        if type(pts) is not array or _hx(list(pts)) != _hx([c for s in ss for c in ga.at(s)[0]]):
            return "smoothed path points differ from the Python port"
        if STATS["table_port"] or STATS["lookup_port"] or STATS["path_port"] or STATS["plan_port"]:
            return f"the C code declined calls of the vector {dict(STATS)}"
    finally:
        STATS.clear()
        STATS.update(saved)
    return ""
