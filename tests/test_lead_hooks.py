"""Lead-integration hooks: smoothing in the planner, fly cut and rapid avoidance in the toolpath."""
from __future__ import annotations

import math

import pytest

from mlaser.toolpath import items as I
from mlaser.cam.toolpath import CamParams, build_toolpath
from mlaser.model import Arc, Contour, Craft, Document, LayerParams, Line, Part
from mlaser.motion.interp import MotionConfig, estimate_time, plan_path, simulate
from mlaser.nc import Machine, compile_job


def _rect(x, y, w, h):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def test_smoothing_blends_corners_and_keeps_endpoints():
    moves = [I.CutLine(a.start, a.end, 200.0) for a in _rect(0, 0, 50, 30).segments]
    p0 = plan_path(moves, MotionConfig(smooth=False))
    p1, g = plan_path(moves, MotionConfig(smooth=True), return_geom=True)
    assert g.cum[-1] < p0.length                 # corners are cut by the blends
    assert p0.length - g.cum[-1] < 4 * 0.5     # ... but only slightly (tolerance-sized blends)
    assert math.dist(g.at(g.cum[-1])[0], moves[-1].end) < 1e-6
    sim = simulate(moves, MotionConfig(smooth=True))
    assert math.dist((sim[-1].x, sim[-1].y), moves[-1].end) < 1e-3


def test_compile_with_smoothing_preserves_pulse_sum():
    doc = Document([Part([_rect(0, 0, 40, 20)], Craft(layer=1))], {1: LayerParams(1, speed=100)})
    job = compile_job(doc, {"return_to_origin": True}, Machine(smooth=True))
    d = job.step_deltas()
    assert (job.start_pulses[0] + sum(a[0] for a in d), job.start_pulses[1] + sum(a[1] for a in d)) == job.end_pulses


def test_avoid_cut_routes_rapids_and_layer_is_set():
    # two holes in a plate; the rapid from hole 1 to hole 2 passes over hole 1's position otherwise
    plate = _rect(0, 0, 100, 40)
    h1, h2 = _rect(10, 15, 10, 10), _rect(70, 15, 10, 10)
    doc = Document([Part([plate, h1, h2], Craft(layer=1))], {1: LayerParams(1, speed=100)})
    base = [it for it in build_toolpath(doc, CamParams()) if isinstance(it, I.Rapid)]
    avoid = [it for it in build_toolpath(doc, CamParams(avoid_cut=True)) if isinstance(it, I.Rapid)]
    assert len(avoid) >= len(base)
    ends = lambda rs: {tuple(map(lambda v: round(v, 6), r.end)) for r in rs}
    assert ends(base) <= ends(avoid)             # every original rapid target is still reached


def test_fly_cut_part_is_cut_as_scan_path():
    from mlaser.cam import flycut as F
    circ = lambda x: Contour([Arc((x + 2, 0.0), (x + 2, 0.0), (x, 0.0), True)], True)
    doc = Document([Part([circ(x)], Craft(layer=1)) for x in (0, 10, 20)] + [Part([_rect(0, 20, 5, 5)], Craft(layer=1))],
                   {1: LayerParams(1, speed=100)})
    for p in doc.parts:
        p.selected = True
    exact = F.CircleFlyParams(lag_deg=0, lag_max=0, overlap_deg=0, overlap_max=0)
    assert F.apply_fly_cut(doc, F.SCAN_CIRCLE, exact) is not None
    items = build_toolpath(doc, CamParams())
    markers = [it for it in items if isinstance(it, I.Marker)]
    assert [m.kind for m in markers].count("scan") == 1
    assert [m.kind for m in markers].count("contour") == 1      # the rectangle is still a normal contour
    seg_len = lambda it: (Arc(it.start, it.end, it.center, it.ccw).length() if isinstance(it, I.CutArc)
                          else math.dist(it.start, it.end))
    on = sum(seg_len(it) for it in items if isinstance(it, (I.CutArc, I.CutLine)) and it.laser)
    assert on >= 3 * 2 * math.pi * 2 - 1e-3
    job = compile_job(doc, None, Machine())
    assert job.laser_on_length_mm == pytest.approx(on, rel=0.02)


def test_app_window_loads_all_extensions():
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import EXTENSIONS, MainWindow
    app = QApplication.instance() or QApplication([])
    w = MainWindow(DemoController(), extensions=EXTENSIONS)
    try:
        missing = [n for n in EXTENSIONS if n.rsplit(".", 1)[-1] not in w.extensions]
        assert not missing, missing
        assert not any("extension" in line and "failed" in line for line in w.log_panel.lines()) \
            if hasattr(w.log_panel, "lines") else True
    finally:
        w.close()


def test_per_contour_craft_overrides_part_craft():
    from mlaser.model import LeadParams
    plate, hole = _rect(0, 0, 100, 40), _rect(40, 15, 10, 10)
    doc = Document([Part([plate, hole], Craft(layer=1, lead_in=LeadParams(kind=0)))], {1: LayerParams(1, speed=100)})
    hole.extra["craft"] = Craft(layer=1, lead_in=LeadParams(kind=1, length=3.0))
    items = build_toolpath(doc, CamParams())
    cut = lambda: sum(math.dist(it.start, it.end) for it in items if isinstance(it, I.CutLine) and it.laser)
    assert cut() == pytest.approx(2 * (100 + 40) + 4 * 10 + 3.0, abs=0.05)    # only the hole has a 3 mm lead


