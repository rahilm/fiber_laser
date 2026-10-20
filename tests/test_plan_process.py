"""The plan compile in a separate process (services/planproc.py, review 2 P1-1).

The process path must give exactly the plan the in-thread compile gives (wire, frames, markers,
steps, carry, laser length, estimate, toolpath, bounds, key), keep the cache and the start gate
semantics, cancel, survive a dead child, and keep the GUI event loop responsive while a heavy
compile runs.
"""
from __future__ import annotations

import copy
import pathlib
import statistics
import threading
import time

import pytest

from mlaser import ops
from mlaser.cam.toolpath import CamParams
from mlaser.model import LayerParams
from mlaser.nc.machine import Machine
from mlaser.services.document import DocumentService
from mlaser.services.jobs import InlineExecutor, JobRequest, JobService, inputs_hash

DATA = pathlib.Path(__file__).parent / "data"


def demo_doc(n_copies: int = 1):
    d = ops.demo_document()
    if n_copies > 1:
        base = list(d.parts)
        for k in range(1, n_copies):
            for p in copy.deepcopy(base):
                _shift(p, 450.0 * k)
                d.parts.append(p)
        from mlaser.model import ensure_unique_ids
        ensure_unique_ids(d)
    for p in d.parts:
        d.layers.setdefault(p.craft.layer, LayerParams(index=p.craft.layer, speed=120.0))
    return d


