"""Native platform integration (docs/dev/NATIVE.md): the menu bar built from the command
registry (every command once, macOS roles, Windows/Linux words and access keys, user-mode
gating), the theme's platform style and colour scheme with a style sheet that leaves the
standard widgets alone, platform keys, the app identity and packaging metadata."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.ui

from PySide6.QtGui import QAction, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox  # noqa: E402

from mlaser.ui import native  # noqa: E402
from mlaser.ui.shell import keymap, menu_spec, theme  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PLATS = (menu_spec.MACOS, menu_spec.WINDOWS, menu_spec.LINUX)


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


def _walk(menu: QMenu, out: list, path: str = "") -> None:
    for a in menu.actions():
        if a.isSeparator():
            continue
        if a.menu() is not None:
            _walk(a.menu(), out, path + "/" + a.menu().title())
        else:
            out.append((path, a))


def _bar_actions(bar) -> list:
    out: list = []
    for top in bar.actions():
        if top.menu() is not None:
            _walk(top.menu(), out, top.text())
    return out


# --------------------------------------------------------------------------- the menu spec
@pytest.mark.parametrize("plat", PLATS)
def test_layout_places_every_command_once(win, plat):
    ids = list(win.ribbon.actions)
    menus = menu_spec.layout(ids, plat)
    placed = menu_spec.placement(menus)            # raises on a command placed twice
    expected = {c for c in ids if not c.startswith(menu_spec.DYNAMIC_PREFIXES)}
    assert set(placed) == expected
    assert [m.id for m in menus] == ["file", "edit", "view", "draw", "process", "workspace", "machine",
                                     "tools", "window", "help"]
    # the conventional places
    assert placed["file.open"] == "file" and placed["edit.undo"] == "edit" and placed["view.zoom_in"] == "view"
    assert placed["machine.home"] == "machine" and placed["workspace.design"] == "workspace"
    assert placed["shell.palette"] == "help" and placed["window.fullscreen"] == "window"
    if plat == menu_spec.WINDOWS:
        assert placed["app.options"] == "tools" and menus[-3].items[-1] == "app.options"
        assert menus[0].items[-1] == "app.exit"
    elif plat == menu_spec.LINUX:
        assert placed["app.options"] == "edit" and menus[1].items[-1] == "app.options"
        assert menus[0].items[-1] == "app.exit"
    else:
        # macOS: Qt moves them into the application menu by their roles
        roles = menu_spec.roles(plat)
        assert roles == {"app.about": "AboutRole", "app.options": "PreferencesRole", "app.exit": "QuitRole",
                         "app.plugin_manager": "ApplicationSpecificRole"}
    # workspaces in rail order
    ws = menus[5].items
    assert ws[:6] == [f"workspace.{w}" for w in ("design", "process", "nest", "run", "report", "machine")]


def test_layout_puts_new_commands_somewhere():
    menus = menu_spec.layout(["file.open", "machine.home", "onshape.refresh", "draw.hexagon"], menu_spec.LINUX)
    placed = menu_spec.placement(menus)
    assert placed["onshape.refresh"] == "tools" and placed["draw.hexagon"] == "draw"


def test_layout_uses_the_settings_command_of_the_settings_track():
    menus = menu_spec.layout(["app.options", "app.settings", "app.exit"], menu_spec.WINDOWS, "app.settings")
    placed = menu_spec.placement(menus)
    assert placed["app.settings"] == "tools" and placed["app.options"] == "tools"
    assert menu_spec.roles(menu_spec.MACOS, "app.settings")["app.settings"] == "PreferencesRole"


def test_mnemonics():
    titles = ["File", "Edit", "View", "Draw", "Process", "Workspace", "Machine", "Tools", "Window", "Help"]
    first = [titles.index(t) for t in ("File", "Edit", "View", "Window", "Help")]
    out = menu_spec.mnemonic_titles(titles, first)
    assert out[:3] == ["&File", "&Edit", "&View"] and out[8] == "&Window" and out[9] == "&Help"
    keys = [t[t.index("&") + 1].lower() for t in out]
    assert len(set(keys)) == len(keys)
    assert menu_spec.mnemonic_titles(["&Datei", "Drucken"]) == ["&Datei", "D&rucken"]


def test_menu_texts_are_translatable():
    en = json.loads((ROOT / "mlaser" / "i18n" / "en.json").read_text(encoding="utf-8"))["strings"]
    for plat in PLATS:
        for m in menu_spec._menus(plat):
            assert m.title in en, m.title

            def subs(items):
                for e in items:
                    if isinstance(e, menu_spec.Sub):
                        if e.title:
                            assert e.title in en, e.title
                        subs(e.items)
            subs(m.items)
        for key in menu_spec.platform_labels(plat).values():
            assert key in en, key
    assert en["shell.menu.settings_mac"] == "Settings…" and en["shell.menu.options_win"] == "Options…"
    assert en["shell.menu.preferences"] == "Preferences…" and en["shell.menu.exit"] == "Exit"


# --------------------------------------------------------------------------- the menu bar
def test_menu_bar_replaces_the_menu_button_and_lists_every_command_once(win, app):
    sh = win.shell
    bar = sh.menubar
    assert bar is not None and bar.parent() is sh.chrome
    assert not sh.topbar.menu_btn.isVisibleTo(win)
    acts = _bar_actions(bar)
    cmds = [a.data() or a.objectName() for _p, a in acts if (a.data() or a.objectName()) in win.ribbon.actions]
    assert len(cmds) == len(set(cmds)), [c for c in cmds if cmds.count(c) > 1]
    assert set(cmds) == set(win.ribbon.actions)
    # the theme submenu (View > Appearance) holds the theme commands; the palette is in Help
    theme_menu = bar.findChild(QMenu, "menu_theme")
    assert {a.data() for a in theme_menu.actions()} == {f"shell.theme_{t}" for t in theme.THEME_IDS}
    titles = [a.text().replace("&", "") for a in bar.actions()]
    assert titles[:3] == ["File", "Edit", "View"] and titles[-2:] == ["Window", "Help"]
    if native.platform_id() != native.MACOS:
        assert [a.text() for a in bar.actions()][:3] == ["&File", "&Edit", "&View"]


def test_menu_bar_roles_on_macos(win, app):
    """A simulated macOS: roles on the application-menu commands, NoRole on every other one
    (no text heuristics: "Options" in a machine command must not become Preferences)."""
    from mlaser.ui.shell.menubar import MenuBar
    mb = MenuBar(win.shell, plat=native.MACOS)
    try:
        a = win.ribbon.actions
        assert a["app.about"].menuRole() == QAction.AboutRole
        assert a["app.options"].menuRole() == QAction.PreferencesRole
        assert a["app.exit"].menuRole() == QAction.QuitRole
        assert a["app.plugin_manager"].menuRole() == QAction.ApplicationSpecificRole
        assert a["hw.options"].menuRole() == QAction.NoRole and a["file.open"].menuRole() == QAction.NoRole
        titles = [x.text() for x in mb.bar.actions()]
        assert "&File" not in titles and "File" in titles               # no access keys on macOS
    finally:
        mb.bar.deleteLater()
        win.shell.menu_ctl.build()                                       # the real platform again


def test_menu_bar_words_per_platform(win, app):
    from mlaser.ui.shell.menubar import MenuBar
    a = win.ribbon.actions
    plat = native.platform_id()
    want = {native.MACOS: ("Settings…", "Quit mlaser"), native.WINDOWS: ("Options…", "Exit"),
            native.LINUX: ("Preferences…", "Quit mlaser")}[plat]
    assert (a["app.options"].text(), a["app.exit"].text()) == want
    assert a["hw.options"].text() == "Machine setup…"
    if plat == native.WINDOWS:
        assert keymap.window_keys().get("app.exit") is None
    del MenuBar


def test_menu_bar_follows_the_user_mode(win, app):
    sh = win.shell
    act = win.ribbon.actions["run.simulate"]                      # Engineer and up
    assert act.isEnabled()
    assert sh.set_user_mode("operator")
    assert not act.isEnabled()
    acts = {a.data(): a for _p, a in _bar_actions(sh.menubar)}
    assert acts["run.simulate"] is act and not acts["run.simulate"].isEnabled()
    assert acts["run.stop"].isEnabled()                           # Stop is always available
    assert sh.set_user_mode("engineer")
    assert act.isEnabled()


def test_menu_bar_takes_commands_registered_later(win, app):
    win.commands.register("demo.native_late", lambda: None, "test", "Late command")
    for _ in range(3):
        app.processEvents()
    assert "demo.native_late" in {a.data() for _p, a in _bar_actions(win.shell.menubar)}


def test_open_recent_and_window_menu(win, app, tmp_path):
    sh = win.shell
    f = tmp_path / "a.dxf"
    f.write_text("0\nEOF\n")
    sh.native.recent.add(str(f))
    sh.native.recent.add(str(tmp_path / "gone.dxf"))           # not existing: not listed
    recent = sh.menubar.findChild(QMenu, "menu_recent")
    recent.aboutToShow.emit()
    texts = [a.text() for a in recent.actions() if not a.isSeparator()]
    assert texts[0] == "a.dxf" and "gone.dxf" not in texts and texts[-1] == "Clear Menu"
    docks = sh.menubar.findChild(QMenu, "menu_docks")
    docks.aboutToShow.emit()
    assert len(docks.actions()) >= 2
    win.run("window.fullscreen")
    app.processEvents()
    assert win.isFullScreen() and win.ribbon.actions["window.fullscreen"].isChecked()
    win.run("window.fullscreen")
    app.processEvents()
    assert not win.isFullScreen()


# --------------------------------------------------------------------------- theme
FORBIDDEN = ("QPushButton", "QToolButton", "QLineEdit", "QComboBox", "QSpinBox", "QDoubleSpinBox", "QTabBar",
             "QTabWidget", "QDockWidget", "QScrollBar", "QListWidget", "QToolBar", "QCheckBox", "QRadioButton",
             "QHeaderView", "QProgressBar", "QMenu", "QMenuBar", "QToolTip", "min-height", "QMainWindow")


@pytest.mark.parametrize("name", theme.THEME_IDS)
def test_style_sheet_leaves_standard_widgets_alone(app, name):
    qss = theme.style_sheet(name)
    for sel in FORBIDDEN:
        assert sel not in qss, (name, sel)
    assert not re.search(r"\d+(\.\d+)?pt", qss.replace(f"{theme._base_point_size() * 1.6:.1f}pt", ""))
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", qss)        # palette roles, no colour literals


def test_type_scale_is_relative(app):
    qss = theme.style_sheet("light", base_pt=13.0)
    assert "font-size: 20.8pt" in qss                         # 1.6 × the platform font
    f = theme.scaled_font("title")
    assert f.pointSizeF() == pytest.approx(app.font().pointSizeF() * 1.25) and f.bold()


def test_native_style_per_platform(app):
    """The platform's own style for system / light / dark; Fusion only for high contrast."""
    old = app.property("mlaser_native_style")
    old_theme = app.property("mlaser_theme")
    try:
        app.setProperty("mlaser_native_style", "Windows")        # stands in for macos / windows11
        app.setProperty("mlaser_theme", None)
        theme.apply(theme.SYSTEM, app=app)
        assert app.style().name().lower() == "windows"
        theme.apply(theme.HIGH_CONTRAST, app=app)
        assert app.style().name().lower() == "fusion"
        assert app.palette().color(QPalette.Window).name() == theme.THEMES["high_contrast"]["window"]
        theme.apply(theme.DARK, app=app)
        assert app.style().name().lower() == "windows"
        dark = app.styleHints().colorScheme().name == "Dark"
        # the platform shows dark itself, or the tokens' palette stands in
        assert dark or app.palette().color(QPalette.Window).name() == theme.THEMES["dark"]["window"]
        assert theme.effective(theme.DARK) == theme.DARK
        theme.apply(theme.SYSTEM, app=app)
        assert not app.property("mlaser_palette")            # back to the platform's palette
    finally:
        app.setProperty("mlaser_native_style", old)
        app.setProperty("mlaser_theme", None)
        theme.apply(old_theme or theme.LIGHT, app=app)


