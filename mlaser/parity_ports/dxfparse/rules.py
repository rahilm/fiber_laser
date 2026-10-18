"""DXF entities -> glyphs -> contours, as DxfParseDllvc100 v1.4.93 and CADModule's import build
them (moved from ``mlaser.io.dxf_import``, Wave 1; docs/cam.md §1, docs/parity.md §3).

The parser rules (INSERT levels, invisible entities, paper space, SPLINE weights, arc scaling),
the entity -> glyph handlers (FUN_10008f80 table), the chaining into contours (FUN_10061c90,
FUN_1006da30, FUN_10073e50), the graph bbox and reversal used by the import sort, and the
placement next to the drawing (FUN_100ecef0).  With these the .chf of every non-TEXT parity
sample is byte-identical to MainApp's import + Save As (docs/parity.md D-12).  TEXT outlines
(font engine) stay in :mod:`mlaser.io.dxf_import`.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Optional

from ezdxf.math import Vec3

from ...geom import curves as geom
from ...model import (GLYPH_ARC, GLYPH_CIRCLE, GLYPH_LINE, GLYPH_LWPOLY, GLYPH_POINT, GLYPH_SPLINE,
                      Arc, Contour, Document, Glyph, Line, Part, Segment)
from .._registry import port

__port__ = {
    "source": "DxfParse",
    "functions": {
        "newDxfFileParse 0x10004390": "collect", "FUN_1000a4b0": "collect (entity factory)",
        "FUN_10008f80": "entity_glyphs (CADModule handler table)", "FUN_10003100": "LINE",
        "FUN_100032e0": "ARC (_arc_glyph)", "FUN_100031f0": "CIRCLE", "FUN_100061f0": "LWPOLYLINE",
        "FUN_10008740": "SPLINE (_spline_glyphs)", "FUN_10003500 / FUN_10097600": "ELLIPSE (_ellipse_glyphs)",
        "FUN_100062f0": "POINT", "FUN_10008c60 / FUN_10008ac0": "INSERT scaling (_insert_levels, _xform)",
        "FUN_10008e60": "ellipse in INSERT (_expand_ellipses)", "FUN_100992d0": "QUAD_TO_CUBIC",
        "FUN_10092fb0": "_straight", "FUN_10061c90": "chain_glyphs", "FUN_1006da30": "_try_append",
        "FUN_10073e50": "_merge", "0x10073980": "reverse_chain_contour", "FUN_100ecef0": "place_document",
        "FUN_1000e5xx": "_acad_weights",
    },
    "parity": ["dxf", "import_bytes"],
}


CHAIN_TOL = 0.1          # CGlyContour closing / chaining tolerance of imported contours (.chf +0xd8 = 0.1)
PLACEMENT = (20.0, 0.0)  # FUN_100ecef0: new graphs go 20 mm (DAT_1011d848) right of the drawing, bbox
#                          bottom on its bottom; for an empty drawing the bbox lower-left lands at (20, 0)
TEXT_LAYER = 10          # FUN_10007bf0: text groups get layer 10 (vtable+0x48(10))
TEXT_LINE_SPACING = 1.2  # DAT_1011d8f0
IMPORT_SORT_MODE = 5     # IGP.AutoSortType pd372, MainApp default "5" (0090d06c): local optimal
ARC_MIN_SWEEP = math.radians(30)   # DAT_1011d808 (FUN_100032e0)
ARC_MIN_SAGITTA = 0.01             # DAT_1011d800
QUAD_TO_CUBIC = 1.882              # DAT_10120f48 (FUN_100992d0)
STRAIGHT_TOL = 1e-5                # DAT_1011d618 (FUN_10092fb0)
UNIFORM_TOL = 1e-3                 # DAT_100198a0 (DxfParse FUN_10008c60)
MIRROR_TOL = -1e-4                 # DAT_10019848

SKIP_TYPES = {"XLINE", "RAY", "HATCH", "DIMENSION", "LEADER", "MLEADER", "IMAGE", "WIPEOUT",
              "MLINE", "ATTRIB", "ATTDEF", "VIEWPORT", "TOLERANCE", "MESH", "3DSOLID", "BODY"}
FACE_TYPES = {"3DFACE", "SOLID", "TRACE"}


def _xy(v) -> tuple[float, float]:
    return (float(v[0]), float(v[1]))


def _norm_pi(a: float) -> float:
    """Angle into (-pi, pi] (FUN_10001470)."""
    a = math.fmod(a, 2 * math.pi)
    if a <= -math.pi:
        a += 2 * math.pi
    elif a > math.pi:
        a -= 2 * math.pi
    return a


def _arc_glyph(c, r: float, a0: float, a1: float) -> Optional[Glyph]:
    """FUN_100032e0: CCW arc a0 -> a1 (a1 > a0); tiny arcs become a line."""
    sw = a1 - a0
    if sw < 1e-8 or r <= 0:
        return None
    a0 = _norm_pi(a0)
    a1 = a0 + sw
    p0, p1 = geom._polar(c, r, a0), geom._polar(c, r, a1)
    if sw < ARC_MIN_SWEEP and r - r * math.cos(sw / 2) < ARC_MIN_SAGITTA:
        return Glyph(GLYPH_LINE, {"start": p0, "end": p1, "tabs": []})
    return Glyph(GLYPH_ARC, {"center": (float(c[0]), float(c[1])), "radius": float(r), "a0": a0, "a1": a1})


# --------------------------------------------------------------------------- INSERT levels
@dataclass
class _Level:
    """One INSERT transform as DxfParse applies it: scale, rotate, translate (+ insert OCS)."""
    lin: tuple            # 2x2 full linear part (a, b, c, d): x' = a x + b y
    off: tuple            # translation
    ang: tuple            # 2x2 linear part used for angles (rotation * sign(scale), OCS)
    rscale: Optional[float]   # radius factor, None = radius unchanged (non-uniform scale)

    def p(self, q):
        a, b, c, d = self.lin
        return (a * q[0] + b * q[1] + self.off[0], c * q[0] + d * q[1] + self.off[1])

    def v(self, q, m=None):
        a, b, c, d = m or self.lin
        return (a * q[0] + b * q[1], c * q[0] + d * q[1])

    @property
    def mirrored(self) -> bool:
        a, b, c, d = self.ang
        return a * d - b * c < 0


def _mul(m, n):
    a, b, c, d = m
    e, f, g, h = n
    return (a * e + b * g, a * f + b * h, c * e + d * g, c * f + d * h)


def _insert_levels(ins) -> list[_Level]:
    sx = float(ins.dxf.get("xscale", 1.0))
    sy = float(ins.dxf.get("yscale", 1.0))
    rot = math.radians(float(ins.dxf.get("rotation", 0.0)))
    cr, sr = math.cos(rot), math.sin(rot)
    R = (cr, -sr, sr, cr)
    ocs = ins.ocs()
    ex, ey = ocs.to_wcs(Vec3(1, 0, 0)), ocs.to_wcs(Vec3(0, 1, 0))
    O = (float(ex.x), float(ey.x), float(ex.y), float(ey.y))
    base = Vec3(ins.block().block.dxf.get("base_point", (0, 0, 0))) if ins.block() else Vec3()
    lin = _mul(O, _mul(R, (sx, 0.0, 0.0, sy)))
    ang = _mul(O, _mul(R, (-1.0 if sx < MIRROR_TOL else 1.0, 0.0, 0.0,
                           -1.0 if sy < MIRROR_TOL else 1.0)))
    rscale = abs(sx) if abs(abs(sx) - abs(sy)) < UNIFORM_TOL else None
    ip = ocs.to_wcs(Vec3(ins.dxf.insert))
    # MINSERT rows/columns (70/71/44/45) are ignored by DxfParse: only one copy (inserts2.dxf)
    ox = ip.x - (lin[0] * base.x + lin[1] * base.y)
    oy = ip.y - (lin[2] * base.x + lin[3] * base.y)
    return [_Level(lin, (float(ox), float(oy)), ang, rscale)]


_ELLIPSE_SRC = "ellipse-entity"     # internal: an ELLIPSE before its Bezier conversion


def _norm_t0(t0: float, t1: float) -> tuple[float, float]:
    """FUN_10008e60's normalisation: t0 into (-pi, pi], t1 moved by the same multiple of 2 pi."""
    while t0 > math.pi:
        t0 -= 2 * math.pi; t1 -= 2 * math.pi
    while t0 <= -math.pi:
        t0 += 2 * math.pi; t1 += 2 * math.pi
    return t0, t1


