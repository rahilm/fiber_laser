"""Machine-operation commands (agent H, phase 3): edge seek, laser type, coordinate systems,
AF home, network test, restart, firmware upgrade, offline job, parameter backup / restore,
task files and system recovery; and (Wave 3) the Machine workspace
(:mod:`mlaser.ui.workspaces.machine`), the guided procedures (:mod:`mlaser.ui.wizards`: edge
seek in every mode, follower calibration, focus homing, laser test, commissioning) and the
settings import from an original installation.  Every command declares the lowest user mode
(:data:`COMMAND_MIN_MODES`).

``register(ctx)`` (PluginContext) binds the commands and re-attaches on every controller change
(``ctx.on_controller_changed``).  The logic lives in :mod:`mlaser.controller.machine_ops` and the
modules it uses; this file is the Qt part.  MachineOps' values are the ConfigService namespace
``machine_ops`` (they fall back to the Options the user set, so hardware options reach it).
Long card operations run in worker threads; results come back through a queued Qt signal.
Evidence: docs/re/protocol.md §16-§21.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QInputDialog, QLineEdit,
                               QMessageBox, QWidget)

from mlaser.controller import coords as C
from mlaser.controller import edgeseek as E
from mlaser.controller.machine_ops import CO2, FIBER, MachineOps
from mlaser.services.config import NamespaceView

from . import style as S
from .extension_context import ExtensionContext
from .i18n import translator
from .style import Dialog

UPGRADE_WORD = "UPGRADE"


def _tr(key: str, default: str, **values) -> str:
    """Text of ``key`` (``default`` when unknown), ``{placeholders}`` filled from ``values``."""
    tr = translator()
    try:
        s = tr.format(key, **values) if values and tr.has(key) else tr.tr(key)
    except Exception:  # noqa: BLE001
        s = None
    if not s or s == key:
        s = default
        if values:
            try:
                s = default.format(**values)
            except (KeyError, IndexError, ValueError):
                pass
    return s


def _plural(base: str, n: int, **values) -> str:
    return translator().plural(base, n, **values)


def param_restore_plan(current, restored, config=None):
    """What a parameter restore changes, as the parameter import's plan (a "backup" plan: one
    area, every value current -> restored): each parameter of the hardware and manual tables by
    its setting key (labels, units and the motion-related marks come from it), any other value
    (the layer tables) by its XML path."""
    from mlaser.formats.params import ParamStore, param_defs
    from mlaser.services import settings_import as SI
    from mlaser.services.config import _XML_KEYS, registry_settings

    registry_settings()                                   # fills _XML_KEYS
    plan = SI.ImportPlan(None, kind="backup")
    seen: set = set()
    for d in param_defs():
        if d.file not in ("HardPara.xml", "ManuPara.xml") or "." not in d.key:
            continue
        old_st, new_st = (current.hard, restored.hard) if d.file == "HardPara.xml" else (current.manu, restored.manu)
        full = ParamStore.full_key(d.category, d.key)
        seen.add(full)
        a, b = old_st.values.get(full), new_st.values.get(full)
        if a == b or b is None:
            continue
        key = _XML_KEYS.get((d.category, d.key), f"{d.category}.{d.key}")
        try:
            old, new = (d.parse(a) if a is not None else None), d.parse(b)
        except (TypeError, ValueError):
            old, new = a, b
        if old == new:
            continue
        s = config.setting(key) if config is not None and hasattr(config, "setting") else None
        plan.changes.append(SI.Change("params", key, s.label if s else "", old, new, s.unit if s else ""))
    for old_st, new_st in ((current.hard, restored.hard), (current.manu, restored.manu),
                           (current.layer, restored.layer)):
        for full in sorted(set(new_st.values) - seen):
            a, b = old_st.values.get(full), new_st.values[full]
            if a != b:
                plan.changes.append(SI.Change("params", full.lstrip("/"), "", a, b))
    return plan


_RESTORE_DIALOG: list = []


def ParamRestoreDialog(ext, config, plan, title_key: str, source: str, do_restore):  # noqa: N802 - a class
    """The parameter import's review for "Restore parameters from the motion card / a file"
    (the class is made on first use: the import dialog's module loads only then)."""
    if not _RESTORE_DIALOG:
        _RESTORE_DIALOG.append(_restore_dialog_class())
    return _RESTORE_DIALOG[0](ext, config, plan, title_key, source, do_restore)


def _restore_dialog_class():
    from .wizards.settings_import import SettingsImportDialog, _value

    class _ParamRestoreDialog(SettingsImportDialog):
        """The list of changes (no folder to choose: the source is given); Restore asks once
        more with the motion-related values listed, backs up first and restores."""

        def __init__(self, ext, config, plan, title_key: str, source: str, do_restore) -> None:
            super().__init__(ext.ctx.parent, config)
            self.ext, self.title_key, self.source, self.do_restore = ext, title_key, source, do_restore
            self.setWindowTitle(_tr(title_key, ""))
            lay = self.layout()
            intro = lay.itemAt(0).widget()
            intro.setTextFormat(Qt.PlainText)
            intro.setText(_tr("machine.params.restore_review", "", source=source))
            _hide_layout(lay.itemAt(1).layout())          # the installation / parameter folders
            for b in (self.b_compare, self.b_preview):
                b.setVisible(False)
            self.tree.setHeaderLabels([_tr("wizard.import.c_setting", ""), _tr("wizard.import.c_label", ""),
                                       _tr("wizard.import.c_current", ""), _tr("machine.params.c_restored", ""),
                                       _tr("wizard.import.c_safety", "")])
            self._fill(plan)
            self.status.setText("" if plan.changes else _tr("wizard.import.no_difference", ""))

        def confirm(self, areas: list) -> bool:
            plan = self.plan
            safe = plan.safety_changes(areas) if plan is not None else []
            detail = "\n".join(f"{c.key}  {self._label(c)}: {_value(c.old, c.unit, c.key)} → "
                               f"{_value(c.new, c.unit, c.key)}" for c in safe)
            text = _plural("machine.params.restore_count", len(plan.changes) if plan is not None else 0)
            if safe:
                text = "\n\n".join((text, _plural("wizard.import.confirm_safety", len(safe))))
            return self.ext._confirm_restore(self.title_key, text, detail=detail)

        def apply(self, confirm: bool = True):
            if self.plan is None or not self.plan.changes:
                return None
            if confirm and not self.confirm(["params"]):
                self.status.setText(_tr("wizard.import.cancelled", ""))
                return None
            try:
                self.result = self.do_restore()
            except Exception as e:  # noqa: BLE001 - said in the dialog and the log
                self.status.setText(_tr("machine.params.restore_failed", "", error=e))
                self.ext.log(self.status.text())
                return None
            self.status.setText(_tr("machine.params.restored", "",
                                    path=str(getattr(self.ext, "restore_backup", "") or "")))
            self.b_apply.setEnabled(False)
            return self.result

    return _ParamRestoreDialog


def _hide_layout(lay) -> None:
    if lay is None:
        return
    for i in range(lay.count()):
        it = lay.itemAt(i)
        if it.widget() is not None:
            it.widget().setVisible(False)
        elif it.layout() is not None:
            _hide_layout(it.layout())


class _Bridge(QObject):
    call = Signal(object)          # callable to run in the GUI thread


class UpgradeConfirmDialog(Dialog):
    """Strong confirmation: the user must type UPGRADE (an interrupted or wrong image can brick
    the board)."""

    def __init__(self, parent, what: str, fw):
        """``what``: ``"mc"`` / ``"af"`` / ``"ec"`` (the board), or a name of the board."""
        if what in ("mc", "af", "ec"):
            heading = _tr(f"machine.upgrade.heading_{what}", "Firmware upgrade")
        else:
            heading = _tr("machine.upgrade.heading", "Firmware upgrade of the {what}", what=what)
        super().__init__(parent, _tr("mf132", "Upgrade"),
                         heading_text=heading,
                         explanation=_tr("machine.upgrade.explanation",
                                         "File: {file} ({size} bytes). A wrong file or an interrupted transfer "
                                         "can make the board unusable. Do not switch off the machine or "
                                         "disconnect the network until it has finished.",
                                         file=fw.path.name, size=len(fw.data)))
        self.edit = QLineEdit()
        # the confirmation word itself stays UPGRADE in every language (typed, not read)
        lab = S.form_row(self.form(), _tr("machine.upgrade.type", "Type {word} to continue",
                                          word=UPGRADE_WORD), self.edit)
        lab.setWordWrap(True)
        self.bb = self.buttons
        ok = self.bb.button(QDialogButtonBox.Ok)
        first = _tr("machine.upgrade.first", "Type {word} first", word=UPGRADE_WORD)
        S.set_enabled(ok, False, first)
        self.edit.textChanged.connect(lambda t: S.set_enabled(ok, t.strip() == UPGRADE_WORD, first))


class RegistryValues(NamespaceView):
    """The machine operations' values with **one home per key** (WORK_ITEMS R-2): a key of the
    ConfigService schema (``MSC.CoordSysType``, ``MC.MarkPtIndex``, ``GRP.EdgeSeek*`` ...) is
    read and written as the registry key itself (Machine › Setup edits it, the operations see
    the change at once); only the operations' own state (``param_dir``, ``edge_angle`` ...) lives
    under ``machine_ops.``."""

    def __init__(self, svc, prefix: str = "machine_ops") -> None:
        super().__init__(svc, prefix, registry_fallback=True)

    def __getitem__(self, key: str):
        if key in self._svc.schema:
            if key in self._svc.values:
                return self._svc.values[key]
            raise KeyError(key)
        return super().__getitem__(key)

    def __setitem__(self, key: str, value) -> None:
        if key in self._svc.schema:
            self._svc.set(key, value)
            return
        super().__setitem__(key, value)

    def migrate(self, log: Optional[Callable[[str], None]] = None) -> list[str]:
        """Fold the old ``machine_ops.<registry key>`` copies into the registry key (the copy
        wins: it is the value the machine ran with) and drop them.  Returns the keys moved."""
        moved = []
        for key in self._own():
            if key not in self._svc.schema:
                continue
            shadow = self._svc.values.get(self._p + key)
            if self._svc.values.get(key) != shadow:
                try:
                    self._svc.set(key, shadow)
                    moved.append(key)
                    if log is not None:
                        log(_tr("machine.log.setting_moved",
                                "Settings: {setting} = {value} (moved from machine_ops.{setting})", setting=key,
                                value=repr(shadow)))
                except ValueError as exc:
                    if log is not None:
                        log(_tr("machine.log.setting_not_moved",
                                "Settings: machine_ops.{setting} = {value} not moved ({error})", setting=key,
                                value=repr(shadow), error=exc))
            self._svc.reset(self._p + key)
        return moved


#: registry keys the machine operations hold objects for (rebuilt when one changes)
COORD_KEYS = frozenset({"MSC.CoordSysType", "MSC.ToolCoordPtStr", "MSC.MarkPtStr", "MSC.DockPtType",
                        "MC.XFastMoveSpeed", "MC.XFastMoveAcc"})
EDGE_KEYS = frozenset(E.EdgeSeekParams.KEYS.values()) | {"GRP.EdgeSeekStartPointX", "GRP.EdgeSeekStartPointY"}


class MachineExtension:
    def __init__(self, ctx: ExtensionContext, app_dir: Optional[str] = None):
        self.ctx = ctx
        self.bridge = _Bridge()
        self.bridge.call.connect(lambda fn: fn())
        self.values = RegistryValues(ctx.config)
        self.values.migrate(self.log)
        self.ops = MachineOps(ctx.controller, self.values, app_dir, log=self.log)
        ctx.config.subscribe(self._config_changed)
        self.layer_tables: dict[int, dict] = {}
        self._params = None
        self.workspace = None                 # the Machine workspace (workspaces.machine)
        self.last_wizard: Optional[QDialog] = None
        from mlaser.services.alarm_history import AlarmHistory, default_history
        try:
            self.history = default_history()
        except Exception as exc:  # noqa: BLE001 - an optional data file never removes the workspace
            self.history = AlarmHistory(None)
            self.history.load_error = f"{type(exc).__name__}: {exc}"
        if self.history.load_error:           # damaged lines skipped, the file kept (review A9)
            ctx.log(_tr("machine.log.history_damaged", "Alarm history: {error} (the file was kept as {path})",
                        error=self.history.load_error, path=f"{self.history.path}.bad"))

    def _config_changed(self, key: str) -> None:
        """A registry value the operations hold changed (Machine › Setup, a wizard, an import):
        rebuilt at once, no restart."""
        if key in COORD_KEYS:
            self.ops.coords = C.CoordSystems.from_values(self.values)
            self.ctx.controller.coords = self.ops.coords          # as select_coord does
        if key in EDGE_KEYS:
            self.ops.edge = E.EdgeSeekParams.from_values(self.values)

    # ------------------------------------------------------------------ helpers
    def log(self, msg: str) -> None:
        panel = self.ctx.log_panel
        if panel is not None:
            try:
                self.bridge.call.emit(lambda m=msg: panel.log(m))
                return
            except RuntimeError:
                pass

    def gui(self, fn: Callable) -> None:
        self.bridge.call.emit(fn)

    def attach(self) -> None:
        self.ops.ctl = self.ctx.controller
        self.ops._set_ctl_laser_type(self.ops.laser_type)
        panel = self.ctx.control
        if panel is not None:
            panel.machine_ops = self.ops
        ctl = self.ctx.controller                      # MachineAPI: every controller has these
        if self.startup_homing not in ctl.connect_hooks:
            ctl.connect_hooks.append(self.startup_homing)
        if self.ops.coords.kind != C.FLOAT:
            ctl.coords = self.ops.coords
        if self.autosave not in ctl.job_start_hooks:
            ctl.job_start_hooks.append(self.autosave)
        bus = ctl.events
        if getattr(self, "_homing_bus", None) is not bus:
            from mlaser.machine import events as EV
            bus.subscribe(EV.HomingDone, self._homing_done)
            self._homing_bus = bus
        self.history.attach(ctl.events)       # alarm history follows the controller

    def params(self):
        """MachineParams of the parameter directory (defaults when none exist yet)."""
        if self._params is None:
            from mlaser.formats.backup import default_param_dir
            from mlaser.formats.params import MachineParams
            d = Path(self.values.get("param_dir") or default_param_dir())
            self._params = MachineParams.load_dir(d) if (d / "HardPara.xml").exists() else MachineParams.defaults()
        return self._params

    def _ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(self.ctx.parent, title, text, QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes

    def _set_doc(self, doc, label: str = "machine") -> None:
        """A new document (task import, recovery): undoable."""
        self.ctx.replace_document(doc, label)

    # ------------------------------------------------------------------ edge seek
    def _apply_edge(self, res: E.EdgeSeekResult, set_origin: bool = False) -> None:
        """FUN_00557d90 / step 0x2a: cfg+0x4c60 := angle, cfg+0x4bc4 := 1, rotate the drawing
        about the anchor (the job origin)."""
        from mlaser.services.procedures.edge_seek import apply_to_docs
        # an undoable edit of the document's rotation state (it used to rotate without undo)
        apply_to_docs(self.ctx, res)
        self.ops.memorised_angle = res.angle
        self.ops.last_edge = res
        self.ops.save_values()
        if set_origin and self.ops.coords.can_set_or_return:
            self.ops.set_origin()
        self.log(_tr("machine.log.edge_angle", "Current memorised edge-seek angle: {angle}",
                     angle=f"{res.angle_deg:.2f}"))

    def apply_edge_result(self, res: E.EdgeSeekResult) -> None:
        """The edge-seek wizard's Apply (GUI thread)."""
        self._apply_edge(res)

    def edge_clear(self) -> None:
        self.ctx.edit(E.clear_document, "clear edge seek", parts=())
        self.log(_tr("machine.log.edge_cleared", "Edge-seek angle cleared"))

    # ------------------------------------------------------------------ wizards / workspace
    def allowed(self, cmd: str) -> bool:
        """The current user mode may run ``cmd`` (else a message says which mode it needs)."""
        from mlaser.services.procedures import mode_allows

        from .workspaces.machine import current_mode
        need = COMMAND_MIN_MODES.get(cmd, "operator")
        if mode_allows(current_mode(self.ctx), need):
            return True
        msg = _tr("machine.log.mode_needed", "{text} ({mode})",
                  text=_tr("ws.machine.mode_needed", "needs a higher user mode"), mode=need)
        self.log(msg)
        if not self.ctx.dialogs_suppressed:
            return self.offer_mode(cmd, need)
        return False

    def offer_mode(self, cmd: str, need: str) -> bool:
        """Say in place which user mode ``cmd`` needs and offer the switch (UX audit O3): the
        shell asks the PIN when one protects that mode.  True when the mode was switched."""
        shell = self.ctx.shell
        mode_name = _tr(f"shell.mode.{need}", need)
        box = QMessageBox(QMessageBox.Information, _tr("ws.machine.title", "Machine"),
                          _tr("machine.mode_offer", "{what} needs {mode} mode.", what=self._label(cmd),
                              mode=mode_name), QMessageBox.NoButton, self.ctx.parent)
        box.setInformativeText(_tr("machine.mode_offer_why", ""))
        switch = None
        if shell is not None and hasattr(shell, "request_mode"):
            switch = box.addButton(_tr("machine.mode_switch", "Switch to {mode} mode", mode=mode_name),
                                   QMessageBox.AcceptRole)
        cancel = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        if switch is not None and box.clickedButton() is switch:
            return bool(shell.request_mode(need))
        return False

    def _label(self, cmd: str) -> str:
        """The command's label as the menus show it (its id when it has none)."""
        a = self.ctx.action(cmd)
        text = a.text().replace("&", "") if a is not None else ""
        return text.rstrip(".…") or cmd

    def _wizard(self, cmd: str, make: Callable[[], QDialog]) -> Optional[QDialog]:
        if not self.allowed(cmd):
            return None
        d = make()
        self.last_wizard = d
        d.exec()
        return d

    def wizard_edge(self, mode: str) -> Optional[QDialog]:
        from .wizards.procedures import EdgeSeekWizard
        cmd = {"cap3": "wizard.edge_seek_cap3", "cap4": "wizard.edge_seek_cap4", "disc": "wizard.edge_seek_disc",
               "two_point": "wizard.edge_seek_two_point"}[mode]
        return self._wizard(cmd, lambda: EdgeSeekWizard(self.ctx.parent, self, mode))

    def edge_capacitive(self) -> Optional[QDialog]:
        """Ribbon "Capacitor seeking": the guided wizard in the configured 3/4-point mode."""
        return self.wizard_edge("cap4" if self.ops.edge.mode == 4 else "cap3")

    def wizard_follower(self) -> Optional[QDialog]:
        from .wizards.procedures import FollowerWizard
        path = self.ctx.config.get("machine.calib_csv", None)
        return self._wizard("wizard.follower_calibration", lambda: FollowerWizard(self.ctx.parent, path))

    def wizard_focus(self) -> Optional[QDialog]:
        from .wizards.procedures import FocusWizard
        return self._wizard("wizard.focus_home", lambda: FocusWizard(self.ctx.parent, self))

    def wizard_laser(self) -> Optional[QDialog]:
        from .wizards.procedures import LaserWizard
        return self._wizard("wizard.laser_test", lambda: LaserWizard(self.ctx.parent))

    def safety_io(self) -> Optional[QDialog]:
        """The Safety I/O dialog: interlock inputs and the heartbeat output (Engineer / Service)."""
        from .safety_io import open_dialog
        if self.ctx.dialogs_suppressed:
            from .safety_io import SafetyIODialog
            return SafetyIODialog(self.ctx.parent)
        return open_dialog(self.ctx.parent)

    def wizard_commissioning(self) -> Optional[QDialog]:
        from .wizards.procedures import CommissioningWizard
        return self._wizard("wizard.commissioning", lambda: CommissioningWizard(self.ctx.parent, self))

    def import_install(self) -> Optional[QDialog]:
        from .wizards.settings_import import SettingsImportDialog
        return self._wizard("machine.import_install", lambda: SettingsImportDialog(self.ctx.parent, self.ctx.config))

    def show_workspace(self, section: str = "") -> Optional[QWidget]:
        ws = self.workspace
        if ws is None:
            return None
        if section:
            ws.show_section(section)
        fn = self.ctx.provider("workspace.show")     # the Shell's switcher, when present
        if fn is not None:
            fn(f"{self.ctx.name}.machine")
        elif ws.parentWidget() is None:
            if not ws.isVisible():
                ws.resize(1180, 780)
            ws.show()
            ws.raise_()
        return ws

    def show_setup(self) -> None:
        """Machine › Setup (``hw.options`` comes here: one editor for the machine settings)."""
        self.ctx.show_workspace("machine")
        if self.workspace is not None:
            self.workspace.show_section("settings")

    # ------------------------------------------------------------------ laser type
    def laser_type(self) -> None:
        names = [_tr("A241024_0", "Fiber laser"), _tr("A241024_1", "CO2 laser")]
        item, ok = QInputDialog.getItem(self.ctx.parent, _tr("gp120", "Laser"), "", names,
                                        min(self.ops.laser_type, 1), False)
        if ok and item in names:
            self.set_laser(names.index(item))
        else:
            self.log(f"gp120: current laser type {names[min(self.ops.laser_type, 1)]}")

    def set_laser(self, new: int) -> None:
        old = self.ops.laser_type
        if not self.ops.switch_laser_type(new):
            return
        # FUN_00458c60: the layer records come from the table of the current laser type
        self.layer_tables[old] = dict(self.ctx.document.layers)

        def swap_layers(doc):
            if new in self.layer_tables:
                doc.layers = dict(self.layer_tables[new])
            else:
                mp = self.params()
                doc.layers = {i + 1: mp.layer_params(i, co2=(new != FIBER)) for i in range(11)}
        self.ctx.edit(swap_layers, "laser type", parts=())

    # ------------------------------------------------------------------ coordinates
    def coord_select(self) -> None:
        names = [_tr(C.NAMES[i], C.NAMES[i]) for i in range(8)]
        item, ok = QInputDialog.getItem(self.ctx.parent, _tr("mf307", "Coordinate"), "", names,
                                        self.ops.coords.kind, False)
        if ok and item in names:
            self.select_coord(names.index(item))
        else:
            self.log(_tr("machine.log.coord_current", "Coordinate: coordinate system {name}",
                         name=names[self.ops.coords.kind]))

    def select_coord(self, kind: int) -> None:
        self.ops.select_coord(kind)
        self.ctx.controller.coords = self.ops.coords
        self.log(_tr("machine.log.coord_selected", "Coordinate: {name}",
                     name=_tr(C.NAMES[kind], C.NAMES[kind])))

    # ------------------------------------------------------------------ hardware
    def net_test(self) -> None:
        from mlaser.controller.nettest import network_test
        ip = self.ctx.config.get("ip", None) or {}
        card = self.ctx.controller.endpoint() or (ip.get("pd445", "10.1.1.168"), 502)   # MachineAPI
        follower = (ip.get("pd448", "10.1.1.169"), 502)      # pd448: FTC IP (pd447 is a gateway)
        self.log(_tr("machine.log.nettest", "Network test: testing {card} and {follower} ...",
                     card=card[0], follower=follower[0]))
        self.ops.run_async("net-test", lambda: [self.log(r.summary()) for r in network_test(card, follower)])

    def restart(self) -> None:
        if not self.ops.require_card("mf131 restart"):
            return
        if self._ask(_tr("mf131", "Restart"),
                     _tr("machine.restart.confirm", "Restart the motion card now?")):
            self.ops.restart()

    def _upgrade(self, kind: str) -> None:
        from mlaser.controller import upgrade as U
        key = {"mc": "mf132", "af": "mf440", "ec": "mf625"}[kind]
        title = _tr(key, "Upgrade")
        if not self.ops.require_card(f"{key} upgrade"):
            return
        if self.ops.busy():
            self.log(_tr("machine.log.machine_busy", "{what}: machine busy", what=title))
            return
        path, _ = QFileDialog.getOpenFileName(self.ctx.parent, _tr("mf174", "Upgrade file"), "",
                                              f"{U.EXTENSIONS[kind][1:]} (*{U.EXTENSIONS[kind]})")
        if not path:
            return
        try:
            fw = U.check_file(path, kind)
        except U.UpgradeError as e:
            QMessageBox.critical(self.ctx.parent, _tr(key, "Upgrade"), str(e))
            self.log(str(e))
            return
        if not UpgradeConfirmDialog(self.ctx.parent, kind, fw).exec():
            return
        ops = self.ops

        def run():
            prog = lambda pc: self.log(_tr("machine.log.upgrade_progress", "{what}: {percent}%",
                                           what=title, percent=pc)) if pc % 10 == 0 else None
            if kind == "mc":
                U.upload_mc(ops.mc, fw, prog, ops.cancel.is_set)
                self.log(_tr("machine.log.upgrade_done_mc", "{what}: done, card restarting (100 := 8888)",
                             what=title))
            else:
                rtu = self._device_rtu(kind)
                try:
                    U.upload_device(rtu, fw, prog, ops.cancel.is_set)
                finally:
                    rtu.close()
                self.log(_tr("machine.log.upgrade_done_device", "{what}: done. Please restart the software.",
                             what=title))
        ops.run_async(f"upgrade-{kind}", run)

    def _device_rtu(self, kind: str):
        v = self.values
        if kind == "af":
            from mlaser.devices.autofocus import AutoFocus
            dev = AutoFocus.connect(str(v.get("AFIP", "10.1.1.168")), int(v.get("AFPort", 888)), timeout_ms=2000)
            return dev.rtu
        from mlaser.devices.ethercat import EtherCATBridge
        return EtherCATBridge.connect(str(v.get("ECIP", "10.1.1.168")), int(v.get("ECPort", 888))).rtu

    # ------------------------------------------------------------------ job / files
    def offline(self) -> None:
        from mlaser.nc.offline import export_offline
        doc = self.ctx.document
        if not doc.parts:
            self.log(_tr("machine.log.drawing_empty", "The drawing is empty"))
            return
        ext = "enc" if self.ops.laser_type == FIBER else "encex"
        path, _ = QFileDialog.getSaveFileName(self.ctx.parent, _tr("A241115_1", "Offline Process"), "",
                                              f"{ext} (*.{ext})")
        if not path:
            return
        # compiled by the JobService worker (not on the GUI thread); the offline job starts at the
        # CAM origin as compile_job without a start did
        import dataclasses
        from mlaser.services.jobs import build_request
        jobs = self.ctx.jobs
        req = build_request("process", self.ctx.docs, self.ctx.controller, self.ctx.machine, self.ctx.cam_params)
        req = dataclasses.replace(req, start=tuple(self.ctx.cam_params.origin), dry_run=False)
        ticket = jobs.plan(req)

        def done(plan, t):
            if t is not ticket:
                return
            jobs.on_planned.remove(done)
            out = export_offline(path, plan.job, doc, laser_type=self.ops.laser_type)
            self.log(_plural("machine.log.offline_written", plan.job.steps, path=out))
        jobs.on_planned.append(done)
        if ticket.done_event.is_set():
            jobs.settle(ticket)

    def _backup(self):
        from mlaser.formats.backup import ParamBackup, default_param_dir
        return ParamBackup(self.params(), Path(self.values.get("param_dir") or default_param_dir()),
                           self.ops.file_dir)

    def _password(self) -> bool:
        """Gate for writing/reading the card's hardware parameters. No vendor password: a
        user-configured one if set, otherwise an explicit confirmation (see formats/backup.py)."""
        from mlaser.formats.backup import PARAM_PASSWORD
        pw = self.values.get("param_password", PARAM_PASSWORD)
        if pw:
            txt, ok = QInputDialog.getText(self.ctx.parent, _tr("ss11", "Password"),
                                           _tr("ss11", "Password"), QLineEdit.Password)
            return bool(ok) and txt == pw
        r = QMessageBox.question(self.ctx.parent, _tr("ss11", "Password"),
                                 _tr("machine.hw.confirm",
                                     "This transfers hardware parameters between the PC and the card. Continue?"),
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return r == QMessageBox.Yes

    def backup_hw(self) -> None:
        if not self.ops.require_card("mf480") or not self._password():
            return
        try:
            self._backup().export_to_hardware(self.ops.mc)
            self.log(_tr("mf236", "mf236: parameters exported to the hardware"))
        except Exception as e:  # noqa: BLE001
            self.log(_tr("machine.log.hw_export_failed",
                         "Writing the parameters to the hardware failed: {error}", error=e))

    def _confirm_restore(self, title_key: str, text: str, detail: str = "") -> bool:
        """Before parameters on this PC are replaced (UX review 2026-11-09 finding 11): a
        question with verb buttons, Cancel the default, after a timestamped copy of the current
        parameters (named in the question).  ``detail``: the values that change how the
        machine moves (behind "Show details")."""
        import time

        from .verb_question import verb_question
        bk = Path(self.ops.file_dir) / time.strftime("params-before-restore-%Y%m%d-%H%M%S.xml")
        try:
            bk.parent.mkdir(parents=True, exist_ok=True)
            self._backup().export_to_file(bk)
        except Exception as e:  # noqa: BLE001 - no copy: say so, do not restore blind
            self.log(_tr("machine.log.params_import_failed", "Importing the parameters failed: {error}",
                         error=e))
            return False
        self.restore_backup = bk
        return verb_question(self.ctx.parent, _tr(title_key, ""), text,
                             _tr("machine.params.restore_yes", ""), _tr("wizard.import.cancel", ""),
                             informative=_tr("machine.params.restore_info", "", path=str(bk)), warning=True,
                             detail=detail)

    def _candidate(self):
        """A copy of the parameters on this PC (a restore is reviewed on it first)."""
        from mlaser.formats.params import MachineParams, ParamStore
        cur = self.params()
        return MachineParams(ParamStore(cur.hard.values), ParamStore(cur.manu.values), ParamStore(cur.layer.values))

    def _review_restore(self, title_key: str, source: str, cand, do_restore: Callable[[], str]):
        """The restore's review (the parameter import's dialog: every value now and restored,
        the motion-related ones marked and first); Restore asks once more (Cancel the default),
        backs up first and calls ``do_restore`` (it logs and returns its line, or raises)."""
        plan = param_restore_plan(self.params(), cand, self.ctx.config)
        d = ParamRestoreDialog(self, self.ctx.config, plan, title_key, source, do_restore)
        self.last_restore = d
        d.exec()
        return d

    def restore_hw(self):
        if not self.ops.require_card("mf481") or not self._password():
            return None
        from mlaser.formats import backup as B
        try:
            cand = self._candidate()
            n = B.read_hard_from_card(self.ops.mc, cand.hard)
            if B.is_default_hard(cand.hard):
                raise ValueError("OnParamRestore Hardware save param is wrong")
            B.restore_from_backups(cand, self._backup().backup_dir)
        except Exception as e:  # noqa: BLE001
            self.log(_tr("machine.log.hw_import_failed",
                         "Reading the parameters from the hardware failed: {error}", error=e))
            return None

        def restore() -> str:
            # what was reviewed is what is stored (the card is not read a second time); the
            # same end state as ParamBackup.import_from_hardware
            cur, bk = self.params(), self._backup()
            for mine, theirs in ((cur.hard, cand.hard), (cur.manu, cand.manu), (cur.layer, cand.layer)):
                mine.values = dict(theirs.values)
            B.save_with_backup(cur, bk.param_dir, bk.backup_dir, ("HardPara.xml",))
            text = _plural("machine.log.hw_read", n)
            self.log(text)
            return text
        return self._review_restore("machine.hw.restore_title", _tr("machine.params.source_card", ""),
                                    cand, restore)

    def backup_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self.ctx.parent, _tr("lp12", "Export"), "", "xml file(*.xml)")
        if path:
            out = self._backup().export_to_file(path)
            self.log(_tr("machine.log.params_exported", "Parameters exported to {path}", path=out))

    def restore_file(self):
        path, _ = QFileDialog.getOpenFileName(self.ctx.parent, _tr("lp9", "Import"), "", "xml file(*.xml)")
        if not path:
            return None
        layers = self._ask(_tr("mf235", "Import"), _tr("A250107_0", "Also restore layer parameters?"))
        from mlaser.formats import backup as B
        try:
            cand = self._candidate()
            B.import_params_file(path, cand, layers)
        except Exception as e:  # noqa: BLE001
            self.log(_tr("machine.log.params_import_failed", "Importing the parameters failed: {error}",
                         error=e))
            return None

        def restore() -> str:
            counts = self._backup().import_from_file(path, layers)
            text = _tr("machine.log.params_imported", "Parameters imported {counts}", counts=counts)
            self.log(text)
            return text
        return self._review_restore("machine.file.restore_title", Path(path).name, cand, restore)

    def task_save(self) -> None:
        from mlaser.formats.backup import save_task
        doc = self.ctx.document
        if not doc.parts:
            self.log(_tr("machine.log.drawing_empty", "The drawing is empty"))
            return
        path, _ = QFileDialog.getSaveFileName(self.ctx.parent, _tr("mf152", "Save"), "", "aut file(*.aut)")
        if not path:
            return
        ctl = self.ctx.controller
        from mlaser.cam.raster_engraving import expand_document
        out = save_task(path, expand_document(doc), self.params().layer, self.ops.laser_type,
                        ctl.autosave_dir, int(ctl.breakpoint is not None))
        self.log(_tr("machine.log.task_saved", "Task saved to {path}", path=out))

    def task_import(self) -> None:
        from mlaser.formats.backup import load_task
        path, _ = QFileDialog.getOpenFileName(self.ctx.parent, _tr("mf150", "Open"), "", "aut file(*.aut)")
        if not path:
            return
        try:
            doc, t = load_task(path, self.ops.temp_dir, self.ops.laser_type)
        except Exception as e:  # noqa: BLE001
            self.log(str(e))
            return
        self._set_doc(doc, "import task")
        self.log(_plural("machine.log.task_imported", len(doc.parts), name=Path(path).name,
                         brk=t.is_break))
        if t.is_break:
            self.ctx.controller.resume_saved(doc, str(self.ops.temp_dir))

    def autosave_store(self):
        """The autosave of the File folder (mlaser.project.Autosave: autosave.mlz + autosave.chf)."""
        from mlaser.project import Autosave
        if getattr(self, "_autosave", None) is None or self._autosave.dir != self.ops.file_dir:
            self._autosave = Autosave(self.ops.file_dir)
        return self._autosave

    def autosave(self, doc) -> None:
        """FUN_004804a0 / FUN_00457de0: File\\autosave.chf when a job is prepared; the rebuild also
        writes the whole project as File\\autosave.mlz (docs/dev/project-format.md)."""
        from mlaser.project import Project
        cur = self.ctx.provider("project.current")
        try:
            pr = cur() if cur is not None else Project()
            pr.document = doc                     # the document of the job being prepared
            self.autosave_store().save(pr)
        except Exception as e:  # noqa: BLE001
            self.log(_tr("machine.log.autosave_failed", "Autosave not written: {error}", error=e))

    def recovery(self, confirm: bool = True) -> None:
        """mf138 System Recovery (FUN_004bb100): reload File\\autosave.mlz (else
        File\\autosave.chf) and, when it belongs to that document, the resume point of the
        interrupted job (File\\resume_point.json, :mod:`mlaser.services.resume_point`).  The
        resume point comes back as a kept point: nothing moves; it is resumed through the normal
        flow (pre-flight, the arm question) and, on a real card, only after homing."""
        store = self.autosave_store()
        p = store.available()
        if p is None:
            self.log(_tr("machine.log.no_autosave", "System recovery: no {path}",
                         path=self.ops.file_dir / "autosave.chf"))
            return
        if confirm and not self._ask(_tr("mf138", "System Recovery"),
                                     _tr("mf167", "Recover the last state?")):
            return
        try:
            rec = store.recover()
        except Exception as e:  # noqa: BLE001 - the project and the .chf are unreadable
            self.log(_tr("machine.log.recovery_failed", "System recovery: {error}", error=e))
            return
        if rec is None:
            return
        for n in rec.notes:
            self.log(n)
        doc = rec.document
        from mlaser.services import resume_point as RP
        point, why = RP.assess(self.ops.file_dir, doc)
        self._set_doc(doc, "system recovery")
        proj = self.ctx.extension("ext_project")
        if proj is not None:
            proj.apply(rec.project)
        name = rec.path.name
        bp = None
        if point is not None:
            bp, why = self._restore_point(point, self.ctx.document if self.ctx.document is not None else doc)
        if bp is not None:
            self.log(_tr("machine.log.recovered_point", "System recovered from {name}, with the resume "
                         "point at X {x}, Y {y} mm (resume it from the resume bar)", name=name,
                         x=f"{bp.x:.1f}", y=f"{bp.y:.1f}"))
            return
        text = _tr("machine.log.recovered_no_point", "System recovered from {name}: the document only. "
                   "The resume point was not restored: {why}", name=name, why=self.recovery_reason(why))
        self.log(text)
        if point is not None and not self.ctx.dialogs_suppressed:
            # the prompt promised the point: say at once that it could not be rebuilt
            QMessageBox.warning(self.ctx.parent, _tr("mf138", "System Recovery"), text)

    def _restore_point(self, point: dict, doc):
        """The recorded resume point as the controller's kept point: (breakpoint, "") or
        (None, reason)."""
        from mlaser.services import resume_point as RP
        ctl = self.ctx.controller
        if ctl.breakpoint is not None or ctl.job_active():
            return None, RP.CONTROLLER
        bp, why = RP.rebuild(point, doc, ctl)
        if bp is None:
            return None, why
        ctl.kept_breakpoint = bp
        self._recovered_bp = bp
        return bp, ""

    def recovery_reason(self, why: str) -> str:
        """Why a resume point is not restored, in the UI language."""
        from mlaser.services import resume_point as RP
        texts = {
            RP.NONE: ("recovery.why.none", "no job was held when the program ended (a running job keeps no "
                      "resume point)"),
            RP.OTHER_DOCUMENT: ("recovery.why.other_document", "the saved resume point belongs to another drawing"),
            RP.UNREADABLE: ("recovery.why.unreadable", "the saved resume point is unreadable"),
            RP.REBUILD: ("recovery.why.rebuild", "the job built again does not reach the same point"),
            RP.CONTROLLER: ("recovery.why.controller", "this machine connection cannot hold a resume point now"),
        }
        key, default = texts.get(why, texts[RP.UNREADABLE])
        return _tr(key, default)

    def recovery_detail(self) -> str:
        """What Recover will restore, exactly (the start-up prompt's text)."""
        from mlaser.services import resume_point as RP
        store = self.autosave_store()
        try:
            rec = store.recover()
        except Exception:  # noqa: BLE001 - said by Recover itself
            rec = None
        if rec is None:
            return _tr("recovery.detail.unreadable", "The saved document could not be read: Recover "
                       "will try again and report what it finds.")
        doc = rec.document
        parts = _plural("recovery.n_parts", len(getattr(doc, "parts", ()) or ()))
        point, why = RP.assess(self.ops.file_dir, doc)
        if point is not None:
            return _tr("recovery.detail.with_point",
                       "Recover restores the drawing of the last session ({parts}) and the resume point of the "
                       "interrupted job at X {x}, Y {y} mm.\n\nNothing moves. The point is offered in the resume "
                       "bar; resuming runs the pre-flight check and asks to arm the laser. On a real machine, home "
                       "it first: the position may have been lost.", parts=parts,
                       x=f"{float(point['x']):.1f}", y=f"{float(point['y']):.1f}")
        return _tr("recovery.detail.no_point",
                   "Recover restores the drawing of the last session ({parts}).\n\nThe cutting position is not "
                   "restored: {why}. Parts may already be cut: check the sheet before you cut it again.",
                   parts=parts, why=self.recovery_reason(why))

    _recovered_bp = None
    _saved_bp = None

    def sync_resume_point(self) -> None:
        """Keep File/resume_point.json in step with the controller's resume point (the window
        timer: a held breakpoint, else a kept one; none: the file is removed)."""
        ctl = self.ctx.controller
        bp = ctl.breakpoint or ctl.kept_breakpoint
        if bp is self._saved_bp:
            return
        self._saved_bp = bp
        from mlaser.services import resume_point as RP
        try:
            if bp is None:
                RP.clear(self.ops.file_dir)
            elif getattr(bp, "job", None) is not None:
                RP.save(self.ops.file_dir, bp)
        except Exception as e:  # noqa: BLE001 - never in the way of the machine
            self.log(_tr("machine.log.resume_point_failed", "Resume point not saved: {error}", error=e))

    def _homing_done(self, ev) -> None:
        """A homing in this session clears the recovered point's homing requirement."""
        bp = self._recovered_bp
        if bp is not None and getattr(ev, "ok", False):
            bp.home_first = False

    def startup_homing(self, ctl) -> None:
        """After the card connected: FUN_00596320's red light off, go-origin prompt (mp119,
        mf156) and statistics read (UiController.startup_homing), on the GUI thread."""
        panel = self.ctx.control
        confirm = getattr(panel, "confirm", None)
        normal = self.normal_exit

        def run():
            ctl.startup_homing(confirm=confirm, normal_exit=normal)
        import threading
        if threading.current_thread() is not threading.main_thread():
            self.bridge.call.emit(run)       # connected from a worker (background connect, #5)
            return
        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, run)
        except Exception:  # noqa: BLE001
            run()

    normal_exit = True       # softPara.ini SC2000 NormalExit read at start-up (FUN_00596320)

    def startup_check(self) -> None:
        """FUN_00596320: NormalExit == 0 -> mp18 (offer recovery); then NormalExit := 0.

        The File folder is created here, so the marker exists from the first session on (the
        first crash of a new profile used to go unnoticed).  An autosave without any marker
        (a profile from before) also counts as an interrupted session."""
        from mlaser.formats.scfile import read_ini, write_ini
        ini = self.ops.file_dir / "softPara.ini"
        try:
            self.ops.file_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        try:
            data = read_ini(ini) if ini.exists() else {}
        except Exception:  # noqa: BLE001
            data = {}
        sec = data.setdefault("SC2000", {})
        flag = sec.get("NormalExit")
        has_autosave = self.autosave_store().available() is not None
        interrupted = flag == "0" or (flag is None and has_autosave)
        self.normal_exit = not interrupted
        if interrupted:
            # a real dialog, not a log line (docs/review/ux.md P0-7)
            self.log(_tr("machine.log.abnormal_exit", "The last run ended abnormally"))
            if not has_autosave:
                self.log(_tr("machine.log.nothing_to_recover", "Nothing was saved to recover"))
            elif not self.ctx.dialogs_suppressed:
                try:
                    from PySide6.QtCore import QTimer

                    QTimer.singleShot(0, self._recovery_prompt)
                except Exception:  # noqa: BLE001
                    pass
        sec["NormalExit"] = "0"
        try:
            write_ini(data, ini)
        except OSError:
            pass

    def _recovery_prompt(self) -> None:
        if self.ctx.dialogs_suppressed:
            return
        from .safety_ui import recovery_prompt
        recovery_prompt(self.ctx.parent, lambda: self.recovery(confirm=False), self.recovery_detail())

    def clean_exit(self) -> None:
        from mlaser.formats.scfile import read_ini, write_ini
        ini = self.ops.file_dir / "softPara.ini"
        if not ini.exists():
            return
        try:
            data = read_ini(ini)
            data.setdefault("SC2000", {})["NormalExit"] = "1"
            write_ini(data, ini)
        except Exception:  # noqa: BLE001
            pass


#: lowest user mode per command (Operator < Engineer < Service; docs/review/ux.md P1-10).  Passed
#: to ``register_command(min_mode=...)`` when the registry supports it (Shell track), else kept
#: here and as ``CommandSpec.min_mode``.
COMMAND_MIN_MODES = {
    "machine.workspace": "operator", "machine.edge_seek": "operator", "machine.edge_capacitive": "operator",
    "machine.edge_disc": "operator", "machine.edge_two_point": "operator", "machine.edge_clear": "operator",
    "machine.home_af": "operator", "wizard.edge_seek_cap3": "operator", "wizard.edge_seek_cap4": "operator",
    "wizard.edge_seek_disc": "operator", "wizard.edge_seek_two_point": "operator",
    "wizard.focus_home": "operator", "wizard.follower_calibration": "engineer",
    "wizard.laser_test": "engineer", "wizard.commissioning": "service", "machine.import_install": "service",
    "machine.laser_type": "engineer", "machine.laser_fiber": "engineer", "machine.laser_co2": "engineer",
    "coord.select": "operator", "coord.set_origin": "operator", "coord.return_origin": "operator",
    "coord.float": "operator", "coord.manual": "operator", "hw.net_test": "operator",
    "hw.restart": "service", "hw.upgrade": "service", "hw.upgrade_ext": "service", "hw.upgrade_ftc": "service",
    "job.offline": "engineer", "param.backup": "service", "param.backup_hw": "service",
    "param.backup_file": "engineer", "param.restore": "service", "param.restore_hw": "service",
    "param.restore_file": "service", "file.task_import": "operator", "file.task_save": "operator",
    "app.recovery": "operator", "machine.safety_io": "engineer", "machine.setup": "engineer",
}
COMMAND_MIN_MODES.update({f"coord.tool{i}": "operator" for i in range(1, 7)})


def _register(ctx, cmd: str, fn, label: str = "") -> None:
    mode = COMMAND_MIN_MODES.get(cmd, "operator")
    if "min_mode" in inspect.signature(ctx.register_command).parameters:
        ctx.register_command(cmd, fn, label=label, min_mode=mode)
        return
    spec = ctx.register_command(cmd, fn, label=label)
    spec.min_mode = mode                      # read by the Shell's mode filter after the merge


def register(ctx, app_dir: Optional[str] = None) -> MachineExtension:
    """Bind the machine-operation commands (``ctx``: PluginContext)."""
    if app_dir is None:
        app_dir = ctx.config.get("app_dir", None)
    ext = MachineExtension(ctx, app_dir)
    o = ext.ops
    binds = {
        "machine.edge_seek": ext.edge_capacitive, "machine.edge_capacitive": ext.edge_capacitive,
        "machine.edge_disc": lambda: ext.wizard_edge("disc"),
        "machine.edge_two_point": lambda: ext.wizard_edge("two_point"),
        "machine.edge_clear": ext.edge_clear, "machine.home_af": o.home_af,
        "machine.laser_type": ext.laser_type, "machine.laser_fiber": lambda: ext.set_laser(FIBER),
        "machine.laser_co2": lambda: ext.set_laser(CO2),
        "coord.select": ext.coord_select, "coord.set_origin": o.set_origin,
        "coord.return_origin": o.return_origin, "coord.float": lambda: ext.select_coord(C.FLOAT),
        "coord.manual": lambda: ext.select_coord(C.MANUAL),
        "hw.net_test": ext.net_test, "hw.restart": ext.restart,
        "hw.upgrade": lambda: ext._upgrade("mc"), "hw.upgrade_ext": lambda: ext._upgrade("ec"),
        "hw.upgrade_ftc": lambda: ext._upgrade("af"), "job.offline": ext.offline,
        "param.backup": ext.backup_hw, "param.backup_hw": ext.backup_hw,
        "param.backup_file": ext.backup_file, "param.restore": ext.restore_hw,
        "param.restore_hw": ext.restore_hw, "param.restore_file": ext.restore_file,
        "file.task_import": ext.task_import, "file.task_save": ext.task_save,
        "app.recovery": ext.recovery,
    }
    for i in range(1, 7):
        binds[f"coord.tool{i}"] = lambda n=i: ext.select_coord(n)
    ctx.provide("project.autosave", ext.autosave_store)
    for cmd, fn in binds.items():
        _register(ctx, cmd, fn)
    for cmd, fn, label in (
            ("machine.workspace", ext.show_workspace, "ws.machine.title"),
            ("machine.setup", ext.show_setup, "home.machine_setup"),
            ("wizard.edge_seek_cap4", lambda: ext.wizard_edge("cap4"), "wizard.edge.title_cap4"),
            ("wizard.edge_seek_cap3", lambda: ext.wizard_edge("cap3"), "wizard.edge.title_cap3"),
            ("wizard.edge_seek_disc", lambda: ext.wizard_edge("disc"), "wizard.edge.title_disc"),
            ("wizard.edge_seek_two_point", lambda: ext.wizard_edge("two_point"), "wizard.edge.title_two_point"),
            ("wizard.follower_calibration", ext.wizard_follower, "wizard.follower.title"),
            ("wizard.focus_home", ext.wizard_focus, "wizard.focus.title"),
            ("wizard.laser_test", ext.wizard_laser, "wizard.laser.title"),
            ("wizard.commissioning", ext.wizard_commissioning, "wizard.comm.title"),
            ("machine.import_install", ext.import_install, "wizard.import.title"),
            ("machine.safety_io", ext.safety_io, "safety.io.title")):
        _register(ctx, cmd, fn, label)
    from .workspaces.machine import MachineWorkspace
    ext.workspace = MachineWorkspace(ctx, ext, ext.history)
    ctx.add_workspace("machine", ext.workspace, _tr("ws.machine.title", "Machine"))
    ctx.provide("machine.monitor_host", ext.workspace.embed_monitor)
    ctx.on_user_mode_changed(lambda _mode: ext.workspace.tick())   # sections / settings follow the mode
    ctx.on_close(lambda: (ext.history.detach(), ext.workspace.close()))
    ctx.on_controller_changed(lambda _ctl: ext.attach())
    ext.attach()
    ctx.poll_timer.timeout.connect(ext.sync_resume_point)   # File/resume_point.json follows the resume point
    ext.startup_check()
    ctx.on_close(ext.clean_exit)
    return ext
