"""Processing report page ("---加工报告单---" / "Processing Report").

Layout recovered from report.exe FUN_004054d0 (paintRequested slot, 0x405764 ...):

* title ``---加工报告单---`` (Chinese) / ``---Processing Report---`` in 宋体 18 pt, rect
  ``(0, 10, page width, 100)``, then a horizontal rule;
* the job thumbnail ``<report dir>/<name>.jpg`` scaled to the page width (message
  "Failed to read the image file" when missing);
* a 4-column table in 12 pt, rows 45 units high separated by rules:
  Name | value | Date | value; Size | value | Estimated processing time | value;
  Cutting Length | value | Estimated cutting time | value; EmptyMove Length | value |
  Estimated empty time | value; Punches | value | Estimated piercing time | value;
* one page per checked row.  PDF export uses QPdfWriter with page size A4 (``setPageSize(0)``)
  and the default file name ``yyyy-MM-dd-HH-mm-ss.pdf`` (0x40dbcc).
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

from PySide6.QtCore import QMarginsF, QRectF, Qt
from PySide6.QtGui import QFont, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen

from .records import JobRecord

LABELS = {
    "zh": dict(title="---加工报告单---", name="加工文件名", date="日期", size="幅面尺寸",
               est="预估加工时长", cut="切割总长", cut_t="预估切割时间", move="空移总长",
               move_t="预估空移时间", pierce="穿孔个数", pierce_t="预估穿孔时间",
               noimg="读取图片文件失败"),
    "en": dict(title="---Processing Report---", name="Name", date="Date", size="Size",
               est="Estimated processing time", cut="Cutting Length", cut_t="Estimated cutting time",
               move="EmptyMove Length", move_t="Estimated empty time", pierce="Punches",
               pierce_t="Estimated piercing time", noimg="Failed to read the image file"),
}
FONT = "SimSun"          # 宋体 (0x40dcb4); Qt falls back to any installed font


def rows_for(rec: JobRecord, lang: str = "zh") -> list[tuple[str, str, str, str]]:
    L = LABELS["zh" if lang == "zh" else "en"]
    return [
        (L["name"], rec.name, L["date"], rec.start),
        (L["size"], rec.size_text(), L["est"], rec.duration_text(lang)),
        (L["cut"], f"{rec.cut_m:.2f} m", L["cut_t"], f"{rec.cut_s:.2f} Sec"),
        (L["move"], f"{rec.move_m:.2f} m", L["move_t"], f"{rec.move_s:.2f} Sec"),
        (L["pierce"], str(rec.pierces), L["pierce_t"], f"{rec.pierce_s:.2f} Sec"),
    ]


def paint_record(p: QPainter, page: QRectF, rec: JobRecord, image_dir: Optional[str],
                 lang: str = "zh") -> list[str]:
    """Paint one report page; returns warnings (missing image)."""
    L = LABELS["zh" if lang == "zh" else "en"]
    warnings = []
    w = page.width()
    unit = max(1.0, w / 800.0)                    # report.exe works in viewport pixels
    x0, y = page.left(), page.top()
    f = QFont(FONT)
    f.setPointSizeF(18)
    f.setBold(True)
    p.setFont(f)
    p.drawText(QRectF(x0, y + 10 * unit, w, 100 * unit), Qt.AlignCenter, L["title"])
    y += 110 * unit
    pen = QPen(Qt.black)
    pen.setWidthF(unit)
    p.setPen(pen)
    p.drawLine(int(x0), int(y), int(x0 + w), int(y))
    y += 10 * unit
    img = QImage(os.path.join(image_dir, rec.name + ".jpg")) if image_dir else QImage()
    if img.isNull():
        warnings.append(L["noimg"])
    else:
        target_w = w * 0.8
        s = target_w / max(1, img.width())
        th = img.height() * s
        p.drawImage(QRectF(x0 + (w - target_w) / 2, y, target_w, th), img)
        y += th + 10 * unit
    f = QFont(FONT)
    f.setPointSizeF(12)
    p.setFont(f)
    rh = 45 * unit
    cols = [0.0, 0.22, 0.5, 0.75, 1.0]
    for row in rows_for(rec, lang):
        p.drawLine(int(x0), int(y), int(x0 + w), int(y))
        for i, txt in enumerate(row):
            r = QRectF(x0 + cols[i] * w + 4 * unit, y, (cols[i + 1] - cols[i]) * w - 8 * unit, rh)
            p.drawText(r, Qt.AlignVCenter | (Qt.AlignLeft if i % 2 == 0 else Qt.AlignLeft), txt)
        y += rh
    p.drawLine(int(x0), int(y), int(x0 + w), int(y))
    return warnings


def paint_records(p: QPainter, page_rect_fn, new_page_fn, records: Iterable[JobRecord],
                  image_dir: Optional[str], lang: str = "zh") -> list[str]:
    warnings: list[str] = []
    for i, rec in enumerate(records):
        if i:
            new_page_fn()
        warnings += paint_record(p, page_rect_fn(), rec, image_dir, lang)
    return warnings


def export_pdf(path: str, records: list[JobRecord], image_dir: Optional[str], lang: str = "zh") -> list[str]:
    """Save the report pages as PDF (report.exe "Save Pdf": QPdfWriter, A4)."""
    wr = QPdfWriter(path)
    wr.setPageSize(QPageSize(QPageSize.A4))
    wr.setPageMargins(QMarginsF(15, 15, 15, 15), QPageLayout.Millimeter)
    wr.setResolution(150)
    p = QPainter(wr)
    try:
        def rect():
            return QRectF(0, 0, wr.width(), wr.height())
        warnings = paint_records(p, rect, wr.newPage, records, image_dir, lang)
    finally:
        p.end()
    return warnings


def print_to(printer, records: list[JobRecord], image_dir: Optional[str], lang: str = "zh") -> list[str]:
    """``paintRequested(QPrinter*)`` handler of the print preview."""
    p = QPainter(printer)
    try:
        def rect():
            r = printer.pageRect(QPrinter_unit())
            return QRectF(0, 0, r.width(), r.height())
        return paint_records(p, rect, printer.newPage, records, image_dir, lang)
    finally:
        p.end()


def QPrinter_unit():
    from PySide6.QtPrintSupport import QPrinter
    return QPrinter.DevicePixel
