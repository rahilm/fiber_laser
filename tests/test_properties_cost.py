"""The properties panel's cost follows what it shows (perf review 2026-11: a selection change
cost 23-43 ms at 2,700 parts).

* each inherited value is evaluated once per layer and once per part that overrides it, not
  once per selected part: the same results as one evaluation per part;
* Design shows Transform and Layer only: a selection change there reads no CAM value; Process
  reads them when it is shown;
* a panel hidden in a shown window (another workspace in front) reads the selection when it is
  shown again, once."""
from __future__ import annotations

import random

import pytest
from test_large_drawing import big_document

from mlaser.model import LayerParams
from mlaser.services import inheritance as I


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app):
    from wiring import dispose, real_window
    w = real_window()
    w.resize(1400, 900)
    w.show()
    app.processEvents()
    yield w
    dispose(w)


def _props(win):
    ext = win.extension("ext_process")
    ext = ext[0] if isinstance(ext, tuple) else ext
    return ext.properties


def _count_effective(monkeypatch) -> list:
    calls: list = []
    real = I.effective
    monkeypatch.setattr(I, "effective", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    return calls


def test_grouped_summary_equals_one_evaluation_per_part():
    from mlaser.ui.properties.panel import _effective_many, _SelectionGroups
    rnd = random.Random(7)
    doc = big_document(60)
    doc.layers = {li: LayerParams(index=li) for li in (1, 2, 3)}
    I.set_override(doc, None, "layer", "craft.kerf_width", 0.3, layer=2)
    for p in doc.parts:
        p.craft.layer = rnd.choice((1, 2, 3))
    for p in rnd.sample(doc.parts, 20):
        I.set_override(doc, None, "part", "craft.kerf_width", rnd.choice((0.15, 0.3)), part=p)
        I.set_override(doc, None, "part", "craft.lead_in.length", 3.5, part=p)
    vids = [*I.CRAFT_IDS, "GP.speed"]
    for _ in range(40):
        sel = rnd.sample(doc.parts, rnd.randint(1, len(doc.parts)))
        groups = _SelectionGroups(sel)
        marks = [I.craft_overrides(p.craft) for p in sel]
        for vid in vids:
            vals = _effective_many(doc, None, vid, sel, marks)
            own = [src == "part" for _v, src in vals]
            several = len({repr(v) for v, _s in vals}) > 1
            assert groups.summary(doc, None, vid) == (vals[0][0], sum(own), several), vid


def test_select_all_costs_per_layer_not_per_part(win, app, monkeypatch):
    """Process shown: the inheritance evaluations of a select-all are the same for 100 and for
    1,000 parts (three layers)."""
    props = _props(win)
    win.shell.show_workspace("process")
    counts = []
    for n in (100, 1000):
        doc = big_document(n)
        for k, p in enumerate(doc.parts):
            p.craft.layer = 1 + k % 3
        win.set_document(doc)
        app.processEvents()
        calls = _count_effective(monkeypatch)
        win.run("select.all")
        app.processEvents()
        assert props.isVisible() and "1" in props.summary.text()
        counts.append(len(calls))
        monkeypatch.undo()
    assert counts[0] > 0 and counts[0] == counts[1], counts


def test_design_reads_no_cam_value_until_process_shows_them(win, app, monkeypatch):
    props = _props(win)
    win.shell.show_workspace("design")
    doc = big_document(50)
    win.set_document(doc)
    app.processEvents()
    I.set_override(doc, None, "part", "craft.kerf_width", 0.25, part=doc.parts[3])
    calls = _count_effective(monkeypatch)
    win.canvas.select_parts([3])
    app.processEvents()
    assert props.mode == "design" and calls == []
    assert props.x.value() == pytest.approx(doc.parts[3].bbox()[0], abs=1e-3)   # Transform is read
    win.shell.show_workspace("process")
    app.processEvents()
    assert calls and props.kerf_w.value() == pytest.approx(0.25)


def test_a_hidden_panel_reads_the_selection_when_shown(win, app, monkeypatch):
    props = _props(win)
    win.set_document(big_document(40))
    win.shell.show_workspace("run")
    app.processEvents()
    assert not props.isVisible()
    refreshed: list = []
    real = props._refresh
    monkeypatch.setattr(props, "_refresh", lambda: (refreshed.append(1), real())[1])
    for idx in ([1], [1, 2], [1, 2, 3]):
        win.canvas.select_parts(idx)
        app.processEvents()
    assert refreshed == []
    win.shell.show_workspace("process")
    app.processEvents()
    assert props.isVisible() and len(refreshed) == 1
    assert "3" in props.summary.text()
