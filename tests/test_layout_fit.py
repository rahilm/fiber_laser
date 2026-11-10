"""The window fits the smallest supported screen through the application's own start path
(``Shell.fit_to_screen`` before ``show``, as ``mlaser.ui.__main__``), every workspace and tab,
also with a taskbar taking part of the screen (docs/dev/RESPONSIVE.md, UI review 2026-11-09
finding 3); the Nest page's panes keep their arrangement when the page is first laid out hidden;
Machine › Monitors shows its first monitor (finding 10); the log viewer does not scroll sideways
with a large font (issue #3)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QPushButton, QWidget

import layout_audit as A

#: Qt styles that need their own window server (see tests/test_responsive_layout.py)
NATIVE_ONLY_STYLES = ("macos", "windows11", "windowsvista")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _pump(app, s: float = 0.0, rounds: int = 3) -> None:
    for _ in range(rounds):
        app.processEvents()
    t = time.monotonic() + s
    while time.monotonic() < t:
        app.processEvents()
        time.sleep(0.005)


# ----------------------------------------------------------------------------- start path
FIT_SCRIPT = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from PySide6.QtGui import QScreen
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
INSET = int(os.environ["FIT_INSET"])
if INSET:
    # a taskbar along the bottom: QScreen.availableGeometry without it (the offscreen platform
    # has no taskbar; every caller in the application is Python and asks this method)
    _avail = QScreen.availableGeometry
    QScreen.availableGeometry = lambda self: _avail(self).adjusted(0, 0, 0, -INSET)
    QScreen.availableSize = lambda self: QScreen.availableGeometry(self).size()
app = QApplication([])
PT = float(os.environ["FIT_PT"])
if PT:
    f = app.font()
    f.setPointSizeF(PT)
    app.setFont(f)
if os.environ.get("MLASER_LAYOUT_STYLE"):
    app.setStyle(os.environ["MLASER_LAYOUT_STYLE"])
QDialog.exec = lambda self, *a: 0
for n in ("information", "warning", "critical", "question"):
    setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
from wiring import real_window
import layout_audit as A
w = real_window(user_mode="engineer")
w.shell.fit_to_screen()
w.show()
def pump(sec):
    t = time.monotonic() + sec
    while time.monotonic() < t:
        app.processEvents()
        time.sleep(0.005)
pump(0.3)
avail = w.screen().availableGeometry()
shots = os.environ.get("FIT_SHOTS", "")
out, seen = [], []
def check(label):
    pump(0.4)
    g = w.frameGeometry()
    if g.left() < avail.left() or g.top() < avail.top() or g.right() > avail.right() or g.bottom() > avail.bottom():
        out.append(f"{label}: window {g.getRect()} outside the screen's room {avail.getRect()}")
    out.extend(f"{label}: {v}" for v in A.audit(w))
    seen.append([label, w.width(), w.height()])
    # a grab lays out every widget it paints: the CI harness grabs each view, and the Nest
    # page laid out hidden that way came back with its panes at their minimum widths
    pm = w.grab()
    if shots:
        os.makedirs(shots, exist_ok=True)
        pm.save(os.path.join(shots, f"{os.environ['FIT_TAG']}_{label.replace('/', '-')}.png"))
for ws in w.shell.host.visible_ids():
    w.show_workspace(ws)
    check(ws)
    page = w.shell.host.pages[ws]
    tabs = getattr(page, "tabs", None)
    if tabs is not None:
        for i in range(1, tabs.count()):
            tabs.setCurrentIndex(i)
            check(f"{ws}/{i}")
        tabs.setCurrentIndex(0)
print(json.dumps({"avail": list(avail.getRect()), "violations": out, "seen": seen}), flush=True)
os._exit(0)
"""

#: (screen width, height, bottom inset of a taskbar, device pixel ratio, font pt (0: default)).
#: 1280 x 800 is the smallest supported screen (docs/dev/RESPONSIVE.md); 48 px is a Windows
#: taskbar at 100 %, 80 px a taskbar and a title bar (the offscreen platform has no frame);
#: 1512 x 982 at 2x is a 14" MacBook Pro.  1366 x 768 is below the supported size and not here.
SCREENS = [(1280, 800, 0, 1, 0), (1280, 800, 48, 1, 0), (1280, 800, 80, 1, 0), (1512, 982, 0, 2, 0),
           (1280, 800, 48, 1, 10)]


