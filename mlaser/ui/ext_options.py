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
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from mlaser.services.config import Setting


def profiles() -> dict:
    """Every registered ParityProfile (the optional ones register on import)."""
    import importlib

    from mlaser.parity_ports.profile import PROFILES

    try:
        importlib.import_module("mlaser.parity_ports.profiles")
    except Exception:  # noqa: BLE001 - an optional profile that cannot load is simply not listed
        pass
    return dict(PROFILES)


class ProfileDialog(QDialog):
    def __init__(self, current: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Parity profile")
        lay = QVBoxLayout(self)
        self.profs = profiles()
        self.combo = QComboBox()
        for name in self.profs:
            self.combo.addItem(name, name)
        self.combo.setCurrentIndex(max(0, self.combo.findData(current)))
        lay.addWidget(self.combo)
        self.info = QLabel()
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.combo.currentIndexChanged.connect(self._show)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._show()

    def name(self) -> str:
        return self.combo.currentData()

    def _show(self) -> None:
        p = self.profs.get(self.name())
        if p is None:
            return
        txt = p.description
        if p.warning:
            txt += f"\n\nWARNING: {p.warning}"
        if p.name == "original":
            txt += "\n\n(bit-exact with the original; the goldens and the Wine parity run use it)"
        self.info.setText(txt)


class NetCheckDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from mlaser import netcheck

        self.setWindowTitle("Network self-test")
        lay = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        last = netcheck.last_result()
        self.text.setPlainText(last.text() if last is not None else "no self-test result yet")
        lay.addWidget(self.text)
        run = QPushButton("Run now (no packet is sent to the card)")
        run.clicked.connect(self.run)
        lay.addWidget(run)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

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


class PluginsDialog(QDialog):
    """The installed third-party plugins and the allowlist (see the module docstring)."""

    COLUMNS = ("enabled", "authority", "name", "target", "package", "status")

    def __init__(self, window, catalog: list, allowlist: dict, editable: bool = True,
                 authority: Optional[list] = None) -> None:
        super().__init__(window)
        self.w = window
        self.setObjectName("plugins_dialog")
        self.setWindowTitle(_t(window, "plugins.title", "Plugins"))
        self.resize(760, 380)
        lay = QVBoxLayout(self)
        warn = QLabel(_t(window, "plugins.warning", PLUGIN_WARNING))
        warn.setObjectName("plugins_warning")
        warn.setWordWrap(True)
        lay.addWidget(warn)
        self.catalog = [(info, st) for info, st in catalog]
        self.allow = dict(allowlist or {})
        self.authority0 = set(authority or ())
        self.table = QTableWidget(len(self.catalog), len(self.COLUMNS))
        self.table.setObjectName("plugins_table")
        self.table.setHorizontalHeaderLabels([_t(window, f"plugins.col_{c}", c.capitalize()) for c in self.COLUMNS])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
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
            lay.addWidget(QLabel(_t(window, "plugins.none", "No third-party plugin is installed.")))
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel if editable else QDialogButtonBox.Close)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

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
        if new and not getattr(self.w, "_test_no_dialogs", False):
            ok = QMessageBox.question(
                self, _t(self.w, "plugins.title", "Plugins"),
                _t(self.w, "plugins.confirm", "Enable {names}?\n\n{warning}", names=", ".join(new),
                   warning=_t(self.w, "plugins.warning", PLUGIN_WARNING)),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ok != QMessageBox.Yes:
                return
        self.accept()


def _t(window, key: str, default: str, **kw) -> str:
    from .workspaces import tr
    return tr(window, key, default, **kw)


class OptionsExtension:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.w = ctx.window
        self.s = ctx.settings_schema(
            [
                Setting("parity_profile", "str", "original", label="Parity profile"),
                Setting("dxf_modern", "bool", False, label="Modern DXF import (ezdxf)"),
                Setting("kerf_clipper", "bool", False, label="Kerf fallback: pyclipper"),
            ]
        )
        self.apply()

    def apply(self) -> None:
        """Make the stored choices active (start-up and after a change)."""
        from mlaser.parity_ports.profile import active_profile, set_profile

        name = self.s["parity_profile"]
        if name != active_profile().name:
            try:
                set_profile(name)
            except KeyError:
                self.ctx.log(f"parity profile {name!r} is not available; using 'original'")
                self.s["parity_profile"] = "original"
                set_profile("original")
        self.w.cam_params.kerf_fallback = "clipper" if self.s["kerf_clipper"] else None
        for cmd, key in (("app.dxf_modern", "dxf_modern"), ("craft.kerf_clipper", "kerf_clipper")):
            act = self.w.ribbon.actions.get(cmd)
            if act is not None and act.isChecked() != bool(self.s[key]):
                act.blockSignals(True)
                act.setChecked(bool(self.s[key]))
                act.blockSignals(False)
        self.w._update_toolpath()

    def dxf_mode(self):
        """``import_dxf`` mode for the file loaders (None = the active profile's rules)."""
        return "modern" if self.s["dxf_modern"] else None

    # ---------------------------------------------------------------- commands
    def profile(self) -> None:
        d = ProfileDialog(self.s["parity_profile"], self.w)
        if not d.exec():
            return
        self.choose_profile(d.name())

    def choose_profile(self, name: str) -> None:
        p = profiles().get(name)
        if p is None:
            return
        self.s["parity_profile"] = name
        self.apply()
        self.ctx.log(f"parity profile: {name}" + (f" - WARNING: {p.warning}" if p.warning else ""))
        if p.warning and not getattr(self.w, "_test_no_dialogs", False):
            QMessageBox.warning(self.w, "Parity profile", f"{name}: {p.warning}")

    def set_dxf_modern(self, on: bool) -> None:
        self.s["dxf_modern"] = bool(on)
        self.ctx.log(f"DXF import: {'modern (ezdxf)' if on else 'original rules'}")

    def set_kerf_clipper(self, on: bool) -> None:
        self.s["kerf_clipper"] = bool(on)
        self.apply()

    def netcheck(self) -> None:
        NetCheckDialog(self.w).exec()

    def plugins(self) -> "PluginsDialog":
        """The Plugins dialog; Save stores the allowlist (persisted at once, used at the next start)."""
        from mlaser.services.plugins import ALLOWLIST_KEY, AUTHORITY_KEY
        cfg = self.ctx.config
        d = PluginsDialog(self.w, list(getattr(self.w, "plugin_catalog", []) or []), cfg.get(ALLOWLIST_KEY, {}),
                          authority=list(cfg.get(AUTHORITY_KEY, []) or []))
        if d.exec():
            self.save_allowlist(d.checked(), d.authority())
        return d

    def plugin_manager(self):
        """The Plugin manager dialog (Engineer mode views; changes need Service mode)."""
        from .plugin_manager import PluginManagerDialog
        d = PluginManagerDialog(self.w)
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
        for info, _st in getattr(self.w, "plugin_catalog", []) or []:
            if info.name in allow and allow.get(info.name) == info.target and info.manifest is not None:
                accepted[info.name] = normalize_permissions(info.manifest.get("permissions", {}))
        cfg.set(ACCEPTED_KEY, accepted)
        try:
            cfg.save()
        except OSError as exc:
            self.ctx.log(f"plugins: the allowlist could not be saved ({exc})")
            return
        try:
            from mlaser.machine.audit import audit
            audit("plugin_allowlist", enabled=sorted(allow), before=sorted(old),
                  authority=list(cfg.get(AUTHORITY_KEY, [])))
        except Exception:  # noqa: BLE001
            pass
        self.ctx.log("plugins enabled: " + (", ".join(sorted(allow)) or "none") + " (at the next start)")


def register(ctx) -> OptionsExtension:
    ext = OptionsExtension(ctx)
    ctx.register_command("app.profile", ext.profile, label="Parity profile")
    ctx.register_command("app.dxf_modern", ext.set_dxf_modern, label="Modern DXF import", checkable=True)
    ctx.register_command(
        "craft.kerf_clipper", ext.set_kerf_clipper, label="Kerf fallback (pyclipper)", checkable=True
    )
    ctx.register_command("app.netcheck", ext.netcheck, label="Network self-test")
    ctx.register_command("app.plugins", ext.plugins, label="plugins.allowlist", min_mode="service")
    ctx.register_command("app.plugin_manager", ext.plugin_manager, label="plugins.title", min_mode="engineer")
    ctx.add_ribbon_tab("Options+", ["app.profile", "app.dxf_modern", "craft.kerf_clipper", "app.netcheck",
                                    "app.plugin_manager"])
    ctx.provide("dxf.mode", ext.dxf_mode)
    # the controller's plugin policy (Safety core): plugins arm / reset / reconfigure safety only
    # with the machine authority granted here; re-installed on every controller swap
    from mlaser.services.plugins import install_policy
    install_policy(ctx.window.ctl, ctx.config)
    ctx.on_controller_changed(lambda ctl: install_policy(ctl, ctx.config))
    ext.apply()
    return ext
