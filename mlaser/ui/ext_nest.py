"""Nest tab (排样, ``mf460``) and the extra cutting-order commands.

``register(ctx)`` (PluginContext) binds the Nest ribbon commands (``nest.*`` in ribbon_spec) and
adds a docked Nest panel (part list ``np_Part``/``np_PartName``/``np_PartNum``, sheet list
``np_Sheet``/``np_SheetName``/``np_SheetNum``, results ``np_Result``), the nest-parameter
dialog (``mf412``, ``pd_Nest_*``), the sheet dialog (``pd767..769``), a cancellable run with
progress, "copy result to main view" (``nset_UI_Result2MainView``) and "export to sheets"
(``mf420``). It also adds the ordering commands that are edits of the part order
(``mf610/611`` layer first/last, ``mv15/16`` first/last, the manual-sort tool ``mf111`` with
``mv18``..``mv23``) and the rapid-avoidance toggle; every ordering *mode* (``sort.*`` incl.
in->out / out->in / ring / grid / shortest path) is the main window's one sort path.  Edits are
``ctx.edit`` transactions, the nest result replaces the document with ``ctx.replace_document``.
"""
from __future__ import annotations

import copy
import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
                               QMessageBox,
                               QProgressDialog, QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from ..model import Document
from ..nest import (ENGINE_RASTER, ENGINE_SPYRROW, GRAVITY_AUTO, GRAVITY_BOTTOM, GRAVITY_LEFT, NestJob,
                    NestParams, NestPart, NestSheet, apply_result, nest, stats)
from ..nest.engine import NestEngineUnavailable, available_engines
from ..nest.apply import layout_parts

PROJECT_FILTER = "Nest project (*.nestproj);;All (*)"


def _tr(window, key: str, default: str = "") -> str:
    tr = getattr(window, "tr_", None)
    if tr is None:
        return default or key
    try:
        return tr.tr(key, default or None)
    except TypeError:
        return tr.tr(key)


def _label(window, key: str, default: str = "") -> str:
    """``pd_Nest_*`` keys read "Group.Name"; show the part after the dot."""
    t = _tr(window, key, default)
    return t.split(".", 1)[1] if "." in t else t


def _spin(lo, hi, val, dec=2) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setValue(val)
    return s


