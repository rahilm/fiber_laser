"""The "Onshape" workspace (PySide6).  All network work runs through ``ctx.run_in_background``;
progress comes back through a queue that a timer on the GUI thread reads."""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame,
                               QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QProgressBar, QPushButton, QRadioButton, QScrollArea,
                               QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from . import pipeline as PL
from .bom import MachineConfig
from .materials import label_thickness, suggest

FILTERS = (("Recent", 5), ("My documents", 0), ("Created by me", 1), ("Shared with me", 2), ("Company", 7),
           ("Public", 4))
COLS = ("Name", "P/N", "Material", "Sheet material", "t faces", "t volume", "t name", "BOM qty", "Made",
        "To cut", "Processes", "DXF face", "Status", "Notes")
C_NAME, C_PN, C_MAT, C_SHEET, C_TP, C_TV, C_TL, C_QTY, C_MADE, C_CUT, C_PROC, C_FACE, C_STATUS, C_NOTES = range(len(COLS))


def _fit_columns(table: Any, priorities: dict, key: int) -> None:
    """The host's responsive columns (no horizontal scroll bar: the less important columns hide
    while the table is narrow), when the host offers them (mlaser 2026-10 and later)."""
    try:
        from mlaser.ui.responsive import ColumnFitter
    except ImportError:          # an older host: the table scrolls as before
        return
    ColumnFitter(table, priorities, stretch=key)


def _mm(v: Optional[float]) -> str:
    return "" if v is None else f"{v:.2f}"


