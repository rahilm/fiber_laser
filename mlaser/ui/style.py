"""The design system's tokens and helpers (docs/dev/STYLE.md; the UI audit's DESIGN.md).

One module, one constant per token, and a helper for every rule, so nobody has to remember the
rule:

* **spacing** on an 8 px grid: :data:`SP0` .. :data:`SP5`; :func:`margins` sets a layout's
  margins by kind (``panel``, ``dialog``, ``dense``, ``dock``, ``none``), never a literal;
* **type scale** as ratios of the platform font (never an absolute ``pt``: macOS points are about
  25 % smaller than Windows / Linux ones): :func:`font` with the roles ``caption``, ``body``,
  ``label_strong``, ``title``, ``display`` and ``display_run``;
* **colour roles** from the theme's palette (:data:`mlaser.ui.shell.theme.THEMES`): ``danger``,
  ``warning``, ``info``, ``success`` with their ``*_fill`` and ``on_*`` partners, ``muted``,
  ``armed``...: :func:`color`, :func:`css`, and :func:`styled` for a domain widget's style sheet
  that follows a theme switch;
* **sizes**: :data:`ICON_S` / :data:`ICON_M` / :data:`ICON_L`, :data:`TARGET` (mouse),
  :data:`TARGET_TOUCH`, :data:`TARGET_RUN` (run bar, Run screen);
* **buttons**: :func:`button`, :func:`primary_button`, :func:`danger_button`,
  :func:`tool_button` (icon only: tooltip and accessible name required), :func:`run_button`
  (Start, Hold/Continue, STOP, Frame, Dry run, Arm laser: one source of truth for the run bar,
  the Run screen and the wizards), :func:`button_row`;
* **forms**: :func:`form` (the platform's label alignment, rows wrap when the form is narrower
  than its widest label plus field) and :func:`form_row` (units in the field's suffix only, a
  caption hint, the reason of a disabled field);
* :func:`section` (a heading and an inset body, no group-box frame), :class:`Dialog` /
  :func:`dialog` (the one dialog frame: title, explanation, :class:`QDialogButtonBox` in the
  platform's order, SP3 margins, size from content), :func:`table` (a stretched key column,
  column priorities instead of a horizontal scroll bar);
* :func:`set_enabled`: a disabled control says why (its tooltip).

``tests/test_style_lint.py`` keeps the code on these: no colour literals outside
:mod:`mlaser.ui.shell.theme` and the safety colour table (:data:`mlaser.ui.safety_ui.COLORS`),
no absolute font sizes, no literal layout margins.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence, Union

from PySide6.QtCore import QEvent, QObject, QSize, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QAbstractButton, QApplication, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLayout, QPushButton, QSizePolicy,
                               QTableWidget, QToolButton, QVBoxLayout, QWidget)


# --------------------------------------------------------------------------- tokens
#: spacing (px, logical): inside composite widgets / icon to label / related controls, form rows /
#: panel and dialog margins, between sections / between groups / empty states, Run screen header
SP0, SP1, SP2, SP3, SP4, SP5 = 0, 4, 8, 16, 24, 32
#: icon sizes (px): menus and inline / toolbars and buttons / touch, rail, Run screen
ICON_S, ICON_M, ICON_L = 16, 20, 28
#: minimum heights (px) of clickable controls: mouse / touch (jog pad, outputs, wizards, every
#: control in Operator density) / the run bar and the Run screen (STOP wider still)
TARGET, TARGET_TOUCH, TARGET_RUN = 32, 44, 56
STOP_EXTRA = 24            # STOP is this much wider than the other run buttons

MARGINS = {"panel": SP3, "dialog": SP3, "dense": SP2, "dock": SP2, "tight": SP1, "none": SP0}

#: the type scale: (size ratio of the platform font, weight)
TYPE_SCALE: dict[str, tuple[float, QFont.Weight]] = {
    "caption": (0.85, QFont.Normal),
    "body": (1.0, QFont.Normal),
    "label_strong": (1.0, QFont.DemiBold),
    "title": (1.25, QFont.DemiBold),
    "display": (1.6, QFont.Bold),
    "display_run": (2.0, QFont.Bold),      # the Run screen's machine state and STOP
}

#: the semantic colour roles every theme defines (shell/theme.py THEMES)
ROLES = ("danger", "on_danger", "danger_fill", "warning", "warning_fill", "on_warning", "info",
         "info_fill", "on_info", "success", "success_fill", "on_success", "muted", "text", "window",
         "base", "border", "accent", "accent_text")


def is_mac() -> bool:
    return sys.platform == "darwin"


# --------------------------------------------------------------------------- spacing
def margins(target: Union[QWidget, QLayout], kind: str = "panel") -> None:
    """Set the contents margins of a layout (or a widget's layout) by kind; the spacing of a
    layout is SP2 (``none``/``tight``: SP1)."""
    m = MARGINS[kind]
    lay = target.layout() if isinstance(target, QWidget) and target.layout() is not None else target
    lay.setContentsMargins(m, m, m, m)
    if isinstance(lay, QLayout):
        lay.setSpacing(SP1 if kind in ("none", "tight") else SP2)


def spacing(layout: QLayout, sp: int = SP2) -> QLayout:
    layout.setSpacing(sp)
    return layout


def vbox(parent: Optional[QWidget] = None, kind: str = "none", sp: Optional[int] = None) -> QVBoxLayout:
    """A QVBoxLayout with ``kind`` margins (default none) and SP2 (or ``sp``) spacing."""
    lay = QVBoxLayout(parent) if parent is not None else QVBoxLayout()
    margins(lay, kind)
    lay.setSpacing(SP2 if sp is None else sp)
    return lay


def hbox(parent: Optional[QWidget] = None, kind: str = "none", sp: Optional[int] = None) -> QHBoxLayout:
    lay = QHBoxLayout(parent) if parent is not None else QHBoxLayout()
    margins(lay, kind)
    lay.setSpacing(SP2 if sp is None else sp)
    return lay


# --------------------------------------------------------------------------- type
def font(role: str = "body", base: Optional[QFont] = None) -> QFont:
    """A QFont of the type-scale ``role``, relative to ``base`` (default: the application
    font): a user who enlarges the system font gets a larger UI everywhere."""
    ratio, weight = TYPE_SCALE[role]
    f = QFont(base if base is not None else QGuiApplication.font())
    if f.pointSizeF() > 0:
        f.setPointSizeF(f.pointSizeF() * ratio)
    elif f.pixelSize() > 0:
        f.setPixelSize(max(1, round(f.pixelSize() * ratio)))
    f.setWeight(weight)
    return f


def set_font(widget: QWidget, role: str) -> QWidget:
    """Give ``widget`` the ``role`` font of the application font (kept on a font change of the
    application: the role is stored as a property and re-applied)."""
    widget.setFont(font(role))
    widget.setProperty("type_role", role)
    return widget


def caption(text: str = "", wrap: bool = True) -> QLabel:
    """A muted caption label (hints, units under fields, footers)."""
    lab = QLabel(text)
    lab.setObjectName("caption")
    lab.setWordWrap(wrap)
    set_font(lab, "caption")
    styled(lab, lambda: f"color:{css('muted')};")
    return lab


def heading(text: str = "", role: str = "label_strong") -> QLabel:
    lab = QLabel(text)
    set_font(lab, role)
    lab.setWordWrap(True)
    return lab


def menu_text(s: str) -> str:
    """A menu item's text: Title Case on macOS (the HIG asks for it), the catalogue's sentence
    case elsewhere."""
    if not is_mac():
        return s
    small = {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with"}
    words = s.split(" ")
    out = []
    for i, w in enumerate(words):
        if w and (i == 0 or i == len(words) - 1 or w.lower() not in small) and w[0].islower():
            w = w[0].upper() + w[1:]
        out.append(w)
    return " ".join(out)


def shortcut_text(seq: Union[str, QKeySequence, QKeySequence.StandardKey]) -> str:
    """A shortcut as the platform writes it (⌘K on macOS, Ctrl+K elsewhere)."""
    ks = seq if isinstance(seq, QKeySequence) else QKeySequence(seq)
    return ks.toString(QKeySequence.NativeText)


# --------------------------------------------------------------------------- colour
def _themes():
    from .shell import theme
    return theme


def current_theme() -> str:
    """The effective theme (light / dark / high_contrast) the application shows now."""
    _theme = _themes()
    app = QApplication.instance()
    t = app.property("mlaser_theme") if app is not None else None
    return _theme.effective(str(t) if t else _theme.SYSTEM)


def color(role: str, theme: Optional[str] = None) -> QColor:
    """The colour of a semantic role (``danger``, ``warning``, ``info``, ``success``, their
    ``*_fill`` / ``on_*`` partners, ``muted``, ``text`` ...) in ``theme`` (default: the effective
    one now)."""
    return QColor(css(role, theme))


def css(role: str, theme: Optional[str] = None) -> str:
    """The colour of ``role`` as ``#rrggbb`` for a domain widget's style sheet."""
    t = _themes().tokens(theme or current_theme())
    if role not in t:
        raise KeyError(f"unknown colour role {role!r}")
    return t[role]


def state_colors(kind: str) -> tuple[str, str]:
    """(background, text) of a machine state (``ARMED``, ``READY``, ``FAULT`` ...): the safety
    colour table, the same in every theme (docs/SAFETY.md: the state is also written out)."""
    from .safety_ui import COLORS
    return COLORS.get(kind, COLORS["OFFLINE"])


class _Restyle(QObject):
    """Re-applies a style sheet built from colour roles when the theme changes."""

    EVENTS = (QEvent.PaletteChange, QEvent.ApplicationPaletteChange, QEvent.StyleChange, QEvent.ThemeChange)

    def __init__(self, w: QWidget, build: Callable[[], str]):
        super().__init__(w)
        self.build = build
        self.apply(w)

    def apply(self, w: QWidget) -> None:
        qss = self.build()
        if w.styleSheet() != qss:
            w.setStyleSheet(qss)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        if ev.type() in self.EVENTS:
            self.apply(obj)
        return False


def styled(widget: QWidget, build: Callable[[], str]) -> QWidget:
    """Give a *domain* widget (STOP, the run bar, banners, chips, canvas overlays: never a
    standard button, input, tab or list) the style sheet ``build()`` returns, built from
    :func:`css` roles, and rebuild it when the theme or the palette changes."""
    old = widget.findChild(_Restyle, "", Qt.FindDirectChildrenOnly)
    if old is not None:
        widget.removeEventFilter(old)
        old.deleteLater()
    f = _Restyle(widget, build)
    widget.installEventFilter(f)
    return widget


def chip(widget: QLabel, role: str = "warning") -> QLabel:
    """A notice chip (a label on the role's fill, in the role's text colour, with padding)."""
    styled(widget, lambda: (f"background:{css(role + '_fill')}; color:{css('on_' + role)};"
                            f"border:1px solid {css(role)}; padding:{SP1}px {SP2}px; border-radius:{SP1}px;"))
    return widget


# --------------------------------------------------------------------------- buttons
def _icon(name: Optional[str], tint: Optional[str] = None):
    from .icons import icon
    return icon(name, tint)


def button(text: str = "", *, icon: Optional[str] = None, kind: str = "secondary",
           on_click: Optional[Callable[[], Any]] = None, tooltip: str = "",
           parent: Optional[QWidget] = None) -> QPushButton:
    """A push button.  ``kind``: ``secondary`` (native), ``primary`` (the default button: the one
    action a dialog or card exists for), ``danger`` (text in the danger colour; never filled:
    STOP is the only filled red button).  An icon only when every button of its group has one
    (DESIGN.md §2)."""
    b = QPushButton(text, parent)
    if icon:
        b.setIcon(_icon(icon))
        b.setIconSize(QSize(ICON_M, ICON_M))
    if tooltip:
        b.setToolTip(tooltip)
    if kind == "primary":
        b.setDefault(True)
        b.setAutoDefault(True)
    elif kind == "danger":
        pal = b.palette()
        pal.setColor(pal.ColorRole.ButtonText, color("danger"))
        b.setPalette(pal)
        b.setProperty("button_kind", "danger")
    if on_click is not None:
        b.clicked.connect(lambda _=False: on_click())
    return b


def primary_button(text: str, **kw) -> QPushButton:
    return button(text, kind="primary", **kw)


def danger_button(text: str, **kw) -> QPushButton:
    return button(text, kind="danger", **kw)


def tool_button(text: str, icon: str, *, tooltip: str = "", on_click: Optional[Callable[[], Any]] = None,
                size: int = ICON_M, parent: Optional[QWidget] = None) -> QToolButton:
    """An icon-only tool button (toolbars, the jog pad, dense rows where every neighbour is also
    icon-only): ``text`` becomes the accessible name and, with the shortcut, the tooltip."""
    if not text:
        raise ValueError("an icon-only button needs a text (its accessible name)")
    b = QToolButton(parent)
    b.setText(text)
    b.setIcon(_icon(icon))
    b.setIconSize(QSize(size, size))
    b.setToolButtonStyle(Qt.ToolButtonIconOnly)
    b.setToolTip(tooltip or text)
    b.setAccessibleName(text)
    if on_click is not None:
        b.clicked.connect(lambda _=False: on_click())
    return b


@dataclass(frozen=True)
class RunKind:
    """One run control: catalogue ids of its text (and alternative text), its tooltip, its icon
    and its colour role."""
    text: str
    default: str
    tip: str
    tip_default: str
    icon: str
    role: str = ""                 # icon tint / fill role ("" = text colour)
    alt: str = ""                  # the other state's text id (Hold -> Continue, Arm -> Disarm)
    alt_default: str = ""


#: the run controls (DESIGN.md §2 "Touch / run" and §6 words): one table for the run bar, the Run
#: screen and the wizards
RUN_KINDS: dict[str, RunKind] = {
    "start": RunKind("run.btn.start", "Start", "run.tip.start", "Start or continue (F5): pre-flight check first",
                     "player-play", "success", "run.btn.continue", "Continue"),
    "hold": RunKind("run.btn.hold", "Hold", "run.tip.hold", "Hold / continue (Space)", "player-pause", "warning",
                    "run.btn.continue", "Continue"),
    "stop": RunKind("run.btn.stop", "STOP", "run.tip.stop", "Stop the job (Esc or F12)", "player-stop",
                    "danger_fill"),
    "frame": RunKind("run.btn.frame", "Frame", "run.tip.frame", "Trace the job outline, laser off (F6)", "frame"),
    "dry_run": RunKind("run.btn.dry_run", "Dry run", "run.tip.dry_run", "Run the job with the laser off", "route"),
    "arm": RunKind("run.btn.arm", "Arm laser", "run.tip.arm", "Arm or disarm laser emission", "bolt", "",
                   "run.btn.disarm", "Disarm laser"),
}


def _tr(tr, key: str, default: str) -> str:
    if tr is None:
        return default
    try:
        if hasattr(tr, "has") and not tr.has(key):
            return default
        return tr.tr(key)
    except Exception:  # noqa: BLE001 - a missing catalogue never hides a run button
        return default


def run_text(kind: str, tr=None, alt: bool = False) -> str:
    k = RUN_KINDS[kind]
    return _tr(tr, k.alt, k.alt_default) if alt and k.alt else _tr(tr, k.text, k.default)


def run_button(kind: str, tr=None, on_click: Optional[Callable[[], Any]] = None, *,
               height: int = TARGET_RUN, role: str = "run", parent: Optional[QWidget] = None) -> QPushButton:
    """A run control (Start, Hold, STOP, Frame, Dry run, Arm laser): icon before the text, the
    glossary's words, at least ``height`` px tall (kept against style sheets), STOP filled in
    the danger colour.  ``role`` names the object (``run_<kind>``)."""
    from .responsive import keep_min_size
    k = RUN_KINDS[kind]
    b = QPushButton(run_text(kind, tr), parent)
    b.setObjectName(f"{role}_{kind}")
    b.setToolTip(_tr(tr, k.tip, k.tip_default))
    b.setAccessibleName(b.text())
    b.setProperty("run_kind", kind)
    b.setIconSize(QSize(ICON_L, ICON_L) if height >= TARGET_RUN else QSize(ICON_M, ICON_M))
    tint = "on_danger" if kind == "stop" else k.role

    def paint() -> str:
        b.setIcon(_icon(k.icon, css(tint) if tint else None))
        if kind == "stop":
            return (f"QPushButton{{background:{css('danger_fill')};color:{css('on_danger')};font-weight:bold;"
                    f"border-radius:{SP1 + 2}px;padding:{SP1}px {SP3}px;}}"
                    f"QPushButton:pressed{{background:{css('danger')};}}"
                    f"QPushButton:disabled{{background:{css('border')};}}")
        return ""
    if kind == "stop":
        styled(b, paint)
    else:
        paint()
    keep_min_size(b, QSize(height + (STOP_EXTRA if kind == "stop" else 0), height))
    b.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
    if on_click is not None:
        b.clicked.connect(lambda _=False: on_click())
    return b


def button_row(*buttons: QWidget, equal: bool = False, align: str = "trailing",
               parent: Optional[QWidget] = None) -> QWidget:
    """A row of buttons: ``equal`` = an equal-width choice set; else content width, aligned to
    the trailing edge (dialog actions) or the leading one. Never a mix in one row."""
    box = QWidget(parent)
    lay = hbox(box)
    if not equal and align == "trailing":
        lay.addStretch(1)
    for b in buttons:
        lay.addWidget(b, 1 if equal else 0)
    if not equal and align == "leading":
        lay.addStretch(1)
    return box


def set_enabled(widget: QWidget, enabled: bool, reason: str = "") -> None:
    """Enable or disable ``widget``; a disabled one says why in its tooltip (the normal tooltip
    comes back when it is enabled again)."""
    normal = widget.property("tooltip_enabled")
    if normal is None:
        normal = widget.toolTip()
        widget.setProperty("tooltip_enabled", normal)
    widget.setEnabled(enabled)
    widget.setProperty("disabled_reason", "" if enabled else reason)
    if enabled or not reason:
        widget.setToolTip(str(normal))
    else:
        widget.setToolTip(f"{normal}\n{reason}" if normal and reason not in str(normal) else reason)


def set_tooltip(widget: QWidget, tip: str) -> None:
    """Set the normal tooltip of a widget managed by :func:`set_enabled`."""
    widget.setProperty("tooltip_enabled", tip)
    reason = widget.property("disabled_reason")
    if widget.isEnabled() or not reason:
        widget.setToolTip(tip)
    else:
        widget.setToolTip(f"{tip}\n{reason}" if tip else str(reason))


# --------------------------------------------------------------------------- forms
class Form(QFormLayout):
    """A QFormLayout whose labels move above their fields when it is narrower than its widest
    label + field + SP3 (decided from the content in :meth:`setGeometry`, never from a screen
    size); its minimum width is the stacked one, so a narrow dock never has to scroll."""

    def _widths(self) -> tuple[int, int]:
        lab = fld = 0
        for r in range(self.rowCount()):
            li = self.itemAt(r, QFormLayout.LabelRole)
            fi = self.itemAt(r, QFormLayout.FieldRole) or self.itemAt(r, QFormLayout.SpanningRole)
            if li is not None and li.widget() is not None and not li.widget().isHidden():
                lab = max(lab, li.widget().sizeHint().width())
            if fi is not None and not (fi.widget() is not None and fi.widget().isHidden()):
                fld = max(fld, fi.minimumSize().width())
        return lab, fld

    def needed_beside(self) -> int:
        lab, fld = self._widths()
        m = self.contentsMargins()
        return lab + fld + SP3 + m.left() + m.right()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt API
        lab, fld = self._widths()
        m = self.contentsMargins()
        base = super().minimumSize()
        return QSize(max(lab, fld) + m.left() + m.right(), base.height())

    def setGeometry(self, rect) -> None:  # noqa: N802 - Qt API
        want = QFormLayout.WrapAllRows if rect.width() < self.needed_beside() else QFormLayout.DontWrapRows
        if self.rowWrapPolicy() != want:
            self.setRowWrapPolicy(want)
        super().setGeometry(rect)


def form(parent: Optional[QWidget] = None) -> Form:
    """A :class:`Form` as the platform lays forms out (the native style picks the label
    alignment: right on macOS, left elsewhere), SP2 row spacing, fields that grow except on
    macOS, labels above the fields when it gets narrower than it needs."""
    f = Form(parent) if parent is not None else Form()
    f.setContentsMargins(0, 0, 0, 0)
    f.setVerticalSpacing(SP2)
    f.setHorizontalSpacing(SP3)
    f.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint if is_mac() else QFormLayout.ExpandingFieldsGrow)
    return f


