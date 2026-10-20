"""The Machine workspace and the wizard dialogs in the real window (offscreen, simulator)."""
import math
import time

import pytest

from mlaser.controller import edgeseek as E
from mlaser.services.procedures import engine as PE


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import real_window
    w = real_window()
    yield w
    w.close()


def pump(app, sec=0.2):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.01)


def wait(app, cond, sec=20.0):
    t = time.time()
    while not cond() and time.time() - t < sec:
        app.processEvents()
        time.sleep(0.02)
    return cond()


def drive(app, d, until=None):
    p = d.proc
    for _ in range(40):
        pump(app, 0.02)
        wait(app, lambda: not p.busy())
        s = p.step
        if p.state in PE.FINAL or s is None or s.id == until:
            return
        if s.kind == "action" and s.id not in p.done:
            d.run()
            wait(app, lambda: not p.busy() and (s.id in p.done or p.state in PE.FINAL))
            continue
        if s.kind == "question":
            d.answer(True)
        if p.can_next():
            d.next()


def test_workspace_registered_with_modes(win):
    ext = win.extension("ext_machine")
    assert win.workspaces["ext_machine.machine"][1] is ext.workspace
    for cmd, mode in (("wizard.commissioning", "service"), ("wizard.edge_seek_cap4", "operator"),
                      ("wizard.laser_test", "engineer"), ("machine.import_install", "service")):
        spec = win.commands.get(cmd)
        assert spec is not None and getattr(spec, "min_mode", None) == mode


def test_workspace_sections_connection_and_monitor_panels(app, win):
    ext = win.extension("ext_machine")
    ws = ext.show_workspace("connection")
    assert ws.isVisible()
    assert ws.connect()                                      # simulator radio is the default
    pump(app, 0.4)
    assert win.ctl.is_simulator() and ws.lbl["conn"].text() == "connected"
    res = ws.run_selftest()
    assert res.card_host == "127.0.0.1" and "self-test" in ws.selftest.toPlainText()
    mon = win.extension("ext_diag").show_monitor("controller")
    assert mon.parentWidget() is not None and not mon.isWindow()     # a panel, not a dialog
    assert ws.section() == "monitors" and mon.isVisible()
    assert win.extension("ext_diag").show_monitor("controller") is mon
    ws.disconnect()
    assert not win.ctl.state().connected


def test_user_mode_gates_sections_and_commands(app, win):
    ext = win.extension("ext_machine")
    ws = ext.workspace
    assert win.set_user_mode("operator")                    # the Shell's user_mode provider
    ws.tick()
    assert not ws.settings_panel.editable
    assert not ws.proc_buttons["wizard.commissioning"].isEnabled()
    assert ws.proc_buttons["wizard.edge_seek_cap4"].isEnabled()
    assert not ext.allowed("wizard.commissioning")
    assert win.commands.run("wizard.commissioning") is None and ext.last_wizard is None
    assert win.set_user_mode("service")
    ws.tick()
    assert ws.settings_panel.editable and ws.proc_buttons["wizard.commissioning"].isEnabled()


def test_settings_panel_applies_typed_values(app, win):
    ws = win.extension("ext_machine").workspace
    sp = ws.settings_panel
    i = sp.section.findData("MachineAxisConfig_0")
    sp.section.setCurrentIndex(i)
    ed = sp._editors["MAC.SoftLimitMaxLen"]
    ed.setValue(2500.0)
    assert sp.apply() == 1
    assert win.config.get("MAC.SoftLimitMaxLen") == 2500.0
    # the search looks through every Setup section (Machine › Setup, WORK_ITEMS R-1)
    sp.search.setText("stroke")
    rows = [sp.tree.topLevelItem(i).child(j) for i in range(sp.tree.topLevelItemCount())
            for j in range(sp.tree.topLevelItem(i).childCount())]
    shown = [r for r in rows if not r.isHidden()]
    assert shown and all("stroke" in (r.text(0) + r.text(2)).lower() for r in shown)
    assert len({sp.keys[r.text(2)] for r in shown}) > 1          # several sections
    sp.search.setText("")
    assert sp.section.currentData() == "MachineAxisConfig_0" and "MAC.SoftLimitMaxLen" in sp._editors


def test_edge_wizard_dry_run_review_and_abort(app, win):
    from mlaser.ui.wizards.procedures import EdgeSeekWizard
    ext = win.extension("ext_machine")
    d = EdgeSeekWizard(win, ext, "cap4")
    d.dry.setChecked(True)
    assert d.proc.dry_run and d.rig is not None and d.rig is not win.ctl
    drive(app, d, "review")
    assert d.proc.step.id == "review"
    assert d.view.data["angle_deg"] == pytest.approx(1.5, abs=0.03)
    assert win.canvas._preview_items                          # outline drawn on the canvas
    d.abort()
    assert d.proc.state == PE.ABORTED and d.proc.halts == 1
    rig = d.rig
    d.done(0)
    assert d.rig is None and not rig.state().connected
    assert not win.canvas._preview_items


