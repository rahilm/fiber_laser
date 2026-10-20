"""The Wave 3 shell in the real main window (offscreen): workspaces and the rail, the plugin
workspace API, the command palette, user modes and PINs, themes, the classic ribbon, keys,
accessibility and the layout at 1280×800 / 150 %."""
from __future__ import annotations

import sys
import types

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.ui

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeySequence, QPalette  # noqa: E402
from PySide6.QtWidgets import (QApplication, QDialog, QLabel, QMessageBox,  # noqa: E402
                               QToolButton, QWidget)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch):
    from wiring import real_window
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window(user_mode="engineer")
    w.resize(1280, 800)
    w.show()
    app.processEvents()
    yield w
    w.shell.set_theme("light", persist=False)
    w._test_no_dialogs = True
    w.close()


def _pump(app, n=5):
    for _ in range(n):
        app.processEvents()


# --------------------------------------------------------------------------- workspaces
def test_rail_has_the_six_workspaces_and_design_is_default(win, app):
    sh = win.shell
    assert sh.host.current == "design"
    assert sh.host.visible_ids()[:6] == ["design", "process", "nest", "run", "report", "machine"]
    assert win.canvas.isVisible() and sh.host.pages["design"].isAncestorOf(win.canvas)
    for i, ws in enumerate(["design", "process", "nest", "run", "report", "machine"], 1):
        act = win.ribbon.actions[f"workspace.{ws}"]
        assert act.shortcut() == QKeySequence(f"Ctrl+{i}")
        btn = sh.host.rail.widgetForAction(sh.host.rail_actions[ws])
        assert btn is not None and btn.accessibleName()
    win.run("workspace.process")
    assert sh.host.current == "process" and sh.host.pages["process"].isAncestorOf(win.canvas)
    win.run("workspace.report")
    assert sh.host.current == "report" and not win.canvas.isVisible()
    # the Wave 3 workspaces fill the slots (EXTENSIONS); their own ids switch too
    assert sh.host.pages["report"].isAncestorOf(win.extension("report").widget)
    assert sh.host.pages["run"].isAncestorOf(win.extension("run").screen)
    assert sh.host.pages["machine"].isAncestorOf(win.extension("ext_machine").workspace)
    assert sh.host.pages["process"].isAncestorOf(win.extension("ext_process").workspace)
    assert all(sh.host.pages[s].placeholder is None for s in ("process", "nest", "run", "report", "machine"))
    for full, slot in (("run.run", "run"), ("nest.nest", "nest"), ("report.report", "report"),
                       ("ext_machine.machine", "machine"), ("ext_process.process", "process")):
        assert win.show_workspace(full) and sh.host.current == slot
        assert win.hooks.provider("workspace.show")(full)
    win.run("process.show")
    assert sh.host.current == "process" and win.canvas.isVisible()
    assert win.hooks.provider("user_mode")() == "engineer"
    win.show_workspace("design")
    assert win.canvas.isVisible()


def test_workspace_docks_follow(win, app):
    win.show_workspace("process")
    _pump(app)
    props = win.findChild(type(win.left_dock), "plugin.ext_process.properties")
    assert props is not None and not props.isHidden() and win.right_dock.isHidden()
    win.show_workspace("report")
    _pump(app)
    assert props.isHidden() and win.right_dock.isHidden() and not win.log_dock.isHidden()
    win.show_workspace("design")
    _pump(app)
    assert not win.right_dock.isHidden() and not win.left_dock.isHidden()


def test_plugin_workspace_can_be_exclusive(win, app):
    """``add_workspace(..., exclusive=True)``: no docked panel shares the window while it is
    active; leaving it brings the docks back (and the run bar is not a dock)."""
    from PySide6.QtWidgets import QDockWidget, QLabel as _L

    from mlaser.services.plugins import PluginContext
    ctx = PluginContext(win, "wide_test")
    ctx.add_workspace("wide", _L("wide"), "Wide", exclusive=True)
    win.show_workspace("design")
    _pump(app)
    assert not win.left_dock.isHidden()
    win.show_workspace("wide_test.wide")
    _pump(app)
    docked = [d.objectName() for d in win.findChildren(QDockWidget)
              if d.parent() is win and d.isVisible() and not d.isFloating()]
    assert docked == [], docked
    win.show_workspace("design")
    _pump(app)
    assert not win.left_dock.isHidden() and not win.right_dock.isHidden()


