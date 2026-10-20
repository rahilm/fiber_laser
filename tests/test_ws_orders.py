"""Import & orders through the real window wiring: the import review after File > Open (one
undo step), Nest > From drawing with recognised parts, multi-file and CSV intake, orders into
the Nest and through the queue to the report, the load-sheet card, and the material gate with
its audited override."""
from __future__ import annotations

import logging
import shutil
import time
from dataclasses import asdict
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part  # noqa: E402

DXF = Path(__file__).parent / "data" / "dxf"


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


def gap_dxf(path, gap=0.2):
    import ezdxf
    d = ezdxf.new()
    msp = d.modelspace()
    msp.add_lwpolyline([(0, 0), (60, 0), (60, 40), (0, 40), (0, gap)])      # open by ``gap``
    msp.add_circle((30, 20), 5)
    d.saveas(path)
    return str(path)


def tech_layer(material="Mild steel S235", thickness=3.0, li=1):
    from mlaser.services.technology import Technology, TechKey, apply_to_layer
    t = Technology(TechKey(material, thickness, "O2", "1.0 double"))
    t.id, t.version = 1, 1
    return apply_to_layer(LayerParams(index=li), t)


def rect(x0, y0, w, h, layer=1):
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    return Part([Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)], Craft(layer=layer))


# ---------------------------------------------------------------- import review
def test_registered(win):
    orders = win.extension("orders")
    assert orders is not None and "orders.orders" in win.workspaces
    for cmd in ("import.review", "parts.review", "orders.import_csv", "orders.add_files", "orders.template",
                "orders.to_nest", "workspace.orders"):
        assert cmd in win.commands
    assert orders in win.preflight_contributors
    assert orders.gate.start_guard in win.jobs.start_guards


def test_import_review_runs_after_open_and_fixes_in_one_undo_step(win, app, tmp_path):
    seen = []
    win.hooks.add("document_imported", lambda path, doc: seen.append((path, len(doc.parts))))
    path = gap_dxf(tmp_path / "gap.dxf")
    assert win.load_path(path)
    assert seen == [(path, 2)]
    orders = win.extension("orders")
    dlg = orders.show_review()
    rep = dlg.report
    (f,) = rep.of("open_contour")
    assert f.value == pytest.approx(0.2) and f.fixable
    assert dlg.list.count() == 1 and dlg.fix_btn.isEnabled()
    done = dlg.preview()                          # nothing changes yet
    assert done["closed"] == 1 and not any(c.closed for p in win.document.parts for c in p.contours
                                           if len(c.segments) > 1)
    dlg.fix()
    assert all(c.closed for p in win.document.parts for c in p.contours)
    assert dlg.report.ok and "Fixed" in dlg.result.text()
    win.undo()                                    # one undo step restores the drawing
    assert sum(1 for p in win.document.parts for c in p.contours if not c.closed) == 1
    dlg.close()


def test_import_review_can_be_switched_off(win, tmp_path):
    orders = win.extension("orders")
    orders.settings["review_after_import"] = False
    called = []
    orders.show_review = lambda: called.append(1)
    assert win.load_path(gap_dxf(tmp_path / "gap.dxf"))
    assert not called


def test_preflight_offers_repair_for_open_contours(win, tmp_path):
    from mlaser.machine.preflight import PreflightReport, WARNING
    from mlaser.ui.safety_ui import PreflightDialog
    orders = win.extension("orders")
    rep = PreflightReport("start")
    rep.add("open_contour", WARNING, "44 open contours in 22 parts (first: part 2 contour 1)")
    (label, fn), = orders.actions(rep)
    assert label.startswith("Repair")
    opened = []
    orders.show_review = lambda: opened.append(1)
    d = PreflightDialog(rep, win, actions=[(label, orders.show_review)])
    d._act(orders.show_review)
    assert opened and d.result() == QDialog.Rejected       # Repair replaces the start


