"""Start's pre-flight runs off the GUI thread and on the job the controller checks.

* Perf review 2026-11-09, finding 10: the pre-flight of Start (with the shapely collision
  analysis) ran on the GUI thread, 2.8 s at 2,700 parts, the window and its Stop button frozen
  right at job start.  Now Start returns at once; the check runs in a worker; a Stop meanwhile
  drops the start (no question, never armed).
* UX review 2026-11-09, finding 10: the UI checked the drawing's box, the controller the
  compiled job (leads, kerf): a part at the sheet edge passed the UI's check, the operator
  confirmed "Arm and start", then the controller refused and disarmed.  Now the check before the
  arming question uses the planned job's extent, the refusal comes first, once, with a fix."""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part  # noqa: E402

pytestmark = pytest.mark.xdist_group("ux_audit_safety")


def _pump(app, s=0.1):
    t = time.monotonic() + s
    while time.monotonic() < t:
        app.processEvents()
        time.sleep(0.01)


def _until(app, pred, timeout=8.0) -> bool:
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return pred()


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialogs(monkeypatch):
    seen: list = []

    def box_exec(self, *a):
        seen.append(dict(kind="box", text=self.text()))
        return QMessageBox.Ok

    def dlg_exec(self, *a):
        seen.append(dict(kind=type(self).__name__, title=self.windowTitle(), dialog=self))
        return 0
    monkeypatch.setattr(QMessageBox, "exec", box_exec)
    monkeypatch.setattr(QDialog, "exec", dlg_exec)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: seen.append(dict(kind="static")) or QMessageBox.Ok))
    return seen


@pytest.fixture
def sim_win(app, monkeypatch, tmp_path):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import dispose, real_window
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.02)
    w = real_window(ctl)
    w.resize(1280, 720)
    w.show()
    assert ctl.connect_simulator(time_scale=1.0)
    _until(app, lambda: ctl.state().connected)
    yield w
    dispose(w)


def _edge_doc(kerf: bool = True):
    """A 20 mm square at the travel's corner: its box is inside the travel, the compiled job with
    an outside kerf offset of 0.2 mm is not (Y -0.19)."""
    pts = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    cr = Craft(layer=1, kerf_comp=1, kerf_width=0.2) if kerf else Craft(layer=1)
    return Document(parts=[Part([Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)], cr)],
                    layers={1: LayerParams(1, speed=100, power_pct=50)})


def _armed(ctl) -> bool:
    return bool(ctl.safety().armed)


def test_the_compiled_extent_refuses_before_the_arming_question(sim_win, app, dialogs):
    w, ctl = sim_win, sim_win.ctl
    w.set_document(_edge_doc())
    w._test_no_dialogs = False
    for attempt in range(2):                        # the second Start: again one dialog, not three
        dialogs.clear()
        w.safety_ui.start()
        assert w.safety_ui.wait_preflight(30)
        _pump(app, 0.3)
        assert not _armed(ctl), "armed for a start the controller refuses"
        assert not [d for d in dialogs if d["kind"] == "box"], "the arming question came first"
        pf = [d for d in dialogs if d["kind"] == "PreflightDialog"]
        assert len(pf) == 1, (attempt, [d["kind"] for d in dialogs])
        d = pf[0]["dialog"]
        assert "out_of_travel" in d.report.codes()
        assert d.report.extent is not None and d.report.extent[1] < 0 <= _edge_doc().bbox()[1]
        fix = d.fixes.get("out_of_travel")
        assert fix is not None and fix.text() == "Show in Design"
        assert ctl.state().mode == "ready" and not ctl.job_active()


def test_the_check_uses_the_same_extent_as_the_controller(sim_win, app):
    """The report before the arming question is the controller's own check of the planned job."""
    w, ctl = sim_win, sim_win.ctl
    w.set_document(_edge_doc())
    w.safety_ui.start()                              # no dialogs: a refusal just returns
    assert w.safety_ui.wait_preflight(30)
    rep = w.safety_ui.last_report
    plan = w.jobs.current or w.jobs.settle()
    assert plan is not None
    mine = ctl.preflight("start", plan.source or plan.document, False, job=plan.job, emits=True)
    assert rep is not None and rep.extent == mine.extent and "out_of_travel" in rep.codes()


