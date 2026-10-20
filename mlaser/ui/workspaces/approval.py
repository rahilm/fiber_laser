"""Run screen > Queue: jobs submitted by the automation API or the CLI, waiting for approval.

A submitted job (:mod:`mlaser.services.intake`) is listed here with where it came from (API
client address or CLI user), its file, quantity, material and the submitter's note.  Only the
operator at the machine can **Approve** it (it becomes an ordinary queue entry, which still runs
only after "Sheet loaded: start" and the normal Start path) or **Reject** it.  There is no remote
approval: the API has no endpoint for it (docs/dev/API.md).
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from mlaser.services import units as U
from mlaser.services.intake import IntakeError
from mlaser.ui.units_qt import on_units_changed

from . import TOUCH_MIN, tr

SOURCE_TEXT = {"api": ("approval.source.api", "automation API"), "cli": ("approval.source.cli", "command line")}


class ApprovalPanel(QFrame):
    """The pending-approval list with Approve / Reject (hidden while nothing waits)."""

    COLS = (("approval.col.name", "Job"), ("approval.col.qty", "Qty"), ("approval.col.from", "From"),
            ("approval.col.material", "Material"), ("approval.col.when", "Submitted"),
            ("approval.col.note", "Note"))

    def __init__(self, ws: Any) -> None:
        super().__init__()
        self.ws = ws
        w = ws.window
        self.setObjectName("queue_approval")
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("QFrame#queue_approval{border:2px solid #ff8f00;border-radius:4px;}")
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        self.title = QLabel()
        self.title.setObjectName("queue_approval_title")
        f = self.title.font()
        f.setBold(True)
        self.title.setFont(f)
        self.title.setWordWrap(True)
        v.addWidget(self.title)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setObjectName("queue_approval_table")
        self.table.setHorizontalHeaderLabels([tr(w, k, d) for k, d in self.COLS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(TOUCH_MIN)
        self.table.setMaximumHeight(TOUCH_MIN * 4)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        v.addWidget(self.table)
        row = QHBoxLayout()
        self.approve_btn = QPushButton(tr(w, "approval.btn.approve", "Approve"))
        self.approve_btn.setObjectName("queue_approve")
        self.reject_btn = QPushButton(tr(w, "approval.btn.reject", "Reject"))
        self.reject_btn.setObjectName("queue_reject")
        for b in (self.approve_btn, self.reject_btn):
            b.setMinimumHeight(TOUCH_MIN)
            row.addWidget(b)
        self.approve_btn.clicked.connect(lambda: self.approve())
        self.reject_btn.clicked.connect(lambda: self.reject())
        v.addLayout(row)
        self.table.itemSelectionChanged.connect(self._selected)
        self.refresh()
        on_units_changed(self, lambda _u: self.refresh(), now=False)

    # ------------------------------------------------------------------ data
    @property
    def store(self) -> Any:
        return self.ws.intake

    def selected_id(self) -> Optional[str]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            return None
        it = self.table.item(rows[0], 0)
        return it.data(Qt.UserRole) if it is not None else None

    def _from(self, job: Any) -> str:
        key, default = SOURCE_TEXT.get(job.source, (job.source, job.source))
        src = tr(self.ws.window, key, default) if key != job.source else job.source
        return f"{src} {job.client}".strip()

    def refresh(self) -> None:
        w = self.ws.window
        jobs = list(self.store.pending)
        self.setVisible(bool(jobs))
        self.title.setText(tr(w, "approval.title",
                              "{n} job(s) submitted from outside wait for your approval. Check the drawing, "
                              "material and quantity; approving only adds it to the queue.", n=len(jobs)))
        keep = self.selected_id()
        self.table.setRowCount(len(jobs))
        for r, j in enumerate(jobs):
            mat = " ".join(x for x in (j.material, U.current().fmt(j.thickness_mm, "length", trim=True)
                                       if j.thickness_mm else "") if x)
            when = _dt.datetime.fromtimestamp(j.submitted).strftime("%Y-%m-%d %H:%M") if j.submitted else ""
            vals = [f"{j.name}  ({os.path.basename(j.path)})", str(j.quantity), self._from(j), mat, when,
                    " · ".join(x for x in (j.reference, j.note) if x)]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if c == 0:
                    it.setData(Qt.UserRole, j.id)
                self.table.setItem(r, c, it)
            if j.id == keep:
                self.table.selectRow(r)
        if jobs and self.selected_id() is None:
            self.table.selectRow(0)
        self._selected()

    def _selected(self) -> None:
        on = self.selected_id() is not None
        self.approve_btn.setEnabled(on)
        self.reject_btn.setEnabled(on)

    # ------------------------------------------------------------------ actions (operator only)
    def _confirm(self, title_id: str, title: str, text_id: str, text: str, **kw: Any) -> bool:
        w = self.ws.window
        if getattr(w, "_test_no_dialogs", False):
            return True
        return QMessageBox.question(w, tr(w, title_id, title), tr(w, text_id, text, **kw),
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def approve(self, job_id: Optional[str] = None) -> Any:
        """The operator approves a submitted job: it is added to the queue (it does not start)."""
        jid = job_id or self.selected_id()
        if not jid:
            return None
        try:
            job = self.store.get(jid)
        except KeyError:
            return None
        if not self._confirm("approval.confirm.title", "Approve the submitted job",
                             "approval.confirm.text",
                             "Add {name} ({file}, {qty} sheet(s)) from {source} to the queue?\n\nIt will not "
                             "start by itself: it runs only when you load the sheet and press Start.",
                             name=job.name, file=os.path.basename(job.path), qty=job.quantity,
                             source=self._from(job)):
            return None
        try:
            entry = self.store.approve(jid, self.ws.queue, operator=self._operator())
        except IntakeError as exc:
            self.ws.log(tr(self.ws.window, "approval.msg.failed", "Approval failed: {err}", err=exc))
            return None
        self.ws.log(tr(self.ws.window, "approval.msg.approved", "Approved: {name} added to the queue",
                       name=job.name))
        self.refresh()
        return entry

    def reject(self, job_id: Optional[str] = None) -> bool:
        jid = job_id or self.selected_id()
        if not jid:
            return False
        try:
            job = self.store.get(jid)
        except KeyError:
            return False
        if not self._confirm("approval.reject.title", "Reject the submitted job", "approval.reject.text",
                             "Remove {name} from the approval list? It is not queued.", name=job.name):
            return False
        self.store.reject(jid, operator=self._operator())
        self.ws.log(tr(self.ws.window, "approval.msg.rejected", "Rejected: {name}", name=job.name))
        self.refresh()
        return True

    def _operator(self) -> str:
        mode = getattr(self.ws.window, "user_mode", "") or "operator"
        return f"{mode}@machine"


__all__ = ["ApprovalPanel"]
