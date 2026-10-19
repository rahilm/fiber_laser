"""Report workspace: production per day and per job, time and cost, alarm history and
utilisation, with CSV and PDF export (docs/review/ux.md P1-7; extends the report.exe port in
:mod:`mlaser.report`).

Recording is event-based: a :class:`mlaser.diag.jobwatch.JobWatcher` on the controller's
:class:`~mlaser.machine.events.EventBus` gives the tracked process jobs (a pause or a fault keeps
the job open until it is resumed or stopped); the alarm and fault history comes from the
``AlarmRaised`` / ``AlarmCleared`` / ``StateChanged`` events.  Records go to
:class:`mlaser.report.ledger.Ledger` (``<data>/Report/production.jsonl``, ``alarms.jsonl``).

Cost: the plan's estimate ``cost`` (the Process track's time-and-cost card) when present, else
the rates of this plugin's settings (``plugin.report.*``: machine, gas and power per hour, per
metre cut, per pierce, material per m²); ``Ledger.cost_hook`` is the hook for other providers.
"""
from __future__ import annotations

import datetime as _dt
import os
import time
from typing import Any, Optional

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QPushButton, QSizePolicy,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from mlaser.machine import events as EV
from mlaser.report import ledger as LG
from mlaser.services.config import Setting
from mlaser.services.operator import fmt_duration

from ..safety_texts import remedy
from . import TOUCH_MIN, show_workspace, tr

PERIODS = (("today", "report.period.today", "Today"), ("week", "report.period.week", "Last 7 days"),
           ("month", "report.period.month", "Last 30 days"), ("all", "report.period.all", "All"))
RATE_KEYS = ("machine_per_h", "gas_per_h", "power_per_h", "per_cut_m", "per_pierce", "material_per_m2")


