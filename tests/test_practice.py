"""Practice mode (mlaser.services.practice): every lesson against the card simulator, with a
driver that does what the operator does on the Run screen (arm, start, frame, hold, reset,
resume through the MachineAPI); the lesson only watches the events and plays its scenario.
Also: a real card is refused, abort halts, progress is kept, and jobs of a lesson are recorded
as ``practice`` in the Report."""
from __future__ import annotations

import time

import pytest

from mlaser.services import practice as PR
from mlaser.services.procedures import engine as PE
from mlaser.services.procedures import simrig


def wait_until(pred, timeout=20.0, step=0.01):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if pred():
            return True
        time.sleep(step)
    return False


class Host:
    def __init__(self, doc=None):
        self.doc = doc
        self.loads = 0

    def load_document(self, doc):
        self.loads += 1
        self.doc = doc
        return True

    def document(self):
        return self.doc


@pytest.fixture
def ctl():
    c = simrig.simulator_controller("card", time_scale=2.0)   # 4x underran the FIFO on a slow CI runner
    yield c
    try:
        if c.sim is not None:
            c.sim.set_estop(False)
    except Exception:  # noqa: BLE001
        pass
    simrig.close(c)


def drive(lesson, actions, timeout=60.0):
    """Run a lesson like the coach panel does; ``actions[step id](lesson)`` is the operator's
    part, done while the step's task waits."""
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        s = lesson.step
        if lesson.state in PE.FINAL or s is None:
            return lesson
        if s.kind == "action" and s.id not in lesson.done:
            if s.auto:
                t = lesson.execute_async()
                fn = actions.get(s.id)
                if fn is not None:
                    fn(lesson)
                t.join(timeout)
            elif not lesson.execute():
                return lesson
            if lesson.state != PE.WAITING:
                return lesson
            continue
        lesson.next()
    raise AssertionError(f"lesson stuck at {lesson.step.id if lesson.step else '-'}")


def streaming(ctl):
    return wait_until(lambda: ctl.sim.state.fifo_running and ctl.job_active(), 20)


def idle(ctl):
    return wait_until(lambda: not ctl.job_active() or ctl.breakpoint is not None, 30)


def start_cut(lesson):
    c = lesson.machine
    if not c.safety().armed:
        assert c.arm()
    c.start(lesson.host.document())
    assert streaming(c)


def reset(lesson):
    c = lesson.machine
    assert wait_until(lambda: not c.sim.state.estop, 20)
    assert wait_until(lambda: c.reset_fault(), 20, 0.1)


def service():
    store = {}
    return PR.PracticeService(load=lambda: dict(store), save=store.update), store


# ----------------------------------------------------------------------------- lessons
def test_load_sheet_lesson(ctl):
    svc, store = service()
    host = Host()
    lesson = svc.begin("load_sheet", ctl, host, task_timeout_s=30)
    assert svc.running
    drive(lesson, {})
    assert lesson.state == PE.DONE and host.loads == 1 and PR.is_practice_document(host.doc)
    assert svc.progress["load_sheet"].passed and store["load_sheet"]["attempts"] == 1
    assert not svc.running


def test_frame_lesson_skips_loading_an_open_sheet(ctl):
    svc, _ = service()
    host = Host(PR.practice_document())
    lesson = svc.begin("frame", ctl, host, task_timeout_s=30)
    drive(lesson, {"frame": lambda p: p.machine.frame(p.host.document())})
    assert lesson.state == PE.DONE and host.loads == 0
    assert [r.outcome for r in lesson.history if r.step == "load"] == ["skipped"]


def test_first_cut_lesson(ctl):
    svc, _ = service()
    host = Host()
    lesson = svc.begin("first_cut", ctl, host, task_timeout_s=40)
    drive(lesson, {"arm": lambda p: p.machine.arm(),
                   "start": lambda p: p.machine.start(p.host.document())})
    assert lesson.state == PE.DONE, lesson.history
    assert svc.progress["first_cut"].passed and svc.progress["first_cut"].best_s > 0


def test_hold_lesson(ctl):
    svc, _ = service()
    lesson = svc.begin("hold", ctl, Host(PR.practice_document()), task_timeout_s=40)

    def hold(p):
        assert streaming(p.machine)
        p.machine.pause()

    def go_on(p):
        assert wait_until(lambda: p.machine.breakpoint is not None, 20)
        if not p.machine.safety().armed:
            assert p.machine.arm()
        p.machine.resume("exact")
    drive(lesson, {"start": start_cut, "hold": hold, "continue": go_on}, timeout=90)
    assert lesson.state == PE.DONE, lesson.history


def test_alarm_lesson_injects_and_releases_the_estop(ctl):
    svc, _ = service()
    lesson = svc.begin("alarm", ctl, Host(PR.practice_document()), task_timeout_s=40)
    seen = {}

    def read(p):
        pass

    def at_reset(p):
        seen["fault"] = p.machine.safety().fault
        reset(p)
    drive(lesson, {"alarm_wait": start_cut, "alarm_read": read, "reset": at_reset}, timeout=90)
    assert lesson.state == PE.DONE, lesson.history
    assert seen["fault"] and not ctl.sim.state.estop and not ctl.safety().fault
    ctl.stop(wait=True)


