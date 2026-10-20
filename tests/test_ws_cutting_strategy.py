"""Nest workspace options and cutting strategy, the Process *Cutting strategy* tab and the
pre-flight warnings, through the real window wiring (docs/user/cutting-strategy.md).

``MLASER_SHOT_DIR=<dir>`` saves screenshots of the Nest workspace with the options open."""

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402

from mlaser.model import Contour, Craft, Document, LayerParams, LeadParams, Line, Part  # noqa: E402
import test_ws_run as _wr  # noqa: E402
from test_ws_run import pump, shot, wait  # noqa: E402

app, win = _wr.app, _wr.win  # the real-window fixtures of the Run / Nest workspace tests


def rect(x0, y0, w, h, lead=2.0, layer=1):
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    return Part(
        [Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)],
        Craft(layer=layer, lead_in=LeadParams(kind=1, length=lead)),
    )


def layers():
    return {1: LayerParams(1, speed=50.0, power_pct=85, gas=2, gas_pressure=12.0, cut_height=0.8)}


def test_nest_options_strategy_and_queue(win, app, tmp_path):
    from mlaser.nest import NestPart
    from mlaser.services.remnants import polygon_of

    ws = win.extension("nest")
    ws.job.parts.clear()
    ws.job.sheets.clear()
    for k, (w_, h_, q) in enumerate(((300, 200, 4), (150, 80, 6), (60, 40, 6))):
        ws.job.add_part(NestPart([rect(0, 0, w_, h_)], name=f"p{k}", quantity=q))
    ws.sheet_w.setValue(1500)
    ws.sheet_h.setValue(1000)
    ws.sheet_n.setValue(1)
    ws.add_sheet()
    win.set_document(Document(parts=[rect(0, 0, 10, 10)], layers=layers()))
    win.commands.run("workspace.nest")
    host = ws.window()
    host.resize(1280, 800)
    pump(app, 0.2)
    ws.refresh()
    # per-part rotation and mirror columns
    combo = ws.parts.cellWidget(1, 4)
    combo.setCurrentIndex(combo.findData("fixed"))
    assert ws.job.parts[1].angles == [0.0]
    ws.parts.item(2, 5).setCheckState(Qt.Checked)
    assert ws.job.parts[2].mirror
    # the options drawer
    ws.show_options(True)
    assert ws.options.isVisible()
    ws.common_line.setChecked(False)
    ws.part_in_part.setChecked(True)
    ws.scrap.setCurrentIndex(ws.scrap.findData("grid"))
    ws.scrap_pitch.setValue(400)
    ws.remnant_cut.setChecked(True)
    ws.lead_fix.setChecked(True)
    ws.time_limit.setValue(10)
    assert ws.start()
    assert wait(app, lambda: not ws.service.running, 120)
    run = ws.run
    assert run is not None and run.placed == 16
    p = run.job.params
    assert p.part_in_part and p.scrap_cut == "grid" and p.remnant_cut
    assert ws.compare.columnCount() == 6 and float(ws.compare.item(0, 3).text()) > 0
    assert int(ws.compare.item(0, 4).text()) == run.pierces > 0
    assert "raster" in ws.compare.item(0, 0).toolTip()
    pump(app, 0.2)
    kinds = {k for _a, _b, k in ws.view.lines}
    assert kinds == {"scrap", "remnant_cut"}  # the sheet view shows the strategy lines
    ws.options.setCurrentIndex(1)
    pump(app, 0.1)
    shot(host, "nest_options_strategy_1280x800.png")
    ws.options.setCurrentIndex(0)
    # a second run with common line, for the comparison
    ws.common_line.setChecked(True)
    # the common line's cut width is the layers' kerf, shown read-only (one home: Process)
    assert ws.kerf_info.property("mlaser_role") == "mirror" and ws.kerf_info.text()
    assert ws.tip_info.property("mlaser_role") == "mirror"
    assert ws.start() and wait(app, lambda: not ws.service.running, 120)
    assert ws.compare.rowCount() == 2
    shot(host, "nest_compare_runs_1280x800.png")
    entries = ws.send_to_queue()
    assert entries and all(e.path.endswith(".mlz") for e in entries)
    assert any(e.extra.get("remnant_rings") for e in entries)
    from mlaser.ui.ext_nest import _load_doc

    doc = _load_doc(entries[0].path)
    assert {pp.extra.get("strategy") for pp in doc.parts} >= {"scrap", "remnant_cut"}
    # the run screen stores the separated remnant exactly as cut
    runx = win.extension("run")
    e = next(e for e in entries if e.extra.get("remnant_rings"))
    e.material, e.thickness_mm = "S235", 3.0
    runx._sheet_doc = doc
    before = len(runx.remnants.available("S235", 3.0))
    runx._sheet_done(e, True)
    made = runx.remnants.available("S235", 3.0)
    assert len(made) == before + 1
    sep = polygon_of(e.extra["remnant_rings"])
    assert made[-1].area_mm2 == pytest.approx(sep.area, rel=1e-3)


