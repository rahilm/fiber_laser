"""The ribbon's CAM operations as document operations (Qt-free).

Moved out of ``mlaser.ui.ext_cam`` (Wave 2, "business logic out of the UI"): arrays, fills,
cleanup, corner features, cool points, common edge and fly cut.  Each function takes the
document plus a parameter object and edits the document in place (run it inside
``DocumentService.edit`` for undo); the plugin keeps only the dialogs.  The algorithms are in
:mod:`mlaser.cam`.
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Callable, Optional

from mlaser.model import Contour, Document, Part

from .document import contour_targets as targets  # raster parts excluded


class OpError(ValueError):
    """An operation cannot run on this selection; ``key`` is the lang key of the message."""

    def __init__(self, key: str, text: str = "") -> None:
        super().__init__(text or key)
        self.key = key


def all_contours(parts) -> list[Contour]:
    return [c for p in parts for c in p.contours if c.segments]


# --------------------------------------------------------------------------- parameters
@dataclasses.dataclass
class CircularArrayParams:
    count: int = 6
    center_x: float = 0.0
    center_y: float = 0.0
    total_angle_deg: float = 360.0
    rotate_items: bool = True
    use_bbox_center: bool = True  # centre = selection bbox centre + (center_x, center_y)


@dataclasses.dataclass
class SheetFillParams:
    width: float = 3000.0
    height: float = 1500.0
    gap: float = 5.0
    margin: float = 5.0


@dataclasses.dataclass
class FilletParams:
    radius: float = 1.0  # pd600 GRP.ArcRoundRadius [0.1, 10]
    min_angle: float = 0.0  # pd601 GRP.ArcRoundMinAngle_deg
    max_angle: float = 90.0  # pd602 GRP.ArcRoundMaxAngle_deg


@dataclasses.dataclass
class UnloadParams:
    radius: float = 0.5  # pd603 GRP.UnloadAngleRadius [0.1, 10]


@dataclasses.dataclass
class AlphaParams:
    max_angle: float = 90.0  # pd607 GRP.AlphaMaxAngle_deg [1, 170]
    min_edge: float = 3.0  # pd608 GRP.AlphaMinEdgeLen [1, 50]
    lead: float = 8.0  # pd609 GRP.AlphaLen [0.5, 10]
    mode: int = 0  # pd614 GRP.AlphaType 0 auto, 1 outside, 2 inner, 3 both


@dataclasses.dataclass
class CoolParams:
    lead_point: bool = False  # pd604 Cool Lead Position
    sharp: bool = True  # pd605 Cool Sharp Position
    max_angle: float = 90.0  # pd606 Max Sharp Angle


@dataclasses.dataclass
class RectShareParams:
    kind: int = 0  # A250707_0 type (A250707_7 / _8 / _9)
    overcut_on: bool = False  # A250707_1
    overcut: float = 0.0  # A250707_2


# --------------------------------------------------------------------------- arrays (mf79)
def rect_array(doc: Document, prm) -> int:
    """mf79 Array (GraphParam array block, :func:`mlaser.cam.array.apply_rect_array`)."""
    from mlaser.cam.array import apply_rect_array

    return apply_rect_array(doc, prm)


def circular_array(doc: Document, prm: CircularArrayParams) -> int:
    from mlaser.cam.array import circular_array as _circ, parts_bbox

    parts = targets(doc)
    cx, cy = prm.center_x, prm.center_y
    if prm.use_bbox_center:
        x0, y0, x1, y1 = parts_bbox(parts)
        cx, cy = cx + (x0 + x1) / 2, cy + (y0 + y1) / 2
    new = _circ(parts, prm.count, (cx, cy), prm.total_angle_deg, prm.rotate_items)
    doc.parts.extend(new)
    return len(new)


def sheet_fill(doc: Document, prm: SheetFillParams) -> int:
    from mlaser.cam.array import fill_sheet_array

    parts = targets(doc)
    new = fill_sheet_array(parts, (0.0, 0.0, prm.width, prm.height), prm.gap, prm.margin)
    if not new:
        return 0
    doc.parts = [p for p in doc.parts if p not in parts] + new
    return len(new)


# --------------------------------------------------------------------------- fill (mf100_1, A250707_10)
def fill(doc: Document, prm, linear: bool) -> int:
    """Fill circles (mf100_1) or line fill (A250707_10) inside the closed target contours.
    Raises :class:`OpError` ("A251103_1") when a target contour is open."""
    from mlaser.cam.array import fill_circles, linear_fill
    from mlaser.cam.ordering import build_tree

    parts = targets(doc)
    if any(not c.closed for c in all_contours(parts)):
        raise OpError("A251103_1")
    conts = all_contours(parts)
    nodes = build_tree(conts, list(range(len(conts))))
    new: list[Part] = []
    for n in nodes:
        if n.depth % 2 != 1:
            continue
        holes = [k.contour for k in n.children if k.contour.closed]
        layer = parts[0].craft.layer if parts else None
        fn = linear_fill if linear else fill_circles
        new += fn(n.contour, holes, prm, layer=layer)
    if linear and not prm.keep_original:
        doc.parts = [p for p in doc.parts if p not in parts]
    doc.parts.extend(new)
    return len(new)


# --------------------------------------------------------------------------- cleanup (mf470)
def cleanup(doc: Document, prm) -> dict:
    from mlaser.cam.cleanup import cleanup_document

    return cleanup_document(doc, prm, only_selected=any(p.selected for p in doc.parts))


def dedup(doc: Document, overlap_gate: float) -> int:
    """mf471 Clear Duplicate Lines (0x100c0a30, OverlapGate)."""
    from mlaser.cam.cleanup import remove_duplicate_contours

    removed = 0
    from .document import is_raster
    allc = [(p, c) for p in doc.parts if not is_raster(p) for c in p.contours]
    keep = {id(c) for c in remove_duplicate_contours([c for _, c in allc], overlap_gate)}
    for p in doc.parts:
        if is_raster(p):
            continue
        n0 = len(p.contours)
        p.contours = [c for c in p.contours if id(c) in keep]
        removed += n0 - len(p.contours)
    doc.parts = [p for p in doc.parts if p.contours]
    return removed


def join(doc: Document, connect_gate: float, mode: int) -> int:
    """mf476 Merge connected lines (0x100c0300, ConnectGate + MegerConnectGraphType)."""
    from mlaser.cam.cleanup import merge_connected

    parts = targets(doc)
    if not parts:
        return 0
    craft = copy.deepcopy(parts[0].craft)
    res = merge_connected(all_contours(parts), connect_gate, mode or 1)
    doc.parts = [p for p in doc.parts if p not in parts] + [
        Part(contours=[c], craft=copy.deepcopy(craft)) for c in res
    ]
    return len(res)


def smooth(doc: Document, accuracy: float) -> int:
    """mf474 / mf213 Curve smoothness: CircleFit-style arc fitting at SmoothAccuracy."""
    from mlaser.cam.cleanup import smooth_contour

    n = 0
    for p in targets(doc):
        new = [smooth_contour(c, accuracy) for c in p.contours]
        n += sum(len(a.segments) - len(b.segments) for a, b in zip(p.contours, new))
        p.contours = new
    return n


# --------------------------------------------------------------------------- corners
def _reset_craft_after_geometry(p: Part) -> None:
    """After a corner edit the original recomputes kerf child / micro-joints (0x10074e00,
    0x1006c930); the rebuild recomputes kerf on the fly and drops stale joint positions."""
    p.craft.micro_joints = []
    p.craft.cool_points = []


def per_contour(doc: Document, fn: Callable[[Contour, Part], Contour], closed_only: bool = True) -> int:
    """Replace every (closed) contour of the target parts by ``fn(contour, part)``; returns the
    number of contours whose segment count changed."""
    n = 0
    for p in targets(doc):
        new = []
        for c in p.contours:
            if closed_only and not c.closed:
                new.append(c)
                continue
            r = fn(c, p)
            n += int(r is not c and len(r.segments) != len(c.segments))
            new.append(r)
        if new != p.contours:
            p.contours = new
            _reset_craft_after_geometry(p)
    return n


def fillet(doc: Document, q: FilletParams) -> int:
    from mlaser.cam.corners import fillet as _fillet

    return per_contour(doc, lambda c, p: _fillet(c, q.radius, q.min_angle, q.max_angle))


def unload(doc: Document, q: UnloadParams) -> int:
    from mlaser.cam.corners import unload_loop

    return per_contour(doc, lambda c, p: unload_loop(c, q.radius))


def _depths(doc: Document):
    from mlaser.cam.ordering import build_tree

    keys, conts = [], []
    for pi, p in enumerate(doc.parts):
        for ci, c in enumerate(p.contours):
            if c.segments:
                keys.append((pi, ci))
                conts.append(c)
    return {n.key: n.depth for n in build_tree(conts, keys)}


def alpha(doc: Document, q: AlphaParams) -> int:
    """mf485 α corners; the replaced contour is kept in ``extra['alpha_source']`` (mf403)."""
    from mlaser.cam.corners import alpha_corners

    depth = _depths(doc)
    tg = targets(doc)
    n = 0
    for pi, p in enumerate(doc.parts):
        if p not in tg:
            continue
        new = []
        for ci, c in enumerate(p.contours):
            if not c.closed or not c.segments:
                new.append(c)
                continue
            r = alpha_corners(c, q.max_angle, q.min_edge, q.lead, q.mode, depth.get((pi, ci), 1))
            n += int(len(r.segments) != len(c.segments))
            if len(r.segments) != len(c.segments):
                r.extra = dict(r.extra)
                r.extra["alpha_source"] = c
            new.append(r)
        p.contours = new
        _reset_craft_after_geometry(p)
    return n


def alpha_clear(doc: Document) -> int:
    """mf403: restore contours saved by the alpha command."""
    n = 0
    for p in targets(doc):
        new = []
        for c in p.contours:
            src = c.extra.get("alpha_source") if isinstance(c.extra, dict) else None
            new.append(src if src is not None else c)
            n += src is not None
        p.contours = new
    return n


def cool_auto(doc: Document, q: CoolParams) -> int:
    from mlaser.cam.corners import auto_cool_points

    n = 0
    for p in targets(doc):
        pts: list = []
        for c in p.contours[:1]:  # Craft.cool_points applies to the part's contours
            if c.closed:
                pts = auto_cool_points(c, q.max_angle, q.sharp, q.lead_point)
        p.craft.cool_points = list(pts)
        ov = getattr(p.contours[0], "craft", None) if p.contours else None
        if ov is not None and ov is not p.craft:   # the contour's own craft is the one cut
            ov.cool_points = list(pts)
        n += len(pts)
    return n


def cool_clear(doc: Document) -> None:
    for p in targets(doc):
        p.craft.cool_points = []
        for c in p.contours:
            ov = getattr(c, "craft", None)
            if ov is not None and ov is not p.craft:
                ov.cool_points = []


# --------------------------------------------------------------------------- common edge
def share_edge(doc: Document, c_type: bool) -> tuple[float, float]:
    """mf250 common edge; returns (cut length before, after) in mm."""
    from mlaser.cam.commonedge import cut_length, share_edge as _share

    parts = targets(doc)
    before = cut_length(parts)
    new = _share(parts, c_type)
    doc.parts = [p for p in doc.parts if p not in parts] + new
    return before, cut_length(new)


def rect_share(doc: Document, q: RectShareParams) -> int:
    """A250707_11 rectangle common edge.  Raises :class:`OpError` when the selection is not a
    complete grid of equal rectangles."""
    from mlaser.cam.commonedge import detect_rect_grid, rect_share_edge

    parts = targets(doc)
    grid = detect_rect_grid(parts)
    if grid is None:
        raise OpError("A250707_11", "selection is not a complete grid of equal rectangles")
    paths = rect_share_edge(grid, q.kind, q.overcut if q.overcut_on else 0.0)
    craft = copy.deepcopy(parts[0].craft)
    craft.kerf_comp = -1
    doc.parts = [p for p in doc.parts if p not in parts] + [
        Part(contours=[c], craft=copy.deepcopy(craft), name="share edge") for c in paths
    ]
    return len(paths)


# --------------------------------------------------------------------------- fly cut (mf112)
def flycut(doc: Document, kind: int, prm) -> Optional[Part]:
    """mf112_1 / mf112_2 scan cutting (:func:`mlaser.cam.flycut.apply_fly_cut`)."""
    from mlaser.cam import flycut as F

    sel = any(p.selected for p in doc.parts)
    return F.apply_fly_cut(doc, kind, prm, selected_only=sel)


def flycut_has_circles(doc: Document) -> bool:
    from mlaser.cam import flycut as F

    return any(F.detect_circle(c) for c in all_contours(targets(doc)))