def _expand_ellipses(gl: list[Glyph]) -> list[Glyph]:
    out = []
    for g in gl:
        if g.kind == _ELLIPSE_SRC:
            d = g.data
            out += _ellipse_glyphs(d["center"], d["major"], d["ratio"], d["t0"], d["t1"])
        else:
            out.append(g)
    return out


def _xform(g: Glyph, lv: _Level) -> Glyph:
    """Apply one INSERT level to a glyph with DxfParse's per-entity rules."""
    d = copy.deepcopy(g.data)
    k = g.kind
    if k == _ELLIPSE_SRC:
        # FUN_10008e60: centre and major axis scaled per axis, then rotated / moved with the
        # INSERT; ratio kept; a mirror maps the parameters to [-t1, -t0] (t0 into (-pi, pi])
        d["center"] = lv.p(d["center"])
        d["major"] = lv.v(d["major"])
        if lv.mirrored:
            d["t0"], d["t1"] = _norm_t0(-d["t1"], -d["t0"])
        return Glyph(k, d, g.dir)
    if k == GLYPH_POINT:
        d["p"] = lv.p(d["p"])
    elif k == GLYPH_LINE:
        d["start"], d["end"] = lv.p(d["start"]), lv.p(d["end"])
    elif k in (GLYPH_ARC, GLYPH_CIRCLE):
        d["center"] = lv.p(d["center"])
        if lv.rscale is not None:
            d["radius"] = d["radius"] * lv.rscale
        if k == GLYPH_ARC:
            def ang(a):
                v = lv.v((math.cos(a), math.sin(a)), lv.ang)
                return math.atan2(v[1], v[0])
            sw = d["a1"] - d["a0"]
            a0 = ang(d["a1"]) if lv.mirrored else ang(d["a0"])
            d["a0"] = _norm_pi(a0)
            d["a1"] = d["a0"] + sw
    elif k == GLYPH_LWPOLY:
        neg = -1.0 if lv.mirrored else 1.0
        d["verts"] = [(lv.p(p), b * neg) for p, b in d["verts"]]
    elif k == GLYPH_SPLINE:
        d["ctrl"] = [lv.p(p) for p in d["ctrl"]]
    return Glyph(k, d, g.dir)