@pytest.mark.sim          # one worker with the timing tests: a whole app in a subprocess
@pytest.mark.parametrize("screen", SCREENS, ids=lambda s: f"{s[0]}x{s[1]}-{s[2]}@{s[3]}x-{s[4] or 'default'}pt")
def test_every_workspace_fits_the_screen_from_the_start_path(tmp_path, screen):
    """Every workspace and tab, built lazily after the start, fits the screen's available room
    without scrolling and the window never grows past it (Orders grew the window to 818 px on
    an 800 px screen, Nest scrolled 14-17 px: the cap was decided once from Design's minimum)."""
    style = os.environ.get("MLASER_LAYOUT_STYLE", "").lower()
    if style in NATIVE_ONLY_STYLES:
        pytest.skip(f"the offscreen platform cannot host the {style} style")
    sw, sh, inset, dpr, pt = screen
    cfg = tmp_path / "screen.json"
    cfg.write_text(json.dumps({"screens": [{"name": "s", "x": 0, "y": 0, "width": sw * dpr, "height": sh * dpr,
                                            "logicalDpi": 96, "dpr": 1}]}), encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen:configfile=screen.json",
               MLASER_CONFIG_DIR=str(tmp_path / "cfg"), MLASER_APP_DIR=str(tmp_path / "app"),
               MLASER_DATA_DIR=str(tmp_path / "data"), FIT_INSET=str(inset), FIT_PT=str(pt),
               FIT_TAG=f"{sw}x{sh}-{inset}")
    env.pop("_MLASER_CONFIG_AUTO", None)
    if dpr != 1:
        env["QT_SCALE_FACTOR"] = str(dpr)
    d = os.environ.get("MLASER_LAYOUT_SHOTS", "")
    if d:
        env["FIT_SHOTS"] = str(Path(d).resolve() / "fit")
    root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join([root, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    r = subprocess.run([sys.executable, "-c", FIT_SCRIPT, str(Path(__file__).parent.resolve())], env=env,
                       capture_output=True, text=True, timeout=600, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr[-3000:]
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["avail"][3] == sh - inset, res["avail"]
    assert {s[0] for s in res["seen"]} >= {"design", "process", "nest", "nest/1", "run", "report", "machine"}, res
    assert not res["violations"], "\n".join(res["violations"])


# ----------------------------------------------------------------------------- TabsWhenNarrow
def _flow_pane(n: int) -> QWidget:
    """A pane of ``n`` buttons that wrap (its minimum height depends on its width)."""
    from mlaser.ui.responsive import FlowLayout
    p = QWidget()
    lay = FlowLayout(p, justify=True)
    for i in range(n):
        b = QPushButton(f"Button number {i}")
        b.setMinimumHeight(40)
        lay.addWidget(b)
    return p


def test_panes_laid_out_hidden_keep_their_arrangement(app):
    """A page first laid out hidden at its minimum width (tabs, every pane at its minimum) comes
    back side by side with the default widths, not the squeezed ones (the Nest page's left
    column stayed at its 164 px minimum, one field per row, and the page scrolled)."""
    from mlaser.ui.responsive import TabsWhenNarrow
    top = QWidget()
    split = TabsWhenNarrow(extra=100, parent=top)
    left, mid, right = _flow_pane(6), QLabel("sheet"), _flow_pane(2)
    mid.setMinimumWidth(200)
    for p in (left, mid, right):
        split.addWidget(p, "pane")
    split.set_sizes([400, 500, 250])
    split.splitter.setStretchFactor(1, 1)
    top.resize(260, 600)
    split.resize(260, 600)                    # hidden, at the minimum width: tabs
    top.grab()                                # lays the hidden page out (as the CI harness's grabs)
    _pump(app)
    assert split.tabbed
    top.resize(1200, 600)
    split.resize(1200, 600)
    top.show()
    _pump(app, 0.05)
    try:
        assert not split.tabbed
        sizes = split.splitter.sizes()
        assert sizes[0] >= 350 and sizes[2] >= 220, sizes
    finally:
        top.close()
        top.deleteLater()
        _pump(app)


def test_side_by_side_minimum_height_is_at_the_panes_widths(app):
    """Side by side, the minimum height is each pane's at the width it has (a splitter takes the
    panes' minimums at their minimum widths, where their rows wrap the most)."""
    from mlaser.ui.responsive import TabsWhenNarrow
    top = QWidget()
    split = TabsWhenNarrow(parent=top)
    left = _flow_pane(6)
    split.addWidget(left, "a")
    split.addWidget(QLabel("b"), "b")
    split.set_sizes([1000, 200])
    top.resize(1300, 600)
    split.resize(1300, 600)
    top.show()
    _pump(app, 0.05)
    try:
        narrow = left.layout().minimumHeightForWidth(left.minimumSizeHint().width())
        at = left.layout().minimumHeightForWidth(left.width())
        assert at < narrow
        assert split.minimumSizeHint().height() < narrow
        assert split.minimumSizeHint().height() >= at
    finally:
        top.close()
        top.deleteLater()
        _pump(app)


# ----------------------------------------------------------------------------- the window
@pytest.fixture(scope="module")
def win(app, tmp_path_factory):
    from wiring import dispose, real_window
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("layout_fit")
    for k, sub in (("MLASER_CONFIG_DIR", "cfg"), ("MLASER_APP_DIR", "app"), ("MLASER_DATA_DIR", "data")):
        mp.setenv(k, str(root / sub))
    mp.setattr(QDialog, "exec", lambda self, *a: 0)
    mp.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    w = real_window()
    w.resize(1280, 800)
    w.show()
    _pump(app, 0.2)
    yield w
    dispose(w)
    _pump(app)
    mp.undo()


def test_log_dock_minimum_is_two_lines(win, app):
    """The log dock under the page can give way to two lines of the log (a list that scrolls),
    so a page needs less of a short screen."""
    lp = win.log_panel
    two = lp.tabBar().sizeHint().height() + 2 * lp.text.fontMetrics().lineSpacing() + 2 * lp.text.frameWidth() + 8
    assert lp.minimumSizeHint().height() <= two
    assert lp.minimumSizeHint().height() < 70 + lp.tabBar().sizeHint().height()


def test_monitors_shows_its_first_monitor(win, app):
    """Machine › Monitors opened from the navigation shows the monitor of the tab on top (it was
    an empty frame until another tab was chosen); nothing is built before the page is shown."""
    win.show_workspace("machine")
    _pump(app, 0.1)
    ws = win.shell.host.pages["machine"]
    mw = ws.findChild(QWidget, "machine_workspace") if ws.objectName() != "machine_workspace" else ws
    from mlaser.ui.workspaces.machine import MachineWorkspace
    mw = mw if isinstance(mw, MachineWorkspace) else ws.findChild(MachineWorkspace)
    assert mw is not None
    assert not mw.monitors, "a monitor was built before its page was shown"
    mw.show_section("monitors")
    _pump(app, 0.2)
    first = mw.mon_tabs.widget(mw.mon_tabs.currentIndex())
    assert first.layout().count() == 1, "the tab on top is an empty frame"
    assert first.layout().itemAt(0).widget().isVisible()
    mw.show_section("connection")
    _pump(app)


def test_log_viewer_does_not_scroll_sideways_with_a_large_font(win, app, tmp_path):
    """Issue #3: the log viewer's file list showed a horizontal scroll bar at a 12 pt font; the
    long names are elided, with the whole name in the tooltip."""
    diag = win.extension("ext_diag")
    diag = diag[0] if isinstance(diag, tuple) else diag
    os.makedirs(diag.log_dir, exist_ok=True)
    name = "mlaser_" + "very_long_log_file_name_" * 4 + "2026-11-10.log"
    (Path(diag.log_dir) / name).write_text("2026-11-10 10:00:00 [Info] started\n", encoding="utf-8")
    f0 = app.font()
    f = app.font()
    f.setPointSizeF(12)
    app.setFont(f)
    try:
        from mlaser.ui.ext_diag import LogViewer
        d = LogViewer(diag)
        d.show()
        _pump(app, 0.1)
        try:
            bad = [str(v) for v in A.audit(d) if v.kind == "hscroll"]
            assert not bad, bad
            it = next(d.files.item(i) for i in range(d.files.count()) if d.files.item(i).text() == name)
            assert it.toolTip() == name
        finally:
            d.close()
            d.deleteLater()
    finally:
        app.setFont(f0)
        _pump(app, 0.1)

