"""Cost of a window-resize step on the Process page with a drawing open (perf review 2026-11
finding 14).

The responsive engine measures the same sizes many times in one layout pass: the technology
form's sections at three column widths, for every folding step, from the form inside each
section and from the panel around it.  A measuring pass (:func:`mlaser.ui.responsive.
measure_pass`) computes each of them once: before, a resize step of the Process page with a
300-part drawing open called ``_hidden`` about 88,000 times and took 260 ms (56 ms without a
drawing); with the pass, about 23,000 times and 130 ms.

* the decisive check is structural: within one layout pass of a columns panel no responsive
  form is measured twice at the same width and arrangement;
* the timing bound is relative to the same page without a drawing, measured in the same run,
  with clear headroom (2.3 times now, 4.6 times before)."""

from __future__ import annotations

import os
import statistics
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest  # noqa: E402

pytest.importorskip("PySide6")

from test_large_drawing import big_document, runner_budget  # noqa: E402

pytestmark = pytest.mark.timing


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app):
    from wiring import dispose, real_window
    w = real_window(user_mode="engineer")
    w.resize(1400, 900)
    w.show()
    _pump(app, 0.5)
    yield w
    dispose(w)


def _pump(app, sec: float) -> None:
    t = time.monotonic()
    while time.monotonic() - t < sec:
        app.processEvents()
        time.sleep(0.002)


def _steps(app, w, n: int = 12) -> list[float]:
    """The cost of ``n`` live-resize steps of 16 px (the resize and what the event loop then
    does), in seconds."""
    out = []
    for i in range(n):
        t = time.perf_counter()
        w.resize(1400 + 16 * (i + 1), 900)
        for _ in range(4):
            app.processEvents()
        out.append(time.perf_counter() - t)
    w.resize(1400, 900)
    _pump(app, 0.3)
    return out


def _plan_delivered(app, w, timeout_s: float = 120.0) -> None:
    """Pump until the document's plan (toolpath, then the estimate: seconds of work in the plan
    thread on a slow runner, competing with the GUI thread for the GIL) is delivered: a resize
    step is measured on its own, not against the plan."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        tk = w.jobs.preview
        if not w.jobs.pending and (tk is None or tk.done_event.is_set()):
            break
        app.processEvents()
        time.sleep(0.01)
    tk = w.jobs.preview
    assert not w.jobs.pending and (tk is None or tk.done_event.is_set()), "the drawing's plan did not finish"


def _open_process(app, w, n_parts: int) -> None:
    from mlaser.model import Document
    w.set_document(big_document(n_parts) if n_parts else Document())
    _pump(app, 1.5)
    _plan_delivered(app, w)
    w.shell.host.activate("process")
    _pump(app, 0.5)
    _plan_delivered(app, w)
    _pump(app, 0.3)                       # the delivered plan's estimate card and preview drawn


def test_a_layout_pass_measures_each_form_once_per_width(win, app, monkeypatch):
    """Inside one measuring pass (until something in it changes the widgets) no form is
    measured twice at the same width and arrangement, and the panels' layout passes measure
    inside one."""
    import mlaser.ui.responsive as R
    _open_process(app, win, 300)
    passes: list[dict] = []           # kept alive: their ids stay unique
    counts: dict = {}
    repeats: list = []
    outside = [0, 0]                  # (measured outside a pass, inside one)
    measure = R.ResponsiveForm._measure_height

    def counted(self, width, beside):
        memo = R._MEMO
        if memo is None:
            outside[0] += 1
        else:
            outside[1] += 1
            if not any(p is memo for p in passes):
                passes.append(memo)
            k = (id(memo), id(self), width, beside)
            counts[k] = counts.get(k, 0) + 1
            if counts[k] == 2:
                repeats.append((type(self.parentWidget()).__name__, width, beside))
        return measure(self, width, beside)
    monkeypatch.setattr(R.ResponsiveForm, "_measure_height", counted)
    _steps(app, win, 6)
    assert not repeats, repeats[:10]
    assert outside[1] > 4 * outside[0], outside


@pytest.mark.flaky(reruns=1)            # a timing ratio on a shared runner (one rerun, like the other timing tests:
                                        # macOS CI once measured 4.7 times, 2.3 here and in its other run)
def test_a_resize_step_with_a_drawing_costs_about_what_one_without_costs(win, app):
    _open_process(app, win, 0)
    _steps(app, win, 3)                                    # warm-up
    empty = statistics.median(_steps(app, win))
    _open_process(app, win, 300)
    _steps(app, win, 3)
    full = statistics.median(_steps(app, win))
    assert full < 3.0 * empty + runner_budget(0.03), (full, empty)
