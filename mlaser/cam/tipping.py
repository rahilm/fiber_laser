"""Tip analysis on a slat bed and the tip-avoiding cut order (rebuild extension, opt-in).

When the last cut frees a part (or a slug, the material inside a hole), it rests on the bed's
slats.  It **tips** when its centre of gravity is not over its support: it rests on one slat
only, or its support does not surround the centre of gravity (an L shape over two slats).  A
tipped part stands up out of the sheet and the head, moving at cut height or in a rapid, hits
it.  This module checks every freed piece against a bed model and mitigates the risk:

* :class:`BedModel`: slat pitch (centre to centre), direction (the slats run along ``"y"``,
  spaced along X, or along ``"x"``), width, and their position (``offset`` of one slat centre
  in sheet coordinates; ``None`` = unknown, then every position of the sheet on the slats is
  tried and the worst one counts).  :meth:`BedModel.from_config` reads ``bed.*`` from
  :class:`mlaser.services.config.ConfigService`.
* :func:`analyse`: one :class:`TipPiece` per freed piece (outer contours with their holes
  removed; slugs = holes with the parts nested in them removed): ``state`` is ``"held"``
  (its contour has micro-joints, it stays in the skeleton), ``"small"`` (smaller than
  ``min_size``: falls through or too small to matter), ``"drops"`` (falls between the slats),
  ``"stable"`` or ``"tips"``.  The support is the piece intersected with the slat strips; it is
  stable when the support's convex hull, shrunk by ``margin``, contains the centre of gravity.
* :func:`apply`: for every tipping piece, the strategy (``Technology.rules['tip']`` can set it
  per material):
  - ``"joint"`` (default): a micro-joint on its contour on the side of the centre of gravity
    (away from the lead-in), so the piece stays in the sheet;
  - ``"last"``: the part is cut after every other part (``Part.extra['cut_phase'] = 1``) and the
    rapids go around it once it is cut;
  - ``"avoid"``: only the rapids go around it (``Part.extra['tip_risk']`` /
    ``Contour.extra['tip_risk']``, read by :func:`mlaser.cam.toolpath.build_toolpath`).

Rapids always lift the head (``Rapid.lift``, ``Follow(False)`` before each move); the rapid
avoidance of :func:`mlaser.cam.ordering.plan_rapids` routes them around the risk pieces already
cut (and around cut holes when *Avoid cut holes* is on).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from ..model import Document, Point

STRATEGIES = ("joint", "last", "avoid")
STATES = ("held", "small", "drops", "stable", "tips")


def _mm_size(w: float, h: float) -> str:
    return f"{w:.0f} x {h:.0f} mm"


#: how :attr:`TipRisk.message` shows a size (text only); the display-units service
#: (mlaser.services.units) installs its formatter here
SIZE_TEXT: list = [_mm_size]


@dataclass
class BedModel:
    pitch: float = 100.0  # slat pitch (mm, centre to centre)
    direction: str = "y"  # the slats run along "y" (spaced along X) or "x"
    offset: Optional[float] = None  # one slat centre in sheet coordinates; None = unknown
    width: float = 3.0  # slat width (mm)
    min_size: float = 10.0  # smaller pieces are not analysed (mm, larger bbox side)
    margin: float = 2.0  # stability margin around the centre of gravity (mm)
    phases: int = 8  # positions tried when the offset is unknown

    @classmethod
    def from_config(cls, cfg: Any) -> "BedModel":
        def g(key: str, default: Any) -> Any:
            try:
                return cfg.get(key)
            except Exception:  # noqa: BLE001 - no such setting: the default
                return default

        off = float(g("bed.slat_offset", -1.0))
        return cls(
            pitch=max(1.0, float(g("bed.slat_pitch", 100.0))),
            direction=("y", "x")[1 if int(g("bed.slat_direction", 0)) == 1 else 0],
            offset=None if off < 0 else off,
            width=max(0.1, float(g("bed.slat_width", 3.0))),
            min_size=max(0.0, float(g("bed.tip_min_size", 10.0))),
            margin=max(0.0, float(g("bed.stability_margin", 2.0))),
        )

    def offsets(self) -> list[float]:
        if self.offset is not None:
            return [float(self.offset)]
        n = max(1, int(self.phases))
        return [self.pitch * k / n for k in range(n)]

    def strips(self, bounds: tuple[float, float, float, float], offset: float):
        """The slat strips (shapely boxes) under ``bounds``."""
        from shapely.geometry import box

        x0, y0, x1, y1 = bounds
        lo, hi = (x0, x1) if self.direction == "y" else (y0, y1)
        k0 = math.floor((lo - offset - self.width) / self.pitch)
        k1 = math.ceil((hi - offset + self.width) / self.pitch)
        out = []
        h = self.width / 2.0
        for k in range(k0, k1 + 1):
            c = offset + k * self.pitch
            if c + h < lo or c - h > hi:
                continue
            out.append(
                box(c - h, y0 - 1, c + h, y1 + 1)
                if self.direction == "y"
                else box(x0 - 1, c - h, x1 + 1, c + h)
            )
        return out


@dataclass
class TipPiece:
    key: tuple  # (part, contour) of the contour whose cut frees the piece
    slug: bool  # the piece is the inside of a hole
    state: str  # held | small | drops | stable | tips
    cg: Point = (0.0, 0.0)
    size: tuple = (0.0, 0.0)
    support: Optional[Point] = None  # centre of the support in the tipping position
    offset: Optional[float] = None  # slat offset of the worst case

    @property
    def at_risk(self) -> bool:
        return self.state == "tips"

    @property
    def message(self) -> str:
        pi, ci = self.key
        what = "slug of" if self.slug else "part"
        return (
            f"{what} part {pi + 1} contour {ci + 1} ({SIZE_TEXT[0](self.size[0], self.size[1])}) "
            "can tip on the slats"
        )


def _polygon(c):
    from shapely.geometry import Polygon
    from ..geom.contour import polyline

    pts = polyline(c, 0.2)
    if len(pts) < 3:
        return None
    g = Polygon(pts)
    if not g.is_valid:
        g = g.buffer(0)
    return g if g.area > 1e-9 else None


def piece_state(geom, bed: BedModel) -> tuple[str, Optional[Point], Optional[float]]:
    """(state, support centre, offset) of one free piece (a shapely polygon) on the bed."""
    from shapely.geometry import Point as SPoint
    from shapely.ops import unary_union

    x0, y0, x1, y1 = geom.bounds
    if max(x1 - x0, y1 - y0) < bed.min_size:
        return "small", None, None
    cg = geom.centroid
    worst = ("drops", None, None)
    stable_seen = False
    for off in bed.offsets():
        sup = [geom.intersection(s) for s in bed.strips(geom.bounds, off)]
        sup = [s for s in sup if not s.is_empty and s.area > 1e-6]
        if not sup:
            continue  # falls between the slats at this position
        hull = unary_union(sup).convex_hull
        core = hull.buffer(-bed.margin) if bed.margin > 0 else hull
        if core.is_empty or not core.contains(SPoint(cg.x, cg.y)):
            c = hull.centroid
            return "tips", (c.x, c.y), off
        stable_seen = True
    if stable_seen:
        return "stable", None, None
    return worst


def analyse(doc: Document, bed: Optional[BedModel] = None, prm: Any = None) -> list[TipPiece]:
    """Every freed piece of the document (see the module doc)."""
    from .ordering import build_tree

    bed = bed or BedModel()
    conts, keys = [], []
    for pi, part in enumerate(doc.parts):
        if part.extra.get("strategy"):
            continue  # cut-up / separation lines free no part
        for ci, c in enumerate(part.contours):
            if c.segments and c.closed:
                conts.append(c)
                keys.append((pi, ci))
    nodes = build_tree(conts, keys)
    from .manual import contour_craft

    out = []
    for n in nodes:
        g = _polygon(n.contour)
        if g is None:
            continue
        for ch in n.children:
            h = _polygon(ch.contour)
            if h is not None:
                g = g.difference(h)
        if g.is_empty:
            continue
        pi, ci = n.key
        slug = n.depth % 2 == 0
        x0, y0, x1, y1 = g.bounds
        cg = g.centroid
        size = (x1 - x0, y1 - y0)
        cr = contour_craft(doc.parts[pi], ci)
        if cr.micro_joints:
            out.append(TipPiece(n.key, slug, "held", (cg.x, cg.y), size))
            continue
        state, sup, off = piece_state(g, bed)
        out.append(TipPiece(n.key, slug, state, (cg.x, cg.y), size, sup, off))
    return out


@dataclass
class TipReport:
    pieces: list[TipPiece] = field(default_factory=list)
    joints: list[tuple] = field(default_factory=list)  # (key, position mm)
    last: list[int] = field(default_factory=list)  # parts cut last
    avoided: list[tuple] = field(default_factory=list)  # keys the rapids avoid

    @property
    def at_risk(self) -> list[TipPiece]:
        return [p for p in self.pieces if p.at_risk]


def _joint_position(c, cg: Point, support: Optional[Point], start_mm: float, keep: float) -> float:
    """Arc length (mm, from the contour's first point) of the joint: the contour point farthest
    towards the centre of gravity side (from the support centre), at least ``keep`` mm from the
    start along the contour."""
    from ..geom.contour import point_at

    L = c.length()
    if support is not None and math.dist(cg, support) > 1e-6:
        v = (cg[0] - support[0], cg[1] - support[1])
    else:
        v = None
    n = max(16, min(720, int(L / 1.0)))
    scored = []
    for k in range(n):
        s = L * k / n
        dd = abs(s - start_mm)
        dd = min(dd, L - dd)
        if dd < keep:
            continue
        p = point_at(c, s)
        if v is not None:
            score = ((p[0] - cg[0]) * v[0] + (p[1] - cg[1]) * v[1]) / math.hypot(*v)
        else:
            score = dd
        scored.append((s, score))
    if not scored:
        return 0.0
    bs = max(sc for _s, sc in scored)
    # the middle of the best stretch (a straight edge facing that way), not its corner
    best = [s for s, sc in scored if sc >= bs - 0.5]
    return best[len(best) // 2]


def apply(
    doc: Document,
    bed: Optional[BedModel] = None,
    strategy: str = "joint",
    joint_width: float = 0.5,
    slugs: bool = True,
    prm: Any = None,
) -> TipReport:
    """Mitigate every tipping piece with ``strategy`` (module doc); returns what was done."""
    from .manual import contour_craft

    if strategy not in STRATEGIES:
        raise ValueError(f"unknown tip strategy {strategy!r} (one of {', '.join(STRATEGIES)})")
    rep = TipReport(analyse(doc, bed, prm))
    for piece in rep.at_risk:
        pi, ci = piece.key
        part = doc.parts[pi]
        c = part.contours[ci]
        if piece.slug and not slugs:
            continue
        if strategy == "joint":
            cr = contour_craft(part, ci, create=True)
            start = float(cr.start_param or 0.0) * c.length()
            keep = max(2.0, float(cr.lead_in.length) + 2.0)
            pos = _joint_position(c, piece.cg, piece.support, start, keep)
            cr.micro_joints = list(cr.micro_joints) + [(pos, float(joint_width))]
            rep.joints.append((piece.key, pos))
            continue
        if strategy == "last" and not piece.slug:
            part.extra["cut_phase"] = max(int(part.extra.get("cut_phase", 0) or 0), 1)
            rep.last.append(pi)
        if piece.slug:
            c.extra["tip_risk"] = True
            part.extra["tip_risk_holes"] = True
        else:
            part.extra["tip_risk"] = True
        rep.avoided.append(piece.key)
    return rep


def strategy_from_technology(tech: Any) -> tuple[str, float]:
    """(strategy, joint width) of ``Technology.rules['tip']`` (defaults ``"joint"``, 0.5 mm)."""
    r = dict(((getattr(tech, "rules", None) or {}).get("tip") or {}))
    s = str(r.get("strategy", "joint"))
    return (s if s in STRATEGIES else "joint"), float(r.get("joint_width_mm", r.get("joint_width", 0.5)))


__all__ = [
    "BedModel",
    "TipPiece",
    "TipReport",
    "analyse",
    "apply",
    "piece_state",
    "strategy_from_technology",
    "STRATEGIES",
    "STATES",
]
