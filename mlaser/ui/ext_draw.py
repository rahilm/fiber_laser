"""Draw tab and CAD editing commands (phase 3, agent F).

``register(ctx)`` (PluginContext) binds these ribbon commands (see docs/re/draw.md for the evidence):

* drawing tools ``draw.point/line/pline/rect/round_rect/circle/circle3/arc3/scan_arc/
  ellipse/polygon/star/curve`` -> interactive tools from :mod:`mlaser.cad.tools`, driven
  through the canvas interaction-tool hook (``CanvasView.set_tool``) with object snapping;
* ``draw.text`` (CTextInputDlg: text, TrueType font, height) and ``draw.qr``
  (CQRCodeCreateDlg: text, size, line density) -> preview follows the cursor, click places;
* ``draw.adv_text`` (精品字: GRP.AdvTextType / GRP.AdvTextOffset -> kerf offset copies),
  ``draw.advert_text`` (广告字: fill circles in the selected letters, GRP.FillCircle*);
* ``draw.trim`` / ``draw.extend`` / ``geom.split`` -> pick tools; ``geom.explode``,
  ``geom.merge`` (tolerance GRP.ManualConnectGate);
* ``draw.fine_tune`` (mf445 keyboard fine-tuning: step pd377 / auto step pd376);
* ``xform.align`` (main button = left align), ``xform.size`` (size dialog with ratio lock
  and 9-point anchor);
* ``select.similar/inner/outer``; ``tool.group*``; ``tool.measure``.

Every document change is an undoable ``ctx.edit`` transaction; the arrow keys use this plugin's
fine-tune step through the ``nudge`` provider, and a view-mode change ends the active tool
(``ctx.on_mode_changed``).
Tests pass ``exec_dialogs=False`` so dialogs apply their stored parameters without opening.
"""
from __future__ import annotations

import copy
import dataclasses
from typing import Callable, Optional

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialogButtonBox, QDoubleSpinBox, QFontComboBox,
                               QLineEdit, QPlainTextEdit, QSpinBox)

from mlaser.cad import arrange as AR
from mlaser.cad import edit as ED
from mlaser.cad import tools as T
from mlaser.cad.text import DEFAULT_FONT
from mlaser.model import Contour, Document, Part
from mlaser.services import units
from mlaser.ui import style as S
from mlaser.ui.dialogs import split_unit, suffix_for
from mlaser.ui.style import Dialog
from mlaser.ui.units_qt import QuantitySpinBox

SNAP_PX = 8.0          # object-snap window (pixels)
MIN_CLICK_PX = 2.0     # clicks closer than this to the previous one are ignored (tool+0x38)
PICK_PX = 6.0          # trim / extend / split pick distance (CCADModule+0x270)


# --------------------------------------------------------------------------- parameters
@dataclasses.dataclass
class TextParams:
    text: str = "Mlaser"
    # gp79 (the original's 宋体); the rebuild draws with the bundled Noto Sans SC subset unless
    # the user picks another font, so the cut geometry is the same on every machine
    font: str = DEFAULT_FONT
    height: float = 10.0          # ti0 / app+0xb490


@dataclasses.dataclass
class QRParams:
    text: str = ""                # QR.QRCodeNote
    size: float = 25.0            # QR.QRCodeSize A260420_2 (10..100 mm)
    density: int = 12             # QR.QRCodeLineDensity A260420_4 (3..30 lines/mm)
    mode: int = 0                 # 0 scan lines (original), 1 outlined squares (rebuild option)


@dataclasses.dataclass
class SizeParams:
    width: float = 100.0
    height: float = 100.0
    lock: bool = True             # GRP.ZoomRatioIsLock pd659 (+0xb3e0)
    anchor: int = 5               # +0xb400, 1..9


@dataclasses.dataclass
class MergeParams:
    tolerance: float = 0.1        # GRP.ManualConnectGate pd368


