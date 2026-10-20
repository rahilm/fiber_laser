"""Manual craft and engraving commands (agent G, phase 3).

``register(ctx)`` (PluginContext) binds these ribbon commands (MainApp handler / command id / CAD view tool
in brackets, evidence in docs/re/craft.md):

* lead: ``craft.lead_manual`` (FUN_004b5520 0x3459, tool 5), ``craft.lead_check`` (0x345a, CCAD
  slot 83), ``craft.lead_clear_mark`` (0x345c, slot 84)
* ``craft.start_point`` (FUN_004b5a40 0x346d, tool 6), ``craft.home_ref`` (FUN_004b57c0 0x346c,
  dialog mf192, tool 7)
* micro-joints: ``craft.mj_manual`` (FUN_004b5d70 0x3462, tool 10), ``craft.mj_explode`` (0x3466,
  slot 93)
* ``craft.gap`` / ``craft.seal`` / ``craft.overcut`` (FUN_004b5ad0 0x3476..0x3478, slot 90)
* ``craft.cool_point`` (FUN_004b5f50 0x348a, tool 15)
* ``craft.circle_cut`` (mf487 "环切", split button of the α corner: FUN_004b4810 0x343a, slot 61)
* ``tool.bridge`` (FUN_004b6350 0x34da, dialog mode 9, tool 11), ``tool.engrave`` (FUN_004b6cf0
  0x34f3, scan type 4), ``tool.onekey`` (FUN_004be780 0x4a42)
* ``opt.adjust_start`` (FUN_004b5240 0x3448, slot 56), ``opt.replace_circle`` (0x3447, slot 62),
  ``opt.segment`` (FUN_004b4480 0x341c, tool 12)
* ``file.bitmap`` (FUN_004b2f90 0x35b6, Bitmap.exe -> ``mlaser.engrave``), ``file.import_scan_gcode``
  (FUN_004b3870 0x35b7)

Edits are undoable ``ctx.edit`` transactions; a view-mode change (Esc, view.*) leaves the active
click tool (``ctx.on_mode_changed``).  The one-key planning steps are
:func:`mlaser.ops.craft.one_key`.
"""
from __future__ import annotations

import dataclasses
import os
from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import (QButtonGroup, QDialog, QDialogButtonBox, QFileDialog, QGridLayout,
                               QGraphicsEllipseItem, QGraphicsItem, QRadioButton, QVBoxLayout)

from mlaser.cam import manual as M
from mlaser.model import Craft, Document, Part
from mlaser.services import units

from .ext_cam import FieldDialog


# --------------------------------------------------------------------------- canvas click tools
# The original switches the CAD view into a numbered tool (MainApp FUN_004d1780(&tool): 5 manual
# lead, 6 path start, 7 dock point, 10 manual micro-joint, 11 bridge, 12 curve segmentation,
# 15 cooling point).  The click goes to CCADModule vt+0x60 (FUN_100ecc30), which calls the
# handler table this+0x118 + 4*tool; hover handlers sit at this+0x1a8 + 4*tool.  ToolHost uses
# the canvas interaction-tool slot ``CanvasView.set_tool`` (agent F) when present, else an event
# filter on the viewport.  Tools get press/move/release/key/cancel in model coordinates.
Point = tuple[float, float]


class PickTool:
    """A tool that reacts to left clicks on the model plane.

    ``on_click(point, modifiers) -> bool`` returns True to stay active (the original keeps
    most tools active until Esc / right click), False to finish.  ``on_hover(point)`` may
    return a point to mark (e.g. the snapped contour point) or None.
    """

    name = "pick"

    def __init__(self, on_click: Callable[[Point, int], bool],
                 on_hover: Optional[Callable[[Point], Optional[Point]]] = None,
                 on_finish: Optional[Callable[[], None]] = None, name: str = "pick"):
        self.on_click, self.on_hover, self.on_finish = on_click, on_hover, on_finish
        self.name = name
        self.host: Optional["ToolHost"] = None

    # interaction-tool protocol --------------------------------------------------------
    def press(self, x: float, y: float, button="left", modifiers=None) -> bool:
        if button in ("right", Qt.RightButton):
            self.finish()
            return True
        if button not in ("left", Qt.LeftButton):
            return False
        keep = self.on_click((x, y), modifiers)
        if not keep:
            self.finish()
        return True

    def move(self, x: float, y: float, modifiers=None) -> None:
        if self.on_hover is not None and self.host is not None:
            self.host.mark(self.on_hover((x, y)))

    def release(self, x: float, y: float, button="left", modifiers=None) -> bool:
        return button in ("left", "right", Qt.LeftButton, Qt.RightButton)

    def key(self, key, modifiers=None) -> bool:
        if key in ("escape", Qt.Key_Escape):
            self.finish()
            return True
        return False

    def cancel(self) -> None:
        self.finish()

    def finish(self) -> None:
        host, self.host = self.host, None
        if host is not None:
            host.clear(self)
        if self.on_finish is not None:
            self.on_finish()