def test_window_keeps_its_size_with_every_workspace(win, app):
    for ws in win.shell.host.visible_ids():
        win.show_workspace(ws)
        _pump(app)
        assert win.width() <= 1280 and win.height() <= 800, ws


def test_plugin_fills_a_slot_and_adds_its_own_workspace(win, app):
    from mlaser.services.plugins import PluginInfo, register_plugin
    proc, tool, old = QLabel("process ui"), QLabel("tool ui"), QLabel("old api")

    def reg(ctx):
        ctx.add_workspace("process", proc, "Process", icon="adjustments", toolbar=["craft.lead", "craft.kerf"])
        ctx.add_workspace("techdb", tool, "Technology", icon="database-import", min_mode="engineer")
        ctx.host.host_add_workspace(f"{ctx.name}.legacy", old, "Legacy")       # the Wave 2 call
    mod = types.ModuleType("shell_test_plugin")
    mod.register = reg
    sys.modules["shell_test_plugin"] = mod
    ctx, _ = register_plugin(win, PluginInfo("shell_test_plugin", "shell_test_plugin"))
    assert ctx is not None
    sh = win.shell
    page = sh.host.pages["process"]
    assert page.isAncestorOf(proc) and page.tabs is not None and page.tabs.count() == 2   # 2nd: a tab
    assert win.show_workspace("shell_test_plugin.process") and sh.host.current == "process"
    page.tabs.setCurrentIndex(1)
    assert proc.isVisible() and win.canvas.isVisible()     # the first contribution kept the canvas
    ids = sh.host.visible_ids()
    assert "shell_test_plugin.techdb" in ids and len(ids) == 8
    assert sh.host.specs["shell_test_plugin.techdb"].shortcut == "Ctrl+7"
    act = win.ribbon.actions["workspace.shell_test_plugin.techdb"]
    assert act.shortcut() == QKeySequence("Ctrl+7")
    win.run("workspace.shell_test_plugin.techdb")
    assert sh.host.current == "shell_test_plugin.techdb" and tool.isVisible()
    assert "shell_test_plugin.legacy" in win.workspaces and win.show_workspace("shell_test_plugin.legacy")
    seen = []
    ctx.on_workspace_changed(seen.append)
    win.show_workspace("design")
    assert seen == ["design"]


# --------------------------------------------------------------------------- palette
def test_palette_finds_runs_and_remembers(win, app):
    sh = win.shell
    entries = sh.entries()
    ids = {e.id for e in entries}
    assert len(entries) >= 256 - 40 and "craft.microjoint" in ids and "hw.upgrade" not in ids
    e = next(e for e in entries if e.id == "craft.microjoint")
    assert e.label == "Micro Joint" and e.workspace == "Process" and e.shortcut == "J"
    assert next(e for e in entries if e.id == "file.save").shortcut == "Ctrl+S"
    sh.open_palette()
    pal = sh.palette
    assert pal.isVisible()
    pal.edit.setText("zoom fit")
    assert pal.matches()[0].entry.id == "view.zoom_fit"
    ran = []
    win.commands.commands["view.zoom_fit"].handler = lambda: ran.append(1)
    pal.run_current()
    assert ran == [1] and not pal.isVisible()
    assert sh.recent.ids[0] == "view.zoom_fit"
    sh.open_palette()
    assert pal.matches()[0].entry.id == "view.zoom_fit" and pal.matches()[0].recent
    pal.close_palette()


def test_palette_says_when_nothing_matches(win, app):
    """Review 2 (features.md §13): "quote" or "label" used to give an empty list and no word."""
    sh = win.shell
    sh.open_palette()
    pal = sh.palette
    for q in ("quote", "zzqxj"):          # "label" matches the label commands since review 2
        pal.edit.setText(q)
        assert pal.matches() == [] and pal.list.count() == 0
        assert pal.empty.isVisibleTo(pal) and not pal.list.isVisibleTo(pal)
        assert "No command matches" in pal.empty.text() and q in pal.empty.text()
        pal.run_current()                       # Enter on no result: nothing runs, no error
        assert pal.isVisible()
    pal.edit.setText("zoom fit")                # a match brings the list back
    assert pal.list.isVisibleTo(pal) and not pal.empty.isVisibleTo(pal) and pal.list.count() > 0
    pal.close_palette()


