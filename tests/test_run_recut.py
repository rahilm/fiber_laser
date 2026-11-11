"""Run screen: "Cut this contour again" after a job (UX deep review 2026-11-09, finding 5, the
single failed pierce).

After a stopped or finished job the operator picks one contour in the live view (a click, Left /
Right with the view focused) and cuts it again: from the contour's menu (right-click, a long
press, the context-menu key or Shift+F10) or the button "Cut contour c of part n again".  It is
the smallest variant of "Cut remaining parts":

* offered only when "Cut remaining parts" is (no job running or held, the machine ready, the
  same drawing), else disabled with the reason;
* the job is planned on the whole drawing and keeps only that contour (its lead-in, pierce and
  technology as in the full job: a hole keeps its lead inside the hole);
* it starts through Start's own path (``SafetyUI.start``: the pre-flight, the arming question,
  the controller's start gate) and the dialog and the question name it ("1 contour of part n");
* afterwards the contour's outcome is kept with the last job; the rest of it is unchanged.
"""
from __future__ import annotations

import math
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QContextMenuEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from mlaser.machine import events as EV  # noqa: E402
from mlaser.model import Contour, Craft, Document, LayerParams, LeadParams, Line, Part  # noqa: E402

pytestmark = pytest.mark.xdist_group("run_recut")


def square(x0, y0, w):
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + w), (x0, y0 + w)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def sheet(speed=60.0, far=False):
    """Three parts; part 0 a plate with a hole (the failed pierce), every contour with a line
    lead-in.  ``far``: a fourth part lies partly outside the travel (X < 0)."""
    cr = Craft(layer=1, lead_in=LeadParams(kind=1, length=3.0))
    parts = [Part([square(20, 20, 40), square(32, 32, 16)], cr),
             Part([square(80, 20, 20)], Craft(layer=1, lead_in=LeadParams(kind=1, length=3.0))),
             Part([square(120, 20, 20)], Craft(layer=1, lead_in=LeadParams(kind=1, length=3.0)))]
    if far:
        parts.append(Part([square(-30, 80, 20)], Craft(layer=1, lead_in=LeadParams(kind=1, length=3.0))))
    return Document(parts=parts, layers={1: LayerParams(1, speed=speed, power_pct=60, gas=1, gas_pressure=8.0,
                                                        cut_height=1.0)})


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def seen(monkeypatch):
    """Every question and dialog; the arming question is answered with ``seen.answer``."""
    out: list = []

    class Seen(list):
        answer = QMessageBox.Cancel
        go_anyway = 0                  # the pre-flight dialog: 0 Cancel, 1 "Start anyway"
    out = Seen()

    def box_exec(self, *a):
        out.append(dict(kind="box", text=self.text(), informative=self.informativeText()))
        return out.answer

    def dlg_exec(self, *a):
        out.append(dict(kind=type(self).__name__, dialog=self))
        return out.go_anyway if type(self).__name__ == "PreflightDialog" else 0
    monkeypatch.setattr(QMessageBox, "exec", box_exec)
    monkeypatch.setattr(QDialog, "exec", dlg_exec)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    return out


@pytest.fixture
def win(app, monkeypatch, tmp_path, seen):
    from wiring import dispose, real_window
    for k, sub in (("MLASER_CONFIG_DIR", "cfg"), ("MLASER_APP_DIR", "app"), ("MLASER_DATA_DIR", "data")):
        monkeypatch.setenv(k, str(tmp_path / sub))
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.02, stream_process=False)
    w = real_window(ctl)
    w.resize(1280, 800)
    w.show()
    assert ctl.connect_simulator(time_scale=1.0, link_deadline_ms=3000.0)
    assert wait(app, lambda: ctl.state().connected and ctl.state().mode == "ready", 20)
    yield w
    try:
        w.ctl.stop()
        w.ctl.disarm("test")
    except Exception:  # noqa: BLE001
        pass
    dispose(w)


def settle(app, sec: float = 0.3) -> None:
    loop = QEventLoop()
    QTimer.singleShot(int(sec * 1000), loop.quit)
    loop.exec()
    for _ in range(5):
        app.processEvents()


def wait(app, pred, timeout=30.0) -> bool:
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


