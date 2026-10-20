"""From BOM parts to DXF files, order lines and sheet groups (Qt-free).

:func:`plan_part` decides and prepares one part: machine filter, quantity, body details, face
choice, flattening (tessellated edges only when an edge needs them), thickness (two measured
values and the one in the material name), material mapping and the DXF file.  :func:`run`
does it for many parts with at most two parts in flight (the client's gate keeps the
requests at two as well), a progress callback and a cancel flag.  :func:`order_lines` turns the
ready parts into :class:`mlaser.services.orders.OrderLine` s and :func:`add_to_book` puts them in
the Orders book, replacing the lines an earlier run made for the same parts.
:func:`group_summaries` sums the parts per material x thickness with an area estimate and a
suggested sheet count.
"""
from __future__ import annotations

import hashlib
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import geometry as G
from .bom import DEFAULT_PROCESS_PROPS, BomPart, MachineConfig, is_sheet, machine_match, quantity_to_cut
from .client import OnshapeClient, OnshapeError
from .dxfout import safe_name, write_dxf
from .materials import MaterialMap, label_thickness, non_metal

READY = "ready"
NOT_MACHINE = "not this machine"
NOTHING = "nothing to cut"
NO_FLAT = "no flat face"
NOT_SHEET = "not sheet"
DXF_ERROR = "DXF error"
NO_MATERIAL = "map the material"
NON_METAL = "not for a fiber laser"
API_ERROR = "Onshape error"
CANCELLED = "cancelled"
PENDING = "not prepared"

ORDERABLE = (READY,)


@dataclass
class Options:
    out_dir: Path
    machine: MachineConfig = field(default_factory=MachineConfig)
    process_props: list[str] = field(default_factory=lambda: list(DEFAULT_PROCESS_PROPS))
    material_map: MaterialMap = field(default_factory=MaterialMap)
    tech_names: list[str] = field(default_factory=list)
    face_overrides: dict[str, str] = field(default_factory=dict)
    made: dict[str, int] = field(default_factory=dict)
    thickness_tol_mm: float = 0.1
    cross_check_volume: bool = True


@dataclass
class PartPlan:
    part: BomPart
    status: str = PENDING
    reason: str = ""
    to_cut: int = 0
    made: int = 0
    t_plane: Optional[float] = None
    t_volume: Optional[float] = None
    t_label: Optional[float] = None
    thickness: Optional[float] = None
    part_material: str = ""          # BTPartMaterial name
    label: str = ""                  # the material name used (BOM Material, else the part material)
    sheet_material: str = ""
    material_how: str = ""
    dxf: str = ""
    face_id: str = ""
    face_label: str = ""
    face_reason: str = ""
    faces: list[tuple[str, str]] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0
    net_area: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def orderable(self) -> bool:
        return self.status in ORDERABLE and self.to_cut > 0 and bool(self.dxf)


def _material_label(part: BomPart, part_material: str) -> str:
    return part.material or part_material


def resolve_material(plan: PartPlan, opts: Options) -> None:
    """Sheet material and status from the material map (also after the user maps a name)."""
    label = plan.label
    mat, how = opts.material_map.resolve(label, opts.tech_names)
    plan.sheet_material, plan.material_how = mat, how
    if plan.status in (READY, NO_MATERIAL, NON_METAL):
        if not mat and non_metal(label):
            plan.status = NON_METAL
        elif not mat:
            plan.status = NO_MATERIAL
        else:
            plan.status = READY


def thickness_notes(plan: PartPlan, tol: float) -> None:
    tp, tv, tl = plan.t_plane, plan.t_volume, plan.t_label
    plan.thickness = tp if tp else (tv if tv else tl)
    if tp and tv and abs(tp - tv) > max(tol, 0.02 * tp):
        plan.notes.append(f"volume / face area gives {tv:.2f} mm, the faces {tp:.2f} mm "
                          "(pockets or bosses?): the face distance is used")
    if plan.thickness and tl and abs(plan.thickness - tl) > tol:
        plan.notes.append(f"the material name says {tl:.3g} mm, the part measures {plan.thickness:.3g} mm")


def dxf_path(opts: Options, part: BomPart) -> Path:
    folder = opts.out_dir / safe_name(part.document or "onshape", "onshape")
    stem = safe_name(part.label)
    cfg = part.source.get("configuration") or ""
    if cfg:
        stem += "_" + hashlib.sha1(cfg.encode()).hexdigest()[:6]
    return folder / f"{stem}.dxf"