# ---------------------------------------------------------------- recognition / nest intake
def test_from_drawing_nests_real_parts(win):
    assert win.load_path(str(DXF / "gear.dxf"))
    assert len(win.document.parts) == 2
    nest = win.extension("nest")
    nest.job.parts.clear()
    assert nest.add_from_drawing() == 1
    (np_,) = nest.job.parts
    assert np_.name == "gear" and np_.quantity == 1 and len(np_.parts) == 2   # the bore stays in
    assert nest.parts.rowCount() == 1


def test_parts_dialog_merge_split(win):
    from mlaser.ui.workspaces.orders import PartsDialog
    doc = Document(parts=[rect(0, 0, 10, 10), rect(50, 0, 10, 10), rect(100, 0, 30, 10)])
    d = PartsDialog(win.extension("orders"), doc)
    assert d.list.count() == 2 and "3 parts (2 different)" in d.head.text()
    d.list.selectAll()
    d.merge()
    assert d.list.count() == 1 and d.partset.groups[0].quantity == 1
    d.list.item(0).setSelected(True)
    d.split()
    assert d.list.count() == 3
    d.mode.setCurrentIndex(d.mode.findData("file"))
    assert d.list.count() == 1


def test_nest_imports_several_files_and_a_parts_list(win, tmp_path):
    nest = win.extension("nest")
    nest.job.parts.clear()
    n = nest.import_part([str(DXF / "gear.dxf"), str(DXF / "inserts.dxf")])
    assert n == 3
    assert [(p.name, p.quantity) for p in nest.job.parts] == [("gear", 1), ("inserts-1", 2), ("inserts-2", 2)]
    assert "5 parts from 2 files" in nest.status.text()
    shutil.copy(DXF / "gear.dxf", tmp_path / "gear.dxf")
    (tmp_path / "list.csv").write_text("file,qty\ngear.dxf,4\nmissing.dxf,1\n")
    nest.import_part(str(tmp_path / "list.csv"))
    orders = win.extension("orders")
    assert [(x.name, x.quantity) for x in orders.book.lines] == [("gear", 4)]
    assert "line 3" in orders.workspace.errors.text()
    legacy = win.extension("ext_nest")
    legacy = legacy[0] if isinstance(legacy, tuple) else legacy
    assert not legacy.panel.isVisible() if hasattr(legacy, "panel") else True   # no legacy dock


def test_template_download(win, tmp_path):
    p = win.extension("orders").save_template(str(tmp_path / "t.csv"))
    assert open(p, encoding="utf-8").read().startswith("file,quantity,material,thickness,customer,order,due,priority")


# ---------------------------------------------------------------- orders -> nest -> queue -> run -> report
def _order_files(tmp_path):
    for n in ("gear", "inserts2", "basic_r2000"):
        shutil.copy(DXF / f"{n}.dxf", tmp_path / f"{n}.dxf")
    p = tmp_path / "order.csv"
    p.write_text("file,quantity,material,thickness,customer,order,due,priority,recognition\n"
                 "gear.dxf,6,mild steel s235,3,ACME,4711,2026-10-29,1,\n"
                 "inserts2.dxf,10,Mild steel S235,3,Beta,B-1,,0,file\n"
                 "basic_r2000.dxf,8,Mild steel S235,3,Beta,B-2,,0,file\n"
                 "gear.dxf,2,Stainless 1.4301,2,Gamma,G-9,,0,\n")
    return str(p)


