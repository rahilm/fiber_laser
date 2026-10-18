"""Record process statistics when jobs run (MainApp cfg+0x43e4 vector, FUN_00598280).

At job start a :class:`~mlaser.report.records.JobRecord` is built from the document and its
CAM toolpath (size, cut / empty-move length, pierce count, estimated cut / move / pierce time,
start time).  When the job completes the record gets the end time and the real duration,
its run counter is incremented, it is appended to ``Report/TotalReport.txt`` and
``File/ProcessesStatistic.txt`` (FUN_004c5380 / FUN_005e6d40) and its thumbnail is saved as
``Report/<name>.jpg``.  The in-memory list ``records`` is what "Work Report [All]" shows.
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import Callable, Optional

from .records import EPOCH0, UNTITLED, JobRecord, ReportStore, now_text


def job_name(path: Optional[str]) -> str:
    """Report name: the document file name (``QR_EnodetoDXFTest.dxf`` in the shipped
    TotalReport) or the untitled name ``未命名-1``."""
    return os.path.basename(path) if path else UNTITLED


def record_for(document, toolpath=None, estimate: Optional[dict] = None, name: str = UNTITLED,
               machine=None) -> JobRecord:
    """Statistics of a job before it runs (lengths in m, times in s)."""
    x0, y0, x1, y1 = document.bbox() if hasattr(document, "bbox") else (0, 0, 0, 0)
    rec = JobRecord(name=name, width=max(0.0, x1 - x0), height=max(0.0, y1 - y0), start=now_text())
    est = dict(estimate or {})
    if toolpath and not est:
        try:
            from mlaser.motion.interp import estimate_time
            cfg = machine.motion_config() if machine is not None else None
            est = estimate_time(toolpath, cfg)
        except Exception:  # noqa: BLE001 - statistics must never block a job
            est = {}
    if est:
        rec.cut_m = est.get("cut_length", 0.0) / 1000.0
        rec.move_m = est.get("rapid_length", 0.0) / 1000.0
        rec.cut_s = est.get("cut", 0.0)
        rec.move_s = est.get("rapid", 0.0)
        rec.pierce_s = est.get("dwell", 0.0)
        rec.duration_s = est.get("total", rec.cut_s + rec.move_s + rec.pierce_s)
    elif document is not None:
        rec.cut_m = sum(c.length() for p in document.parts for c in p.contours) / 1000.0
    if toolpath:
        rec.pierces = sum(1 for m in toolpath if type(m).__name__ == "Pierce")
    else:
        rec.pierces = sum(len(p.contours) for p in getattr(document, "parts", []))
    return rec


class StatsRecorder:
    """Keeps the statistics list and writes the report files on job completion."""

    def __init__(self, store: ReportStore, snapshot: Callable[[], tuple], thumbnails: bool = True):
        """``snapshot()`` returns ``(document, toolpath, estimate, path, machine)`` of the job
        being started (the main window's current state)."""
        self.store = store
        self.snapshot = snapshot
        self.thumbnails = thumbnails
        self.records: list[JobRecord] = []
        self.current: Optional[JobRecord] = None
        self._doc = None
        self.listeners: list[Callable[[JobRecord], None]] = []
        last = store.read_statistic()
        if last is not None and last.start != EPOCH0:
            self.records.append(last)

    def on_start(self, mode: str, info: str = "") -> None:
        if mode != "process":
            return
        doc, tp, est, path, machine = self.snapshot()
        self._doc = doc
        self.current = record_for(doc, tp, est, job_name(path), machine)

    def on_finish(self, ev) -> Optional[JobRecord]:
        rec = self.current
        if rec is None or ev.mode != "process":
            return None
        self.current = None
        if not ev.ok:
            return None
        rec.end = now_text(_dt.datetime.fromtimestamp(ev.end))
        rec.start = now_text(_dt.datetime.fromtimestamp(ev.start))
        if ev.duration_s > 0:
            rec.duration_s = ev.duration_s
        rec.count += 1
        self.records.append(rec)
        self.store.append_history(rec)
        if self.thumbnails and self._doc is not None:
            try:
                from .thumbnail import save_thumbnail
                save_thumbnail(self._doc, self.store.thumbnail_path(rec.name))
            except Exception:  # noqa: BLE001
                pass
        for fn in list(self.listeners):
            try:
                fn(rec)
            except Exception:  # noqa: BLE001
                pass
        return rec

    def selection(self, all_records: bool) -> list[JobRecord]:
        """FUN_004b96c0: 0x3aa6 [New] = the newest record only, 0x3aa7 [All] = every record."""
        if not self.records:
            return []
        return list(self.records) if all_records else [self.records[-1]]