def on_run(win, app, doc):
    win.set_document(doc)
    win.jobs.settle()
    win.commands.run("workspace.run")
    settle(app)
    scr = win.extension("run").screen
    scr.refresh()
    return scr


def stopped_job(win, done_markers: int = 1):
    """The full job of the open document as the controller reports it: started, its markers up
    to ``done_markers`` executed, stopped.  The tracker's context is the workspace's own again
    afterwards (the next start is judged as in the application)."""
    ws = win.extension("run")
    plan = win.plan
    ws.parts.context = lambda: (plan, win.document, "sheet.dxf")
    bus = win.ctl.events
    bus.publish(EV.JobStarted("process", "sheet"))
    bus.publish(EV.JobProgress("process", 0.4, done_markers))
    bus.publish(EV.JobFinished("process", False, "stopped"))
    ws.parts.context = ws._job_context
    return plan


def view_point(canvas, x, y) -> QPoint:
    s, ox, oy = canvas._transform()
    return QPoint(int(x * s + ox), int(oy - y * s))


def click_hole(scr):
    """A click inside part 0's hole picks the hole (the smallest contour there), not the plate."""
    QTest.mouseClick(scr.canvas, Qt.LeftButton, Qt.NoModifier, view_point(scr.canvas, 40, 40))
    scr.refresh()


def contour_items(toolpath, part, contour):
    """The items of one contour (its marker to the next), without the rapids and layer changes."""
    out, on = [], False
    for it in toolpath:
        name = type(it).__name__
        if name == "Marker":
            on = (it.part, it.contour) == (part, contour)
            continue
        if on and name not in ("Rapid", "LayerChange"):
            out.append(it)
    return out


def cut_length(toolpath) -> float:
    return sum(math.dist(it.start, it.end) for it in toolpath if type(it).__name__ == "CutLine")


# ------------------------------------------------------------------ when it is offered
def test_offered_only_after_a_job_and_never_while_one_runs_or_is_held(win, app, seen):
    ctl = win.ctl
    scr = on_run(win, app, sheet(speed=20.0))
    ws = win.extension("run")
    b = scr.buttons["recut"]
    # nothing was cut yet: disabled, with the reason
    key, why = scr.recut_target()
    assert key is None and why == "Nothing was cut yet"
    assert not b.isEnabled() and "Nothing was cut yet" in b.toolTip()
    assert ws.cut_contour(0, 1) is False                          # the command refuses as well

    # a real job, running: disabled (Start's own reason)
    assert ctl.arm()
    win.safety_ui.start()
    assert win.safety_ui.wait_preflight(30)
    assert wait(app, lambda: ctl.job_active() and ctl.state().mode == "process", 30)
    scr.refresh()
    assert not b.isEnabled() and b.toolTip()
    assert scr.recut_target()[0] is None
    # held: disabled, the offer says to resume or stop first
    win.safety_ui.hold()
    assert wait(app, lambda: ctl.state().mode == "pause" or ctl.breakpoint is not None, 20)
    scr.refresh()
    assert not b.isEnabled()
    assert scr.recut_target()[0] is None
    assert ws.cut_contour(0, 1) is False
    # stopped: offered once a contour of the job is picked
    win.safety_ui.stop()
    assert wait(app, lambda: ws.parts.last is not None and ws.parts.last.outcome == "stopped", 20)
    assert wait(app, lambda: ctl.state().mode == "ready" and not ctl.job_active(), 20)
    scr.refresh()
    assert not b.isEnabled() and "Pick a contour" in b.toolTip()
    click_hole(scr)
    assert scr.canvas.picked == (0, 1)
    n = ws.parts.last.number_of(0)
    assert b.text() == f"Cut contour 2 of part {n} again"
    assert scr.recut_target() == ((0, 1), ""), scr.recut_target()
    assert b.isEnabled(), b.toolTip()
    ctl.disarm("test")


