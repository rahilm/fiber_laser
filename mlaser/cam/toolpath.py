"""Document -> MotionItem stream (CAM pipeline).

Mirrors the original data flow (see docs/cam.md):

1. containment tree & depth (CContourTopTree), inner-before-outer order
   (ordering.py, CADModule 0x1003f010);
2. per contour: start point (0x10071f80), kerf child contour (0x100738b0),
   overcut/gap (0x10070eb0, 0x1006f2d0), lead-in/out on the kerf child
   (0x10064e10 / 0x100300c0), micro-joint laser-off spans (0x100673f0),
   cooling points (0x10067fa0, zero-speed stop + dwell);
3. MainApp's job loop (0x00459100) then emits, per contour: rapid jump ->
   pierce sequence (0x00464e10) -> start-cut sequence (gas, follow, laser on,
   delay; 0x00465460) -> cutting -> end-cut sequence (delay, laser off, gas
   off, lift / frog jump; 0x004657e0).

The original sends the controller one XY sample per servo period (after
CContoutSmooth + CVelocityPlanning); this rebuild keeps geometric items
(Rapid/CutLine/CutArc + process items) and leaves sampling to
``mlaser.motion.interp`` / the protocol layer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from ..model import Arc, Contour, Craft, Document, LayerParams, LeadParams, Line, Point, Segment
from ..geom.contour import is_ccw, point_at, reverse_contour, rotate_start, stored_length, sub_seg, tangent_at
from .items import (CutArc, CutLine, Delay, Follow, LaserOff, LaserOn, LayerChange, Marker,
                    MotionItem, Pierce, Rapid)
from .kerf import compensate
from .leads import contour_leads, lead_out, side_flag
from .microjoint import DEFAULT_JOINT_LEN, joint_intervals, split_by_joints
from .ordering import LOCAL_SEED, Node, build_tree, choose_start, order_nodes, toolpath_sort

__all__ = ["CamParams", "build_toolpath", "prepare_contour", "PreparedContour"] + [
    "Rapid", "CutLine", "CutArc", "LaserOn", "LaserOff", "Pierce", "Delay", "IO", "Follow",
    "LayerChange", "Marker", "MotionItem"]
from .items import IO  # noqa: E402  (re-export)


@dataclass
class CamParams:
    """Machine/CAM options for ``build_toolpath`` (names follow the lang keys)."""
    sort_mode: int = 5                 # 0 keep, 1-4 bands, 5 local, 6/7 ring, 20 shortest, 21 grid (ordering.py)
    small_first: bool = False          # pd466
    start_mode: int = 3                # 0 long side, 1 vertex, 2 percent, 3 keep (pd307-310). The original
                                       # cuts from the stored start (Crafts start fraction, default 0) and
                                       # only picks a long-side start when a lead is applied (docops.apply_lead;
                                       # parity D-19), so the toolpath keeps the contour start by default.
    start_percent: float = 0.0
    invert_lead_side: bool = False     # pd322 "outermost layer is inside cut"
    leads_on_open: bool = False        # lead command option "only closed contours" (default on)
    origin: Point = (0.0, 0.0)
    return_to_origin: bool = False
    pierce_time_s: Optional[float] = None   # default: layer.pierce.get("time_s", 0)
    start_delay_s: float = 0.0         # start-cut delay after laser on
    end_delay_s: float = 0.0           # end-cut delay before laser off
    cool_delay_s: float = 0.5          # "Cool Position Delay" (pd596)
    default_feed: float = 100.0
    avoid_cut: bool = False            # rebuild extension: route rapids around cut holes
    avoid_clearance: float = 2.0


@dataclass
class PreparedContour:
    """One contour ready to cut: lead-in + body + lead-out as (segment, laser) pieces."""
    pieces: list[tuple[Segment, bool]] = field(default_factory=list)
    cool_at: list[float] = field(default_factory=list)  # path distances (mm) where to stop
    # per piece: the exact curve the piece flattens (geometry.CurveRef, spline glyphs) or None
    curves: list = field(default_factory=list)
    lead_in_count: int = 0             # leading pieces that are the lead-in
    lead_in_line: bool = False         # lead-in of type 1 (FUN_1006f2d0 appends a type-4 marker)
    layer: int = 1
    part: int = -1
    index: int = -1

    @property
    def entry(self) -> Point:
        return self.pieces[0][0].start

    @property
    def exit(self) -> Point:
        return self.pieces[-1][0].end


def _nearest_dist(c: Contour, p: Point) -> float:
    """Arc length of the contour point nearest to p (polyline approximation)."""
    best, bd = 0.0, math.inf
    acc = 0.0
    for s in c.segments:
        L = s.length()
        n = 1 if isinstance(s, Line) else max(4, min(360, int(L / 0.05)))
        for k in range(n + 1):
            u = k / n
            q = s.point_at(u) if isinstance(s, Arc) else (s.start[0] + (s.end[0] - s.start[0]) * u,
                                                          s.start[1] + (s.end[1] - s.start[1]) * u)
            if isinstance(s, Line) and n == 1:
                # exact projection on the line
                dx, dy = s.end[0] - s.start[0], s.end[1] - s.start[1]
                if L > 0:
                    t = max(0.0, min(1.0, ((p[0] - s.start[0]) * dx + (p[1] - s.start[1]) * dy) / (L * L)))
                    q = (s.start[0] + dx * t, s.start[1] + dy * t)
                    d = math.dist(p, q)
                    if d < bd:
                        bd, best = d, acc + t * L
                break
            d = math.dist(p, q)
            if d < bd:
                bd, best = d, acc + u * L
        acc += L
    return best


def _overcut(craft: Craft, c: Contour, has_lead_out: bool) -> float:
    """Signed overcut (+) / gap (-) length after the 0x10070eb0 / 0x1006f2d0 rules: open contours
    keep only gaps, clamped to +-L/2, |v| <= 0.05 ignored, and FUN_1006f2d0 uses 0 for the value
    whenever the contour has a lead-out (+0x1d8 != 0, local_12d): an overcut AND a gap are
    dropped then, the whole contour is cut and the lead-out leaves from the start (parity JC-02)."""
    v = float(craft.extra.get("overcut", 0.0) or 0.0)
    L = c.length()
    if not c.closed and v > 0:
        v = 0.0
    v = max(-0.5 * L, min(0.5 * L, v))
    if abs(v) <= 0.05:
        v = 0.0
    if has_lead_out:
        v = 0.0
    return v


def _joint_list(craft: Craft) -> list[tuple[float, float]]:
    """(centre mm on the stored contour, width mm) per joint.  Widths: a (pos, len) entry's own,
    else the per-joint widths a .chf load carries (``Craft.extra['micro_joint_widths']``, the
    +0x128 list read by FUN_10072ad0, parity JC-10), else ``extra['micro_joint_len']`` (default
    0.5)."""
    jl = float(craft.extra.get("micro_joint_len", DEFAULT_JOINT_LEN))
    widths = list(craft.extra.get("micro_joint_widths", []) or [])
    out = []
    for i, j in enumerate(craft.micro_joints):
        if isinstance(j, (tuple, list)):
            out.append((float(j[0]), float(j[1])))
        elif i < len(widths) and widths[i] is not None:
            out.append((float(j), float(widths[i])))
        else:
            out.append((float(j), jl))
    return out


def start_fraction(c: Contour, craft: Craft, prm: CamParams) -> float:
    """The start as the original stores it (+0x180, fraction of the contour length): the craft's
    ``start_param`` (a .chf load, the start commands), else :func:`choose_start` with the CAM
    start mode (its arc length is converted with the computed length, the way docops stores it).
    Open contours start at 0 (FUN_10071f30)."""
    if not c.closed:
        return 0.0
    if "start_param" in craft.extra:
        return float(craft.extra["start_param"])
    L0 = c.length()
    d = choose_start(c, prm.start_mode, craft.lead_in.length, prm.start_percent)
    return (d % L0) / L0 if L0 > 0 else 0.0


def prepare_contour(c: Contour, craft: Craft, depth: int, prm: CamParams) -> Optional[PreparedContour]:
    """Start, kerf child, leads, overcut/gap, micro-joints and cooling points of one contour, in
    the order the original's load / craft setters apply them (O, CADModule):

    1. start fraction t (+0x180, :func:`start_fraction`);
    2. kerf child (0x10072540) built from the UNROTATED contour; FUN_100627e0 hands it the
       parent's fraction (FUN_10071f30(child, t)), so the compensated path starts at the child's
       own point t * child length, e.g. the miter corner (-0.2, -0.2) of a 60x40 rectangle grown
       by 0.2 (parity JC-03);
    3. the path starts at t * length (``geometry.stored_length``: a .chf contour keeps its stored
       +0x48) and both leads hang off that point with the tangents of FUN_10063be0
       (``leads.start_tangents``, JC-05/JC-06);
    4. overcut / gap (``_overcut``; nothing with a lead-out, JC-02);
    5. micro-joints with their stored widths (``_joint_list``, JC-10) and cooling points."""
    if not c.segments or c.length() < 1e-6:
        return None
    L0 = c.length()
    t = start_fraction(c, craft, prm)
    start = (t % 1.0) * L0 if c.closed else 0.0          # mm on the parent, for joints / cooling
    # --- kerf child (on the unrotated contour) --------------------------------
    base, base_len = c, stored_length(c)
    if craft.kerf_comp is not None and craft.kerf_comp >= 0 and craft.kerf_width:
        k = compensate(c, craft.kerf_comp, craft.kerf_width, depth)
        if k is not None:
            base, base_len = k, k.length()
    walk = t * base_len if base.closed else 0.0
    body = rotate_start(base, walk) if base.closed else base
    smap = _spline_map(base)
    P_true = _spline_start(smap, walk, base.length()) if base.closed else None
    dmap = None
    if P_true is not None:
        # the start lies inside a spline glyph: the original's start point (+0x188) is on the
        # TRUE curve (FUN_10093b40 at u(s)), not on the chord the polyline split gives
        dmap = _chord_dist_map(body.segments, P_true)
        segs = list(body.segments)
        segs[0] = _with_start(segs[0], P_true)
        segs[-1] = _with_end(segs[-1], P_true)
        body = Contour(segs, True)
    L = body.length()
    scale = L / L0 if L0 > 0 else 1.0
    # --- leads --------------------------------------------------------------
    lin, lout = [], []
    leads_on = body.closed or prm.leads_on_open
    if leads_on:
        flag = side_flag(depth, is_ccw(body) if body.closed else True, prm.invert_lead_side)
        lin, lout = contour_leads(base, craft.lead_in, craft.lead_out, flag,
                                  frac=(t if base.closed else 0.0), length=base_len,
                                  P=body.segments[0].start)
    # --- overcut / gap ------------------------------------------------------
    oc = _overcut(craft, body, leads_on and craft.lead_out.kind != 0)
    d1 = L + oc
    if oc != 0 and lout:        # only reachable without a lead-out type, kept for safety
        P = point_at(body, d1 % L if body.closed else d1)
        flag = side_flag(depth, is_ccw(body), prm.invert_lead_side)
        lout = lead_out(P, tangent_at(body, d1 % L), craft.lead_out, flag)
    # --- micro-joints -------------------------------------------------------
    joints = []
    for pos, ln in _joint_list(craft):
        pos = ((pos - start) % L0 if c.closed else pos) * scale
        joints.append((pos, ln))
    iv = joint_intervals(L, joints, DEFAULT_JOINT_LEN, body.closed)
    pieces = [(s, True) for s in lin]
    body_pieces = split_by_joints(body, iv, 0.0, d1)
    pieces += body_pieces
    pieces += [(s, True) for s in lout]
    curves = [None] * len(lin) + _curve_refs(smap, body_pieces, walk, base.length(), base.closed, dmap) \
        + [None] * len(lout)
    # --- cooling points ------------------------------------------------------
    cool = []
    lead_len = sum(s.length() for s in lin)
    pts = list(craft.extra.get("cool_points", []))
    if craft.cool_point is not None:
        pts.append(craft.cool_point)
    for p in pts:
        dist = _nearest_dist(c, p) if isinstance(p, (tuple, list)) else float(p)
        dist = ((dist - start) % L0 if c.closed else dist) * scale
        if 0.2 < dist < L - 0.2:   # points within 0.2 mm of either end are ignored (0x10104640)
            cool.append(lead_len + dist)
    return PreparedContour(pieces=pieces, cool_at=sorted(cool), layer=craft.layer, curves=curves,
                           lead_in_count=len(lin), lead_in_line=bool(lin) and craft.lead_in.kind == 1)


def _with_start(s: Segment, p: Point) -> Segment:
    return Line(p, s.end) if isinstance(s, Line) else Arc(p, s.end, s.center, s.ccw)


def _with_end(s: Segment, p: Point) -> Segment:
    return Line(s.start, p) if isinstance(s, Line) else Arc(s.start, p, s.center, s.ccw)


def _spline_map(c: Contour) -> list:
    """[(d0, d1, SplineCurve)]: the travel-distance ranges of the contour's spline glyphs."""
    from .leads import _spline_curve
    from .ordering import _GLY_SPLINE, contour_glyph_list
    out, acc = [], 0.0
    for kind, segs, _d, g in contour_glyph_list(c):
        L = sum(x.length() for x in segs)
        if kind == _GLY_SPLINE:
            sc = _spline_curve(g)
            if sc is not None and abs(sc.length - L) <= 1e-6 * max(1.0, L):
                out.append((acc, acc + L, sc))
        acc += L
    return out


