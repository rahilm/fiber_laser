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

The nest engine (raster / sparrow) is a field of the nest-parameter dialog (ext_nest).
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
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


def register(ctx) -> OptionsExtension:
    ext = OptionsExtension(ctx)
    ctx.register_command("app.profile", ext.profile, label="Parity profile")
    ctx.register_command("app.dxf_modern", ext.set_dxf_modern, label="Modern DXF import", checkable=True)
    ctx.register_command(
        "craft.kerf_clipper", ext.set_kerf_clipper, label="Kerf fallback (pyclipper)", checkable=True
    )
    ctx.register_command("app.netcheck", ext.netcheck, label="Network self-test")
    ctx.add_ribbon_tab("Options+", ["app.profile", "app.dxf_modern", "craft.kerf_clipper", "app.netcheck"])
    ctx.provide("dxf.mode", ext.dxf_mode)
    ext.apply()
    return ext
