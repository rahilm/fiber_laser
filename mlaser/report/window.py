"""PySide6 port of the companion program ``Report\\report.exe`` (Qt 5, class ``Qlist_test``).

Observed in report.exe (Ghidra via the REA CLI, docs/re/diag.md "Report viewer"):

* main window 800 x 320 (``setFixedSize(800, 0x140)``), title ``加工统计`` / ``Report``
  (``lang==0`` in ``lang.txt`` = Chinese), icon ``./icon.ico``;
* reads ``<app dir>/report.txt``; only lines with exactly 10 comma fields become rows, newest
  first; column 0 is a checkable "Pick" item (``选择,加工文件名,幅面尺寸,日期,切割总长,
  空移总长,穿孔个数,预估加工时长,预估切割时间,预估空移时间,预估穿孔时间``);
* buttons Select All (全选), Reverse (反选), Add (添加, hidden), Delete (删除, confirmation
  "Are you sure to delete the  data?", removes the lines from report.txt), Count/Bill
  (统计/计费, dialog ``countFee``; hidden in the original), Export (生成加工报告单: print
  preview of the checked rows, "please choose the print contents" when none, Save Pdf);
* ``countFee`` (统计&计费): cumulative empty-move length (m), pierce count, cut length (m),
  processing time (s), a unit price for each and the total cost (费用计算 / Total Cost).

The rebuild shows the Count/Bill button (the original hides it) because it is the only way to
reach the billing dialog.  "Add" stays hidden as in the original.
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
                               QTableView, QVBoxLayout, QWidget)

from . import printing
from .records import JobRecord, ReportStore, cost, totals

HEADERS = {
    "zh": ["选择", "加工文件名", "幅面尺寸", "日期", "切割总长", "空移总长", "穿孔个数", "预估加工时长",
           "预估切割时间", "预估空移时间", "预估穿孔时间"],
    "en": ["Pick", "Name", "Size", "Date", "Cutting", "EmptyMove", "Punches", "Estimated processing time",
           "Estimated cutting time", "Estimated empty time", "Estimated piercing time"],
}
TEXT = {
    "zh": dict(title="加工统计", all="全选", rev="反选", add="添加", delete="删除", count="统计/计费",
               export="生成加工报告单", ask_del="确定删除数据吗？", tips="提示",
               choose="请选择打印内容", fee="统计&计费", price="单价(元)", calc="费用计算",
               move="累计空移总长(米)", pierce="累计穿孔个数", cut="累计切割总长(米)",
               time="累计加工耗时(秒)", total="总费用", preview="打印预览", pdf="保存PDF"),
    "en": dict(title="Report", all="Select All", rev="Reverse", add="Add", delete="Delete",
               count="Count/Bill", export="Export", ask_del="Are you sure to delete the  data?",
               tips="Tips", choose="please choose the print contents", fee="Count/Bill",
               price="Price", calc="Total Cost", move="EmptyMove(m)", pierce="Punches",
               cut="Cutting(m)", time="Process Time(S)", total="Total Cost",
               preview="Print preview", pdf="Save Pdf"),
}


class CountFeeDialog(QDialog):
    """``countFee`` statistics & billing dialog."""

    def __init__(self, records: list[JobRecord], lang: str = "zh", parent=None):
        super().__init__(parent)
        T = TEXT[lang]
        self.setWindowTitle(T["fee"])
        self.tot = totals(records)
        lay = QVBoxLayout(self)
        box = QGroupBox(T["fee"])
        form = QFormLayout(box)
        self.prices = {}
        for key, label, val in (("move", T["move"], f"{self.tot['move_m']:.2f}"),
                                ("pierce", T["pierce"], str(self.tot["pierces"])),
                                ("cut", T["cut"], f"{self.tot['cut_m']:.2f}"),
                                ("time", T["time"], f"{self.tot['time_s']:.2f}")):
            row = QHBoxLayout()
            q = QLineEdit(val)
            q.setReadOnly(True)
            row.addWidget(q)
            row.addWidget(QLabel(T["price"]))
            sp = QDoubleSpinBox()
            sp.setRange(0, 1e9)
            sp.setDecimals(4)
            row.addWidget(sp)
            self.prices[key] = sp
            form.addRow(label, row)
        lay.addWidget(box)
        row = QHBoxLayout()
        self.calc = QPushButton(T["calc"])
        self.calc.clicked.connect(self.cost_cal)
        self.total = QLineEdit("0.00")
        self.total.setReadOnly(True)
        row.addWidget(self.calc)
        row.addWidget(QLabel(T["total"]))
        row.addWidget(self.total)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def cost_cal(self) -> float:
        v = cost(self.tot, self.prices["cut"].value(), self.prices["move"].value(),
                 self.prices["pierce"].value(), self.prices["time"].value())
        self.total.setText(f"{v:.2f}")
        return v


class ReportWindow(QMainWindow):
    """Report viewer over a :class:`ReportStore` (``report.txt`` + thumbnails)."""

    def __init__(self, store: ReportStore, lang: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.store = store
        self.lang = lang or store.viewer_lang()
        T = TEXT[self.lang]
        self.setWindowTitle(T["title"])
        self.resize(800, 320)
        cw = QWidget()
        self.setCentralWidget(cw)
        lay = QVBoxLayout(cw)
        self.view = QTableView()
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.verticalHeader().hide()
        self.view.horizontalHeader().setHighlightSections(False)
        self.view.horizontalHeader().setStyleSheet(
            "QHeaderView::section {color: black;padding-left: 4px;border: 1px solid #6c6c6c;}")
        self.view.doubleClicked.connect(self._double_clicked)
        lay.addWidget(self.view)
        row = QHBoxLayout()
        self.buttons = {}
        for key, fn in (("all", self.select_all), ("rev", self.reverse), ("add", self.add_file),
                        ("delete", self.delete_checked), ("count", self.count_bill),
                        ("export", self.export)):
            b = QPushButton(T[key])
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
            self.buttons[key] = b
        self.buttons["add"].hide()               # hidden in report.exe (QWidget::hide(+0x68))
        row.addStretch(1)
        lay.addLayout(row)
        self.records: list[JobRecord] = []
        self.reload()

    # ------------------------------------------------------------------ model
    def reload(self) -> None:
        self.set_records(self.store.read_report_txt())

    def set_records(self, records: list[JobRecord]) -> None:
        self.records = list(records)
        m = QStandardItemModel(len(self.records), 11, self)
        m.setHorizontalHeaderLabels(HEADERS[self.lang])
        # newest first (FUN_00407220 fills rows from the bottom up)
        for row, rec in enumerate(reversed(self.records)):
            it = QStandardItem(str(row + 1))
            it.setCheckable(True)
            it.setTextAlignment(Qt.AlignCenter)
            it.setEditable(False)
            m.setItem(row, 0, it)
            for col, txt in enumerate(rec.fields(self.lang), start=1):
                c = QStandardItem(txt)
                c.setEditable(False)
                m.setItem(row, col, c)
        self.model = m
        self.view.setModel(m)
        self.view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for col, w in enumerate((50, 160, 160, 100, 120, 120, 120, 160, 160, 160, 160)):
            self.view.setColumnWidth(col, w)

    def _record_at(self, row: int) -> JobRecord:
        return self.records[len(self.records) - 1 - row]

    def checked_rows(self) -> list[int]:
        return [r for r in range(self.model.rowCount())
                if self.model.item(r, 0).checkState() == Qt.Checked]

    def checked_records(self) -> list[JobRecord]:
        return [self._record_at(r) for r in self.checked_rows()]

    # ------------------------------------------------------------------ slots
    def select_all(self) -> None:
        for r in range(self.model.rowCount()):
            self.model.item(r, 0).setCheckState(Qt.Checked)

    def reverse(self) -> None:
        for r in range(self.model.rowCount()):
            it = self.model.item(r, 0)
            it.setCheckState(Qt.Unchecked if it.checkState() == Qt.Checked else Qt.Checked)

    def _double_clicked(self, index) -> None:
        it = self.model.item(index.row(), 0)
        it.setCheckState(Qt.Unchecked if it.checkState() == Qt.Checked else Qt.Checked)

    def add_file(self) -> None:
        """Append the records of another report/history file (button hidden as in report.exe)."""
        from .records import read_records
        path, _ = QFileDialog.getOpenFileName(self, TEXT[self.lang]["add"], self.store.report_dir,
                                              "Report (*.txt)")
        if path:
            self.set_records(self.records + read_records(path))
            self.store.rewrite_report_txt(self.records)

    def delete_checked(self, confirm: bool = True) -> int:
        rows = self.checked_rows()
        if not rows:
            return 0
        T = TEXT[self.lang]
        if confirm and QMessageBox.warning(self, T["tips"], T["ask_del"],
                                           QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return 0
        drop = {id(self._record_at(r)) for r in rows}
        self.set_records([r for r in self.records if id(r) not in drop])
        self.store.rewrite_report_txt(self.records)
        return len(rows)

    def count_bill(self) -> CountFeeDialog:
        recs = self.checked_records() or self.records
        d = CountFeeDialog(recs, self.lang, self)
        d.exec()
        return d

    def export(self, pdf_path: Optional[str] = None) -> Optional[str]:
        """Processing report: print preview, or straight to PDF when ``pdf_path`` is given."""
        T = TEXT[self.lang]
        recs = self.checked_records()
        if not recs:
            QMessageBox.warning(self, T["tips"], T["choose"])
            return None
        if pdf_path:
            printing.export_pdf(pdf_path, recs, self.store.report_dir, self.lang)
            return pdf_path
        from PySide6.QtPrintSupport import QPrinter, QPrintPreviewDialog
        printer = QPrinter(QPrinter.HighResolution)
        dlg = QPrintPreviewDialog(printer, self)
        dlg.setWindowTitle(T["preview"])
        dlg.paintRequested.connect(lambda pr: printing.print_to(pr, recs, self.store.report_dir, self.lang))
        # Save Pdf button (report.exe adds one to the preview toolbar)
        from PySide6.QtWidgets import QToolBar
        bar = dlg.findChild(QToolBar)
        if bar is not None:
            bar.addAction(T["pdf"], lambda: self.save_pdf(recs))
        dlg.exec()
        return None

    def save_pdf(self, recs: Optional[list[JobRecord]] = None) -> Optional[str]:
        T = TEXT[self.lang]
        recs = recs or self.checked_records()
        name = _dt.datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + ".pdf"
        path, _ = QFileDialog.getSaveFileName(self, T["pdf"], os.path.join(self.store.report_dir, name), "*.pdf")
        if not path:
            return None
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        printing.export_pdf(path, recs, self.store.report_dir, self.lang)
        return path


def main(argv=None) -> int:
    """``python -m mlaser.report [data dir]``: the standalone viewer like report.exe."""
    import sys
    from PySide6.QtWidgets import QApplication
    argv = list(sys.argv[1:] if argv is None else argv)
    root = argv[0] if argv else os.path.join(os.path.expanduser("~"), ".mlaser")
    app = QApplication.instance() or QApplication([])
    w = ReportWindow(ReportStore(root))
    w.show()
    return app.exec()
