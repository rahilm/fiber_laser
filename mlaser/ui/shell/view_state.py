"""Splitter positions and current tabs, saved and restored with the window state (UI evaluation
F13: ``QMainWindow.saveState`` covers the docks and tool bars only).

Every ``QSplitter`` and ``QTabWidget`` of the main window gets a key that survives a restart:
its own ``objectName`` path when it has a name, else the path of its nearest named ancestor
plus its class and its index among that ancestor's unnamed widgets of the same class
(``process_workspace/QSplitter[0]``).  The state is one JSON object in ``shell.view_state``:
``{key: {"split": base64}}`` or ``{key: {"tab": index}}``.

* Restored on the window's first show (``Shell._first_show``) and, for a workspace built later
  (:class:`mlaser.ui.workspaces.LazyPage`), when it is built: the entries of widgets that do
  not exist yet wait in the stored state and are kept on save.
* A workspace page's canvas | panel splitter follows the content (``WorkspacePage.fit_split``)
  until the user drags it: only a dragged one is saved, and a restored one stays the user's.
* A saved state that no longer fits (another number of panes or tabs) is ignored.
"""
from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QByteArray
from PySide6.QtWidgets import QSplitter, QTabWidget, QWidget

KEY = "shell.view_state"


def _scope(w: QWidget, root: QWidget) -> Optional[QWidget]:
    a = w.parentWidget()
    while a is not None and a is not root and not a.objectName():
        a = a.parentWidget()
    return a


def _path(w: Optional[QWidget], root: QWidget) -> str:
    names = []
    while w is not None and w is not root:
        if w.objectName():
            names.append(w.objectName())
        w = w.parentWidget()
    return "/".join(reversed(names))


def key_of(w: QWidget, root: QWidget) -> Optional[str]:
    """The widget's stable key (None when it is not inside ``root``)."""
    scope = _scope(w, root)
    if scope is None:
        return None
    if w.objectName():
        return _path(w, root)
    cls = QSplitter if isinstance(w, QSplitter) else QTabWidget
    peers = [c for c in scope.findChildren(cls) if not c.objectName() and _scope(c, root) is scope]
    try:
        i = peers.index(w)
    except ValueError:
        return None
    return f"{_path(scope, root)}/{cls.__name__}[{i}]"


def _page_of(sp: QSplitter) -> Any:
    """The workspace page whose canvas | panel splitter ``sp`` is (None for any other)."""
    a = sp.parentWidget()
    while a is not None:
        if getattr(a, "splitter", None) is sp and hasattr(a, "_user_split"):
            return a
        a = a.parentWidget()
    return None


def _widgets(root: QWidget, under: Optional[QWidget] = None) -> list[QWidget]:
    base = under if under is not None else root
    out: list[QWidget] = []
    for cls in (QSplitter, QTabWidget):
        out += [w for w in base.findChildren(cls) if not w.property("no_view_state")]
    if under is not None and isinstance(under, (QSplitter, QTabWidget)):
        out.append(under)
    return out


def collect(root: QWidget, previous: Optional[dict] = None) -> dict:
    """The state of ``root``'s splitters and tabs, on top of ``previous`` (entries of widgets
    not built in this session are kept)."""
    state = dict(previous or {})
    for w in _widgets(root):
        k = key_of(w, root)
        if not k:
            continue
        if isinstance(w, QSplitter):
            page = _page_of(w)
            if page is not None and not page._user_split:
                state.pop(k, None)             # the content decides: nothing of the user's to keep
                continue
            if w.count() < 2:
                continue
            state[k] = {"split": bytes(w.saveState().toBase64()).decode("ascii")}
        elif w.count() > 1:
            state[k] = {"tab": int(w.currentIndex())}
    return state


def restore(root: QWidget, state: Optional[dict], under: Optional[QWidget] = None) -> int:
    """Apply ``state`` to ``root``'s splitters and tabs (only those under ``under`` when given);
    returns how many were restored."""
    if not isinstance(state, dict) or not state:
        return 0
    n = 0
    for w in _widgets(root, under):
        k = key_of(w, root)
        v = state.get(k) if k else None
        if not isinstance(v, dict):
            continue
        try:
            if isinstance(w, QSplitter) and isinstance(v.get("split"), str):
                if w.restoreState(QByteArray.fromBase64(v["split"].encode("ascii"))):
                    page = _page_of(w)
                    if page is not None:
                        page._user_split = True     # the user's split: fit_split leaves it
                    n += 1
            elif isinstance(w, QTabWidget) and isinstance(v.get("tab"), int):
                i = v["tab"]
                if 0 <= i < w.count() and w.isTabEnabled(i):
                    w.setCurrentIndex(i)
                    n += 1
        except Exception:  # noqa: BLE001 - a broken entry is skipped, never fatal
            continue
    return n


__all__ = ["KEY", "collect", "key_of", "restore"]
