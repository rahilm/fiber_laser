"""Edge seek (mlaser.controller.edgeseek) against the simulator's rotated sheet."""
import math

import pytest

from mlaser.controller import edgeseek as E
from mlaser.controller.client import MotionController, TransportConfig
from mlaser.controller.simulator import CardSimulator, SimSheet
from mlaser.model import Contour, Document, Line, Part


def _rig(sheet, **kw):
    sim = CardSimulator(sheet=sheet, time_scale=kw.pop("time_scale", 20.0), **kw).start()
    mc = MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port))
    mc.open()
    return sim, mc


def _params(**kw):
    p = E.EdgeSeekParams(board_x=400, board_y=300, current_start=False, start=(130.0, 80.0),
                         move_speed=400.0, move_acc=4000.0, timeout_s=20)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_geometry_helpers():
    assert E.line_intersection((0, 0), (1, 0), (2, -1), (2, 1)) == pytest.approx((2, 0))
    assert E.project_on_line((0, 0), (10, 0), (3, 7)) == pytest.approx((3, 0))
    assert E.circle_centre((1, 0), (-1, 0), (0, 1)) == pytest.approx((0, 0))
    a = math.radians(3)
    pts = [(0, 0), (math.cos(a) * 100, math.sin(a) * 100),
           (0, 0), (-math.sin(a) * 50, math.cos(a) * 50)]
    assert E.edge_angle(pts, 4) == pytest.approx(a, abs=1e-12)
    # not square: half of the error is added (case 0x2a, 0x5c5b43)
    pts2 = pts[:3] + [(-math.sin(a + 0.01) * 50, math.cos(a + 0.01) * 50)]
    assert E.edge_angle(pts2, 4) == pytest.approx(a + 0.005, abs=1e-9)
    assert E.two_point_angle((10, 0), (0, 0)) == pytest.approx(0.0)


def test_plan_points_and_directions():
    p = _params()
    plan = E.plan_points((0, 0), 4, p)
    assert [sp.start for sp in plan] == [(0, 0), (200, 0), (0, 0), (0, 150)]
    assert [(sp.axis, sp.sign) for sp in plan] == [(1, -1), (1, -1), (0, -1), (0, -1)]
    p.dock_type = 3
    assert p.directions() == (-1, -1)
    p.dock_type = 5
    with pytest.raises(E.EdgeSeekError):
        p.directions()
    assert E.edge_seek_command(_params())[:6] == [117, 2000, 2000, 30000, 20, 400]


@pytest.mark.parametrize("mode", [3, 4])
def test_capacitive_finds_rotated_sheet(mode):
    corner, ang = (100.0, 50.0), 2.0
    sim, mc = _rig(SimSheet.rect(*corner, 1000, 600, ang))
    try:
        res = E.EdgeSeeker(mc, _params(), poll_s=0.002).run_capacitive(mode)
        assert len(res.points) == mode
        assert res.corner == pytest.approx(corner, abs=0.05)
        assert res.angle_deg == pytest.approx(ang, abs=0.02)
        assert res.origin == pytest.approx((corner[0] + 5, corner[1] + 5), abs=0.05)
        # head ends at the origin (corner + offset)
        pos = mc.position_mm()
        assert (pos[0], pos[1]) == pytest.approx(res.origin, abs=0.05)
        cmds = [c for c in sim.state.commands if c and c[0] == 117]
        assert len(cmds) == mode
    finally:
        mc.close()
        sim.stop()


def test_disc_search_centre():
    sim, mc = _rig(SimSheet.disc(300.0, 200.0, 80.0))
    try:
        res = E.EdgeSeeker(mc, _params(), poll_s=0.002).run_disc((310.0, 190.0))
        assert res.centre == pytest.approx((300.0, 200.0), abs=0.1)
    finally:
        mc.close()
        sim.stop()


def test_rotation_applied_to_job():
    from mlaser.nc import compile_job
    from mlaser.nc.compiler import placed_document
    sq = Contour([Line((0, 0), (10, 0)), Line((10, 0), (10, 10)), Line((10, 10), (0, 10)),
                  Line((0, 10), (0, 0))])
    doc = Document(parts=[Part([sq])])
    E.apply_to_document(doc, math.radians(90), (0.0, 0.0))
    d2 = placed_document(doc)
    seg = d2.parts[0].contours[0].segments[0]
    assert seg.end == pytest.approx((0.0, 10.0), abs=1e-9)
    job = compile_job(doc)
    assert job.wire
    E.clear_document(doc)
    assert placed_document(doc).parts[0].contours[0].segments[0].end == pytest.approx((10, 0))


def test_machine_ops_edge_seek_on_ui_controller():
    """UiController + in-process simulator with a rotated sheet; result rotates the document."""
    from mlaser.controller.machine_ops import MachineOps
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.01)
    assert ctl.connect_simulator(time_scale=20.0, sheet=SimSheet.rect(100.0, 50.0, 1000, 600, -1.5))
    try:
        ops = MachineOps(ctl, {"GRP.EdgeBoardSizeX": 400, "GRP.EdgeBoardSizeY": 300,
                               "GRP.EdgeSeekStartPointX": 130, "GRP.EdgeSeekStartPointY": 80,
                               "MC.XFastMoveSpeed": 400})
        got = []
        t = ops.edge_seek_capacitive(4, apply=got.append, wait=True)
        assert t is not None and got, "edge seek did not finish"
        res = got[0]
        assert res.angle_deg == pytest.approx(-1.5, abs=0.02)
        doc = Document(parts=[Part([Contour([Line((0, 0), (10, 0))])])])
        E.apply_to_document(doc, res.angle, res.origin)
        assert doc.extra["rotated"] and doc.extra["rotate_angle"] == pytest.approx(math.radians(-1.5), abs=4e-4)
    finally:
        ctl.disconnect()
