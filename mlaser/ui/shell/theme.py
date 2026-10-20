"""Themes: light, dark and high contrast (docs/review/ux.md P2-2, docs/user/shell.md §5).

A theme is a set of design tokens (:data:`THEMES`) turned into

* a ``QPalette`` for the application (Fusion style, so every platform honours it; the SVG icons
  of :mod:`mlaser.ui.icons` tint themselves from the palette's button text colour),
* a small style sheet for the shell's own widgets (rail, top bar, chips, palette, empty
  states), the keyboard focus ring and the Operator mode's large touch targets, and
* the canvas colours (:data:`mlaser.ui.canvas.CANVAS_COLORS`) with the layer colours adjusted
  for contrast against the canvas background (:func:`.contrast.adjust_palette`: at least 3:1,
  WCAG 1.4.11, and still distinguishable from each other).

The choice is persisted in the ConfigService (``shell.theme``).  The safety banners keep their
own fixed colours in every theme (docs/SAFETY.md): they always carry the state as text too.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .contrast import adjust_palette, contrast

LIGHT, DARK, HIGH_CONTRAST = "light", "dark", "high_contrast"
THEME_IDS = (LIGHT, DARK, HIGH_CONTRAST)
DEFAULT_THEME = LIGHT

THEMES: dict[str, dict[str, str]] = {
    LIGHT: {
        "window": "#f3f4f6", "base": "#ffffff", "alt_base": "#f6f7f9", "text": "#1d2127",
        "muted": "#59616d", "button": "#e8eaee", "button_text": "#1d2127", "border": "#c4c9d1",
        "accent": "#1b5fc1", "accent_text": "#ffffff", "tooltip": "#ffffe8", "tooltip_text": "#1d2127",
        "disabled": "#8b929c", "rail": "#e4e7ec", "rail_checked": "#ffffff", "topbar": "#ffffff",
        "focus": "#1b5fc1", "link": "#1b5fc1", "placeholder": "#6b7280",
        # canvas
        "c_background": "#fbfbfa", "c_table": "#8b9099", "c_axis_x": "#c62828", "c_axis_y": "#2e7d32",
        "c_cut": "#3a3f46", "c_gap": "#c2410c", "c_rapid": "#3b5bdb", "c_track": "#d62828",
        "c_unclosed": "#d00000", "c_selected": "#101418", "c_start": "#101418", "c_index": "#8a5300",
        "c_bbox": "#7b818b", "c_arrow": "#101418", "c_head": "#d62828", "c_marker": "#0b7285",
        "c_sim": "#101418",
    },
    DARK: {
        "window": "#1f2226", "base": "#16181b", "alt_base": "#1c1f23", "text": "#e6e8eb",
        "muted": "#a3aab4", "button": "#2b2f35", "button_text": "#e6e8eb", "border": "#3d434b",
        "accent": "#4d8ff0", "accent_text": "#0b0d10", "tooltip": "#2b2f35", "tooltip_text": "#e6e8eb",
        "disabled": "#6f7680", "rail": "#181a1d", "rail_checked": "#2f343b", "topbar": "#181a1d",
        "focus": "#7fb0ff", "link": "#7fb0ff", "placeholder": "#8b929c",
        "c_background": "#000000", "c_table": "#606060", "c_axis_x": "#c04040", "c_axis_y": "#40c040",
        "c_cut": "#d0d0d0", "c_gap": "#ff8000", "c_rapid": "#8080ff", "c_track": "#ff6060",
        "c_unclosed": "#ff0000", "c_selected": "#ffffff", "c_start": "#ffffff", "c_index": "#ffd700",
        "c_bbox": "#707070", "c_arrow": "#ffffff", "c_head": "#ff4040", "c_marker": "#00ffff",
        "c_sim": "#ffffff",
    },
    HIGH_CONTRAST: {      # "safety glasses": black, white, yellow; no greys for content
        "window": "#000000", "base": "#000000", "alt_base": "#0a0a0a", "text": "#ffffff",
        "muted": "#ffffff", "button": "#000000", "button_text": "#ffffff", "border": "#ffffff",
        "accent": "#ffd400", "accent_text": "#000000", "tooltip": "#000000", "tooltip_text": "#ffffff",
        "disabled": "#b0b0b0", "rail": "#000000", "rail_checked": "#ffd400", "topbar": "#000000",
        "focus": "#00e5ff", "link": "#00e5ff", "placeholder": "#d0d0d0",
        "c_background": "#000000", "c_table": "#bdbdbd", "c_axis_x": "#ff5252", "c_axis_y": "#69f0ae",
        "c_cut": "#ffffff", "c_gap": "#ff9100", "c_rapid": "#82b1ff", "c_track": "#ff5252",
        "c_unclosed": "#ff1744", "c_selected": "#ffffff", "c_start": "#ffffff", "c_index": "#ffff00",
        "c_bbox": "#bdbdbd", "c_arrow": "#ffffff", "c_head": "#ff1744", "c_marker": "#00e5ff",
        "c_sim": "#ffffff",
    },
}
#: minimum contrast of the layer colours against the canvas (WCAG 1.4.11 asks 3:1; the high
#: contrast theme asks more)
LAYER_MIN_CONTRAST = {LIGHT: 3.0, DARK: 3.0, HIGH_CONTRAST: 4.5}


def tokens(theme: str) -> dict[str, str]:
    return THEMES.get(theme, THEMES[DEFAULT_THEME])


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


def style_sheet(theme: str) -> str:
    """The shell's style sheet (set on the main window, so dialogs of other tracks keep the
    plain palette look)."""
    t = tokens(theme)
    hc = theme == HIGH_CONTRAST
    bw = "2px" if hc else "1px"
    qss = f"""
