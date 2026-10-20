"""mlaser/ui/style.py: tokens, colour roles, buttons, forms, the dialog frame, tables."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QWidget

from mlaser.ui import style as S
from mlaser.ui.shell import theme as T
from mlaser.ui.shell.contrast import contrast


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_tokens_are_on_the_grid():
    assert (S.SP0, S.SP1, S.SP2, S.SP3, S.SP4, S.SP5) == (0, 4, 8, 16, 24, 32)
    assert all(v % 4 == 0 for v in S.MARGINS.values())
    assert S.ICON_S < S.ICON_M < S.ICON_L
    assert S.TARGET < S.TARGET_TOUCH < S.TARGET_RUN


def test_touch_constants_have_one_source():
    from mlaser.ui import responsive, safety_ui, workspaces
    from mlaser.ui.wizards import common
    assert responsive.TARGET == safety_ui.MIN_TARGET == workspaces.TOUCH == S.TARGET_RUN
    assert responsive.TARGET_MIN == common.MIN_TARGET == workspaces.TOUCH_MIN == S.TARGET_TOUCH


@pytest.mark.parametrize("theme", [T.LIGHT, T.DARK, T.HIGH_CONTRAST])
def test_every_theme_has_the_roles_and_they_are_legible(theme):
    t = T.THEMES[theme]
    for role in S.ROLES:
        assert role in t, (theme, role)
    for role in ("danger", "warning", "info", "success"):
        # the role's text on the window and on the base, and its "on" colour on its fill
        assert contrast(t[role], t["window"]) >= 4.5, (theme, role)
        assert contrast(t[role], t["base"]) >= 4.5, (theme, role)
    for role in ("warning", "info", "success"):
        assert contrast(t["on_" + role], t[role + "_fill"]) >= 4.5, (theme, role)
    assert contrast(t["on_danger"], t["danger_fill"]) >= 4.5
    # STOP looks the same in every theme (docs/SAFETY.md)
    assert t["danger_fill"] == T.THEMES[T.LIGHT]["danger_fill"]


def test_color_follows_the_theme(app):
    app.setProperty("mlaser_theme", T.LIGHT)
    try:
        light = S.css("warning")
        app.setProperty("mlaser_theme", T.DARK)
        assert S.css("warning") != light
        assert S.color("danger").name() == T.THEMES[T.DARK]["danger"]
    finally:
        app.setProperty("mlaser_theme", None)
    with pytest.raises(KeyError):
        S.css("no_such_role")


def test_styled_rebuilds_on_a_palette_change(app):
    lab = QLabel("x")
    calls = []

    def build():
        calls.append(1)
        return f"color:{S.css('danger')};"
    S.styled(lab, build)
    assert S.css("danger") in lab.styleSheet()
    n = len(calls)
    app.sendEvent(lab, __import__("PySide6.QtCore", fromlist=["QEvent"]).QEvent(
        __import__("PySide6.QtCore", fromlist=["QEvent"]).QEvent.PaletteChange))
    assert len(calls) > n


def test_font_roles_are_ratios_of_the_platform_font(app):
    base = app.font()
    for role, (ratio, _w) in S.TYPE_SCALE.items():
        f = S.font(role)
        if base.pointSizeF() > 0:
            assert f.pointSizeF() == pytest.approx(base.pointSizeF() * ratio)
    assert S.font("title").weight() > S.font("body").weight()


def test_run_buttons_one_source(app):
    for kind in S.RUN_KINDS:
        b = S.run_button(kind)
        assert b.objectName() == f"run_{kind}"
        assert b.text() and b.toolTip() and not b.icon().isNull()
        assert b.minimumHeight() >= S.TARGET_RUN
    assert S.run_text("arm") == "Arm laser" and S.run_text("arm", alt=True) == "Disarm laser"
    assert S.run_text("hold", alt=True) == "Continue"
    stop = S.run_button("stop")
    assert S.css("danger_fill") in stop.styleSheet()
    assert stop.minimumWidth() >= S.TARGET_RUN + S.STOP_EXTRA


def test_tool_button_needs_a_name(app):
    with pytest.raises(ValueError):
        S.tool_button("", "home")
    b = S.tool_button("Home all", "home")
    assert b.accessibleName() == "Home all" and b.toolTip() == "Home all"


def test_set_enabled_says_why(app):
    b = S.button("Frame", tooltip="Trace the outline")
    S.set_enabled(b, False, "Connect the machine first")
    assert not b.isEnabled() and "Connect the machine first" in b.toolTip()
    S.set_enabled(b, True)
    assert b.isEnabled() and b.toolTip() == "Trace the outline"
    parent = QWidget()
    lay = S.vbox(parent)
    lay.addWidget(b)
    S.set_enabled(b, False)
    b.setToolTip("")
    assert S.walk_disabled_without_reason(parent) == [b]


def test_form_row_units_in_the_suffix_and_reasons(app):
    from mlaser.ui.units_qt import QuantitySpinBox
    host = QWidget()
    f = S.form(host)
    q = QuantitySpinBox("length", minimum=0, maximum=100, value=1.0)
    lab = S.form_row(f, "Kerf width", q, unit_kind="length", hint="Half on each side")
    assert isinstance(lab, QLabel) and lab.buddy() is q
    assert "(" not in lab.text()
    with pytest.raises(TypeError):
        S.form_row(f, "Width", QLineEdit(), unit_kind="length")
    e = QLineEdit()
    S.form_row(f, "Name", e, disabled_reason="Only in Service mode")
    assert not e.isEnabled() and "Service mode" in e.toolTip()
    assert S.label_text("Speed:").rstrip(":") == "Speed"


def test_form_reflows_when_narrow(app):
    host = QWidget()
    f = S.form(host)
    for i in range(3):
        S.form_row(f, f"A rather long label number {i}", QLineEdit())
    host.resize(800, 200)
    host.show()
    app.processEvents()
    assert f.rowWrapPolicy() == QFormLayout.DontWrapRows
    host.resize(120, 400)
    app.processEvents()
    assert f.rowWrapPolicy() == QFormLayout.WrapAllRows
    host.resize(800, 200)
    app.processEvents()
    assert f.rowWrapPolicy() == QFormLayout.DontWrapRows
    host.close()
    host.deleteLater()


def test_dialog_frame(app):
    d = S.dialog(None, "Chamfer", explanation="Cut the corners of the selected parts.")
    assert d.windowTitle() == "Chamfer"
    m = d.layout().contentsMargins()
    assert (m.left(), m.top(), m.right(), m.bottom()) == (S.SP3,) * 4
    assert d.explanation.isVisibleTo(d) and d.explanation.wordWrap()
    assert isinstance(d.buttons, QDialogButtonBox)
    assert d.buttons.button(QDialogButtonBox.Ok) is not None
    f = d.form()
    assert f is d.form()
    d.deleteLater()


def test_table_key_column_stretches(app):
    t = S.table([S.Col("Name", stretch=True), S.Col("Qty", priority=2), "Notes"])
    assert t.columnCount() == 3
    assert t.column_fitter.priorities[0] is None
    t.deleteLater()


def test_menu_text_title_case_on_mac_only(monkeypatch):
    monkeypatch.setattr(S, "is_mac", lambda: True)
    assert S.menu_text("Reset layout to the default") == "Reset Layout to the Default"
    monkeypatch.setattr(S, "is_mac", lambda: False)
    assert S.menu_text("Reset layout") == "Reset layout"
