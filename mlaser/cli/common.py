"""Shared pieces of the command-line tools: errors with exit codes, document loading, the
technology lookup, the job request and JSON-safe summaries.

Every command uses the same Qt-free services as the GUI (docs/user/automation.md): the file
importers and the ``.mlz`` project format, :class:`mlaser.services.technology.TechnologyStore`
with ``apply_to_layer`` / ``apply_craft`` (what Process > Apply does), the
:class:`mlaser.services.jobs.JobService` compile (``plan_sync``: the same PlannedJob the GUI
previews and Start streams), :mod:`mlaser.services.nesting`, :mod:`mlaser.machine.preflight`,
:mod:`mlaser.services.costing` and :mod:`mlaser.services.intake`.  Nothing here connects to a
card: there is no controller in the CLI at all.
"""
from __future__ import annotations

import copy
import math
import os
from typing import Any, Optional

from mlaser.model import Document, LayerParams

# exit codes (docs/user/automation.md#exit-codes)
EXIT_OK = 0
EXIT_CHECK = 1          # the command ran, the result is "no": validate errors, parts left unplaced
EXIT_USAGE = 2          # bad arguments (argparse)
EXIT_INPUT = 3          # an input file is missing, unreadable or of an unsupported type
EXIT_FAILED = 4         # the work failed: compile / nest error, ambiguous or unknown technology
EXIT_OUTPUT = 5         # an output file could not be written
EXIT_REFUSED = 6        # queue add refused (validation, the approval list is full)

INPUT_SUFFIXES = (".dxf", ".svg", ".chf", ".mlz")


class CliError(Exception):
    """An error with its exit code and a stable ``code`` for the JSON output."""

    def __init__(self, message: str, exit_code: int = EXIT_FAILED, code: str = "failed", **detail: Any) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.code = code
        self.detail = detail


# ------------------------------------------------------------------------------ documents
def load_document(path: str) -> Document:
    """A Document from ``.dxf`` / ``.svg`` / ``.chf`` / ``.mlz`` (the GUI's importers)."""
    if not os.path.isfile(path):
        raise CliError(f"{path}: file not found", EXIT_INPUT, "not_found", path=path)
    ext = os.path.splitext(path)[1].lower()
    if ext not in INPUT_SUFFIXES:
        raise CliError(f"{path}: unsupported file type {ext or '(none)'} (use {', '.join(INPUT_SUFFIXES)})",
                       EXIT_INPUT, "unsupported", path=path)
    try:
        if ext == ".dxf":
            from mlaser.io.dxf_import import import_dxf
            doc = import_dxf(path)
        elif ext == ".svg":
            from mlaser.io.svg_import import import_svg
            doc = import_svg(path)
        elif ext == ".chf":
            from mlaser.formats.chf import load_chf
            doc = load_chf(path)
        else:
            from mlaser.project import load_project
            doc = load_project(path).document
    except CliError:
        raise
    except Exception as exc:  # noqa: BLE001 - any importer failure is an input error
        raise CliError(f"{path}: cannot read ({type(exc).__name__}: {exc})", EXIT_INPUT, "unreadable",
                       path=path) from exc
    if doc is None:
        raise CliError(f"{path}: cannot read", EXIT_INPUT, "unreadable", path=path)
    return doc


def save_document(doc: Document, path: str) -> str:
    """Write ``doc`` by the extension of ``path``: ``.mlz`` / ``.chf`` / ``.dxf`` / ``.svg``."""
    ext = os.path.splitext(path)[1].lower()
    try:
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        if ext == ".mlz":
            from mlaser.project import save_project
            save_project(path, doc)
        elif ext == ".chf":
            from mlaser.project import atomic_write, export_chf
            atomic_write(path, export_chf(doc))
        elif ext == ".dxf":
            from .export import write_dxf
            write_dxf(doc, path)
        elif ext == ".svg":
            from .export import write_svg
            write_svg(doc, path)
        else:
            raise CliError(f"{path}: unsupported output type {ext or '(none)'} (use .mlz, .chf, .dxf, .svg)",
                           EXIT_USAGE, "unsupported_output", path=path)
    except CliError:
        raise
    except OSError as exc:
        raise CliError(f"{path}: cannot write ({exc.strerror or exc})", EXIT_OUTPUT, "write_failed",
                       path=path) from exc
    return path


