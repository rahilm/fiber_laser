"""Labels and marking plugin: part labels, QR job tickets, remnant labels and part-ID etching.

The PDF and geometry work is Qt-free in :mod:`mlaser.labels`; this plugin collects the data
(the open document, the queue, the remnant store, the technology database), asks for the file
name and logs the result.  User guide: docs/user/labels.md.

Commands:

=========================  =========  =======================================================
``labels.part_labels``     operator   one label per part copy of the open document (PDF)
``labels.job_tickets``     operator   a QR job ticket per queued sheet (the selected queue
                                      entry, else every entry still to run)
``labels.remnant_labels``  operator   labels of the available remnants
``labels.etch_part_ids``   engineer   etch each part's id inside it on the marking layer
                                      (one undoable edit)
``labels.remove_marking``  engineer   remove the etched ids again
=========================  =========  =======================================================

The marking layer's parameters come from the technology database: a record whose
``rules["kind"]`` is ``"marking"`` (same material and thickness first, then the same material,
then any marking record); without one, :data:`FALLBACK_MARKING` (EXAMPLE values, low power,
no pierce: verify them on the machine first).
"""
from __future__ import annotations

import math
import os
import time
from typing import Any, Optional

from mlaser.labels import labels as L
from mlaser.labels import marking as MK
from mlaser.services import units as U
from mlaser.services.config import Setting

from .workspaces import tr

#: the marking layer when the technology database has no marking record: EXAMPLE values
#: (low peak power and duty, no pierce stage, air), to be verified on the machine
FALLBACK_MARKING = {"GP.CutSpeed": 150.0, "GP.CutPeakCurrent": 20, "GP.CutPower": 30, "GP.CutFreq": 5000,
                    "GP.CutGasType": 0, "GP.CutAirPressure": 0.5, "GP.CutHeight": 1.0, "GP.ManuType": 0}
from mlaser.labels.marking import MARK_LAYER_COLOR as MARK_COLOR  # noqa: E402 - the layer's colour (data)


def _data_dir(window: Any) -> str:
    s = getattr(window, "settings", None)
    d = s.get("data_dir") if s is not None else None
    if not d:
        from mlaser.services.config import data_dir
        d = str(data_dir())
    return str(d)


