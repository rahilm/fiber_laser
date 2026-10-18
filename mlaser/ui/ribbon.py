"""A light-weight ribbon (tabbed toolbar) replacing BCGControlBar's CBCGPRibbonBar.

Built from :mod:`mlaser.ui.ribbon_spec`. Every button triggers a ``QAction`` registered in
``Ribbon.actions[cmd]`` so the main window can connect handlers by command id.
"""
from __future__ import annotations

import os
from functools import lru_cache

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMenu, QTabWidget, QToolButton,
                               QVBoxLayout, QWidget, QGridLayout)

from . import ribbon_spec as spec
from .i18n import Translator, RES_DIR

STRIP_DIR = os.path.join(RES_DIR, "icons", "strips")
BUTTON_DIR = os.path.join(RES_DIR, "icons", "buttons")


@lru_cache(maxsize=None)
def _strip(strip_id: int) -> QPixmap | None:
    path = os.path.join(STRIP_DIR, f"strip_{strip_id}.png")
    return QPixmap(path) if os.path.exists(path) else None


def strip_icon(strip_id: int, index: int, size: int = 32) -> QIcon:
    """Slice icon ``index`` (square, height-sized) from a PNG strip resource."""
    pm = _strip(strip_id)
    if pm is None or pm.isNull():
        return QIcon()
    h = pm.height()
    if (index + 1) * h > pm.width():
        return QIcon()
    icon = QIcon(pm.copy(index * h, 0, h, h))
    small_id = {348: 350, 351: 352, 353: 354, 355: 356, 441: 442, 451: 452}.get(strip_id)
    if small_id:
        sp = _strip(small_id)
        if sp is not None and not sp.isNull() and (index + 1) * sp.height() <= sp.width():
            icon.addPixmap(sp.copy(index * sp.height(), 0, sp.height(), sp.height()))
    return icon


def button_icon(bitmap_id: int) -> QIcon:
    path = os.path.join(BUTTON_DIR, f"btn_{bitmap_id}.png")
    return QIcon(path) if os.path.exists(path) else QIcon()


class Ribbon(QTabWidget):
    """Tabs -> groups (framed, caption underneath) -> tool buttons."""

    def __init__(self, tr: Translator, parent=None):
        super().__init__(parent)
        self.tr_ = tr
        self.actions: dict[str, QAction] = {}
        self.setDocumentMode(True)
        self.setObjectName("Ribbon")
        self.setFixedHeight(118)
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

    # ------------------------------------------------------------------
    def action(self, cmd: str, key: str, icon: QIcon | None = None, checkable: bool = False) -> QAction:
        act = self.actions.get(cmd)
        if act is None:
            act = QAction(self)
            act.setObjectName(cmd)
            act.setData(cmd)
            self.tr_.bind(act, key, act.setText)
            self.tr_.bind(act, key, act.setToolTip)
            self.actions[cmd] = act
        if icon is not None and act.icon().isNull():
            act.setIcon(icon)
        if checkable:
            act.setCheckable(True)
        return act

    def _add_tab(self, tab: spec.Tab) -> None:
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(2, 2, 2, 0)
        lay.setSpacing(2)
        for grp in tab.groups:
            lay.addWidget(self._group_widget(grp))
        lay.addStretch(1)
        idx = self.addTab(page, tab.key)
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
            icon = strip_icon(*b.icon) if b.icon else QIcon()
            act = self.action(b.cmd, b.key, icon, b.checkable)
            tb = QToolButton()
            tb.setDefaultAction(act)
            tb.setAutoRaise(True)
            if b.menu:
                m = QMenu(tb)
                for cmd, key in b.menu:
                    m.addAction(self.action(cmd, key))
                tb.setMenu(m)
                tb.setPopupMode(QToolButton.MenuButtonPopup)
            if b.large:
                tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
                tb.setIconSize(QSize(32, 32))
                tb.setMinimumWidth(52)
                self._flush_small(row, small)
                row.addWidget(tb)
            else:
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
