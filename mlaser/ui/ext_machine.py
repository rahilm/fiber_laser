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

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QInputDialog, QLineEdit,
                               QMessageBox, QWidget)

from mlaser.controller import coords as C
from mlaser.controller import edgeseek as E
from mlaser.controller.machine_ops import CO2, FIBER, MachineOps
from mlaser.services.config import NamespaceView

from . import style as S
from .style import Dialog

UPGRADE_WORD = "UPGRADE"


def _tr(window, key: str, default: str) -> str:
    tr = getattr(window, "tr_", None)
    if tr is None:
        return default
    try:
        s = tr.tr(key)
    except Exception:  # noqa: BLE001
        return default
    return s if s and s != key else default


class _Bridge(QObject):
    call = Signal(object)          # callable to run in the GUI thread


class UpgradeConfirmDialog(Dialog):
    """Strong confirmation: the user must type UPGRADE (an interrupted or wrong image can brick
    the board)."""

    def __init__(self, window, what: str, fw):
        super().__init__(window, _tr(window, "mf132", "Upgrade"),
                         heading_text=f"Firmware upgrade of the {what}",
                         explanation=f"File: {fw.path.name} ({len(fw.data)} bytes). A wrong file or an "
                                     "interrupted transfer can make the board unusable. Do not switch off "
                                     "the machine or disconnect the network until it has finished.")
        self.edit = QLineEdit()
        lab = S.form_row(self.form(), f"Type {UPGRADE_WORD} to continue", self.edit)
        lab.setWordWrap(True)
        self.bb = self.buttons
        ok = self.bb.button(QDialogButtonBox.Ok)
        S.set_enabled(ok, False, f"Type {UPGRADE_WORD} first")
        self.edit.textChanged.connect(lambda t: S.set_enabled(ok, t.strip() == UPGRADE_WORD,
                                                              f"Type {UPGRADE_WORD} first"))


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
                        log(f"settings: {key} = {shadow!r} (moved from machine_ops.{key})")
                except ValueError as exc:
                    if log is not None:
                        log(f"settings: machine_ops.{key} = {shadow!r} not moved ({exc})")
            self._svc.reset(self._p + key)
        return moved


#: registry keys the machine operations hold objects for (rebuilt when one changes)
COORD_KEYS = frozenset({"MSC.CoordSysType", "MSC.ToolCoordPtStr", "MSC.MarkPtStr", "MSC.DockPtType",
                        "MC.XFastMoveSpeed", "MC.XFastMoveAcc"})
EDGE_KEYS = frozenset(E.EdgeSeekParams.KEYS.values()) | {"GRP.EdgeSeekStartPointX", "GRP.EdgeSeekStartPointY"}


