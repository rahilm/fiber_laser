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
:class:`mlaser.ui.units_qt.QuantitySpinBox` (the Units track's widget: shown in the user's
units, stored in mm / mm/s / bar).
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QScrollArea, QSizePolicy, QSpinBox, QToolButton, QVBoxLayout, QWidget)

from mlaser.services import technology as T

from mlaser.services.units import kind_for_unit

from .units_qt import QuantitySpinBox


def shrinkable(w: QWidget, chars: int = 7) -> QWidget:
    """Let a spin box shrink to its content instead of to the text of its largest value (a
    range of ±1e6 asks ~170 px and forces a narrow dock to scroll sideways): the minimum width
    becomes ``chars`` digits plus the suffix in the widget's own font."""
    from PySide6.QtWidgets import QAbstractSpinBox

    if isinstance(w, QAbstractSpinBox):
        fm = w.fontMetrics()
        suffix = w.suffix() if hasattr(w, "suffix") else ""
        w.setMinimumWidth(fm.horizontalAdvance("8" * chars + suffix) + 2 * fm.height())
        w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return w


def quantity(kind: str, lo: float, hi: float, decimals: Optional[int] = None,
             step: Optional[float] = None) -> QuantitySpinBox:
    """A QuantitySpinBox (Units track) with the panels' defaults: no keyboard tracking (a value
    is taken when the field is confirmed), shrinkable."""
    s = QuantitySpinBox(kind, minimum=lo, maximum=hi, decimals=decimals, step=step)
    s.setKeyboardTracking(False)
    shrinkable(s)
    return s

# --------------------------------------------------------------------------- layout helpers
# the layouts are the responsive ones (docs/dev/RESPONSIVE.md): FlowLayout, ResponsiveForm and
# the collapsible sections of a ColumnsPanel
from .responsive import LIST_PROP, CollapsibleSection, ColumnsPanel, FlowLayout, ResponsiveForm  # noqa: E402


class Section(CollapsibleSection):
    """A collapsible section of a form (:class:`~mlaser.ui.responsive.CollapsibleSection`): its
    title is cut with "…" in a narrow panel (full text in the tooltip) and, folded, it shows a
    summary line of its values.  ``expanded=False``: folded until the user opens it."""

    def __init__(self, sid: str, title: str, body: QWidget, expanded: bool = True,
                 parent: Optional[QWidget] = None, priority: int = 0) -> None:
        super().__init__(title, body, priority=priority, parent=parent, fit_title=True)
        self.sid = sid
        self.setObjectName(f"techform_section_{sid}")
        self.header.setObjectName("techform_section_header")
        if not expanded:
            self.user_choice = False
            self.set_expanded(False)


def beside_width(panel: ColumnsPanel) -> int:
    """The column width at which every form of ``panel`` has its labels beside the fields:
    a panel takes a second column only when each column is that wide."""
    out = 0
    for sec in panel.sections:
        body = getattr(sec, "body", None)
        if sec.isHidden() or not isinstance(body, ResponsiveForm):
            continue
        m = sec.frame.layout().contentsMargins()
        out = max(out, body.needed_beside() + m.left() + m.right() + 2 * sec.frame.frameWidth())
    return out


