"""Practice mode: scripted lessons for new operators on the simulator (review 2 features P1-7).

Each lesson is a :class:`~mlaser.services.procedures.engine.Procedure` (the step machine of the
guided procedures).  The operator works with the **normal Run screen buttons** (Frame, Arm,
START, HOLD, Reset, Resume ...); a lesson never presses them.  It only

* loads the practice sheet through its host (:class:`PracticeHost`);
* watches the controller's :class:`~mlaser.machine.events.EventBus` and passes a *task* step
  once the operator has done what it asks (an ``action`` step with ``auto=True`` that waits for
  the right events: ``JobStarted`` / ``JobFinished`` modes, ``Armed``, ``StateChanged``,
  ``BreakpointChanged``);
* plays its scenario on the simulator: the alarm lessons press the simulator's emergency-stop
  input mid-job (``CardSimulator.set_estop``) and release it again, like a colleague would on
  the training machine.

Safety:

* a lesson **refuses to start** unless the controller is the simulator (``is_simulator()``);
  it then raises :class:`PracticeError` before it sends or touches anything;
* abort is the engine's: ``safe_halt`` on the (simulated) machine, then the lesson releases the
  injected emergency stop;
* jobs run while a lesson is active are recorded with source ``practice``
  (:mod:`mlaser.services.runsource`): they never count as production.

:class:`PracticeService` keeps the lesson list, the active lesson and the progress (per lesson:
passed, attempts, best and last time), stored through a ``load`` / ``save`` pair (the plugin uses
its ``plugin.ext_practice.progress`` setting).

Qt-free.  Step ``title`` / ``text`` are catalogue ids; :data:`TEXTS` has their English text.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional, Protocol

from mlaser.machine import events as EV
from mlaser.model import Arc, Contour, Craft, Document, LayerParams, Line, Part

from .procedures.engine import ABORTED, DONE, FAILED, FINAL, Procedure, Step, StepFailed

log = logging.getLogger("mlaser.practice")

TASK_TIMEOUT_S = 900.0              # an operator task waits this long before the lesson fails

#: English texts of the catalogue ids the lessons use (the UI's default text)
TEXTS: dict[str, str] = {
    "practice.lesson.load_sheet": "Load a sheet",
    "practice.lesson.frame": "Frame the job",
    "practice.lesson.first_cut": "Arm and start a cut",
    "practice.lesson.hold": "Hold and continue",
    "practice.lesson.alarm": "Handle an alarm",
    "practice.lesson.resume": "Resume from the breakpoint",
    "practice.step.intro_sim":
        "This lesson runs on the simulator: nothing moves and no laser fires. Use the normal "
        "Run screen buttons; this panel tells you what to do and checks it.",
    "practice.step.load.title": "Load the practice sheet",
    "practice.step.load.text": "Press Load to open the practice sheet: three parts on a {sheet} sheet. "
                               "Look at it in the live view: the parts, their holes and the start points.",
    "practice.step.loaded.title": "Sheet loaded",
    "practice.step.loaded.text": "The practice sheet is open with cutting parameters on every layer. On the real "
                                 "machine you would now put the sheet on the table and check the material.",
    "practice.step.frame.title": "Frame",
    "practice.step.frame.text": "Press Frame on the Run screen. The head travels around the job outline with the "
                                "laser off, so you can see that the job fits on the sheet.",
    "practice.step.arm.title": "Arm the laser",
    "practice.step.arm.text": "Press Arm laser and confirm. Armed means the laser can fire on START: the "
                              "state banner turns red.",
    "practice.step.start.title": "Start the cut",
    "practice.step.start.text": "Press START. The pre-flight check runs first; then the job is cut. Watch the "
                                "progress and the parts turning grey in the live view.",
    "practice.step.finish.title": "Wait for the end of the job",
    "practice.step.finish.text": "Let the job run to the end. After the last part the machine is idle again.",
    "practice.step.hold.title": "Hold",
    "practice.step.hold.text": "While the job cuts, press HOLD (or Space). The machine stops on the path and "
                               "keeps a breakpoint: the job is not lost.",
    "practice.step.continue.title": "Continue",
    "practice.step.continue.text": "Press START / CONTINUE (or a Resume button). The head goes back to the "
                                   "breakpoint with the laser off, pierces again and the job goes on.",
    "practice.step.alarm_wait.title": "Wait for the alarm",
    "practice.step.alarm_wait.text": "Start the cut (arm first). During the cut the lesson presses the simulator's "
                                     "emergency stop: the machine stops and latches a FAULT.",
    "practice.step.alarm_read.title": "Read the alarm",
    "practice.step.alarm_read.text": "The state is FAULT. Read the alarm list on the Run screen: what happened and "
                                     "the remedy. Find the cause before you reset. On the real machine you "
                                     "would now release the e-stop; the lesson releases it for you.",
    "practice.step.reset.title": "Reset",
    "practice.step.reset.text": "Press Reset alarm. The machine comes back disarmed, with the breakpoint of the "
                                "stopped job kept.",
    "practice.step.done_alarm.title": "Alarm handled",
    "practice.step.done_alarm.text": "Well done. Press Stop to drop the stopped job, or practise resuming it in the "
                                     "next lesson.",
    "practice.step.resume.title": "Resume from the breakpoint",
    "practice.step.resume.text": "Arm again, then press Resume: Here (exactly where it stopped) or Contour start "
                                 "(from the start of that contour). The job must finish.",
    "practice.step.pass.title": "Lesson passed",
    "practice.step.pass.text": "You did every step of this lesson.",
    "practice.error.not_simulator": "Practice runs only on the simulator. Connect the simulator first "
                                    "(Machine > Connect simulator); the real machine is never used.",
    "practice.error.no_injection": "This simulator cannot simulate an alarm.",
    "practice.error.load_failed": "The practice sheet could not be opened (a job may be running).",
    "practice.error.busy": "Finish or abort the running lesson first.",
}


class PracticeError(RuntimeError):
    """A lesson cannot start (catalogue id in ``key``)."""

    def __init__(self, key: str) -> None:
        super().__init__(TEXTS.get(key, key))
        self.key = key


class PracticeHost(Protocol):
    """What a lesson needs from the application besides the controller."""

    def load_document(self, doc: Document) -> bool: ...

    def document(self) -> Optional[Document]: ...


# ============================================================================== the sheet
def _rect(x0: float, y0: float, w: float, h: float) -> Contour:
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def _circle(cx: float, cy: float, r: float) -> Contour:
    return Contour([Arc((cx + r, cy), (cx - r, cy), (cx, cy), True),
                    Arc((cx - r, cy), (cx + r, cy), (cx, cy), True)], True)


PRACTICE_SPEED = 40.0               # mm/s: slow enough to Hold in time at the simulator's 1x


def practice_document() -> Document:
    """The practice sheet: three parts (a plate with two holes, a bracket, a washer) inside a
    300 x 200 mm sheet, on layer 1 with low simulator parameters.  Marked
    ``extra["practice"]`` so a lesson recognises it."""
    plate = Part([_circle(60.0, 60.0, 12.0), _circle(140.0, 60.0, 12.0), _rect(20.0, 20.0, 160.0, 80.0)],
                 Craft(layer=1), name="practice plate")
    bracket = Part([_rect(200.0, 20.0, 80.0, 60.0)], Craft(layer=1), name="practice bracket")
    washer = Part([_circle(240.0, 140.0, 10.0), _circle(240.0, 140.0, 30.0)], Craft(layer=1),
                  name="practice washer")
    doc = Document(parts=[plate, bracket, washer],
                   layers={1: LayerParams(1, speed=PRACTICE_SPEED, power_pct=30.0)})
    doc.extra["practice"] = True
    return doc


def is_practice_document(doc: Any) -> bool:
    try:
        return bool(doc is not None and doc.extra.get("practice") and doc.parts)
    except Exception:  # noqa: BLE001
        return False


# ============================================================================== event log
class EventLog:
    """Every event of the controller's bus since the lesson started (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list = []
        self._unsub: Optional[Callable[[], None]] = None

    def attach(self, bus: Any) -> None:
        self.detach()
        self._unsub = bus.subscribe(EV.Event, self._on)

    def detach(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _on(self, ev: Any) -> None:
        with self._lock:
            self.events.append(ev)

    def mark(self) -> int:
        with self._lock:
            return len(self.events)

    def since(self, mark: int) -> list:
        with self._lock:
            return list(self.events[mark:])


def _started(evs: list, mode: str, resume: Optional[bool] = None) -> bool:
    return any(isinstance(e, EV.JobStarted) and e.mode == mode and (resume is None or e.resume == resume)
               for e in evs)


def _finished_ok(evs: list, mode: str) -> bool:
    return any(isinstance(e, EV.JobFinished) and e.mode == mode and e.ok for e in evs)


def _breakpoint(evs: list) -> bool:
    return any(isinstance(e, EV.BreakpointChanged) and e.breakpoint is not None for e in evs)


def _fault(evs: list) -> bool:
    return any(isinstance(e, EV.StateChanged) and e.new == "fault" for e in evs)


def _fault_reset(evs: list) -> bool:
    return any(isinstance(e, EV.StateChanged) and e.old == "fault" and e.new != "fault" for e in evs)


# ============================================================================== lessons
class Lesson(Procedure):
    """Base class of the practice lessons (see the module docstring)."""

    id = "practice"
    title = ""
    min_mode = "operator"
    supports_dry_run = False
    needs_injection = False
    order = 0
    _recorded = False                        # the service stored this attempt's result

    def __init__(self, machine: Any, host: Optional[PracticeHost] = None,
                 task_timeout_s: float = TASK_TIMEOUT_S, log_fn: Optional[Callable[[str], None]] = None) -> None:
        self.host = host
        self.task_timeout_s = float(task_timeout_s)
        self.events = EventLog()
        self._mark = 0
        self._estop = False
        self.started_at = 0.0
        self.finished_at = 0.0
        super().__init__(machine, dry_run=False, log_fn=log_fn)

    # ------------------------------------------------------------------ checks
    @staticmethod
    def allowed(machine: Any) -> bool:
        """The controller is the simulator (never a real card)."""
        try:
            fn = getattr(machine, "is_simulator", None)
            return bool(callable(fn) and fn())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def can_inject(machine: Any) -> bool:
        sim = getattr(machine, "sim", None)
        return callable(getattr(sim, "set_estop", None)) or callable(getattr(machine, "raise_fault", None))

    def start(self) -> None:
        if not self.allowed(self.machine):
            raise PracticeError("practice.error.not_simulator")
        if self.needs_injection and not self.can_inject(self.machine):
            raise PracticeError("practice.error.no_injection")
        self.events.attach(self.machine.events)
        self.started_at = time.time()
        super().start()

    def _enter_next(self) -> None:
        self._mark = self.events.mark()          # a task counts what happens from its step on
        super()._enter_next()
        if self.state in FINAL:
            self._end()

    def on_abort(self) -> None:
        self._release_estop()
        self._end()

    def on_finish(self) -> None:
        self.finished_at = time.time()
        self._release_estop()

    def _end(self) -> None:
        self.events.detach()

    @property
    def elapsed_s(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at) if self.started_at else 0.0

    # ------------------------------------------------------------------ step builders
    def info(self, sid: str, title: str, text: str) -> Step:
        return Step(sid, title, text, "info")

    def task(self, sid: str, title: str, text: str, cond: Callable[[list], bool]) -> Step:
        """Wait (auto) until ``cond(events since the step began)`` holds."""
        def run(p: "Lesson") -> bool:
            p.wait(lambda: cond(p.events.since(p._mark)), p.task_timeout_s, sid, poll_s=0.05)
            return True
        return Step(sid, title, text, "action", run=run, auto=True)

    def load_step(self) -> Step:
        def run(p: "Lesson") -> bool:
            host = p.host
            if host is None or not host.load_document(practice_document()):
                raise StepFailed("practice.error.load_failed")
            if not is_practice_document(host.document()):
                raise StepFailed("practice.error.load_failed")
            return True
        return Step("load", "practice.step.load.title", "practice.step.load.text", "action", run=run,
                    action_label="practice.btn.load")

    def ensure_loaded(self) -> list[Step]:
        """The lessons after the first load the sheet themselves when it is not open."""
        host = self.host

        def skip(_p: Any) -> bool:
            return host is not None and is_practice_document(host.document())
        s = self.load_step()
        s.skip = skip
        return [s]

    def passed(self) -> Step:
        return self.info("pass", "practice.step.pass.title", "practice.step.pass.text")

    # ------------------------------------------------------------------ scenario: e-stop
    def inject_estop(self) -> None:
        sim: Any = getattr(self.machine, "sim", None)
        if callable(getattr(sim, "set_estop", None)):
            sim.set_estop(True)
            self._estop = True
            self.log("simulated emergency stop pressed")
        else:
            self.machine.raise_fault("practice: simulated emergency stop")

    def _release_estop(self) -> None:
        if not self._estop:
            return
        sim: Any = getattr(self.machine, "sim", None)
        try:
            sim.set_estop(False)
            self.log("simulated emergency stop released")
        except Exception:  # noqa: BLE001 - the simulator is gone
            pass
        self._estop = False

    def alarm_steps(self) -> list[Step]:
        """Start, the injected e-stop mid-job, read, reset."""
        def cut_then_trip(p: "Lesson") -> bool:
            def running_long_enough() -> bool:
                evs = p.events.since(p._mark)
                if not _started(evs, "process", resume=False):
                    return False
                # the card is cutting: the e-stop comes mid-cut, never during the host's
                # preparation of the job (on a slow PC that took longer than the 1.5 s the
                # fallback used to count from JobStarted, and the stop came before the cut)
                prog = [(e.t, e.fraction) for e in evs
                        if isinstance(e, EV.JobProgress) and e.mode == "process" and e.fraction > 0]
                if not prog:
                    return False
                return max(f for _t, f in prog) >= 0.15 or time.time() - prog[0][0] > 1.5
            p.wait(running_long_enough, p.task_timeout_s, "alarm_wait")
            p.inject_estop()
            p.wait(lambda: _fault(p.events.since(p._mark)), 30.0, "fault")
            p._release_estop()
            return True
        return [
            Step("alarm_wait", "practice.step.alarm_wait.title", "practice.step.alarm_wait.text", "action",
                 run=cut_then_trip, auto=True),
            self.info("alarm_read", "practice.step.alarm_read.title", "practice.step.alarm_read.text"),
            self.task("reset", "practice.step.reset.title", "practice.step.reset.text", _fault_reset),
        ]


class LoadSheetLesson(Lesson):
    id = "load_sheet"
    title = "practice.lesson.load_sheet"
    order = 10

    def build_steps(self) -> list[Step]:
        return [self.info("intro", "practice.lesson.load_sheet", "practice.step.intro_sim"),
                self.load_step(),
                self.info("loaded", "practice.step.loaded.title", "practice.step.loaded.text"),
                self.passed()]


class FrameLesson(Lesson):
    id = "frame"
    title = "practice.lesson.frame"
    order = 20

    def build_steps(self) -> list[Step]:
        return [self.info("intro", "practice.lesson.frame", "practice.step.intro_sim"), *self.ensure_loaded(),
                self.task("frame", "practice.step.frame.title", "practice.step.frame.text",
                          lambda evs: _started(evs, "frame") and _finished_ok(evs, "frame")),
                self.passed()]


class FirstCutLesson(Lesson):
    id = "first_cut"
    title = "practice.lesson.first_cut"
    order = 30

    def build_steps(self) -> list[Step]:
        return [self.info("intro", "practice.lesson.first_cut", "practice.step.intro_sim"), *self.ensure_loaded(),
                self.task("arm", "practice.step.arm.title", "practice.step.arm.text",
                          lambda evs: any(isinstance(e, EV.Armed) for e in evs) or self._armed()),
                self.task("start", "practice.step.start.title", "practice.step.start.text",
                          lambda evs: _started(evs, "process", resume=False)),
                self.task("finish", "practice.step.finish.title", "practice.step.finish.text",
                          lambda evs: _finished_ok(evs, "process")),
                self.passed()]

    def _armed(self) -> bool:
        try:
            return bool(self.machine.safety().armed)
        except Exception:  # noqa: BLE001
            return False


class HoldLesson(Lesson):
    id = "hold"
    title = "practice.lesson.hold"
    order = 40

    def build_steps(self) -> list[Step]:
        return [self.info("intro", "practice.lesson.hold", "practice.step.intro_sim"), *self.ensure_loaded(),
                self.task("start", "practice.step.start.title", "practice.step.start.text",
                          lambda evs: _started(evs, "process", resume=False)),
                self.task("hold", "practice.step.hold.title", "practice.step.hold.text", _breakpoint),
                self.task("continue", "practice.step.continue.title", "practice.step.continue.text",
                          lambda evs: _started(evs, "process", resume=True)),
                self.task("finish", "practice.step.finish.title", "practice.step.finish.text",
                          lambda evs: _finished_ok(evs, "process")),
                self.passed()]


class AlarmLesson(Lesson):
    id = "alarm"
    title = "practice.lesson.alarm"
    order = 50
    needs_injection = True

    def build_steps(self) -> list[Step]:
        return [self.info("intro", "practice.lesson.alarm", "practice.step.intro_sim"), *self.ensure_loaded(),
                *self.alarm_steps(),
                self.info("done_alarm", "practice.step.done_alarm.title", "practice.step.done_alarm.text"),
                self.passed()]


class ResumeLesson(Lesson):
    id = "resume"
    title = "practice.lesson.resume"
    order = 60
    needs_injection = True

    def build_steps(self) -> list[Step]:
        return [self.info("intro", "practice.lesson.resume", "practice.step.intro_sim"), *self.ensure_loaded(),
                *self.alarm_steps(),
                self.task("resume", "practice.step.resume.title", "practice.step.resume.text",
                          lambda evs: _started(evs, "process", resume=True)),
                self.task("finish", "practice.step.finish.title", "practice.step.finish.text",
                          lambda evs: _finished_ok(evs, "process")),
                self.passed()]


LESSONS: tuple[type[Lesson], ...] = (LoadSheetLesson, FrameLesson, FirstCutLesson, HoldLesson, AlarmLesson,
                                     ResumeLesson)


def lesson_class(lesson_id: str) -> type[Lesson]:
    for c in LESSONS:
        if c.id == lesson_id:
            return c
    raise KeyError(lesson_id)


# ============================================================================== progress
@dataclass
class LessonProgress:
    passed: bool = False
    attempts: int = 0
    best_s: float = 0.0             # fastest pass (0 = never passed)
    last_s: float = 0.0
    last_result: str = ""           # done | aborted | failed
    last_t: float = 0.0

    @classmethod
    def from_json(cls, d: Any) -> "LessonProgress":
        if not isinstance(d, dict):
            return cls()
        names = cls.__dataclass_fields__
        try:
            return cls(**{k: v for k, v in d.items() if k in names})
        except TypeError:
            return cls()


class PracticeService:
    """Lessons, the active lesson and the progress.  ``load()`` returns the stored progress
    dict, ``save(dict)`` stores it."""

    def __init__(self, load: Callable[[], Any] = dict, save: Callable[[dict], None] = lambda d: None,
                 clock: Callable[[], float] = time.time) -> None:
        self._load, self._save, self.clock = load, save, clock
        self.active: Optional[Lesson] = None
        self.listeners: list[Callable[[], None]] = []
        try:
            raw = load() or {}
        except Exception:  # noqa: BLE001 - unreadable progress starts empty
            raw = {}
        self.progress: dict[str, LessonProgress] = {
            c.id: LessonProgress.from_json(raw.get(c.id) if isinstance(raw, dict) else None) for c in LESSONS}

    @property
    def running(self) -> bool:
        """A lesson is active (its jobs are practice jobs)."""
        a = self.active
        return a is not None and a.state not in FINAL

    def lessons(self) -> list[type[Lesson]]:
        return sorted(LESSONS, key=lambda c: c.order)

    def passed_count(self) -> int:
        return sum(1 for p in self.progress.values() if p.passed)

    def begin(self, lesson_id: str, machine: Any, host: Optional[PracticeHost] = None,
              task_timeout_s: float = TASK_TIMEOUT_S, log_fn: Optional[Callable[[str], None]] = None,
              post: Optional[Callable[[Callable[[], None]], None]] = None) -> Lesson:
        """Make and start a lesson (PracticeError: not on the simulator, or one runs)."""
        if self.running:
            raise PracticeError("practice.error.busy")
        cls = lesson_class(lesson_id)
        if not Lesson.allowed(machine):
            raise PracticeError("practice.error.not_simulator")
        lesson = cls(machine, host, task_timeout_s, log_fn)
        lesson.post = post
        lesson.listeners.append(self._lesson_changed)
        prog = self.progress.setdefault(lesson_id, LessonProgress())
        self.active = lesson
        try:
            lesson.start()
        except PracticeError:
            self.active = None
            raise
        prog.attempts += 1
        prog.last_t = self.clock()
        self._store()
        self._notify()
        return lesson

    def abort(self, reason: str = "operator") -> None:
        if self.active is not None and self.active.state not in FINAL:
            self.active.abort(reason)

    def _lesson_changed(self, lesson: Procedure) -> None:
        if lesson is not self.active or not isinstance(lesson, Lesson):
            return
        if lesson.state in FINAL and not getattr(lesson, "_recorded", False):
            lesson._recorded = True              # an abort after the end notifies again
            prog = self.progress.setdefault(lesson.id, LessonProgress())
            prog.last_result = {DONE: "done", ABORTED: "aborted", FAILED: "failed"}.get(lesson.state, "")
            prog.last_s = round(lesson.elapsed_s, 1)
            if lesson.state == DONE:
                prog.passed = True
                prog.best_s = prog.last_s if not prog.best_s else min(prog.best_s, prog.last_s)
            self._store()
        self._notify()

    def _store(self) -> None:
        try:
            self._save({k: asdict(v) for k, v in self.progress.items()})
        except Exception:  # noqa: BLE001 - progress is a convenience, never an error
            log.exception("practice progress not saved")

    def reset_progress(self) -> None:
        self.progress = {c.id: LessonProgress() for c in LESSONS}
        self._store()
        self._notify()

    def _notify(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("practice listener failed")


__all__ = ["Lesson", "LESSONS", "LoadSheetLesson", "FrameLesson", "FirstCutLesson", "HoldLesson", "AlarmLesson",
           "ResumeLesson", "PracticeService", "PracticeError", "PracticeHost", "LessonProgress", "EventLog",
           "practice_document", "is_practice_document", "lesson_class", "TEXTS", "TASK_TIMEOUT_S"]
