"""The step machine behind the guided procedures (wizards), Qt-free.

A :class:`Procedure` is an ordered list of :class:`Step` s run against a
:class:`~mlaser.machine.api.MachineAPI`.  The UI (``mlaser.ui.wizards``) only shows the current
step and calls :meth:`Procedure.next`, :meth:`Procedure.execute` and :meth:`Procedure.abort`;
everything that talks to the machine is here, so every procedure is tested against the
simulator without a display (``tests/test_procedures.py``).

Step kinds:

* ``info``: text and safety notes, the operator presses Next.
* ``input``: the UI edits the procedure's parameters (a form), then Next.
* ``action``: :attr:`Step.run` runs in a worker thread when the operator presses Run (or at
  once with ``auto``); Next is possible after it succeeded.  An exception fails the procedure
  (and halts the machine); :class:`StepFailed` carries a catalogue id for the message.
* ``confirm``: a result to accept (Next) or reject (Back / Abort); e.g. the measured sheet.
* ``question``: the operator answers yes / no (:meth:`Procedure.answer`), e.g. "did the head move
  to the right?"; Next after an answer.

Safety (docs/SAFETY.md):

* :meth:`Procedure.abort` is possible in every state and always sends ``machine.safe_halt``
  (also when nothing moves), then runs the procedure's own clean-up (:meth:`on_abort`: disarm,
  cancel the follower calibration...).  A failing action halts too.
* An action that moves or fires first declares a new operator action
  (``machine.operator_action``): the halt latch of the supervisor is cleared only by the button
  press that started this step, never by a worker on its own.
* A step's long waits go through :meth:`Procedure.wait` / :meth:`Procedure.sleep`, which raise
  :class:`ProcedureAborted` as soon as the operator aborted.
* ``dry_run``: the procedure runs against a private simulator (``procedures.simrig``); results
  are shown but never applied to the document or the configuration.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("mlaser.procedures")

# procedure states
READY, WAITING, RUNNING, DONE, ABORTED, FAILED = "ready", "waiting", "running", "done", "aborted", "failed"
FINAL = (DONE, ABORTED, FAILED)

# user modes (Wave 3 Operator / Engineer / Service); a procedure declares the lowest one
MODES = ("operator", "engineer", "service")


def mode_allows(current: Optional[str], required: str) -> bool:
    """True when ``current`` (None: no modes configured) may use something that needs
    ``required``."""
    if current is None or current not in MODES:
        return True
    return MODES.index(current) >= MODES.index(required if required in MODES else "operator")


class ProcedureAborted(Exception):
    """The operator aborted (raised inside a running step)."""


class StepFailed(Exception):
    """A step failed with a message for the operator: catalogue id + named values."""

    def __init__(self, key: str, **values: Any) -> None:
        super().__init__(key)
        self.key = key
        self.values = values

    def __str__(self) -> str:
        return f"{self.key} {self.values}" if self.values else self.key


@dataclass
class Step:
    id: str
    title: str                                   # catalogue id
    text: str = ""                               # catalogue id of the instructions
    kind: str = "info"                           # info | input | action | confirm | question
    safety: tuple = ()                           # catalogue ids of the safety notes
    run: Optional[Callable[["Procedure"], Any]] = None
    auto: bool = False                           # action: start when the step is entered
    motion: bool = False                         # the step moves the axes or fires
    skip: Optional[Callable[["Procedure"], bool]] = None
    action_label: str = ""                       # catalogue id of the Run button
    jog: bool = False                            # the UI offers hold-to-jog on this step


@dataclass
class StepRecord:
    step: str
    outcome: str                                 # done | failed | aborted | skipped
    detail: str = ""
    t: float = field(default_factory=time.time)


class Procedure:
    """Base class: subclasses implement :meth:`build_steps` (and optionally the hooks)."""

    id = "procedure"
    title = ""                                   # catalogue id
    min_mode = "operator"
    supports_dry_run = True

    def __init__(self, machine: Any, dry_run: bool = False,
                 log_fn: Optional[Callable[[str], None]] = None) -> None:
        self.machine = machine
        self.dry_run = bool(dry_run)
        self._log_fn = log_fn
        self.cancel = threading.Event()
        self.state = READY
        self.index = 0
        self.results: dict[str, Any] = {}
        self.answers: dict[str, bool] = {}
        self.done: set[str] = set()
        self.error: Optional[StepFailed] = None
        self.history: list[StepRecord] = []
        self.listeners: list[Callable[["Procedure"], None]] = []
        self.halts = 0                           # safe_halt calls made by this procedure
        self._worker: Optional[threading.Thread] = None
        # ``post(fn)`` runs fn where the listeners live (the GUI thread); None = synchronously
        self.post: Optional[Callable[[Callable[[], None]], None]] = None
        self.steps: list[Step] = self.build_steps()

    # ------------------------------------------------------------------ subclass API
    def build_steps(self) -> list[Step]:
        raise NotImplementedError

    def on_abort(self) -> None:
        """Clean-up after an abort or a failure (after the halt)."""

    def on_finish(self) -> None:
        """Clean-up after the last step."""

    # ------------------------------------------------------------------ helpers
    def log(self, msg: str) -> None:
        log.info("%s: %s", self.id, msg)
        if self._log_fn is not None:
            try:
                self._log_fn(msg)
            except Exception:  # noqa: BLE001 - logging must not break a step
                pass

    def _notify(self) -> None:
        if self.post is not None:
            try:
                self.post(self._notify_now)
                return
            except Exception:  # noqa: BLE001 - the GUI is gone: deliver here
                pass
        self._notify_now()

    def _notify_now(self) -> None:
        for fn in list(self.listeners):
            try:
                fn(self)
            except Exception:  # noqa: BLE001
                log.exception("procedure listener failed")

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise ProcedureAborted(self.id)

    def sleep(self, seconds: float) -> None:
        if self.cancel.wait(max(0.0, seconds)):
            raise ProcedureAborted(self.id)

    def wait(self, cond: Callable[[], bool], timeout_s: float, what: str, poll_s: float = 0.05) -> None:
        """Wait for ``cond()``; ProcedureAborted on abort, StepFailed after ``timeout_s``."""
        t_end = time.monotonic() + timeout_s
        while not cond():
            self.check_cancel()
            if time.monotonic() > t_end:
                raise StepFailed("wizard.error.timeout", what=what, seconds=round(timeout_s))
            self.sleep(poll_s)

    def operator_action(self, what: str) -> None:
        """The operator started a step that moves or fires: a new action for the supervisor."""
        self.check_cancel()
        self.machine.operator_action(f"{self.id}: {what}")

    def require_connected(self) -> None:
        st = self.machine.state()
        if not st.connected:
            raise StepFailed("wizard.error.not_connected")
        saf = self.machine.safety()
        if saf.fault:
            raise StepFailed("wizard.error.fault", fault=saf.fault)
        if self.machine.job_active():
            raise StepFailed("wizard.error.job_running")

    # ------------------------------------------------------------------ navigation
    @property
    def step(self) -> Optional[Step]:
        return self.steps[self.index] if 0 <= self.index < len(self.steps) else None

    def _skipped(self, s: Step) -> bool:
        return bool(s.skip and s.skip(self))

    def visible_steps(self) -> list[Step]:
        return [s for s in self.steps if not self._skipped(s)]

    def start(self) -> None:
        """Enter the first step."""
        if self.state != READY:
            return
        self.index = -1
        self._enter_next()

    def can_next(self) -> bool:
        s = self.step
        if s is None or self.state != WAITING:
            return False
        if s.kind == "question":
            return s.id in self.answers
        return s.kind != "action" or s.id in self.done

    def answer(self, value: bool) -> None:
        """Answer the current ``question`` step."""
        s = self.step
        if s is None or s.kind != "question" or self.state != WAITING:
            raise RuntimeError(f"{self.id}: no question to answer")
        self.answers[s.id] = bool(value)
        self.on_answer(s, bool(value))
        self._notify()

    def on_answer(self, step: Step, value: bool) -> None:
        """Hook: a question was answered."""

    def can_back(self) -> bool:
        """Back over info / input / confirm steps only (an action already ran)."""
        if self.state != WAITING:
            return False
        i = self.index - 1
        while i >= 0 and self._skipped(self.steps[i]):
            i -= 1
        return i >= 0 and self.steps[i].kind in ("info", "input", "confirm") and \
            self.steps[self.index].kind in ("info", "input", "confirm")

    def next(self) -> None:
        if not self.can_next():
            raise RuntimeError(f"{self.id}: cannot go on from {self.step.id if self.step else '-'} ({self.state})")
        s = self.step
        if s is not None and s.kind != "action":
            self.done.add(s.id)
            self.history.append(StepRecord(s.id, "done"))
        self._enter_next()

    def back(self) -> None:
        if not self.can_back():
            return
        i = self.index - 1
        while i >= 0 and self._skipped(self.steps[i]):
            i -= 1
        self.index = i
        self.done.discard(self.steps[i].id)
        self._notify()

    def _enter_next(self) -> None:
        i = self.index + 1
        while i < len(self.steps) and self._skipped(self.steps[i]):
            self.history.append(StepRecord(self.steps[i].id, "skipped"))
            i += 1
        self.index = i
        if i >= len(self.steps):
            self.state = DONE
            try:
                self.on_finish()
            finally:
                self._notify()
            return
        self.state = WAITING
        self._notify()

    def should_auto_run(self) -> bool:
        s = self.step
        return bool(s is not None and self.state == WAITING and s.kind == "action" and s.auto
                    and s.id not in self.done)

    # ------------------------------------------------------------------ actions
    def execute(self, _worker: bool = False) -> bool:
        """Run the current action step synchronously; True on success."""
        s = self.step
        if self.state == ABORTED:
            return False
        ok_state = self.state == RUNNING if _worker else self.state == WAITING
        if s is None or s.kind != "action" or not ok_state or s.run is None:
            raise RuntimeError(f"{self.id}: no action to run")
        self.state = RUNNING
        self._notify()
        try:
            r = s.run(self)
        except ProcedureAborted:
            if self.state != ABORTED:
                self._abort("operator", notify=False)
            self._notify()
            return False
        except Exception as e:  # noqa: BLE001 - every failure is reported and halts
            if self.cancel.is_set():         # the abort made the step fail (refused, cancelled)
                if self.state != ABORTED:
                    self._abort("operator", notify=False)
                self._notify()
                return False
            if not isinstance(e, StepFailed):
                log.exception("%s: step %s failed", self.id, s.id)
                e = StepFailed("wizard.error.step", detail=f"{type(e).__name__}: {e}")
            self._fail(s, e)
            return False
        if self.state == ABORTED:            # aborted while the step was finishing
            return False
        self.results[s.id] = r
        self.done.add(s.id)
        self.history.append(StepRecord(s.id, "done"))
        self.state = WAITING
        self._notify()
        return True

    def execute_async(self) -> threading.Thread:
        """Run the current action in a worker thread (set :attr:`post` so the listeners run on
        the GUI thread)."""
        s = self.step
        if s is None or s.kind != "action" or self.state != WAITING:
            raise RuntimeError(f"{self.id}: no action to run")
        self.state = RUNNING             # at once: a second click cannot start it twice
        t = threading.Thread(target=self._run_worker, name=f"mlaser-proc-{self.id}", daemon=True)
        self._worker = t
        t.start()
        return t

    def _run_worker(self) -> None:
        self.execute(_worker=True)

    def busy(self) -> bool:
        return self.state == RUNNING

    def join(self, timeout: Optional[float] = None) -> None:
        if self._worker is not None:
            self._worker.join(timeout)

    # ------------------------------------------------------------------ abort / fail
    def _halt(self, reason: str) -> None:
        self.halts += 1
        try:
            self.machine.safe_halt(reason)
        except Exception:  # noqa: BLE001 - the halt is best effort; the supervisor repeats it
            log.exception("%s: safe_halt failed", self.id)

    def abort(self, reason: str = "operator") -> None:
        """Abort now: cancel the running step, ``safe_halt``, clean up.  Possible in every
        state (a finished procedure still halts: the button is a stop button)."""
        self._abort(reason)

    def _abort(self, reason: str, notify: bool = True) -> None:
        self.cancel.set()
        self._halt(f"{self.id}: aborted ({reason})")
        if self.state in FINAL and self.state != DONE:
            if notify:
                self._notify()
            return
        sid = self.step.id if self.step else "-"
        self.state = ABORTED
        self.history.append(StepRecord(sid, "aborted", reason))
        try:
            self.on_abort()
        except Exception:  # noqa: BLE001
            log.exception("%s: on_abort failed", self.id)
        self.log(f"aborted at {sid} ({reason})")
        if notify:
            self._notify()

    def _fail(self, s: Step, e: StepFailed) -> None:
        self.error = e
        self.state = FAILED
        self.history.append(StepRecord(s.id, "failed", str(e)))
        self.cancel.set()
        self._halt(f"{self.id}: step {s.id} failed")
        try:
            self.on_abort()
        except Exception:  # noqa: BLE001
            log.exception("%s: on_abort failed", self.id)
        self.log(f"step {s.id} failed: {e}")
        self._notify()


__all__ = ["Procedure", "Step", "StepRecord", "StepFailed", "ProcedureAborted", "mode_allows",
           "MODES", "READY", "WAITING", "RUNNING", "DONE", "ABORTED", "FAILED", "FINAL"]