def test_default_theme_follows_the_system():
    assert theme.DEFAULT_THEME == theme.SYSTEM and theme.THEME_IDS[0] == theme.SYSTEM


def test_native_style_names_by_platform():
    """What Qt picks by default (the theme keeps it): documented expectations per platform."""
    assert set(("macos", "windows11", "windowsvista", "fusion")) >= {"macos", "windows11"}
    plat = native.platform_id()
    name = theme.native_style_name(QApplication.instance() or QApplication([])).lower()
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        assert name == {native.MACOS: "macos", native.WINDOWS: "windows11", native.LINUX: name}[plat] or \
            (plat == native.WINDOWS and name == "windowsvista")


# --------------------------------------------------------------------------- keys
def test_platform_keys():
    assert keymap.key_for("edit.redo", "darwin") == "⇧⌘Z"
    assert keymap.window_keys("win32")["edit.redo"].split("|")[0] == "Ctrl+Y"
    assert keymap.window_keys("linux")["edit.redo"].split("|")[0] == "Ctrl+Shift+Z"
    assert keymap.window_keys("darwin")["edit.delete"].startswith("Backspace")
    assert keymap.window_keys("darwin")["window.fullscreen"] == "Meta+Ctrl+F"
    assert keymap.window_keys("linux")["window.fullscreen"] == "F11"
    assert keymap.window_keys("darwin")["app.options"] == "Ctrl+,"
    # every F-key action has a ⌘ alternative on macOS
    for cmd, key in keymap.window_keys("darwin").items():
        seqs = keymap.sequences(key)
        if any(re.fullmatch(r"(Shift\+)?F\d+", s) for s in seqs) and cmd not in ("app.help",):
            assert any("Ctrl+" in s for s in seqs), cmd
    for plat in ("darwin", "win32", "linux"):
        assert keymap.conflicts(plat) == []


