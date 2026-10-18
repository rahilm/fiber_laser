"""Ribbon actions and dialogs for the advanced CAM features (agent C, phase 2).

``register(window)`` wires the existing ribbon commands of ``ribbon_spec`` that were
"not implemented" (tool.array, tool.scan_*, tool.share_edge*, opt.*, geom.smooth,
craft.chamfer*, craft.unload, craft.cool_auto, draw.linear_fill) to ``mlaser.cam`` /
``mlaser.motion.smooth`` and adds a "CAM+" ribbon tab for the commands that have no button
yet (fill circle mf100_1, α corner mf485/mf403, circular / fill-sheet array, full import
cleanup). Only public window attributes are used: ``ribbon`` (``actions``, ``action()``,
``addTab``), ``handlers``, ``document``, ``tr_``, ``log_panel`` and the window's edit /
refresh helpers (looked up with ``getattr`` so a renamed helper degrades to a plain
``set_document``).

Each dialog edits a parameter dataclass kept on the extension object
(``window.cam_ext``) so values persist between invocations like the original's
GraphParam block.
"""
from __future__ import annotations

import copy
import dataclasses
from typing import Any, Callable, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QFrame, QHBoxLayout, QLabel, QSpinBox, QTabWidget,
                               QToolButton, QVBoxLayout, QWidget)

from mlaser.model import Document, Part

# --------------------------------------------------------------------------- field-driven dialog
# field spec: (attribute, lang key or literal label, kind, extra)
#   kind "int"/"float": extra = (lo, hi[, decimals])
#   kind "bool": extra = None
#   kind "enum": extra = [lang keys or labels]
Field = tuple


class FieldDialog(QDialog):
    """Form editing the attributes of a dataclass instance (labels from lang keys)."""

    def __init__(self, tr, title_key: str, obj: Any, fields: list[Field], parent=None,
                 extra_widgets: Optional[list[tuple[str, QWidget]]] = None):
        super().__init__(parent)
        self.tr_ = tr
        self.obj = obj
        self.setWindowTitle(self._label(title_key, full=True))
        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)
        self.editors: dict[str, QWidget] = {}
        for attr, key, kind, extra in fields:
            v = getattr(obj, attr)
            if kind == "int":
                w = QSpinBox()
                w.setRange(int(extra[0]), int(extra[1]))
                w.setValue(int(v))
            elif kind == "float":
                w = QDoubleSpinBox()
                w.setDecimals(extra[2] if len(extra) > 2 else 3)
                w.setRange(float(extra[0]), float(extra[1]))
                w.setValue(float(v))
            elif kind == "bool":
                w = QCheckBox()
                w.setChecked(bool(v))
            else:
                w = QComboBox()
                w.addItems([self._label(k) for k in extra])
                w.setCurrentIndex(int(v))
            form.addRow(self._label(key), w)
            self.editors[attr] = w
        for label, w in extra_widgets or []:
            form.addRow(label, w)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr.tr("ap1", "OK"))
        bb.button(QDialogButtonBox.Cancel).setText(tr.tr("ap0", "Cancel"))
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _label(self, key: str, full: bool = False) -> str:
        if not self.tr_.has(key):
            return key
        return self.tr_.tr(key) if full else self.tr_.tr_param(key)[1]

    def store(self) -> None:
        for attr, w in self.editors.items():
            if isinstance(w, QSpinBox):
                setattr(self.obj, attr, w.value())
            elif isinstance(w, QDoubleSpinBox):
                setattr(self.obj, attr, w.value())
            elif isinstance(w, QCheckBox):
                setattr(self.obj, attr, w.isChecked())
            else:
                setattr(self.obj, attr, w.currentIndex())

    def _ok(self) -> None:
        self.store()
        self.accept()


# --------------------------------------------------------------------------- helpers
def _targets(doc: Document) -> list[Part]:
    sel = [p for p in doc.parts if p.selected]
    return sel or list(doc.parts)


def _all_contours(parts):
    return [c for p in parts for c in p.contours if c.segments]


