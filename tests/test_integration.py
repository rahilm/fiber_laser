"""End to end: DXF -> CAM toolpath -> NC compile -> UDP stream -> in-process card simulator."""
from __future__ import annotations

import math
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

ezdxf = pytest.importorskip("ezdxf")

from mlaser.protocol import frames as F  # noqa: E402
from mlaser.controller.client import MotionController, TransportConfig  # noqa: E402
from mlaser.controller.simulator import CardSimulator  # noqa: E402
from mlaser.io.dxf_import import import_dxf  # noqa: E402
from mlaser.model import LayerParams  # noqa: E402
from mlaser.nc import Machine, compile_job  # noqa: E402
from mlaser.nc.stream import JobStreamer  # noqa: E402

PERIMETER = 2 * (100 + 60) + 2 * math.pi * 8 + 2 * math.pi * 15


@pytest.fixture
def dxf_path(tmp_path):
    """A plate (rectangle) with a round hole, plus a separate disc."""
    d = ezdxf.new()
    msp = d.modelspace()
    msp.add_lwpolyline([(10, 10), (110, 10), (110, 70), (10, 70)], close=True)
    msp.add_circle((40, 40), 8)
    msp.add_circle((150, 40), 15)
    p = tmp_path / "plate.dxf"
    d.saveas(p)
    return str(p)


def _doc(path):
    doc = import_dxf(path)
    for p in doc.parts:
        doc.layers.setdefault(p.craft.layer, LayerParams(p.craft.layer, speed=150.0, power_pct=50.0))
    return doc


def test_dxf_to_simulator(dxf_path):
    doc = _doc(dxf_path)
    assert sum(len(p.contours) for p in doc.parts) == 3
    m = Machine(rapid_vel=400.0)
    job = compile_job(doc, {"return_to_origin": False}, m, start=(0.0, 0.0))
    assert job.laser_on_length_mm == pytest.approx(PERIMETER, rel=0.01)
    assert job.pierces == 3
    sim = CardSimulator(period_ms=m.period_ms, time_scale=8.0).start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port)
        with MotionController(config=cfg, pulses_per_mm=m.pulses_per_mm_x) as mc:
            mc.connect()
            seen = []
            JobStreamer(mc).run_job(job, progress=lambda f, i: seen.append(f), timeout_s=60)
            assert seen and seen[-1] == 1.0
            assert sim.wait_idle(10)
            st = mc.read_status()
        # final position
        assert st.axes[0].position == job.end_pulses[0]
        assert st.axes[1].position == job.end_pulses[1]
        # laser-on path length from the steps the card actually executed
        steps = [F.decode_step(p) for op, p in sim.state.history if op == F.OP_STEP]
        assert len(steps) == job.steps
        on = sum(math.hypot(dx, dy) for dx, dy, _t, las, _p in steps if las) / m.pulses_per_mm_x
        assert on == pytest.approx(PERIMETER, rel=0.01)
        assert {p for *_x, las, p in steps if las} == {0xFFFF}   # ManuItem +8 (FUN_00462630, JB-02)
        assert not sim.state.fifo_running and not sim.state.laser
    finally:
        sim.stop()


def test_ui_runs_job_on_simulator(dxf_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from mlaser.controller.ui_adapter import UiController
    from mlaser.ui.mainwindow import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(UiController())
    try:
        assert win.load_path(dxf_path)
        assert win.toolpath and any(type(m).__name__ == "Rapid" for m in win.toolpath)
        for lp in win.document.layers.values():
            lp.speed = 150.0
        assert win.connect_simulator(time_scale=4.0)
        ctl = win.ctl
        app.processEvents()
        win.run("machine.home")                        # UNKNOWN protocol -> logged, no crash
        ctl.start(win.document)
        positions = set()
        end = time.monotonic() + 20
        while time.monotonic() < end:
            app.processEvents()
            win._poll()
            hp = win.canvas._head.pos()
            positions.add((round(hp.x(), 1), round(hp.y(), 1)))
            if len(positions) > 5 and ctl.state().progress > 0.2:
                break
            time.sleep(0.03)
        assert len(positions) > 5, "head marker did not follow the simulator"
        assert ctl.state().mode in ("process", "ready")
        ctl.pause()
        assert ctl.state().mode == "pause"
        ctl.resume()
        ctl.stop()
        assert not ctl._busy()
        log = win.log_panel.text.toPlainText()
        assert "homing is not supported" in log or "Go Origin" in log
        # simulate via mlaser.motion with a time estimate
        win.simulate()
        assert win.estimate["total"] > 0 and win.canvas.simulation_running()
        win.canvas.stop_simulation(clear=True)
    finally:
        win.close()


def test_ui_cam_commands(dxf_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(DemoController())
    try:
        assert win.load_path(dxf_path)
        n_before = len(win.toolpath)
        win.canvas.select_parts(range(len(win.document.parts)))
        win.microjoint_auto()                      # mlaser.cam.microjoint.auto_joints
        gaps = [m for m in win.toolpath if type(m).__name__ in ("CutLine", "CutArc") and not m.laser]
        assert gaps and len(win.toolpath) > n_before
        win.run("sort.left_right")
        assert win.cam_params.sort_mode == 1
        order = win.canvas.contour_order()
        assert len(order) == 3
        win.run("craft.outside")                   # kerf via Craft -> cam.kerf in the toolpath
        assert all(p.craft.kerf_comp == 1 for p in win.document.parts)
        app.processEvents()
    finally:
        win.close()