def _spline_start(smap: list, walk: float, L: float) -> Optional[Point]:
    if not smap or L <= 0:
        return None
    x = walk % L
    for d0, d1, sc in smap:
        if d0 + 1e-9 < x < d1 - 1e-9:
            return sc.point_at_travel(x - d0)
    return None


def _chord_dist_map(segs, P: Point):
    """Distance map from the body whose first / last chord were moved onto the true-curve start
    point ``P`` back to the unmodified rotated polyline (the one the glyph lengths refer to):
    inside the two moved chords the distance is scaled, elsewhere shifted by the change of the
    first chord's length."""
    if not segs:
        return None
    s0, sN = segs[0], segs[-1]
    un0, unN = s0.length(), sN.length()
    mod0 = _with_start(s0, P).length()
    modN = _with_end(sN, P).length()
    Lmod = sum(x.length() for x in segs) - un0 - unN + mod0 + modN
    d0 = mod0 - un0
    Lun = Lmod - d0 - (modN - unN)

    def f(x: float) -> float:
        if x <= mod0:
            return x * (un0 / mod0) if mod0 > 0 else 0.0
        if x >= Lmod - modN:
            return Lun - (Lmod - x) * (unN / modN) if modN > 0 else Lun
        return x - d0
    return f


def _curve_refs(smap: list, body_pieces, walk: float, L: float, closed: bool, dmap=None) -> list:
    """CurveRef per body piece (None outside spline glyphs): the piece's distance range on the
    rotated path, shifted by the start walk, back to the glyph's own arc length and through
    FUN_10093390 to curve parameters.  ``dmap``: distance correction when the start chords were
    moved onto the true curve (:func:`_chord_dist_map`).

    The travel range (``CurveRef.t0``/``t1``) is what the original's primitive window is made of
    (FUN_1006f2d0: glyph fractions from FUN_10063b40): a range that reaches a glyph end is that
    end exactly (fraction 0 or 1), and a piece that ends where the closed path started ends at
    the start walk itself (FUN_1006f2d0 takes the end fraction from t + 1 - 1 = t)."""
    from .geometry import CurveRef
    out = []
    acc = 0.0
    Lb = sum(seg.length() for seg, _on in body_pieces)
    for seg, _on in body_pieces:
        n_mod = seg.length()
        ref = None
        if smap and L > 0:
            x0, x1 = acc, acc + n_mod
            if dmap is not None:
                x0, x1 = dmap(x0), dmap(x1)
            if closed:
                a = (walk + x0) % L
                b = walk % L if abs(acc + n_mod - Lb) <= 1e-9 * max(1.0, Lb) and abs(x1 - L) <= 1e-6 * max(1.0, L) \
                    else (walk + x1) % L
                if b < a - 1e-9:
                    if a > L - 1e-6 * max(1.0, L) and b < x1 - x0 + 1e-6:
                        a -= L          # the piece starts at the wrap
                    else:
                        b += L
            else:
                a, b = x0, x1
            m = 0.5 * (a + b)
            for d0, d1, sc in smap:
                if d0 - 1e-9 <= m <= d1 + 1e-9:
                    gl = d1 - d0
                    sa = min(max(a - d0, 0.0), gl)
                    sb = min(max(b - d0, 0.0), gl)
                    tol = 1e-9 * max(1.0, gl)
                    sa = 0.0 if sa <= tol else (gl if sa >= gl - tol else sa)
                    sb = 0.0 if sb <= tol else (gl if sb >= gl - tol else sb)
                    # glyph ends exactly: own length of the curve (the fraction becomes 0 / 1)
                    ta = sc.length if sa == gl else sa
                    tb = sc.length if sb == gl else sb
                    ref = CurveRef(sc, sc.u_at(sc.travel(ta)), sc.u_at(sc.travel(tb)), ta, tb)
                    break
        out.append(ref)
        acc += n_mod
    return out


