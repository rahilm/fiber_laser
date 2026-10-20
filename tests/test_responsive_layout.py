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
        s = want.expandedTo(win.minimumSizeHint()).expandedTo(win.minimumSize())
        if win.size() != s:
            win.resize(s)
            stable = 0
        _pump(app, settle, rounds=2)
        at_rest(win, app)         # the minimum is read once the relayout has run its course
        if win.size() == want.expandedTo(win.minimumSizeHint()).expandedTo(win.minimumSize()):
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
            m = win.minimumSizeHint().expandedTo(win.minimumSize())
            frame = win.frameGeometry().size() - win.size()
            want = m.boundedTo(_screen_size(win) - frame)
            if win.width() >= want.width() and win.height() >= want.height():
                break
            _pump(app, 0.01)
        _pump(app, rounds=3)


def at_rest(win, app) -> None:
    """Pump until nothing is pending: the document's plan delivered, no zero-delay timer of the
    window's panels is armed (a
    columns panel's or a column fitter's relayout), no page wrapper has a sync queued, and the
    layout signature has not changed for three event-loop turns."""
    from PySide6.QtCore import QTimer
    jobs = getattr(win, "jobs", None)
    if jobs is not None:
        # the document's plan (a selection or a new document starts one, in another process):
        # delivered now, not whenever it happens to arrive (it fills the Process page's
        # estimate card and the preview, so the page's minimum depends on it)
        jobs.settle()
    last, same = None, 0
    for _ in range(200):
        _pump(app, rounds=1)
        sig = _layout_signature(win)
        same = same + 1 if sig == last else 0
        last = sig
        armed = any(t.isActive() and t.interval() == 0 for t in win.findChildren(QTimer))
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
    m = win.minimumSizeHint().expandedTo(win.minimumSize())
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
    sections, the Setup sections) and panels behind a toggle (``responsive_views`` on a
    checkable button: the Nest options)."""
    from PySide6.QtWidgets import QAbstractButton, QComboBox, QListWidget
    for b in page.findChildren(QAbstractButton):
        if b.property("responsive_views") and b.isVisible() and b.isCheckable() and not b.isChecked():
            # a page switch (an exclusive group: Process › Drawing | Library) goes back to the
            # button that was checked; a toggle (the Nest options) is unchecked again
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
    for key in SAFETY:
        b = bar.findChild(QPushButton, f"run_{key}") if bar is not None else None
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


# ----------------------------------------------------------------------------- tests
def test_panels_with_a_selection(win, app):
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
        for theme in ("light", "dark"):
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
    rep.write("selection")
    text = _fail_text(rep.rows)
    assert not text and not grew, text + "\n" + "\n".join(grew)


def test_minimum_size_fits_the_smallest_sample(win, app):
    """What the content needs (docs/dev/RESPONSIVE.md): at most 1280 × 800 at the default font."""
    win.shell.host.activate("design")
    _pump(app)
    m = win.minimumSizeHint().expandedTo(win.minimumSize())
    assert m.width() <= 1280 and m.height() <= 800, m


def test_sample_sizes(win, app):
    rep = Report()
    for size in SAMPLES:
        s = _fit(win, app, size)
        _pump(app, 0.05)
        for label in views(win, app):
            rep.check("sample", s, label, win)
            rep.shot(win, s, label)
    rep.write("sample_sizes")
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
        m = win.minimumSizeHint().expandedTo(win.minimumSize())
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


def test_imperial_sample_sizes(win, app):
    """Imperial display units (longer texts: "0.3937 in", "393.70 in/min"): no scroll bar, no
    clipped widget either."""
    rep = Report()
    win.config.set("units.system", "imperial")
    try:
        _pump(app, 0.05)
        for size in SAMPLES:
            s = _fit(win, app, size)
            for label in views(win, app):
                rep.check("imperial", s, label, win)
                if size == (1512, 875):
                    rep.shot(win, s, f"imperial_{label}")
    finally:
        win.config.set("units.system", "metric")
        _pump(app, 0.05)
    rep.write("imperial")
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
    m = win.minimumSizeHint().expandedTo(win.minimumSize())
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


def test_width_and_height_sweep(win, app):
    """Every width from the window's minimum to 2560 (STEP px) at several heights, every
    workspace: no violation, the safety controls in the window."""
    rep = Report()
    widths, heights = _sweep_sizes(win)
    host = win.shell.host
    bad = []
    for ws in host.visible_ids():
        host.activate(ws)
        _pump(app)
        seen: set[tuple[int, int]] = set()
        for h in heights:
            for wd in widths:
                s = _fit(win, app, (wd, h))
                if (s.width(), s.height()) in seen:
                    continue          # the same size again (a screen that holds less)
                seen.add((s.width(), s.height()))
                rep.check("sweep", s, ws, win)
                bad += _safety_targets(win, f"{s.width()}x{s.height()} {ws}")
    rep.write("sweep")
    text = _fail_text(rep.rows)
    assert not text and not bad, text + "\n" + "\n".join(bad)


def test_dock_width_sweep(win, app):
    """The side docks dragged from their minimum to wide: their panels reflow, nothing clips."""
    rep = Report()
    host = win.shell.host
    for size in ((1280, 800), (1512, 875), (1920, 1080)):
        s = _fit(win, app, size)
        for ws in ("design", "process"):
            if ws not in host.visible_ids():
                continue
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
    rep.write("dock_sweep")
    text = _fail_text(rep.rows)
    assert not text, text


def test_live_resize_recovers(win, app):
    """Shrink from 2560 to the minimum and grow back (STEP px): no violation on the way and the
    same layout at the end as at the start (no state stuck from the narrow end)."""
    rep = Report()
    host = win.shell.host
    widths, _ = _sweep_sizes(win)
    for ws in ("design", "run", "machine"):
        if ws not in host.visible_ids():
            continue
        host.activate(ws)
        s0 = _fit(win, app, (1512, 875))
        _pump(app, 0.05)
        before = _geometry(win)
        for wd in [*reversed(widths), *widths, 1512]:
            s = _fit(win, app, (wd, 875))
            rep.check("live", s, ws, win)
        _fit(win, app, (s0.width(), s0.height()))
        _pump(app, 0.05)
        after = _geometry(win)
        diff = {k: (before.get(k), after.get(k)) for k in set(before) | set(after)
                if not _same(before.get(k), after.get(k))}
        assert not diff, (ws, diff)
    rep.write("live_resize")
    text = _fail_text(rep.rows)
    assert not text, text


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
