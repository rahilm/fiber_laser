"""Breakpoint resume, Backward/Forward along the path, AutosaveParam, UI pause semantics."""
from __future__ import annotations

import math
import time

import pytest

from mlaser.protocol import frames as F
from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part
from mlaser.nc import Machine, compile_job
from mlaser.nc import resume as R


def _rect(x, y, w, h):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def _doc():
    return Document([Part([_rect(0, 0, 40, 20)], Craft(layer=1)),
                     Part([_rect(60, 0, 20, 20)], Craft(layer=1))],
                    {1: LayerParams(1, speed=100, power_pct=50)})


@pytest.fixture(scope="module")
def job():
    return compile_job(_doc(), {"return_to_origin": False}, Machine(pulses_per_mm_x=1000.0, pulses_per_mm_y=1000.0),
                       start=(0.0, 0.0))


def _end(j):
    x, y = j.start_pulses
    for dx, dy, *_ in j.step_deltas():
        x += dx
        y += dy
    return x, y


def test_positions_and_markers(job):
    pos = R.pulse_positions(job)
    assert pos[0] == job.start_pulses and pos[-1] == job.end_pulses
    assert len(job.markers) >= 2
    k = len(job.markers) - 1
    mid = job.markers[k][0] + 10
    g, p = R.to_graph_path(job, mid)
    assert g == k and p == 10 and R.from_graph_path(job, g, p) == mid
    assert R.contour_start(job, mid) == job.markers[k][0]


@pytest.mark.parametrize("mode", ["exact", "contour"])
def test_resume_job_reaches_the_same_end(job, mode):
    idx = len(job.wire) // 2
    rj = R.resume_job(job, idx, current_mm=(5.0, 7.0), mode=mode)
    assert rj.start_pulses == (round(5.0 * 1000), round(7.0 * 1000))
    assert _end(rj) == job.end_pulses
    restart = R.contour_start(job, idx) if mode == "contour" else idx
    assert rj.wire[-1] == job.wire[-1]
    assert len(rj.wire) - (len(job.wire) - restart) == rj.resume_offset + restart
    # the rapid to the restart point is laser-off
    first_on = next(i for i, w in enumerate(rj.wire) if (w[0] & 0xFFFF) == F.OP_STEP and (w[1] >> 16) & 0xFF)
    assert first_on >= rj.resume_offset + restart - 1 or mode == "exact"


def test_resume_rapid_lands_on_the_restart_pulse(job):
    """The laser-off rapid to the restart point is compiled with a fresh pulse remainder and has
    no closing step, so on its own it can stop a pulse short; resume_job closes the residual,
    otherwise the rest of the job is shifted by that pulse (the flaky 1-pulse UI resume)."""
    pos = R.pulse_positions(job)
    for k, idx in enumerate(range(50, len(job.wire) - 1, max(1, len(job.wire) // 40))):
        for cur_p in ((pos[idx][0] + 3 + 7 * k, pos[idx][1]), (pos[idx][0] - 431, pos[idx][1] + 17),
                      (pos[idx][0] + 1, pos[idx][1])):          # a 1 µm rapid degenerates (NaN)
            cur = (cur_p[0] / 1000.0, cur_p[1] / 1000.0)
            rj = R.resume_job(job, idx, current_mm=cur, mode="exact")
            assert rj.start_pulses == cur_p
            pre = rj.wire[:rj.resume_offset + idx]
            x, y = rj.start_pulses
            for w in pre:
                if (w[0] & 0xFFFF) == F.OP_STEP:
                    dx, dy, _f, duty, _p = F.decode_step(w[1:3])
                    x, y = x + dx, y + dy
            assert (x, y) == pos[idx]
            assert _end(rj) == job.end_pulses


def test_along_path_backward_forward(job):
    idx = len(job.wire) * 2 // 3
    seg, new = R.along_path(job, idx, -5.0)
    assert new < idx and seg.wire
    assert all(F.decode_step(w[1:3])[3] == 0 for w in seg.wire)          # laser off
    assert _end(seg) == R.pulse_positions(job)[new]
    walked = sum(math.hypot(dx, dy) for dx, dy, *_ in seg.step_deltas()) / 1000.0
    assert walked == pytest.approx(5.0, abs=0.2)
    seg2, back = R.along_path(job, new, 5.0)
    pos = R.pulse_positions(job)
    assert _end(seg2) == pos[back]
    assert math.dist(pos[back], pos[idx]) / 1000.0 < 0.2       # back where we started


def test_breakpoint_store(tmp_path, job):
    st = R.BreakpointStore(tmp_path)
    p1 = st.save(1.2345, -2.0, 3, 4)
    p2 = st.save(5.0, 6.0, 7, 8)
    assert p1.name == "AutosaveParam1.ini" and p2.name == "AutosaveParam2.ini"
    rec = st.load()
    assert (rec.counter, rec.x_um, rec.y_um, rec.graph_index, rec.path_index) == (1, 5000, 6000, 7, 8)
    st2 = R.BreakpointStore(tmp_path)          # continues the counter
    assert st2.counter == 2
    idx = job.markers[1][0] + 3
    st2.save_job_point(job, idx)
    assert st2.wire_index(job) == idx


def _wait(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_ui_pause_resume_from_breakpoint(tmp_path):
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.02, autosave_dir=str(tmp_path))
    try:
        assert ctl.connect_simulator(time_scale=2.0)
        doc = _doc()
        ctl.start(doc)
        assert _wait(lambda: ctl.state().progress > 0.3, 20)
        ctl.pause()
        assert ctl.state().mode == "pause" and ctl.breakpoint is not None
        bp = ctl.breakpoint
        assert 0 < bp.index < len(bp.job.wire)
        assert (tmp_path / "AutosaveParam1.ini").exists()
        # program stop on the card: decelerated stop + PWM off + follower stop
        cmds = ctl.sim.state.commands
        assert any(c[:3] == [1, 0x1F, 2] for c in cmds)
        # PWM off at LC.PtLaserFreq (FUN_10030f70 sends cfg +0x48bc), not a fixed 5000
        assert [9999, 3, ctl.machine.pt_laser_freq, 0, 0] in cmds and [118, 5, 0] in cmds
        # backward 3 mm along the path while paused
        ctl.step_backward(3.0)
        assert _wait(lambda: ctl.breakpoint is not None and ctl.breakpoint.index < bp.index, 10)
        assert ctl.state().mode == "pause"
        ctl.resume()
        assert ctl.breakpoint is None
        assert ctl.wait_job(30)
        assert ctl.sim.wait_idle(5)
        st = ctl.mc.read_status()
        assert (st.axes[0].position, st.axes[1].position) == bp.job.end_pulses
        assert ctl.state().mode == "ready"
    finally:
        ctl.disconnect()


def test_ui_fifo_pause_mode():
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.02, pause_mode="fifo")
    try:
        assert ctl.connect_simulator(time_scale=2.0)
        ctl.start(_doc())
        assert _wait(lambda: ctl.state().progress > 0.2, 20)
        ctl.pause()
        assert ctl.state().mode == "pause" and ctl._busy()
        time.sleep(0.1)
        assert not ctl.sim.state.fifo_running
        ctl.resume()
        assert ctl.wait_job(30)
        st = ctl.mc.read_status()
        assert (st.axes[0].position, st.axes[1].position) == ctl.job.end_pulses
    finally:
        ctl.disconnect()
