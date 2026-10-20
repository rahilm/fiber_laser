"""SVG icons for the ribbon and the control panel (Tabler Icons, MIT; see docs/ASSETS.md).

Icons are rendered from the vendored SVGs in ``resources/icons/tabler/`` at whatever size and
device-pixel ratio Qt asks for, so they stay crisp at 125 %, 150 % and 200 % scaling. Tabler's
outline icons draw with ``stroke="currentColor"``; :class:`SvgIconEngine` substitutes the
palette's text colour at paint time (so a dark theme gets light icons) or a fixed accent colour
(Start green, Stop red, ...).

``COMMAND_ICONS`` maps every command id that has a fitting icon to a Tabler name. Commands not
listed get no icon and appear as text-only buttons. Ribbon buttons may also name their icon
directly in :mod:`mlaser.ui.ribbon_spec` (``Btn.icon``); that wins over this table.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from PySide6.QtCore import QByteArray, QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .style import ICON_TINTS

ICON_DIR = os.path.join(os.path.dirname(__file__), "resources", "icons", "tabler")

# Semantic accent colours (also readable on a dark background).
GREEN, AMBER, RED, BLUE = (ICON_TINTS[k] for k in ("success", "warning", "danger", "info"))

#: command id -> Tabler icon name. Menu entries, extension commands and the control panel use
#: this table; top-level ribbon buttons carry their icon in ribbon_spec.
COMMAND_ICONS: dict[str, str] = {
    # file / application
    "file.new": "file-plus", "file.open": "folder-open", "file.import": "file-import",
    "file.save": "device-floppy", "file.save_as": "file-pencil", "file.bitmap": "photo",
    "file.import_scan_gcode": "file-code", "file.task_import": "clipboard-list",
    "file.task_save": "clipboard-check", "param.backup_file": "file-export",
    "param.restore_file": "file-import", "app.options": "settings", "app.about": "info-circle",
    "app.exit": "logout", "app.help": "help-circle", "app.recovery": "restore",
    "app.import_language": "language",
    # machine
    "machine.home": "home", "machine.home_xyz": "home-2", "machine.home_x": "letter-x",
    "machine.home_y": "letter-y", "machine.home_af": "focus-2", "machine.edge_seek": "radar-2",
    "machine.edge_disc": "circle-dashed", "machine.edge_capacitive": "wave-sine",
    "machine.edge_two_point": "line", "machine.edge_clear": "eraser", "machine.laser_type": "flare",
    "coord.select": "crosshair", "coord.set_origin": "map-pin-plus",
    "coord.return_origin": "current-location",
    "param.backup": "database-export", "param.restore": "database-import",
    "param.backup_hw": "cpu", "param.restore_hw": "cpu",
    # view / edit / select / show
    "view.select_mode": "pointer", "view.node_edit": "vector", "view.pan_mode": "arrows-move",
    "view.zoom_in": "zoom-in", "view.zoom_out": "zoom-out", "view.zoom_fit": "zoom-reset",
    "view.fit_table": "border-outer", "show.toolpath": "vector-spline",
    "edit.undo": "arrow-back-up", "edit.redo": "arrow-forward-up",
    "select.all": "select-all", "select.invert": "arrows-exchange", "select.unclosed": "line-dashed",
    "select.similar": "copy", "select.inner": "border-inner", "select.outer": "border-outer",
    "show.bbox": "frame", "show.unclosed_red": "highlight", "show.index": "list-numbers",
    "show.start": "flag", "show.direction": "arrow-narrow-right", "show.move_path": "route",
    # transforms
    "xform.size": "dimensions", "xform.scale_dialog": "resize", "xform.align": "layout-align-center",
    "xform.align_left": "layout-align-left", "xform.align_right": "layout-align-right",
    "xform.align_hcenter": "layout-align-center", "xform.align_top": "layout-align-top",
    "xform.align_bottom": "layout-align-bottom", "xform.align_vcenter": "layout-align-middle",
    "xform.align_center": "focus-centered", "xform.mirror_h": "flip-vertical",
    "xform.mirror_v": "flip-horizontal", "xform.rotate": "rotate-clockwise",
    "xform.rot_cw90": "rotate-clockwise", "xform.rot_ccw90": "rotate", "xform.rot_any": "rotate-dot",
    # process (craft)
    "craft.lead": "arrow-ramp-right", "craft.lead_manual": "pencil", "craft.lead_check": "checks",
    "craft.lead_clear_mark": "eraser", "craft.lead_clear": "trash", "craft.start_point": "map-pin",
    "craft.home_ref": "home-dot", "craft.kerf": "brackets-contain", "craft.kerf_cancel": "trash",
    "craft.inside": "arrows-minimize", "craft.outside": "arrows-maximize", "craft.circle_cut": "circle",
    "craft.microjoint": "link", "craft.mj_manual": "pencil", "craft.mj_auto": "wand",
    "craft.mj_params": "adjustments", "craft.mj_explode": "unlink", "craft.mj_clear": "trash",
    "craft.reverse": "arrows-left-right", "craft.seal": "circle-dot", "craft.overcut": "circle-plus",
    "craft.gap": "circle-dashed", "craft.cool_point": "snowflake", "craft.cool_auto": "wand",
    "craft.cool_clear": "trash", "craft.chamfer": "border-radius", "craft.chamfer_auto": "wand",
    "craft.unload": "truck-loading",
    "geom.merge": "arrow-merge", "geom.explode": "arrows-split", "geom.split": "slice",
    "geom.smooth": "wave-sine",
    # sort
    "sort.run": "arrows-sort", "sort.left_right": "arrow-right", "sort.right_left": "arrow-left",
    "sort.bottom_top": "arrow-up", "sort.top_bottom": "arrow-down", "sort.nearest": "route",
    "sort.in_out": "arrows-maximize", "sort.out_in": "arrows-minimize", "sort.manual": "hand-click",
    "sort.shortest": "route", "sort.grid": "layout-grid",
    # tools
    "tool.array": "layout-grid", "tool.group": "stack-2", "tool.scan_cut": "scan",
    "tool.scan_circle": "circle", "tool.scan_line": "line", "tool.engrave": "brush",
    "tool.share_edge": "layout-columns", "tool.bridge": "building-bridge",
    "tool.measure": "ruler-measure", "tool.optimize": "sparkles", "opt.smooth": "wave-sine",
    "opt.dedup": "copy", "opt.replace_circle": "circle", "tool.onekey": "wand",
    # draw
    "draw.line": "line", "draw.pline": "timeline", "draw.rect": "square",
    "draw.round_rect": "square-rounded", "draw.polygon": "hexagon", "draw.star": "star",
    "draw.circle": "circle", "draw.circle3": "circle-dotted", "draw.arc3": "chart-arcs",
    "draw.scan_arc": "chart-arcs-3", "draw.ellipse": "oval", "draw.text": "typography",
    "draw.curve": "vector-spline", "draw.point": "point", "draw.trim": "cut",
    "draw.extend": "arrow-bar-right", "draw.advert_text": "letter-case", "draw.adv_text": "text-resize",
    "draw.qr": "qrcode", "draw.linear_fill": "texture", "draw.fine_tune": "adjustments-horizontal",
    # system analysis
    "mon.controller": "cpu", "mon.ftc": "arrows-vertical", "mon.af": "focus", "mon.ext": "cpu-2",
    "diag.log": "file-text", "diag.log_dir": "folder", "diag.device_report": "report",
    "diag.measure": "chart-line", "diag.work_report": "report-analytics", "sim.speed": "gauge",
    "job.offline": "plug-connected-x", "sim.stop": "player-stop",
    # advanced
    "hw.options": "settings-cog", "hw.reconnect": "plug-connected", "hw.connect_sim": "device-desktop",
    "hw.restart": "refresh", "hw.upgrade": "upload", "hw.set_ip": "network", "hw.net_test": "activity",
    "hw.remoter_match": "device-remote", "aux.interferometer": "wave-square",
    "aux.ball_arm": "circle-dashed", "aux.burn_in": "flame", "aux.light_test": "bulb",
    "aux.batch_cut": "copy", "aux.maintain": "tool", "aux.custom_io": "toggle-left",
    "aux.dedust": "wind",
    # nest
    "nest.new": "file-plus", "nest.open": "folder-open", "nest.save": "device-floppy",
    "nest.save_as": "file-pencil", "nest.import_part": "puzzle", "nest.import_sheet": "square-plus",
    "nest.add_sheet": "square-plus", "nest.run": "layout-board-split", "nest.add_part": "plus",
    "nest.to_main": "transfer-in", "nest.params": "adjustments", "nest.simple": "layout-grid",
    "nest.export": "file-export", "nest.show": "layout-dashboard",
    # CAM+ / Devices / card extensions
    "camx.fill_circle": "circles", "camx.array_circular": "rotate-dot", "camx.array_sheet": "grid-pattern",
    "camx.cleanup": "sparkles", "camx.alpha": "angle", "camx.alpha_clear": "eraser",
    "camx.rect_share": "layout-columns",
    "dev.follower": "arrow-autofit-height", "dev.laser": "bolt", "dev.pendant": "steering-wheel",
    "card.alarms": "alert-triangle", "card.clear_alarms": "bell-off",
    "card.break_resume": "player-skip-forward", "card.params": "settings",
    # control panel (object names of the buttons)
    "jog_Y+": "arrow-up", "jog_Y-": "arrow-down", "jog_X-": "arrow-left", "jog_X+": "arrow-right",
    "jog_Z+": "arrow-bar-to-up", "jog_Z-": "arrow-bar-to-down",
    "jog_zero": "focus-centered", "jog_home_z": "home",
    "run.start": "player-play", "run.pause": "player-pause", "run.stop": "player-stop",
}

#: fixed colours for icons whose meaning is a state (run controls)
COMMAND_COLORS: dict[str, str] = {
    "run.start": GREEN, "run.pause": AMBER, "run.stop": RED, "sim.stop": RED,
    "card.alarms": AMBER,
}


#: an icon file of a plugin (the manifest's ``icon``, ``ctx.icon``) is read up to this size
MAX_ICON_FILE = 512 * 1024


def _is_file_icon(name: str) -> bool:
    return os.path.isabs(name) and name.lower().endswith((".svg", ".png"))


@lru_cache(maxsize=None)
def _svg_source(name: str) -> Optional[bytes]:
    path = name if _is_file_icon(name) else os.path.join(ICON_DIR, f"{name}.svg")
    if _is_file_icon(name):
        try:
            if os.path.getsize(path) > MAX_ICON_FILE:
                return None
        except OSError:
            return None
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def has_icon(name: str) -> bool:
    return _svg_source(name) is not None


class SvgIconEngine(QIconEngine):
    """Render a ``currentColor`` SVG at the exact requested size (no bitmap scaling)."""

    def __init__(self, name: str, color: Optional[str] = None):
        super().__init__()
        self.name = name
        self.color = color

    def _renderer(self, mode, px: int = 24) -> QSvgRenderer:
        if mode == QIcon.Disabled:
            col = self._palette_color(QPalette.Disabled)
            if self.color:
                col.setAlpha(140)
        else:
            col = QColor(self.color) if self.color else self._palette_color(QPalette.Active)
        data = (_svg_source(self.name) or b"").replace(b"currentColor", col.name().encode())
        if px >= 28:   # Tabler's 2 px stroke is drawn on a 24 px grid; keep large icons light
            data = data.replace(b'stroke-width="2"', b'stroke-width="1.6"')
        return QSvgRenderer(QByteArray(data))

    @staticmethod
    def _palette_color(group) -> QColor:
        app = QGuiApplication.instance()
        if app is None:
            return QColor(ICON_TINTS["neutral"])
        return QGuiApplication.palette().color(group, QPalette.ButtonText)

    def paint(self, painter: QPainter, rect: QRect, mode, state) -> None:
        dpr = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
        self._renderer(mode, round(min(rect.width(), rect.height()) / max(dpr, 1.0))).render(
            painter, QRectF(rect))

    def pixmap(self, size: QSize, mode, state) -> QPixmap:
        pm = QPixmap(size)
        pm.fill(Qt.transparent)
        self._render_into(pm, size, mode, min(size.width(), size.height()))
        return pm

    def _render_into(self, pm: QPixmap, size: QSize, mode, logical_px: int) -> None:
        p = QPainter(pm)
        self._renderer(mode, logical_px).render(p, QRectF(QRect(QPoint(0, 0), size)))
        p.end()

    def scaledPixmap(self, size: QSize, mode, state, scale: float) -> QPixmap:
        real = QSize(round(size.width() * scale), round(size.height() * scale))
        pm = QPixmap(real)
        pm.fill(Qt.transparent)
        self._render_into(pm, real, mode, min(size.width(), size.height()))
        pm.setDevicePixelRatio(scale)
        return pm

    def clone(self) -> "SvgIconEngine":
        return SvgIconEngine(self.name, self.color)

    def key(self) -> str:
        return "mlaser-svg"


def icon(name: Optional[str], color: Optional[str] = None) -> QIcon:
    """A crisp, palette-aware QIcon for the Tabler icon ``name`` (empty icon if unknown), or for an
    absolute ``.svg`` / ``.png`` file path (a plugin's icon; SVG ``currentColor`` is tinted)."""
    if name and _is_file_icon(name) and name.lower().endswith(".png"):
        try:
            ok = os.path.isfile(name) and os.path.getsize(name) <= MAX_ICON_FILE
        except OSError:
            ok = False
        return QIcon(name) if ok else QIcon()
    if not name or not has_icon(name):
        return QIcon()
    return QIcon(SvgIconEngine(name, color))


def command_icon(cmd: str, name: Optional[str] = None) -> QIcon:
    """Icon for a command id (``name`` overrides the table); empty when there is none."""
    return icon(name or COMMAND_ICONS.get(cmd), COMMAND_COLORS.get(cmd))


def used_icon_names() -> set[str]:
    """Every icon name referenced by the UI (the set vendored in resources/icons/tabler)."""
    from . import ribbon_spec
    names = set(COMMAND_ICONS.values())
    for tab in ribbon_spec.TABS:
        for grp in tab.groups:
            names.update(b.icon for b in grp.buttons if b.icon)
    return names
