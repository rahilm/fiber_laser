"""mlaser/ui/responsive.py: the helpers on their own (the window-level checks are in
tests/test_responsive_layout.py)."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QSize
from PySide6.QtWidgets import (QApplication, QLabel, QLineEdit, QMainWindow, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from mlaser.ui import responsive as R


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _pump(app, n: int = 5) -> None:
    for _ in range(n):
        app.processEvents()


def test_columns_for():
    assert R.columns_for(100, 200) == 1
    assert R.columns_for(410, 200, spacing=10) == 2
    assert R.columns_for(2000, 200, max_columns=3) == 3


def test_flow_layout_wraps_and_reports_height_for_width(app):
    box = QWidget()
    lay = R.FlowLayout(box, uniform=True)
    buttons = [QPushButton(f"Button {i}") for i in range(6)]
    for b in buttons:
        lay.addWidget(b)
    one = lay.heightForWidth(10_000)
    narrow = lay.heightForWidth(lay.minimumSize().width())
    assert narrow > one                                   # one column: six rows
    assert lay.minimumSize().width() >= max(b.sizeHint().width() for b in buttons)
    box.resize(lay.minimumSize().width(), narrow)
    box.show()
    _pump(app)
    xs = {b.x() for b in buttons}
    assert len(xs) == 1                                   # all in one column
    box.resize(10_000, one)
    _pump(app)
    assert len({b.y() for b in buttons}) == 1             # all in one row
    box.close()


def test_responsive_form_puts_labels_above_when_narrow(app):
    f = R.ResponsiveForm()
    f.addRow("A rather long label", QLineEdit())
    f.addRow("Short", QLineEdit())
    f.resize(f.needed_beside() + 20, 100)
    f.show()
    _pump(app)
    assert f.beside()
    f.resize(f.minimumSizeHint().width(), 200)
    _pump(app)
    assert not f.beside()
    assert f.minimumSizeHint().width() < f.needed_beside()
    f.resize(f.needed_beside() + 20, 100)
    _pump(app)
    assert f.beside()                                     # and back
    f.close()


def test_columns_panel_folds_then_recovers(app):
    p = R.ColumnsPanel(max_columns=2)
    stop = QPushButton("STOP")
    stop.setMinimumHeight(80)                 # a touch target taller than the button's hint
    p.add_pinned(stop)
    secs = []
    for i in range(4):
        body = QWidget()
        v = QVBoxLayout(body)
        for k in range(4):
            v.addWidget(QPushButton(f"s{i} b{k}"))
        s = R.CollapsibleSection(f"Section {i}", body, priority=i)
        p.add_section(s)
        secs.append(s)
    # sizes measured once the style has polished the widgets (the macOS style changes the
    # buttons' and headers' sizes when it polishes them: sizes taken before that were too small
    # and the panel folded at "full" height on the macOS runner)
    p.ensurePolished()
    w0 = p.minimumSizeHint().width()
    full = p.sizeHint().height()
    p.resize(w0, full + 10)
    p.show()
    _pump(app)
    w0 = p.minimumSizeHint().width()
    full = p.sizeHint().height()
    p.resize(w0, full + 10)
    _pump(app)
    assert all(s.expanded() for s in secs) and "1 col" in p.property(R.STATE_PROP)
    assert stop.height() >= 80 and all(s.y() >= stop.geometry().bottom() for s in secs)   # no overlap
    p.resize(w0, p.minimumSizeHint().height())
    _pump(app)
    assert stop.isVisible() and stop.geometry().bottom() < p.height()
    assert not secs[0].expanded()                         # the lowest priority folds first
    assert secs[3].expanded() or not any(s.expanded() for s in secs)
    p.resize(2 * w0 + 40, full // 2 + 60)
    _pump(app)
    assert "2 cols" in p.property(R.STATE_PROP)            # wide and short: two columns
    p.resize(w0, full + 10)
    _pump(app)
    assert all(s.expanded() for s in secs)                # everything back
    # the user's choice stays
    secs[2].header.click()
    _pump(app)
    assert not secs[2].expanded()
    p.resize(w0, full + 20)
    _pump(app)
    assert not secs[2].expanded()
    p.close()


def test_column_fitter_hides_low_priority_columns(app):
    t = QTableWidget(3, 4)
    t.setHorizontalHeaderLabels(["Name", "Important", "Less", "Least"])
    for r in range(3):
        for c in range(4):
            t.setItem(r, c, QTableWidgetItem("x" * (20 if c == 0 else 12)))
    f = R.ColumnFitter(t, {1: 5, 2: 2, 3: 0}, stretch=0)
    t.resize(1200, 200)
    t.show()
    _pump(app)
    f.fit()
    assert not f.auto_hidden
    t.resize(260, 200)
    _pump(app)
    f.fit()
    hh = t.horizontalHeader()
    assert hh.isSectionHidden(3) and not hh.isSectionHidden(0)
    assert not t.horizontalScrollBar().isVisible()
    t.resize(1200, 200)
    _pump(app)
    f.fit()
    assert not any(hh.isSectionHidden(c) for c in range(4))
    t.close()


def test_keep_min_size_survives_a_style_sheet_rule(app):
    """The UI audit's P0: a QSS min-height rule that stops matching resets the minimum to 0."""
    win = QMainWindow()
    b = QPushButton("STOP")
    R.keep_min_size(b, QSize(80, 56))
    win.setCentralWidget(b)
    win.show()
    win.setProperty("userMode", "operator")
    win.setStyleSheet('QMainWindow[userMode="operator"] QPushButton { min-height: 48px; }')
    _pump(app)
    win.setProperty("userMode", "service")
    win.setStyleSheet("")
    win.setStyleSheet('QMainWindow[userMode="operator"] QPushButton { min-height: 48px; }')
    _pump(app)
    assert b.minimumHeight() >= 56 and b.minimumWidth() >= 80
    win.close()


