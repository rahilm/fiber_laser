"""NestService: nesting runs in a worker, results kept for comparison, results sent to the queue.

Qt-free; the logic the Nest workspace and the Nest dock (``ui/ext_nest``) share
(docs/review/ux.md P1-6, wireframe §4.3):

* :meth:`NestService.start` runs :func:`mlaser.nest.nest` in a worker thread with progress
  (``on_progress(fraction, message)``) and cancel; ``on_done(run)`` / ``on_failed(exc)`` are
  delivered through ``post`` (the GUI thread in the application).  The job is copied first, so
  the operator can keep editing the part list while it runs.
* every finished run is kept as a :class:`NestRun` (engine, parameters, sheets, utilisation,
  placed / unplaced, time) so results can be compared and an earlier one picked.
* :func:`sheet_documents` turns a result into one Document per used sheet and
  :meth:`NestService.send_to_queue` writes them as ``.chf`` files and adds one queue entry per
  sheet (quantity 1; identical sheets are merged into one entry with their count), with the
  sheet outline, material, thickness and utilisation for the remnant capture after the cut.
  A remnant sheet is reserved in the remnant store while it is queued.
"""
from __future__ import annotations

import copy
import hashlib
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from mlaser.model import Document
from mlaser.nest import NestJob, NestResult, nest, stats
from mlaser.nest.apply import layout_parts
from mlaser.nest.engine import NestEngineUnavailable

Post = Callable[[Callable[[], None]], None]


class NestCancelled(Exception):
    pass


@dataclass
class NestRun:
    """One finished nesting run."""
    number: int
    job: NestJob                         # the job as it was nested (with ``result``)
    result: NestResult
    stats: dict
    engine: str
    elapsed_s: float
    started: float = field(default_factory=time.time)
    figures: dict = field(default_factory=dict)   # run_figures: cut length, pierces

    @property
    def cut_length_mm(self) -> float:
        return float(self.figures.get("cut_length_mm", 0.0))

    @property
    def pierces(self) -> int:
        return int(self.figures.get("pierces", 0))

    @property
    def sheets(self) -> int:
        return len(self.result.layouts)

    @property
    def utilisation(self) -> float:
        return float(self.stats.get("utilisation", 0.0))

    @property
    def placed(self) -> int:
        return sum(self.result.placed.values())

    @property
    def unplaced(self) -> int:
        return sum(self.result.unplaced.values())

    def summary(self) -> dict:
        p = self.job.params
        return {"run": self.number, "engine": self.engine, "sheets": self.sheets,
                "utilisation": self.utilisation, "placed": self.placed, "unplaced": self.unplaced,
                "elapsed_s": self.elapsed_s, "spacing": p.spacing, "margin": p.margin,
                "rotation_step": p.rotation_step, "cut_length_mm": self.cut_length_mm,
                "pierces": self.pierces, "common_line": p.common_line, "part_in_part": p.part_in_part,
                "scrap_cut": p.scrap_cut, "remnant_cut": p.remnant_cut}


def _inline(fn: Callable[[], None]) -> None:
    fn()


class NestService:
    def __init__(self, post: Post = _inline, threaded: bool = True) -> None:
        self.post = post
        self.threaded = threaded
        self.runs: list[NestRun] = []
        self.running = False
        self.progress = 0.0
        self.message = ""
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._n = 0

    # ------------------------------------------------------------------ running
    def start(self, job: NestJob, on_done: Callable[[NestRun], None] = lambda r: None,
              on_failed: Callable[[BaseException], None] = lambda e: None,
              on_progress: Callable[[float, str], None] = lambda f, m: None) -> bool:
        """Nest a copy of ``job``; False when a run is already going."""
        if self.running:
            return False
        work = copy.deepcopy(job)
        work.result = None
        self._cancel.clear()
        self.running, self.progress, self.message = True, 0.0, ""
        last = [0.0]

        def progress(frac: float, msg: str) -> bool:
            self.progress, self.message = float(frac), msg
            now = time.monotonic()
            if now - last[0] > 0.1:
                last[0] = now
                f, m = float(frac), msg
                self.post(lambda: on_progress(f, m))
            return not self._cancel.is_set()

        def body() -> None:
            t0 = time.perf_counter()
            try:
                res = nest(work, progress)
                if self._cancel.is_set() and not res.layouts:
                    raise NestCancelled("cancelled")
                work.result = res
                self._n += 1
                run = NestRun(self._n, work, res, stats(work, res), res.engine, time.perf_counter() - t0)
                try:
                    run.figures = run_figures(work, res)
                except Exception as fexc:  # noqa: BLE001 - figures are informative only
                    res.notes.append(f"figures: {fexc}")
            except BaseException as exc:  # noqa: BLE001 - reported through on_failed
                err = exc

                def fail() -> None:
                    self.running = False
                    on_failed(err)
                self.post(fail)
                return

            def done() -> None:
                self.running = False
                self.runs.append(run)
                on_done(run)
            self.post(done)

        if self.threaded:
            self._thread = threading.Thread(target=body, name="mlaser-nest", daemon=True)
            self._thread.start()
        else:
            body()
        return True

    def cancel(self) -> None:
        """Stop the running nest (the engine returns what it placed so far)."""
        self._cancel.set()

    def wait(self, timeout: float = 120.0) -> bool:
        t = self._thread
        if t is not None:
            t.join(timeout)
            return not t.is_alive()
        return True

    def best(self) -> Optional[NestRun]:
        """Fewest unplaced, then fewest sheets, then the highest utilisation."""
        if not self.runs:
            return None
        return min(self.runs, key=lambda r: (r.unplaced, r.sheets, -r.utilisation))

    def clear(self) -> None:
        self.runs = []


