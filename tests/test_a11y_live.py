"""Accessibility behaviour of the real window (UI evaluation F7): announcements, the canvas
summary and part navigation, link colours after a live theme switch, the custom-painted views."""
from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QAccessible, QPalette
from PySide6.QtWidgets import QApplication

from wiring import dispose, real_window

pytestmark = pytest.mark.xdist_group("a11y_tree")


def _pump(sec: float = 0.2) -> None:
    t = time.time()
    while time.time() - t < sec:
        QCoreApplication.processEvents()
        time.sleep(0.01)


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    QApplication.instance() or QApplication([])
    from mlaser.ui import docops
    w = real_window()
    w.resize(1400, 900)
    w.show()
    w.set_document(docops.demo_document())
    _pump(0.4)
    yield w
    w.config.set("shell.theme", "light")
    _pump(0.1)
    dispose(w)


def _wcag(c1, c2) -> float:
    from mlaser.ui.shell.contrast import contrast
    return contrast(c1, c2)


# ------------------------------------------------------------------ announcements
def test_announce_once_per_change():
    QApplication.instance() or QApplication([])
    from PySide6.QtWidgets import QLabel
    from mlaser.ui import a11y
    lb = QLabel()
    n = len(a11y.last_announcements)
    assert a11y.announce(lb, "ALARM 8030: Emergency stop", assertive=True)
    assert not a11y.announce(lb, "ALARM 8030: Emergency stop", assertive=True)    # a refresh tick
    assert a11y.announce(lb, "Alarms cleared")
    assert list(a11y.last_announcements)[n:] == [("ALARM 8030: Emergency stop", True), ("Alarms cleared", False)]
    assert not a11y.announce(lb, "  ")


def test_alarm_and_job_transitions_are_announced(window):
    from mlaser.ui import a11y, safety_ui
    w = window
    ui = next(c for c in w.findChildren(safety_ui.SafetyUI))

    class _Alarm:
        code, kind = 8030, "estop"

        def text(self, tr=None):
            return "Emergency stop"
    n = len(a11y.last_announcements)
    alarms = [_Alarm()]
    type(w.ctl).active_alarms = property(lambda self: alarms)
    try:
        ui.refresh()
        ui.refresh()                        # the same alarm: spoken once
        said = list(a11y.last_announcements)[n:]
        assert len(said) == 1 and said[0][1] is True and "8030" in said[0][0] and "Reset" in said[0][0]
        assert "8030" in ui.alarm.accessibleName()
        alarms.clear()
        ui.refresh()
        assert list(a11y.last_announcements)[-1] == (w.tr_.tr("a11y.alarm.cleared"), False)
    finally:
        del type(w.ctl).active_alarms
    # job start / end
    m = len(a11y.last_announcements)
    orig = safety_ui.job_running
    try:
        safety_ui.job_running = lambda _w: True
        ui.refresh()
        ui.refresh()
        safety_ui.job_running = lambda _w: False
        ui.refresh()
    finally:
        safety_ui.job_running = orig
    said = [t for t, _a in list(a11y.last_announcements)[m:]]
    assert said == [w.tr_.tr("a11y.job.started"), w.tr_.tr("a11y.job.ended")]


# ------------------------------------------------------------------ canvas
def test_canvas_summary_and_keyboard_part_navigation(window):
    from mlaser.ui import a11y
    w = window
    w.show_workspace("design")
    _pump(0.2)
    cv = w.canvas
    n = len(w.document.parts)
    assert n >= 2
    iface = QAccessible.queryAccessibleInterface(cv)
    desc = iface.text(QAccessible.Text.Description)
    assert w.tr_.plural("a11y.canvas.summary", n, selected=0) in desc
    cv.setFocus()
    _pump(0.1)
    nav = w.canvas_a11y
    assert nav.step(1) == 0
    assert cv.selected_indices() == [0]
    assert nav.step(1) == 1 and cv.selected_indices() == [1]
    assert nav.step(-1) == 0
    assert nav.step(-1) == n - 1                 # wraps
    assert nav.step("first") == 0
    said = list(a11y.last_announcements)[-1][0]
    assert said.startswith(w.tr_.format("a11y.canvas.part_nosize", index=1, total=n, name="X")[:6])
    desc = QAccessible.queryAccessibleInterface(cv).text(QAccessible.Text.Description)
    assert "1 selected" in desc or w.tr_.plural("a11y.canvas.summary", n, selected=1) in desc
    # the keys reach the navigation while the canvas has the focus
    from PySide6.QtTest import QTest
    QTest.keyClick(cv, Qt.Key_PageDown)
    _pump(0.1)
    assert cv.selected_indices() == [1]


def test_canvas_summary_follows_document_edits(window):
    w = window
    from mlaser.ui import docops
    doc = docops.demo_document()
    doc.parts = doc.parts[:1]
    w.set_document(doc)
    _pump(0.2)
    desc = QAccessible.queryAccessibleInterface(w.canvas).text(QAccessible.Text.Description)
    assert w.tr_.plural("a11y.canvas.summary", 1, selected=0) in desc


