"""``InheritedField`` (SETTINGS_MAP §2 "How the UI shows it", WORK_ITEMS P-3): one value of the
precedence chain (recipe → layer → part → contour) as an editor row with its source.

::

    [ 3.00 mm ]                 from Mild steel S235 3 mm · O2 · v4     ← inherited (link: Edit in …)
    [ 2.00 mm ]  (restore)      set on layer 2                          ← overridden here
    [   —     ]  (restore)      mixed: 2 of 3 parts set their own       ← multi-selection

The owner computes the state (:mod:`mlaser.services.inheritance`) and calls :meth:`set_state`;
the field emits :attr:`edited` (a value the user entered) and :attr:`reset` (the restore
button).  The editor is bound to its value id (:func:`mlaser.ui.homes.bind_editor`), the chip
link navigates to the parent's home.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractSpinBox, QCheckBox, QComboBox, QHBoxLayout, QLineEdit,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from . import style
from .homes import bind_editor, edit_in
from .responsive import ElidedToolButton


class InheritedField(QWidget):
    edited = Signal(object)
    reset = Signal()

    def __init__(self, editor: QWidget, vid: str, tr: Any, window: Any = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from .icons import icon

        self.editor, self.vid, self.tr_, self.window_ = editor, vid, tr, window
        self.overridden = False
        self.source = ""
        self.parent_vid: Optional[str] = None
        self.parent_target: Any = None
        self._loading = False
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(style.SP1)
        row.addWidget(editor, 1)
        self.reset_btn = QToolButton()
        self.reset_btn.setIcon(icon("arrow-back-up"))
        self.reset_btn.setAutoRaise(True)
        self.reset_btn.setAccessibleName(tr.tr("techform.reset"))
        self.reset_btn.clicked.connect(self.reset.emit)
        self.reset_btn.hide()
        row.addWidget(self.reset_btn)
        col.addLayout(row)
        # the source: one line, cut with "…" in a narrow panel (the full text in its tooltip);
        # a link to the parent level's home when there is one
        self.chip = ElidedToolButton()
        self.chip.setObjectName("inherited_chip")
        self.chip.setAutoRaise(True)
        self.chip.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.chip.muted = True
        self.chip.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.chip.clicked.connect(lambda: self._follow(""))
        f = self.chip.font()
        f.setPointSizeF(f.pointSizeF() * 0.85)
        self.chip.setFont(f)
        col.addWidget(self.chip)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        bind_editor(editor, vid)
        self._connect()

    # ---------------------------------------------------------------- value
    def _connect(self) -> None:
        e = self.editor
        if isinstance(e, QCheckBox):
            e.toggled.connect(lambda _v: self._emit())
        elif isinstance(e, QComboBox):
            e.activated.connect(lambda _i: self._emit())
        elif isinstance(e, QAbstractSpinBox):
            e.editingFinished.connect(self._emit)
        elif isinstance(e, QLineEdit):
            e.editingFinished.connect(self._emit)

    def _emit(self) -> None:
        if not self._loading:
            self.edited.emit(self.value())

    def value(self) -> Any:
        e = self.editor
        if isinstance(e, QCheckBox):
            return e.isChecked()
        if isinstance(e, QComboBox):
            d = e.currentData()
            return e.currentIndex() if d is None else d
        if isinstance(e, QLineEdit):
            return e.text()
        return round(float(e.value()), 6) if hasattr(e, "decimals") else e.value()   # type: ignore[attr-defined]

    def set_value(self, v: Any) -> None:
        e = self.editor
        self._loading = True
        e.blockSignals(True)
        try:
            if isinstance(e, QCheckBox):
                e.setChecked(bool(v))
            elif isinstance(e, QComboBox):
                i = e.findData(v)
                e.setCurrentIndex(i if i >= 0 else max(0, min(e.count() - 1, int(v or 0))))
            elif isinstance(e, QLineEdit):
                e.setText("" if v is None else str(v))
            elif v is not None:
                e.setValue(v)   # type: ignore[attr-defined]
        finally:
            e.blockSignals(False)
            self._loading = False

    # ---------------------------------------------------------------- source
    def set_state(self, value: Any, overridden: bool, here: str, source: str,
                  parent_vid: Optional[str] = None, parent_target: Any = None, mixed: str = "") -> None:
        """``here``: words for this level ("layer 2", "this part"); ``source``: where an inherited
        value comes from ("Mild steel S235 3 mm · O2 · v4", "layer 2"); ``parent_vid``: the value
        id at the parent level (the chip link goes there); ``mixed``: text for a selection whose
        values differ."""
        t = self.tr_
        self.set_value(value)
        self.overridden = overridden
        self.source = source
        self.parent_vid, self.parent_target = parent_vid, parent_target
        self.reset_btn.setVisible(overridden or bool(mixed))
        if mixed:
            text = mixed
        elif overridden:
            text = t.format("inherit.set_on", where=here)
        else:
            text = t.format("inherit.from", source=source)
        self.chip.setText(text)
        self.chip.muted = not (parent_vid and not overridden and not mixed)     # a link: normal text
        self.chip.setCursor(Qt.PointingHandCursor if not self.chip.muted else Qt.ArrowCursor)
        self.chip.setFocusPolicy(Qt.TabFocus if not self.chip.muted else Qt.NoFocus)
        self.chip.update()
        f = self.editor.font()
        f.setBold(overridden)
        self.editor.setFont(f)
        self.reset_btn.setToolTip(t.format("inherit.reset_tip", source=source))
        label = self.editor.accessibleName().split(",")[0]
        state = t.format("inherit.set_on", where=here) if overridden else t.format("inherit.from", source=source)
        self.editor.setAccessibleDescription(state)
        if label:
            self.editor.setAccessibleName(f"{label}, {state}")

    def _follow(self, _href: str) -> None:
        if self.parent_vid and self.window_ is not None:
            edit_in(self.window_, self.parent_vid, self.parent_target)


__all__ = ["InheritedField"]
