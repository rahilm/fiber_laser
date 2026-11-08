"""UX audit, workflow and visual findings (W7, W16, O1, O3, O4, O5, O11, O12, O14, O15):

* W7: a layer without a technology shows *Choose technology…* first (open, larger title, *Use
  for layer N*) and folds the value sections until one is chosen; the layer table shows its
  rows (up to four) in full; the Library assigns the selected record to a layer;
* W16: on Fusion the field / check-box boundaries reach 3:1 in every theme (a proxy style that
  stays out of the platforms' own styles);
* O1: the empty canvas offers Open / Import / the sample / practice, and goes with a drawing;
* O3: the welcome sheet's *Connect your machine* path to the commissioning checklist, with its
  Service-mode requirement said in place and the switch offered;
* O4: the commissioning step list shows each step's real status; Back says why it is off;
  homing can be left for later (and the jog checks that need it are skipped, not run);
* O5: Nest's labelled sheet fields, plural texts, one utilisation rounding;
* O11: the Run screen's selected tab is marked by more than a colour shade;
* O12: process fields without trailing zeros ("100 mm/s"), every decimal while editing;
* O14: Properties with nothing selected: the hint and the drawing's extent, no dummy values;
* O15: the plugin log message names the real menu path.
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mlaser import ops

pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def pump(app, s: float = 0.05) -> None:
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))   # own technology store
    from wiring import real_window

    w = real_window()
    w.resize(1400, 900)
    w.show()
    pump(app, 0.1)
    yield w
    w._test_no_dialogs = True
    w.close()


def _undefined_doc(n_layers: int = 1):
    """The demo drawing with its layer definitions removed: every layer is a placeholder
    (no technology), as after importing a DXF."""
    doc = ops.demo_document()
    for i, p in enumerate(doc.parts):
        p.craft.layer = i % n_layers
    doc.layers.clear()
    return doc


def _process(w, app):
    w.show_workspace("process")
    pump(app, 0.1)
    return w.extension("ext_process").workspace


# --------------------------------------------------------------------------- W7
def test_layer_without_technology_shows_choose_technology_first(win, app):
    win.set_document(_undefined_doc())
    ws = _process(win, app)
    insp = ws.inspector
    assert insp.layer == 0 and insp.choosing
    sec = insp.recipe_section
    assert sec.title() == "Choose technology…" and not sec.isHidden()
    others = [x for k, x in insp.form.sections.items() if k != "layer_recipe"]
    assert sec.priority > max(x.priority for x in others)                      # the last to fold
    assert sec.header.font().pointSizeF() > insp.title.font().pointSizeF()      # the larger title
    assert insp.form.section_order()[0] == "layer_recipe"                       # first
    for sid in ("cutting", "gas", "height", "piercing"):
        assert not insp.form.sections[sid].expanded(), sid                      # values folded
    assert insp.assign_btn.text() == "Use for layer 0"
    assert insp.action_buttons["layer.push"].isHidden()                         # no recipe to push to
    # a technology chosen: the picker's section is the Recipe again, the values fold automatically
    tech = insp.picker.picked()
    assert tech is not None
    insp.assign_picked()
    pump(app, 0.1)
    assert not insp.choosing and sec.title() == "Recipe" and insp.assign_btn.text() == "Assign recipe"
    assert sec.user_choice is None
    assert all(insp.form.sections[s].user_choice is None for s in ("cutting", "gas", "height"))


def test_choose_technology_keeps_a_section_the_user_opened(win, app):
    win.set_document(_undefined_doc())
    insp = _process(win, app).inspector
    gas = insp.form.sections["gas"]
    gas._user_toggle(True)                       # the user opens Gas and nozzle while choosing
    insp.assign_picked()
    pump(app, 0.05)
    assert gas.user_choice is True


def test_relayout_follows_a_priority_change(app):
    """The cause of the folded Recipe section (W7): the panel skipped the layout pass when only
    a section's priority changed."""
    from PySide6.QtWidgets import QLabel

    from mlaser.ui.responsive import CollapsibleSection, ColumnsPanel
    panel = ColumnsPanel(max_columns=1)
    a = panel.add_section(CollapsibleSection("a", QLabel("a\n" * 6), priority=10))
    b = panel.add_section(CollapsibleSection("b", QLabel("b\n" * 6), priority=50))
    panel.resize(200, a.height_for(196, True) + b.collapsed_height() + 30)
    panel.show()
    panel.relayout()
    assert b.expanded() and not a.expanded()
    a.priority, b.priority = 90, 10
    panel.relayout()
    assert a.expanded() and not b.expanded()
    panel.close()


