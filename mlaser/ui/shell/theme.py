"""Themes: the system appearance (default), light, dark and high contrast
(docs/review/ux.md P2-2, docs/user/shell.md §5, docs/dev/NATIVE.md).

Native first: standard widgets are drawn by the platform's own style (``macos`` on macOS,
``windows11`` / ``windowsvista`` on Windows, the desktop's style, else Fusion, on Linux: Qt's
default for the platform, recorded the first time a theme is applied) with the platform's
palette.  A theme decides

* the colour scheme: ``system`` follows the operating system's light/dark appearance, live
  (``QStyleHints.colorScheme``); ``light`` / ``dark`` ask the platform for that scheme
  (``QStyleHints.setColorScheme``: native controls and title bars in dark on macOS, Windows and
  the Linux portal) and fall back to the design tokens' ``QPalette`` where the platform cannot
  (plain X11 without a portal, offscreen); ``high_contrast`` is the "safety glasses" palette on
  Fusion (a native style would not honour it);
* a small style sheet for the shell's own widgets only (:func:`style_sheet`);
* the canvas colours (:data:`mlaser.ui.canvas.CANVAS_COLORS`) for the effective scheme, with the
  layer colours adjusted for contrast against the canvas background
  (:func:`.contrast.adjust_palette`: at least 3:1, WCAG 1.4.11, and still distinguishable).

The choice is persisted in the ConfigService (``shell.theme``).  The machine and laser state
colours (:data:`STATE_TOKENS`: neutral for normal states, amber ARMED, red ALARM, dark red FAULT)
have a light, a dark and a high-contrast set; the state is always written out with an icon too
(docs/SAFETY.md).
"""
from __future__ import annotations

import re
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .contrast import adjust_palette, contrast

SYSTEM, LIGHT, DARK, HIGH_CONTRAST = "system", "light", "dark", "high_contrast"
THEME_IDS = (SYSTEM, LIGHT, DARK, HIGH_CONTRAST)
DEFAULT_THEME = SYSTEM

