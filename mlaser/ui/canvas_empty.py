"""The canvas's empty state (UX audit O1): while there is no drawing, the middle of the canvas
offers the ways to get one: *Open drawing…*, *Import DXF/SVG…*, *Try the sample* and *Practice
on the simulator*.

A framed panel centred on the canvas view (a child widget, not a scene item: it does not zoom
or pan), its buttons in the tab order, each running the window's command (the same action as
the menu and tool bar, so the user mode's limits apply: a command the mode does not offer is
not shown).  It hides as soon as the document has a part and comes back when the last one goes.

It never covers the canvas bar (the HUD: *Toolpath*, the legend, *Fit drawing*): it is centred
in the space below the bar.  In a short view it takes a smaller form rather than vanish: the
explanation goes, then the title, then the buttons stand two side by side, then the margins
narrow, and at last one row offers *Open drawing…* and *Try the sample* alone (Windows at a
window of 683 x 649: the four buttons in a column did not fit and the canvas was blank).  Only
where not even that row fits below the bar does the bar, which has nothing to act on while
there is no drawing, step aside until there is one or the view grows.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from . import style

#: (command, label key): the buttons, top to bottom; ``sample`` loads the sample drawing
ENTRIES: tuple[tuple[str, str], ...] = (
    ("file.open", "canvas.empty.open"),
    ("file.import", "canvas.empty.import"),
    ("sample", "canvas.empty.sample"),
    ("practice.open", "canvas.empty.practice"),
)

#: the commands of the smallest form (one row), in this order, as far as they are offered
ONE_ROW: tuple[str, ...] = ("file.open", "sample")

#: the forms, largest first: (explanation shown, title shown, button columns, margin, one row only)
FORMS: tuple[tuple[bool, bool, int, int, bool], ...] = (
    (True, True, 1, style.SP3, False),
    (False, True, 1, style.SP3, False),
    (False, False, 1, style.SP3, False),
    (False, False, 2, style.SP3, False),
    (False, False, 2, style.SP1, False),
    (False, False, 2, style.SP1, True),
)


class CanvasEmptyState(QFrame):
    """See the module docstring.  ``run(cmd)`` runs a command; ``allowed(cmd)`` says whether
    the current user mode offers it; ``open_sample()`` loads the sample drawing."""

    def __init__(self, view: QWidget, tr: Any, run: Callable[[str], Any],
                 open_sample: Callable[[], Any], allowed: Optional[Callable[[str], bool]] = None) -> None:
        super().__init__(view)
        self.view = view
        self.tr_ = tr
        self._run = run
        self._open_sample = open_sample
        self._allowed = allowed or (lambda _c: True)
        self.setObjectName("canvas_empty")
        self.setFrameShape(QFrame.StyledPanel)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.Window)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(style.SP3, style.SP3, style.SP3, style.SP3)
        lay.setSpacing(style.SP1)
        self.title = style.heading("", "title")
        tr.bind(self.title, "canvas.empty.title", self.title.setText)
        self.title.setObjectName("canvas_empty_title")
        self.title.setWordWrap(True)
        lay.addWidget(self.title)
        self.text = QLabel()
        self.text.setWordWrap(True)
        tr.bind(self.text, "canvas.empty.text", self.text.setText)
        lay.addWidget(self.text)
        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(style.SP1)
        lay.addLayout(self._grid)
        self._offered: list[str] = []
        self.buttons: dict[str, QPushButton] = {}
        for cmd, key in ENTRIES:
            b = QPushButton()
            tr.bind(b, key, b.setText)
            b.setObjectName(f"canvas_empty_{cmd.replace('.', '_')}")
            b.setMinimumHeight(style.TARGET)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setFocusPolicy(Qt.StrongFocus)
            # sized and placed as widgets, not as the style's smaller "layout item" rectangles:
            # the macOS style's buttons have layout item margins, and buttons placed one spacing
            # apart by their item rectangles overlapped by 8 px (native check, macOS style; as
            # responsive.FlowLayout.addWidget)
            b.setAttribute(Qt.WA_LayoutUsesWidgetRect, True)
            b.clicked.connect(lambda _c=False, c=cmd: self.trigger(c))
            self._grid.addWidget(b, len(self.buttons), 0)
            self.buttons[cmd] = b
        self.buttons["file.open"].setDefault(True)
        tr.bind(self, "canvas.empty.title", self.setAccessibleName)
        self._doc: Any = None
        self._hid_hud = False          # the canvas bar was hidden to make room (see place)
        view.installEventFilter(self)
        hud = self._hud()
        if hud is not None and hasattr(hud, "collapsedChanged"):
            hud.collapsedChanged.connect(lambda _on: self._view_resized())
        self.hide()

    def _hud(self) -> Optional[QWidget]:
        """The canvas's own bar (``CanvasView.hud``), if the view has one."""
        hud = getattr(self.view, "hud", None)
        return hud if isinstance(hud, QWidget) else None

    def _restore_hud(self) -> None:
        hud = self._hud()
        if not self._hid_hud or hud is None:
            return
        self._hid_hud = False
        hud.show()
        if hasattr(hud, "place"):
            hud.place()
        # a fit made while the bar was away did not keep its band free: fit again
        mode = getattr(self.view, "fit_mode", None)
        if mode == "drawing":
            self.view.zoom_fit()
        elif mode == "table":
            self.view.zoom_fit_table()

    # ------------------------------------------------------------------ state
    def trigger(self, cmd: str) -> None:
        if cmd == "sample":
            self._open_sample()
        else:
            self._run(cmd)

    def update_state(self, document: Any) -> None:
        """Shown while ``document`` has no part (and the view is shown)."""
        self._doc = document
        empty = document is None or not getattr(document, "parts", None)
        self._offered = [cmd for cmd in self.buttons
                         if cmd == "sample" and self._allowed("file.open") or self._allowed(cmd)]
        self._arrange(self._offered, 1)
        self.setVisible(empty)
        if empty:
            self.place()
            self.raise_()
        else:
            self._restore_hud()

    def _view_resized(self) -> None:
        empty = self._doc is None or not getattr(self._doc, "parts", None)
        if empty:
            self.show()
            self.place()

    def place(self) -> None:
        """Centred on the view below the canvas bar, as wide as its content wants (never wider
        than the view).  In a short view the explanation goes first, then the title; where not
        even its smallest form (one row of buttons) fits below the bar, the bar steps aside (it
        comes back with a drawing or a taller view); a view too short for that row shows none
        of it (nothing is ever drawn cut)."""
        v = self.view
        hud = self._hud()
        if self._hid_hud and hud is not None:
            hud.show()                   # the bar first: does it fit now?
            self._hid_hud = False
        if hud is not None and not hud.isHidden():
            if hasattr(hud, "place"):
                hud.place()              # (the view's resize reaches the bar after this filter)
            below = hud.geometry().bottom() + 1
            tops = [(below, False), (0, True)]
        else:
            tops = [(0, False)]
        for top, hide_bar in tops:
            size = self._fit(v.width(), v.height() - top)
            if size is None:
                continue
            if hide_bar and hud is not None:
                hud.hide()
                self._hid_hud = True
            w, h = size
            self.setGeometry((v.width() - w) // 2, top + max(style.SP1, (v.height() - top - h) // 2), w, h)
            # laid out now with the parts shown at last (a size the panel already had brings no
            # resize, and its buttons kept the places of a pass with the explanation shown)
            self.layout().activate()
            return
        self.hide()                      # not even the buttons fit: the menus and the tool bar remain

    def _arrange(self, cmds: list[str], cols: int) -> None:
        """The offered buttons ``cmds`` in ``cols`` columns (row by row, the tab order), the
        others hidden."""
        g = self._grid
        for b in self.buttons.values():
            g.removeWidget(b)
        for cmd, b in self.buttons.items():
            b.setVisible(cmd in cmds)
        for i, cmd in enumerate(cmds):
            g.addWidget(self.buttons[cmd], i // cols, i % cols)

    def _fit(self, width: int, height: int) -> Optional[tuple[int, int]]:
        """(w, h) of the panel in a space of ``width`` x ``height`` in the largest of
        :data:`FORMS` that fits, or None when not even the smallest fits."""
        avail_w = max(1, width - 2 * style.SP3)
        avail_h = height - 2 * style.SP1
        lay = self.layout()
        for text, title, cols, margin, one_row in FORMS:
            cmds = [c for c in ONE_ROW if c in self._offered] if one_row else list(self._offered)
            if not cmds:
                continue
            self.text.setVisible(text)
            self.title.setVisible(title)
            lay.setContentsMargins(margin, margin, margin, margin)
            self._arrange(cmds, len(cmds) if one_row else cols)
            lay.invalidate()
            if cols > 1 and lay.totalMinimumSize().width() > avail_w:
                continue                  # side by side the buttons would be cut
            w = min(max(self.sizeHint().width(), self.minimumSizeHint().width(), 280), avail_w)
            h = max(lay.totalHeightForWidth(w) if lay.hasHeightForWidth() else 0, lay.totalMinimumSize().height())
            if h <= avail_h:
                return w, h
        self.text.setVisible(True)
        self.title.setVisible(True)
        lay.setContentsMargins(style.SP3, style.SP3, style.SP3, style.SP3)
        self._arrange(self._offered, 1)
        return None

    def event(self, ev: QEvent) -> bool:
        # the content's size changed without a resize of the view (a font or language change,
        # a command offered or no longer offered): placed again when it no longer fits
        if ev.type() == QEvent.LayoutRequest and self.isVisible() and self._too_short():
            self.place()
        return super().event(ev)

    def _too_short(self) -> bool:
        lay = self.layout()
        need = lay.totalHeightForWidth(self.width()) if lay.hasHeightForWidth() else 0
        return max(need, lay.totalMinimumSize().height()) > self.height()

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: N802 - Qt API
        if obj is self.view:
            if ev.type() == QEvent.Show:
                self.update_state(self._doc)         # the commands plugins added meanwhile
            elif ev.type() == QEvent.Resize:
                self._view_resized()
        return super().eventFilter(obj, ev)


__all__ = ["CanvasEmptyState", "ENTRIES"]
