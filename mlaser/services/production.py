"""ProductionService: job statistics, device counters, the planned-quantity interlock and the
maintenance reminders (Qt-free; moved out of ``mlaser.ui.ext_diag`` in Wave 2).

Input: the controller's typed job events (:meth:`attach` with ``ctl.events``,
:mod:`mlaser.machine.events`: ``JobStarted`` / ``JobFinished`` of tracked jobs, through
:class:`mlaser.diag.jobwatch.JobWatcher`).  Both controllers (the card adapter and the
``DemoController``) publish them.  Without a bus (old scripts) the watcher falls back to the log
lines (:meth:`feed_log`) and the state (:meth:`poll`); once attached those two are no-ops for the
job detection (:meth:`poll` still runs the use timer and the maintenance check).  Outputs go
through callables, so the UI decides how to show them:

* ``stop()``: stop the running job (the interlock);
* ``notify(kind, text)``: ``kind`` is ``"log"``, ``"popup"``, ``"alarm"`` or ``"plan_reached"``
  (text ``"done/plan"``).

The planned-quantity interlock (SOP.PlanManuCount / SOP.AfterPlanFinishedActionType = 2
"prohibit") is enforced twice: :meth:`start_allowed` is a JobService start guard (the job is
refused before anything is sent), and a job that still starts is stopped at once (the original's
behaviour, FUN_004b9260 area, docs/diag.md).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Optional

from mlaser.diag import batch as B, devicereport as DR, maintain as M
from mlaser.diag.jobwatch import JobWatcher

STATE_FILE = "diag_state.json"  # pre-Wave-2 store, imported once into the ConfigService
PERSISTED_PREFIXES = (
    "SOP.",
    "ST.",
    "LT.",
    "SP.Interfere",
    "GRP.BallArm",
    "GRP.BatchCut",
    "GRP.EnableBatchCut",
    "GRP.EnableDIInput",
    "MC.Maintain",
    "MC.BatchCut",
    "DO.CustomDOStr",
    "DO.ClearAsh",
)


class ProductionService:
    def __init__(
        self,
        settings,
        data_dir: str,
        stop: Callable[[], None] = lambda: None,
        notify: Callable[[str, str], None] = lambda kind, text: None,
        stats: Any = None,
        flush: Callable[[], None] = lambda: None,
        bus: Any = None,
    ) -> None:
        """``stats``: the job-statistics recorder (:class:`mlaser.report.recorder.StatsRecorder`,
        which draws thumbnails with Qt, so the application passes it in); None records none."""
        self.settings = settings
        self.data_dir = data_dir
        self.stop, self.notify, self.flush = stop, notify, flush
        migrate_state_file(settings, data_dir)
        self.counters = DR.DeviceCounters.from_settings(settings)
        self.use_timer = DR.UseTimer(self.counters)
        self.watcher = JobWatcher()
        self.stats = stats
        if stats is not None:
            self.watcher.start_listeners.append(stats.on_start)
        self.watcher.start_listeners.append(self.job_started)
        if stats is not None:
            self.watcher.finish_listeners.append(stats.on_finish)
        self.watcher.finish_listeners.append(self.job_finished)
        self.production = B.ProductionCounter(settings)
        self.maintain_plan = M.MaintainPlan.from_text(settings.get(M.PARAM_KEY))
        self.job_done_listeners: list[Callable[[bool], None]] = []  # batch runner
        self._last_maint = 0.0
        self._announced: set = set()
        if bus is not None:
            self.attach(bus)

    # ------------------------------------------------------------------ events
    def attach(self, bus: Any) -> None:
        """Follow ``bus`` (a controller's ``events``) for the job lifecycle; call again after
        the controller was replaced.  Replaces the log parsing."""
        self.watcher.subscribe(bus)

    @property
    def event_driven(self) -> bool:
        return self.watcher.bus is not None

    # ------------------------------------------------------------------ inputs
    def feed_log(self, msg: str) -> None:
        self.watcher.feed_log(msg)

    def poll(self, state: Any, card_connected: bool, now: Optional[float] = None) -> None:
        self.watcher.poll(state)
        self.use_timer.tick(bool(card_connected))
        now = time.time() if now is None else now
        if now - self._last_maint > 60:
            self._last_maint = now
            self.check_maintenance(now)

    # ------------------------------------------------------------------ interlock
    def start_allowed(self, mode: str = "process") -> tuple[bool, str]:
        """JobService start guard: a real cut is refused once the planned quantity is reached
        with action "prohibit"."""
        if mode == "process" and not self.production.start_allowed():
            return False, "pd859: the planned quantity is reached - processing is prohibited"
        return True, ""

    def job_started(self, mode: str, info: str) -> None:
        if mode == "process" and not self.production.start_allowed():
            self.notify("log", "pd859: the planned quantity is reached - processing is prohibited")
            self.stop()

    def job_finished(self, ev) -> None:
        if ev.mode != "process":
            return
        if ev.ok:
            self.counters.add_job(ev.duration_s)
            if self.production.job_done() == "prompt":
                self.notify("log", f"pd858: planned quantity {self.production.plan} reached")
                self.notify("plan_reached", f"{self.production.done}/{self.production.plan}")
        self.save_state()
        for fn in list(self.job_done_listeners):
            fn(ev.ok)

    # ------------------------------------------------------------------ maintenance
    def process_seconds(self) -> float:
        return self.counters.total_process_h * 3600.0

    def check_maintenance(
        self, now: Optional[float] = None, label: Callable[[str], str] = lambda k: k
    ) -> list:
        due = self.maintain_plan.check(now, self.process_seconds())
        for it, status in due:
            tag = (it.key, status)
            if tag in self._announced:
                continue
            self._announced.add(tag)
            name = it.key if it.custom else label(it.key)
            msg = f"{label('A251113_0') if label('A251113_0') != 'A251113_0' else 'Maintain'}: {name} ({status})"
            if it.prompt == M.PROMPT_POPUP:
                self.notify("popup", msg)
            elif it.prompt == M.PROMPT_ERRBAR:
                self.notify("alarm", msg)
            self.notify("log", msg)
        return due

    # ------------------------------------------------------------------ state
    def save_state(self, batch_list=None) -> None:
        """Counters, maintenance plan and batch list into the settings (ConfigService), then
        written to disk."""
        self.counters.to_settings(self.settings)
        self.settings[M.PARAM_KEY] = self.maintain_plan.to_text()
        if batch_list is not None:
            self.settings[B.TABLE_KEY] = batch_list.to_text()
        self.flush()


def migrate_state_file(settings, data_dir: str) -> int:
    """Import the pre-Wave-2 ``<data>/diag_state.json`` into ``settings`` (values the user has
    not set otherwise); the file is renamed to ``.imported``.  Returns the number of values."""
    path = os.path.join(data_dir, STATE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return 0
    n = 0
    for k, v in data.items():
        if k not in settings:
            try:
                settings[k] = v
                n += 1
            except ValueError:
                continue
    try:
        os.replace(path, path + ".imported")
    except OSError:
        pass
    return n