@dataclasses.dataclass
class CircularArrayParams:
    count: int = 6
    center_x: float = 0.0
    center_y: float = 0.0
    total_angle_deg: float = 360.0
    rotate_items: bool = True
    use_bbox_center: bool = True       # centre = selection bbox centre + (center_x, center_y)


@dataclasses.dataclass
class SheetFillParams:
    width: float = 3000.0
    height: float = 1500.0
    gap: float = 5.0
    margin: float = 5.0


class CamExtension:
    """State + handlers. ``exec_dialogs=False`` (tests) applies the stored params directly."""

    def __init__(self, window, exec_dialogs: bool = True):
        from mlaser.cam import array as A
        from mlaser.cam import cleanup as CL
        self.w = window
        self.exec_dialogs = exec_dialogs
        self.array = A.ArrayParams()
        self.circ = CircularArrayParams()
        self.sheet = SheetFillParams()
        self.fill = A.FillParams()
        self.linear = A.FillParams(fill_type=3, space=2.0, stock=1.0)
        self.cleanup = CL.CleanupParams()
        self.fly = self._new("mlaser.cam.flycut", ("FlyCutParams", "FlycutParams", "FlyParams"))

    # ---------------------------------------------------------------- infrastructure
    @staticmethod
    def _new(module: str, names):
        import importlib
        try:
            m = importlib.import_module(module)
        except Exception:  # noqa: BLE001
            return None
        for n in names:
            cls = getattr(m, n, None)
            if cls is not None:
                try:
                    return cls()
                except Exception:  # noqa: BLE001
                    return None
        return None

    def log(self, msg: str) -> None:
        lp = getattr(self.w, "log_panel", None)
        if lp is not None:
            lp.log(msg)

    def edit(self, fn: Callable[[Document], Any]) -> Any:
        """Undoable document edit through the window's helpers."""
        snap = getattr(self.w, "_snapshot", None)
        if callable(snap):
            snap()
        res = fn(self.w.document)
        refresh = getattr(self.w, "_refresh", None)
        canvas = getattr(self.w, "canvas", None)
        if canvas is not None and hasattr(canvas, "set_document"):
            canvas.set_document(self.w.document)
        if callable(refresh):
            refresh()
        else:
            self.w.set_document(self.w.document, getattr(self.w, "path", None))
        return res

    def ask(self, title_key: str, obj, fields, extra=None) -> bool:
        if obj is None:
            return False
        d = FieldDialog(self.w.tr_, title_key, obj, fields, self.w, extra)
        if not self.exec_dialogs:
            d.store()
            return True
        return bool(d.exec())

    # ---------------------------------------------------------------- array (mf79)
    def array_dialog(self) -> None:
        f = [("rows", "pd332", "int", (1, 10000)), ("cols", "pd333", "int", (1, 10000)),
             ("offset_type", "pd334", "enum", ["pd301", "pd302"]),
             ("row_gap", "pd335", "float", (-1e5, 1e5)), ("col_gap", "pd336", "float", (-1e5, 1e5)),
             ("row_dir", "pd337", "enum", ["pd303", "pd304"]),
             ("col_dir", "pd338", "enum", ["pd305", "pd306"])]
        if self.ask("mf79", self.array, f):
            from mlaser.cam.array import apply_rect_array
            n = self.edit(lambda doc: apply_rect_array(doc, self.array))
            self.log(f"mf79: {n} copies")

    def circular_array_dialog(self) -> None:
        f = [("count", "Count", "int", (2, 10000)), ("center_x", "Centre X", "float", (-1e5, 1e5)),
             ("center_y", "Centre Y", "float", (-1e5, 1e5)),
             ("total_angle_deg", "Total angle (deg)", "float", (-360, 360)),
             ("rotate_items", "Rotate copies", "bool", None),
             ("use_bbox_center", "Centre relative to selection centre", "bool", None)]
        if self.ask("Circular array", self.circ, f):
            from mlaser.cam.array import circular_array, parts_bbox
            prm = self.circ

            def do(doc):
                parts = _targets(doc)
                cx, cy = prm.center_x, prm.center_y
                if prm.use_bbox_center:
                    x0, y0, x1, y1 = parts_bbox(parts)
                    cx, cy = cx + (x0 + x1) / 2, cy + (y0 + y1) / 2
                new = circular_array(parts, prm.count, (cx, cy), prm.total_angle_deg, prm.rotate_items)
                doc.parts.extend(new)
                return len(new)
            self.log(f"circular array: {self.edit(do)} copies")

    def sheet_array_dialog(self) -> None:
        f = [("width", "Sheet width", "float", (1, 1e5)), ("height", "Sheet height", "float", (1, 1e5)),
             ("gap", "pd302", "float", (0, 1e4)), ("margin", "pd727", "float", (0, 1e4))]
        if self.ask("Fill sheet", self.sheet, f):
            from mlaser.cam.array import fill_sheet_array
            prm = self.sheet

            def do(doc):
                parts = _targets(doc)
                new = fill_sheet_array(parts, (0.0, 0.0, prm.width, prm.height), prm.gap, prm.margin)
                if not new:
                    return 0
                doc.parts = [p for p in doc.parts if p not in parts] + new
                return len(new)
            self.log(f"fill sheet: {self.edit(do)} instances")

    # ---------------------------------------------------------------- fill (mf100_1, A250707_10)
    def _fill(self, linear: bool) -> None:
        from mlaser.cam.array import fill_circles, linear_fill
        from mlaser.cam.ordering import build_tree
        prm = self.linear if linear else self.fill
        if linear:
            f = [("angle_deg", "A250707_3", "float", (-360, 360)), ("space", "A250707_4", "float", (0.01, 1e4)),
                 ("stock", "A250707_5", "float", (0, 1e4)), ("keep_original", "A250707_6", "bool", None)]
            title = "A250707_10"
        else:
            f = [("fill_type", "pd725", "enum", ["pd720", "pd721", "pd722"]),
                 ("radius", "pd726", "float", (0.1, 500)), ("stock", "pd727", "float", (0.1, 300)),
                 ("space", "pd728", "float", (0.1, 300))]
            title = "mf100_1"
        if not self.ask(title, prm, f):
            return

        def do(doc):
            parts = _targets(doc)
            if any(not c.closed for c in _all_contours(parts)):
                self.log(self.w.tr_.tr("A251103_1"))
                return 0
            conts = _all_contours(parts)
            nodes = build_tree(conts, list(range(len(conts))))
            new = []
            for n in nodes:
                if n.depth % 2 != 1:
                    continue
                holes = [k.contour for k in n.children if k.contour.closed]
                layer = parts[0].craft.layer if parts else None
                fn = linear_fill if linear else fill_circles
                new += fn(n.contour, holes, prm, layer=layer)
            if linear and not prm.keep_original:
                doc.parts = [p for p in doc.parts if p not in parts]
            doc.parts.extend(new)
            return len(new)
        self.log(f"{title}: {self.edit(do)} items")

    # ---------------------------------------------------------------- cleanup (mf470 Optimize)
    def cleanup_dialog(self) -> None:
        f = [("remove_small", "pd363", "bool", None), ("small_gate", "pd364", "float", (0.01, 10)),
             ("remove_overlap", "pd365", "bool", None), ("overlap_gate", "pd366", "float", (0.01, 0.5)),
             ("merge_type", "pd367", "enum", ["None", "Direction first", "Length first", "Distance first"]),
             ("connect_gate", "pd368", "float", (0.01, 100)), ("smooth", "pd369", "bool", None),
             ("smooth_accuracy", "pd370", "float", (0.01, 10)), ("close_gap", "Close gaps up to", "float", (0, 100))]
        if self.ask("mf470", self.cleanup, f):
            from mlaser.cam.cleanup import cleanup_document
            res = self.edit(lambda doc: cleanup_document(doc, self.cleanup,
                                                         only_selected=any(p.selected for p in doc.parts)))
            self.log(f"mf470: {res['before']} -> {res['after']} contours")

    def dedup(self) -> None:
        """mf471 Clear Duplicate Lines (0x100c0a30, OverlapGate)."""
        from mlaser.cam.cleanup import remove_duplicate_contours

        def do(doc):
            removed = 0
            allc = [(p, c) for p in doc.parts for c in p.contours]
            keep = {id(c) for c in remove_duplicate_contours([c for _, c in allc], self.cleanup.overlap_gate)}
            for p in doc.parts:
                n0 = len(p.contours)
                p.contours = [c for c in p.contours if id(c) in keep]
                removed += n0 - len(p.contours)
            doc.parts = [p for p in doc.parts if p.contours]
            return removed
        self.log(f"Remove: {self.edit(do)}")

    def join(self) -> None:
        """mf476 Merge connected lines (0x100c0300, ConnectGate + MegerConnectGraphType)."""
        from mlaser.cam.cleanup import merge_connected
        mode = self.cleanup.merge_type or 1

        def do(doc):
            parts = _targets(doc)
            if not parts:
                return 0
            craft = copy.deepcopy(parts[0].craft)
            res = merge_connected(_all_contours(parts), self.cleanup.connect_gate, mode)
            doc.parts = [p for p in doc.parts if p not in parts] + [Part(contours=[c], craft=copy.deepcopy(craft)) for c in res]
            return len(res)
        self.log(f"mf476: {self.edit(do)} contours")

    def smooth(self) -> None:
        """mf474 / mf213 Curve smoothness: CircleFit-style arc fitting at SmoothAccuracy."""
        from mlaser.cam.cleanup import smooth_contour

        def do(doc):
            n = 0
            for p in _targets(doc):
                new = [smooth_contour(c, self.cleanup.smooth_accuracy) for c in p.contours]
                n += sum(len(a.segments) - len(b.segments) for a, b in zip(p.contours, new))
                p.contours = new
            return n
        self.log(f"mf474: {self.edit(do)} segments removed")

    # ---------------------------------------------------------------- feature hooks (other modules)
    def call(self, module: str, func: str, *args, **kw):
        import importlib
        try:
            fn = getattr(importlib.import_module(module), func)
        except Exception as exc:  # noqa: BLE001
            self.log(f"{module}.{func}: unavailable ({exc})")
            return None
        return fn(*args, **kw)