THEMES: dict[str, dict[str, str]] = {
    LIGHT: {
        # semantic roles (mlaser.ui.style.color): text colours >= 4.5:1 on the window and on
        # their fill; STOP's fill is the same in every theme (docs/SAFETY.md)
        "danger": "#c62828", "on_danger": "#ffffff", "danger_fill": "#c62828",
        "warning": "#9a4a06", "warning_fill": "#fff4e5", "on_warning": "#7a3a04",
        "info": "#1b5fc1", "info_fill": "#e8f0fc", "on_info": "#123f80",
        "success": "#2f7d32", "success_fill": "#e8f5e9", "on_success": "#1e5420",
        "window": "#f3f4f6", "base": "#ffffff", "alt_base": "#f6f7f9", "text": "#1d2127",
        "muted": "#59616d", "button": "#e8eaee", "button_text": "#1d2127", "border": "#c4c9d1",
        "accent": "#1b5fc1", "accent_text": "#ffffff", "tooltip": "#ffffe8", "tooltip_text": "#1d2127",
        "disabled": "#8b929c", "rail": "#e4e7ec", "rail_checked": "#ffffff", "topbar": "#ffffff",
        "focus": "#1b5fc1", "link": "#1b5fc1", "placeholder": "#6b7280",
        # the boundary of a field / check box drawn by Fusion (>= 3:1 on base and window,
        # WCAG 1.4.11; :class:`FusionBoundaries`)
        "control_border": "#7d8590",
        # canvas
        "c_background": "#fbfbfa", "c_table": "#8b9099", "c_axis_x": "#c62828", "c_axis_y": "#2e7d32",
        "c_cut": "#3a3f46", "c_gap": "#c2410c", "c_rapid": "#3b5bdb", "c_track": "#d62828",
        "c_unclosed": "#d00000", "c_selected": "#101418", "c_start": "#101418", "c_index": "#8a5300",
        "c_bbox": "#7b818b", "c_arrow": "#101418", "c_head": "#d62828", "c_marker": "#0b7285",
        "c_sim": "#101418",
        # the planned toolpath: leads and the kerf-compensated path in their own colours
        # the toolpath preview (preview_scrubber, Orders' sheet preview)
        "c_preview_cut": "#b36b00", "c_preview_rapid": "#1f6fd1", "c_preview_head": "#d62828",
        "c_preview_head_off": "#7b818b", "c_preview": "#8a6d00", "c_preview_alert": "#c62828",
        "c_lead_in": "#c2187a", "c_lead_out": "#6d28d9", "c_kerf": "#a86b00", "c_pierce": "#c2187a",
    },
    DARK: {
        "danger": "#ef5350", "on_danger": "#ffffff", "danger_fill": "#c62828",
        "warning": "#f59e0b", "warning_fill": "#3a2a10", "on_warning": "#ffd089",
        "info": "#7fb0ff", "info_fill": "#16243a", "on_info": "#cfe0ff",
        "success": "#66bb6a", "success_fill": "#163a1a", "on_success": "#c8e6c9",
        "window": "#1f2226", "base": "#16181b", "alt_base": "#1c1f23", "text": "#e6e8eb",
        "muted": "#a3aab4", "button": "#2b2f35", "button_text": "#e6e8eb", "border": "#3d434b",
        "accent": "#4d8ff0", "accent_text": "#0b0d10", "tooltip": "#2b2f35", "tooltip_text": "#e6e8eb",
        "disabled": "#6f7680", "rail": "#181a1d", "rail_checked": "#2f343b", "topbar": "#181a1d",
        "focus": "#7fb0ff", "link": "#7fb0ff", "placeholder": "#8b929c", "control_border": "#747c87",
        "c_background": "#000000", "c_table": "#606060", "c_axis_x": "#c04040", "c_axis_y": "#40c040",
        "c_cut": "#d0d0d0", "c_gap": "#ff8000", "c_rapid": "#8080ff", "c_track": "#ff6060",
        "c_unclosed": "#ff0000", "c_selected": "#ffffff", "c_start": "#ffffff", "c_index": "#ffd700",
        "c_bbox": "#707070", "c_arrow": "#ffffff", "c_head": "#ff4040", "c_marker": "#00ffff",
        "c_sim": "#ffffff",
        "c_preview_cut": "#ffb000", "c_preview_rapid": "#4da3ff", "c_preview_head": "#ff3030",
        "c_preview_head_off": "#c0c0c0", "c_preview": "#e6e600", "c_preview_alert": "#e53935",
        "c_lead_in": "#ff4fa0", "c_lead_out": "#b28cff", "c_kerf": "#ffc53d", "c_pierce": "#ff4fa0",
    },
    HIGH_CONTRAST: {      # "safety glasses": black, white, yellow; no greys for content
        "danger": "#ff5252", "on_danger": "#ffffff", "danger_fill": "#c62828",
        "warning": "#ffd400", "warning_fill": "#000000", "on_warning": "#ffd400",
        "info": "#00e5ff", "info_fill": "#000000", "on_info": "#00e5ff",
        "success": "#69f0ae", "success_fill": "#000000", "on_success": "#69f0ae",
        "window": "#000000", "base": "#000000", "alt_base": "#0a0a0a", "text": "#ffffff",
        "muted": "#ffffff", "button": "#000000", "button_text": "#ffffff", "border": "#ffffff",
        "accent": "#ffd400", "accent_text": "#000000", "tooltip": "#000000", "tooltip_text": "#ffffff",
        "disabled": "#b0b0b0", "rail": "#000000", "rail_checked": "#ffd400", "topbar": "#000000",
        "focus": "#00e5ff", "link": "#00e5ff", "placeholder": "#d0d0d0", "control_border": "#ffffff",
        "c_background": "#000000", "c_table": "#bdbdbd", "c_axis_x": "#ff5252", "c_axis_y": "#69f0ae",
        "c_cut": "#ffffff", "c_gap": "#ff9100", "c_rapid": "#82b1ff", "c_track": "#ff5252",
        "c_unclosed": "#ff1744", "c_selected": "#ffffff", "c_start": "#ffffff", "c_index": "#ffff00",
        "c_bbox": "#bdbdbd", "c_arrow": "#ffffff", "c_head": "#ff1744", "c_marker": "#00e5ff",
        "c_sim": "#ffffff",
        "c_preview_cut": "#ffd740", "c_preview_rapid": "#82b1ff", "c_preview_head": "#ff1744",
        "c_preview_head_off": "#ffffff", "c_preview": "#ffff00", "c_preview_alert": "#ff5252",
        "c_lead_in": "#ff40ff", "c_lead_out": "#c9a0ff", "c_kerf": "#ffd740", "c_pierce": "#ff40ff",
    },
}
#: the machine and laser state colours, (fill, text) per state and theme (ISA-101 style, UX audit
#: W1): normal states (offline, not homed, ready, running, hold, disarmed, dry run, simulator) are
#: neutral greys and slates; ARMED is the amber hazard colour; ALARM / E-STOP red; FAULT dark red.
#: Red is never a normal state, and the only red control is STOP (``danger_fill``).  Every text
#: colour reads at >= 4.5:1 on its fill (tests/test_state_display.py); the state is always written
#: out with an icon too, never colour alone.
_NEUTRAL = {LIGHT: ("#e3e7ed", "#1d2127"), DARK: ("#353a42", "#e6e8eb"), HIGH_CONTRAST: ("#000000", "#ffffff")}
STATE_TOKENS: dict[str, dict[str, tuple[str, str]]] = {
    LIGHT: {
        "READY": _NEUTRAL[LIGHT], "DISARMED": _NEUTRAL[LIGHT], "DRY-RUN": _NEUTRAL[LIGHT],
        "SIMULATOR": _NEUTRAL[LIGHT], "HOLD": _NEUTRAL[LIGHT], "NOT-HOMED": _NEUTRAL[LIGHT],
        "OFFLINE": ("#c9ced6", "#1d2127"), "RUNNING": ("#3d4450", "#ffffff"),
        "ARMED": ("#f5b400", "#1d2127"), "ALARM": ("#c62828", "#ffffff"), "E-STOP": ("#c62828", "#ffffff"),
        "FAULT": ("#7f0000", "#ffffff"),
    },
    DARK: {
        "READY": _NEUTRAL[DARK], "DISARMED": _NEUTRAL[DARK], "DRY-RUN": _NEUTRAL[DARK],
        "SIMULATOR": _NEUTRAL[DARK], "HOLD": _NEUTRAL[DARK], "NOT-HOMED": _NEUTRAL[DARK],
        "OFFLINE": ("#2a2e34", "#c4c9d1"), "RUNNING": ("#4b5568", "#ffffff"),
        "ARMED": ("#ffb300", "#000000"), "ALARM": ("#c62828", "#ffffff"), "E-STOP": ("#c62828", "#ffffff"),
        "FAULT": ("#8e0000", "#ffffff"),
    },
    HIGH_CONTRAST: {
        "READY": _NEUTRAL[HIGH_CONTRAST], "DISARMED": _NEUTRAL[HIGH_CONTRAST], "DRY-RUN": _NEUTRAL[HIGH_CONTRAST],
        "SIMULATOR": _NEUTRAL[HIGH_CONTRAST], "HOLD": _NEUTRAL[HIGH_CONTRAST], "NOT-HOMED": _NEUTRAL[HIGH_CONTRAST],
        "OFFLINE": ("#000000", "#d0d0d0"), "RUNNING": ("#ffffff", "#000000"),
        "ARMED": ("#ffd400", "#000000"), "ALARM": ("#d50000", "#ffffff"), "E-STOP": ("#d50000", "#ffffff"),
        "FAULT": ("#7f0000", "#ffffff"),
    },
}
#: the states that are not normal operation (they get a colour; every other state is neutral)
ABNORMAL_STATES = ("ARMED", "ALARM", "E-STOP", "FAULT")