def colliding_doc():
    doc = Document(
        parts=[rect(0, 0, 50, 30, lead=4.0), rect(0, 33, 50, 30, lead=4.0), rect(80, 0, 60, 40)],
        layers=layers(),
    )
    doc.parts[0].craft.start_param = (50 + 30 + 25) / 160.0
    return doc


def test_process_cutting_strategy_tab(win, app):
    ext = win.extension("ext_process")
    panel = ext.strategy
    win.set_document(colliding_doc())
    win.commands.run("process.show")
    ext.workspace.tabs.setCurrentWidget(panel)
    pump(app, 0.2)
    found = panel.check_leads()
    assert found and panel.issues.count() == len(found)
    assert win.canvas._lead_flags() == {i.key for i in found}  # drawn red
    from PySide6.QtWidgets import QScrollArea

    sa = ext.workspace.findChild(QScrollArea)
    if sa is not None:  # the tab is at the bottom of the column
        sa.ensureWidgetVisible(ext.workspace.tabs, 0, 0)
        sa.horizontalScrollBar().setValue(0)
    pump(app, 0.3)
    shot(ext.workspace.window(), "process_lead_check_1280x800.png")
    rep = panel.fix_leads()
    assert not win.canvas._lead_flags()  # the fixed leads lose the mark
    assert not rep.remaining and panel.issues.count() == 0
    from mlaser.cam import collision

    assert collision.analyse(win.document) == []
    # the bed model is a machine setting (Machine › Setup › Bed): shown here read-only
    win.config.set("bed.slat_pitch", 120.0)
    win.config.set("bed.slat_direction", 1)
    assert "120" in panel.bed_label.text() and panel.bed_label.property("mlaser_role") == "mirror"
    win.config.set("bed.slat_pitch", 100.0)
    win.config.set("bed.slat_direction", 0)
    risk = panel.check_tipping()
    assert risk  # the 60 x 40 plate on 100 mm slats
    assert panel.tip_strategy() == "joint"        # no recipe on the layers: the default rule
    trep = panel.fix_tipping()
    assert trep.joints and not panel.check_tipping()
    shot(ext.workspace.window(), "process_tip_fix_1280x800.png")
    for cmd in ("cam.lead_check", "cam.lead_fix", "cam.lead_place", "cam.tip_check", "cam.tip_fix"):
        assert cmd in win.commands


def test_preflight_warns_about_lead_collisions(win):
    from mlaser.machine.preflight import PreflightReport
    from mlaser.ui.workspaces.cutting_strategy import augment_preflight

    doc = colliding_doc()
    rep = augment_preflight(win, PreflightReport("start"), "start", doc)
    assert {i.code for i in rep.issues} == {"lead_collision"} and rep.ok  # warnings, not errors
    assert augment_preflight(win, PreflightReport("frame"), "frame", doc).issues == []
    win.config.set("cam.preflight_lead_check", False)
    assert augment_preflight(win, PreflightReport("start"), "start", doc).issues == []
    win.config.set("cam.preflight_tip_check", True)
    rep = augment_preflight(win, PreflightReport("start"), "start", doc)
    assert {i.code for i in rep.issues} == {"tip_risk"}
