"""A half-screen window (683 px wide: a Windows Snap half of 1366, half a 14" MacBook Pro): the
side docks fold into one tab group and come back as they were, the user's own arrangement
wins, and the run bar wraps into two rows with every run control whole and reachable
(docs/dev/RESPONSIVE.md, docs/SAFETY.md §10)."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import (QApplication, QDialog, QMessageBox, QPushButton, QTabBar,  # noqa: E402
                               QToolButton)

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


def heights(w) -> list:
    """The window's parts and the page's tallest minimums (a failure's details: what a
    platform's font metrics made taller)."""
    from PySide6.QtWidgets import QWidget
    out = [(type(c).__name__, c.objectName(), c.height(), c.minimumSizeHint().height())
           for c in w.findChildren(QWidget) if c.parent() is w and c.isVisible()]
    page = w.centralWidget()
    for c in page.findChildren(QWidget):
        if c.isVisible() and max(c.minimumSizeHint().height(), c.minimumHeight()) >= 20:
            depth, p = 0, c.parentWidget()
            while p is not None and p is not page:
                depth, p = depth + 1, p.parentWidget()
            text = c.text()[:24] if hasattr(c, "text") and callable(c.text) and isinstance(c.text(), str) else ""
            out.append(("  " * depth + type(c).__name__, c.objectName(), f"{c.width()}x{c.height()}",
                        c.minimumSizeHint().height(), c.minimumHeight(), text))
    return out


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


@pytest.mark.parametrize("size", [(756, 945), (683, 875)])
def test_a_workspace_switch_reflows_instead_of_resizing(win, app, size):
    """W14: at half screen a workspace switch never widens the window into the other half;
    a page taller than the window may raise its height, and the next workspace that fits goes
    back to the user's size.  Design keeps a workable canvas (the docks fold before it is
    squeezed below CANVAS_FOLD)."""
    from mlaser.ops import demo_document
    from mlaser.ui.shell.dock_policy import CANVAS_FOLD
    win.set_document(demo_document())
    win.resize(*size)
    settle(app)
    assert win.size().toTuple() == size
    if size[0] >= 756:      # at 683 px the folded docks (Run Control 251 px) leave about 350
        assert win.canvas.width() >= CANVAS_FOLD, win.canvas.width()
    seen = {}
    for ws in [*win.shell.host.visible_ids(), "design"]:
        win.shell.show_workspace(ws)
        settle(app, 0.3)
        seen[ws] = win.size().toTuple()
        assert win.width() == size[0], (ws, seen)                       # never wider
        if win.minimumSize().height() <= size[1]:
            assert win.height() == size[1], (ws, seen)                  # back to the user's size
    assert seen["design"] == size, seen


def test_tool_bar_labels_most_used_first(win, app):
    """O6: when not every label fits, the workspace's most-used commands keep theirs (text
    beside the icon), the rest are icons only; a wide window shows labels in Process too."""
    from mlaser.services.workspaces import BUILTIN
    host = win.shell.host
    for ws, size in (("design", (1512, 875)), ("process", (1920, 1080))):
        host.activate(ws)
        win.resize(*size)
        settle(app)
        page = host.pages[ws]
        styles = {b.defaultAction(): b.toolButtonStyle() for b in page.toolbar_buttons()}
        labelled = [a for a, st in styles.items() if st != Qt.ToolButtonIconOnly]
        assert labelled, (ws, page.width())
        first = [host.action(c) for c in BUILTIN[ws].extra["toolbar_labels"]]
        first = [a for a in first if a in styles]
        n = len([a for a in labelled if a in first])
        assert first[:n] == [a for a in first if a in labelled]          # in priority order
        if len(labelled) < len(styles):
            assert n == len(first) or not [a for a in labelled if a not in first], ws
        ext = page.toolbar.findChild(QToolButton, "qt_toolbar_ext_button")
        assert ext is None or not ext.isVisible()


def test_canvas_bar_is_compact_and_folds(win, app):
    """O10: the canvas bar (Toolpath, Fit drawing, Fit table, legend) covers a small part of
    the drawing, also at half screen, and folds to its arrow, remembered."""
    from mlaser.ops import demo_document
    from wiring import dispose, real_window
    win.set_document(demo_document())
    hud = win.canvas.hud
    for size in (WIDE, (756, 945), HALF):
        win.resize(*size)
        settle(app)
        v, g = win.canvas, hud.geometry()
        assert g.width() * g.height() <= hud.AREA_SHARE * v.width() * v.height() + 1, (size, g, v.size())
        assert hud.toggle.isVisible() and hud.fit_drawing.isVisible() and hud.fit_table.isVisible()
    hud.fold.click()
    settle(app, 0.2)
    assert hud.collapsed and not hud.toggle.isVisible() and not hud.legend.isVisible()
    assert hud.width() < 3 * hud.fold.width()
    assert win.config.get("shell.canvas_hud_collapsed") is True
    other = real_window(config=win.config)
    try:
        assert other.canvas.hud.collapsed                               # remembered
    finally:
        dispose(other)
    hud.fold.click()
    settle(app, 0.2)
    assert not hud.collapsed and hud.toggle.isVisible()
    assert win.config.get("shell.canvas_hud_collapsed") is False


@pytest.mark.parametrize("size", [(756, 945), (683, 875)])
def test_process_pages_fit_half_screen(win, app, size):
    """Every Process page at half screen: no form scrolls, the canvas (when shown) keeps a
    workable height (CANVAS_MIN_H: when the page under it would leave less, the canvas and the
    page are tabs), and the Library keeps the window's height (its record list and editor are
    tabs, the "Used by" line elided; the docks under the page join the side docks' tab group
    when the window is short for them).  The window never grows."""
    from PySide6.QtWidgets import QAbstractButton, QAbstractScrollArea
    from mlaser.ops import demo_document
    from mlaser.ui.shell.workspace_host import CANVAS_MIN_H
    win.set_document(demo_document())
    win.canvas.select_parts([0])
    win.shell.host.activate("process")
    win.resize(*size)
    settle(app)
    page = win.shell.host.pages["process"]
    pages = sorted((b for b in win.findChildren(QAbstractButton) if b.objectName().startswith("process_page_")),
                   key=lambda b: b.objectName())
    assert pages
    for b in pages:
        if page.view_tabs.isVisible():
            page.view_tabs.setCurrentIndex(1)             # the page's tab, to reach its buttons
            settle(app, 0.2)
        b.setChecked(True)
        settle(app)
        scrolling = [a.objectName() for a in win.findChildren(QAbstractScrollArea)
                     if a.isVisible() and a.objectName() in ("techform_scroll", "shell_ws_scroll")
                     and a.verticalScrollBar().isVisible()]
        assert not scrolling, (size, b.objectName(), scrolling)
        if win.size().toTuple() != size:
            # the details in full in the captured output (the assertion's repr is cut)
            print("HEIGHTS", b.objectName(), win.size().toTuple(), win.minimumSize().toTuple(),
                  win.minimumSizeHint().toTuple())
            for row in heights(win):
                print("HEIGHTS", *row)
        assert win.size().toTuple() == size, (b.objectName(), win.size(), win.minimumSize())
        if page.view_tabs.isVisible():
            page.view_tabs.setCurrentIndex(0)             # the canvas's tab: the whole height
            settle(app, 0.2)
        if win.canvas.isVisible():
            assert win.canvas.height() >= CANVAS_MIN_H, (size, b.objectName(), win.canvas.height())
    pages[0].setChecked(True)
    settle(app, 0.2)


def test_docks_under_the_page_fold_when_short_and_come_back(win, app):
    """A window too short for the log and the preview under the page: they join the side
    docks' tab group, and go back under the page, as they were, when the height is back; no
    stale tab bar (Process at 683x875)."""
    from mlaser.ops import demo_document
    pol = win.shell.dock_policy
    win.set_document(demo_document())
    win.canvas.select_parts([0])
    win.shell.host.activate("process")
    settle(app)
    before, bars = arrangement(win), tab_bars(win)
    assert any(a == 8 for a, _t in before.values()), before            # docks under the page
    short = (HALF[0], 790)
    win.resize(*short)
    settle(app)
    assert win.size().toTuple() == short, (win.size(), win.minimumSize())
    assert pol.vfolded is not None
    assert not any(a == 8 for a, _t in arrangement(win).values())
    for bar in tab_bars(win):
        assert bar in groups(win), (bar, groups(win))
    win.resize(WIDE[0], 1080)       # the height back, with room to spare (larger fonts)
    settle(app)
    assert pol.vfolded is None and pol.folded is None
    assert arrangement(win) == before
    assert tab_bars(win) == bars