def used_layers(doc: Document) -> list[int]:
    return sorted({int(p.craft.layer) for p in doc.parts if p.contours})


# ------------------------------------------------------------------------------ technology
def parse_tech(spec: str) -> dict:
    """``material/thickness[/gas[/nozzle]]`` (e.g. ``S235/3/O2``); the material matches a
    unique record case-insensitively by substring."""
    bits = [b.strip() for b in str(spec).split("/")]
    if len(bits) < 2 or not bits[0]:
        raise CliError(f"--tech {spec!r}: use material/thickness[/gas[/nozzle]], e.g. \"S235/3/O2\"",
                       EXIT_USAGE, "bad_tech")
    try:
        thick = float(bits[1].lower().replace("mm", "").strip())
    except ValueError as exc:
        raise CliError(f"--tech {spec!r}: thickness {bits[1]!r} is not a number", EXIT_USAGE, "bad_tech") from exc
    return {"material": bits[0], "thickness": thick, "gas": bits[2] if len(bits) > 2 else "",
            "nozzle": "/".join(bits[3:]) if len(bits) > 3 else ""}


def open_store(path: Optional[str] = None) -> Any:
    from mlaser.services.technology import TechnologyStore
    return TechnologyStore(path) if path else TechnologyStore()


def find_technology(store: Any, spec: str) -> Any:
    """The one technology record matching ``spec`` (:func:`parse_tech`)."""
    want = parse_tech(spec)
    cands = []
    for t in store.all():
        k = t.key
        if want["material"].lower() not in k.material.lower():
            continue
        if not math.isclose(float(k.thickness_mm), want["thickness"], abs_tol=1e-6):
            continue
        if want["gas"] and want["gas"].lower() != k.gas.lower():
            continue
        if want["nozzle"] and want["nozzle"].lower() != k.nozzle.lower():
            continue
        cands.append(t)
    exact = [t for t in cands if t.key.material.lower() == want["material"].lower()]
    if len(exact) == 1:
        return exact[0]
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise CliError(f"--tech {spec!r}: no technology record matches", EXIT_FAILED, "unknown_tech")
    raise CliError(f"--tech {spec!r}: {len(cands)} records match; add the gas and nozzle",
                   EXIT_FAILED, "ambiguous_tech", candidates=[t.key.label() for t in cands])


def apply_technology(doc: Document, tech: Any, layers: Optional[list[int]] = None, craft: bool = True) -> list[int]:
    """Process > Apply: the record's parameters on ``layers`` (default every used layer) and
    its craft defaults on their parts.  Returns the layers."""
    from mlaser.services import technology as T
    layers = layers or used_layers(doc)
    for li in layers:
        lp = doc.layers.get(li) or LayerParams(index=li)
        doc.layers[li] = T.apply_to_layer(lp, tech)
    if craft:
        T.apply_craft(doc, tech, [p for p in doc.parts if p.craft.layer in layers])
    return layers


def technology_ref(doc: Document) -> dict:
    """Layer -> the technology the layer was set from (``extra['technology']``) or None."""
    from mlaser.services.technology import layer_technology_ref
    out: dict = {}
    for li in used_layers(doc):
        ref = layer_technology_ref(doc.layers.get(li))
        out[str(li)] = ({"key": ref.get("key"), "id": ref.get("id"), "version": ref.get("version"),
                         "example": ref.get("example")} if ref else None)
    return out


# ------------------------------------------------------------------------------ planning
def config_service(path: Optional[str] = None) -> Any:
    from mlaser.services.config import ConfigService
    return ConfigService(path) if path else ConfigService()


