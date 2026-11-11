"""The UI's resume path against the Safety-core start gate (review 2): the open document goes to
``ctl.resume(mode, document=...)`` when the controller takes it, and "a job is streaming" is the
job's phase (review 2026-11-09 arch A1), not ``job_active()`` (which also counts a pending
breakpoint)."""
from mlaser.machine.jobphase import JobPhase as P
from mlaser.ui.resume import moving, resume

#: the mode the controller shows in each phase (machine.jobphase.derive_mode, worker alive)
_MODE = {P.IDLE: "ready", P.STARTING: "process", P.RUNNING: "process", P.HOLDING: "pause",
         P.PAUSING: "pause", P.STOPPING: "pause", P.AT_BREAKPOINT: "pause"}


class _State:
    def __init__(self, mode):
        self.mode, self.connected = mode, True


class NewCtl:
    def __init__(self, phase=P.IDLE):
        self.calls, self.phase = [], phase

    def resume(self, mode="exact", document=None):
        self.calls.append((mode, document))

    def state(self):
        return _State(_MODE[self.phase])

    def job_phase(self):
        return self.phase

    def job_active(self):
        return self.phase is not P.IDLE      # a pending breakpoint counts as active


class Unreadable(NewCtl):
    def job_phase(self):
        raise RuntimeError("no phase")


def test_document_is_always_passed():
    c, doc = NewCtl(), object()
    resume(c, "contour", doc)
    assert c.calls == [("contour", doc)]
    resume(c, "exact", None)
    assert c.calls[-1] == ("exact", None)


def test_moving_is_the_job_phase_not_job_active():
    assert not any(moving(NewCtl(p)) for p in (P.IDLE, P.HOLDING, P.PAUSING, P.AT_BREAKPOINT))
    assert all(moving(NewCtl(p)) for p in (P.STARTING, P.RUNNING))


def test_a_stopping_job_is_moving_although_its_mode_shows_pause():
    """Changed with arch A1: the Stop of a held job shows the mode "pause" until its worker
    ends; the old mode test let a resume through then (the controller refused it)."""
    c = NewCtl(P.STOPPING)
    assert c.state().mode == "pause" and moving(c)


def test_a_phase_that_cannot_be_read_counts_as_moving():
    assert moving(Unreadable(P.AT_BREAKPOINT))
