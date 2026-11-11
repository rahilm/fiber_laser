"""Toolpath role colours, the Run live view's legend and the colour-blind-safe layer palette
(UI review 2026-11-09, findings 6, 7 and 8).

* Every toolpath role (cut, rapid, lead-in, lead-out, pierce, micro-joint gap, kerf, marker, the
  live cut track, the Run view's done parts, the head) has a colour of its own in every theme:
  at least :data:`ROLE_DELTA_E` apart from every other role for normal, deuteranope and protanope
  vision, and readable on the canvas.  The cut track was 7 ΔE from the rapids in dark and the
  same cyan as the pierce ring and the markers in high contrast; the head was the cut path's
  white there.  Each role also differs by line style or shape, never by colour alone.  Red and
  amber stay reserved for alarm, STOP and emission.
* The Run live view: the part being cut in the cut track's colour, the done parts in the canvas's
  done colour (one meaning in both views), and the legend never covers the head marker.
* Preferences > Appearance > colour-blind-safe layer colours (``shell.colorblind_palette``, off
  by default): an Okabe-Ito based layer palette that keeps the layers apart under deuteranopia
  and protanopia, checked here with an independent LMS simulation (Viénot, Brettel and Mollon
  1999), applied live; the document's layer colours (data, exports) do not change."""

from __future__ import annotations

import itertools
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest  # noqa: E402

pytest.importorskip("PySide6")
from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402

from mlaser.ui.shell import theme as T  # noqa: E402
from mlaser.ui.shell.contrast import contrast, cvd_delta_e  # noqa: E402

THEMES = (T.LIGHT, T.DARK, T.HIGH_CONTRAST)
#: every role drawn on the canvas or the Run live view while a job is planned or runs
ROLES = ("cut", "rapid", "lead_in", "lead_out", "pierce", "gap", "kerf", "marker", "track", "done", "head")
ROLE_DELTA_E = 12.0


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def themed(app):
    """Apply a theme's canvas colours (put back afterwards, the colour-blind palette off)."""
    from mlaser.ui import canvas as C
    saved = (dict(C.CANVAS_COLORS), C._DISPLAY_LAYER_COLORS, C._SWATCH_LAYER_COLORS, T.colorblind_palette())
    yield T.apply_canvas_theme
    T.set_colorblind_palette(saved[3])
    C.CANVAS_COLORS.clear()
    C.CANVAS_COLORS.update(saved[0])
    C.set_display_layer_colors(saved[1])
    C.set_swatch_layer_colors(saved[2])


# --------------------------------------------------------------------------- an LMS simulation
# Viénot, Brettel & Mollon 1999: linear sRGB -> LMS, the missing cone's response rebuilt from the
# other two, back to linear sRGB; ΔE (CIE76) in CIELAB, D65.  Independent of the app's own
# (Machado 2009) simulation in mlaser.ui.shell.contrast.
_RGB2LMS = ((17.8824, 43.5161, 4.11935), (3.45565, 27.1554, 3.86714), (0.0299566, 0.184309, 1.46709))
_LMS2RGB = ((0.0809444479, -0.130504409, 0.116721066), (-0.0102485335, 0.0540193266, -0.113614708),
            (-0.000365296938, -0.00412161469, 0.693511405))


def _mul(m, v):
    return tuple(sum(a * b for a, b in zip(row, v)) for row in m)


def _linear(c: str) -> tuple[float, float, float]:
    out = []
    for i in (1, 3, 5):
        v = int(c[i:i + 2], 16) / 255
        out.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
    return out[0], out[1], out[2]


def simulate(c: str, vision: str) -> tuple[float, float, float]:
    rgb = _linear(c)
    if vision == "normal":
        return rgb
    lms = list(_mul(_RGB2LMS, rgb))
    if vision == "protan":
        lms[0] = 2.02344 * lms[1] - 2.52581 * lms[2]
    else:
        lms[1] = 0.494207 * lms[0] + 1.24827 * lms[2]
    r, g, b = _mul(_LMS2RGB, lms)
    return min(1.0, max(0.0, r)), min(1.0, max(0.0, g)), min(1.0, max(0.0, b))


