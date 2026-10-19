"""Alarm history: every card alarm and latched fault with its raise and clear time, Qt-free.

Subscribes to a controller's event bus (``AlarmRaised`` / ``AlarmCleared`` / ``StateChanged``,
:mod:`mlaser.machine.events`) and keeps a bounded list persisted as JSON lines
(``alarm_history.jsonl`` in the data directory).  The Machine workspace shows it with the remedy
texts; a new controller is attached again on ``controller_changed``.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

FILE = "alarm_history.jsonl"


@dataclass
class AlarmRecord:
    kind: str                       # alarm | fault
    text: str
    raised: float
    code: Optional[int] = None
    cleared: Optional[float] = None
    alarm_kind: str = "card"        # controller.alarms.Alarm.kind (remedy lookup)

    @property
    def active(self) -> bool:
        return self.cleared is None

    @property
    def duration_s(self) -> Optional[float]:
        return None if self.cleared is None else max(0.0, self.cleared - self.raised)


def _code(a: Any) -> Optional[int]:
    c = getattr(a, "code", None)
    try:
        return int(c) if c is not None else None
    except (TypeError, ValueError):
        return None


def _text(a: Any) -> str:
    t = getattr(a, "text", None)
    if callable(t):
        try:
            return str(t(None))
        except Exception:  # noqa: BLE001
            pass
    return str(a)


class AlarmHistory:
    def __init__(self, path: Optional[Path] = None, limit: int = 2000) -> None:
        self.path = Path(path) if path is not None else None
        self.limit = int(limit)
        self.records: list[AlarmRecord] = []
        self.listeners: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._unsub: list[Callable[[], None]] = []
        self.load()

    # ------------------------------------------------------------------ events
    def attach(self, events: Any) -> None:
        from mlaser.machine import events as EV

        self.detach()
        self._unsub = [events.subscribe(EV.AlarmRaised, self._raised),
                       events.subscribe(EV.AlarmCleared, self._cleared),
                       events.subscribe(EV.StateChanged, self._state)]

    def detach(self) -> None:
        for un in self._unsub:
            un()
        self._unsub = []

    def _raised(self, e: Any) -> None:
        for a in e.alarms:
            code = _code(a)
            with self._lock:
                if code is not None and any(r.active and r.code == code for r in self.records):
                    continue
            self.add(AlarmRecord("alarm", _text(a), e.t, code, alarm_kind=str(getattr(a, "kind", "card"))))

    def _cleared(self, e: Any) -> None:
        codes = {_code(a) for a in e.alarms}
        texts = {_text(a) for a in e.alarms}
        self._close(lambda r: r.kind == "alarm" and ((r.code is not None and r.code in codes)
                                                     or (r.code is None and r.text in texts)), e.t)

    def _state(self, e: Any) -> None:
        if e.new == "fault" and e.old != "fault":
            self.add(AlarmRecord("fault", e.fault or e.reason, e.t))
        elif e.old == "fault" and e.new != "fault":
            self._close(lambda r: r.kind == "fault", e.t)

    # ------------------------------------------------------------------ records
    def add(self, rec: AlarmRecord) -> None:
        with self._lock:
            self.records.append(rec)
            del self.records[:-self.limit]
        self._changed()

    def _close(self, match: Callable[[AlarmRecord], bool], t: float) -> None:
        n = 0
        with self._lock:
            for r in self.records:
                if r.active and match(r):
                    r.cleared = t
                    n += 1
        if n:
            self._changed()

    def entries(self, newest_first: bool = True) -> list[AlarmRecord]:
        with self._lock:
            out = list(self.records)
        return out[::-1] if newest_first else out

    def active(self) -> list[AlarmRecord]:
        return [r for r in self.entries() if r.active]

    def clear(self) -> None:
        with self._lock:
            self.records = []
        self._changed()

    def _changed(self) -> None:
        self.save()
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        if self.path is None:
            return
        try:
            lines = self.path.read_text("utf-8").splitlines()
        except OSError:
            return
        recs = []
        for ln in lines[-self.limit:]:
            try:
                recs.append(AlarmRecord(**json.loads(ln)))
            except (ValueError, TypeError):
                continue
        with self._lock:
            self.records = recs

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            data = "".join(json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in self.records)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(data, "utf-8")
            tmp.replace(self.path)
        except OSError:
            pass


def default_history() -> AlarmHistory:
    from mlaser.services.config import data_dir

    return AlarmHistory(data_dir() / FILE)


def fmt_time(t: Optional[float]) -> str:
    return "" if t is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


__all__ = ["AlarmHistory", "AlarmRecord", "default_history", "fmt_time"]