def label_text(text: str) -> str:
    """A form label: sentence case from the catalogue, with the platform's colon (Windows and
    Linux write one, macOS does not)."""
    t = text.strip().rstrip(":").rstrip()
    return t if is_mac() or not t else t + ":"


def form_row(f: QFormLayout, label: Union[str, QLabel, None], field: QWidget, *, unit_kind: Optional[str] = None,
             hint: str = "", disabled_reason: str = "") -> QWidget:
    """Add a row to a :func:`form`.  The unit goes in the field's suffix, never in the label too:
    ``unit_kind`` asks for a :class:`mlaser.ui.units_qt.QuantitySpinBox` of that kind.  ``hint``
    is a caption under the field; ``disabled_reason`` disables the field and says why (tooltip
    and caption).  Returns the label widget (or None)."""
    if unit_kind is not None:
        from .units_qt import QuantitySpinBox
        if not isinstance(field, QuantitySpinBox):
            raise TypeError("form_row(unit_kind=...) needs a units_qt.QuantitySpinBox (units in the suffix)")
        if field.kind() != unit_kind:
            raise ValueError(f"field kind {field.kind()!r} is not {unit_kind!r}")
    lab: Optional[QLabel]
    if isinstance(label, str):
        lab = QLabel(label_text(label))
    else:
        lab = label
    if lab is not None:
        lab.setBuddy(field)
    target: QWidget = field
    if hint or disabled_reason:
        box = QWidget()
        v = vbox(box, sp=SP1 // 2)
        v.addWidget(field)
        cap = caption(disabled_reason or hint)
        cap.setObjectName("form_hint")
        v.addWidget(cap)
        target = box
    if disabled_reason:
        set_enabled(field, False, disabled_reason)
    if lab is not None:
        f.addRow(lab, target)
    else:
        f.addRow(target)
    return lab if lab is not None else target


# --------------------------------------------------------------------------- sections, dialogs
def section(title: str, *, collapsible: bool = False, priority: int = 0) -> tuple[QWidget, QVBoxLayout]:
    """A titled section: a ``label_strong`` heading and the body inset by SP2, no group-box frame.
    Returns (section widget, body layout).  ``collapsible``: the responsive CollapsibleSection."""
    body = QWidget()
    lay = vbox(body)
    lay.setContentsMargins(SP2, 0, 0, 0)
    if collapsible:
        from .responsive import CollapsibleSection
        return CollapsibleSection(title, body, collapsible=True, priority=priority), lay
    sec = QWidget()
    v = vbox(sec, sp=SP1)
    head = heading(title)
    head.setObjectName("section_header")
    v.addWidget(head)
    v.addWidget(body)
    sec.setProperty("section_title", title)
    return sec, lay


class Dialog(QDialog):
    """The one dialog frame: the window title, an optional ``title`` heading and explanation
    line, the body (:attr:`body`, a QVBoxLayout; :meth:`form` adds a :func:`form`), and a
    :class:`QDialogButtonBox` (:attr:`buttons`) whose order follows the platform.  SP3 margins,
    a size from the content (no minimum-size constants), a window-modal sheet on macOS when
    opened with :meth:`open`."""

    def __init__(self, parent: Optional[QWidget], title: str, *, explanation: str = "",
                 buttons=QDialogButtonBox.Ok | QDialogButtonBox.Cancel, heading_text: str = "",
                 tr=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        outer = vbox(self, "dialog", sp=SP3)
        self.heading: Optional[QLabel] = None
        if heading_text:
            self.heading = heading(heading_text, "title")
            self.heading.setObjectName("dialog_heading")
            outer.addWidget(self.heading)
        self.explanation = QLabel(explanation)
        self.explanation.setObjectName("dialog_explanation")
        self.explanation.setWordWrap(True)
        self.explanation.setVisible(bool(explanation))
        outer.addWidget(self.explanation)
        holder = QWidget()
        holder.setObjectName("dialog_body")
        self.body = vbox(holder)
        outer.addWidget(holder, 1)
        self.buttons = QDialogButtonBox(buttons)
        self.buttons.setObjectName("dialog_buttons")
        if tr is not None:
            for std, key in ((QDialogButtonBox.Ok, "dialog.ok"), (QDialogButtonBox.Cancel, "dialog.cancel")):
                b = self.buttons.button(std)
                if b is not None and hasattr(tr, "has") and tr.has(key):
                    b.setText(tr.tr(key))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)
        self._form: Optional[QFormLayout] = None
        if is_mac() and parent is not None:
            self.setWindowModality(Qt.WindowModal)

    def form(self) -> QFormLayout:
        """The body's form (made on first use)."""
        if self._form is None:
            host = QWidget()
            host.setObjectName("dialog_form")
            self._form = form(host)
            self.body.addWidget(host)
        return self._form

    def set_explanation(self, text: str) -> None:
        self.explanation.setText(text)
        self.explanation.setVisible(bool(text))

    def ok_button(self) -> Optional[QAbstractButton]:
        return self.buttons.button(QDialogButtonBox.Ok)


def dialog(parent: Optional[QWidget], title: str, *, explanation: str = "",
           buttons=QDialogButtonBox.Ok | QDialogButtonBox.Cancel, heading_text: str = "", tr=None) -> Dialog:
    return Dialog(parent, title, explanation=explanation, buttons=buttons, heading_text=heading_text, tr=tr)


# --------------------------------------------------------------------------- tables
@dataclass
class Col:
    """A table column: its header, whether it is the key column (stretches, elides, never
    hides), and its priority (higher stays longer when the table is narrow)."""
    title: str
    stretch: bool = False
    priority: int = 0


def table(columns: Sequence[Union[Col, str]], parent: Optional[QWidget] = None) -> QTableWidget:
    """A read-only table that never scrolls sideways for its key column (rule R2): the key
    column stretches and elides, the others hide by priority when the view is narrow
    (:class:`mlaser.ui.responsive.ColumnFitter`), rows are selected whole."""
    from PySide6.QtWidgets import QAbstractItemView
    from .responsive import ColumnFitter
    cols = [c if isinstance(c, Col) else Col(c) for c in columns]
    if not any(c.stretch for c in cols) and cols:
        cols[0] = Col(cols[0].title, True, cols[0].priority)
    t = QTableWidget(0, len(cols), parent)
    t.setHorizontalHeaderLabels([c.title for c in cols])
    t.verticalHeader().hide()
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setTextElideMode(Qt.ElideRight)
    t.setWordWrap(False)
    hh = t.horizontalHeader()
    for i, c in enumerate(cols):
        hh.setSectionResizeMode(i, QHeaderView.Stretch if c.stretch else QHeaderView.ResizeToContents)
    key = next(i for i, c in enumerate(cols) if c.stretch)
    t.column_fitter = ColumnFitter(t, {i: (None if i == key else c.priority) for i, c in enumerate(cols)},
                                   stretch=key)
    return t


def walk_disabled_without_reason(root: QWidget, types: Iterable[type] = (QAbstractButton,)) -> list[QWidget]:
    """The visible, disabled controls under ``root`` with no tooltip (tests: a disabled control
    says why)."""
    out = []
    for t in types:
        for w in root.findChildren(t):
            if w.isVisibleTo(root) and not w.isEnabled() and not w.toolTip().strip():
                if w.parentWidget() is not None and not w.parentWidget().isEnabled():
                    continue                     # the whole group is off: its parent says why
                out.append(w)
    return out


__all__ = ["SP0", "SP1", "SP2", "SP3", "SP4", "SP5", "ICON_S", "ICON_M", "ICON_L", "TARGET", "TARGET_TOUCH",
           "TARGET_RUN", "STOP_EXTRA", "MARGINS", "TYPE_SCALE", "ROLES", "margins", "spacing", "vbox", "hbox",
           "font", "set_font", "caption", "heading", "menu_text", "shortcut_text", "color", "css",
           "state_colors", "styled", "chip", "button", "primary_button", "danger_button", "tool_button",
           "RUN_KINDS", "run_text", "run_button", "button_row", "set_enabled", "set_tooltip", "form",
           "Form", "label_text", "form_row", "section", "Dialog", "dialog", "Col", "table",
           "walk_disabled_without_reason", "current_theme", "is_mac"]
