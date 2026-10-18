"""Report viewer (port of Report/report.exe): table, delete, count/bill, PDF export."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from mlaser.report.records import ReportStore, parse_line  # noqa: E402

A = "a.dxf,154.40x103.41 mm,2026-04-13 15:52:07,1.10 m,1.07 m,3,0分43秒,29.76 Sec,10.46 Sec,1.00 Sec"
B = "b.dxf,20.00x19.90 mm,2026-09-16 15:16:01,20.86 m,0.12 m,0,1分21秒,79.21 Sec,1.33 Sec,0.00 Sec"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _store(tmp_path, lang="zh"):
    st = ReportStore(str(tmp_path), lang)
    st.write_report_txt([parse_line(A), parse_line(B)])
    return st


def test_table_and_selection(app, tmp_path):
    from mlaser.report.window import ReportWindow
    w = ReportWindow(_store(tmp_path))
    assert w.windowTitle() == "加工统计" and w.model.columnCount() == 11
    assert w.model.headerData(1, Qt.Horizontal) == "加工文件名"
    # newest (last line) first
    assert w.model.item(0, 1).text() == "b.dxf" and w.model.item(1, 1).text() == "a.dxf"
    assert w.model.item(0, 0).isCheckable() and not w.buttons["add"].isVisibleTo(w)
    w.select_all()
    assert len(w.checked_rows()) == 2
    w.reverse()
    assert w.checked_rows() == []
    w.model.item(1, 0).setCheckState(Qt.Checked)
    assert [r.name for r in w.checked_records()] == ["a.dxf"]
    w.close()


def test_delete_rewrites_report_txt(app, tmp_path):
    from mlaser.report.window import ReportWindow
    st = _store(tmp_path, "en")
    w = ReportWindow(st)
    assert w.windowTitle() == "Report"
    w.model.item(0, 0).setCheckState(Qt.Checked)          # b.dxf
    assert w.delete_checked(confirm=False) == 1
    assert [r.name for r in st.read_report_txt()] == ["a.dxf"]
    w.close()


def test_count_bill_and_pdf(app, tmp_path, monkeypatch):
    from mlaser.report import window as RW
    from mlaser.report.thumbnail import save_thumbnail
    from mlaser.ui.docops import demo_document
    st = _store(tmp_path)
    assert save_thumbnail(demo_document(), st.thumbnail_path("a.dxf"))
    w = RW.ReportWindow(st)
    monkeypatch.setattr(RW.CountFeeDialog, "exec", lambda self: 0)
    d = w.count_bill()
    d.prices["cut"].setValue(2.0)
    d.prices["pierce"].setValue(0.5)
    assert abs(d.cost_cal() - (21.96 * 2 + 3 * 0.5)) < 1e-6 and d.total.text() == "45.42"
    # export without selection warns
    seen = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: seen.append(a) or QMessageBox.Ok))
    assert w.export() is None and seen
    w.select_all()
    pdf = str(tmp_path / "r.pdf")
    assert w.export(pdf) == pdf
    data = open(pdf, "rb").read()
    assert data.startswith(b"%PDF") and data.count(b"/Type /Page") >= 2
    w.close()


def test_paint_rows():
    from mlaser.report.printing import rows_for
    rows = rows_for(parse_line(A), "en")
    assert rows[0][:3] == ("Name", "a.dxf", "Date") and rows[4][1] == "3"
