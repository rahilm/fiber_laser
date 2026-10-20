"""Grouped, searchable editor of a layer's process parameters (the GP.* layer parameters of the
registry), shared by the technology editor (Process workspace) and the Layer parameters dialog.

The 164 layer parameters used to be one tree ("a wall of settings").  Here they are sorted by
what a person setting up a cut thinks about, the important few first:

* **Cutting**: speed, laser power, duty cycle, frequency (slow start, dynamic power, delays as
  secondary fields);
* **Piercing**: the pierce mode and one block per stage, shown only for the stages the mode
  uses (smooth pierce, pre-pierce, gradual focus as secondary fields, each shown only when it is
  switched on);
* **Gas & nozzle**: gas and pressure;
* **Height follow**: cutting height, focus, lift height;
* sections the caller adds (the technology editor: *Leads & kerf*, *Corners & small
  features*, *About this technology*);
* **Advanced**: every other parameter, by its registry group, collapsed.

Every field has a plain-language label with its unit, a help tooltip (with the registry key
and range), and is hidden while it does not apply (a stage the pierce mode does not use, the
slow-start length while slow start is off).  *Show all parameters* adds the secondary fields
and the Advanced section; the search box finds any parameter by label, key or help text.

**Base values**: the form compares every field with a base (the technology a layer was set
from, or the factory version of a technology): a changed field shows a dot before its label
and a reset button, and the header counts them ("3 overridden on this layer").

**Responsive**: each section's form puts the label beside the field when the widest label and
field fit the width the panel actually has, and above the field when they do not; no widths
are fixed, the form never scrolls horizontally.  Unit-bearing fields use
:class:`mlaser.ui.quantity.QuantitySpinBox` (the Units track's widget).
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLayout, QLineEdit, QScrollArea, QSizePolicy, QSpinBox, QToolButton,
                               QVBoxLayout, QWidget)

from mlaser.services import technology as T

from .quantity import QuantitySpinBox, kind_for_unit, shrinkable

# --------------------------------------------------------------------------- layout helpers


class FlowLayout(QLayout):
    """Items left to right, wrapping to the next line when the width runs out (chips, check
    boxes).  Height for width, so the parent grows by lines instead of scrolling sideways."""

    def __init__(self, parent: Optional[QWidget] = None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list = []
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:   # noqa: N802 - Qt
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):   # noqa: N802 - Qt
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):   # noqa: N802 - Qt
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):   # noqa: N802 - Qt
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:   # noqa: N802 - Qt
        return True

    def heightForWidth(self, w: int) -> int:   # noqa: N802 - Qt
        return self._do(QRect(0, 0, w, 0), True)

    def setGeometry(self, r) -> None:   # noqa: N802 - Qt
        super().setGeometry(r)
        self._do(r, False)

    def sizeHint(self) -> QSize:   # noqa: N802 - Qt
        return self.minimumSize()

    def minimumSize(self) -> QSize:   # noqa: N802 - Qt
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do(self, r, test: bool) -> int:
        m = self.contentsMargins()
        x, y = r.x() + m.left(), r.y() + m.top()
        right = r.right() - m.right()
        line_h = 0
        for it in self._items:
            w = it.widget()
            if w is not None and w.isHidden():
                continue
            sh = it.sizeHint()
            if x + sh.width() > right + 1 and line_h > 0:
                x = r.x() + m.left()
                y += line_h + self._sp
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), sh))
            x += sh.width() + self._sp
            line_h = max(line_h, sh.height())
        return y + line_h - r.y() + m.bottom()


class ResponsiveForm(QWidget):
    """A QFormLayout whose rows put the label beside the field while the widest label + field
    fit the form's current width, and above it when they do not (content-driven, re-evaluated
    on every resize; no breakpoints)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.form = QFormLayout(self)
        self.form.setContentsMargins(4, 2, 4, 2)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.form.setRowWrapPolicy(QFormLayout.DontWrapRows)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def needed_width(self) -> int:
        """Width the side-by-side layout needs: widest visible label + widest field minimum."""
        lw = fw = 0
        f = self.form
        for r in range(f.rowCount()):
            li = f.itemAt(r, QFormLayout.LabelRole)
            fi = f.itemAt(r, QFormLayout.FieldRole)
            if li is not None and li.widget() is not None and not li.widget().isHidden():
                lab = li.widget()
                lw = max(lw, lab.fontMetrics().horizontalAdvance(lab.text()) + 4)
            if fi is not None:
                w = fi.widget()
                if w is not None and w.isHidden():
                    continue
                fw = max(fw, fi.minimumSize().width())
        m = f.contentsMargins()
        return lw + fw + max(f.horizontalSpacing(), 6) + m.left() + m.right()

    def update_wrap(self) -> None:
        wrap = self.width() < self.needed_width()
        want = QFormLayout.WrapAllRows if wrap else QFormLayout.DontWrapRows
        if self.form.rowWrapPolicy() != want:
            self.form.setRowWrapPolicy(want)

    def resizeEvent(self, ev) -> None:   # noqa: N802 - Qt
        super().resizeEvent(ev)
        self.update_wrap()

    def minimumSizeHint(self) -> QSize:   # noqa: N802 - Qt
        # wrapped rows need only the widest field: the form never forces a horizontal scroll
        s = super().minimumSizeHint()
        f = self.form
        fw = 0
        for r in range(f.rowCount()):
            fi = f.itemAt(r, QFormLayout.FieldRole)
            if fi is not None and (fi.widget() is None or not fi.widget().isHidden()):
                fw = max(fw, fi.minimumSize().width())
        return QSize(min(s.width(), fw + 12), s.height())


