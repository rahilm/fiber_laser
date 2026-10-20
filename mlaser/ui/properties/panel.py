"""Properties panel: follows the canvas selection and edits it in place (ux.md P1-3, §4.1).

Sections: the selection summary, **Transform** (position and size of the selection's bounding
box, rotation), **Layer** (assignment, the layer's technology, the layer dialog), **Lead in /
Lead out** (with the live :class:`LeadDiagram`), **Kerf**, **Micro-joints**, **Start point**
and **Cooling points**.  Every field shows its unit.

While a value is being changed the canvas shows a live preview of the result (the real CAM
geometry of the selected parts, built with ``cam.toolpath.build_toolpath`` on a copy); the edit
itself is committed when the field is confirmed (Enter, focus out, a choice in a list or a
button) and always goes through ``PluginContext.edit``, so it is one undo step.  The modal
dialogs stay reachable from each section ("Dialog..." buttons run the ribbon commands).
"""

from __future__ import annotations

import copy
import math
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QLabel, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from mlaser import ops
from mlaser.model import Document, LeadParams, Part

from mlaser.ui.quantity import QuantitySpinBox, shrinkable
from mlaser.ui.techform import FlowLayout, ResponsiveForm, Section

from .lead_diagram import LeadDiagram

MAX_PREVIEW_CONTOURS = 400     # live CAM preview only for selections up to this size
LEAD_KINDS = ("props.lead.none", "props.lead.line", "props.lead.arc", "props.lead.line_arc")
KERF_KINDS = ((-1, "props.kerf.none"), (0, "props.kerf.inner"), (1, "props.kerf.outer"))


_KIND = {"mm": "length", "°": "angle"}


def _spin(lo: float, hi: float, dec: int, unit: str, step: float = 1.0) -> QDoubleSpinBox:
    """A unit-bearing field: lengths and angles are QuantitySpinBox (the Units track's widget:
    shown in the user's units, stored in mm), other units (%) a plain spin box."""
    kind = _KIND.get(unit)
    if kind is not None:
        s = QuantitySpinBox(kind, minimum=lo, maximum=hi, decimals=dec, step=step)
    else:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        s.setSingleStep(step)
        if unit:
            s.setSuffix(f" {unit}")
    s.setKeyboardTracking(False)
    s.setAccelerated(True)
    return shrinkable(s)


def _swatch(color: QColor) -> QIcon:
    pm = QPixmap(14, 14)
    pm.fill(color)
    return QIcon(pm)


