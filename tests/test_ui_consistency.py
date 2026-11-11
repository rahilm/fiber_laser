"""One way per thing (docs/dev/STYLE.md; UI review 2026-11 findings 2, 4, 11): the component
helpers of :mod:`mlaser.ui.style` and the rules that keep new code on them.

* **buttons**: two control heights, ``mouse`` (32 px) and ``touch`` (44 px), set by
  ``style.button(size=...)`` / ``style.control_size``; a push button is made by a helper, never
  by ``QPushButton(`` (files not migrated yet are in :data:`BUTTON_ALLOWLIST` with their counts,
  which may only go down); dialog button boxes get one size with ``style.size_buttons``;
* **tabs**: two looks, ``style.tabs(widget, "workspace" | "dock")``;
* **sections**: ``style.section`` (no ``QGroupBox`` frame);
* **tables**: every table and tree goes through ``style.table`` / ``style.table_look``.

The source rules are checked line by line: the helper wraps the constructor on the same line.
"""
from __future__ import annotations

import os
import re

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (QApplication, QDialogButtonBox, QPushButton, QTableWidget,  # noqa: E402
                               QTabWidget, QTreeWidget, QTreeWidgetItem, QWidget)

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SCAN = "mlaser/ui"
EXEMPT = {"mlaser/ui/style.py"}

#: files still to migrate: {path: {rule: count}}.  Only ever lower these numbers; a migrated
#: file leaves the list and no file may join it.  (Owned by the safety, run-screen and canvas
#: work when this rule was set; the plugin manager's own UI is outside mlaser/ui.)
ALLOWLIST: dict[str, dict[str, int]] = {
    "mlaser/ui/safety_ui.py": {"button": 6, "button_box": 1},
    "mlaser/ui/workspaces/run.py": {"button": 5, "table": 1, "tabs": 1},
    "mlaser/ui/jogpad.py": {"button": 2},
    "mlaser/ui/canvas_empty.py": {"button": 1},
    "mlaser/ui/responsive.py": {"tabs": 1},
}

#: rule: (constructor pattern, the helper that must wrap it on the same line)
RULES = {
    "button": (re.compile(r"\bQPushButton\("), re.compile(r"$^")),
    "button_box": (re.compile(r"\bQDialogButtonBox\("), re.compile(r"\bsize_buttons\(")),
    "tabs": (re.compile(r"\bQTab(?:Widget|Bar)\("), re.compile(r"\btabs\(")),
    "table": (re.compile(r"\bQ(?:Table|Tree)(?:Widget|View)\("), re.compile(r"\btable_look\(")),
    "group_box": (re.compile(r"\bQGroupBox\("), re.compile(r"$^")),
}


def _files() -> list[str]:
    out = []
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, SCAN)):
        for f in files:
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(dirpath, f), ROOT).replace(os.sep, "/")
                if rel not in EXEMPT:
                    out.append(rel)
    return sorted(out)


def _code_lines(path: str) -> list[str]:
    out = []
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        for ln in fh:
            code = ln.split("#", 1)[0]
            if "isinstance(" in code or code.lstrip().startswith(("class ", "from ", "import ")):
                continue
            out.append(code)
    return out


def _unrouted(path: str, rule: str) -> list[str]:
    ctor, helper = RULES[rule]
    return [ln.strip() for ln in _code_lines(path) if ctor.search(ln) and not helper.search(ln)]


@pytest.mark.parametrize("rule", sorted(RULES))
def test_widgets_come_from_the_helpers(rule):
    bad, lower = [], []
    for path in _files():
        found = _unrouted(path, rule)
        allowed = ALLOWLIST.get(path, {}).get(rule, 0)
        if len(found) > allowed:
            bad.append(f"{path}: {len(found)} > {allowed}: {found}")
        elif len(found) < allowed:
            lower.append(f"{path}: {rule} {allowed} -> {len(found)} (lower ALLOWLIST)")
    assert not bad, "use the style helper (docs/dev/STYLE.md):\n" + "\n".join(bad)
    assert not lower, "\n".join(lower)