# ---------------------------------------------------------------------------- dialogs
class NestParamsDialog(QDialog):
    """``mf412`` Nest Parameters: spacing ``pd_Nest_PartSpace``, edge ``pd_Nest_EdgeStock``,
    rotation step ``pd_Nest_PartRotateStepAngle``, direction ``pd_Nest_NestDir`` (L2R/D2U),
    part-in-part ``pd_Nest_NestPartInner``, share edge ``pd_Nest_ShareEdge`` (+ min length),
    accuracy ``pd_Nest_NestAccuracy`` (curve tolerance), plus rebuild-only raster cell,
    mirror, kerf, time limit and engine (raster default; sparrow/spyrrow when installed)."""

    def __init__(self, window, params: NestParams, parent=None):
        super().__init__(parent)
        self.params = params
        self.setWindowTitle(_tr(window, "mf412", "Nest Parameters"))
        lay = QVBoxLayout(self)
        f = QFormLayout()
        lay.addLayout(f)
        self.spacing = _spin(0, 1000, params.spacing)
        self.margin = _spin(0, 1000, params.margin)
        self.step = _spin(0, 360, params.rotation_step)
        self.direction = QComboBox()
        self.direction.addItem(_tr(window, "pd_Nest_NestDir_L2R", "Left to Right"), GRAVITY_LEFT)
        self.direction.addItem(_tr(window, "pd_Nest_NestDir_D2U", "Down to Up"), GRAVITY_BOTTOM)
        self.direction.addItem("Auto", GRAVITY_AUTO)       # AutoNest direction 3 (no lang key)
        self.direction.setCurrentIndex(max(0, self.direction.findData(params.gravity)))
        self.inner = QCheckBox()
        self.inner.setChecked(params.part_in_part)
        self.mirror = QCheckBox()
        self.mirror.setChecked(params.allow_mirror)
        self.share = QCheckBox()
        self.share.setChecked(params.common_line)
        self.share_min = _spin(0, 10000, params.share_edge_min_len)
        self.kerf = _spin(0, 100, params.kerf, 3)
        self.accuracy = _spin(0.1, 1.0, params.accuracy)
        self.cell = _spin(0, 50, params.resolution or 0.0)
        self.cell.setSpecialValueText("auto")
        self.time_limit = _spin(1, 3600, params.time_limit_s, 0)
        self.engine = QComboBox()
        self.engine.addItem("Raster (built-in)", ENGINE_RASTER)
        self.engine.addItem("Sparrow (spyrrow)", ENGINE_SPYRROW)
        if ENGINE_SPYRROW not in available_engines():
            item = self.engine.model().item(1)
            if item is not None:
                item.setEnabled(False)
            self.engine.setItemData(1, "needs the optional package spyrrow: pip install 'mlaser[nest]'",
                                    Qt.ToolTipRole)
        self.engine.setCurrentIndex(max(0, self.engine.findData(params.engine)))
        for key, dflt, w in (("pd_Nest_PartSpace", "Part Space", self.spacing),
                             ("pd_Nest_EdgeStock", "Edge stock", self.margin),
                             ("pd_Nest_PartRotateStepAngle", "Part rotate step angle", self.step),
                             ("pd_Nest_NestDir", "Nest Direction", self.direction),
                             ("pd_Nest_NestPartInner", "Is Nest in part inner", self.inner),
                             ("", "Mirror", self.mirror),
                             ("pd_Nest_ShareEdge", "Enable share edge", self.share),
                             ("pd_Nest_ShareEdgeMinLen", "Min length of share edge", self.share_min),
                             ("", "Kerf (share edge spacing)", self.kerf),
                             ("pd_Nest_NestAccuracy", "Nest Graph accuracy", self.accuracy),
                             ("", "Raster cell (mm)", self.cell),
                             ("", "Time limit (s)", self.time_limit),
                             ("", "Engine", self.engine)):
            f.addRow(_label(window, key, dflt) if key else dflt, w)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def apply(self) -> NestParams:
        p = self.params
        p.spacing, p.margin, p.rotation_step = self.spacing.value(), self.margin.value(), self.step.value()
        p.gravity = self.direction.currentData()
        p.part_in_part, p.allow_mirror = self.inner.isChecked(), self.mirror.isChecked()
        p.common_line, p.share_edge_min_len = self.share.isChecked(), self.share_min.value()
        p.kerf = self.kerf.value()
        p.accuracy = self.accuracy.value()
        p.resolution = self.cell.value() or None
        p.time_limit_s = self.time_limit.value()
        p.engine = self.engine.currentData() or ENGINE_RASTER
        return p


class SheetDialog(QDialog):
    """Standard sheet (``mf473``/``np_StdSheet``): width ``pd769``, height ``pd768``,
    count ``pd767``."""

    def __init__(self, window, sheet: Optional[NestSheet] = None, parent=None):
        super().__init__(parent)
        sheet = sheet or NestSheet()
        self.setWindowTitle(_tr(window, "np_StdSheet", "Std Sheet"))
        lay = QVBoxLayout(self)
        f = QFormLayout()
        lay.addLayout(f)
        self.name = QLineEdit(sheet.name)
        self.width = _spin(1, 100000, sheet.width)
        self.height = _spin(1, 100000, sheet.height)
        self.count = QSpinBox()
        self.count.setRange(1, 9999)
        self.count.setValue(sheet.quantity)
        f.addRow(_tr(window, "np_SheetName", "Sheet Name"), self.name)
        f.addRow(_label(window, "pd769", "Sheet Width"), self.width)
        f.addRow(_label(window, "pd768", "Sheet Height"), self.height)
        f.addRow(_label(window, "pd767", "Sheet Number"), self.count)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def sheet(self) -> NestSheet:
        return NestSheet(self.width.value(), self.height.value(), self.count.value(), self.name.text())