def state_tokens(theme: str, kind: str) -> tuple[str, str]:
    """(fill, text) of the machine / laser state ``kind`` (``READY``, ``ARMED``...) in ``theme``."""
    t = STATE_TOKENS.get(effective(theme), STATE_TOKENS[LIGHT])
    return t.get(kind, t["OFFLINE"])


#: minimum contrast of the layer colours against the canvas (WCAG 1.4.11 asks 3:1; the high
#: contrast theme asks more)
LAYER_MIN_CONTRAST = {LIGHT: 3.0, DARK: 3.0, HIGH_CONTRAST: 4.5}


def tokens(theme: str) -> dict[str, str]:
    """The design tokens of a theme (``system``: of the scheme the platform shows now)."""
    if theme == SYSTEM:
        theme = effective(SYSTEM)
    return THEMES.get(theme, THEMES[LIGHT])


def system_is_dark(app: Optional[QApplication] = None) -> bool:
    """The platform's appearance: its colour scheme, else the lightness of its palette."""
    app = app or QApplication.instance()
    if app is None:
        return False
    scheme = app.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    return app.palette().color(QPalette.Window).lightness() < 128


def effective(theme: str, app: Optional[QApplication] = None) -> str:
    """``light``, ``dark`` or ``high_contrast``: what the canvas and the domain widgets use.
    ``system`` is ``high_contrast`` while the operating system asks for more contrast
    (:func:`os_high_contrast`); an explicit choice always wins."""
    if theme in (LIGHT, DARK, HIGH_CONTRAST):
        return theme
    if os_high_contrast(app):
        return HIGH_CONTRAST
    return DARK if system_is_dark(app) else LIGHT


