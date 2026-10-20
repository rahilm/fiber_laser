"""PlannedJob / JobService (mlaser.services.jobs): the plan is the compile, the preview is the
plan, Start streams the plan, and the streamed frames are ``Job.frames()``."""

from __future__ import annotations

import copy
import time

import pytest

from mlaser import ops
from mlaser.cam.toolpath import CamParams
from mlaser.model import LayerParams
from mlaser.nc import compile_job
from mlaser.nc.machine import Machine
from mlaser.services.config import ConfigService
from mlaser.services.document import DocumentService
from mlaser.services.jobs import (
    InlineExecutor,
    JobRequest,
    JobService,
    JobTicket,  # noqa: F401
    build_request,
    inputs_hash,
)


def demo_doc():
    d = ops.demo_document()
    for p in d.parts:
        d.layers.setdefault(p.craft.layer, LayerParams(index=p.craft.layer, speed=120.0))
    return d


def markers(items):
    return [(m.part, m.contour) for m in items if type(m).__name__ == "Marker" and m.kind == "contour"]


def svc_inline():
    return JobService(executor=InlineExecutor(), debounce_s=0)


def req_for(doc, start=(0.0, 0.0), machine=None, mode="process", cam=None):
    return JobRequest(
        mode=mode,
        document=copy.deepcopy(doc),
        machine=machine or Machine(),
        start=start,
        cam_params=cam or CamParams(),
        dry_run=mode != "process",
    )


def test_plan_is_exactly_compile_job():
    doc = demo_doc()
    start = (410.0, 70.0)
    plan = svc_inline().plan_sync(req_for(doc, start))
    job = compile_job(copy.deepcopy(doc), CamParams(), Machine(), start=start)
    assert plan.job.wire == job.wire
    assert plan.frames() == job.frames() and plan.frames(17) == job.frames(17)
    assert markers(plan.toolpath) == markers(job.toolpath)
    assert plan.start == start and plan.mode == "process" and not plan.dry_run
    x0, y0, x1, y1 = plan.bounds
    assert x0 <= 0.0 and x1 >= 420.0 and plan.time_s > 0 and plan.estimate["total"] > 0


def test_preview_origin_is_the_cut_origin():
    """The old preview sorted from CamParams.origin (0, 0); the cut sorts from the head."""
    from mlaser.cam.toolpath import build_toolpath

    doc = demo_doc()
    head = (420.0, 80.0)
    old_preview = build_toolpath(copy.deepcopy(doc), CamParams())
    cut = compile_job(copy.deepcopy(doc), CamParams(), Machine(), start=head)
    rapids = lambda items: [(m.start, m.end) for m in items if type(m).__name__ == "Rapid"]  # noqa: E731
    assert rapids(old_preview)[0][0] == (0.0, 0.0) and rapids(cut.toolpath)[0][0] == head
    assert list(old_preview) != list(cut.toolpath)  # the bug: preview != cut
    plan = svc_inline().plan_sync(req_for(doc, head))
    assert list(plan.toolpath) == list(cut.toolpath)  # now: preview == plan == cut
    assert plan.job.wire == cut.wire


def test_hash_and_cache():
    doc = demo_doc()
    js = svc_inline()
    a = js.plan_sync(req_for(doc))
    b = js.plan_sync(req_for(doc))
    assert a is b and js.stats["cache_hits"] == 1 and js.stats["compiles"] == 1
    sel = copy.deepcopy(doc)
    sel.parts[0].selected = True  # selection does not change the job
    assert inputs_hash(req_for(sel)) == inputs_hash(req_for(doc))
    moved = copy.deepcopy(doc)
    ops.translate(moved, 1.0, 0.0)
    assert inputs_hash(req_for(moved)) != inputs_hash(req_for(doc))
    assert inputs_hash(req_for(doc, (1.0, 0.0))) != inputs_hash(req_for(doc))
    assert inputs_hash(req_for(doc, machine=Machine(rapid_vel=400.0))) != inputs_hash(req_for(doc))
    assert inputs_hash(req_for(doc, mode="dry_run")) != inputs_hash(req_for(doc))
    assert inputs_hash(req_for(doc, cam=CamParams(sort_mode=1))) != inputs_hash(req_for(doc))


def test_cancel_and_supersede():
    doc = demo_doc()
    js = svc_inline()
    js.on_progress.append(lambda t, frac: t.cancel())  # cancel at the first progress step
    t = js.plan(req_for(doc))
    assert t.plan is None and t.cancelled and js.stats["cancelled"] == 1
    js.on_progress.clear()
    seen = []
    js.on_planned.append(lambda plan, ticket: seen.append(plan))
    js.request_preview(req_for(doc))
    t2 = js.request_preview(req_for(doc, (5.0, 5.0)))
    assert js.preview is t2 and js.current is t2.plan and len(seen) == 2


def test_debounce_coalesces_edits():
    calls = []
    timers = []
    js = JobService(executor=InlineExecutor(), schedule=lambda d, fn: (timers.append(fn), lambda: None)[1])
    doc = demo_doc()
    for i in range(5):
        js.schedule_preview(lambda i=i: (calls.append(i), req_for(doc))[1])
    assert calls == [] and js.pending
    timers[-1]()  # the timer fires once
    assert calls == [4] and js.preview.plan is not None and not js.pending