# ------------------------------------------------------------------ links
def test_links_follow_a_live_light_to_dark_switch(window):
    from mlaser.ui.homes import EditInLink
    w = window
    w.config.set("shell.theme", "light")
    _pump(0.3)
    for ws in w.shell.host.visible_ids():
        w.show_workspace(ws)
        _pump(0.1)
    w.show_workspace("process")
    w.canvas.select_parts([0])          # Properties shows its links for a selection (UX audit O14)
    _pump(0.2)
    w.config.set("shell.theme", "dark")
    _pump(0.5)
    links = [lb for lb in w.findChildren(EditInLink) if lb.isVisible()]
    assert links
    for lb in links:
        bg = lb.palette().color(QPalette.Window)
        img = lb.grab().toImage()
        colours = {}
        for x in range(img.width()):
            for y in range(img.height()):
                c = img.pixelColor(x, y).name()
                colours[c] = colours.get(c, 0) + 1
        bgname = bg.name()
        ink = max((c for c in colours if c != bgname), key=lambda c: colours[c])
        assert ink != "#0000ff"
        assert _wcag(ink, bgname) >= 4.5, (lb.text(), ink, bgname)
        assert _wcag(lb.palette().color(QPalette.Link).name(), bgname) >= 4.5


def test_recolor_links_keeps_other_style():
    from mlaser.ui.shell.theme import link_html, recolor_links
    s = recolor_links("<a href='x' style='font-weight:bold; color:#0000ff'>A</a> b <a href=\"y\">C</a>", "#7fb0ff")
    assert s.count("color:#7fb0ff") == 2 and "#0000ff" not in s and "font-weight:bold" in s
    QApplication.instance() or QApplication([])
    assert link_html("Go", "#").startswith("<a href='#' style='color:#")


# ------------------------------------------------------------------ painted views
def test_painted_views_have_translated_names_and_values():
    QApplication.instance() or QApplication([])
    from mlaser.ui.ext_devices import CurvePlot
    from mlaser.ui.i18n import translator
    from mlaser.ui.properties.lead_diagram import LeadDiagram
    from mlaser.ui.workspaces.report import DayChart
    from mlaser.ui.workspaces.run import RunCanvas
    t = translator()
    c = CurvePlot()
    c.set_points([(100, 0), (200, 500), (300, 1000)])
    assert c.accessibleName() == t.tr("a11y.curve.name")
    assert "3" in c.accessibleDescription() and "1000" in c.accessibleDescription()
    d = LeadDiagram()
    from mlaser.model import LeadParams
    d.set_leads(LeadParams(kind=1, length=3.0), LeadParams(kind=0))
    assert d.accessibleName() == t.tr("a11y.lead.name") and t.tr("a11y.lead.none") in d.accessibleDescription()
    ch = DayChart()
    ch.set_days([{"day": "2026-01-02", "run_s": 7200, "alarm_s": 0, "stopped": 0}])
    assert ch.accessibleName() == t.tr("report.chart")
    assert ch.accessibleDescription() == t.plural("a11y.report.chart_desc", 1, hours="2.0")
    rc = RunCanvas()
    from mlaser.ui import docops
    rc.set_document(docops.demo_document())
    rc.set_progress({0}, 1, None)
    n = len(docops.demo_document().parts)
    assert rc.accessibleName() == t.tr("a11y.run.sheet")
    assert rc.accessibleDescription() == t.plural("a11y.run.sheet_progress", n, done=1)


# ------------------------------------------------------------------ OS high contrast
def test_os_high_contrast_switches_the_system_theme_and_a_choice_wins(window, monkeypatch):
    from mlaser.ui.shell import theme as T
    w = window
    sh = w.shell
    monkeypatch.setattr(T, "detect_high_contrast", lambda app=None: True)
    try:
        sh.set_theme(T.SYSTEM, persist=False)
        sh._contrast_changed()                       # Qt's contrastPreferenceChanged
        _pump(0.2)
        app = QApplication.instance()
        assert sh.theme == T.SYSTEM                  # the user's choice is still "system"
        assert app.property("mlaser_theme") == T.HIGH_CONTRAST
        assert T.effective(T.SYSTEM) == T.HIGH_CONTRAST
        assert app.palette().color(QPalette.Window).name() == T.THEMES[T.HIGH_CONTRAST]["window"]
        sh.set_theme(T.LIGHT, persist=False)         # an explicit theme wins
        _pump(0.1)
        assert app.property("mlaser_theme") == T.LIGHT
        sh.set_theme(T.SYSTEM, persist=False)
        monkeypatch.setattr(T, "detect_high_contrast", lambda app=None: False)
        sh._contrast_changed()                       # the user turned it off again
        _pump(0.1)
        assert app.property("mlaser_theme") == T.SYSTEM
        assert T.effective(T.SYSTEM) in (T.LIGHT, T.DARK)
    finally:
        T.set_os_high_contrast(None)


def test_detect_high_contrast_env_override_and_failure_safety(monkeypatch):
    from mlaser.ui.shell import theme as T
    monkeypatch.setenv("MLASER_HIGH_CONTRAST", "1")
    assert T.detect_high_contrast() is True
    monkeypatch.setenv("MLASER_HIGH_CONTRAST", "0")
    assert T.detect_high_contrast() is False
    monkeypatch.delenv("MLASER_HIGH_CONTRAST")
    assert T.detect_high_contrast() is False         # offscreen: no desktop session

    def boom(app=None):
        raise OSError("no settings service")
    monkeypatch.setattr(T, "detect_high_contrast", boom)
    try:
        assert T.os_high_contrast(refresh=True) is False
    finally:
        T.set_os_high_contrast(None)
