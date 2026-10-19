"""Process plugin UI (ext_process): technology applied through the workspace compiles like the
layer dialog, properties-panel edits are undoable, the scrubber follows the PlannedJob."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mlaser import ops
from mlaser.model import LayerParams
from mlaser.services import technology as T

pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))   # own technology store
    from wiring import real_window

    w = real_window()
    w.set_document(ops.demo_document())
    w.jobs.settle()
    app.processEvents()
    yield w
    w._test_no_dialogs = True
    w.close()


def _ext(w):
    return w.extension("ext_process")


def test_plugin_registers_workspace_docks_and_commands(win):
    ext = _ext(win)
    assert ext is not None
    assert "ext_process.process" in win.workspaces
    assert win.workspaces["ext_process.process"][1] is ext.workspace
    for cmd in ("process.show", "process.apply", "process.rates", "props.show", "preview.play", "preview.clear"):
        assert cmd in win.commands
    assert len(ext.store.all()) == 9 and all(t.is_example for t in ext.store.all())


def test_workspace_apply_compiles_like_the_layer_dialog(win, app):
    """Technology -> layer -> compiled job equals typing the same values in the layer dialog."""
    from mlaser.cam.toolpath import CamParams
    from mlaser.nc.compiler import compile_job
    from mlaser.ui import dialogs

    ext = _ext(win)
    ws = ext.workspace
    tech = next(t for t in ext.store.all() if t.key.material.startswith("Stainless") and t.key.thickness_mm == 2.0)
    ws.pick(tech.key)
    assert ws.picked().key == tech.key
    n = ws.apply(tech, ws.layers_in_use(), leads=False, kerf=False, joints=False, rules=False)
    assert n == len(ws.layers_in_use())
    doc_t = win.docs.snapshot()
    # the same values typed into the layer dialog of a fresh layer
    doc_h = win.docs.snapshot()
    for li in list(doc_h.layers):
        lp = LayerParams(index=li, color=doc_h.layers[li].color)
        d = dialogs.LayerParamDialog(win.tr_, lp)
        for k, v in tech.params.items():
            d.values[k] = v
        d._store()
        doc_h.layers[li] = lp
    jt = compile_job(doc_t, CamParams())
    jh = compile_job(doc_h, CamParams())
    assert jt.frames() == jh.frames()
    assert jt.pierces > 0
    # the plan the window previews now uses the technology
    win.jobs.settle()
    app.processEvents()
    assert ext.plan is not None and ext.plan.document.layers[1].speed == tech.params["GP.CutSpeed"]
    # undo restores the layers
    win.undo()
    assert T.layer_technology_ref(win.document.layers.get(1)) is None


def test_workspace_state_save_version_and_diff(win, app):
    ext = _ext(win)
    ws = ext.workspace
    tech = ext.store.all()[0]
    ws.pick(tech.key)
    ws.apply(tech, [1], leads=False, kerf=False, joints=False, rules=False)
    name, state = ws.layer_state(1)
    assert tech.key.material in name and "example" in state
    ws.editor.fields["GP.CutSpeed"].widget.setValue(77.0)
    ws.diff_mode.setCurrentIndex(ws.diff_mode.findData("unsaved"))
    assert [c.key for c in ws.changes()] == ["GP.CutSpeed"]
    saved = ws.save_version()
    assert saved.version == 2 and saved.params["GP.CutSpeed"] == 77.0
    ws.diff_mode.setCurrentIndex(ws.diff_mode.findData("factory"))
    assert {c.key for c in ws.changes()} == {"GP.CutSpeed"}
    assert "2" in ws.layer_state(1)[1]                       # a newer version than the applied one
    # an edit of the layer after applying shows up in "layer vs technology"
    win.document.layers[1].speed = 12.5
    ws.current_layer = 1
    ws.diff_mode.setCurrentIndex(ws.diff_mode.findData("layer"))
    assert [(c.key, c.new) for c in ws.changes()] == [("GP.CutSpeed", 12.5)]
    reverted = ws.revert_factory()
    assert reverted.version == 3 and ext.store.diff_factory(reverted.id) == []


def test_properties_panel_edits_are_undoable(win, app):
    ext = _ext(win)
    pp = ext.properties
    win.canvas.select_parts([0])
    app.processEvents()
    part = win.document.parts[0]
    assert pp.selected() == [part]
    before = (part.bbox(), part.craft.kerf_comp, part.craft.kerf_width, part.craft.lead_in.kind)
    # kerf
    pp.kerf_kind.setCurrentIndex(pp.kerf_kind.findData(1))
    pp.kerf_w.setValue(0.2)
    pp._commit_kerf()
    assert (part.craft.kerf_comp, part.craft.kerf_width) == (1, 0.2)
    # leads with a live preview first (real CAM geometry, nothing changed yet)
    kind, ang, ln, rad = pp.lead_w["in"]
    kind.setCurrentIndex(1)
    ln.setValue(4.0)
    polys = pp.preview_polylines(lambda d: ops.apply_lead(d, *pp.leads()))
    assert polys and win.document.parts[0].craft.lead_in.kind == 0
    pp._commit_leads()
    part = win.document.parts[0]
    assert part.craft.lead_in.kind == 1 and part.craft.lead_in.length == 4.0
    # transform: move and resize (uniform) the selection
    pp.x.setValue(10.0)
    pp.w.setValue(100.0)
    pp._commit_transform()
    x0, y0, x1, y1 = win.document.parts[0].bbox()
    assert (x0, x1 - x0) == pytest.approx((10.0, 100.0), abs=1e-6)
    # layer
    pp.layer.setCurrentIndex(pp.layer.findData(3))
    pp._commit_layer()
    assert win.document.parts[0].craft.layer == 3
    assert pp.edits == 4
    for _ in range(4):
        win.undo()
    p = win.document.parts[0]
    assert p.bbox() == pytest.approx(before[0])
    assert (p.craft.kerf_comp, p.craft.kerf_width, p.craft.lead_in.kind) == before[1:]
    assert p.craft.layer == 1
    win.redo()
    assert win.document.parts[0].craft.kerf_width == 0.2


def test_properties_panel_follows_the_selection(win, app):
    ext = _ext(win)
    pp = ext.properties
    win.canvas.select_parts([])
    app.processEvents()
    assert not pp.sections[0].isEnabled()
    win.canvas.select_parts([0, 1])
    app.processEvents()
    assert pp.sections[0].isEnabled()
    x0, y0, x1, y1 = ops.selection_bbox(win.document)
    assert pp.w.value() == pytest.approx(x1 - x0, abs=1e-3)
    assert "2" in pp.summary.text()


def test_scrubber_plays_the_planned_job(win, app):
    ext = _ext(win)
    plan = ext.plan
    assert plan is not None and plan is win.jobs.current
    tl = ext.timeline
    assert tl is not None and tl.key == plan.key
    assert tl.total_s == pytest.approx(plan.job.total_time_s)
    sc = ext.scrubber
    assert sc.slider.maximum() == int(round(tl.total_s * 1000))
    sc.slider.setValue(sc.slider.maximum() // 2)
    app.processEvents()
    st = tl.state_at(sc.t)
    assert ext.overlay.visible and ext.overlay._head.pos().x() == pytest.approx(st.x)
    sc.step_contour(1)
    assert any(abs(sc.t - m[0]) < 1e-9 for m in tl.markers)
    sc.contour.setValue(1)
    assert sc.t == pytest.approx(tl.markers[0][0])
    sc.clear()
    assert not ext.overlay.visible
    # the estimate card shows the plan's time; rates change the cost
    card = ext.workspace.card.card
    assert card.value("time.total") == pytest.approx(plan.job.total_time_s)
    assert card.value("cost.machine") is None
    ext.settings["machine_per_h"] = 36.0
    assert ext.card.value("cost.machine") == pytest.approx(plan.job.total_time_s / 100.0)


def test_layer_dialog_values_reach_the_compiler(app):
    """The layer dialog's other GP values now go where the compiler reads them."""
    from mlaser.parity_ports.mainapp.layer import LayerView
    from mlaser.ui import dialogs, i18n

    lp = LayerParams(index=1)
    d = dialogs.LayerParamDialog(i18n.Translator(language="en"), lp)
    d.values["GP.CutPower"] = 60
    d.values["GP.DrillHeight0"] = 7.5
    d.values["GP.ManuType"] = 2
    d._store()
    v = LayerView(lp)
    assert lp.duty_pct == 60 and v["GP.CutPower"] == 60          # duty, not the peak current
    assert v["GP.DrillHeight0"] == 7.5 and v.n_stages == 1


def test_large_plan_builds_its_timeline_on_play(win, app, monkeypatch):
    from mlaser.ui import ext_process

    ext = _ext(win)
    monkeypatch.setattr(ext_process, "LAZY_TIMELINE_ITEMS", 0)
    ext._plan(win.jobs.current)
    assert ext.timeline is None and ext.workspace.card.card is None
    assert ext.scrubber.play_btn.isEnabled()
    ext.scrubber.play()
    assert ext.timeline is not None and ext.scrubber.playing
    ext.scrubber.pause()
    assert ext.workspace.card.card is not None
