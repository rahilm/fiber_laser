"""The panels fit (docs/dev/RESPONSIVE.md, ui_wave_brief "no scrolling in forms"), and the last
one-home items (SETTINGS_MAP.md, WORK_ITEMS P-5, P-8, P-9, R-5):

* the Properties dock folds its sections with summary lines instead of scrolling; Design shows
  Transform and Layer only; a multi-contour part's contour gets its own lead / kerf;
* the technology form folds too; *Show all parameters* / a search make it a list;
* the cutting-strategy rules are recipe rules (Library), the panel shows them read-only; the bed
  is Machine › Setup's; the Nest's kerf and tip strategy are derived and read-only;
* the Library page takes the canvas's width; the canvas HUD's buttons get their full size.
"""
from __future__ import annotations

import time
from dataclasses import replace

import pytest
from PySide6.QtWidgets import QAbstractSpinBox, QComboBox, QLineEdit, QWidget

from mlaser import ops

pytestmark = pytest.mark.xdist_group("panels_fit")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def pump(app, s: float = 0.03) -> None:
    t0 = time.monotonic()
    while True:
        app.processEvents()
        if time.monotonic() - t0 >= s:
            break
        time.sleep(0.003)


@pytest.fixture(scope="module")
def win(app, tmp_path_factory):
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("panels_fit")
    for k, sub in (("MLASER_CONFIG_DIR", "cfg"), ("MLASER_APP_DIR", "app"), ("MLASER_DATA_DIR", "data")):
        mp.setenv(k, str(root / sub))
    mp.setenv("MLASER_PLAN_PROCESS", "0")
    from wiring import dispose, real_window

    w = real_window()
    w.resize(1280, 800)
    w.show()
    pump(app, 0.1)
    yield w
    dispose(w)
    mp.undo()


@pytest.fixture
def doc(win, app):
    win.set_document(ops.demo_document())
    win.canvas.select_parts([0])
    pump(app)
    return win.document


def props(win):
    return win.extension("ext_process").properties


# ----------------------------------------------------------------------------- Properties
def test_design_shows_transform_and_layer_only(win, app, doc):
    pp = props(win)
    win.shell.host.activate("design")
    pump(app)
    assert pp.mode == "design"
    shown = {s.pid for s in pp.sections if not s.isHidden()}
    assert shown == {"transform", "layer"}
    assert not pp.design_box.isHidden() and pp.design_link.vid.startswith("craft.")
    assert pp.contour_row.isHidden()                       # per-contour values are CAM: Process
    win.shell.host.activate("process")
    pump(app)
    assert pp.mode == "process" and all(not s.isHidden() for s in pp.sections)
    assert pp.design_box.isHidden()


def test_properties_fold_instead_of_scrolling(win, app, doc):
    pp = props(win)
    win.shell.host.activate("process")
    win.resize(1280, 800)
    pump(app, 0.1)
    assert not pp.verticalScrollBar().isVisible()
    assert not pp.horizontalScrollBar().isVisible()
    by = pp.by_id
    assert by["leads"].expanded() and by["kerf"].expanded()     # the most used CAM settings
    folded = [s for s in pp.sections if not s.isHidden() and not s.expanded()]
    assert folded, "a 1280 x 800 window cannot show every section open"
    assert all(s.summary() for s in folded), [s.title() for s in folded if not s.summary()]
    # the summary is in the folded header's tooltip / accessible description
    s = folded[0]
    assert s.summary() in s.header.toolTip() and s.header.accessibleDescription() == s.summary()


def test_long_titles_are_cut_with_a_tooltip(app):
    from mlaser.ui.responsive import CollapsibleSection

    body = QWidget()
    s = CollapsibleSection("Start point (outer contour) with a long title", body, fit_title=True)
    s.resize(120, 200)
    s.show()
    pump(app)
    h = s.header
    assert h.text().endswith("…") and h.full_text() == s.title()
    assert h.sizeHint().width() <= h.width() + 1             # the shown text fits: not clipped
    assert s.title() in h.toolTip() and h.accessibleName() == s.title()
    s.resize(800, 200)
    pump(app)
    assert h.text() == s.title() and s.title() not in h.toolTip()
    s.close()