class Section(QWidget):
    """A collapsible section: a header button (title, open/closed arrow) and a body."""

    toggled = Signal(bool)

    def __init__(self, sid: str, title: str, body: QWidget, expanded: bool = True,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.sid = sid
        self.setObjectName(f"techform_section_{sid}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(0)
        self.header = QToolButton()
        self.header.setObjectName("techform_section_header")
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setAutoRaise(True)
        # a long title never widens the panel: it is clipped (full text in the tooltip)
        self.header.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.header.setToolTip(title)
        f = self.header.font()
        f.setBold(True)
        self.header.setFont(f)
        self.header.toggled.connect(self.set_expanded)
        lay.addWidget(self.header)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        lay.addWidget(line)
        self.body = body
        lay.addWidget(body)
        self.set_expanded(expanded)

    def set_expanded(self, on: bool) -> None:
        on = bool(on)
        if self.header.isChecked() != on:
            self.header.setChecked(on)
            return
        self.header.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.body.setVisible(on)
        self.toggled.emit(on)

    def expanded(self) -> bool:
        return self.header.isChecked()


# --------------------------------------------------------------------------- the parameters

#: section id -> (title key, primary keys, secondary keys)
STAGE_PRIMARY = ("DrillHeight", "DrillFocusPos", "DrillPeakCurrent", "DrillPower", "DrillFreq",
                 "DrillGasType", "DrillGasPressure", "DrillDelay")
STAGE_SECONDARY = ("EnableFocusGradual", "FocusGradualEndPos", "FocusGradualTime", "EnableGradualDrill",
                   "GradualTime", "BeforeLaserOffDelay", "AfterLaserOffDelay")
MAX_STAGES = 5

SECTIONS: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = [
    ("cutting", "techform.sec.cutting",
     ("GP.CutSpeed", "GP.CutPeakCurrent", "GP.CutPower", "GP.CutFreq"),
     ("GP.SlowStart", "GP.SlowStartLength", "GP.SlowStartSpeed", "GP.PowerAdjustWithSpeed",
      "GP.FreqAdjustWithSpeed", "GP.LaserOnDelay", "GP.LaserOffBeforeDelay", "GP.LaserOffAfterDelay")),
    ("piercing", "techform.sec.piercing", ("GP.ManuType",),
     ("GP.PreDrill", "GP.EnableSmoothPierce", "GP.SmoothPierceDrillHeight", "GP.SmoothPierceDrillTime_ms",
      "GP.SmoothPierceDrillFocusPos", "GP.SmoothPierceDrillPower", "GP.SmoothPierceDrillFreq",
      "GP.SmoothPierceDrillPeakCurrent")),
    ("gas", "techform.sec.gas", ("GP.CutGasType", "GP.CutAirPressure"),
     ("GP.ShortDistGasKeepOn", "GP.NoCloseGasInManu")),
    ("height", "techform.sec.height", ("GP.CutHeight", "GP.CutFocusPos", "GP.UpHeight"),
     ("GP.NoFollow", "GP.ShortDistNoUp", "GP.AdvFixHeightCutPos")),
]

#: parameters never edited here (file names, curves edited elsewhere)
SKIP = ("GP.RecentFile", "GP.LayerFileName", "GP.PWMCurveNodes", "GP.FreqCurveNodes")

#: field -> the switch it depends on (shown only while the switch is on)
DEPENDS: dict[str, str] = {
    "GP.SlowStartLength": "GP.SlowStart", "GP.SlowStartSpeed": "GP.SlowStart",
    "GP.ContourShiftXDist": "GP.EnableContourShift", "GP.ContourShiftYDist": "GP.EnableContourShift",
    **{f"GP.SmoothPierce{k}": "GP.EnableSmoothPierce"
       for k in ("DrillHeight", "DrillTime_ms", "DrillFocusPos", "DrillPower", "DrillFreq", "DrillPeakCurrent")},
    **{f"GP.CleanResidue_{k}": "GP.CleanResidue_Enable"
       for k in ("WorkH", "WorkV", "WorkFocus", "GasType", "GasP", "PeakCurrent", "Power", "Freq", "WorkR",
                 "SpiralTimes")},
    **{f"GP.UD_Up{k}": "GP.UD_UpEnable" for k in ("Len", "Speed", "AdvEnable", "Duty", "Freq")},
    **{f"GP.UD_Down{k}": "GP.UD_DownEnable" for k in ("Len", "Speed", "AdvEnable", "Duty", "Freq")},
    **{f"GP.FocusGradualEndPos{s}": f"GP.EnableFocusGradual{s}" for s in range(MAX_STAGES)},
    **{f"GP.FocusGradualTime{s}": f"GP.EnableFocusGradual{s}" for s in range(MAX_STAGES)},
    **{f"GP.GradualTime{s}": f"GP.EnableGradualDrill{s}" for s in range(MAX_STAGES)},
    **{f"GP.BoltDrill_{k}_{n}": f"GP.BoltDrill_Enable_{n}" for k in ("Power", "Freq") for n in range(1, 6)},
}

#: plain-language label / help keys of the primary fields (techform.f.<name>, techform.h.<name>)
PLAIN = {
    "GP.CutSpeed": "cut_speed", "GP.CutPeakCurrent": "cut_power", "GP.CutPower": "cut_duty",
    "GP.CutFreq": "cut_freq", "GP.ManuType": "pierce_mode", "GP.CutGasType": "gas",
    "GP.CutAirPressure": "gas_pressure", "GP.CutHeight": "cut_height", "GP.CutFocusPos": "focus",
    "GP.UpHeight": "lift_height", "GP.SlowStart": "slow_start", "GP.SlowStartLength": "slow_start_length",
    "GP.SlowStartSpeed": "slow_start_speed", "GP.NoFollow": "no_follow", "GP.ShortDistNoUp": "short_no_lift",
    "GP.PreDrill": "pre_pierce", "GP.EnableSmoothPierce": "smooth_pierce",
    **{f"GP.{k}{s}": "stage_" + n for k, n in zip(STAGE_PRIMARY, (
        "height", "focus", "power", "duty", "freq", "gas", "gas_pressure", "time")) for s in range(MAX_STAGES)},
}


def stage_of(key: str) -> Optional[int]:
    """Pierce stage (0-based) of a per-stage key (``GP.DrillHeight1`` -> 1), else None."""
    for k in STAGE_PRIMARY + STAGE_SECONDARY:
        p = f"GP.{k}"
        if key.startswith(p) and key[len(p):].isdigit():
            return int(key[len(p):])
    if key.startswith("GP.BoltDrill_") and key[-1].isdigit():
        return int(key[-1]) - 1
    return None


class TechField:
    """One parameter: its editor widget (registry type, unit, range), label, help, the
    "overridden" marker and the reset button."""

    def __init__(self, key: str, tr: Any, on_change: Callable[[], None], pressure_unit: int = 0) -> None:
        self.key = key
        self.d = d = T.layer_registry()[key]
        self.tr_ = tr
        unit = T.display_unit(key, pressure_unit)
        self.unit = unit
        kind = kind_for_unit(unit) if d.type_name == "double" else None
        if kind == "pressure" and pressure_unit != 0:
            kind = None                   # stored in MPa: no conversion from bar
        w: QWidget
        if d.type_name == "bool":
            w = QCheckBox()
            w.toggled.connect(lambda _v: on_change())
        elif d.type_name == "enum" and ("GasType" in key or key.endswith("_GasType")):
            from mlaser.ui.params import GAS_KEYS

            w = QComboBox()
            n = max(len(GAS_KEYS), int(d.max) + 1 if d.max > d.min else 0)
            for i in range(n):
                w.addItem(tr.tr(GAS_KEYS[i]) if i < len(GAS_KEYS) else str(i))
            w.currentIndexChanged.connect(lambda _v: on_change())
        elif d.type_name == "enum" and d.options:
            w = QComboBox()
            for o in d.options:
                w.addItem(tr.tr(o[0]) if o[0] else str(o[1]))
            for i in range(len(d.options), int(d.max) + 1 if d.max > d.min else 0):
                w.addItem(str(i))
            w.currentIndexChanged.connect(lambda _v: on_change())
        elif d.type_name in ("int", "enum"):
            w = QSpinBox()
            w.setRange(int(d.min) if d.max > d.min else -2 ** 31, int(d.max) if d.max > d.min else 2 ** 31 - 1)
            w.setKeyboardTracking(False)
            if unit:
                w.setSuffix(f" {unit}")
            w.valueChanged.connect(lambda _v: on_change())
        elif d.type_name == "string":
            w = QLineEdit()
            w.editingFinished.connect(on_change)
        elif kind is not None:
            lo, hi = (d.min, d.max) if d.max > d.min else (-1e6, 1e6)
            w = QuantitySpinBox(kind, minimum=lo, maximum=hi, decimals=3)   # as stored: no rounding
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda _v: on_change())
        else:
            w = QDoubleSpinBox()
            w.setDecimals(3)
            w.setRange(d.min if d.max > d.min else -1e9, d.max if d.max > d.min else 1e9)
            w.setKeyboardTracking(False)
            if unit:
                w.setSuffix(f" {unit}")
            w.valueChanged.connect(lambda _v: on_change())
        if isinstance(w, QComboBox):
            w.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            w.setMinimumContentsLength(6)
        w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        shrinkable(w)
        self.widget = w
        group, name = tr.tr_param(d.lang)
        self.group = group or tr.tr("techform.group.general")
        plain = PLAIN.get(key)
        st = stage_of(key)
        if plain:
            self.label_text = tr.tr(f"techform.f.{plain}")
            self.help = tr.tr(f"techform.h.{plain}")
        else:
            self.label_text = name or key
            self.help = ""
        self.stage = st
        rng = f"{d.min:g} … {d.max:g} {unit}".strip() if d.max > d.min and d.type_name not in ("bool", "enum") else ""
        tip = (self.help + "\n\n" if self.help else "") + (name or key) + f"  [{key}]"
        if rng:
            tip += f"\n{tr.tr('tech.range')}: {rng}"
        self.tooltip = tip
        w.setToolTip(tip)
        w.setAccessibleName(self.label_text)
        # a checkbox carries its own text (DESIGN.md §3); the unit is in the field's suffix only
        self.label = QLabel("" if isinstance(w, QCheckBox) else self.label_text)
        if isinstance(w, QCheckBox):
            w.setText(self.label_text)
        self.label.setToolTip(tip)
        self.label.setBuddy(w)
        self.label.setWordWrap(True)
        from .icons import icon
        self.reset = QToolButton()
        self.reset.setObjectName("techform_reset")
        self.reset.setIcon(icon("arrow-back-up"))
        self.reset.setAccessibleName(tr.tr("techform.reset"))
        self.reset.setAutoRaise(True)
        self.reset.setVisible(False)
        self.reset.setFocusPolicy(Qt.TabFocus)
        self.container = QWidget()
        row = QHBoxLayout(self.container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        row.addWidget(w, 1)
        row.addWidget(self.reset)
        self.base: Any = None
        self.overridden = False
        self.secondary = False
        self.section = ""

    # ---------------------------------------------------------------- value
    def set(self, v: Any) -> None:
        w = self.widget
        w.blockSignals(True)
        try:
            if isinstance(w, QCheckBox):
                w.setChecked(bool(v))
            elif isinstance(w, QComboBox):
                w.setCurrentIndex(max(0, min(w.count() - 1, int(v or 0))))
            elif isinstance(w, QSpinBox):
                w.setValue(int(float(v or 0)))
            elif isinstance(w, (QDoubleSpinBox, QuantitySpinBox)):
                w.setValue(float(v or 0.0))
            else:
                w.setText("" if v is None else str(v))
        finally:
            w.blockSignals(False)

    def get(self) -> Any:
        w = self.widget
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QComboBox):
            return w.currentIndex()
        if isinstance(w, QSpinBox):
            return w.value()
        if isinstance(w, (QDoubleSpinBox, QuantitySpinBox)):
            return round(float(w.value()), 6)
        return w.text()

    def text_value(self, v: Any) -> str:
        """``v`` as the field shows it (for the "was ..." tooltip and the compare view)."""
        w = self.widget
        if isinstance(w, QCheckBox):
            return self.tr_.tr("techform.on") if v else self.tr_.tr("techform.off")
        if isinstance(w, QComboBox):
            i = int(v or 0)
            return w.itemText(i) if 0 <= i < w.count() else str(v)
        if isinstance(v, float):
            return f"{v:g} {self.unit}".strip()
        return f"{v} {self.unit}".strip() if v not in (None, "") else "—"

    def update_marker(self, base_label: str) -> None:
        cur = self.get()
        self.overridden = self.base is not None and not T._same(cur, self.base)
        self.reset.setVisible(self.overridden)
        if self.overridden:
            self.reset.setToolTip(self.tr_.format("techform.reset_tip", base=base_label,
                                                  value=self.text_value(self.base)))
        # changed values: bold label plus the visible reset button (not colour alone)
        for lab in (self.label, self.widget) if isinstance(self.widget, QCheckBox) else (self.label,):
            f = lab.font()
            f.setBold(self.overridden)
            lab.setFont(f)
        self.label.setProperty("overridden", self.overridden)

    def matches(self, text: str) -> bool:
        t = text.strip().lower()
        return not t or t in self.label_text.lower() or t in self.key.lower() or t in self.help.lower() \
            or t in self.group.lower()


class TechForm(QWidget):
    """The parameter form (see the module docstring).  ``values()`` returns every field's value
    (GP key -> value); ``set_values(full, base, base_label)`` loads them with the base the
    override markers compare with."""

    changed = Signal()

    def __init__(self, tr: Any, pressure_unit: int = 0, parent: Optional[QWidget] = None,
                 advanced: bool = False, scope: Optional[str] = None) -> None:
        super().__init__(parent)
        self.tr_ = tr
        self.scope = scope            # "recipe" / "layer": the fields are that home's editors
        self.setObjectName("techform")
        self._loading = False
        self.base_label = ""
        self.fields: dict[str, TechField] = {}
        self.sections: dict[str, Section] = {}
        self._forms: dict[str, ResponsiveForm] = {}
        self._stage_boxes: list[Section] = []
        self._extra_rows: dict[str, list[tuple[QWidget, Callable[[str], bool], bool]]] = {}
        self._pinned: set = set()          # fields "Edit in …" asked to show
        t = tr.tr
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(3)
        # ---- header: what the values are based on, search, all parameters
        self.base_info = QLabel()
        self.base_info.setObjectName("techform_base_info")
        self.base_info.setWordWrap(True)
        self.base_info.setTextFormat(Qt.RichText)
        outer.addWidget(self.base_info)
        bar = QVBoxLayout()
        bar.setSpacing(2)
        self.search = QLineEdit()
        self.search.setObjectName("techform_search")
        self.search.setPlaceholderText(t("techform.search"))
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName(t("techform.search"))
        self.search.textChanged.connect(lambda _t: self.apply_visibility())
        bar.addWidget(self.search, 1)
        self.show_all = QCheckBox(t("techform.show_all"))
        self.show_all.setObjectName("techform_show_all")
        self.show_all.setToolTip(t("techform.show_all_tip"))
        self.show_all.setChecked(advanced)
        self.show_all.toggled.connect(lambda _v: self.apply_visibility())
        from .homes import bind_transient
        bind_transient(self.search, "view.search")      # view controls, not settings
        bind_transient(self.show_all, "view.show_all")
        bar.addWidget(self.show_all)
        outer.addLayout(bar)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("techform_scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        self.body_lay = QVBoxLayout(body)
        self.body_lay.setContentsMargins(0, 0, 4, 0)
        self.body_lay.setSpacing(2)
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll, 1)
        self.empty = QLabel(t("techform.no_match"))
        self.empty.setWordWrap(True)
        self.empty.setVisible(False)
        self.body_lay.addWidget(self.empty)
        # ---- sections
        self.pressure_unit = pressure_unit
        used: set[str] = set()
        for sid, title, primary, secondary in SECTIONS:
            form = self._new_section(sid, t(title))
            for k in primary:
                self._add_field(sid, form, k, secondary=False)
                used.add(k)
            if sid == "piercing":
                self._build_stages(used)
            for k in secondary:
                self._add_field(sid, form, k, secondary=True)
                used.add(k)
        self._extra_anchor = self.body_lay.count()
        # ---- advanced: everything else by registry group
        adv = self._new_section("advanced", t("techform.sec.advanced"), expanded=False)
        for k in T.layer_registry():
            if k in used or k in SKIP or stage_of(k) is not None:
                continue
            self._add_field("advanced", adv, k, secondary=True, grouped=True)
        self.body_lay.addStretch(1)
        self.apply_visibility()

    # ---------------------------------------------------------------- building
    def _new_section(self, sid: str, title: str, expanded: bool = True,
                     at: Optional[int] = None) -> ResponsiveForm:
        form = ResponsiveForm()
        sec = Section(sid, title, form, expanded)
        self.sections[sid] = sec
        self._forms[sid] = form
        if at is None:
            self.body_lay.addWidget(sec)
        else:
            self.body_lay.insertWidget(at, sec)
        return form

    def _add_field(self, sid: str, form: ResponsiveForm, key: str, secondary: bool,
                   grouped: bool = False) -> Optional[TechField]:
        if key in self.fields or key not in T.layer_registry():
            return None
        f = TechField(key, self.tr_, self._edited, self.pressure_unit)
        f.secondary = secondary
        f.section = sid
        f.reset.clicked.connect(lambda _c=False, ff=f: self.reset_field(ff.key))
        if self.scope:
            from .homes import bind_editor
            bind_editor(f.widget, f"{self.scope}.{key}")
        if grouped and form.property("last_group") != f.group:
            form.setProperty("last_group", f.group)
            g = QLabel(f"<b>{f.group}</b>")
            g.setObjectName("techform_group")
            form.form.addRow(g)
            f.group_label = g  # type: ignore[attr-defined]
        form.form.addRow(f.label, f.container)
        self.fields[key] = f
        return f

    def _build_stages(self, used: set) -> None:
        """One collapsible block per pierce stage inside the Piercing section."""
        form = self._forms["piercing"]
        t = self.tr_
        for s in range(MAX_STAGES):
            sf = ResponsiveForm()
            box = Section(f"stage{s}", t.format("techform.stage", n=s + 1), sf, expanded=True)
            box.header.setFont(self.font())
            self._stage_boxes.append(box)
            form.form.addRow(box)
            for k in STAGE_PRIMARY:
                key = f"GP.{k}{s}"
                if self._add_field("piercing", sf, key, secondary=False):
                    used.add(key)
            for k in STAGE_SECONDARY:
                key = f"GP.{k}{s}"
                if self._add_field("piercing", sf, key, secondary=True):
                    used.add(key)
            for k in ("Enable", "Power", "Freq"):
                key = f"GP.BoltDrill_{k}_{s + 1}"
                if self._add_field("piercing", sf, key, secondary=True):
                    used.add(key)
            self._forms[f"stage{s}"] = sf

    def add_section(self, sid: str, title: str, rows: Iterable[tuple[str, QWidget, str, bool]],
                    expanded: bool = True, extra: Optional[QWidget] = None) -> ResponsiveForm:
        """A caller's section (before Advanced): ``rows`` = (label, widget, help, secondary)."""
        form = self._new_section(sid, title, expanded, at=self._extra_anchor)
        self._extra_anchor += 1
        out = self._extra_rows.setdefault(sid, [])
        for label, w, help_, secondary in rows:
            lab = QLabel(label)
            lab.setWordWrap(True)
            lab.setBuddy(w)
            lab.setToolTip(help_)
            w.setToolTip(help_)
            w.setAccessibleName(label)
            if isinstance(w, QComboBox):
                w.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
                w.setMinimumContentsLength(6)
            form.form.addRow(lab, w)
            text = f"{label} {help_}".lower()
            out.append((w, lambda q, tx=text: not q or q in tx, secondary))
        if extra is not None:
            form.form.addRow(extra)
        return form

    # ---------------------------------------------------------------- values
    def set_values(self, full: dict, base: Optional[dict] = None, base_label: str = "") -> None:
        self._loading = True
        try:
            for k, f in self.fields.items():
                f.set(full.get(k, f.d.default_value))
                f.base = None if base is None else base.get(k, f.d.default_value)
            self.base_label = base_label
        finally:
            self._loading = False
        self.update_markers()
        self.apply_visibility()

    def values(self) -> dict:
        return {k: f.get() for k, f in self.fields.items()}

    def set_value(self, key: str, v: Any) -> None:
        self.fields[key].set(v)
        self._edited()

    def reset_field(self, key: str) -> None:
        f = self.fields[key]
        if f.base is not None:
            f.set(f.base)
            self._edited()

    def overridden(self) -> list[str]:
        return [k for k, f in self.fields.items() if f.overridden]

    def _edited(self) -> None:
        if self._loading:
            return
        self.update_markers()
        self.apply_visibility()
        self.changed.emit()

    def update_markers(self) -> None:
        for f in self.fields.values():
            f.update_marker(self.base_label)
        n = len(self.overridden())
        t = self.tr_
        if self.base_label:
            txt = t.format("techform.base", base=self.base_label)
            if n:
                txt += " · <b>" + t.format("techform.overridden", n=n) + "</b>"
        else:
            txt = t.format("techform.overridden", n=n) if n else ""
        self.base_info.setText(txt)
        self.base_info.setVisible(bool(txt))

    # ---------------------------------------------------------------- visibility
    def stages(self) -> int:
        f = self.fields.get("GP.ManuType")
        return T.stage_count({"GP.ManuType": f.get() if f is not None else 0})

    def advanced(self) -> bool:
        return self.show_all.isChecked()

    def reveal_widget(self, w: QWidget) -> None:
        """Show the row of ``w`` (an "Edit in …" target) whatever the filters say."""
        self.search.clear()
        for f in self.fields.values():
            if f.widget is w:
                self._pinned.add(f.key)
                self.show_all.setChecked(f.secondary or self.show_all.isChecked())
        for sid, rows in self._extra_rows.items():
            if any(rw is w for rw, _m, sec in rows if sec):
                self.show_all.setChecked(True)
        self.apply_visibility()

    def field_visible(self, f: TechField, query: str) -> bool:
        """Whether a field applies and is wanted: its stage is used, its switch is on, it is a
        primary field (or all parameters / a search are on) and it matches the search."""
        if f.key in self._pinned and not query:
            return True
        if f.stage is not None and f.stage >= self.stages():
            return False
        dep = DEPENDS.get(f.key)
        if dep is not None and dep in self.fields and not self.fields[dep].get():
            return False
        if query:
            return f.matches(query)
        if f.overridden:
            return True                      # a changed value is never hidden
        return not f.secondary or self.advanced()

    def apply_visibility(self) -> None:
        q = self.search.text().strip().lower()
        shown: dict[str, int] = {}
        for f in self.fields.values():
            vis = self.field_visible(f, q)
            form = self._form_of(f)
            form.form.setRowVisible(f.container, vis)
            if vis:
                shown[f.section] = shown.get(f.section, 0) + 1
                if f.stage is not None:
                    shown[f"stage{f.stage}"] = shown.get(f"stage{f.stage}", 0) + 1
        for sid, rows in self._extra_rows.items():
            for w, match, secondary in rows:
                vis = match(q) and (not secondary or self.advanced() or bool(q))
                self._forms[sid].form.setRowVisible(w, vis)
                if vis:
                    shown[sid] = shown.get(sid, 0) + 1
        for s, box in enumerate(self._stage_boxes):
            self._forms["piercing"].form.setRowVisible(box, shown.get(f"stage{s}", 0) > 0)
        # registry group titles of the Advanced section: only above visible fields
        adv = self._forms.get("advanced")
        if adv is not None:
            groups: dict[QLabel, bool] = {}
            cur = None
            for r in range(adv.form.rowCount()):
                it = adv.form.itemAt(r, QFormLayout.SpanningRole)
                if it is not None and isinstance(it.widget(), QLabel):
                    cur = it.widget()
                    groups[cur] = False
                    continue
                fi = adv.form.itemAt(r, QFormLayout.FieldRole)
                if cur is not None and fi is not None and adv.form.isRowVisible(r):
                    groups[cur] = True
            for lab, vis in groups.items():
                adv.form.setRowVisible(lab, vis)
        for sid, sec in self.sections.items():
            vis = shown.get(sid, 0) > 0
            if sid == "advanced":
                vis = vis and (self.advanced() or bool(q))
            sec.setVisible(vis)
            if q and vis:
                sec.set_expanded(True)
        self.empty.setVisible(bool(q) and not any(sec.isVisible() for sec in self.sections.values()))
        for form in self._forms.values():
            form.update_wrap()

    def _form_of(self, f: TechField) -> ResponsiveForm:
        if f.stage is not None and f"stage{f.stage}" in self._forms and f.section == "piercing":
            return self._forms[f"stage{f.stage}"]
        return self._forms[f.section]

    def section_order(self) -> list[str]:
        """Section ids top to bottom."""
        out = []
        for i in range(self.body_lay.count()):
            w = self.body_lay.itemAt(i).widget()
            if isinstance(w, Section):
                out.append(w.sid)
        return out

    def visible_keys(self) -> list[str]:
        """Parameters currently shown (tests)."""
        out = []
        for k, f in self.fields.items():
            sec = self.sections[f.section]
            if sec.isHidden():
                continue
            form = self._form_of(f)
            if form.form.isRowVisible(f.container) and (f.stage is None or
                                                        self._forms["piercing"].form.isRowVisible(
                                                            self._stage_boxes[f.stage])):
                out.append(k)
        return out


__all__ = ["TechForm", "TechField", "Section", "ResponsiveForm", "FlowLayout", "SECTIONS", "stage_of"]