# --------------------------------------------------------------------------- raw SPLINE weights
def _acad_weights(path: str) -> dict:
    """{handle: weights} for SPLINEs, read the way DxfParse does: a 41 counts only when it directly
    follows the 10/20/30 of a control point; weights written before the control points (ezdxf's
    order) are dropped (D-08).  Missing weights are 1.0."""
    out: dict = {}
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if raw.startswith(b"AutoCAD Binary DXF"):
            return {}
        lines = raw.decode("latin-1").splitlines()
    except OSError:
        return {}
    tags = []
    for i in range(0, len(lines) - 1, 2):
        try:
            tags.append((int(lines[i].strip()), lines[i + 1].strip()))
        except ValueError:
            continue
    i = 0
    while i < len(tags):
        if tags[i] == (0, "SPLINE"):
            j = i + 1
            handle, w, seen_ctrl = None, [], 0
            while j < len(tags) and tags[j][0] != 0:
                code, val = tags[j]
                if code == 5 and handle is None:
                    handle = val.upper()
                elif code == 30:
                    seen_ctrl += 1
                    if j + 1 < len(tags) and tags[j + 1][0] == 41:
                        w.append(float(tags[j + 1][1]))
                        j += 1
                    else:
                        w.append(1.0)
                j += 1
            if handle is not None:
                out[handle] = w if any(abs(x - 1.0) > 1e-12 for x in w) else []
            i = j
            continue
        i += 1
    return out


# --------------------------------------------------------------------------- splines
def _straight(ctrl) -> bool:
    a, b = ctrl[0], ctrl[-1]
    L = math.dist(a, b)
    if L < 1e-12:
        return all(math.dist(a, q) < STRAIGHT_TOL for q in ctrl)
    return all(abs((b[0] - a[0]) * (a[1] - q[1]) - (a[0] - q[0]) * (b[1] - a[1])) / L < STRAIGHT_TOL
               for q in ctrl)


def _norm_knots(knots: list) -> list:
    """FUN_10095640: shift the knots to start at 0; a vector ending below 0.5 is scaled to end at 2."""
    k0 = knots[0]
    ks = [k - k0 for k in knots]
    last = ks[-1]
    if 1e-9 < last < 0.5:
        ks = [k * (2.0 / last) for k in ks]
    return ks


def _spline_piece(ctrl, knots, u1: int = 3) -> Glyph:
    if _straight(ctrl):
        return Glyph(GLYPH_LINE, {"start": tuple(ctrl[0]), "end": tuple(ctrl[-1]), "tabs": []})
    return Glyph(GLYPH_SPLINE, {"u0": 0, "u1": u1, "ctrl": [tuple(map(float, p)) for p in ctrl],
                                "knots": [float(k) for k in knots]})


