"""Accessibility tree of the real window (UI evaluation F7).

Every visible interactive element of every workspace has an accessible name, the canvas tells
assistive technology what is in the drawing, and alarms reach a screen reader.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QApplication

from wiring import dispose, real_window

pytestmark = pytest.mark.xdist_group("a11y_tree")

INTERACTIVE = {QAccessible.Role.PushButton, QAccessible.Role.CheckBox, QAccessible.Role.RadioButton,
               QAccessible.Role.ComboBox, QAccessible.Role.SpinBox, QAccessible.Role.EditableText,
               QAccessible.Role.Slider, QAccessible.Role.PageTab, QAccessible.Role.ButtonMenu,
               QAccessible.Role.ButtonDropDown, QAccessible.Role.ToolBar, QAccessible.Role.List,
               QAccessible.Role.Table, QAccessible.Role.Tree}


def _pump(n: int = 5) -> None:
    for _ in range(n):
        QCoreApplication.processEvents()


def unnamed(widget) -> tuple[int, list[str]]:
    """(visible interactive elements, descriptions of those without an accessible name)."""
    root = QAccessible.queryAccessibleInterface(widget)
    assert root is not None
    total = 0
    out: list[str] = []
    seen = 0

    def walk(iface, depth=0):
        nonlocal total, seen
        if iface is None or not iface.isValid() or depth > 40 or seen > 20000:
            return
        seen += 1
        if iface.state().invisible:
            return
        r = iface.role()
        if r in INTERACTIVE:
            total += 1
            if not (iface.text(QAccessible.Text.Name) or "").strip():
                obj = iface.object()
                par = obj.parent() if obj is not None else None
                out.append(f"{r.name}:{type(obj).__name__}:{obj.objectName() if obj else ''}"
                           f" in {type(par).__name__ if par is not None else '-'}"
                           f"/{type(par.parent()).__name__ if par is not None and par.parent() else '-'}")
        for i in range(iface.childCount()):
            walk(iface.child(i), depth + 1)
    walk(root)
    return total, out


def _pump_for(sec: float) -> None:
    import time
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
    w.resize(1512, 945)
    w.show()
    w.set_document(docops.demo_document())
    _pump_for(0.5)
    yield w
    dispose(w)


@pytest.mark.parametrize("size", [(1512, 945), (1100, 720)])
def test_every_workspace_names_its_interactive_elements(window, size):
    """Wide and narrow: a narrow window turns some tabs and buttons into icons only."""
    w = window
    w.resize(*size)
    _pump_for(0.3)
    ids = [i for i in w.shell.host.visible_ids()]
    assert {"design", "run", "machine", "process"} <= set(ids)
    problems = {}
    for ws in ids:
        w.show_workspace(ws)
        _pump_for(0.3)
        total, missing = unnamed(w)
        for _ in range(25):
            # a workspace still settling: its tool bar steps down (labels off, a second row)
            # when Qt's extension button shows, a few event-loop turns after the page (Windows
            # CI, Process at 1100 px: the button was read in between).  One that stays fails
            if not missing:
                break
            _pump_for(0.2)
            total, missing = unnamed(w)
        assert total > 0
        if missing:
            problems[ws] = missing
    assert not problems, "unnamed interactive elements:\n" + "\n".join(
        f"  [{ws}] {m}" for ws, ms in problems.items() for m in ms)
    QApplication.processEvents()