def test_resume_lesson(ctl):
    svc, _ = service()
    lesson = svc.begin("resume", ctl, Host(PR.practice_document()), task_timeout_s=40)

    def resume(p):
        c = p.machine
        assert c.breakpoint is not None
        assert c.arm()
        c.resume("contour")
    drive(lesson, {"alarm_wait": start_cut, "reset": reset, "resume": resume}, timeout=120)
    assert lesson.state == PE.DONE, lesson.history
    assert svc.progress["resume"].passed


# ----------------------------------------------------------------------------- safety
class RealCard:
    """A controller that is not the simulator: the lesson must not call anything on it."""

    def __init__(self):
        self.calls = []

    def is_simulator(self):
        return False

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError(f"practice touched the real controller: {name}")


def test_a_real_card_is_refused():
    svc, store = service()
    card = RealCard()
    with pytest.raises(PR.PracticeError) as e:
        svc.begin("first_cut", card, Host())
    assert e.value.key == "practice.error.not_simulator"
    lesson = PR.FirstCutLesson(card, Host())
    with pytest.raises(PR.PracticeError):
        lesson.start()
    assert card.calls == [] and not svc.running and store == {}


def test_abort_halts_and_releases_the_estop(ctl):
    svc, _ = service()
    lesson = svc.begin("alarm", ctl, Host(PR.practice_document()), task_timeout_s=40)
    lesson.next()                                    # intro
    t = lesson.execute_async()                       # alarm_wait: waits for the cut
    start_cut(lesson)
    assert wait_until(lambda: lesson._estop or ctl.safety().fault, 20)
    halts = []
    real = ctl.safe_halt
    ctl.safe_halt = lambda reason="": (halts.append(reason), real(reason))
    try:
        svc.abort()
        t.join(10)
    finally:
        ctl.safe_halt = real
    assert lesson.state == PE.ABORTED and halts
    assert not ctl.sim.state.estop and not svc.running
    assert svc.progress["alarm"].last_result == "aborted" and not svc.progress["alarm"].passed


def test_progress_survives_a_new_service():
    store = {"frame": {"passed": True, "attempts": 3, "best_s": 12.5}}
    svc = PR.PracticeService(load=lambda: store, save=store.update)
    assert svc.progress["frame"].passed and svc.progress["frame"].attempts == 3
    assert svc.passed_count() == 1
    svc.reset_progress()
    assert not store["frame"]["passed"]


def test_every_text_id_has_english_text():
    ids = set()
    for cls in PR.LESSONS:
        ids.add(cls.title)
        for s in cls(object(), None).steps:
            ids |= {s.title, s.text}
    assert not ids - set(PR.TEXTS)


# ----------------------------------------------------------------------------- application
@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def pump(app, seconds=0.2):
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        app.processEvents()
        time.sleep(0.01)


def test_practice_jobs_are_recorded_as_practice(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from mlaser.machine import events as EV
    from wiring import real_window
    w = real_window()
    try:
        ext = w.extension("ext_practice")
        rep = w.extension("report")
        w.run("practice.open")
        assert ext.panel is not None and not ext.panel.isModal()
        # the demo controller is a simulator: a lesson can start on it
        lesson = ext.begin("load_sheet")
        assert lesson is not None and w.hooks.provider("practice.active")()
        bus = w.ctl.events
        bus.publish(EV.JobStarted("process", "x"))
        bus.publish(EV.JobFinished("process", True, "done", duration_s=3.0))
        pump(app)
        ext.service.abort()
        assert not w.hooks.provider("practice.active")()
        bus.publish(EV.JobStarted("process", "y"))
        bus.publish(EV.JobFinished("process", True, "done", duration_s=3.0))
        pump(app)
        assert [j.source for j in rep.ledger.jobs()] == ["practice", "simulator"]
        assert rep.ledger.jobs(sources=("real",)) == []
        ext.panel.refresh()
        assert ext.panel.lessons.count() == len(PR.LESSONS)
    finally:
        w.close()


def test_the_load_step_opens_the_sheet_in_the_window(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import real_window
    w = real_window()
    try:
        ext = w.extension("ext_practice")
        lesson = ext.begin("load_sheet")
        lesson.next()
        assert lesson.execute()
        assert PR.is_practice_document(w.document)
        drive(lesson, {})
        pump(app)                                    # the lesson notifies on the GUI thread
        assert lesson.state == PE.DONE and ext.service.progress["load_sheet"].passed
        assert w.config.get("plugin.ext_practice.progress")["load_sheet"]["passed"]
    finally:
        w.close()


def test_the_coach_watches_a_task_and_aborts(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import real_window
    w = real_window()
    try:
        ext = w.extension("ext_practice")
        panel = ext.open()
        w.set_document(PR.practice_document())
        lesson = ext.begin("first_cut")                # the sheet is open: no load step
        panel._next()                                  # intro -> arm (a task)
        pump(app, 0.3)
        assert lesson.step.id == "arm" and lesson.state == PE.RUNNING     # watching the events
        assert panel.abort_btn.isEnabled() and not panel.next_btn.isEnabled()
        panel.abort_btn.click()
        lesson.join(5)
        pump(app)
        assert lesson.state == PE.ABORTED and not ext.service.running
        assert ext.service.progress["first_cut"].last_result == "aborted"
        assert panel.start_btn.isEnabled()
    finally:
        w.close()