def _spline_glyphs(e, weights_map: dict) -> list[Glyph]:
    """FUN_10008740 (SPLINE handler):

    * 2 control points -> line;
    * degree 2 with an odd number n of control points and n + 3 knots (FUN_100992d0): each
      quadratic Bezier (P0, P1, P2) becomes a cubic (P0, P0 + k (P1 - P0), P2 + k (P1 - P2), P2)
      with k = 1.882 w / (2 w + 1), w = weight of P1 (1 if the weights were dropped), one glyph
      per piece with knots 0,0,0,0,1,1,1,1.  (Exact degree elevation would be k = 2/3; the
      constant is the original's.)
    * otherwise the spline is kept as one glyph (FUN_10008680 copies it) with its knots normalised
      (FUN_10095640), also across a triple interior knot (observed, mix.dxf); weights of other
      rational splines are dropped (FUN_10004be0 copies knots and control points only).  Splines of degree other than 3 are
      converted to cubic Bezier pieces here (the original would hand them to splineAnalyer with
      regenerated uniform knots; not copied).
    """
    ctrl = [_xy(p) for p in e.control_points]
    knots = [float(k) for k in e.knots]
    deg = int(e.dxf.get("degree", 3))
    if not ctrl and len(e.fit_points) >= 2:
        try:
            bs = e.construction_tool()       # fit-point spline: interpolate (FUN_100951e0 / analyzer +8)
            ctrl = [_xy(p) for p in bs.control_points]
            knots = [float(k) for k in bs.knots()]
            deg = bs.degree
        except Exception:   # noqa: BLE001
            return []
    n = len(ctrl)
    if n < 2:
        return []
    if n == 2:
        return [Glyph(GLYPH_LINE, {"start": ctrl[0], "end": ctrl[1], "tabs": []})]
    w = weights_map.get(str(e.dxf.handle).upper()) if e.dxf.hasattr("handle") else None
    if w is None:
        w = [float(x) for x in e.weights]       # binary DXF / no raw tags: honour them
    if deg == 2 and (n - 3) % 2 == 0 and len(knots) == n + 3:
        out = []
        for i in range(0, n - 2, 2):
            wm = w[i + 1] if len(w) == n else 1.0
            k = wm * QUAD_TO_CUBIC / (wm + wm + 1.0)
            p0, p1, p2 = ctrl[i], ctrl[i + 1], ctrl[i + 2]
            q1 = (p0[0] + (p1[0] - p0[0]) * k, p0[1] + (p1[1] - p0[1]) * k)
            q2 = (p2[0] + (p1[0] - p2[0]) * k, p2[1] + (p1[1] - p2[1]) * k)
            # u1 (piece +0x50) is never set by FUN_100992d0: stack residue; in both samples
            # (spline_ellipse n = 3, mix n = 5) it equals the control-point count
            out.append(_spline_piece([p0, q1, q2, p2], [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0], u1=n))
        return out
    if deg != 3 or len(knots) != n + 4:
        try:
            from ezdxf.math import BSpline
            bs = BSpline(ctrl, order=deg + 1, knots=knots if len(knots) == n + deg + 1 else None)
            if deg <= 3:
                beziers = list(bs.bezier_decomposition())
            else:
                beziers = list(bs.cubic_bezier_approximation(segments=max(4, 4 * n)))
                beziers = [[_xy(p) for p in (b.control_points)] for b in beziers]  # type: ignore[attr-defined]
        except Exception:   # noqa: BLE001
            return []
        out = []
        for b in beziers:
            pts = [_xy(p) for p in b]
            if len(pts) == 2:
                pts = [pts[0], pts[0], pts[1], pts[1]]
            elif len(pts) == 3:
                p0, p1, p2 = pts
                pts = [p0, (p0[0] + 2 / 3 * (p1[0] - p0[0]), p0[1] + 2 / 3 * (p1[1] - p0[1])),
                       (p2[0] + 2 / 3 * (p1[0] - p2[0]), p2[1] + 2 / 3 * (p1[1] - p2[1])), p2]
            out.append(_spline_piece(pts, [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0]))
        return out
    return [_spline_piece(ctrl, _norm_knots(knots))]


