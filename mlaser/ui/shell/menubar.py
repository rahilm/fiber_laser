"""The native menu bar (``QMenuBar``), built from the command registry by :mod:`.menu_spec`.

* macOS: Qt turns it into the global menu bar; About, Settings… (⌘,), Plugins… and Quit (⌘Q)
  move into the application menu through their menu roles, next to Services, Hide, Hide
  Others and Show All (Qt adds those); the Help menu gets the system's search field.
* Windows: a menu bar in the window, Alt+letter opens a menu (``&File``), File ends with Exit,
  Tools ends with Options….
* Linux: a menu bar in the window, exported to the desktop's global menu where it offers one
  (KDE Plasma, Unity: Qt does it when the platform theme provides a D-Bus menu); File ends with
  Quit, Edit with Preferences….

The menu items are the commands' own QActions (the ribbon's), so their text, shortcut, check
state and enabled state (the user mode, the job lock) are the same everywhere.  The in-window
"Mlaser ⌄" button of the top bar is gone; the command palette stays (Help > Search commands).
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QDockWidget, QMenu, QMenuBar

from .. import native
from . import menu_spec as spec

_ROLE = {spec.ABOUT: QAction.AboutRole, spec.PREFERENCES: QAction.PreferencesRole, spec.QUIT: QAction.QuitRole,
         spec.APP_SPECIFIC: QAction.ApplicationSpecificRole}


class MenuBar:
    """Owns the QMenuBar of the main window (in the shell's chrome, above the top bar)."""

    def __init__(self, shell, plat: Optional[str] = None, parent=None):
        self.shell = shell
        self.w = shell.w
        self.tr = shell.tr
        self.plat = plat or native.platform_id()
        self.bar = QMenuBar(parent if parent is not None else shell.chrome)
        self.bar.setObjectName("shell_menubar")
        self.menus: dict[str, QMenu] = {}
        self.layout: list[spec.Menu] = []
        self._titles: list[str] = []
        self._pending = False
        self._labels_bound: set[str] = set()
        self._heads: dict[str, list[QMenu]] = {}      # drop-down command -> its submenus
        self.build()

    # ------------------------------------------------------------------ helpers
    def settings_cmd(self) -> str:
        """The Settings track's ``app.settings`` when registered, else ``app.options``."""
        return "app.settings" if "app.settings" in self.w.commands else "app.options"

    def action(self, cid: str) -> Optional[QAction]:
        return self.w.ribbon.actions.get(cid)

    def schedule_rebuild(self, *_a) -> None:
        """A command was added (a plugin, a workspace): rebuild once the burst is over."""
        if not self._pending:
            self._pending = True
            QTimer.singleShot(0, self._rebuild_now)

    def _rebuild_now(self) -> None:
        self._pending = False
        try:
            self.build()
        except RuntimeError:            # the window is gone
            pass

    # ------------------------------------------------------------------ build
    def build(self) -> None:
        settings = self.settings_cmd()
        self.layout = spec.layout(list(self.w.ribbon.actions.keys()), self.plat, settings)
        roles = spec.roles(self.plat, settings)
        self._apply_labels(settings)
        old = list(self.menus.values())
        self.bar.clear()
        for qm in old:
            qm.deleteLater()
        for subs in self._heads.values():
            subs.clear()
        self.menus = {}
        self._titles = []
        for i, m in enumerate(self.layout):
            qm = self.bar.addMenu("")
            qm.setObjectName(f"menu_{m.id}")
            self.menus[m.id] = qm
            self._titles.append(m.id)
            self.tr.bind(qm, m.title, lambda t, i=i: self._set_title(i, t))
            self._fill(qm, m.items)
        for cid in spec.all_ids(self.layout):
            act = self.action(cid)
            if act is not None:
                act.setMenuRole(_ROLE.get(roles.get(cid, ""), QAction.NoRole))
        if self.plat == native.MACOS:
            QTimer.singleShot(0, lambda: native.mac_set_help_menu(self.menus["help"].title().replace("&", "")))

    def _set_title(self, i: int, text: str) -> None:
        """Titles with distinct access keys on Windows/Linux (Alt+F opens File)."""
        raw = getattr(self, "_raw_titles", {})
        raw[i] = text.replace("&", "")
        self._raw_titles = raw
        acts = self.bar.actions()
        titles = [raw.get(k, a.text().replace("&", "")) for k, a in enumerate(acts)]
        ids = [m.id for m in self.layout]
        first = [ids.index(m) for m in spec.MNEMONIC_FIRST if m in ids]
        shown = titles if self.plat == native.MACOS else spec.mnemonic_titles(titles, first)
        for a, t in zip(acts, shown):
            if a.text() != t:
                a.setText(t)

    def _apply_labels(self, settings: str) -> None:
        """The platform's words (Settings… / Options… / Preferences…, Quit / Exit)."""
        for cid, key in spec.platform_labels(self.plat, settings).items():
            act = self.action(cid)
            if act is None or cid in self._labels_bound:
                continue
            self._labels_bound.add(cid)
            self.tr.bind(act, key, act.setText)

    def _fill(self, qm: QMenu, items: list) -> None:
        for e in items:
            if e == spec.SEP:
                qm.addSeparator()
            elif isinstance(e, spec.Sub):
                self._sub(qm, e)
            else:
                act = self.action(e)
                if act is not None:
                    qm.addAction(act)

    def _sub(self, parent: QMenu, e: spec.Sub) -> None:
        sub = parent.addMenu("")
        sub.setObjectName(f"menu_{e.id or e.head or e.title}")
        if e.head:
            head = self.action(e.head)
            if head is not None:
                sub.setTitle(self.shell.label(e.head))
                if e.head not in self._heads:
                    self._heads[e.head] = []
                    head.changed.connect(lambda c=e.head: self._retitle(c))
                self._heads[e.head].append(sub)
                sub.addAction(head)
                if e.items:
                    sub.addSeparator()
        elif e.title:
            self.tr.bind(sub, e.title, sub.setTitle)
        if e.id == "recent":
            sub.aboutToShow.connect(lambda s=sub: self._fill_recent(s))
            self._fill_recent(sub)
        elif e.id == "theme":
            from . import theme
            for t in theme.THEME_IDS:
                act = self.action(f"shell.theme_{t}")
                if act is not None:
                    sub.addAction(act)
        elif e.id == "docks":
            sub.aboutToShow.connect(lambda s=sub: self._fill_docks(s))
            self._fill_docks(sub)
        self._fill(sub, e.items)

    def _retitle(self, cid: str) -> None:
        text = self.shell.label(cid)
        for menu in list(self._heads.get(cid, [])):
            try:
                if menu.title() != text:
                    menu.setTitle(text)
            except RuntimeError:
                self._heads[cid].remove(menu)

    def _fill_recent(self, sub: QMenu) -> None:
        sub.clear()
        nat = getattr(self.shell, "native", None)
        paths = nat.recent.existing() if nat is not None else []
        for p in paths:
            a = sub.addAction(os.path.basename(p))
            a.setToolTip(p)
            a.setStatusTip(p)
            a.triggered.connect(lambda _=False, p=p: nat.open_file(p))
        if paths:
            sub.addSeparator()
        clear = sub.addAction("")
        self.tr.bind(clear, "shell.menu.clear_recent", clear.setText)
        clear.setEnabled(bool(paths))
        if nat is not None:
            clear.triggered.connect(lambda: nat.recent.clear())
        sub.setEnabled(True)

    def _fill_docks(self, sub: QMenu) -> None:
        """The panels (docks) of the window: show / hide each."""
        sub.clear()
        docks = sorted((d for d in self.w.findChildren(QDockWidget) if d.windowTitle()),
                       key=lambda d: d.windowTitle().lower())
        for d in docks:
            sub.addAction(d.toggleViewAction())


__all__ = ["MenuBar"]
