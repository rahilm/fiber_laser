"""The questions that start motion (UX review 2026-11-09, finding 2).

"Home the machine now?" after Connect used to have OK as the default (one Enter moved the axes)
and a "Don't ask again" box that made later connects home with no question.  Now: a title, the
verb buttons "Home now" / "Not now" with "Not now" the default and the Escape answer, one line
saying which axes move, and no "don't ask again" (an answer stored by an earlier version is
ignored).  The same for "Go to origin".  Driven on the real MainWindow against the simulator."""
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


def _pump(app, pred=None, timeout=5.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        app.processEvents()
        if pred is not None and pred():
            return True
        time.sleep(0.01)
    return pred is None


class Boxes:
    """Patched QMessageBox.exec: records each box and answers it like a key press: Enter
    (the default button), or the verb when ``answer == "yes"``."""

    def __init__(self, answer: str = "enter") -> None:
        self.seen: list[dict] = []
        self.answer = answer

    def __call__(self, box, *a):
        from PySide6.QtWidgets import QMessageBox
        d = box.defaultButton()
        self.seen.append({"title": box.windowTitle(), "text": box.text(), "info": box.informativeText(),
                          "buttons": [b.text() for b in box.buttons()],
                          "default": d.text() if d is not None else None,
                          "escape": box.escapeButton().text() if box.escapeButton() is not None else None,
                          "checkbox": box.checkBox() is not None})
        if self.answer == "yes":
            from mlaser.ui.verb_question import answer_with
            return answer_with(box, True)
        if d is not None:
            d.click()                       # Enter presses the default button
        return QMessageBox.Cancel


@pytest.fixture
def win(app, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox
    from wiring import dispose, real_window

    from mlaser.controller.ui_adapter import UiController
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    boxes = Boxes()
    monkeypatch.setattr(QMessageBox, "exec", lambda box, *a: boxes(box, *a))
    ctl = UiController(poll_s=0.02)
    w = real_window(ctl)
    w.control.confirm_fn = None              # the real questions (real_window answers "no")
    w.boxes = boxes
    yield w
    ctl.disconnect()
    dispose(w)


def _connect(app, w):
    n = len(w.boxes.seen)
    assert w.ctl.connect_simulator(time_scale=1.0)
    assert _pump(app, lambda: len(w.boxes.seen) > n, 5.0), "no homing question after Connect"
    _pump(app, timeout=0.3)


def test_enter_on_the_homing_question_does_not_move(app, win):
    w = win
    _connect(app, w)
    q = w.boxes.seen[-1]
    # macOS shows no window title on an alert (Qt drops it there): the bold question stands alone
    assert q["title"] == ("" if sys.platform == "darwin" else "Home the machine")
    assert q["buttons"] and set(q["buttons"]) == {"Home now", "Not now"}
    assert q["default"] == "Not now" and q["escape"] == "Not now"
    assert not q["checkbox"], "no \"don't ask again\" on a question that moves the axes"
    assert "X and Y axes" in q["info"] and "table is clear" in q["info"] and "head is up" in q["info"]
    _pump(app, timeout=0.5)
    st = w.ctl.state()
    assert st.mode != "origin" and not st.homed
    assert not any(a.busy for a in w.ctl.sim.state.axes), "Enter moved the axes"


def test_reconnect_always_asks_even_with_an_old_stored_answer(app, win):
    w = win
    # what "Don't ask again" + OK stored in an earlier version (session dict and settings)
    from mlaser.ui import safety_ui
    w.control.settings = {"dont_ask.mp119": True}
    safety_ui.DONT_ASK["dont_ask.mp119"] = True
    try:
        for _ in range(2):
            n = len(w.boxes.seen)
            _connect(app, w)
            assert len(w.boxes.seen) == n + 1
            assert w.ctl.state().mode != "origin"
            w.ctl.disconnect()
            _pump(app, lambda: not w.ctl.state().connected, 5.0)
    finally:
        safety_ui.DONT_ASK.pop("dont_ask.mp119", None)


def test_home_now_homes_with_one_question(app, win):
    w = win
    w.boxes.answer = "yes"
    _connect(app, w)
    assert len(w.boxes.seen) == 1, "mp119 and the mf156 that follows are one question"
    assert _pump(app, lambda: w.ctl.state().homed or w.ctl.state().mode == "origin", 10.0)


def test_go_to_origin_question_defaults_to_not_now(app, win):
    w = win
    w.control.confirm_fn = lambda key: False          # no start-up question here
    assert w.ctl.connect_simulator(time_scale=1.0)
    w.ctl.sim_home_now = None
    for ax in w.ctl.sim.state.axes:
        ax.homed = True
    _pump(app, lambda: w.ctl.state().homed, 3.0)
    w.ctl.sim.state.axes[0].pos = 50 * w.ctl.sim.ppm
    _pump(app, timeout=0.3)
    x0 = w.ctl.state().x
    w.control.confirm_fn = None
    w.control._return_zero()                      # the jog pad's centre cell
    _pump(app, timeout=0.6)
    q = w.boxes.seen[-1]
    assert q["title"] == ("" if sys.platform == "darwin" else "Go to origin")       # as above
    assert q["default"] == "Not now" and not q["checkbox"]
    assert set(q["buttons"]) == {"Go to origin", "Not now"}
    assert abs(w.ctl.state().x - x0) < 1e-6
    assert w.control.DONT_ASK_KEYS == ()