# ------------------------------------------------------------------ OS high contrast
#: the operating system's high-contrast / increase-contrast setting (cached: read at start and
#: when Qt reports a change, :func:`os_high_contrast` with ``refresh``)
_os_hc: Optional[bool] = None


def os_high_contrast(app: Optional[QApplication] = None, refresh: bool = False) -> bool:
    """True when the operating system asks for high contrast: Windows "Contrast themes",
    macOS "Increase contrast", a GNOME / portal high-contrast setting (UI evaluation F7).
    ``MLASER_HIGH_CONTRAST=1`` / ``0`` overrides the detection."""
    global _os_hc
    if _os_hc is None or refresh:
        try:
            _os_hc = detect_high_contrast(app)
        except Exception:  # noqa: BLE001 - detection is a convenience, never an error
            _os_hc = False
    return _os_hc


def set_os_high_contrast(value: Optional[bool]) -> None:
    """Set the cached OS setting (tests; None: detect again on the next query)."""
    global _os_hc
    _os_hc = value


def detect_high_contrast(app: Optional[QApplication] = None) -> bool:
    """Read the OS setting now (see :func:`os_high_contrast`).

    1. Qt 6.10+: ``QStyleHints.accessibility().contrastPreference()`` (Windows high contrast,
       macOS Increase Contrast, the XDG desktop portal's ``contrast`` on Linux);
    2. Windows: ``SystemParametersInfoW(SPI_GETHIGHCONTRAST)``;
    3. macOS: ``com.apple.universalaccess increaseContrast``;
    4. Linux: ``GTK_THEME`` naming a high-contrast theme, GNOME's
       ``org.gnome.desktop.a11y.interface high-contrast``.
    """
    import os
    import sys
    env = os.environ.get("MLASER_HIGH_CONTRAST", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    app = app or QApplication.instance()
    if app is not None:
        hints = app.styleHints()
        acc_fn = getattr(hints, "accessibility", None)
        if acc_fn is not None:
            acc = acc_fn()
            if acc is not None and acc.contrastPreference() == Qt.ContrastPreference.HighContrast:
                return True
    if sys.platform.startswith("win"):
        return _windows_high_contrast()
    if sys.platform == "darwin":
        return _run_bool(["defaults", "read", "com.apple.universalaccess", "increaseContrast"]) is True
    if os.environ.get("QT_QPA_PLATFORM", "") in ("offscreen", "minimal"):
        return False                         # no desktop session (tests, CI)
    if "highcontrast" in os.environ.get("GTK_THEME", "").lower().replace("-", "").replace("_", ""):
        return True
    return _run_bool(["gsettings", "get", "org.gnome.desktop.a11y.interface", "high-contrast"]) is True


def _windows_high_contrast() -> bool:
    import ctypes
    from ctypes import wintypes

    class HIGHCONTRASTW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD), ("lpszDefaultScheme", wintypes.LPWSTR)]
    hc = HIGHCONTRASTW()
    hc.cbSize = ctypes.sizeof(HIGHCONTRASTW)
    SPI_GETHIGHCONTRAST, HCF_HIGHCONTRASTON = 0x0042, 0x0001
    ok = ctypes.windll.user32.SystemParametersInfoW(SPI_GETHIGHCONTRAST, hc.cbSize, ctypes.byref(hc), 0)  # type: ignore[attr-defined]
    return bool(ok) and bool(hc.dwFlags & HCF_HIGHCONTRASTON)