class FormScroll(QScrollArea):
    """The scroll area of a form panel: while it is a *form* its minimum height is the panel's
    folded minimum (the page grows instead of scrolling); while it is a *list* (every parameter,
    a search: ``responsive_scroll = "list"``) it may scroll."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def is_list(self) -> bool:
        return self.property(LIST_PROP) == "list"

    def set_list(self, on: bool) -> None:
        if on != self.is_list():
            self.setProperty(LIST_PROP, "list" if on else "")
            self.updateGeometry()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        w = self.widget()
        base = super().minimumSizeHint()
        if w is None:
            return base
        fw = 2 * self.frameWidth()
        m = w.minimumSizeHint()
        h = base.height() if self.is_list() else m.height() + fw
        return QSize(m.width() + fw, h)


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
        self.kind = kind
        self._name = name or key
        tip = self._make_tip()
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
        col = QVBoxLayout(self.container)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        row.addWidget(w, 1)
        row.addWidget(self.reset)
        col.addLayout(row)
        # source chip (inheritance): "set on layer 2" under an overridden value
        self.chip = QLabel()
        self.chip.setObjectName("techform_chip")
        self.chip.setWordWrap(True)
        f = self.chip.font()
        f.setPointSizeF(f.pointSizeF() * 0.85)
        self.chip.setFont(f)
        self.chip.setForegroundRole(self.chip.foregroundRole())
        self.chip.hide()
        col.addWidget(self.chip)
        self.mark: Optional[bool] = None     # explicit override mark (inheritance), None = diff
        self.here_label = ""
        self.base: Any = None
        self.overridden = False
        self.secondary = False
        self.section = ""

    def _make_tip(self) -> str:
        """Help, registry name and key, and the range in the display units."""
        d, tr = self.d, self.tr_
        rng = ""
        if d.max > d.min and d.type_name not in ("bool", "enum"):
            if self.kind is not None:
                from mlaser.services import units
                u = units.current()
                rng = f"{u.fmt(d.min, self.kind, trim=True)} … {u.fmt(d.max, self.kind, trim=True)}"
            else:
                rng = f"{d.min:g} … {d.max:g} {self.unit}".strip()
        tip = (self.help + "\n\n" if self.help else "") + self._name + f"  [{self.key}]"
        if rng:
            tip += f"\n{tr.tr('tech.range')}: {rng}"
        return tip

    def units_changed(self) -> None:
        self.tooltip = self._make_tip()
        self.widget.setToolTip(self.tooltip)
        self.label.setToolTip(self.tooltip)

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
        if self.kind is not None and isinstance(v, (int, float)):
            from mlaser.services import units
            return units.current().fmt(float(v), self.kind, trim=True)
        if isinstance(v, float):
            return f"{v:g} {self.unit}".strip()
        return f"{v} {self.unit}".strip() if v not in (None, "") else "—"

    def update_marker(self, base_label: str) -> None:
        cur = self.get()
        if self.mark is not None:
            self.overridden = self.mark
        else:
            self.overridden = self.base is not None and not T._same(cur, self.base)
        self.reset.setVisible(self.overridden and self.base is not None)
        if self.here_label:
            self.chip.setText(self.here_label if self.overridden else "")
            self.chip.setVisible(self.overridden)
            tip = self.tooltip + "\n\n" + (self.here_label if self.overridden else
                                             self.tr_.format("techform.from", base=base_label))
            self.widget.setToolTip(tip)
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


def widget_text(w: QWidget) -> str:
    """What an input shows, for a folded section's summary line."""
    from PySide6.QtWidgets import QAbstractSpinBox
    if isinstance(w, QCheckBox):
        return w.text() if w.isChecked() else ""
    if isinstance(w, QComboBox):
        return w.currentText()
    if isinstance(w, QAbstractSpinBox):
        return w.text().strip()
    if isinstance(w, QLineEdit):
        return w.text().strip()
    inner = getattr(w, "editor", None)            # an InheritedField
    return widget_text(inner) if isinstance(inner, QWidget) else ""


#: section id -> priority in the panel (a folded section of lower priority goes first)
PRIORITY = {"cutting": 60, "leads": 50, "layer_craft": 50, "gas": 45, "height": 40, "piercing": 30,
            "corners": 20, "record": 10, "advanced": 0}