def make_request(doc: Document, machine: Any, mode: str = "process", start: tuple = (0.0, 0.0),
                 dry_run: bool = False) -> Any:
    """The JobRequest the GUI builds (``services.jobs.build_request``), without a controller:
    the start is the given head position, no placement, no machine layer table."""
    from mlaser.cam.toolpath import CamParams
    from mlaser.parity_ports import active_profile
    from mlaser.services.jobs import JobRequest
    snap = copy.deepcopy(doc)
    return JobRequest(mode=mode, document=snap, machine=copy.deepcopy(machine),
                      start=(float(start[0]), float(start[1])), cam_params=CamParams(), layers=None,
                      dry_run=(mode != "process") or bool(dry_run), profile=active_profile().name,
                      source=copy.deepcopy(doc))


def plan(doc: Document, machine: Any, mode: str = "process", start: tuple = (0.0, 0.0),
         dry_run: bool = False) -> Any:
    """Compile through :meth:`JobService.plan_sync` (the GUI's compile, in this thread)."""
    from mlaser.services.jobs import InlineExecutor, JobService
    js = JobService(executor=InlineExecutor(), debounce_s=0)
    try:
        return js.plan_sync(make_request(doc, machine, mode, start, dry_run))
    except CliError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported with its type
        raise CliError(f"compile failed: {type(exc).__name__}: {exc}", EXIT_FAILED, "compile_failed") from exc
    finally:
        js.shutdown()


def r(v: Any, nd: int = 3) -> Any:
    """Round floats for the JSON output (NaN / inf as None)."""
    if isinstance(v, float):
        return round(v, nd) if math.isfinite(v) else None
    if isinstance(v, (list, tuple)):
        return [r(x, nd) for x in v]
    if isinstance(v, dict):
        return {str(k): r(x, nd) for k, x in v.items()}
    return v


def plan_summary(p: Any) -> dict:
    """JSON-safe facts of a PlannedJob."""
    job = p.job
    est = dict(p.estimate or {})
    return r({
        "mode": p.mode, "dry_run": bool(p.dry_run), "profile": p.profile, "key": p.key,
        "start": list(p.start), "bounds": list(p.bounds), "cut_bounds": list(p.cut_bounds),
        "wire_items": len(job.wire), "frames": len(job.frames()), "pierces": int(getattr(job, "pierces", 0) or 0),
        "time_s": float(p.time_s), "estimate_s": float(est.get("total", 0.0) or 0.0),
        "cut_length_mm": float(est.get("cut_length", 0.0) or 0.0),
        "rapid_length_mm": float(est.get("rapid_length", 0.0) or 0.0),
        "laser_on_length_mm": float(getattr(job, "laser_on_length_mm", 0.0) or 0.0),
        "compile_s": float(p.compile_s),
    })


def document_summary(doc: Document) -> dict:
    try:
        x0, y0, x1, y1 = doc.bbox()
    except Exception:  # noqa: BLE001
        x0 = y0 = x1 = y1 = 0.0
    n_c = sum(len(p.contours) for p in doc.parts)
    open_c = sum(1 for p in doc.parts for c in p.contours if c.segments and not c.closed)
    return r({"parts": len(doc.parts), "contours": n_c, "open_contours": open_c, "layers": used_layers(doc),
              "bbox": [x0, y0, x1, y1], "size_mm": [max(0.0, x1 - x0), max(0.0, y1 - y0)]})


__all__ = ["CliError", "EXIT_OK", "EXIT_CHECK", "EXIT_USAGE", "EXIT_INPUT", "EXIT_FAILED", "EXIT_OUTPUT",
           "EXIT_REFUSED", "load_document", "save_document", "parse_tech", "open_store", "find_technology",
           "apply_technology", "technology_ref", "config_service", "make_request", "plan", "plan_summary",
           "document_summary", "used_layers", "r"]