# --------------------------------------------------------------------------- user modes
def test_user_modes_gate_commands_rail_and_editing(win, app, monkeypatch):
    sh = win.shell
    a = win.ribbon.actions
    assert win.user_mode == "engineer"
    assert not a["hw.upgrade"].isEnabled() and a["craft.lead"].isEnabled()
    assert win.set_user_mode("operator")
    assert sh.host.visible_ids() == ["run", "report", "machine"]
    assert sh.host.current == "run"
    assert not a["craft.lead"].isEnabled() and a["machine.home"].isEnabled() and a["run.start"].isEnabled()
    assert "craft.lead" not in {e.id for e in sh.entries()}
    assert win.property("userMode") == "operator"
    assert not win.layer_panel.isEnabled()
    edited = []
    monkeypatch.setattr("mlaser.ui.dialogs.LayerParamDialog.exec", lambda self: edited.append(1) or 0)
    win.edit_layer(1)
    assert edited == []                                   # no parameter editing
    from mlaser import ops
    win.shell.set_user_mode("engineer")
    win.set_document(ops.demo_document())
    win.set_user_mode("operator")
    n = len(win.document.parts)
    win.canvas.select_parts([0])
    b = win.document.parts[0].bbox()
    win.nudge(1, 0)
    win.delete_selected()
    assert len(win.document.parts) == n and win.document.parts[0].bbox() == b
    assert win.show_workspace("ext_machine.machine")
    assert not win.extension("ext_machine").workspace.settings_panel.editable
    assert win.set_user_mode("service")
    assert a["hw.upgrade"].isEnabled() and win.extension("ext_machine").workspace.settings_panel.editable
    assert win.layer_panel.isEnabled()


def test_pins_protect_switching_up(win, app, monkeypatch):
    sh = win.shell
    assert win.set_user_mode("service")
    sh.modes.set_pin("engineer", "1111")
    sh.modes.set_pin("service", "2222")
    assert win.set_user_mode("operator")
    asked = []
    monkeypatch.setattr("mlaser.ui.shell.dialogs.ask_pin", lambda p, tr, target: asked.append(target) or "0000")
    win._test_no_dialogs = True
    assert not sh.request_mode("engineer") and win.user_mode == "operator" and asked == ["engineer"]
    assert sh.topbar.mode_combo.currentData() == "operator"
    monkeypatch.setattr("mlaser.ui.shell.dialogs.ask_pin", lambda p, tr, target: "1111")
    assert sh.request_mode("engineer") and win.user_mode == "engineer"
    assert sh.topbar.mode_combo.currentData() == "engineer"
    stored = win.config.get("shell.pin.service")
    assert stored.startswith("pbkdf2-sha256$") and "2222" not in stored


# --------------------------------------------------------------------------- themes
@pytest.mark.parametrize("name", ["dark", "high_contrast", "light"])
def test_themes_palette_canvas_and_persistence(win, app, name):
    from mlaser.ui import canvas as C
    from mlaser.ui.shell import theme
    win.run(f"shell.theme_{name}")
    assert win.shell.theme == name and win.config.get("shell.theme") == name
    t = theme.tokens(name)
    assert app.palette().color(QPalette.Window).name() == t["window"]
    assert win.canvas.backgroundBrush().color().name() == t["c_background"]
    assert C.CANVAS_COLORS["selected"] == t["c_selected"]
    rep = theme.contrast_report(name)
    assert rep["text/window"] >= 7.0 and rep["text/base"] >= 7.0 and rep["accent_text/accent"] >= 4.5
    assert rep["muted/window"] >= 4.5
    need = theme.LAYER_MIN_CONTRAST[name]
    assert all(v >= need - 0.01 for k, v in rep.items() if k.startswith("layer"))
    for role in ("unclosed", "selected", "start", "index", "head"):
        assert rep[f"{role}/canvas"] >= 3.0, role
    from mlaser.ui.shell.contrast import distinct
    assert distinct(theme.layer_colors(name)) >= 12.0
    # the stored document colour is data, not theme
    assert C.layer_color(3).name() == C.LAYER_COLORS[3]