# ============================================================================== recorder
class ProductionRecorder:
    """Writes a :class:`ProductionRecord` per finished process job and the alarm history."""

    def __init__(self, window: Any, ledger: LG.Ledger) -> None:
        from mlaser.diag.jobwatch import JobWatcher
        self.window = window
        self.ledger = ledger
        self.watcher = JobWatcher()
        self.watcher.start_listeners.append(self._started)
        self.watcher.finish_listeners.append(self._finished)
        self._ctx: dict = {}
        self._unsub: list = []
        self._bus: Any = None

    def attach(self, bus: Any) -> None:
        if bus is self._bus:
            return
        for un in self._unsub:
            un()
        self._bus = bus
        self.watcher.subscribe(bus)
        self._unsub = [bus.subscribe(EV.AlarmRaised, lambda e: self.sync_alarms()),
                       bus.subscribe(EV.AlarmCleared, lambda e: self.sync_alarms()),
                       bus.subscribe(EV.StateChanged, lambda e: self.sync_alarms())]

    def _provider(self, name: str) -> Any:
        hooks = getattr(self.window, "hooks", None)
        fn = hooks.provider(name) if hooks is not None else None
        return fn() if fn is not None else None

    def _started(self, mode: str, info: str) -> None:
        if mode != "process":
            self._ctx = {}
            return
        w = self.window
        run = getattr(getattr(w, "jobs", None), "last_run", None)
        plan = getattr(run, "plan", None) if run is not None else None
        plan = plan or getattr(w, "plan", None)
        est = dict(getattr(plan, "estimate", None) or getattr(w, "estimate", None) or {})
        doc = getattr(plan, "document", None) or w.document
        try:
            x0, y0, x1, y1 = doc.bbox()
        except Exception:  # noqa: BLE001
            x0 = y0 = x1 = y1 = 0.0
        pierces = 0
        tp = getattr(plan, "toolpath", None) or ()
        pierces = sum(1 for m in tp if type(m).__name__ == "Pierce") or sum(len(p.contours) for p in doc.parts)
        path = getattr(w, "path", None) or ""
        ctx = {"name": os.path.basename(path) if path else tr(w, "run.untitled", "(untitled)"), "path": path,
               "estimate": est, "est_s": float(est.get("total", 0.0) or 0.0),
               "cut_m": float(est.get("cut_length", 0.0) or 0.0) / 1000.0,
               "move_m": float(est.get("rapid_length", 0.0) or 0.0) / 1000.0,
               "pierces": int(pierces), "width_mm": max(0.0, x1 - x0), "height_mm": max(0.0, y1 - y0)}
        runner = self._provider("queue_runner")
        ent = getattr(runner, "current", None) if runner is not None and runner.state != "idle" else None
        if ent is not None:
            sb = ent.extra.get("sheet_bounds")
            if sb:
                ctx["width_mm"], ctx["height_mm"] = sb[2] - sb[0], sb[3] - sb[1]
            ctx.update(material=ent.material, thickness_mm=ent.thickness_mm, queue_id=ent.id,
                       sheet_no=ent.done + 1, remnant_id=ent.remnant_id,
                       utilisation=float(ent.extra.get("utilisation", 0.0) or 0.0), name=ent.name)
        self._ctx = ctx

    def _finished(self, ev: Any) -> None:
        if ev.mode != "process" or not self._ctx:
            return
        c, self._ctx = self._ctx, {}
        n_al = len(self.ledger.alarms(since=ev.start))
        rec = LG.ProductionRecord(ev.start, ev.end, c["name"], c["path"], ev.mode, bool(ev.ok),
                                  "done" if ev.ok else (ev.info or "stopped"), ev.duration_s, c["est_s"],
                                  c["cut_m"], c["move_m"], c["pierces"], c["width_mm"], c["height_mm"],
                                  c.get("material", ""), float(c.get("thickness_mm", 0.0) or 0.0),
                                  float(c.get("utilisation", 0.0) or 0.0), c.get("queue_id", ""),
                                  int(c.get("sheet_no", 0) or 0), c.get("remnant_id", ""), n_al)
        self.ledger.add_job(rec, c["estimate"])

    def sync_alarms(self) -> None:
        w = self.window
        ctl = w.ctl
        trn = getattr(w, "tr_", None)
        active = []
        try:
            st = ctl.safety()
            if st.banner == "FAULT":
                active.append(("FAULT", st.fault, "fault", remedy(st.fault, trn)))
        except Exception:  # noqa: BLE001
            pass
        for a in list(ctl.active_alarms or []):
            text = a.text(trn) if hasattr(a, "text") else str(a)
            active.append((str(getattr(a, "code", text)), text, getattr(a, "kind", "card"), remedy(a, trn)))
        job = os.path.basename(getattr(w, "path", None) or "") if self.watcher.running else ""
        self.ledger.sync_alarms(active, job)