# ============================================================================== to the queue
def _first_layer(doc: Document) -> int:
    for p in doc.parts:
        if p.contours:
            return int(p.craft.layer)
    return 1


def sheet_document(job: NestJob, layout: Any, layers: Optional[dict] = None, cam_checks: bool = True,
                   bed: Any = None, lead_params: Any = None, tip_strategy: str = "joint",
                   cam_params: Any = None) -> tuple[Document, dict]:
    """One sheet of a result as it is cut (docs/user/cutting-strategy.md): the placed parts, with
    common-line cutting when ``NestParams.common_line`` (``cam.commonedge.share_edge``), the
    skeleton cut-up and remnant separation lines (:mod:`mlaser.nest.skeleton`), and with
    ``cam_checks`` the lead collision fix (``lead_fix``, :func:`mlaser.cam.collision.fix`) and the
    tip-avoiding order (``tip_check``, :func:`mlaser.cam.tipping.apply` on ``bed``).  Returns the
    document and what the strategy did (``remnant`` polygon, line counts, fix counts, notes)."""
    from mlaser.nest.shapes import parts_geometry, sheet_geometry
    from mlaser.nest.skeleton import strategy_lines
    p = job.params
    sh = job.sheet(layout.sheet_id)
    doc = Document(parts=layout_parts(job, layout), layers=copy.deepcopy(layers or {}))
    if p.common_line and doc.parts:
        try:
            from mlaser.cam.commonedge import share_edge
            doc.parts = share_edge(doc.parts, min_len=max(p.share_edge_min_len, 0.1))
        except ImportError:
            pass
    full, _ = sheet_geometry(sh, 0.0)
    out: dict = {"remnant": None, "scrap_lines": 0, "remnant_cut": False, "leads_fixed": 0,
                 "leads_flagged": 0, "tip_risk": 0, "notes": []}
    if p.scrap_cut != "off" or p.remnant_cut:
        used = parts_geometry([q for q in doc.parts if q.contours])
        sr = strategy_lines(full, used, layout.remnant, getattr(job.result, "gravity", "") or p.gravity, p,
                            layer=_first_layer(doc))
        doc.parts += sr.lines
        out.update(remnant=sr.remnant, scrap_lines=sr.scrap_lines, remnant_cut=sr.remnant is not None)
        out["notes"] += sr.notes
    doc.extra["sheet_bounds"] = [float(v) for v in full.bounds]
    if cam_checks and p.lead_fix:
        from mlaser.cam import collision
        rep = collision.fix(doc, cam_params, sheet=full, params=lead_params)
        out["leads_fixed"] = rep.count("moved") + rep.count("shortened")
        out["leads_flagged"] = rep.count("flagged")
    if cam_checks and p.tip_check:
        from mlaser.cam import tipping
        trep = tipping.apply(doc, bed, tip_strategy)
        out["tip_risk"] = len(trep.at_risk)
    return doc, out


def has_strategy(doc: Document) -> bool:
    """The document carries cutting-strategy tags a .chf file cannot hold (cut phases, tip-risk
    marks); such sheets are queued as .mlz projects."""
    return any(q.extra.get("strategy") or q.extra.get("cut_phase") or q.extra.get("tip_risk")
               or q.extra.get("tip_risk_holes") for q in doc.parts)


def sheet_documents(job: NestJob, result: Optional[NestResult] = None, layers: Optional[dict] = None,
                    cam_checks: bool = True, **kw: Any) -> list[tuple[int, Document, dict]]:
    """One Document per used sheet layout (:func:`sheet_document`): ``(layout index, document,
    info)`` where ``info`` has the sheet name, bounds, outline rings, remnant id, utilisation,
    part count and the separated remnant's rings (``remnant_rings``, when a separation cut
    frees it)."""
    from mlaser.nest.shapes import sheet_geometry
    from mlaser.services.remnants import polygons, rings_of
    result = result or job.result
    if result is None:
        return []
    st = stats(job, result)
    out = []
    work = job if job.result is result else copy.copy(job)
    work.result = result
    for k, lay in enumerate(result.layouts):
        if not lay.placements:
            continue
        sh = job.sheet(lay.sheet_id)
        doc, strat = sheet_document(work, lay, layers, cam_checks, **kw)
        full, _ = sheet_geometry(sh, 0.0)
        polys = polygons(full)
        rings = rings_of(polys[0], origin=(0.0, 0.0)) if polys else []
        info = {"sheet": sh.name, "sheet_bounds": list(full.bounds), "sheet_rings": rings,
                "remnant_id": str(sh.extra.get("remnant_id", "")) if sh.is_remnant else "",
                "utilisation": float(st["sheets"][k]["utilisation"]) if k < len(st["sheets"]) else 0.0,
                "parts": len(lay.placements), "gravity": result.gravity or job.params.gravity,
                "spacing": job.params.effective_spacing(),
                "size": f"{full.bounds[2] - full.bounds[0]:.0f} x {full.bounds[3] - full.bounds[1]:.0f} mm",
                "strategy": {kk: v for kk, v in strat.items() if kk != "remnant"}}
        if strat.get("remnant") is not None:
            info["remnant_rings"] = rings_of(strat["remnant"], origin=(0.0, 0.0))
        out.append((k, doc, info))
    return out


