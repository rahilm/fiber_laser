"""The Plugin manager dialog (Options+ > Plugins, command ``app.plugin_manager``).

Tabs:

* **Installed**: every entry-point plugin (installed by the manager, or with pip), its version,
  where it comes from, its status (``loaded`` / ``failed`` this session, ``enabled`` /
  ``disabled`` / ``changed``) and its declared permissions; Enable / Disable (Service mode, with
  the permissions and the trust warning in the confirmation; machine authority asked for
  separately for a plugin that declares ``machine``), Update, Uninstall, Homepage.
* **Browse**: the plugins of the repositories, a search field, the details of the selected one
  (permissions, requirements, compatibility) and Install.
* **Repositories**: the index URLs and ``github:<owner>/<repo>`` shortcuts; add and remove.

Plus Install from file / from URL, Check for updates, and a "Restart to apply" banner after any
change (plugins are loaded at start-up only).  Every change needs Service mode; network work runs
on a worker thread (:meth:`PluginManagerDialog.run`) and the result comes back on the GUI thread.
The service logic is :mod:`mlaser.services.plugin_manager`.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .workspaces import TOUCH_MIN, tr

WARNING = ("A plugin is program code that runs inside mlaser with your user account's rights. The "
           "permissions it declares are enforced on mlaser's own interfaces (network hosts, credentials, "
           "the drawing, orders, the queue, the machine), but they are not a sandbox. Enable only plugins "
           "you trust, from a source you trust. Changes take effect at the next start.")


def _tr(window: Any, key: str, default: str, **kw: Any) -> str:
    return tr(window, key, default, **kw)


def _perm_lines(perms: Optional[dict]) -> list[str]:
    if perms is None:
        return ["legacy plugin (no manifest): no declared permissions, full API-1 access"]
    from mlaser.services.plugin_manifest import Permissions
    try:
        return Permissions.from_dict(perms).lines()
    except ValueError as exc:
        return [f"invalid permissions: {exc}"]


class _Bridge(QObject):
    call = Signal(object)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.call.connect(lambda fn: fn())


class PluginManagerDialog(QDialog):
    """See the module docstring.  ``sync``: run the work on the calling thread (tests)."""

    COLUMNS = ("plugin", "version", "origin", "status", "permissions")

    def __init__(self, window: Any, manager: Any = None, sync: bool = False) -> None:
        super().__init__(window)
        self.w = window
        self.sync = sync
        self.setObjectName("plugin_manager")
        self.setWindowTitle(_tr(window, "plugin_manager.title", "Plugin manager"))
        self.resize(900, 600)
        if manager is None:
            from mlaser.services.plugin_manager import PluginManager
            manager = PluginManager(window.config, reserved=list(getattr(window, "extensions", {}) or {}))
        self.pm = manager
        self.service = str(getattr(window, "user_mode", "service")) == "service"
        self.answer: Optional[Callable[[str, str], bool]] = None     # tests: answers the questions
        self.messages: list[str] = []                                 # what was shown (tests, log)
        self.opened: list[str] = []
        self.rows: list[dict] = []
        self.browse_items: list[Any] = []
        self.updates: dict[str, dict] = {}
        self._bridge = _Bridge(self)
        self._busy = 0

        lay = QVBoxLayout(self)
        self.banner = QFrame()
        self.banner.setObjectName("plugin_manager_banner")
        self.banner.setFrameShape(QFrame.Shape.StyledPanel)
        self.banner.setAutoFillBackground(True)
        self.banner.setBackgroundRole(self.banner.palette().ColorRole.AlternateBase)
        bl = QHBoxLayout(self.banner)
        self.banner_text = QLabel(_tr(window, "plugin_manager.restart",
                                      "Restart mlaser to apply the plugin changes."))
        self.banner_text.setWordWrap(True)
        bl.addWidget(self.banner_text, 1)
        lay.addWidget(self.banner)
        warn = QLabel(_tr(window, "plugin_manager.warning", WARNING))
        warn.setObjectName("plugin_manager_warning")
        warn.setWordWrap(True)
        lay.addWidget(warn)
        if not self.service:
            note = QLabel(_tr(window, "plugin_manager.service_only",
                              "Viewing only: installing, enabling and removing plugins needs Service mode."))
            note.setObjectName("plugin_manager_mode_note")
            note.setWordWrap(True)
            lay.addWidget(note)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("plugin_manager_tabs")
        self.tabs.addTab(self._installed_tab(), _tr(window, "plugin_manager.tab_installed", "Installed"))
        self.tabs.addTab(self._browse_tab(), _tr(window, "plugin_manager.tab_browse", "Browse"))
        self.tabs.addTab(self._repos_tab(), _tr(window, "plugin_manager.tab_repositories", "Repositories"))
        lay.addWidget(self.tabs, 1)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.status = QLabel("")
        self.status.setObjectName("plugin_manager_status")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        row = QHBoxLayout()
        self.btn_file = self._button(_tr(window, "plugin_manager.install_file", "Install from file..."),
                                     self.choose_file, mutating=True)
        self.btn_url = self._button(_tr(window, "plugin_manager.install_url", "Install from URL..."),
                                    self.choose_url, mutating=True)
        self.btn_check = self._button(_tr(window, "plugin_manager.check_updates", "Check for updates"),
                                      self.check_updates)
        for b in (self.btn_file, self.btn_url, self.btn_check):
            row.addWidget(b)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        for bt in bb.buttons():
            bt.setMinimumHeight(TOUCH_MIN)
        row.addWidget(bb)
        lay.addLayout(row)
        self.refresh_installed()
        self.refresh_repositories()
        self._update_banner()

    # ------------------------------------------------------------------ building blocks
    def _button(self, text: str, slot: Callable[[], Any], mutating: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setMinimumHeight(TOUCH_MIN)
        b.clicked.connect(lambda _=False: slot())
        if mutating and not self.service:
            b.setEnabled(False)
            b.setToolTip(_tr(self.w, "plugin_manager.needs_service", "Needs Service mode"))
        b.setProperty("mutating", mutating)
        return b

    def _table(self, cols: list[str]) -> QTableWidget:
        t = QTableWidget(0, len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.verticalHeader().setDefaultSectionSize(TOUCH_MIN)
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        t.horizontalHeader().setStretchLastSection(True)
        return t

    def _installed_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.table = self._table([_tr(self.w, f"plugin_manager.col_{c}", c.capitalize()) for c in self.COLUMNS])
        self.table.setObjectName("plugin_manager_installed")
        self.table.itemSelectionChanged.connect(self._show_installed)
        lay.addWidget(self.table, 1)
        self.installed_detail = QLabel("")
        self.installed_detail.setWordWrap(True)
        self.installed_detail.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.installed_detail)
        row = QHBoxLayout()
        self.btn_enable = self._button(_tr(self.w, "plugin_manager.enable", "Enable..."), self.enable_selected, True)
        self.btn_disable = self._button(_tr(self.w, "plugin_manager.disable", "Disable"), self.disable_selected, True)
        self.btn_update = self._button(_tr(self.w, "plugin_manager.update", "Update"), self.update_selected, True)
        self.btn_uninstall = self._button(_tr(self.w, "plugin_manager.uninstall", "Uninstall..."),
                                          self.uninstall_selected, True)
        self.btn_hosts = self._button(_tr(self.w, "plugin_manager.hosts", "Network hosts..."), self.edit_hosts, True)
        self.btn_home = self._button(_tr(self.w, "plugin_manager.homepage", "Homepage"), self.open_homepage)
        for b in (self.btn_enable, self.btn_disable, self.btn_update, self.btn_uninstall, self.btn_hosts,
                  self.btn_home):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        return w

    def _browse_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName("plugin_manager_search")
        self.search.setPlaceholderText(_tr(self.w, "plugin_manager.search", "Search plugins"))
        self.search.setMinimumHeight(TOUCH_MIN)
        self.search.textChanged.connect(lambda _t: self._fill_browse())
        row.addWidget(self.search, 1)
        self.btn_refresh = self._button(_tr(self.w, "plugin_manager.refresh", "Refresh"),
                                        lambda: self.load_browse(refresh=True))
        row.addWidget(self.btn_refresh)
        lay.addLayout(row)
        self.browse = QListWidget()
        self.browse.setObjectName("plugin_manager_browse")
        self.browse.currentRowChanged.connect(lambda _r: self._show_browse())
        lay.addWidget(self.browse, 1)
        self.browse_detail = QLabel(_tr(self.w, "plugin_manager.browse_hint",
                                        "Select a plugin to see its permissions and requirements."))
        self.browse_detail.setWordWrap(True)
        self.browse_detail.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.browse_detail)
        self.btn_install = self._button(_tr(self.w, "plugin_manager.install", "Install..."),
                                        self.install_selected, True)
        lay.addWidget(self.btn_install, 0, Qt.AlignmentFlag.AlignLeft)
        self._browse_loaded = False
        self.tabs_index_browse = 1
        return w

    def _repos_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.repos = QListWidget()
        self.repos.setObjectName("plugin_manager_repositories")
        lay.addWidget(self.repos, 1)
        hint = QLabel(_tr(self.w, "plugin_manager.repo_hint",
                          "An index URL (https://.../mlaser-plugin-index.json) or github:owner/repo "
                          "(the latest release of that GitHub repository)."))
        hint.setWordWrap(True)
        lay.addWidget(hint)
        row = QHBoxLayout()
        self.repo_edit = QLineEdit()
        self.repo_edit.setObjectName("plugin_manager_repo_url")
        self.repo_edit.setPlaceholderText("github:owner/repo")
        self.repo_edit.setMinimumHeight(TOUCH_MIN)
        row.addWidget(self.repo_edit, 1)
        self.btn_repo_add = self._button(_tr(self.w, "plugin_manager.repo_add", "Add"), self.add_repository, True)
        self.btn_repo_remove = self._button(_tr(self.w, "plugin_manager.repo_remove", "Remove"),
                                            self.remove_repository, True)
        row.addWidget(self.btn_repo_add)
        row.addWidget(self.btn_repo_remove)
        lay.addLayout(row)
        if not self.service:
            self.repo_edit.setEnabled(False)
        return w

    # ------------------------------------------------------------------ helpers
    def run(self, fn: Callable[[], Any], done: Callable[[Any], Any],
            error: Optional[Callable[[BaseException], Any]] = None) -> None:
        """``fn()`` on a worker thread (``sync``: here), then ``done(result)`` / ``error(exc)`` on
        the GUI thread."""
        error = error or self._error
        if self.sync:
            try:
                res = fn()
            except Exception as exc:  # noqa: BLE001 - shown
                error(exc)
                return
            done(res)
            return
        self._set_busy(True)

        def work() -> None:
            try:
                res = fn()
            except Exception as exc:  # noqa: BLE001 - shown
                self._bridge.call.emit(lambda e=exc: self._finish(error, e))
                return
            self._bridge.call.emit(lambda: self._finish(done, res))

        threading.Thread(target=work, name="mlaser-plugin-manager", daemon=True).start()

    def _finish(self, cb: Callable[[Any], Any], arg: Any) -> None:
        self._set_busy(False)
        cb(arg)

    def _set_busy(self, on: bool) -> None:
        self._busy += 1 if on else -1
        self.status.setText(_tr(self.w, "plugin_manager.working", "Working...") if self._busy > 0 else "")

    def _say(self, text: str, error: bool = False) -> None:
        self.messages.append(text)
        self.status.setText(text)
        log = getattr(self.w, "log", None)
        if callable(log):
            log(f"plugin manager: {text}")
        if error and not getattr(self.w, "_test_no_dialogs", False):
            QMessageBox.warning(self, self.windowTitle(), text)

    def _error(self, exc: BaseException) -> None:
        self._say(str(exc), error=True)

    def _ask(self, title: str, text: str) -> bool:
        self.messages.append(text)
        if self.answer is not None:
            return bool(self.answer(title, text))
        if getattr(self.w, "_test_no_dialogs", False):
            return True
        yes, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
        return QMessageBox.question(self, title, text, yes | no, no) == yes

    def _changed(self) -> None:
        self.w._plugin_restart_pending = True
        self._update_banner()
        self.refresh_installed()

    def _update_banner(self) -> None:
        self.banner.setVisible(bool(getattr(self.w, "_plugin_restart_pending", False) or self.pm.changed))

    # ------------------------------------------------------------------ installed
    def refresh_installed(self) -> None:
        try:
            rows = self.pm.catalog()
        except Exception as exc:  # noqa: BLE001
            rows = []
            self._error(exc)
        runtime = {info.name: st for info, st in (getattr(self.w, "plugin_catalog", None) or [])}
        for r in rows:
            st = runtime.get(r["id"])
            if st in ("loaded", "failed") and r["status"] == "enabled":
                r["status"] = st
            elif r["status"] == "enabled" and r["id"] not in runtime:
                r["status"] = "enabled (restart)"
            if r["id"] in self.updates:
                r["update"] = self.updates[r["id"]]["available"]
        self.rows = rows
        t = self.table
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            perms = "; ".join(_perm_lines(r["permissions"]))
            ver = r["version"] or "-"
            if r.get("update"):
                ver += f" -> {r['update']}"
            origin = {"site": _tr(self.w, "plugin_manager.origin_site", "plugin manager"),
                      "environment": _tr(self.w, "plugin_manager.origin_env", "Python environment")}.get(
                r["origin"], r["origin"])
            status = r["status"] + (" (legacy)" if not r["manifest"] else "")
            if r.get("manifest_error"):
                status = "invalid manifest"
            for c, text in enumerate((f"{r['name']} ({r['id']})", ver, origin, status, perms)):
                it = QTableWidgetItem(text)
                it.setToolTip(text)
                t.setItem(i, c, it)
        if rows and not t.selectedItems():
            t.selectRow(0)
        self._show_installed()

    def selected(self) -> Optional[dict]:
        r = self.table.currentRow()
        return self.rows[r] if 0 <= r < len(self.rows) else None

    def select(self, plugin_id: str) -> bool:
        for i, r in enumerate(self.rows):
            if r["id"] == plugin_id:
                self.table.selectRow(i)
                return True
        return False

    def _show_installed(self) -> None:
        r = self.selected()
        on = r is not None
        if r is None:
            self.installed_detail.setText(_tr(self.w, "plugin_manager.none", "No third-party plugin is installed."))
        else:
            lines = [f"{r['name']} {r['version']}", r.get("summary") or "",
                     f"entry point: {r['entry']}" + (f"  ({r['dist']})" if r.get("dist") else ""),
                     "permissions:"] + [f"  - {x}" for x in _perm_lines(r["permissions"])]
            if r.get("granted_hosts"):
                lines.append("granted hosts: " + ", ".join(r["granted_hosts"]))
            if r.get("authority"):
                lines.append("machine authority: granted")
            if r.get("manifest_error"):
                lines.append(f"invalid manifest: {r['manifest_error']}")
            self.installed_detail.setText("\n".join(x for x in lines if x))
        enabled = on and r is not None and r["status"].startswith(("enabled", "loaded", "failed"))
        for b, ok in ((self.btn_enable, on and not enabled and not (r or {}).get("manifest_error")),
                      (self.btn_disable, on and (enabled or (r or {}).get("status") == "changed")),
                      (self.btn_update, on and bool((r or {}).get("update"))),
                      (self.btn_uninstall, on and bool((r or {}).get("installed_by_manager"))),
                      (self.btn_hosts, on and bool((r or {}).get("manifest"))),
                      (self.btn_home, on and str((r or {}).get("homepage", "")).startswith("https://"))):
            b.setEnabled(bool(ok) and (self.service or not b.property("mutating")))

    def enable_selected(self) -> bool:
        r = self.selected()
        if r is None or not self.service:
            return False
        perms = r["permissions"]
        text = _tr(self.w, "plugin_manager.confirm_enable",
                   "Enable {name}?\n\nIt declares:\n{permissions}\n\n{warning}", name=f"{r['name']} {r['version']}",
                   permissions="\n".join(f"  - {x}" for x in _perm_lines(perms)),
                   warning=_tr(self.w, "plugin_manager.warning", WARNING))
        if not r["manifest"]:
            text += "\n\n" + _tr(self.w, "plugin_manager.legacy_warning",
                                 "This plugin has no manifest (legacy): nothing limits what it does through "
                                 "mlaser's interfaces.")
        if not self._ask(self.windowTitle(), text):
            return False
        grant = False
        from mlaser.services.plugin_manifest import Permissions
        if perms is not None and Permissions.from_dict(perms).machine:
            grant = self._ask(self.windowTitle(), _tr(
                self.w, "plugin_manager.confirm_authority",
                "{name} asks for the machine controller. Grant it machine authority (it may then arm, "
                "reset faults and change the safety configuration through the controller's audited "
                "interface)?", name=r["name"]))
        try:
            self.pm.enable(r["id"], r["entry"], perms, grant_authority=grant)
        except Exception as exc:  # noqa: BLE001
            self._error(exc)
            return False
        self._say(_tr(self.w, "plugin_manager.enabled", "{name} enabled (at the next start).", name=r["name"]))
        self._changed()
        return True

    def disable_selected(self) -> bool:
        r = self.selected()
        if r is None or not self.service:
            return False
        try:
            self.pm.disable(r["id"])
        except Exception as exc:  # noqa: BLE001
            self._error(exc)
            return False
        self._say(_tr(self.w, "plugin_manager.disabled", "{name} disabled (at the next start).", name=r["name"]))
        self._changed()
        return True

    def uninstall_selected(self) -> bool:
        r = self.selected()
        if r is None or not self.service or not r.get("installed_by_manager"):
            return False
        if not self._ask(self.windowTitle(), _tr(self.w, "plugin_manager.confirm_uninstall",
                                                 "Uninstall {name}? Its settings and data folder are kept.",
                                                 name=f"{r['name']} {r['version']}")):
            return False
        def done(res: dict) -> None:
            self._say(_tr(self.w, "plugin_manager.uninstalled", "{name} uninstalled.", name=r["name"]))
            self._changed()
        self.run(lambda: self.pm.uninstall(r["id"]), done)
        return True

    def update_selected(self) -> bool:
        r = self.selected()
        if r is None or not self.service:
            return False
        up = self.updates.get(r["id"])
        if up and up.get("permissions_changed"):
            if not self._ask(self.windowTitle(), _tr(
                    self.w, "plugin_manager.confirm_update_perms",
                    "{name} {version} declares different permissions:\n{permissions}\n\nUpdate? It will be "
                    "disabled until you enable it again.", name=r["name"], version=up["available"],
                    permissions="\n".join(f"  - {x}" for x in _perm_lines(up.get("permissions"))))):
                return False

        def done(res: dict) -> None:
            self.updates.pop(r["id"], None)
            msg = _tr(self.w, "plugin_manager.updated", "{name} updated to {version}.", name=r["name"],
                      version=res.get("version", ""))
            if res.get("needs_enable"):
                msg += " " + _tr(self.w, "plugin_manager.needs_enable",
                                 "Its entry point or permissions changed: it is disabled until you enable it again.")
            self._say(msg)
            self._changed()
        self.run(lambda: self.pm.update(r["id"]), done)
        return True

    def edit_hosts(self) -> None:
        r = self.selected()
        if r is None or not self.service:
            return
        text, ok = QInputDialog.getText(
            self, _tr(self.w, "plugin_manager.hosts", "Network hosts..."),
            _tr(self.w, "plugin_manager.hosts_prompt",
                "Extra hosts {name} may contact besides its manifest (comma-separated exact host names, "
                "e.g. a company server):", name=r["name"]),
            QLineEdit.EchoMode.Normal, ", ".join(r.get("granted_hosts") or []))
        if ok:
            self.set_hosts(text)

    def set_hosts(self, text: str) -> bool:
        """Grant exactly these hosts (comma or space separated) to the selected plugin."""
        r = self.selected()
        if r is None or not self.service:
            return False
        want = [h for h in text.replace(",", " ").split() if h]
        have = list(r.get("granted_hosts") or [])
        add = [h for h in want if h.lower() not in have]
        if add and not self._ask(self.windowTitle(), _tr(
                self.w, "plugin_manager.confirm_hosts", "Allow {name} to contact {hosts}?", name=r["name"],
                hosts=", ".join(add))):
            return False
        try:
            self.pm.grant_hosts(r["id"], add=add, remove=[h for h in have if h not in [x.lower() for x in want]])
        except Exception as exc:  # noqa: BLE001
            self._error(exc)
            return False
        self._say(_tr(self.w, "plugin_manager.hosts_saved", "Network hosts of {name} saved.", name=r["name"]))
        pid = r["id"]
        self.refresh_installed()
        self.select(pid)
        return True

    def open_homepage(self) -> bool:
        r = self.selected()
        url = str((r or {}).get("homepage") or "")
        if not url.startswith("https://"):
            return False
        self.opened.append(url)
        if not getattr(self.w, "_test_no_dialogs", False):
            QDesktopServices.openUrl(QUrl(url))
        return True

    def check_updates(self) -> None:
        def done(ups: list) -> None:
            self.updates = {u["id"]: u for u in ups}
            self._say(_tr(self.w, "plugin_manager.updates_found", "{n} update(s) available.", n=len(ups))
                      if ups else _tr(self.w, "plugin_manager.up_to_date", "Every plugin is up to date."))
            self.refresh_installed()
        self.run(lambda: self.pm.check_updates(refresh=True), done)

    # ------------------------------------------------------------------ browse
    def load_browse(self, refresh: bool = False) -> None:
        def done(items: list) -> None:
            self.browse_items = items
            self._browse_loaded = True
            self._fill_browse()
            if self.pm.errors:
                self._say("; ".join(self.pm.errors[:3]))
        self.run(lambda: self.pm.available(refresh=refresh), done)

    def _tab_changed(self, i: int) -> None:
        if i == self.tabs_index_browse and not self._browse_loaded:
            self.load_browse()

    def _fill_browse(self) -> None:
        terms = [t.lower() for t in self.search.text().split()]
        self.browse.clear()
        self._shown = []
        for p in sorted(self.browse_items, key=lambda p: p.id):
            hay = " ".join([p.id, p.name, p.summary, p.description, " ".join(p.authors)]).lower()
            if terms and not all(t in hay for t in terms):
                continue
            best = p.best(self.pm.plugin_api)
            it = QListWidgetItem(f"{p.name}  ({p.id} {best.version if best else '-'})  {p.summary}")
            it.setSizeHint(QSize(it.sizeHint().width(), TOUCH_MIN))
            self.browse.addItem(it)
            self._shown.append(p)
        if self._shown:
            self.browse.setCurrentRow(0)
        self._show_browse()

    def browse_selected(self) -> Any:
        r = self.browse.currentRow()
        shown = getattr(self, "_shown", [])
        return shown[r] if 0 <= r < len(shown) else None

    def _show_browse(self) -> None:
        p = self.browse_selected()
        self.btn_install.setEnabled(p is not None and self.service)
        if p is None:
            return
        best = p.best(self.pm.plugin_api)
        lines = [f"{p.name} ({p.id})", p.summary, p.description.strip()[:600]]
        if p.authors:
            lines.append("by " + ", ".join(p.authors) + (f"  - {p.license}" if p.license else ""))
        lines.append(f"repository: {p.repository}")
        if best is None:
            newest = max(p.versions, key=lambda v: v.version)
            lines.append("no compatible version: " + ("; ".join(newest.compatible(self.pm.plugin_api)) or "pre-release only"))
        else:
            lines.append(f"version {best.version}  (api {best.api or '-'}, mlaser {best.mlaser or '-'})")
            if best.synthetic:
                lines.append("permissions: shown from the wheel before installing")
            else:
                lines += ["permissions:"] + [f"  - {x}" for x in _perm_lines(best.permissions)]
            if best.requires:
                lines.append("requires: " + ", ".join(best.requires))
        self.browse_detail.setText("\n".join(x for x in lines if x))

    def install_selected(self) -> bool:
        p = self.browse_selected()
        if p is None:
            return False
        return self.install(p.id)

    # ------------------------------------------------------------------ install
    def install(self, source: str, sha256: str = "") -> bool:
        """Verify in the background, confirm the permissions here, then install."""
        if not self.service:
            return False

        def confirmed(prep: Any) -> None:
            s = prep.info.summary()
            text = _tr(self.w, "plugin_manager.confirm_install",
                       "Install {name} {version}?\n\nIt declares:\n{permissions}\n\nsha256 {sha256}\n\n"
                       "Installing does not enable it.", name=s["name"], version=s["version"],
                       permissions="\n".join(f"  - {x}" for x in _perm_lines(s["permissions"])), sha256=s["sha256"])
            if not prep.verified_hash:
                text += "\n\n" + _tr(self.w, "plugin_manager.unverified",
                                     "No checksum was given for this download: compare the sha256 above with "
                                     "the one the author publishes before you continue.")
            if not self._ask(self.windowTitle(), text):
                self._say(_tr(self.w, "plugin_manager.cancelled", "Cancelled."))
                return
            def installed(rec: dict) -> None:
                self._say(_tr(self.w, "plugin_manager.installed", "{name} {version} installed. Enable it on the "
                              "Installed tab; it loads at the next start.", name=rec["name"],
                              version=rec["version"]))
                self._changed()
                self.select(rec["id"])
            self.run(lambda: self.pm.commit(prep), installed)

        self.run(lambda: self.pm.prepare(source, sha256=sha256, confirm_hash=lambda h, n: True), confirmed)
        return True

    def install_file(self, path: str) -> bool:
        return self.install(path)

    def choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, _tr(self.w, "plugin_manager.install_file", "Install from file..."),
                                              "", "Python wheels (*.whl)")
        if path:
            self.install_file(path)

    def choose_url(self) -> None:
        url, ok = QInputDialog.getText(self, _tr(self.w, "plugin_manager.install_url", "Install from URL..."),
                                       _tr(self.w, "plugin_manager.url_prompt",
                                           "https://.../plugin.whl#sha256=... or github:owner/repo"))
        if ok and url.strip():
            self.install(url.strip())

    # ------------------------------------------------------------------ repositories
    def refresh_repositories(self) -> None:
        self.repos.clear()
        for r in self.pm.repositories():
            it = QListWidgetItem(r["url"] + (f"   ({r['name']})" if r["name"] else ""))
            it.setData(Qt.ItemDataRole.UserRole, r["url"])
            self.repos.addItem(it)

    def add_repository(self, url: str = "") -> bool:
        url = (url or self.repo_edit.text()).strip()
        if not url or not self.service:
            return False
        try:
            res = self.pm.add_repository(url)
        except Exception as exc:  # noqa: BLE001
            self._error(exc)
            return False
        self.repo_edit.clear()
        self.refresh_repositories()
        self._browse_loaded = False
        self._say(_tr(self.w, "plugin_manager.repo_added", "Repository added: {url}", url=res["url"]))
        return True

    def remove_repository(self, url: str = "") -> bool:
        if not self.service:
            return False
        if not url:
            it = self.repos.currentItem()
            url = it.data(Qt.ItemDataRole.UserRole) if it is not None else ""
        if not url:
            return False
        try:
            self.pm.remove_repository(url)
        except Exception as exc:  # noqa: BLE001
            self._error(exc)
            return False
        self.refresh_repositories()
        self._browse_loaded = False
        return True


__all__ = ["PluginManagerDialog", "WARNING"]
