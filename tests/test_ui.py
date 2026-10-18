"""Offscreen smoke tests for mlaser.ui (QT_QPA_PLATFORM=offscreen)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mlaser.ui import docops, i18n  # noqa: E402
from mlaser.ui.facade import DemoController  # noqa: E402

SHOT = "/home/dev/scratch/mlaser/work/ui/screenshot.png"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_lang_catalogue():
    tr = i18n.Translator(language="en")
    assert tr.tr("mf15") == "Go Origin"
    assert tr.tr("mp54") == "Start"
    assert tr.tr_param("pd125") == ("Cut Basic", "Cut Speed")
    tr.set_language("zh")
    assert tr.tr("mf15") == "回原点"
    assert tr.tr("no_such_key", "x") == "x"
    tr.set_language("de")
    assert tr.tr("mf15")  # German file present, falls back to English if missing


def test_demo_controller_motion():
    t = [0.0]
    c = DemoController(clock=lambda: t[0])
    c.jog_start("X", 1)
    assert c.state().x == 0.0          # not connected -> ignored
    c.connect()
    c.jog_start("X", 1, fast=False)
    t[0] += 1.0
    assert c.state().x == pytest.approx(DemoController.JOG_SLOW)
    c.jog_stop()
    doc = docops.demo_document()
    c.start(doc)
    assert c.state().mode == "process"
    c.pause()
    assert c.state().mode == "pause"
    c.resume()
    t[0] += 1e4
    assert c.state().mode == "ready"
    c.frame(doc)
    assert c.state().mode == "frame"
    c.stop()
    assert c.state().mode == "ready"


def test_docops_roundtrip():
    doc = docops.demo_document()
    b0 = doc.bbox()
    docops.mirror(doc, True)
    docops.mirror(doc, True)
    assert doc.bbox() == pytest.approx(b0)
    docops.rotate(doc, 90)
    docops.rotate(doc, -90)
    assert doc.bbox() == pytest.approx(b0, abs=1e-6)
    n = len(doc.parts)
    doc.parts[0].selected = True
    docops.array(doc, 2, 3, 5, 5)
    assert len(doc.parts) == n + 5


def test_main_window_screenshot(app):
    from mlaser.ui.mainwindow import MainWindow

    win = MainWindow(DemoController())
    win.resize(1400, 860)
    doc = docops.demo_document()
    win.set_document(doc)
    win.ctl.connect()
    win.show()
    app.processEvents()
    # selection + layer assignment through the UI paths
    win.canvas.select_parts([1])
    win.assign_layer(5)
    assert win.document.parts[1].craft.layer == 5
    win.undo()
    assert win.document.parts[1].craft.layer == 2
    # copy / paste / nudge / delete (view key handler FUN_004d0480)
    n = len(win.document.parts)
    win.canvas.select_parts([0])
    win.run("edit.copy"); win.run("edit.paste")
    assert len(win.document.parts) == n + 1 and win.document.parts[-1].selected
    b = win.document.parts[-1].bbox()
    win.nudge(1, 0)
    assert win.document.parts[-1].bbox()[0] == pytest.approx(b[0] + 1.0)
    win.delete_selected()
    assert len(win.document.parts) == n
    # every ribbon command is wired to something callable
    assert "file.open" in win.ribbon.actions and "craft.lead" in win.ribbon.actions
    win.run("show.direction")
    win.run("show.move_path")
    # simulation runs to completion
    assert win.canvas.start_simulation(interval_ms=1)
    for _ in range(2000):
        if not win.canvas.simulation_running():
            break
        win.canvas._sim_step()
    assert not win.canvas.simulation_running()
    win.canvas.stop_simulation(clear=True)   # mv11 "clear track line"
    # language switch re-labels the ribbon
    win.set_language("zh")
    assert win.ribbon.tabText(0) == "开始"
    win.set_language("en")
    assert win.ribbon.tabText(0) == "Start"
    win.canvas.select_parts([2])
    win.canvas.zoom_fit()
    win._poll()
    app.processEvents()
    os.makedirs(os.path.dirname(SHOT), exist_ok=True)
    assert win.grab().save(SHOT)
    assert os.path.getsize(SHOT) > 10_000
    win.close()


def test_dialogs_build(app):
    from mlaser.model import LayerParams, LeadParams
    from mlaser.ui import dialogs
    from mlaser.ui.params import OPTION_SECTIONS, registry

    tr = i18n.Translator(language="en")
    assert len(registry()) > 500
    lp = LayerParams(index=1)
    d = dialogs.LayerParamDialog(tr, lp)
    d.values["GP.CutSpeed"] = 55.0
    d._store()
    assert lp.speed == 55.0
    li, lo = LeadParams(kind=1), LeadParams()
    ld = dialogs.LeadDialog(tr, li, lo)
    ld._w["in"][3].setValue(4.5)
    ld._store()
    assert li.length == 4.5
    assert dialogs.KerfDialog(tr, 0, 0.2).result_values() == (0, 0.2)
    for which, secs in OPTION_SECTIONS.items():
        dialogs.ParamGridDialog(tr, "mf137", secs, {})
    assert dialogs.IPDialog(tr).result_values()["pd445"] == "10.1.1.168"
    for cls in (dialogs.MicroJointDialog, dialogs.ArrayDialog, dialogs.PasswordDialog, dialogs.AboutDialog):
        cls(tr)
