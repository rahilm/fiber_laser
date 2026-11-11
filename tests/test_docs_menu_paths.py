"""The user documentation names the window as it is (review 2026-11-09, ux.md finding 15: the
guides still sent people to ribbon tabs and paths of an earlier UI, "Advanced → Hardware →
Connect to simulator", "System Analysis → Device report", "Process+ → Process").

* every bold path ``**A › B › C**`` in ``docs/user`` and ``README.md`` whose first element is a
  menu of the menu bar resolves in the real window's menu bar (Linux layout, the one the guides
  use), or, for the Machine and Process workspaces, names one of their pages; ``Settings › X``
  names a page of the Settings window;
* the notations of the old guides (``→`` / ``>`` paths in bold, "Wave 3", "until the workspace
  switcher exists", ``Open it with `workspace.…```) do not come back.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = sorted((ROOT / "docs" / "user").glob("*.md")) + [ROOT / "README.md"]
BOLD = re.compile(r"\*\*([^*\n]+?)\*\*")
MENUS = ("File", "Edit", "View", "Draw", "Process", "Workspace", "Machine", "Tools", "Window", "Help")


def _norm(text: str) -> str:
    t = text.replace("&", "").strip()
    for end in ("…", "..."):
        if t.endswith(end):
            t = t[: -len(end)]
    return t.strip().casefold()


def _paths():
    for doc in DOCS:
        if doc.name == "keyboard.md":           # generated from the command registry
            continue
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for m in BOLD.finditer(line):
                parts = [p.strip() for p in m.group(1).split("›")]
                if len(parts) > 1 and all(parts):
                    yield f"{doc.relative_to(ROOT)}:{n}", parts


def test_old_notations_are_gone():
    bad = []
    old = re.compile(r"Wave 3|workspace switcher|Open it with `workspace\.|Start → Help|Advanced → |"
                     r"System Analysis →|Process\+ →")
    for doc in DOCS:
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if old.search(line):
                bad.append(f"{doc.relative_to(ROOT)}:{n}: {line.strip()}")
            for m in BOLD.finditer(line):
                if "→" in m.group(1) or " > " in m.group(1):
                    bad.append(f"{doc.relative_to(ROOT)}:{n}: **{m.group(1)}** (write menu paths with ›)")
    assert bad == []


@pytest.fixture(scope="module")
def menu_tree():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from wiring import dispose, real_window
    w = real_window(user_mode="service")
    tree: dict = {}

    def walk(menu, node):
        for a in menu.actions():
            if a.isSeparator() or not a.text():
                continue
            text = _norm(a.text())
            child = node.setdefault(text, {})
            if a.menu() is not None:
                walk(a.menu(), child)

    # the Linux layout, the one the guides use, on every host: on macOS the Plugin manager,
    # Settings and About sit in the application menu by design (menu_spec, their menu roles),
    # and the guides say so next to the path ("macOS: the mlaser menu › Plugins…")
    from mlaser.ui import native
    from mlaser.ui.shell.menubar import MenuBar
    mb = MenuBar(w.shell, plat=native.LINUX)
    try:
        for a in mb.bar.actions():
            if a.menu() is not None:
                walk(a.menu(), tree.setdefault(_norm(a.text()), {}))
        from PySide6.QtWidgets import QListWidget
        nav = w.findChild(QListWidget, "machine_nav")
        machine_pages = {_norm(nav.item(i).text()) for i in range(nav.count())}
    finally:
        mb.bar.deleteLater()
        dispose(w)
    return tree, machine_pages


def _catalogue() -> dict:
    return json.loads((ROOT / "mlaser" / "i18n" / "en.json").read_text(encoding="utf-8"))["strings"]


def test_menu_paths_in_the_guides_resolve(menu_tree):
    tree, machine_pages = menu_tree
    cat = _catalogue()
    from mlaser.services import settings_catalog as C
    settings_pages = {_norm(cat[key]) for _pid, key in C.PAGES if key in cat}
    process_pages = {_norm(cat[k]) for k in ("process.page.drawing", "process.page.library", "process.layers",
                                             "process.tab.layer", "process.tab.estimate")}
    process_pages |= {"properties", "cutting strategy"}
    bad, checked = [], 0
    for where, parts in _paths():
        head = parts[0]
        if head == "Settings":
            checked += 1
            if _norm(parts[1]) not in settings_pages:
                bad.append(f"{where}: {' › '.join(parts)} (no such Settings page)")
            continue
        if head not in MENUS:
            continue                         # a dialog's own buttons, a template ...
        checked += 1
        node = tree.get(_norm(head), {})
        ok = True
        for p in parts[1:]:
            if _norm(p) not in node:
                ok = False
                break
            node = node[_norm(p)]
        if not ok and head == "Machine" and _norm(parts[1]) in machine_pages:
            ok = True                        # a page of the Machine workspace
        if not ok and head == "Process" and _norm(parts[1]) in process_pages:
            ok = True                        # a page / tab of the Process workspace
        if not ok:
            bad.append(f"{where}: {' › '.join(parts)}")
    assert checked > 50, checked             # the guides do name their menu paths this way
    assert bad == []
