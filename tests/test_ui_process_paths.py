"""Leads, kerf and the other craft settings in the real window, every path a user can take:
the document changes, the plan has the leads / kerf, the canvas shows them at the fitted zoom
and the simulation follows the planned path (both plan modes: in-thread and the plan process,
the application default).  Plus the technology editor (grouping, advanced hidden, edit ->
saved -> applied) and the panels' reflow.

The user's report (macOS): "Lead lines / compensate etc don't seem to work.  Nothing shows on
the UI and simulating doesn't do the leads."  Root causes: a document opened before the window
had its size stayed fitted to a ~100 px view (the drawing a few pixels wide), and leads / the
compensated path were drawn in the plain cut colour underneath the drawing.
"""
from __future__ import annotations

import math
import os
import time

import pytest
from PySide6.QtWidgets import QDialog

from mlaser import ops

SHOTS = os.environ.get("MLASER_SHOTS_DIR")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def pump(app, s: float = 0.05) -> None:
    t0 = time.monotonic()
    while True:
        app.processEvents()
        if time.monotonic() - t0 >= s:
            break
        time.sleep(0.005)


def make_window(app, monkeypatch, tmp_path, process: bool = False, show_doc_first: bool = False):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MLASER_PLAN_PROCESS", "1" if process else "0")
    from wiring import real_window

    w = real_window()
    if show_doc_first:                 # the macOS start-up order: document before the window
        w.set_document(ops.demo_document())
    w.resize(1280, 800)
    w.show()
    pump(app, 0.1)
    if not show_doc_first:
        w.set_document(ops.demo_document())
    settle(w, app)
    return w


def settle(w, app) -> None:
    pump(app, 0.02)
    w.jobs.settle()
    pump(app, 0.05)


@pytest.fixture
def win(app, monkeypatch, tmp_path):
    w = make_window(app, monkeypatch, tmp_path)
    yield w
    w._test_no_dialogs = True
    w.close()


def accept_dialogs(monkeypatch, *classes) -> None:
    for cls in classes:
        monkeypatch.setattr(cls, "exec", lambda self: (self.accept(), QDialog.Accepted)[1])


def leads_via_ribbon(win, app, length: float, parts=None) -> None:
    """The user's path: select parts, ribbon Lead (opens Process › Properties at the lead-in),
    choose Line and a length there."""
    win.canvas.select_parts(range(len(win.document.parts)) if parts is None else parts)
    pump(app)
    win.commands.run("craft.lead")
    pp = win.extension("ext_process").properties
    kind, _ang, ln, _rad = pp.lead_w["in"]
    assert not pp.isHidden() and kind.isEnabled()          # the Properties field, ready to edit
    kind.setCurrentIndex(1)
    ln.setValue(length)
    pp._commit_leads()


def lead_moves(items) -> list:
    return [m for m in items if getattr(m, "lead", None) == "in"]


def drawing_px(w) -> float:
    """Width of the drawing on screen (px) at the current zoom."""
    x0, _y0, x1, _y1 = w.document.bbox()
    return (x1 - x0) * abs(w.canvas.transform().m11())


def sim_has(points, p, tol=0.05) -> bool:
    return any(math.hypot(x - p[0], y - p[1]) <= tol for x, y, *_ in points)


# --------------------------------------------------------------------------- visibility
def test_document_before_show_is_fitted_once_the_window_has_its_size(app, monkeypatch, tmp_path):
    w = make_window(app, monkeypatch, tmp_path, show_doc_first=True)
    try:
        c = w.canvas
        vp = c.viewport().width()
        assert drawing_px(w) > 0.5 * vp, (drawing_px(w), vp)      # was ~70 px of ~690
        assert c.fit_mode == "drawing"
        w.resize(1000, 700)                    # a later layout change keeps the fit
        pump(app, 0.1)
        assert drawing_px(w) > 0.5 * c.viewport().width()
        c.zoom(1.25)                           # the user zoomed: a resize keeps their zoom
        assert c.fit_mode is None
        s = c.transform().m11()
        w.resize(1100, 760)
        pump(app, 0.1)
        assert c.transform().m11() == pytest.approx(s)
        c.zoom_fit_table()
        tw, _th = c.options.table_size
        assert c.fit_mode == "table" and tw * abs(c.transform().m11()) <= c.viewport().width() + 1
        w.commands.run("view.zoom_fit")
        assert c.fit_mode == "drawing"
    finally:
        w._test_no_dialogs = True
        w.close()


