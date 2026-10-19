"""A light-weight ribbon (tabbed toolbar) replacing BCGControlBar's CBCGPRibbonBar.

Built from :mod:`mlaser.ui.ribbon_spec`, with SVG icons from :mod:`mlaser.ui.icons`. Every
button triggers a ``QAction`` registered in ``Ribbon.actions[cmd]`` so the main window can
connect handlers by command id.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QScrollArea,
                               QSizePolicy, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from . import ribbon_spec as spec
from .i18n import Translator
from .icons import command_icon


def wrap_label(text: str, limit: int = 9) -> str:
    """A large ribbon button's label on two lines (at the space nearest the middle), as the
    original BCG ribbon does, instead of eliding it ("Mi…nt" for "Micro Joint")."""
    t = text.replace("\n", " ").strip()
    if len(t.replace("&", "")) <= limit or " " not in t:
        return t
    mid = len(t) / 2
    spaces = [i for i, c in enumerate(t) if c == " "]
    cut = min(spaces, key=lambda i: abs(i - mid))
    return t[:cut] + "\n" + t[cut + 1:]


class RibbonButton(QToolButton):
    """A ribbon tool button that never shrinks below its label (fixed horizontal size policy:
    a tab that does not fit scrolls instead of eliding) and, when ``wrap`` is set, shows a
    long label on two lines.  The tooltip always carries the full label."""

    def __init__(self, wrap: bool = False, parent=None):
        super().__init__(parent)
        self.wrap = wrap
        self.min_width = 0          # large buttons: at least 52 px (an explicit minimum size
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)   # would override the hint)

    def sizeHint(self):  # noqa: N802 - Qt API
        s = super().sizeHint()
        if s.width() < self.min_width:
            s.setWidth(self.min_width)
        return s

    def setDefaultAction(self, act) -> None:  # noqa: N802 - Qt API
        super().setDefaultAction(act)
        self._rewrap()

    def actionEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().actionEvent(ev)
        if ev.type() == QEvent.ActionChanged and ev.action() is self.defaultAction():
            self._rewrap()

    def _rewrap(self) -> None:
        act = self.defaultAction()
        if act is None:
            return
        full = act.text()
        if self.wrap:
            self.setText(wrap_label(full))
        self.setAccessibleName(full.replace("&", ""))



class Ribbon(QTabWidget):
    """Tabs -> groups (framed, caption underneath) -> tool buttons."""

    def __init__(self, tr: Translator, parent=None):
        super().__init__(parent)
        self.tr_ = tr
        self.actions: dict[str, QAction] = {}
        self.on_new_action = None      # MainWindow connects every new action to its registry
        self.setDocumentMode(True)
        self.setObjectName("Ribbon")
        self.setUsesScrollButtons(True)
        self._fit_height()
        self._file_menu = QMenu(self)
        file_btn = QToolButton(self)
        file_btn.setPopupMode(QToolButton.InstantPopup)
        file_btn.setMenu(self._file_menu)
        tr.bind(file_btn, "mf7", file_btn.setText)
        self.setCornerWidget(file_btn, Qt.TopLeftCorner)
        for cmd, key in spec.FILE_MENU:
            if cmd == "-":
                self._file_menu.addSeparator()
                continue
            self._file_menu.addAction(self.action(cmd, key))
        for tab in spec.TABS:
            self._add_tab(tab)
        self._fit_height()

    # ------------------------------------------------------------------
    def _fit_height(self) -> None:
        """Height from the font (two label lines + icon + caption + a scroll bar), so a larger
        font or 150 % scaling never clips the labels (the original's fixed 118 px did)."""
        from PySide6.QtWidgets import QStyle
        fm = self.fontMetrics()
        line = fm.height()
        bar = self.style().pixelMetric(QStyle.PM_ScrollBarExtent)   # a tab wider than the window
        self.setFixedHeight(32 + 3 * line + 2 * 8 + self.tabBar().sizeHint().height() + bar + 4)

    def changeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().changeEvent(ev)
        if ev.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._fit_height()

    def action(self, cmd: str, key: str, icon: QIcon | None = None, checkable: bool = False) -> QAction:
        act = self.actions.get(cmd)
        if act is None:
            act = QAction(self)
            act.setObjectName(cmd)
            act.setData(cmd)
            act.setProperty("lang_key", key)
            self.tr_.bind(act, key, act.setText)
            self.tr_.bind(act, key, act.setToolTip)
            self.actions[cmd] = act
            if self.on_new_action is not None:
                self.on_new_action(act)
        if act.icon().isNull():
            icon = icon if icon is not None and not icon.isNull() else command_icon(cmd)
            if not icon.isNull():
                act.setIcon(icon)
        if checkable:
            act.setCheckable(True)
        return act

    def add_command_tab(self, title: str, commands: list[str]) -> QWidget:
        """A plugin tab (``PluginContext.add_ribbon_tab``): one large button per existing action."""
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(2, 2, 2, 0)
        box = QFrame()
        box.setFrameShape(QFrame.StyledPanel)
        row = QHBoxLayout(box)
        for cmd in commands:
            act = self.actions.get(cmd)
            if act is None:
                continue
            tb = RibbonButton(wrap=True)
            tb.setDefaultAction(act)
            tb.setAutoRaise(True)
            if act.icon().isNull():
                tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
            else:
                tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
                tb.setIconSize(QSize(32, 32))
            row.addWidget(tb)
        lay.addWidget(box)
        lay.addStretch(1)
        self.addTab(self._scroll(page), title)
        return page

    @staticmethod
    def _scroll(page: QWidget) -> QScrollArea:
        """A tab page that is wider than the window scrolls sideways (no elided labels)."""
        sa = QScrollArea()
        sa.setFrameShape(QFrame.NoFrame)
        sa.setWidgetResizable(True)
        sa.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        sa.setWidget(page)
        return sa

    def _add_tab(self, tab: spec.Tab) -> None:
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(2, 2, 2, 0)
        lay.setSpacing(2)
        for grp in tab.groups:
            lay.addWidget(self._group_widget(grp))
        lay.addStretch(1)
        idx = self.addTab(self._scroll(page), tab.key)
        self.tr_.bind(page, tab.key, lambda t, i=idx: self.setTabText(i, t))

    def _group_widget(self, grp: spec.Group) -> QWidget:
        box = QFrame()
        box.setFrameShape(QFrame.StyledPanel)
        v = QVBoxLayout(box)
        v.setContentsMargins(2, 2, 2, 0)
        v.setSpacing(0)
        row = QHBoxLayout()
        row.setSpacing(1)
        small: list[QToolButton] = []
        for b in grp.buttons:
            icon = command_icon(b.cmd, b.icon)
            act = self.action(b.cmd, b.key, icon, b.checkable)
            text_only = act.icon().isNull()
            tb = RibbonButton(wrap=b.large)
            tb.setDefaultAction(act)
            tb.setAutoRaise(True)
            if b.menu:
                m = QMenu(tb)
                for cmd, key in b.menu:
                    m.addAction(self.action(cmd, key))
                tb.setMenu(m)
                tb.setPopupMode(QToolButton.MenuButtonPopup)
            if text_only:
                tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
            if b.large:
                if not text_only:
                    tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
                tb.setIconSize(QSize(32, 32))
                tb.min_width = 52
                tb.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
                self._flush_small(row, small)
                row.addWidget(tb)
            else:
                if not text_only:
                    tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
                tb.setIconSize(QSize(16, 16))
                small.append(tb)
        self._flush_small(row, small)
        v.addLayout(row)
        cap = QLabel()
        cap.setAlignment(Qt.AlignCenter)
        cap.setObjectName("RibbonGroupCaption")
        self.tr_.bind(cap, grp.key, cap.setText)
        v.addWidget(cap)
        return box

    @staticmethod
    def _flush_small(row: QHBoxLayout, small: list) -> None:
        """Stack small buttons in columns of three like a BCG ribbon panel."""
        if not small:
            return
        grid = QGridLayout()
        grid.setSpacing(0)
        for i, tb in enumerate(small):
            grid.addWidget(tb, i % 3, i // 3)
        row.addLayout(grid)
        small.clear()
