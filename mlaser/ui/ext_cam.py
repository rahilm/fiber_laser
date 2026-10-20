"""Dialogs of the advanced CAM features (plugin ``ext_cam``).

``register(ctx)`` (PluginContext) binds tool.array, tool.scan_*, tool.share_edge*, opt.*,
geom.smooth, craft.chamfer*, craft.unload, craft.cool_*, draw.linear_fill and a "CAM+" ribbon
tab (fill circle mf100_1, α corner mf485/mf403, circular / fill-sheet array, full import
cleanup).  The operations themselves are :mod:`mlaser.ops.cam` (Qt-free, unit-tested); this
module asks for the parameters and runs them as undoable edits (``ctx.edit``).

Each dialog edits a parameter dataclass kept on the extension object
(``window.extension("ext_cam")``) so values persist between invocations like the original's
GraphParam block.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QSpinBox, QVBoxLayout, QWidget)

from mlaser.model import Document
from mlaser.ops import cam as OC
from mlaser.services import units
from mlaser.ops.cam import (AlphaParams, CircularArrayParams, CoolParams, FilletParams,  # noqa: F401
                            RectShareParams, SheetFillParams, UnloadParams)
from mlaser.ops.document import targets as _targets  # noqa: F401 - old helper name
from mlaser.ui.units_qt import QuantitySpinBox

# --------------------------------------------------------------------------- field-driven dialog
# field spec: (attribute, lang key or literal label, kind, extra)
#   kind "int"/"float": extra = (lo, hi[, decimals])
#   kind "len"/"slen": a length / small length (tolerance) in mm, shown in the display units
#     (mlaser.ui.units_qt.QuantitySpinBox); extra = (lo, hi[, decimals]) in mm
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
            elif kind in ("len", "slen"):
                w = QuantitySpinBox("length" if kind == "len" else "small_length",
                                    minimum=float(extra[0]), maximum=float(extra[1]),
                                    decimals=extra[2] if len(extra) > 2 else 3, value=float(v))
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
            elif isinstance(w, (QDoubleSpinBox, QuantitySpinBox)):
                setattr(self.obj, attr, w.value())
            elif isinstance(w, QCheckBox):
                setattr(self.obj, attr, w.isChecked())
            else:
                setattr(self.obj, attr, w.currentIndex())

    def _ok(self) -> None:
        self.store()
        self.accept()


# --------------------------------------------------------------------------- extension
class CamExtension:
    """Dialog state + handlers. ``exec_dialogs=False`` (tests) applies the stored params directly."""

    def __init__(self, ctx, exec_dialogs: bool = True):
        from mlaser.cam import array as A
        from mlaser.cam import cleanup as CL
        from mlaser.cam import flycut as F
        self.ctx = ctx
        self.w = ctx.window
        self.exec_dialogs = exec_dialogs
        self.array = A.ArrayParams()
        self.circ = CircularArrayParams()
        self.sheet = SheetFillParams()
        self.fill = A.FillParams()
        self.linear = A.FillParams(fill_type=3, space=2.0, stock=1.0)
        self.cleanup = CL.CleanupParams()
        self.fillet_p, self.unload_p, self.alpha_p, self.cool_p = FilletParams(), UnloadParams(), AlphaParams(), CoolParams()
        self.rect_p = RectShareParams()
        self.circle_fly_p, self.line_fly_p = F.CircleFlyParams(), F.LineFlyParams()

    # ---------------------------------------------------------------- infrastructure
    def log(self, msg: str) -> None:
        self.ctx.log(msg)

    def edit(self, fn: Callable[[Document], Any], label: str = "CAM", parts=None) -> Any:
        """Undoable document edit (``ctx.edit``); an :class:`OpError` is logged and undone."""
        try:
            return self.ctx.edit(fn, label, parts=parts)
        except OC.OpError as e:
            self.log(self.w.tr_.tr(e.key) if self.w.tr_.has(e.key) and str(e) == e.key else f"{e.key}: {e}")
            return None

    def _targets(self):
        return _targets(self.ctx.document)

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
             ("row_gap", "pd335", "len", (-1e5, 1e5)), ("col_gap", "pd336", "len", (-1e5, 1e5)),
             ("row_dir", "pd337", "enum", ["pd303", "pd304"]),
             ("col_dir", "pd338", "enum", ["pd305", "pd306"])]
        if self.ask("mf79", self.array, f):
            n = self.edit(lambda doc: OC.rect_array(doc, self.array), "array", parts=())
            self.log(f"mf79: {n} copies")

    def circular_array_dialog(self) -> None:
        f = [("count", "Count", "int", (2, 10000)), ("center_x", "Centre X", "len", (-1e5, 1e5)),
             ("center_y", "Centre Y", "len", (-1e5, 1e5)),
             ("total_angle_deg", "Total angle (deg)", "float", (-360, 360)),
             ("rotate_items", "Rotate copies", "bool", None),
             ("use_bbox_center", "Centre relative to selection centre", "bool", None)]
        if self.ask("Circular array", self.circ, f):
            self.log(f"circular array: {self.edit(lambda doc: OC.circular_array(doc, self.circ), 'circular array', parts=())} copies")

    def sheet_array_dialog(self) -> None:
        f = [("width", "Sheet width", "len", (1, 1e5)), ("height", "Sheet height", "len", (1, 1e5)),
             ("gap", "pd302", "len", (0, 1e4)), ("margin", "pd727", "len", (0, 1e4))]
        if self.ask("Fill sheet", self.sheet, f):
            self.log(f"fill sheet: {self.edit(lambda doc: OC.sheet_fill(doc, self.sheet), 'fill sheet')} instances")

    # ---------------------------------------------------------------- fill (mf100_1, A250707_10)
    def _fill(self, linear: bool) -> None:
        prm = self.linear if linear else self.fill
        if linear:
            f = [("angle_deg", "A250707_3", "float", (-360, 360)), ("space", "A250707_4", "len", (0.01, 1e4)),
                 ("stock", "A250707_5", "len", (0, 1e4)), ("keep_original", "A250707_6", "bool", None)]
            title = "A250707_10"
        else:
            f = [("fill_type", "pd725", "enum", ["pd720", "pd721", "pd722"]),
                 ("radius", "pd726", "len", (0.1, 500)), ("stock", "pd727", "len", (0.1, 300)),
                 ("space", "pd728", "len", (0.1, 300))]
            title = "mf100_1"
        if not self.ask(title, prm, f):
            return
        n = self.edit(lambda doc: OC.fill(doc, prm, linear), title)
        if n is not None:
            self.log(f"{title}: {n} items")

    def fill_circle(self) -> None:
        self._fill(False)

    def linear_fill(self) -> None:
        self._fill(True)

    # ---------------------------------------------------------------- cleanup (mf470 Optimize)
    def cleanup_dialog(self) -> None:
        f = [("remove_small", "pd363", "bool", None), ("small_gate", "pd364", "slen", (0.01, 10)),
             ("remove_overlap", "pd365", "bool", None), ("overlap_gate", "pd366", "slen", (0.01, 0.5)),
             ("merge_type", "pd367", "enum", ["None", "Direction first", "Length first", "Distance first"]),
             ("connect_gate", "pd368", "slen", (0.01, 100)), ("smooth", "pd369", "bool", None),
             ("smooth_accuracy", "pd370", "slen", (0.01, 10)), ("close_gap", "Close gaps up to", "slen", (0, 100))]
        if self.ask("mf470", self.cleanup, f):
            res = self.edit(lambda doc: OC.cleanup(doc, self.cleanup), "optimize")
            self.log(f"mf470: {res['before']} -> {res['after']} contours")

    def dedup(self) -> None:
        self.log(f"Remove: {self.edit(lambda doc: OC.dedup(doc, self.cleanup.overlap_gate), 'remove duplicates')}")

    def join(self) -> None:
        n = self.edit(lambda doc: OC.join(doc, self.cleanup.connect_gate, self.cleanup.merge_type), "join")
        self.log(f"mf476: {n} contours")

    def smooth(self) -> None:
        n = self.edit(lambda doc: OC.smooth(doc, self.cleanup.smooth_accuracy), "smooth", parts=self._targets())
        self.log(f"mf474: {n} segments removed")

    # ---------------------------------------------------------------- corners / cool / common edge
    def fillet_auto(self) -> None:
        f = [("radius", "pd600", "len", (0.1, 10)), ("min_angle", "pd601", "float", (0, 180)),
             ("max_angle", "pd602", "float", (0, 180))]
        if self.ask("mf401", self.fillet_p, f):
            n = self.edit(lambda doc: OC.fillet(doc, self.fillet_p), "fillet", parts=self._targets())
            self.log(f"mf401: {n} contours")

    def unload(self) -> None:
        if self.ask("mf404", self.unload_p, [("radius", "pd603", "len", (0.1, 10))]):
            n = self.edit(lambda doc: OC.unload(doc, self.unload_p), "unload loop", parts=self._targets())
            self.log(f"mf404: {n} contours")

    def alpha(self) -> None:
        f = [("max_angle", "pd607", "float", (1, 170)), ("min_edge", "pd608", "len", (1, 50)),
             ("lead", "pd609", "len", (0.5, 10)), ("mode", "pd614", "enum", ["pd610", "pd611", "pd612", "pd613"])]
        if self.ask("mf485", self.alpha_p, f):
            self.log(f"mf485: {self.edit(lambda doc: OC.alpha(doc, self.alpha_p), 'alpha corner', parts=self._targets())} contours")

    def alpha_clear(self) -> None:
        self.log(f"mf403: {self.edit(OC.alpha_clear, 'clear alpha corner', parts=self._targets())} contours")

    def cool_auto(self) -> None:
        f = [("lead_point", "pd604", "bool", None), ("sharp", "pd605", "bool", None),
             ("max_angle", "pd606", "float", (0, 180))]
        if self.ask("mf407", self.cool_p, f):
            self.log(f"mf407: {self.edit(lambda doc: OC.cool_auto(doc, self.cool_p), 'cool points', parts=self._targets())} points")

    def cool_clear(self) -> None:
        self.edit(OC.cool_clear, "clear cool points", parts=self._targets())

    def _share(self, c_type: bool) -> None:
        r = self.edit(lambda doc: OC.share_edge(doc, c_type), "common edge")
        if r is not None:
            u = units.current()
            self.log(f"mf250: cut length {u.fmt(r[0], 'length', decimals=1)} -> {u.fmt(r[1], 'length', decimals=1)}")

    def share_edge(self) -> None:
        self._share(False)

    def share_edge_c(self) -> None:
        self._share(True)

    def rect_share(self) -> None:
        f = [("kind", "A250707_0", "enum", ["A250707_7", "A250707_8", "A250707_9"]),
             ("overcut_on", "A250707_1", "bool", None), ("overcut", "A250707_2", "len", (0, 100))]
        if self.ask("A250707_11", self.rect_p, f):
            n = self.edit(lambda doc: OC.rect_share(doc, self.rect_p), "rectangle common edge")
            if n is not None:
                self.log(f"A250707_11: {n} paths")

    # ---------------------------------------------------------------- fly cut (mf112)
    def _flycut(self, kind) -> None:
        from mlaser.cam import flycut as F
        prm = self.circle_fly_p if kind == F.SCAN_CIRCLE else self.line_fly_p
        part = self.edit(lambda doc: OC.flycut(doc, kind, prm), "fly cut")
        if part is None:
            self.log(self.w.tr_.tr("mf161"))
        else:
            self.log(f"mf112: {len(part.extra.get('scan_paths', []))} scan path(s)")

    def flycut_circle(self) -> None:
        from mlaser.cam import flycut as F
        f = [("sort_type", "pd707", "int", (0, 5)), ("max_fly_len", "pd708", "len", (0, 1e4)),
             ("first_single_contour", "pd851", "bool", None), ("velocity_ratio", "pd290", "float", (0.01, 10))]
        if self.ask("mf112_1", self.circle_fly_p, f):
            self._flycut(F.SCAN_CIRCLE)

    def flycut_line(self) -> None:
        from mlaser.cam import flycut as F
        f = [("start_corner", "pd715", "enum", ["pd710", "pd711", "pd712", "pd713"]),
             ("collinear_tol", "pd716", "slen", (0, 100)), ("max_link_len", "pd717", "len", (0, 1e4)),
             ("max_fly_len", "pd718", "len", (0, 1e4))]
        if self.ask("mf112_2", self.line_fly_p, f):
            self._flycut(F.SCAN_LINE)

    def flycut_auto(self) -> None:
        """mf112: circle fly cut if the selection has circles, else line fly cut."""
        (self.flycut_circle if OC.flycut_has_circles(self.ctx.document) else self.flycut_line)()


# --------------------------------------------------------------------------- wiring
REWIRE = {
    "tool.array": "array_dialog",
    "draw.linear_fill": "linear_fill",
    "tool.optimize": "cleanup_dialog",
    "opt.dedup": "dedup",
    "opt.join": "join",
    "opt.smooth": "smooth",
    "geom.smooth": "smooth",
}
FEATURE_REWIRE: dict[str, str] = {
    "craft.chamfer": "fillet_auto", "craft.chamfer_auto": "fillet_auto", "craft.chamfer_manual": "fillet_auto",
    "craft.unload": "unload", "craft.cool_auto": "cool_auto", "craft.cool_clear": "cool_clear",
    "tool.share_edge": "share_edge", "tool.share_edge_c": "share_edge_c",
    "tool.scan_cut": "flycut_auto", "tool.scan_circle": "flycut_circle", "tool.scan_line": "flycut_line",
}
EXTRA_BUTTONS = [
    ("camx.fill_circle", "mf100_1", "fill_circle"),
    ("camx.array_circular", "Circular array", "circular_array_dialog"),
    ("camx.array_sheet", "Fill sheet", "sheet_array_dialog"),
    ("camx.cleanup", "mf470", "cleanup_dialog"),
]
FEATURE_BUTTONS: list[tuple[str, str, str]] = [
    ("camx.alpha", "mf485", "alpha"), ("camx.alpha_clear", "mf403", "alpha_clear"),
    ("camx.rect_share", "A250707_11", "rect_share"),
]


def register(ctx, exec_dialogs: bool = True) -> CamExtension:
    """Bind the advanced-CAM commands (``ctx``: PluginContext)."""
    ext = CamExtension(ctx, exec_dialogs)
    for cmd, name in list(REWIRE.items()) + list(FEATURE_REWIRE.items()):
        ctx.register_command(cmd, getattr(ext, name))
    tab = []
    for cmd, key, name in EXTRA_BUTTONS + FEATURE_BUTTONS:
        ctx.register_command(cmd, getattr(ext, name), label=key)
        tab.append(cmd)
    ext.tab = ctx.add_ribbon_tab("CAM+", tab)
    return ext
