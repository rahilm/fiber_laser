"""Drawing canvas: renders a :class:`mlaser.model.Document` (replaces CADModule's OpenGL view).

Observed original behaviour (CADModule.dll is an OpenGL view: glBegin/glVertex2d/glColor3d,
glLineStipple for dashed rapids; MainApp ribbon "View/Show" panel):
* contours drawn in their layer colour (``layer_colors.json``; per-layer colour fed to
  ``glColor3d`` in CADModule ~0x10065ac0),
* view options ``mf38`` bounding box, ``mf39`` unclosed contours in red, ``mf40`` index numbers,
  ``mf41`` path start, ``mf42`` direction arrows, ``mf43`` move (rapid) path,
* modes ``mf27`` contour select, ``mf28`` vertex edit, ``mf29`` view move (pan),
  ``mf198/199/200`` zoom in/out/reset,
* multi-select with Ctrl or Shift + left button (``np_CtrlShiftSelTip``),
* simulation (``mp59`` Sim, speed ``mf121``) draws the head moving along the path,
  rapids dashed, cut track solid ("clear track line" ``mv11`` / ``pd105``).
Inferred: Y axis up, wheel zooms about the cursor, middle button pans, window vs
crossing rubber-band selection.

Interaction-tool hook (phase 3, drawing tools): :meth:`CanvasView.set_tool` routes mouse and
key input in model coordinates to a tool object, with a temporary preview overlay
(:meth:`set_preview`) and object snapping (:meth:`snap_point`); see the comment block above
``set_tool``.  With no tool installed the behaviour is unchanged.
"""
from __future__ import annotations

import json
import math
import time
import os
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen, QTransform)
from PySide6.QtWidgets import (QFrame, QGraphicsEllipseItem, QGraphicsItem, QGraphicsLineItem,
                               QGraphicsPathItem, QGraphicsRectItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView,
                               QStyleOptionGraphicsItem, QWidget)

from .style import ICON_TINTS, SP1, SP2, font  # noqa: E402

RES_DIR = os.path.join(os.path.dirname(__file__), "resources")

# style-lint: colour table
#: the original's 11 layer colours (document data, as the original shows them)
_DEFAULT_COLORS = ["#00fc00", "#00fcfc", "#fc80fc", "#fcfc00", "#00a0e8", "#6e91be",
                   "#96dce6", "#b47855", "#ed1c24", "#999999", "#e6e6e6"]


def load_layer_colors() -> list[str]:
    path = os.path.join(RES_DIR, "layer_colors.json")
    try:
        return list(json.load(open(path, encoding="utf-8"))["colors"])
    except Exception:
        return list(_DEFAULT_COLORS)


LAYER_COLORS = load_layer_colors()


def layer_color(index: int) -> QColor:
    """The layer's colour as stored in the document (the original's palette; data, not theme)."""
    return QColor(LAYER_COLORS[index % len(LAYER_COLORS)])


# Theme (mlaser.ui.shell.theme.apply_canvas_theme): the colour of each canvas role, and the
# layer colours as drawn (contrast-adjusted for the canvas background).  The defaults are the
# original's black drawing area, so a window without the shell looks as before.
CANVAS_COLORS: dict[str, str] = {
    "background": "#000000", "table": "#606060", "axis_x": "#c04040", "axis_y": "#40c040",
    "cut": "#d0d0d0", "gap": "#ff8000", "rapid": "#8080ff", "track": "#ff6060",
    "unclosed": "#ff0000", "selected": "#ffffff", "start": "#ffffff", "index": "#ffd700",
    "bbox": "#707070", "arrow": "#ffffff", "head": "#ff4040", "marker": "#00ffff",
    "sim": "#ffffff", "lead_in": "#ff4fa0", "lead_out": "#b28cff", "kerf": "#ffc53d",
    "pierce": "#ff4fa0", "preview": "#e6e600", "preview_alert": "#e53935",
}
# style-lint: end
_DISPLAY_LAYER_COLORS: Optional[list[str]] = None


def set_display_layer_colors(colors: Optional[list[str]]) -> None:
    global _DISPLAY_LAYER_COLORS
    _DISPLAY_LAYER_COLORS = list(colors) if colors else None


def display_layer_color(index: int) -> QColor:
    """The layer colour as drawn on the canvas in the current theme."""
    cols = _DISPLAY_LAYER_COLORS or LAYER_COLORS
    return QColor(cols[index % len(cols)])


def _cc(role: str) -> QColor:
    return QColor(CANVAS_COLORS[role])


class SelectionBand(QWidget):
    """The selection rectangle, painted by the canvas itself.  ``QRubberBand`` is drawn by the
    platform style: on macOS under the application's style sheet it came out as an opaque black
    box.  Like CAD programs, the band shows the selection rule: dragged to the right a **window**
    (solid outline, blue: parts entirely inside), to the left a **crossing** (dashed, green: parts
    it touches), the rule ``_rubber_select`` applies."""

    WINDOW = QColor(ICON_TINTS["info"])         # solid: parts entirely inside
    CROSSING = QColor(ICON_TINTS["success"])    # dashed: parts it touches

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setObjectName("selection_band")
        self._origin = None
        self.crossing = False
        self.hide()

    def start(self, pos) -> None:
        self._origin = pos
        self.crossing = False
        self.setGeometry(pos.x(), pos.y(), 0, 0)
        self.show()
        self.raise_()

    def drag(self, pos) -> None:
        if self._origin is None:
            return
        from PySide6.QtCore import QRect
        self.crossing = pos.x() < self._origin.x()
        self.setGeometry(QRect(self._origin, pos).normalized().adjusted(0, 0, 1, 1))
        self.update()

    def finish(self) -> None:
        self._origin = None
        self.hide()

    def paintEvent(self, _ev) -> None:   # noqa: N802 - Qt
        c = QColor(self.CROSSING if self.crossing else self.WINDOW)
        p = QPainter(self)
        fill = QColor(c)
        fill.setAlpha(40)
        p.fillRect(self.rect(), fill)
        pen = QPen(c, 1, Qt.DashLine if self.crossing else Qt.SolidLine)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.end()

@dataclass
class ViewOptions:
    show_bbox: bool = False
    unclosed_red: bool = True
    show_index: bool = True
    show_start: bool = True
    show_direction: bool = True       # direction arrows on the planned path
    show_move_path: bool = True       # rapids (dashed)
    show_toolpath: bool = True        # the planned toolpath: leads, kerf path, gaps, pierce points
    background: str = CANVAS_COLORS["background"]   # SP.DrawblkColor "Drawing area color" (default unverified)
    table_size: tuple[float, float] = (3000.0, 1500.0)
    show_rulers: bool = True      # top / left rulers in the display units (docs/user/units.md)
    show_grid: bool = False       # grid lines at the ruler's major step


#: ruler / grid steps per display unit: (major step in that unit, minor ticks per major)
_METRIC_STEPS = [(m * 10.0 ** e, 5 if m != 2 else 4) for e in range(-2, 6) for m in (1, 2, 5)]
_INCH_STEPS = [(1 / 64, 1), (1 / 32, 2), (1 / 16, 2), (1 / 8, 2), (1 / 4, 4), (1 / 2, 4), (1.0, 4),
               (2.0, 4), (3.0, 3), (6.0, 6), (12.0, 4), (24.0, 4), (36.0, 3), (60.0, 5), (120.0, 10),
               (240.0, 4), (600.0, 5), (1200.0, 10), (2400.0, 4), (6000.0, 5), (12000.0, 10)]


def ruler_step(px_per_mm: float, units=None, min_px: float = 56.0) -> tuple[float, float]:
    """(major, minor) ruler / grid step in **mm** for a zoom of ``px_per_mm``: the smallest
    "nice" step of the display length unit at least ``min_px`` pixels apart (metric 1-2-5 steps;
    inches 1/64 ... 1/2, 1, 2, 3, 6 in, 1, 2, 3, 5, 10 ft ...)."""
    from mlaser.services import units as U
    u = units or U.current()
    unit = u.unit("length")
    mm_per_unit = U.UNIT_TABLE["length"][unit]
    steps = _INCH_STEPS if unit in ("in", "ft") else _METRIC_STEPS
    if unit == "ft":
        steps = [(st / 12.0, n) for st, n in _INCH_STEPS]
    for st, n in steps:
        if st * mm_per_unit * px_per_mm >= min_px:
            return st * mm_per_unit, st * mm_per_unit / n
    st, n = steps[-1]
    return st * mm_per_unit, st * mm_per_unit / n


_PATH_CACHE: dict = {}       # segment values -> (QPainterPath, points): edits rebuild the scene


def _contour_path(contour) -> tuple[QPainterPath, list[tuple[float, float]]]:
    """Display path and polyline of a contour, cached on the values of its segments (the
    scene is rebuilt after every edit; unchanged contours reuse their path)."""
    from mlaser.geom.curves import _fingerprint
    fp = _fingerprint(contour.segments)
    key = None if fp is None else (fp, bool(contour.closed))
    if key is not None:
        hit = _PATH_CACHE.get(key)
        if hit is not None:
            return hit[0], hit[1]
    path, pts = _make_contour_path(contour)
    if key is not None:
        if len(_PATH_CACHE) > 200_000:
            _PATH_CACHE.clear()
        _PATH_CACHE[key] = (path, pts)
    return path, pts


