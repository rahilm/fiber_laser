"""The Workspace menu names every workspace the same way: the plain name ("Design", "Nest"),
not "Workspace: Design" next to "Nest" (the menu's title already says what they are; the
platform guidelines name the items of a View / Window menu without repeating the menu), on every
platform's menu bar, and in the catalogue's language."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def window():
    QApplication.instance() or QApplication([])
    from wiring import dispose, real_window
    w = real_window(user_mode="service")
    yield w
    dispose(w)


@pytest.mark.parametrize("plat", ["linux", "windows", "macos"])
def test_workspace_menu_entries_are_plain_names(window, plat):
    from mlaser.ui.shell.menubar import MenuBar
    tr = window.tr_
    title = tr.tr("shell.menu.workspace").replace("&", "")
    mb = MenuBar(window.shell, plat=plat)
    try:
        menu = next(a.menu() for a in mb.bar.actions()
                    if a.menu() is not None and a.text().replace("&", "") == title)
        names = [a.text().replace("&", "") for a in menu.actions() if not a.isSeparator() and a.text()]
    finally:
        mb.bar.deleteLater()
    expected = [tr.tr(f"shell.ws.{s}") for s in ("design", "process", "nest", "run", "report", "machine")]
    assert names[:6] == expected
    assert not [n for n in names if n.lower().startswith(title.lower())], names


def test_workspace_commands_read_the_workspace_name(window):
    acts = {c: a.text() for c, a in window.ribbon.actions.items() if c.startswith("workspace.")}
    assert acts["workspace.design"] == window.tr_.tr("shell.ws.design")
    assert acts["workspace.process"] == window.tr_.tr("shell.ws.process")
    assert acts["workspace.machine"] == window.tr_.tr("shell.ws.machine")
    assert not [t for t in acts.values() if ":" in t], acts