def test_the_menu_key_right_click_and_arrow_keys(win, app):
    scr = on_run(win, app, sheet())
    stopped_job(win)
    scr.refresh()
    canvas = scr.canvas
    # keyboard: Left / Right pick the contours of the job in its cut order
    canvas.setFocus()
    QTest.keyClick(canvas, Qt.Key_Right)
    first = canvas.picked
    assert first is not None and first[0] in canvas.numbers
    QTest.keyClick(canvas, Qt.Key_Right)
    assert canvas.picked != first
    QTest.keyClick(canvas, Qt.Key_Left)
    assert canvas.picked == first
    # the context-menu key: the picked contour's menu
    QApplication.sendEvent(canvas, QContextMenuEvent(QContextMenuEvent.Keyboard, QPoint(1, 1), canvas.mapToGlobal(QPoint(1, 1))))
    menu = scr._menu
    act = menu.actions()[0]
    assert act.objectName() == "run_recut_action" and act.text().startswith("Cut contour")
    menu.close()
    # a right-click on the hole picks it and opens the menu there
    p = view_point(canvas, 40, 40)
    QApplication.sendEvent(canvas, QContextMenuEvent(QContextMenuEvent.Mouse, p, canvas.mapToGlobal(p)))
    assert canvas.picked == (0, 1) and scr._menu is not menu
    scr._menu.close()


# ------------------------------------------------------------------ Start's own path
def test_the_arming_question_names_it_and_cancel_starts_nothing(win, app, seen, monkeypatch):
    ctl = win.ctl
    scr = on_run(win, app, sheet())
    ws = win.extension("run")
    su = win.safety_ui
    stopped_job(win)
    scr.refresh()
    win._test_no_dialogs = False                # the real questions (answered by ``seen``)
    calls = []
    real_start, real_pf = su.start, su.preflight_ok
    monkeypatch.setattr(su, "start", lambda *a, **k: calls.append(("start", k)) or real_start(*a, **k))
    monkeypatch.setattr(su, "preflight_ok", lambda *a, **k: calls.append(("preflight", k)) or real_pf(*a, **k))
    before = win.jobs.last_run
    assert not ctl.safety().armed
    # right-click on the hole > Cut this contour again
    p = view_point(scr.canvas, 40, 40)
    QApplication.sendEvent(scr.canvas, QContextMenuEvent(QContextMenuEvent.Mouse, p, scr.canvas.mapToGlobal(p)))
    act = scr._menu.actions()[0]
    assert act.isEnabled(), act.toolTip()
    seen.answer = QMessageBox.Cancel
    seen.go_anyway = 1                 # past the pre-flight's warnings (the lead-in check), if any
    act.trigger()
    scr._menu.close()
    assert su.wait_preflight(30)
    settle(app, 0.3)
    # SafetyUI.start with the contour; its pre-flight ran on that job (checked off the GUI thread)
    assert calls[0] == ("start", {"contours": ((0, 1),), "scope": calls[0][1]["scope"]})
    n = ws.parts.last.number_of(0)
    assert calls[0][1]["scope"] == f"1 contour of part {n} (contour 2)"
    assert [c for c in calls if c[0] == "preflight" and c[1].get("checked") is not None]
    # the arming question, naming what is cut; Cancel: nothing armed, nothing started
    boxes = [d for d in seen if d["kind"] == "box"]
    assert len(boxes) == 1 and "not armed" in boxes[0]["text"], seen
    assert f"This start cuts: 1 contour of part {n}" in boxes[0]["text"]
    for d in seen:                     # a pre-flight dialog (warnings) names it as well
        if d["kind"] == "PreflightDialog":
            assert d["dialog"].scope.text() == f"This start cuts: 1 contour of part {n} (contour 2)"
    assert not ctl.safety().armed
    assert not ctl.job_active() and ctl.state().mode == "ready"
    assert win.jobs.last_run is before
    assert ws.parts.last.recut == {}                              # nothing was cut again