# ---------------------------------------------------------------------------- panel
class NestPanel(QWidget):
    """Parts / sheets / results lists of the nest project."""

    PART_COLS = ("np_PartName", "np_PartNum", "max", "priority", "mirror")

    def __init__(self, ext: "NestExtension"):
        super().__init__()
        self.ext = ext
        w = ext.window
        v = QVBoxLayout(self)
        v.addWidget(QLabel(_tr(w, "np_Part", "Part")))
        self.parts = QTableWidget(0, 5)
        self.parts.setHorizontalHeaderLabels([_tr(w, "np_PartName", "Part Name"), _tr(w, "np_PartNum", "Part Num"),
                                              "Max", "Priority", "Mirror"])
        self.parts.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.parts.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.parts.itemChanged.connect(self._part_edited)
        v.addWidget(self.parts)
        v.addWidget(QLabel(_tr(w, "np_Sheet", "Sheet")))
        self.sheets = QTableWidget(0, 4)
        self.sheets.setHorizontalHeaderLabels([_tr(w, "np_SheetName", "Sheet Name"), "W", "H",
                                               _tr(w, "np_SheetNum", "Sheet Num")])
        self.sheets.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sheets.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.sheets.itemChanged.connect(self._sheet_edited)
        v.addWidget(self.sheets)
        v.addWidget(QLabel(_tr(w, "np_Result", "Nest Result")))
        self.results = QTableWidget(0, 3)
        self.results.setHorizontalHeaderLabels([_tr(w, "np_Sheet", "Sheet"), _tr(w, "np_NestPartCount", "Nested Part Num"), "%"])
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        v.addWidget(self.results)
        row = QHBoxLayout()
        for key, dflt, fn in (("mf465", "Add Part", ext.add_part_from_selection),
                              ("mf473", "Import Sheet", ext.add_std_sheet),
                              ("mf412", "Nest Parameters", ext.params_dialog),
                              ("mf464", "Advanced Nest", ext.run),
                              ("nset_UI_Result2MainView", "Copy to main", ext.to_main)):
            b = QPushButton(_tr(w, key, dflt))
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)
        self._sync = False

    def refresh(self) -> None:
        job = self.ext.job
        self._sync = True
        self.parts.setRowCount(len(job.parts))
        for r, p in enumerate(job.parts):
            vals = [p.name, str(p.quantity), "" if p.max_quantity is None else str(p.max_quantity),
                    str(p.priority), "1" if p.mirror else "0"]
            for c, val in enumerate(vals):
                self.parts.setItem(r, c, QTableWidgetItem(val))
        self.sheets.setRowCount(len(job.sheets))
        for r, s in enumerate(job.sheets):
            vals = [s.name, f"{s.width:g}", f"{s.height:g}", str(s.quantity)]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if s.outline and c in (1, 2):
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.sheets.setItem(r, c, it)
        res = job.result
        lays = res.layouts if res else []
        self.results.setRowCount(len(lays))
        for r, lay in enumerate(lays):
            sh = job.sheet(lay.sheet_id)
            for c, val in enumerate([f"{_tr(self.ext.window, 'np_Reslut_', 'Result_')}{r + 1} ({sh.name})",
                                     str(len(lay.placements)), f"{100 * lay.utilisation:.1f}"]):
                it = QTableWidgetItem(val)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.results.setItem(r, c, it)
        if lays:
            self.results.selectRow(0)
        self._sync = False

    def _part_edited(self, it: QTableWidgetItem) -> None:
        if self._sync:
            return
        p = self.ext.job.parts[it.row()]
        txt = it.text().strip()
        try:
            if it.column() == 0:
                p.name = txt
            elif it.column() == 1:
                p.quantity = max(0, int(txt))
            elif it.column() == 2:
                p.max_quantity = int(txt) if txt else None
            elif it.column() == 3:
                p.priority = int(txt)
            elif it.column() == 4:
                p.mirror = txt not in ("", "0", "false", "False")
        except ValueError:
            pass
        self.ext.dirty = True

    def _sheet_edited(self, it: QTableWidgetItem) -> None:
        if self._sync:
            return
        s = self.ext.job.sheets[it.row()]
        txt = it.text().strip()
        try:
            if it.column() == 0:
                s.name = txt
            elif it.column() == 1:
                s.width = float(txt)
            elif it.column() == 2:
                s.height = float(txt)
            elif it.column() == 3:
                s.quantity = max(0, int(txt))
        except ValueError:
            pass
        self.ext.dirty = True

    def selected_layout(self) -> Optional[int]:
        rows = sorted({i.row() for i in self.results.selectedIndexes()})
        return rows[0] if rows else (0 if self.results.rowCount() else None)