class TechForm(QWidget):
    """The parameter form (see the module docstring).  ``values()`` returns every field's value
    (GP key -> value); ``set_values(full, base, base_label)`` loads them with the base the
    override markers compare with.

    Layout (docs/dev/RESPONSIVE.md): the sections are a :class:`ColumnsPanel` (as many columns
    as the width allows; when the height runs out the sections fold, lowest priority first, each
    showing a summary line, so the form does not scroll).  *Show all parameters* and a search
    turn it into a list of every matching parameter, which may scroll."""

    changed = Signal()
    fieldEdited = Signal(str, object)     # key, new value (a user edit of one field)
    fieldReset = Signal(str)              # key: the reset button of an overridden field

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
        self._stage_labels: list[QLabel] = []
        self._group_labels: dict[str, list[tuple[QLabel, list[TechField]]]] = {}
        self._extra_rows: dict[str, list[tuple[QWidget, Callable[[str], bool], bool]]] = {}
        self._pinned: set = set()          # fields "Edit in …" asked to show
        self._widget_sections: set[str] = set()
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
        bar = QWidget()
        bl = FlowLayout(bar, justify=True)
        bl.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setObjectName("techform_search")
        self.search.setPlaceholderText(t("techform.search"))
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName(t("techform.search"))
        fm = self.search.fontMetrics()
        self.search.setMinimumWidth(fm.horizontalAdvance(t("techform.search")) + 3 * fm.height())
        self.search.textChanged.connect(lambda _t: self.apply_visibility())
        bl.addWidget(self.search)
        self.show_all = QCheckBox(t("techform.show_all"))
        self.show_all.setObjectName("techform_show_all")
        self.show_all.setToolTip(t("techform.show_all_tip"))
        self.show_all.setChecked(advanced)
        self.show_all.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.show_all.toggled.connect(lambda _v: self.apply_visibility())
        from .homes import bind_transient
        bind_transient(self.search, "view.search")      # view controls, not settings
        bind_transient(self.show_all, "view.show_all")
        bl.addWidget(self.show_all)
        outer.addWidget(bar)
        self.scroll = FormScroll()
        self.scroll.setObjectName("techform_scroll")
        self.panel = ColumnsPanel(max_columns=3, scroll_overflow=True)
        self.panel.setObjectName("techform_panel")
        self.panel.column_min = lambda: beside_width(self.panel)
        self.scroll.setWidget(self.panel)
        outer.addWidget(self.scroll, 1)
        self.empty = QLabel(t("techform.no_match"))
        self.empty.setWordWrap(True)
        self.empty.setVisible(False)
        self.panel.add_pinned(self.empty)
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
        self._extra_anchor = len(self.panel.sections)
        # ---- advanced: everything else by registry group
        adv = self._new_section("advanced", t("techform.sec.advanced"), expanded=False)
        for k in T.layer_registry():
            if k in used or k in SKIP or stage_of(k) is not None:
                continue
            self._add_field("advanced", adv, k, secondary=True, grouped=True)
        self.apply_visibility()

    # ---------------------------------------------------------------- building
    def _new_section(self, sid: str, title: str, expanded: bool = True,
                     at: Optional[int] = None, priority: Optional[int] = None) -> ResponsiveForm:
        form = ResponsiveForm()
        sec = Section(sid, title, form, expanded, priority=PRIORITY.get(sid, 25) if priority is None else priority)
        self.sections[sid] = sec
        self._forms[sid] = form
        self.panel.add_section(sec)
        if at is not None:
            self.panel.sections.remove(sec)
            self.panel.sections.insert(at, sec)
        sec.toggled.connect(lambda _on: self._update_summaries())
        return form

    def _add_field(self, sid: str, form: ResponsiveForm, key: str, secondary: bool,
                   grouped: bool = False) -> Optional[TechField]:
        if key in self.fields or key not in T.layer_registry():
            return None
        f = TechField(key, self.tr_, lambda k=key: self._field_edited(k), self.pressure_unit)
        f.secondary = secondary
        f.section = sid
        f.reset.clicked.connect(lambda _c=False, ff=f: self.reset_field(ff.key))
        if self.scope:
            from .homes import bind_editor
            bind_editor(f.widget, f"{self.scope}.{key}")
        if f.kind is not None:
            from .units_qt import on_units_changed
            on_units_changed(f.label, lambda _u, ff=f: ff.units_changed(), now=False)
        if grouped:
            groups = self._group_labels.setdefault(sid, [])
            if not groups or groups[-1][1][0].group != f.group:
                g = QLabel(f"<b>{f.group}</b>")
                g.setObjectName("techform_group")
                form.addRow(g)
                groups.append((g, []))
            groups[-1][1].append(f)
        form.addRow(f.label, f.container)
        self.fields[key] = f
        return f

    def _build_stages(self, used: set) -> None:
        """One block per pierce stage inside the Piercing section: a heading and its rows."""
        form = self._forms["piercing"]
        t = self.tr_
        for s in range(MAX_STAGES):
            lab = QLabel(f"<b>{t.format('techform.stage', n=s + 1)}</b>")
            lab.setObjectName(f"techform_stage{s}")
            self._stage_labels.append(lab)
            form.addRow(lab)
            for k in STAGE_PRIMARY:
                key = f"GP.{k}{s}"
                if self._add_field("piercing", form, key, secondary=False):
                    used.add(key)
            for k in STAGE_SECONDARY:
                key = f"GP.{k}{s}"
                if self._add_field("piercing", form, key, secondary=True):
                    used.add(key)
            for k in ("Enable", "Power", "Freq"):
                key = f"GP.BoltDrill_{k}_{s + 1}"
                if self._add_field("piercing", form, key, secondary=True):
                    used.add(key)

    def add_section(self, sid: str, title: str, rows: Iterable[tuple[str, QWidget, str, bool]],
                    expanded: bool = True, extra: Optional[QWidget] = None,
                    priority: Optional[int] = None) -> ResponsiveForm:
        """A caller's section (before Advanced): ``rows`` = (label, widget, help, secondary)."""
        form = self._new_section(sid, title, expanded, at=self._extra_anchor, priority=priority)
        self._extra_anchor += 1
        out = self._extra_rows.setdefault(sid, [])
        for label, w, help_, secondary in rows:
            lab = QLabel(label) if label else None
            if lab is not None:
                lab.setWordWrap(True)
                lab.setBuddy(w)
                lab.setToolTip(help_)
            w.setToolTip(help_)
            w.setAccessibleName(label or w.accessibleName() or (w.text() if hasattr(w, "text") else ""))
            if isinstance(w, QComboBox):
                w.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
                w.setMinimumContentsLength(6)
            form.addRow(lab, w)
            text = f"{label} {help_}".lower()
            out.append((w, lambda q, tx=text: not q or q in tx, secondary))
            for sig in ("valueChanged", "currentIndexChanged", "toggled", "textChanged"):
                if hasattr(w, sig):
                    getattr(w, sig).connect(lambda *_a: self._update_summaries())
                    break
        if extra is not None:
            form.addRow(extra)
        return form

    def add_widget_section(self, sid: str, title: str, body: QWidget, at: int = 0, expanded: bool = True,
                           priority: Optional[int] = None) -> Section:
        """A caller's section holding any widget (the layer inspector's recipe picker): folds
        with the others; hidden while a search runs."""
        sec = Section(sid, title, body, expanded, priority=PRIORITY.get(sid, 25) if priority is None else priority)
        self.sections[sid] = sec
        self.panel.add_section(sec)
        self.panel.sections.remove(sec)
        self.panel.sections.insert(at, sec)
        self._extra_anchor += 1
        self._widget_sections.add(sid)
        self.apply_visibility()
        return sec

    # ---------------------------------------------------------------- values
    def set_values(self, full: dict, base: Optional[dict] = None, base_label: str = "",
                   marks: Optional[set] = None, here_label: str = "") -> None:
        """Load values.  ``base``: what they are compared with / reset to; ``marks``: the keys
        overridden here (inheritance; without it a value differing from ``base`` counts as
        changed); ``here_label``: the words of the chip under an overridden value ("set on
        layer 2")."""
        self._loading = True
        try:
            for k, f in self.fields.items():
                f.set(full.get(k, f.d.default_value))
                f.base = None if base is None else base.get(k, f.d.default_value)
                f.mark = None if marks is None else (k in marks)
                f.here_label = here_label
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

    def _field_edited(self, key: str) -> None:
        if self._loading:
            return
        self._edited()
        self.fieldEdited.emit(key, self.fields[key].get())

    def reset_field(self, key: str) -> None:
        f = self.fields[key]
        if f.mark is not None:                  # inheritance: the owner clears the override
            self.fieldReset.emit(key)
            return
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

    def is_list(self) -> bool:
        """Every parameter (or a search): a list that may scroll, not a form that fits."""
        return self.advanced() or bool(self.search.text().strip())

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
        for sid, form in self._forms.items():
            if form.isAncestorOf(w):
                self.sections[sid].open_by_user()
        self.panel.relayout()
        self.scroll.ensureWidgetVisible(w)

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
        stage_shown: dict[int, int] = {}
        for f in self.fields.values():
            vis = self.field_visible(f, q)
            self._form_of(f).set_row_visible(f.container, vis)
            if vis:
                shown[f.section] = shown.get(f.section, 0) + 1
                if f.stage is not None:
                    stage_shown[f.stage] = stage_shown.get(f.stage, 0) + 1
        for sid, rows in self._extra_rows.items():
            for w, match, secondary in rows:
                vis = match(q) and (not secondary or self.advanced() or bool(q))
                self._forms[sid].set_row_visible(w, vis)
                if vis:
                    shown[sid] = shown.get(sid, 0) + 1
        for s, lab in enumerate(self._stage_labels):
            self._forms["piercing"].set_row_visible(lab, stage_shown.get(s, 0) > 0)
        # registry group titles of the Advanced section: only above visible fields
        for sid, groups in self._group_labels.items():
            for lab, fs in groups:
                self._forms[sid].set_row_visible(lab, any(self._form_of(f).is_row_visible(f.container) for f in fs))
        listy = self.is_list()
        for sid, sec in self.sections.items():
            vis = shown.get(sid, 0) > 0 or (sid in self._widget_sections and not q)
            if sid == "advanced":
                vis = vis and (self.advanced() or bool(q))
            sec.setVisible(vis)
            if q and vis and sec.user_choice is False:
                sec.user_choice = None           # a search shows what it found
        self.empty.setVisible(bool(q) and not any(not sec.isHidden() for sec in self.sections.values()))
        self.panel.fold = not listy
        self.scroll.set_list(listy)
        for form in self._forms.values():
            if form.width() > 0:
                form.set_beside(form.width() >= form.needed_beside())
        self._update_summaries()
        self.panel.schedule()

    def _update_summaries(self) -> None:
        """The summary line of each section (shown while it is folded): its first visible
        values ("100 mm/s · 100 % · 5000 Hz")."""
        for sid, sec in self.sections.items():
            if sid in self._widget_sections or (sec.expanded() and sec.summary()):
                continue
            parts: list[str] = []
            for f in self.fields.values():
                if f.section == sid and self._form_of(f).is_row_visible(f.container) and f.stage in (None, 0):
                    txt = f.text_value(f.get())
                    if txt and txt != "—":
                        parts.append(txt)
                if len(parts) >= 3:
                    break
            for w, _m, _sec in self._extra_rows.get(sid, []):
                if len(parts) >= 3:
                    break
                if self._forms[sid].is_row_visible(w):
                    txt = widget_text(w)
                    if txt:
                        parts.append(txt)
            sec.set_summary(" · ".join(parts))

    def _form_of(self, f: TechField) -> ResponsiveForm:
        return self._forms[f.section]

    def section_order(self) -> list[str]:
        """Section ids top to bottom."""
        return [s.sid for s in self.panel.sections if isinstance(s, Section)]

    def visible_keys(self) -> list[str]:
        """Parameters currently shown (tests)."""
        out = []
        for k, f in self.fields.items():
            sec = self.sections[f.section]
            if sec.isHidden():
                continue
            if self._form_of(f).is_row_visible(f.container):
                out.append(k)
        return out


__all__ = ["TechForm", "TechField", "Section", "ResponsiveForm", "FlowLayout", "SECTIONS", "stage_of"]
