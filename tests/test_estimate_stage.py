"""The time estimate comes with the toolpath stage of a preview (perf review 2026-11-09, finding 8,
left from wave 1): it no longer waits for the full compile, and its numbers are exactly the
compile's ``PlannedJob.estimate`` (in a worker thread and in the plan process).  A request that
waits for the worker (Start, ``settle``) makes the estimate give way; the compile then gives it."""
from __future__ import annotations

import threading

import pytest

from mlaser.cam.toolpath import CamParams
from mlaser.model import LayerParams
from mlaser.nc.machine import Machine
from mlaser.services import planproc
from mlaser.services.jobs import InlineExecutor, JobRequest, JobService

from test_large_drawing import big_document


def _doc(n: int = 40):
    d = big_document(n)
    for p in d.parts:
        d.layers.setdefault(p.craft.layer, LayerParams(index=p.craft.layer, speed=120.0))
    return d


def _req(doc, start=(1.0, 2.0)):
    return JobRequest(mode="process", document=doc, machine=Machine(), start=start, cam_params=CamParams())


@pytest.mark.parametrize("process", [False, True], ids=["thread", "plan-process"])
def test_the_estimate_comes_before_the_compile_with_the_compiles_numbers(process):
    doc = _doc()
    svc = JobService(debounce_s=0, compile_idle_s=600.0, process=process)   # no compile by itself
    got: list = []
    seen = threading.Event()
    svc.on_estimate.append(lambda t: (got.append(dict(t.estimate)), seen.set()))
    try:
        t = svc.request_preview(_req(doc))
        assert seen.wait(120), "no estimate from the toolpath stage"
        assert not t.done_event.is_set() and t.plan is None and svc.stats["compiles"] == 0
        assert got[0]["total"] > 0 and t.estimate == got[0]
        assert svc.settle(t, timeout=120) is not None           # the compile, now
        assert t.plan.estimate == got[0]                          # bit for bit the compile's
        ref = JobService(executor=InlineExecutor(), process=False).plan_sync(_req(doc))
        assert ref.estimate == got[0]
    finally:
        svc.shutdown()


def test_run_estimate_is_estimate_time_and_stops_when_asked():
    from mlaser.motion.interp import estimate_time
    doc = _doc(12)
    r = {"document": doc, "mode": "process", "machine": Machine(), "start": (0.0, 0.0),
         "cam_params": CamParams(), "layers": None, "dry_run": False, "profile": "original"}
    items, _placed, _start = planproc.run_toolpath(r)
    assert planproc.run_estimate(r, items, lambda: False) == estimate_time(items, Machine().motion_config())
    assert planproc.run_estimate(r, items, lambda: True) is None
    assert planproc.run_estimate(dict(r, mode="frame"), items, lambda: False) is None


def test_a_waiting_start_skips_the_estimate_and_the_plan_still_has_it(monkeypatch):
    """A plan request queued behind a toolpath stage (Start) makes its estimate give way: no
    estimate from the stage, and the plan carries the compile's estimate as before."""
    doc = _doc()
    entered, release = threading.Event(), threading.Event()
    real = planproc.run_estimate

    def slow(r, items, stop):
        entered.set()
        release.wait(60)
        return real(r, items, stop)
    monkeypatch.setattr(planproc, "run_estimate", slow)
    svc = JobService(debounce_s=0, compile_idle_s=600.0, process=False)
    try:
        t = svc.request_preview(_req(doc))
        assert entered.wait(60)
        start = svc.plan(_req(doc))                 # Start: the same inputs, waits for the worker
        assert svc._hurry()
        release.set()
        assert start.wait(120) is not None
        assert t.estimate is None and start.plan.estimate["total"] > 0
        assert not svc._hurry()
    finally:
        release.set()
        svc.shutdown()
