"""Lead-in / lead-out ("GuideCurve"), port of CADModule CGuideCurve.

Evidence (CADModule.dll): parameter block contour+0x1b8 (in: type, angle,
length, radius; out at +0x1d8), defaults type 0 / 90 deg / 2 mm / 1 mm
(ctor 0x10068890), setter 0x10064e10, geometry 0x100300c0, arc builder
0x10009b30, disabled when lead length or contour length < 0.01 (0x10064860).

Types (lang pd297-pd300): 0 none, 1 line, 2 arc, 3 line+arc.

Geometry, with a = angle, P = start point, d_in = tangent leaving P,
d_out = tangent arriving at the contour end, flag = side flag::

    v_in  = Rot(flag ? -a : +a) d_in
    v_out = Rot(flag ? +a : -a) d_out
    type 1 in : line  P - v_in*len -> P
    type 2 in : arc   S = P - (d_in + v_in)*len -> P, radius len/tan(a/2)
    type 3 in : t = R tan(a/2); (t < 0.1 or a < 5deg -> type 1; t > len-0.01 -> type 2)
                line S - v_in*(len-t) -> S, arc S -> P (radius R), S = P - (d_in+v_in)*t
    lead-out mirrors with v_out / d_out starting at P.

flag = 0 puts the lead on the RIGHT of the travel direction; arcs are CW
when flag == 0.  Side rule (0x10064e10)::

    flag = (depth is even) XOR (contour is CW) XOR invert

so outer CCW contours (depth 1) and CW holes (depth 2) both get their leads on
the scrap side.  ``invert`` = UI option "outermost layer is inside cut" (pd322).
"""
from __future__ import annotations

import math
from typing import Optional

from ..model import Arc, Contour, LeadParams, Line, Point, Segment
from ..geom.contour import rot, seg_tangent

MIN_LEN = 0.01


def side_flag(depth: int, ccw: bool, invert: bool = False) -> bool:
    return ((depth % 2) == 0) ^ (not ccw) ^ bool(invert)


def _arc_tangent_at_end(S: Point, P: Point, d: Point, R: float, cw: bool) -> Arc:
    """Arc of radius R ending at P with travel direction d at P."""
    n = (d[1], -d[0]) if cw else (-d[1], d[0])   # right normal for CW, left for CCW
    c = (P[0] + n[0] * R, P[1] + n[1] * R)
    return Arc(S, P, c, ccw=not cw)


def _arc_tangent_at_start(P: Point, E: Point, d: Point, R: float, cw: bool) -> Arc:
    """Arc of radius R starting at P with travel direction d at P."""
    n = (d[1], -d[0]) if cw else (-d[1], d[0])
    c = (P[0] + n[0] * R, P[1] + n[1] * R)
    return Arc(P, E, c, ccw=not cw)


def lead_in(P: Point, d_in: Point, lp: LeadParams, flag: bool) -> list[Segment]:
    """Lead-in segments ending at P (travel order)."""
    if lp.kind == 0 or lp.length < MIN_LEN:
        return []
    a = math.radians(lp.angle_deg)
    v = rot(d_in, -a if flag else a)
    kind = lp.kind
    L = lp.length
    t = 0.0
    if kind == 3:
        if math.degrees(a) >= 5.0:
            t = lp.radius * math.tan(a / 2)
        if math.degrees(a) < 5.0 or t < 0.1:
            kind = 1
        elif t > L - 0.01:
            kind = 2
    cw = not flag
    if kind == 1:
        return [Line((P[0] - v[0] * L, P[1] - v[1] * L), P)]
    if kind == 2:
        if math.degrees(a) < 1e-6:
            return [Line((P[0] - v[0] * L, P[1] - v[1] * L), P)]
        S = (P[0] - (d_in[0] + v[0]) * L, P[1] - (d_in[1] + v[1]) * L)
        return [_arc_tangent_at_end(S, P, d_in, L / math.tan(a / 2), cw)]
    S = (P[0] - (d_in[0] + v[0]) * t, P[1] - (d_in[1] + v[1]) * t)
    Q = (S[0] - v[0] * (L - t), S[1] - v[1] * (L - t))
    return [Line(Q, S), _arc_tangent_at_end(S, P, d_in, lp.radius, cw)]