def _run_bool(cmd: list[str]) -> Optional[bool]:
    """A setting read with a command line tool: "1" / "true" -> True; None when unavailable."""
    import shutil
    import subprocess
    if shutil.which(cmd[0]) is None:
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=1.0, check=False).stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        return None
    if out in ("1", "true"):
        return True
    if out in ("0", "false"):
        return False
    return None


def palette(theme: str) -> QPalette:
    t = tokens(theme)
    p = QPalette()
    roles = {
        QPalette.Window: t["window"], QPalette.WindowText: t["text"], QPalette.Base: t["base"],
        QPalette.AlternateBase: t["alt_base"], QPalette.Text: t["text"], QPalette.Button: t["button"],
        QPalette.ButtonText: t["button_text"], QPalette.Highlight: t["accent"],
        QPalette.HighlightedText: t["accent_text"], QPalette.ToolTipBase: t["tooltip"],
        QPalette.ToolTipText: t["tooltip_text"], QPalette.Link: t["link"],
        QPalette.PlaceholderText: t["placeholder"], QPalette.BrightText: "#ffffff",
        QPalette.Light: t["rail_checked"], QPalette.Midlight: t["button"], QPalette.Mid: t["border"],
        QPalette.Dark: t["border"], QPalette.Shadow: "#000000",
    }
    for role, col in roles.items():
        p.setColor(QPalette.All, role, QColor(col))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(t["disabled"]))
    p.setColor(QPalette.Disabled, QPalette.Highlight, QColor(t["border"]))
    return p


def style_sheet(theme: str, base_pt: Optional[float] = None) -> str:
    """The style sheet of the shell's own widgets (set on the main window).  Standard widgets
    (buttons, inputs, tabs, lists, docks, scroll bars, tool bars) get none: the platform style
    draws them.  Only the command palette's overlay frame and the empty-state title, whose size
    is a ratio of the platform font (never an absolute ``pt``: macOS points are ~25 % smaller)."""
    base = base_pt or _base_point_size()
    hc = theme == HIGH_CONTRAST
    bw = "2px" if hc else "1px"
    return (f"QFrame#shell_palette {{ background: palette(base); border: {bw} solid palette(mid); "
            f"border-radius: 8px; }}\n"
            f"QLabel#shell_empty_title {{ font-size: {base * TYPE_SCALE['display']:.1f}pt; font-weight: bold; }}\n")


#: the type scale (docs: ui_audit DESIGN.md §1): sizes as ratios of the platform font
TYPE_SCALE = {"caption": 0.85, "body": 1.0, "title": 1.25, "display": 1.6}


def _base_point_size() -> float:
    from PySide6.QtGui import QGuiApplication
    f = QGuiApplication.font() if QGuiApplication.instance() is not None else None
    pt = f.pointSizeF() if f is not None else -1.0
    return pt if pt > 0 else 10.0


def scaled_font(role: str, base=None):
    """A QFont of the type-scale ``role`` (caption / body / title / display) relative to
    ``base`` (default: the application font)."""
    from PySide6.QtGui import QFont, QGuiApplication
    f = QFont(base if base is not None else QGuiApplication.font())
    if f.pointSizeF() > 0:
        f.setPointSizeF(f.pointSizeF() * TYPE_SCALE.get(role, 1.0))
    elif f.pixelSize() > 0:
        f.setPixelSize(max(1, round(f.pixelSize() * TYPE_SCALE.get(role, 1.0))))
    if role in ("title", "display"):
        f.setBold(True)
    return f


def canvas_colors(theme: str) -> dict[str, str]:
    t = tokens(effective(theme))
    return {k[2:]: v for k, v in t.items() if k.startswith("c_")}


def layer_colors(theme: str, base: Optional[list[str]] = None) -> list[str]:
    """The layer colours as drawn in ``theme`` (contrast-adjusted, distinguishable)."""
    from .. import canvas
    cols = list(base or canvas.LAYER_COLORS)
    theme = effective(theme)
    bg = canvas_colors(theme)["background"]
    return adjust_palette(cols, bg, LAYER_MIN_CONTRAST.get(theme, 3.0))


