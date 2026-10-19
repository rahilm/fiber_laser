"""Wave 3 Run / Nest / Report workspaces through the real window wiring.

* the Run screen on the MCC100 simulator: start, hold, continue, stop, fault display (state,
  reason, alarm list with remedy), all through the SafetyUI path;
* touch sizes at 1280 x 800 and at 1280 x 720 (1920 x 1080 at 150 %);
* the queue with the operator confirmation between sheets, remnant capture after a nested sheet;
* the LAN monitor plugin (off by default, read-only snapshot);
* the Nest workspace (worker run, thumbnails, compare, send to queue) and the Report workspace
  (event-based records, CSV and PDF).

``MLASER_SHOT_DIR=<dir>`` saves screenshots of every screen.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton  # noqa: E402

from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part  # noqa: E402

SHOTS = os.environ.get("MLASER_SHOT_DIR")


def rect(x0, y0, w, h, layer=1):
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    return Part([Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)], Craft(layer=layer))


def doc_of(*parts, speed=50.0):
    return Document(parts=list(parts), layers={1: LayerParams(1, speed=speed, power_pct=85, gas=2,
                                                              gas_pressure=12.0, cut_height=0.8)})


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch, tmp_path):
    from wiring import real_window
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window()
    w.resize(1280, 800)
    yield w
    try:
        w.ctl.stop()
    except Exception:  # noqa: BLE001
        pass
    for name in ("run", "nest", "report"):
        ext = w.extension(name)
        wid = {"run": lambda: ext.screen, "nest": lambda: ext, "report": lambda: ext.widget}[name]()
        host = getattr(wid, "_workspace_window", None)
        if host is not None:
            host.close()
    w.close()


def pump(app, s=0.15):
    t = time.monotonic() + s
    while time.monotonic() < t:
        app.processEvents()
        time.sleep(0.01)


def wait(app, pred, timeout=20.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


def shot(widget, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        widget.grab().save(os.path.join(SHOTS, name))


def show_run(win, app, size=(1280, 800)):
    ws = win.extension("run")
    win.commands.run("workspace.run")
    host = ws.screen.window()
    host.resize(*size)
    pump(app, 0.2)
    ws.screen.refresh()
    return ws, host


def test_workspaces_registered(win):
    assert {"run.run", "nest.nest", "report.report"} <= set(win.workspaces)
    for cmd in ("workspace.run", "workspace.nest", "workspace.report", "queue.add_current",
                "queue.confirm_sheet", "monitor.toggle"):
        assert cmd in win.commands
    run = win.extension("run")
    assert run.monitor is None and run.settings["monitor_enabled"] is False     # off by default


def test_run_screen_on_the_simulator(win, app):
    """Start, hold, continue, stop and the fault display on the MCC100 simulator."""
    from mlaser.controller.ui_adapter import UiController
    # a generous link deadline: this is a UI test, the link timing is tests/safety's job
    ctl = UiController(poll_s=0.02, stream_process=False, link_deadline_ms=3000.0)
    assert ctl.connect_simulator(time_scale=1.0)
    win.set_controller(ctl)
    try:
        win.set_document(doc_of(rect(10, 10, 300, 200), rect(400, 10, 200, 200), rect(10, 300, 300, 150)))
        win.jobs.settle()
        ws, host = show_run(win, app)
        scr = ws.screen
        assert scr.status.kind == "ready" and "disarmed" in scr.reason.text()
        shot(host, "run_ready_sim_1280x800.png")
        assert ctl.arm()
        scr.buttons["start"].click()
        if win.jobs.last_run is not None:
            win.jobs.settle(win.jobs.last_run, timeout=30)
        assert wait(app, lambda: ctl.state().mode == "process" and ctl.state().progress > 0.02, 30)
        scr.refresh()
        st = scr.status
        assert st.kind == "running" and "RUNNING" in scr.state.text()
        assert st.contours > 0 and st.parts == 3 and st.part >= 1
        assert st.power_pct == 85 and st.gas == "N2" and st.height_mm == 0.8
        assert scr.progress.value() > 0 and "ETA" in scr.times.text()
        assert scr.canvas.current >= 0
        shot(host, "run_running_sim_1280x800.png")
        scr.buttons["hold"].click()
        assert wait(app, lambda: ctl.state().mode == "pause" or ctl.breakpoint is not None, 10)
        pump(app, 0.3)
        scr.refresh()
        if scr.status.kind == "fault" and "halted (pause)" in ctl.safety().fault:
            # known race under heavy load (Safety track, merge note): the streamer's in-flight
            # request is refused by the pause halt latch and latches a fault; the breakpoint is
            # kept, so after Reset the screen shows HOLD at the breakpoint
            scr.reset_btn.click()
            pump(app, 0.3)
            scr.refresh()
        assert scr.status.kind == "hold"
        assert scr.buttons["resume_here"].isEnabled() and scr.buttons["resume_contour"].isEnabled()
        assert "CONTINUE" in scr.buttons["start"].text().upper()
        shot(host, "run_hold_sim_1280x800.png")
        scr.resume("contour")                         # resume from the contour start
        assert wait(app, lambda: ctl.state().mode == "process", 15)
        scr.buttons["stop"].click()
        assert wait(app, lambda: not ctl.job_active() and ctl.state().mode == "ready", 15)
        scr.refresh()
        assert scr.status.kind == "ready"
        ctl.raise_fault("link lost: test")
        pump(app, 0.3)
        scr.refresh()
        assert scr.status.kind == "fault" and "FAULT" in scr.state.text()
        assert "link lost" in scr.reason.text()
        assert scr.alarm_box.isVisible() and scr.alarm_list.count() == 1
        assert "Remedy" in scr.alarm_list.item(0).text() and "network cable" in scr.alarm_list.item(0).text()
        assert not scr.buttons["start"].isEnabled() and scr.buttons["stop"].isEnabled()
        shot(host, "run_fault_sim_1280x800.png")
        scr.reset_btn.click()
        pump(app, 0.3)
        scr.refresh()
        assert scr.status.kind == "ready"
    finally:
        ctl.disconnect()


@pytest.mark.parametrize("size", [(1280, 800), (1280, 720)])
def test_touch_targets_and_layout(win, app, size):
    ws, host = show_run(win, app, size)
    scr = ws.screen
    for b in scr.findChildren(QPushButton):
        if b.isVisible():
            assert b.height() >= 44, (b.objectName(), b.height())
            r = b.rect().translated(b.mapTo(host, b.rect().topLeft()))
            assert host.rect().contains(r), (b.objectName(), r, host.size())    # never clipped
    for key in ("stop", "start", "hold", "frame", "dry_run", "arm"):
        assert scr.buttons[key].height() >= 56, key
    assert scr.buttons["stop"].height() >= 70
    if size == (1280, 720):
        shot(host, "run_ready_1280x720_150pct.png")


def _save(doc, path):
    from mlaser.formats.chf import save_chf
    save_chf(doc, str(path))
    return str(path)


def test_queue_flow_with_confirmation_and_remnant(win, app, tmp_path):
    from mlaser.machine.demo import DemoController
    t = [0.0]
    ctl = DemoController(clock=lambda: t[0])
    win.set_controller(ctl)
    ctl.connect()
    assert ctl.arm()
    ws = win.extension("run")
    q = ws.queue
    p1 = _save(doc_of(rect(0, 0, 400, 1500)), tmp_path / "a.chf")
    p2 = _save(doc_of(rect(0, 0, 100, 100)), tmp_path / "b.chf")
    e1 = q.add(p1, 2, extra={"sheet_rings": [[[0, 0], [3000, 0], [3000, 1500], [0, 1500]]],
                             "sheet_bounds": [0, 0, 3000, 1500], "spacing": 5.0, "gravity": "left"},
               material="S235", thickness_mm=3.0)
    e2 = q.add(p2, 1)
    win.set_document(doc_of(rect(0, 0, 10, 10)))      # the layer parameters (a .chf has none)
    show_run(win, app)
    scr = ws.screen
    scr.toggle_queue()
    assert ws.runner.state == "confirm" and scr.buttons["confirm_sheet"].isVisible()
    shot(scr.window(), "run_queue_confirm_1280x800.png")
    assert not ctl.job_active()                    # nothing starts without the operator

    def run_sheet():
        scr.buttons["confirm_sheet"].click()
        if win.jobs.last_run is not None:
            win.jobs.settle(win.jobs.last_run, timeout=30)
        assert wait(app, lambda: ws.runner.state == "running", 10), ws.runner.message
        t[0] += 10000.0                            # the demo machine finishes the path
        ctl.state()
        assert wait(app, lambda: ws.runner.state != "running", 10)

    run_sheet()
    assert e1.done == 1 and ws.runner.state == "confirm" and ws.runner.current is e1
    rem = ws.remnants.available("S235", 3.0)
    assert len(rem) == 1 and rem[0].size[0] == pytest.approx(3000 - 405, abs=1)
    run_sheet()
    assert e1.status == "done" and ws.runner.current is e2
    run_sheet()
    assert e2.status == "done" and ws.runner.state == "idle"
    scr.tabs.setCurrentWidget(scr.queue_panel)
    scr.queue_panel.refresh()
    assert scr.queue_panel.table.rowCount() == 2
    shot(scr.window(), "run_queue_table_1280x800.png")
    from mlaser.services.queue import JobQueue
    again = JobQueue(q.path)                        # persisted
    assert [(e.status, e.done) for e in again] == [("done", 2), ("done", 1)]
    # the report recorded the three sheets from the job events
    rep = win.extension("report")
    jobs = rep.ledger.jobs()
    assert len(jobs) == 3 and all(j.ok for j in jobs)
    assert jobs[0].material == "S235" and jobs[0].queue_id == e1.id and jobs[0].sheet_no == 1


def test_lan_monitor_plugin(win, app):
    import socket
    ws = win.extension("run")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    ws.settings["monitor_port"] = port
    assert ws.set_monitor(True, confirm=False)
    try:
        show_run(win, app)
        assert ws.screen.monitor_chip.isVisible() and str(port) in ws.screen.monitor_chip.text()
        ws.publish_monitor()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", f"/status.json?t={ws.settings['monitor_token']}")
        snap = json.loads(c.getresponse().read())
        assert snap["kind"] in ("offline", "ready") and "labels" in snap
        c.request("POST", f"/status.json?t={ws.settings['monitor_token']}", body=b"x")
        assert c.getresponse().status == 405
        c.close()
        shot(ws.screen.window(), "run_monitor_on_1280x800.png")
    finally:
        ws.set_monitor(False)
    assert ws.monitor is None and ws.settings["monitor_enabled"] is False


def test_nest_workspace(win, app):
    from mlaser.nest import NestPart
    ws = win.extension("nest")
    ws.job.parts.clear()
    ws.job.sheets.clear()
    for k, (w_, h_, q) in enumerate(((300, 200, 6), (150, 150, 8))):
        ws.job.add_part(NestPart([rect(0, 0, w_, h_)], name=f"p{k}", quantity=q))
    ws.sheet_w.setValue(1000)
    ws.sheet_h.setValue(600)
    ws.sheet_n.setValue(3)
    ws.add_sheet()
    run = win.extension("run")
    r = run.remnants.add([[[0, 0], [700, 0], [700, 400], [0, 400]]], "S235", 3.0)
    ws.material.setText("S235")
    ws.thickness.setValue(3.0)
    win.commands.run("workspace.nest")
    host = ws.window()
    host.resize(1280, 800)
    pump(app, 0.2)
    ws.refresh()
    assert ws.stock.count() == 2                     # the sheet and the matching remnant
    ws.stock.item(1).setCheckState(ws.stock.item(1).checkState().__class__.Checked)
    assert any(s.extra.get("remnant_id") == r.id for s in ws.job.sheets)
    ws.time_limit.setValue(10)
    assert ws.start()
    assert wait(app, lambda: not ws.service.running, 120)
    run1 = ws.run
    assert run1 is not None and run1.placed == 14 and ws.results.count() == run1.sheets >= 2
    assert "%" in ws.results.item(0).text()
    shot(host, "nest_results_1280x800.png")
    ws.spacing.setValue(10)
    assert ws.start() and wait(app, lambda: not ws.service.running, 120)
    assert ws.compare.rowCount() == 2
    entries = ws.send_to_queue()
    assert entries and all(e.extra.get("sheet_rings") for e in entries)
    rem_entries = [e for e in entries if e.remnant_id]
    assert rem_entries and run.remnants.get(r.id).status == "reserved"
    assert sum(e.quantity for e in entries) == ws.run.sheets
    assert all(os.path.exists(e.path) for e in entries)


def test_report_workspace_exports(win, app, tmp_path):
    from mlaser.machine import events as EV
    rep = win.extension("report")
    win.set_document(doc_of(rect(0, 0, 100, 50)))
    bus = win.ctl.events
    bus.publish(EV.JobStarted("process", "x"))
    bus.publish(EV.JobFinished("process", True, "done", duration_s=42.0))
    bus.publish(EV.JobStarted("process", "y"))
    bus.publish(EV.JobFinished("process", False, "stopped", duration_s=5.0))
    pump(app, 0.2)
    jobs = rep.ledger.jobs()
    assert [j.ok for j in jobs] == [True, False]
    win.commands.run("workspace.report")
    host = rep.widget.window()
    host.resize(1280, 800)
    pump(app, 0.2)
    d = rep.widget.refresh()
    assert d["summary"]["jobs"] == 2 and rep.widget.jobs.rowCount() == 2
    shot(host, "report_1280x800.png")
    csvs = rep.widget.export_csv(str(tmp_path))
    assert len(csvs) == 3 and all(os.path.getsize(p) > 0 for p in csvs)
    pdf = rep.widget.export_pdf(str(tmp_path / "prod.pdf"))
    with open(pdf, "rb") as f:
        assert f.read(5) == b"%PDF-"


def test_i18n_ids_of_the_workspaces_exist():
    """Every ``tr(window, "<id>", ...)`` id the Wave 3 run-track modules use is in en.json."""
    from mlaser.i18n import catalogue
    import mlaser.ui.workspaces as pkg
    cat = catalogue("en")
    here = os.path.dirname(pkg.__file__)
    pat = re.compile(r"""\btr\(\s*[\w.()]+,\s*["']([a-z_]+\.[a-z0-9_.]+)["']""")
    missing = []
    for fn in ("run.py", "nest.py", "report.py"):
        with open(os.path.join(here, fn), encoding="utf-8") as f:
            src = f.read()
        ids = set(pat.findall(src))
        ids |= set(re.findall(r'\("((?:run|queue|report|nestws|web)\.[a-z0-9_.]+)", "', src))
        missing += [i for i in ids if i not in cat.strings]
    assert not missing, sorted(set(missing))
