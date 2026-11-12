"""Dock sizing policy of the main window (docs/dev/RESPONSIVE.md, rule R4).

* The side docks run the full height of the window (the bottom dock, the log, sits under the
  page only), so a control panel gets every vertical pixel the window has.
* A dock's width is the user's (a separator drag is remembered), else what its content asks for
  (its ``sizeHint``), never less than its content's minimum.  The page in the middle keeps its
  minimum (the canvas: :data:`CANVAS_MIN`, a content need: the drawing must stay workable).
* When the window shrinks the docks give way in proportion, down to their minimums; when it
  grows again they return to their remembered or natural widths (the layout recovers).
* A narrow window (a half-screen one) folds the side docks: when the window is narrower than
  the docks side by side and the page need, they stack into one tab group (:meth:`fold`), and
  when it is wide enough again they go back to the arrangement they had (:meth:`unfold`: the
  same areas, splits and tab groups, rebuilt dock by dock; not ``restoreState``, which would
  also bring back docks a workspace switch hid).  So the window's minimum width is what the
  folded docks and the page need (an explicit minimum while unfolded).  A dock the user moves
  or floats while folded ends the automatic folding: the user's arrangement wins (until Reset
  layout).  Docks are only ever moved by these two steps (moving them on every resize once
  left stale tab bars over the panels; a test checks that unfolding leaves none).

Applied on every window resize, workspace switch and dock visibility change (coalesced).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtWidgets import QApplication, QDockWidget, QMainWindow, QStyle, QTabBar

#: logical px: the canvas width a workspace with the shared canvas keeps (a content need: the
#: drawing stays workable, also in a half-screen window)
CANVAS_MIN = 260
#: logical px: the canvas width the docks give way to (down to their minimums) when the window
#: has the room: a comfortable drawing, CANVAS_MIN only when the window is too narrow for more
CANVAS_COMFORT = 480
#: logical px: below this canvas width the side docks fold into one tab group (fold)
CANVAS_FOLD = 400
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
        self._tried: Optional[tuple] = None      # the last resize asked for (see apply)
        #: folded (see fold): the arrangement to go back to and the width it needs
        self.folded: Optional[dict] = None
        #: the docks under the page folded into the side docks' tab group (vfold)
        self.vfolded: Optional[dict] = None
        self.user_arranged = False               # the user moved a dock while folded
        self.room: Optional[QSize] = None        # a screen smaller than the content (update_min)
        self._moving = False
        self._min = (0, 0)                       # the explicit minimum this set
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
            d.dockLocationChanged.connect(lambda _a: self._moved())
            d.topLevelChanged.connect(lambda _f: self._moved())
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

    def unfolded_need(self) -> int:
        """The window width the docks stay side by side at: the layout's minimum, with the
        shared canvas at least CANVAS_FOLD wide (the docks fold before they squeeze it to its
        bare minimum)."""
        need = self.w.minimumSizeHint().width()
        canvas = getattr(self.w, "canvas", None)
        if canvas is not None and canvas.isVisible():
            # the canvas view sits in its slot with a frame: a separator's width of slack, so
            # the view itself keeps CANVAS_FOLD (it came out 1 px short with an 11 pt font)
            need += CANVAS_FOLD - CANVAS_MIN + self._sep()
        return need

    def page_want(self) -> int:
        """The page width the docks give way to: its minimum, and with the shared canvas on it
        the canvas's comfortable width (the docks still never go below their minimums)."""
        need = self.page_min()
        canvas = getattr(self.w, "canvas", None)
        if canvas is not None and canvas.isVisible():
            need += CANVAS_COMFORT - CANVAS_MIN     # the page's minimum holds CANVAS_MIN
        return need

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
        self._tried = None
        self.folded = None
        self.vfolded = None
        self.user_arranged = False

    def _moved(self) -> None:
        # the user's own arrangement wins over the automatic one: a dock dragged (the mouse
        # is down; Qt also reports the moves of fold / unfold, and some of them only after
        # their layout pass) to another place or out of the window while folded
        if (not self._moving and self.folded is not None and not QApplication.closingDown()
                and QApplication.mouseButtons() & Qt.LeftButton):
            self.folded = None
            self.user_arranged = True
        self.schedule()

    # ------------------------------------------------------------------ folding
    def _sep(self) -> int:
        return self.w.style().pixelMetric(QStyle.PM_DockWidgetSeparatorExtent, None, self.w)

    def arrangement(self) -> list[tuple]:
        """The visible docked docks of the side and bottom areas as (area, groups,
        orientations): the tab groups of each area in order (each a list of docks, the shown
        tab first) and how each group sits next to the one before it."""
        out: list[tuple] = []
        moved = self.vfolded["docks"] if self.vfolded is not None else []
        docks = [d for d in self.docks() if d.isVisible() and not d.isFloating() and d not in moved]
        for area in (*SIDE, Qt.BottomDockWidgetArea):
            ds = [d for d in docks if self.w.dockWidgetArea(d) == area]
            groups: list[list[QDockWidget]] = []
            for d in ds:
                g = next((g for g in groups if d in self.w.tabifiedDockWidgets(g[0])), None)
                if g is None:
                    groups.append([d])
                else:
                    g.append(d)
            groups = [self._tab_order(g) for g in groups]
            # placed by the tab on top (a tab behind keeps a stale geometry)
            groups.sort(key=lambda g: (self._shown(g).x(), self._shown(g).y()))
            orients = []
            for a, b in zip(groups, groups[1:]):
                pa, pb = self._shown(a), self._shown(b)
                dx, dy = abs(pb.x() - pa.x()), abs(pb.y() - pa.y())
                orients.append(Qt.Vertical if dy > dx else Qt.Horizontal)
            if groups:
                out.append((area, groups, orients))
        return out

    def _tab_order(self, g: list) -> list:
        """A tab group's docks in the order of its tab bar."""
        if len(g) < 2:
            return g
        titles = {d.windowTitle(): d for d in g}
        for bar in self.w.findChildren(QTabBar):
            if bar.parent() is not self.w:
                continue
            texts = [bar.tabText(i) for i in range(bar.count())]
            if set(texts) == set(titles):
                return [titles[t] for t in texts]
        return g

    def _area_min(self, groups: list, orients: list) -> int:
        """The minimum width of an area's docks: groups side by side add up."""
        cols = [max(self.min_width(d) for d in groups[0])]
        for g, o in zip(groups[1:], orients):
            m = max(self.min_width(d) for d in g)
            if o == Qt.Horizontal:
                cols.append(m)
            else:
                cols[-1] = max(cols[-1], m)
        return sum(cols) + self._sep() * (len(cols) - 1)

    def _parts(self, arr: list) -> list[tuple[int, list]]:
        """(saving, part) for the parts of ``arr`` folding narrows the window by: the side docks
        in one tab group instead of their columns; the bottom docks (under the page) in one tab
        group when side by side they are wider than the page.  A part that saves nothing is
        left as it is."""
        out: list[tuple[int, list]] = []
        side = [x for x in arr if x[0] in SIDE]
        if sum(len(gs) for _a, gs, _o in side) > 1:
            cols = sum(self._area_min(gs, os_) + self._sep() for _a, gs, os_ in side)
            one = max(self.min_width(d) for _a, gs, _o in side for g in gs for d in g)
            save = cols - one - self._sep()
            if save > 0:
                out.append((save, side))
        bottom = [x for x in arr if x[0] == Qt.BottomDockWidgetArea]
        if bottom and len(bottom[0][1]) > 1:
            _a, gs, os_ = bottom[0]
            c = self.w.centralWidget()
            page = c.minimumSizeHint().width() if c is not None else 0
            one = max(self.min_width(d) for g in gs for d in g)
            save = max(page, self._area_min(gs, os_)) - max(page, one)
            if save > 0:
                out.append((save, bottom))
        return out

    def fold_need(self) -> int:
        """The window's minimum width with the docks folded (:meth:`fold`); its minimum as it is
        when they already are, or folding saves nothing."""
        need = self.w.minimumSizeHint().width()
        if self.folded is not None:
            return need
        return need - sum(save for save, _p in self._parts(self.arrangement()))

    def fold(self) -> None:
        """The visible side docks into one tab group (in the right-hand area when it has docks)
        and the bottom docks into another, remembering their arrangement (:meth:`unfold`)."""
        need = self.unfolded_need()
        narrow = self.fold_need()
        parts = self._parts(self.arrangement())
        if not parts:
            return
        arr = [x for _s, p in parts for x in p]
        shown = [self._shown(g) for _a, gs, _o in arr for g in gs]
        side = [x for x in arr if x[0] in SIDE]
        bottom = [x for x in arr if x[0] == Qt.BottomDockWidgetArea]
        kept: list[list] = []                # the tab groups the others join (left in place)
        self._moving = True
        try:
            if side:
                # onto the largest tab group (emptying a tab group by moving its docks away
                # left its tab bar drawn over the page), on its tab on top
                area, big = max(((a, g) for a, gs, _o in side for g in gs), key=lambda x: len(x[1]))
                head = self._shown(big)
                kept.append(big)
                for a, gs, _o in side:
                    for g in gs:
                        for d in g:
                            if d in big:
                                continue
                            if a != area:
                                self.w.addDockWidget(area, d)      # tabify alone leaves it there
                            self.w.tabifyDockWidget(head, d)
                head.raise_()
            if bottom:
                big = max(bottom[0][1], key=len)
                head = self._shown(big)
                kept.append(big)
                for g in bottom[0][1]:
                    for d in g:
                        if d not in big:
                            self.w.tabifyDockWidget(head, d)
                head.raise_()
        finally:
            self._moving = False
        self.folded = {"arrangement": arr, "need": need, "narrow": narrow,
                       "shown": shown, "kept": kept,
                       "docks": [d for _a, gs, _o in arr for g in gs for d in g]}
        self._tried = None
        if self.vfolded is not None:
            self.vfolded["narrow"] = 0       # measured again for this arrangement

    def unfold(self) -> None:
        """The docks back in the arrangement :meth:`fold` recorded."""
        rec = self.folded
        self.folded = None
        if rec is None:
            return
        self._moving = True
        try:
            # the docks that joined a kept group go back next to it; a kept group stays where
            # it is (moving its docks out and back in left its tab bar behind)
            for area, groups, orients in rec["arrangement"]:
                prev = None
                for i, g in enumerate(groups):
                    first = g[0]
                    if any(g is k for k in rec["kept"]):
                        prev = first
                        continue
                    if first.isFloating():
                        continue
                    if prev is None:
                        self.w.addDockWidget(area, first)
                    else:
                        # out of the folded group, after the group before it (splitDockWidget
                        # next to a tabbed dock only adds another tab)
                        self.w.addDockWidget(area, first, orients[i - 1])
                    for d in g[1:]:
                        if not d.isFloating():
                            if self.w.dockWidgetArea(d) != area:
                                self.w.addDockWidget(area, d)   # tabify alone leaves it there
                            self.w.tabifyDockWidget(first, d)
                    prev = first
            for d in rec["shown"]:
                if not d.isFloating():
                    d.raise_()                       # the tab that was on top is again
        finally:
            self._moving = False
        self._tried = None

    def _fold_step(self) -> bool:
        """Fold or unfold for the window's width; True when docks moved."""
        if self.user_arranged:
            return False
        width = self.w.width()
        if self.folded is not None:
            shown = [d for d in self.folded["docks"] if d.isVisible() and not d.isFloating()]
            # a dock shown since (another workspace's) that folding would move, not in the
            # folded groups: unfolded and folded again with it
            new = [d for _s, part in self._parts(self.arrangement()) for _a, gs, _o in part
                   for g in gs for d in g if d not in self.folded["docks"]]
            # a folded dock hidden or floated since (a workspace switch, the user closed it):
            # the need recorded is no longer this layout's; unfolded, and folded again if the
            # window is still too narrow
            gone = len(shown) < len(self.folded["docks"])
            if width >= self.folded["need"]:
                self.unfold()
                return True
            if gone or new:
                # folded again at once for the docks shown now: never a turn with the docks
                # side by side in a narrow window (its minimum widened the window)
                self.unfold()
                self._refold()
                return True
            return False
        return self._refold()

    # ------------------------------------------------------------------ folding: height
    def _target_height(self) -> int:
        """The height the window should have: the user's (a page that needed more room may
        have grown the window meanwhile), else its own."""
        us = getattr(getattr(getattr(self.w, "shell", None), "host", None), "user_size", None)
        size = getattr(us, "size", None)
        h = self.w.height()
        return min(h, size.height()) if size is not None else h

    def _height_need(self) -> int:
        """The window height its layout needs: its minimum, and where the page in the middle
        does not get the height it needs although that minimum says it does (a page's minimum
        is capped by the screen: a window as tall as the screen, 1280 x 800 on an 800 px screen
        with a larger font), the window's height plus what the page lacks, so the docks under
        the page make room for it instead of the page scrolling.  Also with the side docks
        folded (683 x 875 on Windows: the Library short by 25 px with two docks under it), unless
        the folded side group, holding the bottom docks' minimum widths, would leave the page
        less than its width (:meth:`_squeezes_page`): there only the window's own minimum
        decides."""
        need = self.w.minimumSizeHint().height()
        central = self.w.centralWidget()
        if central is None:
            return need
        bottom = [d for d in self.docks() if d.isVisible() and not d.isFloating()
                  and self.w.dockWidgetArea(d) == Qt.BottomDockWidgetArea]
        side = self.side_docks()
        if not bottom or not side:
            return need
        # the side group takes the bottom docks only where the width it then needs (their
        # minimum) leaves the page area its own minimum (macOS, 683 x 875: the preview's
        # minimum squeezed the Process page below its own)
        grow = max([0] + [self.min_width(d) for d in bottom]) - max([0] + [d.width() for d in side])
        from ..responsive import FitArea
        short = 0
        for a in central.findChildren(FitArea):
            if not a.isVisible() or a.widget() is None:
                continue
            room = a.contentsRect().size()
            page = a.need(room.width())
            if a._cap(page).height() < page.height():          # the cap hides the page's need
                if grow > 0 and central.width() - grow < self._page_width_want(a, page):
                    return need
                short = max(short, page.height() - room.height())
        return max(need, self.w.height() + short) if short > 0 else need

    def _page_width_want(self, a, page) -> int:
        """The room the FitArea ``a`` needs across the middle for its page (``page``: its
        minimum size), and the canvas beside it at its minimum when they are side by side and
        the canvas is shown (the Library hides it)."""
        want = page.width() + a.verticalScrollBar().sizeHint().width()
        sp = a.parentWidget()
        while sp is not None and not hasattr(sp, "fit_orientation"):
            sp = sp.parentWidget()
        canvas = getattr(self.w, "canvas", None)
        if (sp is not None and getattr(sp, "mode", "") == "side" and sp.isAncestorOf(a)
                and canvas is not None and canvas.isVisible()):
            want += CANVAS_MIN + sp.handleWidth()
        return want

    def _squeezes_page(self) -> bool:
        """The docks folded under the page for what the screen cap hid (vfold ``deficit``) now
        take, in the side group, width the page needs (the side docks folded too: a narrow
        window, the group holds the bottom docks' minimum widths)."""
        from ..responsive import FitArea
        central = self.w.centralWidget()
        if central is None:
            return False
        for a in central.findChildren(FitArea):
            if a.isVisible() and a.widget() is not None:
                if central.width() < self._page_width_want(a, a.need(a.contentsRect().width())):
                    return True
        return False

    @staticmethod
    def _page_short(central) -> int:
        """How much height the workspace page lacks now, where its minimum is capped by the
        screen (FitArea's cap) and so does not ask the window for it."""
        from ..responsive import FitArea
        short = 0
        for a in central.findChildren(FitArea):
            if not a.isVisible() or a.widget() is None:
                continue
            room = a.contentsRect().size()
            page = a.need(room.width())
            if a._cap(page).height() < page.height():
                short = max(short, page.height() - room.height())
        return short

    def vfold(self) -> bool:
        """A window too short for the docks under the page (the log, the preview): they join
        the side docks' largest tab group, remembering where they were (:meth:`vunfold`).
        Only with side docks to join (a dock column added for them would widen the window)."""
        arr = self.arrangement()
        bottom = [x for x in arr if x[0] == Qt.BottomDockWidgetArea]
        side = [x for x in arr if x[0] in SIDE]
        if not bottom or not side:
            return False
        own = self.w.minimumSizeHint().height()
        need = self._height_need()
        area, big = max(((a, g) for a, gs, _o in side for g in gs), key=lambda x: len(x[1]))
        head = self._shown(big)
        shown = [self._shown(g) for _a, gs, _o in bottom for g in gs]
        self._moving = True
        try:
            for _a, gs, _o in bottom:
                for g in gs:
                    for d in g:
                        self.w.addDockWidget(area, d)       # tabify alone leaves it there
                        self.w.tabifyDockWidget(head, d)
            head.raise_()                                   # the side panel stays on top
        finally:
            self._moving = False
        # ``deficit``: folded for what the screen cap hid (not the window's own minimum): undone
        # when the side docks fold too and the side group then holds the bottom docks' minimum
        # widths against the page (a narrow window, :meth:`_squeezes_page`)
        # ``narrow``: with the side docks folded too, the width the window turned out to need
        # with their group holding the bottom docks' minimum widths (:meth:`update_min`)
        self.vfolded = {"arrangement": bottom, "need": need, "shown": shown, "head": head,
                        "docks": [d for _a, gs, _o in bottom for g in gs for d in g],
                        "deficit": need > own, "narrow": 0}
        self._tried = None
        return True

    def vunfold(self) -> None:
        """The docks :meth:`vfold` moved back under the page, as they were."""
        rec = self.vfolded
        self.vfolded = None
        if rec is None:
            return
        self._moving = True
        try:
            for area, groups, orients in rec["arrangement"]:
                prev = None
                for i, g in enumerate(groups):
                    first = g[0]
                    if first.isFloating():
                        continue
                    if prev is None:
                        self.w.addDockWidget(area, first)
                    else:
                        self.w.addDockWidget(area, first, orients[i - 1])
                    for d in g[1:]:
                        if not d.isFloating():
                            self.w.addDockWidget(area, d)
                            self.w.tabifyDockWidget(first, d)
                    prev = first
            for d in [*rec["shown"], rec["head"]]:
                if not d.isFloating() and d.isVisible():
                    d.raise_()
        finally:
            self._moving = False
        self._tried = None

    def _vfold_step(self, height: int) -> bool:
        """Fold or unfold the docks under the page for ``height``; True when docks moved."""
        if self.user_arranged:
            return False
        if self.vfolded is not None:
            rec = self.vfolded
            shown = [d for d in rec["docks"] if d.isVisible() and not d.isFloating()]
            new = [d for _a, gs, _o in self.arrangement() if _a == Qt.BottomDockWidgetArea
                   for g in gs for d in g]
            if (height >= rec["need"] or len(shown) < len(rec["docks"]) or new or not rec["head"].isVisible()
                    or (rec.get("deficit") and self.folded is not None and self._squeezes_page())):
                self.vunfold()
                if height < self._height_need():
                    self.vfold()                            # again, with the docks shown now
                return True
            return False
        if height < self._height_need() and not QApplication.closingDown():
            return self.vfold()
        return False

    def fit_width(self, width: int, height: Optional[int] = None) -> None:
        """Fold, at once, the docks a workspace switch has just shown when ``width`` is too
        narrow for them side by side, and the docks under the page when ``height`` is too short
        for them (WorkspaceHost.activate, before the window's minimum follows the new page)."""
        if not self.enabled or self.user_arranged:
            return
        if self.vfolded is not None:
            self.vunfold()
        if self.folded is not None:
            self.unfold()
        hint = self.w.minimumSizeHint().width()
        if width < self.unfolded_need() and self.fold_need() < hint:
            self.fold()
        if height is not None and height < self._height_need():
            self.vfold()
        self.update_min()

    def _refold(self) -> bool:
        hint = self.w.minimumSizeHint().width()
        if (self.w.width() < self.unfolded_need() and self.fold_need() < hint
                and not QApplication.closingDown()):
            self.fold()
            return True
        return False

    def update_min(self) -> None:
        """The window's explicit minimum: while unfolded the window may be as narrow as the
        folded docks need (it folds them then), else the layout's own minimum; on a screen
        smaller than that (:attr:`room`, set by Shell._clamp_to_screen) at most the screen,
        where a page too large scrolls."""
        want_w = 0
        if self.folded is not None:
            # what the folded docks and the page need (measured before folding: the layout's
            # own minimum still counts the docks side by side until it is laid out again)
            want_w = self.folded["narrow"]
            v = self.vfolded
            if v is not None:
                # the docks from under the page joined the side group after that measure, and
                # the group holds their minimum widths: where the page is left less than its
                # minimum, the layout's minimum as it is laid out now, kept until the docks
                # move again (Process at its minimum size: the preview's 313 px in a group
                # measured at 177 cut the canvas bar and the empty-state panel, Windows CI)
                c = self.w.centralWidget()
                if c is not None and c.width() < c.minimumSizeHint().width():
                    v["narrow"] = max(v["narrow"], self.w.minimumSizeHint().width())
                want_w = max(want_w, v["narrow"])
        elif not self.user_arranged:
            f = self.fold_need()
            if f < self.w.minimumSizeHint().width():
                want_w = f
        # the height too: the layout's present minimum, set on every pass (a minimum left to
        # the layout is the one of its last activation, which could be an earlier, taller state:
        # the window was held 885 px tall with 731 needed and could not return to the user's
        # 875 px, Windows CI)
        hint = self.w.minimumSizeHint()
        want_h = hint.height()
        if self.room is not None:
            want_w = min(want_w or hint.width(), self.room.width())
            want_h = min(hint.height(), self.room.height())
        if (want_w, want_h) != self._min or self.w.minimumSize().toTuple() != (want_w or self.w.minimumWidth(), want_h):
            self._min = (want_w, want_h)
            # width 0: the layout's own minimum
            self.w.setMinimumSize(want_w, want_h)

    @staticmethod
    def _shown(group: list) -> QDockWidget:
        """The tab on top of a tab group (the one whose area is drawn), else the first."""
        for d in group:
            if not d.visibleRegion().isEmpty():
                return d
        return group[0]

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
            # the docks under the page folded into a side group for what the screen cap hid
            # (vfold, ``deficit``) do not widen it: their natural width is the page's (the log
            # asks for 800 px), which took the canvas's width.  Folded for the window's own
            # minimum (a short half-screen window) they still do: the group takes the width
            # the canvas and the page cannot both use side by side (macOS CI, 683 x 875:
            # narrower, it left room for them side by side and the page got 141 px)
            rec = self.vfolded
            moved = rec["docks"] if rec is not None and rec.get("deficit") else ()
            want[area] = [max(self.prefs.get(d.objectName(), self.natural_width(d))
                              for d in (g if all(d in moved for d in g) else [d for d in g if d not in moved]))
                          for g in groups]
            low[area] = [max(self.min_width(d) for d in g) for g in groups]
            # a tab group is measured and resized through the tab on top: a tab behind it keeps
            # a stale geometry (its width already "at the target" left the shown tab unsized:
            # the Properties dock stayed 63 px narrower until the window was resized)
            cols[area] = [self._shown(g) for g in groups]
        central = self.w.centralWidget()
        avail = (central.width() if central is not None else 0) + sum(
            g.width() for gs in cols.values() for g in gs)
        room = avail - self.page_want()
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
        h = self._target_height()
        # the docks under the page come back first (they went into the side docks' group
        # after the side docks folded, and are restored before those unfold)
        if (self.vfolded is not None and self._vfold_step(h)) or self._fold_step() or self._vfold_step(h):
            self._timer.start(0)             # sized once the docks have their new places
            return
        self.update_min()
        t = self.targets()
        changed = {d: w for d, w in t.items() if abs(d.width() - w) > 1}
        log = getattr(self.w, "log_dock", None)
        vert: list = []
        if log is not None and log.isVisible() and not log.isFloating():
            h = self.vprefs.get(log.objectName(), log.widget().sizeHint().height() if log.widget() else 0)
            central = self.w.centralWidget()
            if central is not None:
                # less what the page in the middle lacks where the screen cap hides its need
                # (Nest on a 1280 x 800 screen with a taskbar): the log gives that room first
                spare = (central.height() + log.height() - central.minimumSizeHint().height()
                         - self._page_short(central))
                h = max(log.minimumSizeHint().height(), min(h, spare))
            if abs(log.height() - h) > 1:
                vert = [log, h]
        # the same resize asked again of the same geometry: the window cannot give it (smaller
        # than its content's minimum, on a screen below that).  Not asked again: each
        # resizeDocks relaid the window out, which scheduled this again, for ever (97 % idle
        # CPU on an 800 x 800 screen)
        tried = (self.w.size().toTuple(), tuple((id(d), d.width(), wd) for d, wd in changed.items()),
                 (id(vert[0]), vert[0].height(), vert[1]) if vert else None)
        if not changed and not vert:
            self._tried = None
            return
        if tried == self._tried:
            return
        self._tried = tried
        self._applying = True
        try:
            if changed:
                self.w.resizeDocks(list(changed), list(changed.values()), Qt.Horizontal)
            if vert:
                self.w.resizeDocks([vert[0]], [vert[1]], Qt.Vertical)
        finally:
            self._applying = False


__all__ = ["DockPolicy", "CANVAS_MIN", "CANVAS_COMFORT"]
