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

from safety_helpers import loop_heartbeat  # noqa: E402

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
    # the event loop keeps turning while the pre-flight is in flight: no turn held up, and no
    # stretch without a timer beat longer than two consecutive turns (safety_helpers)
    longest, gap, two_turns, beats = loop_heartbeat(app)
    assert entered.is_set() and not release.is_set()             # still in flight
    assert longest < 0.3, f"the event loop was frozen during the pre-flight ({longest:.3f} s)"
    assert gap <= two_turns, (f"the event loop was frozen during the pre-flight: no beat for "
                              f"{gap * 1000:.0f} ms, two turns {two_turns * 1000:.0f} ms ({beats} beats)")
    w.safety_ui.stop()                               # the on-screen Stop, while checking
    release.set()
    assert w.safety_ui.wait_preflight(30)
    _pump(app, 0.3)
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


def test_a_press_while_the_head_still_moves_is_checked_again_from_its_rest(sim_win, app, dialogs, monkeypatch):
    """Start pressed before the polled head position is the position of rest (right after a
    Stop: the axes run out): the job was planned from where the head no longer is, the check
    reported it translated by the difference ("outside the travel" for a job that fits; macOS
    CI: "Cut remaining parts" 11.5 mm off, refused).  Now the worker sees that the head moved,
    waits for it to rest and the check begins again from there: one question, the job runs."""
    import dataclasses

    from mlaser.services import jobs as J
    w, ctl = sim_win, sim_win.ctl
    w.set_document(_edge_doc(kerf=False))
    w._test_no_dialogs = False
    real = J.build_request
    stale = []

    def build(*a, **k):
        req = real(*a, **k)
        if not stale:                               # the press: the poll's head, 11.5 mm behind
            stale.append(req.start)
            req = dataclasses.replace(req, start=(req.start[0], req.start[1] + 11.5))
        return req
    monkeypatch.setattr(J, "build_request", build)
    w.safety_ui.start()
    assert w.safety_ui.wait_preflight(30)           # waits for the second check too
    assert stale, "the stale request was never built"
    rep = w.safety_ui.last_report
    assert rep is None or "out_of_travel" not in rep.codes(), rep.summary()
    assert _until(app, lambda: ctl.job_active() or ctl.state().mode == "process", 20)
    assert [d["kind"] for d in dialogs] == ["box"]          # the one arming question, no refusal
    assert _armed(ctl)
    ctl.stop(wait=True)


def test_a_head_that_never_rests_ends_in_a_report_not_in_a_loop(sim_win, app, dialogs, monkeypatch):
    """The check begins again a bounded number of times; then the report stands (and the
    controller's start gate has the last word anyway)."""
    import dataclasses

    from mlaser.services import jobs as J
    from mlaser.ui.safety_ui import SafetyUI
    w, ctl = sim_win, sim_win.ctl
    w.set_document(_edge_doc(kerf=False))
    w._test_no_dialogs = False
    real = J.build_request
    built = []

    def build(*a, **k):
        req = real(*a, **k)
        built.append(req.start)
        return dataclasses.replace(req, start=(req.start[0], req.start[1] + 11.5))
    monkeypatch.setattr(J, "build_request", build)
    monkeypatch.setattr(SafetyUI, "_head_settled", staticmethod(lambda ctl, alive, timeout_s=2.0: None))
    w.safety_ui.start()
    assert w.safety_ui.wait_preflight(30)
    _pump(app, 0.3)
    assert len(built) == SafetyUI.PREFLIGHT_HEAD_RETRIES + 1, built
    assert not _armed(ctl) and not ctl.job_active(), "a job planned from another place was started"
    assert [d["kind"] for d in dialogs] == ["PreflightDialog"]      # refused once, before any arming


def test_the_check_reads_the_head_from_the_card_not_from_the_last_poll(sim_win, app, dialogs, monkeypatch):
    """The other face of the same press (macOS CI): the poll's position was behind at the press
    and still behind when the check ran, so the check passed, the operator armed, and the
    controller's start gate (which reads the card) refused: "the head is at (150.00, 67.52) mm
    but the job starts at (150.00, 69.94) mm".  The check's worker reads the card itself."""
    from mlaser.services import jobs as J
    w, ctl = sim_win, sim_win.ctl
    w.set_document(_edge_doc(kerf=False))
    w._test_no_dialogs = False
    assert ctl.wait_commands(10)
    real_state, real_fresh = ctl.state, ctl._fresh_head
    behind, built = [True], []

    def state():                                    # the last poll: 2.42 mm behind the card
        s = real_state()
        if behind[0]:
            s.y += 2.42
        return s

    def fresh():                                    # a card read stores the state's position
        behind[0] = False
        return real_fresh()
    real_build = J.build_request
    monkeypatch.setattr(J, "build_request", lambda *a, **k: built.append(1) or real_build(*a, **k))
    monkeypatch.setattr(ctl, "state", state)
    monkeypatch.setattr(ctl, "_fresh_head", fresh)
    w.safety_ui.start()
    assert w.safety_ui.wait_preflight(30)
    assert _until(app, lambda: ctl.job_active() or real_state().mode == "process", 20), ctl.last_preflight
    assert [d["kind"] for d in dialogs] == ["box"]          # armed once, and the job started
    assert len(built) == 2                                  # planned again, from the card's position
    ctl.stop(wait=True)


def test_a_fresh_head_is_never_read_on_the_gui_thread(sim_win, app, monkeypatch):
    """``head_position(fresh=True)`` is a card read (up to the link timeout with a silent
    card): on the GUI thread it returns the polled position, off it the card's."""
    ctl = sim_win.ctl
    assert ctl.wait_commands(10)
    reads = []
    real = ctl._fresh_head
    monkeypatch.setattr(ctl, "_fresh_head", lambda: reads.append(threading.current_thread().name) or real())
    here = ctl.head_position(fresh=True)
    assert not reads and here == pytest.approx((ctl.state().x, ctl.state().y), abs=1e-9)
    got = []
    t = threading.Thread(target=lambda: got.append(ctl.head_position(fresh=True)), name="pf-worker")
    t.start()
    t.join(10)
    assert reads == ["pf-worker"] and got and got[0] == pytest.approx(here, abs=0.01)
