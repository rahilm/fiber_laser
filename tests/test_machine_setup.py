"""Machine › Setup and the Run Control / jog values have one home each (SETTINGS_MAP.md,
WORK_ITEMS R-1 .. R-4): every machine setting has exactly one editor (Setup), the machine
operations read the registry keys live (no ``machine_ops.*`` shadow, no restart), the jog step and
fast are one persisted value for every jog pad, Run Control has no unbound input and no copy of
the run block, "Start selection only" is explicit, ``hw.options`` opens Setup."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from mlaser.ui import homes


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    from wiring import real_window
    w = real_window()
    w.resize(1512, 875)
    w.show()
    app.processEvents()
    yield w
    w.close()


def _pump(app, n=5):
    for _ in range(n):
        app.processEvents()


def _setup(win):
    return win.extension("ext_machine").workspace.settings_panel


def test_every_setup_key_has_exactly_one_editor(win, app):
    sp = _setup(win)
    assert len(sp.keys) > 500
    seen: dict[str, int] = {}
    for i in range(sp.section.count()):
        sp.section.setCurrentIndex(i)
        _pump(app)
        for ed in sp._editors.values():                  # the section's editors (the old ones go)
            assert ed.property(homes.PROP_ROLE) == homes.EDITOR
            seen[ed.property(homes.PROP_VALUE)] = seen.get(ed.property(homes.PROP_VALUE), 0) + 1
    want = {f"cfg:{k}" for k in sp.keys}
    assert set(seen) == want
    assert all(n == 1 for n in seen.values())
    # nowhere else in the window (the dock, other pages) edits a Setup key
    outside = [w for w in homes.editors(win) if w.property(homes.PROP_VALUE) in want and not sp.isAncestorOf(w)]
    assert not outside
    # every Setup key and the jog values have their home here, implemented
    for k in list(sp.keys)[:50] + ["MC.StepLength", "MSC.CoordSysType"]:
        h = homes.home_of(f"cfg:{k}")
        assert h is not None and h.workspace == "machine" and h.implemented


def test_setup_has_the_other_axes_and_options_rows_and_is_service_only(win, app):
    sp = _setup(win)
    for key in ("MAC_1.AxisReverse", "MAC_4.SoftLimitMaxLen", "MC.JogFastSpeed", "GRP.EdgeSeekOutSpeed",
                "MSC.DockPtType"):
        assert key in sp.keys, key
    assert sp.show_key("MAC_1.AxisReverse") is not None
    assert win.set_user_mode("engineer")
    _pump(app)
    assert not any(e.isEnabled() for e in sp._editors.values())       # Service mode to edit
    assert win.set_user_mode("service")
    _pump(app)
    assert all(e.isEnabled() for e in sp._editors.values())


def test_edit_in_and_hw_options_open_setup(win, app):
    w = homes.edit_in(win, "cfg:MAC_1.AxisReverse")
    _pump(app)
    assert w is not None and w.objectName() == "setup_MAC_1.AxisReverse" and w.isVisible()
    win.show_workspace("design")
    win.commands.run("hw.options")
    _pump(app)
    ws = win.extension("ext_machine").workspace
    assert win.shell.host.current == "machine" and ws.section() == "settings"
    assert not [d for d in app.topLevelWidgets() if type(d).__name__ == "ParamGridDialog" and d.isVisible()]


def test_machine_ops_follow_the_registry_live(win, app):
    ext = win.extension("ext_machine")
    win.config.set("MSC.CoordSysType", 2)
    assert ext.ops.coords.kind == 2                      # no restart
    win.config.set("GRP.EdgeSeekOutSpeed", 77.0)
    assert ext.ops.edge.out_speed == 77.0
    ext.ops.set_mark_index(3)
    assert win.config.get("MC.MarkPtIndex") == 3          # the registry key, not machine_ops.*
    assert "machine_ops.MC.MarkPtIndex" not in win.config.values


def test_machine_ops_shadow_copies_are_merged_on_start(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    from mlaser.services.config import ConfigService
    cfg = ConfigService(tmp_path / "c.json")
    cfg.set("MSC.CoordSysType", 0)
    cfg.set("machine_ops.MSC.CoordSysType", 3)          # the old shadow the machine ran with
    cfg.set("machine_ops.param_dir", str(tmp_path))      # the operations' own state stays
    from wiring import real_window
    w = real_window(config=cfg)
    try:
        assert cfg.get("MSC.CoordSysType") == 3
        assert "machine_ops.MSC.CoordSysType" not in cfg.values
        assert cfg.get("machine_ops.param_dir") == str(tmp_path)
        assert w.extension("ext_machine").ops.coords.kind == 3
    finally:
        w._test_no_dialogs = True
        w.close()


def test_edge_seek_wizard_edits_are_saved(win, app):
    from mlaser.ui.wizards.procedures import EdgeSeekWizard
    ext = win.extension("ext_machine")
    d = EdgeSeekWizard(win, ext, "cap4")
    try:
        step = next(s for s in d.proc.steps if s.id == "params")
        page = d.page(step)                              # noqa: F841 - keeps the page's widgets
        d.ed["follow_height"].setValue(3.25)
        d.ed["out_speed"].setValue(66.0)
        assert d.collect(step)
        assert win.config.get("GRP.EdgeSeekFollowHeight") == 3.25
        assert win.config.get("GRP.EdgeSeekOutSpeed") == 66.0     # survives a restart (config)
    finally:
        d.done(0)


def test_one_jog_step_for_every_jog_pad(win, app):
    ws = win.extension("ext_machine").workspace
    pad = ws.jog
    panel = win.control
    ten = next(b for b in pad.step_buttons if float(b.property("step_mm")) == 10.0)
    ten.click()
    _pump(app)
    assert win.config.get("MC.IsStepMove") is True and win.config.get("MC.StepLength") == 10.0
    assert panel.mode_step.isChecked() and panel._step_mm() == 10.0      # Run Control shows it
    panel.mode_fast.setChecked(True)
    _pump(app)
    assert win.config.get("MC.IsFastMode") is True and pad.fast.isChecked()
    pad.step_buttons[0].click()                          # continuous
    _pump(app)
    assert win.config.get("MC.IsStepMove") is False and not panel.mode_step.isChecked()
    # one editor (the Machine pad); the others are quick switches on the same key
    eds = [w for w in homes.editors(win) if w.property(homes.PROP_VALUE) == "cfg:MC.StepLength"]
    assert eds and all(ws.isAncestorOf(w) for w in eds)


def test_run_control_has_no_unbound_input_and_no_run_block(win, app):
    panel = win.control
    bad = [type(w).__name__ for w in panel.findChildren(type(panel).__mro__[1])
           if homes.is_input(w) and w.property(homes.PROP_VALUE) is None
           and not (w.parentWidget() is not None and w.parentWidget().property(homes.PROP_VALUE) is not None)]
    assert not bad
    for gone in ("mp54", "mp55", "mp56", "mp57", "mp58"):      # Start / Pause / Stop / Dry Cut / Frame
        assert gone not in panel.buttons
    assert not hasattr(panel, "selected_only") and not hasattr(panel, "return_to")


def test_start_selection_is_explicit(win, app):
    su = win.safety_ui
    started = []
    su._runner = lambda: type("R", (), {"start": lambda self, doc, selected_only=False: started.append(selected_only)})()
    su.preflight_ok = lambda *a, **k: True
    su.arm = lambda *a, **k: True
    su.start()
    assert started == [False]                            # Start = the whole job, always
    su.start_selection()                                 # nothing selected: refused
    assert started == [False]
    from mlaser import ops
    win.set_document(ops.demo_document())
    win.canvas.select_parts([0])
    su.start_selection()
    assert started == [False, True]
    menu = su.run_bar.start_menu.menu_
    assert [a.objectName() for a in menu.actions()] == ["run_start_selection"]
    assert win.commands.get("run.start_selection") is not None
