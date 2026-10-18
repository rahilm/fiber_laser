"""Main window: ribbon + canvas + docks + status bar (layout per docs/ui.md)."""
from __future__ import annotations

import copy
import importlib
import os
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QComboBox, QDockWidget, QFileDialog, QLabel, QMainWindow,
                               QMessageBox, QScrollArea, QTabWidget)

from mlaser.model import Document, LayerParams, LeadParams

from . import dialogs, docops
from .canvas import CanvasView, layer_color
from ..machine.api import ControllerFacade
from .facade import DemoController
from .i18n import Translator, translator
from .panels import ControlPanel, LayerPanel, LogPanel, PartListPanel
from .params import OPTION_SECTIONS
from .ribbon import Ribbon


def _new_cam_params():
    cls = _lazy("mlaser.cam.toolpath", "CamParams")
    return cls() if cls else None


def _new_machine():
    cls = _lazy("mlaser.nc", "Machine")
    return cls() if cls else None


# ribbon sort command -> CamParams.sort_mode (CADModule 0x1003f010 modes 1..5, docs/cam.md §8)
SORT_MODES = {"left_right": 1, "right_left": 2, "bottom_top": 3, "top_bottom": 4, "nearest": 5,
              "in_out": 6, "out_in": 7}      # 6/7: CRingSort (docs/cam.md §8)
# Feature extensions; each module exposes register(window) (docs/BRIEF.md, phase 2).
EXTENSIONS = ("ext_cam", "ext_nest", "ext_devices", "ext_card",
              "ext_draw", "ext_craft", "ext_machine", "ext_diag")


def _lazy(module: str, attr: str) -> Optional[Callable]:
    """Import ``module.attr`` if the sibling package exists yet (written by other agents)."""
    try:
        return getattr(importlib.import_module(module), attr)
    except Exception:
        return None


def _clear_joints(doc) -> None:
    for p in docops._targets(doc):
        p.craft.micro_joints = []



def default_lead_params() -> tuple[LeadParams, LeadParams]:
    """Lead dialog defaults of a fresh install (O, parity harness ``app_lead``: the original's
    引线 dialog with no settings saved): lead-in **and** lead-out = line, 90°, 3 mm, r 2."""
    return (LeadParams(kind=1, angle_deg=90.0, length=3.0, radius=2.0),
            LeadParams(kind=1, angle_deg=90.0, length=3.0, radius=2.0))