class OnshapeWorkspace(QWidget):
    def __init__(self, service: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.ctx = service.ctx
        self.progress_q: "queue.Queue[tuple[int, int, str]]" = queue.Queue()
        self.busy = False
        self.parent_stack: list[tuple[str, str]] = []      # (kind, id) of folders / projects entered
        self.doc: dict = {}
        self.wvm: tuple[str, str] = ("", "")
        self.assembly: dict = {}
        self.handle: Any = None
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        self._load_settings()

    # ================================================================== layout
    def _build(self) -> None:
        root = QHBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._browse_tab(), "Browse")
        self.tabs.addTab(self._machine_tab(), "Machine")
        self.tabs.addTab(self._materials_tab(), "Materials")
        self.tabs.addTab(self._connection_tab(), "Connection")
        split.addWidget(self.tabs)
        right = QWidget()
        rl = QVBoxLayout(right)
        self.title = QLabel("No assembly loaded")
        self.title.setWordWrap(True)
        rl.addWidget(self.title)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        _fit_columns(self.table, {C_PN: 3, C_MAT: 2, C_SHEET: 4, C_TP: 0, C_TV: 0, C_TL: 1, C_QTY: 5, C_MADE: 4,
                                  C_CUT: 6, C_PROC: 1, C_FACE: 0, C_STATUS: 5, C_NOTES: -1}, C_NAME)
        self.table.itemChanged.connect(self._item_changed)
        rl.addWidget(self.table, 3)
        bar = QHBoxLayout()
        self.b_sel = QPushButton("Select the parts for this machine")
        self.b_sel.clicked.connect(self.select_for_machine)
        self.b_face = QPushButton("DXF face…")
        self.b_face.clicked.connect(self.choose_face)
        self.b_go = QPushButton("Download DXFs && create orders")
        self.b_go.clicked.connect(self.prepare_selected)
        self.b_orders = QPushButton("Create orders for ready parts")
        self.b_orders.clicked.connect(self.create_orders)
        self.b_cancel = QPushButton("Cancel")
        self.b_cancel.setEnabled(False)
        self.b_cancel.clicked.connect(self.cancel)
        # two rows, so the panel fits a 1280 px window next to the tabs
        bar.addWidget(self.b_sel)
        bar.addWidget(self.b_face)
        bar.addStretch(1)
        bar.addWidget(self.b_cancel)
        rl.addLayout(bar)
        bar2 = QHBoxLayout()
        bar2.addWidget(self.b_go)
        bar2.addWidget(self.b_orders)
        bar2.addStretch(1)
        rl.addLayout(bar2)
        pl = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.calls = QLabel("API calls: 0")
        pl.addWidget(self.progress, 1)
        pl.addWidget(self.calls)
        rl.addLayout(pl)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        rl.addWidget(self.status)
        # sheet groups
        sheet = QHBoxLayout()
        sheet.addWidget(QLabel("Sheet"))
        self.sheet_w = QDoubleSpinBox()
        self.sheet_h = QDoubleSpinBox()
        for s in (self.sheet_w, self.sheet_h):
            s.setRange(10.0, 20000.0)
            s.setDecimals(1)
            s.setSuffix(" mm")
            s.valueChanged.connect(self._sheet_changed)
        sheet.addWidget(self.sheet_w)
        sheet.addWidget(QLabel("x"))
        sheet.addWidget(self.sheet_h)
        sheet.addStretch(1)
        rl.addLayout(sheet)
        self.cards_box = QWidget()
        self.cards = QHBoxLayout(self.cards_box)
        self.cards.addStretch(1)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self.cards_box)
        sc.setMinimumHeight(150)
        rl.addWidget(sc, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([420, 900])

    def _browse_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search documents")
        self.search.returnPressed.connect(self.reload_documents)
        self.filter = QComboBox()
        for label, val in FILTERS:
            self.filter.addItem(label, val)
        self.filter.currentIndexChanged.connect(lambda i: self.reload_documents())
        b = QPushButton("Search")
        b.clicked.connect(self.reload_documents)
        row.addWidget(self.search, 1)
        row.addWidget(self.filter)
        row.addWidget(b)
        lay.addLayout(row)
        nav = QHBoxLayout()
        self.b_up = QPushButton("Up")
        self.b_up.setEnabled(False)
        self.b_up.clicked.connect(self.go_up)
        self.b_more = QPushButton("More")
        self.b_more.setEnabled(False)
        self.b_more.clicked.connect(self.more_documents)
        nav.addWidget(self.b_up)
        nav.addWidget(self.b_more)
        nav.addStretch(1)
        lay.addLayout(nav)
        self.docs = QListWidget()
        self.docs.itemActivated.connect(self._doc_activated)
        self.docs.itemClicked.connect(self._doc_activated)
        lay.addWidget(QLabel("Documents, folders and projects"))
        lay.addWidget(self.docs, 2)
        self.wvm_box = QComboBox()
        self.wvm_box.currentIndexChanged.connect(self._wvm_changed)
        lay.addWidget(QLabel("Workspace or version"))
        lay.addWidget(self.wvm_box)
        self.asms = QListWidget()
        self.asms.itemActivated.connect(self._asm_activated)
        lay.addWidget(QLabel("Assemblies"))
        lay.addWidget(self.asms, 1)
        self.b_bom = QPushButton("Load bill of materials")
        self.b_bom.clicked.connect(lambda: self._asm_activated(self.asms.currentItem()))
        lay.addWidget(self.b_bom)
        self.bom_info = QLabel("")
        self.bom_info.setWordWrap(True)
        lay.addWidget(self.bom_info)
        self._next_offset = 0
        return w

    def _machine_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.m_process = QRadioButton("By a process property")
        self.m_sheet = QRadioButton("Every sheet-like part (no process property)")
        self.m_process.setChecked(True)
        f.addRow(self.m_process)
        f.addRow(self.m_sheet)
        self.values = QLineEdit()
        self.values.setPlaceholderText("Laser, Fiber laser")
        self.values.setToolTip("Values of the process property that mean this machine (comma separated, any case)")
        pick = QPushButton("Pick from the BOM…")
        pick.clicked.connect(self.pick_values)
        row = QHBoxLayout()
        row.addWidget(self.values, 1)
        row.addWidget(pick)
        f.addRow("This machine is", row)
        self.slot_box = QWidget()
        self.slot_lay = QHBoxLayout(self.slot_box)
        self.slot_lay.setContentsMargins(0, 0, 0, 0)
        self.slot_checks: dict[str, QCheckBox] = {}
        f.addRow("Properties that count", self.slot_box)
        self.after = QCheckBox("Include parts whose laser step comes after another process")
        f.addRow(self.after)
        note = QLabel("Onshape does not know which steps are done: such a part is either always or never "
                      "included.  Enter what was already made in the parts table (Made).")
        note.setWordWrap(True)
        f.addRow(note)
        self.robots = QSpinBox()
        self.robots.setRange(1, 100)
        self.robots.setToolTip("Robots (or assemblies) to build: the BOM quantity is multiplied by this")
        f.addRow("Robots", self.robots)
        self.spares = QSpinBox()
        self.spares.setRange(0, 1000)
        f.addRow("Spares per part", self.spares)
        self.ratio = QDoubleSpinBox()
        self.ratio.setRange(1.0, 100.0)
        self.ratio.setToolTip("Sheet mode: a part is sheet when its smaller side is at least this many thicknesses")
        f.addRow("Sheet: width ≥ thickness ×", self.ratio)
        self.max_t = QDoubleSpinBox()
        self.max_t.setRange(0.1, 100.0)
        self.max_t.setSuffix(" mm")
        f.addRow("Sheet: up to", self.max_t)
        apply = QPushButton("Apply")
        apply.clicked.connect(self.apply_machine)
        f.addRow(apply)
        return w

    def _materials_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Onshape material -> technology database material (the sheet material of Orders and Nest)"))
        self.mat_table = QTableWidget(0, 3)
        self.mat_table.setHorizontalHeaderLabels(("Onshape material", "Thickness in the name", "Technology material"))
        self.mat_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.mat_table, 1)
        return w

    def _connection_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.server = QLineEdit()
        f.addRow("Onshape server", self.server)
        self.access = QLineEdit()
        self.access.setPlaceholderText("access key (saved in the secret store)")
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.Password)
        self.secret.setPlaceholderText("secret key")
        f.addRow("Access key", self.access)
        f.addRow("Secret key", self.secret)
        row = QHBoxLayout()
        save = QPushButton("Save")
        save.clicked.connect(self.save_connection)
        test = QPushButton("Test connection")
        test.clicked.connect(self.test_connection)
        forget = QPushButton("Forget the keys")
        forget.clicked.connect(self.forget_keys)
        for b in (save, test, forget):
            row.addWidget(b)
        f.addRow(row)
        self.conn = QLabel("")
        self.conn.setWordWrap(True)
        f.addRow(self.conn)
        self.props = QLineEdit()
        self.props.setToolTip("The custom properties that name a part's processes, in the order the steps happen")
        f.addRow("Process properties", self.props)
        self.customer = QLineEdit()
        self.customer.setPlaceholderText("empty: the document name")
        f.addRow("Customer / team", self.customer)
        self.tol = QDoubleSpinBox()
        self.tol.setRange(0.0, 5.0)
        self.tol.setDecimals(2)
        self.tol.setSuffix(" mm")
        f.addRow("Thickness tolerance", self.tol)
        self.xcheck = QCheckBox("Cross-check the thickness with mass properties (one more call per part)")
        f.addRow(self.xcheck)
        clear = QPushButton("Clear the download cache")
        clear.clicked.connect(self.clear_cache)
        f.addRow(clear)
        help_ = QLabel("Create API keys in Onshape's Developer Portal (dev-portal.onshape.com > API keys) with "
                       "read access.  Calls count against that account's daily Onshape limit; answers are "
                       "cached by document version.")
        help_.setWordWrap(True)
        f.addRow(help_)
        return w

    # ================================================================== settings
    def _load_settings(self) -> None:
        s = self.service
        self.server.setText(str(s.get("server")))
        self.props.setText(", ".join(s.process_props))
        self.customer.setText(str(s.get("customer") or ""))
        self.tol.setValue(float(s.get("thickness_tol")))
        self.xcheck.setChecked(bool(s.get("cross_check_volume")))
        self.sheet_w.blockSignals(True)
        self.sheet_h.blockSignals(True)
        self.sheet_w.setValue(float(s.get("sheet_w")))
        self.sheet_h.setValue(float(s.get("sheet_h")))
        self.sheet_w.blockSignals(False)
        self.sheet_h.blockSignals(False)
        if s.keys():
            self.access.setPlaceholderText("saved (enter a new key to replace it)")
            self.secret.setPlaceholderText("saved")
        self._load_machine()

    def _load_machine(self) -> None:
        m = self.service.machine
        self.m_sheet.setChecked(m.mode == "sheet")
        self.m_process.setChecked(m.mode != "sheet")
        self.values.setText(", ".join(m.values))
        for cb in self.slot_checks.values():
            self.slot_lay.removeWidget(cb)
            cb.deleteLater()
        self.slot_checks = {}
        for p in self.service.process_props:
            cb = QCheckBox(p)
            cb.setChecked(p in m.slots)
            self.slot_lay.addWidget(cb)
            self.slot_checks[p] = cb
        self.after.setChecked(m.include_after_other)
        self.robots.setValue(m.robots)
        self.spares.setValue(m.spares)
        self.ratio.setValue(m.sheet_ratio)
        self.max_t.setValue(m.max_sheet_mm)

    def apply_machine(self) -> MachineConfig:
        m = MachineConfig(mode="sheet" if self.m_sheet.isChecked() else "process",
                          values=[v.strip() for v in self.values.text().split(",") if v.strip()],
                          slots=[p for p, cb in self.slot_checks.items() if cb.isChecked()],
                          include_after_other=self.after.isChecked(), robots=self.robots.value(),
                          spares=self.spares.value(), sheet_ratio=self.ratio.value(), max_sheet_mm=self.max_t.value())
        self.service.machine = m
        self._reclassify()
        return m

    def save_connection(self) -> None:
        try:
            self.server.setText(self.service.set_server(self.server.text()))
        except ValueError as exc:
            self.conn.setText(str(exc))
            return
        if self.access.text().strip() or self.secret.text().strip():
            self.service.save_keys(self.access.text(), self.secret.text())
            self.access.clear()
            self.secret.clear()
            self.access.setPlaceholderText("saved (enter a new key to replace it)")
            self.secret.setPlaceholderText("saved")
        props = [p.strip() for p in self.props.text().split(",") if p.strip()]
        if props != self.service.process_props:
            self.service.set("process_props", props)
            m = self.service.machine
            m.slots = [s for s in m.slots if s in props] or [p for p in props if "pre" not in p.lower()]
            self.service.machine = m
            self._load_machine()
        self.service.set("customer", self.customer.text().strip())
        self.service.set("thickness_tol", self.tol.value())
        self.service.set("cross_check_volume", self.xcheck.isChecked())
        self.conn.setText("Saved.")

    def forget_keys(self) -> None:
        self.service.forget_keys()
        self.access.setPlaceholderText("access key (saved in the secret store)")
        self.secret.setPlaceholderText("secret key")
        self.conn.setText("The keys were removed from the secret store.")

    def clear_cache(self) -> None:
        try:
            self.service.client().clear_cache()
            self.conn.setText("Cache cleared.")
        except Exception as exc:  # noqa: BLE001 - shown
            self.conn.setText(str(exc))

    def _sheet_changed(self) -> None:
        self.service.set("sheet_w", self.sheet_w.value())
        self.service.set("sheet_h", self.sheet_h.value())
        self.refresh_groups()

    # ================================================================== background
    def _bg(self, fn: Callable[[], Any], done: Callable[[Any], None], what: str) -> Any:
        self.status.setText(what + "…")

        def ok(r: Any) -> None:
            self._update_calls()
            done(r)

        def err(exc: BaseException) -> None:
            self._update_calls()
            self._finish_busy()
            self.status.setText(f"{what}: {exc}")
            self.ctx.show_message(f"Onshape: {exc}", "error") if hasattr(self.ctx, "show_message") else None

        return self.ctx.run_in_background(fn, on_done=ok, on_error=err)

    def _update_calls(self) -> None:
        self.calls.setText(f"API calls: {self.service.calls}")

    def _poll(self) -> None:
        last = None
        while True:
            try:
                last = self.progress_q.get_nowait()
            except queue.Empty:
                break
        if last is not None:
            i, n, msg = last
            self.progress.setRange(0, max(1, n))
            self.progress.setValue(i)
            self.status.setText(f"{i}/{n} {msg}")
        if self.busy:
            self._update_calls()

    def test_connection(self) -> None:
        self.save_connection()
        self._bg(self.service.test_connection, lambda name: self.conn.setText(f"Connected as {name}."),
                 "Testing the connection")

    # ================================================================== browse
    def _parent(self) -> tuple[str, str]:
        return self.parent_stack[-1] if self.parent_stack else ("", "")

    def reload_documents(self) -> None:
        self._next_offset = 0
        self.docs.clear()
        self._fetch_documents()

    def more_documents(self) -> None:
        self._fetch_documents()

    def _fetch_documents(self) -> None:
        kind, pid = self._parent()
        q, flt, off = self.search.text().strip(), self.filter.currentData(), self._next_offset
        if pid:
            flt = None

        def fn() -> dict:
            return self.service.documents(q, flt, parent=pid if kind == "folder" else "",
                                          project=pid if kind == "project" else "", offset=off)

        self._bg(fn, self._show_documents, "Listing documents")

    def _show_documents(self, page: dict) -> None:
        items = (page or {}).get("items") or []
        for it in items:
            kind = str(it.get("jsonType") or "")
            container = bool(it.get("isContainer")) or kind in ("folder", "project")
            prefix = {"folder": "[folder] ", "project": "[project] "}.get(kind, "[folder] " if container else "")
            li = QListWidgetItem(prefix + str(it.get("name", it.get("id"))))
            li.setData(Qt.UserRole, it)
            self.docs.addItem(li)
        self._next_offset += len(items)
        self.b_more.setEnabled(bool((page or {}).get("next")))
        self.b_up.setEnabled(bool(self.parent_stack))
        self.status.setText(f"{self.docs.count()} item(s)")

    def go_up(self) -> None:
        if self.parent_stack:
            self.parent_stack.pop()
        self.reload_documents()

    def _doc_activated(self, item: Optional[QListWidgetItem]) -> None:
        if item is None or self.busy:
            return
        it = item.data(Qt.UserRole) or {}
        kind = str(it.get("jsonType") or "")
        if it.get("isContainer") or kind in ("folder", "project"):
            self.parent_stack.append(("project" if kind == "project" else "folder", str(it.get("id"))))
            self.reload_documents()
            return
        self.doc = it
        did = str(it.get("id"))
        default = str((it.get("defaultWorkspace") or {}).get("id") or "")
        self.service.remember(did, str(it.get("name", did)))
        self._bg(lambda: self.service.workspace_choices(did, default), self._show_wvm, "Reading workspaces")

    def _show_wvm(self, choices: list) -> None:
        self.wvm_box.blockSignals(True)
        self.wvm_box.clear()
        for label, wvm, i in choices:
            self.wvm_box.addItem(label, (wvm, i))
        self.wvm_box.blockSignals(False)
        if choices:
            self._wvm_changed(0)

    def _wvm_changed(self, index: int) -> None:
        data = self.wvm_box.itemData(index)
        if not data or not self.doc:
            return
        self.wvm = tuple(data)
        did = str(self.doc.get("id"))
        self._bg(lambda: self.service.assemblies(did, data[0], data[1]), self._show_assemblies, "Reading assemblies")

    def _show_assemblies(self, els: list) -> None:
        self.asms.clear()
        for e in els:
            li = QListWidgetItem(str(e.get("name", e.get("id"))))
            li.setData(Qt.UserRole, e)
            self.asms.addItem(li)
        if els:
            self.asms.setCurrentRow(0)
        self.status.setText(f"{len(els)} assembl{'y' if len(els) == 1 else 'ies'}")

    def _asm_activated(self, item: Optional[QListWidgetItem]) -> None:
        if item is None or not self.doc or self.busy:
            return
        e = item.data(Qt.UserRole) or {}
        self.assembly = e
        did, (wvm, wid) = str(self.doc.get("id")), self.wvm
        dname, aname = str(self.doc.get("name", did)), str(e.get("name", e.get("id")))
        self._bg(lambda: self.service.load_bom(did, wvm, wid, str(e.get("id")), aname, dname), self._show_bom,
                 "Reading the bill of materials")

    def _show_bom(self, res: Any) -> None:
        self.title.setText(f"{self.doc.get('name', '')} / {self.assembly.get('name', '')} — {self.wvm_box.currentText()}")
        found = ", ".join(f"{k}: {v}" for k, v in res.columns.items())
        txt = f"{len(res.parts)} part rows. Columns: {found}."
        if res.missing:
            txt += f" No column for {', '.join(res.missing)} (check the property names in Connection)."
        if res.skipped:
            txt += f" {len(res.skipped)} row(s) skipped (sub-assemblies, standard content)."
        self.bom_info.setText(txt)
        self._reclassify()
        self.select_for_machine()

    # ================================================================== table
    def _reclassify(self) -> None:
        if self.service.bom is None:
            return
        fresh = {p.part.key: p for p in self.service.classify(self.service.bom.parts)}
        for k, p in fresh.items():
            old = self.service.plans.get(k)
            if old is not None and old.status not in (PL.PENDING, PL.NOT_MACHINE, PL.NOTHING, PL.CANCELLED) \
                    and p.status == PL.PENDING:
                old.to_cut, old.made = p.to_cut, p.made
                fresh[k] = old
        self.service.plans = fresh
        self.refresh_table()

    def plans(self) -> list[PL.PartPlan]:
        if self.service.bom is None:
            return []
        return [self.service.plans[p.key] for p in self.service.bom.parts if p.key in self.service.plans]

    def refresh_table(self) -> None:
        plans = self.plans()
        checked = self.checked_keys()
        self.table.blockSignals(True)
        self.table.setRowCount(len(plans))
        for r, p in enumerate(plans):
            pt = p.part
            procs = "; ".join(f"{k}: {v}" for k, v in pt.processes.items() if v)
            cells = [pt.name, pt.part_number, p.label or pt.material, p.sheet_material, _mm(p.t_plane), _mm(p.t_volume),
                     _mm(p.t_label if p.t_label is not None else label_thickness(pt.material)), str(pt.quantity),
                     str(p.made), str(p.to_cut), procs, p.face_label, p.status,
                     "; ".join([p.reason] + p.notes if p.reason and p.status != PL.READY else p.notes)]
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if c != C_MADE:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                if c == C_NAME:
                    it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                    it.setCheckState(Qt.Checked if pt.key in checked else Qt.Unchecked)
                    it.setData(Qt.UserRole, pt.key)
                self.table.setItem(r, c, it)
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        self.refresh_materials()
        self.refresh_groups()

    def checked_keys(self) -> set[str]:
        out = set()
        for r in range(self.table.rowCount()):
            it = self.table.item(r, C_NAME)
            if it is not None and it.checkState() == Qt.Checked:
                out.add(it.data(Qt.UserRole))
        return out

    def set_checked(self, keys: set[str]) -> None:
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            it = self.table.item(r, C_NAME)
            if it is not None:
                it.setCheckState(Qt.Checked if it.data(Qt.UserRole) in keys else Qt.Unchecked)
        self.table.blockSignals(False)

    def select_for_machine(self) -> None:
        self.set_checked({p.part.key for p in self.plans() if p.status not in (PL.NOT_MACHINE, PL.NOTHING)})

    def _item_changed(self, it: QTableWidgetItem) -> None:
        if it.column() != C_MADE:
            return
        key = self.table.item(it.row(), C_NAME).data(Qt.UserRole)
        try:
            n = max(0, int(it.text().strip() or 0))
        except ValueError:
            n = 0
        self.service.set_made(key, n)
        self._reclassify()

    def selected_plan(self) -> Optional[PL.PartPlan]:
        r = self.table.currentRow()
        if r < 0 or self.table.item(r, C_NAME) is None:
            return None
        return self.service.plans.get(self.table.item(r, C_NAME).data(Qt.UserRole))

    def choose_face(self, pick: Optional[str] = None) -> Optional[str]:
        """Pick the DXF face of the current part (``pick``: a face id or "" for automatic, for
        tests); the choice is saved for this part and used by the next download."""
        p = self.selected_plan()
        if p is None:
            self.status.setText("Select a part first.")
            return None
        if not p.faces:
            self.status.setText("Download the part first: its flat faces are listed after that.")
            return None
        if pick is None:
            labels = ["Automatic (largest flat side)"] + [lab for _, lab in p.faces]
            lab, ok = QInputDialog.getItem(self, "DXF face", f"Face of {p.part.label} to flatten:", labels, 0, False)
            if not ok:
                return None
            pick = "" if labels.index(lab) == 0 else p.faces[labels.index(lab) - 1][0]
        self.service.set_face(p.part.key, pick)
        self.status.setText(f"{p.part.label}: face {'automatic' if not pick else pick} saved; download again to apply.")
        return pick

    def pick_values(self) -> None:
        bom = self.service.bom
        vals = sorted({v for vs in (bom.process_values.values() if bom else []) for v in vs}, key=str.casefold)
        if not vals:
            self.status.setText("Load a bill of materials first: its process values are listed here.")
            return
        cur = {v.strip().casefold() for v in self.values.text().split(",")}
        menu = QMenu(self)
        acts = []
        for v in vals:
            a = menu.addAction(v)
            a.setCheckable(True)
            a.setChecked(v.casefold() in cur)
            acts.append(a)
        menu.addSeparator()
        ok = menu.addAction("Use the checked values")
        chosen = menu.exec(self.mapToGlobal(self.values.geometry().bottomLeft()))
        if chosen is ok or chosen in acts:
            self.values.setText(", ".join(a.text() for a in acts if a.isChecked()))

    # ================================================================== prepare / orders
    def _finish_busy(self) -> None:
        self.busy = False
        self.b_go.setEnabled(True)
        self.b_cancel.setEnabled(False)

    def prepare_selected(self) -> None:
        if self.busy:
            return
        keys = self.checked_keys()
        parts = [p for p in (self.service.bom.parts if self.service.bom else []) if p.key in keys]
        if not parts:
            self.status.setText("Tick the parts to download first.")
            return
        self.busy = True
        self.b_go.setEnabled(False)
        self.b_cancel.setEnabled(True)
        self.progress.setRange(0, len(parts))
        self.progress.setValue(0)
        cancel = threading.Event()
        self._cancel_event = cancel

        def fn() -> list:
            return self.service.prepare(parts, lambda i, n, m: self.progress_q.put((i, n, m)), cancel)

        def done(plans: list) -> None:
            self._finish_busy()
            self._poll()
            self.refresh_table()
            self.set_checked(keys)
            if cancel.is_set():
                self.status.setText("Cancelled; the parts done so far are kept.")
                return
            self.create_orders(plans)

        self.handle = self._bg(fn, done, "Downloading")

    def cancel(self) -> None:
        ev = getattr(self, "_cancel_event", None)
        if ev is not None:
            ev.set()
        if self.handle is not None and hasattr(self.handle, "cancel"):
            self.handle.cancel()
        self.status.setText("Cancelling after the parts in flight…")

    def create_orders(self, plans: Optional[list] = None) -> tuple[int, int]:
        keys = self.checked_keys()
        ps = plans if plans else [p for p in self.plans() if p.part.key in keys]
        try:
            added, replaced = self.service.create_orders(ps)
        except Exception as exc:  # noqa: BLE001 - shown
            self.status.setText(str(exc))
            return (0, 0)
        counts: dict[str, int] = {}
        for p in ps:
            counts[p.status] = counts.get(p.status, 0) + 1
        summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        msg = f"{added} order line(s) added ({replaced} replaced from an earlier run). Parts: {summary}."
        if any(p.status == PL.NO_MATERIAL for p in ps):
            msg += " Map the materials in the Materials tab, then Create orders again."
        self.status.setText(msg)
        if hasattr(self.ctx, "show_message"):
            self.ctx.show_message(f"Onshape: {added} order line(s) added")
        self.refresh_groups()
        return added, replaced

    # ================================================================== materials
    def refresh_materials(self) -> None:
        labels: dict[str, PL.PartPlan] = {}
        for p in self.plans():
            lab = p.label or p.part.material
            if lab and p.status not in (PL.NOT_MACHINE,):
                labels.setdefault(lab, p)
        names = self.service.tech_names()
        mm = self.service.material_map
        self.mat_table.setRowCount(len(labels))
        for r, (lab, p) in enumerate(sorted(labels.items(), key=lambda x: x[0].casefold())):
            self.mat_table.setItem(r, 0, QTableWidgetItem(lab))
            t = label_thickness(lab)
            self.mat_table.setItem(r, 1, QTableWidgetItem("" if t is None else f"{t:g} mm"))
            box = QComboBox()
            box.setEditable(True)
            box.addItem("")
            for s in suggest(lab, names):
                box.addItem(s)
            for nm in names:
                if box.findText(nm) < 0:
                    box.addItem(nm)
            cur = mm.get(lab) or (p.sheet_material if p.material_how == "same name" else "")
            box.setCurrentText(cur)
            if not cur and box.count() > 1:
                box.setToolTip("Suggestions first: " + ", ".join(suggest(lab, names)))
            box.currentTextChanged.connect(lambda text, lab=lab: self._map(lab, text))
            self.mat_table.setCellWidget(r, 2, box)

    def _map(self, label: str, material: str) -> None:
        self.service.map_material(label, material.strip())
        self.refresh_table_only()

    def refresh_table_only(self) -> None:
        for r in range(self.table.rowCount()):
            key = self.table.item(r, C_NAME).data(Qt.UserRole)
            p = self.service.plans.get(key)
            if p is not None:
                self.table.item(r, C_SHEET).setText(p.sheet_material)
                self.table.item(r, C_STATUS).setText(p.status)
        self.refresh_groups()

    # ================================================================== groups
    def refresh_groups(self) -> None:
        while self.cards.count() > 1:
            w = self.cards.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        groups = self.service.groups(self.plans())
        for g in groups:
            self.cards.insertWidget(self.cards.count() - 1, self._card(g))

    def _card(self, g: PL.GroupSummary) -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.StyledPanel)
        f.setObjectName("onshape_group")
        lay = QGridLayout(f)
        lay.addWidget(QLabel(f"<b>{g.title}</b>"), 0, 0, 1, 2)
        lay.addWidget(QLabel(f"{g.parts} part(s), {g.quantity} to cut"), 1, 0, 1, 2)
        lay.addWidget(QLabel(f"area {g.net_area_mm2 / 1e6:.3f} m² (boxes {g.box_area_mm2 / 1e6:.3f} m²)"), 2, 0, 1, 2)
        lay.addWidget(QLabel(f"about {g.sheets} sheet(s) of {self.sheet_w.value():g} x {self.sheet_h.value():g} mm"),
                      3, 0, 1, 2)
        if g.too_big:
            warn = QLabel("larger than the sheet: " + ", ".join(g.too_big))
            warn.setWordWrap(True)
            lay.addWidget(warn, 4, 0, 1, 2)
        b = QPushButton("Load into Nest")
        b.setToolTip("Loads this material x thickness group of the Orders list into the Nest workspace")
        b.clicked.connect(lambda _=False, key=g.key: self.load_into_nest(key))
        lay.addWidget(b, 5, 0, 1, 2)
        return f

    def load_into_nest(self, key: tuple) -> Any:
        try:
            r = self.service.load_into_nest(key)
        except Exception as exc:  # noqa: BLE001 - shown
            self.status.setText(str(exc))
            return None
        self.status.setText(f"Loaded {key[0]} {key[1]:g} mm into Nest." if r else
                            "Nothing was loaded: create the orders first (and check the Nest workspace).")
        return r