def test_fit_area_takes_the_page_minimum_capped_by_the_screen(app):
    page = QWidget()
    page.setMinimumSize(300, 200)
    QVBoxLayout(page).addWidget(QLabel("page"))
    a = R.FitArea(page)
    assert a.minimumSizeHint() == QSize(300, 200)
    big = QWidget()
    big.setMinimumSize(100_000, 100_000)
    a2 = R.FitArea(big)
    avail = a2.screen().availableSize()
    m = a2.minimumSizeHint()
    assert m.width() <= avail.width() and m.height() <= avail.height()
    a2.resize(m)
    a2.show()
    _pump(app)
    assert a2.verticalScrollBar().isVisible()             # below the page's need: it scrolls
    a2.close()


def test_current_page_stack_is_sized_by_the_current_page(app):
    st = R.CurrentPageStack()
    small, tall = QWidget(), QWidget()
    small.setMinimumSize(50, 50)
    tall.setMinimumSize(50, 900)
    st.addWidget(small)
    st.addWidget(tall)
    assert st.minimumSizeHint().height() == 50
    st.setCurrentWidget(tall)
    assert st.minimumSizeHint().height() == 900


def test_fit_area_deleted_with_a_layout_request_queued(app, monkeypatch):
    """A FitArea deleted while its page's layout request is queued (a window disposed of in the
    middle of a relayout): the queued sync is dropped, no "Internal C++ object (FitArea) already
    deleted" from the event loop (seen in the Ubuntu CI log)."""
    import shiboken6
    from PySide6.QtCore import QCoreApplication, QEvent
    errors = []
    monkeypatch.setattr("sys.excepthook", lambda *exc: errors.append(exc))
    page = QWidget()
    QVBoxLayout(page).addWidget(QLabel("page"))
    area = R.FitArea(page)
    area.show()
    _pump(app)
    QCoreApplication.sendEvent(page, QEvent(QEvent.LayoutRequest))   # queues the area's sync
    shiboken6.delete(area)                                           # the C++ side goes first
    _pump(app)
    assert not errors, errors


def test_responsive_form_height_for_width_follows_the_arrangement(app):
    """ResponsiveForm reports the height of the arrangement a width gets (above when narrow),
    before it is resized: a parent that sized it from the current (beside) arrangement left
    the stacked form short (Run Control's DO spin box 22 of 24 px, native-check X11/Windows)."""
    from PySide6.QtWidgets import QSpinBox
    form = R.ResponsiveForm()
    for i in range(3):
        form.addRow(f"A rather long label {i}", QSpinBox())
    wide = form.needed_beside() + 40
    narrow = form.needed_beside() - 20
    form.resize(wide, form.heightForWidth(wide))
    form.show()
    _pump(app)
    assert form.beside()
    h_narrow = form.heightForWidth(narrow)            # asked while still beside
    assert h_narrow > form.heightForWidth(wide)
    form.resize(narrow, h_narrow)
    _pump(app)
    assert not form.beside()
    for _lab, f in form.rows():                       # every field gets its full height
        assert f.height() >= f.minimumSizeHint().height()
        assert f.geometry().bottom() < form.height()
    assert form.layout().minimumSize().height() <= h_narrow


def test_flow_layout_uses_widget_rects_with_layout_item_margins(app):
    """A style with layout item margins (the macOS style: a push button's bezel shadow lies
    outside its "layout item" rectangle): the flow sizes rows from the widgets' own rectangles,
    so every button gets its minimum height and stays inside the flow's box (native-check
    macOS: "Go Origin height 28 < minimum 32", "Return cut by its parent")."""
    from PySide6.QtWidgets import QComboBox, QProxyStyle, QStyle

    class Margins(QProxyStyle):
        def subElementRect(self, el, opt, w=None):  # noqa: N802 - Qt API
            if el in (QStyle.SE_PushButtonLayoutItem, QStyle.SE_ComboBoxLayoutItem):
                return opt.rect.adjusted(6, 2, -6, -6)
            return super().subElementRect(el, opt, w)

        def sizeFromContents(self, ct, opt, size, w=None):  # noqa: N802 - Qt API
            s = super().sizeFromContents(ct, opt, size, w)
            if ct in (QStyle.CT_PushButton, QStyle.CT_ComboBox):      # the bezel's 32 px
                s.setHeight(max(s.height(), 32))
            return s

    old_style = app.style().name()
    app.setStyle(Margins("Fusion"))      # an application style: the layout item margins apply
    widgets = [QPushButton("Go Origin"), QComboBox(), QPushButton("Mark"), QPushButton("Return")]
    widgets[1].addItem("Mark P1")
    box = R.flow(widgets)                 # no margins, as in the panels
    box_l = box.layout()
    width = sum(x.sizeHint().width() for x in widgets[:3]) + 20
    box.resize(width, box_l.heightForWidth(width))
    box.show()
    _pump(app)
    try:
        for i, x in enumerate(widgets):
            assert x.height() >= x.minimumSizeHint().height(), (x, x.size())
            assert box.rect().contains(x.geometry()), (x, x.geometry(), box.rect())
            for y in widgets[i + 1:]:
                assert not x.geometry().intersects(y.geometry()), (x, y)
    finally:
        box.deleteLater()
        app.setStyle(old_style)
        _pump(app)