class MainWindow(QMainWindow):
    def __init__(self, controller: Optional[ControllerFacade] = None, tr: Optional[Translator] = None,
                 extensions: tuple = ()):
        super().__init__()
        self.tr_ = tr or translator()
        self.ctl = controller or DemoController()
        self.document = Document()
        self.path: Optional[str] = None
        self._undo: list[Document] = []
        self._redo: list[Document] = []
        self.settings: dict = {}            # SoftParam/GraphParam/... values edited by Options
        # lead dialog state: defaults of a fresh install (D-17) + options (D-18/D-19)
        self.lead_in, self.lead_out = default_lead_params()
        self.lead_closed_only = True     # "只对封闭图形有效" (closed graphs only), on by default
        self.lead_start_mode = 0         # "自动选择起点位置(长边优先)" (auto, long side first)
        self.cam_params = _new_cam_params()      # mlaser.cam.toolpath.CamParams (sort mode etc.)
        self.machine = _new_machine()            # mlaser.nc.Machine (simulation dynamics)
        self.toolpath: list = []
        self.estimate: dict = {}
        self.mj_params = {"kind": 0, "count": 4, "distance": 100.0, "length": 0.5, "min_size": 0.0}
        self.resize(1400, 900)
        self.tr_.bind(self, "mf459", lambda t: self.setWindowTitle(f"Mlaser - {t}"))

        self.ribbon = Ribbon(self.tr_, self)
        self.setMenuWidget(self.ribbon)
        self.lang_combo = QComboBox()
        self._fill_lang_combo()
        self.lang_combo.currentIndexChanged.connect(
            lambda i: self.set_language(self.lang_combo.itemData(i)) if self.lang_combo.itemData(i) else None)
        self.ribbon.setCornerWidget(self.lang_combo, Qt.TopRightCorner)

        self.canvas = CanvasView(self)
        self.setCentralWidget(self.canvas)

        # left: layers + parts (tabbed dock)
        self.layer_panel = LayerPanel(self.tr_)
        self.part_list = PartListPanel(self.tr_)
        left = QTabWidget()
        left.addTab(self.layer_panel, "")
        left.addTab(self.part_list, "")
        self.tr_.bind(left, "gp83", lambda t: left.setTabText(0, t))
        self.tr_.bind(left, "np_Part", lambda t: left.setTabText(1, t))
        self.left_dock = self._dock("lp0", left, Qt.LeftDockWidgetArea)

        # right: control panel
        self.control = ControlPanel(self.tr_, self.ctl, lambda: self.document)
        sa = QScrollArea()
        sa.setWidget(self.control)
        sa.setWidgetResizable(True)
        self.right_dock = self._dock("mf141", sa, Qt.RightDockWidgetArea)

        # bottom: log / alarms
        self.log_panel = LogPanel(self.tr_)
        self.log_dock = self._dock("mf124", self.log_panel, Qt.BottomDockWidgetArea)
        self.ctl.add_log_listener(self.log_panel.log)

        self.resizeDocks([self.log_dock], [150], Qt.Vertical)
        self.resizeDocks([self.left_dock, self.right_dock], [320, 300], Qt.Horizontal)
        self._status_bar()
        self._wire()
        self._shortcuts()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(100)
        self.set_document(Document())
        self.extensions = self._load_extensions(extensions)

    def _load_extensions(self, names) -> dict:
        """Import each feature extension and let it add its actions; a failing one is logged
        and skipped so the core window still works."""
        import importlib
        loaded = {}
        for name in names:
            try:
                mod = importlib.import_module(f"{__package__}.{name}")
            except ImportError:
                continue
            try:
                loaded[name] = mod.register(self)
            except Exception as e:  # noqa: BLE001 - an extension must not take the app down
                self.log_panel.log(f"extension {name} failed: {e}")
        return loaded

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        QTimer.singleShot(0, self.canvas.zoom_fit)   # fit once the view has its real size

    def closeEvent(self, ev) -> None:
        try:
            self.ctl.disconnect()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(ev)

    # ------------------------------------------------------------------ setup
    def _dock(self, key: str, w, area) -> QDockWidget:
        d = QDockWidget(self)
        d.setObjectName(key)
        d.setWidget(w)
        d.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.tr_.bind(d, key, d.setWindowTitle)
        self.addDockWidget(area, d)
        return d

    def _status_bar(self) -> None:
        """Panes observed in MainApp (CBCGPRibbonStatusBar AddElement ids MC, ZF, LF, XY, CV, PD, DA)."""
        sb = self.statusBar()
        self.st_mc, self.st_zf, self.st_lf = QLabel(), QLabel(), QLabel()
        self.st_xy, self.st_cv, self.st_pd, self.st_da = QLabel(), QLabel(), QLabel(), QLabel()
        for w in (self.st_mc, self.st_zf, self.st_lf, self.st_xy, self.st_cv, self.st_pd):
            sb.addWidget(w)
        sb.addPermanentWidget(self.st_da)
        self.st_da.setText("X: 0.000 Y: 0.000")

    def _wire(self) -> None:
        a = self.ribbon.actions
        handlers: dict[str, Callable[[], None]] = {
            "file.new": self.file_new, "file.open": self.file_open, "file.import": self.file_import,
            "file.save": self.file_save, "file.save_as": lambda: self.file_save(True),
            "app.exit": self.close, "app.import_language": self.import_language, "app.about": lambda: dialogs.AboutDialog(self.tr_, self).exec(),
            "app.options": lambda: self.options_dialog("app.options"),
            "hw.options": lambda: self.options_dialog("hw.options"),
            "hw.set_ip": self.ip_dialog, "hw.reconnect": self.reconnect,
            "machine.home": lambda: self.ctl.home("XY"), "machine.home_x": lambda: self.ctl.home("X"),
            "machine.home_y": lambda: self.ctl.home("Y"), "machine.home_xyz": lambda: self.ctl.home("XYZ"),
            "machine.home_zf_then_xy": lambda: (self.ctl.home("Z"), self.ctl.home("XY")),
            "view.select_mode": lambda: self.set_mode(CanvasView.MODE_SELECT),
            "view.pan_mode": lambda: self.set_mode(CanvasView.MODE_PAN),
            "view.node_edit": lambda: self.set_mode(CanvasView.MODE_NODE),
            "view.zoom_in": lambda: self.canvas.zoom(1.25), "view.zoom_out": lambda: self.canvas.zoom(0.8),
            "view.zoom_fit": self.canvas.zoom_fit,
            "edit.undo": self.undo, "edit.redo": self.redo,
            "select.all": lambda: self.canvas.select_parts(range(len(self.document.parts))),
            "select.invert": lambda: self._select(lambda p: not p.selected),
            "select.unclosed": lambda: self._select(lambda p: any(not c.closed for c in p.contours)),
            "xform.mirror_h": lambda: self._edit(docops.mirror, True),
            "xform.mirror_v": lambda: self._edit(docops.mirror, False),
            "xform.rot_cw90": lambda: self._edit(docops.rotate, -90), "xform.rot_ccw90": lambda: self._edit(docops.rotate, 90),
            "xform.rot_180": lambda: self._edit(docops.rotate, 180), "xform.rotate": lambda: self._edit(docops.rotate, 90),
            "xform.rot_any": self.rotate_dialog,
            "xform.scale_10": lambda: self._edit(docops.scale, 0.1), "xform.scale_50": lambda: self._edit(docops.scale, 0.5),
            "xform.scale_200": lambda: self._edit(docops.scale, 2.0), "xform.scale_dialog": self.scale_dialog,
            "xform.scale_30": lambda: self._edit(docops.scale, 0.3), "xform.scale_75": lambda: self._edit(docops.scale, 0.75),
            "xform.scale_500": lambda: self._edit(docops.scale, 5.0),
            **{f"xform.size_{mm}mm": (lambda mm=mm: self._edit(docops.scale_to, float(mm))) for mm in (10, 50, 100, 200)},
            "craft.lead": self.lead_dialog, "craft.lead_clear": lambda: self._edit(docops.apply_lead, LeadParams(0), LeadParams(0), False),
            "craft.kerf": self.kerf_dialog, "craft.kerf_set": self.kerf_dialog,
            "craft.kerf_cancel": lambda: self._edit(docops.apply_kerf, -1, 0.0),
            "craft.inside": lambda: self._edit(docops.apply_kerf, 0, self._kerf_width()),
            "craft.outside": lambda: self._edit(docops.apply_kerf, 1, self._kerf_width()),
            "craft.reverse": lambda: self._edit(docops.reverse),
            "craft.microjoint": self.microjoint_dialog, "craft.mj_params": self.microjoint_dialog,
            "tool.array": self.array_dialog,
            "sim.speed": self.sim_speed_dialog, "sim.stop": lambda: self.canvas.stop_simulation(True),
            "diag.log": lambda: self.log_dock.show(),
            "edit.copy": self.copy_selected, "edit.paste": self.paste,
        }
        for how in ("left", "right", "hcenter", "top", "bottom", "vcenter", "center"):
            handlers[f"xform.align_{how}"] = lambda _=False, h=how: self._edit(docops.align, h)
        for how in ("left_right", "right_left", "bottom_top", "top_bottom", "nearest", "small_first", "in_out", "out_in"):
            handlers[f"sort.{how}"] = lambda _=False, h=how: self.sort(h)
        handlers["sort.run"] = lambda: self.sort("nearest")
        handlers["craft.mj_auto"] = self.microjoint_auto
        handlers["craft.mj_clear"] = lambda: self._edit(_clear_joints)
        handlers["hw.connect_sim"] = self.connect_simulator
        show = {"show.bbox": "show_bbox", "show.unclosed_red": "unclosed_red", "show.index": "show_index",
                "show.start": "show_start", "show.direction": "show_direction", "show.move_path": "show_move_path"}
        for cmd, attr in show.items():
            act = a[cmd]
            act.setChecked(getattr(self.canvas.options, attr))
            act.toggled.connect(lambda on, at=attr: (setattr(self.canvas.options, at, on), self.canvas.rebuild()))
        for cmd, act in a.items():
            if cmd in show:
                continue
            fn = handlers.get(cmd)
            act.triggered.connect(fn if fn else (lambda _=False, c=cmd: self.not_implemented(c)))
        self.handlers = handlers

        self.canvas.setContextMenuPolicy(Qt.CustomContextMenu)
        self.canvas.customContextMenuRequested.connect(self._context_menu)
        self.canvas.mouseMoved.connect(lambda x, y: self.st_da.setText(f"X: {x:.3f} Y: {y:.3f}"))
        self.canvas.selectionChanged.connect(self.part_list.sync_selection)
        self.part_list.selectionToCanvas.connect(lambda idx: self.canvas.select_parts(idx))
        self.layer_panel.assignLayer.connect(self.assign_layer)
        self.layer_panel.editLayer.connect(self.edit_layer)
        self.control.simulateRequested.connect(self.simulate)

    def _shortcuts(self) -> None:
        """Accelerators observed in RT_ACCELERATOR 128 (Ctrl+N/O/S/Z/X/C/V, Alt+Backspace,
        Shift+Del, Ctrl+Ins, Shift+Ins, F6) and the view key handler FUN_004d0480 (Esc cancel +
        deselect, arrows nudge selection, Del delete, Shift multi-select, Ctrl+A/C/V/Y/Z).
        ``F`` (zoom fit) and Ctrl+I are rebuild additions."""
        keys = {
            "Ctrl+N": "file.new", "Ctrl+O": "file.open", "Ctrl+S": "file.save",
            "Ctrl+Z": "edit.undo", "Alt+Backspace": "edit.undo", "Ctrl+Y": "edit.redo",
            "Ctrl+A": "select.all", "F": "view.zoom_fit", "Ctrl+I": "file.import",
            "Ctrl+C": "edit.copy", "Ctrl+V": "edit.paste",
        }
        for seq, cmd in keys.items():
            QShortcut(QKeySequence(seq), self, activated=lambda c=cmd: self.run(c))
        QShortcut(QKeySequence(Qt.Key_Delete), self, activated=self.delete_selected)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=lambda: (self.canvas.select_parts([]), self.set_mode(CanvasView.MODE_SELECT)))
        for key, (dx, dy) in ((Qt.Key_Left, (-1, 0)), (Qt.Key_Right, (1, 0)), (Qt.Key_Up, (0, 1)), (Qt.Key_Down, (0, -1))):
            QShortcut(QKeySequence(key), self.canvas, activated=lambda dx=dx, dy=dy: self.nudge(dx, dy))

    # ------------------------------------------------------------------ helpers
    def run(self, cmd: str) -> None:
        act = self.ribbon.actions.get(cmd)
        if act is not None:
            act.trigger()
        elif cmd in self.handlers:
            self.handlers[cmd]()

    def not_implemented(self, cmd: str) -> None:
        self.log_panel.log(f"{cmd}: not implemented in the rebuild yet")

    def _fill_lang_combo(self) -> None:
        self.lang_combo.blockSignals(True)
        self.lang_combo.clear()
        for li in self.tr_.available():
            self.lang_combo.addItem(li.name, li.code)
        self.lang_combo.setCurrentIndex(max(0, self.lang_combo.findData(self.tr_.language)))
        self.lang_combo.blockSignals(False)

    def import_language(self, path: Optional[str] = None) -> list[str]:
        """File > Import Language Files: load the translations of the user's own Mlaser install
        (folder or one Lang/*.txt file) for the languages mlaser does not ship."""
        if path is None:
            path = QFileDialog.getExistingDirectory(self, self.tr_.tr("main.import_language_title"))
        if not path:
            return []
        try:
            codes = self.tr_.import_from(path)
        except (OSError, ValueError) as exc:
            self.log_panel.log(f"{self.tr_.tr('main.import_language_none')} ({exc})")
            return []
        names = ", ".join(self.tr_.info(c).name for c in codes if c in {li.code for li in self.tr_.available()})
        self.log_panel.log(self.tr_.format("main.import_language_done", languages=names))
        self._fill_lang_combo()
        return codes

    def set_language(self, code: str) -> None:
        self.tr_.set_language(code)
        idx = self.lang_combo.findData(code)
        if idx >= 0 and idx != self.lang_combo.currentIndex():
            self.lang_combo.blockSignals(True)
            self.lang_combo.setCurrentIndex(idx)
            self.lang_combo.blockSignals(False)
        self.layer_panel.refresh()
        self.part_list.refresh()

    def set_mode(self, mode: str) -> None:
        self.canvas.set_mode(mode)
        a = self.ribbon.actions
        for cmd, m in (("view.select_mode", CanvasView.MODE_SELECT), ("view.pan_mode", CanvasView.MODE_PAN),
                       ("view.node_edit", CanvasView.MODE_NODE)):
            a[cmd].setChecked(m == mode)

    def set_document(self, doc: Document, path: Optional[str] = None) -> None:
        for li in sorted({p.craft.layer for p in doc.parts}):
            doc.layers.setdefault(li, LayerParams(index=li, color=layer_color(li).name()))
        self.document = doc
        self.path = path
        self.canvas.set_document(doc)
        self._update_toolpath()
        self.layer_panel.set_document(doc)
        self.part_list.set_document(doc)

    def _update_toolpath(self) -> None:
        """Rebuild the CAM toolpath (mlaser.cam.build_toolpath) and show it on the canvas."""
        build = _lazy("mlaser.cam.toolpath", "build_toolpath")
        self.toolpath = []
        if build is not None and self.document.parts:
            try:
                self.toolpath = build(self.document, self.cam_params)
            except Exception as exc:  # noqa: BLE001 - CAM errors must not break editing
                self.log_panel.log(f"toolpath: {exc}")
        self.canvas.set_toolpath(self.toolpath)

    def _refresh(self) -> None:
        self._update_toolpath()
        self.layer_panel.set_document(self.document)
        self.part_list.set_document(self.document)

    def _snapshot(self) -> None:
        self._undo.append(copy.deepcopy(self.document))
        del self._undo[:-50]
        self._redo.clear()

    def _edit(self, fn, *args) -> None:
        self._snapshot()
        fn(self.document, *args)
        self._refresh()

    def _select(self, pred) -> None:
        docops.select_where(self.document, pred)
        self.canvas.refresh_styles()
        self.part_list.sync_selection()

    def undo(self) -> None:
        if self._undo:
            self._redo.append(self.document)
            self.document = self._undo.pop()
            self.set_document(self.document, self.path)

    def redo(self) -> None:
        if self._redo:
            self._undo.append(self.document)
            self.document = self._redo.pop()
            self.set_document(self.document, self.path)

    def delete_selected(self) -> None:
        if any(p.selected for p in self.document.parts):
            self._edit(docops.delete_selected)

    def _context_menu(self, pos) -> None:
        """Canvas right-click menu (lang keys mv0..mv35 observed: mv1 Reset View, mv7 Copy,
        mv8 Paste, mv9 Delete, mv11 Clear track line, mv34 Select Same Layer Contours)."""
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        t = self.tr_.tr
        m.addAction(t("mv1"), self.canvas.zoom_fit)
        m.addSeparator()
        m.addAction(t("mv7"), self.copy_selected)
        m.addAction(t("mv8"), self.paste)
        m.addAction(t("mv9"), self.delete_selected)
        m.addSeparator()
        m.addAction(t("mv11"), lambda: self.canvas.stop_simulation(clear=True))
        def same_layer():
            layers = {p.craft.layer for p in self.document.parts if p.selected}
            self._select(lambda p: p.craft.layer in layers)
        m.addAction(t("mv34"), same_layer)
        m.exec(self.canvas.mapToGlobal(pos))

    def copy_selected(self) -> None:
        self._clipboard = [copy.deepcopy(p) for p in self.document.parts if p.selected]

    def paste(self) -> None:
        """Paste copies offset by 10 mm (the original pastes at a picked point, menu 134)."""
        clip = getattr(self, "_clipboard", None)
        if not clip:
            return
        def do(d):
            for p in d.parts:
                p.selected = False
            new = [copy.deepcopy(p) for p in clip]
            docops.translate(d, 10.0, 10.0, new)
            for p in new:
                p.selected = True
            d.parts.extend(new)
            d.extra.pop("order", None)
        self._edit(do)

    def nudge(self, dx: float, dy: float) -> None:
        """mf445 keyboard fine-tuning: move selection by pd377 step length (default 1 mm)."""
        step = float(self.settings.get("SP.KeyboardMoveStep", 1.0))
        if any(p.selected for p in self.document.parts):
            self._edit(lambda d: docops.translate(d, dx * step, dy * step, [p for p in d.parts if p.selected]))

    def assign_layer(self, layer: int) -> None:
        if any(p.selected for p in self.document.parts):
            self._edit(docops.set_layer, layer)

    def edit_layer(self, layer: int) -> None:
        lp = self.document.layers.setdefault(layer, LayerParams(index=layer, color=layer_color(layer).name()))
        if dialogs.LayerParamDialog(self.tr_, lp, self).exec():
            self.layer_panel.refresh()

    def _kerf_width(self) -> float:
        sel = [p for p in self.document.parts if p.selected] or self.document.parts
        return sel[0].craft.kerf_width if sel and sel[0].craft.kerf_width else 0.1

    # ------------------------------------------------------------------ files
    FILTER = "Supported (*.dxf *.chf *.nc *.txt *.cnc *.g *.plt *.svg);;DXF (*.dxf);;CHF (*.chf);;All (*)"

    def file_new(self) -> None:
        self._snapshot()
        self.set_document(Document())

    def load_path(self, path: str) -> bool:
        ext = os.path.splitext(path)[1].lower()
        loader = None
        if ext == ".chf":
            loader = _lazy("mlaser.formats.chf", "load_chf")
        elif ext == ".dxf":
            loader = _lazy("mlaser.io.dxf_import", "import_dxf")
        elif ext == ".svg":
            loader = _lazy("mlaser.io.svg_import", "import_svg")
        elif ext in (".nc", ".txt", ".cnc", ".g"):
            loader = _lazy("mlaser.io.gcode", "read_gcode")
        if loader is None:
            self.log_panel.log(f"mf155: no importer for {ext} yet")
            return False
        try:
            doc = loader(path)
        except Exception as exc:  # noqa: BLE001 - show any loader failure to the user
            self.log_panel.log(f"mv32: {exc}")
            return False
        self._snapshot()
        self.set_document(doc, path if ext == ".chf" else None)
        self.log_panel.log(f"{os.path.basename(path)}: {len(doc.parts)} parts")
        return True

    def file_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.tr_.tr("mf150"), "", self.FILTER)
        if path:
            self.load_path(path)

    def file_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.tr_.tr("mf13"), "", self.FILTER)
        if not path:
            return
        before = self.document
        if self.load_path(path) and before.parts:
            merged = copy.deepcopy(before)
            merged.parts.extend(self.document.parts)
            self.set_document(merged, self.path)

    def file_save(self, save_as: bool = False) -> None:
        saver = _lazy("mlaser.formats.chf", "save_chf")
        if saver is None:
            self.log_panel.log("mf153: .chf writer not available yet")
            return
        path = self.path
        if save_as or not path:
            path, _ = QFileDialog.getSaveFileName(self, self.tr_.tr("mf152"), "", "chf (*.chf)")
        if path:
            saver(self.document, path)
            self.path = path
            self.log_panel.log(f"np_FileSaveDone: {path}")

    # ------------------------------------------------------------------ dialogs
    def options_dialog(self, which: str) -> None:
        dialogs.ParamGridDialog(self.tr_, "mf137" if which == "app.options" else "mf129",
                                OPTION_SECTIONS[which], self.settings, self).exec()

    def ip_dialog(self) -> None:
        d = dialogs.IPDialog(self.tr_, self.settings.get("ip"), self)
        if d.exec():
            self.settings["ip"] = d.result_values()

    def reconnect(self) -> None:
        host = (self.settings.get("ip") or {}).get("pd445", "10.1.1.168")
        ok = self.ctl.connect(host)
        self.log_panel.log("mf165" if ok else "mf166")

    def lead_dialog(self) -> None:
        d = dialogs.LeadDialog(self.tr_, self.lead_in, self.lead_out, self)
        if d.exec():
            self._edit(docops.apply_lead, self.lead_in, self.lead_out, self.lead_closed_only,
                       self.lead_start_mode)
            self.log_panel.log("mf207")

    def kerf_dialog(self) -> None:
        d = dialogs.KerfDialog(self.tr_, 1, self._kerf_width(), self)
        if d.exec():
            self._edit(docops.apply_kerf, *d.result_values())

    def sort(self, how: str) -> None:
        """Cutting order through mlaser.cam.ordering (CamParams.sort_mode / small_first); modes
        the CAM module does not provide (out->in, layer first/last) fall back to docops."""
        if self.cam_params is None:
            self._edit(docops.sort_parts, how)
            return
        if how == "small_first":
            self.cam_params.small_first = not self.cam_params.small_first
        else:
            self.cam_params.sort_mode = SORT_MODES.get(how, 5)
        self._update_toolpath()
        self.log_panel.log(f"sort: mode {self.cam_params.sort_mode}, small first {self.cam_params.small_first}")

    def microjoint_dialog(self) -> None:
        d = dialogs.MicroJointDialog(self.tr_, self)
        mp = self.mj_params
        d.kind.setCurrentIndex(mp["kind"]); d.number.setValue(mp["count"])
        d.distance.setValue(mp["distance"]); d.length.setValue(mp["length"]); d.min_size.setValue(mp["min_size"])
        if d.exec():
            mp.update(kind=d.kind.currentIndex(), count=d.number.value(), distance=d.distance.value(),
                      length=d.length.value(), min_size=d.min_size.value())
            self.microjoint_auto()

    def microjoint_auto(self) -> None:
        """mf103 auto micro-joints via mlaser.cam.microjoint.auto_joints (0x10071180)."""
        auto = _lazy("mlaser.cam.microjoint", "auto_joints")
        if auto is None:
            self.log_panel.log("craft.mj_auto: mlaser.cam not available")
            return
        mp = self.mj_params

        def do(doc):
            n = 0
            for p in docops._targets(doc):
                if not p.contours:
                    continue
                c = max(p.contours, key=lambda k: k.length())
                js = auto(c, count=mp["count"] if mp["kind"] == 0 else 0,
                          spacing=mp["distance"] if mp["kind"] == 1 else 0.0,
                          joint_len=mp["length"], min_size=mp["min_size"])
                p.craft.micro_joints = [list(j) for j in js]
                p.craft.extra["micro_joint_len"] = mp["length"]
                n += len(js)
            self.log_panel.log(f"mf103: {n} micro-joints")
        self._edit(do)

    def array_dialog(self) -> None:
        d = dialogs.ArrayDialog(self.tr_, self)
        if d.exec():
            sx = -1 if d.col_dir.currentIndex() else 1
            sy = -1 if d.row_dir.currentIndex() else 1
            self._edit(docops.array, d.rows.value(), d.cols.value(), sx * d.col_gap.value(), sy * d.row_gap.value())

    def rotate_dialog(self) -> None:
        d = dialogs.ValueDialog(self.tr_, "mf185", "pd339", 30.0, -360, 360, self)
        if d.exec():
            self._edit(docops.rotate, d.value.value())

    def scale_dialog(self) -> None:
        d = dialogs.ValueDialog(self.tr_, "mf60", "pd348", 100.0, 0.1, 10000, self)
        if d.exec():
            self._edit(docops.scale, d.value.value() / 100.0)

    def sim_speed_dialog(self) -> None:
        d = dialogs.ValueDialog(self.tr_, "mf121", "mf121", self.canvas.sim_speed, 0.1, 100, self)
        if d.exec():
            self.canvas.sim_speed = d.value.value()

    # ------------------------------------------------------------------ machine
    def simulate(self) -> None:
        """mp59 Sim: time-sampled simulation of the CAM toolpath (mlaser.motion.simulate) with
        the job time estimate (mlaser.motion.estimate_time)."""
        if not self.document.parts:
            self.log_panel.log("mp76")
            return
        sim = _lazy("mlaser.motion.interp", "simulate")
        est = _lazy("mlaser.motion.interp", "estimate_time")
        if not self.toolpath or sim is None or self.machine is None:
            self.canvas.start_simulation()
            return
        cfg = self.machine.motion_config()
        self.estimate = e = est(self.toolpath, cfg)
        pierces = sum(1 for m in self.toolpath if type(m).__name__ == "Pierce")
        total = e["total"]
        args = (int(total // 60), total % 60, e["cut_length"] / 1000.0, e["rapid_length"] / 1000.0, pierces)
        self.log_panel.log(self.tr_.format("mp83", *args))   # panel.estimate
        samples = sim(self.toolpath, cfg, period_ms=4.0)
        self.canvas.start_simulation(path=[(s.x, s.y, s.laser) for s in samples])

    def set_controller(self, ctl: ControllerFacade) -> None:
        """Swap the machine controller (e.g. demo -> real adapter)."""
        try:
            self.ctl.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.ctl = ctl
        self.control.ctl = ctl
        ctl.add_log_listener(self.log_panel.log)

    def connect_simulator(self, time_scale: float = 1.0) -> bool:
        """Start the in-process MCC100 simulator (mlaser.controller.simulator) on localhost UDP
        and connect the real protocol stack to it."""
        cls = _lazy("mlaser.controller.ui_adapter", "UiController")
        if cls is None:
            self.log_panel.log("mlaser.controller not available")
            return False
        if not isinstance(self.ctl, cls):
            self.set_controller(cls(machine=self.machine, cam_params=self.cam_params))
        self.ctl.cam_params = self.cam_params
        return self.ctl.connect_simulator(time_scale=time_scale)

    _last_mode = "offline"
    _MC = {"offline": "mp22", "ready": "mp7", "origin": "mp8", "jog": "mp9", "stop": "mp10",
           "process": "mp11", "pause": "mp55", "frame": "mp58", "dry_run": "mp57"}
    _ZF = {"offline": "mp23", "ready": "mp12", "follow": "mp13", "drill": "mp14", "jog_up": "mp15",
           "jog_down": "mp16", "estop": "mp17"}

    def _poll(self) -> None:
        st = self.ctl.state()
        t = self.tr_.tr
        mc = t(self._MC.get(st.mode, "mp22"))
        if not mc.startswith("MCC") and "<" not in mc:
            mc = f"MCC <{mc}>"
        self.st_mc.setText(mc)
        self.st_zf.setText(t(self._ZF.get(st.ftc, "mp23")))
        self.st_lf.setText(t("mp25" if st.laser == "ready" else "mp24"))
        self.st_xy.setText(f"X: {st.x:.3f}  Y: {st.y:.3f}  Z: {st.z:.3f}")
        self.st_cv.setText(self.tr_.format("mp80", unit="", speed=st.speed))   # status.speed
        if not self.canvas.simulation_running():
            self.canvas.set_head_position(st.x, st.y)
        if st.mode in ("process", "dry_run", "frame"):
            self.canvas.add_track_point(st.x, st.y, st.mode == "process" and st.outputs.get("laser", False))
        elif st.mode == "ready" and self._last_mode in ("process", "dry_run", "frame"):
            self.canvas.add_track_point(st.x, st.y, False)
        self._last_mode = st.mode