def test_layer_table_shows_up_to_four_rows_in_full(win, app):
    win.set_document(_undefined_doc(4))
    ws = _process(win, app)
    tb = ws.layers
    assert tb.rowCount() == 4
    pump(app, 0.1)
    rows = sum(tb.rowHeight(r) for r in range(4))
    assert tb.minimumHeight() >= tb.horizontalHeader().sizeHint().height() + rows
    assert tb.wordWrap()
    for r in range(tb.rowCount()):
        assert tb.rowHeight(r) >= tb.sizeHintForRow(r)                          # the state is not cut


def test_library_uses_the_record_for_a_layer(win, app):
    win.set_document(_undefined_doc(2))
    ws = _process(win, app)
    lib = ws.library
    ws.select_layer(1)
    rec = ws.store.all()[0]
    lib.select(rec.key)
    pump(app, 0.05)
    assert lib.use_btn.isEnabled() and lib.use_btn.text() == "Use for layer 1"
    assert lib.assign_menu_btn.isEnabled() and lib.assign_menu_btn.text() == "Assign to layer…"
    lib._fill_assign_menu()
    labels = [a.text() for a in lib.assign_menu.actions() if a.text()]
    assert labels == ["Layer 0", "Layer 1", "All layers of the drawing"]
    ws.show_page(1)
    assert lib.use_for_current()
    pump(app, 0.05)
    from mlaser.services import technology as T
    assert (T.layer_technology_ref(win.document.layers[1]) or {}).get("id") == rec.id
    assert ws.pages.currentIndex() == 0 and ws.current_layer == 1               # back on the drawing
    assert not ws.inspector.choosing
    # Assign to layer…: every layer of the drawing at once
    assert lib.assign_to([0, 1])
    assert (T.layer_technology_ref(win.document.layers[0]) or {}).get("id") == rec.id


def test_library_assign_says_why_without_a_drawing(win, app):
    from mlaser.model import Document
    win.set_document(Document())
    ws = _process(win, app)
    lib = ws.library
    lib.select(ws.store.all()[0].key)
    lib.refresh_assign()
    assert not lib.use_btn.isEnabled() and not lib.assign_menu_btn.isEnabled()
    assert "Open a drawing first" in lib.use_btn.toolTip()


# --------------------------------------------------------------------------- W16
@pytest.mark.parametrize("theme_id", ["light", "dark", "high_contrast"])
def test_boundary_colour_reaches_3_to_1(app, theme_id):
    from mlaser.ui.shell import theme
    from mlaser.ui.shell.contrast import contrast
    t = theme.tokens(theme_id)
    col = theme.boundary_color(theme.palette(theme_id), theme_id).name()
    assert contrast(col, t["base"]) >= 3.0 and contrast(col, t["window"]) >= 3.0, (theme_id, col)


def test_boundary_colour_for_a_platform_palette(app):
    """A palette the tokens do not know (the desktop's): a colour that still reaches 3:1."""
    from PySide6.QtGui import QColor, QPalette

    from mlaser.ui.shell import theme
    from mlaser.ui.shell.contrast import contrast
    p = QPalette()
    for role, c in ((QPalette.Base, "#2a2a2a"), (QPalette.Window, "#303030"), (QPalette.Text, "#f0f0f0")):
        p.setColor(role, QColor(c))
    col = theme.boundary_color(p, "dark_unknown").name()
    assert contrast(col, "#2a2a2a") >= 3.0 and contrast(col, "#303030") >= 3.0


