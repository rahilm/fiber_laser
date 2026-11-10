"""The last job's parts (UX deep review, finding 5): which parts a job finished, where it
stopped, and what "Cut remaining parts" / "Start from part..." offer (mlaser.services.job_parts)."""
from __future__ import annotations

from types import SimpleNamespace

from mlaser.machine import events as EV
from mlaser.services.job_parts import JobPartsTracker, cut_order, map_to_document


def _part(uid):
    return SimpleNamespace(uid=uid)


def _plan(parts_uids, markers):
    return SimpleNamespace(document=SimpleNamespace(parts=[_part(u) for u in parts_uids]),
                           job=SimpleNamespace(markers=markers))


# a 4-part document; the plan cuts them in the order 2, 0, 3, 1 (two contours for part 0)
DOC = SimpleNamespace(parts=[_part(10), _part(11), _part(12), _part(13)])
MARKERS = [(0, 2, 0), (10, 0, 0), (20, 0, 1), (30, 3, 0), (40, 1, 0)]
PLAN = _plan([10, 11, 12, 13], MARKERS)


def tracker(plan=PLAN, doc=DOC, t=None):
    now = t if t is not None else [1000.0]
    return JobPartsTracker(lambda: (plan, doc, "sheet.dxf"), clock=lambda: now[0], key=lambda d: "K")


def test_cut_order_and_mapping():
    order, marker_part = cut_order(PLAN)
    assert order == [2, 0, 3, 1] and marker_part == [0, 1, 1, 2, 3]
    assert map_to_document(PLAN, order, DOC) == [2, 0, 3, 1]
    # a selected-parts job: its placed copy holds only parts 0 and 3 (uids 10, 13)
    sub = _plan([13, 10], [(0, 0, 0), (5, 1, 0)])
    assert map_to_document(sub, cut_order(sub)[0], DOC) == [3, 0]
    # a plan part that is not in the document (a separation line, say)
    odd = _plan([10, 99], [(0, 0, 0), (5, 1, 0)])
    assert map_to_document(odd, [0, 1], DOC) == [0, None]


def test_stopped_mid_part_counts_that_part_as_not_cut():
    t = tracker()
    t.started("process")
    last = t.last
    assert last.outcome == "running" and last.total == 4 and last.done == 0 and last.doc_key == "K"
    t.progress("process", 0)                 # part 2, its only contour
    t.progress("process", 1)                 # part 0, first contour: part 2 is finished
    assert last.done == 1
    t.progress("process", 2)                 # part 0, second contour: still in part 0
    assert last.done == 1
    t.finished("process", False, "stopped")
    assert last.outcome == "stopped" and last.stopped_at == 2
    assert last.done_parts() == [2]
    assert last.remaining() == [0, 3, 1]    # part 0 was interrupted: not finished
    assert last.from_part(3) == [3, 1] and last.number_of(3) == 3
    # progress never goes backwards, and events after the end change nothing
    t.progress("process", 4)
    assert last.done == 1


def test_finished_job_finished_every_part_and_hold_resume_is_one_job():
    t = tracker()
    t.started("process")
    t.progress("process", 3)
    t.finished("process", False, "paused")   # Hold: a breakpoint, the job goes on
    assert t.last.outcome == "held" and t.last.done == 2
    job = t.last
    t.started("process", resume=True)        # Continue
    assert t.last is job and job.outcome == "running" and job.done == 2
    t.progress("process", 4)
    t.finished("process", True, "done")
    assert job.outcome == "finished" and job.done == 4 and job.remaining() == []
    assert job.stopped_at == 0


def test_fault_then_stop_and_untracked_jobs():
    now = [1000.0]
    t = tracker(t=now)
    t.started("process")
    t.progress("process", 1)
    t.finished("process", False, "fault: link lost")
    assert t.last.outcome == "held"          # a breakpoint is kept: Resume can continue it
    t.settle(held_now=True)
    assert t.last.outcome == "held"
    t.settle(held_now=False)                 # no breakpoint (yet: a Hold sets it after the end)
    now[0] += 1.0
    t.settle(held_now=False)
    assert t.last.outcome == "held"
    now[0] += 1.5
    t.settle(held_now=False)                 # still none: the job is over
    assert t.last.outcome == "stopped" and t.last.remaining() == [0, 3, 1]
    job = t.last
    t.started("frame", tracked=False)         # Frame: not a job of parts
    t.finished("frame", True, "done", tracked=False)
    t.started("process", tracked=False)       # a laser test
    assert t.last is job


def test_a_new_start_replaces_the_last_job():
    plans = [PLAN, _plan([13, 11], [(0, 0, 0), (5, 1, 0)])]
    t = JobPartsTracker(lambda: (plans[0], DOC, "a"), key=lambda d: "K")
    t.started("process")
    t.finished("process", False, "stopped")
    plans.pop(0)
    t.started("process")                     # "Cut remaining parts": a new job of two parts
    assert t.last.order == (3, 1) and t.last.outcome == "running"


def test_follows_the_controller_event_bus():
    bus = EV.EventBus()
    t = tracker()
    seen = []
    t.listeners.append(lambda: seen.append(t.last.outcome))
    t.attach(bus)
    bus.publish(EV.JobStarted("process", "x"))
    bus.publish(EV.JobProgress("process", 0.5, 3))
    bus.publish(EV.JobFinished("process", False, "stopped"))
    assert t.last.outcome == "stopped" and t.last.done == 2 and seen[-1] == "stopped"
    t.detach()
    bus.publish(EV.JobStarted("process", "y"))
    assert t.last.outcome == "stopped"