def apply_canvas_theme(theme: str, view=None) -> None:
    from .. import canvas
    canvas.CANVAS_COLORS.update(canvas_colors(theme))
    canvas.set_display_layer_colors(layer_colors(theme))
    if view is not None:
        view.apply_theme()


def native_style_name(app: QApplication) -> str:
    """The platform's own style (Qt's default, before any theme changed it)."""
    name = app.property("mlaser_native_style")
    if not name:
        name = app.style().name()
        app.setProperty("mlaser_native_style", name)
    return str(name)


#: the least contrast of a field's boundary against its fill and the window (WCAG 1.4.11)
BOUNDARY_MIN_CONTRAST = 3.0
_boundary_cache: dict[tuple, str] = {}


def boundary_color(pal: QPalette, theme: Optional[str] = None) -> QColor:
    """The colour of a field's / check box's boundary on ``pal``: the theme's
    ``control_border`` token while it reaches :data:`BOUNDARY_MIN_CONTRAST` against the
    palette's base and window colours, else the text colour blended towards the base just as
    far as still reaches it (a platform palette the tokens do not know)."""
    if theme is None:
        app = QApplication.instance()
        theme = str(app.property("mlaser_theme") or SYSTEM) if app is not None else SYSTEM
    base, win, text = (pal.color(QPalette.Base).name(), pal.color(QPalette.Window).name(),
                       pal.color(QPalette.Text).name())
    key = (theme, base, win, text)
    hit = _boundary_cache.get(key)
    if hit is None:
        cands = [tokens(theme).get("control_border", "#7d8590")]
        tb, bb = QColor(text), QColor(base)
        for i in range(10, -1, -1):            # 100 % .. 0 % of the base mixed in
            f = i / 10.0
            cands.append(QColor.fromRgbF(tb.redF() * (1 - f) + bb.redF() * f, tb.greenF() * (1 - f) + bb.greenF() * f,
                                         tb.blueF() * (1 - f) + bb.blueF() * f).name())
        hit = next((c for c in cands if contrast(c, base) >= BOUNDARY_MIN_CONTRAST
                    and contrast(c, win) >= BOUNDARY_MIN_CONTRAST), text)
        _boundary_cache[key] = hit
    return QColor(hit)


def _fusion_boundaries_class():
    """:class:`FusionBoundaries`, built on first use (QtWidgets' QProxyStyle)."""
    global FusionBoundaries
    if FusionBoundaries is not None:
        return FusionBoundaries
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QPainter, QPen
    from PySide6.QtWidgets import QProxyStyle, QStyle

    framed = {QStyle.PE_FrameLineEdit, QStyle.PE_IndicatorCheckBox, QStyle.PE_IndicatorRadioButton}

    class _FusionBoundaries(QProxyStyle):
        """Fusion with visible field boundaries (UX audit W16).  Fusion outlines line edits,
        spin boxes and check / radio indicators in the window colour darkened by 40 %: about
        1.1:1 on the dark theme, 2:1 on the light one, black on black in high contrast.  This
        proxy draws Fusion as it is and then the :func:`boundary_color` outline on top (a
        focused field keeps Fusion's focus outline).  Only installed while the style is Fusion:
        the native macOS / Windows styles draw their own boundaries."""

        def name(self) -> str:  # noqa: D401 - Qt API (Python callers: the style's name)
            return self.baseStyle().name()

        @staticmethod
        def _outline(p, opt, rect, kind: str) -> None:
            st = opt.state
            if kind != "check" and st & QStyle.State_HasFocus:
                return                           # Fusion's focus outline
            col = boundary_color(opt.palette)
            if not st & QStyle.State_Enabled:
                col.setAlphaF(0.5)
            p.save()
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(col, 1.0))
            p.setBrush(Qt.NoBrush)
            r = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
            if kind == "radio":
                p.drawEllipse(r)
            elif kind == "check":
                p.drawRect(r)
            else:
                p.drawRoundedRect(r, 2.0, 2.0)
            p.restore()

        def drawPrimitive(self, el, opt, p, w=None) -> None:  # noqa: N802 - Qt API
            super().drawPrimitive(el, opt, p, w)
            if el in framed and p is not None:
                kind = ("check" if el == QStyle.PE_IndicatorCheckBox else
                        "radio" if el == QStyle.PE_IndicatorRadioButton else "field")
                self._outline(p, opt, opt.rect, kind)

        def drawComplexControl(self, cc, opt, p, w=None) -> None:  # noqa: N802 - Qt API
            super().drawComplexControl(cc, opt, p, w)
            if cc == QStyle.CC_SpinBox and p is not None and getattr(opt, "frame", True):
                self._outline(p, opt, opt.rect, "field")

    FusionBoundaries = _FusionBoundaries
    return FusionBoundaries