@dataclasses.dataclass
class AdvTextParams:
    kind: int = 0                 # GRP.AdvTextType pd1810: 0 all shrink (pd503), 1 all expand (pd504)
    offset: float = 1.0           # GRP.AdvTextOffset pd1811 (registered default 0 = no-op)


@dataclasses.dataclass
class FineTuneParams:
    step: float = 1.0             # IGP.KeyboardMoveStepLength pd377
    auto: bool = True             # IGP.AutoCalcKeyboardMoveStepLength pd376


# --------------------------------------------------------------------------- canvas adapter
class CanvasToolDriver:
    """Bridges :mod:`mlaser.cad.tools` state machines to ``CanvasView.set_tool``: snaps the
    points, shows the preview and hands the result to ``on_done``."""

    def __init__(self, ext: "DrawExtension", tool: T.Tool, on_done: Callable[[T.Tool], None],
                 snap: bool = True):
        self.ext = ext
        self.tool = tool
        self.on_done = on_done
        self.snap_enabled = snap
        self._snap_marker = None

    @property
    def canvas(self):
        return self.ext.w.canvas

    def _snap(self, x, y, mods=()):
        if not self.snap_enabled or "shift" in (mods or ()):
            self._snap_marker = None
            return (x, y)
        p, kind = self.canvas.snap_point(x, y, SNAP_PX, self.tool.snap_points())
        self._snap_marker = p if kind else None
        return p

    def _update(self):
        markers = [self._snap_marker] if self._snap_marker is not None else []
        self.canvas.set_preview(self.tool.preview(), markers)

    def press(self, x, y, button, mods=frozenset()):
        if button == "right":
            if self.tool.finish():
                self._complete()
            elif isinstance(self.tool, T.PickTool) or not self.tool.points:
                self.ext.end_tool()             # mv3 End Draw
            return
        self.tool.tol = MIN_CLICK_PX * self.canvas.pixel_size()
        p = self._snap(x, y, mods)
        self.tool.click(p)
        if self.tool.done:
            self._complete()
        else:
            self._update()

    def move(self, x, y, mods=frozenset()):
        self.tool.hover(self._snap(x, y, mods))
        self._update()

    def release(self, x, y, button, mods=frozenset()):
        pass

    def key(self, name, mods=frozenset()):
        if name == "return":
            if self.tool.finish():
                self._complete()
            return True
        if name in ("escape",):
            return False
        used = self.tool.key(name)
        if self.tool.done:
            self._complete()
        elif used:
            self._update()
        return used

    def cancel(self):
        self.canvas.clear_preview()

    def _complete(self):
        tool = self.tool
        self.canvas.clear_preview()
        self.on_done(tool)


# --------------------------------------------------------------------------- dialogs
class _Dialog(Dialog):
    """The drawing tools' parameter prompts on the one dialog frame (style.Dialog): a form,
    units in the fields' suffixes, OK / Cancel in the platform's order."""

    def __init__(self, tr, title_key: str, parent=None):
        super().__init__(parent, tr.tr(title_key) if tr.has(title_key) else title_key)
        self.tr_ = tr
        self.buttons.button(QDialogButtonBox.Ok).setText(tr.tr("ap1", "OK"))
        self.buttons.button(QDialogButtonBox.Cancel).setText(tr.tr("ap0", "Cancel"))

    def label(self, key: str) -> str:
        if not self.tr_.has(key):
            return key
        t = self.tr_.tr(key)
        return t.split(".", 1)[1] if key.startswith(("pd", "A2")) and "." in t else t

    def add(self, key: str, w):
        label, unit = split_unit(self.label(key))
        if isinstance(w, QCheckBox) and not w.text():
            w.setText(label)
            S.form_row(self.form(), "", w)
            return w
        if unit and isinstance(w, (QSpinBox, QDoubleSpinBox)) and not isinstance(w, QuantitySpinBox):
            w.setSuffix(suffix_for(unit))
        S.form_row(self.form(), label, w)
        return w

    def add_row(self, label: str, w):
        S.form_row(self.form(), label, w)
        return w

    def finish(self):
        self.adjustSize()


