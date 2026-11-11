"""The canvas's look (UI review 2026-11, findings 7 and 8).

* Finding 7: ruler labels in the theme's ruler colour (>= 4.5:1 on the canvas in every theme;
  the lightened table colour was 1.2:1 on the light canvas) and the type scale's caption font;
  the canvas bar past the rulers' corner, opaque, its legend folded unless the view is tall and
  the legend covers little, each legend entry drawn with the real pen (a dashed rapid, a dotted
  gap, the kerf band, the pierce ring); thin lines one logical pixel wide (a width-0 pen is one
  device pixel, half as heavy at 2x); the kerf band only where the kerf is at least 2 px wide
  on screen; one background for the Design canvas and the Run live view.
* Finding 8: the layer colours as drawn and the toolpath roles are at least 10 ΔE apart for
  normal, deuteranope and protanope vision; the pierce ring has its own colour; every toolpath
  role has its own line style or marker; the canvas names a part's layer on hover.  The
  document's colours (data) do not change."""

from __future__ import annotations

import itertools
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402

from test_large_drawing import big_document  # noqa: E402

THEMES = ("light", "dark", "high_contrast")
#: the toolpath roles the legend explains, and those drawn with them
TOOLPATH_ROLES = ("lead_in", "lead_out", "kerf", "gap", "rapid", "pierce", "index", "preview_cut")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def themed(app):
    """Apply a theme's canvas colours (put back afterwards)."""
    from mlaser.ui import canvas as C
    from mlaser.ui.shell import theme as T
    saved = (dict(C.CANVAS_COLORS), C._DISPLAY_LAYER_COLORS)

    def apply(name):
        T.apply_canvas_theme(name)
    yield apply
    C.CANVAS_COLORS.clear()
    C.CANVAS_COLORS.update(saved[0])
    C.set_display_layer_colors(saved[1])


def _canvas(app, n=24, toolpath=True, size=(640, 480)):
    from mlaser.cam.toolpath import CamParams, build_toolpath
    from mlaser.model import LeadParams
    from mlaser.ui.canvas import CanvasView
    doc = big_document(n)
    for k, p in enumerate(doc.parts):
        p.craft.lead_in = LeadParams(kind=1, length=2.0, angle_deg=30.0)
        if k % 2 == 0:
            p.craft.kerf_comp, p.craft.kerf_width = 1, 0.2
    v = CanvasView()
    v.resize(*size)
    v.show()
    v.set_document(doc)
    if toolpath:
        v.set_toolpath(build_toolpath(doc, CamParams()))
    v.finish_overlay()
    app.processEvents()
    return v


# --------------------------------------------------------------------------- finding 8: colours
@pytest.mark.parametrize("name", THEMES)
def test_layer_colours_differ_for_red_green_colour_blind_users(name):
    from mlaser.ui import canvas as C
    from mlaser.ui.shell import theme as T
    from mlaser.ui.shell.contrast import contrast, cvd_delta_e, distinct
    cols = T.layer_colors(name)
    bg = T.canvas_colors(name)["background"]
    worst = min((cvd_delta_e(a, b), i + 1, j + 1) for (i, a), (j, b) in itertools.combinations(enumerate(cols), 2))
    assert worst[0] >= T.LAYER_CVD_DELTA_E, worst
    assert distinct(cols) >= 12.0
    assert all(contrast(c, bg) >= T.LAYER_MIN_CONTRAST[name] - 0.01 for c in cols)
    # the review's pairs (layers 1/9, 1/4, 3/5 light; 1/4, 2/7, 3/5 dark) were 3-9 apart
    for a, b in ((1, 9), (1, 4), (3, 5), (4, 9), (2, 7)):
        assert cvd_delta_e(cols[a - 1], cols[b - 1]) >= T.LAYER_CVD_DELTA_E
    # the document's colours are data: unchanged
    assert [C.layer_color(i).name() for i in range(len(C.LAYER_COLORS))] == C.LAYER_COLORS