class ToolHost(QObject):
    """Runs one PickTool at a time on a ``CanvasView``."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.tool: Optional[PickTool] = None
        self._marker: Optional[QGraphicsItem] = None
        self._filter_installed = False

    # ------------------------------------------------------------------ activation
    def start(self, tool: PickTool) -> None:
        if self.tool is not None:
            self.tool.finish()
        self.tool = tool
        tool.host = self
        setter = getattr(self.canvas, "set_tool", None)
        if callable(setter):
            setter(tool)
        elif not self._filter_installed:
            self.canvas.viewport().installEventFilter(self)
            self._filter_installed = True
        self.canvas.setCursor(Qt.CrossCursor)

    def clear(self, tool: Optional[PickTool] = None) -> None:
        if tool is not None and tool is not self.tool:
            return
        self.tool = None
        setter = getattr(self.canvas, "set_tool", None)
        if callable(setter) and getattr(self.canvas, "tool", None) is tool and tool is not None:
            setter(None)
        self.mark(None)
        self.canvas.setCursor(Qt.ArrowCursor)

    def active(self) -> bool:
        return self.tool is not None

    # ------------------------------------------------------------------ overlay
    def mark(self, p: Optional[Point]) -> None:
        prev = getattr(self.canvas, "set_preview", None)
        if callable(prev) and getattr(self.canvas, "tool", None) is not None:
            prev(markers=[p] if p is not None else [])
            return
        sc = self.canvas.scene()
        if p is None:
            if self._marker is not None:
                sc.removeItem(self._marker)
                self._marker = None
            return
        if self._marker is None:
            it = QGraphicsEllipseItem(-4, -4, 8, 8)
            it.setPen(QPen(QColor("#ffcc00"), 1.5))
            it.setBrush(QBrush(Qt.NoBrush))
            it.setFlag(QGraphicsItem.ItemIgnoresTransformations)
            it.setZValue(30)
            sc.addItem(it)
            self._marker = it
        self._marker.setPos(QPointF(p[0], p[1]))

    def tolerance(self, px: float = 8.0) -> float:
        """Pick tolerance in model units: CADModule this+0x270 = 8.0 (DAT_1011ff18) x scale."""
        return px / max(abs(self.canvas.transform().m11()), 1e-9)

    # ------------------------------------------------------------------ fallback event filter
    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        t = self.tool
        if t is None:
            return False
        et = ev.type()
        if et in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseMove):
            sp = self.canvas.mapToScene(ev.position().toPoint())
            if et == QEvent.MouseMove:
                t.move(sp.x(), sp.y())
                return False
            if ev.button() == Qt.MiddleButton:
                return False
            if et == QEvent.MouseButtonPress:
                return t.press(sp.x(), sp.y(), ev.button(), ev.modifiers())
            return t.release(sp.x(), sp.y(), ev.button(), ev.modifiers())
        if et == QEvent.KeyPress:
            return t.key(ev.key())
        return False


# --------------------------------------------------------------------------- parameters
@dataclasses.dataclass
class GapParams:
    """GRP.LoopGapOverCutLength (GraphParam 0xb228): registered twice, as pd323 Gap Length
    and pd324 Overcut Length, on the same offset, so both dialogs edit one value (default
    hint 2 mm)."""
    value: float = 2.0


@dataclasses.dataclass
class ManualJointParams:
    length: float = 0.5           # pd325 手动微连.微连长度


@dataclasses.dataclass
class BridgeParams:
    width: float = 1.0            # pd347 桥接参数.桥接宽度


@dataclasses.dataclass
class OneKeyParams:
    """mf197 OneKey planning parameters (pd476..pd481).  Only pd479..481 are registered in the
    v0.0.1.51 GraphParam block (GRP.OneKeySort 0xb34f, OneKeyGuideLine 0xb350, OneKeyMicroLink
    0xb351); the mirror/optimize steps exist only as lang labels."""
    mirror_h: bool = False        # pd476
    mirror_v: bool = False        # pd477
    optimize: bool = False        # pd478
    sort: bool = True             # pd479
    lead: bool = True             # pd480
    micro_joint: bool = False     # pd481


@dataclasses.dataclass
class DockParams:
    mode: int = 0                 # SoftParam 0x497c: 0 manual pick, 1..9 = ap2..ap10 grid


DOCK_KEYS = ["ap2", "ap3", "ap4", "ap5", "ap6", "ap7", "ap8", "ap9", "ap10"]


def _targets(doc: Document) -> list[Part]:
    """Selection (else all), without raster engraving parts (contour tools)."""
    from mlaser.ops.document import contour_targets
    return contour_targets(doc)


class CraftExtension:
    """State + handlers. ``exec_dialogs=False`` (tests) applies stored parameters directly."""

    def __init__(self, ctx, exec_dialogs: bool = True):
        self.ctx = ctx
        self.w = window = ctx.window
        self.exec_dialogs = exec_dialogs
        self.gap_p = GapParams()
        self.mj_p = ManualJointParams()
        self.bridge_p = BridgeParams()
        self.onekey_p = OneKeyParams()
        self.dock_p = DockParams()
        from mlaser.cam.flycut import EngraveScanParams
        self.engrave_p = EngraveScanParams()
        import tempfile
        self.bitmap_dir = os.path.join(tempfile.gettempdir(), "mlaser_bitmap")   # original: <app>\\Bitmap
        self.host = ToolHost(window.canvas)
        self.tool_name: Optional[str] = None
        self.last_message = ""

    # ---------------------------------------------------------------- infrastructure
    def log(self, msg: str) -> None:
        self.last_message = msg
        lp = getattr(self.w, "log_panel", None)
        if lp is not None:
            lp.log(msg)

    def t(self, key: str, default: Optional[str] = None) -> str:
        return self.w.tr_.tr(key, default)

    def edit(self, fn: Callable[[Document], Any], label: str = "craft") -> Any:
        """Undoable edit; the views are rebuilt without zoom-fit (keeps the view while a click
        tool is active)."""
        return self.ctx.edit(fn, label)

    def refresh(self) -> None:
        """Views after a change that is deliberately not an undo step (mf91 clear marks)."""
        self.w.refresh_views()

    def ask(self, title_key: str, obj, fields) -> bool:
        d = FieldDialog(self.w.tr_, title_key, obj, fields, self.w)
        if not self.exec_dialogs:
            d.store()
            return True
        return bool(d.exec())

    # ---------------------------------------------------------------- canvas tools
    def start_tool(self, name: str, on_click, status_key: str, hover: bool = True) -> PickTool:
        def hov(p):
            h = M.hit_test(self.w.document, p, self.host.tolerance())
            return h.point if h else None
        tool = PickTool(on_click, hov if hover else None, self._tool_done, name)
        self.tool_name = name
        self.host.start(tool)
        self.log(self.t(status_key))
        return tool

    def _tool_done(self) -> None:
        self.tool_name = None

    def cancel_tool(self) -> None:
        if self.host.tool is not None:
            self.host.tool.finish()

    def click(self, x: float, y: float, button=Qt.LeftButton) -> None:
        """Feed a click to the active tool (tests / scripted use)."""
        if self.host.tool is not None:
            self.host.tool.press(x, y, "right" if button in ("right", Qt.RightButton) else "left", set())

    def _hit(self, p, closed_only: bool = False) -> Optional[M.Hit]:
        doc = self.w.document
        h = M.hit_test(doc, p, self.host.tolerance())
        if h is not None and closed_only and not doc.parts[h.part].contours[h.contour].closed:
            h = None
        if h is None:
            self.log("no contour under the cursor")
        return h

    # ---------------------------------------------------------------- start point (tool 6)
    def start_point(self) -> None:
        def click(p, mods):
            h = self._hit(p, closed_only=True)
            if h is not None:
                f = self.edit(lambda d: M.set_start_at(d.parts[h.part], h.contour, h.dist))
                self.log(f"mf93: start {f:.4f}")       # a fraction of the contour (no unit)
            return True
        self.start_tool("start_point", click, "mf93")

    # ---------------------------------------------------------------- manual lead (tool 5)
    def lead_manual(self) -> None:
        """Two clicks: the lead start point, then the contour (FUN_100be7d0).  A second click
        that hits no closed contour becomes the new first point."""
        state = {"q": None}

        def click(p, mods):
            if state["q"] is None:
                state["q"] = p
                self.log(self.t("mf88") + ": " + units.current().fmt_xy(p[0], p[1], "small_length"))
                return True
            h = self._hit(p, closed_only=True)
            if h is None:
                state["q"] = p
                return True
            q = state["q"]
            state["q"] = None

            def do(d):
                depth = M.contour_depths(d).get((h.part, h.contour), 1)
                inv = bool(getattr(self.w.cam_params, "invert_lead_side", False))
                return M.manual_lead(d.parts[h.part], h.contour, q, h.dist, depth, inv)
            lp = self.edit(do)
            self.log(f"mf88: {units.current().fmt(lp.length, 'small_length')}" if lp is not None and lp.kind
                     else "mf88: 0")
            return True
        self.start_tool("lead_manual", click, "mf88")

    # ---------------------------------------------------------------- bridge (tool 11)
    def bridge(self) -> None:
        if not self.ask("mf114", self.bridge_p, [("width", "pd347", "len", (0.01, 1000, 3))]):
            return
        state = {"a": None}

        def click(p, mods):
            h = self._hit(p, closed_only=True)
            if h is None:
                return True
            if state["a"] is None:
                state["a"] = h
                return True
            a, b = state["a"], h
            state["a"] = None
            if (b.part, b.contour) < (a.part, a.contour):
                a, b = b, a
            res = self.edit(lambda d: self._apply_bridge(d, a, b))
            self.log(f"mf114: {'OK' if res else self.t('mf161')}")
            return True
        self.start_tool("bridge", click, "mf114")

    def _apply_bridge(self, doc: Document, a: M.Hit, b: M.Hit) -> bool:
        pa, pb = doc.parts[a.part], doc.parts[b.part]
        ca, cb = pa.contours[a.contour], pb.contours[b.contour]
        w = self.bridge_p.width
        if (a.part, a.contour) == (b.part, b.contour):
            res = M.bridge_same(ca, a.point, b.point, w)
            if not res:
                return False
            pa.contours[a.contour:a.contour + 1] = res
            return True
        merged = M.bridge_two(ca, a.point, cb, b.point, w)
        if merged is None:
            return False
        pa.contours[a.contour] = merged
        pb.contours.pop(b.contour)
        if not pb.contours:
            doc.parts.remove(pb)
        return True

    # ---------------------------------------------------------------- manual micro-joint (tool 10)
    def mj_manual(self) -> None:
        if not self.ask("mf102", self.mj_p, [("length", "pd325", "slen", (0.01, 100, 3))]):
            return

        def click(p, mods):
            h = self._hit(p)
            if h is None:
                return True

            def do(d):
                part = d.parts[h.part]
                cr = M.contour_craft(part, h.contour, create=True)
                return M.toggle_joint(cr, part.contours[h.contour], h.dist, self.mj_p.length)
            self.log(f"mf101: {self.edit(do)}")
            return True
        self.start_tool("mj_manual", click, "mf101")

    def mj_explode(self) -> None:
        """CCAD 0x100bad60 -> 0x10075640: selected graphs only (scans skipped); beam-on pieces
        >= 0.05 mm become new open contours with default craft (layer kept)."""
        def do(doc):
            n = 0
            for p in doc.parts:
                if not p.selected or p.extra.get("scan_paths"):
                    continue
                new, changed = [], False
                for ci, c in enumerate(p.contours):
                    pieces = M.explode_joints(c, M.contour_craft(p, ci))
                    if pieces != [c]:
                        changed = True
                        n += 1
                        pieces = [k for k in pieces if k.length() >= 0.05]
                    new += pieces
                if changed:
                    p.contours = new
                    p.craft = Craft(layer=p.craft.layer)
            return n
        self.log(f"mf103_1: {self.edit(do)}")

    # ---------------------------------------------------------------- gap / seal / overcut
    def _gap(self, mode: int) -> None:
        """CCAD 0x100c46a0(mode, value): selected graphs only (groups expanded); a contour is
        changed when |v - current| > 0.02 and |v| < 0.33 * length."""
        if mode == M.GAP and not self.ask("mf95", self.gap_p, [("value", "pd323", "slen", (0, 100, 3))]):
            return
        if mode == M.OVERCUT and not self.ask("mf97", self.gap_p, [("value", "pd324", "slen", (0, 100, 3))]):
            return
        val = self.gap_p.value

        def do(doc):
            n = 0
            for p in doc.parts:
                if not p.selected:
                    continue
                for ci, c in enumerate(p.contours):
                    v = mode * abs(val)
                    cr = M.contour_craft(p, ci)
                    cur = float(cr.extra.get("overcut", 0.0) or 0.0)
                    if abs(v - cur) <= 0.02 or abs(v) >= 0.33 * c.length():
                        continue
                    M.set_gap_seal(M.contour_craft(p, ci, create=True), c, mode, val)
                    n += 1
            return n
        self.log(f"{ {M.GAP: 'mf95', M.SEAL: 'mf96', M.OVERCUT: 'mf97'}[mode]}: {self.edit(do)}")

    def gap(self) -> None:
        self._gap(M.GAP)

    def seal(self) -> None:
        self._gap(M.SEAL)

    def overcut(self) -> None:
        self._gap(M.OVERCUT)

    # ---------------------------------------------------------------- cooling point (tool 15)
    def cool_point(self) -> None:
        def click(p, mods):
            h = self._hit(p)
            if h is None:
                return True

            def do(d):
                part = d.parts[h.part]
                cr = M.contour_craft(part, h.contour, create=True)
                c = part.contours[h.contour]
                pos = M.snap_to_vertex(c, p, 2 * self.host.tolerance())
                return M.place_cool_point(cr, c, h.dist if pos is None else pos, self.host.tolerance())
            self.log(f"mf406: {self.edit(do)}")
            return True
        self.start_tool("cool_point", click, "mf406")

    # ---------------------------------------------------------------- α corner (circle cut)
    def circle_cut(self) -> None:
        cam = self.w.extension("ext_cam") if hasattr(self.w, "extension") else None
        if cam is not None and hasattr(cam, "alpha"):
            cam.alpha()
        else:
            self.log("mf487: α corner (ext_cam) not loaded")

    # ---------------------------------------------------------------- curve segmentation (tool 12)
    def segment(self) -> None:
        def click(p, mods):
            h = self._hit(p)
            if h is None:
                return True

            def do(d):
                part = d.parts[h.part]
                c = part.contours[h.contour]
                pieces = M.split_contour_at(c, h.dist, self.host.tolerance())
                part.contours[h.contour:h.contour + 1] = pieces
                return len(pieces)
            self.log(f"mf475: {self.edit(do)}")
            return True
        self.start_tool("segment", click, "mf475")

    # ---------------------------------------------------------------- lead check / marks
    def lead_check(self) -> None:
        inv = bool(getattr(self.w.cam_params, "invert_lead_side", False))
        marked = self.edit(lambda d: M.check_leads(d, inv))
        for pi, _ci in marked:
            self.w.document.parts[pi].selected = True
        self.w.canvas.refresh_styles()
        self.log(f"mf89: {len(marked)}")

    def lead_clear_mark(self) -> None:
        n = M.clear_lead_marks(self.w.document)          # no undo step in the original
        self.refresh()
        self.log(f"mf91: {n}")

    # ---------------------------------------------------------------- optimize sub-commands
    def replace_circle(self) -> None:
        n = self.edit(M.replace_circles)
        self.log(f"mf472: {n}")

    def adjust_start(self) -> None:
        n = self.edit(lambda d: M.adjust_starts([p for p in d.parts if p.selected]))
        self.log(f"mf604: {n}")

    # ---------------------------------------------------------------- scan engraving
    def _engrave_dialog(self, title: str = "mf112_5") -> bool:
        f = [("side_len", "pd719_3", "len", (0, 1000, 3)),
             ("direction", "pd719_4", "int", (0, 3))]
        return self.ask(title, self.engrave_p, f)

    def engrave(self) -> None:
        """mf112_5 (0x34f3): dialog FUN_004310c0(0), then scan type 4 on the selection."""
        from mlaser.cam import flycut as F
        if not self._engrave_dialog():
            return
        part = self.edit(lambda d: F.apply_engrave_scan(d, self.engrave_p, F.SCAN_ENGRAVE,
                                                        selected_only=any(p.selected for p in d.parts)))
        self.log(self.t("mf161") if part is None else "mf112_5: 1 scan path")

    def import_scan_gcode(self, paths=None) -> int:
        """A251104_0 (0x35b7, FUN_004b3870): pick G-code files (A251104_1 filter), new document,
        per file (<= 100 MB, .g/.nc/.cnc) the mode-2 dialog, load, then the type-5 scan."""
        if paths is None:
            flt = "Supported (*.nc *.cnc *.g)"
            paths, _ = QFileDialog.getOpenFileNames(self.w, self.t("A251104_0"), "", flt)
        paths = [p for p in (paths or []) if p]
        if not paths:
            return 0
        return self._load_scan_files(paths)

    def _load_scan_files(self, paths) -> int:
        from mlaser.cam import flycut as F
        new = []
        for path in paths:
            ext = os.path.splitext(path)[1].lower()
            if ext not in (".g", ".nc", ".cnc") or not os.path.isfile(path) or os.path.getsize(path) > 100 * 2 ** 20:
                self.log(f"{self.t('mv25')}: {path}")
                continue
            if not self._engrave_dialog("A251104_0"):
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                part = F.gcode_scan_part(fh.read(), self.engrave_p)
            if part is not None:
                part.name = os.path.basename(path)
                new.append(part)
        if not new:
            return 0
        self.ctx.replace_document(Document(parts=new), "import scan G-code")
        self.log(f"A251104_0: {len(new)}")
        return len(new)

    def bitmap(self) -> Optional[str]:
        """A250622_0 (0x35b6, FUN_004b2f90): the original runs Bitmap\\Bitmap.exe modally and
        then imports Bitmap\\newGcode.nc like a scan G-code; the rebuild opens the
        ``mlaser.engrave`` dialog in-process and imports its G-code the same way."""
        try:
            from mlaser.engrave.dialog import BitmapDialog
        except Exception as exc:  # noqa: BLE001
            self.log(f"A250622_0: mlaser.engrave unavailable ({exc})")
            return None
        os.makedirs(self.bitmap_dir, exist_ok=True)
        d = BitmapDialog(self.w.tr_, self.w, out_dir=self.bitmap_dir)
        self.bitmap_dialog = d
        if self.exec_dialogs and not d.exec():
            return None
        return self.import_bitmap_result()

    def import_bitmap_result(self) -> Optional[str]:
        """The engraving the bitmap dialog generated, as one :class:`RasterEngraving` part
        (``mlaser.cam.raster_engraving``).  Without one (G-code generated outside the dialog), as
        MainApp does: check ``Bitmap\\status.ini`` (FUN_004ae380: 0 at start-up, 1 after
        "Generate GCode") and load ``Bitmap\\newGcode.nc`` as a scan G-code (type-5 scan)."""
        d = getattr(self, "bitmap_dialog", None)
        doc = d.result_document() if d is not None and hasattr(d, "result_document") else None
        if doc is not None:
            # the in-process generator: one RasterEngraving part (not one Part per burn segment)
            self.ctx.replace_document(doc, "bitmap engraving")
            self.log(f"A250622_0: raster engraving, {len(doc.parts[0].extra['raster'])} burn runs"
                     if doc.parts and "raster" in doc.parts[0].extra else "A250622_0")
            return "raster"
        from mlaser.engrave import raster
        if raster.read_status_ini(self.bitmap_dir) != 1:
            return None
        path = os.path.join(self.bitmap_dir, raster.GCODE_NAME)
        if os.path.isfile(path) and self._load_scan_files([path]):
            return path
        return None

    # ---------------------------------------------------------------- dock point (home ref)
    def home_ref(self) -> None:
        d = DockDialog(self.w.tr_, self.dock_p.mode, self.w)
        if self.exec_dialogs and not d.exec():
            return
        if self.exec_dialogs:
            self.dock_p.mode = d.mode()
        if self.dock_p.mode == 0:
            def click(p, mods):
                self.set_dock(p)
                return False
            self.start_tool("home_ref", click, "mf92", hover=False)
            return
        doc = self.w.document
        if not doc.parts:
            return
        x0, y0, x1, y1 = doc.bbox()
        k = self.dock_p.mode
        x = (x1, x0, (x0 + x1) / 2)[k % 3]
        y = y1 if k < 4 else ((y0 + y1) / 2 if k < 7 else y0)
        try:
            st = self.w.ctl.state()
            head = (float(st.x), float(st.y))
        except Exception:  # noqa: BLE001 - no controller state: machine origin
            head = (0.0, 0.0)
        d = (x - head[0], y - head[1])
        if abs(d[0]) + abs(d[1]) > 0.01:
            from mlaser import ops
            self.edit(lambda dd: ops.translate(dd, -d[0], -d[1], list(dd.parts)), "home ref")
        self.set_dock(head)

    def set_dock(self, p) -> None:
        self.w.settings["SP.DockPoint"] = (float(p[0]), float(p[1]))
        self.w.document.extra["dock_point"] = (float(p[0]), float(p[1]))
        self.w.settings["SP.DockMode"] = self.dock_p.mode
        self.log("mp68: " + units.current().fmt_xy(p[0], p[1], "small_length"))

    # ---------------------------------------------------------------- one key planning
    def onekey(self) -> None:
        f = [("mirror_h", "pd476", "bool", None), ("mirror_v", "pd477", "bool", None),
             ("optimize", "pd478", "bool", None), ("sort", "pd479", "bool", None),
             ("lead", "pd480", "bool", None), ("micro_joint", "pd481", "bool", None)]
        if not self.ask("mf197", self.onekey_p, f):
            return
        q = self.onekey_p
        from mlaser.ops.craft import one_key
        cam = self.w.extension("ext_cam") if hasattr(self.w, "extension") else None
        self.edit(lambda doc: one_key(doc, mirror_h=q.mirror_h, mirror_v=q.mirror_v, optimize=q.optimize,
                                      cleanup_params=getattr(cam, "cleanup", None), lead=q.lead,
                                      lead_in=self.w.lead_in, lead_out=self.w.lead_out,
                                      micro_joint=q.micro_joint, mj_params=self.w.mj_params),
                  "one-key planning")
        if q.sort:
            self.w.sort("nearest")
        self.log(self.t("mf209"))


class DockDialog(QDialog):
    """mf192 "Home Ref Parameters" (MainApp FUN_005e0cd0): radio 20000 = manual pick on the
    canvas, radios 20001..20009 = 3x3 grid ap2..ap10 (top-left .. bottom-right) of the
    graphics bounding box; the choice is SoftParam 0x497c."""

    def __init__(self, tr, mode: int = 0, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr.tr("mf192"))
        lay = QVBoxLayout(self)
        self.group = QButtonGroup(self)
        manual = QRadioButton(tr.tr("mp207", "Manual"))   # label of radio 20000 is U
        self.group.addButton(manual, 0)
        lay.addWidget(manual)
        grid = QGridLayout()
        for i, k in enumerate(DOCK_KEYS):
            b = QRadioButton(tr.tr(k))
            self.group.addButton(b, i + 1)
            grid.addWidget(b, i // 3, i % 3)
        lay.addLayout(grid)
        btn = self.group.button(mode if 0 <= mode <= 9 else 0)
        btn.setChecked(True)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr.tr("ap1"))
        bb.button(QDialogButtonBox.Cancel).setText(tr.tr("ap0"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def mode(self) -> int:
        return max(0, self.group.checkedId())


# --------------------------------------------------------------------------- wiring
REWIRE = {
    "craft.start_point": "start_point", "craft.mj_manual": "mj_manual", "craft.mj_explode": "mj_explode",
    "craft.gap": "gap", "craft.seal": "seal", "craft.overcut": "overcut",
    "craft.cool_point": "cool_point", "craft.circle_cut": "circle_cut", "opt.segment": "segment",
    "craft.home_ref": "home_ref", "tool.onekey": "onekey",
    "craft.lead_manual": "lead_manual", "tool.bridge": "bridge",
    "craft.lead_check": "lead_check", "craft.lead_clear_mark": "lead_clear_mark",
    "opt.replace_circle": "replace_circle", "opt.adjust_start": "adjust_start",
    "tool.engrave": "engrave", "file.import_scan_gcode": "import_scan_gcode", "file.bitmap": "bitmap",
}


def register(ctx, exec_dialogs: bool = True) -> CraftExtension:
    """Bind the manual craft / engraving commands (``ctx``: PluginContext)."""
    ext = CraftExtension(ctx, exec_dialogs)
    for cmd, name in REWIRE.items():
        ctx.register_command(cmd, getattr(ext, name))
    # Esc (MainWindow shortcut -> set_mode(select)) and view.* also leave the active click tool
    ctx.on_mode_changed(lambda _mode: ext.cancel_tool())
    return ext
