"""Responsive layout harness (docs/dev/RESPONSIVE.md): the real window (tests/wiring.py, every
built-in plugin and the Onshape plugin when it is installed) at sample sizes, over a sweep of
widths and heights, with the docks dragged from narrow to wide, with a larger font, at 2× scale
and while it is resized live (shrink, then grow): no visible horizontal scroll bar anywhere, no
vertical scroll bar on a form or control panel, no clipped widget (tests/layout_audit.py).

The sizes are *sample points*, not design targets: the layouts adapt to the space they get.

``MLASER_LAYOUT_SHOTS=<dir>`` writes a report (``report.json``, ``report.md``) and a screenshot of
every view at every sample size into ``<dir>``; ``MLASER_LAYOUT_TAG`` names the run (subfolder).
``MLASER_LAYOUT_STEP`` sets the sweep step (default 40 px).  ``MLASER_LAYOUT_STYLE`` runs under
that Qt style (``macos``, ``windows11``, ``windowsvista``, ``fusion``, ...: the platform's native
styles on the CI runners); by default the application's own style.
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

SAMPLES = [(1280, 800), (1440, 900), (1512, 875), (1800, 1169), (1920, 1080)]
SWEEP_HEIGHTS = (800, 875, 1169)
STEP = int(os.environ.get("MLASER_LAYOUT_STEP", "40") or 40)
MAX_W = 2560
SAFETY = ("stop", "hold", "arm")
TOUCH_MIN = 40

#: violations in panels another track owns (ui_wave_brief.md "File ownership"): reported, not
#: failed here; the owner's harness entry goes once it is fixed (test_pending_still_needed)
PENDING: dict[str, str] = {
    # Process & technology track (ui_wave_brief.md): Properties dock, Process workspace panels
    "PropertiesPanel#properties_panel": "Process & technology",
    "ProcessWorkspace#process_workspace >": "Process & technology",
}


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
    # the offscreen screen is 800 × 800: pages must not be capped to it (FitArea's screen cap is
    # tested on its own, test_fit_area_screen_cap)
    from mlaser.ui.responsive import FitArea
    mp.setattr(FitArea, "SCREEN_SHARE", 100.0)
    w = real_window(config=ConfigService())
    onshape = _load_onshape(w)
    w.shell.host.refresh_mode()
    style = os.environ.get("MLASER_LAYOUT_STYLE", "")
    if style:
        from PySide6.QtWidgets import QStyleFactory
        if style.lower() not in [k.lower() for k in QStyleFactory.keys()]:
            pytest.skip(f"style {style} not available here ({QStyleFactory.keys()})")
        app.setStyle(style)
    w.resize(*SAMPLES[2])
    w.show()
    _pump(app, 0.3)
    w.onshape_loaded = onshape and "onshape" in w.shell.host.specs
    yield w
    w.close()
    w.deleteLater()
    _pump(app)
    mp.undo()


def _fit(win, app, size) -> QSize:
    """Resize to ``size`` (never below the window's own minimum, which is what the content needs)."""
    s = QSize(*size).expandedTo(win.minimumSizeHint()).expandedTo(win.minimumSize())
    win.resize(s)
    _pump(app)
    return win.size()


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
    """Pages behind a navigation list (``responsive_views``, e.g. the Machine sections)."""
    from PySide6.QtWidgets import QListWidget
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

    def add(self, kind: str, size, label: str, violations) -> None:
        self.rows.append({"run": kind, "size": f"{size.width()}x{size.height()}", "view": label,
                          "style": self.style,
                          "violations": [{"kind": v.kind, "path": v.path, "detail": v.detail,
                                          "owner": _owned_elsewhere(v)} for v in violations]})

    def shot(self, win, size, label: str) -> None:
        if self.dir is None:
            return
        p = self.dir / "shots" / f"{size.width()}x{size.height()}" / f"{label}.png"
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
        lines.append(f"distinct violations: {len(uniq)}")
        lines.append("")
        for (kind, path, owner), where in sorted(uniq.items()):
            lines.append(f"* **{kind}** `{path}`" + (f" (owner: {owner})" if owner else ""))
            lines.append(f"  - {len(where)}×, e.g. {', '.join(where[:4])}")
        (self.dir / f"{name}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fail_text(rows) -> str:
    out = []
    for r in rows:
        for v in r["violations"]:
            if not v["owner"]:
                out.append(f'{r["size"]} {r["view"]}: {v["kind"]} {v["path"]} {v["detail"]}')
    return "\n".join(dict.fromkeys(out))


# ----------------------------------------------------------------------------- tests
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
            v = A.audit(win)
            rep.add("sample", s, label, v)
            rep.shot(win, s, label)
    rep.write("sample_sizes")
    text = _fail_text(rep.rows)
    assert not text, text


def test_safety_controls_visible_everywhere(win, app):
    """STOP, Hold and Arm in every workspace at every sample size, light and dark, and the Run
    screen's targets at least TOUCH_MIN px."""
    bad = []
    for theme in ("light", "dark"):
        win.shell.set_theme(theme, persist=False)
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
    assert not bad, "\n".join(dict.fromkeys(bad))


def _sweep_sizes(win):
    m = win.minimumSizeHint().expandedTo(win.minimumSize())
    w0 = max(m.width(), 1)
    heights = sorted({max(m.height(), h) for h in (m.height(), *SWEEP_HEIGHTS)})
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
        last = None
        for h in heights:
            for wd in widths:
                s = _fit(win, app, (wd, h))
                if s == last:
                    continue
                last = QSize(s)
                rep.add("sweep", s, ws, A.audit(win))
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
                    rep.add("docks", s, f"{ws}:{d.objectName()}={d.width()}", A.audit(win))
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
            rep.add("live", s, ws, A.audit(win))
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
                rep.add("font125", s, label, A.audit(win))
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
w.show()
out = []
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
print(json.dumps({"dpr": w.devicePixelRatioF(), "violations": out}))
w.close()
"""


def test_scale_2x(tmp_path):
    """2× device pixel ratio (Retina, 200 % on Windows): the same layout in logical pixels."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR="2",
               MLASER_CONFIG_DIR=str(tmp_path / "cfg"), MLASER_APP_DIR=str(tmp_path / "app"),
               MLASER_DATA_DIR=str(tmp_path / "data"))
    env.pop("_MLASER_CONFIG_AUTO", None)
    d = os.environ.get("MLASER_LAYOUT_SHOTS", "")
    if d:
        env["MLASER_LAYOUT_SHOTS_2X"] = str(Path(d) / (os.environ.get("MLASER_LAYOUT_TAG", "") or "run") / "shots_2x")
    r = subprocess.run([sys.executable, "-c", SCALE_SCRIPT, str(Path(__file__).parent)], env=env,
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-3000:]
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["dpr"] == 2.0
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
