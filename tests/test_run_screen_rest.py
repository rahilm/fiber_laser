"""The Run screen after a job, the calm run bar, the readout and the welcome sheet (UX deep
review finding 5; UI deep review findings 5 and 13).

* after STOP or a finished job the Run screen says so ("Last job stopped at ... in part 3 of 6")
  and offers a NEW job of the parts not finished ("Cut the 4 remaining parts") or from a part
  in the cut order ("Start from part n", the number typed or the part clicked in the live
  view); both select those parts and use the ordinary selected-parts Start, so its pre-flight
  and arming question run unchanged; a changed drawing or a job held at a breakpoint withdraws
  the offer with the reason;
* the run bar on the Run workspace is one row (the state, the progress line), and elsewhere it
  names the target as plain text, not a framed box;
* the process values are label / value pairs in tabular figures that wrap between pairs, the
  readiness notice is a notice with its action, the welcome sheet fits 1280 x 800 unscrolled.
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEventLoop, QPoint, QRect, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox  # noqa: E402

from mlaser.machine import events as EV  # noqa: E402
from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part  # noqa: E402


def rect(x0, y0, w, h, layer=1):
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    return Part([Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)], Craft(layer=layer))


def six_parts(size=40.0, speed=40.0):
    return Document(parts=[rect(10 + 70 * (i % 3), 10 + 60 * (i // 3), size, size) for i in range(6)],
                    layers={1: LayerParams(1, speed=speed, power_pct=60, gas=1, gas_pressure=8.0, cut_height=1.0)})


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch, tmp_path):
    from wiring import dispose, real_window
    for k, sub in (("MLASER_CONFIG_DIR", "cfg"), ("MLASER_APP_DIR", "app"), ("MLASER_DATA_DIR", "data")):
        monkeypatch.setenv(k, str(tmp_path / sub))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window()
    w.resize(1280, 800)
    w.show()
    app.processEvents()
    yield w
    try:
        w.ctl.stop()
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
    return False


def _job(win, done_markers: int, outcome: str = "stopped"):
    """A job of the open document's preview plan, as the controller reports it: started, its
    markers up to ``done_markers`` executed, then stopped (or finished)."""
    bus = win.ctl.events
    plan = win.plan
    ws = win.extension("run")
    ws.parts.context = lambda: (plan, win.document, "six.dxf")
    bus.publish(EV.JobStarted("process", "six"))
    bus.publish(EV.JobProgress("process", 0.4, done_markers))
    if outcome == "finished":
        bus.publish(EV.JobFinished("process", True, "done"))
    else:
        bus.publish(EV.JobFinished("process", False, "stopped"))
    return plan


def _on_run(win, app, size=(1280, 800)):
    win.resize(*size)
    win.commands.run("workspace.run")
    settle(app)
    scr = win.extension("run").screen
    scr.refresh()
    return scr


# ------------------------------------------------------------------ UX 5: after the job
def test_after_a_stop_the_screen_says_where_and_offers_the_rest(win, app, monkeypatch):
    win.set_document(six_parts())
    win.jobs.settle()
    plan = win.plan
    order = []
    for _w, p, _c in plan.job.markers:
        if p not in order:
            order.append(p)
    assert len(order) == 6
    scr = _on_run(win, app)
    assert not scr.last_job.isVisibleTo(scr) and not scr.rest_section.isVisibleTo(scr)   # never started
    # stopped while the 3rd part in the cut order was being cut (its marker index is 2)
    _job(win, 2)
    scr.refresh()
    ws = win.extension("run")
    last = ws.parts.last
    assert last.outcome == "stopped" and last.done == 2 and last.stopped_at == 3
    assert scr.last_job.isVisibleTo(scr)
    assert "part 3 of 6" in scr.last_job.text() and "six.dxf" in scr.last_job.text()
    assert scr.rest_section.isVisibleTo(scr)
    assert scr.buttons["cut_remaining"].text() == "Cut the 4 remaining parts"
    # the live view: the finished parts dashed (not by colour alone), every part numbered
    assert scr.canvas.done == set(order[:2]) and scr.canvas.skip == set()
    assert sorted(scr.canvas.numbers.values()) == [1, 2, 3, 4, 5, 6]
    assert scr.canvas.numbers[order[2]] == 3
    assert scr.from_spin.value() == 3                    # Start from: the part it stopped in
    # Resume and "cut the rest" are told apart: no breakpoint, so Resume is off with its reason
    assert not scr.buttons["resume_here"].isEnabled()
    assert "Resume" in scr.rest_hint.text() and "beginning" in scr.rest_hint.text()

    # "Cut remaining parts" selects the parts not finished and goes through the ordinary
    # selected-parts Start (its pre-flight and arming question): never a hidden switch
    su = win.safety_ui
    starts = []
    monkeypatch.setattr(su, "start", lambda selected_only=False: starts.append(selected_only))
    monkeypatch.setattr(ws, "rest_state", lambda: (last, ""))       # Start's rules aside (no machine)
    ws.cut_rest(None)
    assert starts == [True]
    assert [i for i, p in enumerate(win.document.parts) if p.selected] == sorted(order[2:])
    # "Start from part" by a click on the part in the live view
    canvas = scr.canvas
    s, ox, oy = canvas._transform()
    r = [path for i, _l, path in canvas._paths if i == order[4]][0].boundingRect()
    QTest.mouseClick(canvas, Qt.LeftButton, Qt.NoModifier,
                     QPoint(int(r.center().x() * s + ox), int(oy - r.center().y() * s)))
    assert scr.from_spin.value() == 5
    assert scr.buttons["start_from"].text() == "Start from part 5"
    scr.start_from()
    assert starts == [True, True]
    assert [i for i, p in enumerate(win.document.parts) if p.selected] == sorted(order[4:])
    # the preview of that job: parts 3 and 4 would be left out (dotted)
    scr._rest_refresh()
    assert canvas.skip == set(order[2:4]) and canvas.done == set(order[:2])


def test_the_offer_is_withdrawn_with_its_reason(win, app):
    win.set_document(six_parts())
    win.jobs.settle()
    scr = _on_run(win, app)
    ws = win.extension("run")
    _job(win, 2)
    scr.refresh()
    last, why = ws.rest_state()
    assert last is not None and why == ""
    # the drawing changed since the job (the breakpoint's document key): withdrawn, said why
    win.docs.edit(lambda d: d.parts.append(rect(300, 300, 20, 20)), "add")
    win.jobs.settle()
    scr.refresh()
    last, why = ws.rest_state()
    assert last is not None and "changed" in why
    assert not scr.buttons["cut_remaining"].isEnabled() and not scr.buttons["start_from"].isEnabled()
    assert why in scr.buttons["cut_remaining"].toolTip() and why in scr.last_job.text()
    assert scr.canvas.numbers == {}                       # no done / remaining picture of another drawing
    assert ws.cut_rest(None) is False                     # the command refuses as well
    win.commands.run("edit.undo") if "edit.undo" in win.commands else win.docs.undo()
    win.jobs.settle()
    scr.refresh()
    assert ws.rest_state()[1] == ""                       # the same drawing again
    # a finished job: nothing remains; Start from part is still offered (cut a part again)
    _job(win, 5, "finished")
    scr.refresh()
    assert "finished" in scr.last_job.text() and "all 6 parts" in scr.last_job.text()
    assert not scr.buttons["cut_remaining"].isEnabled()
    assert "every part" in scr.buttons["cut_remaining"].toolTip()
    assert scr.canvas.done == set(range(6))             # the finished sheet reads as done
    # a new start replaces the summary while it runs
    win.ctl.events.publish(EV.JobStarted("process", "again"))
    scr.refresh()
    assert ws.parts.last.outcome == "running"


def test_cut_remaining_on_the_simulator(win, app):
    """End to end: a real job on the simulator, STOP, then "Cut remaining parts": the new job
    has exactly the parts the first one did not finish, through Start's own path."""
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.02, stream_process=False)
    assert ctl.connect_simulator(time_scale=1.0, link_deadline_ms=3000.0)
    win.set_controller(ctl)
    win.set_document(six_parts(size=20.0, speed=40.0))
    win.jobs.settle()
    scr = _on_run(win, app)
    ws = win.extension("run")
    su = win.safety_ui
    calls = []
    real = su.preflight_ok
    su.preflight_ok = lambda *a, **k: calls.append(k.get("selected_only")) or real(*a, **k)
    assert ctl.arm()
    su.start()
    if win.jobs.last_run is not None:
        win.jobs.settle(win.jobs.last_run, timeout=30)
    assert wait(app, lambda: ws.parts.last is not None and ws.parts.last.done >= 2, 60)
    su.stop()
    assert wait(app, lambda: ws.parts.last.outcome == "stopped", 20)
    assert wait(app, lambda: ctl.state().mode == "ready", 20)
    scr.refresh()
    last = ws.parts.last
    left = last.remaining()
    assert 0 < len(left) < 6 and last.stopped_at == last.done + 1
    assert scr.buttons["cut_remaining"].isEnabled(), scr.buttons["cut_remaining"].toolTip()
    before = win.jobs.last_run
    if not ctl.safety().armed:
        assert ctl.arm()
    calls.clear()
    scr.buttons["cut_remaining"].click()
    assert calls and calls[0] is True                      # Start's pre-flight, for the selection
    assert wait(app, lambda: win.jobs.last_run is not before, 20)
    win.jobs.settle(win.jobs.last_run, timeout=30)
    plan = win.jobs.last_run.plan
    uids = {p.uid for p in plan.document.parts}
    assert uids == {win.document.parts[i].uid for i in left}
    assert wait(app, lambda: ws.parts.last is not last and ws.parts.last.order, 20)
    assert sorted(ws.parts.last.order) == sorted(left)
    ctl.stop()
    ctl.disarm("test")