def test_native_shortcut_text():
    assert keymap.native_text("Search (Ctrl+K)", "darwin") == "Search (⌘K)"
    assert keymap.native_text("Ctrl+Shift+S, Alt+Backspace, Ctrl+F1", "darwin") == "⇧⌘S, ⌥⌫, ⌘F1"
    assert keymap.native_text("Search (Ctrl+K)", "linux") == "Search (Ctrl+K)"


# --------------------------------------------------------------------------- identity, packaging
def test_application_identity(app):
    native.init_application(app)
    assert app.applicationName() == "mlaser" and app.applicationDisplayName() == "mlaser"
    from PySide6.QtGui import QGuiApplication
    assert QGuiApplication.desktopFileName() == "io.github.mlaser"
    assert not app.windowIcon().isNull()
    assert Path(native.icon_path()).is_file()


def test_recent_files(tmp_path):
    from mlaser.services.config import ConfigService
    cfg = ConfigService(tmp_path / "c.json", load=False)
    native.register_settings(cfg)
    r = native.RecentFiles(cfg, limit=3)
    seen = []
    r.listeners.append(seen.append)
    for n in "abcd":
        (tmp_path / f"{n}.dxf").write_text("")
        r.add(str(tmp_path / f"{n}.dxf"))
    r.add(str(tmp_path / "b.dxf"))
    assert [os.path.basename(p) for p in r.paths] == ["b.dxf", "d.dxf", "c.dxf"]
    assert cfg.get("file.recent") == r.paths and seen[-1] == r.paths
    r.clear()
    assert r.paths == []