def _make_contour_path(contour) -> tuple[QPainterPath, list[tuple[float, float]]]:
    from mlaser.geom.curves import flatten_points
    pts = flatten_points(contour, 0.05)
    path = QPainterPath()
    if pts:
        path.moveTo(*pts[0])
        for p in pts[1:]:
            path.lineTo(*p)
        if contour.closed and pts[0] != pts[-1]:
            path.closeSubpath()
    return path, pts


_LABEL_FONT: list = []


def _label_font() -> QFont:
    """The cut-order labels' font, made once (each ``QFont("Sans")`` is a font match; made per
    label it was a large part of drawing a new toolpath on macOS)."""
    if not _LABEL_FONT:
        f = QFont("Sans")
        f.setPointSizeF(font("caption").pointSizeF() or QFont().pointSizeF())
        _LABEL_FONT.append(f)
    return _LABEL_FONT[0]


def _arc_to(path: QPainterPath, start, end, center, ccw: bool) -> None:
    """Append the arc ``start`` -> ``end`` about ``center`` (``model.Arc`` semantics: a full
    circle when start == end) to ``path``, whose current point is ``start``.

    Qt measures arc angles with y pointing down; the scene's y points up (the view flips it),
    so the scene angle a is Qt's -a."""
    cx, cy = center
    r = math.hypot(start[0] - cx, start[1] - cy)
    if r <= 0:
        path.lineTo(*end)
        return
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    a1 = math.atan2(end[1] - cy, end[0] - cx)
    d = a1 - a0
    if ccw:
        while d <= 1e-12:
            d += 2 * math.pi
    else:
        while d >= -1e-12:
            d -= 2 * math.pi
    path.arcTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), -math.degrees(a0), -math.degrees(d))


def _seg_dist(p, a, b) -> float:
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


_TOOL_KEYS = {Qt.Key_Escape: "escape", Qt.Key_Return: "return", Qt.Key_Enter: "return",
              Qt.Key_Backspace: "backspace", Qt.Key_Delete: "delete"}


def _button(b) -> str:
    return {Qt.LeftButton: "left", Qt.RightButton: "right"}.get(b, "middle")


def _mods(m) -> set:
    out = set()
    if m & Qt.ShiftModifier:
        out.add("shift")
    if m & Qt.ControlModifier:
        out.add("ctrl")
    if m & Qt.AltModifier:
        out.add("alt")
    return out


# Level of detail for large documents (architecture.md §4.2): above LOD_CONTOURS contours the
# per-contour overlays (start markers, index labels, direction arrows, lead previews, move path,
# bounding boxes) are not built, and contours are drawn by _LodPathItem, which paints a contour
# smaller than LOD_TINY_PX screen pixels as a single point.
LOD_CONTOURS = 3000
#: the overlay is built for at most this long per event-loop turn (the rest follows)
OVERLAY_SLICE_S = 0.012
#: drawings with up to this many contours get their overlay at once
OVERLAY_SYNC_CONTOURS = 300
LOD_TINY_PX = 3.0


class _LodPathItem(QGraphicsPathItem):
    """Contour item that degrades to a point when it is tiny at the current zoom."""

    def paint(self, painter, option, widget=None) -> None:
        br = self.boundingRect()
        lod = QStyleOptionGraphicsItem.levelOfDetailFromTransform(painter.worldTransform())
        if max(br.width(), br.height()) * lod < LOD_TINY_PX:
            painter.setPen(self.pen())
            painter.drawPoint(br.center())
            return
        super().paint(painter, option, widget)


