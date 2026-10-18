"""Drawing tools: state machines, the canvas interaction-tool hook and the ext_draw commands."""
from __future__ import annotations

import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mlaser.cad import primitives as P  # noqa: E402
from mlaser.cad import tools as T  # noqa: E402
from mlaser.model import Arc, Contour, Document, Line, Part  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def win(app):
    from wiring import real_window
    w = real_window()
    w.resize(800, 600)
    yield w
    w.close()


# ------------------------------------------------------------------ pure state machines
def run(tool, *clicks, finish=False):
    for c in clicks:
        tool.hover(c)
        tool.click(c)
    if finish:
        tool.finish()
    return tool


def test_two_and_three_click_tools():
    assert run(T.LineTool(), (0, 0), (10, 0)).done
    c = run(T.CircleTool(), (0, 0), (3, 4)).result()[0]
    assert c.segments[0].radius == pytest.approx(5)
    rr = run(T.RoundRectTool(), (0, 0), (40, 20), (40, 23)).result()[0]
    assert len(rr.segments) == 8
    st = run(T.StarTool(), (0, 0), (10, 0), (4, 0)).result()[0]
    assert len(st.segments) == 12
    el = run(T.EllipseTool(), (0, 0), (10, 0), (0, 5))
    assert el.done and el.result()[0].extra["ellipse"]["ratio"] == pytest.approx(0.5)
    a3 = run(T.Arc3Tool(), (10, 0), (0, 10), (-10, 0)).result()[0]
    assert a3.segments[0].ccw
    sa = run(T.ScanArcTool(), (0, 0), (5, 0), (-1, 0)).result()[0]
    assert sa.segments[0].sweep() == pytest.approx(math.pi)


def test_min_distance_and_degenerate_clicks():
    t = T.LineTool(tol=0.5)
    run(t, (0, 0), (0.1, 0.2))
    assert not t.done and len(t.points) == 1       # ignored (within tol in x and y)
    t = T.Circle3Tool()
    run(t, (0, 0), (1, 1), (2, 2))                  # collinear -> waits
    assert not t.done and len(t.points) == 2
    t.click((2, 0))
    assert t.done
    r = T.RectTool(tol=0.5)
    run(r, (0, 0), (10, 0.2))                       # |dy| < tol: rejected by CCreateRect
    assert not r.done


def test_polyline_line_arc_close():
    t = T.PolylineTool()
    run(t, (0, 0), (10, 0))
    t.key("a")
    t.click((10, 10))                                # tangent arc
    t.key("l")
    t.click((0, 10))
    assert t.preview()
    t.key("c")
    assert t.done
    c = t.result()[0]
    assert c.closed and isinstance(c.segments[1], Arc) and len(c.segments) == 4
    t2 = run(T.PolylineTool(), (0, 0), (5, 5), (10, 0), finish=True)
    assert t2.done and not t2.result()[0].closed
    t3 = run(T.PolylineTool(), (0, 0))
    assert not t3.finish()                          # needs two vertices


def test_curve_and_backspace():
    t = T.CurveTool()
    run(t, (0, 0), (10, 10), (20, 0), (30, 10))
    t.key("backspace")
    assert len(t.points) == 3
    assert t.finish() and t.result()[0].extra["spline_fit"][-1] == (20.0, 0.0)


def test_polygon_sides_keys():
    t = T.PolygonTool()
    t.key("+")
    t.key("+")
    run(t, (0, 0), (10, 0))
    assert len(t.result()[0].segments) == 8


def test_measure_and_place():
    m = run(T.MeasureTool(), (1, 1), (4, 5))
    assert m.done and m.measurement == (3, 4, 5) and m.result() == []
    tmpl = [P.rect((0, 0), (2, 1))]
    p = T.PlaceTool(tmpl)
    p.hover((10, 10))
    assert p.preview()
    p.click((10, 10))
    assert p.result()[0].segments[0].start == (10, 10)


# ------------------------------------------------------------------ canvas hook (simulated mouse)
def _vp(win, x, y) -> QPoint:
    return win.canvas.mapFromScene(x, y)