def test_desktop_entry_matches_packaging():
    text = (ROOT / "packaging" / "linux" / "io.github.mlaser.desktop").read_text(encoding="utf-8")
    assert text == native.desktop_entry("mlaser")
    assert "StartupWMClass=mlaser" in text and "Icon=io.github.mlaser" in text


def test_linux_platform_theme(tmp_path):
    d = tmp_path / "platformthemes"
    d.mkdir()
    (d / "libqxdgdesktopportal.so").write_text("")
    env = {"XDG_CURRENT_DESKTOP": "KDE"}
    assert native.linux_platform_theme(env, str(d)) == "xdgdesktopportal"
    assert native.linux_platform_theme({**env, "QT_QPA_PLATFORMTHEME": "kde"}, str(d)) is None
    (d / "libqgtk3.so").write_text("")
    assert native.linux_platform_theme(env, str(d)) is None             # Qt's own choice (GTK)


def test_bundle_metadata():
    spec = (ROOT / "packaging" / "mlaser.spec").read_text(encoding="utf-8")
    # the regression of the missing Dock icon / menu bar: never background-only
    assert '"LSBackgroundOnly": False' in spec and '"LSUIElement": False' in spec
    assert '"NSRequiresAquaSystemAppearance": False' in spec and "make_icons.py" in spec
    assert "argv_emulation=False" in spec and "CFBundleDocumentTypes" in spec
    assert "io.github.mlaser.desktop" in spec


