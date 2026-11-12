"""A workspace tool bar whose extension button appears after the fit (a style that lays the
buttons out wider than their size hints, the layout pass after the queued check: Windows CI,
every width of the Design sweep) steps down to icons only / two rows until no action is hidden
behind the button (mlaser/ui/shell/workspace_host.py, docs/dev/RESPONSIVE.md)."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QToolButton  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _pump(app, n=10):
    for _ in range(n):
        app.processEvents()


def test_an_extension_button_that_appears_late_is_stepped_down(app, monkeypatch, tmp_path):
    from wiring import dispose, real_window
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window()
    try:
        w.resize(1280, 800)
        w.show()
        w.shell.host.activate("design")
        _pump(app)
        w.resize(w.minimumSizeHint().expandedTo(w.minimumSize()))
        _pump(app)
        page = w.shell.host.pages["design"]
        tb = page.toolbar
        ext = tb.findChild(QToolButton, "qt_toolbar_ext_button")
        assert ext is not None and not ext.isVisible()
        # the buttons laid out wider than the fit measured them, with no resize to follow
        for act in tb.actions():
            b = tb.widgetForAction(act)
            if isinstance(b, QToolButton) and not act.icon().isNull():
                b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.layout().invalidate()
        tb.layout().activate()
        _pump(app)
        assert not ext.isVisible(), (tb.width(), tb.sizeHint().width(), page.width())
    finally:
        dispose(w)


@pytest.mark.parametrize("ws", ["design", "process"])
def test_a_second_row_that_overflows_late_is_stepped_down(app, monkeypatch, tmp_path, ws):
    """The same for the second row (Windows CI: the Process tool bar's extension button at
    1100 px wide, a11y audit): its buttons laid out wider than the fit measured them; the row
    break moves and the page's minimum takes both rows, until no command is hidden."""
    from wiring import dispose, real_window
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window()
    try:
        w.resize(1280, 800)
        w.show()
        w.shell.host.activate(ws)
        _pump(app)
        w.resize(w.minimumSizeHint().expandedTo(w.minimumSize()))
        _pump(app)
        page = w.shell.host.pages[ws]
        tb, tb2 = page.toolbar, page.toolbar2
        assert tb2.isVisible(), "the tool bar is in one row at the window's minimum width"
        ext2 = tb2.findChild(QToolButton, "qt_toolbar_ext_button")
        assert ext2 is not None and not ext2.isVisible()
        # the second row's buttons laid out wider than the fit measured them, no resize to follow
        for act in tb2.actions():
            b = tb2.widgetForAction(act)
            if isinstance(b, QToolButton) and not act.icon().isNull():
                b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb2.layout().invalidate()
        tb2.layout().activate()
        _pump(app, 20)
        hidden = [t.objectName() for t in (tb, tb2) if t.isVisible()
                  and t.findChild(QToolButton, "qt_toolbar_ext_button").isVisible()]
        assert not hidden, (hidden, tb.width(), tb.sizeHint().width(), tb2.width(), tb2.sizeHint().width(),
                            page.width())
    finally:
        dispose(w)


def _hidden(page):
    return [(t.objectName(), t.width(), t.sizeHint().width()) for t in (page.toolbar, page.toolbar2)
            if t.isVisible() and t.findChild(QToolButton, "qt_toolbar_ext_button").isVisible()]