def test_orders_into_nest_queue_card_and_report(win, app, tmp_path, caplog):
    from mlaser.machine.demo import DemoController
    orders = win.extension("orders")
    res = orders.import_csv(_order_files(tmp_path))
    assert res.ok and len(orders.book.lines) == 4
    ws = orders.workspace
    assert ws.table.rowCount() == 4 and ws.group.count() == 2
    nest = win.extension("nest")
    parts = orders.to_nest(key=("mild steel s235", 3.0))
    assert [(p.name, p.quantity, p.priority) for p in nest.job.parts] == \
        [("gear", 6, 1), ("inserts2", 10, 0), ("basic_r2000", 8, 0)]
    assert parts[0].extra["order"]["order"] == "4711"
    # one place for material and thickness: the database name, and the technology on the layer
    assert nest.material.text() == "Mild steel S235" and nest.thickness.value() == 3.0
    ref = win.document.layers[0].extra["technology"]["key"]
    assert (ref["material"], ref["thickness_mm"]) == ("Mild steel S235", 3.0)
    assert win.extension("ext_process").workspace.picked().key.material == "Mild steel S235"
    nest.sheet_w.setValue(1000)
    nest.sheet_h.setValue(500)
    nest.sheet_n.setValue(3)
    nest.add_sheet()
    nest.time_limit.setValue(5)
    assert nest.start() and wait(app, lambda: not nest.service.running, 120)
    assert nest.run.placed == 24
    (e,) = nest.send_to_queue()
    assert e.material == "Mild steel S235" and e.thickness_mm == 3.0
    assert {(r["order"], r["part"], r["qty"]) for r in e.extra["orders"]} == \
        {("4711", "gear", 6), ("B-1", "inserts2", 10), ("B-2", "basic_r2000", 8)}
    # the Run screen: the load card while the queue waits, with the next sheet's picture
    t = [0.0]
    ctl = DemoController(clock=lambda: t[0])
    win.set_controller(ctl)
    ctl.connect()
    assert ctl.arm()
    win.set_document(Document(parts=[rect(0, 0, 5, 5, layer=0)], layers={0: tech_layer(li=0)}))
    run = win.extension("run")
    win.commands.run("workspace.run")
    scr = run.screen
    scr.window().resize(1280, 800)
    pump(app, 0.2)
    scr.toggle_queue()
    scr.refresh()
    card = scr.load_card
    assert card.isVisible() and card.material.text() == "Mild steel S235 3 mm"
    assert "1000 × 500 mm" in card.sheet.text() and "sheet 1/1" in card.sheet.text()
    assert "ACME · 4711: 6 × gear" in card.details.text() and "Nozzle: 1.0 double" in card.details.text()
    assert not card.warning.isVisible() and not card.picture.pixmap().isNull()
    shown, _sheet = run.view_document()
    assert shown is run.entry_document(e) and len(shown.parts) > 2     # the sheet, not the drawing
    # cut it: the report keeps the order references
    scr.buttons["confirm_sheet"].click()
    if win.jobs.last_run is not None:
        win.jobs.settle(win.jobs.last_run, timeout=60)
    assert wait(app, lambda: run.runner.state == "running", 20), run.runner.message
    assert not card.isVisible()
    t[0] += 1e6
    ctl.state()
    assert wait(app, lambda: run.runner.state != "running", 20)
    (rec,) = win.extension("report").ledger.jobs()
    assert {(r["order"], r["qty"]) for r in rec.orders} == {("4711", 6), ("B-1", 10), ("B-2", 8)}
    prog = orders.progress()
    gear_line = orders.book.lines[0]
    assert prog[gear_line.id] == 6
    ws.refresh()
    assert ws.table.item(0, 8).text() == "6/6"