def test_cvd_separation_moves_lightness_only():
    """Red and green of the same lightness: one is moved in lightness, hue and saturation stay."""
    import colorsys

    from mlaser.ui.shell.contrast import _hex, cvd_delta_e, separate_for_cvd
    cols = ["#00aa00", "#ed1c24"]
    assert cvd_delta_e(*cols) < 5
    out = separate_for_cvd(cols, "#ffffff", 3.0, 10.0)
    assert cvd_delta_e(*out) >= 10.0
    for a, b in zip(cols, out):
        ha, _la, sa = colorsys.rgb_to_hls(*_hex(a))
        hb, _lb, sb = colorsys.rgb_to_hls(*_hex(b))
        assert abs(ha - hb) < 0.02 and abs(sa - sb) < 0.05
    assert separate_for_cvd(["#000000", "#ffffff"], "#808080", 1.0) == ["#000000", "#ffffff"]


@pytest.mark.parametrize("name", THEMES)
def test_toolpath_roles_differ_for_everyone(name):
    from mlaser.ui.shell import theme as T
    from mlaser.ui.shell.contrast import contrast, cvd_delta_e
    cc = T.canvas_colors(name)
    bg = cc["background"]
    low = [(a, b, round(cvd_delta_e(cc[a], cc[b]), 1)) for a, b in itertools.combinations(TOOLPATH_ROLES, 2)
           if cvd_delta_e(cc[a], cc[b]) < 10.0]
    assert not low, low
    assert cc["pierce"] != cc["lead_in"]
    for role in TOOLPATH_ROLES:
        assert contrast(cc[role], bg) >= (4.5 if role == "index" else 3.0), role
    assert contrast(cc["ruler"], bg) >= 4.5


def test_every_toolpath_role_has_its_own_line_style():
    from mlaser.ui.canvas import TOOLPATH_STYLE, toolpath_pen
    assert toolpath_pen("rapid").style() == Qt.DashLine
    assert toolpath_pen("gap").style() == Qt.DotLine           # laser off: not a solid cut line
    assert all(toolpath_pen(r).isCosmetic() and toolpath_pen(r).widthF() >= 1.0 for r in TOOLPATH_STYLE if r != "bad")


def test_hover_names_the_part_and_its_layer(app):
    v = _canvas(app, toolpath=False)
    try:
        part = v.document.parts[5]
        x, y = part.contours[0].segments[2].start
        assert v.hover_text(x, y) == f"{part.name}: Layer {part.craft.layer}"
        part.name = ""
        assert v.hover_text(x, y) == f"Layer {part.craft.layer}"
        assert v.hover_text(-500.0, -500.0) == ""
    finally:
        v.close()


# --------------------------------------------------------------------------- finding 7: the canvas
def test_thin_lines_are_one_logical_pixel(app):
    v = _canvas(app)
    try:
        pens = [it.pen() for _, _, it, _ in v._items] + [v._table.pen()] + [a.pen() for a in v._axes]
        pens += [it.pen() for role in ("rapid", "cut") for it in v.toolpath_items(role)]
        assert pens and all(p.isCosmetic() and p.widthF() >= 1.0 for p in pens)
    finally:
        v.close()