#: the QProxyStyle class (built on first use, see :func:`_fusion_boundaries_class`)
FusionBoundaries: Any = None


def _set_style(app: QApplication, name: str) -> None:
    if app.style().name().lower() != name.lower():
        app.setStyle(name)
    # Fusion: field boundaries that reach 3:1 (the platforms' own styles draw theirs)
    if app.style().name().lower() == "fusion":
        cls = _fusion_boundaries_class()
        if not isinstance(app.style(), cls):
            app.setStyle(cls("Fusion"))


def _set_scheme(app: QApplication, scheme: Optional["Qt.ColorScheme"]) -> bool:
    """Ask the platform for ``scheme`` (None: follow the system).  True when it shows it."""
    hints = app.styleHints()
    try:
        if scheme is None:
            hints.unsetColorScheme()
            return True
        hints.setColorScheme(scheme)
    except AttributeError:          # Qt < 6.8
        return False
    return hints.colorScheme() == scheme


def apply(theme: str, window=None, app: Optional[QApplication] = None) -> str:
    """Apply ``theme`` to the application (style, colour scheme, palette), the window's style
    sheet and the canvas.  Returns the theme id actually applied."""
    theme = theme if theme in THEME_IDS else DEFAULT_THEME
    app = app or QApplication.instance()
    # "system" follows the OS high-contrast setting too (an explicit choice wins)
    mode = HIGH_CONTRAST if theme == SYSTEM and os_high_contrast(app) else theme
    changed = False
    if app is not None and app.property("mlaser_theme") != mode:
        changed = True
        # application-wide changes only when the theme really changes: setStyle / setPalette
        # post a change event to every widget of every window
        native = native_style_name(app)
        custom: Optional[QPalette] = None
        if mode == HIGH_CONTRAST:
            _set_style(app, "Fusion")
            _set_scheme(app, Qt.ColorScheme.Dark)
            custom = palette(HIGH_CONTRAST)
        else:
            _set_style(app, native)
            want = {LIGHT: Qt.ColorScheme.Light, DARK: Qt.ColorScheme.Dark}.get(theme)
            if not _set_scheme(app, want) and want is not None:
                custom = palette(theme)          # the platform cannot: the tokens' palette
        if custom is not None:
            app.setPalette(custom)
            app.setProperty("mlaser_palette", True)
        elif app.property("mlaser_palette"):
            app.setPalette(QPalette())           # back to the platform's (live) palette
            app.setProperty("mlaser_palette", False)
        app.setProperty("mlaser_theme", mode)
    if window is not None:
        qss = style_sheet(mode)
        if changed and window.styleSheet():
            # widgets under a style sheet keep the palette they were polished with: a palette
            # switch (light <-> dark where the platform cannot switch, offscreen, plain X11)
            # left their text black on the dark window.  Re-polish them with the new palette.
            window.setStyleSheet("")
        if window.styleSheet() != qss:
            window.setStyleSheet(qss)
        apply_canvas_theme(effective(theme, app), getattr(window, "canvas", None))
        if changed:
            schedule_link_refresh(window)
        # icons re-tint from the new palette: repaint everything
        window.update()
    return theme


# ------------------------------------------------------------------ links in rich text
#: an ``<a ...>`` start tag and a ``color:`` declaration inside its style attribute
_A_TAG = re.compile(r"<a\b([^>]*)>", re.IGNORECASE)
_STYLE_ATTR = re.compile(r"""\s*style\s*=\s*(['"])(.*?)\1""", re.IGNORECASE | re.DOTALL)
_COLOR_DECL = re.compile(r"(?:^|;)\s*color\s*:[^;]*", re.IGNORECASE)