def test_threaded_worker_and_settle():
    js = JobService(debounce_s=0.05)
    try:
        doc = demo_doc()
        js.schedule_preview(lambda: req_for(doc))
        plan = js.settle(timeout=60)
        assert plan is not None and js.current is plan
    finally:
        js.shutdown()


def test_config_option_changes_the_compiled_job(tmp_path):
    """A hardware option set in the GUI (ConfigService) reaches the compiled job."""
    cfg = ConfigService(tmp_path / "c.json")
    doc = demo_doc()
    js = svc_inline()
    before = js.plan_sync(req_for(doc, machine=cfg.machine()))
    cfg.set("GC.GasDelay", 750)  # Options > GasParam > Gas On Delay
    after = js.plan_sync(req_for(doc, machine=cfg.machine()))
    assert after.key != before.key and after.job.wire != before.job.wire
    expect = compile_job(copy.deepcopy(doc), CamParams(), cfg.machine(), start=(0.0, 0.0))
    assert after.job.wire == expect.wire


def test_build_request_snapshot_place_and_laser(tmp_path):
    from types import SimpleNamespace

    docs = DocumentService(demo_doc())
    calls = []

    def place(d):
        calls.append(d)
        out = copy.copy(d)
        out.extra = dict(d.extra, job_offset=(100.0, 0.0))
        return out

    ctl = SimpleNamespace(
        state=lambda: SimpleNamespace(x=3.0, y=4.0), place=place, laser_enabled=False, layers=None
    )
    r = build_request("process", docs, ctl, Machine(), CamParams())
    assert r.start == (3.0, 4.0) and r.dry_run is True and r.document.extra["job_offset"] == (100.0, 0.0)
    assert r.document.parts[0] is not docs.document.parts[0]  # a private snapshot
    plan = svc_inline().plan_sync(r)
    assert plan.cut_bounds[0] >= 99.0 and plan.bounds[0] == 3.0  # placement in the plan; travel from the head
    docs.document.parts[0].selected = True
    rs = build_request("process", docs, ctl, Machine(), CamParams(), selected_only=True)
    assert len(rs.document.parts) == 1


def test_streamed_frames_are_job_frames():
    """JobStreamer writes exactly PlannedJob.frames() (= Job.frames(), the ManuItem-grouped frames
    parity verified) to the card, not a per-item regrouping."""
    from mlaser.controller.client import MotionController, TransportConfig
    from mlaser.controller.simulator import CardSimulator
    from mlaser.controller.stream import JobStreamer

    doc = demo_doc()
    plan = svc_inline().plan_sync(req_for(doc))
    groups = plan.job.groups()
    assert any(len(g) > 1 for g in groups)  # there are ManuItem groups
    from mlaser.protocol import frames as F

    flat = F.build_fifo_frames(plan.wire, 1)
    # time_scale 2: the card runs the 1.2 s motion cap in 0.6 s of real time, ample for a slow
    # host to refill (at 5x, 0.24 s, the FIFO ran dry on a Windows runner: unexpected_idle)
    sim = CardSimulator(time_scale=2.0).start()
    try:
        with MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port)) as mc:
            seq0 = mc.read_status().fifo_seq + 1
            sent = []
            orig = mc.fifo_write_frame
            mc.fifo_write_frame = lambda fr: (sent.append(list(fr)), orig(fr))[1]
            # no motion cap: the whole 64 KB FIFO (~5 s of card time, 2.7 s of real time at 2x) as
            # margin; this test is about the frames, and a stall of a shared macOS runner longer
            # than the capped 0.6 s ran the simulated card dry (a real underrun, as on a card)
            JobStreamer(mc, max_buffer_ms=None).run_job(plan, timeout_s=120)
            assert sent == plan.frames(seq0)
            assert sent == plan.job.frames(seq0)
            if flat != plan.frames(1):  # what the streamer used to send
                assert sent != F.build_fifo_frames(plan.wire, seq0)
    finally:
        sim.stop()


# ----------------------------------------------------------------------------- the application
@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.mark.ui
def test_window_preview_is_the_plan_and_start_streams_it(app):
    from wiring import real_window

    w = real_window()
    try:
        ctl = w.ctl
        ctl.connect()
        assert ctl.arm()                                          # every connection starts disarmed
        ctl._s.x, ctl._s.y = 420.0, 80.0  # the head is not at the origin
        w.set_document(demo_doc())
        plan = w.jobs.settle(timeout=60)
        app.processEvents()
        assert plan is w.plan and plan.start == (420.0, 80.0)
        # the canvas draws the planned items themselves
        assert len(w.canvas.toolpath) == len(plan.toolpath)
        assert all(a is b for a, b in zip(w.canvas.toolpath, plan.toolpath))
        assert w.canvas.contour_order() == markers(plan.toolpath)
        got = []
        ctl.start_job = lambda job, generation=None: got.append(job)
        compiles = w.jobs.stats["compiles"]
        w.control.buttons["mp54"].click()  # the run controls' Start
        w.jobs.settle(w.jobs.last_run, timeout=60)
        assert got and got[0] is plan  # exactly the previewed plan
        assert w.jobs.stats["compiles"] == compiles  # no recompile
        w.control.buttons["mp58"].click()  # Frame: planned in the worker too
        w.jobs.settle(w.jobs.last_run, timeout=60)
        assert len(got) == 2 and got[1].mode == "frame" and got[1].dry_run
    finally:
        w.close()