# --------------------------------------------------------------------------- ribbon wiring
REWIRE = {
    "tool.array": "array_dialog",
    "draw.linear_fill": "linear_fill",
    "tool.optimize": "cleanup_dialog",
    "opt.dedup": "dedup",
    "opt.join": "join",
    "opt.smooth": "smooth",
    "geom.smooth": "smooth",
}

EXTRA_BUTTONS = [
    ("camx.fill_circle", "mf100_1", "fill_circle"),
    ("camx.array_circular", "Circular array", "circular_array_dialog"),
    ("camx.array_sheet", "Fill sheet", "sheet_array_dialog"),
    ("camx.cleanup", "mf470", "cleanup_dialog"),
]


def _bind(ext: CamExtension, name: str) -> Callable[[], None]:
    def run(_=False):
        try:
            getattr(ext, name)()
        except Exception as exc:  # noqa: BLE001 - a CAM failure must not kill the UI
            ext.log(f"{name}: {exc}")
    return run


def register(window, exec_dialogs: bool = True) -> CamExtension:
    """Install the advanced-CAM commands on ``window`` (a ``MainWindow``)."""
    ext = CamExtension(window, exec_dialogs)
    ext.fill_circle = lambda: ext._fill(False)
    ext.linear_fill = lambda: ext._fill(True)
    _install_feature_handlers(ext)
    window.cam_ext = ext
    rib = window.ribbon
    handlers = getattr(window, "handlers", {})
    for cmd, name in list(REWIRE.items()) + list(FEATURE_REWIRE.items()):
        if not hasattr(ext, name):
            continue
        fn = _bind(ext, name)
        act = rib.actions.get(cmd)
        if act is not None:
            try:
                act.triggered.disconnect()
            except (RuntimeError, TypeError):
                pass
            act.triggered.connect(fn)
        handlers[cmd] = fn
    # extra tab
    page = QWidget()
    lay = QHBoxLayout(page)
    lay.setContentsMargins(2, 2, 2, 0)
    box = QFrame()
    box.setFrameShape(QFrame.StyledPanel)
    row = QHBoxLayout(box)
    for cmd, key, name in EXTRA_BUTTONS + FEATURE_BUTTONS:
        if not hasattr(ext, name):
            continue
        act = rib.action(cmd, key)
        fn = _bind(ext, name)
        act.triggered.connect(fn)
        handlers[cmd] = fn
        tb = QToolButton()
        tb.setDefaultAction(act)
        tb.setAutoRaise(True)
        if act.icon().isNull():
            tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        else:
            tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            tb.setIconSize(QSize(32, 32))
        row.addWidget(tb)
    lay.addWidget(box)
    lay.addStretch(1)
    rib.addTab(page, "CAM+")
    ext.tab = page
    return ext


