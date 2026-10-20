"""Lead-in collision check and automatic fix (rebuild extension, opt-in).

A lead-in pierces in the scrap next to its contour.  It goes wrong when the lead (with its kerf)
reaches another contour (a neighbouring part, a hole, a part nested in a hole), crosses its own
contour, or pierces at or beyond the sheet edge.  :func:`analyse` finds those leads on the
toolpath exactly as it is cut (the same tree, crafts, start points, kerf child and lead geometry
as :func:`mlaser.cam.toolpath.build_toolpath`); :func:`fix` repairs them in the document:

1. **move the start** along the contour to the candidates of :mod:`mlaser.cam.leadplace` (straight
   segments, corners, fallbacks), the first one whose lead is clear wins;
2. else **shorten the lead** with the original's lead-check rule (CADModule 0x10020e50,
   docs/re/craft.md §3): the new length is the distance d from the start point to the first
   conflict along the lead, times 0.7 when the conflict is another contour (or the sheet edge)
   and the lead is not a straight line (type 1); repeated (x 0.7) until it is clear;
3. else **flag it**: ``Craft.lead_error`` (the original's error mark, drawn red), the length kept.

Clearances (:class:`LeadCheckParams`): the lead's centre line keeps ``kerf / 2 + clearance``
from every other contour outline (so its kerf stays out of the neighbour), its pierce point
keeps ``edge_clearance`` from the sheet edge.  The sheet is the ``sheet`` argument (a shapely
polygon or an ``(x0, y0, x1, y1)`` box), else ``Document.extra['sheet_bounds']`` (set on nest
sheets), else the edge test is skipped.

Nothing here runs unless called: the default toolpath is unchanged.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any, Optional

from ..model import Contour, Craft, Document, Point
from .leadplace import LeadRules, candidates

KINDS = ("contour", "own", "edge")


@dataclass
class LeadCheckParams:
    kerf: float = 0.2               # mm, beam width when the craft has no kerf width
    clearance: float = 0.5          # mm kept between the lead's kerf and another contour
    edge_clearance: float = 2.0     # mm kept between the pierce point and the sheet edge
    min_length: float = 0.5         # mm: a lead that would get shorter is flagged instead
    move_start: bool = True
    shorten: bool = True
    max_candidates: int = 24
    rules: LeadRules = field(default_factory=LeadRules)


@dataclass
class LeadIssue:
    key: tuple                       # (part index, contour index)
    kind: str                        # contour | own | edge
    point: Point                     # pierce point of the lead
    other: Optional[tuple] = None    # (part, contour) hit, for kind "contour"

    @property
    def message(self) -> str:
        pi, ci = self.key
        where = f"part {pi + 1} contour {ci + 1}"
        if self.kind == "own":
            return f"{where}: the lead-in crosses its own contour"
        if self.kind == "edge":
            return f"{where}: the lead-in pierces at the sheet edge"
        if self.other is not None:
            return f"{where}: the lead-in reaches part {self.other[0] + 1} contour {self.other[1] + 1}"
        return f"{where}: the lead-in reaches another contour"


@dataclass
class LeadFix:
    key: tuple
    action: str                      # moved | shortened | flagged
    before: float = 0.0              # start fraction (moved) or lead length (shortened)
    after: float = 0.0


@dataclass
class FixReport:
    found: list[LeadIssue] = field(default_factory=list)
    fixes: list[LeadFix] = field(default_factory=list)
    remaining: list[LeadIssue] = field(default_factory=list)

    def count(self, action: str) -> int:
        return sum(1 for f in self.fixes if f.action == action)


# ============================================================================ preparation
def _as_cam(prm: Any):
    from .toolpath import _as_params
    return _as_params(prm)


def _node_craft(doc: Document, n) -> Craft:
    pi, _ci = n.key
    c = n.contour
    ov = c.craft if isinstance(getattr(c, "craft", None), Craft) else (
        c.extra.get("craft") if isinstance(c.extra, dict) else None)
    return ov if isinstance(ov, Craft) else doc.parts[pi].craft


def prepared_nodes(doc: Document, prm: Any = None) -> list:
    """[(node, craft, PreparedContour)] the way :func:`build_toolpath` prepares them (fly-cut
    and raster parts excluded)."""
    from .flycut import scan_paths_of_part
    from .ordering import build_tree
    from .raster_engraving import raster_part_indices
    from .toolpath import prepare_cached
    prm = _as_cam(prm)
    skip = {pi for pi, p in enumerate(doc.parts) if scan_paths_of_part(p)} | raster_part_indices(doc)
    conts, keys = [], []
    for pi, part in enumerate(doc.parts):
        if pi in skip:
            continue
        for ci, c in enumerate(part.contours):
            if c.segments:
                conts.append(c)
                keys.append((pi, ci))
    out = []
    for n in build_tree(conts, keys):
        craft = _node_craft(doc, n)
        pc = prepare_cached(n.contour, craft, n.depth, prm)
        if pc is not None:
            out.append((n, craft, pc))
    return out


def _poly(segs, tol: float = 0.05) -> list[Point]:
    if not segs:
        return []
    return list(Contour(list(segs), closed=False).points(tol))


def _lead_points(pc) -> list[Point]:
    """Lead-in polyline in travel order (pierce point first, contour start last)."""
    return _poly([s for s, _on in pc.pieces[:pc.lead_in_count]])


def _sheet_of(doc: Document, sheet: Any):
    from shapely.geometry import Polygon, box
    if sheet is None:
        sb = doc.extra.get("sheet_bounds") if isinstance(doc.extra, dict) else None
        if sb and len(sb) == 4:
            sheet = tuple(float(v) for v in sb)
    if sheet is None:
        return None
    if isinstance(sheet, (tuple, list)) and len(sheet) == 4 and not isinstance(sheet[0], (tuple, list)):
        return box(*sheet)
    if isinstance(sheet, (tuple, list)):
        return Polygon(sheet)
    return sheet


class _Index:
    """Contour outlines in an STRtree for the distance tests."""

    def __init__(self, items: list) -> None:
        import shapely
        from shapely.geometry import LineString
        self.keys, self.geoms, self.kerf = [], [], {}
        for n, craft, _pc in items:
            pts = list(n.contour.points(0.05))
            if n.contour.closed and pts:
                pts = pts + [pts[0]]
            if len(pts) < 2:
                continue
            self.keys.append(n.key)
            self.geoms.append(LineString(pts))
        self.by_key = dict(zip(self.keys, self.geoms))
        self.tree = shapely.STRtree(self.geoms) if self.geoms else None

    def near(self, geom, r: float, exclude) -> list[tuple]:
        if self.tree is None:
            return []
        out = []
        for i in self.tree.query(geom.buffer(r)).tolist():
            k = self.keys[i]
            if k == exclude:
                continue
            out.append((k, self.geoms[i]))
        return out


def _kerf(craft: Craft, p: LeadCheckParams) -> float:
    kw = float(craft.kerf_width or 0.0)
    return kw if kw > 0 else p.kerf


def _trim_end(pts: list[Point], length: float) -> list[Point]:
    """``pts`` without its last ``length`` mm (the part next to the contour start)."""
    if length <= 0 or len(pts) < 2:
        return pts
    acc = 0.0
    rev = list(reversed(pts))
    for k in range(1, len(rev)):
        d = math.dist(rev[k - 1], rev[k])
        if acc + d >= length:
            u = (length - acc) / d if d > 0 else 0.0
            q = (rev[k - 1][0] + (rev[k][0] - rev[k - 1][0]) * u, rev[k - 1][1] + (rev[k][1] - rev[k - 1][1]) * u)
            return list(reversed([q] + rev[k:]))
        acc += d
    return []


def _own_trim(r: float, lead_angle_deg: float) -> float:
    s = abs(math.sin(math.radians(lead_angle_deg or 90.0)))
    return r / max(s, 0.25) + 0.05


def _lead_conflicts(key, craft: Craft, pts: list[Point], idx: _Index, sheet, p: LeadCheckParams,
                    first_only: bool = False) -> list[LeadIssue]:
    from shapely.geometry import LineString, Point as SPoint
    if len(pts) < 2:
        return []
    out: list[LeadIssue] = []
    Q = pts[0]
    lead = LineString(pts)
    r = _kerf(craft, p) / 2.0 + p.clearance
    for k, g in idx.near(lead, r, key):
        if lead.distance(g) < r:
            out.append(LeadIssue(key, "contour", Q, k))
            if first_only:
                return out
            break
    own = idx.by_key.get(key)
    if own is not None:
        tp = _trim_end(pts, _own_trim(r, craft.lead_in.angle_deg))
        if len(tp) >= 2 and LineString(tp).intersects(own):
            out.append(LeadIssue(key, "own", Q))
            if first_only:
                return out
    if sheet is not None:
        inner = sheet.buffer(-p.edge_clearance) if p.edge_clearance > 0 else sheet
        if not inner.contains(SPoint(Q)) or not sheet.buffer(1e-9).contains(lead):
            out.append(LeadIssue(key, "edge", Q))
    return out


def analyse(doc: Document, prm: Any = None, sheet: Any = None,
            params: Optional[LeadCheckParams] = None) -> list[LeadIssue]:
    """Every lead-in that collides (one issue per lead and kind), in contour order."""
    p = params or LeadCheckParams()
    items = prepared_nodes(doc, prm)
    idx = _Index(items)
    sh = _sheet_of(doc, sheet)
    out = []
    for n, craft, pc in items:
        if pc.lead_in_count <= 0:
            continue
        out += _lead_conflicts(n.key, craft, _lead_points(pc), idx, sh, p)
    out.sort(key=lambda i: (i.key, KINDS.index(i.kind)))
    return out


# ============================================================================ fix
def _clear_at(key, craft, pts, idx, sheet, p) -> bool:
    return not _lead_conflicts(key, craft, pts, idx, sheet, p, first_only=True)


def _first_conflict(key, craft: Craft, pts: list[Point], idx: _Index, sheet, p: LeadCheckParams
                    ) -> tuple[float, str]:
    """(distance from the contour start along the lead to the first conflict, its kind)."""
    from shapely.geometry import LineString, Point as SPoint
    r = _kerf(craft, p) / 2.0 + p.clearance
    rev = list(reversed(pts))                 # from the contour start outwards
    lead = LineString(pts)
    near = [g for _k, g in idx.near(lead, r, key)]
    own = idx.by_key.get(key)
    trim = _own_trim(r, craft.lead_in.angle_deg)
    inner = None
    if sheet is not None:
        inner = sheet.buffer(-p.edge_clearance) if p.edge_clearance > 0 else sheet
    acc = 0.0
    step = 0.05
    for a, b in zip(rev, rev[1:]):
        d = math.dist(a, b)
        m = max(1, int(math.ceil(d / step)))
        for j in range(1, m + 1):
            q = (a[0] + (b[0] - a[0]) * j / m, a[1] + (b[1] - a[1]) * j / m)
            s = acc + d * j / m
            sp = SPoint(q)
            if any(g.distance(sp) < r for g in near):
                return s, "contour"
            if own is not None and s > trim and own.distance(sp) < 1e-3:
                return s, "own"
            if inner is not None and not inner.contains(sp):
                return s, "edge"
        acc += d
    return acc, ""


def _set_start(doc, key, frac: float) -> None:
    from .manual import contour_craft
    pi, ci = key
    contour_craft(doc.parts[pi], ci, create=True).start_param = float(frac)


def _set_length(doc, key, length: float, error: bool = False) -> None:
    from .manual import contour_craft
    pi, ci = key
    cr = contour_craft(doc.parts[pi], ci, create=True)
    cr.lead_in = copy.deepcopy(cr.lead_in)
    cr.lead_in.length = float(length)
    if error:
        cr.lead_error = True


def _trial(n, craft: Craft, prm, **changes):
    from .toolpath import prepare_contour
    cr = copy.deepcopy(craft)
    for k, v in changes.items():
        if k == "length":
            cr.lead_in = copy.deepcopy(cr.lead_in)
            cr.lead_in.length = v
        else:
            setattr(cr, k, v)
    pc = prepare_contour(n.contour, cr, n.depth, prm)
    return cr, (_lead_points(pc) if pc is not None and pc.lead_in_count > 0 else [])


def fix(doc: Document, prm: Any = None, sheet: Any = None, params: Optional[LeadCheckParams] = None,
        keys: Optional[set] = None) -> FixReport:
    """Repair every colliding lead-in of ``doc`` in place (module doc: move, shorten, flag).
    ``keys`` limits the repair to those (part, contour) keys."""
    p = params or LeadCheckParams()
    prm = _as_cam(prm)
    items = prepared_nodes(doc, prm)
    idx = _Index(items)
    sh = _sheet_of(doc, sheet)
    rep = FixReport()
    for n, craft, pc in items:
        if pc.lead_in_count <= 0 or (keys is not None and n.key not in keys):
            continue
        pts = _lead_points(pc)
        found = _lead_conflicts(n.key, craft, pts, idx, sh, p)
        if not found:
            continue
        rep.found += found
        before = craft.start_param
        done = False
        if p.move_start and n.contour.closed:
            for cand in candidates(n.contour, p.rules)[:max(1, p.max_candidates)]:
                cr, tp = _trial(n, craft, prm, start_param=cand.frac)
                if tp and _clear_at(n.key, cr, tp, idx, sh, p):
                    _set_start(doc, n.key, cand.frac)
                    rep.fixes.append(LeadFix(n.key, "moved", float(before or 0.0), cand.frac))
                    done = True
                    break
        if not done and p.shorten:
            L0 = float(craft.lead_in.length)
            d, kind = _first_conflict(n.key, craft, pts, idx, sh, p)
            factor = 1.0 if (kind == "own" or craft.lead_in.kind == 1) else 0.7
            new = min(L0, d * factor)
            for _ in range(8):
                if new < p.min_length:
                    break
                cr, tp = _trial(n, craft, prm, length=new)
                if tp and _clear_at(n.key, cr, tp, idx, sh, p):
                    _set_length(doc, n.key, new)
                    rep.fixes.append(LeadFix(n.key, "shortened", L0, new))
                    done = True
                    break
                new *= 0.7
        if not done:
            from .manual import contour_craft
            pi, ci = n.key
            contour_craft(doc.parts[pi], ci, create=True).lead_error = True
            rep.fixes.append(LeadFix(n.key, "flagged", float(craft.lead_in.length), float(craft.lead_in.length)))
    if rep.found:
        rep.remaining = analyse(doc, prm, sheet, p)
    return rep


def mark(doc: Document, issues: list[LeadIssue]) -> int:
    """Set the lead-check error mark on the contours of ``issues`` (drawn red); returns how many."""
    from .manual import contour_craft
    keys = sorted({i.key for i in issues})
    for pi, ci in keys:
        contour_craft(doc.parts[pi], ci, create=True).lead_error = True
    return len(keys)


__all__ = ["LeadCheckParams", "LeadIssue", "LeadFix", "FixReport", "analyse", "fix", "mark",
           "prepared_nodes", "KINDS"]
