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
import os
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPolygonF,
                           QTransform)
from PySide6.QtWidgets import (QGraphicsEllipseItem, QGraphicsItem, QGraphicsLineItem,
                               QGraphicsPathItem, QGraphicsRectItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView, QRubberBand,
                               QStyleOptionGraphicsItem)

RES_DIR = os.path.join(os.path.dirname(__file__), "resources")

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
    "sim": "#ffffff",
}
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


@dataclass
class ViewOptions:
    show_bbox: bool = False
    unclosed_red: bool = True
    show_index: bool = True
    show_start: bool = True
    show_direction: bool = False
    show_move_path: bool = False
    background: str = "#000000"   # SP.DrawblkColor "Drawing area color" (default unverified)
    table_size: tuple[float, float] = (3000.0, 1500.0)


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


class CanvasView(QGraphicsView):
    mouseMoved = Signal(float, float)
    selectionChanged = Signal()
    simulationFinished = Signal()

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
        self._bbox_cache: dict[int, tuple[tuple, tuple]] = {}   # part uid -> (segments, bbox)
        self._order: list[tuple[int, int]] = []
        self._pan_start: Optional[QPointF] = None
        self._press_pos = None
        self._rubber = QRubberBand(QRubberBand.Rectangle, self.viewport())
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
        self._static_items()

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
        for _, _, it, _ in self._items:
            self._scene.removeItem(it)
        for it in self._overlay:
            self._scene.removeItem(it)
        for _, rit in self._raster_items:
            self._scene.removeItem(rit)
        self._items.clear()
        self._item_styles.clear()
        self._overlay.clear()
        self._raster_items.clear()
        doc = self.document
        if doc is None:
            return
        self._order = self.contour_order()
        many = sum(len(p.contours) for p in doc.parts) > LOD_CONTOURS
        item_cls = _LodPathItem if many else QGraphicsPathItem
        for pi, part in enumerate(doc.parts):
            if isinstance(part.extra, dict) and "raster" in part.extra:
                rit = _RasterItem(part, display_layer_color(part.craft.layer))
                if rit.raster is not None:
                    self._scene.addItem(rit)
                    self._raster_items.append((pi, rit))
            for ci, c in enumerate(part.contours):
                path, pts = _contour_path(c)
                it = item_cls(path)
                it.setZValue(1)
                self._scene.addItem(it)
                self._items.append((pi, ci, it, pts))
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
        from mlaser.model import Arc
        cut, gap, rap = QPainterPath(), QPainterPath(), QPainterPath()
        pos = None
        rasters = {pi for pi, _ in self._raster_items}
        skip = False            # inside a raster part's block: its item shows the burn runs
        for m in self.toolpath:
            name = type(m).__name__
            if name == "Marker":
                skip = getattr(m, "part", -1) in rasters
                continue
            if skip:
                continue
            if name == "Rapid":
                rap.moveTo(*m.start)
                rap.lineTo(*m.end)
                pos = None
            elif name in ("CutLine", "CutArc"):
                path = cut if getattr(m, "laser", True) else gap
                path.moveTo(*m.start)
                if name == "CutArc":
                    a = Arc(m.start, m.end, m.center, m.ccw)
                    n = max(4, min(360, int(a.length() / 0.5)))
                    for k in range(1, n + 1):
                        path.lineTo(*a.point_at(k / n))
                else:
                    path.lineTo(*m.end)
        for path, color, style, show in ((cut, CANVAS_COLORS["cut"], Qt.SolidLine, True),
                                         (gap, CANVAS_COLORS["gap"], Qt.SolidLine, True),
                                         (rap, CANVAS_COLORS["rapid"], Qt.DashLine, self.options.show_move_path)):
            if not show or path.isEmpty():
                continue
            it = QGraphicsPathItem(path)
            pen = QPen(QColor(color), 0, style)
            pen.setCosmetic(True)
            it.setPen(pen)
            it.setZValue(0.5)
            self._add_overlay(it)

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
        doc = self.document
        o = self.options
        prev_end = None
        if self.toolpath:
            self._toolpath_overlay()
        if len(self._items) > LOD_CONTOURS:
            return          # level of detail: no per-contour markers / labels / arrows / boxes
        lookup = {(pi, ci): pts for pi, ci, _, pts in self._items}
        for n, (pi, ci) in enumerate(self._order, start=1):
            pts = lookup.get((pi, ci))
            if not pts:
                continue
            part = doc.parts[pi]
            sx, sy = pts[0]
            if o.show_start:
                self._add_overlay(_Marker(sx, sy, 2.5, _cc("start")))
            if o.show_index:
                t = QGraphicsSimpleTextItem(str(n))
                t.setBrush(QBrush(_cc("index")))
                t.setFont(QFont("Sans", 8))
                t.setFlag(QGraphicsItem.ItemIgnoresTransformations)
                t.setPos(sx, sy)
                t.setZValue(6)
                self._add_overlay(t)
            if o.show_direction and len(pts) > 1:
                self._add_overlay(self._arrow(pts))
            lead = getattr(part.craft, "lead_in", None)
            if lead is not None and lead.kind and len(pts) > 1 and not self.toolpath:
                self._add_overlay(self._lead_item(pts, lead, display_layer_color(part.craft.layer)))
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
                if part.contours:
                    x0, y0, x1, y1 = part.bbox()
                    r = QGraphicsRectItem(QRectF(x0, y0, x1 - x0, y1 - y0))
                    pen = QPen(_cc("bbox"), 0, Qt.DotLine)
                    pen.setCosmetic(True)
                    r.setPen(pen)
                    self._add_overlay(r)

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
        self.scale(factor, factor)

    def zoom_fit(self) -> None:
        doc = self.document
        if doc is not None and doc.parts:
            x0, y0, x1, y1 = doc.bbox()
        else:
            w, h = self.options.table_size
            x0, y0, x1, y1 = 0, 0, w, h
        w, h = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
        m = 0.08 * max(w, h)
        self.fitInView(QRectF(x0 - m, y0 - m, w + 2 * m, h + 2 * m), Qt.KeepAspectRatio)
        # fitInView resets the transform; re-apply the Y flip keeping the scale
        s = abs(self.transform().m11())
        c = QPointF((x0 + x1) / 2, (y0 + y1) / 2)
        self.setTransform(QTransform.fromScale(s, -s))
        self.centerOn(c)

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
            self._rubber.setGeometry(self._press_pos.x(), self._press_pos.y(), 0, 0)
            self._rubber.show()
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
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(d.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(d.y()))
            return
        if self._press_pos is not None:
            from PySide6.QtCore import QRect
            self._rubber.setGeometry(QRect(self._press_pos, ev.position().toPoint()).normalized())
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
            self._rubber.hide()
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

    def set_preview(self, polylines=(), markers=(), color: str = "#e6e600") -> None:
        """Temporary overlay: ``polylines`` drawn as dashed lines (the original's yellow
        0.9/0.9/0 stipple, CADModule 0x10084000) and ``markers`` as small squares
        (snap indicators).  Replaces the previous preview."""
        self.clear_preview()
        pen = QPen(QColor(color), 1, Qt.DashLine)
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
