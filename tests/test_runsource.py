"""Simulator, dry-run and practice jobs are recorded with their source and kept out of the
production figures and counters (review 2 features P1-7, §2.1 item 10)."""
from __future__ import annotations

import json

import pytest

from mlaser.machine import events as EV
from mlaser.report.ledger import PRODUCTION_SOURCES, Ledger, ProductionRecord, summary
from mlaser.services import runsource as RS
from mlaser.services.production import ProductionService


class Ctl:
    def __init__(self, sim: bool = False, laser: bool = True) -> None:
        self.sim, self.laser_enabled = sim, laser

    def is_simulator(self) -> bool:
        return self.sim


@pytest.mark.parametrize("ctl, mode, practice, dry, want", [
    (Ctl(), "process", False, None, "real"),
    (Ctl(sim=True), "process", False, None, "simulator"),
    (Ctl(laser=False), "process", False, None, "dry_run"),
    (Ctl(), "dry_run", False, None, "dry_run"),
    (Ctl(), "process", False, True, "dry_run"),
    (Ctl(sim=True), "dry_run", False, None, "dry_run"),
    (Ctl(sim=True), "process", True, None, "practice"),
    (Ctl(), "process", True, None, "practice"),
    (object(), "process", False, None, "real"),             # an unknown controller: counted, never hidden
])
def test_job_source(ctl, mode, practice, dry, want):
    assert RS.job_source(ctl, mode, practice, dry) == want


def test_ledger_filters_and_old_records(tmp_path):
    L = Ledger(str(tmp_path))
    for src in ("real", "simulator", "dry_run", "practice", "real"):
        L.add_job(ProductionRecord(1.0, 2.0, src, duration_s=60.0, source=src))
    with open(L.production_path, "a", encoding="utf-8") as f:   # written before sources existed
        f.write(json.dumps({"start": 3.0, "end": 4.0, "name": "old", "duration_s": 60.0}) + "\n")
    allj = L.jobs()
    assert len(allj) == 6 and allj[-1].source == "real"
    prod = L.jobs(sources=PRODUCTION_SOURCES)
    assert [j.name for j in prod] == ["real", "real", "old"]
    assert summary(prod)["run_s"] == 180.0
    assert [j.production for j in allj] == [True, False, False, False, True, True]


class Settings(dict):
    pass


def finished(bus, ok=True, mode="process"):
    bus.publish(EV.JobStarted(mode, "x"))
    bus.publish(EV.JobFinished(mode, ok, "done" if ok else "stopped", duration_s=30.0))


@pytest.mark.parametrize("source, counted", [("real", True), ("simulator", False), ("dry_run", False),
                                             ("practice", False)])
def test_simulator_jobs_do_not_count(tmp_path, source, counted):
    bus = EV.EventBus()
    recorded = []

    class Stats:
        current = None

        def on_start(self, mode, info):
            self.current = mode

        def on_finish(self, ev):
            recorded.append(ev)
            self.current = None

    svc = ProductionService(Settings(), str(tmp_path), bus=bus, stats=Stats(), job_source=lambda mode: source)
    done = []
    svc.job_done_listeners.append(done.append)
    before = svc.counters.process_count
    finished(bus)
    assert (svc.counters.process_count - before == 1) is counted
    assert (svc.production.done == 1) is counted
    assert bool(recorded) is counted                       # TotalReport.txt statistics
    assert done == [True]                                  # the batch runner still hears the job end


def test_a_broken_source_counts_as_real(tmp_path):
    bus = EV.EventBus()

    def broken(mode):
        raise RuntimeError("x")
    svc = ProductionService(Settings(), str(tmp_path), bus=bus, job_source=broken)
    finished(bus)
    assert svc.counters.process_count == 1


def test_maintenance_listeners(tmp_path):
    from mlaser.diag import maintain as M
    svc = ProductionService(Settings(), str(tmp_path))
    got = []
    svc.maintenance_listeners.append(lambda key, status, msg: got.append((key, status)))
    svc.maintenance_listeners.insert(0, lambda *a: (_ for _ in ()).throw(RuntimeError("bad listener")))
    item = type("It", (), {"key": "nozzle", "custom": True, "prompt": M.PROMPT_POPUP})()
    svc.maintain_plan.check = lambda now, s: [(item, "due")]
    svc.check_maintenance(0.0)
    assert got == [("nozzle", "due")]


def test_report_records_the_source(app, tmp_path, monkeypatch):
    """The Report workspace's recorder in the real wiring: the demo controller's jobs are
    simulator jobs; the same job on a 'real' controller is production; a dry run is dry_run."""
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import real_window
    w = real_window()
    try:
        rep = w.extension("report")
        bus = w.ctl.events
        finished(bus)
        monkeypatch.setattr(type(w.ctl), "is_simulator", lambda self: False)
        finished(bus)
        finished(bus, mode="dry_run")
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        assert [j.source for j in rep.ledger.jobs()] == ["simulator", "real", "dry_run"]
        rep.widget.runs.setCurrentIndex(rep.widget.runs.findData("production"))
        d = rep.widget.refresh()
        assert d["summary"]["jobs"] == 1 and d["hidden"] == 2
        assert "not counted" in rep.widget.note.text()
        diag = w.extension("ext_diag")
        assert diag.svc.job_source("process") == "real"
    finally:
        w.close()


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])
