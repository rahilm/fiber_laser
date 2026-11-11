"""Theme details (UI review 2026-11-09 findings 6 and 9).

* An icon tint names a colour role and is looked up when the icon is painted: after a live
  switch to dark the interlocks chip's icon was the light theme's ``on_warning`` on the dark
  fill (1.60:1); the fixed tints (warning 2.7:1 on light, neutral 1.3:1 on dark) become the
  theme's roles.
* High contrast: push buttons have a visible edge (Fusion drew it black on black, 1.26:1).
* Disabled text reads at 3.5:1 or more on the window and on a button.
* Red and amber are reserved for alarm, STOP and hazard: the canvas head marker and cut track
  are not red, the simulation's stop icon is neutral, the "interlocks not configured" chip is a
  neutral notice (a tripped interlock keeps its colour elsewhere)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest  # noqa: E402

pytest.importorskip("PySide6")
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402

from mlaser.ui import icons  # noqa: E402
from mlaser.ui import style as S  # noqa: E402
from mlaser.ui.shell import theme as T  # noqa: E402
from mlaser.ui.shell.contrast import contrast  # noqa: E402


@pytest.fixture
def app():
    a = QApplication.instance() or QApplication([])
    old = a.property("mlaser_theme")
    yield a
    T.apply(old or T.LIGHT, app=a)


def _ink(ic, size: int = 32) -> QColor:
    """The colour of an icon's strokes (its most opaque pixel)."""
    img = ic.pixmap(size, size).toImage()
    best, col = -1, QColor()
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > best:
                best, col = c.alpha(), c
    return col


@pytest.mark.parametrize("tint", ["warning", icons.AMBER])
def test_an_icon_tint_follows_a_live_theme_switch(app, tint):
    T.apply(T.LIGHT, app=app)
    ic = icons.icon("alert-triangle", tint)            # made once, as a chip's icon is
    assert _ink(ic).name() == T.THEMES[T.LIGHT]["warning"]
    T.apply(T.DARK, app=app)
    assert _ink(ic).name() == T.THEMES[T.DARK]["warning"]


def test_state_icon_roles_read_on_every_window():
    for theme in (T.LIGHT, T.DARK, T.HIGH_CONTRAST):
        t = T.THEMES[theme]
        for role in set(icons.COMMAND_COLORS.values()) | {r for r in ("success", "warning", "danger", "info")}:
            assert contrast(t[role], t["window"]) >= 3.0, (theme, role)
            assert contrast(t[role], t["button"]) >= 3.0, (theme, role)


def test_the_run_buttons_icons_follow_a_live_theme_switch(app):
    T.apply(T.LIGHT, app=app)
    b = S.run_button("start")
    try:
        assert _ink(b.icon()).name() == T.THEMES[T.LIGHT]["success"]
        T.apply(T.DARK, app=app)
        assert _ink(b.icon()).name() == T.THEMES[T.DARK]["success"]
    finally:
        b.deleteLater()


def test_interlocks_chip_is_a_neutral_notice_that_follows_the_theme(app):
    from wiring import dispose, real_window
    T.apply(T.LIGHT, app=app)
    w = real_window()
    try:
        chip = w.findChild(QWidget, "chip_interlocks")
        assert chip is not None
        for theme in (T.LIGHT, T.DARK):
            T.apply(theme, app=app)
            app.processEvents()
            t = T.THEMES[theme]
            qss = chip.styleSheet()
            assert t["warning_fill"] not in qss and t["warning"] not in qss, qss     # not amber
            ink = _ink(chip.icon(), 16).name()
            assert contrast(ink, t["base"]) >= 4.5, (theme, ink)                      # 1.60:1 before
    finally:
        dispose(w)


def test_high_contrast_push_buttons_have_an_edge(app):
    T.apply(T.HIGH_CONTRAST, app=app)
    top = QWidget()
    top.resize(200, 80)
    b = QPushButton("Frame", top)
    b.setGeometry(20, 20, 120, 40)
    top.show()
    app.processEvents()
    try:
        img = top.grab().toImage()
        win = QColor(T.THEMES[T.HIGH_CONTRAST]["window"]).name()
        # the button's left edge, half way down: the brightest of its first pixels
        edge = max((img.pixelColor(x, 40) for x in range(19, 24)), key=lambda c: c.lightness())
        assert contrast(edge.name(), win) >= 3.0, edge.name()
    finally:
        top.close()
        top.deleteLater()


@pytest.mark.parametrize("theme", [T.LIGHT, T.DARK, T.HIGH_CONTRAST])
def test_disabled_text_reads(theme):
    t = T.THEMES[theme]
    assert contrast(t["disabled"], t["window"]) >= 3.5
    assert contrast(t["disabled"], t["button"]) >= 3.5
    assert contrast(t["muted"], t["window"]) > contrast(t["disabled"], t["window"]) or theme == T.HIGH_CONTRAST


def _reddish(c: str) -> bool:
    col = QColor(c)
    h = col.hslHueF() * 360
    return col.hslSaturationF() > 0.35 and (h < 20 or h > 340)


@pytest.mark.parametrize("theme", [T.LIGHT, T.DARK, T.HIGH_CONTRAST])
def test_normal_canvas_marks_are_not_red(theme):
    t = T.THEMES[theme]
    for role in ("c_track", "c_head"):
        assert not _reddish(t[role]), (theme, role, t[role])
    for role in ("c_track", "c_head"):
        assert contrast(t[role], t["c_background"]) >= 3.0, (theme, role)


def test_stopping_the_simulation_is_not_red():
    assert "sim.stop" not in icons.COMMAND_COLORS
    assert icons.COMMAND_COLORS["run.stop"] == "danger"


def test_one_type_scale():
    assert T.TYPE_SCALE["title"] == S.TYPE_SCALE["title"][0]
    assert T.scaled_font("title").pointSizeF() == S.font("title").pointSizeF()