def test_narrow_then_wide_leaves_no_command_hidden(app, monkeypatch, tmp_path):
    """Design at the window's minimum width (two rows), the second row overflowing for a
    moment (a layout pass at a narrower width: Windows), then wider in the sweep's 40 px steps
    to 800 px: at rest after each step neither row shows its extension button.  (Windows CI,
    design-800: the first row 260-460 px wide, its hint 246, the button shown; the step-down
    had moved commands into the first row for the passing overflow of the second, and nothing
    moved them back.)"""
    from wiring import dispose, real_window

    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window()
    try:
        w.resize(1280, 800)
        w.show()
        w.shell.host.activate("design")
        _pump(app)
        page = w.shell.host.pages["design"]
        w.resize(w.minimumSizeHint().expandedTo(w.minimumSize()).width(), 800)
        _pump(app, 30)
        assert page.toolbar2.isVisible() and not _hidden(page)
        before = (w.width(), page._split, page.toolbar.actions())
        # the second row overflows for a moment
        page.toolbar2.setMaximumWidth(page.toolbar2.sizeHint().width() // 2)
        _pump(app, 30)
        page.toolbar2.setMaximumWidth(16777215)
        _pump(app, 30)
        # at rest the rows are as before: no command was moved for the passing overflow
        assert (w.width(), page._split, page.toolbar.actions()) == before
        bad = []
        if _hidden(page):
            bad.append((w.width(), _hidden(page)))
        for width in range(w.width(), 801, 40):
            w.resize(width, 800)
            _pump(app, 30)
            if _hidden(page):
                bad.append((w.width(), _hidden(page)))
        assert not bad, bad
    finally:
        dispose(w)


def _stick(app, tb, squeeze, release):
    """Leave ``tb``'s extension button as Qt does when the row gets its width back inside the
    layout pass that shows the button (``release`` on the first Hide of a command's button,
    after ``squeeze`` made the row overflow).  True when the button is stuck."""
    from PySide6.QtCore import QEvent, QObject

    class Widen(QObject):
        armed = True

        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Hide and self.armed:
                self.armed = False
                release()
            return False
    f = Widen()
    btns = [tb.widgetForAction(a) for a in tb.actions() if isinstance(tb.widgetForAction(a), QToolButton)]
    for b in btns:
        b.installEventFilter(f)
    try:
        squeeze()
        _pump(app)
    finally:
        for b in btns:
            b.removeEventFilter(f)
    ext = tb.findChild(QToolButton, "qt_toolbar_ext_button")
    return ext.isHidden() and ext.isVisible()


def test_qt_leaves_the_extension_button_stuck_and_it_is_unstuck(app):
    """The Qt behaviour itself, on a plain tool bar: the row squeezed, and given its width back
    during the layout pass that shows the extension button.  The button stays visible at every
    width after it, and ``hide()`` does nothing; ``unstick_extension`` hides it, and a real
    overflow after that shows and hides it as usual."""
    from PySide6.QtGui import QAction, QIcon, QPixmap
    from PySide6.QtWidgets import QHBoxLayout, QToolBar, QWidget

    from mlaser.ui.shell.workspace_host import unstick_extension

    pm = QPixmap(20, 20)
    pm.fill(Qt.red)
    page = QWidget()
    lay = QHBoxLayout(page)
    lay.setContentsMargins(0, 0, 0, 0)
    tb = QToolBar()
    tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    for i in range(6):
        tb.addAction(QAction(QIcon(pm), f"Command {i}", tb))
    side = QWidget()
    side.setFixedWidth(100)
    lay.addWidget(tb, 1)
    lay.addWidget(side)
    try:
        page.resize(tb.sizeHint().width() + 150, 80)
        page.show()
        _pump(app)
        ext = tb.findChild(QToolButton, "qt_toolbar_ext_button")
        assert not ext.isVisible() and not unstick_extension(tb)
        if not _stick(app, tb, lambda: side.setFixedWidth(page.width() - 200), lambda: side.setFixedWidth(100)):
            pytest.skip("this Qt does not leave the extension button stuck")
        page.resize(page.width() + 300, 80)
        _pump(app)
        ext.hide()
        assert ext.isVisible() and tb.width() > tb.sizeHint().width()      # stuck, the row fits
        assert unstick_extension(tb) and not ext.isVisible()
        _pump(app)
        assert not ext.isVisible() and not unstick_extension(tb)
        side.setFixedWidth(page.width() - 200)                              # a real overflow
        _pump(app)
        assert ext.isVisible() and not ext.isHidden() and not unstick_extension(tb)
        assert ext.isVisible()                                              # (left alone)
        side.setFixedWidth(100)
        _pump(app)
        assert not ext.isVisible()
    finally:
        page.close()
        page.deleteLater()


def test_a_stuck_extension_button_costs_no_label_and_no_row(app, monkeypatch, tmp_path):
    """Windows CI, Design at every width of a sweep: the first row's extension button visible
    with every command in the row (260-1795 px wide, its size hint 246).  The step-down took
    the stuck button for an overflow: every label went, then the second row was used, at any
    width.  Now the button is hidden and the tool bar stays as the fit made it."""
    from wiring import dispose, real_window
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window()
    try:
        w.resize(1600, 875)
        w.show()
        w.shell.host.activate("design")
        _pump(app, 30)
        page = w.shell.host.pages["design"]
        tb = page.toolbar
        ext = tb.findChild(QToolButton, "qt_toolbar_ext_button")

        def rows():
            return (page._split, [tb.widgetForAction(a).toolButtonStyle() for a in tb.actions()
                                  if isinstance(tb.widgetForAction(a), QToolButton)])
        before = rows()
        assert not ext.isVisible() and not page.toolbar2.isVisible()
        for width in (1600, 1640, 1200, 700):
            w.resize(width, 875)
            _pump(app, 30)
            before = rows()
            # as Qt leaves it: shown, and marked hidden by the nested layout pass
            ext.show()
            ext.setAttribute(Qt.WA_WState_Hidden, True)
            assert ext.isVisible()
            _pump(app, 30)
            assert not ext.isVisible(), width
            assert rows() == before, width
            assert not _hidden(page), width
    finally:
        dispose(w)