def _ellipse_glyphs(center, major, ratio: float, t0: float, t1: float) -> list[Glyph]:
    """FUN_10003500 + FUN_10097600: the ellipse arc t0..t1 as cubic Bezier pieces split at
    multiples of pi/2 (the multiple is taken with truncation toward zero, so a negative t0 is cut at
    the next multiple towards 0), control points P + 4/3 tan(dt/4) P'(t); one spline glyph per piece
    (u1 = 4) with knots 0,0,0,0,K,K,K,K where K = 1/count, scaled to 2 when below 0.5
    (FUN_10095640)."""
    if t0 < -math.pi:
        t0 += 2 * math.pi; t1 += 2 * math.pi
    if t0 > math.pi:
        t0 -= 2 * math.pi; t1 -= 2 * math.pi
    if t1 < t0:
        t1 += 2 * math.pi
    a = (float(major[0]), float(major[1]))
    b = (-a[1] * ratio, a[0] * ratio)
    c = (float(center[0]), float(center[1]))
    P = lambda t: (c[0] + a[0] * math.cos(t) + b[0] * math.sin(t), c[1] + a[1] * math.cos(t) + b[1] * math.sin(t))  # noqa: E731
    D = lambda t: (-a[0] * math.sin(t) + b[0] * math.cos(t), -a[1] * math.sin(t) + b[1] * math.cos(t))  # noqa: E731
    q = math.pi / 2
    cuts = [t0]
    t = t0
    while t1 - t > 1e-8:
        nxt = int(t / q) * q + q            # FUN_1010e5d0 = _ftol: truncation toward zero
        if nxt - t < 0.01:
            nxt += q
        t = min(nxt, t1)
        cuts.append(t)
    count = len(cuts) - 1
    K = 1.0 / count if count else 1.0
    if K < 0.5:
        K = 2.0
    out = []
    for u, v in zip(cuts, cuts[1:]):
        k = 4.0 / 3.0 * math.tan((v - u) / 4)
        p0, p3, d0, d3 = P(u), P(v), D(u), D(v)
        ctrl = [p0, (p0[0] + k * d0[0], p0[1] + k * d0[1]), (p3[0] - k * d3[0], p3[1] - k * d3[1]), p3]
        out.append(Glyph(GLYPH_SPLINE, {"u0": 0, "u1": 4, "ctrl": ctrl, "knots": [0.0] * 4 + [K] * 4}))
    return out


# --------------------------------------------------------------------------- entities -> glyphs
def _ocs_lin(e):
    ocs = e.ocs()
    ex, ey = ocs.to_wcs(Vec3(1, 0, 0)), ocs.to_wcs(Vec3(0, 1, 0))
    return ocs, (float(ex.x), float(ey.x), float(ex.y), float(ey.y))


@port("FUN_10008f80", source="CADModule", parity=("dxf", "import_bytes"))
def _entity_glyphs(e, weights_map: dict) -> list[Glyph]:
    t = e.dxftype()
    if t == "LINE":
        a, b = _xy(e.dxf.start), _xy(e.dxf.end)
        return [Glyph(GLYPH_LINE, {"start": a, "end": b, "tabs": []})] if math.dist(a, b) > 1e-9 else []
    if t in ("ARC", "CIRCLE"):
        ocs, L = _ocs_lin(e)
        c = _xy(ocs.to_wcs(Vec3(e.dxf.center)))
        r = float(e.dxf.radius)
        if t == "CIRCLE":
            return [Glyph(GLYPH_CIRCLE, {"center": c, "radius": r})] if r > 0 else []
        a0, a1 = math.radians(e.dxf.start_angle), math.radians(e.dxf.end_angle)
        if a1 < a0:
            a1 += 2 * math.pi
        if L[0] * L[3] - L[1] * L[2] < 0:            # mirrored OCS: CCW arc a0..a1 -> pi-a1..pi-a0
            def ang(x):
                return math.atan2(L[2] * math.cos(x) + L[3] * math.sin(x), L[0] * math.cos(x) + L[1] * math.sin(x))
            a0, a1 = ang(a1), ang(a1) + (a1 - a0)
        g = _arc_glyph(c, r, a0, a1)
        return [g] if g else []
    if t in ("LWPOLYLINE", "POLYLINE"):
        if t == "POLYLINE" and (e.is_poly_face_mesh or e.is_polygon_mesh):
            return []
        if t == "LWPOLYLINE":
            ocs, L = _ocs_lin(e)
            elev = float(e.dxf.get("elevation", 0.0))
            vs = [(_xy(ocs.to_wcs(Vec3(x, y, elev))), float(b)) for x, y, b in e.get_points("xyb")]
            closed = bool(e.closed)
        else:
            two_d = e.is_2d_polyline
            ocs, L = _ocs_lin(e) if two_d else (None, (1.0, 0.0, 0.0, 1.0))
            vs = []
            for v in e.vertices:
                p = Vec3(v.dxf.location)
                vs.append((_xy(ocs.to_wcs(p) if ocs else p), float(v.dxf.get("bulge", 0.0)) if two_d else 0.0))
            closed = bool(e.is_closed)
        if L[0] * L[3] - L[1] * L[2] < 0:
            vs = [(p, -b) for p, b in vs]
        if len(vs) < 2:
            return []
        return [Glyph(GLYPH_LWPOLY, {"flag": 1 if closed else 0, "verts": vs})]
    if t in ("SPLINE", "HELIX"):
        try:
            return _spline_glyphs(e, weights_map)
        except Exception:   # noqa: BLE001
            return []
    if t == "ELLIPSE":
        ct = e.construction_tool()
        major = _xy(ct.major_axis)
        ratio = float(ct.ratio)
        t0, t1 = float(e.dxf.start_param), float(e.dxf.end_param)
        mn = _xy(ct.minor_axis)
        std = (-major[1] * ratio, major[0] * ratio)
        if mn[0] * std[0] + mn[1] * std[1] < 0:      # extrusion -Z: parameter runs the other way
            t0, t1 = -t1, -t0
        if abs((t1 - t0) % (2 * math.pi)) < 1e-9 and abs(t1 - t0) > 1e-9:
            t1 = t0 + 2 * math.pi
        # kept as the parser entity until the INSERT levels are applied (DxfParse transforms the
        # ELLIPSE itself, FUN_10008e60), then cut into Bezier glyphs (_expand_ellipses)
        return [Glyph(_ELLIPSE_SRC, {"center": _xy(ct.center), "major": major, "ratio": ratio,  # type: ignore[arg-type]
                                     "t0": t0, "t1": t1})]
    if t == "POINT":
        return [Glyph(GLYPH_POINT, {"p": _xy(e.dxf.location)})]
    return []