def test_material_mismatch_blocks_the_queue_sheet_and_the_override_is_audited(win, app, tmp_path, caplog):
    from mlaser.formats.chf import save_chf
    from mlaser.machine.demo import DemoController
    t = [0.0]
    ctl = DemoController(clock=lambda: t[0])
    win.set_controller(ctl)
    ctl.connect()
    assert ctl.arm()
    path = str(tmp_path / "sheet.chf")
    save_chf(Document(parts=[rect(0, 0, 100, 100)]), path)
    layers = {"1": asdict(tech_layer("Mild steel S235", 3.0))}
    run = win.extension("run")
    run.queue.add(path, 1, material="Aluminium", thickness_mm=1.0, extra={"layers": layers})
    win.set_document(Document(parts=[rect(0, 0, 5, 5)], layers={1: tech_layer()}))
    win.commands.run("workspace.run")
    pump(app, 0.2)
    scr = run.screen
    scr.toggle_queue()
    scr.refresh()
    assert scr.load_card.warning.isVisible() and "Aluminium 1 mm" in scr.load_card.warning.text()
    with caplog.at_level(logging.INFO, logger="mlaser.audit"):
        scr.confirm_sheet()
        if win.jobs.last_run is not None:
            win.jobs.settle(win.jobs.last_run, timeout=30)
        pump(app, 0.3)
        assert not ctl.job_active() and run.runner.state == "confirm"     # blocked
        doc = win.document
        assert doc.extra["sheet"]["material"] == "Aluminium"
        ok, msg = win.jobs.check_start("process")
        assert not ok and "Mild steel S235 3 mm" in msg
        assert win.jobs.check_start("dry_run") == (True, "")
        # the pre-flight shows it as a blocking issue
        from mlaser.ui.safety_ui import contribute_preflight
        rep = contribute_preflight(win, ctl.preflight("start", doc), "start", doc, True)
        assert "material_mismatch" in {i.code for i in rep.errors}
        # the override: confirmed by the operator, audited, then the sheet runs
        orders = win.extension("orders")
        assert orders.gate.override("checked: the 3 mm steel sheet is on the table")
        scr.confirm_sheet()
        if win.jobs.last_run is not None:
            win.jobs.settle(win.jobs.last_run, timeout=30)
        assert wait(app, lambda: run.runner.state == "running", 20), run.runner.message
    events = [r.audit["event"] for r in caplog.records if hasattr(r, "audit")]
    assert "material_check" in events and "material_override" in events
    refused = [r.audit for r in caplog.records if getattr(r, "audit", {}).get("event") == "material_check"]
    assert refused[0]["result"] == "refused" and refused[-1]["result"] == "overridden"
    ctl.stop()


def test_override_dialog_turns_the_error_into_a_warning(win, monkeypatch):
    from mlaser.machine.preflight import ERROR, PreflightReport
    from mlaser.ui.safety_ui import override_preflight
    orders = win.extension("orders")
    asked = []

    def fake(issues, parent):
        asked.append([i.code for i in issues])
        return True
    monkeypatch.setattr(orders, "override", fake)
    rep = PreflightReport("start")
    rep.add("material_mismatch", ERROR, "layer 1 has the technology for Aluminium 1 mm")
    rep.add("not_homed", ERROR, "X/Y are not homed")
    out = override_preflight(win, rep)
    assert asked == [["material_mismatch"]]
    assert [i.code for i in out.errors] == ["not_homed"]                # other errors stay
    assert out.warnings[0].message.startswith("overridden:")
    monkeypatch.setattr(orders, "override", lambda issues, parent: False)
    assert [i.code for i in override_preflight(win, rep).errors] == ["material_mismatch", "not_homed"]


def test_copy_sheet_to_drawing_declares_the_sheet(win, app):
    from mlaser.nest import NestPart
    nest = win.extension("nest")
    nest.job.parts.clear()
    nest.job.sheets.clear()
    nest.job.add_part(NestPart([rect(0, 0, 100, 50)], name="p", quantity=2))
    nest.sheet_w.setValue(500)
    nest.sheet_h.setValue(300)
    nest.sheet_n.setValue(1)
    nest.add_sheet()
    nest.material.setText("Mild steel S235")
    nest.thickness.setValue(3.0)
    nest.time_limit.setValue(5)
    assert nest.start() and wait(app, lambda: not nest.service.running, 60)
    assert nest.to_drawing()
    assert win.document.extra["sheet"] == {"material": "Mild steel S235", "thickness_mm": 3.0, "source": "nest",
                                           "entry": "", "sheet_no": 0, "size": "", "remnant_id": ""}