@pytest.mark.parametrize("process", [False, True], ids=["thread", "plan_process"])
def test_lead_command_leads_reach_plan_canvas_and_simulation(app, monkeypatch, tmp_path, process):
    w = make_window(app, monkeypatch, tmp_path, process=process)
    try:
        assert w.jobs.process_mode == process
        assert not any(p.selected for p in w.document.parts)
        w.commands.run("craft.lead")           # nothing selected: says what to do, edits nothing
        assert "Select the parts" in w.log_panel.text.toPlainText()
        assert all(p.craft.lead_in.kind == 0 for p in w.document.parts)
        leads_via_ribbon(w, app, 5.0)
        closed = [p for p in w.document.parts if all(c.closed for c in p.contours)]
        assert closed and all(p.craft.lead_in.kind == 1 and p.craft.lead_in.length == 5.0 for p in closed)
        settle(w, app)
        plan = w.jobs.current
        leads = lead_moves(plan.toolpath)
        assert len(leads) >= len(closed)
        c = w.canvas
        assert lead_moves(c.toolpath) and c.plan_state == "ready"
        items = c.toolpath_items("lead_in")
        assert items and all(it.isVisible() for it in items)
        assert len(c.toolpath_items("pierce")) == len(c.toolpath_items("lead_glyph")) >= len(closed)
        # at the fitted zoom a 5 mm lead is several pixels long
        assert 5.0 * abs(c.transform().m11()) >= 3.0
        # the simulation: the canvas path and the time-sampled one both go through the leads
        sp = c.simulation_path()
        assert all(sim_has(sp, m.start) for m in leads)
        w.simulate()
        assert c.simulation_running()
        assert sim_has(c._sim_path, leads[0].start, tol=0.5)
        c.stop_simulation(clear=True)
        # frame / dry run / start compile the same toolpath (with the leads)
        from mlaser.services.jobs import build_request

        for mode in ("dry_run", "process"):
            req = build_request(mode, w.docs, w.ctl, w.machine, w.cam_params)
            got = w.jobs.plan_sync(req)
            assert len(lead_moves(got.toolpath)) == len(leads), mode
        if SHOTS and not process:
            w.grab().save(os.path.join(SHOTS, "design_leads_test.png"))
    finally:
        w._test_no_dialogs = True
        w.close()


def test_lead_on_the_selection_only_and_restore(win, app):
    from mlaser.services import inheritance as I

    leads_via_ribbon(win, app, 4.0, parts=[0])
    assert win.document.parts[0].craft.lead_in.kind == 1
    assert all(p.craft.lead_in.kind == 0 for p in win.document.parts[1:])
    assert "craft.lead_in.length" in I.craft_overrides(win.document.parts[0].craft)
    pp = win.extension("ext_process").properties
    fld = pp.inherit["craft.lead_in.length"]
    assert fld.overridden and not fld.reset_btn.isHidden()
    fld.reset.emit()                           # restore length and type: the inherited (no) lead
    pp.inherit["craft.lead_in.kind"].reset.emit()
    assert win.document.parts[0].craft.lead_in.kind == 0
    assert not I.craft_overrides(win.document.parts[0].craft) & {"craft.lead_in.kind", "craft.lead_in.length"}


