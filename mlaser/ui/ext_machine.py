"""Machine-operation commands (agent H, phase 3): edge seek, laser type, coordinate systems,
AF home, network test, restart, firmware upgrade, offline job, parameter backup / restore,
task files and system recovery.

``register(ctx)`` (PluginContext) binds the commands and re-attaches on every controller change
(``ctx.on_controller_changed``).  The logic lives in :mod:`mlaser.controller.machine_ops` and the
modules it uses; this file is the Qt part.  MachineOps' values are the ConfigService namespace
``machine_ops`` (they fall back to the Options the user set, so hardware options reach it).
Long card operations run in worker threads; results come back through a queued Qt signal.
Evidence: docs/protocol.md §16-§21.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QVBoxLayout)

from mlaser.controller import coords as C
from mlaser.controller import edgeseek as E
from mlaser.controller.machine_ops import CO2, FIBER, MachineOps

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


class EdgeSeekDialog(QDialog):
    """EdgeSeekAuto (FUN_00559210 init): mode 3/4 radio, start point (current / fixed), board
    size, X/Y offsets; shows the last cross point and angle."""

    def __init__(self, window, p: E.EdgeSeekParams, last: Optional[E.EdgeSeekResult]):
        super().__init__(window)
        self.p = p
        self.setWindowTitle(_tr(window, "A250211_0", "Capacitor seeking"))
        lay = QVBoxLayout(self)
        f = QFormLayout()
        lay.addLayout(f)
        self.mode = QComboBox()
        self.mode.addItems(["3 points", "4 points"])
        self.mode.setCurrentIndex(1 if p.mode == 4 else 0)
        f.addRow("Mode", self.mode)
        self.cur = QCheckBox()
        self.cur.setChecked(p.current_start)
        f.addRow("Start at current position", self.cur)
        self.ed: dict[str, QDoubleSpinBox] = {}
        for attr, label in (("start_x", "Start X"), ("start_y", "Start Y"), ("board_x", "Board size X"),
                            ("board_y", "Board size Y"), ("x_percent", "X spacing ratio"),
                            ("y_percent", "Y spacing ratio"), ("offset_x", "X edge offset"),
                            ("offset_y", "Y edge offset"), ("out_speed", "Edging speed"),
                            ("follow_height", "Follow height"), ("up_height", "Move-out up height")):
            w = QDoubleSpinBox()
            w.setRange(-100000, 100000)
            w.setDecimals(3)
            v = p.start[0] if attr == "start_x" else p.start[1] if attr == "start_y" else getattr(p, attr)
            w.setValue(float(v))
            f.addRow(label, w)
            self.ed[attr] = w
        if last is not None and last.corner:
            lay.addWidget(QLabel(f"cross point ({last.corner[0]:.3f}, {last.corner[1]:.3f}), "
                                 f"angle {last.angle_deg:.3f}°"))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _ok(self) -> None:
        p = self.p
        p.mode = 4 if self.mode.currentIndex() == 1 else 3
        p.current_start = self.cur.isChecked()
        for attr, w in self.ed.items():
            if attr == "start_x":
                p.start = (w.value(), p.start[1])
            elif attr == "start_y":
                p.start = (p.start[0], w.value())
            else:
                setattr(p, attr, w.value())
        self.accept()


class TwoPointDialog(QDialog):
    """CEdgeSeekDlg (FUN_005e17e0): record P1 / P2 with the head (red light) -> angle."""

    def __init__(self, window, ops: MachineOps):
        super().__init__(window)
        self.ops = ops
        self.p1 = self.p2 = None
        self.angle = 0.0
        self.setWindowTitle(_tr(window, "A250211_1", "Manual two-point edging"))
        lay = QVBoxLayout(self)
        self.l1, self.l2, self.la = QLabel("P1: -"), QLabel("P2: -"), QLabel("angle: 0.000°")
        b1, b2 = QPushButton("P1"), QPushButton("P2")
        b1.clicked.connect(lambda: self.mark(1))
        b2.clicked.connect(lambda: self.mark(2))
        for w in (b1, self.l1, b2, self.l2, self.la):
            lay.addWidget(w)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def mark(self, n: int) -> None:
        pos = self.ops.head()
        if n == 1:
            self.p1 = pos
            self.l1.setText(f"P1: {pos[0]:.2f}, {pos[1]:.2f}")
        else:
            self.p2 = pos
            self.l2.setText(f"P2: {pos[0]:.2f}, {pos[1]:.2f}")
        if self.p1 and self.p2:
            self.angle = E.two_point_angle(self.p1, self.p2)
            self.la.setText(f"angle: {math.degrees(self.angle):.3f}°")


class UpgradeConfirmDialog(QDialog):
    """Strong confirmation: the user must type UPGRADE (an interrupted or wrong image can brick
    the board)."""

    def __init__(self, window, what: str, fw):
        super().__init__(window)
        self.setWindowTitle(_tr(window, "mf132", "Upgrade"))
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            f"<b>Firmware upgrade of the {what}</b><br>File: {fw.path.name} ({len(fw.data)} bytes)<br><br>"
            "A wrong file or an interrupted transfer can make the board unusable.<br>"
            "Do not switch off the machine or disconnect the network until it has finished.<br><br>"
            f"Type <b>{UPGRADE_WORD}</b> to continue:"))
        self.edit = QLineEdit()
        lay.addWidget(self.edit)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = self.bb.button(QDialogButtonBox.Ok)
        ok.setEnabled(False)
        self.edit.textChanged.connect(lambda t: ok.setEnabled(t.strip() == UPGRADE_WORD))
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)


class MachineExtension:
    def __init__(self, ctx, app_dir: Optional[str] = None):
        self.ctx = ctx
        self.w = window = ctx.window
        self.values = ctx.config.namespace("machine_ops")
        self.ops = MachineOps(window.ctl, self.values, app_dir, log=self.log)
        self.bridge = _Bridge()
        self.bridge.call.connect(lambda fn: fn())
        self.layer_tables: dict[int, dict] = {}
        self._params = None

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
        hooks = getattr(self.w.ctl, "connect_hooks", None)
        if isinstance(hooks, list) and self.startup_homing not in hooks:
            hooks.append(self.startup_homing)
        if hasattr(self.w.ctl, "coords") and self.ops.coords.kind != C.FLOAT:
            self.w.ctl.coords = self.ops.coords
        hooks = getattr(self.w.ctl, "job_start_hooks", None)
        if isinstance(hooks, list) and self.autosave not in hooks:
            hooks.append(self.autosave)

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
        centre = res.origin or res.corner or (0.0, 0.0)
        # an undoable edit of the document's rotation state (it used to rotate without undo)
        self.ctx.edit(lambda d: E.apply_to_document(d, res.angle, centre), "edge seek", parts=())
        self.ops.memorised_angle = res.angle
        if set_origin and self.ops.coords.can_set_or_return:
            self.ops.set_origin()
        self.log(f"A250211_2: Current memory scouting Angle : {res.angle_deg:.2f}")

    def edge_capacitive(self) -> None:
        d = EdgeSeekDialog(self.w, self.ops.edge, self.ops.last_edge)
        if not d.exec():
            return
        self.ops.edge_seek_capacitive(apply=lambda r: self.gui(lambda: self._apply_edge(r)))

    def edge_disc(self) -> None:
        auto = self._ask(_tr(self.w, "newLang27", "Disc Search"),
                         "Search the disc centre from the current head position?\n"
                         "(Yes also sets the work origin at the centre)")
        if not auto:
            return
        self.ops.edge_seek_disc(apply=lambda r: self.gui(lambda: self._apply_edge(r, True)))

    def edge_two_point(self) -> None:
        d = TwoPointDialog(self.w, self.ops)
        if d.exec() and d.p1 and d.p2:
            res = E.EdgeSeekResult(mode=0, points=[d.p1, d.p2], angle=d.angle, origin=d.p1)
            self.ops.last_edge = res
            self._apply_edge(res)

    def edge_clear(self) -> None:
        self.ctx.edit(E.clear_document, "clear edge seek", parts=())
        self.log("A250211_3: edge-seek angle cleared")

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
        if hasattr(self.w.ctl, "coords"):
            self.w.ctl.coords = self.ops.coords
        self.log(f"mf307: {_tr(self.w, C.NAMES[kind], C.NAMES[kind])}")

    # ------------------------------------------------------------------ hardware
    def net_test(self) -> None:
        from mlaser.controller.nettest import network_test
        cfg = getattr(self.w.ctl, "cfg", None)
        ip = (getattr(self.w, "settings", {}) or {}).get("ip") or {}
        card = (getattr(cfg, "host", None) or ip.get("pd445", "10.1.1.168"), getattr(cfg, "port", 502))
        follower = (ip.get("pd447", "10.1.1.169"), 502)
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
        from mlaser.nc import compile_job
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
        place = getattr(self.w.ctl, "place", None)
        d = place(doc) if callable(place) else doc
        job = compile_job(d, getattr(self.w, "cam_params", None), getattr(self.w, "machine", None))
        out = export_offline(path, job, doc, laser_type=self.ops.laser_type)
        self.log(f"A241115_1: offline job written to {out} ({job.steps} steps)")

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
        out = save_task(path, doc, self.params().layer, self.ops.laser_type,
                        getattr(ctl, "autosave_dir", None), int(getattr(ctl, "breakpoint", None) is not None))
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
        if t.is_break and hasattr(self.w.ctl, "resume_saved"):
            self.w.ctl.resume_saved(doc, str(self.ops.temp_dir))

    def autosave(self, doc) -> None:
        """FUN_004804a0 / FUN_00457de0: File\\autosave.chf when a job is prepared."""
        from mlaser.formats.chf import save_chf
        try:
            self.ops.file_dir.mkdir(parents=True, exist_ok=True)
            save_chf(doc, str(self.ops.file_dir / "autosave.chf"))
        except Exception as e:  # noqa: BLE001
            self.log(f"autosave.chf not written: {e}")

    def recovery(self) -> None:
        """mf138 System Recovery (FUN_004bb100): mf167, reload File\\autosave.chf and the
        AutosaveParam breakpoint (FUN_00480860), mf169."""
        p = self.ops.file_dir / "autosave.chf"
        if not p.exists():
            self.log(f"mf138: no {p}")
            return
        if not self._ask(_tr(self.w, "mf138", "System Recovery"), _tr(self.w, "mf167", "Recover the last state?")):
            return
        from mlaser.formats.chf import load_chf
        doc = load_chf(str(p))
        self._set_doc(doc, "system recovery")
        resumed = False
        if hasattr(self.w.ctl, "resume_saved"):
            resumed = self.w.ctl.resume_saved(doc, str(self.ops.file_dir))
        self.log(f"mf169: system recovered from {p.name}" + (" (resuming breakpoint)" if resumed else ""))

    def startup_homing(self, ctl) -> None:
        """After the card connected: FUN_00596320's red light off, go-origin prompt (mp119,
        mf156) and statistics read (UiController.startup_homing), on the GUI thread."""
        panel = getattr(self.w, "control", None)
        confirm = getattr(panel, "confirm", None)
        normal = self.normal_exit

        def run():
            if hasattr(ctl, "startup_homing"):
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
            self.log("mp18: the last run ended abnormally; use mf138 System Recovery to restore it")
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


def register(ctx, app_dir: Optional[str] = None) -> MachineExtension:
    """Bind the machine-operation commands (``ctx``: PluginContext)."""
    if app_dir is None:
        app_dir = ctx.config.get("app_dir", None)
    ext = MachineExtension(ctx, app_dir)
    o = ext.ops
    binds = {
        "machine.edge_seek": ext.edge_capacitive, "machine.edge_capacitive": ext.edge_capacitive,
        "machine.edge_disc": ext.edge_disc, "machine.edge_two_point": ext.edge_two_point,
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
    ctx.register_commands(binds)
    ctx.on_controller_changed(lambda _ctl: ext.attach())
    ext.attach()
    ext.startup_check()
    ctx.on_close(ext.clean_exit)
    return ext