def test_fusion_draws_visible_field_boundaries(app):
    """The dark theme on Fusion: the line edit's edge reaches 3:1 against its fill (it was
    1.1:1), and the proxy still calls itself Fusion; a platform style gets no proxy."""
    from PySide6.QtWidgets import QApplication, QCheckBox, QLineEdit, QSpinBox, QVBoxLayout, QWidget

    from mlaser.ui.shell import theme
    from mlaser.ui.shell.contrast import contrast
    qa = QApplication.instance()
    old_style, old_native = qa.style().name(), qa.property("mlaser_native_style")
    old_pal = qa.palette()
    try:
        qa.setProperty("mlaser_native_style", "Fusion")
        qa.setProperty("mlaser_theme", None)
        theme.apply("dark")
        assert isinstance(qa.style(), theme.FusionBoundaries) and qa.style().name() == "fusion"
        qa.setPalette(theme.palette("dark"))
        w = QWidget()
        lay = QVBoxLayout(w)
        le, cb, sb = QLineEdit("abc"), QCheckBox("x"), QSpinBox()
        for x in (le, cb, sb):
            lay.addWidget(x)
        w.resize(200, 140)
        w.show()
        pump(app, 0.05)
        img = w.grab().toImage()
        base = theme.tokens("dark")["base"]

        def edge_contrast(widget) -> float:
            g = widget.geometry()
            y = g.center().y()
            return max(contrast(img.pixelColor(x, y).name(), base) for x in range(g.x(), g.x() + 3))
        assert edge_contrast(le) >= 3.0
        assert edge_contrast(sb) >= 3.0
        w.close()
        # a platform style (stands in for macos / windows11): no proxy, nothing drawn over it
        qa.setProperty("mlaser_native_style", "Windows")
        qa.setProperty("mlaser_theme", None)
        theme.apply("light")
        assert not isinstance(qa.style(), theme.FusionBoundaries) and qa.style().name().lower() == "windows"
    finally:
        qa.setProperty("mlaser_native_style", old_native)
        qa.setProperty("mlaser_theme", None)
        qa.setStyle(old_style)
        qa.setPalette(old_pal)


# --------------------------------------------------------------------------- O1
def test_empty_canvas_offers_the_ways_to_a_drawing(win, app):
    from PySide6.QtCore import Qt

    from mlaser.model import Document
    win.set_document(Document())
    win.show_workspace("design")
    pump(app, 0.1)
    e = win.canvas_empty
    assert e.isVisible()
    texts = [b.text() for b in e.buttons.values() if b.isVisible()]
    assert texts == ["Open drawing…", "Import DXF/SVG…", "Try the sample", "Practice on the simulator"]
    assert all(b.focusPolicy() & Qt.TabFocus for b in e.buttons.values())
    g, c = e.geometry(), win.canvas.rect()
    assert c.contains(g) and abs(g.center().x() - c.center().x()) <= 2               # centred
    e.trigger("sample")
    pump(app, 0.1)
    assert win.document.parts and not e.isVisible()
    win.set_document(Document())
    pump(app, 0.05)
    assert e.isVisible()


def test_empty_canvas_buttons_run_the_commands(win, app):
    from mlaser.model import Document
    win.set_document(Document())
    ran = []
    win.canvas_empty._run = ran.append
    for cmd in ("file.open", "file.import", "practice.open"):
        win.canvas_empty.buttons[cmd].click()
    assert ran == ["file.open", "file.import", "practice.open"]


# --------------------------------------------------------------------------- O3
def test_welcome_sheet_connect_your_machine_path(win, app):
    from mlaser.ui.shell.dialogs import WelcomeSheet
    assert win.set_user_mode("engineer")
    d = WelcomeSheet(win.shell)
    b = d.actions["wizard.commissioning"]
    assert b.text() == "Switch to Service mode and open the checklist"
    assert not d.mode_note.isHidden() and "Service mode" in d.mode_note.text() and "Engineer" in d.mode_note.text()
    ran = []
    win.run = ran.append
    assert d.open_commissioning()                       # no PIN set: switches, then opens it
    assert win.shell.modes.mode == "service" and ran == ["wizard.commissioning"]
    assert d.mode_note.isHidden() and b.text() == "Open the commissioning checklist"
    d.close()


