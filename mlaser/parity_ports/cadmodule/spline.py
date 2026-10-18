"""Exact spline glyphs in the cutting pipeline (parity JC-07); moved from ``mlaser.cam.geometry``.

:class:`SplineCurve` keeps a spline glyph's true curve next to the polyline its contour is
flattened to; :class:`CurveRef` is the curve range of one cut piece.  Contour fractions map to
curve parameters as FUN_10093390 does, points and tangents come from the openNURBS evaluation
(``curvetable``).
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Optional

from ...model import Point
from .._registry import port

__port__ = {
    "source": "CADModule",
    "functions": {"FUN_10093390": "SplineCurve.u_at", "FUN_10093b40": "SplineCurve.point (vt+0x40)",
                  "FUN_10093dc0": "SplineCurve.tangent (vt+0x44, analyser +0x3c)",
                  "FUN_100953d0": "SplineCurve sample table (geom.curves.flatten_bspline_params)",
                  "FUN_1006f2d0": "CurveRef.t0/t1 (primitive window)"},
    "parity": ["jobcad"],
}


@port("FUN_10093390", "FUN_10093b40", "FUN_10093dc0", source="CADModule", parity=("jobcad",))
class SplineCurve:
    """The exact curve of a spline glyph (CEditableSpline, glyph kind 7) next to the polyline the
    contour's segments flatten it to.

    The original keeps the spline through the whole cutting pipeline (parity JC-07): the glyph's
    length +0x48 is the length of the splineAnalyer polyline, whose samples (u, x, y, s) sit at
    +0x124 (FUN_100953d0, ported as ``geom.curves.flatten_bspline_params``); a contour fraction is
    turned into a curve parameter by FUN_10093390 (target = t * length; the first sample k >= 1
    with target <= s_k; u linear in s between samples k-1 and k) and the point / tangent are
    then evaluated on the TRUE curve (vtable +0x40 FUN_10093b40 -> analyser +0x28, +0x44
    FUN_10093dc0 -> analyser +0x3c).  Arc lengths ``s`` here are always in the glyph's own
    direction; :meth:`travel` converts a travel-order distance."""

    def __init__(self, ctrl, knots, gdir: int = 1):
        from ...geom.curves import flatten_bspline_params
        self.ctrl = [tuple(map(float, c[:2])) for c in ctrl]
        self.knots = [float(k) for k in knots]
        self.dir = -1 if gdir == -1 else 1
        tab = flatten_bspline_params(self.ctrl, self.knots)
        self.u = [u for u, _p in tab]
        self.pts = [p for _u, p in tab]
        self.s = [0.0]
        for a, b in zip(self.pts, self.pts[1:]):
            self.s.append(self.s[-1] + math.dist(a, b))
        self.length = self.s[-1]

    @classmethod
    def from_glyph(cls, g) -> Optional["SplineCurve"]:
        d = getattr(g, "data", None) or {}
        if "ctrl" not in d or "knots" not in d:
            return None
        return cls(d["ctrl"], d["knots"], getattr(g, "dir", 1))

    def u_at(self, s: float) -> float:
        """FUN_10093390: curve parameter at own arc length ``s`` of the sample polyline."""
        n = len(self.s)
        if n < 2:
            return self.u[0] if self.u else 0.0
        k = bisect.bisect_left(self.s, s, 1, n - 1) if s > self.s[1] else 1
        while k < n - 1 and not (s <= self.s[k]):
            k += 1
        ds = self.s[k] - self.s[k - 1]
        f = (s - self.s[k - 1]) / ds if ds > 0 else 0.0
        return self.u[k - 1] + (self.u[k] - self.u[k - 1]) * f

    def travel(self, s_travel: float) -> float:
        """Own arc length of a travel-order distance (the glyph may be run backwards)."""
        return s_travel if self.dir == 1 else self.length - s_travel

    def point(self, u: float) -> Point:
        from ...geom.curves import _bs_eval
        lo, hi = self.u[0], self.u[-1]
        if u <= lo + 1e-12:
            return self.pts[0]
        if u >= hi - 1e-12:
            return self.pts[-1]
        return _bs_eval(self.ctrl, self.knots, u)

    def derivative(self, u: float, order: int = 1) -> Point:
        from ...geom.curves import bspline_derivative
        return bspline_derivative(self.ctrl, self.knots, u, order)

    def tangent(self, u: float) -> Point:
        """Unit tangent in the glyph's own direction: the analyser's EvTangent (FUN_10093dc0 ->
        analyser +0x3c -> ON_Curve::EvTangent FUN_1004d1d0): first derivative of the openNURBS
        evaluation (a single-span glyph takes the Bezier path FUN_10057270) unitized with
        ON_3dVector::Unitize, as ``motion.curvetable`` ports them bit for bit (parity JC-07: the
        lead-in start then matches to the last bit)."""
        from .curvetable import _Nurbs, _ev_tangent
        if len(self.knots) == len(self.ctrl) + 4 and len(self.ctrl) >= 4:
            cur = getattr(self, "_on_curve", None)
            if cur is None:
                cur = self._on_curve = _Nurbs(self.ctrl, self.knots)
            ok, _p, t = _ev_tangent(cur, u)
            if ok:
                return t
        d = self.derivative(u)
        n = math.hypot(*d) or 1.0
        return (d[0] / n, d[1] / n)

    def curvature(self, u: float) -> float:
        """Signed curvature (1/mm, + = left turn in the glyph's own direction)."""
        d1, d2 = self.derivative(u), self.derivative(u, 2)
        n = math.hypot(*d1)
        return 0.0 if n < 1e-15 else (d1[0] * d2[1] - d1[1] * d2[0]) / n ** 3

    def point_at_travel(self, s_travel: float) -> Point:
        return self.point(self.u_at(self.travel(s_travel)))

    def tangent_at_travel(self, s_travel: float) -> Point:
        t = self.tangent(self.u_at(self.travel(s_travel)))
        return t if self.dir == 1 else (-t[0], -t[1])


@port("FUN_1006f2d0", source="CADModule", parity=("jobcad",))
@dataclass
class CurveRef:
    """The exact geometry of one cut piece that the contour flattens: the piece runs on
    ``curve`` from parameter ``u0`` to ``u1`` (in travel order; u decreases on a backwards
    glyph).  Consumers that understand curves (the planner's curvature, sample positions on
    the true curve) use it; everything else keeps using the piece's Line/Arc chord geometry."""
    curve: SplineCurve
    u0: float
    u1: float
    # the same range as travel distances on the glyph (0 .. curve.length): the original's
    # primitive window is t / glyph length (FUN_1006f2d0 +0xc0/+0xc8, used by motion.smooth)
    t0: Optional[float] = None
    t1: Optional[float] = None

    def sub(self, f0: float, f1: float) -> "CurveRef":
        """The part from fraction ``f0`` to ``f1`` of this piece."""
        t0 = t1 = None
        if self.t0 is not None and self.t1 is not None:
            t0 = self.t0 + (self.t1 - self.t0) * f0
            t1 = self.t0 + (self.t1 - self.t0) * f1
        return CurveRef(self.curve, self.u0 + (self.u1 - self.u0) * f0,
                        self.u0 + (self.u1 - self.u0) * f1, t0, t1)

    def point(self, f: float) -> Point:
        """Point at fraction ``f`` of the piece (parameter linear in arc length within the
        piece, as FUN_10093390 maps between samples)."""
        return self.curve.point(self.u0 + (self.u1 - self.u0) * f)
