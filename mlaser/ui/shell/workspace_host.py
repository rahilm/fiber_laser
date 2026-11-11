"""The workspace host: the central page stack, the left rail and the per-workspace toolbars.

See :mod:`mlaser.services.workspaces` for the slots and :class:`WorkspaceSpec` for the options a
plugin passes to ``ctx.add_workspace``.  Each workspace is a :class:`WorkspacePage`: a slim
contextual toolbar on top, then either

* the **shared canvas** (``canvas=True``; the one ``window.canvas`` is moved into the active
  page), with the workspace's own widget, if any, in a side panel to its right; or
* the workspace's **own widget** (one contribution), a tab widget (several contributions to the
  same slot) or, until a plugin fills the slot, an **empty-state placeholder** that offers the
  task's classic commands as large buttons.

The rail is a vertical, non-movable QToolBar (``shell_rail``) with one checkable button per
workspace the user mode may see; Ctrl+1..6 (and each workspace's ``shortcut``) switch.

**Exclusive workspaces** (the Run operator screen, :data:`EXCLUSIVE`, or a spec with
``extra["exclusive_docks"]``): no docked panel shares the window with them.  Every dock that is
not one of the workspace's own is hidden while it is active, also one a plugin shows later (the
classic Nest dock used to squeeze the Run screen until STOP, START and Arm were cut off at
1440 × 900, review 2), and shown again when the operator leaves the workspace.  A floating dock
does not take room from the page and is left alone.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
                               QSplitter, QStyle, QTabBar, QTabWidget, QToolBar, QToolButton,
                               QVBoxLayout, QWidget)

from mlaser.services import usermodes as um
from mlaser.services.workspaces import BUILTIN, SLOTS, WorkspaceSpec, slot_for

from .. import style
from ..icons import command_icon, icon
from ..responsive import CurrentPageStack, FitArea
from .dock_policy import CANVAS_COMFORT, CANVAS_MIN


#: workspaces that never share the window with docked panels (see the module docstring)
EXCLUSIVE = frozenset({"run"})


class EmptyState(QWidget):
    """Placeholder of a workspace slot that no plugin has filled yet: title, what the workspace
    will do, and the task's commands as large, keyboard-reachable buttons."""

    def __init__(self, host: "WorkspaceHost", spec: WorkspaceSpec, commands: tuple[str, ...]):
        super().__init__()
        self.setObjectName("shell_empty")
        self.host = host
        self.spec = spec
        tr = host.tr
        outer = QVBoxLayout(self)
        outer.setContentsMargins(style.SP5, style.SP4, style.SP5, style.SP4)
        self.title = QLabel()
        self.title.setObjectName("shell_empty_title")
        tr.bind(self.title, spec.title, self.title.setText)
        self.text = QLabel()
        self.text.setObjectName("shell_empty_text")
        self.text.setWordWrap(True)
        tr.bind(self.text, spec.description, self.text.setText)
        self.note = QLabel()
        self.note.setObjectName("shell_empty_text")
        self.note.setWordWrap(True)
        tr.bind(self.note, "shell.ws.placeholder_note", self.note.setText)
        outer.addWidget(self.title)
        outer.addWidget(self.text)
        outer.addWidget(self.note)
        grid_box = QWidget()
        self.grid = QGridLayout(grid_box)
        self.grid.setContentsMargins(0, style.SP3, 0, 0)
        self.grid.setSpacing(style.SP2)
        self.buttons: dict[str, QToolButton] = {}
        self.commands = commands
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setWidget(grid_box)
        sa.viewport().setAutoFillBackground(False)
        grid_box.setAutoFillBackground(False)
        outer.addWidget(sa, 1)
        self.rebuild()

    def rebuild(self) -> None:
        while self.grid.count():
            it = self.grid.takeAt(0)
            if it.widget() is not None:
                it.widget().hide()
                it.widget().deleteLater()
        self.buttons.clear()
        n = 0
        cols = 4
        for cmd in self.commands:
            act = self.host.action(cmd)
            if act is None or not self.host.allowed(cmd):
                continue
            b = QToolButton()
            b.setObjectName("shell_empty_cmd")
            b.setDefaultAction(act)
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setIconSize(QSize(32, 32))
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setFocusPolicy(Qt.StrongFocus)
            self.grid.addWidget(b, n // cols, n % cols)
            self.buttons[cmd] = b
            n += 1
        self.grid.setRowStretch(n // cols + 1, 1)


#: logical px: the height a canvas stacked over its page keeps; when the page and this do not
#: both fit, the canvas and the page are tabs (each the whole height)
CANVAS_MIN_H = 240


class CanvasSplitter(QSplitter):
    """The canvas and the page beside it (Process, ...), in the arrangement the room allows:

    * ``side``: side by side while both have their minimum widths;
    * ``stack``: the page under the canvas in a narrower window (a half-screen one), while the
      page's height and a workable canvas (:data:`CANVAS_MIN_H`) both fit;
    * ``tabs``: else one of them at a time with the whole room, switched by the page's tab bar
      (:attr:`tab`: 0 the canvas, 1 the page; the user's choice is remembered).

    The minimum width is the wider of the two, not their sum, so a workspace switch reflows
    instead of widening the window.  The choice follows the size alone (the minimum follows
    the choice, never the reverse)."""

    modeChanged = Signal(str)

    def __init__(self, *a) -> None:
        super().__init__(*a)
        self.mode = "side"
        self.tab = 0

    def _canvas_wanted(self) -> bool:
        return self.count() > 1 and not self.widget(0).isHidden()

    def _shown(self) -> list:
        """The canvas when it is wanted (a page may hide it: Process › Library) and the page
        (hidden only while the canvas tab is on top)."""
        return [self.widget(i) for i in range(self.count()) if i == 1 or not self.widget(i).isHidden()]

    def _side_by_side_need(self) -> int:
        ws = [x.minimumSizeHint().expandedTo(x.minimumSize()).width() for x in self._shown()]
        return sum(ws) + self.handleWidth() * max(len(ws) - 1, 0)

    def page_height(self) -> int:
        """The height the page needs at the splitter's width (its wrapper's need, height-for-
        width aware).  Not the wrapper's minimum: that is capped by what the screen leaves
        around the wrapper, and under the canvas almost nothing is left."""
        if self.count() < 2:
            return 0
        own = self.widget(1)
        areas = [own] if isinstance(own, FitArea) else own.findChildren(FitArea)
        area = next((a for a in areas if a.isVisibleTo(own)), None)
        if area is None:
            return own.minimumSizeHint().expandedTo(own.minimumSize()).height()
        chrome = max(own.height() - area.viewport().height(), 0) if own is not area else 0
        if self.orientation() == Qt.Vertical or own.isHidden():
            # under the canvas, or in its tab: the splitter's whole width (a hidden page's
            # own width is stale)
            width = max(self.width() - (max(own.width() - area.viewport().width(), 0)
                                        if own.isVisible() else 0), 1)
        else:
            width = max(area.viewport().width(), 1)
        return area.need(width).height() + chrome

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        s = super().minimumSizeHint()
        ws = [x.minimumSizeHint().expandedTo(x.minimumSize()).width() for x in self._shown()]
        h = s.height()
        if self.mode in ("stack", "tabs"):
            # what the smaller arrangement (tabs) needs: stacking never raises the window,
            # it is used only while the height allows it
            h = max(CANVAS_MIN_H, self.page_height())
        return QSize(max(ws, default=s.width()), h)

    def fit_orientation(self) -> None:
        if not self._canvas_wanted() or self.width() >= self._side_by_side_need():
            mode = "side"
        elif self.height() >= CANVAS_MIN_H + self.handleWidth() + self.page_height():
            mode = "stack"
        else:
            mode = "tabs"
        want = Qt.Horizontal if mode == "side" else Qt.Vertical
        if self.orientation() != want:
            self.setOrientation(want)
        if mode != self.mode:
            self.mode = mode
            self.updateGeometry()
            self.modeChanged.emit(mode)

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(ev)
        self.fit_orientation()


class WorkspacePage(QWidget):
    """One workspace: toolbar + body (see the module docstring)."""

    def __init__(self, host: "WorkspaceHost", spec: WorkspaceSpec):
        super().__init__()
        self.host = host
        self.spec = spec
        self.setObjectName(f"workspace_{spec.id}")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self._toolbar_need = 0            # the tool bar's width with icons only (fit_toolbar)
        self._user_split = False          # the user dragged the canvas / page splitter
        self._fitting_split = False
        self._wide_need: Optional[int] = None    # fit_split widened the page: its narrow need
        self.toolbar = QToolBar()
        self.toolbar.setObjectName("shell_ws_toolbar")
        self.toolbar.setIconSize(QSize(20, 20))
        self.toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tr = host.tr
        tr.bind(self.toolbar, spec.title or spec.id, lambda t: self.toolbar.setAccessibleName(
            tr.format("a11y.ws.toolbar", workspace=t)))
        v.addWidget(self.toolbar)
        # whenever the extension button appears (the layout pass of a style that lays the
        # buttons out wider than their size hints, after the check fit_toolbar queued had
        # already found none: Windows CI, every width of a sweep), the tool bar steps down
        ext = self.toolbar.findChild(QToolButton, "qt_toolbar_ext_button")
        if ext is not None:
            ext.installEventFilter(self)
        # the second row of the tool bar in a narrow window (fit_toolbar): icons only, the
        # commands after the separator nearest the middle; never an overflow menu
        self.toolbar2 = QToolBar()
        self.toolbar2.setObjectName("shell_ws_toolbar_2")
        self.toolbar2.setIconSize(QSize(20, 20))
        self.toolbar2.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.toolbar2.setVisible(False)
        tr.bind(self.toolbar2, spec.title or spec.id, lambda t: self.toolbar2.setAccessibleName(
            tr.format("a11y.ws.toolbar2", workspace=t)))
        v.addWidget(self.toolbar2)
        self._acts: list = []             # the tool bar's actions in order (both rows)
        self._split = 0                   # how many of them the first row holds
        self.body = QWidget()
        self.body_lay = QHBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 0, 0, 0)
        self.body_lay.setSpacing(0)
        # the canvas and the page as tabs when stacked they do not both fit (CanvasSplitter)
        self.view_tabs = style.tabs(QTabBar())
        self.view_tabs.setObjectName("shell_view_tabs")
        self.view_tabs.setExpanding(False)
        self.view_tabs.setVisible(False)
        self.view_tabs.currentChanged.connect(self._view_tab_chosen)
        v.addWidget(self.view_tabs)
        v.addWidget(self.body, 1)
        self.splitter: Optional[QSplitter] = None
        self.canvas_slot: Optional[QWidget] = None
        self.contributions: list[WorkspaceSpec] = []
        self.placeholder: Optional[EmptyState] = None
        self.tabs: Optional[QTabWidget] = None
        self._build_body()
        self.fill_toolbar()

    # ------------------------------------------------------------------ body
    def _build_body(self) -> None:
        canvas = self.host.canvas_widget()
        if canvas is not None and self.isAncestorOf(canvas):
            canvas.hide()
            canvas.setParent(self.host.stack)       # parked; show_canvas puts it back
        old = []
        while self.body_lay.count():
            it = self.body_lay.takeAt(0)
            if it.widget() is not None:
                old.append(it.widget())
        for w in old:                  # release the contributed widgets from their old wrappers
            areas = [w] if isinstance(w, QScrollArea) and w.objectName() == "shell_ws_scroll" else []
            areas += [a for a in w.findChildren(QScrollArea, "shell_ws_scroll")]
            for a in areas:
                inner = a.takeWidget()
                if inner is not None:
                    inner.setParent(self.host.stack)
                    inner.hide()
        self.canvas_slot = None
        self.splitter = None
        self.tabs = None
        spec = self.spec
        own = self._own_widget()
        if spec.canvas:
            self.splitter = CanvasSplitter(Qt.Horizontal)
            self.splitter.setChildrenCollapsible(True)
            self.canvas_slot = QWidget()
            self.canvas_slot.setMinimumWidth(CANVAS_MIN)       # the drawing stays workable
            lay = QVBoxLayout(self.canvas_slot)
            lay.setContentsMargins(0, 0, 0, 0)
            self.splitter.addWidget(self.canvas_slot)
            if own is not None:
                self.splitter.addWidget(own)
                self.splitter.setStretchFactor(0, 3)
                self.splitter.setStretchFactor(1, 2)
                self.splitter.setSizes([600, 400])
                self._user_split = False
                self.splitter.modeChanged.connect(self._split_mode)
                self.splitter.tab = self._remembered_tab()
                self.splitter.splitterMoved.connect(self._split_moved)
            self.body_lay.addWidget(self.splitter)
        else:
            if own is None:
                self.placeholder = EmptyState(self.host, spec, tuple(spec.extra.get("commands", spec.toolbar)))
                own = self.placeholder
            self.body_lay.addWidget(own)
        for w in old:               # contributed widgets were moved into the new body first
            if w is not own:
                w.hide()
                w.deleteLater()
        self._old = None

    @staticmethod
    def _fit(widget: QWidget) -> QWidget:
        """A contributed page in a :class:`~mlaser.ui.responsive.FitArea`: the window keeps the
        page's own minimum (the page reflows above it, it never scrolls), capped by the screen
        so a page too large for a small screen scrolls instead of pushing the window off it."""
        sa = FitArea(widget)
        widget.show()
        return sa

    def _own_widget(self) -> Optional[QWidget]:
        ws = [c.widget for c in self.contributions if c.widget is not None]
        if not ws and self.spec.widget is not None and not self.spec.placeholder:
            ws = [self.spec.widget]
        if not ws:
            return None
        if len(ws) == 1:
            return self._fit(ws[0])
        self.tabs = style.tabs(QTabWidget(), "workspace")      # a workspace's main views
        for c in self.contributions:
            if c.widget is not None:
                i = self.tabs.addTab(self._fit(c.widget), "")
                self.host.tr.bind(c.widget, c.title or c.id, lambda t, i=i, tb=self.tabs: tb.setTabText(i, t))
        return self.tabs

    def contribute(self, spec: WorkspaceSpec) -> None:
        """A plugin fills this slot (see the slot rule in :mod:`mlaser.services.workspaces`)."""
        first = not self.contributions
        self.contributions.append(spec)
        s = self.spec
        if first:
            # the first contribution decides the page kind; options it leaves out keep the slot's
            s.canvas = bool(spec.extra.get("canvas_given")) and spec.canvas
            if spec.toolbar:
                s.toolbar = tuple(spec.toolbar)
            elif not s.canvas:
                s.toolbar = ()
            if spec.docks is not None:
                s.docks = tuple(spec.docks)
            if spec.extra.get("min_mode_given"):
                s.min_mode = spec.min_mode
            if spec.description:
                s.description = spec.description
            s.placeholder = False
        s.owner = spec.owner
        self.placeholder = None
        self._build_body()
        self.fill_toolbar()

    # ------------------------------------------------------------------ toolbar
    def fill_toolbar(self) -> None:
        tb = self.toolbar
        tb.clear()
        self.toolbar2.clear()
        self.toolbar2.setVisible(False)
        last_sep = True
        for cmd in self.spec.toolbar:
            if cmd == "-":
                if not last_sep:
                    tb.addSeparator()
                    last_sep = True
                continue
            act = self.host.action(cmd)
            if act is None or not self.host.allowed(cmd):
                continue
            tb.addAction(act)
            self._dress(tb, act)
            last_sep = False
        self._acts = list(tb.actions())
        self._split = len(self._acts)
        tb.setVisible(bool(tb.actions()))
        self.fit_toolbar()

    @staticmethod
    def _dress(tb: QToolBar, act) -> None:
        btn = tb.widgetForAction(act)
        if btn is not None and not act.isSeparator():
            btn.setAccessibleName(act.text().replace("&", ""))
            btn.setFocusPolicy(Qt.TabFocus)
            if act.icon().isNull():
                btn.setToolButtonStyle(Qt.ToolButtonTextOnly)

    def _label_order(self) -> list:
        """The tool bar's actions in the order they get their labels when not all fit: the
        workspace's ``toolbar_labels`` (its most-used commands) first, then the tool bar's
        order."""
        acts = [a for a in self._acts if not a.isSeparator()]
        first = []
        for cmd in self.spec.extra.get("toolbar_labels", ()) or ():
            act = self.host.action(cmd)
            if act is not None and act in acts and act not in first:
                first.append(act)
        return first + [a for a in acts if a not in first]

    def toolbar_actions(self) -> list:
        """The tool bar's actions in order, both rows."""
        return list(self._acts)

    def toolbar_buttons(self) -> list:
        """The tool bar's buttons in order (the keyboard order), both rows."""
        out = []
        for act in self._acts:
            for tb in (self.toolbar, self.toolbar2):
                b = tb.widgetForAction(act)
                if b is not None and isinstance(b, QToolButton):
                    out.append(b)
                    break
        return out

    def _place(self, split: int) -> None:
        """The first ``split`` actions in the first row, the rest in the second."""
        if split == self._split:
            return
        self._split = split
        tb, tb2 = self.toolbar, self.toolbar2
        for t in (tb, tb2):
            for act in list(t.actions()):
                t.removeAction(act)
        first, second = self._acts[:split], self._acts[split:]
        while second and second[0].isSeparator():
            second = second[1:]
        while first and first[-1].isSeparator():
            first = first[:-1]
        for act in first:
            tb.addAction(act)
            self._dress(tb, act)
        for act in second:
            tb2.addAction(act)
            self._dress(tb2, act)
        tb2.setVisible(bool(second) and tb.isVisibleTo(self))

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(ev)
        self.fit_toolbar()
        self.fit_split()

    def changeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().changeEvent(ev)
        if ev.type() in (QEvent.StyleChange, QEvent.FontChange):
            # the tool bar's width (and so this page's minimum) changes with the style and the
            # font: measured again now, not at the next resize (after a theme switch the page
            # kept the old minimum until the window was resized, and the docks beside it took
            # their widths from that stale value)
            self.fit_toolbar()

    def _split_moved(self, _pos: int, _index: int) -> None:
        if not self._fitting_split:
            self._user_split = True          # the user's split stays (until a reset)

    # ------------------------------------------------------------------ canvas | page tabs
    def _config(self):
        return getattr(self.host.w, "config", None)

    def _remembered_tab(self) -> int:
        cfg = self._config()
        try:
            tabs = dict(cfg.get("shell.view_tabs", {}) or {}) if cfg is not None else {}
        except Exception:  # noqa: BLE001 - an unregistered key in a test configuration
            tabs = {}
        return 1 if int(tabs.get(self.spec.id, 0) or 0) == 1 else 0

    def _split_mode(self, mode: str) -> None:
        tb = self.view_tabs
        if mode == "tabs":
            if tb.count() == 0:
                tb.blockSignals(True)
                tb.addTab(self.host.tr.tr("shell.view.canvas"))
                tb.addTab(self.host.tr.tr(self.spec.title) if self.spec.title else self.spec.id)
                tb.blockSignals(False)
            tb.blockSignals(True)
            tb.setCurrentIndex(self.splitter.tab if self.splitter is not None else 0)
            tb.blockSignals(False)
        tb.setVisible(mode == "tabs")
        self.fit_split()

    def _view_tab_chosen(self, i: int) -> None:
        sp = self.splitter
        if sp is None or i < 0:
            return
        sp.tab = 1 if i == 1 else 0
        cfg = self._config()
        if cfg is not None:
            try:
                tabs = dict(cfg.get("shell.view_tabs", {}) or {})
                tabs[self.spec.id] = sp.tab
                cfg.set("shell.view_tabs", tabs)            # the user's choice is remembered
            except Exception:  # noqa: BLE001 - an unregistered key in a test configuration
                pass
        self.fit_split()

    def _canvas_shown(self) -> bool:
        sp = self.splitter
        return sp is not None and sp.count() > 1 and not sp.widget(0).isHidden()

    def fit_split(self) -> None:
        """The page beside the canvas gets the width its content asks for (its size hint), less
        what keeps the canvas at CANVAS_COMFORT as long as the page still fits its height there,
        and, if the hint is not enough for the height it has, more, until it fits or the canvas
        is down to CANVAS_MIN; the canvas takes the rest.  A fixed share (40 %) left the Process page
        so narrow that its wrapped rows pushed the window taller than the screen with the
        Windows and macOS fonts (test_panels_with_a_selection: 1280 x 892)."""
        sp = self.splitter
        if sp is None or sp.count() < 2:
            return
        if isinstance(sp, CanvasSplitter) and sp.mode != "tabs" and sp.widget(1).isHidden():
            sp.widget(1).show()                 # out of the tabs: the page again
        if not sp.isVisible() or (self._user_split and getattr(sp, "mode", "") != "tabs"):
            return
        if sp.orientation() != Qt.Horizontal:
            total = sp.height() - sp.handleWidth()
            if total <= 0 or not isinstance(sp, CanvasSplitter):
                return
            own = sp.widget(1)
            if sp.mode == "tabs":
                # one of them with the whole height: the page hidden under the canvas tab (a
                # collapsed page would show its scroll bar), the canvas collapsed under the
                # page's
                own.setVisible(sp.tab == 1)
                sizes = [total, 0] if sp.tab == 0 else [0, total]
            else:
                # stacked: the page under the canvas gets the height it needs, the canvas the
                # rest (at least CANVAS_MIN_H: else the splitter is in tabs)
                ph = max(min(sp.page_height(), total - CANVAS_MIN_H), 0)
                sizes = [total - ph, ph]
            if sp.sizes() != sizes:
                self._fitting_split = True
                try:
                    sp.setSizes(sizes)
                finally:
                    self._fitting_split = False
            return
        own = sp.widget(1)
        total = sp.width() - sp.handleWidth()
        if total <= 0 or own is None:
            return
        low = own.minimumSizeHint().width()
        most = max(total - CANVAS_MIN, low)
        pw = min(max(own.sizeHint().width(), low), most)
        areas = [own] if isinstance(own, FitArea) else own.findChildren(FitArea)
        area = next((a for a in areas if a.isVisibleTo(own)), None)
        if area is not None:
            avail = sp.height() - (own.height() - area.viewport().height())
            chrome_w = own.width() - area.viewport().width()
            # the size hint while the canvas keeps its comfortable width; narrower than the
            # hint only as far as the page still fits its height there (a larger font: the
            # hint grew with the text and left the canvas at its bare minimum)
            # (the page's own minimum width: the area's is capped by the screen)
            comfort = max(total - CANVAS_COMFORT, low, area.need(0).width() + chrome_w)
            if self._canvas_shown() and comfort < pw:
                fit = comfort
                while fit < pw and area.need(max(fit - chrome_w, 1)).height() > avail:
                    fit += 16
                pw = min(fit, pw)
            # a page whose minimum height (sections folded at its width, wrapped rows) does not
            # fit the height it has gets every pixel the canvas can spare: wider, it folds into
            # fewer rows (its own minimum follows its width once it is laid out)
            if self._wide_need is not None and avail >= self._wide_need:
                self._wide_need = None        # the height its narrow minimum needs is back
            if self._wide_need is None and own.width() < most:
                need = area.need(own.width() - chrome_w).height()
                if need > avail:
                    self._wide_need = need    # remembered: back to the hint once this fits
            if self._wide_need is not None:
                pw = most
        sizes = [total - pw, pw]
        if sp.sizes() != sizes:
            self._fitting_split = True
            try:
                sp.setSizes(sizes)
            finally:
                self._fitting_split = False

    def fit_toolbar(self) -> None:
        """Labels beside the icons when they fit, icons only (labels in the tooltips and the
        accessible names) when the page is too narrow, so nothing hides behind the overflow."""
        tb = self.toolbar
        if not tb.actions():
            if self._toolbar_need:
                self._toolbar_need = 0
                self.updateGeometry()
            return
        def apply(style) -> None:
            tb.setToolButtonStyle(style)
            for act in tb.actions():         # a command without an icon keeps its text
                btn = tb.widgetForAction(act)
                if isinstance(btn, QToolButton):
                    # each button too: a label given to it alone (below) stays otherwise
                    btn.setToolButtonStyle(Qt.ToolButtonTextOnly if act.icon().isNull() else style)
            if tb.layout() is not None:
                tb.layout().invalidate()     # its size hint is cached until the next layout pass
        # icons only in two rows is the narrowest the tool bar gets: the page never goes
        # below that width (the window's minimum follows), so no action hides behind the
        # extension button; one row (icons only, or with their labels) while it fits
        tb2 = self.toolbar2
        apply(Qt.ToolButtonIconOnly)
        tb2.setToolButtonStyle(Qt.ToolButtonIconOnly)
        acts = self._acts
        sp = tb.layout().spacing() if tb.layout() is not None else 0

        def width(act) -> int:
            for t in (tb, tb2):
                b = t.widgetForAction(act)
                if b is not None:
                    return b.sizeHint().width()
            if act.isSeparator():            # left out at the row break
                return tb.style().pixelMetric(QStyle.PM_ToolBarSeparatorExtent, None, tb)
            return 0
        ws = [width(a) for a in acts]
        on1 = [i for i, a in enumerate(acts) if tb.widgetForAction(a) is not None]
        chrome = max(tb.sizeHint().width() - sum(ws[i] for i in on1) - sp * max(len(on1) - 1, 0), 0)
        one = sum(ws) + sp * max(len(ws) - 1, 0) + chrome
        need = one if tb.isVisibleTo(self) else 0
        split = len(acts)
        if tb.isVisibleTo(self) and len(acts) > 2:
            total = sum(ws)
            # the row break at the separator nearest the middle (else the middle action)
            mid = min(range(1, len(ws)), key=lambda i: abs(sum(ws[:i]) - total / 2))
            seps = [i for i, a in enumerate(acts) if a.isSeparator() and 0 < i < len(ws)]
            k = min(seps, key=lambda i: abs(sum(ws[:i]) - total / 2)) if seps else mid
            need = max(sum(ws[:k]) + sp * max(k - 1, 0), sum(ws[k:]) + sp * max(len(ws) - k - 1, 0)) + chrome
            split = k if self.width() < one else len(acts)
            # the estimate decides only whether to try one row: one row is kept only when the
            # tool bar's own size hint (the style's spacing and margins: macOS has its own)
            # says it fits, else it goes back to two rows; never the extension button
            self._place(split)
            if split == len(acts):
                apply(Qt.ToolButtonIconOnly)
                if tb.sizeHint().width() > max(self.width(), 1):
                    split = k
                    self._place(split)
        else:
            self._place(split)
        if split == len(acts):
            apply(Qt.ToolButtonTextBesideIcon)
            if tb.sizeHint().width() > max(self.width(), 1):
                # not every label fits: the most-used commands keep theirs, in the order of
                # the workspace's priority list (its tool bar order by default), the rest
                # icons only (O6: a wide window showed no label at all)
                apply(Qt.ToolButtonIconOnly)
                for act in self._label_order():
                    btn = tb.widgetForAction(act)
                    if not isinstance(btn, QToolButton) or act.icon().isNull():
                        continue
                    btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
                    if tb.layout() is not None:
                        tb.layout().invalidate()
                    if tb.sizeHint().width() > max(self.width(), 1):
                        btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
                        if tb.layout() is not None:
                            tb.layout().invalidate()
                        break
        else:
            apply(Qt.ToolButtonIconOnly)
            tb2.setToolButtonStyle(Qt.ToolButtonIconOnly)
            if tb2.layout() is not None:
                tb2.layout().invalidate()
            # the two rows as the style lays them out (the estimate only chose the break)
            need = max(need, tb.sizeHint().width(), tb2.sizeHint().width())
        if tb.layout() is not None:
            tb.layout().invalidate()
        if need != self._toolbar_need:
            self._toolbar_need = need
            self.updateGeometry()
        if not self._overflow_check:
            self._overflow_check = True
            QTimer.singleShot(0, self, self._no_overflow)

    _overflow_check = False

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        if ev.type() == QEvent.Show and obj.objectName() == "qt_toolbar_ext_button" \
                and obj.parent() is self.toolbar and not self._overflow_check:
            self._overflow_check = True
            QTimer.singleShot(0, self, self._no_overflow)
        return False

    def _no_overflow(self) -> None:
        """Once the tool bar is laid out: if its extension button shows after all (a style lays
        it out wider than its size hint said: macOS), a label goes, then the second row is
        used, until every command is in a row.  Bounded by the number of commands."""
        self._overflow_check = False
        tb = self.toolbar
        for _ in range(len(self._acts) + 1):
            ext = tb.findChild(QToolButton, "qt_toolbar_ext_button")
            if ext is None or not ext.isVisible():
                return
            labelled = [tb.widgetForAction(a) for a in reversed(self._label_order())
                        if isinstance(tb.widgetForAction(a), QToolButton) and not a.icon().isNull()
                        and tb.widgetForAction(a).toolButtonStyle() != Qt.ToolButtonIconOnly]
            if labelled:
                labelled[0].setToolButtonStyle(Qt.ToolButtonIconOnly)
            elif self._split == len(self._acts) and len(self._acts) > 2:
                ws = [tb.widgetForAction(a).sizeHint().width() if tb.widgetForAction(a) is not None else 0
                      for a in self._acts]
                total = sum(ws)
                mid = min(range(1, len(ws)), key=lambda i: abs(sum(ws[:i]) - total / 2))
                self._place(mid)
                self._toolbar_need = max(self._toolbar_need, tb.sizeHint().width(), self.toolbar2.sizeHint().width())
                self.updateGeometry()
            else:
                return
            if tb.layout() is not None:
                tb.layout().invalidate()
                tb.layout().activate()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return super().minimumSizeHint().expandedTo(QSize(self._toolbar_need, 0))

    def show_canvas(self, canvas: QWidget) -> None:
        if self.canvas_slot is None:
            return
        lay = self.canvas_slot.layout()
        if canvas.parent() is not self.canvas_slot:
            canvas.setParent(self.canvas_slot)
            lay.addWidget(canvas)
        canvas.show()


class PageStack(CurrentPageStack):
    """The workspace page stack (its size is the current page's)."""


class _RailFit(QObject):
    """The rail's labels go (icons only, the names in the tooltips and accessible names) when
    the window is too short for them, and come back when it grows; its minimum height is what
    the icon-only buttons need, so no workspace ever hides in the overflow menu."""

    def __init__(self, rail: QToolBar):
        super().__init__(rail)
        self.rail = rail
        self._busy = False
        rail.installEventFilter(self)

    def _need(self, style) -> int:
        old = self.rail.toolButtonStyle()
        if old != style:
            self.rail.setToolButtonStyle(style)
        lay = self.rail.layout()
        sp = lay.spacing() if lay is not None else 0
        m = lay.contentsMargins() if lay is not None else None
        h = sum(self.rail.widgetForAction(a).sizeHint().height() + sp for a in self.rail.actions()
                if a.isVisible() and self.rail.widgetForAction(a) is not None)
        if m is not None:
            h += m.top() + m.bottom()
        h += 2 * self.rail.style().pixelMetric(QStyle.PM_ToolBarFrameWidth, None, self.rail)
        if old != style:
            self.rail.setToolButtonStyle(old)
        return h

    def fit(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            full = self._need(Qt.ToolButtonTextUnderIcon)
            compact = self._need(Qt.ToolButtonIconOnly)
            want = Qt.ToolButtonTextUnderIcon if self.rail.height() >= full else Qt.ToolButtonIconOnly
            if self.rail.toolButtonStyle() != want:
                self.rail.setToolButtonStyle(want)
            if self.rail.minimumHeight() != compact:
                self.rail.setMinimumHeight(compact)
            self.rail.setProperty("responsive_state", "labels" if want == Qt.ToolButtonTextUnderIcon else "icons")
        finally:
            self._busy = False

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        if obj is self.rail and ev.type() in (QEvent.Resize, QEvent.Show, QEvent.StyleChange, QEvent.FontChange,
                                              QEvent.ActionAdded, QEvent.ActionRemoved, QEvent.ActionChanged):
            QTimer.singleShot(0, self.fit)
        return False


class _UserSize(QObject):
    """The window size the user chose (or the application placed): a resize that only grew the
    window to its minimum (a page that needs more room) is not one, so a workspace switch can
    go back to the user's size when the next page fits it (W14: a half-screen window stays in
    its half)."""

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self.w = window
        self.size: Optional[QSize] = None
        self.switching = False
        window.installEventFilter(self)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        if obj is self.w and ev.type() == QEvent.Resize and not self.switching:
            old, new = ev.oldSize(), ev.size()
            m = self.w.minimumSize()
            forced = old.isValid() and new == old.expandedTo(m) and new != old
            if not forced and not self._restoring:
                self.size = QSize(new)
        elif obj is self.w and ev.type() == QEvent.LayoutRequest and not self._queued:
            self._queued = True
            QTimer.singleShot(0, self, self._restore)
        return False

    _queued = False
    _restoring = False

    def _restore(self) -> None:
        """Back to the user's size once the content's minimum allows it again (a page that
        needed more room grew the window; another page or a narrower state needs less)."""
        self._queued = False
        w = self.w
        if (self.size is None or self.switching or not w.isVisible()
                or w.windowState() & (Qt.WindowMaximized | Qt.WindowFullScreen)):
            return
        want = self.size.expandedTo(w.minimumSize())
        cur = w.size()
        if want != cur and want.width() <= cur.width() and want.height() <= cur.height():
            self._restoring = True
            try:
                w.resize(want)
            finally:
                self._restoring = False


class WorkspaceHost:
    """Pages, rail and switching.  Created by the main window before the plugins load (so
    ``ctx.add_workspace`` works from ``register``)."""

    def __init__(self, window, tr, actions: Callable[[str], Optional[QAction]],
                 allowed: Callable[[str], bool], mode: Callable[[], str]):
        self.w = window
        self.user_size = _UserSize(window)
        self.tr = tr
        self._action = actions
        self._allowed = allowed
        self._mode = mode
        self.stack = PageStack()
        self.stack.setObjectName("shell_stack")
        self.pages: dict[str, WorkspacePage] = {}
        self.specs: dict[str, WorkspaceSpec] = {}
        self.current: str = ""
        self.aliases: dict[str, str] = {}        # contributed id ("run.run") -> its slot ("run")
        self.listeners: list[Callable[[str], None]] = []
        self.rail = QToolBar()
        self.rail.setObjectName("shell_rail")
        self.rail.setOrientation(Qt.Vertical)
        self.rail.setMovable(False)
        self.rail.setFloatable(False)
        self.rail.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.rail.setIconSize(QSize(26, 26))
        self.rail.toggleViewAction().setVisible(False)
        self._rail_fit = _RailFit(self.rail)
        tr.bind(self.rail, "shell.rail", self.rail.setWindowTitle)
        tr.bind(self.rail, "shell.rail", self.rail.setAccessibleName)
        self._group = QActionGroup(self.rail)
        self._group.setExclusive(True)
        self.rail_actions: dict[str, QAction] = {}
        self._managed_docks: set[str] = set()
        self._suppressed: set[str] = set()      # docks hidden by an exclusive workspace
        self._watched: set[int] = set()         # docks whose visibility is watched
        for slot in SLOTS:
            b = BUILTIN[slot]
            self._add_page(WorkspaceSpec(**{**b.__dict__, "extra": dict(b.extra)}))

    # ------------------------------------------------------------------ helpers
    def action(self, cmd: str) -> Optional[QAction]:
        return self._action(cmd)

    def allowed(self, cmd: str) -> bool:
        return self._allowed(cmd)

    def canvas_widget(self):
        return getattr(self.w, "canvas", None)

    # ------------------------------------------------------------------ registration
    def _add_page(self, spec: WorkspaceSpec) -> WorkspacePage:
        page = WorkspacePage(self, spec)
        self.pages[spec.id] = page
        self.specs[spec.id] = spec
        self.stack.addWidget(page)
        if spec.docks:
            self._managed_docks.update(spec.docks)
        act = QAction(self.rail)
        act.setCheckable(True)
        act.setData(spec.id)
        act.setObjectName(f"rail_{spec.id}")
        ic = icon(spec.icon) if spec.icon else command_icon(f"workspace.{spec.id}")
        if not ic.isNull():
            act.setIcon(ic)

        def label(t, a=act, s=spec):
            a.setText(t)
            a.setIconText(t)
            key = f" ({s.shortcut})" if s.shortcut else ""
            desc = self.tr.tr(s.description) if s.description else ""
            a.setToolTip(f"{t}{key}" + (f"\n{desc}" if desc else ""))
        self.tr.bind(act, spec.title, label)
        act.triggered.connect(lambda _=False, i=spec.id: self.activate(i))
        self._group.addAction(act)
        self.rail_actions[spec.id] = act
        self._rebuild_rail()
        return page

    def add(self, spec: WorkspaceSpec) -> WorkspacePage:
        """Register a contributed workspace (slot rule, then a page of its own)."""
        slot = slot_for(spec.id, self.tr.tr(spec.title) if spec.title else "", spec.slot)
        if slot:
            page = self.pages[slot]
            self.aliases[spec.id] = slot
            page.contribute(spec)
            if spec.docks:
                self._managed_docks.update(spec.docks)
            self._rebuild_rail()
            if self.current == slot:
                self.activate(slot, force=True)
            return page
        if spec.id in self.specs:
            raise KeyError(f"workspace {spec.id!r} already exists")
        if not spec.shortcut:
            used = {s.shortcut for s in self.specs.values()}
            for n in range(7, 10):
                if f"Ctrl+{n}" not in used:
                    spec.shortcut = f"Ctrl+{n}"
                    break
        return self._add_page(spec)

    def _rebuild_rail(self) -> None:
        self.rail.clear()
        mode = self._mode()
        for spec in sorted(self.specs.values(), key=lambda s: (s.order, s.id)):
            act = self.rail_actions[spec.id]
            visible = um.rank(mode) >= um.rank(spec.min_mode)
            act.setVisible(visible)
            if visible:
                self.rail.addAction(act)
                btn = self.rail.widgetForAction(act)
                if btn is not None:
                    btn.setAccessibleName(act.text())
                    btn.setFocusPolicy(Qt.TabFocus)
                    btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        if self.current:
            self.rail_actions[self.current].setChecked(True)

    def visible_ids(self) -> list[str]:
        mode = self._mode()
        return [s.id for s in sorted(self.specs.values(), key=lambda s: (s.order, s.id))
                if um.rank(mode) >= um.rank(s.min_mode)]

    # ------------------------------------------------------------------ switching
    def activate(self, ws_id: str, force: bool = False) -> bool:
        """Show workspace ``ws_id`` (a slot, a plugin workspace or the id of a contribution to a
        slot such as ``"run.run"``; False when it is unknown or hidden in this user mode)."""
        ws_id = self.resolve(ws_id)
        spec = self.specs.get(ws_id)
        if spec is None or ws_id not in self.visible_ids():
            return False
        if ws_id == self.current and not force:
            self.rail_actions[ws_id].setChecked(True)
            return True
        prev = self.specs.get(self.current)
        page = self.pages[ws_id]
        win = self.w
        before = None
        if win.isVisible() and not (win.windowState() & (Qt.WindowMaximized | Qt.WindowFullScreen)):
            # the user's size (not one an earlier page's minimum grew the window to)
            before = QSize(self.user_size.size) if self.user_size.size is not None else win.size()
        self.user_size.switching = True
        canvas = self.canvas_widget()
        if spec.canvas and canvas is not None:
            page.show_canvas(canvas)
        # docks the new workspace does not want go first, its own come after the page switch:
        # the window's minimum never adds the old docks to the new page (or the reverse), which
        # used to grow the window on a switch (docs/dev/RESPONSIVE.md)
        self._apply_docks(prev, spec, show=False)
        self.stack.setCurrentWidget(page)
        self.current = ws_id
        self.rail_actions[ws_id].setChecked(True)
        self._apply_docks(prev, spec, hide=False)
        pol = getattr(getattr(win, "shell", None), "dock_policy", None)
        if before is not None and pol is not None:
            # the new workspace's docks fold now if the window is too narrow for them side by
            # side: its minimum must not widen the window first (a switch reflows, W14)
            pol.fit_width(before.width(), before.height())
        if before is not None:
            # the new page's minimum is first asked for before its wrapper knows the room around
            # it (its screen cap: FitArea._cap), and the window grows to that; once the page has
            # its real minimum the window goes back to the size the user had, if that fits.
            # Settled here, synchronously (the pending layout requests first): no timer, so the
            # result does not depend on how soon a loaded machine runs it
            for _ in range(4):
                QCoreApplication.sendPostedEvents(None, QEvent.LayoutRequest)
            self._keep_size(before)
        self.user_size.switching = False
        for fn in list(self.listeners):
            fn(ws_id)
        return True

    def _keep_size(self, before: QSize) -> None:
        win = self.w
        if not win.isVisible() or win.windowState() & (Qt.WindowMaximized | Qt.WindowFullScreen):
            return
        size = win.size()
        if size != before:
            # back to the user's size as far as this page's minimum allows (the minimum Qt
            # keeps: the dock policy's explicit one while the docks can fold)
            want = before.expandedTo(win.minimumSize())
            if want != size:
                win.resize(want)

    def resolve(self, ws_id: str) -> str:
        return self.aliases.get(ws_id, ws_id)

    def exclusive(self, spec: Optional[WorkspaceSpec] = None) -> bool:
        """Is ``spec`` (default: the active workspace) an exclusive workspace?"""
        spec = spec if spec is not None else self.specs.get(self.current)
        return spec is not None and (spec.id in EXCLUSIVE or bool(spec.extra.get("exclusive_docks")))

    def _apply_docks(self, prev: Optional[WorkspaceSpec], spec: WorkspaceSpec, show: bool = True,
                     hide: bool = True) -> None:
        """Docks named by the new workspace are shown; docks named by any other workspace (the
        "owned" docks) are hidden; every other dock stays as the user or a plugin left it,
        except in an exclusive workspace, which hides every other docked panel (restored when
        the operator leaves it)."""
        if hide:
            self.watch_docks()
        if self.exclusive(spec):
            want = set(spec.docks or ())
            for d in self.docks():
                if hide and d.objectName() not in want and not d.isHidden() and not d.isFloating():
                    self._suppressed.add(d.objectName())
                    d.hide()
        elif self._suppressed and show:
            owned = set(self._managed_docks)
            for s in self.specs.values():
                owned.update(s.docks or ())
            back, self._suppressed = self._suppressed, set()
            for d in self.docks():
                if d.objectName() in back and d.objectName() not in owned and d.isHidden():
                    d.show()
        if spec.docks is None:
            return
        want = set(spec.docks)
        owned = set(self._managed_docks)
        for s in self.specs.values():
            owned.update(s.docks or ())
        for d in self.docks():
            name = d.objectName()
            if name in want:
                if show and d.isHidden() and not d.isFloating():
                    d.show()
            elif hide and name in owned and not d.isHidden() and not d.isFloating():
                d.hide()

    def docks(self) -> list:
        from PySide6.QtWidgets import QDockWidget
        return [d for d in self.w.findChildren(QDockWidget) if d.parent() is self.w]

    def watch_docks(self) -> None:
        """Watch every dock's visibility (new ones too: called on each switch and by the shell's
        poll): one that appears docked while an exclusive workspace is active is hidden again."""
        for d in self.docks():
            if id(d) in self._watched:
                continue
            self._watched.add(id(d))
            d.visibilityChanged.connect(lambda vis, d=d: self._dock_shown(d) if vis else None)
            d.topLevelChanged.connect(lambda floating, d=d: self._dock_shown(d) if not floating else None)
            d.destroyed.connect(lambda *_a, k=id(d): self._watched.discard(k))
            if d.isVisible():
                self._dock_shown(d)

    def _dock_shown(self, d) -> None:
        spec = self.specs.get(self.current)
        if spec is None or not self.exclusive(spec) or d.objectName() in (spec.docks or ()):
            return
        if d.isFloating():
            return
        self._suppressed.add(d.objectName())
        QTimer.singleShot(0, lambda d=d: self._hide_if_still(d))

    def _hide_if_still(self, d) -> None:
        """Hide ``d`` and every other docked panel that is visible on the exclusive workspace:
        hiding one tab of a tabified group raises its sibling (macOS delivers that sibling's
        visibility change before it is visible), so sweep until none is left."""
        spec = self.specs.get(self.current)
        if spec is None or not self.exclusive(spec):
            return
        keep = set(spec.docks or ())
        for _ in range(8):
            shown = []
            for x in [d, *self.docks()]:
                try:
                    if x.isVisible() and not x.isFloating() and x.objectName() not in keep:
                        shown.append(x)
                except RuntimeError:     # the dock was deleted meanwhile
                    continue
            if not shown:
                return
            for x in shown:
                self._suppressed.add(x.objectName())
                x.hide()

    def refresh_mode(self) -> None:
        """The user mode changed: rail, toolbars and placeholders follow."""
        self._rebuild_rail()
        for page in self.pages.values():
            page.fill_toolbar()
            if page.placeholder is not None:
                page.placeholder.rebuild()
        vis = self.visible_ids()
        if self.current not in vis and vis:
            self.activate("run" if "run" in vis else vis[0])

    def refresh_actions(self) -> None:
        for page in self.pages.values():
            page.fill_toolbar()
            if page.placeholder is not None:
                page.placeholder.rebuild()


__all__ = ["WorkspaceHost", "WorkspacePage", "EmptyState"]