# ------------------------------------------------------------------ UI 5: the run bar
def test_run_bar_is_one_calm_row_on_run_and_names_the_target_as_text(win, app):
    rb = win.safety_ui.run_bar
    win.resize(756, 945)
    win.commands.run("workspace.design")
    settle(app)
    win.safety_ui.refresh()
    # the target: plain caption text after the buttons, not a framed box; no idle progress slab
    assert isinstance(rb.target, QLabel) and rb.target.isVisible()
    assert rb.target.text().startswith("Start:") or "…" in rb.target.text()
    assert not rb.progress.isVisible() and rb.progress.maximumHeight() <= 8
    win.commands.run("workspace.run")
    settle(app)
    win.safety_ui.refresh()
    settle(app)
    # one row on the Run workspace: no second row reserved for hidden buttons (was 124 px)
    assert not rb.strip.wrapped
    assert rb.height() <= rb.pill.height() + 24, (rb.height(), rb.pill.height())
    assert not rb.target.isVisible() and not rb.eta.isVisible()        # the header has them
    assert win.width() == 756


# ------------------------------------------------------------------ UI 13
def test_readout_pairs_in_tabular_figures_wrap_between_pairs(win, app):
    scr = _on_run(win, app, (756, 945))
    v = scr.values
    settle(app)
    assert "Speed" in v.text() and "Power" in v.text()
    tops = set()
    for key, lab in v.fields.items():
        assert lab.font().featureValue(QFont.Tag("tnum")) == 1, key
        assert lab.width() >= lab.fontMetrics().horizontalAdvance(lab.text()), key   # never split
        cap = v.captions[key]
        assert cap.parentWidget() is lab.parentWidget()        # caption and value in one tile
        tops.add(lab.parentWidget().y())
    assert v.flow.count() == len(v.fields) + 1