# ---------------------------------------------------------------------------- extension
class NestExtension:
    def __init__(self, ctx):
        self.ctx = ctx
        self.window = window = ctx.window
        self.job = NestJob()
        self.path: Optional[str] = None
        self.dirty = False
        self.panel = NestPanel(self)
        dock = ctx.add_dock(self.panel, _tr(window, "mf460", "Nest"), "left", "mf460")
        dock.hide()
        self.dock = dock

    def log(self, msg: str) -> None:
        lp = getattr(self.window, "log_panel", None)
        if lp is not None:
            lp.log(msg)

    def show(self) -> None:
        """Show the classic Nest dock, except while the Nest workspace is active: it shows the
        same nest project, and the dock used to stay open in Run and Report afterwards."""
        shell = getattr(self.window, "shell", None)
        host = getattr(shell, "host", None)
        if host is None or host.resolve(host.current) != "nest":
            self.dock.show()
            self.dock.raise_()
        self.panel.refresh()

    # -------------------------------------------------- project
    def new_project(self) -> None:
        self.job = NestJob()
        self.path = None
        self.dirty = False
        self.show()

    def open_project(self, path: Optional[str] = None) -> bool:
        from ..nest.project import load_project
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self.window, _tr(self.window, "FileTitle_OpenNestProjFile"), "", PROJECT_FILTER)
        if not path:
            return False
        try:
            self.job = load_project(path)
        except Exception as exc:  # noqa: BLE001 - report bad files to the user
            self.log(f"{_tr(self.window, 'MsgBox_Nest_InvalieFileSuffix')}: {exc}")
            return False
        self.path = path
        self.dirty = False
        self.show()
        return True

    def save_project(self, save_as: bool = False, path: Optional[str] = None) -> bool:
        from ..nest.project import save_project
        path = path or (None if save_as else self.path)
        if not path:
            path, _ = QFileDialog.getSaveFileName(self.window, _tr(self.window, "FileTitle_SaveNestProjFile"), "", PROJECT_FILTER)
        if not path:
            return False
        save_project(self.job, path)
        self.path = path
        self.dirty = False
        self.log(f"{_tr(self.window, 'np_FileSaveDone')} {path}")
        return True

    # -------------------------------------------------- parts / sheets
    def add_part_from_selection(self) -> int:
        """``mf465`` Add Part: every selected part of the main view (all if none selected)."""
        doc: Document = self.window.document
        src = [p for p in doc.parts if p.selected] or list(doc.parts)
        n = 0
        for p in src:
            if p.contours:
                q = copy.deepcopy(p)
                q.selected = False
                self.job.add_part(NestPart([q], name=p.name, quantity=1))
                n += 1
        self.dirty = self.dirty or n > 0
        self.show()
        return n

    def import_part(self, path: Optional[str] = None) -> int:
        """``mf461`` Import Part: a DXF/chf file becomes one nest part (all its parts)."""
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self.window, _tr(self.window, "mf461"), "",
                                                  "Parts (*.dxf *.chf *.svg);;All (*)")
        if not path:
            return 0
        doc = _load_doc(path)
        if doc is None or not doc.parts:
            return 0
        self.job.add_part(NestPart([p for p in doc.parts if p.contours],
                                   name=os.path.splitext(os.path.basename(path))[0]))
        self.dirty = True
        self.show()
        return 1

    def add_std_sheet(self) -> Optional[NestSheet]:
        d = SheetDialog(self.window, None, self.window)
        if not d.exec():
            return None
        sh = self.job.add_sheet(d.sheet())
        self.dirty = True
        self.show()
        return sh

    def add_sheet_from_selection(self) -> Optional[NestSheet]:
        """``mf466`` Add Sheet: selected main-view geometry becomes a remnant sheet."""
        sel = [copy.deepcopy(p) for p in self.window.document.parts if p.selected]
        if not sel:
            return None
        from ..nest.shapes import parts_geometry
        g = parts_geometry(sel)
        x0, y0, x1, y1 = g.bounds
        sh = self.job.add_sheet(NestSheet(x1 - x0, y1 - y0, 1, origin=(x0, y0), outline=sel, is_remnant=True))
        self.dirty = True
        self.show()
        return sh

    def params_dialog(self) -> None:
        d = NestParamsDialog(self.window, self.job.params, self.window)
        if d.exec():
            d.apply()
            self.dirty = True

    # -------------------------------------------------- run / result
    def run(self, interactive: bool = True):
        if not self.job.parts:
            self.add_part_from_selection()
        if not self.job.sheets:
            self.job.add_sheet(NestSheet())
        dlg = None
        if interactive:
            dlg = QProgressDialog(_tr(self.window, "mf464", "Advanced Nest"), _tr(self.window, "ap0", "Cancel"),
                                  0, 1000, self.window)
            dlg.setWindowModality(Qt.WindowModal)
            dlg.setMinimumDuration(300)

        def progress(frac: float, msg: str):
            if dlg is None:
                return True
            dlg.setValue(int(1000 * min(1.0, frac)))
            dlg.setLabelText(msg)
            QApplication.processEvents()
            return not dlg.wasCanceled()

        try:
            res = nest(self.job, progress)
        except NestEngineUnavailable as e:
            if dlg is not None:
                dlg.close()
            self.log(f"nest: {e}")
            if interactive:
                QMessageBox.warning(self.window, _tr(self.window, "mf464", "Advanced Nest"), str(e))
            return None
        if dlg is not None:
            dlg.setValue(1000)
        for note in res.notes:
            self.log(f"nest: {note}")
        st = stats(self.job)
        self.log(f"nest: {sum(res.placed.values())} parts on {len(res.layouts)} sheets, "
                 f"{100 * st['utilisation']:.1f}%, unplaced {sum(res.unplaced.values())}, {res.elapsed_s:.2f}s")
        self.dirty = True
        self.show()
        return res

    def result_document(self, layout_index: Optional[int] = None, include_sheet: bool = True) -> Document:
        return apply_result(self.job, layout_index=layout_index, include_sheet=include_sheet)

    def to_main(self) -> bool:
        """``nset_UI_Result2MainView``: replace the main document with the selected result
        sheet (undoable)."""
        if self.job.result is None or not self.job.result.layouts:
            return False
        idx = self.panel.selected_layout() or 0
        base = self.window.document
        doc = self.result_document(idx, include_sheet=False)
        doc.layers = copy.deepcopy(base.layers)
        self.ctx.replace_document(doc, "nest result")
        return True

    def export_sheets(self, directory: Optional[str] = None) -> list[str]:
        """``mf420`` Export to Sheets: one .chf per result layout (``np_Reslut_<n>``)."""
        if self.job.result is None:
            return []
        try:
            from ..formats.chf import save_chf
        except Exception:  # noqa: BLE001
            self.log("mf420: .chf writer not available")
            return []
        if directory is None:
            directory = QFileDialog.getExistingDirectory(self.window, _tr(self.window, "FileTitle_ExportNestFile"))
        if not directory:
            return []
        out = []
        base = self.window.document
        for k, lay in enumerate(self.job.result.layouts):
            doc = Document(parts=layout_parts(self.job, lay), layers=copy.deepcopy(base.layers))
            path = os.path.join(directory, f"{_tr(self.window, 'np_Reslut_', 'Result_')}{k + 1}.chf")
            from ..cam.raster_engraving import expand_document
            save_chf(expand_document(doc), path)
            out.append(path)
        self.log(f"mf420: {len(out)} files")
        return out