# --------------------------------------------------------------------------- corners / common edge
@dataclasses.dataclass
class FilletParams:
    radius: float = 1.0           # pd600 GRP.ArcRoundRadius [0.1, 10]
    min_angle: float = 0.0        # pd601 GRP.ArcRoundMinAngle_deg
    max_angle: float = 90.0       # pd602 GRP.ArcRoundMaxAngle_deg


@dataclasses.dataclass
class UnloadParams:
    radius: float = 0.5           # pd603 GRP.UnloadAngleRadius [0.1, 10]


@dataclasses.dataclass
class AlphaParams:
    max_angle: float = 90.0       # pd607 GRP.AlphaMaxAngle_deg [1, 170]
    min_edge: float = 3.0         # pd608 GRP.AlphaMinEdgeLen [1, 50]
    lead: float = 8.0             # pd609 GRP.AlphaLen [0.5, 10]
    mode: int = 0                 # pd614 GRP.AlphaType 0 auto, 1 outside, 2 inner, 3 both


@dataclasses.dataclass
class CoolParams:
    lead_point: bool = False      # pd604 Cool Lead Position
    sharp: bool = True            # pd605 Cool Sharp Position
    max_angle: float = 90.0       # pd606 Max Sharp Angle


@dataclasses.dataclass
class RectShareParams:
    kind: int = 0                 # A250707_0 type (A250707_7 / _8 / _9)
    overcut_on: bool = False      # A250707_1
    overcut: float = 0.0          # A250707_2