def run_figures(job: NestJob, result: NestResult, layers: Optional[dict] = None) -> dict:
    """Comparison figures of a result: total cut length (mm) and pierces of its sheets as cut
    (common line, cut-up and separation lines included; :func:`sheet_documents` without the
    lead / tip fixes; :func:`mlaser.nest.skeleton.sheet_stats`)."""
    from mlaser.nest.skeleton import sheet_stats
    tot = {"cut_length_mm": 0.0, "pierces": 0}
    for _k, doc, _info in sheet_documents(job, result, layers, cam_checks=False):
        f = sheet_stats(doc)
        tot["cut_length_mm"] += f["cut_length_mm"]
        tot["pierces"] += f["pierces"]
    return tot


def _layout_key(doc: Document) -> str:
    h = hashlib.sha1()
    for p in doc.parts:
        for c in p.contours:
            for s in c.segments:
                h.update(repr((round(s.start[0], 3), round(s.start[1], 3), round(s.end[0], 3),
                               round(s.end[1], 3))).encode())
    return h.hexdigest()


def send_to_queue(job: NestJob, queue: Any, directory: str, name: str = "nest", layers: Optional[dict] = None,
                  material: str = "", thickness_mm: float = 0.0, remnants: Any = None,
                  estimate: Optional[Callable[[Document], float]] = None,
                  save: Optional[Callable[[Document, str], None]] = None,
                  strategy: Optional[dict] = None) -> list:
    """Write every used sheet as ``<directory>/<name>_sheet<k>.chf`` and queue it; identical
    sheets (same layout on the same stock) become one entry with their count.  A sheet with
    cutting-strategy tags (cut-up lines, tip-risk order; :func:`has_strategy`) is written as
    ``.mlz`` (the .chf format cannot hold them).  ``strategy``: keyword arguments of
    :func:`sheet_document` (``bed``, ``lead_params``, ``tip_strategy``, ``cam_params``).
    Returns the queue entries."""
    custom_save = save is not None
    if save is None:
        from mlaser.cam.raster_engraving import expand_document
        from mlaser.formats.chf import save_chf

        def save(doc: Document, path: str) -> None:
            if path.endswith(".mlz"):
                from mlaser.project.format import save_project
                save_project(path, doc)
            else:
                save_chf(expand_document(doc), path)
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    groups: dict[tuple, list] = {}
    order: list[tuple] = []
    for k, doc, info in sheet_documents(job, layers=layers, **(strategy or {})):
        key: tuple = (_layout_key(doc), info["sheet"], info["remnant_id"])
        if key not in groups or info["remnant_id"]:
            if key in groups:                       # a remnant is one physical sheet
                key = key + (k,)
            groups[key] = [k, doc, info, 0]
            order.append(key)
        groups[key][3] += 1
    entries = []
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name) or "nest"
    for n, key in enumerate(order, 1):
        k, doc, info, count = groups[key]
        ext = ".mlz" if (has_strategy(doc) and not custom_save) else ".chf"
        path = os.path.join(directory, f"{safe}_{stamp}_sheet{n}{ext}")
        save(doc, path)
        est = float(estimate(doc)) if estimate is not None else 0.0
        extra = {kk: info[kk] for kk in ("sheet_bounds", "sheet_rings", "utilisation", "parts", "gravity", "spacing")}
        if info.get("remnant_rings"):
            extra["remnant_rings"] = info["remnant_rings"]      # the separated remnant (exact)
        extra["layers"] = {str(li): asdict(lp) for li, lp in (layers or {}).items()
                           if not getattr(lp, "extra", {}).get("placeholder")}
        e = queue.add(path, count, name=f"{name} #{n}", material=material, thickness_mm=thickness_mm,
                      sheet=info["remnant_id"] or info["size"], remnant_id=info["remnant_id"], est_s=est,
                      extra=extra)
        if info["remnant_id"] and remnants is not None:
            try:
                remnants.reserve(info["remnant_id"], f"queue:{e.id}")
            except KeyError:
                pass
        entries.append(e)
    return entries


__all__ = ["NestService", "NestRun", "NestCancelled", "NestEngineUnavailable", "sheet_documents",
           "sheet_document", "has_strategy", "run_figures", "send_to_queue"]
