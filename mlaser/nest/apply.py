"""Apply a nest result back to a Document, plus statistics and job helpers.

The original copies a result sheet to the main (cutting) view with
``nset_UI_Result2MainView`` (see docs/re/nest.md); here :func:`apply_result` builds a
Document whose parts are transformed copies of the nest parts. Each copy carries
``Part.extra['nest'] = {part_id, sheet_id, layout, x, y, angle, mirror}``.
"""
from __future__ import annotations

import copy
from typing import Optional

from shapely.geometry import box

from ..model import Contour, Craft, Document, Line, Part
from .model import NestJob, NestPart, NestResult, NestSheet, SheetLayout
from .shapes import parts_geometry, sheet_geometry, transform_part


def rect_part(x0: float, y0: float, x1: float, y1: float, layer: int = 0, name: str = "") -> Part:
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    c = Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)
    return Part([c], Craft(layer=layer), name=name)


def layout_parts(job: NestJob, layout: SheetLayout, offset=(0.0, 0.0)) -> list[Part]:
    out = []
    for k, pl in enumerate(layout.placements):
        np_ = job.part(pl.part_id)
        for src in np_.parts:
            q = transform_part(src, pl)
            if offset != (0.0, 0.0):
                from .model import Placement
                q = transform_part(q, Placement(-1, offset[0], offset[1]))
            q.name = np_.name
            q.extra = dict(q.extra)
            q.extra["nest"] = {"part_id": pl.part_id, "sheet_id": layout.sheet_id, "index": k,
                               "x": pl.x, "y": pl.y, "angle": pl.angle, "mirror": pl.mirror}
            out.append(q)
    return out


def sheet_part(sheet: NestSheet, offset=(0.0, 0.0)) -> Part:
    """Sheet outline as a Part on layer 0 (the original's back layer, not cut)."""
    if sheet.outline:
        from .model import Placement
        parts = [transform_part(p, Placement(-1, offset[0], offset[1])) for p in sheet.outline]
        cs = [c for p in parts for c in p.contours]
        q = Part(cs, Craft(layer=0), name=sheet.name)
    else:
        x0, y0 = sheet.origin[0] + offset[0], sheet.origin[1] + offset[1]
        q = rect_part(x0, y0, x0 + sheet.width, y0 + sheet.height, 0, sheet.name)
    q.extra["nest_sheet"] = sheet.id
    return q


def apply_result(job: NestJob, result: Optional[NestResult] = None, layout_index: Optional[int] = None,
                 doc: Optional[Document] = None, include_sheet: bool = False,
                 gap: float = 50.0, share_edges: Optional[bool] = None) -> Document:
    """Return ``doc`` (or a new Document) with the placed parts appended.

    ``layout_index`` selects one sheet layout; ``None`` lays all of them out side by side
    (each next sheet shifted by its width + ``gap`` along X). With common-line nesting
    (``share_edges``, default ``job.params.common_line``) the placed parts of each sheet go
    through :func:`mlaser.cam.commonedge.share_edge` (CShareEdge port) with the minimum
    shared length ``NestParams.share_edge_min_len``."""
    if share_edges is None:
        share_edges = job.params.common_line
    result = result or job.result
    if result is None:
        raise ValueError("job has no nest result")
    doc = doc if doc is not None else Document()
    layouts = list(enumerate(result.layouts))
    if layout_index is not None:
        layouts = [layouts[layout_index]]
    dx = 0.0
    for li, lay in layouts:
        sh = job.sheet(lay.sheet_id)
        full, _ = sheet_geometry(sh, 0.0)
        x0, _, x1, _ = full.bounds
        off = (dx, 0.0) if layout_index is None else (0.0, 0.0)
        if include_sheet:
            doc.parts.append(sheet_part(sh, off))
        placed = layout_parts(job, lay, off)
        if share_edges and placed:
            try:
                from ..cam.commonedge import share_edge
                placed = share_edge(placed, min_len=max(job.params.share_edge_min_len, 0.1))
            except ImportError:
                pass
        doc.parts.extend(placed)
        dx += (x1 - x0) + gap
    doc.extra.pop("order", None)
    return doc


def job_from_document(doc: Document, sheet: Optional[NestSheet] = None, quantity: int = 1,
                      selected_only: bool = False) -> NestJob:
    """One nest part per Part of the document (the original's "import part from main view")."""
    job = NestJob()
    for p in doc.parts:
        if selected_only and not p.selected:
            continue
        if not p.contours:
            continue
        job.add_part(NestPart([copy.deepcopy(p)], name=p.name, quantity=quantity))
    job.add_sheet(sheet or NestSheet())
    return job


def stats(job: NestJob, result: Optional[NestResult] = None) -> dict:
    """Utilisation figures per layout: net part area / sheet area, and over the used extent."""
    result = result or job.result
    out = {"sheets": [], "placed": dict(result.placed), "unplaced": dict(result.unplaced),
           "elapsed_s": result.elapsed_s}
    tot_part = tot_sheet = 0.0
    for lay in result.layouts:
        sh = job.sheet(lay.sheet_id)
        full, _ = sheet_geometry(sh, 0.0)
        area = sum(parts_geometry(job.part(pl.part_id).parts).area for pl in lay.placements)
        x0, y0, x1, y1 = full.bounds
        used = full.area
        if lay.remnant:
            used -= box(*lay.remnant).intersection(full).area
        out["sheets"].append({"sheet_id": sh.id, "name": sh.name, "parts": len(lay.placements),
                              "part_area": area, "sheet_area": full.area,
                              "utilisation": area / full.area if full.area else 0.0,
                              "used_utilisation": area / used if used > 0 else 0.0,
                              "remnant": lay.remnant})
        tot_part += area
        tot_sheet += full.area
    out["utilisation"] = tot_part / tot_sheet if tot_sheet else 0.0
    return out