def test_edge_apply_through_the_window_is_undoable(app, win):
    from mlaser.model import Contour, Document, Line, Part
    win.set_document(Document(parts=[Part([Contour([Line((0, 0), (10, 0))])])]))
    ext = win.extension("ext_machine")
    res = E.EdgeSeekResult(mode=4, points=[], corner=(5.0, 5.0), origin=(10.0, 10.0), angle=math.radians(1.2))
    ext.apply_edge_result(res)
    assert win.document.extra["rotated"] and win.document.extra["rotate_center"] == (10.0, 10.0)
    win.commands.run("edit.undo")
    assert not win.document.extra.get("rotated")
    win.commands.run("edit.redo")
    assert win.document.extra["rotated"]


def test_follower_wizard_plots_the_curve(app, win, tmp_path):
    from mlaser.ui.wizards.procedures import FollowerWizard
    d = FollowerWizard(win, str(tmp_path / "calib.csv"))
    d.dry.setChecked(True)
    drive(app, d, "review")
    assert d.proc.step.id == "review" and len(d.plot.points) == 22
    assert "excellent" in d.quality.text()
    drive(app, d)
    assert d.proc.state == PE.DONE and not (tmp_path / "calib.csv").exists()     # dry run: not saved
    d.done(0)


def test_laser_wizard_asks_before_arming(app, win, monkeypatch):
    from mlaser.ui.wizards import base
    from mlaser.ui.wizards.procedures import LaserWizard
    asked = []
    monkeypatch.setattr(base.QMessageBox, "question",
                        staticmethod(lambda *a, **k: (asked.append(a[2]), base.QMessageBox.Cancel)[1]))
    win._test_no_dialogs = False
    d = LaserWizard(win)
    d.dry.setChecked(True)
    drive(app, d, "arm")
    d.run()                                                   # Cancel in the confirmation: nothing armed
    assert asked and "Arm the laser" in asked[0] and "arm" not in d.proc.done and not d.rig.safety().armed
    d.abort()
    d.done(0)


def test_commissioning_wizard_checklist(app, win):
    from mlaser.ui.wizards.procedures import CommissioningWizard
    d = CommissioningWizard(win, win.extension("ext_machine"))
    d.dry.setChecked(True)
    drive(app, d)
    assert d.proc.state == PE.DONE
    assert d.checklist.rowCount() == 8
    texts = [d.checklist.item(i, 1).text() for i in range(8)]
    # 6 pass; soft limits and the interlocks (none configured: flagged) warn
    assert sum("pass" in t for t in texts) == 6, [(d.checklist.item(i, 0).text(), t, d.checklist.item(i, 2).text())
                                                   for i, t in enumerate(texts)]
    assert d.proc.report_path is not None and d.proc.report_path.exists()
    d.done(0)


def test_jogpad_dead_man(app, win):
    from mlaser.ui.wizards.common import JogPad
    calls = []

    class Ctl:
        def jog_start(self, a, d, fast=False):
            calls.append(("start", a, d))

        def jog_keepalive(self):
            calls.append(("alive",))

        def jog_stop(self, a=None):
            calls.append(("stop", a))

        def jog_step(self, a, d, s, fast=False):
            calls.append(("step", a, d, s))
    c = Ctl()
    pad = JogPad(win, lambda: c)
    pad.show()
    pad.buttons["X+"].pressed.emit()
    # the keep-alive is a 100 ms QTimer: wait for its first tick instead of a fixed 0.35 s (a
    # loaded macOS runner delivered none within 0.35 s); its timing is not what is tested here
    assert wait(app, lambda: ("alive",) in calls, 5.0)
    assert calls[0] == ("start", "X", 1)
    pad.hide()                                               # hidden while held: stops at once
    assert calls[-1] == ("stop", "X")
    pad.step_buttons[2].setChecked(True)
    pad.buttons["Y-"].pressed.emit()
    assert calls[-1] == ("step", "Y", -1, 1.0)


def test_settings_import_dialog(app, win, tmp_path):
    from test_settings_import import make_install

    from mlaser.ui.wizards.settings_import import SettingsImportDialog
    inst, _ = make_install(tmp_path)
    d = SettingsImportDialog(win, win.config, install=str(inst))
    plan = d.preview()
    assert plan is not None and d.b_apply.isEnabled() and d.tree.topLevelItemCount() == 6
    top = d.tree.topLevelItem(0)
    from PySide6.QtCore import Qt
    top.setCheckState(0, Qt.Unchecked)                       # skip the machine parameters
    assert "params" not in d.chosen_areas()
    res = d.apply()
    assert res is not None and "params" not in res.applied and "ip" in res.applied
    d.done(0)
