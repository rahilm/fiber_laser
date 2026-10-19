"""Job queue: ordering, persistence through restarts, the runner's sheet-by-sheet flow with the
operator confirmation, and event-based job detection (mlaser.services.queue)."""
from __future__ import annotations

import json

import pytest

from mlaser.machine import events as EV
from mlaser.services.queue import JobQueue, QueueEntry, QueueRunner


def test_order_moves_and_next(tmp_path):
    q = JobQueue(tmp_path / "queue.json")
    a = q.add("a.chf", 2)
    b = q.add("b.chf", 1)
    c = q.add("c.chf", 3, position=0)
    assert [e.name for e in q] == ["c", "a", "b"]
    assert q.move(b.id, -5) == 0
    assert [e.name for e in q] == ["b", "c", "a"]
    q.move_to(b.id, 99)
    assert [e.name for e in q] == ["c", "a", "b"]
    assert q.next_entry() is c
    q.skip(c.id)
    assert q.next_entry() is a
    q.set_quantity(a.id, 5)
    assert q.get(a.id).quantity == 5
    assert q.totals()["sheets"] == 6          # skipped entry not counted
    q.remove(a.id)
    assert [e.name for e in q] == ["c", "b"]


def test_persistence_round_trip_and_interrupted(tmp_path):
    p = tmp_path / "queue.json"
    q = JobQueue(p)
    e = q.add("/jobs/flange.chf", 4, material="S235", thickness_mm=3.0, est_s=120.0)
    e.done, e.status = 1, "running"
    q.changed()
    data = json.loads(p.read_text())
    assert data["format"] == "mlaser-queue/1" and data["entries"][0]["quantity"] == 4
    q2 = JobQueue(p)                               # a restart
    e2 = q2.get(e.id)
    assert (e2.path, e2.quantity, e2.done, e2.material, e2.thickness_mm) == ("/jobs/flange.chf", 4, 1, "S235", 3.0)
    assert e2.status == "interrupted"              # it was running when the program ended
    assert q2.next_entry() is e2 and e2.remaining == 3


def test_corrupt_file_is_kept_aside(tmp_path):
    p = tmp_path / "queue.json"
    p.write_text("{not json")
    q = JobQueue(p)
    assert len(q) == 0 and q.load_error
    assert (tmp_path / "queue.json.bad").exists()
    q.add("x.chf")
    assert JobQueue(p).entries[0].path == "x.chf"


def test_from_json_sanitises():
    e = QueueEntry.from_json({"path": "a", "quantity": 0, "done": 9, "status": "bogus", "junk": 1})
    assert e.quantity == 1 and e.done == 1 and e.status == "waiting"


class _Machine:
    """Publishes job events like a controller."""

    def __init__(self):
        self.events = EV.EventBus()
        self.started = 0
        self.refuse = False

    def start(self):
        if self.refuse:
            return False
        self.started += 1
        self.events.publish(EV.JobStarted("process", "job"))
        return True

    def finish(self, ok=True, reason="done"):
        self.events.publish(EV.JobFinished("process", ok, reason, duration_s=10.0))


def _runner(tmp_path, m, clock=None):
    q = JobQueue(tmp_path / "q.json")
    loaded = []
    r = QueueRunner(q, load=lambda e: loaded.append(e.name) or True, start=m.start,
                    **({"clock": clock} if clock else {}))
    r.attach(m.events)
    return q, r, loaded


def test_runner_confirms_between_sheets(tmp_path):
    m = _Machine()
    q, r, loaded = _runner(tmp_path, m)
    a = q.add("a.chf", 2)
    b = q.add("b.chf", 1)
    assert r.begin() and r.state == "confirm" and r.current is a
    assert m.started == 0                          # nothing starts without the operator
    assert r.confirm_sheet() and r.state == "running" and a.status == "running"
    m.finish()
    assert a.done == 1 and a.status == "waiting" and r.state == "confirm" and r.current is a
    assert m.started == 1                          # the next sheet waits for the confirmation
    r.confirm_sheet()
    m.finish()
    assert a.status == "done" and r.current is b and r.state == "confirm"
    r.confirm_sheet()
    m.finish()
    assert b.status == "done" and r.state == "idle" and r.message == "queue.msg.finished"
    assert loaded == ["a", "a", "b"]
    assert JobQueue(tmp_path / "q.json").get(a.id).done == 2      # persisted after each sheet


def test_runner_stop_pause_and_fault(tmp_path):
    m = _Machine()
    q, r, _ = _runner(tmp_path, m)
    a = q.add("a.chf", 1)
    r.confirm_sheet()                              # idle -> begin -> confirm -> start
    assert r.state == "running"
    m.events.publish(EV.JobFinished("process", False, "paused"))      # pause keeps the job open
    m.events.publish(EV.JobFinished("process", False, "fault: link lost"))
    assert r.state == "running" and a.status == "running"
    m.events.publish(EV.JobStarted("process", "resume from wire 10", resume=True))
    m.finish(False, "stopped")
    assert a.status == "stopped" and a.done == 0 and r.state == "confirm"
    r.confirm_sheet()                              # retry
    m.finish()
    assert a.status == "done" and r.state == "idle"


def test_runner_refusals(tmp_path):
    m = _Machine()
    q, r, _ = _runner(tmp_path, m)
    a = q.add("a.chf", 1)
    m.refuse = True                                # pre-flight / arming refused
    assert not r.confirm_sheet()
    assert r.state == "confirm" and a.status == "waiting"
    r.start_allowed = lambda: (False, "pd859: planned quantity reached")
    m.refuse = False
    assert not r.confirm_sheet() and "pd859" in a.note
    r.start_allowed = lambda: (True, "")
    r.load = lambda e: False                       # document missing
    assert not r.confirm_sheet() and a.status == "stopped"


def test_runner_ignores_foreign_and_untracked_jobs(tmp_path):
    m = _Machine()
    q, r, _ = _runner(tmp_path, m)
    a = q.add("a.chf", 1)
    m.events.publish(EV.JobStarted("process", "manual job"))          # not started by the queue
    m.finish()
    assert a.done == 0 and r.state == "idle"
    r.begin()
    m.events.publish(EV.JobStarted("frame", "frame", tracked=False))
    assert r.state == "confirm"


def test_start_timeout(tmp_path):
    t = [0.0]
    m = _Machine()
    m.start = lambda: True                         # accepted but no job ever starts
    q, r, _ = _runner(tmp_path, m, clock=lambda: t[0])
    q.add("a.chf")
    r.confirm_sheet()
    assert r.state == "starting"
    t[0] = 100.0
    r.tick()
    assert r.state == "confirm"


def test_running_entry_cannot_be_skipped(tmp_path):
    q = JobQueue(None)
    e = q.add("a.chf")
    e.status = "running"
    with pytest.raises(ValueError):
        q.skip(e.id)