class LabelsPlugin:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.w = ctx.window
        self.settings = ctx.settings_schema([
            Setting("label_size", "enum", 0, options=tuple(s.key for s in L.SIZES), label="labels.set.label_size"),
            Setting("remnant_size", "enum", 0, options=tuple(s.key for s in L.SIZES),
                    label="labels.set.remnant_size"),
            Setting("ticket_page", "enum", 0, options=("A4", "A5"), label="labels.set.ticket_page"),
            Setting("remnant_auto", "bool", False, label="labels.set.remnant_auto"),
            Setting("mark_layer", "int", 10, min=1, max=10, label="labels.set.mark_layer"),
            Setting("mark_min_height", "float", 2.0, unit="mm", min=0.5, max=50.0, label="labels.set.mark_min_height"),
            Setting("mark_max_height", "float", 8.0, unit="mm", min=0.5, max=100.0, label="labels.set.mark_max_height"),
            Setting("mark_margin", "float", 1.5, unit="mm", min=0.2, max=20.0, label="labels.set.mark_margin"),
        ])
        self.last_path: Optional[str] = None
        self.last_result: Optional[MK.EtchResult] = None
        self._known_remnants: set[str] = set()
        for cid, fn, label, mode in (
                ("labels.part_labels", self.part_labels, "labels.cmd.part_labels", "operator"),
                ("labels.job_tickets", self.job_tickets, "labels.cmd.job_tickets", "operator"),
                ("labels.remnant_labels", self.remnant_labels, "labels.cmd.remnant_labels", "operator"),
                ("labels.etch_part_ids", self.etch_part_ids, "labels.cmd.etch_part_ids", "engineer"),
                ("labels.remove_marking", self.remove_marking, "labels.cmd.remove_marking", "engineer")):
            ctx.register_command(cid, lambda f=fn: f(), label=label, min_mode=mode)
        store = self._provider("remnants")
        if store is not None:
            self._known_remnants = {r.id for r in store}
            store.subscribe(self._remnants_changed)

    # ------------------------------------------------------------------ helpers
    def _provider(self, name: str) -> Any:
        hooks = getattr(self.w, "hooks", None)
        fn = hooks.provider(name) if hooks is not None else None
        try:
            return fn() if fn is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _size(self, key: str) -> L.LabelSize:
        i = int(self.settings[key] or 0)
        return L.SIZES[i] if 0 <= i < len(L.SIZES) else L.label_size(L.DEFAULT_SIZE)

    def _save_path(self, title_id: str, title: str, name: str, path: Optional[str]) -> Optional[str]:
        if path:
            return path
        from PySide6.QtWidgets import QFileDialog
        d = os.path.join(_data_dir(self.w), "labels")
        os.makedirs(d, exist_ok=True)
        p, _ = QFileDialog.getSaveFileName(self.w, tr(self.w, title_id, title), os.path.join(d, name), "PDF (*.pdf)")
        if not p:
            return None
        return p if p.lower().endswith(".pdf") else p + ".pdf"

    def _doc_material(self, doc: Any) -> tuple[str, float]:
        """Material and thickness of the open document: the queue sheet being run, else the
        technology on its first used layer."""
        runner = self._provider("queue_runner")
        ent = getattr(runner, "current", None) if runner is not None else None
        if ent is not None and getattr(runner, "state", "idle") != "idle" and ent.material:
            return ent.material, float(ent.thickness_mm or 0.0)
        from mlaser.services.technology import layer_technology_ref
        for p in getattr(doc, "parts", []):
            ref = layer_technology_ref((getattr(doc, "layers", {}) or {}).get(p.craft.layer))
            if ref and isinstance(ref.get("key"), dict):
                k = ref["key"]
                return str(k.get("material", "")), float(k.get("thickness_mm", 0.0) or 0.0)
        return "", 0.0

    # ------------------------------------------------------------------ labels
    def part_labels(self, path: Optional[str] = None) -> Optional[str]:
        doc = self.w.document
        mat, th = self._doc_material(doc)
        runner = self._provider("queue_runner")
        ent = getattr(runner, "current", None) if runner is not None and runner.state != "idle" else None
        labs = L.part_labels(doc, mat, th, (ent.remnant_id or ent.sheet) if ent else "", ent.id if ent else "")
        if not labs:
            self.ctx.log(tr(self.w, "labels.msg.no_parts", "No parts to label"))
            return None
        base = os.path.splitext(os.path.basename(getattr(self.w, "path", None) or "parts"))[0]
        path = self._save_path("labels.cmd.part_labels", "Part labels (PDF)...", f"labels-{base}.pdf", path)
        if not path:
            return None
        L.render_part_labels(path, labs, self._size("label_size"), units=U.current())
        self.last_path = path
        self.ctx.log(tr(self.w, "labels.msg.written", "{n} label(s) written: {path}", n=len(labs), path=path))
        return path

    def _ticket_entries(self) -> list:
        q = self._provider("queue")
        if q is None:
            return []
        run = self.w.extension("run") if hasattr(self.w, "extension") else None
        panel = getattr(getattr(run, "screen", None), "queue_panel", None)
        sel = panel.selected_id() if panel is not None else None
        if sel:
            try:
                return [q.get(sel)]
            except KeyError:
                pass
        return [e for e in q if e.status not in ("done", "skipped")]

    def job_tickets(self, path: Optional[str] = None) -> Optional[str]:
        entries = self._ticket_entries()
        if not entries:
            self.ctx.log(tr(self.w, "labels.msg.no_queue", "The queue has no job for a ticket"))
            return None
        from .ext_nest import _load_doc
        tickets = []
        for e in entries:
            doc = _load_doc(e.path) if os.path.exists(e.path) else None
            tickets.append(L.ticket_from_entry(e, doc))
        path = self._save_path("labels.cmd.job_tickets", "Job tickets (PDF)...",
                               time.strftime("tickets-%Y%m%d-%H%M%S.pdf"), path)
        if not path:
            return None
        L.render_tickets(path, tickets, ("A4", "A5")[int(self.settings["ticket_page"] or 0) % 2], units=U.current())
        self.last_path = path
        self.ctx.log(tr(self.w, "labels.msg.tickets", "{n} job ticket(s) written: {path}", n=len(tickets), path=path))
        return path

    def remnant_labels(self, path: Optional[str] = None, remnants: Optional[list] = None) -> Optional[str]:
        store = self._provider("remnants")
        items = remnants if remnants is not None else ([r for r in store if r.status == "available"]
                                                       if store is not None else [])
        if not items:
            self.ctx.log(tr(self.w, "labels.msg.no_remnants", "No remnant to label"))
            return None
        path = self._save_path("labels.cmd.remnant_labels", "Remnant labels (PDF)...",
                               time.strftime("remnants-%Y%m%d-%H%M%S.pdf"), path)
        if not path:
            return None
        L.render_remnant_labels(path, L.labels_of(items), self._size("remnant_size"), units=U.current())
        self.last_path = path
        self.ctx.log(tr(self.w, "labels.msg.written", "{n} label(s) written: {path}", n=len(items), path=path))
        return path

    def _remnants_changed(self) -> None:
        """A remnant was saved: write its label PDF into ``<data>/labels`` (setting
        ``remnant_auto``)."""
        store = self._provider("remnants")
        if store is None:
            return
        ids = {r.id for r in store}
        new = [r for r in store if r.id not in self._known_remnants and r.status == "available"]
        self._known_remnants = ids
        if not new or not bool(self.settings["remnant_auto"]):
            return
        d = os.path.join(_data_dir(self.w), "labels")
        try:
            os.makedirs(d, exist_ok=True)
            for r in new:
                p = os.path.join(d, f"remnant-{r.id}.pdf")
                L.render_remnant_labels(p, [L.remnant_label(r)], self._size("remnant_size"), units=U.current())
                self.ctx.log(tr(self.w, "labels.msg.remnant_auto", "Remnant label written: {path}", path=p))
        except OSError as exc:  # a label must never disturb the queue
            self.ctx.log(f"labels: {exc}")

    # ------------------------------------------------------------------ etching
    def marking_technology(self, material: str, thickness: float) -> Any:
        """The marking technology record for ``material`` / ``thickness`` (see the module
        docstring), or None."""
        ext = self.w.extension("ext_process") if hasattr(self.w, "extension") else None
        store = getattr(ext, "store", None)
        if store is None:
            return None
        try:
            marks = [t for t in store.all() if (t.rules or {}).get("kind") == "marking"]
        except Exception:  # noqa: BLE001 - the database is unavailable
            return None
        same = [t for t in marks if t.key.material == material]
        exact = [t for t in same if math.isclose(t.key.thickness_mm, thickness)]
        pool = exact or same or [t for t in marks if t.key.material in ("", "*", "any")] or marks
        if not pool:
            return None
        return min(pool, key=lambda t: abs(t.key.thickness_mm - thickness))

    def marking_layer(self, layer: int, material: str, thickness: float) -> tuple[Any, str]:
        from mlaser.model import LayerParams
        from mlaser.services.technology import TechKey, Technology, apply_to_layer
        tech = self.marking_technology(material, thickness)
        origin = "technology"
        if tech is None:
            tech = Technology(TechKey(material or "any", thickness, "Air", "marking"), dict(FALLBACK_MARKING),
                              rules={"kind": "marking"}, notes="EXAMPLE VALUES: verify on your machine before cutting.",
                              origin="example")
            origin = "fallback"
        lp = apply_to_layer(LayerParams(index=layer, color=MARK_COLOR), tech)
        lp.extra["marking"] = True
        return lp, origin

    def etch_part_ids(self) -> Optional[MK.EtchResult]:
        doc = self.w.document
        layer = int(self.settings["mark_layer"])
        used = {p.craft.layer for p in doc.parts if p.contours}
        if layer in used:
            self.ctx.log(tr(self.w, "labels.msg.layer_used", "Layer {layer} is used for cutting: choose another "
                            "marking layer (Options: labels mark_layer)", layer=layer))
            return None
        mat, th = self._doc_material(doc)
        lp, origin = self.marking_layer(layer, mat, th)
        lo = float(self.settings["mark_min_height"])
        hi = max(lo, float(self.settings["mark_max_height"]))
        margin = float(self.settings["mark_margin"])
        box: dict = {}

        def edit(d: Any) -> None:
            box["r"] = MK.etch_document(d, layer, min_height=lo, max_height=hi, margin=margin, layer_params=lp)

        self.ctx.edit(edit, tr(self.w, "labels.undo.etch", "Etch part IDs"))
        res = box.get("r")
        self.last_result = res
        if res is not None:
            self.ctx.log(tr(self.w, "labels.msg.etched", "Part IDs etched on layer {layer}: {n} part(s), {skipped} "
                            "without room", layer=layer, n=len(res.marked), skipped=len(res.skipped)))
            if origin == "fallback":
                self.ctx.log(tr(self.w, "labels.msg.fallback", "No marking technology for {material}: the marking "
                                "layer uses EXAMPLE low-power values; verify them before cutting",
                                material=mat or "-"))
        return res

    def remove_marking(self) -> int:
        layer = int(self.settings["mark_layer"])
        box = {"n": 0}

        def edit(d: Any) -> None:
            box["n"] = MK.remove_marking(d, layer)

        self.ctx.edit(edit, tr(self.w, "labels.undo.remove", "Remove part ID marking"))
        return box["n"]


def register(ctx: Any) -> LabelsPlugin:
    return LabelsPlugin(ctx)


__all__ = ["register", "LabelsPlugin", "FALLBACK_MARKING"]
