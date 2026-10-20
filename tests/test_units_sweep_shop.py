"""Imperial sweep of the shop-floor workspaces (Nest with a run result, Orders with order lines,
Report with jobs, the cutting-strategy tab, the import review, the cost rates, labels, the
practice text) and edits in imperial through the widgets that must store mm
(docs/user/units.md)."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from units_sweep import metric_hits, offenders  # noqa: E402

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
    w.show()
    yield w
    w.config.set("units.system", "metric")
    w._test_no_dialogs = True
    w.close()


def wait(app, pred, timeout=60.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


def pump(app, n=6):
    for _ in range(n):
        app.processEvents()


def type_into(sb, text):
    """Type ``text`` into a QuantitySpinBox like the operator does (then leave the field)."""
    sb.lineEdit().selectAll()
    sb.lineEdit().clear()
    QTest.keyClicks(sb.lineEdit(), text)
    sb.lineEdit().editingFinished.emit()
    sb.editingFinished.emit()


def _orders_csv(tmp_path):
    for n in ("gear", "inserts2"):
        shutil.copy(DXF / f"{n}.dxf", tmp_path / f"{n}.dxf")
    p = tmp_path / "order.csv"
    p.write_text("file,quantity,material,thickness,customer,order,due,priority\n"
                 "gear.dxf,2,Mild steel S235,3,ACME,4711,2026-10-29,1\n"
                 "inserts2.dxf,3,Mild steel S235,3,Beta,B-1,,0\n")
    return str(p)


def _job(**kw):
    from mlaser.report import ledger as LG
    now = time.time()
    rec = LG.ProductionRecord(now - 120, now, "bracket.dxf", "", "process", True, "done", 120.0, 110.0,
                              12.5, 3.0, 14, 1000.0, 500.0, "Mild steel S235", 3.0, 0.62)
    for k, v in kw.items():
        setattr(rec, k, v)
    return rec


def _check(where, widget):
    bad = offenders(widget)
    assert not bad, f"metric text in imperial ({where}):\n  " + "\n  ".join(bad)


def test_shop_workspaces_show_no_metric_text_in_imperial(win, app, tmp_path):
    win.config.set("units.system", "imperial")
    # ---- orders with lines, into the nest
    orders = win.extension("orders")
    assert orders.import_csv(_orders_csv(tmp_path)).ok
    win.show_workspace("orders")
    pump(app)
    _check("orders", orders.workspace)
    assert orders.workspace.table.horizontalHeaderItem(3).text().endswith("(in)")
    assert orders.workspace.table.item(0, 3).text() == "0.118"
    assert " in · " in orders.workspace.group.itemText(0)
    # ---- nest with a run result, the options drawer open
    nest = win.extension("nest")
    orders.to_nest(key=("mild steel s235", 3.0))
    nest.sheet_w.setValue(1000)
    nest.sheet_h.setValue(500)
    nest.add_sheet()
    nest.time_limit.setValue(3)
    assert nest.start() and wait(app, lambda: not nest.service.running, 120)
    assert nest.run is not None and nest.run.placed > 0
    win.show_workspace("nest")
    nest.show_options(True)
    pump(app)
    _check("nest", nest)
    assert "39.37 × 19.69 in" in nest.sheet_title.text()
    assert nest.compare.horizontalHeaderItem(3).text() == "Cut (ft)"
    for i in range(nest.stock.count()):
        assert not metric_hits(nest.stock.item(i).text())
    for i in range(nest.results.count()):
        assert not metric_hits(nest.results.item(i).text())
    for r in range(nest.compare.rowCount()):
        assert not metric_hits(nest.compare.item(r, 0).toolTip())
    nest.options.setCurrentIndex(1)
    pump(app)
    _check("nest strategy options", nest)
    # ---- report with jobs (every tab)
    rep = win.extension("report")
    rep.ledger.add_job(_job())
    rep.ledger.add_job(_job(name="plate.dxf", cut_m=2.0, thickness_mm=6.35))
    win.show_workspace("report")
    pump(app)
    ws = rep.widget
    ws.refresh()
    for i in range(ws.tabs.count()):
        ws.tabs.setCurrentIndex(i)
        pump(app, 2)
        _check(f"report tab {i}", ws)
    assert ws.tiles["cut"].text() == "47.6 ft"
    assert ws.jobs.horizontalHeaderItem(5).text() == "Cut (ft)"
    # ---- the cutting-strategy tab of Process
    proc = win.extension("ext_process")
    win.show_workspace("process")
    proc.workspace.tabs.setCurrentWidget(proc.strategy)
    pump(app)
    _check("cutting strategy", proc.strategy)
    assert "pitch 3.937 in" in proc.strategy.bed_label.text()
    # ---- the import review and the cost rates
    dlg = orders.show_review()
    dlg.show()
    pump(app)
    _check("import review", dlg)
    dlg.close()
    # the cost rates' home: Settings › Costing (process.rates opens it)
    win.run("process.rates")
    rd = win._settings_window
    pump(app)
    _check("rates", rd.pages["costing"])
    assert rd.rows["costing.gas_o2_per_m3"].editor.suffix().endswith("/ft³")
    rd.close()


def test_live_switch_relabels_the_shop_workspaces(win, app):
    rep = win.extension("report")
    rep.ledger.add_job(_job())
    win.show_workspace("report")
    rep.widget.refresh()
    pump(app)
    assert rep.widget.tiles["cut"].text() == "12.5 m"
    assert offenders(rep.widget)                        # metric shows metric (the sweep is not vacuous)
    win.config.set("units.system", "imperial")
    pump(app)
    assert rep.widget.tiles["cut"].text() == "41.0 ft"
    proc = win.extension("ext_process")
    assert "in" in proc.strategy.bed_label.text() and not metric_hits(proc.strategy.bed_label.text())
    win.config.set("units.system", "metric")
    pump(app)
    assert rep.widget.tiles["cut"].text() == "12.5 m"
    assert "pitch 100 mm" in proc.strategy.bed_label.text()


# --------------------------------------------------------------------------- edits store mm
def test_nest_sheet_size_and_spacing_edited_in_inches_store_mm(win, app):
    win.config.set("units.system", "imperial")
    nest = win.extension("nest")
    type_into(nest.sheet_w, "120")
    type_into(nest.sheet_h, "60 in")
    type_into(nest.spacing, '1/8"')
    assert nest.sheet_w.value() == pytest.approx(3048.0)
    assert nest.sheet_h.value() == pytest.approx(1524.0)
    nest._apply_params()
    assert nest.job.params.spacing == pytest.approx(3.175)
    assert nest.job.params.kerf == pytest.approx(0.2, abs=1e-12)     # no layer kerf: the default
    margin = nest.margin.value()
    nest._apply_params()
    assert nest.job.params.margin == margin == 5.0          # untouched: exactly the mm value
    nest.add_sheet()
    sh = nest.job.sheets[-1]
    assert (sh.width, sh.height) == (pytest.approx(3048.0), pytest.approx(1524.0))


def test_orders_join_tolerance_and_thickness_edited_in_inches_store_mm(win, app, tmp_path):
    win.config.set("units.system", "imperial")
    orders = win.extension("orders")
    dlg = orders.show_review()
    type_into(dlg.join_tol, "0.004")
    type_into(dlg.close_gap, "0.5 mm")
    o = dlg.options()
    assert o.join_tol == pytest.approx(0.1016) and o.close_gap == pytest.approx(0.5)
    dlg.close()
    assert orders.import_csv(_orders_csv(tmp_path)).ok
    ws = orders.workspace
    line = orders.book.lines[0]
    ws.table.item(0, 3).setText("1/8")
    assert line.thickness_mm == pytest.approx(3.175)
    ws.refresh()
    ws.table.item(0, 3).setText(ws.table.item(0, 3).text())   # retyping the shown text keeps the value
    assert line.thickness_mm == pytest.approx(3.175, abs=0)
    ws.table.item(1, 3).setText("4 mm")
    assert orders.book.lines[1].thickness_mm == pytest.approx(4.0)


def test_cutting_strategy_bed_edited_in_inches_stores_mm(win, app):
    win.config.set("units.system", "imperial")
    from mlaser.ui.homes import edit_in, editors
    proc = win.extension("ext_process")
    # the bed's one home: Machine › Setup › Bed (the cutting-strategy panel shows it read-only:
    # its link goes there)
    assert proc.strategy.bed_link.vid == "cfg:bed.slat_pitch"
    pitch = edit_in(win, "cfg:bed.slat_pitch")
    app.processEvents()
    width = editors(win, "cfg:bed.slat_width")
    assert pitch is not None and len(editors(win, "cfg:bed.slat_pitch")) == 1 and len(width) == 1
    type_into(pitch, "4")
    type_into(width[0], "0.125 in")
    from mlaser.ui.workspaces.machine import SettingsPanel
    panel = pitch.parentWidget()
    while not isinstance(panel, SettingsPanel):
        panel = panel.parentWidget()
    panel.apply()                                            # Machine › Setup applies on Apply
    assert win.config.get("bed.slat_pitch") == pytest.approx(101.6)
    assert win.config.get("bed.slat_width") == pytest.approx(3.175)
    assert not win.config.is_set("bed.slat_offset")           # never touched: never written
    assert "4 in" in proc.strategy.bed_label.text()
    # the lead clearance is a recipe rule: its home is the Library editor
    ed = proc.workspace.library.editor
    type_into(ed.clearance, "0.02")
    assert ed.current().rules["lead_check"]["clearance_mm"] == pytest.approx(0.508)


def test_report_rate_per_square_foot_is_stored_per_square_metre(win, app):
    from mlaser.ui.units_qt import QuantitySpinBox
    win.config.set("units.system", "imperial")
    rep = win.extension("report")
    sb = QuantitySpinBox("per_large_area", minimum=0, maximum=1e6)
    sb.setValue(float(win.config.get("costing.material_per_m2")))
    sb.valueChanged.connect(lambda v: win.config.set("costing.material_per_m2", v))
    type_into(sb, "1")                                         # 1 per ft²
    assert float(win.config.get("costing.material_per_m2")) == pytest.approx(1 / 0.09290304)
    assert rep.ledger.rates.material_per_m2 == pytest.approx(10.7639104, rel=1e-7)
    sb2 = QuantitySpinBox("per_long_length", minimum=0, maximum=1e6)
    type_into(sb2, "0.3048")                                   # per ft -> per m
    assert sb2.value() == pytest.approx(1.0)


def test_rates_dialog_edits_in_imperial_store_metric(win, app):
    """Settings › Costing (the rates' home) takes imperial prices and stores metric ones."""
    from mlaser.ui.settings_dialog import SettingsDialog
    win.config.set("units.system", "imperial")
    rd = SettingsDialog(win, instant=True)
    rd.show()
    type_into(rd.rows["costing.gas_n2_per_m3"].editor, "1")      # 1 per ft³
    type_into(rd.rows["costing.material_per_kg"].editor, "2")    # 2 per lb
    before = float(win.config.get("costing.machine_per_h"))
    assert float(win.config.get("costing.gas_n2_per_m3")) == pytest.approx(1 / 0.028316846592)
    assert float(win.config.get("costing.material_per_kg")) == pytest.approx(2 / 0.45359237)
    assert float(win.config.get("costing.machine_per_h")) == before
    rd.close()


# --------------------------------------------------------------------------- text outside widgets
def test_cost_notes_labels_and_practice_text_in_imperial(win, app, tmp_path):
    from mlaser.labels import labels as L
    from mlaser.services import costing, units as U
    from mlaser.ui.ext_practice import _t
    win.config.set("units.system", "imperial")
    assert costing._sheet_size(3000, 1500) == "118.11 × 59.06 in"
    t = _t(win, "practice.step.load.text")
    assert "11.811 × 7.874 in" in t and not metric_hits(t)
    u = U.current()
    assert L._mat("S235", 3.175, u) == "S235 0.125 in" and L._mat("S235", 3.0) == "S235 3 mm"
    assert L._size(62, 29, u) == "2.44 x 1.14 in" and L._size(62, 29) == "62 x 29 mm"
    from mlaser.report import printing
    from mlaser.report.records import parse_line
    rec = parse_line("a.dxf,154.40x103.41 mm,2026-04-13 15:52:07,1.10 m,1.07 m,3,0分43秒,29.76 Sec,10.46 Sec,1.00 Sec")
    rows = printing.rows_for(rec, "en", u)
    assert rows[1][1] == "6.08 × 4.07 in" and rows[2][1] == "3.61 ft"
    assert printing.rows_for(rec, "en")[2][1] == "1.10 m"      # without units: report.exe's text
