"""Production ledger: one record per job and per alarm, with day totals, costs and CSV export.

Qt-free.  It extends the report.exe port (:mod:`.records`, whose 10-field lines stay exactly as
the original writes them) with what a production report needs (docs/review/ux.md P1-7):

* ``Report/production.jsonl``: one :class:`ProductionRecord` per finished tracked job (ok or
  stopped): times, actual vs. estimated duration, cut / move length, pierces, sheet, material,
  thickness, utilisation, queue entry, alarms during the job, cost;
* ``Report/alarms.jsonl``: one :class:`AlarmRecord` per raised card alarm or latched fault, with
  the time it was cleared;
* :func:`by_day`, :func:`by_job`, :func:`summary` aggregate them; :func:`write_csv` exports.

Costs: :class:`CostRates` (machine, gas and power per hour, per metre cut, per pierce, material
per m²) gives :func:`job_cost`.  The Process track's time-and-cost card may provide its own
figures: a plan estimate that carries ``estimate["cost"]`` (a dict with ``total`` and parts) is
used as is (``cost_hook`` takes precedence over both).

JSON lines are appended (one ``write`` per record), so a crash loses at most the last line and
a damaged line is skipped on reading.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable, Iterable, Optional

PRODUCTION_FILE = "production.jsonl"
ALARM_FILE = "alarms.jsonl"


# ============================================================================== records
@dataclass
class ProductionRecord:
    start: float                       # time.time()
    end: float
    name: str = ""
    path: str = ""
    mode: str = "process"
    ok: bool = True
    reason: str = ""                   # "done" | "stopped" | fault text
    duration_s: float = 0.0            # actual (pauses included)
    est_s: float = 0.0                 # planner estimate
    cut_m: float = 0.0
    move_m: float = 0.0
    pierces: int = 0
    width_mm: float = 0.0
    height_mm: float = 0.0
    material: str = ""
    thickness_mm: float = 0.0
    utilisation: float = 0.0           # 0..1 (nest), 0 = unknown
    queue_id: str = ""
    sheet_no: int = 0                  # sheet number inside the queue entry (1-based)
    remnant_id: str = ""
    alarms: int = 0                    # alarms raised during the job
    cost: dict = field(default_factory=dict)   # {"total": .., "machine": .., ..., "currency": ..}

    @property
    def day(self) -> str:
        return _dt.datetime.fromtimestamp(self.start).strftime("%Y-%m-%d")

    @classmethod
    def from_json(cls, d: dict) -> "ProductionRecord":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class AlarmRecord:
    t: float
    code: str = ""
    text: str = ""
    kind: str = ""                     # card alarm kind, or "fault"
    cleared: float = 0.0               # 0 = still active (or the program ended first)
    job: str = ""                      # the job running when it was raised
    remedy: str = ""

    @property
    def day(self) -> str:
        return _dt.datetime.fromtimestamp(self.t).strftime("%Y-%m-%d")

    def duration_s(self, now: Optional[float] = None) -> float:
        end = self.cleared or (now if now is not None else self.t)
        return max(0.0, end - self.t)

    @classmethod
    def from_json(cls, d: dict) -> "AlarmRecord":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


# ============================================================================== cost
@dataclass
class CostRates:
    """Cost fields (configurable per machine; the Process track's cost card uses the same)."""
    machine_per_h: float = 0.0         # machine + operator, per hour of cutting
    gas_per_h: float = 0.0             # assist gas, per hour of cutting
    power_per_h: float = 0.0           # electricity, per hour of cutting
    per_cut_m: float = 0.0             # nozzle / lens wear, per metre cut
    per_pierce: float = 0.0
    material_per_m2: float = 0.0       # sheet material, per m² of the used sheet
    currency: str = "EUR"

    @classmethod
    def from_mapping(cls, m: Any, prefix: str = "") -> "CostRates":
        kw: dict[str, Any] = {}
        for f in fields(cls):
            try:
                v = m.get(prefix + f.name)
            except Exception:  # noqa: BLE001 - a mapping without the key
                v = None
            if v is not None and v != "":
                kw[f.name] = str(v) if f.name == "currency" else float(v)
        return cls(**kw)


def job_cost(rec: ProductionRecord, rates: CostRates) -> dict:
    h = rec.duration_s / 3600.0
    area_m2 = rec.width_mm * rec.height_mm / 1e6
    parts = {
        "machine": rates.machine_per_h * h,
        "gas": rates.gas_per_h * h,
        "power": rates.power_per_h * h,
        "wear": rates.per_cut_m * rec.cut_m + rates.per_pierce * rec.pierces,
        "material": rates.material_per_m2 * area_m2,
    }
    parts = {k: round(v, 4) for k, v in parts.items()}
    parts["total"] = round(sum(parts.values()), 4)
    parts["currency"] = rates.currency  # type: ignore[assignment]
    return parts


# ============================================================================== ledger
class Ledger:
    """The production and alarm history of one installation (``<data>/Report``)."""

    def __init__(self, report_dir: str, clock: Callable[[], float] = time.time) -> None:
        self.dir = report_dir
        self.clock = clock
        self.rates = CostRates()
        self.cost_hook: Optional[Callable[[ProductionRecord, dict], Optional[dict]]] = None
        self.listeners: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._open_alarms: dict[str, AlarmRecord] = {}

    @property
    def production_path(self) -> str:
        return os.path.join(self.dir, PRODUCTION_FILE)

    @property
    def alarm_path(self) -> str:
        return os.path.join(self.dir, ALARM_FILE)

    def _append(self, path: str, obj: dict) -> None:
        os.makedirs(self.dir, exist_ok=True)
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock, open(path, "a", encoding="utf-8") as f:
            f.write(line)

    def _changed(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _read(path: str) -> list[dict]:
        out = []
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        d = json.loads(ln)
                    except ValueError:
                        continue                # a torn last line after a crash
                    if isinstance(d, dict):
                        out.append(d)
        except OSError:
            return []
        return out

    # ------------------------------------------------------------------ jobs
    def add_job(self, rec: ProductionRecord, estimate: Optional[dict] = None) -> ProductionRecord:
        """Store a finished job; fills ``cost`` (hook, then the estimate's cost card, then the
        rates)."""
        if not rec.cost:
            cost = None
            if self.cost_hook is not None:
                try:
                    cost = self.cost_hook(rec, dict(estimate or {}))
                except Exception:  # noqa: BLE001 - a broken hook must not lose the record
                    cost = None
            if cost is None and estimate and isinstance(estimate.get("cost"), dict):
                cost = dict(estimate["cost"])
            rec.cost = cost if cost is not None else job_cost(rec, self.rates)
        self._append(self.production_path, asdict(rec))
        self._changed()
        return rec

    def jobs(self, since: float = 0.0, until: float = float("inf")) -> list[ProductionRecord]:
        out = []
        for d in self._read(self.production_path):
            try:
                r = ProductionRecord.from_json(d)
            except TypeError:
                continue
            if since <= r.start < until:
                out.append(r)
        return out

    # ------------------------------------------------------------------ alarms
    def alarm_raised(self, code: str, text: str, kind: str = "", job: str = "", remedy: str = "",
                     t: Optional[float] = None) -> Optional[AlarmRecord]:
        """Record a raised alarm (idempotent while it stays active)."""
        if code in self._open_alarms:
            return None
        rec = AlarmRecord(t if t is not None else self.clock(), str(code), text, kind, 0.0, job, remedy)
        self._open_alarms[code] = rec
        self._append(self.alarm_path, asdict(rec))
        self._changed()
        return rec

    def alarm_cleared(self, code: str, t: Optional[float] = None) -> Optional[AlarmRecord]:
        rec = self._open_alarms.pop(str(code), None)
        if rec is None:
            return None
        rec.cleared = t if t is not None else self.clock()
        self._append(self.alarm_path, {"t": rec.t, "code": rec.code, "cleared": rec.cleared, "_clear": True})
        self._changed()
        return rec

    def sync_alarms(self, active: Iterable[tuple[str, str, str, str]], job: str = "") -> None:
        """Make the open alarms match ``active`` ``(code, text, kind, remedy)`` tuples."""
        now = self.clock()
        codes = set()
        for code, text, kind, rem in active:
            codes.add(str(code))
            self.alarm_raised(str(code), text, kind, job, rem, now)
        for code in [c for c in self._open_alarms if c not in codes]:
            self.alarm_cleared(code, now)

    def open_alarms(self) -> list[AlarmRecord]:
        return list(self._open_alarms.values())

    def alarms(self, since: float = 0.0, until: float = float("inf")) -> list[AlarmRecord]:
        recs: list[AlarmRecord] = []
        index: dict[tuple, AlarmRecord] = {}
        for d in self._read(self.alarm_path):
            if d.get("_clear"):
                r = index.get((d.get("t"), str(d.get("code"))))
                if r is not None:
                    r.cleared = float(d.get("cleared") or 0.0)
                continue
            try:
                r = AlarmRecord.from_json(d)
            except TypeError:
                continue
            index[(r.t, r.code)] = r
            recs.append(r)
        return [r for r in recs if since <= r.t < until]


# ============================================================================== aggregates
def _day_bounds(day: str) -> tuple[float, float]:
    d = _dt.datetime.strptime(day, "%Y-%m-%d")
    return d.timestamp(), (d + _dt.timedelta(days=1)).timestamp()


def period(kind: str, now: Optional[float] = None) -> tuple[float, float]:
    """``today`` / ``week`` (last 7 days) / ``month`` (last 30 days) / ``all``."""
    now = time.time() if now is None else now
    today = _dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (today + _dt.timedelta(days=1)).timestamp()
    if kind == "today":
        return today.timestamp(), end
    if kind == "week":
        return (today - _dt.timedelta(days=6)).timestamp(), end
    if kind == "month":
        return (today - _dt.timedelta(days=29)).timestamp(), end
    return 0.0, float("inf")


def summary(jobs: Iterable[ProductionRecord], alarms: Iterable[AlarmRecord] = (),
            now: Optional[float] = None) -> dict:
    jobs = list(jobs)
    alarms = list(alarms)
    run_s = sum(j.duration_s for j in jobs)
    est = [j for j in jobs if j.ok and j.est_s > 0]
    util = [j.utilisation for j in jobs if j.ok and j.utilisation > 0]
    cost = sum(float(j.cost.get("total", 0.0) or 0.0) for j in jobs)
    currency = next((j.cost.get("currency") for j in jobs if j.cost.get("currency")), "")
    return {
        "jobs": len(jobs),
        "ok": sum(1 for j in jobs if j.ok),
        "stopped": sum(1 for j in jobs if not j.ok),
        "run_s": run_s,
        "est_s": sum(j.est_s for j in est),
        "actual_of_est_s": sum(j.duration_s for j in est),
        "cut_m": sum(j.cut_m for j in jobs),
        "move_m": sum(j.move_m for j in jobs),
        "pierces": sum(j.pierces for j in jobs),
        "utilisation": sum(util) / len(util) if util else 0.0,
        "cost": cost,
        "currency": currency,
        "alarms": len(alarms),
        "alarm_s": sum(a.duration_s(now) for a in alarms),
    }


def by_day(jobs: Iterable[ProductionRecord], alarms: Iterable[AlarmRecord] = (),
           now: Optional[float] = None) -> list[dict]:
    """One row per day (newest first) with :func:`summary` figures and the ``day``."""
    days: dict[str, tuple[list, list]] = {}
    for j in jobs:
        days.setdefault(j.day, ([], []))[0].append(j)
    for a in alarms:
        days.setdefault(a.day, ([], []))[1].append(a)
    out = []
    for day in sorted(days, reverse=True):
        js, als = days[day]
        row = summary(js, als, now)
        row["day"] = day
        out.append(row)
    return out


def by_job(jobs: Iterable[ProductionRecord]) -> list[dict]:
    """One row per job name (most run first): sheets, ok, time, cut length, cost."""
    groups: dict[str, list[ProductionRecord]] = {}
    for j in jobs:
        groups.setdefault(j.name or "-", []).append(j)
    rows = []
    for name, js in groups.items():
        row = summary(js)
        row["name"] = name
        rows.append(row)
    rows.sort(key=lambda r: (-r["jobs"], r["name"]))
    return rows


# ============================================================================== CSV
JOB_COLUMNS = ("start", "end", "name", "ok", "reason", "duration_s", "est_s", "cut_m", "move_m",
               "pierces", "width_mm", "height_mm", "material", "thickness_mm", "utilisation",
               "queue_id", "sheet_no", "remnant_id", "alarms", "cost_total", "currency", "path")
DAY_COLUMNS = ("day", "jobs", "ok", "stopped", "run_s", "est_s", "cut_m", "move_m", "pierces",
               "utilisation", "cost", "currency", "alarms", "alarm_s")
ALARM_COLUMNS = ("time", "code", "text", "kind", "cleared", "duration_s", "job", "remedy")


def _ts(t: float) -> str:
    return _dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""


def job_rows(jobs: Iterable[ProductionRecord]) -> list[list]:
    out = []
    for j in jobs:
        out.append([_ts(j.start), _ts(j.end), j.name, int(j.ok), j.reason, round(j.duration_s, 1),
                    round(j.est_s, 1), round(j.cut_m, 3), round(j.move_m, 3), j.pierces,
                    round(j.width_mm, 1), round(j.height_mm, 1), j.material, j.thickness_mm,
                    round(j.utilisation, 4), j.queue_id, j.sheet_no, j.remnant_id, j.alarms,
                    round(float(j.cost.get("total", 0.0) or 0.0), 2), j.cost.get("currency", ""), j.path])
    return out


def day_rows(days: Iterable[dict]) -> list[list]:
    return [[d["day"], d["jobs"], d["ok"], d["stopped"], round(d["run_s"], 1), round(d["est_s"], 1),
             round(d["cut_m"], 3), round(d["move_m"], 3), d["pierces"], round(d["utilisation"], 4),
             round(d["cost"], 2), d["currency"], d["alarms"], round(d["alarm_s"], 1)] for d in days]


def alarm_rows(alarms: Iterable[AlarmRecord], now: Optional[float] = None) -> list[list]:
    return [[_ts(a.t), a.code, a.text, a.kind, _ts(a.cleared), round(a.duration_s(now), 1), a.job, a.remedy]
            for a in alarms]


def csv_text(columns: Iterable[str], rows: Iterable[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(list(columns))
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def write_csv(path: str, columns: Iterable[str], rows: Iterable[list]) -> str:
    """UTF-8 with BOM (spreadsheet programs then detect the encoding)."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(csv_text(columns, rows))
    return path


__all__ = ["ProductionRecord", "AlarmRecord", "CostRates", "Ledger", "job_cost", "summary", "by_day",
           "by_job", "period", "job_rows", "day_rows", "alarm_rows", "csv_text", "write_csv",
           "JOB_COLUMNS", "DAY_COLUMNS", "ALARM_COLUMNS"]