def test_canvas_hook_draws_rect_with_real_mouse_events(win):
    win.show()
    QApplication.processEvents()
    c = win.canvas
    c.setTransform(c.transform().fromScale(2, -2))
    c.centerOn(25, 25)
    win.run("draw.rect")
    assert c.tool is not None
    vp = c.viewport()
    p0, p1 = _vp(win, 10, 10), _vp(win, 40, 30)
    QTest.mouseMove(vp, p0)
    QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, p0)
    QTest.mouseMove(vp, p1)
    assert c._preview_items                      # rubber band shown
    n0 = len(win.document.parts)
    QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, p1)
    assert len(win.document.parts) == n0 + 1
    new = win.document.parts[-1]
    assert new.selected and new.contours[0].closed
    x0, y0, x1, y1 = new.bbox()
    assert (x1 - x0) == pytest.approx(30, abs=1.0) and (y1 - y0) == pytest.approx(20, abs=1.0)
    assert c.tool is None and not c._preview_items
    win.undo()
    assert len(win.document.parts) == n0


def test_canvas_escape_cancels_and_snaps(win):
    win.show()
    c = win.canvas
    win.set_document(Document(parts=[Part([P.rect((0, 0), (20, 20))])]))
    c.setTransform(c.transform().fromScale(4, -4))
    c.centerOn(10, 10)
    win.run("draw.line")
    vp = c.viewport()
    QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, _vp(win, 0.4, 0.3))     # snaps to (0, 0)
    QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, _vp(win, 10.2, 19.8))   # snaps to mid (10, 20)
    ln = win.document.parts[-1].contours[0].segments[0]
    assert ln.start == pytest.approx((0, 0)) and ln.end == pytest.approx((10, 20))
    win.run("draw.pline")
    QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, _vp(win, 5, 5))
    QTest.keyClick(vp, Qt.Key_Escape)
    assert c.tool is None
    assert len(win.document.parts) == 2


def test_driver_polyline_right_click_finish(win):
    ext = win.extension("ext_draw")
    win.set_document(Document())
    win.canvas.setTransform(win.canvas.transform().fromScale(10, -10))   # 0.1 mm per pixel
    ext.draw_tool("draw.pline")
    d = win.canvas.tool
    for p in ((0, 0), (10, 0), (10, 10)):
        d.move(*p, set())
        d.press(*p, "left", set())
    d.press(10, 10, "right", set())
    c = win.document.parts[-1].contours[0]
    assert len(c.segments) == 2 and not c.closed
    assert win.canvas.tool is None


# ------------------------------------------------------------------ commands
def test_text_and_qr_commands(win):
    ext = win.extension("ext_draw")
    win.set_document(Document())
    win.canvas.setTransform(win.canvas.transform().fromScale(10, -10))
    ext.text.text, ext.text.height = "A", 20.0
    win.run("draw.text")
    d = win.canvas.tool
    d.move(100, 100, set())
    d.press(100, 100, "left", set())
    part = win.document.parts[-1]
    assert part.contours and part.bbox()[0] == pytest.approx(100) and part.bbox()[1] == pytest.approx(100)
    ext.qr.text, ext.qr.density = "Mlaser", 3
    win.run("draw.qr")
    win.canvas.tool.press(0, 0, "left", set())
    q = win.document.parts[-1]
    assert len(q.contours) > 10 and q.bbox()[2] <= 25.0 + 1e-6


def _zoom(win, s=10):
    win.canvas.setTransform(win.canvas.transform().fromScale(s, -s))


def test_trim_extend_split_commands_and_undo(win):
    ext = win.extension("ext_draw")
    win.set_document(Document(parts=[Part([Contour([Line((0, 0), (100, 0))], False)], name="h"),
                                     Part([Contour([Line((50, -10), (50, 10))], False)], name="v")]))
    _zoom(win)
    win.run("draw.trim")
    tool = win.canvas.tool
    tool.press(80, 0, "left", set())
    hs = [p for p in win.document.parts if p.name.startswith("h")]
    assert len(hs) == 1 and hs[0].bbox()[2] == pytest.approx(50)
    tool.press(500, 500, "left", set())             # miss: no extra undo step
    n_undo = len(win.docs.stack)
    tool.press(500, 500, "left", set())
    assert len(win.docs.stack) == n_undo
    tool.press(0, 0, "right", set())                 # ends the pick tool
    assert win.canvas.tool is None
    win.undo()
    assert [p for p in win.document.parts if p.name == "h"][0].bbox()[2] == pytest.approx(100)
    win.set_document(Document(parts=[Part([Contour([Line((0, 0), (10, 0))], False)], name="a"),
                                     Part([Contour([Line((30, -5), (30, 5))], False)], name="w")]))
    _zoom(win)
    win.run("draw.extend")
    win.canvas.tool.press(9, 0, "left", set())
    assert win.document.parts[0].bbox()[2] == pytest.approx(30)
    win.run("geom.split")
    win.canvas.tool.press(15, 0, "left", set())
    assert len(win.document.parts) == 3


