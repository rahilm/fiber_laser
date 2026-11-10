"""Order inside the commissioning checklist (UX review 2026-11-09, finding 4).

The checklist used to let Home and Jog run while "Card scaling" was failing (a 10x mismatch
makes a 10 mm jog a 100 mm move) and before the emergency stop was tested, and it ended
"Finished" with failed checks.  Now a motion step is disabled with the reason until the checks
it depends on pass (scaling before any motion, the e-stop test before a jog), it can be left for
later, and a checklist with failures ends "Not finished: N checks failed".  The real MainWindow
and wizard, offscreen, against the card simulator."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/wiring.py


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def wait(app, pred, timeout=10.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


def _window(app, monkeypatch, tmp_path, **sim_kw):
    from PySide6.QtWidgets import QDialog, QMessageBox
    from wiring import real_window

    from mlaser.controller.ui_adapter import UiController
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    ctl = UiController(poll_s=0.02)
    assert ctl.connect_simulator(time_scale=1.0, **sim_kw)
    return real_window(ctl)


@pytest.fixture
def sim_win(app, monkeypatch, tmp_path):
    from wiring import dispose
    w = _window(app, monkeypatch, tmp_path)
    yield w
    w.ctl.disconnect()
    dispose(w)


@pytest.fixture
def mismatch_win(app, monkeypatch, tmp_path):
    """The card reports 200 p/mm; the PC's parameters say 100 (the reviewer's 10x case in kind)."""
    from wiring import dispose
    w = _window(app, monkeypatch, tmp_path, ppm=200.0, step_ppm=200.0)
    yield w
    w.ctl.disconnect()
    dispose(w)


def _wizard(w):
    from mlaser.ui.wizards.procedures import CommissioningWizard
    return CommissioningWizard(w, w.extension("ext_machine"))


def _go_to(app, d, step_id, timeout=20.0):
    """Next through the checklist (auto steps run by themselves) up to ``step_id``."""
    p = d.proc
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        app.processEvents()
        s = p.step
        assert s is not None and p.state not in ("failed", "aborted"), (p.state, p.error)
        if s.id == step_id and p.state == "waiting":
            d.refresh()
            return
        if p.state == "waiting" and p.can_next():
            d.next()
        elif p.state == "waiting" and s.kind == "question":
            d.answer(True)
        time.sleep(0.01)
    raise AssertionError(f"did not reach {step_id}: at {p.step.id if p.step else None} ({p.state})")


def _moving(w) -> bool:
    return any(a.busy for a in w.ctl.sim.state.axes)


def test_homing_and_jog_refused_while_scaling_fails(app, mismatch_win):
    w = mismatch_win
    d = _wizard(w)
    try:
        _go_to(app, d, "homing")
        assert d.proc.items["scaling"].status == "fail"
        assert d.b_run.isVisibleTo(d) and not d.b_run.isEnabled()
        assert "card scaling" in d.b_run.toolTip().lower()
        assert "card scaling" in d.status.text().lower()
        pos = (w.ctl.state().x, w.ctl.state().y)
        d.run()                                     # a click that slipped through: refused
        wait(app, lambda: False, 0.4)
        assert d.proc.step.id == "homing" and "homing" not in d.proc.done
        assert not _moving(w) and (w.ctl.state().x, w.ctl.state().y) == pos and not w.ctl.state().homed
        # the procedure refuses on its own too, before anything moves
        assert d.proc.execute() is False and d.proc.error is not None
        assert d.proc.error.key == "wizard.comm.blocked_scaling"
        assert not _moving(w) and not w.ctl.state().homed
    finally:
        d.done(0)


def test_skip_homing_when_blocked_and_end_not_finished(app, mismatch_win):
    w = mismatch_win
    d = _wizard(w)
    try:
        _go_to(app, d, "homing")
        assert d.b_skip.isVisibleTo(d) and d.b_skip.isEnabled()
        d.skip()
        _go_to(app, d, "summary")
        assert not any(s in d.proc.done for s in ("homing", "jog_x", "jog_y"))
        state, n = d.proc.finish_state()
        assert state == "failed" and n >= 1
        assert d.b_next.text() == "Close, not finished"
        assert d.status.text().startswith("Not finished:")
        d.next()
        assert wait(app, lambda: d.proc.state == "done")
        d.refresh()
        assert d.title.text() == (f"Not finished: {n} checks failed" if n != 1 else "Not finished: 1 check failed")
        assert "Finished" not in d.title.text().replace("Not finished", "")
    finally:
        d.done(0)


def test_jog_waits_for_the_estop_test(app, sim_win):
    w = sim_win
    d = _wizard(w)
    try:
        _go_to(app, d, "homing")
        assert d.proc.items["scaling"].status == "pass"
        assert d.b_run.isEnabled(), d.b_run.toolTip()
        d.run()
        assert wait(app, lambda: "homing" in d.proc.done, 30.0), d.proc.error
        _go_to(app, d, "jog_x")
        assert not d.b_run.isEnabled() and "emergency stop" in d.b_run.toolTip().lower()
        x0 = w.ctl.state().x
        d.run()
        wait(app, lambda: False, 0.4)
        assert "jog_x" not in d.proc.done and abs(w.ctl.state().x - x0) < 1e-6
        # the operator tests the e-stop: pressed (the guard refuses), released, alarm cleared
        sim = w.ctl.sim
        d.update_live()
        sim.set_estop(True)
        assert wait(app, lambda: d.update_live()["estop"].status == "pass", 5.0)
        sim.set_estop(False)
        w.ctl.clear_alarms()
        assert wait(app, lambda: (d.update_live(), d.proc.estop_tested())[1], 5.0)
        assert wait(app, lambda: (d.update_live(), d.b_run.isEnabled())[1], 5.0), d.b_run.toolTip()
        d.run()
        assert wait(app, lambda: "jog_x" in d.proc.done, 30.0), d.proc.error
        assert w.ctl.state().x > x0 + 1.0
    finally:
        d.done(0)


def test_skipping_a_blocked_jog_skips_its_direction_question(app, sim_win):
    from mlaser.services.procedures import commissioning as CM
    w = sim_win
    p = CM.CommissioningProcedure(w.ctl, config=w.config)
    p.items["homing"].status = CM.PASS
    p.index = [s.id for s in p.steps].index("jog_x")
    p.state = "waiting"
    assert p.blocked_reason() == "wizard.comm.blocked_estop"
    assert p.can_skip() and p.skip_current()
    assert p.items["jog_x"].status == CM.SKIP
    assert p.step.id == "jog_y"                     # jog_x_dir skipped with it
    p.live_tests["estop"] = "tested"
    assert p.blocked_reason() == ""