@pytest.mark.ui
def test_window_edit_replans_off_the_gui_thread(app):
    from wiring import real_window

    w = real_window()
    try:
        w.set_document(demo_doc())
        first = w.jobs.settle(timeout=60)
        w.canvas.select_parts([0])
        t0 = time.perf_counter()
        w.run("xform.rot_cw90")
        gui_ms = (time.perf_counter() - t0) * 1e3
        assert w.jobs.pending or (w.jobs.preview is not None and w.jobs.preview.plan is not first)
        second = w.jobs.settle(timeout=60)
        assert second is not first and second.key != first.key
        assert gui_ms < 2000
        w.undo()
        third = w.jobs.settle(timeout=60)
        assert third is first  # the cache answers the undo
    finally:
        w.close()


@pytest.mark.ui
@pytest.mark.sim
def test_window_start_on_the_card_controller_streams_the_plan(app):
    from mlaser.controller.ui_adapter import UiController
    from wiring import real_window

    w = real_window(UiController())
    w.control.confirm_fn = lambda key: False  # no start-up homing prompt
    try:
        # 4x: the streamed dry run keeps 0.3 s of real time in the card's FIFO (at 20x, 60 ms)
        assert w.connect_simulator(time_scale=4.0)
        w.set_document(demo_doc())
        for _ in range(20):
            app.processEvents()
        st = w.ctl.state()
        plan = w.jobs.settle(timeout=120)
        assert plan.start == (st.x, st.y)
        ran = []
        orig = w.ctl._run_job
        w.ctl._run_job = lambda job, mode, what, **kw: (ran.append((job, mode)), orig(job, mode, what, **kw))[
            1
        ]
        w.run_job("dry_run")
        w.jobs.settle(w.jobs.last_run, timeout=120)
        assert ran and ran[0][1] == "dry_run" and ran[0][0].dry_run
        w.ctl.stop()
    finally:
        w.close()


@pytest.mark.ui
def test_gui_option_reaches_the_compiled_job(app):
    """Options dialog -> ConfigService -> machine profile -> re-plan: the wire changes."""
    from mlaser.ui import dialogs
    from wiring import real_window

    w = real_window()
    try:
        w.set_document(demo_doc())
        before = w.jobs.settle(timeout=60)
        d = dialogs.ParamGridDialog(w.tr_, "mf137", ["ManuParam"], w.settings)
        ed = d._editors["GC.GasDelay"]
        assert ed.maximum() == 10000 and "ms" in ed.suffix()       # typed, ranged, with unit
        ed.setValue(750)
        d._ok()
        assert d.errors == [] and w.config.get("GC.GasDelay") == 750
        assert w.machine.process.gas_delay_ms == 750
        after = w.jobs.settle(timeout=60)
        assert after is not before and after.job.wire != before.job.wire
        expect = compile_job(copy.deepcopy(w.document), w.cam_params, w.config.machine(), start=after.start)
        assert after.job.wire == expect.wire
    finally:
        w.close()


def test_planned_job_uses_the_packed_wire():
    from mlaser.nc.packed import PackedWire
    plan = svc_inline().plan_sync(req_for(demo_doc()))
    assert isinstance(plan.job.wire, PackedWire)
    assert list(plan.iter_frames(5)) == plan.frames(5) == plan.job.frames(5)


def test_run_result_is_not_lost_when_the_plan_finishes_before_submit_returns():
    """A plan that completes on the worker thread before ``JobService.plan`` has returned (an
    inline post delivers there) still reaches ``submitter.start_job`` and ``on_result``: the
    one-shot delivery used to be consumed before ``run`` had registered its listener."""
    import threading
    from concurrent.futures import Future

    class FinishFirst:
        def submit(self, fn, *a, **kw):
            fut: Future = Future()

            def go():
                try:
                    fut.set_result(fn(*a, **kw))
                except BaseException as e:  # noqa: BLE001
                    fut.set_exception(e)
            th = threading.Thread(target=go)
            th.start()
            th.join()                       # done (and delivered) before submit returns
            return fut

        def shutdown(self, *a, **kw):
            pass

    class Sub:
        def __init__(self):
            self.plans = []

        def start_job(self, plan):
            self.plans.append(plan)
            return True

    for _ in range(3):
        svc = JobService(executor=FinishFirst(), debounce_s=0)
        sub, res = Sub(), []
        t = svc.run(req_for(demo_doc()), sub, lambda ok, m: res.append(ok))
        assert t is not None and res == [True] and len(sub.plans) == 1
        assert svc.on_planned == [] and svc.on_failed == []     # listeners removed