QToolBar#shell_rail {{ background: {t['rail']}; border: none; border-right: {bw} solid {t['border']};
    spacing: 2px; padding: 4px 2px; }}
QToolBar#shell_rail QToolButton {{ color: {t['text']}; border: {bw} solid transparent; border-radius: 6px;
    padding: 4px 2px; min-width: 64px; min-height: 52px; }}
QToolBar#shell_rail QToolButton:checked {{ background: {t['rail_checked']}; border-color: {t['border']};
    {('color: ' + t['accent_text'] + ';') if hc else ('color: ' + t['accent'] + ';')} font-weight: bold; }}
QToolBar#shell_rail QToolButton:hover {{ border-color: {t['border']}; }}
QToolBar#shell_rail QToolButton:focus {{ border: 2px solid {t['focus']}; }}
QWidget#shell_topbar {{ background: {t['topbar']}; border-bottom: {bw} solid {t['border']}; }}
QWidget#shell_topbar QLabel#shell_file {{ font-weight: bold; color: {t['text']}; }}
QPushButton#shell_search {{ text-align: left; padding: 4px 10px; border: {bw} solid {t['border']};
    border-radius: 6px; background: {t['base']}; color: {t['placeholder']}; min-width: 220px; }}
QPushButton#shell_search:focus {{ border: 2px solid {t['focus']}; }}
QLabel[chip="true"] {{ border-radius: 9px; padding: 2px 8px; font-weight: bold; }}
QToolBar#shell_ws_toolbar {{ background: {t['window']}; border: none; border-bottom: {bw} solid {t['border']};
    spacing: 2px; }}
QWidget#shell_empty {{ background: {t['window']}; }}
QLabel#shell_empty_title {{ font-size: 18pt; font-weight: bold; color: {t['text']}; }}
QLabel#shell_empty_text {{ color: {t['muted']}; }}
QToolButton#shell_empty_cmd {{ border: {bw} solid {t['border']}; border-radius: 6px; padding: 6px;
    background: {t['base']}; color: {t['text']}; min-width: 120px; min-height: 64px; }}
QToolButton#shell_empty_cmd:focus {{ border: 2px solid {t['focus']}; }}
QFrame#shell_palette {{ background: {t['base']}; border: {bw} solid {t['border']}; border-radius: 8px; }}
QFrame#shell_palette QLineEdit {{ font-size: 13pt; padding: 6px; border: none;
    border-bottom: {bw} solid {t['border']}; background: {t['base']}; color: {t['text']}; }}