def test_lead_edit_keeps_a_start_set_by_hand(win, app):
    from mlaser.cam.manual import contour_craft

    pp = win.extension("ext_process").properties
    win.canvas.select_parts([2])
    pump(app)
    pp.start.setValue(40.0)
    pp._commit_start()
    part = win.document.parts[2]
    ci = max(range(len(part.contours)), key=lambda i: part.contours[i].length())
    assert contour_craft(part, ci).start_param == pytest.approx(0.4)
    leads_via_ribbon(win, app, 3.0, parts=[2])
    assert contour_craft(win.document.parts[2], ci).start_param == pytest.approx(0.4)


def test_properties_panel_leads_and_kerf_show_on_the_canvas(win, app):
    pp = win.extension("ext_process").properties
    c = win.canvas
    win.canvas.select_parts([0])
    pump(app)
    kind, _ang, ln, _rad = pp.lead_w["in"]
    kind.setCurrentIndex(1)
    ln.setValue(4.0)
    pp._commit_leads()
    pp.kerf_kind.setCurrentIndex(pp.kerf_kind.findData(1))
    pp.kerf_w.setValue(0.4)
    pp._commit_kerf()
    settle(win, app)
    assert lead_moves(win.jobs.current.toolpath)
    assert c.toolpath_items("lead_in") and c.toolpath_items("kerf") and c.toolpath_items("kerf_band")
    # the kerf path is the compensated one: off the drawn outline by the compensation value
    # (on a part with holes, whose contours got their own craft from the leads: ops.apply_kerf
    # used to leave those at "no compensation")
    kerf_path = c.toolpath_items("kerf")[0].path()
    x0, y0, x1, y1 = win.document.parts[0].bbox()
    br = kerf_path.boundingRect()
    assert br.width() > (x1 - x0) and br.width() == pytest.approx((x1 - x0) + 0.8, abs=0.05)


def test_technology_lead_rule_reaches_the_parts(win, app):
    ext = win.extension("ext_process")
    ws = ext.workspace
    tech = ext.store.all()[0]
    n = ws.apply(tech, ws.layers_in_use(), leads=True, kerf=True, joints=False, rules=False)
    assert n
    closed = [p for p in win.document.parts if all(c.closed for c in p.contours)]
    want = tech.craft.get("lead_in", {}).get("length", 3.0)
    assert all(p.craft.lead_in.kind and p.craft.lead_in.length == pytest.approx(want) for p in closed)
    settle(win, app)
    assert lead_moves(win.jobs.current.toolpath) and win.canvas.toolpath_items("lead_in")


def test_microjoints_start_overcut_cooling_and_lead_check(win, app):
    c = win.canvas
    craft = win.extension("ext_craft")
    # micro-joints: gaps over the drawing
    win.commands.run("craft.mj_auto")
    settle(win, app)
    assert any(not getattr(m, "laser", True) for m in win.jobs.current.toolpath)
    assert c.toolpath_items("gap") and c.toolpath_items("gap")[0].zValue() > 1
    win.undo()
    # overcut with nothing selected says so instead of silently doing nothing
    win.canvas.select_parts([])
    win.commands.run("craft.overcut")
    assert "Select the parts first" in craft.last_message
    win.canvas.select_parts(range(len(win.document.parts)))
    before = len(win.jobs.current.toolpath)
    craft.gap_p.value = 2.0
    win.commands.run("craft.overcut")
    settle(win, app)
    assert len(win.jobs.current.toolpath) != before
    win.undo()
    # start point: the marker is where the plan starts the contour, not the drawing's first point
    pp = win.extension("ext_process").properties
    win.canvas.select_parts([1])
    pump(app)
    pp.start.setValue(50.0)
    pp._commit_start()
    settle(win, app)
    marks = {(m.part, m.contour): m for m in c.toolpath_marks}
    part = win.document.parts[1]
    ci = max(range(len(part.contours)), key=lambda i: part.contours[i].length())
    first = part.contours[ci].segments[0].start
    assert math.dist(marks[(1, ci)].start, first) > 1.0
    # cooling points split the contour (an extra pierce at the corner); the demo drawing has no
    # sharp corner on a first contour, so a square plate
    from mlaser.model import Contour, Craft, Document, Line, Part

    sq = [(0, 0), (60, 0), (60, 40), (0, 40)]
    win.set_document(Document([Part([Contour([Line(sq[i], sq[(i + 1) % 4]) for i in range(4)], True)],
                                    Craft(layer=1))]))
    settle(win, app)
    win.canvas.select_parts([0])
    pump(app)
    pierces = sum(1 for m in win.jobs.current.toolpath if type(m).__name__ == "Pierce")
    pp.cool_angle.setValue(120.0)
    pp._cool_auto()
    settle(win, app)
    assert sum(1 for m in win.jobs.current.toolpath if type(m).__name__ == "Pierce") > pierces
    # lead check without leads: says that there are none
    win.commands.run("craft.lead_check")
    assert "No contour has a lead-in" in win.log_panel.text.toPlainText()