def plan_part(client: Optional[OnshapeClient], part: BomPart, opts: Options,
              cancelled: Callable[[], bool] = lambda: False) -> PartPlan:
    plan = PartPlan(part)
    plan.made = int(opts.made.get(part.key, 0) or 0)
    ok, why = machine_match(part, opts.machine, opts.process_props)
    plan.reason = why
    if not ok:
        plan.status = NOT_MACHINE
        return plan
    plan.to_cut = quantity_to_cut(part.quantity, opts.machine, plan.made)
    if plan.to_cut <= 0:
        plan.status = NOTHING
        return plan
    if client is None or cancelled():
        plan.status = CANCELLED if cancelled() else PENDING
        return plan
    src = part.source
    try:
        mv = client.microversion(src["did"], src["wvm"], src["wvmid"])
        body = G.find_body(client.body_details(src, mv), src["pid"])
        plan.part_material = body.material
        plan.label = _material_label(part, body.material)
        plan.t_label = label_thickness(plan.label)
        plan.faces = G.face_list(body) if body.planar else []
        choice = G.choose_face(body, opts.face_overrides.get(part.key, ""))
        flat = G.flatten(body, choice)
        if flat.need_tessellation:
            tess = G.tessellation_map(client.tessellated_edges(src, flat.need_tessellation, mv))
            flat = G.flatten(body, choice, tess)
            if flat.need_tessellation:
                raise G.GeometryError(f"{len(flat.need_tessellation)} edge(s) could not be drawn")
        if not all(lp.closed for lp in flat.loops):
            raise G.GeometryError("; ".join(flat.notes) or "a loop is open")
        plan.face_id, plan.face_label, plan.face_reason = flat.face_id, flat.label, flat.reason
        plan.notes.extend(flat.notes)
        plan.width, plan.height, plan.net_area = flat.width, flat.height, flat.net_area
        plan.t_plane = G.plane_thickness(body)
        if opts.cross_check_volume or plan.t_plane is None:
            try:
                plan.t_volume = G.volume_thickness(body, G.mass_volume(client.mass_properties(src, mv), src["pid"]))
            except OnshapeError as exc:
                plan.notes.append(f"mass properties: {exc}")
        thickness_notes(plan, opts.thickness_tol_mm)
        if opts.machine.mode == "sheet" and not is_sheet(flat.width, flat.height, plan.thickness, opts.machine):
            plan.status = NOT_SHEET
            plan.reason = f"not a plate ({flat.width:.0f} x {flat.height:.0f} x {plan.thickness or 0:.1f} mm)"
            return plan
        out = dxf_path(opts, part)
        write_dxf(flat, out)
        plan.dxf = str(out)
        plan.status = READY
        resolve_material(plan, opts)
    except G.NoFlatFace as exc:
        plan.status, plan.reason = NO_FLAT, str(exc)
    except G.GeometryError as exc:
        plan.status, plan.reason = DXF_ERROR, str(exc)
    except OnshapeError as exc:
        plan.status, plan.reason = API_ERROR, str(exc)
    return plan


def run(client: Optional[OnshapeClient], parts: Iterable[BomPart], opts: Options,
        progress: Callable[[int, int, str], None] = lambda i, n, msg: None,
        cancel: Optional[threading.Event] = None, workers: int = 2) -> list[PartPlan]:
    parts = list(parts)
    cancel = cancel or threading.Event()
    done = [0]
    lock = threading.Lock()

    def one(p: BomPart) -> PartPlan:
        if cancel.is_set():
            return PartPlan(p, status=CANCELLED)
        r = plan_part(client, p, opts, cancel.is_set)
        with lock:
            done[0] += 1
            progress(done[0], len(parts), f"{p.label}: {r.status}")
        return r

    with ThreadPoolExecutor(max_workers=max(1, min(2, workers))) as ex:
        return list(ex.map(one, parts))


# ============================================================================== orders
def order_lines(plans: Iterable[PartPlan], customer: str = "", order: str = "") -> list:
    from mlaser.services.orders import OrderLine
    out = []
    for p in plans:
        if not p.orderable:
            continue
        pt = p.part
        notes = "; ".join(x for x in (f"P/N {pt.part_number}" if pt.part_number else "",
                                      pt.name if pt.part_number else "",
                                      f"configuration {pt.source.get('configuration')}" if pt.source.get("configuration") else "")
                          if x)
        out.append(OrderLine(file=p.dxf, quantity=int(p.to_cut), material=p.sheet_material,
                             thickness_mm=round(float(p.thickness or 0.0), 2),
                             customer=customer or pt.document, order=order or pt.assembly, notes=notes,
                             source=f"onshape:{pt.key}"))
    return out


def add_to_book(book: Any, lines: list) -> tuple[int, int]:
    """Add ``lines`` to an OrderBook, replacing the lines an earlier run made for the same parts.
    (added, replaced)."""
    srcs = {ln.source for ln in lines}
    old = [x.id for x in getattr(book, "lines", []) if getattr(x, "source", "") in srcs]
    n_old = book.remove(old) if old else 0
    book.add(lines)
    return len(lines), n_old


# ============================================================================== groups
@dataclass
class GroupSummary:
    key: tuple[str, float]
    material: str
    thickness: float
    parts: int
    quantity: int
    net_area_mm2: float
    box_area_mm2: float
    sheets: int
    too_big: list[str]

    @property
    def title(self) -> str:
        return f"{self.material or '?'} {self.thickness:g} mm"


def group_summaries(plans: Iterable[PartPlan], sheet_w: float, sheet_h: float, utilisation: float = 0.7,
                    gap_mm: float = 5.0) -> list[GroupSummary]:
    """Ready parts per material x thickness.  The sheet estimate puts each part's box (plus
    the gap) on sheets used to ``utilisation``: a first guess, Nest decides."""
    from mlaser.services.orders import material_key
    groups: dict[tuple[str, float], list[PartPlan]] = {}
    for p in plans:
        if p.orderable:
            groups.setdefault(material_key(p.sheet_material, round(float(p.thickness or 0.0), 2)), []).append(p)
    out = []
    sheet_area = max(1.0, sheet_w * sheet_h) * max(0.05, min(1.0, utilisation))
    for key, ps in groups.items():
        box = sum((p.width + gap_mm) * (p.height + gap_mm) * p.to_cut for p in ps)
        big = [p.part.label for p in ps
               if not ((p.width <= sheet_w and p.height <= sheet_h) or (p.width <= sheet_h and p.height <= sheet_w))]
        out.append(GroupSummary(key, ps[0].sheet_material, round(float(ps[0].thickness or 0.0), 2), len(ps),
                                sum(p.to_cut for p in ps), sum(p.net_area * p.to_cut for p in ps), box,
                                max(1, math.ceil(box / sheet_area)), big))
    out.sort(key=lambda g: (g.material.casefold(), g.thickness))
    return out