@dataclass
class _Item:
    glyphs: list
    key: tuple
    kind: str                     # dxftype
    text: Optional[dict] = None


@port("0x10004390", "FUN_1000a4b0", source="DxfParse", parity=("dxf", "import_bytes"))
def _collect(layout, weights_map, levels, out, stats, depth=0):
    for e in layout:
        t = e.dxftype()
        if e.dxf.get("invisible", 0):
            continue
        if t == "INSERT":
            if depth > 16:
                continue
            try:
                blk = e.block()
            except Exception:   # noqa: BLE001
                blk = None
            if blk is None:
                continue
            for lv in _insert_levels(e):
                _collect(blk, weights_map, [lv] + levels, out, stats, depth + 1)
            continue
        key = (e.dxf.get("layer", "0"), int(e.dxf.get("color", 256)))
        if t in SKIP_TYPES:
            continue
        if t in FACE_TYPES:
            stats["faces"] += 1
            continue
        if t == "REGION":
            stats["regions"] += 1
            continue
        if t in ("TEXT", "MTEXT"):
            txt = e.plain_text() if t == "MTEXT" else e.dxf.text
            ins = Vec3(e.dxf.insert)
            if t == "TEXT":
                ins = e.ocs().to_wcs(ins)
            h = float(e.dxf.get("char_height" if t == "MTEXT" else "height", 2.5) or 2.5)
            rot = float(e.dxf.get("rotation", 0.0))
            p = _xy(ins)
            ang = math.radians(rot)
            for lv in levels:
                p = lv.p(p)
                v = lv.v((math.cos(ang), math.sin(ang)))
                ang = math.atan2(v[1], v[0])
                h *= lv.rscale if lv.rscale is not None else 1.0
            out.append(_Item([], key, t, {"type": t, "text": txt, "insert": p, "height": h,
                                         "rotation": math.degrees(ang), "layer": key[0]}))
            continue
        try:
            gl = _entity_glyphs(e, weights_map)
        except Exception:   # noqa: BLE001
            continue
        for lv in levels:
            gl = [_xform(g, lv) for g in gl]
        gl = _expand_ellipses(gl)
        if gl:
            out.append(_Item(gl, key, t))


# --------------------------------------------------------------------------- chaining
def _near(p, q, tol) -> bool:
    return abs(p[0] - q[0]) < tol and abs(p[1] - q[1]) < tol


@dataclass
class _Chain:
    glyphs: list = field(default_factory=list)     # Glyph with dir set
    start: tuple = (0.0, 0.0)
    end: tuple = (0.0, 0.0)
    closed: bool = False
    key: tuple = ("0", 256)
    kind: str = ""

    def ends(self, g):
        s, e = geom.glyph_endpoints(g)
        return (s, e) if g.dir == 1 else (e, s)