def _sub_ref(ref, u0: float, u1: float):
    if ref is None:
        return None
    return ref.sub(u0, u1)


def _split_piece_at(pieces, dists, curves=None):
    """Split the piece list at path distances; returns list of piece-lists.  With ``curves``
    (one CurveRef or None per piece) the pieces come back as (segment, laser, curve)."""
    with_ref = curves is not None
    curves = curves if with_ref else [None] * len(pieces)
    out, cur, acc = [], [], 0.0
    di = 0
    for (s, on), ref in zip(pieces, curves):
        L = s.length()
        u0 = 0.0
        while di < len(dists) and dists[di] < acc + L - 1e-9:
            u = (dists[di] - acc) / L if L > 0 else 0.0
            if u > u0 + 1e-9:
                cur.append((sub_seg(s, u0, u), on, _sub_ref(ref, u0, u)))
                u0 = u
            out.append(cur)
            cur = []
            di += 1
        if u0 == 0.0:
            cur.append((s, on, ref))
        else:
            cur.append((sub_seg(s, u0, 1.0), on, _sub_ref(ref, u0, 1.0)))
        acc += L
    out.append(cur)
    out = [p for p in out if p]
    return out if with_ref else [[(s, on) for s, on, _r in p] for p in out]


def _move(seg: Segment, on: bool, feed: float, layer: int, curve=None) -> MotionItem:
    """CutLine / CutArc of a piece.  A piece that flattens a spline glyph keeps its chord as
    the line and carries the exact curve as ``move.curve`` (geometry.CurveRef): a consumer that
    understands it (curvature for the planner, sample positions on the true curve, parity
    JC-07) uses it, everything else sees an ordinary CutLine."""
    if isinstance(seg, Line):
        mv = CutLine(seg.start, seg.end, feed, layer, on)
        if curve is not None:
            mv.curve = curve
        return mv
    return CutArc(seg.start, seg.end, seg.center, seg.ccw, feed, layer, on)


