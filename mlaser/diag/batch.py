"""Batch cutting ("批量加工" mf705, command id 0x4270) and the production counter.

Batch dialog (FUN_004c4550 -> FUN_004fe130, O): GraphParam ``GRP.EnableBatchCut`` pd2415,
``GRP.BatchCutX_LeftEdge`` / ``GRP.BatchCutX_RightEdge`` pd2416/2417 = dock point X / Y
before processing, ``GRP.BatchCutMicoLink`` pd2418 auto micro-joint after loading a file,
``GRP.BatchCutRoate`` / ``GRP.BatchCutRoateAngle`` pd2419/2420 auto rotate after loading,
``GRP.EnableDIInput`` pd2420_1 enable start DI, and a file table ``MC.BatchCutFileTableStr``
(columns ap101 file path, ap102 planned quantity, ap103 done quantity, ap104 start DI; the
string encoding of the table is U, the rebuild stores JSON).  On OK with batch enabled, MainApp
takes the first entry whose done count (+0x20) is below its planned count (+0x1c), loads the
file (FUN_004c5e60, IsEnd check), moves to the dock point and starts the job.

Production counter (SoftParam, O): ``SOP.PlanManuCount`` pd858 (100), ``SOP.TotalManuCount``
pd850 finished count, ``SOP.AfterPlanFinishedActionType`` pd859 {0 no action, 1 prompt a
message box, 2 prohibit further processing}.  FUN_00598280 at job completion: if
``PlanManuCount <= TotalManuCount`` and the action is 1, the prompt is shown once
(FUN_004a7c60, flag +0x1f29).  The rebuild increments TotalManuCount per completed job (I:
the increment itself is outside the decompiled block) and enforces action 2 by stopping a job
that is started after the plan is reached.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .paramset import ParamSet

KEYS = ("GRP.EnableBatchCut", "GRP.BatchCutX_LeftEdge", "GRP.BatchCutX_RightEdge", "GRP.BatchCutMicoLink",
        "GRP.BatchCutRoate", "GRP.BatchCutRoateAngle", "GRP.EnableDIInput")
COUNTER_KEYS = ("SOP.PlanManuCount", "SOP.TotalManuCount", "SOP.AfterPlanFinishedActionType")
TABLE_KEY = "MC.BatchCutFileTableStr"
ACTION_NONE, ACTION_PROMPT, ACTION_PROHIBIT = 0, 1, 2


def params(settings=None) -> ParamSet:
    return ParamSet(KEYS, settings)


@dataclass
class BatchEntry:
    path: str
    planned: int = 1
    done: int = 0
    start_di: int = 0           # ap104 (U: per-entry start input)

    @property
    def finished(self) -> bool:
        return self.done >= self.planned


@dataclass
class BatchList:
    entries: list[BatchEntry] = field(default_factory=list)

    def to_text(self) -> str:
        return json.dumps([asdict(e) for e in self.entries], ensure_ascii=False)

    @classmethod
    def from_text(cls, text: str) -> "BatchList":
        if not text:
            return cls()
        try:
            rows = json.loads(text)
        except ValueError:
            return cls()
        out = cls()
        for r in rows:
            if isinstance(r, dict) and r.get("path"):
                out.entries.append(BatchEntry(str(r["path"]), int(r.get("planned", 1)), int(r.get("done", 0)),
                                              int(r.get("start_di", 0))))
        return out

    def current(self) -> Optional[BatchEntry]:
        """FUN_004c4550: first entry with done < planned."""
        for e in self.entries:
            if not e.finished:
                return e
        return None

    def reset(self) -> None:
        for e in self.entries:
            e.done = 0

    def remaining(self) -> int:
        return sum(max(0, e.planned - e.done) for e in self.entries)


class ProductionCounter:
    def __init__(self, settings: dict):
        self.p = ParamSet(COUNTER_KEYS, settings)
        self.prompted = False

    @property
    def plan(self) -> int:
        return int(self.p["SOP.PlanManuCount"])

    @property
    def done(self) -> int:
        return int(self.p["SOP.TotalManuCount"])

    @property
    def action(self) -> int:
        return int(self.p["SOP.AfterPlanFinishedActionType"])

    def reached(self) -> bool:
        return self.plan > 0 and self.plan <= self.done

    def job_done(self) -> Optional[str]:
        """Count one job; returns "prompt" once when the plan is reached with action 1."""
        self.p["SOP.TotalManuCount"] = self.done + 1
        if self.reached() and self.action == ACTION_PROMPT and not self.prompted:
            self.prompted = True
            return "prompt"
        return None

    def start_allowed(self) -> bool:
        return not (self.reached() and self.action == ACTION_PROHIBIT)

    def reset(self) -> None:
        self.p["SOP.TotalManuCount"] = 0
        self.prompted = False


class BatchRunner:
    """Runs the batch list: load -> (micro-joints / rotate) -> dock move -> start, one job per
    completion until every entry reached its planned count."""

    def __init__(self, window, p: ParamSet, blist: BatchList, log: Callable[[str], None] = lambda m: None):
        self.w, self.p, self.list, self.log = window, p, blist, log
        self.active = False
        self.entry: Optional[BatchEntry] = None
        self._loaded_path: Optional[str] = None

    def start(self) -> bool:
        if not bool(self.p["GRP.EnableBatchCut"]):
            self.log("mf705: batch cut is not enabled (pd2415)")
            return False
        self.active = True
        return self._run_next()

    def stop(self) -> None:
        self.active = False

    def _run_next(self) -> bool:
        e = self.list.current()
        if e is None:
            self.active = False
            self.log("mf705: batch finished")
            return False
        if not os.path.exists(e.path):
            self.log(f"mf705: file not found: {e.path}")
            self.active = False
            return False
        self.entry = e
        if self._loaded_path != e.path:
            if not self.w.load_path(e.path):
                self.active = False
                return False
            self._loaded_path = e.path
            self._after_load()
        ctl = self.w.ctl
        x, y = float(self.p["GRP.BatchCutX_LeftEdge"]), float(self.p["GRP.BatchCutX_RightEdge"])
        try:
            ctl.move_to(x, y)
        except Exception as ex:  # noqa: BLE001
            self.log(f"mf705: dock move failed: {ex}")
        self._pending_start = True
        return True

    def _after_load(self) -> None:
        if bool(self.p["GRP.BatchCutMicoLink"]):
            fn = getattr(self.w, "handlers", {}).get("craft.mj_auto")
            if fn:
                fn()
        if bool(self.p["GRP.BatchCutRoate"]):
            ang = float(self.p["GRP.BatchCutRoateAngle"])
            if ang:
                from mlaser import ops
                docs = getattr(self.w, "docs", None)       # mlaser.services DocumentService
                if docs is not None:
                    docs.edit(lambda d: ops.rotate(d, ang), "batch rotate")
                else:
                    ops.rotate(self.w.document, ang)

    def tick(self) -> None:
        """Start the job once the dock move ended (called from the UI timer)."""
        if not self.active or not getattr(self, "_pending_start", False):
            return
        st = self.w.ctl.state()
        if st.mode in ("ready",) or not st.connected:
            self._pending_start = False
            run_job = getattr(self.w, "run_job", None)      # the planned job (JobService)
            if callable(run_job):
                run_job("process")
            else:
                self.w.ctl.start(self.w.document)

    def job_done(self, ok: bool) -> None:
        if not self.active or self.entry is None:
            return
        if ok:
            self.entry.done += 1
            self.log(f"mf705: {os.path.basename(self.entry.path)} {self.entry.done}/{self.entry.planned}")
        else:
            self.active = False
            return
        self._run_next()