def test_welcome_sheet_commissioning_keeps_the_mode_on_a_refused_pin(win, app, monkeypatch):
    from mlaser.ui.shell.dialogs import WelcomeSheet
    assert win.set_user_mode("engineer")
    monkeypatch.setattr(win.shell, "request_mode", lambda target: False)
    d = WelcomeSheet(win.shell)
    ran = []
    win.run = ran.append
    assert not d.open_commissioning()
    assert win.shell.modes.mode == "engineer" and ran == []
    d.close()


def test_machine_command_offers_the_mode_switch(win, app, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    ext = win.extension("ext_machine")
    assert win.set_user_mode("engineer")
    seen = {}

    def fake_exec(box):
        seen["text"] = box.text()
        seen["buttons"] = [b.text() for b in box.buttons()]
        box_btn = [b for b in box.buttons() if b.text().startswith("Switch to")][0]
        box_btn.click()
        return 0
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    win._test_no_dialogs = False
    try:
        assert ext.offer_mode("wizard.commissioning", "service")
    finally:
        win._test_no_dialogs = True
    assert "needs Service mode" in seen["text"] and "Switch to Service mode" in seen["buttons"]
    assert win.shell.modes.mode == "service"


# --------------------------------------------------------------------------- O4
class _Machine:
    def __init__(self):
        self.halts = []

    def safe_halt(self, reason=""):
        self.halts.append(reason)

    def operator_action(self, what=""):
        pass


def test_step_status_is_the_real_status():
    from mlaser.services.procedures import engine as PE

    class P(PE.Procedure):
        def build_steps(self):
            return [PE.Step("intro", "t", kind="info"),
                    PE.Step("act", "t", kind="action", run=lambda p: 1),
                    PE.Step("later", "t", kind="action", run=lambda p: 1, skippable=True),
                    PE.Step("q", "t", kind="question"),
                    PE.Step("end", "t", kind="info")]
    p = P(_Machine())
    p.start()
    st = {s.id: p.step_status(s) for s in p.steps}
    assert st == {"intro": "todo", "act": "todo", "later": "todo", "q": "todo", "end": "todo"}
    assert p.why_not_back() == "wizard.back_why.first"
    p.next()
    p.execute()
    assert p.why_not_back() == "wizard.back_why.action"
    p.next()
    assert p.can_skip() and p.skip_current()
    assert not p.halts                                   # nothing ran, nothing to halt
    st = {s.id: p.step_status(s) for s in p.steps}
    assert st["intro"] == "done" and st["act"] == "pass" and st["later"] == "skipped" and st["q"] == "todo"
    assert not p.can_skip()                              # a question is not skippable


def test_commissioning_skip_homing_skips_the_jog_checks(tmp_path):
    from mlaser.services.procedures import commissioning as CM
    from mlaser.services.procedures import engine as PE
    from mlaser.services.procedures import simrig

    from test_procedures import _netcheck_ok
    ctl = simrig.simulator_controller("card", heartbeat_output=7)
    try:
        p = CM.CommissioningProcedure(ctl, report_dir=tmp_path, netcheck_fn=_netcheck_ok, relay_window_s=0.2)
        p.start()
        for _ in range(30):
            s = p.step
            if s.id == "homing":
                break
            if s.kind == "action" and s.id not in p.done:
                assert p.execute()
                continue
            p.next()
        assert p.step.id == "homing" and p.can_skip()
        homed_before = ctl.state().homed
        assert p.skip_current()
        assert ctl.state().homed == homed_before                      # nothing moved
        assert p.items["homing"].status == CM.SKIP
        assert p.items["jog_x"].status == CM.SKIP and p.items["jog_y"].status == CM.SKIP
        ids = [s.id for s in p.visible_steps()]
        assert "jog_x" not in ids and "jog_y_dir" not in ids and "homing" in ids
        st = {s.id: p.step_status(s) for s in p.visible_steps()}
        assert st["homing"] == "skipped" and st["soft_limits"] == "warn" and st["network"] == "pass"
        assert st["summary"] == "todo"
        while p.state not in PE.FINAL:
            s = p.step
            if s.kind == "action" and s.id not in p.done:
                p.execute()
                continue
            if s.kind == "question":
                p.answer(True)
            p.next()
        assert p.state == PE.DONE
        assert not any(h.step.startswith("jog_") and h.outcome == "done" for h in p.history)
    finally:
        simrig.close(ctl)


def test_wizard_step_list_marks_and_back_reason(win, app):
    from mlaser.services.procedures import engine as PE
    from mlaser.ui.wizards.procedures import CommissioningWizard
    d = CommissioningWizard(win, win.extension("ext_machine"))
    d.dry.setChecked(True)
    pump(app, 0.05)
    assert not d.b_back.isEnabled() and d.b_back.toolTip() == "This is the first step."
    first = d.steps_list.item(0)
    assert first.text().startswith("▶") and first.toolTip() == "To do"
    assert d.steps_list.item(1).text().startswith("…")
    from test_machine_workspace import drive
    drive(app, d, until="homing")
    assert d.proc.step.id == "homing" and not d.b_skip.isHidden() and d.b_skip.text() == "Skip, do later"
    marks = {d.proc.visible_steps()[i].id: d.steps_list.item(i).text()[0] for i in range(d.steps_list.count())}
    assert marks["network"] == "✓" and marks["soft_limits"] == "!" and marks["homing"] == "▶"
    assert "An action" not in d.b_back.toolTip() and d.b_back.toolTip().startswith("Back is not possible")
    d.b_skip.click()
    pump(app, 0.05)
    assert d.proc.items["homing"].status == "skip"
    marks = {d.proc.visible_steps()[i].id: d.steps_list.item(i).text()[0] for i in range(d.steps_list.count())}
    assert marks["homing"] == "–"
    d.abort()
    d.done(0)
    assert d.proc.state in PE.FINAL


# --------------------------------------------------------------------------- O5
def test_nest_plurals_and_one_utilisation_rounding(win, app):
    from mlaser.ui.workspaces import trn
    from mlaser.ui.workspaces.nest import util_pct
    assert trn(win, "nestws.msg.recognized", 1, k=1) == "1 part (1 different) added"
    assert trn(win, "nestws.msg.recognized", 3, k=2) == "3 parts (2 different) added"
    assert trn(win, "run.queue.title", 1, left=trn(win, "run.queue.left", 1)) == "Queue: 1 job, 1 sheet left"
    assert trn(win, "run.queue.title", 2, left=trn(win, "run.queue.left", 3)) == "Queue: 2 jobs, 3 sheets left"
    assert trn(win, "queue.totals", 1, done=0, sheets=1, left="-").startswith("1 job ·")
    assert util_pct(0.63449) == 63.4 and f"{util_pct(0.6345):.1f}" == f"{util_pct(0.6345):.1f}"
    import json
    from pathlib import Path
    cat = json.loads((Path(__file__).resolve().parents[1] / "mlaser" / "i18n" / "en.json").read_text("utf-8"))
    s = cat["strings"]
    assert "{util:.1f}" in s["nestws.result_item"] and "{util:.1f}" in s["nestws.sheet_title"]
    assert "{util:.1f}" in s["nestws.total"]


def test_nest_sheet_fields_have_visible_labels(win, app):
    from PySide6.QtWidgets import QLabel
    win.show_workspace("nest")
    pump(app, 0.1)
    ws = win.extension("nest")
    for field, text in ((ws.material, "Material"), (ws.thickness, "Thickness"), (ws.sheet_w, "Width"),
                        (ws.sheet_h, "Height"), (ws.sheet_n, "Count")):
        labels = [lb for lb in field.parentWidget().findChildren(QLabel) if lb.buddy() is field]
        assert labels and labels[0].text() == text, text
        assert labels[0].isVisible()
    assert not ws.material.placeholderText()             # a label, not a placeholder


# --------------------------------------------------------------------------- O11
@pytest.mark.parametrize("theme_id", ["light", "dark", "high_contrast"])
def test_run_tabs_mark_the_selected_tab(win, app, theme_id):
    from mlaser.ui.shell.contrast import contrast
    win.shell.set_theme(theme_id, persist=False)
    win.show_workspace("run")
    pump(app, 0.1)
    from PySide6.QtWidgets import QTabWidget
    tabs = [t for t in win.findChildren(QTabWidget) if t.objectName() == "run_tabs"][0]
    css = tabs.styleSheet()
    assert "QTabBar::tab:selected" in css and "palette(highlight)" in css and "font-weight:bold" in css
    bar = tabs.tabBar()
    tabs.setCurrentIndex(0)
    pump(app, 0.05)
    img = bar.grab().toImage()
    r = bar.tabRect(0)
    hl = bar.palette().color(bar.palette().ColorRole.Highlight).name()
    bottom = img.pixelColor(r.center().x(), r.bottom() - 1).name()
    other = bar.tabRect(1)
    bottom_other = img.pixelColor(other.center().x(), other.bottom() - 1).name()
    assert bottom == hl and bottom_other != hl
    assert contrast(bottom, bar.palette().color(bar.palette().ColorRole.Window).name()) >= 3.0
    win.shell.set_theme("light", persist=False)


# --------------------------------------------------------------------------- O12
def test_process_fields_without_trailing_zeros(app):
    from PySide6.QtWidgets import QWidget

    from mlaser.ui.units_qt import QuantitySpinBox
    host = QWidget()
    s = QuantitySpinBox("speed", host, minimum=0, maximum=1e5, decimals=3)
    s.setTrimZeros(True)
    s.setValue(100.0)
    assert s.text() == "100 mm/s"
    s.setValue(0.125)
    assert s.text() == "0.125 mm/s"
    p = QuantitySpinBox("pressure", host, minimum=0, maximum=30, decimals=3)
    p.setTrimZeros(True)
    assert p.text() == "0 bar"
    host.show()
    s.setValue(100.0)
    s.setFocus()
    pump(app, 0.05)
    if s.hasFocus():
        assert s.text() == "100.000 mm/s"                 # every decimal while editing
    s.clearFocus()
    pump(app, 0.05)
    assert s.text() == "100 mm/s" and s.value() == 100.0
    host.close()


def test_techform_fields_trim(win, app):
    win.set_document(_undefined_doc())
    insp = _process(win, app).inspector
    f = insp.form.fields["GP.CutSpeed"].widget
    assert f.trimZeros() and f.text() == "100 mm/s"


# --------------------------------------------------------------------------- O14
def test_properties_with_nothing_selected(win, app):
    win.set_document(ops.demo_document())
    ext = win.extension("ext_process")
    pp = ext.properties
    win.canvas.select_parts([])
    pp.refresh()
    pump(app, 0.05)
    assert all(g.isHidden() for g in pp.sections)                       # no dummy values
    txt = pp.summary.text()
    assert txt.startswith("Nothing selected") and "The drawing:" in txt and "parts" in txt
    win.canvas.select_parts([0])
    pump(app, 0.05)
    assert not pp.by_id["transform"].isHidden() and not pp.by_id["layer"].isHidden()


# --------------------------------------------------------------------------- O15
def test_plugin_message_names_the_real_menu(win):
    path = win.plugins_menu_path()
    assert "Options+" not in path
    from mlaser.ui import style
    if style.is_mac():
        assert path.endswith("Plugins…")
    else:
        assert path == "Tools › Plugins"
    text = win.tr_.format("main.plugin.not_enabled", names="onshape", where=path)
    assert "Options+" not in text and path in text