def _lab(rgb) -> tuple[float, float, float]:
    r, g, b = rgb
    xyz = ((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047, 0.2126 * r + 0.7152 * g + 0.0722 * b,
           (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
    fx, fy, fz = (t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116 for t in xyz)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def lms_delta_e(a: str, b: str) -> float:
    """The smallest ΔE of ``a`` and ``b`` for normal, deuteranope and protanope vision."""
    return min(math.dist(_lab(simulate(a, v)), _lab(simulate(b, v))) for v in ("normal", "deutan", "protan"))


def test_the_simulation_collapses_red_and_green():
    """The simulation itself: a red and a green of one lightness look alike to both."""
    assert math.dist(_lab(simulate("#00aa00", "normal")), _lab(simulate("#ed1c24", "normal"))) > 100
    assert lms_delta_e("#00aa00", "#ed1c24") < 15
    assert lms_delta_e("#0072b2", "#e69f00") > 40                  # blue / orange survive


def _reddish_or_amber(c: str) -> bool:
    col = QColor(c)
    h = col.hslHueF() * 360
    return col.hslSaturationF() > 0.35 and 0.15 < col.lightnessF() < 0.95 and (h < 50 or h > 340)


# --------------------------------------------------------------------------- toolpath roles
@pytest.mark.parametrize("name", THEMES)
def test_every_toolpath_role_has_its_own_colour(name):
    cc = T.canvas_colors(name)
    bg = cc["background"]
    for sim, label in ((cvd_delta_e, "machado"), (lms_delta_e, "lms")):
        low = [(a, b, round(sim(cc[a], cc[b]), 1)) for a, b in itertools.combinations(ROLES, 2)
               if sim(cc[a], cc[b]) < ROLE_DELTA_E]
        assert not low, (name, label, low)
    floor = 4.5 if name == T.HIGH_CONTRAST else 3.0
    weak = {r: round(contrast(cc[r], bg), 2) for r in ROLES if contrast(cc[r], bg) < (3.0 if r == "done" else floor)}
    assert not weak, (name, weak)
    # the review's collisions
    assert cc["head"] != cc["cut"] and cc["track"] not in (cc["pierce"], cc["marker"], cc["rapid"])


@pytest.mark.parametrize("name", THEMES)
def test_red_and_amber_are_not_used_for_normal_marks(name):
    """Red and amber are alarm, STOP and emission (finding 6): no normal toolpath role is red or
    amber.  The X axis keeps the CAD convention's red, the open-contour flag is an error, the
    preview's laser-on head and cut are emission."""
    cc = T.canvas_colors(name)
    hits = {r: cc[r] for r in ROLES if _reddish_or_amber(cc[r])}
    assert not hits, (name, hits)


def test_every_role_differs_by_line_style_or_shape_too(app):
    from mlaser.ui.canvas import PIERCE_RING, TOOLPATH_STYLE, TRACK_PX, toolpath_pen
    lines = {r: (toolpath_pen(r).style(), toolpath_pen(r).widthF()) for r in ("cut", "kerf", "rapid", "gap",
                                                                               "lead_in", "lead_out")}
    lines["track"] = (Qt.SolidLine, TRACK_PX)
    assert len(set(lines.values())) == len(lines), lines
    assert TRACK_PX > max(w for _z, w, _s in TOOLPATH_STYLE.values())
    assert PIERCE_RING[0] > 0                                        # the pierce: a ring


def test_design_canvas_draws_the_track_and_head_in_their_roles(app, themed):
    from mlaser.ui import canvas as C
    from mlaser.ui.canvas import TRACK_PX, CanvasView
    themed(T.HIGH_CONTRAST)
    v = CanvasView()
    try:
        v.add_track_point(0, 0, True)
        v.add_track_point(10, 0, True)
        v.set_head_position(10, 0)
        assert v._track_pen().color().name() == C.CANVAS_COLORS["track"] and v._track_pen().widthF() == TRACK_PX
        assert v._head.pen().color().name() == C.CANVAS_COLORS["head"] != C.CANVAS_COLORS["cut"]
    finally:
        v.close()


# --------------------------------------------------------------------------- the Run live view
def _run_canvas(app, size, head):
    from mlaser.cam.toolpath import CamParams  # noqa: F401 - the document module loads with it
    from mlaser.ui.workspaces.run import RunCanvas
    from test_large_drawing import big_document
    rc = RunCanvas()
    rc.resize(*size)
    rc.set_document(big_document(9))
    x0, y0, x1, y1 = rc._bounds
    rc.set_progress({0, 1}, 2, head if head is not None else (x0, y0))
    rc.show()
    app.processEvents()
    return rc


@pytest.mark.parametrize("name", THEMES)
def test_run_view_colours_are_the_canvas_roles(app, themed, name):
    from mlaser.ui import canvas as C
    from mlaser.ui.workspaces.run import RunCanvas
    themed(name)
    assert RunCanvas.current_color().name() == C.CANVAS_COLORS["track"]
    assert RunCanvas.done_color().name() == C.CANVAS_COLORS["done"]


@pytest.mark.parametrize("size", [(260, 220), (400, 300), (683, 400), (1200, 700)])
def test_run_legend_never_covers_the_head(app, themed, size):
    """The head at the sheet's origin (the bottom-left corner, where the legend was) and at the
    other corners: the legend moves to a corner clear of it, and the head is drawn last."""
    from mlaser.ui import canvas as C
    themed(T.HIGH_CONTRAST)
    rc = _run_canvas(app, size, None)
    try:
        x0, y0, x1, y1 = rc._bounds
        for head in ((x0, y0), (x1, y0), (x0, y1), (x1, y1), ((x0 + x1) / 2, (y0 + y1) / 2)):
            rc.set_progress({0, 1}, 2, head)
            rect, entries = rc.legend_layout()
            hx, hy = rc.head_point()
            r = rc.HEAD_R
            assert entries and rect.right() <= rc.width() and rect.left() >= 0
            assert not rect.intersects(QRectF(hx - r, hy - r, 2 * r, 2 * r)), (size, head, rect, (hx, hy))
            img = rc.grab().toImage()
            assert img.pixelColor(round(hx), round(hy)).name() == QColor(C.CANVAS_COLORS["head"]).name()
    finally:
        rc.close()


def test_run_legend_wraps_in_a_narrow_view(app, themed):
    themed(T.LIGHT)
    rc = _run_canvas(app, (260, 220), None)
    try:
        rect, entries = rc.legend_layout()
        assert rect.width() <= rc.width() - 2 * 8 + 0.5
        assert len({round(y) for _x, y, *_ in entries}) >= 2          # two rows at least
    finally:
        rc.close()


def test_head_is_on_top_even_in_a_view_with_no_free_corner(app, themed):
    """A legend that fills the whole view (no corner is free of the head): the head is still
    drawn over it."""
    from mlaser.ui import canvas as C
    themed(T.DARK)
    rc = _run_canvas(app, (260, 220), None)
    try:
        ink = QColor(C.CANVAS_COLORS["head"]).darker()
        rows = [(0.0, float(y), "X" * 40, ink, Qt.SolidLine) for y in range(0, rc.height(), 8)]
        rc.legend_layout = lambda fm=None: (QRectF(0, 0, rc.width(), rc.height()), rows)
        hx, hy = rc.head_point()
        img = rc.grab().toImage()
        assert img.pixelColor(round(hx), round(hy)).name() == QColor(C.CANVAS_COLORS["head"]).name()
    finally:
        rc.close()


# --------------------------------------------------------------------------- colour-blind palette
@pytest.mark.parametrize("name", THEMES)
def test_colour_blind_palette_keeps_the_layers_apart(name):
    from mlaser.ui import canvas as C
    cb = T.COLORBLIND_LAYERS[name]
    assert len(cb) == len(C.LAYER_COLORS)
    bg = T.canvas_colors(name)["background"]
    worst = min((lms_delta_e(a, b), i, j) for (i, a), (j, b) in itertools.combinations(enumerate(cb), 2))
    default = min(lms_delta_e(a, b) for a, b in itertools.combinations(T.layer_colors(name), 2))
    assert worst[0] >= 14.0 and worst[0] > default + 3, (name, worst, default)
    assert min(cvd_delta_e(a, b) for a, b in itertools.combinations(cb, 2)) >= T.COLORBLIND_DELTA_E
    assert all(contrast(c, bg) >= T.LAYER_MIN_CONTRAST[name] for c in cb), name


def test_colour_blind_palette_is_display_only(app, themed):
    from mlaser.ui import canvas as C
    doc_colours = list(C.LAYER_COLORS)
    themed(T.DARK)
    assert not T.colorblind_palette()
    assert C.display_layer_color(1).name() == T.layer_colors(T.DARK)[1]
    assert C.swatch_layer_color(1).name() == C.layer_color(1).name() == doc_colours[1]
    T.set_colorblind_palette(True)
    themed(T.DARK)
    assert [C.display_layer_color(i).name() for i in range(len(doc_colours))] == T.COLORBLIND_LAYERS[T.DARK]
    assert C.swatch_layer_color(1).name() == T.COLORBLIND_LAYERS[T.DARK][1]
    assert C.LAYER_COLORS == doc_colours and C.layer_color(1).name() == doc_colours[1]   # the data
    themed(T.LIGHT)                                                   # follows the theme
    assert C.display_layer_color(4).name() == T.COLORBLIND_LAYERS[T.LIGHT][4]
    T.set_colorblind_palette(False)
    themed(T.LIGHT)
    assert C.display_layer_color(4).name() == T.layer_colors(T.LIGHT)[4]
    assert C.swatch_layer_color(4).name() == doc_colours[4]


@pytest.fixture
def win(app, tmp_path, monkeypatch, themed):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import real_window
    w = real_window(user_mode="engineer")
    yield w
    d = getattr(w, "_settings_window", None)
    if d is not None:
        d.close()
    w.close()


def test_the_setting_is_off_by_default_and_applies_live(win, app):
    from mlaser.ui import canvas as C
    from mlaser.services import settings_catalog as SC
    from mlaser.ui.settings_dialog import SettingsDialog
    from test_large_drawing import big_document
    assert win.config.get("shell.colorblind_palette") is False and not T.colorblind_palette()
    assert any(e.key == "shell.colorblind_palette" and e.page == "appearance" for e in SC.ENTRIES)
    doc = big_document(4)
    for k, p in enumerate(doc.parts):
        p.craft.layer = k + 1
    win.set_document(doc)
    app.processEvents()
    stored = {li: lp.color for li, lp in win.document.layers.items()}
    assert stored and all(c == C.layer_color(li).name() for li, c in stored.items())
    d = SettingsDialog(win, instant=True)
    d.show()
    d.show_page("appearance")
    app.processEvents()
    box = d.rows["shell.colorblind_palette"].editor
    assert not box.isChecked()
    box.setChecked(True)
    app.processEvents()
    eff = T.effective(win.shell.theme, app)
    assert T.colorblind_palette() and win.config.get("shell.colorblind_palette") is True
    assert C.display_layer_color(0).name() == T.COLORBLIND_LAYERS[eff][0]
    lp = win.layer_panel
    assert lp.color_buttons[2].icon().pixmap(16, 16).toImage().pixelColor(8, 8).name() == T.COLORBLIND_LAYERS[eff][2]
    assert {li: p.color for li, p in win.document.layers.items()} == stored        # the document's
    box.setChecked(False)
    app.processEvents()
    assert not T.colorblind_palette()
    assert C.display_layer_color(0).name() == T.layer_colors(eff)[0]
    assert lp.color_buttons[2].icon().pixmap(16, 16).toImage().pixelColor(8, 8).name() == C.LAYER_COLORS[2]
    d.close()


def test_appearance_page_fits_at_1280x800_and_12pt(win, app):
    """The new setting fits the Appearance page without scrolling at the smallest supported
    screen and a 12 pt font."""
    from PySide6.QtGui import QFont
    from mlaser.ui.settings_dialog import SettingsDialog
    d = SettingsDialog(win, instant=True)
    f = QFont(d.font())
    f.setPointSizeF(12.0)
    d.setFont(f)
    d.resize(min(1280, max(d.minimumWidth(), 900)), 800 - 80)
    d.show()
    d.show_page("appearance")
    for _ in range(5):
        app.processEvents()
    sc = d.stack.currentWidget()
    assert "shell.colorblind_palette" in {r.key for r in d.current_page().rows}
    assert not sc.verticalScrollBar().isVisible() and sc.verticalScrollBar().maximum() == 0
    d.close()
