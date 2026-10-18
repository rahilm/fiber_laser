"""Job thumbnail for the processing report (``Report\\<name>.jpg``).

MainApp FUN_004b96c0 draws every contour of the job with GDI+ (white background,
``CreateCompatibleBitmap`` of the record's bitmap size at record +0x80/+0x84, interpolation
mode 2) and saves it with ``CImage::Save`` as JPEG next to report.txt.  Here the contours are
flattened with :meth:`mlaser.model.Contour.points` and drawn with QPainter.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF


def render_thumbnail(document, width: int = 400, height: int = 300, margin: int = 10) -> QImage:
    """White image with the document contours in black, fitted with ``margin`` pixels."""
    img = QImage(width, height, QImage.Format_RGB32)
    img.fill(QColor(255, 255, 255))
    parts = list(getattr(document, "parts", []) or [])
    bb = document.bbox() if hasattr(document, "bbox") else (0, 0, 0, 0)
    x0, y0, x1, y1 = bb
    w, h = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
    if not parts or (x1 - x0 <= 0 and y1 - y0 <= 0):
        return img
    s = min((width - 2 * margin) / w, (height - 2 * margin) / h)
    ox = (width - w * s) / 2
    oy = (height - h * s) / 2
    p = QPainter(img)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(0, 0, 0), 1))
        for part in parts:
            for c in part.contours:
                pts = [QPointF(ox + (x - x0) * s, height - (oy + (y - y0) * s)) for x, y in c.points(0.2)]
                if len(pts) >= 2:
                    p.drawPolyline(QPolygonF(pts))
    finally:
        p.end()
    return img


def save_thumbnail(document, path: str, width: int = 400, height: int = 300) -> Optional[str]:
    """Render and save as JPEG; returns the path or None when saving failed."""
    img = render_thumbnail(document, width, height)
    return path if img.save(path, "JPG") else None