def test_theme_restored_in_a_new_window(app, monkeypatch):
    from mlaser.services.config import ConfigService
    from wiring import real_window
    cfg = ConfigService()
    cfg.set("shell.theme", "dark")
    cfg.set("shell.classic_ribbon", True)
    w = real_window(config=cfg)
    try:
        assert w.shell.theme == "dark" and w.shell.classic
        assert w.ribbon.isVisibleTo(w) and not w.shell.host.rail.isVisibleTo(w)
    finally:
        w.shell.set_theme("light", persist=False)
        w._test_no_dialogs = True
        w.close()


# --------------------------------------------------------------------------- classic ribbon
def test_classic_ribbon_toggle_and_no_elided_labels(win, app):
    from mlaser.ui.ribbon import RibbonButton
    sh = win.shell
    win.run("shell.classic_ribbon")
    _pump(app)
    assert sh.classic and win.ribbon.isVisible() and not sh.topbar.isVisible() and not sh.host.rail.isVisible()
    assert win.config.get("shell.classic_ribbon") is True
    assert win.lang_combo.isVisible()
    squeezed = []
    for i in range(win.ribbon.count()):
        win.ribbon.setCurrentIndex(i)
        _pump(app)
        for b in win.ribbon.widget(i).findChildren(RibbonButton):
            if b.isVisible() and b.width() < b.sizeHint().width():
                squeezed.append((win.ribbon.tabText(i), b.text()))
            if b.defaultAction() is not None:
                assert b.toolTip() or b.defaultAction().toolTip()
    assert squeezed == []
    win.run("shell.classic_ribbon")
    _pump(app)
    assert not sh.classic and sh.topbar.isVisible() and sh.host.rail.isVisible()


# --------------------------------------------------------------------------- keys, a11y
def test_keys_and_shortcut_sheet(win, app):
    from mlaser.ui.shell import keymap
    a = win.ribbon.actions
    for cmd, key in keymap.window_keys().items():
        assert cmd in a, cmd
        assert [s.toString() for s in a[cmd].shortcuts()] == [QKeySequence(k).toString()
                                                               for k in keymap.sequences(key)], cmd
        assert a[cmd] in win.actions()
    assert QKeySequence("F5") in a["run.start"].shortcuts() and QKeySequence("F12") in a["run.stop"].shortcuts()
    # the tooltip shows the platform's native key text: "Ctrl+S" on Windows / Linux, "⌘S" on macOS
    assert f"({QKeySequence('Ctrl+S').toString(QKeySequence.NativeText)})" in a["file.save"].toolTip()
    rows = win.shell.shortcut_rows()
    keys = {r[2] for r in rows}
    assert keymap.native_text("Ctrl+K") in keys and any("F1" in k for k in keys) and any("F5" in k for k in keys)
    assert "Space" in keys
    d = win.shell.show_shortcuts()
    assert d.table.rowCount() == len(rows)
    d.filter.setText("undo")
    assert any(not d.table.isRowHidden(r) for r in range(d.table.rowCount()))
    d.close()


def test_accessible_names(win, app):
    sh = win.shell
    for ws in sh.host.visible_ids():
        btn = sh.host.rail.widgetForAction(sh.host.rail_actions[ws])
        assert btn.accessibleName(), ws
    for page in sh.host.pages.values():
        for act in page.toolbar.actions():
            b = page.toolbar.widgetForAction(act)
            if b is not None and isinstance(b, QToolButton):
                assert b.accessibleName(), act.data()
    for w in (sh.topbar.search, sh.topbar.mode_combo, win.lang_combo, win.canvas):
        assert w.accessibleName(), w
    for b in win.safety_ui.run_bar.buttons.values():
        assert b.text()                                   # text, not colour alone