def test_kerf_band_only_where_the_kerf_shows(app):
    from PySide6.QtGui import QImage, QPainter, QPainterPath
    from PySide6.QtWidgets import QStyleOptionGraphicsItem

    from mlaser.ui.canvas import KERF_BAND_MIN_PX, _KerfBand
    path = QPainterPath()
    path.moveTo(1, 1)
    path.lineTo(9, 9)
    band = _KerfBand(path, "#ff0000", 0.2)

    def drawn(scale: float) -> bool:
        img = QImage(400, 400, QImage.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.scale(scale, scale)
        band.paint(p, QStyleOptionGraphicsItem(), None)
        p.end()
        return any(img.pixel(x, y) for x in range(0, 400, 2) for y in range(0, 400, 2))
    assert not drawn(KERF_BAND_MIN_PX / 0.2 * 0.9)            # 1.8 px of kerf: the path alone
    assert drawn(KERF_BAND_MIN_PX / 0.2 * 1.1)
    v = _canvas(app)
    try:
        assert v.toolpath_items("kerf_band") and all(isinstance(b, _KerfBand) and b.kerf == pytest.approx(0.2)
                                                     for b in v.toolpath_items("kerf_band"))
    finally:
        v.close()


def test_ruler_labels_read_on_the_light_canvas(app, themed):
    from mlaser.ui import canvas as C
    from mlaser.ui.style import font
    themed("light")
    v = _canvas(app)
    try:
        v.apply_theme()
        v.finish_overlay()
        assert v.ruler_font().pointSizeF() == pytest.approx(font("caption").pointSizeF())
        img = v.grab().toImage()
        r = v.ruler_px()
        band = {img.pixelColor(x, y).name() for x in range(r, img.width(), 1) for y in range(0, r)}
        assert C.CANVAS_COLORS["ruler"] in band                 # the ticks, in the ruler colour
        old = QColor(C.CANVAS_COLORS["table"]).lighter(160).name()
        assert old not in band
    finally:
        v.close()


def test_canvas_bar_keeps_off_the_rulers_and_folds_its_legend(app):
    v = _canvas(app, size=(640, 480))
    hud = v.hud
    try:
        assert hud.x() >= v.ruler_px() and hud.y() >= v.ruler_px()
        assert "rgba" not in hud.styleSheet()                   # opaque
        # a short view: the legend is folded behind its button
        assert hud.legend_button.isVisible() and not hud.legend.isVisible()
        assert not hud.legend_button.isChecked() and hud.legend_button.accessibleName()
        hud.legend_button.click()
        app.processEvents()
        assert hud.legend.isVisible() and hud.legend_button.isChecked()
        assert set(hud.legend.swatches) == {"lead_in", "pierce", "lead_out", "kerf", "gap", "rapid"}
        assert "Pierce" in hud.legend.text()
        hud.legend_button.click()
        app.processEvents()
        assert not hud.legend.isVisible()
        # a tall view opens it by itself (when it covers little of the drawing)
        hud._legend_choice = None
        v.resize(1200, hud.LEGEND_AUTO_H + 40)
        app.processEvents()
        assert hud.legend.isVisible()
        area = v.width() * v.height()
        assert hud.width() * hud.height() <= hud.AREA_SHARE * area + 1
        # no rulers: the bar goes to the corner
        v.options.show_rulers = False
        hud.place()
        assert hud.x() == 8
    finally:
        v.close()


def test_legend_swatches_draw_the_real_pens(app):
    """The rapid's sample is dashed (gaps along the line), the gap's dotted, the pierce a ring."""
    v = _canvas(app)
    try:
        sw = v.hud.legend.swatches

        def row(role):
            img = sw[role].grab().toImage()
            y = img.height() // 2
            return [img.pixelColor(x, y).alpha() > 0 and img.pixelColor(x, y) != img.pixelColor(0, 0)
                    for x in range(img.width())]
        solid, dashed, dotted = row("lead_in"), row("rapid"), row("gap")
        runs = lambda r: sum(1 for a, b in zip(r, r[1:]) if a and not b)   # noqa: E731
        assert runs(solid) == 1 and runs(dashed) >= 2 and runs(dotted) >= 3
        ring = row("pierce")
        assert runs(ring) == 2                                    # through a ring: two edges
    finally:
        v.close()


def test_run_live_view_has_the_canvas_background(app, themed):
    from mlaser.ui import canvas as C
    from mlaser.ui.workspaces.run import RunCanvas
    for name in ("dark", "light"):
        themed(name)
        rc = RunCanvas()
        rc.resize(300, 240)
        img = rc.grab().toImage()
        assert img.pixelColor(3, 3).name() == QColor(C.CANVAS_COLORS["background"]).name(), name
        rc.close()