def test_allowlist_names_files_that_exist():
    for path in ALLOWLIST:
        assert os.path.exists(os.path.join(ROOT, path)), path


def test_rules_catch_what_they_should():
    ctor, helper = RULES["table"]
    assert ctor.search("t = QTableWidget(0, 3)") and not helper.search("t = QTableWidget(0, 3)")
    assert helper.search("t = style.table_look(QTreeWidget())")
    assert RULES["button"][0].search("b = QPushButton(text)")
    assert not RULES["button"][0].search("class Hold(QPushButton):")
    assert RULES["tabs"][1].search("x = S.tabs(QTabWidget())")


# --------------------------------------------------------------------------- the helpers
@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_two_control_heights(app):
    from mlaser.ui import style as S
    assert S.SIZES == {"mouse": S.TARGET, "touch": S.TARGET_TOUCH, "run": S.TARGET_RUN}
    b = S.button("Mark")
    assert b.minimumHeight() == S.TARGET and b.property("control_size") == "mouse"
    assert b.sizeHint().height() <= b.minimumSizeHint().height() or b.minimumHeight() == S.TARGET
    t = S.button("Gas", size="touch", checkable=True, name="gas")
    assert t.minimumHeight() == S.TARGET_TOUCH and t.isCheckable() and t.objectName() == "gas"
    with pytest.raises(KeyError):
        S.button("x", size="huge")
    run = S.run_button("start")
    assert run.property("control_size") == "run"


def test_dialog_buttons_get_one_size_also_when_added_later(app):
    from mlaser.ui import style as S
    bb = S.size_buttons(QDialogButtonBox(QDialogButtonBox.Ok), "touch")
    later = bb.addButton("Preview", QDialogButtonBox.ActionRole)
    bb.addButton(QDialogButtonBox.Close)
    app.processEvents()
    assert {b.minimumHeight() for b in bb.buttons()} == {S.TARGET_TOUCH}
    assert later.property("control_size") == "touch"
    d = S.Dialog(None, "t")
    assert {b.minimumHeight() for b in d.buttons.buttons()} == {S.TARGET}
    d.deleteLater()


def test_two_tab_looks(app):
    from mlaser.ui import style as S
    dock = S.tabs(QTabWidget())
    assert dock.documentMode() and dock.property("tab_kind") == "dock" and not dock.styleSheet()
    ws = S.tabs(QTabWidget(), "workspace")
    assert ws.property("tab_kind") == "workspace" and "QTabBar::tab" in ws.styleSheet()
    ws.addTab(QWidget(), "Live view")
    ws.show()
    app.processEvents()
    assert ws.tabBar().sizeHint().height() >= S.TARGET_TOUCH
    ws.hide()
    with pytest.raises(ValueError):
        S.tabs(QTabWidget(), "pill")


