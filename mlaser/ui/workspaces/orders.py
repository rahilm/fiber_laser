"""Import & orders: the import review (DXF repair), part recognition before nesting, the Orders
tab of the Nest workspace (parts lists, multi-file import), the sheet material check and its
audited override.  Plugin ``orders`` (built in, listed in ``mlaser.ui.mainwindow.EXTENSIONS``).

The logic is Qt-free and tested without a display: :mod:`mlaser.services.repair`,
:mod:`mlaser.services.parts`, :mod:`mlaser.services.orders`,
:mod:`mlaser.services.material_check`.  This module holds the dialogs and the wiring:

* after File > Open / Import (hook ``document_imported``) the **Import review** lists the
  problems of the drawing (open contours with their gap, duplicates, tiny or zero-length
  geometry, self-intersections, unconverted text); *Preview* draws the gap closures on the
  canvas, *Fix all safe issues* applies them as **one undo step**.  It opens by itself when
  something is found (setting ``plugin.orders.review_after_import``) and from the pre-flight
  dialog (*Repair...* next to "44 open contours").  Command ``import.review``.
* **Recognised parts** (``parts.review``): the parts of the drawing with their counts, merge and
  split, then *Nest these* (Nest > *From drawing* uses it).
* **Orders** (a tab next to Nest): import a parts list (CSV, template ``orders.template``), add
  drawings (several files or a folder), set material/thickness, and load one material ×
  thickness group into the Nest workspace with quantities, priorities and the order references.
* the **material check**: a pre-flight contributor (``window.preflight_contributors``) and the
  ``JobService`` start guard (:class:`~mlaser.services.material_check.MaterialGate`); a
  mismatch blocks Start; the operator may override it with a confirmation, which is audited.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from mlaser.services import material_check as MC
from mlaser.services import orders as O
from mlaser.services import repair as R
from mlaser.services.config import Setting
from mlaser.services.parts import RecognitionRules, recognize

from . import TOUCH_MIN, show_workspace, tr

OVERRIDE_CODES = (MC.MISMATCH,)


def _dialogs(window: Any) -> bool:
    return not getattr(window, "_test_no_dialogs", False)


def parts_pixmap(parts, size: QSize, palette: QPalette, rings=None, tol: float = 1.0) -> QPixmap:
    """A thumbnail of model parts (Y up), optionally on a sheet outline (``rings``: lists of
    (x, y))."""
    pm = QPixmap(size)
    pm.fill(palette.color(QPalette.Base))
    path = QPainterPath()
    path.setFillRule(Qt.OddEvenFill)
    for p in parts:
        for c in p.contours:
            pts = list(c.points(tol))
            if len(pts) < 2:
                continue
            path.moveTo(pts[0][0], pts[0][1])
            for x, y in pts[1:]:
                path.lineTo(x, y)
            if c.closed:
                path.closeSubpath()
    sheet = QPainterPath()
    for ring in rings or ():
        try:
            pts = [(float(a), float(b)) for a, b in ring]
        except (TypeError, ValueError):
            continue
        if len(pts) >= 3:
            sheet.moveTo(*pts[0])
            for q in pts[1:]:
                sheet.lineTo(*q)
            sheet.closeSubpath()
    br = path.boundingRect().united(sheet.boundingRect()) if not sheet.isEmpty() else path.boundingRect()
    if br.width() <= 0 and br.height() <= 0:
        return pm
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = size.width() - 6, size.height() - 6
        s = min(w / max(br.width(), 1e-6), h / max(br.height(), 1e-6))
        p.translate(size.width() / 2 - br.center().x() * s, size.height() / 2 + br.center().y() * s)
        p.scale(s, -s)
        if not sheet.isEmpty():
            p.setPen(QPen(palette.color(QPalette.Mid), 0))
            p.setBrush(palette.color(QPalette.AlternateBase))
            p.drawPath(sheet)
        p.setPen(QPen(QColor("#1565c0"), 0))
        p.setBrush(QColor(21, 101, 192, 70))
        p.drawPath(path)
    finally:
        p.end()
    return pm


# ============================================================================== import review
class ImportReviewDialog(QDialog):
    """The import review / repair wizard (non-modal)."""

    MAX_ROWS = 1000                     # findings listed (the head line counts them all)

    def __init__(self, plugin: "OrdersPlugin") -> None:
        super().__init__(plugin.window)
        self.plugin = plugin
        w = plugin.window
        self.setObjectName("import_review")
        self.setWindowTitle(tr(w, "review.title", "Import review"))
        self.setModal(False)
        lay = QVBoxLayout(self)
        self.head = QLabel()
        self.head.setObjectName("import_review_head")
        self.head.setWordWrap(True)
        f = self.head.font()
        f.setBold(True)
        self.head.setFont(f)
        lay.addWidget(self.head)
        self.list = QListWidget()
        self.list.setObjectName("import_review_list")
        self.list.currentRowChanged.connect(self._zoom)
        lay.addWidget(self.list, 1)
        form = QFormLayout()
        self.join_tol = QDoubleSpinBox()
        self.join_tol.setRange(0.0, 5.0)
        self.join_tol.setDecimals(3)
        self.join_tol.setSuffix(" mm")
        self.close_gap = QDoubleSpinBox()
        self.close_gap.setRange(0.0, 50.0)
        self.close_gap.setDecimals(3)
        self.close_gap.setSuffix(" mm")
        form.addRow(tr(w, "review.join_tol", "Join open ends closer than"), self.join_tol)
        form.addRow(tr(w, "review.close_gap", "Close gaps up to"), self.close_gap)
        lay.addLayout(form)
        self.checks: dict[str, QCheckBox] = {}
        row = QHBoxLayout()
        for key, attr, text in (("open", "fix_open", "open contours"), ("dup", "fix_duplicates", "duplicates"),
                                ("tiny", "fix_tiny_objects", "tiny objects"),
                                ("seg", "fix_tiny_segments", "tiny segments"), ("text", "convert_text", "text")):
            cb = QCheckBox(tr(w, f"review.fix.{key}", text))
            cb.setChecked(True)
            self.checks[attr] = cb
            row.addWidget(cb)
        lay.addLayout(row)
        self.auto = QCheckBox(tr(w, "review.auto", "Review every drawing after import"))
        self.auto.setChecked(bool(plugin.settings["review_after_import"]))
        self.auto.toggled.connect(lambda on: plugin.settings.__setitem__("review_after_import", bool(on)))
        lay.addWidget(self.auto)
        self.result = QLabel()
        self.result.setObjectName("import_review_result")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        bb = QDialogButtonBox()
        self.preview_btn = bb.addButton(tr(w, "review.btn.preview", "Preview"), QDialogButtonBox.ActionRole)
        self.fix_btn = bb.addButton(tr(w, "review.btn.fix", "Fix all safe issues"), QDialogButtonBox.AcceptRole)
        self.fix_btn.setObjectName("import_review_fix")
        bb.addButton(QDialogButtonBox.Close)
        self.preview_btn.clicked.connect(self.preview)
        self.fix_btn.clicked.connect(self.fix)
        bb.rejected.connect(self.close)
        for b in (self.preview_btn, self.fix_btn):
            b.setMinimumHeight(TOUCH_MIN)
        lay.addWidget(bb)
        self.report: R.RepairReport = R.RepairReport()
        self.resize(640, 460)

    def options(self) -> R.RepairOptions:
        o = self.plugin.repair_options()
        o.join_tol, o.close_gap = self.join_tol.value(), self.close_gap.value()
        for attr, cb in self.checks.items():
            setattr(o, attr, cb.isChecked())
        o.fix_zero = o.fix_tiny_segments
        o.fix_overlaps = o.fix_duplicates
        return o

    def refresh(self) -> R.RepairReport:
        w = self.plugin.window
        o = self.plugin.repair_options()
        self.join_tol.setValue(o.join_tol)
        self.close_gap.setValue(o.close_gap)
        rep = R.analyze(w.document, self.options())
        self.report = rep
        self.list.clear()
        if rep.ok:
            self.head.setText(tr(w, "review.ok", "No problems found in {n} contours.", n=rep.contours))
        else:
            parts = [tr(w, f"repair.kind.{k}", R.KIND_TEXT[k]) + f": {n}" for k, n in rep.counts().items()]
            self.head.setText(tr(w, "review.found", "{n} problems in {c} contours ({fix} can be fixed): {what}",
                                 n=len(rep.findings), c=rep.contours, fix=rep.fixable, what=", ".join(parts)))
        for f in rep.findings[:self.MAX_ROWS]:
            it = QListWidgetItem(("✔ " if f.fixable else "⚠ ") + f.message)
            it.setData(Qt.UserRole, f.where)
            self.list.addItem(it)
        if len(rep.findings) > self.MAX_ROWS:
            self.list.addItem(tr(w, "review.more", "... and {n} more", n=len(rep.findings) - self.MAX_ROWS))
        self.fix_btn.setEnabled(rep.fixable > 0)
        return rep

    def _zoom(self, row: int) -> None:
        if row < 0:
            return
        where = self.list.item(row).data(Qt.UserRole)
        canvas = getattr(self.plugin.window, "canvas", None)
        if not where or canvas is None:
            return
        try:
            canvas.set_preview((), [tuple(where)], "#e53935")
            x, y = where
            canvas.fitInView(QRectF(x - 20, y - 20, 40, 40), Qt.KeepAspectRatio)
        except Exception:  # noqa: BLE001 - zoom is a convenience
            pass

    def preview(self) -> dict:
        w = self.plugin.window
        done, lines = R.preview(w.document, self.options())
        canvas = getattr(w, "canvas", None)
        if canvas is not None:
            marks = [f.where for f in self.report.findings if f.where is not None]
            canvas.set_preview(lines, marks, "#e6e600")
        self.result.setText(self.plugin.describe(done, preview=True))
        return done

    def fix(self) -> dict:
        done = self.plugin.apply_repair(self.options())
        self.result.setText(self.plugin.describe(done))
        self.refresh()
        return done

    def closeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        canvas = getattr(self.plugin.window, "canvas", None)
        if canvas is not None:
            canvas.clear_preview()
        super().closeEvent(ev)


# ============================================================================== recognised parts
class PartsDialog(QDialog):
    """Recognised parts with their counts: merge, split, rules; OK nests them."""

    def __init__(self, plugin: "OrdersPlugin", doc: Any, indices: Optional[list[int]] = None) -> None:
        super().__init__(plugin.window)
        self.plugin = plugin
        self.doc = doc
        self.indices = indices
        w = plugin.window
        self.setObjectName("parts_review")
        self.setWindowTitle(tr(w, "parts.title", "Recognised parts"))
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.mode = QComboBox()
        for m, text in (("nested", "Outline with holes"), ("layer", "One part per layer"),
                        ("block", "One part per block"), ("file", "Whole drawing as one part"),
                        ("none", "Every object a part")):
            self.mode.addItem(tr(w, f"parts.mode.{m}", text), m)
        self.mode.setCurrentIndex(max(0, self.mode.findData(plugin.rules().mode)))
        self.mode.currentIndexChanged.connect(lambda _i: self.recognize())
        top.addWidget(QLabel(tr(w, "parts.rule", "Parts by")))
        top.addWidget(self.mode, 1)
        lay.addLayout(top)
        self.head = QLabel()
        self.head.setObjectName("parts_review_head")
        lay.addWidget(self.head)
        self.list = QListWidget()
        self.list.setObjectName("parts_review_list")
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setIconSize(QSize(64, 48))
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        for key, text, fn in (("parts.btn.merge", "Merge", self.merge), ("parts.btn.split", "Split", self.split)):
            b = QPushButton(tr(w, key, text))
            b.setMinimumHeight(TOUCH_MIN)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr(w, "parts.btn.nest", "Nest these"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.partset = None
        self.recognize()
        self.resize(520, 480)

    def recognize(self) -> None:
        r = self.plugin.rules()
        r.mode = self.mode.currentData() or r.mode
        self.partset = recognize(self.doc, r, indices=self.indices)
        self.fill()

    def fill(self) -> None:
        w = self.plugin.window
        ps = self.partset
        self.list.clear()
        pal = self.palette()
        for g in ps.groups:
            pm = parts_pixmap([self.doc.parts[i] for i in g.members], QSize(64, 48), pal)
            self.list.addItem(QListWidgetItem(QIcon(pm), g.label()))
        self.head.setText(tr(w, "parts.summary", "{summary}", summary=ps.summary()))

    def selected(self) -> list[int]:
        return sorted(self.list.row(i) for i in self.list.selectedItems())

    def merge(self) -> None:
        rows = self.selected()
        if len(rows) >= 2:
            self.partset.merge(rows)
            self.fill()

    def split(self) -> None:
        for r in reversed(self.selected()):
            self.partset.split(r)
        self.fill()


# ============================================================================== orders tab
class OrdersWorkspace(QWidget):
    COLS = (("orders.col.file", "File"), ("orders.col.qty", "Qty"), ("orders.col.material", "Material"),
            ("orders.col.thickness", "Thickness"), ("orders.col.customer", "Customer"),
            ("orders.col.order", "Order"), ("orders.col.due", "Due"), ("orders.col.priority", "Priority"),
            ("orders.col.cut", "Cut"))
    EDIT = {1: "quantity", 2: "material", 3: "thickness_mm", 4: "customer", 5: "order", 6: "due", 7: "priority"}

    def __init__(self, plugin: "OrdersPlugin") -> None:
        super().__init__()
        self.plugin = plugin
        w = plugin.window
        self.setObjectName("orders_workspace")
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        bar = QHBoxLayout()
        for key, text, fn in (("orders.btn.csv", "Import parts list...", plugin.import_csv),
                              ("orders.btn.files", "Add drawings...", plugin.add_files),
                              ("orders.btn.template", "Template...", plugin.save_template),
                              ("orders.btn.remove", "Remove", self.remove)):
            b = QPushButton(tr(w, key, text))
            b.setMinimumHeight(TOUCH_MIN)
            b.clicked.connect(lambda _=False, f=fn: f())
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setObjectName("orders_table")
        self.table.setHorizontalHeaderLabels([tr(w, k, d) for k, d in self.COLS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._edited)
        lay.addWidget(self.table, 1)
        self.errors = QLabel()
        self.errors.setObjectName("orders_errors")
        self.errors.setWordWrap(True)
        self.errors.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.errors)
        nest = QHBoxLayout()
        self.group = QComboBox()
        self.group.setObjectName("orders_group")
        self.group.setMinimumHeight(TOUCH_MIN - 8)
        nest.addWidget(QLabel(tr(w, "orders.group", "Material × thickness")))
        nest.addWidget(self.group, 2)
        self.to_nest_btn = QPushButton(tr(w, "orders.btn.nest", "Load into Nest ▶"))
        self.to_nest_btn.setObjectName("orders_to_nest")
        self.to_nest_btn.setMinimumHeight(TOUCH_MIN + 8)
        self.to_nest_btn.clicked.connect(lambda _=False: plugin.to_nest())
        nest.addWidget(self.to_nest_btn, 1)
        lay.addLayout(nest)
        self.status = QLabel()
        self.status.setObjectName("orders_status")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self._sync = False
        self.refresh()

    def showEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().showEvent(ev)
        self.refresh()

    def refresh(self) -> None:
        book = self.plugin.book
        prog = self.plugin.progress()
        self._sync = True
        self.table.setRowCount(len(book.lines))
        for r, x in enumerate(book.lines):
            need = O.parts_needed(x)
            vals = [os.path.basename(x.file), str(x.quantity), x.material, f"{x.thickness_mm:g}" if x.thickness_mm else "",
                    x.customer, x.order, x.due, str(x.priority), f"{prog.get(x.id, 0)}/{need}"]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c not in self.EDIT:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                if c == 0:
                    it.setToolTip(x.file)
                    it.setData(Qt.UserRole, x.id)
                self.table.setItem(r, c, it)
        self._sync = False
        cur = self.group.currentData()
        self.group.blockSignals(True)
        self.group.clear()
        for key, lines in book.groups():
            first = lines[0]
            label = tr(self.plugin.window, "orders.group_item", "{material} {t} mm · {n} lines",
                       material=first.material or "?", t=f"{first.thickness_mm:g}", n=len(lines))
            self.group.addItem(label, key)
        i = self.group.findData(cur)
        self.group.setCurrentIndex(i if i >= 0 else 0)
        self.group.blockSignals(False)
        self.to_nest_btn.setEnabled(bool(book.lines))

    def _edited(self, it: QTableWidgetItem) -> None:
        if self._sync or it.column() not in self.EDIT:
            return
        book = self.plugin.book
        x = book.lines[it.row()]
        attr = self.EDIT[it.column()]
        txt = it.text().strip()
        try:
            if attr == "quantity":
                v: Any = int(txt)
                if v < 1:
                    raise ValueError
            elif attr == "thickness_mm":
                v = O._number(txt) if txt else 0.0
                if not 0.0 <= v <= 100.0:
                    raise ValueError
            elif attr == "priority":
                v = O._priority(txt)
            elif attr == "due":
                v = O._date(txt)
            else:
                v = " ".join(txt.split())
        except ValueError:
            self.refresh()
            return
        setattr(x, attr, v)
        book.changed()

    def selected_ids(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        out = []
        for r in rows:
            it = self.table.item(r, 0)
            if it is not None:
                out.append(it.data(Qt.UserRole))
        return out

    def remove(self) -> int:
        return self.plugin.book.remove(self.selected_ids())

    # drag and drop: drawings and parts lists
    def dragEnterEvent(self, ev) -> None:  # noqa: N802 - Qt API
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:  # noqa: N802 - Qt API
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        self.plugin.add_paths(paths)
        ev.acceptProposedAction()


# ============================================================================== plugin
class OrdersPlugin:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.window = w = ctx.window
        self.settings = ctx.settings_schema([
            Setting("review_after_import", "bool", True, label="orders.set.review_after_import"),
            Setting("join_tol", "float", 0.1, unit="mm", min=0.0, max=5.0, label="orders.set.join_tol"),
            Setting("close_gap", "float", 0.5, unit="mm", min=0.0, max=50.0, label="orders.set.close_gap"),
            Setting("recognition", "str", "nested", label="orders.set.recognition"),
            Setting("feature_layers", "str", "ETCH*,MARK*,ENGRAV*,SCRIBE*,TEXT*", label="orders.set.feature_layers"),
            Setting("ignore_layers", "str", "BEND*,DIM*,DEFPOINTS,CONSTRUCT*,CENTER*,HIDDEN*,TITLE*,FRAME*,BORDER*",
                    label="orders.set.ignore_layers"),
            Setting("islands", "str", "keep", label="orders.set.islands"),
        ])
        self.book = O.OrderBook(os.path.join(self._data_dir(), "orders.json"))
        if self.book.load_error:
            ctx.log(f"orders: {self.book.load_error} (the file was kept as orders.json.bad)")
        self.gate = MC.MaterialGate(lambda: self.window.document, user=self._user)
        jobs = getattr(ctx, "jobs", None)
        if jobs is not None and hasattr(jobs, "add_start_guard"):
            jobs.add_start_guard(self.gate.start_guard)
        contributors = getattr(w, "preflight_contributors", None)
        if contributors is None:
            contributors = []
            w.preflight_contributors = contributors
        contributors.append(self)
        self.overridable = set(OVERRIDE_CODES)
        self.review: Optional[ImportReviewDialog] = None
        self.last_csv: Optional[O.CsvResult] = None
        self.workspace = OrdersWorkspace(self)
        self.book.subscribe(self.workspace.refresh)
        ctx.add_workspace("orders", self.workspace, tr(w, "ws.orders", "Orders"), slot="nest")
        ctx.register_command("import.review", self.show_review, label="review.cmd")
        ctx.register_command("parts.review", self.review_parts, label="parts.cmd")
        ctx.register_command("orders.import_csv", self.import_csv, label="orders.cmd.csv")
        ctx.register_command("orders.add_files", self.add_files, label="orders.cmd.files")
        ctx.register_command("orders.template", self.save_template, label="orders.cmd.template")
        ctx.register_command("orders.to_nest", self.to_nest, label="orders.cmd.to_nest")
        # for plugins (a command is called without arguments): load one material x thickness
        # group, ``fn(key, replace=None) -> list`` of the nest parts added
        ctx.provide("orders.load_group", self.to_nest)
        ctx.register_command("workspace.orders", lambda: show_workspace(ctx, "orders", self.workspace,
                                                                          tr(w, "ws.orders", "Orders")),
                             label="ws.orders")
        ctx.provide("orders", lambda: self.book)
        ctx.provide("parts.rules", self.rules)
        ctx.provide("material_gate", lambda: self.gate)
        ctx.on("document_imported", self._imported)
        proj = w.extension("ext_project") if hasattr(w, "extension") else None
        proj = proj[0] if isinstance(proj, tuple) else proj
        if proj is not None and hasattr(proj, "register_section"):
            proj.register_section("orders", self.book.to_json, self.book.set_json)

    # ------------------------------------------------------------------ helpers
    def _data_dir(self) -> str:
        s = getattr(self.window, "settings", None)
        d = s.get("data_dir") if s is not None else None
        if not d:
            from mlaser.services.config import data_dir
            d = str(data_dir())
        return os.path.join(d, "production")

    def _user(self) -> str:
        hooks = getattr(self.window, "hooks", None)
        fn = hooks.provider("user_mode") if hooks is not None else None
        try:
            return str(fn()) if fn is not None else ""
        except Exception:  # noqa: BLE001
            return ""

    def rules(self) -> RecognitionRules:
        s = self.settings
        try:
            return RecognitionRules.from_dict({"mode": s["recognition"], "feature_layers": s["feature_layers"],
                                               "ignore_layers": s["ignore_layers"], "islands": s["islands"]})
        except ValueError:
            return RecognitionRules()

    def repair_options(self) -> R.RepairOptions:
        return R.RepairOptions(join_tol=float(self.settings["join_tol"]), close_gap=float(self.settings["close_gap"]))

    def nest_ws(self) -> Any:
        e = self.window.extension("nest") if hasattr(self.window, "extension") else None
        return e[0] if isinstance(e, tuple) else e

    def process_ext(self) -> Any:
        e = self.window.extension("ext_process") if hasattr(self.window, "extension") else None
        return e[0] if isinstance(e, tuple) else e

    def store(self) -> Any:
        p = self.process_ext()
        return getattr(p, "store", None)

    def describe(self, done: dict, preview: bool = False) -> str:
        w = self.window
        bits = [tr(w, f"repair.done.{k}", k.replace("_", " ") + ": {n}", n=v)
                for k, v in done.items() if v and k != "open_left"]
        head = tr(w, "review.would", "Would fix") if preview else tr(w, "review.fixed", "Fixed")
        text = f"{head}: {', '.join(bits) or tr(w, 'review.nothing', 'nothing')}"
        if done.get("open_left"):
            text += " · " + tr(w, "review.open_left", "{n} open contours remain (larger gaps: check the drawing)",
                               n=done["open_left"])
        return text

    # ------------------------------------------------------------------ import review
    def _imported(self, path: str, doc: Any) -> None:
        if not bool(self.settings["review_after_import"]):
            return
        rep = R.analyze(doc, self.repair_options())
        if rep.ok:
            return
        self.ctx.log(tr(self.window, "review.log", "Import review: {what} (command import.review)",
                        what=rep.summary()))
        if _dialogs(self.window):
            self.show_review()

    def show_review(self) -> ImportReviewDialog:
        if self.review is None:
            self.review = ImportReviewDialog(self)
        self.review.refresh()
        if _dialogs(self.window):
            self.review.show()
            self.review.raise_()
        return self.review

    def apply_repair(self, opts: Optional[R.RepairOptions] = None) -> dict:
        """Fix the open document with ``opts`` as one undo step."""
        o = opts or self.repair_options()
        out: dict = {}
        from ..safety_ui import job_locked
        if job_locked(self.window, "repair"):
            return out

        def fn(d):
            out.update(R.repair(d, o))
        self.ctx.edit(fn, tr(self.window, "review.undo", "repair drawing"))
        self.ctx.log(self.describe(out))
        return out

    # ------------------------------------------------------------------ recognised parts
    def recognize_drawing(self, indices: Optional[list[int]] = None, interactive: Optional[bool] = None) -> list:
        """NestParts of the drawing's recognised parts (the review dialog first when
        ``interactive``); [] when cancelled."""
        doc = self.window.document
        interactive = _dialogs(self.window) if interactive is None else interactive
        if interactive:
            d = PartsDialog(self, doc, indices)
            if not d.exec():
                return []
            ps = d.partset
        else:
            ps = recognize(doc, self.rules(), indices=indices)
        if ps.loose:
            self.ctx.log(tr(self.window, "parts.loose", "{n} open contours are in no part and were left out "
                            "(Import review closes small gaps)", n=len(ps.loose)))
        return ps.nest_parts(doc)

    def review_parts(self) -> int:
        ws = self.nest_ws()
        if ws is None:
            return 0
        return ws.add_from_drawing()

    # ------------------------------------------------------------------ orders
    def import_csv(self, path: Optional[str] = None) -> O.CsvResult:
        w = self.window
        if path is None:
            path, _ = QFileDialog.getOpenFileName(w, tr(w, "orders.cmd.csv", "Import parts list"), "",
                                                  "Parts list (*.csv *.txt);;All (*)")
        if not path:
            return O.CsvResult()
        try:
            res = O.parse_parts_csv(path)
        except OSError as exc:
            res = O.CsvResult(errors=[O.RowError(0, str(exc))])
        self.last_csv = res
        self.book.add(res.lines)
        msg = tr(w, "orders.msg.csv", "{file}: {summary}", file=os.path.basename(path), summary=res.summary())
        self.workspace.status.setText(msg)
        self.workspace.errors.setText("\n".join(tr(w, "orders.msg.row", "line {n}: {msg}", n=e.line, msg=e.message)
                                                for e in res.errors[:50]))
        self.ctx.log(msg)
        return res

    def add_files(self, paths: Optional[list[str]] = None) -> list:
        w = self.window
        if paths is None:
            paths, _ = QFileDialog.getOpenFileNames(w, tr(w, "orders.cmd.files", "Add drawings"), "",
                                                    "Drawings (*.dxf *.svg *.chf);;All (*)")
        if not paths:
            return []
        ws = self.nest_ws()
        mat = ws.material.text().strip() if ws is not None else ""
        th = float(ws.thickness.value()) if ws is not None else 0.0
        lines = self.book.add(O.lines_from_files(paths, material=mat, thickness_mm=th))
        self.workspace.status.setText(tr(w, "orders.msg.files", "{n} drawings added", n=len(lines)))
        return lines

    def add_paths(self, paths: list[str]) -> None:
        csvs = [p for p in paths if p.lower().endswith((".csv", ".txt"))]
        for p in csvs:
            self.import_csv(p)
        rest = [p for p in paths if p not in csvs]
        if rest:
            self.add_files(rest)

    def save_template(self, path: Optional[str] = None) -> str:
        w = self.window
        if path is None:
            path, _ = QFileDialog.getSaveFileName(w, tr(w, "orders.cmd.template", "Save the parts-list template"),
                                                  "parts_list.csv", "CSV (*.csv)")
        if not path:
            return ""
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(O.template_csv())
        self.workspace.status.setText(tr(w, "orders.msg.template", "Template saved: {path}", path=path))
        return path

    def progress(self) -> dict[str, int]:
        rep = self.window.extension("report") if hasattr(self.window, "extension") else None
        rep = rep[0] if isinstance(rep, tuple) else rep
        ledger = getattr(rep, "ledger", None)
        if ledger is None:
            return {}
        try:
            return O.order_progress(self.book.lines, ledger.jobs())
        except Exception:  # noqa: BLE001 - the report is information only
            return {}

    def resolve_material(self, material: str) -> str:
        """The technology database's name for ``material`` (case-insensitive, or the one name
        that contains it: "S235" -> "Mild steel S235"); ``material`` when there is none."""
        store = self.store()
        names = list(store.materials()) if store is not None else []
        m = " ".join(str(material or "").split())
        for n in names:
            if n.casefold() == m.casefold():
                return n
        hits = [n for n in names if m and m.casefold() in n.casefold()]
        return hits[0] if len(hits) == 1 else m

    def to_nest(self, key: Any = None, replace: Optional[bool] = None) -> list:
        """Load the order lines of one material × thickness group into the Nest workspace."""
        w = self.window
        ws = self.nest_ws()
        if ws is None:
            return []
        groups = dict(self.book.groups())
        key = key if key is not None else self.workspace.group.currentData()
        if key is None and groups:
            key = next(iter(groups))
        lines = groups.get(tuple(key) if key is not None else None, [])
        if not lines:
            return []
        parts, problems = O.nest_parts_for(lines, self.rules())
        if not parts:
            self.workspace.status.setText("\n".join(problems))
            return []
        job = ws.job
        if replace is None:
            replace = True
            if job.parts and _dialogs(w):
                ans = QMessageBox.question(w, tr(w, "orders.cmd.to_nest", "Load into Nest"),
                                           tr(w, "orders.ask.replace", "Replace the {n} parts in the Nest list?",
                                              n=len(job.parts)), QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
                if ans == QMessageBox.Cancel:
                    return []
                replace = ans == QMessageBox.Yes
        if replace:
            job.parts.clear()
            job.result = None
        for p in parts:
            job.add_part(p)
        first = lines[0]
        material = self.resolve_material(first.material)
        if material:
            ws.material.setText(material)
            ws._material_auto = False
        if first.thickness_mm:
            ws.thickness.setValue(first.thickness_mm)
        ws._mark_dirty()
        ws.refresh()
        self.use_technology(material, first.thickness_mm, parts)
        msg = tr(w, "orders.msg.nest", "{n} parts from {k} order lines loaded into Nest ({material} {t} mm)",
                 n=sum(p.quantity for p in parts), k=len(lines), material=material or "?",
                 t=f"{first.thickness_mm:g}")
        self.workspace.status.setText("\n".join([msg] + problems))
        self.ctx.log(msg)
        for p in problems:
            self.ctx.log(f"orders: {p}")
        show_workspace(self.ctx, "nest", ws, tr(w, "ws.nest", "Nest"))
        return parts

    def use_technology(self, material: str, thickness: float, nest_parts: Optional[list] = None,
                       interactive: Optional[bool] = None) -> Any:
        """Material and thickness are set in one place: pick the technology for them (the
        Process selection when it matches, the only match, or the operator's choice), apply its
        layer values to the layers the nest parts use (one undo step) and select it in the
        Process workspace."""
        from mlaser.model import LayerParams
        from mlaser.services import technology as T
        w = self.window
        store = self.store()
        if store is None or not material or not thickness:
            return None
        cands = [t for t in store.all() if MC.same_material(t.key.material, t.key.thickness_mm, material, thickness)]
        if not cands:
            self.ctx.log(tr(w, "orders.msg.no_tech", "No technology for {material} {t} mm in the database: "
                            "apply one in the Process workspace", material=material, t=f"{thickness:g}"))
            return None
        proc = self.process_ext()
        pws = getattr(proc, "workspace", None)
        picked = pws.picked() if pws is not None else None
        tech = next((t for t in cands if picked is not None and t.id == picked.id), None)
        if tech is None and len(cands) == 1:
            tech = cands[0]
        if tech is None:
            interactive = _dialogs(w) if interactive is None else interactive
            if not interactive:
                tech = cands[0]
            else:
                labels = [t.key.label() for t in cands]
                lab, ok = QInputDialog.getItem(w, tr(w, "orders.tech", "Technology"),
                                               tr(w, "orders.tech.pick", "Technology for {material} {t} mm:",
                                                  material=material, t=f"{thickness:g}"), labels, 0, False)
                if not ok:
                    return None
                tech = cands[labels.index(lab)]
        geo = [q for np_ in (nest_parts or []) for q in np_.parts]
        layers = sorted({q.craft.layer for q in geo}) or sorted({p.craft.layer for p in w.document.parts}) or [0]

        def fn(d):
            from ..canvas import layer_color
            for li in layers:
                lp = d.layers.get(li) or LayerParams(index=li, color=layer_color(li).name())
                d.layers[li] = T.apply_to_layer(lp, tech)
        self.ctx.edit(fn, tr(w, "orders.undo.tech", "technology {name}", name=tech.key.label()), parts=())
        # the layer values only: leads, kerf and micro-joints stay as the parts were drawn (a
        # lead added here could reach past the sheet edge the nest placed the part against)
        if pws is not None:
            try:
                pws.pick(tech.key)
                pws.refresh_layers()
            except Exception:  # noqa: BLE001 - the Process view is only told
                pass
        self.ctx.log(tr(w, "orders.msg.tech", "Technology {name} applied to layer(s) {layers}",
                        name=tech.key.label(), layers=", ".join(map(str, layers))))
        return tech

    # ------------------------------------------------------------------ pre-flight contributor
    def issues(self, action: str, doc: Any, emits: Any) -> list:
        if action not in ("start", "resume") or doc is None:
            return []
        issues = MC.check(doc)
        if self.gate.overridden(doc):           # confirmed (and audited) for this sheet already
            from dataclasses import replace
            from mlaser.machine.preflight import WARNING
            issues = [replace(i, severity=WARNING, message=f"overridden: {i.message}") if i.code == MC.MISMATCH else i
                      for i in issues]
        return issues

    def override(self, issues: list, parent: Any) -> bool:
        """Ask the operator to cut anyway (audited by the gate)."""
        w = self.window
        text = tr(w, "material.ask", "The sheet does not match the technology:\n\n{issues}\n\n"
                  "Cut it anyway? Only when you have checked the sheet and the technology. This is recorded.",
                  issues="\n".join(f"• {i.message}" for i in issues))
        box = QMessageBox(QMessageBox.Warning, tr(w, "material.title", "Material check"), text,
                          QMessageBox.Cancel, w)
        go = box.addButton(tr(w, "material.override", "Override and cut"), QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is not go:
            return False
        return self.gate.override("operator confirmed in the pre-flight dialog")

    def actions(self, rep: Any) -> list:
        if "open_contour" in rep.codes():
            return [(tr(self.window, "review.btn.repair", "Repair..."), self.show_review)]
        return []


def register(ctx: Any) -> OrdersPlugin:
    return OrdersPlugin(ctx)


__all__ = ["register", "OrdersPlugin", "OrdersWorkspace", "ImportReviewDialog", "PartsDialog", "parts_pixmap"]