def lead_out(P: Point, d_out: Point, lp: LeadParams, flag: bool) -> list[Segment]:
    """Lead-out segments starting at P."""
    if lp.kind == 0 or lp.length < MIN_LEN:
        return []
    a = math.radians(lp.angle_deg)
    v = rot(d_out, a if flag else -a)
    kind = lp.kind
    L = lp.length
    t = 0.0
    if kind == 3:
        if math.degrees(a) >= 5.0:
            t = lp.radius * math.tan(a / 2)
        if math.degrees(a) < 5.0 or t < 0.1:
            kind = 1
        elif t > L - 0.01:
            kind = 2
    cw = not flag
    if kind == 1:
        return [Line(P, (P[0] + v[0] * L, P[1] + v[1] * L))]
    if kind == 2:
        if math.degrees(a) < 1e-6:
            return [Line(P, (P[0] + v[0] * L, P[1] + v[1] * L))]
        E = (P[0] + (d_out[0] + v[0]) * L, P[1] + (d_out[1] + v[1]) * L)
        return [_arc_tangent_at_start(P, E, d_out, L / math.tan(a / 2), cw)]
    E = (P[0] + (d_out[0] + v[0]) * t, P[1] + (d_out[1] + v[1]) * t)
    return [_arc_tangent_at_start(P, E, d_out, lp.radius, cw),
            Line(E, (E[0] + v[0] * (L - t), E[1] + v[1] * (L - t)))]


# FUN_10063be0 constants (CADModule .rdata)
BOUNDARY_TOL = 0.05      # DAT_1011d6d0: a start within 0.05 mm of a glyph end is "at the vertex"
LWPOLY_PROBE = 0.05      # DAT_1011d6d0 * (+1 | DAT_1011d8c8 = -1): FUN_1008bb90 probe offset


def _glyph_tangent(segs, s: float, extrapolate: bool = True) -> Point:
    """Travel tangent of a glyph (its segments in travel order) at arc length ``s`` from the
    glyph's travel start; outside [0, length] the first / last piece is extended."""
    acc = 0.0
    for k, seg in enumerate(segs):
        L = seg.length()
        if s < acc + L or k == len(segs) - 1:
            u = (s - acc) / L if L > 0 else 0.0
            if not extrapolate:
                u = min(max(u, 0.0), 1.0)
            if isinstance(seg, Arc):
                # angle linear in the local parameter, also beyond the ends (FUN_1008bb90)
                a0 = math.atan2(seg.start[1] - seg.center[1], seg.start[0] - seg.center[0])
                a = a0 + seg.sweep() * u
                sg = 1.0 if seg.ccw else -1.0
                return (-math.sin(a) * sg, math.cos(a) * sg)
            return seg_tangent(seg, 0.0)
        acc += L
    return seg_tangent(segs[-1], 1.0)


def _lwpoly_tangent(segs, s: float, offset: float, gdir: int) -> Point:
    """FUN_1008bb90(lwpoly, t, offset): the exact tangent of the piece at s + offset (travel
    order).  The binary works in the lwpoly's own direction: target = length*t + offset*dir,
    + length when negative, past the end the last piece is extended; in travel order that is a
    wrap below 0 (dir 1) or above the length (dir -1) and an extension on the other side."""
    L = sum(seg.length() for seg in segs)
    q = s + offset
    if gdir == 1 and q < 0.0:
        q += L
    elif gdir == -1 and q > L:
        q -= L
    return _glyph_tangent(segs, q)