def test_size_presets_scale_about_centre():
    from mlaser.ui import docops
    doc = Document([Part([_rect(10, 10, 20, 10)], Craft(layer=1))])
    docops.scale(doc, 2.0)
    assert doc.bbox() == pytest.approx((0, 5, 40, 25))
    docops.scale_to(doc, 100.0)
    b = doc.bbox()
    assert max(b[2] - b[0], b[3] - b[1]) == pytest.approx(100.0)
    assert ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) == pytest.approx((20.0, 15.0))


def test_imported_drawing_cuts_the_same_after_chf_round_trip(tmp_path):
    """A fresh import and its saved/reloaded .chf must give the same job: the toolpath keeps the
    stored start (default 0) like the original, instead of picking a long-side start itself."""
    ezdxf = pytest.importorskip("ezdxf")
    from mlaser.formats.chf import load_chf, save_chf
    from mlaser.io.dxf_import import import_dxf
    d = ezdxf.new()
    m = d.modelspace()
    m.add_lwpolyline([(0, 0), (10, 0), (10, 40), (0, 40)], close=True)    # long sides are vertical
    m.add_circle((30, 20), 5)
    m.add_line((50, 0), (70, 10))
    src = tmp_path / "t.dxf"
    d.saveas(src)
    doc = import_dxf(str(src))
    save_chf(doc, str(tmp_path / "t.chf"))
    j1 = compile_job(doc, None, Machine(), start=(0, 0))
    j2 = compile_job(load_chf(str(tmp_path / "t.chf")), None, Machine(), start=(0, 0))
    assert j1.steps == j2.steps
    assert j1.step_deltas() == j2.step_deltas()


def test_micro_joint_without_stored_width_keeps_default_after_chf(tmp_path):
    from mlaser.formats.chf import load_chf, save_chf
    p = Part([_rect(0, 0, 40, 20)], Craft(layer=1, micro_joints=[10.0]))
    save_chf(Document([p], {1: LayerParams(1)}), str(tmp_path / "j.chf"))
    c = load_chf(str(tmp_path / "j.chf")).parts[0].craft
    widths = c.extra.get("micro_joint_widths", [])
    assert widths and widths[0] == pytest.approx(0.5)


def test_chf_saves_micro_joints_given_as_position_width_pairs(tmp_path):
    from mlaser.formats.chf import load_chf, save_chf
    p = Part([_rect(0, 0, 40, 20)], Craft(layer=1, micro_joints=[(10.0, 0.8), 30.0]))
    save_chf(Document([p], {1: LayerParams(1)}), str(tmp_path / "pairs.chf"))      # used to raise list/float
    c = load_chf(str(tmp_path / "pairs.chf")).parts[0].craft
    assert c.extra["micro_joint_widths"][0] == pytest.approx(0.8)
    assert len(c.micro_joints) == 2


def test_sim_estimate_message_is_formatted():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from mlaser.ui import docops
    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import MainWindow
    app = QApplication.instance() or QApplication([])
    w = MainWindow(DemoController())
    try:
        w.set_document(docops.demo_document())
        lines = []
        w.log_panel.log = lambda m, *a, **k: lines.append(m)
        w.simulate()
        est = [l for l in lines if l.startswith("Estimated time:")]
        assert est, (lines, w.jobs.preview and (w.jobs.preview.cancelled, w.jobs.preview.error))
        assert "%" not in est[0] and "{" not in est[0] and " min " in est[0]
    finally:
        from wiring import dispose
        dispose(w)


def test_smoothed_compile_is_not_quadratic():
    """ContourSmoother._locate used to rebuild a list of all pieces per 1 ms sample, making
    compile O(samples x pieces): 36 s for a 839-element scan drawing. Guard the per-sample cost."""
    import time
    from mlaser.motion.smooth import ContourSmoother
    n = 400                                        # a 400-gon: many pieces, many samples
    pts = [(50 * math.cos(2 * math.pi * k / n), 50 * math.sin(2 * math.pi * k / n)) for k in range(n)]
    doc = Document([Part([Contour([Line(pts[k], pts[(k + 1) % n]) for k in range(n)], True)], Craft(layer=1))],
                   {1: LayerParams(1, speed=20)})
    calls = {"n": 0}
    orig = ContourSmoother._locate

    def counting(self, s):
        calls["n"] += 1
        return orig(self, s)
    from mlaser.parity_ports import use_profile
    ContourSmoother._locate = counting
    try:
        t = time.perf_counter()
        with use_profile("original-python"):         # the port: _locate once per sample
            job = compile_job(doc, None, Machine(smooth=True))
        dt = time.perf_counter() - t
    finally:
        ContourSmoother._locate = orig
    assert job.steps > 10000 and calls["n"] > 1000
    assert dt / calls["n"] < 2e-4                   # per-sample cost stays O(log pieces)
    # the default profile (the C kernel samples the path when it is built): per step as well
    t = time.perf_counter()
    job = compile_job(doc, None, Machine(smooth=True))
    assert (time.perf_counter() - t) / job.steps < 2e-4