def test_chips_carry_text(win, app):
    sh = win.shell
    win._poll()
    sh._poll()
    chips = sh.topbar.chips
    assert not chips["SIMULATOR"].isHidden() and "SIMULATOR" in chips["SIMULATOR"].text()
    win.ctl.connect()
    assert win.ctl.arm()
    sh._poll()
    assert not chips["ARMED"].isHidden() and "ARMED" in chips["ARMED"].text()
    assert chips["DISARMED"].isHidden()
    win.ctl.raise_fault("test fault")
    sh._poll()
    assert not chips["FAULT"].isHidden() and "test fault" in chips["FAULT"].toolTip()
    win.ctl.reset_fault()


def test_dirty_marker_and_title(win, app, tmp_path):
    from mlaser import ops
    sh = win.shell
    win.set_document(ops.demo_document())
    # the platform's modified marker ([*]: the close-button dot on macOS, "*" elsewhere)
    assert sh.dirty and win.isWindowModified() and "[*]" in win.windowTitle() and "●" in sh.topbar.file.text()
    path = tmp_path / "x.mlz"
    from PySide6.QtWidgets import QFileDialog
    orig = QFileDialog.getSaveFileName
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(path), ""))
    try:
        win.file_save()
    finally:
        QFileDialog.getSaveFileName = orig
    assert not sh.dirty and "x.mlz" in win.windowTitle() and not win.isWindowModified()
    assert win.windowFilePath() == str(path)                      # macOS proxy icon
    assert win.shell.native.recent.paths[0] == str(path)          # File > Open Recent
    win.canvas.select_parts([0])
    win.run("xform.mirror_h")
    assert sh.dirty


# --------------------------------------------------------------------------- layout
@pytest.mark.parametrize("size", [(1280, 800), (1280, 720)])
def test_layout_fits_and_reset(win, app, size):
    win.resize(*size)
    win.shell.fit_docks()
    _pump(app, 10)
    tb = win.shell.topbar
    for child in tb.findChildren(QWidget):
        if child.isVisible() and child.parent() is tb:
            g = child.geometry()
            assert g.left() >= 0 and g.right() <= tb.width(), child.objectName()
    rb = win.safety_ui.run_bar
    for key in ("start", "hold", "stop", "frame"):
        r = rb.buttons[key].geometry()
        assert r.right() <= rb.width() and rb.buttons[key].isVisible()
    assert win.canvas.width() >= 480                     # the canvas keeps a usable width
    win.right_dock.setFloating(True)
    win.left_dock.hide()
    win.run("shell.reset_layout")
    _pump(app)
    assert not win.right_dock.isFloating() and not win.left_dock.isHidden()
    assert win.shell.host.current == "design"


def test_welcome_sheet(win, app):
    d = win.shell.show_welcome()
    assert "hw.connect_sim" in d.buttons and "shell.import_params" in d.buttons and "app.import_language" in d.buttons
    d.again.setChecked(False)
    d.close()
    assert win.config.get("shell.welcome_done") is True
    assert not win.shell.maybe_show_welcome()


def test_import_params(win, app, tmp_path):
    from mlaser.services.config import ConfigService
    src = ConfigService(tmp_path / "src.json", load=False)
    key = next(k for k, s in src.schema.items() if s.file == "ManuPara.xml" and s.kind == "float"
               and s.max is not None and s.min is not None and s.max > s.min)
    s = src.setting(key)
    val = s.min + (s.max - s.min) / 3
    src.set(key, val)
    src.export_dat(tmp_path / "dat")
    n = win.shell.import_params(str(tmp_path / "dat"))
    assert n >= 1 and win.config.get(key) == pytest.approx(val, rel=1e-3)


def test_focus_order(win, app):
    sh = win.shell
    chain = sh.focus_chain()
    tb = sh.topbar
    assert chain[:3] == [tb.search, tb.mode_combo, win.lang_combo]
    assert win.canvas in chain and chain.index(win.canvas) < chain.index(win.safety_ui.run_bar.buttons["start"])
    # Qt's own chain follows it: from the search box, Tab reaches the user-mode box next
    nxt = tb.search.nextInFocusChain()
    while nxt is not None and not (nxt.focusPolicy() & Qt.TabFocus and nxt.isVisible()):
        nxt = nxt.nextInFocusChain()
    assert nxt is tb.mode_combo