def _reset_craft_after_geometry(p: Part) -> None:
    """After a corner edit the original recomputes kerf child / micro-joints (0x10074e00,
    0x1006c930); the rebuild recomputes kerf on the fly and drops stale joint positions."""
    p.craft.micro_joints = []
    p.craft.extra.pop("cool_points", None)


def _depths(doc: Document):
    from mlaser.cam.ordering import build_tree
    keys, conts = [], []
    for pi, p in enumerate(doc.parts):
        for ci, c in enumerate(p.contours):
            if c.segments:
                keys.append((pi, ci))
                conts.append(c)
    return {n.key: n.depth for n in build_tree(conts, keys)}


def _install_feature_handlers(ext: CamExtension) -> None:
    ext.fillet_p, ext.unload_p, ext.alpha_p, ext.cool_p = FilletParams(), UnloadParams(), AlphaParams(), CoolParams()
    ext.rect_p = RectShareParams()

    def per_contour(fn, closed_only=True):
        def do(doc):
            n = 0
            for p in _targets(doc):
                new = []
                for c in p.contours:
                    if closed_only and not c.closed:
                        new.append(c)
                        continue
                    r = fn(c, p)
                    n += int(r is not c and len(r.segments) != len(c.segments))
                    new.append(r)
                if new != p.contours:
                    p.contours = new
                    _reset_craft_after_geometry(p)
            return n
        return do

    def fillet_auto():
        from mlaser.cam.corners import fillet
        f = [("radius", "pd600", "float", (0.1, 10)), ("min_angle", "pd601", "float", (0, 180)),
             ("max_angle", "pd602", "float", (0, 180))]
        if ext.ask("mf401", ext.fillet_p, f):
            q = ext.fillet_p
            n = ext.edit(per_contour(lambda c, p: fillet(c, q.radius, q.min_angle, q.max_angle)))
            ext.log(f"mf401: {n} contours")

    def unload():
        from mlaser.cam.corners import unload_loop
        if ext.ask("mf404", ext.unload_p, [("radius", "pd603", "float", (0.1, 10))]):
            n = ext.edit(per_contour(lambda c, p: unload_loop(c, ext.unload_p.radius)))
            ext.log(f"mf404: {n} contours")

    def alpha():
        from mlaser.cam.corners import alpha_corners
        f = [("max_angle", "pd607", "float", (1, 170)), ("min_edge", "pd608", "float", (1, 50)),
             ("lead", "pd609", "float", (0.5, 10)), ("mode", "pd614", "enum", ["pd610", "pd611", "pd612", "pd613"])]
        if not ext.ask("mf485", ext.alpha_p, f):
            return
        q = ext.alpha_p

        def do(doc):
            depth = _depths(doc)
            n = 0
            for pi, p in enumerate(doc.parts):
                if p not in _targets(doc):
                    continue
                new = []
                for ci, c in enumerate(p.contours):
                    if not c.closed or not c.segments:
                        new.append(c)
                        continue
                    r = alpha_corners(c, q.max_angle, q.min_edge, q.lead, q.mode, depth.get((pi, ci), 1))
                    n += int(len(r.segments) != len(c.segments))
                    if len(r.segments) != len(c.segments):
                        r.extra = dict(r.extra)
                        r.extra["alpha_source"] = c
                    new.append(r)
                p.contours = new
                _reset_craft_after_geometry(p)
            return n
        ext.log(f"mf485: {ext.edit(do)} contours")

    def alpha_clear():
        """mf403: restore contours saved by the alpha command."""
        def do(doc):
            n = 0
            for p in _targets(doc):
                new = []
                for c in p.contours:
                    src = c.extra.get("alpha_source") if isinstance(c.extra, dict) else None
                    new.append(src if src is not None else c)
                    n += src is not None
                p.contours = new
            return n
        ext.log(f"mf403: {ext.edit(do)} contours")

    def cool_auto():
        from mlaser.cam.corners import auto_cool_points
        f = [("lead_point", "pd604", "bool", None), ("sharp", "pd605", "bool", None),
             ("max_angle", "pd606", "float", (0, 180))]
        if not ext.ask("mf407", ext.cool_p, f):
            return
        q = ext.cool_p

        def do(doc):
            n = 0
            for p in _targets(doc):
                pts = []
                for c in p.contours[:1]:      # Craft.extra['cool_points'] applies to the part's contours
                    if c.closed:
                        pts = auto_cool_points(c, q.max_angle, q.sharp, q.lead_point)
                p.craft.extra["cool_points"] = pts
                n += len(pts)
            return n
        ext.log(f"mf407: {ext.edit(do)} points")

    def cool_clear():
        def do(doc):
            for p in _targets(doc):
                p.craft.extra.pop("cool_points", None)
                p.craft.cool_point = None
        ext.edit(do)

    def share(c_type: bool):
        from mlaser.cam.commonedge import cut_length, detect_rect_grid, rect_share_edge, share_edge

        def do(doc):
            parts = _targets(doc)
            before = cut_length(parts)
            new = share_edge(parts, c_type)
            doc.parts = [p for p in doc.parts if p not in parts] + new
            return before, cut_length(new)
        b, a = ext.edit(do)
        ext.log(f"mf250: cut length {b:.1f} -> {a:.1f} mm")

    def rect_share():
        from mlaser.cam.commonedge import detect_rect_grid, rect_share_edge
        f = [("kind", "A250707_0", "enum", ["A250707_7", "A250707_8", "A250707_9"]),
             ("overcut_on", "A250707_1", "bool", None), ("overcut", "A250707_2", "float", (0, 100))]
        if not ext.ask("A250707_11", ext.rect_p, f):
            return
        q = ext.rect_p

        def do(doc):
            parts = _targets(doc)
            grid = detect_rect_grid(parts)
            if grid is None:
                ext.log("A250707_11: selection is not a complete grid of equal rectangles")
                return 0
            paths = rect_share_edge(grid, q.kind, q.overcut if q.overcut_on else 0.0)
            craft = copy.deepcopy(parts[0].craft)
            craft.kerf_comp = -1
            doc.parts = [p for p in doc.parts if p not in parts] + [
                Part(contours=[c], craft=copy.deepcopy(craft), name="share edge") for c in paths]
            return len(paths)
        ext.log(f"A250707_11: {ext.edit(do)} paths")

    ext.fillet_auto, ext.unload, ext.alpha, ext.alpha_clear = fillet_auto, unload, alpha, alpha_clear
    ext.cool_auto, ext.cool_clear = cool_auto, cool_clear
    ext.share_edge = lambda: share(False)
    ext.share_edge_c = lambda: share(True)
    ext.rect_share = rect_share
    _install_flycut(ext)