def _lspin(lo, hi, v, dec=3, kind: str = "length") -> QuantitySpinBox:
    """A length in the display units (``value()`` in mm; ``dec`` = the metric decimals)."""
    return QuantitySpinBox(kind, minimum=lo, maximum=hi, decimals=dec, value=v)


def _ispin(lo, hi, v):
    w = QSpinBox()
    w.setRange(lo, hi)
    w.setValue(int(v))
    return w


# --------------------------------------------------------------------------- extension
class DrawExtension:
    def __init__(self, ctx, exec_dialogs: bool = True):
        self.ctx = ctx
        self.w = window = ctx.window
        self.exec_dialogs = exec_dialogs
        self.text = TextParams()
        self.qr = QRParams()
        self.size = SizeParams()
        self.merge = MergeParams()
        self.adv = AdvTextParams()
        # the arrow-key step is a preference (Settings > Design & CAM defaults > Editing)
        cfg = window.config
        self.fine = FineTuneParams(step=float(cfg.get("design.nudge_step", 1.0)),
                                   auto=bool(cfg.get("design.nudge_auto", True)))
        cfg.subscribe(self._config_changed)
        from mlaser.cam.array import FillParams
        self.fill = FillParams()
        self.polygon_sides = 6     # FUN_100ef350 passes 6
        self.star_points = 6
        self.driver: Optional[CanvasToolDriver] = None
        self.last_measure: Optional[tuple[float, float, float]] = None
        self.new_layer = 1

    # ------------------------------------------------------------ helpers
    def log(self, msg: str) -> None:
        lp = getattr(self.w, "log_panel", None)
        if lp is not None:
            lp.log(msg)

    def t(self, key, default=None):
        return self.w.tr_.tr(key, default)

    def edit(self, fn, label: str = "draw", parts=None):
        """One undoable transaction.  ``parts`` is its scope: the parts ``fn`` changes in place
        (DocumentService.edit copies only those for undo; None = every part).  Each command
        below passes the parts it touches, normally the selection (review 2 P1-3: a one-part
        nudge copied all 2,000 parts, 5.2 s)."""
        return self.ctx.edit(fn, label, parts=parts)

    def _sync_selection(self):
        self.w.canvas.refresh_styles()
        pl = getattr(self.w, "part_list", None)
        if pl is not None:
            pl.sync_selection()
        self.w.canvas.selectionChanged.emit()

    def _selected(self, doc=None) -> list[Part]:
        d = doc or self.w.document
        return [p for p in d.parts if p.selected]

    def _run_dialog(self, dlg) -> bool:
        return True if not self.exec_dialogs else bool(dlg.exec())

    # ------------------------------------------------------------ tool lifecycle
    def start(self, tool: T.Tool, on_done: Callable[[T.Tool], None], snap: bool = True) -> None:
        self.end_tool()
        self.driver = CanvasToolDriver(self, tool, on_done, snap)
        self.w.canvas.set_tool(self.driver)
        self.w.canvas.setFocus()
        self.log(f"{tool.name}: {self.t('mv2')} = right click / Enter, {self.t('mv3')} = Esc")

    def end_tool(self) -> None:
        if self.driver is not None and self.w.canvas.tool is self.driver:
            self.w.canvas.set_tool(None)
        self.driver = None

    def add_contours(self, contours: list[Contour], name: str, one_part: bool = True) -> None:
        """Append the new graph(s) selected (COpCreateCmd) as one undoable edit."""
        contours = [c for c in contours if c.segments]
        if not contours:
            return
        from mlaser.model import Craft

        def do(doc: Document):
            for p in doc.parts:
                p.selected = False
            groups = [contours] if one_part else [[c] for c in contours]
            for cs in groups:
                doc.parts.append(Part(contours=copy.deepcopy(cs), craft=Craft(layer=self.new_layer),
                                      name=name, selected=True))
            doc.extra.pop("order", None)
        self.edit(do, parts=self._selected())      # in place: only the deselected parts

    def draw_tool(self, cmd: str) -> None:
        cls = T.TOOLS[cmd]
        tool = cls()
        if isinstance(tool, T.PolygonTool):
            tool.sides = self.polygon_sides
        if isinstance(tool, T.StarTool):
            tool.n = self.star_points

        def done(t: T.Tool):
            if isinstance(t, T.PolygonTool):
                self.polygon_sides = t.sides
            if isinstance(t, T.StarTool):
                self.star_points = t.n
            self.add_contours(t.result(), cmd.split(".")[1])
            self.end_tool()
        self.start(tool, done)

    # ------------------------------------------------------------ text / qr
    def text_dialog(self) -> None:
        from mlaser.cad.text import resolve_family, text_contours
        p = self.text
        d = _Dialog(self.w.tr_, "mf55", self.w)
        ed = QPlainTextEdit(p.text)
        ed.setFixedHeight(70)
        d.add_row(self.t("mv20"), ed)
        fc = d.add("gp77", QFontComboBox())
        fc.setFontFilters(QFontComboBox.ScalableFonts)
        from PySide6.QtGui import QFont
        fc.setCurrentFont(QFont(resolve_family(p.font)))
        hs = d.add("ti0", _lspin(0.1, 10000, p.height, 2))
        d.finish()
        if not self._run_dialog(d):
            return
        if self.exec_dialogs:
            p.text, p.font, p.height = ed.toPlainText(), fc.currentFont().family(), hs.value()
        if not p.text.strip():
            return
        cs = text_contours(p.text, resolve_family(p.font), p.height)
        if not cs:
            self.log("mf55: no outline for this text / font")
            return
        self.place(cs, "text")

    def place(self, contours: list[Contour], name: str) -> None:
        tool = T.PlaceTool(contours, name=name)

        def done(t):
            self.add_contours(t.result(), name)
            self.end_tool()
        self.start(tool, done)

    def qr_dialog(self) -> None:
        from mlaser.cad.qr import qr_contours
        p = self.qr
        d = _Dialog(self.w.tr_, "A260420_1", self.w)
        ed = d.add("mv20", QLineEdit(p.text))
        sz = d.add("A260420_2", _lspin(10, 100, p.size, 2))
        dn = d.add("A260420_4", _ispin(3, 30, p.density))
        md = QComboBox()
        md.addItems(["Scan lines", "Outline squares"])
        md.setCurrentIndex(p.mode)
        d.add_row("Mode", md)
        d.finish()
        if not self._run_dialog(d):
            return
        if self.exec_dialogs:
            p.text, p.size, p.density, p.mode = ed.text(), sz.value(), dn.value(), md.currentIndex()
        if not p.text:
            self.log(self.t("A260420_5"))
            return
        try:
            cs = qr_contours(p.text, p.size, p.density, (0.0, 0.0), "squares" if p.mode else "scan")
        except Exception as exc:  # noqa: BLE001 - encoder errors (too long ...)
            self.log(f"{self.t('A260420_6')} {exc}")
            return
        self.place(cs, "qr")

    # ------------------------------------------------------------ adv / advert text
    def adv_text(self) -> None:
        """精品字 (0x34ed): CGraphParaDlg section 0x21 -> ICADModule+0x17c (0x100c3170): for each
        selected contour a kerf-offset copy (FUN_100738b0, mode 2 + type) is added."""
        from mlaser.cam.kerf import compensate
        p = self.adv
        d = _Dialog(self.w.tr_, "mf251", self.w)
        kd = d.add("pd1810", QComboBox())
        kd.addItems([d.label("pd503"), d.label("pd504")])
        kd.setCurrentIndex(p.kind)
        off = d.add("pd1811", _lspin(0, 1000, p.offset))
        d.finish()
        if not self._run_dialog(d):
            return
        if self.exec_dialogs:
            p.kind, p.offset = kd.currentIndex(), off.value()
        if abs(p.offset) < 0.01:          # |offset| >= 0.01 (0x1011d800) or nothing happens
            return

        def do(doc):
            new = []
            for part in self._selected(doc):
                cs = [compensate(c, 2 + p.kind, p.offset) for c in part.contours if c.closed]
                cs = [c for c in cs if c is not None]
                if cs:
                    new.append(Part(contours=cs, craft=copy.deepcopy(part.craft), name=part.name + "+adv"))
            doc.parts.extend(new)
            return len(new)
        if not self._selected():
            return
        self.log(f"mf251: {self.edit(do, parts=self._selected())}")

    def advert_text(self) -> None:
        """广告字 (0x3440): worker mode 1 -> ICADModule+0x100 -> CFillCircle 0x100ce2d0 with
        GRP.FillGlyDir / FillCircleRadius / FillCircleStock / FillCircleSpace (app+0xb300..)."""
        from mlaser.cam.array import fill_circles
        from mlaser.cam.ordering import build_tree
        p = self.fill
        d = _Dialog(self.w.tr_, "mf254", self.w)
        ft = d.add("pd725", QComboBox())
        ft.addItems([d.label("pd720"), d.label("pd721"), d.label("pd722")])
        ft.setCurrentIndex(p.fill_type if p.fill_type < 3 else 0)
        r = d.add("pd726", _lspin(0.1, 500, p.radius))
        st = d.add("pd727", _lspin(0.1, 300, p.stock))
        sp = d.add("pd728", _lspin(0.1, 300, p.space))
        d.finish()
        if not self._run_dialog(d):
            return
        if self.exec_dialogs:
            p.fill_type, p.radius, p.stock, p.space = ft.currentIndex(), r.value(), st.value(), sp.value()
        sel = self._selected()
        if not sel:
            return
        if any(not c.closed for q in sel for c in q.contours):
            self.log(self.t("A251103_1"))
            return

        def do(doc):
            parts = self._selected(doc)
            conts = [c for q in parts for c in q.contours if c.segments]
            nodes = build_tree(conts, list(range(len(conts))))
            new = []
            for n in nodes:
                if n.depth % 2 != 1:
                    continue
                holes = [k.contour for k in n.children if k.contour.closed]
                new += fill_circles(n.contour, holes, p, layer=parts[0].craft.layer)
            doc.parts.extend(new)
            return len(new)
        n = self.edit(do, parts=sel)
        self.log(f"mf254: {n} part(s)" if n else self.t("A251103_0"))

    # ------------------------------------------------------------ trim / extend / split
    def pick(self, name: str, op: Callable[[list, tuple, float], bool]) -> None:
        def action(pt) -> bool:
            tol = PICK_PX * self.w.canvas.pixel_size()
            trial = copy.deepcopy(self.w.document.parts)
            if not op(trial, pt, tol):
                return False
            self.edit(lambda doc: doc.parts.__setitem__(slice(None), trial))
            return True
        self.start(T.PickTool(action, name), lambda t: self.end_tool(), snap=False)

    def trim(self):
        self.pick("trim", ED.trim_at)

    def extend(self):
        self.pick("extend", ED.extend_at)

    def split(self):
        self.pick("split", ED.split_at)

    # ------------------------------------------------------------ explode / merge
    def explode(self):
        sel = self._selected()
        if not sel:
            return

        def do(doc):
            out, n = [], 0
            for p in doc.parts:
                if p.selected and any(len(c.segments) > 1 for c in p.contours):
                    pieces = ED.explode_parts([p])
                    n += len(pieces)
                    out += pieces
                else:
                    out.append(p)
            doc.parts = out
            doc.extra.pop("order", None)
            return n
        self.log(f"mf85: {self.edit(do, parts=sel)}")

    def merge_dialog(self):
        p = self.merge
        d = _Dialog(self.w.tr_, "mf84", self.w)
        tol = d.add("pd368", _lspin(0.01, 100, p.tolerance, 3, "small_length"))
        d.finish()
        if not self._run_dialog(d):
            return
        if self.exec_dialogs:
            p.tolerance = tol.value()
        sel = self._selected()
        if len([c for q in sel for c in q.contours if not c.closed]) < 1:
            return

        def do(doc):
            parts = self._selected(doc)
            idx = doc.parts.index(parts[0])
            merged = ED.merge_parts(parts, p.tolerance)
            doc.parts = [q for q in doc.parts if q not in parts]
            doc.parts[idx:idx] = merged
            doc.extra.pop("order", None)
            return len(merged)
        self.log(f"mf84: {self.edit(do, parts=sel)}")

    # ------------------------------------------------------------ align / size
    def align_left(self):
        if len(self._selected()) >= 2:
            self.edit(lambda doc: AR.align(self._selected(doc), 0), parts=self._selected())

    def size_dialog(self):
        sel = self._selected() or list(self.w.document.parts)
        if not any(q.contours for q in sel):
            return
        x0, y0, x1, y1 = AR.bbox(sel)
        p = self.size
        p.width, p.height = x1 - x0, y1 - y0
        d = _Dialog(self.w.tr_, "mf591", self.w)
        wv = d.add("gp64", _lspin(0.001, 1e6, p.width))
        hv = d.add("gp63", _lspin(0.001, 1e6, p.height))
        lk = d.add("pd659", QCheckBox())
        lk.setChecked(p.lock)
        an = d.add("gp65", QComboBox())
        an.addItems(["↖", "↑", "↗", "←", "·", "→", "↙", "↓", "↘"])
        an.setCurrentIndex(p.anchor - 1)
        d.finish()
        if not self._run_dialog(d):
            return
        if self.exec_dialogs:
            p.width, p.height, p.lock, p.anchor = wv.value(), hv.value(), lk.isChecked(), an.currentIndex() + 1

        def do(doc):
            parts = self._selected(doc) or list(doc.parts)
            return AR.resize(parts, p.width, p.height, p.lock, p.anchor)
        self.edit(do, parts=sel)

    # ------------------------------------------------------------ selection
    def select_similar(self):
        n = AR.select_similar(self.w.document)
        self._sync_selection()
        self.log(f"mf37: {n} contour(s) selected")

    def select_depth(self, sub: int):
        AR.select_by_topology(self.w.document, sub)
        self._sync_selection()

    # ------------------------------------------------------------ groups
    def group(self):
        if len([q for q in self._selected() if q.contours]) >= 2:
            self.edit(AR.group, parts=self._selected())

    def explode_group(self, selected_only: bool):
        doc = self.w.document
        groups = [q for q in doc.parts if AR.is_group(q) and (q.selected or not selected_only)]
        if groups:
            self.edit(lambda d: AR.explode_group(d, selected_only), parts=groups)

    def select_groups(self):
        AR.select_groups(self.w.document)
        self._sync_selection()

    # ------------------------------------------------------------ measure
    def measure(self):
        def done(t: T.MeasureTool):
            self.end_tool()
            if t.measurement is None:
                return
            self.last_measure = t.measurement
            dx, dy, L = t.measurement
            u = units.current()
            # a measurement: the fine resolution (3 decimals in mm, 4 in inches)
            msg = self.w.tr_.format("mv17", u.fmt(dx, "small_length"), u.fmt(dy, "small_length"),
                                    u.fmt(L, "small_length"))   # view.measure_result
            self.log(msg)
            try:
                self.w.statusBar().showMessage(msg, 15000)
            except Exception:  # noqa: BLE001
                pass
        self.start(T.MeasureTool(), done)

    # ------------------------------------------------------------ fine-tune
    def _config_changed(self, key: str) -> None:
        if key in ("*", "design.nudge_step", "design.nudge_auto"):
            cfg = self.w.config
            self.fine.step = float(cfg.get("design.nudge_step", 1.0))
            self.fine.auto = bool(cfg.get("design.nudge_auto", True))

    def fine_tune_dialog(self):
        """mf445 keyboard fine-tuning: the arrow-key step's one home is Settings › Canvas
        (``design.nudge_step`` / ``design.nudge_auto``); the command opens that page."""
        opener = getattr(self.w, "settings_dialog", None)
        if self.exec_dialogs and opener is not None:
            opener().show_page("canvas")
        act = self.w.ribbon.actions.get("draw.fine_tune")
        if act is not None:
            act.blockSignals(True)
            act.setChecked(self.fine.auto)
            act.blockSignals(False)

    def step_for(self, dx: float, dy: float) -> float:
        """FUN_004d0930: fixed pd377 step, or the auto step from the selection extent."""
        if not self.fine.auto:
            return self.fine.step
        sel = self._selected()
        if not sel:
            return self.fine.step
        x0, y0, x1, y1 = AR.bbox(sel)
        return AR.auto_step((x1 - x0) if dx else (y1 - y0))

    def nudge(self, dx: float, dy: float) -> None:
        """Arrow keys (view key handler FUN_004d0480 -> FUN_004d0930 -> ICADModule+0x128)."""
        if not self._selected():
            return
        s = self.step_for(dx, dy)
        self.edit(lambda doc: AR.translate(self._selected(doc), dx * s, dy * s), parts=self._selected())