class _RasterItem(QGraphicsItem):
    """One item for a raster engraving part (mlaser.cam.raster_engraving): its burn runs as a
    QImage on the pixel grid (pitch 1/rho, rows at the run y), placed with the part's frame
    placement; without a known pitch the runs are drawn as one line path."""

    MAX_PIXELS = 16_000_000

    def __init__(self, part, color: QColor):
        super().__init__()
        from mlaser.cam import raster_engraving as R
        self.raster = R.raster_of_part(part)
        r = self.raster
        self.setZValue(0.8)
        self._image: Optional[QImage] = None
        self._path: Optional[QPainterPath] = None
        self._rect = QRectF()
        self._color = QColor(color)
        self._mask = None
        try:
            pl = R.placement(part)
        except R.RasterFrameError:
            pl = None
        if pl is not None:
            self.setTransform(QTransform(pl.a, pl.c, pl.b, pl.d, pl.tx, pl.ty))
        if r is None or len(r) == 0:
            return
        if r.rho:
            self._build_mask(r)
        else:
            path = QPainterPath()
            y = r.y
            for a, b, yy in zip(r.x0.tolist(), r.x1.tolist(), y.tolist()):
                path.moveTo(a, yy)
                path.lineTo(b, yy)
            self._path = path
            self._rect = path.boundingRect()
        self.set_color(color)

    def _build_mask(self, r) -> None:
        import numpy as np
        xa, ya, xb, yb = r.frame
        rho = float(r.rho)
        lo, hi = np.minimum(r.x0, r.x1), np.maximum(r.x0, r.x1)
        ys = r.y
        ncol = max(1, int(round((xb - xa) * rho)))
        nrow = int(round((float(ys.max()) - ya) * rho)) + 1
        k = 1
        while (ncol // k + 1) * (nrow // k + 1) > self.MAX_PIXELS:
            k += 1
        c0 = np.clip(np.rint((lo - xa) * rho).astype(np.int64) // k, 0, ncol // k)
        c1 = np.clip(-(-np.rint((hi - xa) * rho).astype(np.int64) // k), 0, ncol // k + 1)
        c1 = np.maximum(c1, c0 + 1)
        pr = np.clip(np.rint((ys - ya) * rho).astype(np.int64) // k, 0, (nrow - 1) // k)
        W, H = ncol // k + 1, (nrow - 1) // k + 1
        diff = np.zeros((H, W + 1), np.int32)
        np.add.at(diff, (pr, c0), 1)
        np.add.at(diff, (pr, c1), -1)
        self._mask = np.cumsum(diff[:, :W], axis=1) > 0
        px = k / rho
        self._rect = QRectF(xa, ya - 0.5 / rho, W * px, H * px)

    def set_color(self, color: QColor) -> None:
        self._color = QColor(color)
        if self._mask is not None:
            import numpy as np
            argb = np.uint32(self._color.rgba() | 0xFF000000)
            buf = np.where(self._mask, argb, np.uint32(0)).astype(np.uint32)
            h, w = buf.shape
            self._buf = np.ascontiguousarray(buf)
            self._image = QImage(self._buf.data, w, h, 4 * w, QImage.Format_ARGB32).copy()
        self.update()

    def boundingRect(self) -> QRectF:
        return QRectF(self._rect)

    def contains_scene(self, x: float, y: float) -> bool:
        inv, ok = self.sceneTransform().inverted()
        return bool(ok) and self._rect.contains(inv.map(QPointF(x, y)))

    def paint(self, painter, option, widget=None) -> None:
        if self._image is not None:
            lod = QStyleOptionGraphicsItem.levelOfDetailFromTransform(painter.worldTransform())
            px = self._rect.width() / max(1, self._image.width())
            painter.setRenderHint(QPainter.SmoothPixmapTransform, px * lod < 1.0)
            painter.drawImage(self._rect, self._image)
        elif self._path is not None:
            pen = QPen(self._color, 0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawPath(self._path)


class _Marker(QGraphicsEllipseItem):
    """Fixed pixel-size dot (ignores view zoom)."""

    def __init__(self, x, y, r, color: QColor):
        super().__init__(-r, -r, 2 * r, 2 * r)
        self.setPos(x, y)
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        self.setBrush(QBrush(color))
        self.setPen(QPen(Qt.NoPen))
        self.setZValue(5)


@dataclass(frozen=True)
class ToolpathMark:
    """Where a contour's planned cut begins (from the toolpath, so a moved start or a lead shows
    where the machine really pierces)."""

    part: int
    contour: int
    pierce: tuple                # first point the laser fires (the lead-in start)
    start: tuple                 # where the contour itself starts (end of the lead-in)
    direction: tuple             # unit cut direction at ``start``
    lead_length: float = 0.0     # mm; 0 = no lead-in
    lead_direction: Optional[tuple] = None   # unit direction of the lead-in where it meets the contour


_LEAD_OUT_MOVES: dict = {}


def _lead_out_moves(lp) -> int:
    """How many moves :func:`mlaser.cam.leads.lead_out` makes for ``lp`` (the count does not
    depend on the contour, only on the lead's type and size)."""
    key = (int(lp.kind), round(float(lp.length), 9), round(float(lp.angle_deg), 9), round(float(lp.radius), 9))
    n = _LEAD_OUT_MOVES.get(key)
    if n is None:
        try:
            from mlaser.cam.leads import lead_out
            n = len(lead_out((0.0, 0.0), (1.0, 0.0), lp, True))
        except Exception:  # noqa: BLE001 - display only
            n = 0
        _LEAD_OUT_MOVES[key] = n
    return n


def _move_length(m) -> float:
    if type(m).__name__ == "CutArc":
        from mlaser.model import Arc
        return Arc(tuple(m.start), tuple(m.end), tuple(m.center), bool(m.ccw)).length()
    return math.dist(m.start, m.end)


def _move_direction(m, end: bool = False) -> tuple:
    """Unit direction of a cut move at its start (``end``: at its end)."""
    if type(m).__name__ == "CutArc":
        p = m.end if end else m.start
        rx, ry = p[0] - m.center[0], p[1] - m.center[1]
        d = (-ry, rx) if m.ccw else (ry, -rx)
    else:
        d = (m.end[0] - m.start[0], m.end[1] - m.start[1])
    n = math.hypot(*d)
    return (d[0] / n, d[1] / n) if n > 1e-12 else (1.0, 0.0)


class _LeadGlyph(QGraphicsItem):
    """A lead-in that stays visible at any zoom: at the contour start, a stroke of ``MIN_PX``
    screen pixels along the lead, drawn only while the lead itself is shorter than that on
    screen (the pierce ring, :class:`_Ring`, marks the real pierce point)."""

    MIN_PX = 12.0
    RING_PX = 3.5

    def __init__(self, view, mark: ToolpathMark, color: QColor) -> None:
        super().__init__()
        self._view = view
        self._mark = mark
        self._color = QColor(color)
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        self.setPos(*mark.start)
        self.setZValue(5.5)
        self.setData(0, "toolpath.lead_glyph")

    def boundingRect(self) -> QRectF:
        r = self.MIN_PX + self.RING_PX + 2
        return QRectF(-r, -r, 2 * r, 2 * r)

    def screen_length(self) -> float:
        return self._mark.lead_length * abs(self._view.transform().m11())

    def paint(self, painter, option, widget=None) -> None:
        if self.screen_length() >= self.MIN_PX:
            return
        dx, dy = self._mark.lead_direction or (1.0, 0.0)
        # item coordinates are screen pixels with y down; the scene's y is up
        x, y = -dx * self.MIN_PX, dy * self.MIN_PX
        pen = QPen(self._color, 2.0)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(0, 0), QPointF(x, y))


class _Ring(QGraphicsEllipseItem):
    """Fixed pixel-size ring (pierce point)."""

    def __init__(self, x, y, r, color: QColor):
        super().__init__(-r, -r, 2 * r, 2 * r)
        self.setPos(x, y)
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        pen = QPen(color, 1.6)
        self.setPen(pen)
        self.setBrush(Qt.NoBrush)
        self.setZValue(5.4)
        self.setData(0, "toolpath.pierce")


class CanvasHud(QFrame):
    """The canvas's own bar (top left of the view): the **Toolpath** toggle with the colour
    legend of the planned path, the plan state ("Computing toolpath…") and *Fit drawing* /
    *Fit table*.  It adapts to the view's width: the legend is dropped first, then the button
    texts shorten to icons-free short labels; nothing is a fixed width."""

    def __init__(self, view: "CanvasView") -> None:
        super().__init__(view)
        from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QToolButton, QVBoxLayout
        from .i18n import tr
        self.view = view
        self.setObjectName("canvas_hud")
        self.setFrameShape(QFrame.NoFrame)
        self.setAutoFillBackground(False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP2, SP1, SP2, SP1)
        outer.setSpacing(SP1 // 4)
        lay = QHBoxLayout()
        lay.setSpacing(SP2)
        outer.addLayout(lay)
        self.toggle = QCheckBox(tr("canvas.hud.toolpath"))
        self.toggle.setObjectName("canvas_hud_toolpath")
        self.toggle.setToolTip(tr("canvas.hud.toolpath_tip"))
        self.toggle.setChecked(view.options.show_toolpath)
        self.toggle.toggled.connect(view.set_show_toolpath)
        lay.addWidget(self.toggle)
        self.legend = QLabel()
        self.legend.setObjectName("canvas_hud_legend")
        self.legend.setTextFormat(Qt.RichText)
        self.legend.setWordWrap(True)
        self.status = QLabel()
        self.status.setObjectName("canvas_hud_status")
        lay.addWidget(self.status)
        self.fit_drawing = QToolButton()
        self.fit_drawing.setText(tr("canvas.fit_drawing"))
        self.fit_drawing.setToolTip(tr("canvas.fit_drawing_tip"))
        self.fit_drawing.setObjectName("canvas_fit_drawing")
        self.fit_drawing.clicked.connect(view.zoom_fit)
        self.fit_table = QToolButton()
        self.fit_table.setText(tr("canvas.fit_table"))
        self.fit_table.setToolTip(tr("canvas.fit_table_tip"))
        self.fit_table.setObjectName("canvas_fit_table")
        self.fit_table.clicked.connect(view.zoom_fit_table)
        for b in (self.fit_drawing, self.fit_table):
            b.setAutoRaise(True)
            b.setFocusPolicy(Qt.TabFocus)
            lay.addWidget(b)
        outer.addWidget(self.legend)
        self.refresh()

    def refresh(self) -> None:
        from .i18n import tr
        c = CANVAS_COLORS
        bg = QColor(c["background"])
        fg = c["cut"]                      # the theme's drawing colour: readable on the canvas
        panel = QColor(bg)
        panel.setAlpha(225)
        self.setStyleSheet(
            f"QFrame#canvas_hud {{ background: rgba({panel.red()},{panel.green()},{panel.blue()},225);"
            f" border: 1px solid {c['table']}; border-radius: 6px; }}"
            f" QFrame#canvas_hud QLabel, QFrame#canvas_hud QCheckBox, QFrame#canvas_hud QToolButton {{ color: {fg}; }}")
        items = (("lead_in", "canvas.legend.lead_in"), ("lead_out", "canvas.legend.lead_out"),
                 ("kerf", "canvas.legend.kerf"), ("gap", "canvas.legend.gap"), ("rapid", "canvas.legend.rapid"))
        self.legend.setText(" &nbsp; ".join(
            f"<span style='color:{c[role]}; font-weight:bold'>&#9473;&#9473;</span>&nbsp;"
            + tr(key).replace(" ", "&nbsp;") for role, key in items))
        self.legend.setToolTip(tr("canvas.legend.tip"))
        self.set_status(self.view.plan_state)
        self.place()

    def set_status(self, state: str) -> None:
        from .i18n import tr
        text = {"planning": tr("canvas.plan.planning"), "failed": tr("canvas.plan.failed"),
                "simulating_wait": tr("canvas.plan.sim_wait")}.get(state, "")
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.place()

    def place(self) -> None:
        """Size and position for the view's current width (content-driven: the legend shows
        only when the whole bar fits)."""
        avail = max(0, self.view.width() - 16)
        self.legend.setVisible(self.view.options.show_toolpath)
        lay = self.layout()
        lay.activate()
        # what surrounds the row: the layout's margins and the frame's own (the style sheet's
        # border), so the buttons get their full size hint
        lm, fm = lay.contentsMargins(), self.contentsMargins()
        pad = lm.left() + lm.right() + fm.left() + fm.right()
        w = min(max(lay.itemAt(0).sizeHint().width() + pad, 0), avail)
        if self.legend.isVisible():
            # the legend wraps to the bar's width; it goes when even a short line does not fit
            w = min(max(w, self.legend.sizeHint().width() + pad), avail)
        h = self.layout().heightForWidth(w) if self.layout().hasHeightForWidth() else self.sizeHint().height()
        if self.legend.isVisible() and h > self.view.height() // 3:
            self.legend.setVisible(False)
            lay.activate()
            h = self.sizeHint().height()
        self.setGeometry(8, 8, w, max(h, self.minimumSizeHint().height()))
        self.raise_()


class CanvasView(QGraphicsView):
    mouseMoved = Signal(float, float)
    selectionChanged = Signal()
    simulationFinished = Signal()
    toolpathToggled = Signal(bool)

    MODE_SELECT, MODE_PAN, MODE_NODE = "select", "pan", "node"
    tool = None            # interaction tool (set_tool); class default so early events see it

    def __init__(self, parent=None):
        self._scene = QGraphicsScene()
        super().__init__(self._scene, parent)
        self.document = None
        self.options = ViewOptions()
        self.mode = self.MODE_SELECT
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setMouseTracking(True)
        self.setBackgroundBrush(_cc("background"))
        self.setTransform(QTransform.fromScale(1, -1))
        self._scene.setSceneRect(-1e6, -1e6, 2e6, 2e6)
        self._items: list[tuple[int, int, QGraphicsPathItem, list]] = []   # (part, contour, item, pts)
        self._overlay: list[QGraphicsItem] = []
        self._raster_items: list[tuple[int, _RasterItem]] = []   # (part, item) of raster parts
        self._item_styles: list = []       # the pen key set on each of _items (refresh_styles)
        self._item_keys: list = []         # (its QPainterPath object, item class) per item
        self._tp_paths: Optional[tuple] = None
        self._overlay_gen = 0
        self._overlay_job: Optional[tuple] = None        # (generation, steps) still to build   # (toolpath item ids, (cut, gap, rapid) paths)
        self._bbox_cache: dict[int, tuple[tuple, tuple]] = {}   # part uid -> (segments, bbox)
        self._order: list[tuple[int, int]] = []
        self._pan_start: Optional[QPointF] = None
        self._press_pos = None
        self._rubber = SelectionBand(self.viewport())
        self._head = None
        self._sim_timer = QTimer(self)
        self._sim_timer.timeout.connect(self._sim_step)
        self._sim_path: list[tuple[float, float, bool]] = []
        self._sim_pos = 0
        self._sim_trail: Optional[QGraphicsPathItem] = None
        self.sim_speed = 1.0   # multiplier (mf121 "Simulation speed")
        self.toolpath: list = []                 # mlaser.cam MotionItems (set_toolpath)
        self._tp_order: list[tuple[int, int]] = []
        self._track: Optional[QGraphicsPathItem] = None   # live machine track
        self._track_path = QPainterPath()
        self._track_last = None
        self.tool = None                          # interaction tool (see set_tool)
        self._preview_items: list[QGraphicsItem] = []
        self._fit_pending: Optional[str] = "drawing"   # see _fit
        self.toolpath_marks: list[ToolpathMark] = []
        self.plan_state = "idle"                  # idle | planning | ready | failed (set_plan_state)
        self._static_items()
        self.hud = CanvasHud(self)
        from .units_qt import on_units_changed
        on_units_changed(self, lambda _u: self.viewport().update(), now=False)   # rulers relabel

    # ------------------------------------------------------------------ scene
    def _static_items(self) -> None:
        w, h = self.options.table_size
        pen = QPen(_cc("table"), 0, Qt.DashLine)
        self._table = self._scene.addRect(QRectF(0, 0, w, h), pen)
        self._table.setZValue(-10)
        ax = QPen(_cc("axis_x"), 0)
        ay = QPen(_cc("axis_y"), 0)
        self._axes = [self._scene.addLine(0, 0, 60, 0, ax), self._scene.addLine(0, 0, 0, 60, ay)]
        for a in self._axes:
            a.setZValue(-9)

    def apply_theme(self) -> None:
        """Re-read :data:`CANVAS_COLORS` / the display layer colours (theme change)."""
        self.setBackgroundBrush(_cc("background"))
        self._table.setPen(QPen(_cc("table"), 0, Qt.DashLine))
        self._axes[0].setPen(QPen(_cc("axis_x"), 0))
        self._axes[1].setPen(QPen(_cc("axis_y"), 0))
        if self._head is not None:
            self._head.setPen(QPen(_cc("head"), 1.5))
        if self._track is not None:
            pen = QPen(_cc("track"), 1.5)
            pen.setCosmetic(True)
            self._track.setPen(pen)
        self.rebuild()
        self.hud.refresh()

    def set_show_toolpath(self, on: bool) -> None:
        """The Toolpath toggle (canvas bar, View > Show > Toolpath)."""
        on = bool(on)
        if self.options.show_toolpath == on and self.hud.toggle.isChecked() == on:
            return
        self.options.show_toolpath = on
        if self.hud.toggle.isChecked() != on:
            self.hud.toggle.blockSignals(True)
            self.hud.toggle.setChecked(on)
            self.hud.toggle.blockSignals(False)
        self.rebuild()
        self.hud.place()
        self.toolpathToggled.emit(on)

    def set_plan_state(self, state: str) -> None:
        """"planning" while the plan of the current drawing is computed, "ready", "failed"
        (shown in the canvas bar; the simulation waits for "ready")."""
        self.plan_state = state
        self.hud.set_status(state)

    def toolpath_items(self, role: str) -> list:
        """The overlay items of one toolpath role ("lead_in", "lead_out", "kerf", "gap",
        "rapid", "cut", "bad", "pierce", "lead_glyph", "arrow"); tests and tools."""
        return [it for it in self._overlay if it.data(0) == "toolpath." + role]

    # ------------------------------------------------------------------ rulers / grid
    RULER_PX = 18

    def ruler_steps(self) -> tuple[float, float]:
        """(major, minor) step in mm of the rulers and the grid at the current zoom."""
        return ruler_step(1.0 / self.pixel_size())

    def drawBackground(self, painter, rect) -> None:  # noqa: N802 - Qt
        super().drawBackground(painter, rect)
        if not self.options.show_grid:
            return
        major, _minor = self.ruler_steps()
        pen = QPen(_cc("table"), 0, Qt.DotLine)
        painter.save()
        painter.setPen(pen)
        x = math.floor(rect.left() / major) * major
        while x <= rect.right():
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            x += major
        y = math.floor(rect.top() / major) * major
        while y <= rect.bottom():
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            y += major
        painter.restore()

    def ruler_labels(self) -> tuple[list[tuple[float, str]], list[tuple[float, str]], str]:
        """The major ticks shown: ([(viewport x, label)], [(viewport y, label)], unit)."""
        from mlaser.services import units as U
        u = U.current()
        major, _ = self.ruler_steps()
        vp = self.viewport().rect()
        tl, br = self.mapToScene(vp.topLeft()), self.mapToScene(vp.bottomRight())
        xs, ys = [], []
        x0, x1 = min(tl.x(), br.x()), max(tl.x(), br.x())
        y0, y1 = min(tl.y(), br.y()), max(tl.y(), br.y())
        dec = max(0, -math.floor(math.log10(u.to_display(major, "length")) + 1e-9)) + 2
        k = math.floor(x0 / major)
        while k * major <= x1 and len(xs) < 400:
            v = k * major
            xs.append((self.mapFromScene(QPointF(v, 0)).x(), u.number(v, "length", dec, trim=True)))
            k += 1
        k = math.floor(y0 / major)
        while k * major <= y1 and len(ys) < 400:
            v = k * major
            ys.append((self.mapFromScene(QPointF(0, v)).y(), u.number(v, "length", dec, trim=True)))
            k += 1
        return xs, ys, u.suffix("length")

    def drawForeground(self, painter, rect) -> None:  # noqa: N802 - Qt
        super().drawForeground(painter, rect)
        if not self.options.show_rulers:
            return
        major, minor = self.ruler_steps()
        xs, ys, unit = self.ruler_labels()
        vp = self.viewport().rect()
        r = self.RULER_PX
        bg = QColor(_cc("background"))
        bg.setAlpha(210)
        fg = _cc("table").lighter(160)
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.fillRect(QRectF(0, 0, vp.width(), r), bg)
        painter.fillRect(QRectF(0, 0, r, vp.height()), bg)
        f = QFont(painter.font())
        f.setPointSizeF(max(6.0, f.pointSizeF() * 0.75))
        painter.setFont(f)
        painter.setPen(QPen(fg, 1))
        painter.drawLine(QPointF(r, r), QPointF(vp.width(), r))
        painter.drawLine(QPointF(r, r), QPointF(r, vp.height()))
        n = max(1, int(round(major / minor)))
        px_minor = minor / self.pixel_size()
        for px, label in xs:
            if px_minor >= 4:
                for i in range(1, n):
                    mx = px + i * px_minor
                    painter.drawLine(QPointF(mx, r - 4), QPointF(mx, r))
            if px >= r:
                painter.drawLine(QPointF(px, 0), QPointF(px, r))
                painter.drawText(QPointF(px + 2, r - 6), label)
        for py, label in ys:
            if px_minor >= 4:
                for i in range(1, n):
                    my = py - i * px_minor
                    painter.drawLine(QPointF(r - 4, my), QPointF(r, my))
            if py >= r:
                painter.drawLine(QPointF(0, py), QPointF(r, py))
                painter.save()
                painter.translate(r - 6, py - 2)
                painter.rotate(-90)
                painter.drawText(QPointF(0, 0), label)
                painter.restore()
        painter.fillRect(QRectF(0, 0, r, r), bg)
        painter.drawText(QRectF(0, 0, r, r), Qt.AlignCenter, unit)
        painter.restore()

    def set_table_size(self, w: float, h: float) -> None:
        self.options.table_size = (w, h)
        self._table.setRect(QRectF(0, 0, w, h))

    def set_document(self, doc) -> None:
        self.stop_simulation()
        self.document = doc
        self.toolpath, self._tp_order = [], []
        self.rebuild()
        self.zoom_fit()

    def contour_order(self) -> list[tuple[int, int]]:
        """Cutting order as (part index, contour index). Uses doc.extra['order'] if a CAM
        sorter stored one, else document order with inner contours before the outer one
        (an outer contour is assumed to be the part's longest)."""
        doc = self.document
        if doc is None:
            return []
        if self._tp_order:
            n = len(doc.parts)
            valid = [o for o in self._tp_order if o[0] < n and o[1] < len(doc.parts[o[0]].contours)]
            if valid:
                return valid
        order = doc.extra.get("order") if isinstance(getattr(doc, "extra", None), dict) else None
        if order:
            return [tuple(o) for o in order]
        out = []
        for pi, part in enumerate(doc.parts):
            idx = list(range(len(part.contours)))
            if len(idx) > 1:
                outer = max(idx, key=lambda i: part.contours[i].length())
                idx.remove(outer)
                idx.append(outer)
            out.extend((pi, ci) for ci in idx)
        return out

    def rebuild(self) -> None:
        """Show the document.  Incremental: a contour whose display path is unchanged (the
        path cache returns the same QPainterPath for the same segment values) keeps its scene
        item and pen; only changed, added and removed contours touch the scene.  A one-part
        edit of a 2,000-part drawing re-created 5,000 items before (review 2 §1.2)."""
        for it in self._overlay:
            self._scene.removeItem(it)
        for _, rit in self._raster_items:
            self._scene.removeItem(rit)
        self._overlay.clear()
        self._raster_items.clear()
        old_items, old_styles, old_keys = self._items, self._item_styles, self._item_keys
        self._items, self._item_styles, self._item_keys = [], [], []
        doc = self.document
        if doc is None:
            for _, _, it, _ in old_items:
                self._scene.removeItem(it)
            self._overlay_job = None
            return
        self._order = self.contour_order()
        many = sum(len(p.contours) for p in doc.parts) > LOD_CONTOURS
        item_cls = _LodPathItem if many else QGraphicsPathItem
        pool: dict = {}
        # keyed by the identity of the path objects, which old_keys keeps alive meanwhile
        for (_, _, it, _), (opath, ocls), style in zip(old_items, old_keys, old_styles):
            pool.setdefault((id(opath), ocls), []).append((it, style))
        for pi, part in enumerate(doc.parts):
            if isinstance(part.extra, dict) and "raster" in part.extra:
                rit = _RasterItem(part, display_layer_color(part.craft.layer))
                if rit.raster is not None:
                    self._scene.addItem(rit)
                    self._raster_items.append((pi, rit))
            for ci, c in enumerate(part.contours):
                path, pts = _contour_path(c)
                key = (id(path), item_cls)
                free = pool.get(key)
                if free:
                    it, style = free.pop()
                else:
                    it, style = item_cls(path), None
                    it.setZValue(1)
                    self._scene.addItem(it)
                self._items.append((pi, ci, it, pts))
                self._item_keys.append((path, item_cls))
                self._item_styles.append(style)
        for free in pool.values():
            for it, _ in free:
                self._scene.removeItem(it)
        self.refresh_styles()
        self._build_overlay()

    def set_toolpath(self, items) -> None:
        """Show the real CAM toolpath (mlaser.cam.build_toolpath): cut moves including leads and
        kerf offset, rapids dashed when ``show_move_path``; its contour order drives the index
        numbers and the simulation."""
        self.toolpath = list(items or [])
        self._tp_order = [(m.part, m.contour) for m in self.toolpath
                          if type(m).__name__ == "Marker" and getattr(m, "kind", "") == "contour"]
        self.rebuild()

    def _toolpath_overlay(self) -> None:
        """The planned toolpath, drawn over the drawing so every CAM change shows: lead-ins and
        lead-outs in their own colours, the kerf-compensated path of compensated contours (the
        drawing stays in its layer colour underneath), micro-joint gaps (laser off), the
        lead-in the lead check flagged (red) and the rapids (dashed).  The paths are cached for
        the toolpath's items (a rebuild after an edit redraws the same toolpath until the new
        plan arrives); arcs are drawn with ``arcTo`` (review 2 P2-4)."""
        rasters = tuple(sorted(pi for pi, _ in self._raster_items))
        flagged = frozenset(self._lead_flags())
        kerfed = frozenset(self._kerf_contours())
        louts = self._lead_out_counts()
        tp = self.toolpath
        hit = self._tp_paths
        key = (rasters, flagged, kerfed, louts)
        if hit is not None and hit[0] is tp and hit[1] == len(tp) and hit[2] == key:
            paths, marks = hit[3]
        else:
            paths, marks = self._toolpath_paths(tp, set(rasters), flagged, kerfed, dict(louts))
            self._tp_paths = (tp, len(tp), key, (paths, marks))
        self.toolpath_marks = marks
        show_tp = self.options.show_toolpath
        for name, z, width, style, show in (
                ("rapid", 0.4, 0, Qt.DashLine, self.options.show_move_path and show_tp),
                ("cut", 0.5, 0, Qt.SolidLine, True),
                ("kerf", 1.5, 1.5, Qt.SolidLine, show_tp),
                ("gap", 1.6, 2.0, Qt.SolidLine, show_tp),
                ("lead_out", 2.0, 2.0, Qt.SolidLine, show_tp),
                ("lead_in", 2.1, 2.0, Qt.SolidLine, show_tp),
                ("bad", 2.2, 2.0, Qt.SolidLine, True)):
            path = paths[name]
            if not show or path.isEmpty():
                continue
            color = CANVAS_COLORS["unclosed"] if name == "bad" else CANVAS_COLORS[name]
            if name == "kerf":
                # the kerf: a translucent band along the compensated path (the material the beam
                # removes), so it reads as "offset path" even where it lies on the drawing
                band = QGraphicsPathItem(path)
                bc = QColor(color)
                bc.setAlpha(80)
                bpen = QPen(bc, 6.0)
                bpen.setCosmetic(True)
                bpen.setCapStyle(Qt.RoundCap)
                bpen.setJoinStyle(Qt.RoundJoin)
                band.setPen(bpen)
                band.setZValue(z - 0.05)
                band.setData(0, "toolpath.kerf_band")
                self._add_overlay(band)
            it = QGraphicsPathItem(path)
            pen = QPen(QColor(color), width, style)
            pen.setCosmetic(True)
            pen.setCapStyle(Qt.RoundCap)
            it.setPen(pen)
            it.setZValue(z)
            it.setData(0, "toolpath." + name)
            self._add_overlay(it)

    def _kerf_contours(self) -> set:
        """(part, contour) keys with kerf compensation (inside or outside, width > 0): their
        planned path is drawn in the kerf colour."""
        doc = getattr(self, "document", None)
        out: set = set()
        for pi, part in enumerate(getattr(doc, "parts", None) or []):
            pc = getattr(part, "craft", None)
            for ci, c in enumerate(part.contours):
                cr = c.craft if c.craft is not None else pc
                if cr is not None and int(getattr(cr, "kerf_comp", -1)) in (0, 1) and \
                        float(getattr(cr, "kerf_width", 0.0) or 0.0) > 0:
                    out.add((pi, ci))
        return out

    def _lead_out_counts(self) -> tuple:
        """((part, contour), n) of the contours with a lead-out: the number of moves the CAM
        emits for it (:func:`mlaser.cam.leads.lead_out`, the same for any start point), the
        trailing cut moves of the contour's block."""
        doc = getattr(self, "document", None)
        out = []
        for pi, part in enumerate(getattr(doc, "parts", None) or []):
            pc = getattr(part, "craft", None)
            for ci, c in enumerate(part.contours):
                cr = c.craft if c.craft is not None else pc
                lo = getattr(cr, "lead_out", None)
                if lo is None or not lo.kind or not c.closed:
                    continue
                n = _lead_out_moves(lo)
                if n:
                    out.append(((pi, ci), n))
        return tuple(out)

    def _lead_flags(self) -> set:
        """(part, contour) keys whose lead-in carries the lead-check error mark
        (``Craft.lead_error``, drawn red like the original's 0x100659e0)."""
        doc = getattr(self, "document", None)
        out: set = set()
        for pi, part in enumerate(getattr(doc, "parts", None) or []):
            pc = getattr(part, "craft", None)
            for ci, c in enumerate(part.contours):
                cr = c.craft if c.craft is not None else pc
                if getattr(cr, "lead_error", False):
                    out.add((pi, ci))
        return out

    @staticmethod
    def _toolpath_paths(toolpath, rasters: set, flagged=frozenset(), kerfed=frozenset(),
                        lead_outs: Optional[dict] = None) -> tuple[dict, list]:
        """({role: QPainterPath}, marks) of a toolpath.  Roles: ``cut``, ``kerf`` (cut moves of
        compensated contours), ``gap`` (laser off), ``lead_in``, ``lead_out``, ``bad`` (flagged
        lead-ins), ``rapid``.  ``marks``: one :class:`ToolpathMark` per contour (pierce point,
        contour start, cut direction, lead-in length) for the markers."""
        paths = {k: QPainterPath() for k in ("cut", "kerf", "gap", "lead_in", "lead_out", "bad", "rapid")}
        marks: list[ToolpathMark] = []
        lead_outs = lead_outs or {}
        skip = False            # inside a raster part's block: its item shows the burn runs
        cur = None
        block: list = []

        def flush() -> None:
            if cur is None or not block:
                return
            n_out = lead_outs.get(cur, 0)
            n_in = sum(1 for m in block if getattr(m, "lead", None) == "in")
            if n_in + n_out >= len(block):
                n_out = max(0, len(block) - n_in - 1)
            lead_len = 0.0
            for k, m in enumerate(block):
                if k < n_in and getattr(m, "lead", None) == "in":
                    role = "bad" if cur in flagged else "lead_in"
                    lead_len += _move_length(m)
                elif k >= len(block) - n_out:
                    role = "lead_out"
                elif not getattr(m, "laser", True):
                    role = "gap"
                else:
                    role = "kerf" if cur in kerfed else "cut"
                path = paths[role]
                path.moveTo(*m.start)
                if type(m).__name__ == "CutArc":
                    _arc_to(path, m.start, m.end, m.center, m.ccw)
                else:
                    path.lineTo(*m.end)
            body = block[n_in] if n_in < len(block) else block[-1]
            marks.append(ToolpathMark(cur[0], cur[1], tuple(block[0].start), tuple(body.start),
                                      _move_direction(body), lead_len if n_in else 0.0,
                                      _move_direction(block[n_in - 1], end=True) if n_in else None))

        for m in toolpath:
            name = type(m).__name__
            if name == "Marker":
                flush()
                block = []
                skip = getattr(m, "part", -1) in rasters
                cur = ((getattr(m, "part", -1), getattr(m, "contour", -1))
                       if getattr(m, "kind", "") == "contour" else None)
                continue
            if skip:
                continue
            if name == "Rapid":
                paths["rapid"].moveTo(*m.start)
                paths["rapid"].lineTo(*m.end)
            elif name in ("CutLine", "CutArc"):
                if cur is None:
                    path = paths["cut" if getattr(m, "laser", True) else "gap"]
                    path.moveTo(*m.start)
                    if name == "CutArc":
                        _arc_to(path, m.start, m.end, m.center, m.ccw)
                    else:
                        path.lineTo(*m.end)
                else:
                    block.append(m)
        flush()
        return paths, marks

    def add_track_point(self, x: float, y: float, cutting: bool) -> None:
        """Live machine track while a job runs (cut segments solid, moves skipped)."""
        if self._track is None:
            self._track = QGraphicsPathItem()
            pen = QPen(_cc("track"), 1.5)
            pen.setCosmetic(True)
            self._track.setPen(pen)
            self._track.setZValue(9)
            self._scene.addItem(self._track)
        if self._track_last is None or not cutting:
            self._track_path.moveTo(x, y)
        else:
            self._track_path.lineTo(x, y)
        self._track_last = (x, y)
        self._track.setPath(self._track_path)

    def clear_track(self) -> None:
        self._track_path = QPainterPath()
        self._track_last = None
        if self._track is not None:
            self._track.setPath(self._track_path)

    def refresh_styles(self) -> None:
        """Pens of the contour items; an item whose style (colour, selected) is unchanged is not
        touched (a selection change of a few parts does not re-pen 25,000 items)."""
        doc = self.document
        if doc is None:
            return
        pens: dict = {}
        styles = self._item_styles
        if len(styles) != len(self._items):
            styles[:] = [None] * len(self._items)
        sel_rgba = _cc("selected").rgba()
        for k, (pi, ci, it, _) in enumerate(self._items):
            part = doc.parts[pi]
            if part.selected:
                key = (True, sel_rgba)
            else:
                c = part.contours[ci]
                color = _cc("unclosed") if self.options.unclosed_red and not c.closed else \
                    display_layer_color(part.craft.layer)
                key = (False, color.rgba())
            if styles[k] == key:
                continue
            pen = pens.get(key)
            if pen is None:
                if key[0]:
                    pen = QPen(_cc("selected"), 1.5, Qt.DashLine)
                else:
                    pen = QPen(QColor.fromRgba(key[1]), 0)
                pen.setCosmetic(True)
                pens[key] = pen
            it.setPen(pen)
            styles[k] = key
        for pi, rit in self._raster_items:
            part = doc.parts[pi]
            color = display_layer_color(part.craft.layer)
            rit.set_color(color.lighter(160) if part.selected else color)

    def _add_overlay(self, item: QGraphicsItem) -> None:
        self._scene.addItem(item) if item.scene() is None else None
        self._overlay.append(item)

    def _build_overlay(self) -> None:
        """Start (re)building the overlay: what fits in :data:`OVERLAY_SLICE_S` now, the rest in
        later event-loop turns (:meth:`_overlay_run`), so a new toolpath or an edit of a large
        drawing never holds the GUI thread for the whole overlay (several markers per contour;
        review 2 P1-1: a Stop waits for the GUI thread).  The toolpath paths and
        ``toolpath_marks`` are built at once; :meth:`finish_overlay` completes the rest now."""
        self._overlay_gen += 1
        self._overlay_job = (self._overlay_gen, self._overlay_steps())
        if len(self._items) <= OVERLAY_SYNC_CONTOURS:
            self.finish_overlay()        # a small drawing: complete at once (deterministic)
            return
        self._overlay_run(self._overlay_gen)

    def _overlay_run(self, gen: int) -> None:
        job = self._overlay_job
        if job is None or job[0] != gen:
            return                       # superseded by a later rebuild
        t_end = time.perf_counter() + OVERLAY_SLICE_S
        for _ in job[1]:
            if time.perf_counter() > t_end:
                QTimer.singleShot(0, self, lambda g=gen: self._overlay_run(g))
                return
        self._overlay_job = None

    def finish_overlay(self) -> None:
        """Complete a pending overlay build now (screenshots, tests)."""
        job, self._overlay_job = self._overlay_job, None
        if job is not None:
            for _ in job[1]:
                pass

    @property
    def overlay_pending(self) -> bool:
        return self._overlay_job is not None

    def _overlay_steps(self):
        """The overlay, a generator: it yields between contours (see :meth:`_build_overlay`)."""
        doc = self.document
        o = self.options
        prev_end = None
        self.toolpath_marks = []
        if self.toolpath:
            self._toolpath_overlay()
        if len(self._items) > LOD_CONTOURS:
            return          # level of detail: no per-contour markers / labels / arrows / boxes
        if self.toolpath and o.show_toolpath:
            yield from self._toolpath_markers()
        else:
            lookup = {(pi, ci): pts for pi, ci, _, pts in self._items}
            for n, (pi, ci) in enumerate(self._order, start=1):
                yield
                pts = lookup.get((pi, ci))
                if not pts:
                    continue
                part = doc.parts[pi]
                sx, sy = pts[0]
                if o.show_start:
                    self._add_overlay(_Marker(sx, sy, 2.5, _cc("start")))
                if o.show_index:
                    self._add_overlay(self._index_label(n, sx, sy))
                if o.show_direction and len(pts) > 1 and not self.toolpath:
                    self._add_overlay(self._arrow(pts))
                lead = getattr(part.craft, "lead_in", None)
                if lead is not None and lead.kind and len(pts) > 1 and not self.toolpath:
                    self._add_overlay(self._lead_item(pts, lead, _cc("lead_in")))
                if o.show_move_path and prev_end is not None and not self.toolpath:
                    pen = QPen(_cc("rapid"), 0, Qt.DashLine)
                    pen.setCosmetic(True)
                    ln = QGraphicsLineItem(prev_end[0], prev_end[1], sx, sy)
                    ln.setPen(pen)
                    ln.setZValue(0)
                    self._add_overlay(ln)
                prev_end = pts[-1] if not doc.parts[pi].contours[ci].closed else pts[0]
        if o.show_bbox:
            for part in doc.parts:
                yield
                if part.contours:
                    x0, y0, x1, y1 = part.bbox()
                    r = QGraphicsRectItem(QRectF(x0, y0, x1 - x0, y1 - y0))
                    pen = QPen(_cc("bbox"), 0, Qt.DotLine)
                    pen.setCosmetic(True)
                    r.setPen(pen)
                    self._add_overlay(r)

    def _toolpath_markers(self):
        """Per-contour markers of the plan: pierce ring, contour start, cut order number,
        direction arrow and the minimum-size lead glyph (tiny leads stay visible)."""
        o = self.options
        lead_c = _cc("lead_in")
        for n, mk in enumerate(self.toolpath_marks, start=1):
            yield
            sx, sy = mk.start
            if mk.lead_length > 0:
                self._add_overlay(_Ring(mk.pierce[0], mk.pierce[1], 3.5, _cc("pierce")))
                self._add_overlay(_LeadGlyph(self, mk, lead_c))
            if o.show_start:
                self._add_overlay(_Marker(sx, sy, 2.5, _cc("start")))
            if o.show_index:
                self._add_overlay(self._index_label(n, sx, sy))
            if o.show_direction:
                self._add_overlay(self._arrow_at(mk.start, mk.direction))

    @staticmethod
    def _index_label(n: int, x: float, y: float) -> QGraphicsSimpleTextItem:
        t = QGraphicsSimpleTextItem(str(n))
        t.setBrush(QBrush(_cc("index")))
        t.setFont(_label_font())
        t.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        t.setPos(x, y)
        t.setZValue(6)
        return t

    @staticmethod
    def _arrow_at(p, d) -> QGraphicsPathItem:
        """Direction arrow (fixed pixel size) a little after ``p`` along the cut direction ``d``."""
        ang = math.atan2(d[1], d[0])
        path = QPainterPath()
        L = 9.0
        path.moveTo(0, 0)
        path.lineTo(-L * math.cos(0.45), L * math.sin(0.45))
        path.moveTo(0, 0)
        path.lineTo(-L * math.cos(0.45), -L * math.sin(0.45))
        path.moveTo(0, 0)
        path.lineTo(-1.6 * L, 0)
        it = QGraphicsPathItem(path)
        it.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        it.setPos(p[0], p[1])
        it.setRotation(-math.degrees(ang))
        it.setTransform(QTransform.fromTranslate(16, 0), False)
        pen = QPen(_cc("arrow"), 1.2)
        it.setPen(pen)
        it.setZValue(6)
        it.setData(0, "toolpath.arrow")
        return it

    @staticmethod
    def _arrow(pts) -> QGraphicsPathItem:
        (x0, y0), (x1, y1) = pts[0], pts[1]
        ang = math.atan2(y1 - y0, x1 - x0)
        path = QPainterPath()
        L = 10.0
        path.moveTo(0, 0)
        path.lineTo(-L * math.cos(0.4), L * math.sin(0.4))
        path.moveTo(0, 0)
        path.lineTo(-L * math.cos(0.4), -L * math.sin(0.4))
        it = QGraphicsPathItem(path)
        it.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        it.setPos((x0 + x1) / 2, (y0 + y1) / 2)
        it.setRotation(-math.degrees(ang))
        pen = QPen(_cc("arrow"), 1)
        it.setPen(pen)
        it.setZValue(6)
        return it

    @staticmethod
    def _lead_item(pts, lead, color: QColor) -> QGraphicsPathItem:
        """Preview of a straight lead-in ending at the contour start.

        Approximation for display only (real geometry comes from mlaser.cam): the lead
        approaches at ``angle_deg`` to the first segment, from the left-hand side.
        """
        (x0, y0), (x1, y1) = pts[0], pts[1]
        t = math.atan2(y1 - y0, x1 - x0)
        a = t + math.pi - math.radians(lead.angle_deg)
        L = max(lead.length, 0.1)
        path = QPainterPath()
        path.moveTo(x0 + L * math.cos(a), y0 + L * math.sin(a))
        path.lineTo(x0, y0)
        it = QGraphicsPathItem(path)
        pen = QPen(color.lighter(130), 0)
        pen.setCosmetic(True)
        it.setPen(pen)
        it.setZValue(2)
        return it

    # ------------------------------------------------------------------ view
    def zoom(self, factor: float) -> None:
        self._fit_pending = None          # the user chose a zoom: a resize keeps it
        self.scale(factor, factor)

    def zoom_fit(self) -> None:
        """Fit the drawing (the machine table when the document is empty)."""
        self._fit("drawing")

    def zoom_fit_table(self) -> None:
        """Fit the whole machine table."""
        self._fit("table")

    def fit_rect(self, kind: str = "drawing") -> QRectF:
        doc = self.document
        if kind == "drawing" and doc is not None and doc.parts:
            x0, y0, x1, y1 = doc.bbox()
        else:
            w, h = self.options.table_size
            x0, y0, x1, y1 = 0, 0, w, h
        w, h = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
        m = 0.08 * max(w, h)
        return QRectF(x0 - m, y0 - m, w + 2 * m, h + 2 * m)

    def _fit(self, kind: str) -> None:
        """Fit and keep fitting: the fit is re-applied when the view is resized (the window is
        laid out, a dock or splitter moves, the workspace changes) until the user zooms or
        pans.  A fit made before the view had its size (a document opened before the window
        was shown, on macOS before the window's final geometry) used to leave the drawing a
        few pixels wide."""
        r = self.fit_rect(kind)
        vp = self.viewport().rect()
        hud = getattr(self, "hud", None)
        top = hud.geometry().bottom() + 4 if hud is not None and hud.isVisible() else 0
        top = min(top, vp.height() // 3)       # the canvas bar's band is kept free
        s = min(max(vp.width() - 2, 1) / r.width(), max(vp.height() - top - 2, 1) / r.height())
        s = max(s, 1e-9)
        self.setTransform(QTransform.fromScale(s, -s))
        c = r.center()
        self.centerOn(QPointF(c.x(), c.y() + top / (2 * s)))
        self._fit_pending = kind

    @property
    def fit_mode(self) -> Optional[str]:
        """"drawing" / "table" while the view follows a fit, None after a user zoom or pan."""
        return self._fit_pending

    def resizeEvent(self, ev) -> None:   # noqa: N802 - Qt
        super().resizeEvent(ev)
        if self._fit_pending:
            self._fit(self._fit_pending)
        if getattr(self, "hud", None) is not None:
            self.hud.place()

    def showEvent(self, ev) -> None:   # noqa: N802 - Qt
        super().showEvent(ev)
        if self._fit_pending:
            self._fit(self._fit_pending)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.setCursor(Qt.OpenHandCursor if mode == self.MODE_PAN else Qt.ArrowCursor)

    def set_head_position(self, x: float, y: float) -> None:
        if self._head is None:
            pen = QPen(_cc("head"), 1.5)
            path = QPainterPath()
            path.addEllipse(QPointF(0, 0), 6, 6)
            path.moveTo(-10, 0); path.lineTo(10, 0)
            path.moveTo(0, -10); path.lineTo(0, 10)
            self._head = QGraphicsPathItem(path)
            self._head.setPen(pen)
            self._head.setFlag(QGraphicsItem.ItemIgnoresTransformations)
            self._head.setZValue(20)
            self._scene.addItem(self._head)
        self._head.setPos(x, y)

    # ------------------------------------------------------------------ selection
    def pick(self, sx: float, sy: float, tol_px: float = 5.0) -> Optional[int]:
        """Return the part index nearest to scene point (sx, sy) within ``tol_px``."""
        tol = tol_px / max(abs(self.transform().m11()), 1e-9)
        best, best_d = None, tol
        for pi, ci, it, pts in self._items:
            br = it.boundingRect().adjusted(-tol, -tol, tol, tol)
            if not br.contains(QPointF(sx, sy)):
                continue
            closed = self.document.parts[pi].contours[ci].closed
            seq = pts + ([pts[0]] if closed and pts else [])
            for a, b in zip(seq, seq[1:]):
                d = _seg_dist((sx, sy), a, b)
                if d < best_d:
                    best, best_d = pi, d
        if best is None:
            for pi, rit in self._raster_items:
                if rit.contains_scene(sx, sy):
                    return pi
        return best

    def select_parts(self, indices, add: bool = False) -> None:
        if self.document is None:
            return
        idx = set(indices)
        for i, p in enumerate(self.document.parts):
            p.selected = (p.selected and add) or i in idx
        self.refresh_styles()
        self.selectionChanged.emit()

    def selected_indices(self) -> list[int]:
        if self.document is None:
            return []
        return [i for i, p in enumerate(self.document.parts) if p.selected]

    def part_bbox(self, part) -> tuple:
        """``part.bbox()``, cached by uid while the part's segment objects are the same (the
        PartListPanel scheme: edits replace segment objects, they never mutate them)."""
        segs = tuple(sg for c in part.contours for sg in c.segments)
        hit = self._bbox_cache.get(part.uid)
        if hit is not None and len(hit[0]) == len(segs) and all(a is b for a, b in zip(hit[0], segs)):
            return hit[1]
        bb = part.bbox()
        self._bbox_cache[part.uid] = (segs, bb)
        return bb

    def _rubber_select(self, r: QRectF, crossing: bool, add: bool) -> None:
        hits = []
        parts = self.document.parts
        if len(self._bbox_cache) > 2 * max(len(parts), 64):
            live = {p.uid for p in parts}
            self._bbox_cache = {u: v for u, v in self._bbox_cache.items() if u in live}
        for i, part in enumerate(parts):
            if not part.contours:
                continue
            x0, y0, x1, y1 = self.part_bbox(part)
            pr = QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized()
            if (crossing and r.intersects(pr)) or (not crossing and r.contains(pr)):
                hits.append(i)
        self.select_parts(hits, add)

    # ------------------------------------------------------------------ events
    def wheelEvent(self, ev) -> None:
        if ev.angleDelta().y() == 0:
            return
        f = 1.25 if ev.angleDelta().y() > 0 else 0.8
        self.zoom(f)

    def mousePressEvent(self, ev) -> None:
        if self.tool is not None and ev.button() in (Qt.LeftButton, Qt.RightButton):
            sp = self.mapToScene(ev.position().toPoint())
            self._tool_call("press", sp.x(), sp.y(), _button(ev.button()), _mods(ev.modifiers()))
            return
        if ev.button() == Qt.MiddleButton or (ev.button() == Qt.LeftButton and self.mode == self.MODE_PAN):
            self._pan_start = ev.position()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if ev.button() == Qt.LeftButton:
            self._press_pos = ev.position().toPoint()
            self._rubber.start(self._press_pos)
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:
        sp = self.mapToScene(ev.position().toPoint())
        self.mouseMoved.emit(sp.x(), sp.y())
        if self.tool is not None and self._pan_start is None:
            self._tool_call("move", sp.x(), sp.y(), _mods(ev.modifiers()))
            return
        if self._pan_start is not None:
            d = ev.position() - self._pan_start
            self._pan_start = ev.position()
            if d.manhattanLength() > 0:
                self._fit_pending = None
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(d.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(d.y()))
            return
        if self._press_pos is not None:
            self._rubber.drag(ev.position().toPoint())
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:
        if self._pan_start is not None:
            self._pan_start = None
            self.set_mode(self.mode)
            if self.tool is not None:
                self.setCursor(Qt.CrossCursor)
            return
        if self.tool is not None and ev.button() in (Qt.LeftButton, Qt.RightButton):
            sp = self.mapToScene(ev.position().toPoint())
            self._tool_call("release", sp.x(), sp.y(), _button(ev.button()), _mods(ev.modifiers()))
            return
        if self._press_pos is not None and ev.button() == Qt.LeftButton:
            self._rubber.finish()
            add = bool(ev.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier))
            end = ev.position().toPoint()
            p0 = self._press_pos
            self._press_pos = None
            if self.document is None:
                return
            if (end - p0).manhattanLength() < 4:
                sp = self.mapToScene(end)
                hit = self.pick(sp.x(), sp.y())
                if hit is None:
                    if not add:
                        self.select_parts([])
                elif add and self.document.parts[hit].selected:
                    self.document.parts[hit].selected = False
                    self.refresh_styles()
                    self.selectionChanged.emit()
                else:
                    self.select_parts([hit], add)
            else:
                a, b = self.mapToScene(p0), self.mapToScene(end)
                self._rubber_select(QRectF(a, b).normalized(), crossing=end.x() < p0.x(), add=add)
            return
        super().mouseReleaseEvent(ev)

    # ------------------------------------------------------------------ interaction tool API
    # A small hook for the drawing tools (mlaser.ui.ext_draw).  While ``self.tool`` is set,
    # left/right mouse presses, releases, moves and key presses go to it in *model*
    # coordinates (scene units = mm; Y up) instead of the selection logic; the middle button
    # and the wheel still pan/zoom.  The tool object may implement any of
    #     press(x, y, button, mods)      button: "left" | "right"; mods: set of "shift"/"ctrl"/"alt"
    #     move(x, y, mods)
    #     release(x, y, button, mods)
    #     key(name, mods) -> bool        name: "escape", "return", "backspace", "delete",
    #                                    "left"/"right"/"up"/"down" or the typed text (lower case)
    #     cancel()                       called on Esc when key() did not consume it, and by
    #                                    set_tool() when another tool replaces this one
    # Esc/Enter/Backspace are taken before the window shortcuts while a tool is active, and
    # the right-click context menu is suppressed.  Without a tool nothing changes.
    def set_tool(self, tool) -> None:
        """Install (or with ``None`` remove) the interaction tool; clears the preview."""
        old, self.tool = self.tool, tool
        if old is not None and old is not tool and hasattr(old, "cancel"):
            try:
                old.cancel()
            except Exception:  # noqa: BLE001 - a tool must not break the view
                pass
        self.clear_preview()
        self.setCursor(Qt.CrossCursor if tool is not None else
                       (Qt.OpenHandCursor if self.mode == self.MODE_PAN else Qt.ArrowCursor))

    def pixel_size(self) -> float:
        """Model units (mm) per screen pixel at the current zoom."""
        return 1.0 / max(abs(self.transform().m11()), 1e-12)

    def snap_point(self, x: float, y: float, tol_px: float = 8.0, extra=()):
        """Object snap (mlaser.cad.snap): ((x, y), kind) of the nearest end / mid / centre /
        quadrant point within ``tol_px`` pixels, or ((x, y), None)."""
        if self.document is None:
            return (x, y), None
        from mlaser.cad.snap import snap
        hit = snap(self.document.parts, (x, y), tol_px * self.pixel_size(), extra)
        return (hit[0], hit[1]) if hit else ((x, y), None)

    def set_preview(self, polylines=(), markers=(), color: str = "") -> None:
        """Temporary overlay: ``polylines`` drawn as dashed lines (the original's yellow
        0.9/0.9/0 stipple, CADModule 0x10084000) and ``markers`` as small squares
        (snap indicators).  Replaces the previous preview."""
        self.clear_preview()
        pen = QPen(QColor(color or CANVAS_COLORS["preview"]), 1, Qt.DashLine)
        pen.setCosmetic(True)
        for pts in polylines:
            pts = list(pts)
            if len(pts) < 2:
                continue
            path = QPainterPath()
            path.moveTo(*pts[0])
            for p in pts[1:]:
                path.lineTo(*p)
            it = QGraphicsPathItem(path)
            it.setPen(pen)
            it.setZValue(30)
            self._scene.addItem(it)
            self._preview_items.append(it)
        mpen = QPen(_cc("marker"), 1.5)
        for (mx, my) in markers:
            it = QGraphicsRectItem(-5, -5, 10, 10)
            it.setPen(mpen)
            it.setFlag(QGraphicsItem.ItemIgnoresTransformations)
            it.setPos(mx, my)
            it.setZValue(31)
            self._scene.addItem(it)
            self._preview_items.append(it)

    def clear_preview(self) -> None:
        for it in self._preview_items:
            if it.scene() is not None:
                self._scene.removeItem(it)
        self._preview_items.clear()

    def _tool_call(self, name: str, *args):
        fn = getattr(self.tool, name, None)
        if fn is None:
            return None
        return fn(*args)

    def event(self, ev) -> bool:
        if self.tool is not None:
            if ev.type() == QEvent.ShortcutOverride and ev.key() in _TOOL_KEYS:
                ev.accept()
                return True
            if ev.type() == QEvent.ContextMenu:
                ev.accept()
                return True
        return super().event(ev)

    def keyPressEvent(self, ev) -> None:
        if self.tool is not None:
            name = _TOOL_KEYS.get(ev.key()) or ev.text().lower()
            used = bool(self._tool_call("key", name, _mods(ev.modifiers()))) if name else False
            if not used and ev.key() == Qt.Key_Escape:
                tool = self.tool
                self._tool_call("cancel")
                if self.tool is tool:
                    self.set_tool(None)
            return
        super().keyPressEvent(ev)

    # ------------------------------------------------------------------ simulation
    def simulation_path(self) -> list[tuple[float, float, bool]]:
        """Flatten the cut order into (x, y, cutting) points; rapids have cutting=False.

        Uses the CAM toolpath when one is set (leads, kerf, micro-joint gaps included)."""
        out: list[tuple[float, float, bool]] = []
        if self.toolpath:
            from mlaser.model import Arc
            for m in self.toolpath:
                name = type(m).__name__
                if name == "Rapid":
                    out.append((m.end[0], m.end[1], False))
                elif name == "CutArc":
                    a = Arc(m.start, m.end, m.center, m.ccw)
                    n = max(4, min(360, int(a.length() / 0.5)))
                    out.extend((*a.point_at(k / n), bool(m.laser)) for k in range(1, n + 1))
                elif name == "CutLine":
                    out.append((m.end[0], m.end[1], bool(m.laser)))
            return out
        lookup = {(pi, ci): pts for pi, ci, _, pts in self._items}
        for pi, ci in self._order:
            pts = lookup.get((pi, ci)) or []
            if not pts:
                continue
            closed = self.document.parts[pi].contours[ci].closed
            out.append((pts[0][0], pts[0][1], False))
            out.extend((x, y, True) for x, y in pts[1:])
            if closed:
                out.append((pts[0][0], pts[0][1], True))
        return out

    def start_simulation(self, interval_ms: int = 15, path=None) -> bool:
        """Animate the head along ``path`` ((x, y, cutting) points, e.g. time samples from
        mlaser.motion.simulate) or along :meth:`simulation_path`."""
        if self.document is None:
            return False
        self.stop_simulation()
        self._sim_path = list(path) if path is not None else self.simulation_path()
        if not self._sim_path:
            return False
        self._sim_pos = 0
        self._sim_trail = QGraphicsPathItem()
        pen = QPen(_cc("sim"), 2)
        pen.setCosmetic(True)
        self._sim_trail.setPen(pen)
        self._sim_trail.setZValue(10)
        self._scene.addItem(self._sim_trail)
        self._sim_draw = QPainterPath()
        self._sim_timer.start(interval_ms)
        return True

    def stop_simulation(self, clear: bool = False) -> None:
        self._sim_timer.stop()
        if clear and self._sim_trail is not None:
            self._scene.removeItem(self._sim_trail)
            self._sim_trail = None

    def simulation_running(self) -> bool:
        return self._sim_timer.isActive()

    def _sim_step(self) -> None:
        n = max(1, int(4 * self.sim_speed))
        for _ in range(n):
            if self._sim_pos >= len(self._sim_path):
                self._sim_timer.stop()
                self.simulationFinished.emit()
                return
            x, y, cut = self._sim_path[self._sim_pos]
            if cut and self._sim_pos > 0:
                self._sim_draw.lineTo(x, y)
            else:
                self._sim_draw.moveTo(x, y)
            self._sim_pos += 1
        self._sim_trail.setPath(self._sim_draw)
        self.set_head_position(x, y)