def test_auto_lead_placement_and_manual_lead(win, app, monkeypatch):
    strat = win.extension("ext_process").strategy
    assert strat.place_leads() == 0
    assert "No contour has a lead-in" in strat.summary.text()
    leads_via_ribbon(win, app, 3.0)
    settle(win, app)
    strat.place_leads()
    settle(win, app)
    assert win.canvas.toolpath_items("lead_in")
    # manual lead tool: two clicks (lead start, contour) add a lead on that contour
    craft = win.extension("ext_craft")
    win.commands.run("craft.lead_manual")
    craft.click(-10.0, 40.0)
    craft.click(0.0, 40.0)
    assert "mf88: 10.000 mm" in craft.last_message
    settle(win, app)
    assert any(m.part == 0 for m in win.canvas.toolpath_marks if m.lead_length > 9.9)


def test_toolpath_toggle_and_tiny_lead_glyph(win, app, monkeypatch):
    c = win.canvas
    leads_via_ribbon(win, app, 0.3)            # far below a pixel's worth at a table fit
    settle(win, app)
    c.zoom_fit_table()
    glyphs = c.toolpath_items("lead_glyph")
    assert glyphs and all(g.screen_length() < g.MIN_PX for g in glyphs)   # the glyph is drawn
    c.zoom(400.0)
    assert all(g.screen_length() >= g.MIN_PX for g in glyphs)             # the real lead is
    act = win.ribbon.actions["show.toolpath"]
    c.hud.toggle.setChecked(False)
    assert not c.options.show_toolpath and not act.isChecked()
    assert not c.toolpath_items("lead_in") and not c.toolpath_items("pierce")
    act.trigger()
    assert c.options.show_toolpath and c.hud.toggle.isChecked() and c.toolpath_items("lead_in")


def test_simulate_waits_for_the_plan(win, app, monkeypatch):
    """An edit followed at once by Simulate: the simulation runs the new plan (the leads), it
    never falls back to the raw drawing, and the canvas said the plan was being computed."""
    c = win.canvas
    states = []
    orig = c.set_plan_state
    monkeypatch.setattr(c, "set_plan_state", lambda s: (states.append(s), orig(s)))
    leads_via_ribbon(win, app, 6.0)
    assert win.jobs.pending
    win.simulate()
    assert "planning" in states and "simulating_wait" in states and states[-1] == "ready"
    leads = lead_moves(c.toolpath)
    assert leads and sim_has(c._sim_path, leads[0].start, tol=0.5)
    c.stop_simulation(clear=True)