def start_tangents(c: Contour, frac: float = 0.0, length: Optional[float] = None) -> tuple[Point, Point]:
    """(d_in, d_out): the lead directions at the start fraction ``frac`` of the UNROTATED contour,
    port of FUN_10064860 -> FUN_10063be0(contour, t, flag) (O, CADModule.c).

    * target = length * t (``length`` = the stored contour length +0x48, see
      :func:`geometry.stored_length`); the glyphs are walked with their own lengths;
    * the start is "at a vertex" when it lies within 0.05 mm (DAT_1011d6d0) of the end of glyph
      i: flag 1 takes the travel start of glyph i+1 (past the last glyph: the end of the last
      glyph on a closed contour, the start of glyph 0 on an open one), flag 0 the travel start of
      glyph i itself;
    * otherwise the glyph containing the target: an lwpoly glyph (kind 6) asks FUN_1008bb90 for
      the tangent 0.05 mm after (flag 1) / before (flag 0) the point, any other glyph its exact
      tangent there.

    FUN_10030b50 gets the flag-1 tangent for the lead-IN and the flag-0 tangent for the
    lead-OUT, so at t = 0 (the usual stored start) both leads use the DEPARTING direction of
    glyph 0: on a rectangle the lead-out leaves along the first edge and ends where the lead-in
    started from the other side (parity JC-05); on bulge arcs the two leads are turned by
    +-0.05/r against each other (JC-06)."""
    from .ordering import _GLY_LWPOLY, _GLY_SPLINE, contour_glyph_list
    glys = [(k, segs, d, g) for k, segs, d, g in contour_glyph_list(c) if segs]
    if not glys:
        return (1.0, 0.0), (1.0, 0.0)
    lens = [sum(seg.length() for seg in segs) for _k, segs, _d, _g in glys]
    total = sum(lens)
    target = (total if length is None else length) * frac
    n = len(glys)
    cum = 0.0
    hit, idx = None, n - 1
    for i in range(n):
        cum += lens[i]
        if abs(cum - target) < BOUNDARY_TOL:
            hit = i
            break
        if target < cum:
            idx = i
            break

    def tangent(j, s_loc, extrapolate=True):
        kind, segs, _gdir, g = glys[j]
        if kind == _GLY_SPLINE:
            sc = _spline_curve(g)
            if sc is not None:     # FUN_10093dc0: the true curve's tangent at u(s)
                return sc.tangent_at_travel(min(max(s_loc, 0.0), sc.length))
        return _glyph_tangent(segs, s_loc, extrapolate)

    if hit is not None:
        j = hit + 1
        if j < n:
            d_in = tangent(j, 0.0, False)
        elif c.closed:
            d_in = tangent(n - 1, lens[n - 1], False)
        else:
            d_in = tangent(0, 0.0, False)
        return d_in, tangent(hit, 0.0, False)
    s_loc = target - (sum(lens[:idx]))
    kind, segs, gdir, _g = glys[idx]
    if kind == _GLY_LWPOLY:
        return (_lwpoly_tangent(segs, s_loc, +LWPOLY_PROBE, gdir),
                _lwpoly_tangent(segs, s_loc, -LWPOLY_PROBE, gdir))
    t = tangent(idx, s_loc)
    return t, t


def _spline_curve(g):
    """The :class:`geometry.SplineCurve` of a spline glyph (cached on the glyph object)."""
    if g is None:
        return None
    sc = getattr(g, "_mlaser_curve", None)
    if sc is None:
        from .geometry import SplineCurve
        sc = SplineCurve.from_glyph(g)
        try:
            object.__setattr__(g, "_mlaser_curve", sc)
        except Exception:      # noqa: BLE001 - slotted / frozen glyph: no cache
            pass
    return sc


def contour_leads(c: Contour, lin: LeadParams, lout: LeadParams, flag: bool,
                  frac: Optional[float] = None, length: Optional[float] = None, P: Optional[Point] = None):
    """(lead_in_segments, lead_out_segments).

    With ``frac`` the contour is the UNROTATED contour (or kerf child) and the leads are built at
    its start fraction with :func:`start_tangents`; ``P`` is the start point (default: the point
    at the fraction).  Without it the contour is taken as already rotated to its start
    (fraction 0).  Both leads hang off the start point: the original cuts the whole contour
    when a lead-out exists (FUN_1006f2d0 zeroes overcut and gap then, parity JC-02)."""
    if not c.segments or c.length() < MIN_LEN:
        return [], []
    f = 0.0 if frac is None else frac
    d_in, d_out = start_tangents(c, f, length)
    if P is None:
        if frac is None:
            P = c.segments[0].start
        else:
            from ..geom.contour import point_at
            P = point_at(c, f * (c.length() if length is None else length))
    Pend = P if c.closed else c.segments[-1].end
    if not c.closed:
        d_out = seg_tangent(c.segments[-1], 1.0)
    return lead_in(P, d_in, lin, flag), lead_out(Pend, d_out, lout, flag)