# --------------------------------------------------------------------------- wiring
def _commands(ext: DrawExtension) -> dict[str, Callable[[], None]]:
    cmds: dict[str, Callable[[], None]] = {c: (lambda c=c: ext.draw_tool(c)) for c in T.TOOLS}
    cmds.update({
        "draw.text": ext.text_dialog, "draw.qr": ext.qr_dialog,
        "draw.adv_text": ext.adv_text, "draw.advert_text": ext.advert_text,
        "draw.trim": ext.trim, "draw.extend": ext.extend, "draw.fine_tune": ext.fine_tune_dialog,
        "geom.explode": ext.explode, "geom.merge": ext.merge_dialog, "geom.split": ext.split,
        "xform.align": ext.align_left, "xform.size": ext.size_dialog,
        "select.similar": ext.select_similar,
        "select.inner": lambda: ext.select_depth(1),     # 0x331e, mf651
        "select.outer": lambda: ext.select_depth(2),     # 0x331f, mf652
        "tool.group": ext.group, "tool.group_select_all": ext.select_groups,
        "tool.group_explode": lambda: ext.explode_group(True),
        "tool.group_explode_all": lambda: ext.explode_group(False),
        "tool.measure": ext.measure,
    })
    return cmds


def register_bundled_fonts() -> list[str]:
    """Make the bundled fonts (Noto Sans SC / Arabic subsets, ``mlaser.cad.fonts``) available to
    the text tool's font combo; returns the families Qt registered."""
    from PySide6.QtGui import QFontDatabase

    from mlaser.cad.fonts import bundled_fonts
    fams: list[str] = []
    for ref in bundled_fonts():
        fid = QFontDatabase.addApplicationFont(ref.path)
        if fid >= 0:
            fams += QFontDatabase.applicationFontFamilies(fid)
    return fams


def _bind(ext: DrawExtension, cmd: str, fn: Callable[[], None]) -> Callable:
    def run():
        try:
            fn()
        except Exception:
            ext.end_tool()              # a failed tool must not stay active
            raise
    return run


def register(ctx, exec_dialogs: bool = True) -> DrawExtension:
    """Bind the drawing / CAD-editing commands (``ctx``: PluginContext)."""
    ext = DrawExtension(ctx, exec_dialogs)
    register_bundled_fonts()
    for cmd, fn in _commands(ext).items():
        ctx.register_command(cmd, _bind(ext, cmd, fn))
    # choosing a view mode ends the active drawing tool (FUN_004d1780 resets the view mode)
    ctx.on_mode_changed(lambda _mode: ext.end_tool())
    # arrow keys use the fine-tune step
    ctx.provide("nudge", ext.nudge)
    return ext