def test_lead_rows_follow_the_lead_type(win, app, doc):
    pp = props(win)
    win.shell.host.activate("process")
    pump(app)
    kind, ang, ln, rad = pp.lead_w["in"]
    rows = pp.lead_rows["in"]
    form = rows[0].parentWidget()
    for k, want in ((0, (False, False, False)), (1, (True, True, False)), (3, (True, True, True))):
        kind.setCurrentIndex(k)
        pump(app)
        assert tuple(form.is_row_visible(r) for r in rows) == want, k
    kind.setCurrentIndex(0)
    pp.refresh()


def test_contour_picker_sets_a_contour_override(win, app, doc):
    from mlaser.cam.manual import contour_craft
    from mlaser.services import inheritance as I

    pp = props(win)
    win.shell.host.activate("process")
    pump(app)
    part = doc.parts[0]
    assert len(part.contours) > 1 and not pp.contour_row.isHidden()
    assert [pp.contour.itemData(i) for i in range(pp.contour.count())] == [None, *range(len(part.contours))]
    hole = next(ci for ci in range(len(part.contours)) if ci != pp._outer(part))
    pp.pick_contour(hole)
    pump(app)
    assert pp.contour_index() == hole
    before = (part.craft.lead_in.kind, part.craft.lead_in.length)
    kind, ang, ln, rad = pp.lead_w["in"]
    pp._updating = True
    kind.setCurrentIndex(1)
    ln.setValue(1.5)
    ang.setValue(45.0)
    pp._updating = False
    pp._commit_leads()
    part = win.document.parts[0]
    cr = contour_craft(part, hole)
    assert cr is not part.craft and (cr.lead_in.kind, cr.lead_in.length) == (1, 1.5)
    assert (part.craft.lead_in.kind, part.craft.lead_in.length) == before    # the part keeps its own
    assert "craft.lead_in.length" in I.craft_overrides(cr)
    assert I.effective(win.document, pp.store, "craft.lead_in.length", part=part, contour=hole) == (1.5, "contour")
    fld = pp.inherit["craft.lead_in.length"]
    assert fld.overridden and fld.reset_btn.isVisibleTo(fld)
    win.undo()                                                   # one undo step
    part = win.document.parts[0]
    assert I.effective(win.document, pp.store, "craft.lead_in.length", part=part, contour=hole)[1] != "contour"
    win.redo()
    # restore: the contour takes the part's value again
    pp._reset("craft.lead_in.length")
    part = win.document.parts[0]
    assert I.effective(win.document, pp.store, "craft.lead_in.length", part=part, contour=hole)[1] != "contour"
    pp.pick_contour(None)
    pump(app)
    assert pp.contour_index() is None


def test_properties_layer_link_goes_to_the_layer_inspector(win, app, doc):
    pp = props(win)
    win.shell.host.activate("process")
    pump(app)
    assert pp.layer_link.vid == "layer.GP.CutSpeed" and pp.layer_link.target == doc.parts[0].craft.layer
    assert pp.layer_link.property("mlaser_role") == "mirror"


# ----------------------------------------------------------------------------- the technology form
def test_techform_folds_and_lists(app):
    from mlaser.ui.i18n import Translator
    from mlaser.ui.techform import TechForm

    f = TechForm(Translator(language="en"))
    f.resize(360, 260)
    f.show()
    pump(app, 0.1)
    assert not f.scroll.is_list() and f.panel.fold
    assert not f.scroll.verticalScrollBar().isVisible()          # it folds instead
    assert any(not s.expanded() for s in f.sections.values() if not s.isHidden())
    assert all(s.summary() for s in f.sections.values() if not s.isHidden() and not s.expanded())
    f.show_all.setChecked(True)
    pump(app, 0.1)
    assert f.scroll.is_list() and not f.panel.fold               # every parameter: a list
    f.show_all.setChecked(False)
    f.search.setText("speed")
    pump(app, 0.1)
    assert f.scroll.is_list()
    f.search.clear()
    pump(app, 0.1)
    assert not f.scroll.is_list()
    f.close()


