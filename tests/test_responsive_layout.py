"""Responsive layout harness (docs/dev/RESPONSIVE.md): the real window (tests/wiring.py, every
built-in plugin and the Onshape plugin when it is installed) at sample sizes, over a sweep of
widths and heights, with the docks dragged from narrow to wide, with a larger font, at 2× scale
and while it is resized live (shrink, then grow): no visible horizontal scroll bar anywhere, no
vertical scroll bar on a form or control panel, no clipped widget (tests/layout_audit.py).

The sizes are *sample points*, not design targets: the layouts adapt to the space they get.

``MLASER_LAYOUT_SHOTS=<dir>`` writes a report (``report.json``, ``report.md``) and a screenshot of
every view at every sample size into ``<dir>``; ``MLASER_LAYOUT_TAG`` names the run (subfolder).
``MLASER_LAYOUT_STEP`` sets the sweep step (default 40 px).  ``MLASER_LAYOUT_STYLE`` runs under
that Qt style (``macOS``, ``windows11``, ``windowsvista``, ``Fusion``, ...: the platform's native
styles on the CI runners); by default the application's own style.  With a real window server
(``QT_QPA_PLATFORM=cocoa`` / ``windows`` / ``xcb``, the native-check workflow) the sizes are
bounded by that screen.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton, QWidget

import layout_audit as A

#: the smallest window the application supports (docs/dev/RESPONSIVE.md)
SUPPORTED_MIN = (1280, 800)
SAMPLES = [(1280, 800), (1440, 900), (1512, 875), (1800, 1169), (1920, 1080)]
#: half a screen (a Windows Snap half of 1366 px, half of a 14" MacBook Pro): the side docks
#: fold into tabs, the run bar and the workspace tool bar wrap (docs/dev/RESPONSIVE.md)
HALF = [(683, 875), (756, 945)]
SWEEP_HEIGHTS = (800, 875, 1169)
STEP = int(os.environ.get("MLASER_LAYOUT_STEP", "40") or 40)
MAX_W = 2560
SAFETY = ("stop", "hold", "arm")
TOUCH_MIN = 40

#: violations in panels another track owns (ui_wave_brief.md "File ownership"): reported, not
#: failed here; the owner's harness entry goes once it is fixed (test_pending_still_needed)
PENDING: dict[str, str] = {}


# ----------------------------------------------------------------------------- the window
def _pump(app, s: float = 0.0, rounds: int = 3) -> None:
    for _ in range(rounds):
        app.processEvents()
    if s:
        t = time.monotonic() + s
        while time.monotonic() < t:
            app.processEvents()
            time.sleep(0.005)


def _load_onshape(win) -> bool:
    """The Onshape plugin, when installed: registered like an enabled entry point, its manifest
    read from the package (an editable install does not list it in the distribution's files)."""
    try:
        import mlaser_onshape
    except ImportError:
        return False
    from mlaser.services.plugin_manifest import parse_manifest
    from mlaser.services.plugins import PluginInfo
    path = Path(mlaser_onshape.__file__).parent / "mlaser-plugin.toml"
    manifest = parse_manifest(path.read_bytes()) if path.exists() else None
    info = PluginInfo("onshape", "mlaser_onshape:plugin", "entry-point", manifest=manifest)
    return bool(win._register(info))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def win(app, tmp_path_factory):
    from wiring import real_window

    from mlaser.services.config import ConfigService
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("responsive")
    for k, sub in (("MLASER_CONFIG_DIR", "cfg"), ("MLASER_APP_DIR", "app"), ("MLASER_DATA_DIR", "data")):
        mp.setenv(k, str(root / sub))
    mp.setattr(QDialog, "exec", lambda self, *a: 0)
    mp.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    for n in ("information", "warning", "critical", "question"):
        mp.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    # the sample sizes are measured whatever the screen (the offscreen one is 800 × 800, a CI
    # runner's 1024 × 768): pages are not capped to it here (FitArea's screen cap is tested on its
    # own; test_fits_this_screen checks the real screen on a window server)
    real_cap = None
    try:
        from mlaser.ui.responsive import FitArea
        real_cap = FitArea._cap
        mp.setattr(FitArea, "_cap", lambda self, size: size)
    except ImportError:          # a checkout before the responsive layout (the baseline run)
        pass
    w = real_window(config=ConfigService())
    onshape = _load_onshape(w)
    w.shell.host.refresh_mode()
    style = os.environ.get("MLASER_LAYOUT_STYLE", "")
    if style:
        from PySide6.QtWidgets import QStyleFactory
        if style.lower() not in [k.lower() for k in QStyleFactory.keys()]:
            pytest.skip(f"style {style} not available here ({QStyleFactory.keys()})")
        _restyle(app)
    w.resize(*SAMPLES[2])
    w.show()
    _pump(app, 0.3)
    w.onshape_loaded = onshape and "onshape" in w.shell.host.specs
    w.real_fitarea_cap = real_cap
    yield w
    try:
        from wiring import dispose
    except ImportError:          # an older checkout (the baseline run)
        w.close()
    else:
        dispose(w)
    _pump(app)
    mp.undo()


def _restyle(app) -> None:
    """``MLASER_LAYOUT_STYLE``: (again) the style under test (a theme switch may set another)."""
    style = os.environ.get("MLASER_LAYOUT_STYLE", "")
    if style and app.style().name().lower() != style.lower():
        app.setStyle(style)


def real_screen() -> bool:
    """A real window server (cocoa, windows, xcb, wayland): the sizes stay within its screen."""
    from PySide6.QtGui import QGuiApplication
    return QGuiApplication.platformName() not in ("offscreen", "minimal", "")


def _screen_size(win) -> QSize:
    scr = win.screen()
    return scr.availableGeometry().size() if scr is not None else QSize(MAX_W, 1600)


def _fit(win, app, size) -> QSize:
    """Resize to ``size`` (never below the window's own minimum, which is what the content needs)."""
    want = QSize(*size)
    settle = 0.03 if real_screen() else 0.0     # a window server answers a resize asynchronously
    stable = 0
    for _ in range(8):            # a page that needs more room at this size raises the minimum
        # the window's minimum as Qt keeps it: the layout's, or the explicit one the dock
        # policy sets while the docks can fold (below the layout's own)
        s = want.expandedTo(win.minimumSize())
        if win.size() != s:
            win.resize(s)
            stable = 0
        _pump(app, settle, rounds=2)
        at_rest(win, app)         # the minimum is read once the relayout has run its course
        if win.size() == want.expandedTo(win.minimumSize()):
            stable += 1
            if stable >= 2:
                break
    return win.size()


def settle(win, app) -> None:
    """Make the view deterministic before it is audited: every periodic refresh of the window
    (the active repeating timers in its tree: the Machine page's 200 ms tick, the Run screen's
    status refresh, ...) runs once now, then the events they post.  Otherwise whether a refresh
    had changed a label's text before the audit depended on how loaded the machine was (a
    test_larger_font failure seen once in a full xdist run, never alone)."""
    from PySide6.QtCore import QTimer
    for t in win.findChildren(QTimer):
        if t.isActive() and not t.isSingleShot() and t.interval() > 0:
            t.timeout.emit()
    # then until the layout is at rest: a page that grows (the Nest options opened) reaches the
    # window's minimum through a chain of layout requests and zero-delay syncs, one event-loop
    # turn each; a fixed number of turns ran out on a loaded worker
    at_rest(win, app)
    if real_screen():
        # a window server applies a grown minimum asynchronously (X11: a round trip to the
        # window manager): wait for that condition, not for a time
        t_end = time.monotonic() + 5.0
        while time.monotonic() < t_end:
            m = win.minimumSize()               # Qt's: the layout's or the dock policy's
            frame = win.frameGeometry().size() - win.size()
            want = m.boundedTo(_screen_size(win) - frame)
            if win.width() >= want.width() and win.height() >= want.height():
                break
            _pump(app, 0.01)
        _pump(app, rounds=3)


def at_rest(win, app) -> None:
    """Pump until nothing is pending: the document's plan delivered, no relayout timer of the
    window's columns panels, column fitters or dock policy armed, no page wrapper sync queued,
    and the layout signature unchanged for three event-loop turns (at most 60 turns)."""
    from PySide6.QtCore import QTimer
    jobs = getattr(win, "jobs", None)
    t = getattr(jobs, "preview", None)
    if jobs is not None and (getattr(jobs, "pending", None) or (t is not None and not t.done_event.is_set())):
        # the document's plan in flight (a selection or a new document starts one, in another
        # process): delivered now, not whenever it happens to arrive (it fills the Process
        # page's estimate card and the preview, so the page's minimum depends on it); a plan
        # already delivered is left alone
        jobs.settle(timeout=60.0)
    from mlaser.ui.responsive import ColumnFitter, ColumnsPanel
    from mlaser.ui.shell.dock_policy import DockPolicy
    layout_timers = [t for t in win.findChildren(QTimer)
                     if isinstance(t.parent(), (ColumnsPanel, ColumnFitter, DockPolicy))]
    last, same = None, 0
    for _ in range(60):
        _pump(app, rounds=1)
        sig = _layout_signature(win)
        same = same + 1 if sig == last else 0
        last = sig
        armed = any(t.isActive() for t in layout_timers if t.isSingleShot())
        if same >= 3 and not sig[-1] and not armed:
            break


def _layout_signature(win) -> tuple:
    """The sizes that change while a relayout travels between a page and the window; the last
    item: a page wrapper still has a sync queued."""
    from mlaser.ui.responsive import FitArea
    m = win.minimumSizeHint()
    out: list = [win.width(), win.height(), m.width(), m.height()]
    st = win.shell.host.stack
    out += [st.width(), st.height(), st.minimumSizeHint().width(), st.minimumSizeHint().height()]
    queued = False
    for fa in win.findChildren(FitArea):
        if fa.isVisible():
            n = fa.need(fa.viewport().width())
            m2 = fa.minimumSizeHint()
            out += [fa.width(), fa.height(), n.width(), n.height(), fa.widget().width(), fa.widget().height(),
                    m2.width(), m2.height()]
            queued = queued or bool(getattr(fa, "_sync_queued", False))
    return (*out, queued)


def unreachable(win) -> bool:
    """On a window server, the window ended up smaller than its own minimum: the screen is
    smaller than this sample (the macOS runner's 1024 × 768 screen keeps windows at most 649
    px high, its window server refuses more).  The content is then squeezed by the screen, not
    by a layout, and the sample is reported as not reachable instead of audited; every size the
    screen can hold is audited as usual, and test_fits_this_screen checks the screen itself."""
    if not real_screen():
        return False
    m = win.minimumSize()
    return win.width() < m.width() or win.height() < m.height()


def views(win, app, deep: bool = True):
    """Every workspace (each tab of a tabbed one) with its default docks, each dock of a tabbed
    dock group raised in turn: yields a label per view."""
    host = win.shell.host
    for ws in host.visible_ids():
        host.activate(ws)
        _pump(app)
        page = host.pages[ws]
        tabs = page.tabs
        n = tabs.count() if (tabs is not None and deep) else 1
        for i in range(n):
            if tabs is not None and deep:
                tabs.setCurrentIndex(i)
                _pump(app)
            label = ws if n == 1 else f"{ws}.{i}"
            yield label
            if deep:
                yield from _sub_views(page, app, label)
            if not deep:
                continue
            for group in _dock_groups(win):
                for d in group[1:]:
                    d.raise_()
                    _pump(app)
                    yield f"{label}+{d.objectName()}"
                group[0].raise_()
                _pump(app)
        if tabs is not None and deep:
            tabs.setCurrentIndex(0)


def _sub_views(page, app, label: str):
    """Views behind a navigation list or a section combo (``responsive_views``: the Machine
    sections, the Setup sections), pages behind a tab bar (``responsive_views`` on a QTabBar:
    Process › Drawing | Library) and panels behind a toggle (``responsive_views`` on a
    checkable button: the Nest options)."""
    from PySide6.QtWidgets import QAbstractButton, QComboBox, QListWidget, QTabBar
    for bar in page.findChildren(QTabBar):
        if not bar.property("responsive_views") or not bar.isVisible():
            continue
        cur = bar.currentIndex()
        for i in range(bar.count()):
            if i == cur:
                continue
            bar.setCurrentIndex(i)
            _pump(app)
            yield f"{label}/{bar.objectName()}/{i}"
        bar.setCurrentIndex(cur)
        _pump(app)
    for b in page.findChildren(QAbstractButton):
        if b.property("responsive_views") and b.isVisible() and b.isCheckable() and not b.isChecked():
            # a page switch (an exclusive group) goes back to the button that was checked; a
            # toggle (the Nest options) is unchecked again
            grp = b.group()
            prev = grp.checkedButton() if grp is not None and grp.exclusive() else None
            b.setChecked(True)
            _pump(app)
            yield f"{label}/{b.objectName()}"
            if prev is not None:
                prev.setChecked(True)
            else:
                b.setChecked(False)
            _pump(app)
    for nav in page.findChildren(QListWidget):
        if not nav.property("responsive_views") or not nav.isVisible():
            continue
        cur = nav.currentRow()
        for r in range(nav.count()):
            if r == cur or nav.item(r).isHidden():
                continue
            nav.setCurrentRow(r)
            _pump(app)
            yield f"{label}/{nav.item(r).text()}"
            for combo in page.findChildren(QComboBox):
                if combo.property("responsive_views") and combo.isVisible():
                    c0 = combo.currentIndex()
                    for i in range(combo.count()):
                        if i != c0:
                            combo.setCurrentIndex(i)
                            _pump(app)
                            yield f"{label}/{nav.item(r).text()}/{combo.itemText(i)}"
                    combo.setCurrentIndex(c0)
                    _pump(app)
        nav.setCurrentRow(cur)
        _pump(app)


def _dock_groups(win) -> list[list]:
    seen: set[int] = set()
    out = []
    for d in win.shell.host.docks():
        if id(d) in seen or not d.isVisible() or d.isFloating():
            continue
        tab = [x for x in win.tabifiedDockWidgets(d) if x.isVisible()]
        if not tab:
            continue
        group = [d, *tab]
        for x in group:
            seen.add(id(x))
        # the raised one first (its title bar is drawn on the tab bar, its contents on top)
        group.sort(key=lambda x: 0 if not x.visibleRegion().isEmpty() else 1)
        out.append(group)
    return out


def _owned_elsewhere(v: A.Violation) -> str:
    for key, owner in PENDING.items():
        if key in v.path:
            return owner
    return ""


def _safety_targets(win, label: str) -> list[str]:
    """STOP, Hold and Arm of the run bar: inside the window, large enough to hit."""
    bad = []
    bar = win.findChild(QWidget, "run_bar")
    # the Run workspace has one set of run controls, its own (UX audit W4): the run bar keeps
    # only its state and progress there
    on_run = getattr(getattr(getattr(win, "shell", None), "host", None), "current", "") == "run"
    for key in SAFETY:
        b = bar.findChild(QPushButton, f"run_{key}") if bar is not None else None
        if on_run:
            if b is not None and b.isVisible():
                bad.append(f"{label}: run bar {key} shown beside the Run screen's")
            b = win.extension("run").screen.buttons.get(key)
        if b is None or not b.isVisible():
            bad.append(f"{label}: run bar {key} hidden")
            continue
        r = b.rect().translated(b.mapTo(win, b.rect().topLeft()))
        if not win.rect().contains(r):
            bad.append(f"{label}: run bar {key} outside the window {r}")
        if min(b.width(), b.height()) < TOUCH_MIN:
            bad.append(f"{label}: run bar {key} {b.width()}x{b.height()} < {TOUCH_MIN}")
    return bad


# ----------------------------------------------------------------------------- reporting
class Report:
    def __init__(self):
        d = os.environ.get("MLASER_LAYOUT_SHOTS", "")
        tag = os.environ.get("MLASER_LAYOUT_TAG", "") or "run"
        style = os.environ.get("MLASER_LAYOUT_STYLE", "") or "default"
        self.dir = Path(d) / tag if d else None
        self.style = style
        self.rows: list[dict] = []
        self.notes: list[str] = []

    def unreachable(self, kind: str, size, label: str) -> None:
        self.rows.append({"run": kind, "size": f"{size.width()}x{size.height()}", "view": label,
                          "style": self.style, "unreachable": True, "violations": []})

    def check(self, kind: str, size, label: str, win) -> None:
        """Audit the window as it is now, after :func:`settle` (see :func:`unreachable`)."""
        settle(win, QApplication.instance())
        if unreachable(win):
            self.unreachable(kind, size, label)
        else:
            self.add(kind, size, label, A.audit(win))

    def add(self, kind: str, size, label: str, violations, allowed=lambda v: "") -> None:
        """``allowed(v)``: a reason the violation is expected here (reported, not failed)."""
        self.rows.append({"run": kind, "size": f"{size.width()}x{size.height()}", "view": label,
                          "style": self.style,
                          "violations": [{"kind": v.kind, "path": v.path, "detail": v.detail,
                                          "owner": _owned_elsewhere(v) or allowed(v)} for v in violations]})

    def shot(self, win, size, label: str) -> None:
        if self.dir is None:
            return
        p = self.dir / "shots" / f"{size.width()}x{size.height()}" / f"{shot_name(label)}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        win.grab().save(str(p))

    def write(self, name: str) -> None:
        if self.dir is None:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{name}.json").write_text(json.dumps(self.rows, indent=1), encoding="utf-8")
        lines = [f"# {name} ({self.style})", ""]
        tot: dict[str, int] = {}
        for r in self.rows:
            for v in r["violations"]:
                tot[v["kind"]] = tot.get(v["kind"], 0) + 1
        lines.append("totals: " + ", ".join(f"{k} {n}" for k, n in sorted(tot.items())) if tot else "totals: none")
        lines.append("")
        uniq: dict[tuple, list[str]] = {}
        for r in self.rows:
            for v in r["violations"]:
                uniq.setdefault((v["kind"], v["path"], v["owner"]), []).append(f'{r["size"]} {r["view"]}')
        skipped = sorted({r["size"] for r in self.rows if r.get("unreachable")})
        if skipped:
            lines.append(f"not reachable on this screen (window below its minimum): {', '.join(skipped)}")
            lines.append("")
        for n in self.notes:
            lines.append(f"note: {n}")
        if self.notes:
            lines.append("")
        lines.append(f"distinct violations: {len(uniq)}")
        lines.append("")
        for (kind, path, owner), where in sorted(uniq.items()):
            lines.append(f"* **{kind}** `{path}`" + (f" (owner: {owner})" if owner else ""))
            lines.append(f"  - {len(where)}×, e.g. {', '.join(where[:4])}")
        (self.dir / f"{name}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def shot_name(label: str) -> str:
    """A view label as a portable relative path: the label's ``/`` keep their sub-folders, and
    every character that a Windows file name or an artifact upload refuses (``: * ? " < > |``,
    e.g. the Machine section "Axes: common drive settings") becomes ``_``."""
    bad = set('\\:*?"<>|')
    parts = ["".join("_" if (c in bad or ord(c) < 32) else c for c in part).strip(" .") or "_"
             for part in label.split("/")]
    return "/".join(parts)


def _fail_text(rows) -> str:
    if rows and all(r.get("unreachable") for r in rows):
        return "no sample size was reachable on this screen: nothing was audited"
    out = []
    for r in rows:
        for v in r["violations"]:
            if not v["owner"]:
                out.append(f'{r["size"]} {r["view"]}: {v["kind"]} {v["path"]} {v["detail"]}')
    return "\n".join(dict.fromkeys(out))


def _size_id(size) -> str:
    return f"{size[0]}x{size[1]}"


# ----------------------------------------------------------------------------- tests
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_panels_with_a_selection(win, app, theme):
    """The panels that follow the selection, with one: the Properties dock in Design (Transform
    and Layer) and in Process (every CAM section; a multi-contour part's contour picked), the
    layer inspector with a layer chosen, the Library page; light and dark.  The window keeps
    the sample size (the Process page does not push it taller)."""
    from mlaser import ops
    rep = Report()
    doc0 = win.document
    win.set_document(ops.demo_document())
    host = win.shell.host
    grew = []
    try:
        win.shell.set_theme(theme, persist=False)
        _restyle(app)
        for size in SAMPLES:
            for ws in ("design", "process"):
                host.activate(ws)
                win.canvas.select_parts([0])
                _pump(app)
                s = _fit(win, app, size)
                if s.height() > size[1]:
                    grew.append(f"{theme} {ws} {size}: window {s.width()}x{s.height()}")
                _pump(app, 0.02)
                rep.check("selection", s, f"{theme}_{ws}", win)
                rep.shot(win, s, f"selection_{theme}_{ws}")
                if ws != "process":
                    continue
                pp = win.extension("ext_process").properties
                pp.pick_contour(1)                      # a hole of the plate: its own values
                _pump(app)
                rep.check("selection", s, f"{theme}_{ws}_contour", win)
                pp.pick_contour(None)
                pw = win.extension("ext_process").workspace
                pw.show_page(1)
                _pump(app)
                s = _fit(win, app, size)
                rep.check("selection", s, f"{theme}_{ws}_library", win)
                rep.shot(win, s, f"selection_{theme}_library")
                pw.show_page(0)
                _pump(app)
    finally:
        win.shell.set_theme("light", persist=False)
        _restyle(app)
        win.canvas.select_parts([])
        win.set_document(doc0)
        _pump(app)
    rep.write(f"selection_{theme}")
    text = _fail_text(rep.rows)
    assert not text and not grew, text + "\n" + "\n".join(grew)


def test_minimum_size_fits_the_smallest_sample(win, app):
    """What the content needs (docs/dev/RESPONSIVE.md): at most 1280 × 800 at the default font."""
    win.shell.host.activate("design")
    _pump(app)
    m = win.minimumSizeHint().expandedTo(win.minimumSize())
    assert m.width() <= 1280 and m.height() <= 800, m


@pytest.mark.parametrize("size", SAMPLES + HALF, ids=_size_id)
def test_sample_sizes(win, app, size):
    rep = Report()
    s = _fit(win, app, size)
    _pump(app, 0.05)
    for label in views(win, app):
        rep.check("sample", s, label, win)
        rep.shot(win, s, label)
    rep.write(f"sample_sizes_{_size_id(size)}")
    text = _fail_text(rep.rows)
    assert not text, text


#: the workspaces every screen the application supports must show without scrolling
CORE = ("design", "run", "machine")


def test_fits_this_screen(win, app):
    """On a window server (the native-check runners: macOS, Windows, X11), the core workspaces
    fit the real screen: their minimum is within its available area and, at that size, nothing
    scrolls or clips (the screen cap of the page wrapper included)."""
    if not real_screen():
        pytest.skip("needs a window server (QT_QPA_PLATFORM=cocoa / windows / xcb)")
    from mlaser.ui.responsive import FitArea
    mp = pytest.MonkeyPatch()
    mp.setattr(FitArea, "_cap", win.real_fitarea_cap)          # the real screen cap
    avail = _screen_size(win)
    # a screen smaller than the smallest supported window (the CI runners' 1024 × 768): the page
    # wrapper scrolling its page is the documented fallback there (docs/dev/RESPONSIVE.md rule
    # 6); nothing else may scroll or clip
    small = avail.width() < SUPPORTED_MIN[0] or avail.height() < SUPPORTED_MIN[1]

    def below(v: A.Violation) -> str:
        if small and v.kind in ("vscroll", "hscroll") and v.path.endswith("FitArea#shell_ws_scroll"):
            return f"screen {avail.width()}x{avail.height()} below the supported {SUPPORTED_MIN[0]}x{SUPPORTED_MIN[1]}"
        return ""
    rep = Report()
    bad = []
    for ws in CORE:
        win.shell.host.activate(ws)
        _pump(app, rounds=8)
        frame = win.frameGeometry().size() - win.size()
        m = win.minimumSize()
        over = m.width() + frame.width() > avail.width() or m.height() + frame.height() > avail.height()
        if over and small:
            # the window's minimum (at most 1280 x 800: test_minimum_size_fits_the_smallest_sample)
            # is larger than an unsupported screen: reported, not failed
            rep.notes.append(f"{ws}: minimum {m.width()}x{m.height()} > screen {avail.width()}x{avail.height()} "
                             f"(below the supported {SUPPORTED_MIN[0]}x{SUPPORTED_MIN[1]})")
        elif over:
            bad.append(f"{ws}: minimum {m.width()}x{m.height()} > screen {avail.width()}x{avail.height()}")
        s = _fit(win, app, (avail.width() - frame.width(), avail.height() - frame.height()))
        settle(win, app)
        rep.add("screen", s, ws, A.audit(win), allowed=below)
        rep.shot(win, s, f"screen_{ws}")
    mp.undo()
    rep.write("this_screen")
    text = _fail_text(rep.rows)
    assert not text and not bad, text + "\n" + "\n".join(bad)


@pytest.mark.parametrize("size", SAMPLES, ids=_size_id)
def test_imperial_sample_sizes(win, app, size):
    """Imperial display units (longer texts: "0.3937 in", "393.70 in/min"): no scroll bar, no
    clipped widget either."""
    rep = Report()
    win.config.set("units.system", "imperial")
    try:
        _pump(app, 0.05)
        s = _fit(win, app, size)
        for label in views(win, app):
            rep.check("imperial", s, label, win)
            if size == (1512, 875):
                rep.shot(win, s, f"imperial_{label}")
    finally:
        win.config.set("units.system", "metric")
        _pump(app, 0.05)
    rep.write(f"imperial_{_size_id(size)}")
    text = _fail_text(rep.rows)
    assert not text, text


def test_safety_controls_visible_everywhere(win, app):
    """STOP, Hold and Arm in every workspace at every sample size, light and dark, and the Run
    screen's targets at least TOUCH_MIN px."""
    bad = []
    for theme in ("light", "dark"):
        win.shell.set_theme(theme, persist=False)
        _restyle(app)
        for size in SAMPLES:
            s = _fit(win, app, size)
            for label in views(win, app, deep=False):
                bad += _safety_targets(win, f"{theme} {s.width()}x{s.height()} {label}")
            win.shell.host.activate("run")
            _pump(app)
            scr = win.extension("run").screen
            for key in ("stop", "hold", "start", "arm"):
                b = scr.buttons[key]
                if not b.isVisible() or min(b.width(), b.height()) < TOUCH_MIN:
                    bad.append(f"{theme} {s.width()}x{s.height()} run screen {key} {b.size()}")
            for b in scr.findChildren(QPushButton):
                if b.isVisible() and b.height() < TOUCH_MIN:
                    bad.append(f"{theme} {s.width()}x{s.height()} run screen target {b.objectName() or b.text()} "
                               f"{b.width()}x{b.height()}")
    win.shell.set_theme("light", persist=False)
    _restyle(app)
    assert not bad, "\n".join(dict.fromkeys(bad))


def _sweep_sizes(win):
    m = win.minimumSize()
    w0 = max(m.width(), 1)
    heights = sorted({max(m.height(), h) for h in (m.height(), *SWEEP_HEIGHTS)})
    if real_screen():
        # a window server keeps a window within its screen: heights above that are the same
        # size again (the macOS runner: every height was 649, and each was swept)
        frame = win.frameGeometry().size() - win.size()
        top = _screen_size(win).height() - frame.height()
        heights = sorted({min(h, max(top, m.height())) for h in heights})
    widths = list(range(w0, MAX_W + 1, STEP))
    if widths[-1] != MAX_W:
        widths.append(MAX_W)
    return widths, heights


def _window_note(win, s) -> str:
    """What the window and its page ask for at a size with a violation (for a failure seen on
    one platform only: is the window narrower than its layout needs?)."""
    def wh(q) -> str:
        return f"{q.width()}x{q.height()}"
    c = win.centralWidget()
    pol = getattr(getattr(win, "shell", None), "dock_policy", None)
    folded = getattr(pol, "folded", None)
    return (f"  ({wh(s)}: window minimum {wh(win.minimumSize())}, its layout's {wh(win.minimumSizeHint())}; "
            f"page {wh(c.size())}, its minimum {wh(c.minimumSizeHint())}; docks "
            f"{'folded, narrow=' + str(folded.get('narrow')) if folded is not None else 'side by side'})")


#: the sweep's cases: one per workspace and height row, which xdist spreads over its workers (as
#: one test the sweep took 8 minutes on one worker).  A workspace not named here (a plugin's) is
#: swept by the "other" case, at every height.
SWEEP_WORKSPACES = ("design", "process", "nest", "run", "report", "machine")
SWEEP_ROWS = ("min", *SWEEP_HEIGHTS)
SWEEP_CASES = [*((ws, row) for ws in SWEEP_WORKSPACES for row in SWEEP_ROWS), ("other", "all")]


def _sweep_heights(win, rows) -> list[int]:
    """The heights of the sweep rows ``rows`` (``"min"``: the window's minimum; SWEEP_HEIGHTS),
    as :func:`_sweep_sizes` has them: a row whose height an earlier row already has (a minimum
    above it, a screen that holds less) is swept by that row, not again."""
    m = win.minimumSize().height()

    def height(row) -> int:
        h = m if row == "min" else max(m, row)
        if real_screen():         # within the screen, as in _sweep_sizes
            frame = win.frameGeometry().size() - win.size()
            h = min(h, max(_screen_size(win).height() - frame.height(), m))
        return h
    return [height(r) for i, r in enumerate(SWEEP_ROWS)
            if r in rows and height(r) not in [height(e) for e in SWEEP_ROWS[:i]]]


@pytest.mark.parametrize("ws,row", SWEEP_CASES, ids=[f"{w}-{r}" for w, r in SWEEP_CASES])
def test_width_and_height_sweep(win, app, ws, row):
    """Every width from the window's minimum to 2560 (STEP px) at several heights, every
    workspace: no violation, the safety controls in the window."""
    rep = Report()
    host = win.shell.host
    ids = host.visible_ids()
    if ws == "other":
        todo = [x for x in ids if x not in SWEEP_WORKSPACES]
    else:
        assert ws in ids, f"workspace {ws} is not shown ({ids}): update SWEEP_WORKSPACES"
        todo = [ws]
    bad = []
    notes: list[str] = []
    for ws_id in todo:
        host.activate(ws_id)
        _pump(app)
        widths, _ = _sweep_sizes(win)
        seen: set[tuple[int, int]] = set()
        for h in _sweep_heights(win, SWEEP_ROWS if row == "all" else (row,)):
            for wd in widths:
                s = _fit(win, app, (wd, h))
                if (s.width(), s.height()) in seen:
                    continue          # the same size again (a screen that holds less)
                seen.add((s.width(), s.height()))
                rep.check("sweep", s, ws_id, win)
                if any(not v["owner"] for v in rep.rows[-1]["violations"]):
                    notes.append(_window_note(win, s))
                bad += _safety_targets(win, f"{s.width()}x{s.height()} {ws_id}")
                # the page in the middle has its minimum width: a window minimum below what
                # the docks around it need squeezed it (Process at its minimum size, the docks
                # from under the page in the folded side group: 245 of 260 px here, 224 on
                # Windows, where the canvas bar and the empty-state panel were cut)
                c = win.centralWidget()
                if c.width() < c.minimumSizeHint().width():
                    bad.append(f"{s.width()}x{s.height()} {ws_id}: the page is {c.width()} px wide, its "
                               f"minimum is {c.minimumSizeHint().width()}")
                    notes.append(_window_note(win, s))
    rep.write(f"sweep_{ws}_{row}")
    text = _fail_text(rep.rows)
    assert not text and not bad, text + "\n" + "\n".join(bad + notes)


@pytest.mark.parametrize("pt", [0, 10])
def test_half_screen_window(win, app, pt):
    """Every workspace takes half a screen (683 px wide): the window gets that width, also
    with a 10 pt font (Windows' metrics are wider than the offscreen default's)."""
    f0 = app.font()
    if pt:
        f = app.font()
        f.setPointSizeF(pt)
        app.setFont(f)
        _restyle(app)
        _pump(app, 0.1)
    try:
        for ws in win.shell.host.visible_ids():
            win.shell.host.activate(ws)
            s = _fit(win, app, HALF[0])
            assert s.width() == HALF[0][0], (pt, ws, s, win.minimumSize(), win.minimumSizeHint())
    finally:
        if pt:
            app.setFont(f0)
            _pump(app, 0.1)
        _fit(win, app, SAMPLES[2])


@pytest.mark.parametrize("ws", ["design", "process"])
@pytest.mark.parametrize("size", [HALF[0], (1280, 800), (1512, 875), (1920, 1080)], ids=_size_id)
def test_dock_width_sweep(win, app, size, ws):
    """The side docks dragged from their minimum to wide: their panels reflow, nothing clips."""
    rep = Report()
    host = win.shell.host
    s = _fit(win, app, size)
    if ws in host.visible_ids():
        host.activate(ws)
        _pump(app)
        for d in host.docks():
            if not d.isVisible() or d.isFloating() or win.dockWidgetArea(d) not in (
                    Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea):
                continue
            lo = max(d.minimumSizeHint().width(), 1)
            for wd in range(lo, min(lo + 700, s.width() // 2) + 1, STEP):
                win.resizeDocks([d], [wd], Qt.Horizontal)
                _pump(app)
                _pump(app)
                rep.check("docks", s, f"{ws}:{d.objectName()}={d.width()}", win)
        win.shell.reset_layout()
        _pump(app)
    rep.write(f"dock_sweep_{_size_id(size)}_{ws}")
    text = _fail_text(rep.rows)
    assert not text, text


@pytest.mark.parametrize("ws", ["design", "run", "machine"])
def test_live_resize_recovers(win, app, ws):
    """Shrink from 2560 to the minimum and grow back (STEP px): no violation on the way and the
    same layout at the end as at the start (no state stuck from the narrow end)."""
    rep = Report()
    host = win.shell.host
    widths, _ = _sweep_sizes(win)
    if ws in host.visible_ids():
        host.activate(ws)
        s0 = _fit(win, app, (1512, 875))
        _pump(app, 0.05)
        before = _geometry(win)
        report0 = _dock_report(win)
        for wd in [*reversed(widths), *widths, 1512]:
            s = _fit(win, app, (wd, 875))
            rep.check("live", s, ws, win)
        _fit(win, app, (s0.width(), s0.height()))
        _pump(app, 0.05)
        after = _geometry(win)
        diff = {k: (before.get(k), after.get(k)) for k in set(before) | set(after)
                if not _same(before.get(k), after.get(k))}
        assert not diff, (ws, diff, "before:", report0, "after:", _dock_report(win))
    rep.write(f"live_resize_{ws}")
    text = _fail_text(rep.rows)
    assert not text, text


def _dock_report(win) -> dict:
    """Why a dock has its width (a failed recovery's message): natural and minimum widths,
    the user's remembered width, the panel's hint."""
    pol = getattr(win.shell, "dock_policy", None) or getattr(win, "dock_policy", None)
    out = {}
    for d in win.shell.host.docks():
        if d.isVisible() and not d.isFloating():
            inner = d.widget()
            out[d.objectName()] = {
                "w": d.width(), "min": d.minimumSizeHint().width(),
                "hint": inner.sizeHint().width() if inner is not None else None,
                "pref": pol.prefs.get(d.objectName()) if pol is not None else None,
                "tabbed": [x.objectName() for x in win.tabifiedDockWidgets(d)],
                "parts": [(type(c).__name__, c.sizeHint().width()) for c in
                          (inner.findChildren(QWidget, options=Qt.FindDirectChildrenOnly) if inner is not None else [])
                          if c.isVisible()][:6]}
    c = win.centralWidget()
    out["window"] = {"size": (win.width(), win.height()),
                     "central": (c.width(), c.minimumSizeHint().width()) if c is not None else None,
                     "page_min": win.shell.host.stack.minimumSizeHint().width(),
                     "targets": {d.objectName(): t for d, t in pol.targets().items()} if pol is not None else None}
    return out


def _same(a, b, tol: int = 4) -> bool:
    """Equal states; dock sizes within ``tol`` px (a dock's natural width can move by a few px
    with its content's size hint, e.g. a label that was elided)."""
    if isinstance(a, tuple) and isinstance(b, tuple):
        return all(abs(x - y) <= tol for x, y in zip(a, b))
    return a == b


def _geometry(win) -> dict:
    out = {}
    for d in win.shell.host.docks():
        if d.isVisible() and not d.isFloating():
            if win.tabifiedDockWidgets(d) and d.visibleRegion().isEmpty():
                continue      # a tab behind another: its geometry is not shown (nor kept current)
            out[f"dock:{d.objectName()}"] = (d.width(), d.height())
    for w in win.findChildren(QWidget):
        state = w.property("responsive_state")
        if state and w.isVisible():
            out[f"state:{A.widget_path(w, win)}"] = state
    return out


def test_larger_font(win, app):
    """A larger system font (125 %): the layouts reflow, the window minimum grows if it must."""
    rep = Report()
    f0 = app.font()
    f = app.font()
    f.setPointSizeF((f0.pointSizeF() if f0.pointSizeF() > 0 else 10) * 1.25)
    app.setFont(f)
    try:
        _pump(app, 0.1)
        for size in ((1512, 875), (1920, 1080)):
            s = _fit(win, app, size)
            for label in views(win, app):
                rep.check("font125", s, label, win)
                rep.shot(win, s, f"font125_{label}")
    finally:
        app.setFont(f0)
        _pump(app, 0.1)
    rep.write("larger_font")
    text = _fail_text(rep.rows)
    assert not text, text


SCALE_SCRIPT = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
app = QApplication([])
QDialog.exec = lambda self, *a: 0
for n in ("information", "warning", "critical", "question"):
    setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
from wiring import real_window
import layout_audit as A
w = real_window()
if os.environ.get("MLASER_LAYOUT_STYLE"):
    app.setStyle(os.environ["MLASER_LAYOUT_STYLE"])
out = []
# first start: the whole available screen, never larger
w.shell.fit_to_screen()
w.show()
for _ in range(10):
    app.processEvents()
avail = w.screen().availableGeometry()
first = [w.frameGeometry().x(), w.frameGeometry().y(), w.frameGeometry().width(), w.frameGeometry().height()]
for size in ((1280, 800), (1512, 875)):
    w.resize(*size)
    for _ in range(5):
        app.processEvents()
    for ws in w.shell.host.visible_ids():
        w.shell.host.activate(ws)
        for _ in range(3):
            app.processEvents()
        out += [f"{size} {ws}: {v}" for v in A.audit(w)]
        shots = os.environ.get("MLASER_LAYOUT_SHOTS_2X")
        if shots:
            os.makedirs(shots, exist_ok=True)
            w.grab().save(os.path.join(shots, f"{size[0]}x{size[1]}_{ws}.png"))
# a saved geometry larger than this screen (another monitor) comes back clamped
w.resize(2400, 1400)
w.move(300, 200)
for _ in range(3):
    app.processEvents()
w.shell._on_close()
w.resize(900, 600)
w.shell.fit_to_screen()
for _ in range(5):
    app.processEvents()
g = w.frameGeometry()
print(json.dumps({"dpr": w.screen().devicePixelRatio(), "grab_dpr": w.grab().devicePixelRatio(), "violations": out, "avail": [avail.x(), avail.y(), avail.width(),
                  avail.height()], "first": first, "restored": [g.x(), g.y(), g.width(), g.height()]}))
w.close()
"""

#: the user's MacBook Pro 14" screen: 3024 × 1964 pixels at 2x = 1512 × 982 points (offscreen
#: platform screen; QT_SCALE_FACTOR=2 gives the widgets their 2x backing store)
#: Qt styles that only work on their own platform plugin (cocoa, windows): the macOS style under
#: the offscreen platform crashed the 2x child with SIGSEGV on the macOS runner
NATIVE_ONLY_STYLES = ("macos", "windows11", "windowsvista")

SCREEN_2X = {"screens": [{"name": "mbp14", "x": 0, "y": 0, "width": 3024, "height": 1964,
                          "logicalDpi": 96, "dpr": 1}]}


def test_scale_2x_and_first_start_geometry(tmp_path):
    """2x device pixel ratio (Retina, 200 % on Windows) on a 1512 × 982 screen: the same layout
    in logical pixels; the first start fills the available screen; a saved geometry larger than
    the screen comes back inside it."""
    style = os.environ.get("MLASER_LAYOUT_STYLE", "").lower()
    if style in NATIVE_ONLY_STYLES:
        pytest.skip(f"the offscreen platform cannot host the {style} style (it needs its own window "
                    "server); the 2x run is the CI job's, under the default style")
    cfg = tmp_path / "screen.json"
    cfg.write_text(json.dumps(SCREEN_2X), encoding="utf-8")
    # a path relative to the child's working directory: the platform arguments are split at
    # ":", so a Windows drive letter ("D:\\...") cut the file name and the child crashed
    # (0xC0000409) before it printed anything
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen:configfile=screen.json",
               MLASER_CONFIG_DIR=str(tmp_path / "cfg"), MLASER_APP_DIR=str(tmp_path / "app"),
               MLASER_DATA_DIR=str(tmp_path / "data"))
    env.pop("_MLASER_CONFIG_AUTO", None)
    env["QT_SCALE_FACTOR"] = "2"
    d = os.environ.get("MLASER_LAYOUT_SHOTS", "")
    if d:
        env["MLASER_LAYOUT_SHOTS_2X"] = str(Path(d).resolve() / (os.environ.get("MLASER_LAYOUT_TAG", "") or "run") / "shots_2x")
    r = subprocess.run([sys.executable, "-c", SCALE_SCRIPT, str(Path(__file__).parent.resolve())], env=env,
                       capture_output=True, text=True, timeout=600, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr[-3000:]
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["dpr"] == 2.0 and res["grab_dpr"] == 2.0
    ax, ay, aw, ah = res["avail"]
    assert (aw, ah) == (1512, 982)
    assert res["first"] == res["avail"], res
    x, y, w, h = res["restored"]
    assert ax <= x and ay <= y and x + w <= ax + aw and y + h <= ay + ah, res
    bad = [v for v in res["violations"] if not any(k in v for k in PENDING)]
    assert not bad, "\n".join(bad)


SMALL_SCRIPT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
app = QApplication([])
QDialog.exec = lambda self, *a: 0
for n in ("information", "warning", "critical", "question"):
    setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
from wiring import real_window
w = real_window()
w.shell.fit_to_screen()
w.show()
for _ in range(10):
    app.processEvents()
avail = w.screen().availableGeometry()
seen = []
for ws in w.shell.host.visible_ids():
    w.shell.host.activate(ws)
    for _ in range(5):
        app.processEvents()
    w.shell._clamp_to_screen()
    for _ in range(3):
        app.processEvents()
    g = w.frameGeometry()
    seen.append([ws, g.x(), g.y(), g.width(), g.height()])
print(json.dumps({"avail": [avail.x(), avail.y(), avail.width(), avail.height()], "seen": seen}))
w.close()
"""


def test_a_screen_below_the_supported_size_keeps_the_window_on_it(tmp_path):
    """A 1024 × 768 screen (below the supported 1280 × 800; the macOS CI runner's): the window
    is never placed partly off the screen (macOS showed the rail and the run bar's left end cut
    off); the workspace page scrolls instead."""
    style = os.environ.get("MLASER_LAYOUT_STYLE", "").lower()
    if style in NATIVE_ONLY_STYLES:
        pytest.skip(f"the offscreen platform cannot host the {style} style")
    cfg = tmp_path / "screen.json"
    cfg.write_text(json.dumps({"screens": [{"name": "small", "x": 0, "y": 0, "width": 1024, "height": 768,
                                            "logicalDpi": 96, "dpr": 1}]}), encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen:configfile=screen.json",
               MLASER_CONFIG_DIR=str(tmp_path / "cfg"), MLASER_APP_DIR=str(tmp_path / "app"),
               MLASER_DATA_DIR=str(tmp_path / "data"))
    env.pop("_MLASER_CONFIG_AUTO", None)
    r = subprocess.run([sys.executable, "-c", SMALL_SCRIPT, str(Path(__file__).parent.resolve())], env=env,
                       capture_output=True, text=True, timeout=600, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr[-3000:]
    res = json.loads(r.stdout.strip().splitlines()[-1])
    ax, ay, aw, ah = res["avail"]
    off = [s for s in res["seen"] if s[1] < ax or s[2] < ay or s[1] + s[3] > ax + aw or s[2] + s[4] > ay + ah]
    assert not off, (res["avail"], off)


AT_REST_SCRIPT = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
app = QApplication([])
QDialog.exec = lambda self, *a: 0
for n in ("information", "warning", "critical", "question"):
    setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
from mlaser.ui import responsive as R
passes = {}
_relayout = R.ColumnsPanel._relayout
def counted(self, W, H):
    k = self.objectName() or type(self).__name__
    passes[k] = passes.get(k, 0) + 1
    return _relayout(self, W, H)
R.ColumnsPanel._relayout = counted
from wiring import real_window
w = real_window()
mode = sys.argv[2]
if mode == "maximized":
    w.showMaximized()
else:
    w.resize(1400, 900)
    w.show()
    assert w.connect_simulator(time_scale=1.0)
def pump(sec):
    t = time.monotonic()
    while time.monotonic() - t < sec:
        app.processEvents()
pump(1.5)
out = {}
for ws in ("design", "run"):
    w.shell.host.activate(ws)
    pump(1.0)
    passes.clear()
    pump(0.8)
    out[ws] = dict(passes)
print(json.dumps({"state": int(w.windowState().value), "size": [w.width(), w.height()], "passes": out}))
sys.stdout.flush()
# a clean exit: the window closed (simulator disconnected) and deleted, with every page wrapper
# and its event filter, before the interpreter goes (os._exit with the window alive crashed on
# Windows: Qt's teardown in the DLL detach still delivered events to a FitArea's filter)
R.ColumnsPanel._relayout = _relayout
from wiring import dispose
dispose(w)
del w
app.processEvents()
"""


@pytest.mark.sim          # one worker with the timing tests: a whole app in a subprocess loads the runner
@pytest.mark.parametrize("mode", ["maximized", "connected"])
def test_window_at_rest_does_not_relayout(tmp_path, mode):
    """Issue #4: a maximized window (the 1280 x 800 screen, the smallest supported) and a
    1400 x 900 window connected to the simulator come to rest: no panel lays itself out again
    and again.  Run Control, compact at that height, used to be laid out 75 times a second for
    ever (each pass measures it comfortable and turns it compact again, which invalidated every
    layout in it and so posted the requests of the next pass): the panel jittered in a
    maximized window on macOS and ``processEvents`` could stay busy without returning."""
    style = os.environ.get("MLASER_LAYOUT_STYLE", "").lower()
    if style in NATIVE_ONLY_STYLES:
        pytest.skip(f"the offscreen platform cannot host the {style} style")
    cfg = tmp_path / "screen.json"
    screen = (1280, 800) if mode == "maximized" else (800, 600)
    cfg.write_text(json.dumps({"screens": [{"name": "s", "x": 0, "y": 0, "width": screen[0], "height": screen[1],
                                            "logicalDpi": 96, "dpr": 1}]}), encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen:configfile=screen.json",
               MLASER_CONFIG_DIR=str(tmp_path / "cfg"), MLASER_APP_DIR=str(tmp_path / "app"),
               MLASER_DATA_DIR=str(tmp_path / "data"))
    env.pop("_MLASER_CONFIG_AUTO", None)
    root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join([root, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    r = subprocess.run([sys.executable, "-c", AT_REST_SCRIPT, str(Path(__file__).parent.resolve()), mode], env=env,
                       capture_output=True, text=True, timeout=600, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr[-3000:]
    res = json.loads(r.stdout.strip().splitlines()[-1])
    if mode == "maximized":
        assert res["state"] & Qt.WindowMaximized.value, res
    # at rest: at most a pass or two (a periodic refresh may change a label), never a stream
    busy = {ws: {k: n for k, n in p.items() if n > 3} for ws, p in res["passes"].items()}
    assert not any(busy.values()), res


IDLE_SCRIPT = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
app = QApplication([])
QDialog.exec = lambda self, *a: 0
for n in ("information", "warning", "critical", "question"):
    setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
from wiring import dispose, real_window
from mlaser.controller.ui_adapter import UiController
from mlaser.ui import docops
w = real_window(UiController(poll_s=0.05), user_mode="engineer")
w.shell.fit_to_screen()             # as the application starts: the whole screen
w.show()
def idle(sec):
    # the GUI thread's CPU time while the event loop waits (not a busy processEvents loop)
    loop = QEventLoop()
    QTimer.singleShot(int(sec * 1000), loop.quit)
    t0 = time.thread_time()
    loop.exec()
    return (time.thread_time() - t0) / sec
idle(1.0)
assert w.connect_simulator(time_scale=1.0)
w.set_document(docops.demo_document())
idle(1.0)
import collections
from PySide6.QtCore import QEvent, QObject
class Count(QObject):
    # after the measurement: what the GUI thread handled in one more idle second (shown when
    # the test fails; the filter's own cost is not in the measurement)
    def __init__(self):
        super().__init__()
        self.n = collections.Counter()
    def eventFilter(self, o, e):
        self.n[f"{type(o).__name__}/{o.objectName()}/{QEvent.Type(e.type()).name}"] += 1
        return False
out, events = {}, {}
for ws in ("machine", "run", "design"):
    w.show_workspace(ws)
    idle(1.0)
    out[ws] = idle(2.0)
    c = Count()
    app.installEventFilter(c)
    idle(1.0)
    app.removeEventFilter(c)
    events[ws] = c.n.most_common(12)
print(json.dumps({"size": [w.width(), w.height()], "cpu": out, "events": events}))
sys.stdout.flush()
dispose(w)
del w
app.processEvents()
"""


@pytest.mark.sim          # one worker with the timing tests (see above)
@pytest.mark.parametrize("screen", [(800, 600), (1366, 768)])
def test_idle_gui_thread_stays_idle(tmp_path, screen):
    """An idle window connected to the simulator costs the GUI thread little CPU, also on a
    screen smaller than the content's minimum (800 x 600: the window is held to the screen).
    There the dock policy asked for the same dock widths on every turn, which the window could
    not give; each resizeDocks relaid the window out and scheduled the policy again: about 97 %
    of a core at idle."""
    style = os.environ.get("MLASER_LAYOUT_STYLE", "").lower()
    if style in NATIVE_ONLY_STYLES:
        pytest.skip(f"the offscreen platform cannot host the {style} style")
    cfg = tmp_path / "screen.json"
    cfg.write_text(json.dumps({"screens": [{"name": "s", "x": 0, "y": 0, "width": screen[0], "height": screen[1],
                                            "logicalDpi": 96, "dpr": 1}]}), encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen:configfile=screen.json",
               MLASER_CONFIG_DIR=str(tmp_path / "cfg"), MLASER_APP_DIR=str(tmp_path / "app"),
               MLASER_DATA_DIR=str(tmp_path / "data"))
    env.pop("_MLASER_CONFIG_AUTO", None)
    root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join([root, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    r = subprocess.run([sys.executable, "-c", IDLE_SCRIPT, str(Path(__file__).parent.resolve())], env=env,
                       capture_output=True, text=True, timeout=600, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr[-3000:]
    res = json.loads(r.stdout.strip().splitlines()[-1])
    # the loop this guards against held the GUI thread at ~100 %; a busy CI runner measures a
    # few per cent for an idle window (5.5 % once on Windows), so the bound leaves room for that
    busy = {ws: round(c, 3) for ws, c in res["cpu"].items() if c >= 0.15}
    assert not busy, res


def test_pending_still_needed(win, app):
    """An entry of PENDING that no longer matches anything goes (the owner fixed it)."""
    if not PENDING:
        return
    hit: set[str] = set()
    for size in ((1280, 800), (1512, 875)):
        _fit(win, app, size)
        for _label in views(win, app):
            for v in A.audit(win):
                for k in PENDING:
                    if k in v.path:
                        hit.add(k)
    assert set(PENDING) <= hit, sorted(set(PENDING) - hit)
