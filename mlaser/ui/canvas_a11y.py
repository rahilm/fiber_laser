"""The drawing canvas for assistive technology (UI evaluation F7).

The canvas is a painted QGraphicsView: a screen reader sees one "Drawing canvas" element and
nothing of the drawing. This module adds, without touching the scene-building code:

* a **summary** as the canvas' accessible description: part count, how many are selected and,
  for one selected part, its name and size ("4 parts, 1 selected. Selected: Bracket, 120 x 80
  mm"); updated on every document change and selection change (``DescriptionChanged``);
* **keyboard navigation of the parts** while the canvas has the focus: Page Down / Page Up
  select the next / previous part (wrapping), Home / End the first / last; the selected part is
  announced (polite) and kept in view. The arrows stay the nudge keys, Tab leaves the canvas.

A full ``QAccessibleInterface`` with one child per part (``QAccessible.installFactory``) is not
used: interfaces created from Python are owned by Qt's accessibility cache, which deletes them
on its own schedule, and PySide6 cannot hand that ownership over safely.
"""
from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QKeySequence, QShortcut

from . import a11y

#: navigation keys (canvas focus): key -> step (+1 next, -1 previous, "first", "last")
NAV_KEYS: dict[Any, Any] = {Qt.Key_PageDown: 1, Qt.Key_PageUp: -1, Qt.Key_Home: "first", Qt.Key_End: "last"}


class CanvasAccessibility(QObject):
    """Summary and part navigation for ``canvas`` (a :class:`mlaser.ui.canvas.CanvasView`)."""

    def __init__(self, canvas: Any, tr: Any, hooks: Any = None) -> None:
        super().__init__(canvas)
        self.canvas, self.tr_ = canvas, tr
        canvas.selectionChanged.connect(self.refresh)
        if hooks is not None:
            hooks.add("document_changed", lambda *_a: self.refresh(), "canvas_a11y")
        self.shortcuts = []
        for key, step in NAV_KEYS.items():
            sc = QShortcut(QKeySequence(key), canvas)
            sc.setContext(Qt.WidgetWithChildrenShortcut)          # only while the canvas has focus
            sc.activated.connect(lambda s=step: self.step(s))
            self.shortcuts.append(sc)
        self.refresh()

    # ------------------------------------------------------------------ summary
    def _parts(self) -> list:
        doc = getattr(self.canvas, "document", None)
        return list(getattr(doc, "parts", None) or [])

    def part_text(self, i: int, parts: Optional[list] = None) -> str:
        """"Part 2 of 4: Bracket, 120 x 80 mm" for assistive technology."""
        parts = self._parts() if parts is None else parts
        p = parts[i]
        nm = (getattr(p, "name", "") or "").strip() or self.tr_.format("a11y.canvas.part_n", n=i + 1)
        size = ""
        try:
            from ..services import units as U
            x0, y0, x1, y1 = self.canvas.part_bbox(p)
            size = U.current().fmt_size(x1 - x0, y1 - y0)
        except Exception:  # noqa: BLE001 - a part without geometry has no size
            pass
        if not size:
            return self.tr_.format("a11y.canvas.part_nosize", index=i + 1, total=len(parts), name=nm)
        return self.tr_.format("a11y.canvas.part", index=i + 1, total=len(parts), name=nm, size=size)

    def summary(self) -> str:
        parts = self._parts()
        if not parts:
            return self.tr_.tr("a11y.canvas.empty")
        sel = [i for i, p in enumerate(parts) if getattr(p, "selected", False)]
        text = self.tr_.plural("a11y.canvas.summary", len(parts), selected=len(sel))
        if len(sel) == 1:
            text = self.tr_.format("a11y.canvas.selected", summary=text, part=self.part_text(sel[0], parts))
        return text

    def refresh(self) -> None:
        try:
            a11y.name(self.canvas, self.canvas.accessibleName(), self.summary())
        except RuntimeError:                # the canvas was deleted
            pass

    # ------------------------------------------------------------------ navigation
    def step(self, step: Any) -> Optional[int]:
        """Select the next / previous / first / last part; returns its index (None: no parts)."""
        cv = self.canvas
        parts = self._parts()
        if not parts or getattr(cv, "tool", None) is not None:
            return None
        sel = [i for i, p in enumerate(parts) if getattr(p, "selected", False)]
        n = len(parts)
        if step == "first":
            i = 0
        elif step == "last":
            i = n - 1
        elif not sel:
            i = 0 if step > 0 else n - 1
        else:
            i = ((sel[-1] if step > 0 else sel[0]) + step) % n
        cv.select_parts([i])
        try:
            x0, y0, x1, y1 = cv.part_bbox(parts[i])
            from PySide6.QtCore import QRectF
            r = QRectF(x0, y0, x1 - x0, y1 - y0)            # scene = model coordinates
            if not cv.mapToScene(cv.viewport().rect()).boundingRect().intersects(r):
                cv.centerOn(r.center())
        except Exception:  # noqa: BLE001 - keeping the part in view is a convenience
            pass
        a11y.announce(cv, self.part_text(i, parts), force=True)
        return i


def install(window: Any) -> CanvasAccessibility:
    """Attach :class:`CanvasAccessibility` to ``window.canvas``."""
    return CanvasAccessibility(window.canvas, window.tr_, getattr(window, "hooks", None))


__all__ = ["CanvasAccessibility", "NAV_KEYS", "install"]
