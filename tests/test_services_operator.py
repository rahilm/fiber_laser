"""Operator status (mlaser.services.operator) and event-based job detection
(ProductionService / JobWatcher on the controller's EventBus)."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from mlaser.machine import events as EV
from mlaser.machine.api import MachineState
from mlaser.model import Document, LayerParams, Part, Craft
from mlaser.services.operator import estimate_remaining, fmt_duration, marker_position, run_status


def _plan(markers, layers=None, parts=3):
    doc = Document(parts=[Part([], Craft(layer=1)) for _ in range(parts)],
                   layers=layers or {1: LayerParams(1, power_pct=85, gas=2, gas_pressure=12.0, cut_height=0.8)})
    return NS(job=NS(markers=markers), document=doc, estimate={"total": 100.0})


def test_marker_position():
    plan = _plan([(0, 0, 0), (10, 0, 1), (20, 2, 0), (30, 1, 0)])
    assert marker_position(plan, -1) == (0, 4, 0, 3, -1, ())
    assert marker_position(plan, 0) == (1, 4, 1, 3, 0, ())
    assert marker_position(plan, 2) == (3, 4, 2, 3, 2, (0,))
    assert marker_position(plan, 3) == (4, 4, 3, 3, 1, (0, 2))
    assert marker_position(NS(job=None), 3) == (0, 0, 0, 0, -1, ())


def test_states_and_reasons():
    ms = MachineState(connected=True, mode="ready")
    st = NS(banner="DISARMED", armed=False, dry_run=False, simulator=True, fault="")
    assert run_status(ms, st, []).kind == "ready"
    assert run_status(ms, st, []).reason == "run.reason.disarmed"
    assert run_status(MachineState(), st, []).kind == "offline"
    ms.mode, ms.progress, ms.job_index = "process", 0.5, 2
    r = run_status(ms, st, [], _plan([(0, 0, 0), (10, 0, 1), (20, 2, 0), (30, 1, 0)]), elapsed_s=40.0)
    assert (r.kind, r.contour, r.part, r.parts, r.done_parts) == ("running", 3, 2, 3, (0,))
    assert (r.power_pct, r.gas, r.gas_bar, r.height_mm) == (85, "N2", 12.0, 0.8)
    assert r.eta_s == pytest.approx(40.0)              # rate-based at 50 %
    ms.mode = "pause"
    assert run_status(ms, st, []).kind == "hold"
    bp = NS(x=10.0, y=20.0, mode="process")
    r = run_status(MachineState(connected=True, mode="pause"), st, [], breakpoint=bp)
    assert r.reason == "run.reason.breakpoint" and r.reason_args == {"x": 10.0, "y": 20.0}
    alarm = NS(code=8104, text_en="X servo input alarm")
    r = run_status(ms, st, [alarm])
    assert r.kind == "alarm" and r.reason_args["code"] == 8104
    st.banner, st.fault = "FAULT", "link lost"
    r = run_status(ms, st, [alarm])
    assert r.kind == "fault" and r.reason_args["fault"] == "link lost"


def test_eta_blend_and_format():
    assert estimate_remaining(0.0, 0.0, 100.0) == 100.0
    assert estimate_remaining(0.0, 0.0, 0.0) is None
    assert estimate_remaining(0.5, 50.0, 0.0) == pytest.approx(50.0)
    assert estimate_remaining(0.1, 20.0, 100.0) == pytest.approx(0.5 * 90 + 0.5 * 180)
    assert fmt_duration(None) == "-" and fmt_duration(65) == "1:05" and fmt_duration(3725) == "1:02:05"


class _Settings(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def test_production_service_follows_events(tmp_path):
    from mlaser.services.production import ProductionService
    bus = EV.EventBus()
    svc = ProductionService(_Settings(), str(tmp_path), bus=bus)
    assert svc.event_driven
    done = []
    svc.job_done_listeners.append(done.append)
    svc.feed_log("process: 3 parts")             # log lines no longer drive the detection
    svc.feed_log("done: process")
    assert done == []
    bus.publish(EV.JobStarted("process", "job"))
    bus.publish(EV.JobFinished("process", False, "paused"))        # the job continues
    bus.publish(EV.JobStarted("process", "resume", resume=True))
    bus.publish(EV.JobFinished("process", True, "done", duration_s=12.0))
    assert done == [True]
    assert svc.counters.jobs == 1 if hasattr(svc.counters, "jobs") else True
    bus.publish(EV.JobStarted("frame", "frame", tracked=False))
    bus.publish(EV.JobFinished("frame", True, "done", tracked=False))
    assert done == [True]


def test_demo_controller_publishes_job_events():
    """The fallback log parsing is only needed without events: DemoController has them."""
    from mlaser.diag.jobwatch import JobWatcher
    from mlaser.machine.demo import DemoController
    from mlaser.model import Contour, Line
    t = [0.0]
    ctl = DemoController(clock=lambda: t[0])
    ctl.connect()
    w = JobWatcher(lambda: t[0])
    w.subscribe(ctl.events)
    seen = []
    w.finish_listeners.append(seen.append)
    doc = Document(parts=[Part([Contour([Line((0, 0), (10, 0)), Line((10, 0), (10, 10)),
                                         Line((10, 10), (0, 0))], True)])])
    ctl.dry_run(doc)
    assert w.running
    t[0] = 1000.0
    ctl.state()
    assert len(seen) == 1 and seen[0].ok and seen[0].mode == "dry_run"
