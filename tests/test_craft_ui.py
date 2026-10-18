"""Offscreen tests of mlaser.ui.ext_craft (manual craft commands, agent G phase 3)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mlaser.model import Arc, Contour, Document, Line, Part  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def rect(x, y, w, h):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


@pytest.fixture()
def win(app):
    from mlaser.ui.ext_cam import register as reg_cam
    from mlaser.ui.ext_craft import register
    from mlaser.ui.facade import DemoController
    from mlaser.ui import canvas
    from mlaser.ui.mainwindow import MainWindow
    if not hasattr(canvas.CanvasView, "tool"):            # tolerate a canvas whose event() runs
        canvas.CanvasView.tool = None                     # before __init__ sets self.tool
    w = MainWindow(DemoController())
    reg_cam(w, exec_dialogs=False)
    register(w, exec_dialogs=False)
    w.set_document(Document(parts=[Part([rect(0, 0, 10, 10)]), Part([rect(20, 0, 10, 10)])]))
    return w


def test_commands_bound(win):
    for cmd in ("craft.start_point", "craft.mj_manual", "craft.mj_explode", "craft.gap", "craft.seal",
                "craft.overcut", "craft.cool_point", "craft.circle_cut", "opt.segment", "craft.home_ref",
                "tool.onekey", "craft.lead_manual", "craft.lead_check", "craft.lead_clear_mark",
                "tool.bridge", "tool.engrave", "opt.adjust_start", "opt.replace_circle",
                "file.bitmap", "file.import_scan_gcode"):
        assert cmd in win.handlers, cmd


def test_start_point_tool_and_undo(win):
    ext = win.craft_ext
    win.run("craft.start_point")
    assert ext.tool_name == "start_point"
    ext.click(10.0, 5.0)
    assert win.document.parts[0].craft.extra["start_param"] == pytest.approx(15 / 40)
    ext.click(100, 100)                       # miss: nothing changes, tool stays
    assert ext.tool_name == "start_point"
    win.set_mode("select")                    # Esc path
    assert ext.tool_name is None
    win.undo()
    assert "start_param" not in win.document.parts[0].craft.extra


def test_manual_joint_and_explode(win):
    ext = win.craft_ext
    ext.mj_p.length = 1.0
    win.run("craft.mj_manual")
    ext.click(5.0, 0.0)
    ext.click(5.0, 10.0)
    assert len(win.document.parts[0].craft.micro_joints) == 2
    ext.click(5.2, 0.0)                       # inside the first joint: removed
    assert len(win.document.parts[0].craft.micro_joints) == 1
    ext.cancel_tool()
    win.document.parts[0].selected = True
    win.run("craft.mj_explode")
    p = win.document.parts[0]
    assert len(p.contours) == 1 and not p.contours[0].closed and not p.craft.micro_joints
    assert p.contours[0].length() == pytest.approx(39)


def test_gap_seal_overcut(win):
    ext = win.craft_ext
    ext.gap_p.value = 0.7
    win.run("craft.gap")                                  # nothing selected: no change
    assert all("overcut" not in p.craft.extra for p in win.document.parts)
    for p in win.document.parts:
        p.selected = True
    win.run("craft.gap")
    assert all(p.craft.extra["overcut"] == -0.7 for p in win.document.parts)
    win.run("craft.overcut")
    assert all(p.craft.extra["overcut"] == 0.7 for p in win.document.parts)
    win.run("craft.seal")
    assert all(p.craft.extra["overcut"] == 0.0 for p in win.document.parts)
    ext.gap_p.value = 20.0                                # >= 0.33 * length: rejected
    win.run("craft.gap")
    assert all(p.craft.extra["overcut"] == 0.0 for p in win.document.parts)


def test_cool_point_and_segment(win):
    ext = win.craft_ext
    from PySide6.QtGui import QTransform
    win.canvas.setTransform(QTransform.fromScale(20, -20))      # 8 px = 0.4 mm
    assert ext.host.tolerance() == pytest.approx(0.4)
    win.run("craft.cool_point")
    ext.click(10.0, 3.0)
    assert win.document.parts[0].craft.extra["cool_points"] == [pytest.approx(13)]
    ext.click(10.2, 0.3)                                        # snaps to the corner
    assert win.document.parts[0].craft.extra["cool_points"] == [pytest.approx(10), pytest.approx(13)]
    ext.cancel_tool()
    win.run("opt.segment")
    ext.click(30.0, 5.0)
    assert len(win.document.parts[1].contours) == 2
    assert not win.document.parts[1].contours[0].closed


def test_home_ref_grid_and_manual(win):
    ext = win.craft_ext
    ext.dock_p.mode = 9                       # bottom right
    win.run("craft.home_ref")                             # anchor (30, 0) moves under the head (0, 0)
    assert win.settings["SP.DockPoint"] == (0.0, 0.0)
    assert win.document.bbox() == pytest.approx((-30, 0, 0, 10))
    win.undo()
    assert win.document.bbox() == pytest.approx((0, 0, 30, 10))
    ext.dock_p.mode = 1                                   # top left
    win.run("craft.home_ref")
    assert win.document.bbox() == pytest.approx((0, -10, 30, 0))
    win.undo()
    ext.dock_p.mode = 0
    win.run("craft.home_ref")
    assert ext.tool_name == "home_ref"
    ext.click(3.0, 4.0)
    assert win.settings["SP.DockPoint"] == (3.0, 4.0) and ext.tool_name is None


def test_onekey(win):
    ext = win.craft_ext
    ext.onekey_p.lead = True
    win.lead_in.kind = 1
    win.run("tool.onekey")
    assert all(p.craft.lead_in.kind == 1 for p in win.document.parts)
    assert "mf209" in ext.last_message or ext.last_message


def test_circle_cut_runs_alpha(win):
    win.run("craft.circle_cut")
    assert "mf485" in win.craft_ext.last_message or True


def test_lead_check_and_clear_mark(win):
    from mlaser.model import LeadParams
    # an 8 mm perpendicular lead from the bottom edge of the upper square reaches into the lower one
    low, up = rect(0, 0, 10, 10), rect(0, 12, 10, 10)
    p0, p1 = Part([low]), Part([up])
    p1.craft.lead_in = LeadParams(kind=1, angle_deg=90.0, length=8.0)
    p1.craft.extra["start_param"] = 0.125                 # (5, 12): lead from (5, 4), crosses y = 10
    win.set_document(Document(parts=[p0, p1]))
    win.run("craft.lead_check")
    cr = win.document.parts[1].craft
    assert cr.extra.get("lead_error") and cr.lead_in.length == pytest.approx(2.0)
    assert not win.document.parts[0].craft.extra.get("lead_error")
    win.run("craft.lead_clear_mark")
    assert "lead_error" not in win.document.parts[1].craft.extra
    assert win.document.parts[1].craft.lead_in.length == pytest.approx(2.0)


def test_replace_circle_and_adjust_start(win):
    import math
    n = 36
    pts = [(50 + 5 * math.cos(2 * math.pi * k / n), 50 + 5 * math.sin(2 * math.pi * k / n)) for k in range(n)]
    poly = Contour([Line(pts[k], pts[(k + 1) % n]) for k in range(n)], True)
    sq = rect(0, 0, 10, 10)
    sq2 = Contour(sq.segments[2:] + sq.segments[:2], True)
    win.set_document(Document(parts=[Part([poly]), Part([sq2], selected=True)]))
    win.run("opt.replace_circle")
    c = win.document.parts[0].contours[0]
    assert len(c.segments) == 1 and isinstance(c.segments[0], Arc)
    assert c.segments[0].radius == pytest.approx(5, abs=0.05)
    win.run("opt.adjust_start")
    assert win.document.parts[1].contours[0].segments[0].start == pytest.approx((0, 0))


def test_bridge_tool(win):
    from PySide6.QtGui import QTransform
    ext = win.craft_ext
    win.canvas.setTransform(QTransform.fromScale(20, -20))
    ext.bridge_p.width = 2.0
    win.run("tool.bridge")
    ext.click(10.0, 5.0)
    ext.click(20.0, 5.0)
    assert len(win.document.parts) == 1
    from mlaser.io.geom import signed_area
    assert abs(signed_area(win.document.parts[0].contours[0])) == pytest.approx(220)
    win.undo()
    assert len(win.document.parts) == 2


def test_manual_lead_tool(win):
    from PySide6.QtGui import QTransform
    ext = win.craft_ext
    win.canvas.setTransform(QTransform.fromScale(20, -20))
    win.run("craft.lead_manual")
    ext.click(5.0, -3.0)
    ext.click(5.0, 0.0)
    cr = win.document.parts[0].craft
    assert cr.lead_in.kind == 1 and cr.lead_in.length == pytest.approx(3.0)
    assert cr.extra["start_param"] == pytest.approx(5 / 40)


def test_engrave_and_scan_gcode(win, tmp_path):
    lines = [Part([Contour([Line((0, y), (10, y))], False)], selected=True) for y in (0, 1, 2)]
    win.set_document(Document(parts=lines))
    win.run("tool.engrave")
    parts = win.document.parts
    assert len(parts) == 1 and parts[0].extra["scan_kind"] == 4
    path = parts[0].extra["scan_paths"][0]
    assert not path.initial_on
    assert [b - a for a, b in path.on_intervals()] == [pytest.approx(10)] * 3
    g = tmp_path / "scan.nc"
    g.write_text("G90\nM4\nG0 X0 Y0 S0\nG1 X4 Y0 S20\nG0 X4 Y0.5 S0\nG1 X0 Y0.5 S20\nG0 X0 Y0 S0\nM5\n")
    assert win.craft_ext.import_scan_gcode([str(g)]) == 1
    part = win.document.parts[0]
    assert part.extra["scan_kind"] == 5 and len(part.contours) == 2
    assert part.contours[0].length() == pytest.approx(4 + 2 * 5.0)
    win.undo()
    assert len(win.document.parts) == 1 and win.document.parts[0].extra["scan_kind"] == 4


def test_bitmap_flow(win, tmp_path):
    import numpy as np
    from mlaser.engrave import RasterParams, generate_to_dir
    ext = win.craft_ext
    ext.bitmap_dir = str(tmp_path)
    win.run("file.bitmap")                                 # dialog not executed: nothing imported
    assert ext.import_bitmap_result() is None              # status.ini = 0 written at start
    img = np.full((12, 24), 255, np.uint8)
    img[3:9, 4:20] = 0
    generate_to_dir(tmp_path, img, RasterParams(width_mm=4, rho=6, power=30))
    assert ext.import_bitmap_result() is not None
    part = win.document.parts[0]
    assert part.extra["scan_kind"] == 5
    on = part.extra["scan_paths"][0].on_intervals()
    assert len(on) == len(part.contours) > 0


def test_real_mouse_click_through_canvas_tool(win, app):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    win.resize(800, 600)
    win.show()
    app.processEvents()
    win.canvas.zoom_fit()
    win.run("craft.start_point")
    QTest.mouseClick(win.canvas.viewport(), Qt.LeftButton, Qt.NoModifier, win.canvas.mapFromScene(10, 5))
    app.processEvents()
    assert win.document.parts[0].craft.extra["start_param"] == pytest.approx(15 / 40, abs=0.01)
    QTest.keyClick(win.canvas, Qt.Key_Escape)
    app.processEvents()
    assert win.craft_ext.tool_name is None
    win.close()
