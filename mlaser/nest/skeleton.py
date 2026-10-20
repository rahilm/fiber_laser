"""Cutting strategy of a nested sheet: skeleton cut-up, remnant separation cut, sheet statistics.

Rebuild extension (docs/user/cutting-strategy.md); every option is off by default
(:class:`mlaser.nest.NestParams`).

* **Remnant separation cut** (``remnant_cut``): a straight line across the whole sheet along the
  inner edge of the leftover strip (``SheetLayout.remnant``, ``spacing`` away from the parts on
  the gravity side), so the usable remnant comes free.  It is cut last.  The separated piece is
  returned as a polygon (sheet coordinates) and goes to the remnant store when the sheet has been
  cut (``queue entry extra['remnant_rings']``).
* **Skeleton cut-up** (``scrap_cut``): straight lines through the scrap (the sheet minus the parts
  and minus the separated remnant) so the skeleton comes out in pieces no larger than
  ``scrap_pitch``.  ``"grid"``: lines at a fixed pitch from the sheet corner; ``"gaps"``: the
  lines are moved into the gaps between the parts (offset cuts along the free columns and rows,
  the pitch kept as the largest distance), which gives fewer, longer lines.  A line ends
  ``strategy_kerf / 2 + scrap_clearance`` from a part outline: with a clearance of 0 it ends in
  the part's kerf, so the piece comes free without touching the part.  Lines shorter than
  ``scrap_min_length`` are dropped.  They are cut last.
* A line that ends inside the sheet starts there, in the kerf cut before it (a part's contour
  or the separation cut): no pierce in solid material.  A line from sheet edge to sheet edge is
  pierced in its middle and cut outwards in both directions, so no line starts off the sheet.

The lines are parts tagged ``Part.extra['strategy']`` (``"scrap"`` / ``"remnant_cut"``) with
``Part.extra['cut_phase']`` 3 / 2 (read by :func:`mlaser.cam.toolpath.build_toolpath`), on the
layer of the sheet's parts (``layer``), without leads or kerf compensation.

:func:`sheet_stats` gives the comparison figures of a sheet document: cut length, pierces,
rapid length (the toolpath of the default CAM parameters).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from ..model import Contour, Craft, Document, LeadParams, Line, Part, Point
from .model import (GRAVITY_BOTTOM, GRAVITY_LEFT, GRAVITY_RIGHT, GRAVITY_TOP, SCRAP_GAPS, SCRAP_GRID,
                    SCRAP_OFF, NestParams)

PHASE_TIP, PHASE_REMNANT, PHASE_SCRAP = 1, 2, 3
STRATEGY_SCRAP, STRATEGY_REMNANT = "scrap", "remnant_cut"


@dataclass
class StrategyResult:
    lines: list[Part] = field(default_factory=list)     # scrap + separation parts
    scrap_lines: int = 0
    remnant: Any = None                                  # shapely polygon of the separated remnant
    remnant_line: Optional[tuple[Point, Point]] = None
    notes: list[str] = field(default_factory=list)


def _line_part(a: Point, b: Point, layer: int, kind: str, phase: int, sheet_poly=None) -> Part:
    """One cut line as a part.  A line that ends inside the sheet (in a part's kerf or the
    separation cut, both cut before it) starts there: no pierce in solid material.  A line
    with both ends on the sheet edge is pierced in its middle and cut outwards as two open
    contours, so it never starts off the sheet."""
    def on_edge(q: Point) -> bool:
        if sheet_poly is None:
            return True
        from shapely.geometry import Point as SPoint
        return sheet_poly.exterior.distance(SPoint(q)) < 0.5 or not sheet_poly.contains(SPoint(q))
    ea, eb = on_edge(a), on_edge(b)
    if not ea or not eb:
        s, e = (a, b) if not ea else (b, a)
        cs = [Contour([Line(s, e)], closed=False)]
    else:
        m = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        cs = [Contour([Line(m, end)], closed=False) for end in (a, b) if math.dist(m, end) > 1e-6]
    cr = Craft(layer=layer, lead_in=LeadParams(kind=0), lead_out=LeadParams(kind=0))
    p = Part(cs, cr, name="remnant cut" if kind == STRATEGY_REMNANT else "scrap cut")
    p.extra["strategy"] = kind
    p.extra["cut_phase"] = phase
    return p


def _lines_of(geom) -> list:
    from shapely.geometry import LineString, MultiLineString
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, MultiLineString):
        return list(geom.geoms)
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, LineString)]


def remnant_cut(sheet_poly, used, layout_remnant, gravity: str, params: NestParams):
    """(line (a, b), separated remnant polygon) or None.  ``layout_remnant`` is the leftover
    rectangle of the layout; the line runs along its inner edge across the whole sheet."""
    from shapely.geometry import LineString, box
    if not layout_remnant or sheet_poly is None or sheet_poly.is_empty:
        return None
    rx0, ry0, rx1, ry1 = layout_remnant
    sx0, sy0, sx1, sy1 = sheet_poly.bounds
    g = gravity if gravity in (GRAVITY_LEFT, GRAVITY_RIGHT, GRAVITY_TOP, GRAVITY_BOTTOM) else GRAVITY_LEFT
    k = max(params.strategy_kerf, 0.0) / 2.0
    if g == GRAVITY_LEFT:
        pos, width = rx0, sx1 - rx0
        line = LineString([(pos, sy0 - 1), (pos, sy1 + 1)])
        keep = box(pos + k, sy0 - 1, sx1 + 1, sy1 + 1)
    elif g == GRAVITY_RIGHT:
        pos, width = rx1, rx1 - sx0
        line = LineString([(pos, sy0 - 1), (pos, sy1 + 1)])
        keep = box(sx0 - 1, sy0 - 1, pos - k, sy1 + 1)
    elif g == GRAVITY_BOTTOM:
        pos, width = ry0, sy1 - ry0
        line = LineString([(sx0 - 1, pos), (sx1 + 1, pos)])
        keep = box(sx0 - 1, pos + k, sx1 + 1, sy1 + 1)
    else:
        pos, width = ry1, ry1 - sy0
        line = LineString([(sx0 - 1, pos), (sx1 + 1, pos)])
        keep = box(sx0 - 1, sy0 - 1, sx1 + 1, pos - k)
    if width < max(params.remnant_min_width, 1.0):
        return None
    if used is not None and not used.is_empty and line.distance(used) < k + 1e-6:
        return None                                   # the line would touch a part
    segs = _lines_of(line.intersection(sheet_poly))
    if not segs:
        return None
    seg = max(segs, key=lambda s: s.length)
    (ax, ay), (bx, by) = seg.coords[0], seg.coords[-1]
    rem = sheet_poly.intersection(keep)
    if rem.is_empty:
        return None
    if rem.geom_type != "Polygon":
        rem = max(getattr(rem, "geoms", [rem]), key=lambda q: q.area)
    return ((ax, ay), (bx, by)), rem


def _positions(lo: float, hi: float, pitch: float) -> list[float]:
    n = int(math.floor((hi - lo) / pitch))
    return [lo + pitch * k for k in range(1, n + 1) if lo + pitch * k < hi - 1e-6]


def _gap_positions(lo: float, hi: float, pitch: float, blocks: list[tuple[float, float]]) -> list[float]:
    """Positions in [lo, hi] at most ``pitch`` apart, in the free intervals of ``blocks``
    (part extents on this axis) when there is one within reach."""
    blocks = sorted(b for b in blocks if b[1] > lo and b[0] < hi)
    free = []
    cur = lo
    for b0, b1 in blocks:
        if b0 > cur:
            free.append((cur, b0))
        cur = max(cur, b1)
    if cur < hi:
        free.append((cur, hi))
    mids = [(a + b) / 2.0 for a, b in free if b - a > 1e-6]
    out: list[float] = []
    last = lo
    while hi - last > pitch + 1e-6:
        target = last + pitch
        ok = [m for m in mids if last + 0.25 * pitch < m <= target + 1e-9]
        p = max(ok) if ok else target
        out.append(p)
        last = p
    return out


def scrap_lines(sheet_poly, used, params: NestParams, remnant=None) -> list:
    """Scrap cut-up lines (shapely LineStrings) for ``params.scrap_cut`` (module doc)."""
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union
    mode = params.scrap_cut
    if mode not in (SCRAP_GRID, SCRAP_GAPS) or sheet_poly is None or sheet_poly.is_empty:
        return []
    pitch = max(float(params.scrap_pitch), 10.0)
    k = max(params.strategy_kerf, 0.0) / 2.0
    keep_off = k + max(params.scrap_clearance, 0.0)
    filled = []
    if used is not None and not used.is_empty:
        for g in getattr(used, "geoms", [used]):
            if isinstance(g, Polygon):
                filled.append(Polygon(g.exterior))
    parts = unary_union(filled) if filled else None
    region = sheet_poly
    if parts is not None:
        region = region.difference(parts.buffer(keep_off, join_style="mitre"))
    if remnant is not None and not remnant.is_empty:
        region = region.difference(remnant.buffer(k, join_style="mitre"))
    if region.is_empty:
        return []
    x0, y0, x1, y1 = sheet_poly.bounds
    if mode == SCRAP_GAPS and filled:
        xs = _gap_positions(x0, x1, pitch, [(g.bounds[0] - keep_off, g.bounds[2] + keep_off) for g in filled])
        ys = _gap_positions(y0, y1, pitch, [(g.bounds[1] - keep_off, g.bounds[3] + keep_off) for g in filled])
    else:
        xs, ys = _positions(x0, x1, pitch), _positions(y0, y1, pitch)
    cand = [LineString([(x, y0 - 1), (x, y1 + 1)]) for x in xs] + \
        [LineString([(x0 - 1, y), (x1 + 1, y)]) for y in ys]
    out = []
    for ln in cand:
        for piece in _lines_of(ln.intersection(region)):
            if piece.length >= max(params.scrap_min_length, 0.0):
                out.append(piece)
    return out


def strategy_lines(sheet_poly, used, layout_remnant, gravity: str, params: NestParams,
                   layer: int = 1) -> StrategyResult:
    """The cut-up and separation lines of one sheet (parts tagged for the toolpath)."""
    res = StrategyResult()
    remnant = None
    if params.remnant_cut:
        rc = remnant_cut(sheet_poly, used, layout_remnant, gravity, params)
        if rc is None:
            if layout_remnant:
                res.notes.append("remnant cut: leftover too narrow or blocked, no separation cut")
        else:
            (a, b), remnant = rc
            res.remnant, res.remnant_line = remnant, (a, b)
    if params.scrap_cut != SCRAP_OFF:
        for seg in scrap_lines(sheet_poly, used, params, remnant):
            (ax, ay), (bx, by) = seg.coords[0], seg.coords[-1]
            res.lines.append(_line_part((ax, ay), (bx, by), layer, STRATEGY_SCRAP, PHASE_SCRAP, sheet_poly))
            res.scrap_lines += 1
    if res.remnant_line is not None:
        a, b = res.remnant_line
        res.lines.append(_line_part(a, b, layer, STRATEGY_REMNANT, PHASE_REMNANT, sheet_poly))
    return res


def is_strategy_part(p: Part) -> bool:
    return bool(p.extra.get("strategy"))


def sheet_stats(doc: Document) -> dict:
    """Comparison figures of a sheet document: cut length (mm, the contours; leads not
    counted), pierces (one per contour) and parts (strategy lines not counted)."""
    cut, pierces, parts = 0.0, 0, 0
    for p in doc.parts:
        n = 0
        for c in p.contours:
            if c.segments:
                cut += c.length()
                n += 1
        pierces += n
        if n and not is_strategy_part(p):
            parts += 1
    return {"cut_length_mm": cut, "pierces": pierces, "parts": parts}


__all__ = ["StrategyResult", "strategy_lines", "scrap_lines", "remnant_cut", "sheet_stats",
           "is_strategy_part", "PHASE_TIP", "PHASE_SCRAP", "PHASE_REMNANT", "STRATEGY_SCRAP",
           "STRATEGY_REMNANT"]
