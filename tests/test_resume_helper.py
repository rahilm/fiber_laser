"""The UI's resume path against the Safety-core start gate (review 2): the open document goes to
``ctl.resume(mode, document=...)`` when the controller takes it, and "a job is streaming" is the
machine mode, not ``job_active()`` (which also counts a pending breakpoint)."""
from mlaser.ui.resume import moving, resume


class _State:
    def __init__(self, mode):
        self.mode = mode


class NewCtl:
    def __init__(self, mode="ready"):
        self.calls, self.mode = [], mode

    def resume(self, mode="exact", document=None):
        self.calls.append((mode, document))

    def state(self):
        return _State(self.mode)

    def job_active(self):
        return True                      # a pending breakpoint counts as active



def test_document_is_always_passed():
    c, doc = NewCtl(), object()
    resume(c, "contour", doc)
    assert c.calls == [("contour", doc)]
    resume(c, "exact", None)
    assert c.calls[-1] == ("exact", None)


def test_moving_is_the_machine_mode_not_job_active():
    assert not moving(NewCtl("ready")) and not moving(NewCtl("pause")) and not moving(NewCtl("stop"))
    assert moving(NewCtl("process")) and moving(NewCtl("frame")) and moving(NewCtl("dry_run"))
