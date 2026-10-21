"""The task-based shell of Wave 3 (docs/review/ux.md §3 P1-1/P1-10/P1-11/P2-2/P2-4/P2-7, §4;
user docs: docs/user/shell.md; plugin API: docs/dev/PLUGINS.md §5).

Layout of the main window with the shell::

    ┌────────────── menu bar (macOS: the global one; menubar.py) ──────────────────┐
    ├────────────────────────── top bar ───────────────────────────────────────────┤
    │ file.chf ●     [Search commands…  Ctrl+K]   ◆SIMULATOR ●DISARMED │ mode │ lang
    ├──────────────────────── safety state banner / alarm banner (safety_ui) ──────┤
    │rail│ workspace toolbar                                                        │
    │ ✎  ├───────────────────────────────────────────────┬──────────────────────────┤
    │ ⚙  │ page: canvas (+ side panel) or the workspace   │ docks of the workspace   │
    │ ▦  │ widget / empty-state placeholder               │                          │
    │ ▶  │                                                │                          │
    ├────┴────────────────────────── run bar (safety_ui) ───────────────────────────┤
    └───────────────────────────── status bar ─────────────────────────────────────┘

* :class:`Shell` is created by :class:`mlaser.ui.mainwindow.MainWindow` before the plugins load
  (so ``ctx.add_workspace`` works in ``register``) and finished after the safety UI is installed
  (:meth:`Shell.finish`): commands, shortcuts, palette, theme, user mode, layout.
* The **classic ribbon** (Settings > Classic ribbon, ``shell.classic_ribbon``) shows the
  original's ribbon instead of the top bar and the rail; the palette and every key still work.

Persisted in the ConfigService: ``shell.theme``, ``shell.classic_ribbon``, ``shell.user_mode``,
``shell.pin.*`` (hashes), ``shell.recent_commands``, ``shell.welcome_done``, ``shell.layout``.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from PySide6.QtCore import QByteArray, QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (QFileDialog, QInputDialog, QMessageBox, QToolButton,
                               QVBoxLayout, QWidget)

from mlaser.services import usermodes as um
from mlaser.services.command_search import Entry, RecentCommands
from mlaser.services.config import Setting
from mlaser.services.workspaces import SLOTS, WorkspaceSpec, command_workspace

from . import keymap, theme
from .dock_policy import CANVAS_MIN, DockPolicy  # noqa: F401 - CANVAS_MIN re-exported
from .workspace_host import WorkspaceHost

SETTINGS = [
    Setting("shell.theme", "str", theme.DEFAULT_THEME, label="shell.theme", section="shell", source="app"),
    Setting("shell.classic_ribbon", "bool", False, label="shell.classic_ribbon", section="shell", source="app"),
    Setting("shell.recent_commands", "json", [], label="shell.recent", section="shell", source="app"),
    Setting("shell.welcome_done", "bool", False, label="shell.welcome.title", section="shell", source="app"),
    Setting("shell.layout", "str", "", label="shell.reset_layout", section="shell", source="app"),
    Setting("shell.workspace", "str", "design", label="shell.rail", section="shell", source="app"),
    Setting("shell.geometry", "str", "", label="shell.reset_layout", section="shell", source="app"),
]
LAYOUT_VERSION = 4    # 4: side docks run the full height (dock_policy)


#: the smallest supported screen (docs/dev/RESPONSIVE.md); below it pages may scroll
SUPPORTED_SCREEN = QSize(1280, 800)


class _WindowWatch(QObject):
    """First show: initial dock sizes (or the saved layout); resize: palette placement."""

    def __init__(self, shell: "Shell"):
        super().__init__(shell.w)
        self.shell = shell
        self._shown = False

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        if obj is self.shell.w:
            t = ev.type()
            if t == QEvent.Show and not self._shown:
                self._shown = True
                QTimer.singleShot(0, self.shell._first_show)
                if self.shell.native is not None:
                    QTimer.singleShot(0, self.shell.native.window_shown)
            elif t == QEvent.WindowStateChange:
                self.shell.set_full_screen(self.shell.w.isFullScreen())
            elif t == QEvent.Resize and self.shell.palette is not None and self.shell.palette.isVisible():
                self.shell.palette._place()
        return False


class Shell:
    """See the module docstring."""

    def __init__(self, window):
        self.w = window
        self.tr = window.tr_
        self.config = window.config
        try:
            self.config.register(SETTINGS)
        except KeyError:
            pass
        self.modes = um.UserModeService(self.config)
        self.recent = RecentCommands(list(self.config.get("shell.recent_commands", []) or []))
        self.dirty = False
        self.classic = False
        self.theme = theme.DEFAULT_THEME
        self._mode_disabled: set[str] = set()
        self._default_state: Optional[QByteArray] = None
        self.palette = None
        self.topbar = None
        self.dock_policy = DockPolicy(window)
        self.menu_ctl = None
        self.menubar = None
        self.native = None
        self.host = WorkspaceHost(window, self.tr, self.action, self.allows, lambda: self.modes.mode)
        self.host.listeners.append(self._workspace_changed)
        window.setCentralWidget(self.host.stack)
        window.addToolBar(Qt.LeftToolBarArea, self.host.rail)
        from .topbar import TopBar
        self.topbar = TopBar(self)
        self.chrome = QWidget()
        self.chrome.setObjectName("shell_chrome")
        v = QVBoxLayout(self.chrome)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self._chrome_layout = v
        v.addWidget(self.topbar)
        v.addWidget(window.ribbon)
        window.setMenuWidget(self.chrome)
        self.host.activate("design")
        # providers for the plugins (Machine, Run, Nest, Report, Process workspaces)
        window.hooks.provide("workspace.show", self.show_workspace, "shell")
        window.hooks.provide("user_mode", lambda: self.modes.mode, "shell")

    # ------------------------------------------------------------------ helpers
    def action(self, cmd: str) -> Optional[QAction]:
        return self.w.ribbon.actions.get(cmd)

    def allows(self, cmd: str) -> bool:
        return self.modes.allows(cmd, self.w.commands.get(cmd))

    def label(self, cmd: str) -> str:
        act = self.action(cmd)
        t = act.text() if act is not None else cmd
        return t.replace("&", "").strip() or cmd

    def ws_title(self, ws_id: str) -> str:
        spec = self.host.specs.get(ws_id)
        return self.tr.tr(spec.title) if spec is not None and spec.title else ws_id

    # ------------------------------------------------------------------ phase 2
    def finish(self) -> None:
        w = self.w
        from ..native import NativeIntegration
        self.native = NativeIntegration(w, self.config, self.tr)
        self._register_commands()
        self._apply_keys()
        from .palette import CommandPalette
        self.palette = CommandPalette(w, self.entries, lambda: list(self.recent.ids), self.tr)
        self.palette.commandChosen.connect(self._palette_run)
        self._build_menu()
        w.commands.on_register.append(self._command_added)
        for act in list(w.ribbon.actions.values()):
            self._track_action(act)
        prev = w.ribbon.on_new_action
        w.ribbon.on_new_action = lambda act, prev=prev: (prev(act) if prev else None, self._track_action(act))
        w.docs.subscribe(self._document_changed)
        w._timer.timeout.connect(self._poll)
        w.hooks.add("close", self._on_close, "shell")
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.styleHints().colorSchemeChanged.connect(self._scheme_changed)
        self._watch = _WindowWatch(self)
        w.installEventFilter(self._watch)
        w.setProperty("userMode", self.modes.mode)          # before the style sheet: one polish
        self.set_theme(self.config.get("shell.theme", theme.DEFAULT_THEME), persist=False)
        self._apply_mode(self.modes.mode)
        self.set_classic(bool(self.config.get("shell.classic_ribbon", False)), persist=False)
        self._update_title()
        self.host.activate(self.host.current or "design", force=True)   # the plugins' docks too
        self.apply_focus_order()
        self._poll()

    # ------------------------------------------------------------------ commands
    def _register_commands(self) -> None:
        w = self.w
        reg = w.commands.register
        su = w.safety_ui
        cmds: list[tuple[str, Any, str, bool, str]] = [
            # id, handler, label key, checkable, min_mode
            ("shell.palette", lambda: self.open_palette(), "shell.palette.title", False, ""),
            ("shell.shortcuts", lambda: self.show_shortcuts(), "shell.shortcuts.title", False, ""),
            ("shell.welcome", lambda: self.show_welcome(), "shell.welcome.title", False, ""),
            ("shell.docs", lambda: self.open_docs(), "shell.docs", False, ""),
            ("shell.reset_layout", lambda: self.reset_layout(), "shell.reset_layout", False, ""),
            ("shell.classic_ribbon", lambda on: self.set_classic(bool(on)), "shell.classic_ribbon", True, ""),
            ("shell.user_mode", lambda: self.choose_mode(), "shell.mode.choose", False, ""),
            ("shell.set_pins", lambda: self.set_pins(), "shell.pin.setup_title", False, ""),
            ("shell.import_params", lambda: self.import_params(), "shell.import_params", False, "engineer"),
            ("run.start", lambda: su.start(), "shell.run.start", False, ""),
            ("run.start_selection", lambda: su.start_selection(), "shell.run.start_selection", False, ""),
            ("run.hold", lambda: su.hold(), "shell.run.hold", False, ""),
            ("run.stop", lambda: su.stop(), "shell.run.stop", False, ""),
            ("run.frame", lambda: su.frame(), "shell.run.frame", False, ""),
            ("run.dry_run", lambda: su.dry_run(), "shell.run.dry_run", False, ""),
            ("run.arm", lambda: su.toggle_arm(), "shell.run.arm", False, ""),
            ("run.simulate", lambda: w.simulate(), "shell.run.simulate", False, "engineer"),
            # Window menu (Native track): every platform; ⌃⌘F / F11 full screen
            ("window.minimize", lambda: w.showMinimized(), "shell.window.minimize", False, "operator"),
            ("window.zoom", lambda: self.toggle_zoom(), "shell.window.zoom", False, "operator"),
            ("window.fullscreen", lambda on: self.set_full_screen(bool(on)), "shell.window.fullscreen", True,
             "operator"),
        ]
        for m in um.MODES:
            cmds.append((f"shell.mode_{m}", lambda m=m: self.request_mode(m), f"shell.mode.switch_{m}", False, ""))
        for t in theme.THEME_IDS:
            cmds.append((f"shell.theme_{t}", lambda on, t=t: self.set_theme(t) if on else None,
                         f"shell.theme.{t}", True, ""))
        for cid, fn, label, checkable, mm in cmds:
            reg(cid, fn, "shell", label, checkable, min_mode=mm)
        grp = QActionGroup(w.ribbon)
        grp.setExclusive(True)
        for t in theme.THEME_IDS:
            grp.addAction(self.action(f"shell.theme_{t}"))
        self._theme_group = grp
        for ws in list(self.host.specs):
            self._register_workspace_command(ws)
        # a classic-ribbon tab with the workspaces (the rail is hidden in the classic view)
        w.ribbon.add_command_tab(self.tr.tr("shell.rail"), [f"workspace.{s}" for s in SLOTS]
                                 + ["shell.palette", "shell.classic_ribbon"])

    def _register_workspace_command(self, ws_id: str) -> None:
        cid = f"workspace.{ws_id}"
        if cid in self.w.commands:
            return
        spec = self.host.specs[ws_id]
        self.w.commands.register(cid, lambda i=ws_id: self.show_workspace(i), "shell", spec.title or ws_id,
                                 min_mode=spec.min_mode if spec.min_mode in um.MODES else "", workspace=ws_id)
        act = self.action(cid)
        if act is not None:
            title = spec.title or ws_id
            self.tr.bind(act, title, lambda t, a=act: a.setText(self.tr.format("shell.ws.goto", workspace=t)))
        if act is not None and spec.icon:
            from ..icons import icon
            act.setIcon(icon(spec.icon))
        if act is not None and spec.shortcut and getattr(self, "_keys_applied", False):
            self._set_keys(act, spec.shortcut)

    def register_workspace(self, spec: WorkspaceSpec) -> None:
        """MainWindow.host_register_workspace: a plugin's workspace."""
        page = self.host.add(spec)
        if getattr(self, "_keys_applied", False):
            self._register_workspace_command(page.spec.id)
            self.host.refresh_actions()

    def _command_added(self, spec) -> None:
        act = self.action(spec.id)
        if act is not None:
            self._apply_mode_to(spec.id, act)

    def _track_action(self, act: QAction) -> None:
        cmd = act.data() or act.objectName()
        if not cmd or act.property("shell_tracked"):
            return
        act.setProperty("shell_tracked", True)
        act.triggered.connect(lambda _=False, c=cmd: self._used(c))
        self._apply_mode_to(cmd, act)

    def _used(self, cmd: str) -> None:
        if cmd in ("shell.palette",):
            return
        self.recent.add(cmd)
        self.config.set("shell.recent_commands", list(self.recent.ids))

    def entries(self) -> list[Entry]:
        """Palette entries: every command the user mode allows."""
        out = []
        w = self.w
        for cid, act in w.ribbon.actions.items():
            spec = w.commands.get(cid)
            if spec is None or not self.allows(cid):
                continue
            ws = command_workspace(cid, spec)
            out.append(Entry(cid, self.label(cid), self.ws_title(ws) if ws else "",
                             keymap.key_for(cid), (cid.split(".", 1)[0],), act.isEnabled()))
        return out

    def _palette_run(self, cmd: str) -> None:
        self.w.run(cmd)
        if cmd not in self.recent.ids[:1]:
            self._used(cmd)

    def open_palette(self, text: str = "") -> None:
        if self.palette is not None:
            self.palette.open(text)

    # ------------------------------------------------------------------ keys
    def _set_keys(self, act: QAction, key: str) -> None:
        seqs = [QKeySequence(k) for k in keymap.sequences(key)]
        act.setShortcuts(seqs)
        act.setShortcutContext(Qt.WindowShortcut)
        if act not in self.w.actions():
            self.w.addAction(act)
        lk = act.property("lang_key") or ""
        shown = " / ".join(s.toString(QKeySequence.NativeText) for s in seqs)
        if lk:
            self.tr.bind(act, lk, lambda t, a=act, s=shown: a.setToolTip(f"{t} ({s})"))
        else:
            act.setToolTip(f"{act.text()} ({shown})")

    def _apply_keys(self) -> None:
        w = self.w
        keys = keymap.window_keys()
        for cmd, key in keys.items():
            act = self.action(cmd)
            if act is not None:
                self._set_keys(act, key)
        for spec in self.host.specs.values():
            act = self.action(f"workspace.{spec.id}")
            if act is not None and spec.shortcut and spec.shortcut not in keys.values():
                self._set_keys(act, spec.shortcut)
        self._canvas_keys = []
        for cmd, key in keymap.CANVAS_KEYS.items():
            sc = QShortcut(QKeySequence(key), w.canvas)
            sc.setContext(Qt.WidgetShortcut)
            sc.activated.connect(lambda c=cmd: w.run(c))
            self._canvas_keys.append(sc)
            act = self.action(cmd)
            if act is not None and cmd not in keys:
                lk = act.property("lang_key") or ""
                if lk:
                    self.tr.bind(act, lk, lambda t, a=act, k=key: a.setToolTip(f"{t} ({k})"))
        self._keys_applied = True

    def shortcut_rows(self) -> list[tuple[str, str, str, str]]:
        def ws(cmd: str) -> str:
            k = command_workspace(cmd, self.w.commands.get(cmd))
            return self.ws_title(k) if k else ""
        return keymap.rows(self.label, ws, self.tr.tr, known=self.w.ribbon.actions.keys(), plat=self._plat)

    def show_shortcuts(self):
        from .dialogs import ShortcutSheet
        d = ShortcutSheet(self.shortcut_rows(), self.tr, self.w)
        d.setAttribute(Qt.WA_DeleteOnClose)
        d.show()
        self._sheet = d
        return d

    # ------------------------------------------------------------------ workspaces
    def show_workspace(self, ws_id: str) -> bool:
        """Switch to ``ws_id`` (slot, plugin workspace, or contribution id such as
        ``"ext_machine.machine"``); shows the main window if it is hidden."""
        ws_id = self.host.resolve(ws_id)
        if not self.w.isVisible() and ws_id in self.host.visible_ids():
            self.w.show()
        ok = self.host.activate(ws_id)
        if not ok and ws_id in self.host.specs:
            self.w.statusBar().showMessage(self.tr.format("shell.ws.hidden", workspace=self.ws_title(ws_id)), 4000)
        return ok

    def focus_chain(self) -> list[QWidget]:
        """The keyboard focus order: top bar (menu, search, user mode, language), the rail, the
        workspace toolbar, the canvas (or the workspace page), then the run bar."""
        w = self.w
        tb = self.topbar
        chain: list[QWidget] = [tb.search, tb.mode_combo, w.lang_combo]
        rail = self.host.rail
        for ws in self.host.visible_ids():
            b = rail.widgetForAction(self.host.rail_actions[ws])
            if b is not None:
                chain.append(b)
        page = self.host.pages.get(self.host.current)
        if page is not None:
            for act in page.toolbar.actions():
                b = page.toolbar.widgetForAction(act)
                if b is not None and isinstance(b, QToolButton):
                    chain.append(b)
            if page.spec.canvas:
                chain.append(w.canvas)
            elif page.placeholder is not None:
                chain.extend(page.placeholder.buttons.values())
        su = getattr(w, "safety_ui", None)
        if su is not None:
            chain.extend(su.run_bar.buttons.values())
        return chain

    def apply_focus_order(self) -> None:
        chain = [c for c in self.focus_chain() if c is not None and c.window() is self.w]
        for a, b in zip(chain, chain[1:]):
            QWidget.setTabOrder(a, b)

    def _workspace_changed(self, ws_id: str) -> None:
        if self.topbar is not None and getattr(self, "_keys_applied", False):
            self.apply_focus_order()
        self.config.set("shell.workspace", ws_id)
        try:
            self.w.hooks.emit("workspace_changed", ws_id)
        except KeyError:
            pass
        if getattr(self, "_fit_screen", False) and self.w.isVisible():
            # a workspace with a taller minimum must not push the window past a small screen
            QTimer.singleShot(0, self._clamp_to_screen)

    # ------------------------------------------------------------------ user mode
    def request_mode(self, target: str) -> bool:
        """Switch the user mode; asks for the PIN when one protects ``target``."""
        if target == self.modes.mode:
            return True
        pin = None
        if self.modes.needs_pin(target):
            from .dialogs import ask_pin
            pin = ask_pin(self.w, self.tr, target)
            if pin is None:
                return False
        if not self.modes.set_mode(target, pin):
            if not getattr(self.w, "_test_no_dialogs", False):
                QMessageBox.warning(self.w, self.tr.tr("shell.pin.title"), self.tr.tr("shell.pin.wrong"))
            self.w.log(self.tr.format("shell.mode.refused", mode=self.tr.tr(f"shell.mode.{target}")))
            return False
        self._apply_mode(target)
        self.w.log(self.tr.format("shell.mode.changed", mode=self.tr.tr(f"shell.mode.{target}")))
        return True

    def set_user_mode(self, target: str, pin: Optional[str] = None) -> bool:
        """Programmatic switch (tests, plugins): no dialog; ``pin`` when one is needed."""
        if not self.modes.set_mode(target, pin):
            return False
        self._apply_mode(target)
        return True

    def choose_mode(self) -> None:
        names = [self.tr.tr(f"shell.mode.{m}") for m in um.MODES]
        cur = um.MODES.index(self.modes.mode)
        item, ok = QInputDialog.getItem(self.w, self.tr.tr("shell.mode.title"), self.tr.tr("shell.mode.choose"),
                                        names, cur, False)
        if ok and item in names:
            self.request_mode(um.MODES[names.index(item)])

    def set_pins(self) -> None:
        if self.modes.mode != um.SERVICE:
            self.w.log(self.tr.tr("shell.pin.service_only"))
            return
        from .dialogs import PinSetupDialog
        PinSetupDialog(self.modes, self.tr, self.w).exec()

    def _apply_mode_to(self, cmd: str, act: QAction) -> None:
        ok = self.allows(cmd)
        if not ok:
            if act.isEnabled():
                act.setEnabled(False)
                self._mode_disabled.add(cmd)
        elif cmd in self._mode_disabled:
            self._mode_disabled.discard(cmd)
            act.setEnabled(True)

    def _apply_mode(self, mode: str) -> None:
        w = self.w
        for cmd, act in w.ribbon.actions.items():
            self._apply_mode_to(cmd, act)
        if w.property("userMode") != mode:
            w.setProperty("userMode", mode)
            w.setStyleSheet("")                             # re-polish the [userMode] rules
            w.setStyleSheet(theme.style_sheet(self.theme))
        self.host.refresh_mode()
        if self.topbar is not None:
            self.topbar.set_mode(mode)
        lp = getattr(w, "layer_panel", None)
        if lp is not None:
            lp.setEnabled(mode != um.OPERATOR)               # Operator: no parameter editing
        if getattr(self, "_keys_applied", False):
            self.apply_focus_order()
        try:
            w.hooks.emit("user_mode_changed", mode)
        except KeyError:
            pass

    # ------------------------------------------------------------------ theme / classic
    def set_theme(self, name: str, persist: bool = True) -> str:
        self.theme = theme.apply(name, self.w)
        if self.native is not None:
            from PySide6.QtWidgets import QApplication
            self.native.color_scheme_changed(theme.effective(self.theme, QApplication.instance()) != theme.LIGHT)
        act = self.action(f"shell.theme_{self.theme}")
        if act is not None and not act.isChecked():
            act.blockSignals(True)
            act.setChecked(True)
            act.blockSignals(False)
        if persist:
            self.config.set("shell.theme", self.theme)
        return self.theme

    def _scheme_changed(self, *_a) -> None:
        """The system switched light/dark: the canvas (theme ``system``) and the title bar."""
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        eff = theme.effective(self.theme, app)
        if self.theme == theme.SYSTEM:
            theme.apply_canvas_theme(eff, getattr(self.w, "canvas", None))
            self.w.update()
        if self.native is not None:
            self.native.color_scheme_changed(eff != theme.LIGHT)

    # ------------------------------------------------------------------ window
    def toggle_zoom(self) -> None:
        """Window > Zoom (macOS) / Maximize: the window fills the screen's work area or comes back."""
        if self.w.isMaximized():
            self.w.showNormal()
        else:
            self.w.showMaximized()

    def set_full_screen(self, on: bool) -> None:
        """Window > Enter Full Screen (⌃⌘F on macOS, F11 elsewhere)."""
        if on and not self.w.isFullScreen():
            self.w.showFullScreen()
        elif not on and self.w.isFullScreen():
            self.w.showNormal()
        act = self.action("window.fullscreen")
        if act is not None and act.isChecked() != self.w.isFullScreen():
            act.blockSignals(True)
            act.setChecked(self.w.isFullScreen())
            act.blockSignals(False)

    def set_classic(self, on: bool, persist: bool = True) -> None:
        """Classic ribbon view (the original's layout) or the task shell."""
        w = self.w
        self.classic = on
        w.ribbon.setVisible(on)
        self.topbar.setVisible(not on)
        self.host.rail.setVisible(not on)
        for page in self.host.pages.values():
            page.toolbar.setVisible(not on and bool(page.toolbar.actions()))
        combo = w.lang_combo
        if on:
            w.ribbon.setCornerWidget(combo, Qt.TopRightCorner)
            combo.show()
        else:
            if w.ribbon.cornerWidget(Qt.TopRightCorner) is combo:
                w.ribbon.setCornerWidget(None, Qt.TopRightCorner)   # (hides the old corner)
            self.topbar.take_language_combo(combo)
        act = self.action("shell.classic_ribbon")
        if act is not None and act.isChecked() != on:
            act.blockSignals(True)
            act.setChecked(on)
            act.blockSignals(False)
        if on and self.host.current != "design":
            self.host.activate("design")
        if persist:
            self.config.set("shell.classic_ribbon", on)

    # ------------------------------------------------------------------ file / title
    def _document_changed(self, kind: str) -> None:
        if kind == "replace":
            doc = self.w.docs.document
            self.dirty = bool(doc.parts) and not self.w.docs.path
        else:
            self.dirty = True
        self._update_title()

    def mark_saved(self) -> None:
        self.dirty = False
        self._update_title()

    def _update_title(self) -> None:
        """The document title, the platform's way: the file name with the modified marker
        (``[*]``: the dot in the close button on macOS, "*" elsewhere), the represented file
        (macOS proxy icon, ⌘-click path menu), and Qt appends " — mlaser" on Windows/Linux."""
        path = self.w.docs.path
        name = os.path.basename(path) if path else self.tr.tr("shell.untitled")
        self.w.setWindowTitle(f"{name}[*]")
        self.w.setWindowModified(self.dirty)
        self.w.setWindowFilePath(path or "")
        if path and self.native is not None:
            self.native.document_path(path)
        if self.topbar is not None:
            self.topbar.set_file(path, self.dirty)

    # ------------------------------------------------------------------ polling
    def _poll(self) -> None:
        ctl = self.w.ctl
        try:
            st = ctl.safety()
        except Exception:  # noqa: BLE001 - a controller without the safety layer
            st = None
        alarms = list(ctl.active_alarms or [])
        if self.topbar is not None and self.topbar.isVisible():
            self.topbar.refresh_chips(st, alarms)
        if self.native is not None:
            from .. import safety_ui
            running = safety_ui.job_running(self.w)
            prog = 0.0
            if running:
                try:
                    prog = float(ctl.state().progress or 0.0)
                except Exception:  # noqa: BLE001 - a controller without a state snapshot
                    prog = 0.0
            self.native.job_feedback(running, prog, bool(alarms) or getattr(st, "banner", "") == "FAULT")

    # ------------------------------------------------------------------ layout
    def _first_show(self) -> None:
        w = self.w
        if self._fit_screen:
            self._clamp_to_screen()          # the frame is known now
        pol = self.dock_policy
        pol.enabled = True
        # the canvas starts with the keyboard focus (its one-key tools, Space = Hold while a job
        # runs), not the first widget of the chain: the command search field
        from PySide6.QtWidgets import QApplication
        fw = QApplication.focusWidget()
        canvas = getattr(w, "canvas", None)
        if canvas is not None and canvas.isVisible() and (fw is None or fw is getattr(self.topbar, "search", None)):
            canvas.setFocus(Qt.OtherFocusReason)
        if self._default_state is None:
            self.fit_docks()
            self._default_state = w.saveState(LAYOUT_VERSION)
        saved = self.config.get("shell.layout", "")
        if saved:
            try:
                if w.restoreState(QByteArray.fromBase64(saved.encode("ascii")), LAYOUT_VERSION):
                    # the saved widths are the user's: kept, and clamped to this window
                    w.layout().activate()
                    for d in pol.side_docks():
                        pol.remember(d)
            except Exception:  # noqa: BLE001 - a broken saved layout falls back to the default
                pass
            self.host.activate(self.host.current, force=True)
        ws = self.config.get("shell.workspace", "design")
        if ws in self.host.specs and ws != self.host.current:
            self.host.activate(ws)
        pol.apply()

    def fit_docks(self) -> None:
        """Dock sizes for the window as it is now (:mod:`.dock_policy`): the remembered or
        natural widths, given back in proportion when the page needs the room."""
        w = self.w
        lay = w.layout()
        if lay is not None:
            lay.activate()
        pol = self.dock_policy
        was, pol.enabled = pol.enabled, True
        try:
            pol.apply()
        finally:
            pol.enabled = was or pol.enabled
        if lay is not None:
            lay.activate()

    def reset_layout(self) -> None:
        """Default dock positions and sizes, all toolbars back, the Design workspace."""
        w = self.w
        if self._default_state is not None:
            w.restoreState(self._default_state, LAYOUT_VERSION)
        for d in self.host.docks():
            if d.isFloating():
                d.setFloating(False)
        self.dock_policy.forget()
        self.fit_docks()
        self.config.set("shell.layout", "")
        self.host.activate("design" if "design" in self.host.visible_ids() else self.host.visible_ids()[0],
                           force=True)
        w.statusBar().showMessage(self.tr.tr("shell.layout_reset"), 3000)

    def _on_close(self) -> None:
        """Remember the dock layout (only of a window that was shown: a test or a headless
        window has no layout worth keeping) and, for a window placed by :meth:`fit_to_screen`,
        its geometry."""
        if self._default_state is not None:
            try:
                self.config.set("shell.layout", bytes(self.w.saveState(LAYOUT_VERSION).toBase64()).decode("ascii"))
                if self._fit_screen:
                    self.config.set("shell.geometry", bytes(self.w.saveGeometry().toBase64()).decode("ascii"))
            except Exception:  # noqa: BLE001
                pass
        if self.native is not None:
            self.native.close()

    # ------------------------------------------------------------------ window geometry
    _fit_screen = False

    def fit_to_screen(self, screen=None) -> None:
        """Start-up geometry (the application calls it before ``show``): the saved geometry, or
        on the first start the whole available screen (menu bar and Dock / taskbar excluded),
        never larger than the screen and never smaller than what the content needs.  Checked
        again once the window frame is known (:meth:`_clamp_to_screen`)."""
        from PySide6.QtGui import QGuiApplication
        w = self.w
        self._fit_screen = True
        scr = screen or w.screen() or QGuiApplication.primaryScreen()
        if scr is None:
            return
        avail = scr.availableGeometry()
        saved = self.config.get("shell.geometry", "") or ""
        restored = False
        if saved:
            try:
                restored = bool(w.restoreGeometry(QByteArray.fromBase64(saved.encode("ascii"))))
            except Exception:  # noqa: BLE001 - a broken saved geometry: first-start size
                restored = False
        if not restored:
            w.setGeometry(avail)
        self._clamp_to_screen(scr)

    def _clamp_to_screen(self, screen=None) -> None:
        w = self.w
        scr = screen or w.screen()
        if scr is None or w.isMaximized() or w.isFullScreen():
            return
        avail = scr.availableGeometry()
        frame = w.frameGeometry()
        extra_w = max(frame.width() - w.width(), 0)
        extra_h = max(frame.height() - w.height(), 0)
        room = avail.size() - QSize(extra_w, extra_h)
        # a screen smaller than the content's minimum (below the supported 1280x800): the
        # window still fits the screen and the workspace page scrolls (docs/dev/RESPONSIVE.md);
        # otherwise the minimum wins over the resize and the window hangs off the screen
        need = w.minimumSizeHint().expandedTo(w.minimumSize())
        small = avail.width() < SUPPORTED_SCREEN.width() or avail.height() < SUPPORTED_SCREEN.height()
        if small or need.width() >= room.width() or need.height() >= room.height():
            # an explicit minimum: Qt's layouts keep it, so a workspace with a taller page cannot
            # raise the window past the screen (FitArea's screen cap alone crept by the frame
            # margins on every relayout); that page scrolls instead
            w.setMinimumSize(need.boundedTo(room))
        size = w.size().boundedTo(room)
        if size != w.size():
            w.resize(size)
        frame = w.frameGeometry()
        # the top-left (title bar, menus, rail) always on the screen
        x = max(min(frame.x(), avail.right() - frame.width() + 1), avail.x())
        y = max(min(frame.y(), avail.bottom() - frame.height() + 1), avail.y())
        if (x, y) != (frame.x(), frame.y()):
            w.move(x, y)

    # ------------------------------------------------------------------ welcome / docs / import
    def show_welcome(self):
        from .dialogs import WelcomeSheet
        d = WelcomeSheet(self, self.w)
        d.setAttribute(Qt.WA_DeleteOnClose)
        d.show()
        self._welcome = d
        return d

    def maybe_show_welcome(self) -> bool:
        """First run (application start-up only, not in tests): the welcome sheet."""
        if self.config.get("shell.welcome_done", False):
            return False
        QTimer.singleShot(300, self.show_welcome)
        return True

    def open_docs(self) -> bool:
        from .dialogs import open_docs
        return open_docs(self.w, self.tr)

    def import_params(self, path: Optional[str] = None) -> int:
        """Import the HardPara.xml / ManuPara.xml of an original Mlaser install (its ``dat``
        folder) into the ConfigService."""
        if path is None:
            path = QFileDialog.getExistingDirectory(self.w, self.tr.tr("shell.import_params"))
        if not path:
            return 0
        try:
            n = self.config.import_dat(path)
        except (OSError, ValueError) as exc:
            self.w.log(self.tr.format("shell.import_params.failed", error=str(exc)))
            return 0
        self.w.log(self.tr.format("shell.import_params.done", count=n, path=path))
        return n

    # ------------------------------------------------------------------ menu bar
    @property
    def _plat(self) -> str:
        from ..native import platform_id
        return platform_id()

    def _build_menu(self) -> None:
        """The native menu bar (menubar.py), above the top bar; on macOS the global one."""
        from .menubar import MenuBar
        self.menu_ctl = MenuBar(self)
        self.menubar = self.menu_ctl.bar
        self._chrome_layout.insertWidget(0, self.menubar)
        self.topbar.menu_btn.hide()            # the in-window "Mlaser ⌄" menu is replaced
        self.w.commands.on_register.append(self.menu_ctl.schedule_rebuild)


def install(window) -> Shell:
    shell = Shell(window)
    window.shell = shell
    return shell


__all__ = ["Shell", "install", "SETTINGS"]