QFrame#shell_palette QListWidget {{ border: none; background: {t['base']}; color: {t['text']}; }}
QFrame#shell_palette QListWidget::item:selected {{ background: {t['accent']}; color: {t['accent_text']}; }}
QMainWindow[userMode="operator"] QPushButton, QMainWindow[userMode="operator"] QToolButton,
QMainWindow[userMode="operator"] QComboBox {{ min-height: 48px; font-size: 12pt; }}
QMainWindow[userMode="operator"] QToolBar#shell_rail QToolButton {{ min-width: 84px; min-height: 72px; }}
"""
    if hc:
        qss += f"""
QPushButton, QToolButton, QComboBox, QLineEdit, QAbstractSpinBox {{ border: 1px solid {t['border']}; }}
QPushButton:focus, QToolButton:focus, QComboBox:focus, QLineEdit:focus,
QAbstractSpinBox:focus {{ border: 3px solid {t['focus']}; }}
QToolTip {{ color: #ffffff; background: #000000; border: 1px solid #ffffff; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; border: 2px solid #ffffff;
    background: #000000; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {t['accent']}; }}
QProgressBar {{ border: 1px solid #ffffff; color: #ffffff; text-align: center; }}
QProgressBar::chunk {{ background: {t['accent']}; }}
QTabBar::tab:selected {{ color: {t['accent_text']}; background: {t['accent']}; }}
QHeaderView::section {{ border: 1px solid #ffffff; }}
"""
    return qss


def canvas_colors(theme: str) -> dict[str, str]:
    t = tokens(theme)
    return {k[2:]: v for k, v in t.items() if k.startswith("c_")}


def layer_colors(theme: str, base: Optional[list[str]] = None) -> list[str]:
    """The layer colours as drawn in ``theme`` (contrast-adjusted, distinguishable)."""
    from .. import canvas
    cols = list(base or canvas.LAYER_COLORS)
    bg = canvas_colors(theme)["background"]
    return adjust_palette(cols, bg, LAYER_MIN_CONTRAST.get(theme, 3.0))


def apply_canvas_theme(theme: str, view=None) -> None:
    from .. import canvas
    canvas.CANVAS_COLORS.update(canvas_colors(theme))
    canvas.set_display_layer_colors(layer_colors(theme))
    if view is not None:
        view.apply_theme()


def apply(theme: str, window=None, app: Optional[QApplication] = None) -> str:
    """Apply ``theme`` to the application (palette, Fusion), the window's style sheet and the
    canvas.  Returns the theme id actually applied."""
    theme = theme if theme in THEMES else DEFAULT_THEME
    app = app or QApplication.instance()
    if app is not None and app.property("mlaser_theme") != theme:
        # application-wide changes only when the theme really changes: setStyle / setPalette
        # post a change event to every widget of every window
        if app.style().objectName().lower() != "fusion":
            app.setStyle("Fusion")
        app.setPalette(palette(theme))
        app.setProperty("mlaser_theme", theme)
    if window is not None:
        qss = style_sheet(theme)
        if window.styleSheet() != qss:
            window.setStyleSheet(qss)
        apply_canvas_theme(theme, getattr(window, "canvas", None))
        # icons re-tint from the new palette: repaint everything
        window.update()
    return theme


def contrast_report(theme: str) -> dict[str, float]:
    """Contrast ratios that matter (tests, docs/user/shell.md): text on the window and base,
    accent text, and every layer colour against the canvas."""
    t = tokens(theme)
    out = {"text/window": contrast(t["text"], t["window"]), "text/base": contrast(t["text"], t["base"]),
           "accent_text/accent": contrast(t["accent_text"], t["accent"]),
           "muted/window": contrast(t["muted"], t["window"]),
           "disabled/window": contrast(t["disabled"], t["window"])}
    bg = canvas_colors(theme)["background"]
    for i, c in enumerate(layer_colors(theme)):
        out[f"layer{i}/canvas"] = contrast(c, bg)
    for role in ("unclosed", "selected", "start", "index", "cut", "head"):
        out[f"{role}/canvas"] = contrast(canvas_colors(theme)[role], bg)
    return out


__all__ = ["THEME_IDS", "THEMES", "LIGHT", "DARK", "HIGH_CONTRAST", "DEFAULT_THEME", "apply",
           "palette", "style_sheet", "canvas_colors", "layer_colors", "apply_canvas_theme",
           "contrast_report", "tokens"]