def test_readiness_notice_is_a_notice_with_its_action(win, app, monkeypatch):
    from mlaser.services.readiness import ReadinessItem
    su = win.safety_ui
    item = ReadinessItem("technology", False, "Technology", "4 layers have no technology (layers 1, 2, 3, 4)",
                         "workspace.process")
    monkeypatch.setattr(su, "readiness", lambda: [item])
    ran = []
    monkeypatch.setattr(su, "run_readiness_action", ran.append)
    scr = _on_run(win, app)
    scr._ready_tick = 0
    scr.refresh()
    settle(app)
    box = scr.ready_notices["technology"]
    b = scr.ready_buttons["technology"]
    assert box.label.text() == item.reason and b.text() != item.reason      # the text is not a button
    assert b.width() < scr.ready_box.width() // 2                         # never full width
    b.click()
    assert ran == ["workspace.process"]


def test_welcome_sheet_fits_the_smallest_supported_screen_unscrolled(win, app):
    d = win.shell.show_welcome()

    class Screen:
        @staticmethod
        def availableGeometry():
            return QRect(0, 0, 1280, 800 - 40)          # 1280 x 800 less a task bar
    d.screen = lambda: Screen
    d.fit()
    settle(app)
    assert d.columns == 2 and d.height() <= 760, d.size()
    assert not d.scroll.verticalScrollBar().isVisible()
    assert d.sections_clipped() == []
    d.close()