def _as_params(mp) -> CamParams:
    if mp is None:
        return CamParams()
    if isinstance(mp, CamParams):
        return mp
    if isinstance(mp, dict):
        return CamParams(**{k: v for k, v in mp.items() if k in CamParams.__dataclass_fields__})
    raise TypeError(mp)


def build_toolpath(doc: Document, machine_params=None) -> list[MotionItem]:
    """Turn a Document into an ordered MotionItem stream (see module doc)."""
    prm = _as_params(machine_params)
    from .flycut import scan_motion_items, scan_paths_of_part
    scan_parts = {pi for pi, part in enumerate(doc.parts) if scan_paths_of_part(part)}
    conts, keys = [], []
    for pi, part in enumerate(doc.parts):
        if pi in scan_parts:          # fly-cut parts are cut as their scan paths (cam.md §11)
            continue
        for ci, c in enumerate(part.contours):
            if c.segments:
                conts.append(c)
                keys.append((pi, ci))
    nodes = build_tree(conts, keys)
    prepared: dict[int, PreparedContour] = {}
    crafts: dict[int, Craft] = {}

    def prepare(n: Node) -> None:
        pi, ci = n.key
        craft = crafts[id(n)]
        pc = prepare_contour(n.contour, craft, n.depth, prm)
        prepared.pop(id(n), None)
        if pc is None:
            return
        pc.part, pc.index = pi, ci
        prepared[id(n)] = pc
        n.entry, n.exit = pc.entry, pc.exit

    for n in nodes:
        pi, _ci = n.key
        # a contour may carry its own craft (manual lead/start/joints on one hole; docs/craft.md)
        ov = n.contour.extra.get("craft") if isinstance(n.contour.extra, dict) else None
        crafts[id(n)] = ov if isinstance(ov, Craft) else doc.parts[pi].craft
        n.layer = crafts[id(n)].layer
        prepare(n)
    if prm.sort_mode == 5:
        # the original's sort (0x1003f010 mode 5) on the graphs' entry / exit points; open
        # contours it flips are reversed (0x1003c840 -> vtable +0xb0 = 0x10073980) and their
        # leads prepared again on the new direction
        order, flipped = toolpath_sort(nodes, prm.small_first, LOCAL_SEED)
        for n in order:
            if id(n) in flipped and not n.contour.closed:
                n.contour = reverse_contour(n.contour)
                prepare(n)
    else:
        order = order_nodes([n for n in nodes], prm.sort_mode, prm.small_first, prm.origin)
    order = [n for n in order if id(n) in prepared]
    rapid_paths = None
    if prm.avoid_cut and order:
        from .ordering import plan_rapids
        rapid_paths = plan_rapids(order, prm.origin, prm.avoid_clearance)

    items: list[MotionItem] = []
    pos = prm.origin
    layer = None
    for k_node, n in enumerate(order):
        pc = prepared[id(n)]
        lp: LayerParams = doc.layers.get(pc.layer, LayerParams(index=pc.layer, speed=prm.default_feed))
        feed = lp.speed or prm.default_feed
        items.append(Marker("contour", pc.part, pc.index))
        if math.dist(pos, pc.entry) > 1e-9:
            legs = rapid_paths[k_node] if rapid_paths is not None else [pos, pc.entry]
            for a, b in zip(legs, legs[1:]):
                if math.dist(a, b) > 1e-9:
                    items.append(Rapid(tuple(a), tuple(b)))
        if layer != pc.layer:
            items.append(LayerChange(pc.layer))
            layer = pc.layer
        pt = prm.pierce_time_s if prm.pierce_time_s is not None else float(lp.pierce.get("time_s", 0.0))
        chunks = _split_piece_at(pc.pieces, pc.cool_at, pc.curves or [None] * len(pc.pieces))
        for k, chunk in enumerate(chunks):
            items.append(Follow(True))
            items.append(Pierce(chunk[0][0].start, pc.layer, pt if k == 0 else 0.0,
                                list(lp.pierce.get("stages", [])) if k == 0 else []))
            items.append(LaserOn(pc.layer))
            if prm.start_delay_s > 0:
                items.append(Delay(prm.start_delay_s))
            for j, (seg, on, ref) in enumerate(chunk):
                mv = _move(seg, on, feed, pc.layer, ref)
                if k == 0 and j < pc.lead_in_count:
                    mv.lead = "in"      # lead-in piece: motion.interp.lead_in_index (JC-08)
                items.append(mv)
            if prm.end_delay_s > 0:
                items.append(Delay(prm.end_delay_s))
            items.append(LaserOff())
            if k + 1 < len(chunks):
                items.append(Delay(prm.cool_delay_s))   # cooling point: stop, beam off, dwell
            items.append(Follow(False))
        pos = pc.exit
    for pi in sorted(scan_parts):
        part = doc.parts[pi]
        lp = doc.layers.get(part.craft.layer, LayerParams(index=part.craft.layer, speed=prm.default_feed))
        for k, path in enumerate(scan_paths_of_part(part)):
            items += scan_motion_items(path, lp.speed or prm.default_feed, part.craft.layer, pos,
                                       pierce_time_s=float(lp.pierce.get("time_s", 0.0)),
                                       stages=list(lp.pierce.get("stages", [])), part=pi, index=k,
                                       layer_change=(layer != part.craft.layer))
            layer = part.craft.layer
            pos = path.contour.segments[-1].end
    if prm.return_to_origin and math.dist(pos, prm.origin) > 1e-9:
        items.append(Rapid(pos, prm.origin))
    return items
