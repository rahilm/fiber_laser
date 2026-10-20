"""Imperial sweep: open every workspace (and each tab in it) of the real main window in imperial
and fail on any visible metric unit text (labels, suffixes, headers, cells, tooltips); then the
live switch: change the system with everything open and check that the texts follow both ways.

``MLASER_UNITS_SHOTS=<dir>`` saves a 1280×800 screenshot of each workspace in imperial."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QTabWidget  # noqa: E402

from units_sweep import offenders, texts  # noqa: E402

SHOTS = os.environ.get("MLASER_UNITS_SHOTS", "")
WORKSPACES = ("design", "process", "nest", "run", "report", "machine", "orders")


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch):
    from wiring import real_window
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window(user_mode="service")
    w.resize(1280, 800)
    w.show()
    _pump(app)
    yield w
    w._test_no_dialogs = True
    w.config.set("units.system", "metric")
    w.close()


def _pump(app, n=6):
    for _ in range(n):
        app.processEvents()


def _load_sample(win):
    """A small drawing so the canvas, the part list and the layer table have content."""
    from mlaser.model import Document
    from mlaser import ops
    doc = getattr(win, "document", None)
    if doc is None:
        return
    try:
        ops.add_rectangle(doc, 0, 0, 101.6, 50.8)   # 4 x 2 in
    except Exception:  # noqa: BLE001 - optional helper
        pass
    _ = Document


def _each_tab(win, app):
    """Show each tab of every QTabWidget in the window once (yield after each)."""
    for tw in win.findChildren(QTabWidget):
        if not tw.isVisible():
            continue
        cur = tw.currentIndex()
        for i in range(tw.count()):
            tw.setCurrentIndex(i)
            _pump(app, 2)
            yield f"{tw.objectName() or type(tw.parent()).__name__}#{i}"
        tw.setCurrentIndex(cur)


def _workspace_ids(win):
    return [w for w in win.shell.host.visible_ids()]


def test_every_workspace_shows_no_metric_text_in_imperial(win, app):
    win.config.set("units.system", "imperial")
    _pump(app)
    bad: dict[str, list[str]] = {}
    for ws in _workspace_ids(win):
        win.show_workspace(ws)
        _pump(app)
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            win.grab().save(os.path.join(SHOTS, f"{ws}.png"))
        hits = set(offenders(win))
        for tab in _each_tab(win, app):
            hits |= {f"[{tab}] {h}" for h in offenders(win) if h not in hits}
        hits = {h for h in hits if not h.startswith("[") or h.split("] ", 1)[1] not in hits}
        if hits:
            bad[ws] = sorted(hits)
    # Orders shares the Nest slot (a page of it): shown on its own too
    if win.show_workspace("orders.orders"):
        _pump(app)
        if SHOTS:
            win.grab().save(os.path.join(SHOTS, "orders.png"))
        hits = set(offenders(win))
        if hits:
            bad["orders"] = sorted(hits)
    assert not bad, "metric text in imperial:\n" + "\n".join(
        f"== {ws}\n  " + "\n  ".join(h) for ws, h in bad.items())


def test_live_switch_updates_open_windows(win, app):
    """Change the setting while everything is open: the texts follow, both ways."""
    win.show_workspace("design")
    _pump(app)
    metric = {k: v for k, v in texts(win) if " mm" in v or v.endswith("mm")}
    assert metric, "no metric text to watch in the Design workspace"
    win.config.set("units.system", "imperial")
    _pump(app)
    assert not offenders(win)
    win.config.set("units.system", "metric")
    _pump(app)
    back = {k: v for k, v in texts(win) if " mm" in v or v.endswith("mm")}
    assert set(metric) <= set(back)
