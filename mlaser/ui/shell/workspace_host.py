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

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
                               QSplitter, QStyle, QTabWidget, QToolBar, QToolButton,
                               QVBoxLayout, QWidget)

from mlaser.services import usermodes as um
from mlaser.services.workspaces import BUILTIN, SLOTS, WorkspaceSpec, slot_for

from .. import style
from ..icons import command_icon, icon
from ..responsive import CurrentPageStack, FitArea
from .dock_policy import CANVAS_MIN


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
        self.toolbar = QToolBar()
        self.toolbar.setObjectName("shell_ws_toolbar")
        self.toolbar.setIconSize(QSize(20, 20))
        self.toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        v.addWidget(self.toolbar)
        self.body = QWidget()
        self.body_lay = QHBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 0, 0, 0)
        self.body_lay.setSpacing(0)
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
            self.splitter = QSplitter(Qt.Horizontal)
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
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
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
            btn = tb.widgetForAction(act)
            if btn is not None:
                btn.setAccessibleName(act.text().replace("&", ""))
                btn.setFocusPolicy(Qt.TabFocus)
                if act.icon().isNull():
                    btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
            last_sep = False
        tb.setVisible(bool(tb.actions()))
        self.fit_toolbar()

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(ev)
        self.fit_toolbar()

    def fit_toolbar(self) -> None:
        """Labels beside the icons when they fit, icons only (labels in the tooltips and the
        accessible names) when the page is too narrow, so nothing hides behind the overflow."""
        tb = self.toolbar
        if not tb.actions():
            return
        def apply(style) -> None:
            tb.setToolButtonStyle(style)
            for act in tb.actions():         # a command without an icon keeps its text
                btn = tb.widgetForAction(act)
                if isinstance(btn, QToolButton) and act.icon().isNull():
                    btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        apply(Qt.ToolButtonTextBesideIcon)
        if tb.sizeHint().width() > max(self.width(), 1):
            apply(Qt.ToolButtonIconOnly)
        if tb.layout() is not None:
            tb.layout().invalidate()

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


class WorkspaceHost:
    """Pages, rail and switching.  Created by the main window before the plugins load (so
    ``ctx.add_workspace`` works from ``register``)."""

    def __init__(self, window, tr, actions: Callable[[str], Optional[QAction]],
                 allowed: Callable[[str], bool], mode: Callable[[], str]):
        self.w = window
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
        self.rail = QToolBar("Workspaces")
        self.rail.setObjectName("shell_rail")
        self.rail.setOrientation(Qt.Vertical)
        self.rail.setMovable(False)
        self.rail.setFloatable(False)
        self.rail.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.rail.setIconSize(QSize(26, 26))
        self.rail.toggleViewAction().setVisible(False)
        self._rail_fit = _RailFit(self.rail)
        tr.bind(self.rail, "shell.rail", self.rail.setWindowTitle)
        self.rail.setAccessibleName("Workspaces")
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
        for fn in list(self.listeners):
            fn(ws_id)
        return True

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
