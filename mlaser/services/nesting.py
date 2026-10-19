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
                "rotation_step": p.rotation_step}


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
def sheet_documents(job: NestJob, result: Optional[NestResult] = None, layers: Optional[dict] = None
                    ) -> list[tuple[int, Document, dict]]:
    """One Document per used sheet layout: ``(layout index, document, info)`` where ``info``
    has the sheet name, bounds, outline rings, remnant id, utilisation and part count."""
    from mlaser.nest.shapes import sheet_geometry
    from mlaser.services.remnants import polygons, rings_of
    result = result or job.result
    if result is None:
        return []
    st = stats(job, result)
    out = []
    for k, lay in enumerate(result.layouts):
        if not lay.placements:
            continue
        sh = job.sheet(lay.sheet_id)
        doc = Document(parts=layout_parts(job, lay), layers=copy.deepcopy(layers or {}))
        full, _ = sheet_geometry(sh, 0.0)
        polys = polygons(full)
        rings = rings_of(polys[0], origin=(0.0, 0.0)) if polys else []
        info = {"sheet": sh.name, "sheet_bounds": list(full.bounds), "sheet_rings": rings,
                "remnant_id": str(sh.extra.get("remnant_id", "")) if sh.is_remnant else "",
                "utilisation": float(st["sheets"][k]["utilisation"]) if k < len(st["sheets"]) else 0.0,
                "parts": len(lay.placements), "gravity": result.gravity or job.params.gravity,
                "spacing": job.params.effective_spacing(),
                "size": f"{full.bounds[2] - full.bounds[0]:.0f} x {full.bounds[3] - full.bounds[1]:.0f} mm"}
        out.append((k, doc, info))
    return out


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
                  save: Optional[Callable[[Document, str], None]] = None) -> list:
    """Write every used sheet as ``<directory>/<name>_sheet<k>.chf`` and queue it; identical
    sheets (same layout on the same stock) become one entry with their count.  Returns the
    queue entries."""
    if save is None:
        from mlaser.cam.raster_engraving import expand_document
        from mlaser.formats.chf import save_chf

        def save(doc: Document, path: str) -> None:
            save_chf(expand_document(doc), path)
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    groups: dict[tuple, list] = {}
    order: list[tuple] = []
    for k, doc, info in sheet_documents(job, layers=layers):
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
        path = os.path.join(directory, f"{safe}_{stamp}_sheet{n}.chf")
        save(doc, path)
        est = float(estimate(doc)) if estimate is not None else 0.0
        extra = {kk: info[kk] for kk in ("sheet_bounds", "sheet_rings", "utilisation", "parts", "gravity", "spacing")}
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
           "send_to_queue"]