def test_a_move_target_out_of_travel_has_no_design_fix(app):
    from mlaser.machine.preflight import PreflightReport, ERROR
    from mlaser.ui.safety_ui import PreflightDialog
    rep = PreflightReport("move")
    rep.add("out_of_travel", ERROR, "target out of travel")
    assert "out_of_travel" not in PreflightDialog(rep).fixes


def test_start_returns_at_once_and_stop_stays_live(sim_win, app, dialogs, monkeypatch):
    """A slow pre-flight (a large drawing) runs in a worker: Start returns at once, the event
    loop runs (the Stop button works), and a Stop meanwhile drops the start: no arming question,
    not armed, no job."""
    w, ctl = sim_win, sim_win.ctl
    w.set_document(_edge_doc(kerf=False))
    # the new document's own relayouts (docks, panels) are over before the event loop is
    # timed: what is measured below is the pre-flight alone
    from test_responsive_layout import at_rest
    at_rest(w, app)
    w._test_no_dialogs = False
    from mlaser.ui.workspaces import cutting_strategy as CS
    entered, release = threading.Event(), threading.Event()
    real = CS.augment_preflight

    def slow(*a, **k):
        if threading.current_thread() is threading.main_thread():
            pytest.fail("the cutting-strategy check ran on the GUI thread")
        entered.set()
        release.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(CS, "augment_preflight", slow)
    t0 = time.monotonic()
    w.safety_ui.start()
    assert time.monotonic() - t0 < 1.0, "Start blocked the GUI thread"
    assert _until(app, entered.is_set, 30)
    # the event loop keeps turning while the pre-flight is in flight: 20 turns, a 10 ms timer
    # due at each (every turn sleeps 10 ms), no turn held up.  Counted per turn, not per
    # wall-clock time: a turn is 40+ ms on a macOS runner (its sleep overshoots), not 10 ms.
    ticks = []
    from PySide6.QtCore import Qt, QTimer
    tm = QTimer()
    tm.setTimerType(Qt.PreciseTimer)
    tm.timeout.connect(lambda: ticks.append(time.monotonic()))
    tm.start(10)
    turns, longest = 0, 0.0
    while turns < 20:
        t1 = time.monotonic()
        app.processEvents()
        longest = max(longest, time.monotonic() - t1)
        turns += 1
        time.sleep(0.01)
    assert entered.is_set() and not release.is_set()             # still in flight
    assert longest < 0.3, f"the event loop was frozen during the pre-flight ({longest:.3f} s)"
    assert len(ticks) >= turns - 1, f"the event loop was frozen during the pre-flight {len(ticks), turns}"
    w.safety_ui.stop()                               # the on-screen Stop, while checking
    release.set()
    assert w.safety_ui.wait_preflight(30)
    _pump(app, 0.3)
    tm.stop()
    assert not [d for d in dialogs if d["kind"] == "box"], "asked to arm after the Stop"
    assert not _armed(ctl) and not ctl.job_active() and ctl.state().mode == "ready"


def test_start_without_a_stop_still_asks_and_runs(sim_win, app, dialogs):
    """The order is kept: pre-flight, then the one arming question, then the start (armed)."""
    w, ctl = sim_win, sim_win.ctl
    w.set_document(_edge_doc(kerf=False))
    w._test_no_dialogs = False
    w.safety_ui.start()
    assert w.safety_ui.wait_preflight(30)
    assert _until(app, lambda: ctl.job_active() or ctl.state().mode == "process", 20)
    assert [d["kind"] for d in dialogs] == ["box"]
    assert _armed(ctl)
    ctl.stop(wait=True)