def recolor_links(html: str, color: str) -> str:
    """``html`` with every link drawn in ``color``. A QLabel resolves its links' colour once,
    from the palette it had then: after a live light -> dark switch they stayed #0000ff on the
    dark window (1.86:1, UI evaluation F7). An explicit colour from the current palette's
    ``Link`` role, re-applied on every theme change (:func:`refresh_links`), follows the theme."""
    def tag(m: "re.Match[str]") -> str:
        attrs = m.group(1)
        sm = _STYLE_ATTR.search(attrs)
        if sm is None:
            return f"<a{attrs} style='color:{color}'>"
        decls = _COLOR_DECL.sub("", sm.group(2)).strip("; ")
        new = f"color:{color}" + (f"; {decls}" if decls else "")
        return f"<a{attrs[:sm.start()]} style='{new}'{attrs[sm.end():]}>"
    return _A_TAG.sub(tag, html)


def link_color(widget=None) -> str:
    """The link colour of ``widget``'s palette (the application's without one)."""
    pal = widget.palette() if widget is not None else QApplication.palette()
    return pal.color(QPalette.Link).name()


def link_html(text: str, href: str = "#", widget=None) -> str:
    """``<a href=...>text</a>`` in the current palette's link colour (see :func:`recolor_links`)."""
    import html as _html
    return recolor_links(f"<a href='{_html.escape(href, quote=True)}'>{text}</a>", link_color(widget))


def refresh_links(root) -> int:
    """Re-colour the links of every rich-text QLabel under ``root`` (and ``root`` itself) from
    its current palette. Returns how many labels changed."""
    from PySide6.QtWidgets import QLabel
    n = 0
    labels = root.findChildren(QLabel)
    if isinstance(root, QLabel):
        labels.append(root)
    for lb in labels:
        try:
            text = lb.text()
            if "<a" not in text and "<A" not in text:
                continue
            new = recolor_links(text, link_color(lb))
            if new != text:
                lb.setText(new)
                n += 1
        except RuntimeError:            # deleted meanwhile
            continue
    return n


def schedule_link_refresh(window) -> None:
    """:func:`refresh_links` now and once more after the event loop delivered the palette
    change (a platform colour-scheme switch reaches the widgets' palettes asynchronously)."""
    refresh_links(window)
    from PySide6.QtCore import QTimer
    import weakref
    ref = weakref.ref(window)

    def later() -> None:
        w = ref()
        if w is not None:
            try:
                refresh_links(w)
            except RuntimeError:
                pass
    QTimer.singleShot(0, later)


def contrast_report(theme: str) -> dict[str, float]:
    """Contrast ratios that matter (tests, docs/user/shell.md): text on the window and base,
    accent text, and every layer colour against the canvas."""
    theme = effective(theme)
    t = tokens(theme)
    out = {"text/window": contrast(t["text"], t["window"]), "text/base": contrast(t["text"], t["base"]),
           "accent_text/accent": contrast(t["accent_text"], t["accent"]),
           "muted/window": contrast(t["muted"], t["window"]),
           "disabled/window": contrast(t["disabled"], t["window"])}
    bg = canvas_colors(theme)["background"]
    for i, c in enumerate(layer_colors(theme)):
        out[f"layer{i}/canvas"] = contrast(c, bg)
    for role in ("unclosed", "selected", "start", "index", "cut", "head", "lead_in", "lead_out", "kerf"):
        out[f"{role}/canvas"] = contrast(canvas_colors(theme)[role], bg)
    return out


__all__ = ["THEME_IDS", "THEMES", "SYSTEM", "LIGHT", "DARK", "HIGH_CONTRAST", "DEFAULT_THEME", "apply",
           "palette", "style_sheet", "effective", "system_is_dark", "native_style_name", "scaled_font",
           "TYPE_SCALE", "canvas_colors", "layer_colors", "apply_canvas_theme",
           "contrast_report", "tokens", "boundary_color", "BOUNDARY_MIN_CONTRAST"]
