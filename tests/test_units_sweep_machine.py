"""Imperial sweep of the Run and Machine workspaces and the machine dialogs and wizards, and
"edit in imperial through the widget -> the stored value is mm" for a machine setting, the jog
step and a wizard input (docs/user/units.md).  No simulator: the demo controller only."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QTabWidget  # noqa: E402

from units_sweep import offenders  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch, tmp_path):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import real_window
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window(user_mode="service")
    w.resize(1280, 800)
    w.show()
    _pump(app)
    w.config.set("units.system", "imperial")
    _pump(app)
    yield w
    w._test_no_dialogs = True
    w.config.set("units.system", "metric")
    w.close()


def _pump(app, n=6):
    for _ in range(n):
        app.processEvents()


def _type(sb, text):
    sb.lineEdit().selectAll()
    sb.lineEdit().clear()
    QTest.keyClicks(sb.lineEdit(), text)
    sb.lineEdit().editingFinished.emit()
    sb.editingFinished.emit()


def _tabs(root, app):
    hits = set(offenders(root))
    for tw in root.findChildren(QTabWidget):
        if not tw.isVisible():
            continue
        for i in range(tw.count()):
            tw.setCurrentIndex(i)
            _pump(app, 2)
            hits |= set(offenders(root))
    return hits


def test_run_workspace_in_imperial(win, app):
    win.show_workspace("run")
    _pump(app)
    scr = win.extension("run").screen
    scr.refresh()
    hits = _tabs(scr, app)          # the Run workspace's own page (the docks are other forks' files)
    assert not hits, "\n".join(sorted(hits))
    assert " in" in scr.values.text() and "in/min" in scr.values.text()
    snap, _svg = win.extension("run").monitor_snapshot()
    assert snap["speed"].endswith("in/min") and snap["position"].endswith(" in")


def test_run_breakpoint_reason_and_queue_thickness(win, app):
    from mlaser.services.operator import RunStatus
    ws = win.extension("run")
    s = RunStatus()
    s.reason, s.reason_args = "run.reason.breakpoint", {"x": 25.4, "y": 50.8}
    t = ws.reason_text(s)
    assert "X 1.00, Y 2.00 in" in t and "mm" not in t
    from mlaser.ui.workspaces.run import card_in_units, mm_text, thickness_text
    assert thickness_text(3.175) == "0.125 in"
    assert mm_text("layer 1 has the technology for Steel 3 mm, but the sheet is Steel 6.35 mm") == \
        "layer 1 has the technology for Steel 0.118 in, but the sheet is Steel 0.25 in"

    class E:
        thickness_mm, extra = 6.35, {"sheet_bounds": [0, 0, 1219.2, 609.6]}
    d = card_in_units({"thickness": "6.35 mm", "size": "1219 × 610 mm", "mismatch": []}, E())
    assert d["thickness"] == "0.25 in" and d["size"] == "48.00 × 24.00 in"


def test_machine_workspace_every_section_in_imperial(win, app):
    win.show_workspace("machine")
    _pump(app)
    ws = win.extension("ext_machine").workspace
    bad = {}
    for i in range(ws.nav.count()):
        ws.nav.setCurrentRow(i)
        ws.tick()
        _pump(app, 3)
        sp = ws.settings_panel
        hits = set()
        if ws.section() == "settings":
            for j in range(sp.section.count()):
                sp.section.setCurrentIndex(j)
                _pump(app, 2)
                hits |= set(offenders(ws))
        hits |= _tabs(ws, app)
        if hits:
            bad[ws.section()] = sorted(hits)
    assert not bad, bad
    assert ws.pos_lbl.text().endswith(" in")


def test_machine_setting_edited_in_imperial_is_stored_in_mm(win, app):
    ws = win.extension("ext_machine").workspace
    sp = ws.settings_panel
    sp.section.setCurrentIndex(sp.section.findData("HomeParam"))
    assert sp.changed() == {}                                 # opening in imperial changes nothing
    ed, sp_ed = sp._editors["A0.HomeOffset"], sp._editors["HP.FastSpeed"]
    assert ed.suffix() == " in" and sp_ed.suffix() == " in/min"
    _type(ed, "1/2")                                          # 0.5 in
    _type(sp_ed, "120")                                       # 120 in/min
    assert sp.apply() == 2
    assert win.config.get("A0.HomeOffset") == pytest.approx(12.7)
    assert win.config.get("HP.FastSpeed") == pytest.approx(50.8)
    assert win.config.get("A1.HomeOffset") == 10.0            # untouched
    # titles carry the display unit and follow a switch
    def titles():
        t = sp.tree
        return [t.topLevelItem(i).child(j).text(0) for i in range(t.topLevelItemCount())
                for j in range(t.topLevelItem(i).childCount())]
    imp = titles()
    win.config.set("units.system", "metric")
    met = titles()
    assert any("(in/min)" in t for t in imp) and any("(mm/s)" in t for t in met)
    assert not any("(mm" in t for t in imp)


def test_jog_step_in_imperial_moves_mm(win, app):
    from mlaser.ui.wizards.common import JogPad
    calls = []

    class Ctl:
        def jog_step(self, a, d, s, fast=False):
            calls.append((a, d, s))
    pad = JogPad(win, lambda: Ctl())
    assert [b.text() for b in pad.step_buttons[1:]] == ["0.005 in", "0.05 in", "0.5 in"]
    pad.step_buttons[2].setChecked(True)
    pad.buttons["X+"].pressed.emit()
    assert calls[-1] == ("X", 1, pytest.approx(1.27))
    win.config.set("units.system", "metric")                 # live switch: metric steps again
    assert [b.text() for b in pad.step_buttons[1:]] == ["0.1 mm", "1 mm", "10 mm"]
    pad.buttons["Y-"].pressed.emit()
    assert calls[-1] == ("Y", -1, 1.0)


def test_edge_wizard_input_in_imperial_is_stored_in_mm(win, app):
    from mlaser.ui.wizards.procedures import EdgeSeekWizard
    d = EdgeSeekWizard(win, win.extension("ext_machine"), "cap4")
    d.show()
    _pump(app)
    for _ in range(5):
        if d.proc.step.id == "params":
            break
        d.next()
        _pump(app)
    assert d.proc.step.id == "params"
    hits = offenders(d)
    assert not hits, hits
    before = d.proc.params.start
    _type(d.ed["start_x"], '2"')
    _type(d.ed["out_speed"], "60")                           # 60 in/min
    d.collect(d.proc.step)
    assert d.proc.params.start == (pytest.approx(50.8), before[1])   # start_y untouched: exact
    assert d.proc.params.out_speed == pytest.approx(25.4)
    d.done(0)


def test_wizard_texts_and_checklist_values_in_imperial(win, app):
    from mlaser.ui.wizards.common import T, unit_values
    from mlaser.ui.wizards.procedures import _detail_values
    t = T(win, "wizard.comm.jog_x_text", **unit_values())
    assert "0.394 in" in t and "mm" not in t
    assert "mm" not in T(win, "wizard.edge.error.too_close", **unit_values())
    v = _detail_values("wizard.comm.jog_moved", {"axis": "X", "moved": 10.0, "step": 10.0})
    assert T(win, "wizard.comm.jog_moved", **v) == "X moved 0.3937 in (step 0.3937 in)."
    v = _detail_values("wizard.comm.home_ok", {"seconds": 1.5, "x": 0.0, "y": 25.4})
    assert T(win, "wizard.comm.home_ok", **v) == "Homed in 1.5 s at (0, 1) in."


def test_diag_dialogs_in_imperial(win, app):
    from mlaser.diag.interferometer import PitchTable
    from mlaser.ui import ext_diag as D
    ext = win.extension("ext_diag")
    ext = ext[0] if isinstance(ext, tuple) else ext
    shown = []
    for cls in ("MeasureWindow", "DeviceReportDialog"):
        c = getattr(D, cls, None)
        if c is None:
            continue
        dlg = c(ext)
        dlg.show()
        _pump(app)
        shown.append(dlg)
        hits = offenders(dlg)
        assert not hits, (cls, hits)
    rows = D.device_rows(ext.counters)
    moves = [r for r in rows if r[0].endswith("_move_m")]
    assert moves and all(r[1].endswith("(ft)") for r in moves)
    pan = D.InterferometerPanel(ext)
    pan.show()
    t = PitchTable([0.0, 25.4], [0.0, 25.4254], [0.0, 25.4508])
    pan.set_table(t)
    _pump(app)
    assert pan.ctable.item(1, 0).text() == "1.00000" and "(in)" in pan.ctable.horizontalHeaderItem(0).text()
    assert pan.backlash.text() == "Average backlash: 0.00050 in"
    tabs = pan.findChild(QTabWidget)
    for i in range(tabs.count()):
        tabs.setCurrentIndex(i)
        _pump(app, 2)
        assert not offenders(pan), offenders(pan)
    win.config.set("units.system", "metric")
    assert pan.ctable.item(1, 0).text() == "25.4000" and "(mm)" in pan.ctable.horizontalHeaderItem(0).text()
    pan.close()
    for kind in ("af", "ec"):
        win.config.set("units.system", "imperial")
        m = D.DeviceMonitor(ext, kind)
        m.show()
        _pump(app)
        assert not offenders(m), (kind, offenders(m))
        if kind == "ec":
            assert m.speed.value() == 10 and m.length.value() == 10 and m.speed.suffix() == " in/min"
        m.close()
    for d in shown:
        d.close()
