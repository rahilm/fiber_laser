"""The resume point across a crash or a power loss (deep review 2026-11, UX finding 6).  Qt-free.

While the machine is held at a breakpoint (a Hold, a fault mid-job) or keeps a resume point
after a Stop while held, the UI writes ``File/resume_point.json`` next to the autosave; the
file is removed when there is no such point (the job was resumed, ended or the point was
discarded).  After an interrupted session :func:`assess` says whether the point belongs to the
recovered document and :func:`rebuild` turns it back into a breakpoint for it:

* the document fingerprint (:func:`mlaser.nc.resume.document_key`) of the recovered document
  must equal the one the paused job was started from: the controller's own check on Resume
  stays authoritative (the rebuilt point carries the same key);
* the job is compiled again from the same start position and the point is located by its
  contour and step (``to_graph_path``); it must land on the same machine position (1 µm), or
  it is not restored;
* the point comes back as a **kept** resume point (``ctl.kept_breakpoint``): nothing moves; the
  operator brings it back explicitly, then resumes through the normal flow (pre-flight, the arm
  question).  It is marked ``home_first``: on a real card it cannot be brought back before the
  machine was homed in this session (an E-stop or a power loss can lose the position).

A running job keeps no resume point (none is recorded mid-cut): after a crash mid-cut the
document is recovered and the operator is told that parts may already be cut.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional

from mlaser._atomic import atomic_write_text

NAME = "resume_point.json"
FORMAT = "mlaser-resume-point/1"
#: the rebuilt point must land within this distance of the recorded one (mm)
POSITION_TOL_MM = 0.001

#: why a recorded point is not restored (catalogue ids: ``recovery.why.<reason>``)
NONE = "none"                      # no point recorded: no job was held when the session ended
OTHER_DOCUMENT = "other_document"
UNREADABLE = "unreadable"
REBUILD = "rebuild"                # the job compiled now does not reach the same point
CONTROLLER = "controller"          # this controller cannot hold a resume point


def record(bp: Any) -> dict:
    """The file's content for breakpoint ``bp`` (:class:`mlaser.nc.resume.Breakpoint`)."""
    from mlaser.nc.resume import to_graph_path
    job = bp.job
    graph, path = to_graph_path(job, int(bp.index))
    sx, sy = getattr(job, "start_mm", (0.0, 0.0))
    return {"format": FORMAT, "doc_key": str(getattr(bp, "doc_key", "") or ""), "mode": str(getattr(bp, "mode", "process")),
            "graph": int(graph), "path": int(path), "index": int(bp.index), "x": float(bp.x), "y": float(bp.y),
            "start": [float(sx), float(sy)], "dry_run": bool(getattr(job, "dry_run", False))}


def save(directory: Path, bp: Any) -> Path:
    p = Path(directory) / NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(record(bp), indent=1))
    return p


def clear(directory: Path) -> None:
    try:
        (Path(directory) / NAME).unlink()
    except FileNotFoundError:
        pass


def load(directory: Path) -> tuple[Optional[dict], str]:
    """(record, "") or (None, reason)."""
    p = Path(directory) / NAME
    if not p.exists():
        return None, NONE
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(rec, dict) or rec.get("format") != FORMAT:
            return None, UNREADABLE
        for k in ("graph", "path", "index"):
            int(rec[k])
        float(rec["x"]), float(rec["y"])
        [float(v) for v in rec["start"]]
        if not rec.get("doc_key"):
            return None, UNREADABLE
    except (OSError, ValueError, TypeError, KeyError):
        return None, UNREADABLE
    return rec, ""


def assess(directory: Path, doc: Any) -> tuple[Optional[dict], str]:
    """The recorded point when it belongs to ``doc`` ((record, "")), else (None, reason)."""
    rec, why = load(directory)
    if rec is None:
        return None, why
    from mlaser.nc.resume import document_key
    if document_key(doc) != rec["doc_key"]:
        return None, OTHER_DOCUMENT
    return rec, ""


def rebuild(rec: dict, doc: Any, ctl: Any) -> tuple[Optional[Any], str]:
    """The breakpoint of ``rec`` on ``doc`` compiled by ``ctl`` now: (Breakpoint, "") or
    (None, reason).  The breakpoint is marked ``recovered`` and ``home_first``."""
    from mlaser.nc.resume import Breakpoint, document_key, from_graph_path, position_mm
    key = document_key(doc)
    if key != rec.get("doc_key"):
        return None, OTHER_DOCUMENT
    place = getattr(ctl, "place", None)
    if not callable(place) or not hasattr(ctl, "cam_params") or getattr(ctl, "machine", None) is None:
        return None, CONTROLLER
    try:
        from mlaser.nc import compile_job
        sx, sy = (float(v) for v in rec["start"])
        dry = bool(rec.get("dry_run")) or not bool(getattr(ctl, "laser_enabled", True))
        job = compile_job(place(doc), ctl.cam_params, ctl.machine, start=(sx, sy), dry_run=dry,
                          layers=getattr(ctl, "layers", None))
        idx = from_graph_path(job, int(rec["graph"]), int(rec["path"]))
        x, y = position_mm(job, idx)
    except Exception:  # noqa: BLE001 - any failure: the point is not restored (said why)
        return None, REBUILD
    if not 0 < idx < len(job.wire) or math.dist((x, y), (float(rec["x"]), float(rec["y"]))) > POSITION_TOL_MM:
        return None, REBUILD
    bp = Breakpoint(job, idx, x, y, mode=str(rec.get("mode") or "process"), doc_key=key)
    # read by the UI's wording, and by ControllerFacade.restore_kept_breakpoint (homing first)
    setattr(bp, "recovered", True)
    setattr(bp, "home_first", True)
    return bp, ""


__all__ = ["NAME", "record", "save", "clear", "load", "assess", "rebuild", "NONE", "OTHER_DOCUMENT",
           "UNREADABLE", "REBUILD", "CONTROLLER"]
