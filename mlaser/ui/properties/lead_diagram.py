"""Live lead diagram: the lead-in / lead-out geometry of :mod:`mlaser.cam.leads` on a sample
contour corner, redrawn on every parameter change (properties panel, technology editor).

The geometry is the real CAM code (``cam.leads.lead_in`` / ``lead_out`` on the side the toolpath
puts an outer CCW contour's leads: the scrap side), not a sketch.  Colours come from the palette
(dark-theme safe): the part in the text colour, the leads in the highlight colour, the pierce
point as a ring.
"""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from mlaser.cam.leads import lead_in, lead_out, side_flag
from mlaser.model import Arc, LeadParams


def _seg_points(seg, n: int = 24):
    if isinstance(seg, Arc):
        return [seg.point_at(k / n) for k in range(n + 1)]
    return [seg.start, seg.end]


class LeadDiagram(QWidget):
    """Draws ``lead_in`` / ``lead_out`` at the start point of a sample rectangle (mm scale)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.lead_in_p = LeadParams(kind=1, length=3.0)
        self.lead_out_p = LeadParams(kind=0)
        self.setMinimumSize(160, 110)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setAccessibleName("lead diagram")

    def sizeHint(self) -> QSize:
        return QSize(240, 130)

    def set_leads(self, lin: LeadParams, lout: LeadParams) -> None:
        self.lead_in_p, self.lead_out_p = lin, lout
        self.update()

    def geometry_mm(self):
        """(contour polyline, lead-in polylines, lead-out polylines) in mm: a 20 mm wide edge of
        an outer CCW contour, start point at the origin travelling +x."""
        P, d = (0.0, 0.0), (1.0, 0.0)
        flag = side_flag(1, True)
        lin = [_seg_points(s) for s in lead_in(P, d, self.lead_in_p, flag)]
        lout = [_seg_points(s) for s in lead_out(P, (1.0, 0.0), self.lead_out_p, flag)]
        w = 12.0
        part = [(-w, 0.0), (w, 0.0), (w, 8.0), (-w, 8.0), (-w, 0.0)]
        return part, lin, lout

    def paintEvent(self, ev) -> None:  # noqa: N802 - Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pal = self.palette()
        p.fillRect(self.rect(), pal.color(QPalette.Base))
        part, lin, lout = self.geometry_mm()
        pts = [q for poly in [part] + lin + lout for q in poly]
        x0 = min(q[0] for q in pts) - 1.0
        x1 = max(q[0] for q in pts) + 1.0
        y0 = min(q[1] for q in pts) - 1.0
        y1 = max(q[1] for q in pts) + 1.0
        r = QRectF(self.rect()).adjusted(8, 8, -8, -22)
        s = min(r.width() / max(x1 - x0, 1e-6), r.height() / max(y1 - y0, 1e-6))
        cx = r.center().x() - (x0 + x1) / 2 * s
        cy = r.center().y() + (y0 + y1) / 2 * s

        def path(poly):
            pp = QPainterPath()
            pp.moveTo(QPointF(cx + poly[0][0] * s, cy - poly[0][1] * s))
            for q in poly[1:]:
                pp.lineTo(QPointF(cx + q[0] * s, cy - q[1] * s))
            return pp

        txt = pal.color(QPalette.Text)
        mid = QColor(txt)
        mid.setAlpha(110)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(mid, 1.0, Qt.DashLine))
        p.drawPath(path(part))
        p.setPen(QPen(txt, 2.0))
        p.drawPath(path([(-12.0, 0.0), (12.0, 0.0)]))
        hi = pal.color(QPalette.Highlight)
        p.setPen(QPen(hi, 2.2))
        for poly in lin:
            p.drawPath(path(poly))
        p.setPen(QPen(hi, 2.2, Qt.DotLine))
        for poly in lout:
            p.drawPath(path(poly))
        # pierce point: the start of the lead-in (or the contour start without one)
        pierce = lin[0][0] if lin else (0.0, 0.0)
        p.setPen(QPen(hi, 1.6))
        p.drawEllipse(QPointF(cx + pierce[0] * s, cy - pierce[1] * s), 4.0, 4.0)
        # direction arrow on the contour
        ax, ay = cx + 6.0 * s, cy
        p.setPen(QPen(txt, 1.4))
        p.drawLine(QPointF(ax - 5, ay - 4), QPointF(ax, ay))
        p.drawLine(QPointF(ax - 5, ay + 4), QPointF(ax, ay))
        # scale bar: 1 mm
        p.setPen(QPen(txt, 1.0))
        bx, by = 10.0, self.height() - 10.0
        p.drawLine(QPointF(bx, by), QPointF(bx + s, by))
        p.drawText(QPointF(bx + s + 4, by + 4), "1 mm")
        lp = self.lead_in_p
        if lp.kind:
            info = f"{lp.length:g} mm · {lp.angle_deg:g}°" + (f" · r {lp.radius:g} mm" if lp.kind == 3 else "")
            p.drawText(QRectF(self.rect()).adjusted(0, 0, -8, -4), int(Qt.AlignRight | Qt.AlignBottom), info)
        p.end()

    @staticmethod
    def lead_length(poly) -> float:
        return sum(math.dist(a, b) for a, b in zip(poly, poly[1:]))
