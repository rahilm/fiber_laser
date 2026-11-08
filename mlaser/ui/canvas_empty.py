"""The canvas's empty state (UX audit O1): while there is no drawing, the middle of the canvas
offers the ways to get one: *Open drawing…*, *Import DXF/SVG…*, *Try the sample* and *Practice
on the simulator*.

A framed panel centred on the canvas view (a child widget, not a scene item: it does not zoom
or pan), its buttons in the tab order, each running the window's command (the same action as
the menu and tool bar, so the user mode's limits apply: a command the mode does not offer is
not shown).  It hides as soon as the document has a part and comes back when the last one goes.
The canvas HUD (legend, *Fit drawing*) stays where it is.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from . import style

#: (command, label key): the buttons, top to bottom; ``sample`` loads the sample drawing
ENTRIES: tuple[tuple[str, str], ...] = (
    ("file.open", "canvas.empty.open"),
    ("file.import", "canvas.empty.import"),
    ("sample", "canvas.empty.sample"),
    ("practice.open", "canvas.empty.practice"),
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
        self.buttons: dict[str, QPushButton] = {}
        for cmd, key in ENTRIES:
            b = QPushButton()
            tr.bind(b, key, b.setText)
            b.setObjectName(f"canvas_empty_{cmd.replace('.', '_')}")
            b.setMinimumHeight(style.TARGET)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setFocusPolicy(Qt.StrongFocus)
            b.clicked.connect(lambda _c=False, c=cmd: self.trigger(c))
            lay.addWidget(b)
            self.buttons[cmd] = b
        self.buttons["file.open"].setDefault(True)
        tr.bind(self, "canvas.empty.title", self.setAccessibleName)
        self._doc: Any = None
        view.installEventFilter(self)
        self.hide()

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
        for cmd, b in self.buttons.items():
            b.setVisible(cmd == "sample" and self._allowed("file.open") or self._allowed(cmd))
        self.setVisible(empty)
        if empty:
            self.place()
            self.raise_()

    def _view_resized(self) -> None:
        empty = self._doc is None or not getattr(self._doc, "parts", None)
        if empty:
            self.show()
            self.place()

    def place(self) -> None:
        """Centred on the view, as wide as its content wants (never wider than the view).  In
        a short view the explanation goes first, then the title; a view too short for the
        buttons shows none of it (nothing is ever drawn cut)."""
        v = self.view
        avail_w = max(1, v.width() - 2 * style.SP3)
        avail_h = max(1, v.height() - 2 * style.SP1)
        self.text.setVisible(True)
        self.title.setVisible(True)
        w = h = 0
        for drop in (None, self.text, self.title):
            if drop is not None:
                drop.setVisible(False)
            lay = self.layout()
            lay.invalidate()
            w = min(max(self.sizeHint().width(), self.minimumSizeHint().width(), 280), avail_w)
            h = max(lay.totalHeightForWidth(w) if lay.hasHeightForWidth() else 0, lay.totalMinimumSize().height())
            if h <= avail_h:
                break
        else:
            self.hide()             # not even the buttons fit: the menus and the tool bar remain
            return
        self.setGeometry((v.width() - w) // 2, max(style.SP1, (v.height() - h) // 2), w, h)

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: N802 - Qt API
        if obj is self.view:
            if ev.type() == QEvent.Show:
                self.update_state(self._doc)         # the commands plugins added meanwhile
            elif ev.type() == QEvent.Resize:
                self._view_resized()
        return super().eventFilter(obj, ev)


__all__ = ["CanvasEmptyState", "ENTRIES"]
