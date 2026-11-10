"""Nest hand-off (deep review 2026-11, UX finding 13): one path from a finished nest to the Run
workspace, a job label that names the sheet and its parts, and the example-values warning once
per recipe and session after an explicit go-ahead (it still blocks until then, Cancel the
default)."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.ui

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton  # noqa: E402

from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch, tmp_path):
    from wiring import dispose, real_window
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
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


def wait(app, pred, timeout=30.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


def rect(x0, y0, w, h, layer=1, name="gear"):
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    return Part([Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)], Craft(layer=layer), name=name)


def tech_layer(material="Mild steel S235", thickness=3.0, li=1, example=False):
    from mlaser.services.technology import Technology, TechKey, apply_to_layer
    t = Technology(TechKey(material, thickness, "O2", "1.0 double"))
    t.id, t.version = 1, 1
    if example:
        t.origin = "example"
    return apply_to_layer(LayerParams(index=li), t)


# ------------------------------------------------------------------------------ hand-off
def test_nest_to_run_in_one_path_and_start_names_the_sheet(win, app):
    win.set_document(Document(parts=[rect(0, 0, 100, 50)], layers={1: tech_layer()}))
    nest = win.extension("nest")
    nest.job.parts.clear()
    nest.job.sheets.clear()
    nest.time_limit.setValue(5)
    # Nest with no parts listed takes the drawing's parts (no "From drawing" step first)
    assert nest.start(), nest.status.text()
    assert nest.job.parts and wait(app, lambda: not nest.service.running, 60)
    assert nest.run is not None and nest.run.result.layouts
    total = len(nest.run.result.layouts)
    # one primary hand-off, naming the sheet; the queue is the other, named for all sheets
    assert nest.main_btn.text() == win.tr_.format("nestws.btn.to_run_n", n=1, total=total)
    assert nest.main_btn.isEnabled()
    assert "all sheets" in nest.queue_btn.text()
    assert not [b for b in nest.findChildren(QPushButton) if "Copy sheet" in b.text()]
    mode0 = win.ctl.state().mode
    nest.main_btn.click()
    app.processEvents()
    assert win.shell.host.current == "run"                     # Run is shown ...
    assert win.ctl.state().mode == mode0                       # ... and nothing started
    doc = win.document
    placed = len(doc.parts)
    assert doc.extra["nest_sheet"]["n"] == 1 and doc.extra["nest_sheet"]["total"] == total
    label = win.safety_ui.start_target_name()
    pname = nest.job.parts[0].name                             # the recognised part's name
    assert label.startswith(f"Sheet 1 of {total}") and f"{pname} × {placed}" in label, label
    assert "(untitled)" not in label
    # the run bar's progress text and the Run screen's job name read the same source
    win.safety_ui.refresh()
    rb = win.safety_ui.run_bar
    assert rb.buttons["start"].property("start_target") == win.tr_.format("run.start_target", name=label)
    run = win.extension("run")
    screen = getattr(run, "screen", None)
    if screen is not None and hasattr(screen, "job_name"):
        assert screen.job_name() == label


def test_job_label_for_a_drawing_names_it_and_counts_parts(win, tmp_path):
    su = win.safety_ui
    win.set_document(Document(parts=[rect(0, 0, 10, 10, name="a"), rect(20, 0, 10, 10, name="b")],
                              layers={1: tech_layer()}))
    assert su.start_target_name() == win.tr_.format("run.target.drawing", name=win.tr_.tr("run.untitled"),
                                                    parts=win.tr_.plural("run.target.n_parts", 2))
    win.path = str(tmp_path / "plate.dxf")
    assert su.start_target_name().startswith("plate.dxf: 2 parts")


# ------------------------------------------------------------------------------ example values
def _example_doc(thickness=3.0):
    from mlaser.services.material_check import SheetSpec, set_sheet
    doc = Document(parts=[rect(0, 0, 100, 50)], layers={1: tech_layer(thickness=thickness, example=True)})
    set_sheet(doc, SheetSpec("Mild steel S235", thickness, "nest"))
    return doc


@pytest.fixture
def shown(win, monkeypatch):
    """The pre-flight dialogs shown: (codes and severities, default button text); the answer
    is ``answer[0]`` (1 = go ahead, 0 = Cancel)."""
    from mlaser.ui import safety_ui
    class Shown(list):
        answer: list

    out = Shown()
    answer = [1]

    def exec_(self):
        default = [b.text() for b in self.findChildren(QPushButton) if b.isDefault()]
        out.append(([(i.code, i.severity) for i in self.report.issues], default))
        return answer[0]
    monkeypatch.setattr(safety_ui.PreflightDialog, "exec", exec_)
    win._test_no_dialogs = False
    win.ctl.connect()
    out.answer = answer
    return out


def _example_warnings(entry):
    return [c for c in entry[0] if c[0] == "technology_example"]


def test_example_values_warning_blocks_until_confirmed_once_per_recipe(win, app, shown):
    su = win.safety_ui
    doc = _example_doc()
    # Cancel: blocked, and asked again next time
    shown.answer[0] = 0
    assert not su.preflight_ok("start", doc, True, assume_armed=True)
    assert _example_warnings(shown[-1]) == [("technology_example", "warning")]
    assert shown[-1][1] and "Cancel" in shown[-1][1][0]           # Cancel is the default
    n = len(shown)
    assert not su.preflight_ok("start", doc, True, assume_armed=True)
    assert len(shown) == n + 1
    # an explicit go-ahead confirms this recipe for the session
    shown.answer[0] = 1
    assert su.preflight_ok("start", doc, True, assume_armed=True)
    n = len(shown)
    rep_ok = su.preflight_ok("start", _example_doc(), True, assume_armed=True)   # the next sheet
    assert rep_ok
    assert all(not _example_warnings(e) for e in shown[n:])                       # not asked again
    assert any(i.code == "technology_example" and i.severity == "info" for i in su.last_report.issues)
    # another recipe is still asked
    n = len(shown)
    shown.answer[0] = 0
    assert not su.preflight_ok("start", _example_doc(thickness=5.0), True, assume_armed=True)
    assert len(shown) == n + 1 and _example_warnings(shown[-1])


def test_example_values_message_says_what_going_ahead_does(win):
    orders = win.extension("orders")
    (i,) = [i for i in orders.issues("start", _example_doc(), True) if i.code == "technology_example"]
    assert i.severity == "warning" and "until mlaser closes" in i.message
