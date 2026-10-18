"""Process statistics records and files (mlaser.report.records / recorder)."""
from mlaser.diag.jobwatch import JobEvent, JobWatcher
from mlaser.model import Contour, Document, Line, Part
from mlaser.report.recorder import StatsRecorder, record_for
from mlaser.report.records import (JobRecord, ReportStore, cost, parse_duration, parse_line,
                                   parse_statistic_line, read_records, totals)

# lines copied from the shipped Report/*.txt and File/ProcessesStatistic.txt
NEW = "QR_EnodetoDXFTest.dxf,154.40x103.41 mm,2026-04-13 15:52:07,1.10 m,1.07 m,0,0分43秒,29.76 Sec,10.46 Sec,0.00 Sec"
TOTAL = "未命名-1,20.00×19.90 mm,2026-09-16 15:16:01,20.86 m,0.12 m,0,1分21秒,79.21 Sec,1.33 Sec,0.00 Sec"
OLD = "2024-09-20 16:21:09  未命名-1,200.00×68.60mm,7.98m,0.35m,708,10分12秒,2024-09-20 16:21:05"
GARBAGE = ("2026-03-18 16:44:38  未命名-1,0.00x0.00 mm,1970-01-01 08:00:00,0.00 m,0.00 m,0,0分00秒,"
           "-92559631349317830000000000000000000000000000000000000000000000.00 Sec,0.00 Sec,0.00 Sec")
STAT = "未命名-1,0.0X0.0mm,0mm,0mm,0,0秒,1970-01-01 08:00:00"


def test_parse_shipped_lines():
    r = parse_line(NEW)
    assert r.name == "QR_EnodetoDXFTest.dxf" and (r.width, r.height) == (154.40, 103.41)
    assert r.start == "2026-04-13 15:52:07" and r.cut_m == 1.10 and r.move_m == 1.07
    assert r.duration_s == 43 and r.cut_s == 29.76 and r.move_s == 10.46 and r.pierce_s == 0
    t = parse_line(TOTAL)
    assert (t.width, t.height, t.duration_s) == (20.0, 19.9, 81)
    o = parse_line(OLD)
    assert o.pierces == 708 and o.duration_s == 612 and o.start == "2024-09-20 16:21:05"
    assert o.extra["logged"] == "2024-09-20 16:21:09" and o.cut_m == 7.98
    g = parse_line(GARBAGE)
    assert g.cut_s == 0.0                    # absurd doubles from the original are dropped
    assert parse_line("") is None and parse_line("a,b,c") is None
    s = parse_statistic_line(STAT)
    assert s.name == "未命名-1" and s.start == "1970-01-01 08:00:00"


def test_round_trip_and_formats():
    r = parse_line(NEW)
    assert r.to_line("zh") == NEW
    assert r.to_line("en").split(",")[6] == "00Min43Sec"
    assert parse_duration("01Min05Sec") == 65 and parse_duration("12秒") == 12
    assert parse_line(r.to_line("zh", "×")).width == r.width
    assert len(r.fields()) == 10


def test_store_files(tmp_path):
    st = ReportStore(str(tmp_path), "zh")
    a, b = parse_line(NEW), parse_line(TOTAL)
    st.append_history(a)
    st.append_history(b)
    raw = open(st.total_report, "rb").read()
    assert raw.endswith(b"0.00 Sec\r\n") and "×".encode() in raw
    assert [r.name for r in st.history()] == [a.name, b.name]
    assert st.read_statistic().name == b.name           # newest record, GBK
    st.write_report_txt([a, b])
    assert open(st.report_txt, "rb").read().count(b"\r\r\n") == 2
    assert (tmp_path / "Report" / "lang.txt").read_bytes() == b"lang==0"
    assert len(read_records(st.log_report)) == 2
    assert [r.name for r in st.read_report_txt()] == [a.name, b.name]


def test_totals_cost():
    recs = [parse_line(NEW), parse_line(TOTAL)]
    t = totals(recs)
    assert round(t["cut_m"], 2) == 21.96 and t["pierces"] == 0 and t["time_s"] == 124
    assert round(cost(t, price_cut=2.0, price_time=0.1), 2) == round(21.96 * 2 + 12.4, 2)


def _doc():
    sq = Contour([Line((0, 0), (10, 0)), Line((10, 0), (10, 5)), Line((10, 5), (0, 5)), Line((0, 5), (0, 0))])
    return Document(parts=[Part([sq])])


def test_record_for_without_toolpath():
    r = record_for(_doc(), name="x.dxf")
    assert (r.width, r.height) == (10, 5) and abs(r.cut_m - 0.03) < 1e-9 and r.pierces == 1


def test_recorder_on_job_events(tmp_path):
    st = ReportStore(str(tmp_path), "en")
    doc = _doc()
    rec = StatsRecorder(st, lambda: (doc, None, {"cut_length": 30.0, "rapid_length": 5.0, "cut": 3.0,
                                                  "rapid": 1.0, "dwell": 0.5, "total": 4.5}, "/a/b/part.dxf", None))
    w = JobWatcher(clock=iter([100.0, 112.0]).__next__)
    w.start_listeners.append(rec.on_start)
    w.finish_listeners.append(rec.on_finish)
    w.feed_log("process: 4 points, 30.0 mm")
    w.feed_log("done: process in 12.0 s")
    assert len(rec.records) == 1
    r = rec.records[0]
    assert r.name == "part.dxf" and r.duration_s == 12.0 and r.count == 1 and r.cut_m == 0.03
    assert st.history()[0].name == "part.dxf"
    assert (tmp_path / "Report" / "part.dxf.jpg").exists()
    assert rec.selection(False) == [r] and rec.selection(True) == [r]
    # stopped jobs are not recorded
    w2 = JobWatcher()
    w2.start_listeners.append(rec.on_start)
    w2.finish_listeners.append(rec.on_finish)
    w2.feed_log("process: 4 points")
    w2.feed_log("stopped")
    assert len(rec.records) == 1


def test_jobwatch_filters():
    w = JobWatcher()
    ev = []
    w.finish_listeners.append(ev.append)
    w.feed_log("process: laser test 100 ms at 10 %")
    assert not w.running
    w.feed_log("process: job")
    w.feed_log("paused (send pause manu cmd): breakpoint at wire 5 (0, 0)")
    w.feed_log("process: resume from wire 5")
    assert w.running
    w.feed_log("done: process in 3.0 s")
    assert len(ev) == 1 and ev[0].ok and isinstance(ev[0], JobEvent)
    w.feed_log("done: origin")
    assert len(ev) == 1
