"""Dock sizing policy of the main window (docs/dev/RESPONSIVE.md, rule R4).

* The side docks run the full height of the window (the bottom dock, the log, sits under the
  page only), so a control panel gets every vertical pixel the window has.
* A dock's width is the user's (a separator drag is remembered), else what its content asks for
  (its ``sizeHint``), never less than its content's minimum.  The page in the middle keeps its
  minimum (the canvas: :data:`CANVAS_MIN`, a content need: the drawing must stay workable).
* When the window shrinks the docks give way in proportion, down to their minimums; when it
  grows again they return to their remembered or natural widths (the layout recovers).
* No dock is moved between areas automatically (that left stale tab bars over the panels); the
  window's minimum width is what the visible docks and the page need.

Applied on every window resize, workspace switch and dock visibility change (coalesced).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QDockWidget, QMainWindow

#: logical px: the canvas width a workspace with the shared canvas keeps (a content need)
CANVAS_MIN = 480
SIDE = (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea)


class DockPolicy(QObject):
    def __init__(self, window: QMainWindow):
        super().__init__(window)
        self.w = window
        self.prefs: dict[str, int] = {}          # dock objectName -> width the user dragged to
        self.vprefs: dict[str, int] = {}         # bottom docks: height the user dragged to
        self.enabled = False                     # after the first show
        self._applying = False
        self._watched: set[int] = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.apply)
        window.setCorner(Qt.BottomLeftCorner, Qt.LeftDockWidgetArea)
        window.setCorner(Qt.BottomRightCorner, Qt.RightDockWidgetArea)
        window.setCorner(Qt.TopLeftCorner, Qt.LeftDockWidgetArea)
        window.setCorner(Qt.TopRightCorner, Qt.RightDockWidgetArea)
        window.installEventFilter(self)

    # ------------------------------------------------------------------ docks
    def docks(self) -> list[QDockWidget]:
        return [d for d in self.w.findChildren(QDockWidget) if d.parent() is self.w]

    def side_docks(self) -> list[QDockWidget]:
        return [d for d in self.docks() if d.isVisible() and not d.isFloating() and self.w.dockWidgetArea(d) in SIDE]

    def watch(self) -> None:
        for d in self.docks():
            if id(d) in self._watched:
                continue
            self._watched.add(id(d))
            d.installEventFilter(self)
            d.visibilityChanged.connect(lambda _v: self.schedule())
            d.dockLocationChanged.connect(lambda _a: self.schedule())
            d.topLevelChanged.connect(lambda _f: self.schedule())
            d.destroyed.connect(lambda *_a, k=id(d): self._watched.discard(k))

    @staticmethod
    def natural_width(d: QDockWidget) -> int:
        inner = d.widget()
        want = inner.sizeHint().width() if inner is not None else 0
        return max(want, d.minimumSizeHint().width(), d.minimumWidth())

    @staticmethod
    def min_width(d: QDockWidget) -> int:
        return max(d.minimumSizeHint().width(), d.minimumWidth())

    def page_min(self) -> int:
        c = self.w.centralWidget()
        return c.minimumSizeHint().width() if c is not None else 0

    # ------------------------------------------------------------------ events
    def schedule(self) -> None:
        if self.enabled and not self._applying and not self._timer.isActive():
            self._timer.start(0)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        t = ev.type()
        if obj is self.w:
            if t == QEvent.Resize:
                self.schedule()
            elif t == QEvent.LayoutRequest:
                self.schedule()
        elif isinstance(obj, QDockWidget) and t == QEvent.Resize and not self._applying:
            # a separator drag (the mouse is down on the window): remember the user's size
            if QApplication.mouseButtons() & Qt.LeftButton and not obj.isFloating():
                area = self.w.dockWidgetArea(obj)
                if area in SIDE:
                    self.prefs[obj.objectName()] = obj.width()
                elif area in (Qt.BottomDockWidgetArea, Qt.TopDockWidgetArea):
                    self.vprefs[obj.objectName()] = obj.height()
        return False

    def remember(self, d: QDockWidget, width: Optional[int] = None) -> None:
        """Remember ``d``'s width as the user's (tests, restored layouts)."""
        self.prefs[d.objectName()] = width if width is not None else d.width()

    def forget(self) -> None:
        self.prefs.clear()
        self.vprefs.clear()

    # ------------------------------------------------------------------ policy
    def targets(self) -> dict[QDockWidget, int]:
        """Width per visible side dock: remembered or natural, shrunk in proportion (down to the
        minimums) when the docks and the page do not fit the window."""
        docks = self.side_docks()
        if not docks:
            return {}
        # one column per area: tabbed docks share it; docks split side by side add up
        cols: dict[int, list[QDockWidget]] = {}
        for d in docks:
            cols.setdefault(int(self.w.dockWidgetArea(d).value), []).append(d)
        want, low = {}, {}
        for area, ds in cols.items():
            groups: list[list[QDockWidget]] = []
            for d in ds:
                for g in groups:
                    if d in self.w.tabifiedDockWidgets(g[0]):
                        g.append(d)
                        break
                else:
                    groups.append([d])
            want[area] = [max(self.prefs.get(d.objectName(), self.natural_width(d)) for d in g) for g in groups]
            low[area] = [max(self.min_width(d) for d in g) for g in groups]
            cols[area] = [g[0] for g in groups]
        central = self.w.centralWidget()
        avail = (central.width() if central is not None else 0) + sum(
            g.width() for gs in cols.values() for g in gs)
        room = avail - self.page_min()
        total = sum(sum(v) for v in want.values())
        lows = sum(sum(v) for v in low.values())
        out: dict[QDockWidget, int] = {}
        for area, gs in cols.items():
            for i, g in enumerate(gs):
                w, lo = want[area][i], low[area][i]
                if total > room and total > lows:
                    # shrink in proportion to how much each can give
                    give = (total - max(room, lows)) * (w - lo) / max(total - lows, 1)
                    w = int(round(w - give))
                out[g] = max(lo, w)
        return out

    def apply(self) -> None:
        if not self.enabled or self._applying:
            return
        if QApplication.mouseButtons() & Qt.LeftButton:
            self._timer.start(50)          # not while the user drags a separator
            return
        self.watch()
        t = self.targets()
        changed = {d: w for d, w in t.items() if abs(d.width() - w) > 1}
        log = getattr(self.w, "log_dock", None)
        self._applying = True
        try:
            if changed:
                self.w.resizeDocks(list(changed), list(changed.values()), Qt.Horizontal)
            if log is not None and log.isVisible() and not log.isFloating():
                h = self.vprefs.get(log.objectName(), log.widget().sizeHint().height() if log.widget() else 0)
                central = self.w.centralWidget()
                if central is not None:
                    spare = central.height() + log.height() - central.minimumSizeHint().height()
                    h = max(log.minimumSizeHint().height(), min(h, spare))
                if abs(log.height() - h) > 1:
                    self.w.resizeDocks([log], [h], Qt.Vertical)
        finally:
            self._applying = False


__all__ = ["DockPolicy", "CANVAS_MIN"]