# --------------------------------------------------------------------------- technology editor
def test_technology_editor_groups_hides_advanced_and_persists(win, app):
    from mlaser.services import technology as T
    from mlaser.ui.techform import SECTIONS

    ext = win.extension("ext_process")
    ws = ext.workspace
    form = ws.editor.form
    sids = form.section_order()
    assert sids[:4] == [s[0] for s in SECTIONS] and sids[-1] == "advanced"
    assert {"leads", "corners", "record"} <= set(sids)
    tech = ext.store.all()[0]
    ws.pick(tech.key)
    pump(app)
    vis = set(form.visible_keys())
    assert {"GP.CutSpeed", "GP.CutPeakCurrent", "GP.CutPower", "GP.CutFreq", "GP.CutHeight"} <= vis
    assert "GP.CleanResidue_Enable" not in vis and form.sections["advanced"].isHidden()
    # only the stages the pierce mode uses
    stages = form.stages()
    assert all(f"GP.DrillHeight{s}" in vis for s in range(stages))
    assert all(f"GP.DrillHeight{s}" not in vis for s in range(stages, 5))
    # dependent fields: slow start length only while slow start is on
    form.show_all.setChecked(True)
    pump(app)
    if not form.fields["GP.SlowStart"].get():
        assert "GP.SlowStartLength" not in form.visible_keys()
        form.fields["GP.SlowStart"].widget.setChecked(True)
        assert "GP.SlowStartLength" in form.visible_keys()
        form.fields["GP.SlowStart"].widget.setChecked(False)
    assert "GP.CleanResidue_Enable" in form.visible_keys()
    form.show_all.setChecked(False)
    # search
    form.search.setText("residue")
    pump(app)
    assert form.visible_keys() == ["GP.CleanResidue_Enable"] and not form.sections["advanced"].isHidden()
    form.search.setText("")
    # edit -> marked against the factory version -> saved -> applied to a layer -> compiled
    ws.editor.fields["GP.CutSpeed"].widget.setValue(77.0)
    assert form.overridden() == ["GP.CutSpeed"] and not form.fields["GP.CutSpeed"].reset.isHidden()
    saved = ws.save_version()
    assert saved.version == 2 and ext.store.get(saved.id).params["GP.CutSpeed"] == 77.0
    assert form.overridden() == ["GP.CutSpeed"]                  # still differs from the factory
    ws.apply(saved, [1], leads=False, kerf=False, joints=False, rules=False)
    assert win.document.layers[1].speed == 77.0
    assert T.layer_technology_ref(win.document.layers[1])["version"] == 2


def test_layer_inspector_inherits_and_overrides(win, app):
    """Process › Layers: the layer follows its recipe; a value set on the layer is marked and
    survives re-assigning the recipe; restore goes back to the recipe's value."""
    from mlaser.services import inheritance as I

    ext = win.extension("ext_process")
    ws = ext.workspace
    tech = ext.store.all()[0]
    ws.apply(tech, [1])
    ws.select_layer(1)
    insp = ws.inspector
    speed = tech.full_params()["GP.CutSpeed"]
    f = insp.form.fields["GP.CutSpeed"]
    assert f.get() == pytest.approx(speed) and not f.overridden and tech.key.material in insp.form.base_label
    f.widget.setValue(speed + 10.0)           # the user types a value: one undo step
    lp = win.document.layers[1]
    assert lp.speed == pytest.approx(speed + 10.0) and "GP.CutSpeed" in I.layer_overrides(lp)
    assert insp.form.fields["GP.CutSpeed"].overridden and not insp.form.fields["GP.CutSpeed"].chip.isHidden()
    assert "1 " in ws.layer_state(1)[1] or "1" in ws.layer_state(1)[1]
    ws.apply(tech, [1])                        # re-assign: the value set on the layer stays
    assert win.document.layers[1].speed == pytest.approx(speed + 10.0)
    insp.form.reset_field("GP.CutSpeed")       # restore: the recipe's value
    assert win.document.layers[1].speed == pytest.approx(speed)
    assert "GP.CutSpeed" not in I.layer_overrides(win.document.layers[1])
    win.undo()
    assert win.document.layers[1].speed == pytest.approx(speed + 10.0)
    # a layer-level kerf default reaches the parts of the layer
    fld = insp.craft_fields["craft.kerf_width"]
    fld.edited.emit(0.2)
    assert all(p.craft.kerf_width == 0.2 for p in win.document.parts if p.craft.layer == 1)
    # the layer list's double-click and the Properties button go to the inspector
    win.show_workspace("design")
    win.edit_layer(1)
    assert ws.pages.currentIndex() == 0 and ws.tabs.currentWidget() is insp


