"""The Settings window (mlaser.ui.settings_dialog) in the real window, offscreen: opening it,
search, edit, persistence, reset, validation, user-mode gating, one home per value, platform
conventions, live apply of a representative set, and the responsive layout (no horizontal
scrolling at any width down to the window's minimum, the sidebar collapsing to a drop-down, the
labels going above the fields, and recovering when the window grows again)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel  # noqa: E402

from mlaser.services import settings_catalog as C  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def win(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import real_window
    w = real_window(user_mode="engineer")
    yield w
    d = getattr(w, "_settings_window", None)
    if d is not None:
        d.close()
    w.close()


def dialog(win, instant=True):
    from mlaser.ui.settings_dialog import SettingsDialog
    d = SettingsDialog(win, instant=instant)
    d.show()
    QApplication.processEvents()
    return d


def pump(n=5):
    for _ in range(n):
        QApplication.processEvents()


# --------------------------------------------------------------------------- opening
def test_app_options_and_app_settings_open_the_same_window(win):
    win.run("app.options")
    d = win._settings_window
    assert d.isVisible()
    win.run("app.settings")
    assert win._settings_window is d
    # no generic parameter grid any more: every row is a catalogue entry or a plugin's setting
    assert set(d.rows) <= set(C.BY_KEY) | {k for v in C.plugin_settings(win.config.schema).values()
                                           for k in (s.key for s in v)} | {
        k for k in win.config.schema if k.startswith("units.")}
    assert not [k for k in d.rows if k.startswith(("SP.", "SOP.", "GRP.", "IGP.", "MP.", "MC."))]


@pytest.mark.parametrize("platform,title,menu,instant", [
    ("darwin", "Settings", "Settings…", True), ("win32", "Options", "Options…", False),
    ("linux", "Preferences", "Preferences…", False)])
def test_platform_names_and_apply_convention(win, platform, title, menu, instant):
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QDialogButtonBox

    from mlaser.ui import settings_dialog as S
    assert S.menu_label(win.tr_, platform) == menu
    assert S.instant_apply(platform) is instant
    d = S.SettingsDialog(win, platform=platform)
    assert d.windowTitle() == title
    assert (d.buttons.button(QDialogButtonBox.Apply) is None) is instant
    assert (d.buttons.button(QDialogButtonBox.Close) is not None) is instant
    # the native menu bar (shell/menubar.py) gives app.settings the PreferencesRole; Ctrl/Cmd+,
    # is bound once
    from mlaser.ui.shell import menu_spec as spec
    mb = win.shell.menu_ctl
    assert mb.settings_cmd() == "app.settings"
    assert spec.roles(spec.MACOS, mb.settings_cmd())["app.settings"] == spec.PREFERENCES
    if mb.plat == spec.MACOS:
        assert win.ribbon.actions["app.settings"].menuRole() == QAction.PreferencesRole
    from PySide6.QtGui import QKeySequence
    comma = [c for c, a in win.ribbon.actions.items() if a.shortcut() == QKeySequence("Ctrl+,")]
    assert comma == ["app.settings"], comma
    d.close()


def test_operator_may_open_settings(win):
    from mlaser.services import usermodes as um
    assert um.allowed("app.settings", um.OPERATOR) and um.allowed("app.options", um.OPERATOR)


# --------------------------------------------------------------------------- search
def test_search_filters_rows_and_pages(win):
    d = dialog(win)
    d.search.setText("remnant")
    pump()
    shown = d.matches()
    assert "plugin.run.remnant_min_width" in shown and "display.show_bbox" not in shown
    assert not d.sidebar.item(C.PAGE_IDS.index("run")).isHidden()
    assert d.sidebar.item(C.PAGE_IDS.index("canvas")).isHidden()
    d.search.setText("nudge")
    pump()
    assert d.current_page().page_id == "canvas"          # jumps to the first page with a match
    d.search.setText("zzzz-no-such-setting")
    pump()
    assert d.current_page().empty.isVisible()
    d.search.setText("")
    pump()
    assert all(not d.sidebar.item(i).isHidden() for i in range(d.sidebar.count()))
    d.close()


# --------------------------------------------------------------------------- edit / persist / reset
def test_edit_persists_marks_modified_and_resets(win):
    d = dialog(win)
    r = d.rows["costing.machine_per_h"]
    assert r.marker.isHidden() and r.reset_btn.isHidden()
    r.editor.setValue(85.0)
    r.editor.editingFinished.emit()
    assert win.config.get("costing.machine_per_h") == 85.0
    assert not r.marker.isHidden() and not r.reset_btn.isHidden()
    data = json.loads(win.config.path.read_text("utf-8"))        # saved at once
    assert data["values"]["costing.machine_per_h"] == 85.0
    r.reset_btn.click()
    assert not win.config.is_set("costing.machine_per_h") and r.marker.isHidden()
    # reset page
    d.show_page("canvas")
    d.rows["display.show_bbox"].editor.setChecked(True)
    d.rows["display.show_move_path"].editor.setChecked(True)
    d.reset_page()
    assert not win.config.is_set("display.show_bbox") and not win.config.is_set("display.show_move_path")
    d.close()


def test_validation_refuses_out_of_range(win):
    d = dialog(win)
    d._commit("plugin.run.monitor_port", 80)              # below 1024
    r = d.rows["plugin.run.monitor_port"]
    assert not r.error.isHidden() and "outside" in r.error.text()
    assert win.config.get("plugin.run.monitor_port") == 8765
    d._commit("plugin.run.monitor_port", 9000)
    assert r.error.isHidden() and win.config.get("plugin.run.monitor_port") == 9000
    d.close()


def test_staged_apply_and_cancel(win):
    """Windows / Linux: changes wait for OK / Apply; Cancel discards them."""
    d = dialog(win, instant=False)
    d.rows["display.show_bbox"].editor.setChecked(True)
    assert not win.config.is_set("display.show_bbox")
    assert not d.rows["display.show_bbox"].marker.isHidden()     # staged: shown as modified
    d.reject()
    assert not win.config.is_set("display.show_bbox")
    assert d.rows["display.show_bbox"].editor.isChecked() is False
    d.show()
    d.rows["display.show_bbox"].editor.setChecked(True)
    assert d.apply() == 1
    assert win.config.get("display.show_bbox") is True and win.canvas.options.show_bbox is True
    d.close()


# --------------------------------------------------------------------------- gating, one home
def test_operator_mode_can_only_change_operator_settings(win):
    d = dialog(win)
    assert win.set_user_mode("operator")
    pump()
    assert d.rows["display.show_bbox"].editor.isEnabled()
    assert d.rows["shell.theme"].editor.isEnabled()
    assert not d.rows["costing.machine_per_h"].editor.isEnabled()
    assert not d.rows["plugin.ext_options.kerf_clipper"].editor.isEnabled()
    d._commit("costing.machine_per_h", 10.0)               # refused
    assert not win.config.is_set("costing.machine_per_h")
    d.reset_setting("costing.machine_per_h")
    assert win.set_user_mode("engineer")
    pump()
    assert d.rows["costing.machine_per_h"].editor.isEnabled()
    d.close()


def test_values_with_another_home_are_read_only_with_a_link(win, monkeypatch):
    d = dialog(win)
    r = d.rows["plugin.ext_notify.smtp_host"]
    assert isinstance(r.editor, QLabel) and r.editor.isEnabled()          # readable, not greyed
    assert isinstance(d.rows["plugin.ext_automation.port"].editor, QLabel)
    page = d.pages["notify"]
    btns = [b for b in page.findChildren(type(d.reset_page_btn)) if b.property("command") == "notify.settings"]
    assert len(btns) == 1
    ran = []
    monkeypatch.setattr(win, "run", lambda cmd: ran.append(cmd))
    btns[0].click()
    assert ran == ["notify.settings"]
    # every editable row has no other editor: no entry with a home is editable
    for key, row in d.rows.items():
        e = C.BY_KEY.get(key)
        if e is not None and e.home:
            assert isinstance(row.editor, QLabel), key
    # safety values are linked, never shown
    assert not [k for k in d.rows if k.startswith("safety.")]
    assert any(b.property("command") == "machine.safety_io" for b in d.pages["machine"].findChildren(
        type(d.reset_page_btn)))
    d.close()


# --------------------------------------------------------------------------- live apply
def test_live_apply_display_theme_and_canvas(win):
    d = dialog(win)
    d.rows["display.show_bbox"].editor.setChecked(True)
    assert win.canvas.options.show_bbox is True
    assert win.ribbon.actions["show.bbox"].isChecked()
    win.run("show.move_path")                              # the View toggle writes the preference
    assert win.config.get("display.show_move_path") is True
    assert d.rows["display.show_move_path"].editor.isChecked()            # the window follows
    combo = d.rows["shell.theme"].editor
    combo.setCurrentIndex(combo.findData("dark"))
    combo.activated.emit(combo.currentIndex())
    assert win.shell.theme == "dark" and win.config.get("shell.theme") == "dark"
    d.close()


def test_live_apply_tools_nest_autosave_and_engines(win):
    d = dialog(win)
    d._commit("plugin.ext_options.kerf_clipper", True)
    assert win.cam_params.kerf_fallback == "clipper"
    d._commit("plugin.ext_options.kerf_clipper", False)
    assert win.cam_params.kerf_fallback is None
    # the nest defaults are remembered by the Nest workspace (their home), not set here
    assert "nest.sheet_width" not in d.rows
    win.config.set("nest.sheet_width", 2000.0)
    from mlaser.ui.ext_nest import default_sheet, new_job
    assert default_sheet(win.config).width == 2000.0
    win.config.set("nest.spacing", 6.0)
    assert new_job(win.config).params.spacing == 6.0
    proj = win.extension("ext_project")
    d._commit("plugin.ext_project.autosave_interval_s", 30)
    assert proj.timer.interval() == 30000 and proj.timer.isActive()
    d._commit("plugin.ext_project.autosave_interval_s", 0)
    assert not proj.timer.isActive()
    d.close()


def test_remembered_and_read_only_values_reach_their_consumers(win):
    """Values with another home (nudge step, clean-up, per-job dialogs) still apply live when
    their home changes them."""
    draw = win.extension("ext_draw")
    win.config.set("design.nudge_step", 2.5)
    win.config.set("design.nudge_auto", False)
    assert draw.fine.step == 2.5 and draw.step_for(1, 0) == 2.5
    cam = win.extension("ext_cam")
    win.config.set("import.small_gate", 0.7)
    assert cam.cleanup.small_gate == 0.7
    # per-job values (leads, the cutting order) are not settings: no key for them
    assert win.config.setting("cam.lead_in_length") is None and win.config.setting("cam.sort_mode") is None


def test_cleanup_on_open_and_last_file(win, tmp_path):
    from mlaser.model import Contour, Document, Line, Part
    doc = Document()
    sq = [Line((0, 0), (10, 0)), Line((10, 0), (10, 10)), Line((10, 10), (0, 10)), Line((0, 10), (0, 0))]
    doc.parts = [Part(contours=[Contour(segments=list(sq), closed=True)]),
                 Part(contours=[Contour(segments=list(sq), closed=True)]),            # a duplicate
                 Part(contours=[Contour(segments=[Line((50, 50), (50.01, 50))], closed=False)])]  # dust
    win.set_document(doc)
    win.hooks.emit("document_imported", str(tmp_path / "a.dxf"), win.document)
    assert win.config.get("app.last_file").endswith("a.dxf")
    assert sum(len(p.contours) for p in win.document.parts) == 3           # off: untouched
    win.config.set("import.cleanup_on_open", True)
    win.hooks.emit("document_imported", str(tmp_path / "b.dxf"), win.document)
    assert sum(len(p.contours) for p in win.document.parts) < 3


def test_language_preference_follows_and_persists(win):
    win.set_language("en")
    assert win.config.get("shell.language") == "en"
    from mlaser.ui.settings_apply import resolve_language
    assert resolve_language(win.tr_, "") in {li.code for li in win.tr_.available()}
    assert resolve_language(win.tr_, "xx") == "en"


# --------------------------------------------------------------------------- responsive
def _no_horizontal_overflow(d):
    sc = d.stack.currentWidget()
    page = sc.page
    vp = sc.viewport().width()
    assert page.width() <= vp + 1, (page.width(), vp)
    for r in page.rows:
        if r.isHidden():
            continue
        right = r.editor.mapTo(page, r.editor.rect().topRight()).x()
        assert right <= page.width(), (r.key, right, page.width())


@pytest.mark.parametrize("page", ["cam", "import", "run", "general"])
def test_responsive_sweep_never_scrolls_sideways(win, page):
    d = dialog(win)
    d.show_page(page)
    lo = d.minimumWidth()
    seen_narrow = seen_wide = seen_stacked = False
    widths = list(range(lo, 2561, 120)) + list(range(2560, lo - 1, -160))   # shrink after growing
    for wdt in widths:
        d.resize(wdt, 700)
        pump(3)
        _no_horizontal_overflow(d)
        seen_narrow |= d.narrow
        seen_wide |= not d.narrow
        seen_stacked |= any(r.stacked for r in d.current_page().rows)
    assert seen_narrow and seen_wide and (seen_stacked or page == "general")   # short labels never need to
    d.resize(2000, 900)                                     # grows back: wide layout again
    pump(3)
    assert not d.narrow and d.sidebar.isVisible()
    assert not any(r.stacked for r in d.current_page().rows)
    d.close()


def test_a_page_shown_wide_reflows_when_the_window_shrinks(win):
    """A page laid out wide (side by side) could not shrink below that layout's minimum: its
    rows reflowed from the page's own width, which the scroll area never made narrower than
    that minimum, so after Import was shown wide and the window shrank to its minimum the page
    scrolled sideways (Windows CI: 484 > 454; here by switching pages)."""
    d = dialog(win)
    for first in ("cam", "general", "run"):
        d.show_page(first)
        d.resize(1400, 700)
        pump(3)
        d.show_page("import")
        pump(3)
        d.resize(d.minimumWidth(), 700)
        pump(3)
        _no_horizontal_overflow(d)
    d.close()


def test_larger_font_still_fits(win):
    """A larger system font: the thresholds come from the font, not from pixel constants."""
    from PySide6.QtGui import QFont
    from mlaser.ui.settings_dialog import SettingsDialog
    d = SettingsDialog(win, instant=True)
    f = QFont(d.font())
    f.setPointSizeF(f.pointSizeF() * 1.6)
    d.setFont(f)
    d.show()
    d.show_page("cam")
    for wdt in (d.minimumSizeHint().width(), 900, 1400, 700):
        d.resize(max(wdt, d.minimumWidth()), 800)
        pump(3)
        _no_horizontal_overflow(d)
    d.close()


def test_narrow_window_uses_a_page_drop_down(win):
    d = dialog(win)
    d.resize(d.minimumWidth(), 700)
    pump()
    assert d.narrow and d.page_combo.isVisible() and not d.sidebar.isVisible()
    d.page_combo.setCurrentIndex(C.PAGE_IDS.index("costing"))
    assert d.current_page().page_id == "costing"
    d.close()


def test_checkbox_carries_its_label_and_fields_line_up(win):
    d = dialog(win)
    d.resize(1400, 800)
    d.show_page("import")
    pump()
    r = d.rows["import.cleanup_on_open"]
    assert isinstance(r.editor, QCheckBox) and r.editor.text()
    page = d.current_page()
    xs = {r.editor.mapTo(page, r.editor.rect().topLeft()).x() for r in page.rows if not r.isHidden() and not r.stacked}
    assert len(xs) == 1, xs                                 # one field column per page
    d.close()


# --------------------------------------------------------------------------- IA: one home
def test_search_finds_values_that_live_elsewhere(win, monkeypatch):
    d = dialog(win)
    d.search.setText("slat")
    pump()
    items = [b for b in d.elsewhere.findChildren(type(d.reset_page_btn)) if b.property("setting")]
    assert d.elsewhere.isVisible() and any(b.property("setting") == "bed.slat_pitch" for b in items)
    assert all(b.property("setting") not in d.rows for b in items)        # navigation, never an editor
    went = []
    monkeypatch.setattr(win, "machine_setup", lambda: went.append("setup") or True)
    next(b for b in items if b.property("setting") == "bed.slat_pitch").click()
    assert went == ["setup"]
    d.search.setText("")
    pump()
    assert not d.elsewhere.isVisible()
    d.close()


def test_every_row_is_a_settings_home_key(win):
    d = dialog(win)
    for key, row in d.rows.items():
        s = win.config.schema[key]
        e = C.BY_KEY.get(key)
        if e is not None and e.home:
            continue                      # a read-only summary with "Edit in …"
        assert s.home == "settings", (key, s.home)
    # no registry row of the original's tables, no machine value is editable here
    assert not [k for k in d.rows if win.config.schema[k].source == "registry"]
    d.close()


def test_cam_tool_last_used_values_persist_and_reset(win):
    cam = win.extension("ext_cam")
    cam.array.rows = 7
    assert cam.ask("mf79", cam.array, [("rows", "pd332", "int", (1, 10000))])
    assert win.config.get("cam.tool.array")["rows"] == 7
    d = dialog(win)
    r = d.rows["cam.tool.array"]
    assert "rows 7" in r.editor.text() and not r.reset_btn.isHidden()
    r.reset_btn.click()
    assert not win.config.is_set("cam.tool.array")
    assert cam.array.rows == type(cam.array)().rows                        # the tool's default again
    d.close()


def test_cam_tool_values_survive_a_restart(app, tmp_path, monkeypatch):
    from wiring import real_window
    w = real_window()
    w.extension("ext_craft").bridge_p.width = 4.5
    w.extension("ext_craft").ask("mf0", w.extension("ext_craft").bridge_p, [])
    w.config.save()
    path = w.config.path
    w.close()
    from mlaser.services.config import ConfigService
    w2 = real_window(config=ConfigService(path))
    assert w2.extension("ext_craft").bridge_p.width == 4.5
    w2.close()


def test_costing_is_one_model(win, monkeypatch):
    from mlaser.services.costing import rates_from_config
    win.config.set("costing.machine_per_h", 60.0)
    win.config.set("costing.currency", "CHF")
    assert rates_from_config(win.config).machine_per_h == 60.0
    rep = win.extension("report")
    rep = rep[0] if isinstance(rep, tuple) else rep
    assert rep.ledger.rates.machine_per_h == 60.0 and rep.ledger.rates.currency == "CHF"
    win.run("process.rates")                               # the rates' home is Settings › Costing
    assert win._settings_window.current_page().page_id == "costing"
    win._settings_window.close()


def test_hw_options_and_app_plugins_navigate_to_their_homes(win, monkeypatch):
    assert win.set_user_mode("service")                    # Machine setup is a Service-mode home
    win.run("hw.options")
    ws = win.extension("ext_machine").workspace
    assert ws.section() == "settings"
    from mlaser.ui import plugin_manager as PM
    opened = []
    monkeypatch.setattr(PM.PluginManagerDialog, "exec", lambda self: opened.append(1) or 0)
    win.set_user_mode("service")
    win.run("app.plugins")
    assert opened == [1]


def test_users_page_shows_mode_and_pins(win):
    d = dialog(win)
    assert "Engineer" in d.users.text() or "engineer" in d.users.text().lower()
    links = [b.property("command") for b in d.pages["users"].findChildren(type(d.reset_page_btn))]
    assert "shell.user_mode" in links and "shell.set_pins" in links
    d.close()


def test_settings_editors_are_the_only_editors_of_their_values(win, app):
    """mlaser.ui.homes: a cfg:<key> the Settings window edits has no editor anywhere else."""
    from mlaser.ui import homes
    d = dialog(win)
    for ws in ["design", "process", "nest", "run", "report", "machine"]:
        win.show_workspace(ws)
        app.processEvents()
    mine = {w.property(homes.PROP_VALUE) for w in homes.editors(d)}
    assert "cfg:costing.machine_per_h" in mine and "cfg:shell.language" in mine
    elsewhere = [w for w in homes.editors(win) if w.property(homes.PROP_VALUE) in mine and d not in _ancestors(w)]
    assert not elsewhere, [w.property(homes.PROP_VALUE) for w in elsewhere]
    # every mirror and editor of the window has a home; search hits that live elsewhere go there
    for w in homes.editors(d) + homes.editors(d, role=homes.MIRROR):
        assert homes.home_of(w.property(homes.PROP_VALUE)) is not None
    assert homes.home_of("cfg:bed.slat_pitch").workspace == "machine"
    assert homes.home_of("cfg:MC.JogFastSpeed").workspace == "machine"
    d.close()


def _ancestors(w):
    out = []
    p = w.parentWidget()
    while p is not None:
        out.append(p)
        p = p.parentWidget()
    return out


def test_edit_in_opens_the_settings_window_at_the_value(win):
    from mlaser.ui import homes
    homes.edit_in(win, "cfg:costing.per_pierce")
    d = win._settings_window
    assert d.isVisible() and d.current_page().page_id == "costing"
    d.close()