def test_table_recipe(app):
    from mlaser.ui import style as S
    t = S.table_look(QTableWidget(0, 3), numeric=(1,), empty="No orders yet.")
    t.setHorizontalHeaderLabels(["File", "Qty", "Material"])
    t.resize(400, 200)
    t.show()
    app.processEvents()
    assert t.property("table_look")
    assert t.horizontalHeader().defaultAlignment() & Qt.AlignLeft
    assert t.horizontalHeaderItem(1).textAlignment() & Qt.AlignRight       # kept by the labels
    assert t.verticalHeader().defaultSectionSize() == S.row_height(t)
    assert not t.alternatingRowColors()
    empty = t.viewport().findChild(QWidget, "table_empty")
    assert empty is not None and empty.isVisible() and empty.text() == "No orders yet."
    t.insertRow(0)
    from PySide6.QtWidgets import QStyleOptionViewItem
    opt = QStyleOptionViewItem()
    t.itemDelegate().initStyleOption(opt, t.model().index(0, 1))
    assert opt.displayAlignment & Qt.AlignRight
    assert not empty.isVisible()
    t.setRowCount(0)
    assert empty.isVisible()
    S.set_empty_text(t, "Keine Aufträge.")
    assert empty.text() == "Keine Aufträge."
    wide = S.table_look(QTableWidget(0, 9))
    assert wide.alternatingRowColors()
    tree = S.table_look(QTreeWidget())
    tree.setColumnCount(2)
    it = QTreeWidgetItem(["a", "b"])
    tree.addTopLevelItem(it)
    assert tree.itemDelegate().sizeHint(QStyleOptionViewItem(), tree.model().index(0, 0)).height() >= S.row_height(tree)
    t.hide()
    k = S.table([S.Col("Name", stretch=True), S.Col("Qty", numeric=True)], empty="Nothing")
    assert k.property("table_look") and k.horizontalHeaderItem(1).textAlignment() & Qt.AlignRight


def test_safety_io_has_sections_not_group_boxes(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QGroupBox
    for k, sub in (("MLASER_CONFIG_DIR", "cfg"), ("MLASER_APP_DIR", "app"), ("MLASER_DATA_DIR", "data")):
        monkeypatch.setenv(k, str(tmp_path / sub))
    from wiring import dispose, real_window
    w = real_window()
    try:
        from mlaser.ui.safety_io import SafetyIODialog
        d = SafetyIODialog(w)
        assert not d.findChildren(QGroupBox)
        heads = [h.text() for h in d.findChildren(QWidget, "section_header")]
        assert len(heads) >= 2
        assert d.findChild(QWidget, "safety_io_heartbeat").property("section_title")
        assert {b.minimumHeight() for b in d.findChildren(QPushButton)} == {32}
        d.deleteLater()
    finally:
        dispose(w)


# --------------------------------------------------------------------------- in the window
def test_one_size_per_panel_in_the_window(app, tmp_path, monkeypatch):
    """The Control dock's buttons move the machine or switch an output: all touch size; the
    Design and Process panels at the PC: mouse size; the shop-floor workspaces: touch."""
    from PySide6.QtWidgets import QDialog
    for k, sub in (("MLASER_CONFIG_DIR", "cfg"), ("MLASER_APP_DIR", "app"), ("MLASER_DATA_DIR", "data")):
        monkeypatch.setenv(k, str(tmp_path / sub))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    from wiring import dispose, real_window
    from mlaser.ui import style as S
    from mlaser.ui.panels import ControlPanel, LayerPanel
    w = real_window()
    try:
        cp = w.findChild(ControlPanel)
        # (the jog pad's own buttons are touch squares of jogpad.py)
        assert {b.minimumHeight() for b in cp.findChildren(QPushButton)} == {S.TARGET_TOUCH}
        lp = w.findChild(LayerPanel)
        assert {b.property("control_size") for b in lp.findChildren(QPushButton)} == {"mouse"}
        for name, size in (("process_workspace", "mouse"), ("orders_workspace", "touch")):
            page = w.findChild(QWidget, name)
            if page is None:
                w.show_workspace(name.split("_")[0])
                app.processEvents()
                page = w.findChild(QWidget, name)
            assert page is not None, name
            # the canvas's empty-state card is the canvas's own (canvas_empty.py, not migrated yet)
            got = {b.property("control_size") for b in page.findChildren(QPushButton)
                   if not b.objectName().startswith("canvas_empty_")}
            assert got == {size}, (name, got)
        # every table and tree of the window has the table recipe (the Run queue: run.py's own)
        tables = w.findChildren(QTableWidget) + w.findChildren(QTreeWidget)
        missing = [v.objectName() or type(v.parentWidget()).__name__ for v in tables if not v.property("table_look")]
        assert set(missing) <= {"queue_table"}, missing
    finally:
        dispose(w)
