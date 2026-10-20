"""Sheet material check and the "load this sheet" card (Qt-free).

The most expensive mistake on a laser is the wrong sheet or the wrong technology.  Every layer
a technology was applied to names it (``LayerParams.extra["technology"]["key"]``: material,
thickness, gas, nozzle), and every queued sheet says what it is cut from (``QueueEntry.material``
/ ``thickness_mm``).  Before a sheet starts they are compared:

* :func:`check` returns pre-flight issues (:class:`mlaser.machine.preflight.Issue`):
  ``material_mismatch`` (**error**) when a used layer's technology is for another material or
  thickness than the sheet; ``material_unverified`` (warning) for a layer without a technology
  (set by hand) or a sheet without a declared material; ``technology_example`` (warning) for
  example values.
* :class:`MaterialGate` is the start guard (``JobService.add_start_guard``): a process start
  with a mismatch is refused unless the operator overrode exactly that mismatch (same sheet,
  same layers) in the last ``ttl_s`` seconds.  :meth:`MaterialGate.override` writes an audit
  record (``material_override``), so do refusals (``material_check``).
* The sheet of a document is ``doc.extra["sheet"]`` (:func:`set_sheet`): the queue sets it
  when it loads a sheet, the Nest workspace when it copies a sheet to the drawing.  A drawing
  without one (a plain design) is not checked.
* :func:`load_card` is the data of the Run screen's "Load this sheet" card.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from mlaser.machine.audit import audit
from mlaser.machine.preflight import ERROR, WARNING, Issue, used_layers
from mlaser.services.orders import material_key
from mlaser.services.technology import layer_technology_ref

__all__ = ["SheetSpec", "sheet_of", "set_sheet", "check", "MaterialGate", "load_card", "layer_techs",
           "MISMATCH", "same_material", "summarize_orders"]

MISMATCH = "material_mismatch"
THICKNESS_TOL = 0.01


@dataclass(frozen=True)
class SheetSpec:
    material: str
    thickness_mm: float
    source: str = ""                  # queue | nest | order
    entry: str = ""                   # queue entry id
    sheet_no: int = 0                 # 1-based sheet number inside the entry
    size: str = ""
    remnant_id: str = ""

    def label(self) -> str:
        if not self.material and not self.thickness_mm:
            return "(not declared)"
        return f"{self.material or '?'} {self.thickness_mm:g} mm"

    def to_dict(self) -> dict:
        return {"material": self.material, "thickness_mm": self.thickness_mm, "source": self.source,
                "entry": self.entry, "sheet_no": self.sheet_no, "size": self.size, "remnant_id": self.remnant_id}


def sheet_of(doc: Any) -> Optional[SheetSpec]:
    d = (getattr(doc, "extra", None) or {}).get("sheet")
    if not isinstance(d, dict):
        return None
    try:
        return SheetSpec(str(d.get("material", "")), float(d.get("thickness_mm", 0.0) or 0.0),
                         str(d.get("source", "")), str(d.get("entry", "")), int(d.get("sheet_no", 0) or 0),
                         str(d.get("size", "")), str(d.get("remnant_id", "")))
    except (TypeError, ValueError):
        return None


def set_sheet(doc: Any, spec: Optional[SheetSpec]) -> None:
    if spec is None:
        doc.extra.pop("sheet", None)
    else:
        doc.extra["sheet"] = spec.to_dict()


def same_material(a: str, ta: float, b: str, tb: float) -> bool:
    ka, kb = material_key(a, ta), material_key(b, tb)
    return ka[0] == kb[0] and abs(float(ta) - float(tb)) <= THICKNESS_TOL


def layer_techs(doc: Any) -> dict[int, Optional[dict]]:
    """Technology ref (``{"key": {...}, "example": ..}``) of every used layer (None: by hand)."""
    layers = getattr(doc, "layers", None) or {}
    out: dict[int, Optional[dict]] = {}
    for li in sorted(used_layers(doc)):
        lp = layers.get(li)
        if lp is not None and (lp.extra or {}).get("placeholder"):
            continue                         # undefined: the layer_undefined check refuses it
        out[li] = layer_technology_ref(lp) if lp is not None else None
    return out


def check(doc: Any, sheet: Optional[SheetSpec] = None) -> list[Issue]:
    """The material issues of ``doc`` cut from ``sheet`` (default: the document's own)."""
    sheet = sheet if sheet is not None else sheet_of(doc)
    if sheet is None:
        return []
    out: list[Issue] = []
    if not sheet.material and not sheet.thickness_mm:
        out.append(Issue("material_unverified", WARNING,
                         "the sheet has no material or thickness: it is not checked against the technology"))
        return out
    for li, ref in layer_techs(doc).items():
        if ref is None:
            out.append(Issue("material_unverified", WARNING,
                             f"layer {li} has no technology (set by hand): its material cannot be checked "
                             f"against the sheet ({sheet.label()})", (li,)))
            continue
        key = ref.get("key") or {}
        m, t = str(key.get("material", "")), float(key.get("thickness_mm", 0.0) or 0.0)
        if not same_material(m, t, sheet.material, sheet.thickness_mm):
            out.append(Issue(MISMATCH, ERROR,
                             f"layer {li} has the technology for {m} {t:g} mm, but the sheet is "
                             f"{sheet.label()}: load the right sheet or apply the right technology", (li,)))
        elif ref.get("example"):
            out.append(Issue("technology_example", WARNING,
                             f"layer {li} uses example values ({m} {t:g} mm): verify them on this machine", (li,)))
    return out


def signature(doc: Any, sheet: Optional[SheetSpec], issues: list[Issue]) -> str:
    """What an override is for: the sheet (queue entry and sheet number included) and the
    mismatching layers' technologies."""
    s = sheet or SheetSpec("", 0.0)
    bits = [s.source, s.entry, str(s.sheet_no), material_key(s.material, s.thickness_mm)[0],
            f"{s.thickness_mm:.2f}"]
    bits += sorted(i.message for i in issues if i.code == MISMATCH)
    return "|".join(bits)


class MaterialGate:
    """The start guard for the material check (see the module docstring)."""

    def __init__(self, document: Callable[[], Any], clock: Callable[[], float] = time.time,
                 ttl_s: float = 900.0, user: Callable[[], str] = lambda: "") -> None:
        self.document = document
        self.clock = clock
        self.ttl_s = ttl_s
        self.user = user
        self._overrides: dict[str, float] = {}
        self.last_refusal = ""

    def issues(self, doc: Any = None) -> list[Issue]:
        return check(doc if doc is not None else self.document())

    def mismatches(self, doc: Any = None) -> list[Issue]:
        return [i for i in self.issues(doc) if i.code == MISMATCH]

    def overridden(self, doc: Any = None) -> bool:
        doc = doc if doc is not None else self.document()
        mm = self.mismatches(doc)
        if not mm:
            return False
        t = self._overrides.get(signature(doc, sheet_of(doc), mm))
        return t is not None and self.clock() - t <= self.ttl_s

    def override(self, reason: str = "", doc: Any = None) -> bool:
        """The operator confirmed cutting this sheet with the mismatching technology (audited).
        False when there is nothing to override."""
        doc = doc if doc is not None else self.document()
        mm = self.mismatches(doc)
        if not mm:
            return False
        sheet = sheet_of(doc)
        self._overrides[signature(doc, sheet, mm)] = self.clock()
        audit("material_override", logging.WARNING, sheet=sheet.label() if sheet else "",
              entry=sheet.entry if sheet else "", sheet_no=sheet.sheet_no if sheet else 0,
              issues=[i.message for i in mm], reason=reason, user=self.user())
        return True

    def start_guard(self, mode: str) -> tuple[bool, str]:
        """``JobService`` start guard: a cut (``process``) with a mismatch that was not
        overridden is refused.  Laser-off runs (dry run, frame) are not checked."""
        if mode != "process":
            return True, ""
        doc = self.document()
        mm = self.mismatches(doc)
        if not mm:
            return True, ""
        if self.overridden(doc):
            sheet = sheet_of(doc)
            audit("material_check", logging.WARNING, result="overridden", sheet=sheet.label() if sheet else "",
                  issues=[i.message for i in mm])
            return True, ""
        msg = "material check: " + "; ".join(i.message for i in mm)
        self.last_refusal = msg
        sheet = sheet_of(doc)
        audit("material_check", logging.WARNING, result="refused", sheet=sheet.label() if sheet else "",
              issues=[i.message for i in mm])
        return False, msg


# ============================================================================== card
def summarize_orders(refs: Any) -> list[str]:
    """One line per order on a sheet: "Customer A · PO-1234: 6 × bracket"."""
    out: list[str] = []
    for r in refs or []:
        if not isinstance(r, dict):
            continue
        who = " · ".join(x for x in (str(r.get("customer", "")), str(r.get("order", ""))) if x)
        out.append(f"{who}: {int(r.get('qty', 0) or 0)} × {r.get('part', '')}" if who else
                   f"{int(r.get('qty', 0) or 0)} × {r.get('part', '')}")
    return out


def load_card(entry: Any, doc: Any = None) -> dict:
    """The "Load this sheet" card for queue entry ``entry`` (``doc``: its document, when
    loaded, for the technology line and the mismatch flag)."""
    ex = dict(getattr(entry, "extra", None) or {})
    sb = ex.get("sheet_bounds")
    size = ""
    if sb and len(sb) == 4:
        try:
            size = f"{float(sb[2]) - float(sb[0]):.0f} × {float(sb[3]) - float(sb[1]):.0f} mm"
        except (TypeError, ValueError):
            size = ""
    size = size or str(getattr(entry, "sheet", "") or "")
    techs: list[str] = []
    nozzles: set[str] = set()
    for _k, d in sorted((ex.get("layers") or {}).items()):
        ref = ((d or {}).get("extra") or {}).get("technology") if isinstance(d, dict) else None
        key = (ref or {}).get("key") if isinstance(ref, dict) else None
        if isinstance(key, dict):
            lab = f"{key.get('material', '')} {float(key.get('thickness_mm', 0) or 0):g} mm · {key.get('gas', '')}"
            if lab not in techs:
                techs.append(lab)
            if key.get("nozzle"):
                nozzles.add(str(key["nozzle"]))
    material = str(getattr(entry, "material", "") or "")
    thick = float(getattr(entry, "thickness_mm", 0.0) or 0.0)
    issues: list[Issue] = []
    if doc is not None:
        issues = check(doc, SheetSpec(material, thick, "queue", str(getattr(entry, "id", "")),
                                      int(getattr(entry, "done", 0)) + 1))
    q = int(getattr(entry, "quantity", 1) or 1)
    n = min(int(getattr(entry, "done", 0) or 0) + 1, q)
    return {"title": "Load this sheet", "name": str(getattr(entry, "name", "")),
            "material": material or "(material not set)", "thickness": f"{thick:g} mm" if thick else "",
            "size": size, "remnant_id": str(getattr(entry, "remnant_id", "") or ""),
            "sheet": f"{n}/{q}", "parts": int(ex.get("parts", 0) or 0),
            "utilisation": float(ex.get("utilisation", 0.0) or 0.0),
            "technology": techs, "nozzle": ", ".join(sorted(nozzles)),
            "orders": summarize_orders(ex.get("orders")),
            "mismatch": [i.message for i in issues if i.code == MISMATCH],
            "rings": ex.get("sheet_rings") or [], "path": str(getattr(entry, "path", ""))}