FEATURE_REWIRE: dict[str, str] = {
    "craft.chamfer": "fillet_auto", "craft.chamfer_auto": "fillet_auto", "craft.chamfer_manual": "fillet_auto",
    "craft.unload": "unload", "craft.cool_auto": "cool_auto", "craft.cool_clear": "cool_clear",
    "tool.share_edge": "share_edge", "tool.share_edge_c": "share_edge_c",
    "tool.scan_cut": "flycut_auto", "tool.scan_circle": "flycut_circle", "tool.scan_line": "flycut_line",
}
FEATURE_BUTTONS: list[tuple[str, str, str]] = [
    ("camx.alpha", "mf485", "alpha"), ("camx.alpha_clear", "mf403", "alpha_clear"),
    ("camx.rect_share", "A250707_11", "rect_share"),
]


def _install_flycut(ext: CamExtension) -> None:
    """Fly-cut handlers (mf112 Scan Cutting: mf112_1 circle, mf112_2 segment)."""
    from mlaser.cam import flycut as F
    ext.circle_fly_p, ext.line_fly_p = F.CircleFlyParams(), F.LineFlyParams()

    def run(kind):
        def do(doc):
            sel = any(p.selected for p in doc.parts)
            prm = ext.circle_fly_p if kind == F.SCAN_CIRCLE else ext.line_fly_p
            return F.apply_fly_cut(doc, kind, prm, selected_only=sel)
        part = ext.edit(do)
        if part is None:
            ext.log(ext.w.tr_.tr("mf161"))
        else:
            ext.log(f"mf112: {len(part.extra.get('scan_paths', []))} scan path(s)")

    def circle():
        f = [("sort_type", "pd707", "int", (0, 5)), ("max_fly_len", "pd708", "float", (0, 1e4)),
             ("first_single_contour", "pd851", "bool", None), ("velocity_ratio", "pd290", "float", (0.01, 10))]
        if ext.ask("mf112_1", ext.circle_fly_p, f):
            run(F.SCAN_CIRCLE)

    def line():
        f = [("start_corner", "pd715", "enum", ["pd710", "pd711", "pd712", "pd713"]),
             ("collinear_tol", "pd716", "float", (0, 100)), ("max_link_len", "pd717", "float", (0, 1e4)),
             ("max_fly_len", "pd718", "float", (0, 1e4))]
        if ext.ask("mf112_2", ext.line_fly_p, f):
            run(F.SCAN_LINE)

    def auto():
        """mf112: circle fly cut if the selection has circles, else line fly cut."""
        parts = _targets(ext.w.document)
        has_circle = any(F.detect_circle(c) for c in _all_contours(parts))
        (circle if has_circle else line)()

    ext.flycut_circle, ext.flycut_line, ext.flycut_auto = circle, line, auto