def _load_doc(path: str) -> Optional[Document]:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".dxf":
            from ..io.dxf_import import import_dxf
            return import_dxf(path)
        if ext == ".chf":
            from ..formats.chf import load_chf
            return load_chf(path)
        if ext == ".svg":
            from ..io.svg_import import import_svg
            return import_svg(path)
        if ext == ".mlz":           # queued nest sheets with a cutting strategy (services.nesting)
            from ..project.format import load_project
            return load_project(path).document
    except Exception:  # noqa: BLE001
        return None
    return None


# ---------------------------------------------------------------------------- sorting
class SortExtension:
    """Ordering edits and the manual-sort tool.

    MainApp FUN_004b6190 (docs/re/cam.md section 8): mf610/611 selected layer first/last = types
    10/11 (stable partition of the graph list), mf111 manual sort = tool 9; mv15/mv16 move one
    part last/first.  Each changes the part order and switches to mode 0 (keep)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.window = ctx.window
        self.manual_active = False
        self.step = 0

    def log(self, msg: str) -> None:
        self.ctx.log(msg)

    def set_mode(self, mode: int, **flags) -> None:
        self.window.set_sort_mode(mode, **flags)

    def _order_edit(self, fn, label: str):
        """An edit that only reorders parts (scope: no part changes in place)."""
        return self.ctx.edit(fn, label, parts=())

    def layer_first(self, last: bool = False) -> bool:
        """Types 10/11: all selected parts must share one layer (as the original checks)."""
        from ..cam import ordering as O
        layers = {p.craft.layer for p in self.ctx.document.parts if p.selected}
        if len(layers) != 1:
            self.log("mf610: select parts of one layer")
            return False
        self._order_edit(lambda doc: O.set_layer_priority(doc, layers, last), "layer order")
        self.set_mode(O.MODE_KEEP)
        return True

    # ---------------------------------------------------- manual sort (tool 9)
    def toggle_manual(self) -> None:
        if self.manual_active:
            self.finish_manual()
        else:
            self.start_manual()

    def start_manual(self) -> bool:
        from ..cam import ordering as O
        if len(self.ctx.document.parts) < 2:
            return False
        self.manual_active = True
        self.step = 1
        cv = self.window.canvas
        cv.select_parts([])
        cv.selectionChanged.connect(self._picked)
        self.set_mode(O.MODE_KEEP)
        self.log(_tr(self.window, "mv21", "Manual sort start: ") + _tr(self.window, "mv22", "Manual Sort: Step 0"))
        return True

    def pick(self, index: int) -> None:
        """One click: part ``index`` goes to slot ``step``; ends automatically after the last.
        Each pick is one undo step."""
        from ..cam import ordering as O
        if not self.manual_active:
            return
        step = self.step
        nxt = self._order_edit(lambda doc: O.manual_pick(doc, index, step), "manual sort")
        if nxt != self.step:
            self.log(f"{_tr(self.window, 'mv19', 'Manual Sort: Step')} {self.step}")
            self.step = nxt
        if self.step == 0:
            self.finish_manual()

    def previous_step(self) -> None:
        """mv13: step back so the last slot can be picked again."""
        if self.manual_active and self.step >= 2:
            self.step -= 1

    def _picked(self) -> None:
        sel = self.window.canvas.selected_indices()
        if sel:
            self.pick(sel[0])

    def finish_manual(self) -> None:
        if not self.manual_active:
            return
        try:
            self.window.canvas.selectionChanged.disconnect(self._picked)
        except (RuntimeError, TypeError):
            pass
        self.manual_active = False
        self.step = 0
        self.log(_tr(self.window, "mv18", "Manual Sort: End"))

    def move_selected(self, last: bool) -> bool:
        """mv16 / mv15: the single selected part becomes first / last."""
        from ..cam import ordering as O
        sel = [i for i, p in enumerate(self.ctx.document.parts) if p.selected]
        if len(sel) != 1:
            return False
        self._order_edit(lambda doc: O.move_first_last(doc, sel[0], last), "move first/last")
        self.set_mode(O.MODE_KEEP)
        return True


# commands added to the sort.run button menu (the ordering modes' handlers are MainWindow.sort)
SORT_MENU = ["sort.keep", "sort.shortest", "sort.grid", "sort.avoid", "sort.manual_prev",
             "sort.manual_end", "sort.first", "sort.last"]


def _sort_button_menu(window) -> Optional[QMenu]:
    act = window.ribbon.actions.get("sort.run")
    for tb in window.ribbon.findChildren(QToolButton):
        if tb.defaultAction() is act and tb.menu() is not None:
            return tb.menu()
    return None


def register(ctx) -> tuple[NestExtension, SortExtension]:
    """Bind the Nest commands and the ordering edits (``ctx``: PluginContext)."""
    window = ctx.window
    nx = NestExtension(ctx)
    sx = SortExtension(ctx)
    keys = {"nest.open": "mf_S_File_OpenProject", "nest.add_sheet": "mf466", "nest.params": "mf412",
            "nest.simple": "mf463", "nest.export": "mf420", "nest.show": "mf460"}
    for cmd, fn in {"nest.new": nx.new_project, "nest.open": nx.open_project,
                    "nest.save": nx.save_project, "nest.save_as": lambda: nx.save_project(True),
                    "nest.import_part": nx.import_part, "nest.import_sheet": nx.add_std_sheet,
                    "nest.add_part": nx.add_part_from_selection, "nest.add_sheet": nx.add_sheet_from_selection,
                    "nest.params": nx.params_dialog, "nest.run": nx.run, "nest.simple": nx.run,
                    "nest.to_main": nx.to_main, "nest.export": nx.export_sheets,
                    "nest.show": nx.show}.items():
        ctx.register_command(cmd, fn, label=keys.get(cmd, cmd))
    menu = _sort_button_menu(window)
    ctx.register_command("sort.avoid", lambda on: sx.set_mode(window.cam_params.sort_mode, avoid_cut=bool(on)),
                         label="Avoid cut holes (rapids)", checkable=True)
    for cmd, key, fn in (("sort.manual_prev", "mv13", sx.previous_step),
                         ("sort.manual_end", "mv23", sx.finish_manual),
                         ("sort.first", "mv16", lambda: sx.move_selected(False)),
                         ("sort.last", "mv15", lambda: sx.move_selected(True))):
        ctx.register_command(cmd, fn, label=key)
    ctx.register_command("sort.layer_first", lambda: sx.layer_first(False))
    ctx.register_command("sort.layer_last", lambda: sx.layer_first(True))
    ctx.register_command("sort.manual", sx.toggle_manual)
    if menu is not None:
        have = set(menu.actions())
        for cmd in SORT_MENU:
            act = window.ribbon.actions.get(cmd)
            if act is not None and act not in have:
                menu.addAction(act)
    return nx, sx
