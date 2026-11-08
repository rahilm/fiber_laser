"""A half-screen window (683 px wide: a Windows Snap half of 1366, half a 14" MacBook Pro): the
side docks fold into one tab group and come back as they were, the user's own arrangement
wins, and the run bar wraps into two rows with every run control whole and reachable
(docs/dev/RESPONSIVE.md, docs/SAFETY.md §10)."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton, QTabBar  # noqa: E402

HALF = (683, 875)
WIDE = (1512, 875)
RUN_KEYS = ("start", "hold", "stop", "frame", "dry_run", "arm")


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
    w.resize(*WIDE)
    w.show()
    w.shell.host.activate("design")
    settle(app)
    yield w
    dispose(w)


def settle(app, sec: float = 0.4) -> None:
    loop = QEventLoop()
    QTimer.singleShot(int(sec * 1000), loop.quit)
    loop.exec()
    for _ in range(5):
        app.processEvents()


def arrangement(w) -> dict:
    """Each visible docked dock: its area and the docks tabbed with it."""
    out = {}
    for d in w.shell.host.docks():
        if d.isVisible() and not d.isFloating():
            out[d.objectName()] = (int(w.dockWidgetArea(d).value),
                                   sorted(x.objectName() for x in w.tabifiedDockWidgets(d) if not x.isFloating()))
    return out


def tab_bars(w) -> list:
    """The main window's tab bars drawn in the window (a stale one shows over a panel)."""
    out = []
    for bar in w.findChildren(QTabBar):
        if bar.parent() is w and bar.isVisible() and w.rect().intersects(bar.geometry()):
            out.append(sorted(bar.tabText(i) for i in range(bar.count())))
    return sorted(out)


def wide_ones(root) -> list:
    """The widgets whose minimum width keeps the window wider (a failure's details)."""
    from PySide6.QtWidgets import QWidget
    return [(type(x).__name__, x.objectName(), x.minimumSizeHint().width(), x.minimumWidth())
            for x in root.findChildren(QWidget) if x.isVisible()
            and max(x.minimumSizeHint().width(), x.minimumWidth()) > 450][:12]


def groups(w) -> list:
    out = []
    for d in w.shell.host.docks():
        if d.isVisible() and not d.isFloating():
            g = [d, *[x for x in w.tabifiedDockWidgets(d) if not x.isFloating()]]
            if len(g) > 1:
                out.append(sorted(x.windowTitle() for x in g))
    return out


def test_docks_fold_when_narrow_and_come_back(win, app):
    pol = win.shell.dock_policy
    before, bars = arrangement(win), tab_bars(win)
    assert len([a for a, _t in before.values() if a in (1, 2)]) >= 2, before   # docks on both sides
    win.resize(*HALF)
    settle(app)
    assert win.width() == HALF[0], (win.width(), win.minimumSize())
    assert pol.folded is not None
    side = [n for n, (a, _t) in arrangement(win).items() if a in (1, 2)]
    assert len({arrangement(win)[n][0] for n in side}) == 1           # one area, one tab group
    for bar in tab_bars(win):
        assert bar in groups(win), (bar, groups(win))                   # no stale tab bar
    win.resize(*WIDE)
    settle(app)
    assert pol.folded is None
    assert arrangement(win) == before
    assert tab_bars(win) == bars                                        # none left behind


def test_users_own_arrangement_wins(win, app, monkeypatch):
    pol = win.shell.dock_policy
    win.resize(*HALF)
    settle(app)
    assert pol.folded is not None
    # the user drags Run Control out of the tab group to the right-hand side
    rc = win.findChild(type(win.shell.host.docks()[0]), "mf141")
    monkeypatch.setattr(QApplication, "mouseButtons", staticmethod(lambda: Qt.LeftButton))
    win.addDockWidget(Qt.RightDockWidgetArea, rc)
    app.processEvents()
    monkeypatch.undo()
    settle(app)
    assert pol.user_arranged and pol.folded is None
    mine = arrangement(win)
    win.resize(*WIDE)
    settle(app)
    assert arrangement(win) == mine                                     # not put back
    win.run("shell.reset_layout")
    settle(app)
    assert not pol.user_arranged


def test_run_bar_wraps_with_every_control_whole_and_reachable(win, app):
    rb = win.safety_ui.run_bar
    one_row = rb.height()
    for ws in win.shell.host.visible_ids():
        win.shell.host.activate(ws)
        win.resize(*HALF)
        settle(app, 0.2)
        assert win.width() == HALF[0], (ws, win.minimumSize(), wide_ones(win.centralWidget()))
        top = win.rect()
        if ws == "run":
            # one set of run controls there, the Run screen's (UX audit W4)
            scr = win.extension("run").screen
            for key in RUN_KEYS:
                assert not rb.buttons[key].isVisible(), (ws, key)
                if key in scr.buttons:
                    assert scr.buttons[key].isVisible(), (ws, key)
            continue
        assert rb.strip.wrapped and rb.height() > one_row
        for key in RUN_KEYS:
            b = rb.findChild(QPushButton, f"run_{key}")
            assert b is rb.buttons[key] and b.isVisible(), (ws, key)
            r = b.rect().translated(b.mapTo(win, b.rect().topLeft()))
            assert top.contains(r), (ws, key, r, top)                   # inside the window
            assert b.width() >= b.minimumWidth() and b.height() >= b.minimumHeight(), (ws, key)
            assert b.focusPolicy() & Qt.TabFocus, key                     # reachable by Tab
        # the full texts (no icon-only state)
        for key in RUN_KEYS:
            assert rb.buttons[key].text()
    # the keyboard order is the bar's order, also in two rows
    chain = win.shell.focus_chain()
    pos = [chain.index(rb.buttons[k]) for k in rb.buttons]
    assert pos == sorted(pos)
    stop = rb.buttons["stop"]
    w0 = stop.width()
    win.resize(*WIDE)
    settle(app, 0.2)
    assert not rb.strip.wrapped and rb.height() == one_row
    assert stop.minimumWidth() <= w0                                    # Stop keeps its size


def test_page_tool_bar_wraps_instead_of_overflowing(win, app):
    page = win.shell.host.pages["design"]
    win.resize(*HALF)
    settle(app)
    assert page.toolbar2.isVisible()
    acts = [a for a in page.toolbar_actions() if not a.isSeparator()]
    shown = [b.defaultAction() for b in page.toolbar_buttons()]
    assert [a for a in acts if a not in shown] == []                    # every command in a row
    for tb in (page.toolbar, page.toolbar2):
        ext = tb.findChild(object, "qt_toolbar_ext_button")
        assert ext is None or not ext.isVisible()
    win.resize(*WIDE)
    settle(app)
    assert not page.toolbar2.isVisible()