def _is_closed_glyph(g: Glyph) -> bool:
    if g.kind == GLYPH_CIRCLE:
        return True
    if g.kind == GLYPH_LWPOLY and g.data.get("flag"):
        return True
    return False


def _try_append(ch: _Chain, g: Glyph, tol: float) -> bool:
    """FUN_1006da30: add glyph ``g`` to the open contour ``ch`` at its end (forward or reversed)
    or at its start; returns False when it does not connect."""
    if ch.closed:
        return False
    s, e = geom.glyph_endpoints(g)
    if not ch.glyphs:
        ch.glyphs = [Glyph(g.kind, g.data, 1)]
        ch.start, ch.end = s, e
    elif g.kind == GLYPH_POINT or ch.glyphs[0].kind == GLYPH_POINT or _is_closed_glyph(g):
        return False
    elif _near(s, ch.end, tol):
        ch.glyphs.append(Glyph(g.kind, g.data, 1)); ch.end = e
    elif _near(e, ch.end, tol):
        ch.glyphs.append(Glyph(g.kind, g.data, -1)); ch.end = s
    elif _near(s, ch.start, tol):
        ch.glyphs.insert(0, Glyph(g.kind, g.data, -1)); ch.start = e
    elif _near(e, ch.start, tol):
        ch.glyphs.insert(0, Glyph(g.kind, g.data, 1)); ch.start = s
    else:
        return False
    _update_closed(ch, tol)
    return True


def _update_closed(ch: _Chain, tol: float) -> None:
    if len(ch.glyphs) == 1:
        g = ch.glyphs[0]
        ch.closed = _is_closed_glyph(g) or (g.kind not in (GLYPH_POINT, GLYPH_LINE, GLYPH_ARC)
                                            and _near(ch.start, ch.end, tol))
    else:
        ch.closed = _near(ch.start, ch.end, tol)


def _merge(a: _Chain, b: _Chain, tol: float) -> bool:
    """FUN_10073e50: join open contour ``b`` onto open contour ``a``."""
    if a.closed or b.closed or not a.glyphs or not b.glyphs:
        return False
    if a.glyphs[0].kind == GLYPH_POINT or b.glyphs[0].kind == GLYPH_POINT:
        return False
    rev = [Glyph(g.kind, g.data, -g.dir) for g in reversed(b.glyphs)]
    if _near(a.end, b.start, tol):
        a.glyphs += b.glyphs; a.end = b.end
    elif _near(a.end, b.end, tol):
        a.glyphs += rev; a.end = b.start
    elif _near(a.start, b.end, tol):
        a.glyphs = b.glyphs + a.glyphs; a.start = b.start
    elif _near(a.start, b.start, tol):
        a.glyphs = rev + a.glyphs; a.start = b.end
    else:
        return False
    _update_closed(a, tol)
    return True


@port("FUN_10061c90", source="CADModule", parity=("dxf", "import_bytes"))
def chain_glyphs(items: list[_Item], tol: float = CHAIN_TOL) -> list[_Chain]:
    """FUN_10061c90 (sequential mode): every glyph is appended to the current contour when it
    connects, else it starts a new one; then, from the last contour backwards, an open contour
    is merged into the nearest earlier open contour it connects to."""
    chains: list[_Chain] = []
    for it in items:
        for g in it.glyphs:
            if not chains or chains[-1].key != it.key or not _try_append(chains[-1], g, tol):
                ch = _Chain(key=it.key, kind=it.kind)
                _try_append(ch, g, tol)
                _update_closed(ch, tol)
                chains.append(ch)
    k = len(chains) - 1
    while k > 0:
        b = chains[k]
        if not b.closed:
            for j in range(k - 1, -1, -1):
                if chains[j].key == b.key and _merge(chains[j], b, tol):
                    del chains[k]
                    break
        k -= 1
    return chains


def _snap(segs: list[Segment], closed: bool) -> list[Segment]:
    """Make consecutive segments share end points (gaps up to the chaining tolerance)."""
    out: list[Segment] = []
    for s in segs:
        if out and out[-1].end != s.start:
            p = out[-1].end
            s = Line(p, s.end) if isinstance(s, Line) else Arc(p, s.end, s.center, s.ccw)
        out.append(s)
    if closed and out and out[-1].end != out[0].start and len(out) > 1:
        s = out[-1]
        p = out[0].start
        out[-1] = Line(s.start, p) if isinstance(s, Line) else Arc(s.start, p, s.center, s.ccw)
    return out