def test_library_takes_the_canvas_width(win, app, doc):
    win.shell.host.activate("process")
    pump(app)
    pw = win.extension("ext_process").workspace
    page = win.shell.host.pages["process"]
    pw.show_page(1)
    pump(app, 0.05)
    assert page.canvas_slot.isHidden()
    assert pw.width() > win.width() // 2
    pw.page_buttons.button(0).click()
    pump(app, 0.05)
    assert not page.canvas_slot.isHidden() and pw.pages.currentIndex() == 0


# ----------------------------------------------------------------------------- P-9, R-5
def _recipe_with_rules(win, rules: dict):
    store = win.extension("ext_process").store
    tech = store.all()[0]
    return store.save(replace(tech, rules={**(tech.rules or {}), **rules}), "test rules")


def test_cutting_strategy_rules_are_the_recipes(win, app, doc):
    ext = win.extension("ext_process")
    panel = ext.strategy
    # no input in the panel: the rules are shown, Check / Fix stay
    assert not [w for w in panel.findChildren(QWidget) if isinstance(w, (QAbstractSpinBox, QComboBox, QLineEdit))]
    tech = _recipe_with_rules(win, {"lead_place": {"prefer": "corner", "min_feature_mm": 3.0},
                                    "lead_check": {"clearance_mm": 0.8}, "tip": {"strategy": "last"}})
    li = doc.parts[0].craft.layer
    ext.workspace.inspector.assign(tech, [li])
    ext.workspace.select_layer(li)
    panel.refresh_bed()
    prm = panel.lead_params()
    assert (prm.rules.prefer, prm.rules.min_feature, prm.clearance) == ("corner", 3.0, 0.8)
    assert panel.tip_strategy() == "last"
    assert tech.key.material in panel.lead_rules.text() and panel.rules_link.target is not None
    assert panel.lead_rules.property("mlaser_role") == "mirror"
    assert panel.bed_label.property("mlaser_value") == "cfg:bed.slat_pitch"


def test_library_editor_holds_the_rules(win, app):
    ed = win.extension("ext_process").workspace.library.editor
    store = win.extension("ext_process").store
    tech = next(t for t in store.all() if not (t.rules or {}).get("tip"))
    ed.set_technology(tech)
    cur = ed.current()
    assert "tip" not in cur.rules and "lead_check" not in cur.rules     # defaults are not written
    ed.tip_strategy.setCurrentIndex(ed.tip_strategy.findData("avoid"))
    ed.prefer.setCurrentIndex(ed.prefer.findData("straight"))
    cur = ed.current()
    assert cur.rules["tip"]["strategy"] == "avoid" and cur.rules["lead_place"]["prefer"] == "straight"
    for w in (ed.prefer, ed.min_feature, ed.clearance, ed.tip_strategy):
        assert w.property("mlaser_role") == "editor" and w.property("mlaser_value").startswith("recipe.rules.")


def test_bed_is_edited_in_machine_setup(win):
    from mlaser.ui.workspaces.machine import setup_keys

    keys = setup_keys(win.config)
    assert {k for k, sec in keys.items() if sec == "bed"} >= {"bed.slat_pitch", "bed.slat_width",
                                                             "bed.slat_offset", "bed.slat_direction"}


