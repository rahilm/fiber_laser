"""ext_diag on the main window: every command, job statistics end to end, persistence."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

COMMANDS = ["mon.controller", "mon.ftc", "mon.af", "mon.ext", "diag.device_report", "diag.work_report",
            "diag.work_report_all", "diag.log_dir", "diag.measure", "aux.custom_io", "aux.custom_io_nl",
            "aux.dedust", "aux.light_test", "aux.burn_in", "aux.ball_arm", "aux.interferometer",
            "aux.batch_cut", "aux.maintain", "app.help"]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def win(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    from wiring import real_window
    w = real_window()
    ext = w.extension("ext_diag")
    yield w, ext
    w.close()


def test_all_commands_rebound(win):
    w, ext = win
    missing = []
    w.not_implemented = lambda c: missing.append(c)
    for cmd in COMMANDS:
        assert cmd in w.handlers
        w.ribbon.actions[cmd].trigger()
        QApplication.processEvents()
    assert missing == []
    assert ext.interferometer_dock is not None and not ext.interferometer_dock.isHidden()
    assert "help" in ext.windows and "report" in ext.windows
    # the device monitors are panels of the Machine workspace (Wave 3), not floating windows
    assert "controller" in w.extension("ext_machine").workspace.monitors
    w.run("aux.interferometer")
    assert ext.interferometer_dock.isHidden()


def test_job_statistics_end_to_end(win, tmp_path):
    from mlaser.model import Contour, Document, LayerParams, Line, Part
    from mlaser.ui.facade import DemoController
    w, ext = win
    sq = Contour([Line((0, 0), (5, 0)), Line((5, 0), (5, 5)), Line((5, 5), (0, 5)), Line((0, 5), (0, 0))])
    w.set_document(Document(parts=[Part([sq])], layers={1: LayerParams(1)}))
    w.ctl.connect()
    assert w.ctl.arm()
    DemoController.RAPID = 4000.0
    try:
        w.ctl.start(w.document)
        t_end = time.monotonic() + 10
        while w.ctl.state().mode == "process" and time.monotonic() < t_end:
            time.sleep(0.01)
        ext.tick()
    finally:
        DemoController.RAPID = 400.0
    assert len(ext.stats.records) == 1
    rec = ext.stats.records[0]
    assert rec.width == 5 and rec.count == 1
    assert ext.counters.process_count == 1 and ext.settings["SOP.TotalManuCount"] == 1
    assert (tmp_path / "Report" / "TotalReport.txt").exists()
    assert (tmp_path / "File" / "ProcessesStatistic.txt").exists()
    rw = ext.work_report(False)
    assert rw.model.rowCount() == 1 and rw.model.item(0, 1).text() == rec.name
    # the UI log is written in the original line format
    logs = list((tmp_path / "Log").glob("*.log"))
    assert logs and "[Info] -> " in logs[0].read_text("utf-8")
    # persistence: a new window over the same data dir gets the counters back
    ext.save_state()
    from wiring import real_window
    w2 = real_window()
    ext2 = w2.extension("ext_diag")
    assert ext2.counters.process_count == 1
    w2.close()


def test_prohibit_after_plan(win):
    w, ext = win
    ext.settings.update({"SOP.PlanManuCount": 1, "SOP.TotalManuCount": 1, "SOP.AfterPlanFinishedActionType": 2})
    w.ctl.connect()
    assert w.ctl.arm()
    from mlaser.ui.docops import demo_document
    w.set_document(demo_document())
    for lp in w.document.layers.values():
        lp.extra.pop("placeholder", None)          # layer parameters set by the operator
    w.ctl.start(w.document)
    assert w.ctl.state().mode == "process"
    QApplication.processEvents()
    assert w.ctl.state().mode == "ready"


def test_dedust_and_measure(win):
    w, ext = win
    ext.settings["DO.ClearAsh"] = 6
    act = w.ribbon.actions["aux.dedust"]
    assert ext.dedust() is True and act.isChecked()
    assert act.text() == ext.w.tr_.tr("mf170")
    assert ext.dedust() is False and act.text() == ext.w.tr_.tr("mf171")
    assert ext.toggle_measure() is True
    w.ctl.connect()
    ext.tick()
    ext.tick()
    assert len(ext.measure.samples) == 2
    mw = ext.windows["measure"]
    mw.refresh()
    assert mw.table.rowCount() == 2
    assert ext.toggle_measure() is False


def test_ball_arm_and_custom_io(win):
    w, ext = win
    from mlaser.diag import ballarm
    doc = ext.apply_ball_arm(ballarm.params(ext.settings))
    assert w.document is doc and len(doc.parts) == 2
    w.undo()
    assert w.document is not doc
    ext.settings["DO.CustomDOStr"] = "Clamp#7#-,Blow#8#0"
    d = ext.custom_io_dialog(True)
    assert list(d.buttons) == [7]
    assert d.press(ext.custom_io()[0]) is True and d.buttons[7].isChecked()
    nl = ext.custom_io_dialog(False)
    assert list(nl.buttons) == [8]
    nl.buttons[8].pressed.emit()
    assert ext.outputs.get(8)
    nl.buttons[8].released.emit()
    assert not ext.outputs.get(8)
    nl.editor.setRowCount(0)
    nl._add_row()
    nl.save()
    assert ext.settings["DO.CustomDOStr"] == "DO#1#0"


def test_dialog_contents(win):
    w, ext = win
    lt = ext.light_test()
    assert len(lt.form.editors) == 5
    bi = ext.burn_in()
    assert "ST.Type" in bi.form.editors
    bd = ext.batch_cut()
    bd.add_file(__file__, planned=3)
    bd._ok()
    assert ext.batch_list.entries[0].planned == 3
    md = ext.maintain()
    assert md.table.rowCount() == 17
    md.custom.setText("Check chiller")
    md.add_custom()
    md._ok()
    assert "Check chiller" in ext.settings["MC.MaintainParamStr"]
    lv = ext.log_dir_viewer()
    lv.reload()
    hw = ext.help()
    assert hw.tree.topLevelItemCount() >= 4 and len(hw.topics) >= 20
    hw.tree.setCurrentItem(hw.tree.topLevelItem(hw.tree.topLevelItemCount() - 1))   # About
    assert "omitted" in hw.view.toPlainText()
