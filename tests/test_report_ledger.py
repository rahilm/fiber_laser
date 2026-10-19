"""Production ledger (mlaser.report.ledger): records, alarms history, day totals, costs, CSV."""
from __future__ import annotations

import csv
import datetime as dt

import pytest

from mlaser.report.ledger import (ALARM_COLUMNS, DAY_COLUMNS, JOB_COLUMNS, CostRates, Ledger,
                                  ProductionRecord, alarm_rows, by_day, by_job, day_rows, job_cost,
                                  job_rows, period, summary, write_csv)


def _t(day: int, hour: int) -> float:
    return dt.datetime(2026, 10, day, hour, 0, 0).timestamp()


def test_jobs_alarms_and_days(tmp_path):
    L = Ledger(str(tmp_path / "Report"))
    L.rates = CostRates(machine_per_h=60.0, per_pierce=0.01, currency="EUR")
    L.add_job(ProductionRecord(_t(17, 9), _t(17, 10), "flange", duration_s=3600, est_s=3500, cut_m=40,
                               pierces=100, utilisation=0.8))
    L.add_job(ProductionRecord(_t(18, 9), _t(18, 9) + 600, "flange", ok=False, reason="stopped",
                               duration_s=600, cut_m=5))
    L.add_job(ProductionRecord(_t(18, 11), _t(18, 12), "bracket", duration_s=1800, cut_m=10, utilisation=0.6),
              estimate={"cost": {"total": 12.5, "currency": "EUR"}})       # the cost card's figure
    L.alarm_raised("8104", "X servo input alarm", "servo", "flange", t=_t(18, 9) + 300)
    L.alarm_cleared("8104", t=_t(18, 9) + 420)
    L.alarm_raised("FAULT", "link lost", "fault", t=_t(18, 13))
    jobs = L.jobs()
    assert [j.name for j in jobs] == ["flange", "flange", "bracket"]
    assert jobs[0].cost["total"] == pytest.approx(60.0 + 1.0)
    assert jobs[2].cost["total"] == 12.5
    al = L.alarms()
    assert al[0].cleared == _t(18, 9) + 420 and al[0].duration_s() == 120
    assert al[1].cleared == 0.0
    days = by_day(jobs, al, now=_t(18, 14))
    assert [d["day"] for d in days] == ["2026-10-18", "2026-10-17"]
    d18 = days[0]
    assert (d18["jobs"], d18["ok"], d18["stopped"], d18["alarms"]) == (2, 1, 1, 2)
    assert d18["run_s"] == 2400 and d18["alarm_s"] == pytest.approx(120 + 3600)
    assert d18["utilisation"] == pytest.approx(0.6)
    s = summary(jobs, al, now=_t(18, 14))
    assert s["cut_m"] == 55 and s["est_s"] == 3500 and s["actual_of_est_s"] == 3600
    per_job = by_job(jobs)
    assert per_job[0]["name"] == "flange" and per_job[0]["jobs"] == 2
    since, until = _t(18, 0), _t(19, 0)
    assert len(L.jobs(since, until)) == 2 and len(L.alarms(since, until)) == 2


def test_torn_line_is_skipped(tmp_path):
    L = Ledger(str(tmp_path))
    L.add_job(ProductionRecord(1.0, 2.0, "a"))
    with open(L.production_path, "a", encoding="utf-8") as f:
        f.write('{"start": 3.0, "end"')          # crash in the middle of a write
    assert [j.name for j in L.jobs()] == ["a"]


def test_sync_alarms_is_idempotent(tmp_path):
    t = [100.0]
    L = Ledger(str(tmp_path), clock=lambda: t[0])
    L.sync_alarms([("1", "a", "card", "")])
    L.sync_alarms([("1", "a", "card", ""), ("2", "b", "card", "")])
    t[0] = 160.0
    L.sync_alarms([("2", "b", "card", "")])
    al = L.alarms()
    assert [a.code for a in al] == ["1", "2"] and al[0].cleared == 160.0 and al[1].cleared == 0.0


def test_cost_formula():
    r = ProductionRecord(0, 0, duration_s=1800, cut_m=100, pierces=50, width_mm=3000, height_mm=1500)
    c = job_cost(r, CostRates(machine_per_h=50, gas_per_h=10, power_per_h=4, per_cut_m=0.02,
                              per_pierce=0.01, material_per_m2=20, currency="CHF"))
    assert c["machine"] == 25 and c["gas"] == 5 and c["power"] == 2
    assert c["wear"] == pytest.approx(2.5) and c["material"] == pytest.approx(90)
    assert c["total"] == pytest.approx(124.5) and c["currency"] == "CHF"
    assert CostRates.from_mapping({"x.machine_per_h": "12", "x.currency": "USD"}, "x.").machine_per_h == 12.0


def test_csv_export(tmp_path):
    L = Ledger(str(tmp_path))
    L.add_job(ProductionRecord(_t(18, 9), _t(18, 10), "flange, big", duration_s=3600, cut_m=1.5))
    L.alarm_raised("9005", "FIFO starvation", "fifo", t=_t(18, 9))
    jobs, al = L.jobs(), L.alarms()
    p1 = write_csv(str(tmp_path / "jobs.csv"), JOB_COLUMNS, job_rows(jobs))
    p2 = write_csv(str(tmp_path / "days.csv"), DAY_COLUMNS, day_rows(by_day(jobs, al)))
    p3 = write_csv(str(tmp_path / "alarms.csv"), ALARM_COLUMNS, alarm_rows(al))
    with open(p1, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == list(JOB_COLUMNS) and rows[1][2] == "flange, big" and rows[1][0] == "2026-10-18 09:00:00"
    with open(p2, encoding="utf-8-sig") as f:
        assert f.read().splitlines()[1].startswith("2026-10-18,1,1,0,3600,")
    with open(p3, encoding="utf-8-sig") as f:
        assert "FIFO starvation" in f.read()


def test_periods():
    now = _t(18, 15)
    a, b = period("today", now)
    assert a == _t(18, 0) and b == _t(19, 0)
    assert period("week", now)[0] == _t(12, 0)
    assert period("all", now) == (0.0, float("inf"))