def test_compare_two_technologies(win, app):
    from mlaser.ui.workspaces.process import TechCompareDialog

    ext = win.extension("ext_process")
    a, b = ext.store.all()[:2]
    d = TechCompareDialog(win.tr_, ext.store, a, b)
    rows = d.rows()
    assert rows and all(r[4] for r in rows)
    fa, fb = a.full_params(), b.full_params()
    keys = {k for k in fa if k in d._form.fields and fa[k] != fb.get(k)}
    assert len(rows) >= len(keys) > 0
    assert d.tree.topLevelItemCount() > 0
    d.only_diff.setChecked(False)
    assert len(d.rows()) > len(rows)


# --------------------------------------------------------------------------- reflow
def _table_geometry(tb) -> str:
    """What decides a table's sideways scroll range (the failure message: the platform styles
    differ in frames, header metrics and scroll bars)."""
    hh = tb.horizontalHeader()
    shown = {c: hh.sectionSize(c) for c in range(tb.columnCount()) if not tb.isColumnHidden(c)}
    return (f"style {tb.style().name()} table {tb.width()} viewport {tb.viewport().width()} "
            f"header {hh.width()} length {hh.length()} min section {hh.minimumSectionSize()} "
            f"sections {shown} vbar {tb.verticalScrollBar().isVisible()} "
            f"({tb.verticalScrollBar().width()}) frame {tb.frameWidth()} mode {tb.horizontalScrollMode()}")


@pytest.mark.parametrize("width", [220, 260, 340, 520, 900])
def test_panels_never_scroll_sideways(app, width):
    from mlaser.ui.i18n import Translator
    from mlaser.ui.panels import LayerPanel
    from mlaser.ui.techform import TechForm

    tr = Translator(language="en")
    f = TechForm(tr)
    f.show_all.setChecked(True)
    f.resize(width, 700)
    f.show()
    pump(app, 0.02)
    assert f.scroll.horizontalScrollBar().maximum() == 0
    assert f.minimumSizeHint().width() <= max(width, 240)
    lp = LayerPanel(tr)
    lp.set_document(ops.demo_document())
    lp.resize(width, 300)
    lp.show()
    pump(app, 0.02)
    assert lp.table.horizontalScrollBar().maximum() == 0, _table_geometry(lp.table)
    assert not lp.table.isColumnHidden(0)
    f.close()
    lp.close()


def test_properties_panel_reflows(win, app):
    from PySide6.QtWidgets import QFormLayout

    pp = win.extension("ext_process").properties
    dock = win.extension("ext_process").props_dock
    win.canvas.select_parts([0])
    for width in (230, 600):
        dock.setFloating(True)
        dock.resize(width, 700)
        pump(app, 0.05)
        assert pp.horizontalScrollBar().maximum() == 0
        forms = [s.body for s in pp.sections]
        wrapped = [f.form.rowWrapPolicy() == QFormLayout.WrapAllRows for f in forms]
        assert any(wrapped) if width == 230 else not any(wrapped)
    dock.setFloating(False)


def test_cutting_order_is_saved_with_the_drawing(win, app, tmp_path):
    from mlaser.cam.toolpath import CamParams
    from mlaser.project.format import load_project, save_project

    win.sort("left_right")
    mode = win.cam_params.sort_mode
    assert mode != CamParams().sort_mode
    path = tmp_path / "order.mlz"
    save_project(path, win.document)
    win.set_document(ops.demo_document())          # another drawing: its own (default) order
    assert win.cam_params.sort_mode == CamParams().sort_mode
    win.set_document(load_project(path).document)
    assert win.cam_params.sort_mode == mode