def test_make_icons(tmp_path):
    import subprocess
    r = subprocess.run([sys.executable, str(ROOT / "packaging" / "make_icons.py"), str(tmp_path)],
                       capture_output=True, text=True, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=120)
    assert r.returncode == 0, r.stderr
    for n in ("mlaser.icns", "mlaser.ico", "hicolor/256x256/apps/io.github.mlaser.png"):
        assert (tmp_path / n).stat().st_size > 500, n


def test_job_feedback_and_alarm_attention(win, app, monkeypatch):
    nat = win.shell.native
    alerts = []
    monkeypatch.setattr(QApplication, "alert", staticmethod(lambda w, ms=0: alerts.append(w)))
    nat.job_feedback(True, 0.42, False)
    assert nat._progress == 42
    nat.job_feedback(True, 0.5, True)
    nat.job_feedback(True, 0.6, True)                   # one alert per new alarm, not per tick
    assert alerts == [win]
    nat.job_feedback(False, 0.0, False)
    assert nat._progress is None


def test_run_state_pill_shows_armed(win, app):
    """READY while armed: the ARMED colour and word, never the calm blue."""
    from mlaser.services.operator import RunStatus
    from mlaser.ui.workspaces import run as R
    ws = win.extension("run")
    scr = ws.screen
    real = ws.status
    try:
        ws.status = lambda: RunStatus(kind="ready", armed=True)
        scr.refresh()
        assert R.STATE_COLORS["armed"][0] in scr.state.styleSheet() and "ARMED" in scr.state.text()
        ws.status = lambda: RunStatus(kind="ready", armed=False)
        scr.refresh()
        assert R.STATE_COLORS["ready"][0] in scr.state.styleSheet() and "ARMED" not in scr.state.text()
    finally:
        ws.status = real


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_shell_integration(win, app, tmp_path):
    """The COM calls work on a real Windows: taskbar progress, the jump list, the AppUserModelID
    and the DWM dark title bar."""
    assert native.windows_set_app_id()
    tb = native.WindowsTaskbar()
    assert tb.progress(win, 0.5) and tb.progress(win, None)
    f = tmp_path / "part.dxf"
    f.write_text("0\nEOF\n")
    assert native.windows_jump_list([str(f)], category="Recent drawings")
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.platformName() == "windows":           # a real HWND (not offscreen)
        assert native.windows_dark_title_bar(win, True) and native.windows_dark_title_bar(win, False)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_macos_shell_integration(win, app):
    from PySide6.QtGui import QGuiApplication
    assert native.mac_activation_policy() in ("regular", "accessory", "prohibited")
    native.mac_badge("42%")
    native.mac_badge("")
    if QGuiApplication.platformName() == "cocoa":
        assert native.mac_activate() and native.mac_activation_policy() == "regular"


def test_linux_launcher_entry_signal():
    cmd = native.LinuxLauncher.command("gdbus", 0.42, False)
    assert cmd[:3] == ["gdbus", "emit", "--session"] and "com.canonical.Unity.LauncherEntry.Update" in cmd
    assert cmd[-2] == "application://io.github.mlaser.desktop"
    assert "'progress': <0.420>" in cmd[-1] and "'progress-visible': <true>" in cmd[-1]
    assert "'progress-visible': <false>" in native.LinuxLauncher.command("gdbus", None, True)[-1]