def test_edit_and_select_commands(win):
    ext = win.extension("ext_draw")
    doc = Document(parts=[Part([P.rect((0, 0), (10, 10))], selected=True),
                          Part([P.rect((20, 0), (30, 10))]),
                          Part([P.rect((0, 50), (5, 70))])])
    win.set_document(doc)
    win.run("select.similar")
    assert [p.selected for p in win.document.parts] == [True, True, False]
    win.run("tool.group")
    assert len(win.document.parts) == 2 and win.document.parts[0].extra.get("group")
    for p in win.document.parts:
        p.selected = False
    win.run("tool.group_select_all")
    assert win.document.parts[0].selected
    win.run("tool.group_explode")
    assert len(win.document.parts) == 3
    for p in win.document.parts:
        p.selected = True
    win.run("geom.explode")
    assert len(win.document.parts) == 12
    ext.merge.tolerance = 0.1
    win.run("geom.merge")
    assert len(win.document.parts) == 3 and all(p.contours[0].closed for p in win.document.parts)
    for p in win.document.parts:
        p.selected = True
    win.run("xform.align")
    assert all(p.bbox()[0] == pytest.approx(0) for p in win.document.parts)
    win.run("select.inner")       # mf651: depth 1
    assert all(p.selected for p in win.document.parts)
    win.run("select.outer")       # mf652: depth 2 (none here)
    assert not any(p.selected for p in win.document.parts)


def test_size_adv_advert_measure_fine_tune(win):
    ext = win.extension("ext_draw")
    win.set_document(Document(parts=[Part([P.rect((0, 0), (100, 50))], selected=True)]))
    ext.size_dialog()                               # exec_dialogs=False keeps the current size
    ext.size.width, ext.size.height, ext.size.lock, ext.size.anchor = 200, 50, True, 7
    win.document.parts[0].selected = True
    # apply the stored values (dialog fields are refreshed from the selection on open)
    from mlaser.cad import arrange as AR
    win._edit(lambda d: AR.resize([p for p in d.parts if p.selected], 200, 50, True, 7))
    assert win.document.parts[0].bbox() == pytest.approx((0, 0, 200, 100))
    ext.adv.kind, ext.adv.offset = 0, 2.0
    win.run("draw.adv_text")
    assert len(win.document.parts) == 2
    assert win.document.parts[1].bbox() == pytest.approx((2, 2, 198, 98))
    win.document.parts[1].selected = False
    ext.fill.radius, ext.fill.stock, ext.fill.space, ext.fill.fill_type = 4, 5, 5, 0
    win.run("draw.advert_text")
    assert len(win.document.parts) > 10
    _zoom(win)
    win.run("tool.measure")
    t = win.canvas.tool
    t.press(0, 0, "left", {"shift"})
    t.press(30, 40, "left", {"shift"})
    assert ext.last_measure == (30, 40, 50)
    win.run("draw.fine_tune")
    assert win.ribbon.actions["draw.fine_tune"].isChecked() == ext.fine.auto
    win.set_document(Document(parts=[Part([P.rect((0, 0), (100, 100))], selected=True)]))
    win.nudge(1, 0)                                 # auto step: 0.02 * 100 = 2 mm
    assert win.document.parts[0].bbox()[0] == pytest.approx(2)
    ext.fine.auto = False
    ext.fine.step = 0.5
    win.nudge(0, -1)
    assert win.document.parts[0].bbox()[1] == pytest.approx(-0.5)


def test_command_coverage_for_draw_commands(app):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "command_coverage", os.path.join(os.path.dirname(__file__), "..", "tools", "command_coverage.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    r = mod.coverage()
    mine = set(T.COMMAND_IDS) | {"draw.fine_tune"}
    assert not (mine & set(r["unimplemented"]))
    assert not (mine & set(r["errors"]))
