"""Onshape bills of materials -> parts, and which of them this machine cuts (Qt-free).

:func:`parse_bom` reads a flattened BOM (``BTBillOfMaterialsInfo``: ``headers`` and ``rows`` with
``headerIdToValue`` and ``itemSource``).  Columns are found by their header name or property
name, case-insensitively: Name, Part number, Quantity, Material, Description, and the process
properties the user names (default ``Process``, ``Process 2``, ``Pre Process``).  A configured
part keeps its row's configuration; standard content (screws from Onshape's library) and rows
without a part id are skipped.

:class:`MachineConfig` says which parts are for this laser: the values of the process
properties that mean "this machine" (several allowed, case-insensitive), which property slots
count, and whether a part whose laser step comes after another step is included (Onshape does
not know what has been made, so such a part is either always or never included).  Without a
process property, ``mode = "sheet"`` takes every part that measures as a plate.

:func:`quantity_to_cut` = BOM quantity × robots + spares − already made (never below 0).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

DEFAULT_PROCESS_PROPS = ("Pre Process", "Process", "Process 2")
STANDARD = {
    "name": ("name", "part name"),
    "part_number": ("part number", "partnumber", "part no", "p/n", "pn"),
    "quantity": ("quantity", "qty"),
    "material": ("material",),
    "description": ("description",),
}


def _norm(s: Any) -> str:
    return " ".join(str(s or "").replace("_", " ").split()).casefold()


def cell_text(v: Any) -> str:
    """A BOM cell as text (a material cell is an object with ``displayName``)."""
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("displayName", "name", "value", "id"):
            if v.get(k):
                return str(v[k])
        return ""
    if isinstance(v, list):
        return ", ".join(cell_text(x) for x in v if cell_text(x))
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _qty(v: Any) -> int:
    try:
        q = float(cell_text(v).replace(",", "."))
    except ValueError:
        return 0
    return max(0, int(round(q)))


@dataclass
class BomPart:
    key: str                                     # did/eid/pid/configuration
    name: str
    part_number: str = ""
    quantity: int = 1
    material: str = ""                           # the Material property (BOM)
    description: str = ""
    processes: dict[str, str] = field(default_factory=dict)    # property name -> value
    source: dict = field(default_factory=dict)   # did, wvm, wvmid, eid, pid, configuration, link
    assembly: str = ""
    document: str = ""

    @property
    def label(self) -> str:
        return self.part_number or self.name


@dataclass
class BomResult:
    parts: list[BomPart]
    skipped: list[str]
    columns: dict[str, str]                      # field / property -> header name found
    process_values: dict[str, list[str]]         # property -> distinct values seen
    missing: list[str]                           # configured properties with no column


def parse_bom(bom: dict, process_props: Iterable[str] = DEFAULT_PROCESS_PROPS, assembly_did: str = "",
              assembly: str = "", document: str = "") -> BomResult:
    headers = [h for h in (bom or {}).get("headers") or [] if isinstance(h, dict)]
    by_name: dict[str, str] = {}
    for h in headers:
        for nm in (h.get("name"), h.get("propertyName")):
            if nm and _norm(nm) not in by_name:
                by_name[_norm(nm)] = str(h.get("id"))
        if h.get("id"):
            by_name.setdefault("id:" + _norm(h["id"]), str(h["id"]))
    header_names = {str(h.get("id")): str(h.get("name") or h.get("propertyName") or h.get("id")) for h in headers}
    cols: dict[str, str] = {}
    for fld, names in STANDARD.items():
        for nm in names:
            if nm in by_name:
                cols[fld] = by_name[nm]
                break
    props = [p for p in process_props if str(p).strip()]
    proc_cols: dict[str, str] = {}
    missing = []
    for p in props:
        hid = by_name.get(_norm(p)) or by_name.get("id:" + _norm(p))
        if hid is None and _norm(p) == "process":                 # "Process 1" is the same slot
            hid = by_name.get("process 1")
        if hid is None:
            missing.append(p)
        else:
            proc_cols[p] = hid
    values: dict[str, list[str]] = {p: [] for p in props}
    parts, skipped = [], []
    for row in (bom or {}).get("rows") or []:
        if not isinstance(row, dict):
            continue
        vals = row.get("headerIdToValue") or {}
        src = row.get("itemSource") or {}
        name = cell_text(vals.get(cols.get("name", ""))) or str(row.get("name") or "")
        if not src.get("partId"):
            skipped.append(f"{name or '?'}: not a part (a sub-assembly or a non-geometric item)")
            continue
        if src.get("isStandardContent"):
            skipped.append(f"{name or '?'}: standard content")
            continue
        procs = {p: cell_text(vals.get(h)) for p, h in proc_cols.items()}
        for p, v in procs.items():
            if v and v not in values[p]:
                values[p].append(v)
        cfg = str(src.get("fullConfiguration") or src.get("configuration") or "")
        did = str(src.get("documentId") or "")
        s = {"did": did, "wvm": str(src.get("wvmType") or "w"), "wvmid": str(src.get("wvmId") or ""),
             "eid": str(src.get("elementId") or ""), "pid": str(src["partId"]), "configuration": cfg,
             "link": assembly_did if assembly_did and did != assembly_did else ""}
        parts.append(BomPart(
            key=f"{did}/{s['eid']}/{s['pid']}/{cfg}", name=name or s["pid"],
            part_number=cell_text(vals.get(cols.get("part_number", ""))),
            quantity=_qty(vals.get(cols.get("quantity", ""))) if "quantity" in cols else 1,
            material=cell_text(vals.get(cols.get("material", ""))),
            description=cell_text(vals.get(cols.get("description", ""))),
            processes=procs, source=s, assembly=assembly, document=document))
    columns = {k: header_names.get(v, v) for k, v in cols.items()}
    columns.update({p: header_names.get(h, h) for p, h in proc_cols.items()})
    return BomResult(parts, skipped, columns, values, missing)


# ============================================================================== machine
@dataclass
class MachineConfig:
    mode: str = "process"                 # process | sheet
    values: list[str] = field(default_factory=lambda: ["Laser"])
    slots: list[str] = field(default_factory=lambda: ["Process", "Process 2"])
    include_after_other: bool = False     # a part whose laser step comes after another process
    robots: int = 1
    spares: int = 0
    sheet_ratio: float = 3.0              # mode "sheet": width >= ratio x thickness
    max_sheet_mm: float = 25.0            # mode "sheet": thicker parts are not sheet

    def matches_value(self, v: str) -> bool:
        want = {_norm(x) for x in self.values if str(x).strip()}
        return _norm(v) in want

    def to_json(self) -> dict:
        return {"mode": self.mode, "values": list(self.values), "slots": list(self.slots),
                "include_after_other": self.include_after_other, "robots": self.robots, "spares": self.spares,
                "sheet_ratio": self.sheet_ratio, "max_sheet_mm": self.max_sheet_mm}

    @classmethod
    def from_json(cls, d: Any) -> "MachineConfig":
        c = cls()
        if not isinstance(d, dict):
            return c
        if d.get("mode") in ("process", "sheet"):
            c.mode = d["mode"]
        for k in ("values", "slots"):
            if isinstance(d.get(k), list):
                setattr(c, k, [str(x) for x in d[k] if str(x).strip()])
        c.include_after_other = bool(d.get("include_after_other", c.include_after_other))
        c.robots = max(1, min(100, int(d.get("robots", c.robots) or 1)))
        c.spares = max(0, min(1000, int(d.get("spares", c.spares) or 0)))
        c.sheet_ratio = float(d.get("sheet_ratio", c.sheet_ratio) or 3.0)
        c.max_sheet_mm = float(d.get("max_sheet_mm", c.max_sheet_mm) or 25.0)
        return c


def machine_match(part: BomPart, cfg: MachineConfig, order: Iterable[str] = DEFAULT_PROCESS_PROPS) -> tuple[bool, str]:
    """(for this machine?, reason).  ``order`` is the process properties in the order the steps
    happen (pre-process first); only the properties in ``cfg.slots`` can select the machine."""
    if cfg.mode == "sheet":
        return True, "sheet mode: decided by the measured shape"
    seq = list(dict.fromkeys(list(order) + [s for s in cfg.slots if s not in order]))
    slots = {_norm(s) for s in cfg.slots}
    for i, prop in enumerate(seq):
        v = part.processes.get(prop, "")
        if _norm(prop) in slots and v and cfg.matches_value(v):
            earlier = [part.processes.get(s, "") for s in seq[:i]]
            earlier = [e for e in earlier if e and not cfg.matches_value(e)]
            if earlier and not cfg.include_after_other:
                return False, (f"comes after {', '.join(earlier)} (left out: Onshape does not know "
                               "whether that step is done)")
            return True, f"{prop} = {v}"
    vals = [f"{k} = {v}" for k, v in part.processes.items() if v]
    return False, ("not this machine (" + "; ".join(vals) + ")") if vals else "no process set"


def quantity_to_cut(bom_qty: int, cfg: MachineConfig, made: int = 0) -> int:
    return max(0, int(bom_qty) * max(1, int(cfg.robots)) + max(0, int(cfg.spares)) - max(0, int(made)))


def is_sheet(width: float, height: float, thickness: Optional[float], cfg: MachineConfig) -> bool:
    if not thickness or thickness <= 0:
        return False
    return thickness <= cfg.max_sheet_mm and min(width, height) >= cfg.sheet_ratio * thickness