def test_nest_kerf_and_tip_strategy_are_derived(win, app):
    from mlaser.model import Contour, Craft, Line, Part
    from mlaser.nest import NestPart

    ws = win.extension("nest")
    ws.job.parts.clear()
    pts = [(0, 0), (50, 0), (50, 30), (0, 30)]
    part = Part([Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)], Craft(layer=1, kerf_width=0.15))
    ws.job.add_part(NestPart([part], name="p", quantity=1))
    assert ws.cut_width() == (pytest.approx(0.3), 1)              # twice the compensation
    ws.common_line.setChecked(True)
    ws._apply_params()
    assert ws.job.params.kerf == pytest.approx(0.3) and ws.job.params.strategy_kerf == pytest.approx(0.3)
    ws.refresh_derived()
    assert "layer 1" in ws.kerf_info.text()
    assert ws.kerf_info.property("mlaser_role") == "mirror" and ws.tip_info.property("mlaser_role") == "mirror"
    assert ws.job.extra["tip_strategy"] == ws.tip_strategy()
    ws.job.parts.clear()
    ws._apply_params()
    assert ws.job.params.kerf == pytest.approx(0.2)             # no kerf: the default beam width


# ----------------------------------------------------------------------------- canvas HUD
def test_hud_buttons_get_their_size(win, app):
    hud = win.canvas.findChild(QWidget, "canvas_hud")
    for size in ((1280, 800), (1512, 875), (1920, 1080)):
        win.resize(*size)
        pump(app, 0.05)
        for b in (hud.fit_drawing, hud.fit_table):
            if b.isVisible():
                assert b.width() >= b.sizeHint().width(), (size, b.text(), b.width(), b.sizeHint())
    win.resize(1280, 800)


# ----------------------------------------------------------------------------- helpers
def test_columns_panel_keeps_the_newest_choice_open(app):
    from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout

    from mlaser.ui.responsive import CollapsibleSection, ColumnsPanel

    sa = QScrollArea()
    sa.setWidgetResizable(True)
    p = ColumnsPanel(max_columns=1, scroll_overflow=True)
    sa.setWidget(p)
    secs = []
    for i in range(3):
        body = QWidget()
        v = QVBoxLayout(body)
        for k in range(8):
            v.addWidget(QLabel(f"row {k}"))
        s = CollapsibleSection(f"S{i}", body, priority=i, fit_title=True)
        s.set_summary(f"summary {i}")
        p.add_section(s)
        secs.append(s)
    sa.resize(240, 140)
    sa.show()
    pump(app, 0.05)
    assert not sa.verticalScrollBar().isVisible()               # automatic sections fold
    secs[0].header.click()                                       # the lowest priority, opened by hand
    pump(app, 0.05)
    assert secs[0].expanded()                                    # stays open: the overflow scrolls
    secs[1].header.click()
    pump(app, 0.05)
    assert secs[1].expanded() and not secs[0].expanded()          # the newest choice wins
    sa.close()


def test_responsive_form_rows(app):
    from PySide6.QtWidgets import QLabel

    from mlaser.ui.responsive import ResponsiveForm

    f = ResponsiveForm()
    a, b, span = QLineEdit(), QLineEdit(), QLabel("a heading spanning both columns")
    f.addRow("First", a)
    f.addRow(span)
    f.addRow("Second", b)
    f.resize(400, 200)
    f.show()
    pump(app)
    assert f.is_row_visible(b)
    h0 = f.minimumSizeHint().height()
    f.set_row_visible(b, False)
    pump(app)
    assert not f.is_row_visible(b) and f.row_widgets(b)[0].isHidden()
    assert f.minimumSizeHint().height() < h0                     # a hidden row takes no room
    assert span.width() > a.width()                               # the heading spans
    f.close()


def test_docks_settle_at_the_window_minimum(win, app, doc):
    """At the window's minimum the Properties dock keeps one width (a compact / comfortable
    flip of its panel made it alternate by a pixel, and the canvas re-fitted on every turn)."""
    win.shell.host.activate("design")
    win.resize(800, 600)                      # below the minimum: the window takes its minimum
    pump(app, 0.2)
    dock = win.extension("ext_process").props_dock
    vp = win.canvas.viewport()
    seen = set()
    for _ in range(12):
        app.processEvents()
        seen.add((dock.width(), vp.width()))
    assert len(seen) == 1, seen
    win.resize(1280, 800)
    pump(app, 0.1)