@port("FUN_10061c90", source="CADModule", parity=("dxf", "import_bytes"))
def _contour(ch: _Chain, flatten_tol: float) -> Contour:
    segs: list[Segment] = []
    for g in ch.glyphs:
        segs += geom.glyph_segments(g, flatten_tol)
    if not (len(ch.glyphs) == 1 and ch.glyphs[0].kind == GLYPH_POINT):
        segs = [s for s in segs if s.length() > 1e-12 or isinstance(s, Arc)]
    c = Contour(_snap(segs, ch.closed), ch.closed, {"close_tol": CHAIN_TOL})
    c.set_source_glyphs(ch.glyphs)
    return c


def _glyph_bbox(g: Glyph):
    """Bbox of one glyph from its own data: arcs and circles from the stored centre and radius
    (centre +- r at the quadrant angles inside the sweep, plus the end points), so that equal
    extremes stay bit-equal (inserts.dxf: an arc top and a line end both at 22.165063509461095;
    the sorter's container test 0x1003c480 is strict).  Other glyphs: their segments."""
    d = g.data
    if g.kind in (GLYPH_ARC, GLYPH_CIRCLE):
        c, r = d["center"], d["radius"]
        a0, a1 = (d["a0"], d["a1"]) if g.kind == GLYPH_ARC else (0.0, 2 * math.pi)
        pts = [geom._polar(c, r, a0), geom._polar(c, r, a1)]
        k = math.ceil(a0 / (math.pi / 2) - 1e-12)
        while k * math.pi / 2 <= a1 + 1e-12:
            q = k % 4
            pts.append((c[0] + (r, 0.0, -r, 0.0)[q], c[1] + (0.0, r, 0.0, -r)[q]))
            k += 1
    else:
        segs = geom.glyph_segments(Glyph(g.kind, g.data, 1))
        lo, hi = geom.segments_bbox(segs)
        pts = [lo, hi]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _graph_bbox(c: Contour) -> tuple:
    src = c.source_glyphs()
    if not src:
        lo, hi = geom.segments_bbox(c.segments)
        return (lo[0], lo[1], hi[0], hi[1])
    bs = [_glyph_bbox(g) for g in src]
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def reverse_chain_contour(c: Contour) -> Contour:
    """CGlyContour vtable +0xb0 (0x10073980) on an imported open contour: the glyph list is
    reversed with every glyph's direction flipped, and so are the segments."""
    src = c.source_glyphs()
    out = Contour([s.reversed() for s in reversed(c.segments)], c.closed, dict(c.extra))
    out.extra.pop("glyphs", None)
    out.extra.pop("glyph_segments", None)
    if src is not None:
        out.set_source_glyphs([Glyph(g.kind, g.data, -g.dir) for g in reversed(src)])
    return out


def _part_bbox(p: Part):
    xs, ys = [], []
    for c in p.contours:
        lo, hi = geom.segments_bbox(c.segments)
        xs += [lo[0], hi[0]]; ys += [lo[1], hi[1]]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def place_document(doc: Document, corner: tuple = PLACEMENT) -> None:
    """Move every part so that the drawing's bounding box lower-left is at ``corner``
    (FUN_100ecef0 with an empty drawing)."""
    bbs = [b for b in (_part_bbox(p) for p in doc.parts) if b]
    if not bbs:
        return
    dx = corner[0] - min(b[0] for b in bbs)
    dy = corner[1] - min(b[1] for b in bbs)
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return
    for p in doc.parts:
        for c in p.contours:
            src = c.source_glyphs()
            c.segments = [_move(s, dx, dy) for s in c.segments]
            if src is not None:
                c.set_source_glyphs([geom.translate_glyph(g, dx, dy) for g in src])
    doc.extra["placement_offset"] = (dx, dy)
    doc.extra["points"] = [(x + dx, y + dy) for x, y in doc.extra.get("points", [])]


def _move(s: Segment, dx: float, dy: float) -> Segment:
    t = lambda p: (p[0] + dx, p[1] + dy)   # noqa: E731
    if isinstance(s, Line):
        return Line(t(s.start), t(s.end))
    return Arc(t(s.start), t(s.end), t(s.center), s.ccw)


# the profile's ``dxf`` slot names (mlaser.parity_ports.profile.DxfRules)
entity_glyphs = _entity_glyphs
collect = _collect
contour_of_chain = _contour