class PropertiesPanel(QScrollArea):
    """See the module docstring.  ``ctx`` is the plugin's PluginContext, ``canvas`` the
    CanvasView (selection, live preview)."""

    def __init__(self, ctx: Any, canvas: Any, parent: Optional[QWidget] = None,
                 technology_name: Optional[Callable[[int], str]] = None, store: Any = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.store = store                 # technology store (inheritance: recipe values)
        self.inherit: dict[str, Any] = {}  # vid -> InheritedField
        self.canvas = canvas
        self.tr_ = ctx.tr
        self.technology_name = technology_name or (lambda layer: "")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)   # rows reflow instead
        self.setObjectName("properties_panel")
        body = QWidget()
        self.setWidget(body)
        self.lay = QVBoxLayout(body)
        self.lay.setContentsMargins(6, 6, 6, 6)
        self._updating = False
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(120)
        self._preview_timer.timeout.connect(self._show_preview)
        self._preview_fn: Optional[Callable[[Document], Any]] = None
        self.edits = 0                      # committed edits (tests, status)
        t = self._t
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("props_summary")
        self.lay.addWidget(self.summary)
        self.sections: list[Section] = []

        # ---- transform
        g, f = self._section("props.transform")
        self.x = _spin(-1e6, 1e6, 3, "mm")
        self.y = _spin(-1e6, 1e6, 3, "mm")
        self.w = _spin(0.001, 1e6, 3, "mm")
        self.h = _spin(0.001, 1e6, 3, "mm")
        self.rot = _spin(-360.0, 360.0, 2, "°", 15.0)
        for key, wdg in (("props.x", self.x), ("props.y", self.y), ("props.width", self.w),
                         ("props.height", self.h), ("props.rotate_by", self.rot)):
            self._row(f, key, wdg)
        self.size_note = QLabel(t("props.size_uniform"))
        self.size_note.setWordWrap(True)
        self.size_note.setEnabled(False)
        f.addRow(self.size_note)
        for wdg in (self.x, self.y, self.w, self.h, self.rot):
            wdg.valueChanged.connect(self._transform_changed)
            wdg.editingFinished.connect(self._commit_transform)

        # ---- layer
        g, f = self._section("props.layer")
        self.layer = QComboBox()
        self.layer.activated.connect(self._commit_layer)
        self._row(f, "props.layer", self.layer)
        self.tech_label = QLabel()
        self.tech_label.setWordWrap(True)
        self._row(f, "props.technology", self.tech_label)
        b = QPushButton(t("props.layer_dialog"))
        b.clicked.connect(self._layer_dialog)
        f.addRow(b)

        # ---- leads
        g, f = self._section("props.leads")
        self.lead_w: dict[str, tuple[QComboBox, QDoubleSpinBox, QDoubleSpinBox, QDoubleSpinBox]] = {}
        for name, title in (("in", "props.lead_in"), ("out", "props.lead_out")):
            kind = QComboBox()
            kind.addItems([t(k) for k in LEAD_KINDS])
            ang = _spin(0.0, 180.0, 1, "°", 5.0)
            ln = _spin(0.0, 1000.0, 2, "mm", 0.5)
            rad = _spin(0.0, 1000.0, 2, "mm", 0.5)
            hdr = QLabel(f"<b>{t(title)}</b>")
            f.addRow(hdr)
            for key, field, wdg in (("props.lead_kind", "kind", kind), ("props.lead_angle", "angle_deg", ang),
                                    ("props.lead_length", "length", ln), ("props.lead_radius", "radius", rad)):
                self._inherited_row(f, key, wdg, f"craft.lead_{name}.{field}")
            kind.activated.connect(self._commit_leads)
            for wdg in (ang, ln, rad):
                wdg.valueChanged.connect(self._leads_changed)
                wdg.editingFinished.connect(self._commit_leads)
            kind.currentIndexChanged.connect(self._leads_changed)
            self.lead_w[name] = (kind, ang, ln, rad)
        self.diagram = LeadDiagram()
        f.addRow(self.diagram)

        # ---- kerf
        g, f = self._section("props.kerf")
        self.kerf_kind = QComboBox()
        for v, k in KERF_KINDS:
            self.kerf_kind.addItem(t(k), v)
        self.kerf_w = _spin(0.0, 10.0, 3, "mm", 0.01)
        self._inherited_row(f, "props.kerf_side", self.kerf_kind, "craft.kerf_comp")
        self._inherited_row(f, "props.kerf_width", self.kerf_w, "craft.kerf_width")
        self.kerf_kind.activated.connect(self._commit_kerf)
        self.kerf_kind.currentIndexChanged.connect(self._kerf_changed)
        self.kerf_w.valueChanged.connect(self._kerf_changed)
        self.kerf_w.editingFinished.connect(self._commit_kerf)

        # ---- micro-joints
        g, f = self._section("props.joints", expanded=False)
        self.mj_info = QLabel()
        f.addRow(self.mj_info)
        self.mj_count = QSpinBox()
        self.mj_count.setRange(1, 100)
        self.mj_count.setValue(4)
        self.mj_len = _spin(0.01, 50.0, 2, "mm", 0.1)
        self.mj_len.setValue(0.5)
        self._row(f, "props.joint_count", self.mj_count)
        self._row(f, "props.joint_length", self.mj_len)
        roww = QWidget()
        row = FlowLayout(roww, 6)
        b1 = QPushButton(t("props.apply"))
        b1.clicked.connect(self._commit_joints)
        b2 = QPushButton(t("props.clear"))
        b2.clicked.connect(lambda: self._edit(ops.clear_joints, "props.edit.joints_clear"))
        for x in (b1, b2):
            row.addWidget(x)
        f.addRow(roww)
        # the micro-joint spec of the window (``craft.mj_auto``, OneKey) is edited here only
        mp = getattr(ctx.window, "mj_params", None)
        if mp is not None:
            self.mj_count.setValue(max(1, int(mp.count)))
            self.mj_len.setValue(float(mp.length))
        self.mj_count.valueChanged.connect(lambda v: self._set_window_param("mj_params", "count", int(v)))
        self.mj_len.valueChanged.connect(lambda v: self._set_window_param("mj_params", "length", float(v)))

        # ---- overcut / gap
        g, f = self._section("props.overcut", expanded=False)
        self.overcut = _spin(0.0, 100.0, 3, "mm", 0.1)
        self.overcut.setToolTip(t("props.overcut_tip"))
        self._row(f, "props.overcut_len", self.overcut)
        craft_ext = ctx.window.extension("ext_craft") if hasattr(ctx.window, "extension") else None
        gp = getattr(craft_ext, "gap_p", None)
        if gp is not None:
            self.overcut.setValue(abs(float(gp.value)))
        self.overcut.valueChanged.connect(self._overcut_value)
        roww = QWidget()
        row = FlowLayout(roww, 6)
        for key, cmd in (("props.gap", "craft.gap"), ("props.do_overcut", "craft.overcut"), ("props.seal", "craft.seal")):
            b = QPushButton(t(key))
            b.clicked.connect(lambda _c=False, c=cmd: self._run(c))
            row.addWidget(b)
        f.addRow(roww)

        # ---- start point
        g, f = self._section("props.start", expanded=False)
        self.start = _spin(0.0, 100.0, 1, "%", 5.0)
        self._row(f, "props.start_pos", self.start)
        self.start.editingFinished.connect(self._commit_start)
        b = QPushButton(t("props.start_auto"))
        b.clicked.connect(self._start_auto)
        f.addRow(b)

        # ---- cooling points
        g, f = self._section("props.cool", expanded=False)
        self.cool_info = QLabel()
        f.addRow(self.cool_info)
        self.cool_angle = _spin(1.0, 179.0, 0, "°", 5.0)
        self.cool_angle.setValue(90.0)
        cam = ctx.window.extension("ext_cam") if hasattr(ctx.window, "extension") else None
        cp = getattr(cam, "cool_p", None)
        if cp is not None:
            self.cool_angle.setValue(float(cp.max_angle))
        self.cool_angle.valueChanged.connect(lambda v: self._set_ext_param("ext_cam", "cool_p", "max_angle", float(v)))
        self._row(f, "props.cool_angle", self.cool_angle)
        roww = QWidget()
        row = FlowLayout(roww, 6)
        b1 = QPushButton(t("props.cool_auto"))
        b1.clicked.connect(self._cool_auto)
        b2 = QPushButton(t("props.clear"))
        b2.clicked.connect(self._cool_clear)
        row.addWidget(b1)
        row.addWidget(b2)
        f.addRow(roww)
        from mlaser.ui.homes import bind_editor

        for vid, w in (("part.x", self.x), ("part.y", self.y), ("part.width", self.w), ("part.height", self.h),
                       ("part.rotate_by", self.rot), ("part.layer", self.layer),
                       ("craft.kerf_comp", self.kerf_kind), ("craft.kerf_width", self.kerf_w),
                       ("craft.micro_joint.count", self.mj_count), ("craft.micro_joint.length", self.mj_len),
                       ("craft.start_param", self.start), ("craft.cool.max_angle", self.cool_angle),
                       ("craft.overcut", self.overcut)):
            bind_editor(w, vid)
        for name in ("in", "out"):
            for field, w in zip(("kind", "angle_deg", "length", "radius"), self.lead_w[name]):
                bind_editor(w, f"craft.lead_{name}.{field}")
        self.lay.addStretch(1)
        for cb in body.findChildren(QComboBox):          # no combo forces the dock wider
            cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(6)
        self.refresh()

    # ---------------------------------------------------------------- helpers
    def _t(self, key: str) -> str:
        return self.tr_.tr(key)

    def _section(self, key: str, expanded: bool = True) -> tuple[Section, QFormLayout]:
        """A collapsible section whose rows put the label above the field when the dock is
        too narrow for both side by side (ResponsiveForm)."""
        form = ResponsiveForm()
        g = Section(key.replace(".", "_"), self._t(key), form, expanded)
        self.lay.addWidget(g)
        self.sections.append(g)
        return g, form.form

    def _inherited_row(self, form: QFormLayout, key: str, w: QWidget, vid: str) -> None:
        """A row of the inheritance chain: the editor with its source chip and restore button."""
        from mlaser.ui.inherited_field import InheritedField

        w.setAccessibleName(self._t(key))
        fld = InheritedField(w, vid, self.tr_, self.ctx.window)
        fld.reset.connect(lambda v=vid: self._reset(v))
        lab = QLabel(self._t(key))
        lab.setBuddy(w)
        form.addRow(lab, fld)
        self.inherit[vid] = fld

    def _set_window_param(self, attr: str, field: str, value: Any) -> None:
        obj = getattr(self.ctx.window, attr, None)
        if obj is not None and not self._updating:
            setattr(obj, field, value)

    def _set_ext_param(self, ext: str, attr: str, field: str, value: Any) -> None:
        e = self.ctx.window.extension(ext) if hasattr(self.ctx.window, "extension") else None
        obj = getattr(e, attr, None)
        if obj is not None and not self._updating:
            setattr(obj, field, value)

    def _overcut_value(self, v: float) -> None:
        e = self.ctx.window.extension("ext_craft") if hasattr(self.ctx.window, "extension") else None
        gp = getattr(e, "gap_p", None)
        if gp is not None:
            gp.value = float(v)

    def _reset(self, vid: str) -> None:
        """Restore: the selected parts take the inherited value of ``vid`` again."""
        from mlaser.services import inheritance as I

        def fn(d: Document) -> None:
            for p in [q for q in d.parts if q.selected]:
                I.clear_override(d, self.store, "part", vid, part=p)
        self._edit(fn, "props.edit.reset")

    def _show_inherited(self, sel: list) -> None:
        """Source chip of every chain value for the selection."""
        from mlaser.services import inheritance as I

        doc = self.document
        t = self.tr_
        for vid, fld in self.inherit.items():
            vals = [I.effective(doc, self.store, vid, part=p) for p in sel]
            parents = [I.effective(doc, self.store, vid, part=p, upto=2) for p in sel]
            own = [src == "part" for _v, src in vals]
            li = sel[0].craft.layer
            psrc = parents[0][1]
            source = I.source_label(doc, self.store, psrc, li, t)
            mixed = ""
            if len(sel) > 1 and (len({repr(v) for v, _s in vals}) > 1 or len(set(own)) > 1):
                mixed = t.format("inherit.mixed", n=sum(own), total=len(sel))
            here = t.tr("inherit.src.part") if len(sel) == 1 else t.format("inherit.src.parts", n=len(sel))
            parent_vid = ("layer." + vid) if psrc == "layer" else ("recipe." + vid) if psrc == "recipe" else None
            fld.set_state(vals[0][0], all(own), here, source, parent_vid=parent_vid,
                          parent_target=li if psrc == "layer" else I.recipe_of(doc, li, self.store), mixed=mixed)

    def _row(self, form: QFormLayout, key: str, w: QWidget) -> None:
        lab = QLabel(self._t(key))
        lab.setBuddy(w)
        w.setAccessibleName(self._t(key))
        form.addRow(lab, w)

    def _run(self, cmd: str) -> None:
        try:
            self.ctx.commands.run(cmd)
        finally:
            self.refresh()

    @property
    def document(self) -> Document:
        return self.ctx.document

    def selected(self) -> list[Part]:
        return [p for p in self.document.parts if p.selected]

    def _locked(self, what: str) -> bool:
        from mlaser.ui import safety_ui

        win = self.ctx.window
        return bool(getattr(win, "safety_ui", None)) and safety_ui.job_locked(win, what)

    def _edit(self, fn: Callable[[Document], Any], label_key: str) -> Any:
        """One undoable edit of the selected parts (``PluginContext.edit``)."""
        self.canvas.clear_preview()
        sel = self.selected()
        if not sel or self._updating:
            return None
        if self._locked(self._t(label_key)):
            self.refresh()
            return None
        out = self.ctx.edit(fn, self._t(label_key), parts=sel)
        self.edits += 1
        self.refresh()
        return out

    # ---------------------------------------------------------------- refresh
    def refresh(self) -> None:
        """Read the selection into the fields (no edit)."""
        self._updating = True
        try:
            self._refresh()
        finally:
            self._updating = False

    def _refresh(self) -> None:
        doc = self.document
        sel = self.selected()
        t = self._t
        for g in self.sections:
            g.setEnabled(bool(sel))
        if not sel:
            self.summary.setText(t("props.none"))
            self.diagram.set_leads(LeadParams(kind=0), LeadParams(kind=0))
            return
        n_c = sum(len(p.contours) for p in sel)
        open_c = sum(1 for p in sel for c in p.contours if not c.closed)
        if len(sel) == 1:
            p = sel[0]
            name = p.name or f"#{doc.parts.index(p) + 1}"
            txt = self.tr_.format("props.summary_one", name=name, contours=n_c)
        else:
            txt = self.tr_.format("props.summary_many", parts=len(sel), contours=n_c)
        if open_c:
            txt += " · " + self.tr_.format("props.open_contours", n=open_c)
        self.summary.setText(txt)
        x0, y0, x1, y1 = ops.selection_bbox(doc)
        for wdg, v in ((self.x, x0), (self.y, y0), (self.w, max(x1 - x0, 0.001)), (self.h, max(y1 - y0, 0.001))):
            wdg.setValue(v)
        self.rot.setValue(0.0)
        # layer
        from mlaser.ui.canvas import layer_color

        layers = sorted({p.craft.layer for p in doc.parts} | set(doc.layers) | set(range(1, 17)))
        self.layer.clear()
        for li in layers:
            self.layer.addItem(_swatch(layer_color(li)), f"{t('props.layer')} {li}", li)
        cur = {p.craft.layer for p in sel}
        if len(cur) == 1:
            li = next(iter(cur))
            self.layer.setCurrentIndex(self.layer.findData(li))
            self.tech_label.setText(self.technology_name(li) or t("props.no_technology"))
        else:
            self.layer.setCurrentIndex(-1)
            self.tech_label.setText(t("props.mixed"))
        # leads (of the first selected part's craft)
        c0 = sel[0].craft
        for name, lead in (("in", c0.lead_in), ("out", c0.lead_out)):
            kind, ang, ln, rad = self.lead_w[name]
            kind.setCurrentIndex(max(0, min(3, int(lead.kind))))
            ang.setValue(lead.angle_deg)
            ln.setValue(lead.length)
            rad.setValue(lead.radius)
        self.diagram.set_leads(copy.deepcopy(c0.lead_in), copy.deepcopy(c0.lead_out))
        self.kerf_kind.setCurrentIndex(max(0, self.kerf_kind.findData(int(c0.kerf_comp))))
        self.kerf_w.setValue(c0.kerf_width)
        self.diagram.set_kerf(int(c0.kerf_comp), c0.kerf_width)
        self._show_inherited(sel)
        nj = sum(len(p.craft.micro_joints) for p in sel)
        self.mj_info.setText(self.tr_.format("props.joint_info", n=nj))
        from mlaser.cam.manual import contour_craft, start_distance

        p = sel[0]
        if p.contours:
            ci = self._outer(p)
            c = p.contours[ci]
            L = c.length()
            self.start.setValue(100.0 * start_distance(c, contour_craft(p, ci)) / L if L > 0 else 0.0)
        ncool = sum(len(p.craft.extra.get("cool_points", []) or []) for p in sel)
        self.cool_info.setText(self.tr_.format("props.cool_info", n=ncool))

    @staticmethod
    def _outer(p: Part) -> int:
        return max(range(len(p.contours)), key=lambda i: p.contours[i].length()) if p.contours else 0

    # ---------------------------------------------------------------- live preview
    def _schedule_preview(self, fn: Callable[[Document], Any]) -> None:
        if self._updating:
            return
        self._preview_fn = fn
        self._preview_timer.start()

    def preview_polylines(self, fn: Callable[[Document], Any]) -> list[list[tuple[float, float]]]:
        """Cut moves (leads and kerf included) of the selected parts after ``fn``, built by the
        CAM on a copy: what the toolpath will be once the edit is committed."""
        from mlaser.cam.toolpath import CamParams, build_toolpath
        from mlaser.model import Arc

        sel = self.selected()
        if not sel or sum(len(p.contours) for p in sel) > MAX_PREVIEW_CONTOURS:
            return []
        tmp = Document(parts=[copy.deepcopy(p) for p in sel], layers=self.document.layers)
        for p in tmp.parts:
            p.selected = True
        fn(tmp)
        out: list[list[tuple[float, float]]] = []
        cur: list[tuple[float, float]] = []
        for m in build_toolpath(tmp, CamParams(sort_mode=0)):
            name = type(m).__name__
            if name in ("CutLine", "CutArc"):
                if not cur or math.dist(cur[-1], m.start) > 1e-6:
                    if len(cur) > 1:
                        out.append(cur)
                    cur = [tuple(m.start)]
                if name == "CutArc":
                    a = Arc(tuple(m.start), tuple(m.end), tuple(m.center), bool(m.ccw))
                    n = max(4, min(180, int(a.length() / 0.5)))
                    cur.extend(a.point_at(k / n) for k in range(1, n + 1))
                else:
                    cur.append(tuple(m.end))
        if len(cur) > 1:
            out.append(cur)
        return out

    def _show_preview(self) -> None:
        fn = self._preview_fn
        if fn is None:
            return
        try:
            polys = self.preview_polylines(fn)
        except Exception:  # noqa: BLE001 - a preview must never break editing
            polys = []
        hi = self.palette().highlight().color().name()
        self.canvas.set_preview(polys, color=hi)

    # ---------------------------------------------------------------- transform
    def _transform_values(self) -> tuple[float, float, Optional[float], Optional[float]]:
        x0, y0, x1, y1 = ops.selection_bbox(self.document)
        w = self.w.value() if abs(self.w.value() - (x1 - x0)) > 5e-4 else None
        h = self.h.value() if w is None and abs(self.h.value() - (y1 - y0)) > 5e-4 else None
        return self.x.value(), self.y.value(), w, h

    def _transform_changed(self) -> None:
        if self._updating:
            return
        x, y, w, h = self._transform_values()
        ang = self.rot.value()

        def fn(d: Document) -> None:
            ops.set_bbox(d, x, y, w, h)
            if ang:
                ops.rotate(d, ang)
        self._schedule_preview(fn)

    def _commit_transform(self) -> None:
        if self._updating or not self.selected():
            return
        x, y, w, h = self._transform_values()
        ang = self.rot.value()
        x0, y0, _, _ = ops.selection_bbox(self.document)
        if w is None and h is None and not ang and abs(x - x0) < 5e-4 and abs(y - y0) < 5e-4:
            return

        def fn(d: Document) -> None:
            ops.set_bbox(d, x, y, w, h)
            if ang:
                ops.rotate(d, ang)
        self._edit(fn, "props.edit.transform")

    # ---------------------------------------------------------------- layer
    def _commit_layer(self) -> None:
        li = self.layer.currentData()
        if li is None or self._updating:
            return
        if all(p.craft.layer == li for p in self.selected()):
            return
        self._edit(lambda d: ops.set_layer(d, int(li)), "props.edit.layer")

    def _layer_dialog(self) -> None:
        li = self.layer.currentData()
        win = self.ctx.window
        if li is not None and hasattr(win, "edit_layer"):
            win.edit_layer(int(li))
        self.refresh()

    # ---------------------------------------------------------------- leads
    def leads(self) -> tuple[LeadParams, LeadParams]:
        out = []
        for name in ("in", "out"):
            kind, ang, ln, rad = self.lead_w[name]
            out.append(LeadParams(kind=kind.currentIndex(), angle_deg=ang.value(), length=ln.value(),
                                  radius=rad.value()))
        return out[0], out[1]

    def _leads_changed(self) -> None:
        if self._updating:
            return
        lin, lout = self.leads()
        self.diagram.set_leads(lin, lout)
        self._schedule_preview(lambda d: ops.apply_lead(d, lin, lout))

    def _commit_leads(self) -> None:
        if self._updating or not self.selected():
            return
        self._lead_defaults()
        lin, lout = self.leads()
        if all(_same_lead(p.craft.lead_in, lin) and _same_lead(p.craft.lead_out, lout) for p in self.selected()):
            self.canvas.clear_preview()
            return
        from mlaser.services import inheritance as I

        store = self.store

        def fn(d: Document) -> None:
            ops.apply_lead(d, lin, lout, keep_start=I.start_is_set)   # stores the start as before
            for p in [q for q in d.parts if q.selected]:
                for name, lp in (("in", lin), ("out", lout)):
                    for field in I.LEAD_FIELDS[:4]:
                        vid = f"craft.lead_{name}.{field}"
                        I.set_override(d, store, "part", vid, getattr(lp, field), part=p)
        self._edit(fn, "props.edit.leads")

    def _lead_defaults(self) -> None:
        """A lead type chosen with no size yet gets the original's dialog defaults (line 3 mm,
        90°, radius 2 mm: ops.craft.default_lead_params)."""
        from mlaser.ops.craft import default_lead_params

        d = default_lead_params()[0]
        for name in ("in", "out"):
            kind, ang, ln, rad = self.lead_w[name]
            if kind.currentIndex() and ln.value() <= 0 and rad.value() <= 0:
                self._updating = True
                try:
                    ang.setValue(d.angle_deg)
                    ln.setValue(d.length)
                    rad.setValue(d.radius)
                finally:
                    self._updating = False

    # ---------------------------------------------------------------- kerf
    def _kerf(self) -> tuple[int, float]:
        return int(self.kerf_kind.currentData()), float(self.kerf_w.value())

    def _kerf_changed(self) -> None:
        if self._updating:
            return
        k, w = self._kerf()
        self.diagram.set_kerf(k, w)
        self._schedule_preview(lambda d: ops.apply_kerf(d, k, w))

    def _commit_kerf(self) -> None:
        if self._updating or not self.selected():
            return
        k, w = self._kerf()
        if all(p.craft.kerf_comp == k and p.craft.kerf_width == w for p in self.selected()):
            self.canvas.clear_preview()
            return
        from mlaser.services import inheritance as I

        store = self.store

        def fn(d: Document) -> None:
            for p in [q for q in d.parts if q.selected]:
                I.set_override(d, store, "part", "craft.kerf_comp", k, part=p)
                I.set_override(d, store, "part", "craft.kerf_width", w, part=p)
        self._edit(fn, "props.edit.kerf")

    # ---------------------------------------------------------------- joints, start, cooling
    def _commit_joints(self) -> None:
        prm = ops.MicroJointParams(kind=0, count=self.mj_count.value(), length=self.mj_len.value())
        self._edit(lambda d: ops.auto_microjoints(d, prm), "props.edit.joints")

    def _commit_start(self) -> None:
        if self._updating or not self.selected():
            return
        frac = self.start.value() / 100.0
        from mlaser.cam.manual import set_start_at
        from mlaser.services.inheritance import mark_start

        def fn(d: Document) -> None:
            for p in ops.contour_targets(d):
                if p.contours:
                    ci = self._outer(p)
                    set_start_at(p, ci, frac * p.contours[ci].length())
                    mark_start(p, ci)        # set by hand: lead edits keep it
        self._edit(fn, "props.edit.start")

    def _start_auto(self) -> None:
        from mlaser.cam.manual import set_start_at
        from mlaser.cam.ordering import choose_start

        def fn(d: Document) -> None:
            for p in ops.contour_targets(d):
                if p.contours:
                    ci = self._outer(p)
                    c = p.contours[ci]
                    if c.closed:
                        set_start_at(p, ci, choose_start(c, 0, 0.0))
        self._edit(fn, "props.edit.start")

    def _cool_auto(self) -> None:
        from mlaser.ops.cam import CoolParams, cool_auto

        q = CoolParams(sharp=True, max_angle=self.cool_angle.value())
        self._edit(lambda d: cool_auto(d, q), "props.edit.cool")

    def _cool_clear(self) -> None:
        from mlaser.ops.cam import cool_clear

        self._edit(cool_clear, "props.edit.cool_clear")


def _same_lead(a: LeadParams, b: LeadParams) -> bool:
    return (int(a.kind), round(a.angle_deg, 6), round(a.length, 6), round(a.radius, 6)) == \
        (int(b.kind), round(b.angle_deg, 6), round(b.length, 6), round(b.radius, 6))
