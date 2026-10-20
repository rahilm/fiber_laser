"""Display units in the Design area (canvas rulers and status bar, Properties position/size, the
parameter grids, the lead / kerf / micro-joint / CAM / draw / nest dialogs, the layer table, the
part list, jog steps): no metric text in imperial, and edits in imperial store mm."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from mlaser.services import units as U  # noqa: E402
from mlaser.ui.units_qt import QuantitySpinBox  # noqa: E402

from units_sweep import offenders  # noqa: E402

IN = 25.4


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch):
    from wiring import real_window
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window(user_mode="service")
    w.resize(1280, 800)
    w.show()
    app.processEvents()
    yield w
    w._test_no_dialogs = True
    w.config.set("units.system", "metric")
    w.close()


def _type(sb, text):
    sb.setFocus()
    sb.lineEdit().selectAll()
    sb.lineEdit().clear()
    QTest.keyClicks(sb.lineEdit(), text)
    sb.lineEdit().editingFinished.emit()
    sb.editingFinished.emit()


def _rect(win, w=101.6, h=50.8):
    """A document with one w x h mm rectangle at (10, 20) (default 4 x 2 in); returns the part."""
    from mlaser.model import Contour, Document, Line, Part
    pts = [(10.0, 20.0), (10.0 + w, 20.0), (10.0 + w, 20.0 + h), (10.0, 20.0 + h)]
    win.set_document(Document(parts=[Part([Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)],
                                          name="r")]))
    return win.document.parts[0]


#: dialog texts that stay metric on purpose (see docs/dev/units-audit.md)
DESIGN_ALLOW = (
    # UN.GasPressureUnit (Options > ManuParam): the original's choice of the unit the layers' gas
    # pressures are STORED in (bar / MPa): data semantics, not a display unit
    "'MPa'",
)


# --------------------------------------------------------------------------- canvas
def test_ruler_steps_are_nice_inch_steps():
    from mlaser.ui.canvas import ruler_step
    imp = U.Units("imperial")
    seen = []
    for ppm in (0.02, 0.05, 0.2, 1.0, 4.0, 20.0, 100.0):
        major, minor = ruler_step(ppm, imp)
        inches = round(major / IN, 9)
        assert inches in (1 / 64, 1 / 32, 1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 3, 6, 12, 24, 36, 60, 120, 240,
                          600, 1200, 2400, 6000, 12000) or abs(inches - round(inches * 64) / 64) < 1e-9
        assert major * ppm >= 56                          # labels never crowd
        assert major % minor < 1e-9 or abs(major % minor - minor) < 1e-9
        seen.append(inches)
    assert seen == sorted(seen, reverse=True)
    major, _ = ruler_step(1.0, U.Units("metric"))
    assert major == 100.0                                 # 1-2-5 mm steps in metric


def test_canvas_rulers_and_status_follow_the_units(win, app):
    c = win.canvas
    c.zoom_fit()
    app.processEvents()
    xs, _ys, unit = c.ruler_labels()
    assert unit == "mm" and xs
    win.config.set("units.system", "imperial")
    app.processEvents()
    xs, ys, unit = c.ruler_labels()
    assert unit == "in" and xs and ys
    major, _ = c.ruler_steps()
    assert round(major / IN, 9) in (1 / 4, 1 / 2, 1, 2, 3, 6, 12, 24, 36, 60, 120, 240)
    c.grab()                                              # paints the rulers (no exception)
    # coordinate readout in the status bar
    c.mouseMoved.emit(25.4, 50.8)
    assert win.st_da.text() == "X: 1.000 Y: 2.000 in"
    win.config.set("units.system", "metric")
    assert win.st_da.text() == "X: 25.40 Y: 50.80 mm"


# --------------------------------------------------------------------------- properties
def test_properties_position_size_edit_in_imperial_stores_mm(win, app):
    part = _rect(win)
    win.canvas.select_parts([len(win.document.parts) - 1])
    app.processEvents()
    win.config.set("units.system", "imperial")
    props = None
    from mlaser.ui.properties.panel import PropertiesPanel
    for p in win.findChildren(PropertiesPanel):
        props = p
    assert props is not None
    props.refresh() if hasattr(props, "refresh") else None
    app.processEvents()
    assert isinstance(props.w, QuantitySpinBox)
    assert props.w.text() == "4.000 in" and props.x.text() == "0.394 in"
    assert props.x.value() == pytest.approx(10.0)       # the exact mm value, not 0.394 in
    _type(props.w, "5")                                   # 5 in wide
    app.processEvents()
    x0, y0, x1, y1 = part.bbox() if part in win.document.parts else win.document.parts[-1].bbox()
    assert x1 - x0 == pytest.approx(5 * IN, abs=1e-6)


# --------------------------------------------------------------------------- dialogs
def _capture(monkeypatch, store):
    def exec_(self, *a):
        store.append(self)
        return 0
    monkeypatch.setattr(QDialog, "exec", exec_)


def test_design_dialogs_show_no_metric_text_in_imperial(win, app, monkeypatch):
    win.config.set("units.system", "imperial")
    got: list = []
    _capture(monkeypatch, got)
    _rect(win)
    win.canvas.select_parts([len(win.document.parts) - 1])
    got.append(win.settings_dialog())          # app.options: the Settings window (modeless)
    cam = win.extension("ext_cam")
    cam.exec_dialogs = True
    for m in ("array_dialog", "circular_array_dialog", "sheet_array_dialog", "cleanup_dialog", "fillet_auto",
              "unload", "alpha", "rect_share", "flycut_circle", "flycut_line", "linear_fill", "fill_circle"):
        getattr(cam, m)()
    craft = win.extension("ext_craft")
    craft.exec_dialogs = True
    for m in ("bridge", "mj_manual"):         # gap / overcut use the Properties field (no dialog)
        getattr(craft, m)()
    craft._engrave_dialog()
    draw = win.extension("ext_draw")
    draw.exec_dialogs = True
    for m in ("text_dialog", "qr_dialog", "adv_text", "advert_text", "merge_dialog", "size_dialog",
              "fine_tune_dialog"):
        getattr(draw, m)()
    nest = win.extension("ext_nest")[0]
    nest.params_dialog()
    nest.add_std_sheet()
    assert len(got) >= 20
    bad = {}
    for d in got:
        if type(d).__name__ == "SettingsDialog":
            # the unit choosers list every unit by name; every other row must show imperial
            widgets = [r for k, r in d.rows.items() if not k.startswith("units.")]
            hits = [h for r in widgets for h in offenders(r, visible_only=False)
                    if not any(a in h for a in DESIGN_ALLOW)]
        else:
            hits = [h for h in offenders(d, visible_only=False) if not any(a in h for a in DESIGN_ALLOW)]
        if hits:
            bad[d.windowTitle()] = hits
    assert not bad, bad


def test_param_grid_edit_in_imperial_stores_mm(win, app):
    from mlaser.ui import dialogs
    win.config.set("units.system", "imperial")
    vals: dict = {}
    d = dialogs.ParamGridDialog(win.tr_, "mf129", ["ManuParam"], vals, win)
    mm = [(k, w) for k, w in d._editors.items() if isinstance(w, QuantitySpinBox) and w.kind() == "length"
          and d._settings[k].kind == "float"]
    assert mm, "no length parameter in the grid"
    key, w = mm[0]
    before = w.value()
    assert w.suffix() == " in"
    assert not [h for h in offenders(d, visible_only=False) if not any(a in h for a in DESIGN_ALLOW)]
    d._ok()                                               # OK without edits: nothing stored
    assert key not in vals
    lo, hi = w.minimum(), w.maximum()
    target = min(max(1.0 * IN, lo), hi)
    _type(w, f"{target / IN:g} in")
    d._ok()
    assert vals[key] == pytest.approx(target, abs=1e-9) or before == target


def test_properties_leads_and_kerf_store_mm_in_imperial(win, app):
    """Imperial: select a part, Properties' lead length shows inches; typing 0.25" stores
    6.35 mm in the part's craft (leads and kerf are edited only there: the Lead / Kerf dialogs
    are gone, the commands open these fields)."""
    from mlaser.ui.units_qt import QuantitySpinBox
    win.config.set("units.system", "imperial")
    part = _rect(win)
    win.canvas.select_parts([0])
    app.processEvents()
    win.commands.run("craft.lead")                        # opens Process › Properties › Leads
    pp = win.extension("ext_process").properties
    kind, ang, ln, rad = pp.lead_w["in"]
    assert isinstance(ln, QuantitySpinBox) and ln.suffix() == " in"
    assert ln.text().endswith(" in") and "mm" not in ln.text()
    kind.setCurrentIndex(1)
    _type(ln, '0.25"')
    app.processEvents()
    pp._commit_leads()
    part = win.document.parts[0]
    assert part.craft.lead_in.kind == 1 and part.craft.lead_in.length == pytest.approx(6.35, abs=1e-9)
    assert ln.text() == "0.25 in" or ln.text().startswith("0.250")
    assert isinstance(pp.kerf_w, QuantitySpinBox) and pp.kerf_w.suffix() == " in"
    pp.kerf_kind.setCurrentIndex(pp.kerf_kind.findData(1))
    _type(pp.kerf_w, "0.004")
    pp._commit_kerf()
    assert win.document.parts[0].craft.kerf_width == pytest.approx(0.004 * IN, abs=1e-9)
    for w in (pp.mj_len, pp.overcut):
        assert isinstance(w, QuantitySpinBox) and w.suffix() == " in"


# --------------------------------------------------------------------------- panels
def test_layer_table_and_part_list_in_imperial(win, app):
    _rect(win)
    win.layer_panel.set_document(win.document)
    win.part_list.set_document(win.document)
    win.config.set("units.system", "imperial")
    app.processEvents()
    t = win.layer_panel.table
    heads = [t.horizontalHeaderItem(c).text() for c in range(t.columnCount())]
    assert any(h.endswith("(in/min)") for h in heads) and any(h.endswith("(psi)") for h in heads)
    col = [a for _, a in win.layer_panel.COLS].index("cut_height")
    li = win.layer_panel._row_layer(0)
    it = t.item(0, col)
    assert not it.flags() & Qt.ItemIsEditable             # a legend: edited in the layer inspector
    win.edit_layer(li)                                    # double-click: Process › Layers
    insp = win.extension("ext_process").workspace.inspector
    fld = insp.form.fields["GP.CutHeight"].widget
    assert fld.suffix() == " in"
    _type(fld, "0.04")                                    # 0.04 in
    app.processEvents()
    assert win.document.layers[li].cut_height == pytest.approx(0.04 * IN)
    hdr = win.part_list.headerItem().text(4)
    assert hdr.endswith("(in)")
    assert win.part_list.topLevelItem(win.part_list.topLevelItemCount() - 1).text(4) == "4.000×2.000"


def test_jog_steps_in_inches_never_longer_than_metric(win, app):
    ctl = win.control
    metric = ctl.step_values()
    win.config.set("units.system", "imperial")
    imperial = ctl.step_values()
    assert len(metric) == len(imperial) == ctl.step.count()
    assert all(i <= m + 1e-9 for i, m in zip(imperial, metric))
    assert ctl.step.itemText(5) == "1 in"
    assert ctl.step.currentData() == pytest.approx(imperial[ctl.step.currentIndex()])
    assert isinstance(ctl.bf_len, QuantitySpinBox) and ctl.bf_len.value() == 10.0


def test_size_presets_follow_units(win, app):
    act = win.ribbon.actions["xform.size_100mm"]
    assert act.text() == "100 mm"
    win.config.set("units.system", "imperial")
    assert act.text() == "4 in"
    from mlaser.ui.mainwindow import size_preset
    assert size_preset("xform.size_100mm") == pytest.approx(4 * IN)


def test_canvas_point_conversion_sanity(win):
    # the rulers map scene mm to the viewport like the canvas does
    c = win.canvas
    p = c.mapFromScene(QPointF(0, 0))
    assert isinstance(p.x(), int)