def test_the_job_is_that_contour_as_planned_and_ends_disarmed(win, app):
    ctl = win.ctl
    scr = on_run(win, app, sheet())
    ws = win.extension("run")
    full = stopped_job(win)
    scr.refresh()
    last = ws.parts.last
    kept = (last.outcome, last.done, last.order, last.stopped_at)
    click_hole(scr)
    assert ctl.arm()                            # armed: Start asks nothing more (as any Start)
    before = win.jobs.last_run
    scr.buttons["recut"].click()
    assert win.safety_ui.wait_preflight(30)
    assert wait(app, lambda: win.jobs.last_run is not before, 20)
    win.jobs.settle(win.jobs.last_run, timeout=30)
    plan = win.jobs.last_run.plan
    # only that contour: one marker, the hole of part 0
    assert [(p, c) for _w, p, c in plan.job.markers] == [(0, 1)]
    assert len(full.job.markers) == 4
    assert cut_length(plan.toolpath) < cut_length(full.toolpath) / 3
    # exactly as planned in the full job: the same pierce, lead-in and cut (the lead inside the
    # hole, as the whole drawing's containment puts it), up to the job placement
    mine, theirs = contour_items(plan.toolpath, 0, 1), contour_items(full.toolpath, 0, 1)
    assert [type(i).__name__ for i in mine] == [type(i).__name__ for i in theirs]
    pm = [i for i in mine if type(i).__name__ == "Pierce"][0].point
    pt = [i for i in theirs if type(i).__name__ == "Pierce"][0].point
    dx, dy = pm[0] - pt[0], pm[1] - pt[1]
    for a, b in zip(mine, theirs):
        if type(a).__name__ == "CutLine":
            assert math.dist(a.start, (b.start[0] + dx, b.start[1] + dy)) < 1e-6
            assert math.dist(a.end, (b.end[0] + dx, b.end[1] + dy)) < 1e-6
    assert 32 <= pt[0] <= 48 and 32 <= pt[1] <= 48, pt             # the pierce at the hole, not outside
    # it runs and ends: the contour's state is kept with the last job, the rest unchanged
    assert wait(app, lambda: ws.parts.last.recut.get((0, 1)) == "finished", 60)
    assert ws.parts.last is last
    assert (last.outcome, last.done, last.order, last.stopped_at) == kept
    assert wait(app, lambda: not ctl.job_active() and ctl.state().mode == "ready", 20)
    assert not ctl.safety().armed                # disarmed after the job, as after any job
    scr.refresh()
    assert "Contour 2 of part" in scr.last_job.text() and "cut again" in scr.last_job.text()


def test_a_contour_outside_the_travel_is_refused_as_any_start(win, app, seen):
    ctl = win.ctl
    scr = on_run(win, app, sheet(far=True))
    ws = win.extension("run")
    stopped_job(win)
    scr.refresh()
    win._test_no_dialogs = False
    seen.answer = QMessageBox.Ok                 # would arm, if it were asked
    canvas = scr.canvas
    QTest.mouseClick(canvas, Qt.LeftButton, Qt.NoModifier, view_point(canvas, -20, 90))
    scr.refresh()
    assert canvas.picked == (3, 0)
    assert scr.buttons["recut"].isEnabled(), scr.buttons["recut"].toolTip()
    before = win.jobs.last_run
    scr.buttons["recut"].click()
    assert win.safety_ui.wait_preflight(30)
    settle(app, 0.3)
    pf = [d for d in seen if d["kind"] == "PreflightDialog"]
    assert len(pf) == 1, [d["kind"] for d in seen]
    d = pf[0]["dialog"]
    assert "out_of_travel" in d.report.codes()
    n = ws.parts.last.number_of(3)
    assert d.scope.text() == f"This start cuts: 1 contour of part {n} (contour 1)"
    assert not [x for x in seen if x["kind"] == "box"]          # refused before the arming question
    assert not ctl.safety().armed and not ctl.job_active()
    assert win.jobs.last_run is before


def test_stop_during_it_is_the_stop_of_any_job(win, app):
    ctl = win.ctl
    scr = on_run(win, app, sheet(speed=4.0))
    ws = win.extension("run")
    stopped_job(win)
    scr.refresh()
    last = ws.parts.last
    click_hole(scr)
    assert ctl.arm()
    scr.buttons["recut"].click()
    assert win.safety_ui.wait_preflight(30)
    assert wait(app, lambda: ctl.job_active() and ctl.state().mode == "process", 30)
    assert ws.parts.last.recut.get((0, 1)) == "running"
    scr.refresh()
    assert not scr.buttons["recut"].isEnabled()                 # not while it runs
    win.safety_ui.stop()
    assert wait(app, lambda: not ctl.job_active() and ctl.state().mode == "ready", 20)
    assert wait(app, lambda: ws.parts.last.recut.get((0, 1)) == "stopped", 20)
    assert ws.parts.last is last and last.outcome == "stopped"
    assert not ctl.safety().armed
    assert ctl.breakpoint is None
    scr.refresh()
    assert "again was stopped" in scr.last_job.text()