def grid_doc(n: int):
    """``n`` parts (review 2's benchmark document): a 24 mm square with a hole, a 24-point star
    with two holes; line lead-ins, four layers."""
    import math

    from mlaser.model import Arc, Contour, Craft, Document, LeadParams, Line, Part

    def rect(x, y, w, h):
        p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)

    def circ(cx, cy, r):
        s = (cx + r, cy)
        return Contour([Arc(s, s, (cx, cy), True)], True)

    def star(cx, cy, r0, r1, k=12):
        pts = [(cx + (r0 if i % 2 == 0 else r1) * math.cos(math.pi * i / k),
                cy + (r0 if i % 2 == 0 else r1) * math.sin(math.pi * i / k)) for i in range(2 * k)]
        return Contour([Line(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))], True)
    cols = int(math.ceil(math.sqrt(n * 2)))
    parts = []
    for k in range(n):
        x, y = (k % cols) * 30.0, (k // cols) * 30.0
        cs = ([circ(x + 12, y + 12, 5), rect(x, y, 24, 24)] if k % 2 == 0 else
              [circ(x + 8, y + 12, 2), circ(x + 16, y + 12, 2), star(x + 12, y + 12, 12, 7)])
        parts.append(Part(cs, Craft(layer=1 + k % 4, lead_in=LeadParams(kind=1, length=2.0))))
    d = Document(parts=parts)
    for li in range(1, 5):
        d.layers[li] = LayerParams(index=li, speed=120.0)
    return d


def _shift(p, dy):
    from mlaser.cad import arrange as AR
    AR.translate([p], 0.0, dy)


def req_for(doc, mode="process", start=(5.0, 7.0), machine=None, cam=None, profile="original"):
    return JobRequest(mode=mode, document=copy.deepcopy(doc), machine=machine or Machine(), start=start,
                      cam_params=cam or CamParams(), dry_run=mode != "process", profile=profile)


@pytest.fixture(scope="module")
def proc_svc():
    s = JobService(executor=InlineExecutor(), debounce_s=0, process=True)
    assert s.process_mode
    yield s
    s.shutdown()


def thread_svc():
    return JobService(executor=InlineExecutor(), debounce_s=0, process=False)


def same_plan(a, b):
    assert a.key == b.key
    assert a.mode == b.mode and a.start == b.start and a.profile == b.profile
    assert list(a.toolpath) == list(b.toolpath)
    assert a.job.wire == b.job.wire
    assert a.frames() == b.frames() and a.frames(33) == b.frames(33)
    assert list(a.iter_frames(5)) == list(b.iter_frames(5))
    ja, jb = a.job, b.job
    assert ja.markers == jb.markers and ja.cut_blocks == jb.cut_blocks
    assert ja.steps == jb.steps and ja.duration_s == jb.duration_s and ja.dry_run == jb.dry_run
    assert [x.hex() for x in ja.pulse_carry] == [x.hex() for x in jb.pulse_carry]
    assert ja.laser_on_length_mm.hex() == jb.laser_on_length_mm.hex()
    assert ja.start_pulses == jb.start_pulses and ja.end_pulses == jb.end_pulses
    assert ja.pierces == jb.pierces and ja.process_items == jb.process_items
    assert repr(ja.estimate) == repr(jb.estimate) and a.estimate == b.estimate
    assert [(i, repr(m)) for i, m in ja.manu] == [(i, repr(m)) for i, m in jb.manu]
    assert ja.layers == jb.layers
    assert a.bounds == b.bounds and a.cut_bounds == b.cut_bounds
    assert a.summary() == b.summary()
    assert a.document.layers == b.document.layers and a.document.parts == b.document.parts
    assert a.document.extra == b.document.extra


def _chf_doc():
    from mlaser.formats.chf import load_chf
    return load_chf(DATA / "chf" / "sample_v9.chf")


@pytest.mark.parametrize("case", ["demo", "chf", "placed", "frame", "dry_run", "nosmooth"])
def test_process_plan_equals_thread_plan(proc_svc, case):
    doc = _chf_doc() if case == "chf" else demo_doc()
    mode = {"frame": "frame", "dry_run": "dry_run"}.get(case, "process")
    if case == "placed":
        doc.extra["job_offset"] = (12.5, -3.25)          # the placement changes every part
    machine = Machine(smooth=False) if case == "nosmooth" else Machine()
    req = req_for(doc, mode, machine=machine)
    a = proc_svc.plan_sync(req)
    b = thread_svc().plan_sync(req)          # the same request: round-trip records by identity
    same_plan(a, b)
    assert proc_svc.stats["process_compiles"] >= 1


def test_the_plan_process_runs_the_per_chain_pool(monkeypatch):
    """The plan process may own the per-chain compile pool (nc/parallel.py), so moving the
    compile out of the GUI process does not make it serial; the result is the serial one."""
    monkeypatch.setenv("MLASER_COMPILE_WORKERS", "2")
    svc = JobService(executor=InlineExecutor(), debounce_s=0, process=True)
    try:
        req = req_for(demo_doc(2))
        plan = svc.plan_sync(req)
        assert svc.stats["child_parallel"].get("parallel_jobs", 0) >= 1, svc.stats
        monkeypatch.setenv("MLASER_COMPILE_WORKERS", "0")
        same_plan(plan, thread_svc().plan_sync(req))
    finally:
        svc.shutdown()


def test_process_cache_and_per_part_encoding():
    """A replan after a one-part edit sends one part; the unchanged plan is a cache hit; equal
    inputs give the same key in both paths and through the codec."""
    docs = DocumentService(demo_doc())
    svc = JobService(executor=InlineExecutor(), debounce_s=0, process=True)
    try:
        def req():
            return JobRequest("process", docs.plan_snapshot(), Machine(smooth=False), (0.0, 0.0), CamParams())
        r1 = req()
        p1 = svc.plan_sync(r1)
        n_parts = len(r1.document.parts)
        assert svc.codec.encoded == n_parts
        assert inputs_hash(r1) == inputs_hash(r1, svc.codec) == p1.key
        p1b = svc.plan_sync(req())
        assert p1b is p1 and svc.stats["cache_hits"] == 1
        target = docs.document.parts[1]
        from mlaser.cad import arrange as AR
        docs.edit(lambda d: AR.translate([target], 1.0, 0.0), parts=[target])
        r2 = req()
        assert sum(a is not b for a, b in zip(r2.document.parts, r1.document.parts)) == 1
        p2 = svc.plan_sync(r2)
        assert p2.key != p1.key
        assert svc.codec.encoded == n_parts + 1             # only the edited part was pickled
        same_plan(p2, thread_svc().plan_sync(JobRequest("process", docs.snapshot(), Machine(smooth=False),
                                                        (0.0, 0.0), CamParams())))
        docs.undo()
        p3 = svc.plan_sync(req())
        assert p3 is p1                                     # undo: the same inputs, a cache hit
    finally:
        svc.shutdown()


def test_cancel_reaches_the_child_and_the_next_plan_runs():
    svc = JobService(debounce_s=0, process=True)
    try:
        big = demo_doc(6)
        t = svc.plan(req_for(big))
        assert t.wait_toolpath(120) is not None             # the child is compiling now
        t.cancel()
        assert t.done_event.wait(60)
        assert t.plan is None and t.error is None and svc.stats["cancelled"] == 1
        small = demo_doc()
        p = svc.plan(req_for(small, machine=Machine(smooth=False))).wait(120)
        assert p is not None
        same_plan(p, thread_svc().plan_sync(req_for(small, machine=Machine(smooth=False))))
    finally:
        svc.shutdown()


def test_dead_child_falls_back_to_the_thread():
    """The plan process dies in the middle of a compile: the plan is compiled in the thread
    (the same plan), and the next one goes to a new plan process."""
    svc = JobService(debounce_s=0, process=True)
    try:
        doc = demo_doc(4)
        req = req_for(doc)
        t = svc.plan(req)
        assert t.wait_toolpath(120) is not None and not t.done_event.is_set()
        svc.planproc.proc.kill()
        plan = t.wait(300)
        assert plan is not None, t.error
        same_plan(plan, thread_svc().plan_sync(req))
        assert svc.stats["thread_compiles"] == 1 and svc.stats["process_compiles"] == 0
        small = demo_doc()
        p2 = svc.plan(req_for(small, machine=Machine(smooth=False))).wait(120)
        assert p2 is not None and svc.stats["process_compiles"] == 1        # a new child
    finally:
        svc.shutdown()


def test_profile_unknown_to_the_child_compiles_in_the_thread():
    from mlaser.parity_ports.profile import ORIGINAL, PROFILES, register_profile
    import dataclasses
    prof = dataclasses.replace(ORIGINAL, name="runtime-test-profile", warning="")
    register_profile(prof)
    svc = JobService(executor=InlineExecutor(), debounce_s=0, process=True)
    try:
        doc = demo_doc()
        plan = svc.plan_sync(req_for(doc, machine=Machine(smooth=False), profile="runtime-test-profile"))
        assert plan.profile == "runtime-test-profile" and svc.stats["thread_compiles"] == 1
    finally:
        svc.shutdown()
        PROFILES.pop("runtime-test-profile", None)


def test_plan_finishing_after_a_stop_never_starts(monkeypatch):
    """The start gate's generation holds with the process path: Stop while the child compiles,
    the plan arrives afterwards and is refused."""
    from mlaser.ui.facade import DemoController
    from mlaser.services.jobs import ControllerSubmitter, build_request
    monkeypatch.setenv("MLASER_COMPILE_WORKERS", "0")      # a serial compile: seconds long
    ctl = DemoController()
    docs = DocumentService(grid_doc(400))
    svc = JobService(debounce_s=0, process=True)
    results: list = []
    try:
        req = build_request("dry_run", docs, ctl, Machine(), CamParams())
        t = svc.run(req, ControllerSubmitter(ctl), lambda ok, m: results.append((ok, m)))
        assert t.wait_toolpath(120) is not None and not t.done_event.is_set()
        ctl.stop()
        assert t.wait(120) is not None
        svc.settle(t)          # the worker may be delivering it right now (inline post): wait
        t_end = time.monotonic() + 20
        while not results and time.monotonic() < t_end:
            time.sleep(0.01)
        assert results and results[0][0] is False, results
        assert not ctl.job_active()
        assert svc.stats["process_compiles"] == 1
    finally:
        svc.shutdown()


# ------------------------------------------------------------------ GUI responsiveness (timing)
def _latency_while(app, busy: callable, period_s: float = 0.02, max_s: float = 300.0) -> list:
    """Post a call to the GUI thread every ``period_s`` from another thread (the path a Stop
    takes, QtDispatcher) and record post -> run, while ``busy()``; the GUI thread runs its event
    loop meanwhile."""
    from PySide6.QtCore import QEventLoop, QTimer

    from mlaser.ui.qtbridge import QtDispatcher
    disp = QtDispatcher()
    lat: list = []
    stop = threading.Event()

    def poster():
        disp(lambda: None)                   # warm-up: the dispatcher's first cross-thread post
        time.sleep(period_s)
        while not stop.is_set():
            t0 = time.perf_counter()
            disp(lambda t0=t0: lat.append((t0, (time.perf_counter() - t0) * 1e3)))
            time.sleep(period_s)
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)
    t_end = time.monotonic() + max_s
    timer.timeout.connect(lambda: loop.quit() if not busy() or time.monotonic() > t_end else None)
    app.processEvents()                  # what was queued before (layout, paint) is not measured
    th = threading.Thread(target=poster, daemon=True)
    QTimer.singleShot(0, th.start)       # posting starts once the event loop runs
    timer.start()
    loop.exec()
    timer.stop()
    stop.set()
    if th.ident is not None:
        th.join(2)
    app.processEvents()
    return lat


