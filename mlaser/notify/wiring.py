"""Machine and queue events -> notifications (Qt-free, so it is tested without a display).

:class:`EventWiring` subscribes to

* a controller's :class:`~mlaser.machine.events.EventBus` (``attach_bus``): ``JobStarted`` /
  ``JobFinished`` of tracked process jobs (**job finished**; a pause or a fault keeps the job
  open), ``AlarmRaised`` (**alarm**, once per newly raised alarm code) and ``StateChanged`` to
  ``fault`` (**alarm**, once per latched fault);
* the job queue's runner (``attach_runner``, :class:`mlaser.services.queue.QueueRunner` by duck
  typing): a finished sheet (**sheet done**) and the runner waiting for the operator to load the
  next sheet after one (**queue waiting**);
* the maintenance check (:meth:`maintenance`, ``ProductionService.maintenance_listeners``).

Jobs that are not real (simulator, dry run, practice; ``source(mode)``) are skipped unless
``include_simulator()``.  Handlers only build a :class:`Notification` and call the non-blocking
:meth:`Notifier.emit <mlaser.notify.core.Notifier.emit>`, so they are safe on any thread.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from mlaser.machine import events as EV

from .core import Notification, Notifier, make

log = logging.getLogger("mlaser.notify")

Text = Callable[..., str]


def _default_text(key: str, default: str = "", **kw: Any) -> str:
    try:
        return (default or key).format(**kw)
    except (KeyError, IndexError, ValueError):
        return default or key


def _alarm_code(a: Any) -> str:
    return str(getattr(a, "code", a))


def _alarm_text(a: Any) -> str:
    return str(getattr(a, "text_en", None) or a)


class EventWiring:
    def __init__(self, notifier: Notifier, source: Callable[[str], str] = lambda mode: "real",
                 include_simulator: Callable[[], bool] = lambda: False, text: Text = _default_text,
                 machine: Callable[[], str] = lambda: "") -> None:
        self.notifier = notifier
        self.source = source
        self.include_simulator = include_simulator
        self.text = text
        self.machine = machine
        self._bus: Any = None
        self._unsub: list[Callable[[], None]] = []
        self._runner: Any = None
        self._runner_unsub: Optional[Callable[[], None]] = None
        self._runner_state = ""
        self._after_sheet = False
        self._job: Optional[tuple[str, str]] = None        # (info, source) of the open job
        self._alarms: set[str] = set()
        self._fault = False

    # ------------------------------------------------------------------ helpers
    def _counts(self, source: str) -> bool:
        return (source or "real") == "real" or bool(self.include_simulator())

    def _current_source(self, mode: str = "process") -> str:
        try:
            return str(self.source(mode) or "real")
        except Exception:  # noqa: BLE001 - unknown counts as real (never hides an alarm)
            return "real"

    def _title(self, key: str, default: str, **kw: Any) -> str:
        t = self.text(key, default, **kw)
        m = (self.machine() or "").strip()
        return f"{m}: {t}" if m else t

    def _emit(self, n: Notification) -> None:
        self.notifier.emit(n)

    # ------------------------------------------------------------------ bus
    def attach_bus(self, bus: Any) -> None:
        if bus is self._bus:
            return
        for un in self._unsub:
            un()
        self._bus = bus
        self._job = None
        self._alarms = set()
        self._fault = False
        if bus is None:
            self._unsub = []
            return
        self._unsub = [bus.subscribe(EV.JobStarted, self._on_started),
                       bus.subscribe(EV.JobFinished, self._on_finished),
                       bus.subscribe(EV.AlarmRaised, self._on_alarm),
                       bus.subscribe(EV.AlarmCleared, self._on_alarm_cleared),
                       bus.subscribe(EV.StateChanged, self._on_state)]

    def detach(self) -> None:
        self.attach_bus(None)
        if self._runner_unsub is not None:
            self._runner_unsub()
            self._runner_unsub = None
        r = self._runner
        if r is not None and self._sheet_done in getattr(r, "sheet_listeners", []):
            r.sheet_listeners.remove(self._sheet_done)
        self._runner = None

    def _on_started(self, ev: Any) -> None:
        if not ev.tracked or ev.mode != "process":
            return
        if ev.resume and self._job is not None:
            return
        self._job = (ev.info, self._current_source(ev.mode))

    def _on_finished(self, ev: Any) -> None:
        if not ev.tracked or ev.mode != "process" or self._job is None:
            return
        if ev.reason == "paused" or ev.reason.startswith("fault"):
            return                                 # continues after the pause / fault reset
        info, source = self._job
        self._job = None
        if not self._counts(source):
            return
        name = os.path.basename(info) if info else ""
        if ev.ok:
            title = self._title("notify.msg.job_done.title", "Job finished")
            msg = self.text("notify.msg.job_done", "{name} finished ({minutes} min).", name=name or "-",
                            minutes=f"{ev.duration_s / 60:.1f}")
        else:
            title = self._title("notify.msg.job_stopped.title", "Job stopped")
            msg = self.text("notify.msg.job_stopped", "{name} stopped: {reason}.", name=name or "-",
                            reason=ev.reason or "stopped")
        self._emit(make("job_finished", title, msg, key=f"job:{ev.t}", ok=bool(ev.ok), reason=ev.reason,
                        duration_s=ev.duration_s, source=source))

    def _alarm_counts(self) -> bool:
        return self._counts(self._job[1] if self._job is not None else self._current_source())

    def _on_alarm(self, ev: Any) -> None:
        active = {_alarm_code(a) for a in (ev.active or ev.alarms)}
        new = [a for a in ev.alarms if _alarm_code(a) not in self._alarms]
        self._alarms |= active | {_alarm_code(a) for a in ev.alarms}
        if not new or not self._alarm_counts():
            return
        for a in new:
            code, text = _alarm_code(a), _alarm_text(a)
            self._emit(make("alarm", self._title("notify.msg.alarm.title", "Alarm {code}", code=code),
                            self.text("notify.msg.alarm", "Alarm {code}: {text}. Check the machine; "
                                      "reset it at the machine.", code=code, text=text),
                            key=f"alarm:{code}", code=code, text=text))

    def _on_alarm_cleared(self, ev: Any) -> None:
        self._alarms = {_alarm_code(a) for a in ev.active}

    def _on_state(self, ev: Any) -> None:
        if ev.new == "fault" and not self._fault:
            self._fault = True
            if not self._alarm_counts():
                return
            fault = ev.fault or ev.reason or "fault"
            self._emit(make("alarm", self._title("notify.msg.fault.title", "FAULT latched"),
                            self.text("notify.msg.fault", "Fault: {fault}. The machine stopped; reset it "
                                      "at the machine.", fault=fault), key=f"fault:{fault}", fault=fault))
        elif ev.new != "fault":
            self._fault = False

    # ------------------------------------------------------------------ queue
    def attach_runner(self, runner: Any) -> None:
        if runner is self._runner:
            return
        if self._runner is not None:
            if self._runner_unsub is not None:
                self._runner_unsub()
            if self._sheet_done in getattr(self._runner, "sheet_listeners", []):
                self._runner.sheet_listeners.remove(self._sheet_done)
        self._runner = runner
        self._runner_unsub = None
        if runner is None:
            return
        runner.sheet_listeners.append(self._sheet_done)
        self._runner_unsub = runner.subscribe(self._runner_changed)
        self._runner_state = getattr(runner, "state", "")

    def _sheet_done(self, entry: Any, ok: bool) -> None:
        if not ok:
            return
        source = self._job[1] if self._job is not None else self._current_source()
        if not self._counts(source):
            return
        self._after_sheet = True
        self._emit(make("sheet_done", self._title("notify.msg.sheet_done.title", "Sheet done"),
                        self.text("notify.msg.sheet_done", "{name}: sheet {done}/{total} cut.",
                                  name=entry.name, done=entry.done, total=entry.quantity),
                        key=f"sheet:{entry.id}:{entry.done}", entry=entry.id, done=entry.done,
                        quantity=entry.quantity))

    def _runner_changed(self) -> None:
        r = self._runner
        if r is None:
            return
        state = getattr(r, "state", "")
        prev, self._runner_state = self._runner_state, state
        if state == "confirm" and prev == "running" and self._after_sheet:
            self._after_sheet = False
            e = r.current
            if e is None:
                return
            sheet = e.remnant_id or e.sheet
            mat = " ".join(x for x in (e.material, f"{e.thickness_mm:g} mm" if e.thickness_mm else "") if x)
            self._emit(make("queue_waiting", self._title("notify.msg.queue_waiting.title", "Load the next sheet"),
                            self.text("notify.msg.queue_waiting", "The queue waits for the next sheet: {name} "
                                      "(sheet {n}/{total}) {details}. Load it and confirm at the machine.",
                                      name=e.name, n=min(e.done + 1, e.quantity), total=e.quantity,
                                      details=", ".join(x for x in (mat, sheet) if x)),
                            key=f"wait:{e.id}:{e.done}", entry=e.id))
        elif state != "confirm" and state != "running":
            self._after_sheet = False

    # ------------------------------------------------------------------ maintenance
    def maintenance(self, key: str, status: str, message: str) -> None:
        self._emit(make("maintenance_due", self._title("notify.msg.maintenance.title", "Maintenance due"),
                        message or self.text("notify.msg.maintenance", "Maintenance due: {item} ({status}).",
                                             item=key, status=status),
                        key=f"maint:{key}:{status}", item=key, status=status))

    # ------------------------------------------------------------------ test
    def test(self) -> bool:
        return self.notifier.emit(make("test", self._title("notify.msg.test.title", "mlaser test notification"),
                                       self.text("notify.msg.test", "Notifications work. This message was sent "
                                                 "from the machine PC; nothing can be controlled by replying.")),
                                  force=True)


__all__ = ["EventWiring"]
