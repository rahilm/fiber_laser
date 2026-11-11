"""Engines, profiles and the network self-test (plugin ``ext_options``).

The optional engines of the performance and libraries track, as typed plugin settings
(``plugin.ext_options.*`` in the ConfigService, persisted) with an "Options+" ribbon tab:

* ``app.profile``: the ParityProfile (``original`` is the bit-exact default; the others are
  listed with their description, and a profile's ``warning`` is shown when it is chosen, e.g.
  ``modern-ruckig``: "NOT validated on hardware").  The PlannedJob hash includes the profile.
* ``app.dxf_modern`` (checkable): DXF import through ezdxf (``import_dxf(mode="modern")``).
* ``craft.kerf_clipper`` (checkable): ``CamParams.kerf_fallback = "clipper"`` when the analytic
  kerf offset fails (pyclipper + CircleFit).
* ``app.netcheck``: the network self-test (``mlaser.netcheck``): the last result, and a run.
* ``app.plugin_manager``: the Plugin manager (:mod:`mlaser.ui.plugin_manager`): installed plugins
  with their permissions, enable / disable (Service mode), install from the repositories, a
  ``github:`` shortcut, a wheel file or URL, update and uninstall (docs/user/plugins.md).
* ``app.plugins``: the compact allowlist of the installed entry points (``plugins.allowlist``,
  ``plugins.authority``; Service mode; takes effect at the next start).  Plugins run with full
  user privileges (docs/dev/PLUGINS.md §6).

The nest engine (raster / sparrow) is a field of the nest-parameter dialog (ext_nest).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
)

from mlaser.services.config import Setting

from . import style as S
from .extension_context import ExtensionContext, dialogs_suppressed
from .extension_context import text as _text
from .style import Dialog


def profiles() -> dict:
    """Every registered ParityProfile (the optional ones register on import)."""
    import importlib

    from mlaser.parity_ports.profile import PROFILES

    try:
        importlib.import_module("mlaser.parity_ports.profiles")
    except Exception:  # noqa: BLE001 - an optional profile that cannot load is simply not listed
        pass
    return dict(PROFILES)


class ProfileDialog(Dialog):
    def __init__(self, current: str, parent=None) -> None:
        super().__init__(parent, _t("options.profile.title", "Parity profile"),
                         explanation=_t("options.profile.explanation",
                                        "How the toolpath and the motion are computed. The original "
                                        "profile is bit-exact with the original program."))
        self.profs = profiles()
        self.combo = QComboBox()
        for name in self.profs:
            self.combo.addItem(name, name)
        self.combo.setCurrentIndex(max(0, self.combo.findData(current)))
        S.form_row(self.form(), _t("options.profile.combo", "Profile"), self.combo)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.body.addWidget(self.info)
        self.combo.currentIndexChanged.connect(self._show)
        self._show()

    def name(self) -> str:
        return self.combo.currentData()

    def _show(self) -> None:
        p = self.profs.get(self.name())
        if p is None:
            return
        paras = [p.description]                  # the profile's own text (from its registration)
        if p.warning:
            paras.append(_t("options.profile.warning", "Warning: {warning}", warning=p.warning))
        if p.name == "original":
            paras.append(_t("options.profile.original_note",
                            "(Bit-exact with the original; the goldens and the Wine parity run use it.)"))
        self.info.setText("\n\n".join(paras))


class NetCheckDialog(Dialog):
    def __init__(self, parent=None) -> None:
        from mlaser import netcheck

        super().__init__(parent, _t("options.netcheck.title", "Network self-test"),
                         buttons=QDialogButtonBox.Close,
                         explanation=_t("options.netcheck.explanation",
                                        "Checks this computer's network for the motion card. "
                                        "No packet is sent to the card."))
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        last = netcheck.last_result()
        self.text.setPlainText(last.text() if last is not None else
                               _t("options.netcheck.none", "No self-test result yet."))
        self.body.addWidget(self.text, 1)
        self.run_button = self.buttons.addButton(_t("options.netcheck.run", "Run now"),
                                                 QDialogButtonBox.ActionRole)
        self.run_button.clicked.connect(self.run)

    def run(self) -> None:
        from mlaser import netcheck

        res = netcheck.run()
        try:
            netcheck.save_result(res)
        except OSError:
            pass
        self.text.setPlainText(res.text())


PLUGIN_WARNING = (
    "A plugin is program code that runs inside mlaser with your user account's rights and the "
    "machine's authority: it can read and write your files, use the network and command the "
    "motion card. The safety guard stops accidents, not a hostile plugin. Enable only plugins "
    "you trust, from a source you trust. Changes take effect at the next start.")


class PluginsDialog(Dialog):
    """The installed third-party plugins and the allowlist (see the module docstring)."""

    COLUMNS = ("enabled", "authority", "name", "target", "package", "status")

    def __init__(self, window, catalog: list, allowlist: dict, editable: bool = True,
                 authority: Optional[list] = None) -> None:
        super().__init__(window, _t("plugins.title", "Plugins"),
                         buttons=QDialogButtonBox.Save | QDialogButtonBox.Cancel if editable else QDialogButtonBox.Close,
                         explanation=_t("plugins.warning", PLUGIN_WARNING))
        self._parent_window = window
        self.setObjectName("plugins_dialog")
        warn = self.explanation
        warn.setObjectName("plugins_warning")
        S.chip(warn, "warning")
        lay = self.body
        self.catalog = [(info, st) for info, st in catalog]
        self.allow = dict(allowlist or {})
        self.authority0 = set(authority or ())
        self.table = S.table_look(QTableWidget(len(self.catalog), len(self.COLUMNS)))
        self.table.setObjectName("plugins_table")
        self.table.setHorizontalHeaderLabels([_t(f"plugins.col_{c}", c.capitalize()) for c in self.COLUMNS])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        from .responsive import ColumnFitter      # no sideways scrolling: later columns hide first
        n = len(self.COLUMNS)
        # the "enabled" check box stays longest (it is the column one edits), then status,
        # authority, target, package
        self.table.column_fitter = ColumnFitter(self.table, {0: 10, 1: 4, 2: None, 3: 2, 4: 1, 5: 3}, stretch=2)
        for r, (info, st) in enumerate(self.catalog):
            chk = QTableWidgetItem()
            flags = Qt.ItemIsUserCheckable | Qt.ItemIsSelectable
            if editable:
                flags |= Qt.ItemIsEnabled
            chk.setFlags(flags)
            chk.setCheckState(Qt.Checked if self.allow.get(info.name) == info.target else Qt.Unchecked)
            self.table.setItem(r, 0, chk)
            # machine authority: the plugin may arm, reset faults, hold the heartbeat or change the
            # safety configuration (the controller's plugin policy; refused otherwise)
            auth = QTableWidgetItem()
            auth.setFlags(flags)
            auth.setCheckState(Qt.Checked if info.name in self.authority0 else Qt.Unchecked)
            self.table.setItem(r, 1, auth)
            for c, text in enumerate((info.name, info.target, info.dist, st), 2):
                it = QTableWidgetItem(text)
                it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                self.table.setItem(r, c, it)
        lay.addWidget(self.table, 1)
        if not self.catalog:
            lay.addWidget(QLabel(_t("plugins.none", "No third-party plugin is installed.")))
        self.buttons.accepted.disconnect()
        self.buttons.accepted.connect(self._save)

    def checked(self) -> dict:
        """{name: target} of the rows checked now."""
        out = {}
        for r, (info, _st) in enumerate(self.catalog):
            if self.table.item(r, 0).checkState() == Qt.Checked:
                out[info.name] = info.target
        return out

    def authority(self) -> list[str]:
        """Names granted machine authority (only enabled plugins can hold it)."""
        on = self.checked()
        return sorted(info.name for r, (info, _st) in enumerate(self.catalog)
                      if self.table.item(r, 1).checkState() == Qt.Checked and info.name in on)

    def new_ones(self) -> list[str]:
        return [n for n, t in self.checked().items() if self.allow.get(n) != t]

    def _save(self) -> None:
        new = self.new_ones() + [n for n in self.authority() if n not in self.authority0]
        if new and not dialogs_suppressed(self._parent_window):
            ok = QMessageBox.question(
                self, _t("plugins.title", "Plugins"),
                _t("plugins.confirm", "Enable {names}?\n\n{warning}", names=", ".join(new),
                   warning=_t("plugins.warning", PLUGIN_WARNING)),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ok != QMessageBox.Yes:
                return
        self.accept()


def _t(key: str, default: str, **kw) -> str:
    return _text(key, default, **kw)


class OptionsExtension:
    def __init__(self, ctx: ExtensionContext) -> None:
        self.ctx = ctx
        self.s = ctx.settings_schema(
            [
                Setting("parity_profile", "str", "original", label="Parity profile"),
                Setting("dxf_modern", "bool", False, label="Modern DXF import (ezdxf)"),
                Setting("kerf_clipper", "bool", False, label="Kerf fallback: pyclipper"),
            ]
        )
        self.apply()
        # the Settings window writes these keys directly: make the change active at once
        ctx.config.subscribe(self._config_changed)

    def _config_changed(self, key: str) -> None:
        if key in ("*", "plugin.ext_options.parity_profile", "plugin.ext_options.kerf_clipper",
                   "plugin.ext_options.dxf_modern"):
            self.apply()

    def apply(self) -> None:
        """Make the stored choices active (start-up and after a change)."""
        from mlaser.parity_ports.profile import active_profile, set_profile

        name = self.s["parity_profile"]
        if name != active_profile().name:
            try:
                set_profile(name)
            except KeyError:
                self.ctx.log(_t("options.profile.unavailable",
                                "Parity profile {name} is not available; using 'original'", name=repr(name)))
                self.s["parity_profile"] = "original"
                set_profile("original")
        self.ctx.cam_params.kerf_fallback = "clipper" if self.s["kerf_clipper"] else None
        for cmd, key in (("app.dxf_modern", "dxf_modern"), ("craft.kerf_clipper", "kerf_clipper")):
            act = self.ctx.action(cmd)
            if act is not None and act.isChecked() != bool(self.s[key]):
                act.blockSignals(True)
                act.setChecked(bool(self.s[key]))
                act.blockSignals(False)
        self.ctx.update_toolpath()

    def dxf_mode(self):
        """``import_dxf`` mode for the file loaders (None = the active profile's rules)."""
        return "modern" if self.s["dxf_modern"] else None

    # ---------------------------------------------------------------- commands
    def profile(self) -> None:
        d = ProfileDialog(self.s["parity_profile"], self.ctx.parent)
        if not d.exec():
            return
        self.choose_profile(d.name())

    def choose_profile(self, name: str) -> None:
        p = profiles().get(name)
        if p is None:
            return
        self.s["parity_profile"] = name
        self.apply()
        self.ctx.log(_t("options.profile.chosen_warning", "Parity profile: {name} - warning: {warning}",
                        name=name, warning=p.warning) if p.warning else
                     _t("options.profile.chosen", "Parity profile: {name}", name=name))
        if p.warning and not self.ctx.dialogs_suppressed:
            QMessageBox.warning(self.ctx.parent, _t("options.profile.title", "Parity profile"),
                                _t("options.profile.warning_box", "{name}: {warning}", name=name,
                                   warning=p.warning))

    def set_dxf_modern(self, on: bool) -> None:
        self.s["dxf_modern"] = bool(on)
        self.ctx.log(_t("options.dxf.modern", "DXF import: modern (ezdxf)") if on
                     else _t("options.dxf.original", "DXF import: original rules"))

    def set_kerf_clipper(self, on: bool) -> None:
        self.s["kerf_clipper"] = bool(on)
        self.apply()

    def netcheck(self) -> None:
        NetCheckDialog(self.ctx.parent).exec()

    def plugins(self):
        """``app.plugins``: the allowlist's one home is the Plugin manager (enable / disable,
        machine authority, permissions); this command opens it."""
        return self.plugin_manager()

    def allowlist_dialog(self) -> "PluginsDialog":
        """The compact allowlist dialog (kept for the plugin-trust tests and scripts); Save stores
        the allowlist (persisted at once, used at the next start)."""
        from mlaser.services.plugins import ALLOWLIST_KEY, AUTHORITY_KEY
        cfg = self.ctx.config
        d = PluginsDialog(self.ctx.parent, list(self.ctx.plugin_catalog), cfg.get(ALLOWLIST_KEY, {}),
                          authority=list(cfg.get(AUTHORITY_KEY, []) or []))
        if d.exec():
            self.save_allowlist(d.checked(), d.authority())
        return d

    def plugin_manager(self):
        """The Plugin manager dialog (Engineer mode views; changes need Service mode)."""
        from .plugin_manager import PluginManagerDialog
        d = PluginManagerDialog(self.ctx.parent)
        d.exec()
        return d

    def save_allowlist(self, allow: dict, authority: Optional[list] = None) -> None:
        from mlaser.services.plugins import ACCEPTED_KEY, ALLOWLIST_KEY, AUTHORITY_KEY
        cfg = self.ctx.config
        old = dict(cfg.get(ALLOWLIST_KEY, {}) or {})
        cfg.set(ALLOWLIST_KEY, dict(allow))
        cfg.set(AUTHORITY_KEY, sorted(n for n in (authority or []) if n in allow))
        # the manifest permissions accepted now (a later change needs enabling again)
        from mlaser.services.plugin_manifest import normalize_permissions
        accepted = {k: v for k, v in (cfg.get(ACCEPTED_KEY, {}) or {}).items() if k in allow}
        for info, _st in self.ctx.plugin_catalog:
            if info.name in allow and allow.get(info.name) == info.target and info.manifest is not None:
                accepted[info.name] = normalize_permissions(info.manifest.get("permissions", {}))
        cfg.set(ACCEPTED_KEY, accepted)
        try:
            cfg.save()
        except OSError as exc:
            self.ctx.log(_t("options.plugins.not_saved", "Plugins: the allowlist could not be saved ({error})",
                            error=exc))
            return
        try:
            from mlaser.machine.audit import audit
            audit("plugin_allowlist", enabled=sorted(allow), before=sorted(old),
                  authority=list(cfg.get(AUTHORITY_KEY, [])))
        except Exception:  # noqa: BLE001
            pass
        self.ctx.log(_t("options.plugins.enabled", "Plugins enabled: {names} (at the next start)",
                        names=", ".join(sorted(allow)) or _t("options.plugins.none", "none")))


def register(ctx: ExtensionContext) -> OptionsExtension:
    ext = OptionsExtension(ctx)
    # labels: the catalogue's cmd.<id> (the window looks it up for a command without a label)
    ctx.register_command("app.profile", ext.profile)
    ctx.register_command("app.dxf_modern", ext.set_dxf_modern, checkable=True)
    ctx.register_command("craft.kerf_clipper", ext.set_kerf_clipper, checkable=True)
    ctx.register_command("app.netcheck", ext.netcheck)
    ctx.register_command("app.plugins", ext.plugins, label="plugins.allowlist", min_mode="service")
    ctx.register_command("app.plugin_manager", ext.plugin_manager, label="plugins.title", min_mode="engineer")
    ctx.add_ribbon_tab(_t("options.tab", "Options+"), ["app.profile", "app.dxf_modern", "craft.kerf_clipper", "app.netcheck",
                                    "app.plugin_manager"])
    ctx.provide("dxf.mode", ext.dxf_mode)
    # the controller's plugin policy (Safety core): plugins arm / reset / reconfigure safety only
    # with the machine authority granted here; re-installed on every controller swap
    from mlaser.services.plugins import install_policy
    install_policy(ctx.controller, ctx.config)
    ctx.on_controller_changed(lambda ctl: install_policy(ctl, ctx.config))
    ext.apply()
    return ext