class MachineExtension:
    def __init__(self, ctx, app_dir: Optional[str] = None):
        self.ctx = ctx
        self.w = window = ctx.window
        self.bridge = _Bridge()
        self.bridge.call.connect(lambda fn: fn())
        self.values = RegistryValues(ctx.config)
        self.values.migrate(self.log)
        self.ops = MachineOps(window.ctl, self.values, app_dir, log=self.log)
        ctx.config.subscribe(self._config_changed)
        self.layer_tables: dict[int, dict] = {}
        self._params = None
        self.workspace = None                 # the Machine workspace (workspaces.machine)
        self.last_wizard: Optional[QDialog] = None
        from mlaser.services.alarm_history import default_history
        self.history = default_history()

    def _config_changed(self, key: str) -> None:
        """A registry value the operations hold changed (Machine › Setup, a wizard, an import):
        rebuilt at once, no restart."""
        if key in COORD_KEYS:
            self.ops.coords = C.CoordSystems.from_values(self.values)
            self.w.ctl.coords = self.ops.coords          # as select_coord does
        if key in EDGE_KEYS:
            self.ops.edge = E.EdgeSeekParams.from_values(self.values)

    # ------------------------------------------------------------------ helpers
    def log(self, msg: str) -> None:
        panel = getattr(self.w, "log_panel", None)
        if panel is not None:
            try:
                self.bridge.call.emit(lambda m=msg: panel.log(m))
                return
            except RuntimeError:
                pass

    def gui(self, fn: Callable) -> None:
        self.bridge.call.emit(fn)

    def attach(self) -> None:
        self.ops.ctl = self.w.ctl
        self.ops._set_ctl_laser_type(self.ops.laser_type)
        panel = getattr(self.w, "control", None)
        if panel is not None:
            panel.machine_ops = self.ops
        ctl = self.w.ctl                      # MachineAPI: every controller has these
        if self.startup_homing not in ctl.connect_hooks:
            ctl.connect_hooks.append(self.startup_homing)
        if self.ops.coords.kind != C.FLOAT:
            ctl.coords = self.ops.coords
        if self.autosave not in ctl.job_start_hooks:
            ctl.job_start_hooks.append(self.autosave)
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
        return QMessageBox.question(self.w, title, text, QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes

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
        self.log(f"A250211_2: Current memory scouting Angle : {res.angle_deg:.2f}")

    def apply_edge_result(self, res: E.EdgeSeekResult) -> None:
        """The edge-seek wizard's Apply (GUI thread)."""
        self._apply_edge(res)

    def edge_clear(self) -> None:
        self.ctx.edit(E.clear_document, "clear edge seek", parts=())
        self.log("A250211_3: edge-seek angle cleared")

    # ------------------------------------------------------------------ wizards / workspace
    def allowed(self, cmd: str) -> bool:
        """The current user mode may run ``cmd`` (else a message says which mode it needs)."""
        from mlaser.services.procedures import mode_allows

        from .workspaces.machine import current_mode
        need = COMMAND_MIN_MODES.get(cmd, "operator")
        if mode_allows(current_mode(self.ctx), need):
            return True
        msg = _tr(self.w, "ws.machine.mode_needed", "needs a higher user mode") + f" ({need})"
        self.log(msg)
        if not getattr(self.w, "_test_no_dialogs", False):
            QMessageBox.information(self.w, _tr(self.w, "ws.machine.title", "Machine"), msg)
        return False

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
        return self._wizard(cmd, lambda: EdgeSeekWizard(self.w, self, mode))

    def edge_capacitive(self) -> Optional[QDialog]:
        """Ribbon "Capacitor seeking": the guided wizard in the configured 3/4-point mode."""
        return self.wizard_edge("cap4" if self.ops.edge.mode == 4 else "cap3")

    def wizard_follower(self) -> Optional[QDialog]:
        from .wizards.procedures import FollowerWizard
        path = self.ctx.config.get("machine.calib_csv", None)
        return self._wizard("wizard.follower_calibration", lambda: FollowerWizard(self.w, path))

    def wizard_focus(self) -> Optional[QDialog]:
        from .wizards.procedures import FocusWizard
        return self._wizard("wizard.focus_home", lambda: FocusWizard(self.w, self))

    def wizard_laser(self) -> Optional[QDialog]:
        from .wizards.procedures import LaserWizard
        return self._wizard("wizard.laser_test", lambda: LaserWizard(self.w))

    def safety_io(self) -> Optional[QDialog]:
        """The Safety I/O dialog: interlock inputs and the heartbeat output (Engineer / Service)."""
        from .safety_io import open_dialog
        if getattr(self.w, "_test_no_dialogs", False):
            from .safety_io import SafetyIODialog
            return SafetyIODialog(self.w)
        return open_dialog(self.w)

    def wizard_commissioning(self) -> Optional[QDialog]:
        from .wizards.procedures import CommissioningWizard
        return self._wizard("wizard.commissioning", lambda: CommissioningWizard(self.w, self))

    def import_install(self) -> Optional[QDialog]:
        from .wizards.settings_import import SettingsImportDialog
        return self._wizard("machine.import_install", lambda: SettingsImportDialog(self.w, self.ctx.config))

    def show_workspace(self, section: str = "") -> Optional[QWidget]:
        ws = self.workspace
        if ws is None:
            return None
        if section:
            ws.show_section(section)
        fn = self.ctx.host.hooks.provider("workspace.show")     # the Shell's switcher, when present
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
        show = getattr(self.w, "show_workspace", None)
        if callable(show):
            show("machine")
        if self.workspace is not None:
            self.workspace.show_section("settings")

    # ------------------------------------------------------------------ laser type
    def laser_type(self) -> None:
        names = [_tr(self.w, "A241024_0", "Fiber laser"), _tr(self.w, "A241024_1", "CO2 laser")]
        item, ok = QInputDialog.getItem(self.w, _tr(self.w, "gp120", "Laser"), "", names,
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
        names = [_tr(self.w, C.NAMES[i], C.NAMES[i]) for i in range(8)]
        item, ok = QInputDialog.getItem(self.w, _tr(self.w, "mf307", "Coordinate"), "", names,
                                        self.ops.coords.kind, False)
        if ok and item in names:
            self.select_coord(names.index(item))
        else:
            self.log(f"mf307: coordinate system {names[self.ops.coords.kind]}")

    def select_coord(self, kind: int) -> None:
        self.ops.select_coord(kind)
        self.w.ctl.coords = self.ops.coords
        self.log(f"mf307: {_tr(self.w, C.NAMES[kind], C.NAMES[kind])}")

    # ------------------------------------------------------------------ hardware
    def net_test(self) -> None:
        from mlaser.controller.nettest import network_test
        ip = self.ctx.config.get("ip", None) or {}
        card = self.w.ctl.endpoint() or (ip.get("pd445", "10.1.1.168"), 502)   # MachineAPI
        follower = (ip.get("pd448", "10.1.1.169"), 502)      # pd448: FTC IP (pd447 is a gateway)
        self.log(f"mf436: testing {card[0]} and {follower[0]} ...")
        self.ops.run_async("net-test", lambda: [self.log(r.summary()) for r in network_test(card, follower)])

    def restart(self) -> None:
        if not self.ops.require_card("mf131 restart"):
            return
        if self._ask(_tr(self.w, "mf131", "Restart"), "Restart the motion card now?"):
            self.ops.restart()

    def _upgrade(self, kind: str) -> None:
        from mlaser.controller import upgrade as U
        what = {"mc": "motion card", "af": "autofocus head", "ec": "extension card"}[kind]
        key = {"mc": "mf132", "af": "mf440", "ec": "mf625"}[kind]
        if not self.ops.require_card(f"{key} upgrade"):
            return
        if self.ops.busy():
            self.log(f"{key}: machine busy")
            return
        path, _ = QFileDialog.getOpenFileName(self.w, _tr(self.w, "mf174", "Upgrade file"), "",
                                              f"{U.EXTENSIONS[kind][1:]} (*{U.EXTENSIONS[kind]})")
        if not path:
            return
        try:
            fw = U.check_file(path, kind)
        except U.UpgradeError as e:
            QMessageBox.critical(self.w, _tr(self.w, key, "Upgrade"), str(e))
            self.log(str(e))
            return
        if not UpgradeConfirmDialog(self.w, what, fw).exec():
            return
        ops = self.ops

        def run():
            prog = lambda pc: self.log(f"{key}: {pc}%") if pc % 10 == 0 else None
            if kind == "mc":
                U.upload_mc(ops.mc, fw, prog, ops.cancel.is_set)
                self.log(f"{key}: done, card restarting (100 := 8888); mf179")
            else:
                rtu = self._device_rtu(kind)
                try:
                    U.upload_device(rtu, fw, prog, ops.cancel.is_set)
                finally:
                    rtu.close()
                self.log(f"{key}: done ({'mf439' if kind == 'af' else 'mf624'})")
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
        doc = self.w.document
        if not doc.parts:
            self.log("mp76: graphic data is empty")
            return
        ext = "enc" if self.ops.laser_type == FIBER else "encex"
        path, _ = QFileDialog.getSaveFileName(self.w, _tr(self.w, "A241115_1", "Offline Process"), "",
                                              f"{ext} (*.{ext})")
        if not path:
            return
        # compiled by the JobService worker (not on the GUI thread); the offline job starts at the
        # CAM origin as compile_job without a start did
        import dataclasses
        from mlaser.services.jobs import build_request
        jobs = self.ctx.jobs
        req = build_request("process", self.ctx.docs, self.w.ctl, self.w.machine, self.w.cam_params)
        req = dataclasses.replace(req, start=tuple(self.w.cam_params.origin), dry_run=False)
        ticket = jobs.plan(req)

        def done(plan, t):
            if t is not ticket:
                return
            jobs.on_planned.remove(done)
            out = export_offline(path, plan.job, doc, laser_type=self.ops.laser_type)
            self.log(f"A241115_1: offline job written to {out} ({plan.job.steps} steps)")
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
            txt, ok = QInputDialog.getText(self.w, _tr(self.w, "ss11", "Password"),
                                           _tr(self.w, "ss11", "Password"), QLineEdit.Password)
            return bool(ok) and txt == pw
        r = QMessageBox.question(self.w, _tr(self.w, "ss11", "Password"),
                                 "This transfers hardware parameters between the PC and the card. Continue?",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return r == QMessageBox.Yes

    def backup_hw(self) -> None:
        if not self.ops.require_card("mf480") or not self._password():
            return
        try:
            self._backup().export_to_hardware(self.ops.mc)
            self.log(_tr(self.w, "mf236", "mf236: parameters exported to the hardware"))
        except Exception as e:  # noqa: BLE001
            self.log(f"mf237: {e}")

    def restore_hw(self) -> None:
        if not self.ops.require_card("mf481") or not self._password():
            return
        if not self._ask(_tr(self.w, "mf481", "Import Parameters From Hardware"), _tr(self.w, "mf238", "Continue?")):
            return
        try:
            n = self._backup().import_from_hardware(self.ops.mc)
            self.log(f"mf239: {n} hardware parameters read; restart the software")
        except Exception as e:  # noqa: BLE001
            self.log(f"mf240: {e}")

    def backup_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self.w, _tr(self.w, "lp12", "Export"), "", "xml file(*.xml)")
        if path:
            out = self._backup().export_to_file(path)
            self.log(f"lp13: parameters exported to {out}")

    def restore_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self.w, _tr(self.w, "lp9", "Import"), "", "xml file(*.xml)")
        if not path:
            return
        layers = self._ask(_tr(self.w, "mf235", "Import"), _tr(self.w, "A250107_0", "Also restore layer parameters?"))
        try:
            counts = self._backup().import_from_file(path, layers)
            self.log(f"lp14: parameters imported {counts}")
        except Exception as e:  # noqa: BLE001
            self.log(f"A250105_1: {e}")

    def task_save(self) -> None:
        from mlaser.formats.backup import save_task
        doc = self.w.document
        if not doc.parts:
            self.log("mp76: graphic data is empty")
            return
        path, _ = QFileDialog.getSaveFileName(self.w, _tr(self.w, "mf152", "Save"), "", "aut file(*.aut)")
        if not path:
            return
        ctl = self.w.ctl
        from mlaser.cam.raster_engraving import expand_document
        out = save_task(path, expand_document(doc), self.params().layer, self.ops.laser_type,
                        ctl.autosave_dir, int(ctl.breakpoint is not None))
        self.log(f"A250320_2: task saved to {out}")

    def task_import(self) -> None:
        from mlaser.formats.backup import load_task
        path, _ = QFileDialog.getOpenFileName(self.w, _tr(self.w, "mf150", "Open"), "", "aut file(*.aut)")
        if not path:
            return
        try:
            doc, t = load_task(path, self.ops.temp_dir, self.ops.laser_type)
        except Exception as e:  # noqa: BLE001
            self.log(str(e))
            return
        self._set_doc(doc, "import task")
        self.log(f"A250320_1: task {Path(path).name} imported ({len(doc.parts)} parts, break {t.is_break})")
        if t.is_break:
            self.w.ctl.resume_saved(doc, str(self.ops.temp_dir))

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
        cur = self.w.hooks.provider("project.current") if hasattr(self.w, "hooks") else None
        try:
            pr = cur() if cur is not None else Project()
            pr.document = doc                     # the document of the job being prepared
            self.autosave_store().save(pr)
        except Exception as e:  # noqa: BLE001
            self.log(f"autosave not written: {e}")

    def recovery(self, confirm: bool = True) -> None:
        """mf138 System Recovery (FUN_004bb100): mf167, reload File\\autosave.mlz (else
        File\\autosave.chf) and the AutosaveParam breakpoint (FUN_00480860), mf169."""
        store = self.autosave_store()
        p = store.available()
        if p is None:
            self.log(f"mf138: no {self.ops.file_dir / 'autosave.chf'}")
            return
        if confirm and not self._ask(_tr(self.w, "mf138", "System Recovery"),
                                     _tr(self.w, "mf167", "Recover the last state?")):
            return
        try:
            rec = store.recover()
        except Exception as e:  # noqa: BLE001 - the project and the .chf are unreadable
            self.log(f"mf138: {e}")
            return
        if rec is None:
            return
        for n in rec.notes:
            self.log(n)
        doc = rec.document
        self._set_doc(doc, "system recovery")
        proj = self.w.extension("ext_project") if hasattr(self.w, "extension") else None
        if proj is not None:
            proj.apply(rec.project)
        resumed = self.w.ctl.resume_saved(doc, str(self.ops.file_dir))
        self.log(f"mf169: system recovered from {rec.path.name}" + (" (resuming breakpoint)" if resumed else ""))

    def startup_homing(self, ctl) -> None:
        """After the card connected: FUN_00596320's red light off, go-origin prompt (mp119,
        mf156) and statistics read (UiController.startup_homing), on the GUI thread."""
        panel = getattr(self.w, "control", None)
        confirm = getattr(panel, "confirm", None)
        normal = self.normal_exit

        def run():
            ctl.startup_homing(confirm=confirm, normal_exit=normal)
        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, run)
        except Exception:  # noqa: BLE001
            run()

    normal_exit = True       # softPara.ini SC2000 NormalExit read at start-up (FUN_00596320)

    def startup_check(self) -> None:
        """FUN_00596320: NormalExit == 0 -> mp18 (offer recovery); then NormalExit := 0."""
        from mlaser.formats.scfile import read_ini, write_ini
        ini = self.ops.file_dir / "softPara.ini"
        if not self.ops.file_dir.exists():
            return
        try:
            data = read_ini(ini) if ini.exists() else {}
        except Exception:  # noqa: BLE001
            data = {}
        sec = data.setdefault("SC2000", {})
        self.normal_exit = sec.get("NormalExit", "1") != "0"
        if sec.get("NormalExit", "1") == "0":
            # a real dialog, not a log line (docs/review/ux.md P0-7)
            self.log("mp18: the last run ended abnormally")
            try:
                from PySide6.QtCore import QTimer

                from .safety_ui import recovery_prompt
                if not getattr(self.w, "_test_no_dialogs", False):
                    QTimer.singleShot(0, lambda: recovery_prompt(self.w, lambda: self.recovery(confirm=False)))
            except Exception:  # noqa: BLE001
                pass
        sec["NormalExit"] = "0"
        try:
            write_ini(data, ini)
        except OSError:
            pass

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
    ctx.add_workspace("machine", ext.workspace, _tr(ctx.window, "ws.machine.title", "Machine"))
    ctx.provide("machine.monitor_host", ext.workspace.embed_monitor)
    ctx.on_user_mode_changed(lambda _mode: ext.workspace.tick())   # sections / settings follow the mode
    ctx.on_close(lambda: (ext.history.detach(), ext.workspace.close()))
    ctx.on_controller_changed(lambda _ctl: ext.attach())
    ext.attach()
    ext.startup_check()
    ctx.on_close(ext.clean_exit)
    return ext