# ============================================================================== chart
class DayChart(QWidget):
    """Run time per day as bars (newest right), alarm time stacked in red."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.days: list[dict] = []
        self.setAccessibleName("run time per day")

    def set_days(self, days: list[dict]) -> None:
        self.days = list(reversed(days[:30]))
        self.update()

    def paintEvent(self, _ev) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        try:
            pal = self.palette()
            p.fillRect(self.rect(), pal.color(QPalette.Base))
            if not self.days:
                p.setPen(pal.color(QPalette.PlaceholderText))
                p.drawText(self.rect(), Qt.AlignCenter, tr(self.window(), "report.no_data", "No production recorded yet"))
                return
            m, bottom = 8, self.height() - 22
            top = 18
            mx = max(max(d["run_s"], d["alarm_s"] * 0 + d["run_s"]) for d in self.days) or 1.0
            n = len(self.days)
            bw = max(4.0, (self.width() - 2 * m) / n)
            p.setPen(pal.color(QPalette.Text))
            p.drawText(m, 14, tr(self.window(), "report.chart", "Cutting time per day (h)") + f"  max {mx / 3600:.1f} h")
            for i, d in enumerate(self.days):
                x = m + i * bw
                h = (bottom - top) * d["run_s"] / mx
                p.fillRect(QRectF(x + 2, bottom - h, bw - 4, h), QColor("#2e7d32"))
                if d["stopped"]:
                    p.setPen(QPen(QColor("#c62828"), 2))
                    p.drawLine(int(x + 2), int(bottom + 2), int(x + bw - 2), int(bottom + 2))
                p.setPen(pal.color(QPalette.Text))
                if n <= 14 or i % max(1, n // 10) == 0:
                    p.drawText(QRectF(x, bottom + 3, bw, 18), Qt.AlignHCenter, d["day"][5:])
        finally:
            p.end()


# ============================================================================== workspace
def _table(cols: list[str], name: str) -> QTableWidget:
    t = QTableWidget(0, len(cols))
    t.setObjectName(name)
    t.setHorizontalHeaderLabels(cols)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def _fill(t: QTableWidget, rows: list[list]) -> None:
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            it = QTableWidgetItem(str(v))
            if isinstance(v, (int, float)):
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            t.setItem(r, c, it)


class ReportWorkspace(QWidget):
    def __init__(self, ctx: Any, ledger: LG.Ledger) -> None:
        super().__init__()
        self.ctx = ctx
        self.w = w = ctx.window
        self.ledger = ledger
        self.setObjectName("report_workspace")
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        top = QHBoxLayout()
        self.period = QComboBox()
        self.period.setObjectName("report_period")
        self.period.setMinimumHeight(TOUCH_MIN)
        for key, tid, text in PERIODS:
            self.period.addItem(tr(w, tid, text), key)
        self.period.setCurrentIndex(1)
        self.period.currentIndexChanged.connect(lambda _i: self.refresh())
        top.addWidget(QLabel(tr(w, "report.period", "Period")))
        top.addWidget(self.period)
        top.addStretch(1)
        for key, tid, text, fn in (("refresh", "report.btn.refresh", "Refresh", self.refresh),
                                   ("csv", "report.btn.csv", "Export CSV...", self.export_csv),
                                   ("pdf", "report.btn.pdf", "Export PDF...", self.export_pdf),
                                   ("legacy", "report.btn.legacy", "Job report pages...", self.legacy_viewer)):
            b = QPushButton(tr(w, tid, text))
            b.setObjectName(f"report_{key}")
            b.setMinimumHeight(TOUCH_MIN)
            b.clicked.connect(lambda _=False, f=fn: f())
            top.addWidget(b)
        v.addLayout(top)
        # tiles
        tiles = QFrame()
        tiles.setObjectName("report_tiles")
        g = QGridLayout(tiles)
        self.tiles: dict[str, QLabel] = {}
        for n, (key, tid, text) in enumerate((("jobs", "report.tile.jobs", "Jobs"),
                                              ("run", "report.tile.run", "Cutting time"),
                                              ("est", "report.tile.est", "Actual / estimated"),
                                              ("cut", "report.tile.cut", "Cut length"),
                                              ("pierces", "report.tile.pierces", "Pierces"),
                                              ("util", "report.tile.util", "Utilisation"),
                                              ("cost", "report.tile.cost", "Cost"),
                                              ("alarms", "report.tile.alarms", "Alarms"))):
            cap = QLabel(tr(w, tid, text))
            val = QLabel("-")
            val.setObjectName(f"report_tile_{key}")
            f = val.font()
            f.setPointSizeF(17)
            f.setBold(True)
            val.setFont(f)
            box = QVBoxLayout()
            box.addWidget(cap)
            box.addWidget(val)
            g.addLayout(box, 0, n)
            self.tiles[key] = val
        v.addWidget(tiles)
        self.chart = DayChart()
        v.addWidget(self.chart)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("report_tabs")
        self.tabs.setStyleSheet(f"QTabBar::tab{{min-height:{TOUCH_MIN - 12}px;padding:4px 12px;}}")
        self.days = _table([tr(w, "report.col.day", "Day"), tr(w, "report.col.jobs", "Jobs"),
                            tr(w, "report.col.ok", "Done"), tr(w, "report.col.stopped", "Stopped"),
                            tr(w, "report.col.run", "Cutting time"), tr(w, "report.col.cut", "Cut (m)"),
                            tr(w, "report.col.pierces", "Pierces"), tr(w, "report.col.util", "Util. %"),
                            tr(w, "report.col.cost", "Cost"), tr(w, "report.col.alarms", "Alarms"),
                            tr(w, "report.col.alarm_time", "Alarm time")], "report_days")
        self.jobs = _table([tr(w, "report.col.start", "Start"), tr(w, "report.col.name", "Job"),
                            tr(w, "report.col.result", "Result"), tr(w, "report.col.run", "Cutting time"),
                            tr(w, "report.col.est", "Estimated"), tr(w, "report.col.cut", "Cut (m)"),
                            tr(w, "report.col.pierces", "Pierces"), tr(w, "report.col.material", "Material"),
                            tr(w, "report.col.util", "Util. %"), tr(w, "report.col.cost", "Cost")], "report_jobs")
        self.per_job = _table([tr(w, "report.col.name", "Job"), tr(w, "report.col.sheets", "Sheets"),
                               tr(w, "report.col.ok", "Done"), tr(w, "report.col.run", "Cutting time"),
                               tr(w, "report.col.cut", "Cut (m)"), tr(w, "report.col.cost", "Cost")], "report_per_job")
        self.alarms = _table([tr(w, "report.col.time", "Time"), tr(w, "report.col.code", "Code"),
                              tr(w, "report.col.text", "Alarm"), tr(w, "report.col.duration", "Duration"),
                              tr(w, "report.col.job", "Job"), tr(w, "report.col.remedy", "Remedy")], "report_alarms")
        self.tabs.addTab(self.days, tr(w, "report.tab.days", "Per day"))
        self.tabs.addTab(self.jobs, tr(w, "report.tab.jobs", "Jobs"))
        self.tabs.addTab(self.per_job, tr(w, "report.tab.per_job", "Per job"))
        self.tabs.addTab(self.alarms, tr(w, "report.tab.alarms", "Alarm history"))
        v.addWidget(self.tabs, 1)
        self.note = QLabel()
        self.note.setObjectName("report_note")
        v.addWidget(self.note)
        self.data: dict = {}
        ledger.listeners.append(self._ledger_changed)

    def _ledger_changed(self) -> None:
        if self.isVisible():
            QTimer.singleShot(0, self.refresh)

    def showEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().showEvent(ev)
        self.refresh()

    def span(self) -> tuple[float, float]:
        return LG.period(self.period.currentData() or "week")

    def refresh(self) -> dict:
        w = self.w
        since, until = self.span()
        now = time.time()
        jobs = self.ledger.jobs(since, until)
        alarms = self.ledger.alarms(since, until)
        s = LG.summary(jobs, alarms, now)
        days = LG.by_day(jobs, alarms, now)
        cur = s["currency"] or self.ledger.rates.currency
        self.tiles["jobs"].setText(f"{s['ok']} / {s['jobs']}")
        self.tiles["run"].setText(fmt_duration(s["run_s"]))
        self.tiles["est"].setText(f"{100 * s['actual_of_est_s'] / s['est_s']:.0f} %" if s["est_s"] else "-")
        self.tiles["cut"].setText(f"{s['cut_m']:.1f} m")
        self.tiles["pierces"].setText(str(s["pierces"]))
        self.tiles["util"].setText(f"{100 * s['utilisation']:.1f} %" if s["utilisation"] else "-")
        self.tiles["cost"].setText(f"{s['cost']:.2f} {cur}")
        self.tiles["alarms"].setText(f"{s['alarms']}  ({fmt_duration(s['alarm_s'])})")
        self.chart.set_days(days)
        _fill(self.days, [[d["day"], d["jobs"], d["ok"], d["stopped"], fmt_duration(d["run_s"]), round(d["cut_m"], 2),
                           d["pierces"], round(100 * d["utilisation"], 1), f"{d['cost']:.2f}", d["alarms"],
                           fmt_duration(d["alarm_s"])] for d in days])
        ok, stopped = tr(w, "report.result.ok", "done"), tr(w, "report.result.stopped", "stopped")
        _fill(self.jobs, [[_dt.datetime.fromtimestamp(j.start).strftime("%Y-%m-%d %H:%M"), j.name,
                           ok if j.ok else f"{stopped} ({j.reason})", fmt_duration(j.duration_s),
                           fmt_duration(j.est_s) if j.est_s else "-", round(j.cut_m, 2), j.pierces,
                           " ".join(x for x in (j.material, f"{j.thickness_mm:g} mm" if j.thickness_mm else "") if x),
                           round(100 * j.utilisation, 1) if j.utilisation else "-",
                           f"{float(j.cost.get('total', 0) or 0):.2f}"] for j in reversed(jobs)])
        _fill(self.per_job, [[r["name"], r["jobs"], r["ok"], fmt_duration(r["run_s"]), round(r["cut_m"], 2),
                              f"{r['cost']:.2f}"] for r in LG.by_job(jobs)])
        _fill(self.alarms, [[_dt.datetime.fromtimestamp(a.t).strftime("%Y-%m-%d %H:%M:%S"), a.code, a.text,
                             fmt_duration(a.duration_s(now)) + ("" if a.cleared else " …"), a.job, a.remedy]
                            for a in reversed(alarms)])
        self.note.setText(tr(w, "report.note", "Records: {path}", path=self.ledger.dir))
        self.data = {"jobs": jobs, "alarms": alarms, "summary": s, "days": days}
        return self.data

    # ------------------------------------------------------------------ export
    def _period_label(self) -> str:
        return self.period.currentText()

    def export_csv(self, directory: Optional[str] = None) -> list[str]:
        if directory is None:
            directory = QFileDialog.getExistingDirectory(self, tr(self.w, "report.btn.csv", "Export CSV..."),
                                                         self.ledger.dir)
        if not directory:
            return []
        d = self.refresh()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        now = time.time()
        out = [LG.write_csv(os.path.join(directory, f"production-days-{stamp}.csv"), LG.DAY_COLUMNS, LG.day_rows(d["days"])),
               LG.write_csv(os.path.join(directory, f"production-jobs-{stamp}.csv"), LG.JOB_COLUMNS, LG.job_rows(d["jobs"])),
               LG.write_csv(os.path.join(directory, f"alarms-{stamp}.csv"), LG.ALARM_COLUMNS,
                            LG.alarm_rows(d["alarms"], now))]
        self.ctx.log(tr(self.w, "report.msg.csv", "Production CSV written: {path}", path=directory))
        return out

    def export_pdf(self, path: Optional[str] = None) -> Optional[str]:
        from mlaser.report.printing import export_production_pdf
        w = self.w
        if path is None:
            name = time.strftime("production-%Y%m%d-%H%M%S.pdf")
            path, _ = QFileDialog.getSaveFileName(self, tr(w, "report.btn.pdf", "Export PDF..."),
                                                  os.path.join(self.ledger.dir, name), "PDF (*.pdf)")
        if not path:
            return None
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        d = self.refresh()
        s = d["summary"]
        cur = s["currency"] or self.ledger.rates.currency
        summary_rows = [[tr(w, "report.tile.jobs", "Jobs"), f"{s['ok']} / {s['jobs']}"],
                        [tr(w, "report.tile.run", "Cutting time"), fmt_duration(s["run_s"])],
                        [tr(w, "report.tile.cut", "Cut length"), f"{s['cut_m']:.1f} m"],
                        [tr(w, "report.tile.pierces", "Pierces"), s["pierces"]],
                        [tr(w, "report.tile.util", "Utilisation"), f"{100 * s['utilisation']:.1f} %"],
                        [tr(w, "report.tile.cost", "Cost"), f"{s['cost']:.2f} {cur}"],
                        [tr(w, "report.tile.alarms", "Alarms"), f"{s['alarms']} ({fmt_duration(s['alarm_s'])})"]]
        sections = [
            (tr(w, "report.pdf.summary", "Summary"), ["", ""], summary_rows),
            (tr(w, "report.tab.days", "Per day"), [self.days.horizontalHeaderItem(i).text() for i in range(self.days.columnCount())],
             [[self.days.item(r, c).text() for c in range(self.days.columnCount())] for r in range(self.days.rowCount())]),
            (tr(w, "report.tab.jobs", "Jobs"), [self.jobs.horizontalHeaderItem(i).text() for i in range(self.jobs.columnCount())],
             [[self.jobs.item(r, c).text() for c in range(self.jobs.columnCount())] for r in range(self.jobs.rowCount())]),
            (tr(w, "report.tab.alarms", "Alarm history"),
             [self.alarms.horizontalHeaderItem(i).text() for i in range(self.alarms.columnCount() - 1)],
             [[self.alarms.item(r, c).text() for c in range(self.alarms.columnCount() - 1)]
              for r in range(self.alarms.rowCount())]),
        ]
        export_production_pdf(path, tr(w, "report.pdf.title", "Production report"),
                              f"{self._period_label()} · {time.strftime('%Y-%m-%d %H:%M')}", sections)
        self.ctx.log(tr(w, "report.msg.pdf", "Production report written: {path}", path=path))
        return path

    def legacy_viewer(self) -> None:
        """The report.exe port (per-job report pages with thumbnails)."""
        if "diag.work_report_all" in self.ctx.commands:
            self.ctx.commands.run("diag.work_report_all")


class ReportPlugin:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        w = ctx.window
        self.settings = ctx.settings_schema(
            [Setting(k, "float", 0.0, unit=u, min=0.0, max=1e6, label=f"report.set.{k}")
             for k, u in zip(RATE_KEYS, ("/h", "/h", "/h", "/m", "/pierce", "/m²"))]
            + [Setting("currency", "str", "EUR", label="report.set.currency")])
        s = getattr(w, "settings", None)
        root = (s.get("data_dir") if s is not None else None)
        if not root:
            from mlaser.services.config import data_dir
            root = str(data_dir())
        self.ledger = LG.Ledger(os.path.join(root, "Report"))
        self.update_rates()
        ctx.config.subscribe(lambda key: self.update_rates() if key.startswith("plugin.report.") else None)
        self.recorder = ProductionRecorder(w, self.ledger)
        self.recorder.attach(w.ctl.events)
        ctx.on_controller_changed(lambda c: self.recorder.attach(c.events))
        ctx.provide("ledger", lambda: self.ledger)
        self.widget = ReportWorkspace(ctx, self.ledger)
        title = tr(w, "ws.report", "Report")
        ctx.add_workspace("report", self.widget, title)
        ctx.register_command("workspace.report", lambda: show_workspace(ctx, "report", self.widget, title),
                             label="ws.report")

    def update_rates(self) -> None:
        kw: dict = {k: float(self.settings[k] or 0.0) for k in RATE_KEYS}
        self.ledger.rates = LG.CostRates(currency=str(self.settings["currency"] or "EUR"), **kw)


def register(ctx: Any) -> ReportPlugin:
    return ReportPlugin(ctx)


__all__ = ["register", "ReportPlugin", "ReportWorkspace", "ProductionRecorder", "DayChart"]