def _p99(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * 0.99))]


@pytest.mark.flaky(reruns=1)
def test_gui_event_loop_stays_responsive_during_a_heavy_compile(monkeypatch):
    """Event-loop latency (post -> run on the GUI thread) while a multi-second compile runs.
    Before (compile in the GUI process's worker thread): the loop waited up to 19 s and
    ``canvas.set_toolpath`` took 53x longer (review 2 §1.3).  Asserted: p99 below the 50 ms
    Stop budget, and the toolpath reached the canvas long before the compile finished."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("MLASER_PLAN_PROCESS", "1")
    monkeypatch.setenv("MLASER_COMPILE_WORKERS", "0")      # a serial compile: seconds long
    from wiring import real_window
    w = real_window()
    try:
        assert w.jobs.process_mode
        w.jobs.planproc.ensure_ready()
        w.set_document(grid_doc(500))
        app.processEvents()
        t = w.jobs.flush()
        assert t is not None
        draw: list = []                  # the GUI thread drawing the new toolpath (GUI work)
        w.jobs.on_toolpath.insert(0, lambda tk: draw.append(time.perf_counter()) if tk is t else None)
        w.jobs.on_toolpath.append(lambda tk: draw.append(time.perf_counter()) if tk is t else None)
        t0 = time.perf_counter()
        samples = _latency_while(app, lambda: not t.done_event.is_set())
        t_done = time.perf_counter() - t0
        assert t.plan is not None, t.error
        app.processEvents()
        assert len(draw) == 2 and draw[0] - t0 < t_done * 0.8, "the toolpath came only with the plan"
        # samples posted while the GUI thread drew the toolpath wait for that drawing; the rest
        # measure what the compile costs the GUI process: the wait for the interpreter
        lat = [ms for tp, ms in samples if not (draw[0] - 0.05 <= tp <= draw[1])]
        info = (f"{len(lat)} of {len(samples)} samples kept, compile {t_done:.2f} s, toolpath drawn "
                f"in {(draw[1] - draw[0]) * 1e3:.0f} ms, max {max(ms for _, ms in samples):.1f} ms")
        assert len(lat) > 50 and t_done > 1.0, info
        print(f"compile {t_done:.1f} s; event loop latency median {statistics.median(lat):.2f} ms, "
              f"p99 {_p99(lat):.2f} ms, max {max(lat):.1f} ms, n={len(lat)}; toolpath drawn in "
              f"{(draw[1] - draw[0]) * 1e3:.0f} ms")
        p99 = _p99(lat)
        assert p99 < 50.0, f"event loop p99 {p99:.1f} ms (median {statistics.median(lat):.1f}); {info}"
        assert draw[1] - draw[0] < 2.0
        assert w.jobs.stats["process_compiles"] >= 1
    finally:
        w.jobs.shutdown()
        w.close()
