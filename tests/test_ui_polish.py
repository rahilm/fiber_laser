"""The UI deep review's polish findings (2026-11-09, ui.md 4, 11, 12, 13; fw.md F12): one section
header look, the workspace tab look and the table recipe on Run and Process, the readout tiles
with their units muted, the Setup tree's label column capped, the control heights of Nest and
Report from the style helpers, status icons instead of font glyphs, the Properties empty state,
and no unused responsive helper."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QLabel, QMessageBox, QPushButton,  # noqa: E402
                               QTabBar, QWidget)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch, tmp_path):
    from wiring import dispose, real_window
    for k, sub in (("MLASER_CONFIG_DIR", "cfg"), ("MLASER_APP_DIR", "app"), ("MLASER_DATA_DIR", "data")):
        monkeypatch.setenv(k, str(tmp_path / sub))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window()
    w.resize(1280, 800)
    w.show()
    app.processEvents()
    yield w
    dispose(w)


def settle(app, sec: float = 0.3) -> None:
    loop = QEventLoop()
    QTimer.singleShot(int(sec * 1000), loop.quit)
    loop.exec()
    for _ in range(5):
        app.processEvents()


def _right_edge(w: QWidget) -> QColor:
    """The colour at the right end of a widget's middle row (past any text)."""
    img = w.grab().toImage()
    return QColor(img.pixel(img.width() - 3, img.height() // 2))


# ------------------------------------------------------------------ finding 4: section header
def test_section_header_is_flat_and_the_same_open_or_folded(app):
    from mlaser.ui import style
    from mlaser.ui.responsive import CollapsibleSection
    from mlaser.ui.techform import Section
    host = QWidget()
    host.setAutoFillBackground(True)
    v = style.vbox(host, "panel")                 # away from the pointer (no hover look)
    body = QLabel("body")
    plain = CollapsibleSection("Jog", body)
    fitted = Section("leads", "Leads", QLabel("body"))
    v.addWidget(plain)
    v.addWidget(fitted)
    host.resize(400, 200)
    host.move(400, 400)
    host.show()
    settle(app, 0.1)
    window = host.palette().color(QPalette.Window)
    for sec in (plain, fitted):
        assert sec.frame.frameShape() == QFrame.NoFrame                 # no box around the body
        assert sec.header.property("type_role") == "label_strong"
        # open (a checked tool button: it drew a grey box, the header changed shape with its
        # state) and folded: the header's background is the panel's
        assert sec.expanded() and sec.header.isChecked()             # the state stays for a11y
        assert _right_edge(sec.header) == window, sec.title()
        sec.set_expanded(False)
        settle(app, 0.05)
        assert _right_edge(sec.header) == window, sec.title()
        assert sec.header.arrowType() == Qt.RightArrow
    # style.section's collapsible body is inset once (by the section, not twice)
    s, lay = style.section("Inset", collapsible=True)
    assert lay.contentsMargins().left() == 0 and s.frame.layout().contentsMargins().left() == style.SP2
    host.close()


def test_compact_when_narrow_is_gone():
    """fw F12: the unused class is deleted; the two forms are kept with one job each (the module
    docstring says which)."""
    from mlaser.ui import responsive
    assert not hasattr(responsive, "CompactWhenNarrow")
    assert "CompactWhenNarrow" not in responsive.__all__
    assert "style.form()" in responsive.__doc__ and "dialogs" in responsive.__doc__


# ------------------------------------------------------------------ the Run workspace
def _on_run(win, app, size=(1280, 800), tab=0):
    win.resize(*size)
    win.commands.run("workspace.run")
    settle(app)
    scr = win.extension("run").screen
    scr.tabs.setCurrentIndex(tab)
    scr.refresh()
    settle(app, 0.1)
    return scr


def test_run_tabs_and_queue_table_go_through_the_style_helpers(win, app):
    from mlaser.ui import style
    scr = _on_run(win, app, tab=1)
    assert scr.tabs.property("tab_kind") == "workspace"
    assert style.TARGET_TOUCH <= scr.tabs.tabBar().height() <= style.TARGET_TOUCH + 2   # was 48 px
    t = scr.queue_panel.table
    assert t.property("table_look")
    assert t.horizontalHeader().defaultAlignment() & Qt.AlignLeft
    for c in scr.queue_panel.NUMERIC:                                  # #, Qty, Done
        assert t.horizontalHeaderItem(c).textAlignment() & Qt.AlignRight
    assert t.alternatingRowColors()                                    # ten columns
    assert t.verticalHeader().defaultSectionSize() >= style.TARGET_TOUCH   # a row stays a touch target
    empty = t.viewport().findChild(QWidget, "table_empty")
    assert empty is not None and empty.isVisible() and empty.text()


def test_readout_tiles_mute_the_unit_and_never_split(win, app):
    from mlaser.ui import style
    scr = _on_run(win, app, (756, 945))
    scr.timer.stop()                              # the card's values would replace the test's
    v = scr.values
    v.set_values({"speed": ("44.7", "mm/s"), "power": ("60", "%"), "gas": ("O2 8.0", "bar"),
                  "height": ("1.00", "mm"), "x": ("1234.56", "mm"), "y": ("0.00", "mm"), "z": ("0.00", "mm")})
    settle(app, 0.1)
    assert "Gas O2 8.0 bar" in v.text() and "Speed 44.7 mm/s" in v.text()
    muted = style.color("muted").name()
    for key, val in v.fields.items():
        unit = v.units[key]
        assert unit.isVisible() and unit.parentWidget() is val.parentWidget()     # one tile
        assert unit.styleSheet().replace(" ", "").endswith(f"color:{muted};"), key
        assert val.property("type_role") == "label_strong" and unit.property("type_role") != "label_strong"
        # value and unit on one line: the unit sits right of the value, never under it
        assert unit.y() < val.y() + val.height() and unit.x() >= val.x() + val.width(), key
    # a value with more digits does not shrink back: the tile keeps its width
    w0 = v.fields["x"].minimumWidth()
    v.set_values({"x": ("1.00", "mm")})
    assert v.fields["x"].minimumWidth() == w0
    v.set_values({"power": ("-", "")})
    assert not v.units["power"].isVisible()


def test_readiness_notice_one_line_when_short(win, app, monkeypatch):
    from mlaser.services.readiness import ReadinessItem
    su = win.safety_ui
    item = ReadinessItem("technology", False, "Technology", "1 layer has no technology (layer 0)",
                         "workspace.process")
    monkeypatch.setattr(su, "readiness", lambda: [item])
    scr = _on_run(win, app)
    scr._ready_tick = 0
    scr.refresh()
    settle(app, 0.1)
    lab = scr.ready_notices["technology"].label
    assert lab.heightForWidth(lab.width()) < 2 * lab.fontMetrics().height(), lab.size()     # was three lines


# ------------------------------------------------------------------ Process: Drawing | Library
def test_process_pages_are_workspace_tabs(win, app):
    win.show_workspace("process")
    settle(app)
    pw = win.extension("ext_process").workspace
    bar = next(b for b in win.findChildren(QTabBar) if b.objectName() == "process_pages")
    assert bar is pw.page_tabs and bar.property("tab_kind") == "workspace" and bar.count() == 2
    assert bar.property("responsive_views")                     # the layout harness visits it
    bar.setCurrentIndex(1)
    settle(app, 0.1)
    assert pw.pages.currentIndex() == 1
    pw.show_page(0)
    assert bar.currentIndex() == 0 and pw.pages.currentIndex() == 0


# ------------------------------------------------------------------ Machine › Setup
def test_setup_tree_label_column_is_capped(win, app):
    win.resize(1800, 1000)
    win.show_workspace("machine")
    settle(app)
    m = win.extension("ext_machine")
    m = m[0] if isinstance(m, tuple) else m
    m.show_setup()
    settle(app)
    setup = m.workspace.settings_panel
    tree = setup.tree
    settle(app, 0.2)
    h = tree.header()
    cap = tree.sizeHintForColumn(0) + 24
    assert tree.viewport().width() > cap + h.sectionSize(1) + h.sectionSize(2)
    assert h.sectionSize(0) <= cap, (h.sectionSize(0), cap)        # the values next to their names
    win.resize(683, 800)
    settle(app, 0.3)
    assert h.sectionSize(0) <= max(cap, tree.viewport().width())   # narrow: it stretches again


# ------------------------------------------------------------------ control heights
def test_nest_and_report_buttons_take_their_height_from_the_style(win, app):
    from mlaser.ui import style
    for ws in ("nest", "report"):
        win.show_workspace(ws)
        settle(app)
        page = win.findChild(QWidget, f"{ws}_workspace") or win.shell.host.pages[ws]
        sizes = {b.property("control_size") for b in page.findChildren(QPushButton) if b.isVisibleTo(page)}
        assert sizes == {"touch"}, (ws, sizes)
        heights = {b.minimumHeight() for b in page.findChildren(QPushButton) if b.isVisibleTo(page)}
        assert heights == {style.TARGET_TOUCH}, (ws, heights)


# ------------------------------------------------------------------ finding 12: icons
def test_status_lines_show_the_warning_icon_not_a_glyph(app):
    from mlaser.ui import style
    lab = style.IconLabel()
    lab.setWordWrap(True)
    style.set_status(lab, style.warning_text("The pulse did not run."))
    assert lab.text() == "The pulse did not run." and lab.icon_name() == "alert-triangle"
    assert lab.indent() >= style.ICON_S
    style.set_status(lab, "All good.")
    assert lab.icon_name() == "" and lab.indent() == -1
    from mlaser.ui import icons
    assert icons.has_icon("player-skip-back") and "player-skip-back" in icons.used_icon_names()


def test_commissioning_rows_carry_status_icons(app):
    from mlaser.services.procedures import commissioning_live as CL
    from mlaser.ui.wizards.commissioning_live import STATUS
    from mlaser.ui import icons
    for st, (ico, _key, _role) in STATUS.items():
        assert ico in icons.STATUS_ICONS, st
    assert set(STATUS) == {CL.PASS, CL.WARN, CL.FAIL, CL.SKIP, CL.PENDING}


# ------------------------------------------------------------------ finding 13: Properties
def test_properties_empty_state(win, app):
    from mlaser.ops import demo_document
    win.set_document(demo_document())
    win.show_workspace("design")
    settle(app)
    win.canvas.select_parts([])
    settle(app, 0.1)
    from mlaser.ui.properties.panel import PropertiesPanel
    panel = win.findChild(PropertiesPanel)
    assert panel is not None
    t0 = time.monotonic()
    while panel.summary.icon_name() == "" and time.monotonic() - t0 < 5:
        settle(app, 0.05)
    assert panel.summary.icon_name() and panel.summary.property("empty_state")
    win.canvas.select_parts([0])
    settle(app, 0.1)
    panel.refresh()
    assert panel.summary.icon_name() == "" and not panel.summary.property("empty_state")
