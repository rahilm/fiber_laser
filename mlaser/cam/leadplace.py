"""Automatic lead placement: where each closed contour starts (rebuild extension, opt-in).

The original picks the start on the longest side when a lead is applied (``choose_start`` mode 0,
docs/re/cam.md §3).  This module ranks start candidates by configurable rules
(docs/user/cutting-strategy.md):

* **corners**: vertices where the contour turns by at least ``180 - corner_max_angle`` degrees.
  A start in a corner hides the pierce mark in the corner; with ``prefer = "auto"`` a contour
  that has such a corner starts in one (the sharpest, then the one between the longest sides);
* **straight segments**: a point on a straight segment at least ``min_feature`` long
  (``straight_pos`` along it, ``edge_margin`` kept from its ends), the longest first;
* **small features** are avoided: segments shorter than ``min_feature`` (small arcs, notches,
  flattened curves) carry no start, and a corner needs ``min_feature`` on both sides;
* **finished edges**: the segments listed in ``Contour.extra['finished_edges']`` (indices) carry
  no start and neither do the corners at their ends; the remaining candidates are ranked by
  their distance from them, so the start goes to the side away from a finished (visible) edge;
* ``prefer_side`` ("left", "right", "bottom", "top"): candidates on that side of the part first;
* fallbacks: eight evenly spaced positions, so a collision fix (:mod:`mlaser.cam.collision`)
  always has somewhere to go on round contours.

Per-material rules come from the technology database (``Technology.rules['lead_place']``,
:meth:`LeadRules.from_dict`).  :func:`place_leads` writes the start as the contour's
``start_param`` (a per-contour craft, :func:`mlaser.cam.manual.contour_craft`), the field a
manual start or a .chf start uses, so the result is visible, saved and undoable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Iterable, Optional

from ..model import Arc, Contour, Document, Line, Part, Point
from ..geom.contour import seg_tangent

PREFER = ("auto", "corner", "straight", "keep")
SIDES = ("", "left", "right", "bottom", "top")


@dataclass
class LeadRules:
    """Rules for :func:`candidates` / :func:`place_leads` (defaults: docs/user/cutting-strategy.md)."""

    prefer: str = "auto"  # auto | corner | straight | keep (leave the start alone)
    min_feature: float = 5.0  # mm: shorter segments (small features) carry no start
    corner_max_angle: float = 100.0  # deg: corners whose angle is at most this are "sharp"
    straight_pos: float = 0.5  # where on a straight segment (fraction of its length)
    edge_margin: float = 2.0  # mm kept from the ends of a straight segment
    prefer_side: str = ""  # "" | left | right | bottom | top
    holes: bool = True  # place the start of holes too
    outers: bool = True  # and of outer contours

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "LeadRules":
        """From ``Technology.rules['lead_place']`` (unknown keys ignored, ``*_mm`` / ``*_deg``
        spellings accepted)."""
        d = dict(d or {})
        alias = {
            "min_feature_mm": "min_feature",
            "corner_max_angle_deg": "corner_max_angle",
            "edge_margin_mm": "edge_margin",
        }
        for a, b in alias.items():
            if a in d:
                d.setdefault(b, d.pop(a))
        names = {f.name for f in fields(cls)}
        out = cls(**{k: v for k, v in d.items() if k in names})
        if out.prefer not in PREFER:
            out.prefer = "auto"
        if out.prefer_side not in SIDES:
            out.prefer_side = ""
        return out


@dataclass(frozen=True)
class Candidate:
    frac: float  # start as a fraction of the contour length (Craft.start_param)
    point: Point
    kind: str  # "corner" | "straight" | "fallback"
    rank: tuple  # sort key (lower first)


def _turn(a: Point, b: Point) -> float:
    """Signed turn angle (rad) from direction a to direction b."""
    return math.atan2(a[0] * b[1] - a[1] * b[0], a[0] * b[0] + a[1] * b[1])


def _seg_dist(p: Point, s) -> float:
    if isinstance(s, Arc):
        n = max(4, min(64, int(s.length() / 1.0)))
        return min(math.dist(p, s.point_at(k / n)) for k in range(n + 1))
    ax, ay = s.start
    dx, dy = s.end[0] - ax, s.end[1] - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 <= 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.dist(p, (ax + dx * t, ay + dy * t))


def candidates(c: Contour, rules: Optional[LeadRules] = None) -> list[Candidate]:
    """Start candidates of a closed contour, best first (see the module doc)."""
    rules = rules or LeadRules()
    if not c.closed or not c.segments:
        return []
    segs = list(c.segments)
    lens = [s.length() for s in segs]
    L = sum(lens)
    if L <= 1e-9:
        return []
    finished = set()
    ex = c.extra if isinstance(c.extra, dict) else {}
    for i in ex.get("finished_edges") or ():
        try:
            finished.add(int(i) % len(segs))
        except (TypeError, ValueError):
            continue
    fin_segs = [segs[i] for i in sorted(finished)]
    xs = [s.start[0] for s in segs]
    ys = [s.start[1] for s in segs]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w, h = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)

    def side_score(p: Point) -> float:
        sd = rules.prefer_side
        if sd == "left":
            return (p[0] - x0) / w
        if sd == "right":
            return (x1 - p[0]) / w
        if sd == "bottom":
            return (p[1] - y0) / h
        if sd == "top":
            return (y1 - p[1]) / h
        return 0.0

    def fin_score(p: Point) -> float:  # nearer to a finished edge = worse
        if not fin_segs:
            return 0.0
        return -min(_seg_dist(p, s) for s in fin_segs)

    corners, straights = [], []
    cum = [0.0]
    for ln in lens:
        cum.append(cum[-1] + ln)
    sharp_turn = math.radians(180.0 - max(0.0, min(180.0, rules.corner_max_angle)))
    n = len(segs)
    for i in range(n):
        # corner at the start of segment i (between i-1 and i)
        prev, cur = segs[i - 1], segs[i]
        if (i - 1) % n in finished or i in finished:
            pass
        elif lens[(i - 1) % n] >= rules.min_feature and lens[i] >= rules.min_feature and n > 1:
            turn = abs(_turn(seg_tangent(prev, 1.0), seg_tangent(cur, 0.0)))
            if turn >= sharp_turn - 1e-9 and turn > math.radians(1.0):
                p = cur.start
                angle = 180.0 - math.degrees(turn)
                corners.append(
                    Candidate(
                        (cum[i] / L) % 1.0,
                        p,
                        "corner",
                        (
                            round(side_score(p), 3),
                            round(fin_score(p), 3),
                            round(angle, 1),
                            -min(lens[(i - 1) % n], lens[i]),
                        ),
                    )
                )
        if isinstance(cur, Line) and i not in finished:
            m = rules.edge_margin
            if lens[i] >= max(rules.min_feature, 2 * m + 1e-6):
                d = min(max(rules.straight_pos * lens[i], m), lens[i] - m)
                u = d / lens[i]
                p = (
                    cur.start[0] + (cur.end[0] - cur.start[0]) * u,
                    cur.start[1] + (cur.end[1] - cur.start[1]) * u,
                )
                straights.append(
                    Candidate(
                        ((cum[i] + d) / L) % 1.0,
                        p,
                        "straight",
                        (round(side_score(p), 3), round(fin_score(p), 3), -lens[i]),
                    )
                )
    corners.sort(key=lambda k: k.rank)
    straights.sort(key=lambda k: k.rank)
    prefer = rules.prefer
    if prefer == "corner" or (prefer == "auto" and corners):
        out = corners + straights
    else:
        out = straights + corners
    from ..geom.contour import point_at

    fb = []
    for k in range(8):
        f = k / 8.0
        p = point_at(c, f * L)
        si = next((j for j in range(n) if cum[j + 1] >= f * L), n - 1)
        if si in finished:
            continue
        fb.append(Candidate(f, p, "fallback", (round(side_score(p), 3), round(fin_score(p), 3), k)))
    fb.sort(key=lambda k: k.rank)
    seen: list[float] = []
    res = []
    for cand in out + fb:
        if any(abs(cand.frac - s) < 1e-6 or abs(abs(cand.frac - s) - 1.0) < 1e-6 for s in seen):
            continue
        seen.append(cand.frac)
        res.append(cand)
    return res


def _depths(doc: Document) -> dict:
    from .manual import contour_depths

    return contour_depths(doc)


def place_leads(
    doc: Document,
    rules: Optional[LeadRules] = None,
    parts: Optional[Iterable[Part]] = None,
    only_with_lead: bool = True,
) -> int:
    """Set the start of every closed contour (of ``parts``, default all) to its best candidate.
    Contours without a lead-in are left alone when ``only_with_lead``.  Returns the number of
    contours whose start changed."""
    from .manual import contour_craft

    rules = rules or LeadRules()
    if rules.prefer == "keep":
        return 0
    depths = _depths(doc)
    want = None if parts is None else {id(p) for p in parts}
    n = 0
    for pi, part in enumerate(doc.parts):
        if want is not None and id(part) not in want:
            continue
        for ci, c in enumerate(part.contours):
            if not c.closed or not c.segments:
                continue
            hole = depths.get((pi, ci), 1) % 2 == 0
            if (hole and not rules.holes) or (not hole and not rules.outers):
                continue
            cr = contour_craft(part, ci)
            if only_with_lead and (cr.lead_in.kind == 0 or cr.lead_in.length < 0.01):
                continue
            cands = candidates(c, rules)
            if not cands:
                continue
            f = cands[0].frac
            if cr.start_param is not None and abs(float(cr.start_param) - f) < 1e-9:
                continue
            contour_craft(part, ci, create=True).start_param = f
            n += 1
    return n


__all__ = ["LeadRules", "Candidate", "candidates", "place_leads", "PREFER", "SIDES"]
