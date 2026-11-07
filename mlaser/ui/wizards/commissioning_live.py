"""The live status panel of the commissioning wizard (GitHub issue #6).

One row per check (:mod:`mlaser.services.procedures.commissioning_live`): a status chip (symbol
and word: never colour alone), the check's name, what is measured now, the "test it" prompt
("Open the door now") and a button per fix action that goes to the one place that fixes it.
The rows sit in three sections in a :class:`~mlaser.ui.responsive.ColumnsPanel` (one or two
columns as the width allows; sections fold, lowest priority first, when the height is short:
the panel never scrolls).

The panel only shows; the wizard owns the fix actions (``on_fix(row_id, fix_id)``) and the
timer that recomputes the rows from the controller's published state.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFrame, QGridLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
                               QWidget)

from mlaser.services import units as U
from mlaser.services.procedures import commissioning_live as CL

from .. import style
from ..responsive import CollapsibleSection, ColumnsPanel, FlowLayout, mark_list
from .common import T

#: status -> (symbol, catalogue id of the word, colour role or "" for a plain chip)
STATUS = {CL.PASS: ("✓", "wizard.comm.s_pass", "success"), CL.WARN: ("!", "wizard.comm.s_warn", "warning"),
          CL.FAIL: ("✗", "wizard.comm.s_fail", "danger"), CL.SKIP: ("–", "wizard.comm.s_skip", ""),
          CL.PENDING: ("…", "wizard.comm.s_pending", "info")}
#: the sections: (catalogue id of the title, fold priority (higher folds last), row ids)
SECTIONS = (("wizard.comm.live.sec_safety", 2, ("estop", "ilk.door", "ilk.gas", "ilk.laser", "heartbeat")),
            ("wizard.comm.live.sec_link", 1, ("connection", "alarms", "scaling", "soft_limits")),
            ("wizard.comm.live.sec_motion", 0, ("homing", "jog_x", "jog_y")))
#: below this width (px) for the measurement block, it goes under the name
NARROW_INFO = 240
#: a second column of sections only when each gets this much (px): the rows want their width
COLUMN_MIN = 560
#: detail ids whose values are lengths in mm (display units)
LENGTH_VALUES = {"wizard.comm.home_ok": ("x", "y"), "wizard.comm.live.homed": ("x", "y"),
                 "wizard.comm.jog_moved": ("moved", "step"), "wizard.comm.jog_position": ("moved", "step")}


def detail_values(window: Any, detail: str, values: dict) -> dict:
    """The values of a detail text: lengths in the display units, the guard's refusals in words."""
    out = {k: v for k, v in values.items() if not isinstance(v, (dict, list))}
    u = U.current()
    for k in LENGTH_VALUES.get(detail, ()):
        if isinstance(out.get(k), (int, float)):
            out[k] = u.number(float(out[k]), "length", u.decimals("small_length"), trim=True)
    out.setdefault("unit", u.suffix("length"))
    if isinstance(values.get("refused"), tuple):
        out["refused"] = ", ".join(T(window, f"wizard.comm.live.refuse_{a}") for a in values["refused"]) or "-"
    return out


def fix_text(window: Any, fix: str) -> str:
    kind, _, arg = fix.partition(":")
    key = CL.FIXES.get(kind, kind)
    return T(window, key, axis=arg) if kind == "reverse" else T(window, key)


class LiveRow(QWidget):
    """One check: chip, name, detail, prompt, fix buttons."""

    def __init__(self, window: Any, rid: str, on_fix: Callable[[str, str], None],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.w, self.rid, self.on_fix = window, rid, on_fix
        self.setObjectName(f"live_{rid}")
        self.check: Optional[CL.LiveCheck] = None
        self._role: Optional[str] = None
        self._fixes: tuple = ()
        self.buttons: dict[str, QPushButton] = {}
        # wide: chip | name | what is measured, the prompt and the fixes; narrow: the
        # measurement block goes under the name (the row arranges itself from its width)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, style.SP1)
        self.grid.setHorizontalSpacing(style.SP2)
        self.grid.setVerticalSpacing(style.SP1 // 2)
        self.chip = QLabel()
        self.chip.setObjectName("live_chip")
        self.chip.setAlignment(Qt.AlignCenter)
        self.chip.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        fm = self.chip.fontMetrics()
        self.chip.setMinimumWidth(max(fm.horizontalAdvance(f"{sym} {T(window, key)}") for sym, key, _r in STATUS.values())
                                  + 2 * style.SP2 + 4)
        self.grid.addWidget(self.chip, 0, 0, Qt.AlignTop)
        self.name = QLabel(T(window, CL.TITLES[rid]))
        style.set_font(self.name, "label_strong")
        self.name.setWordWrap(True)
        self.grid.addWidget(self.name, 0, 1, Qt.AlignTop)
        self.info = QWidget()
        v = QVBoxLayout(self.info)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(style.SP1 // 2)
        self.detail = QLabel()
        self.detail.setObjectName("live_detail")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.detail)
        self.prompt = QLabel()
        self.prompt.setObjectName("live_prompt")
        self.prompt.setWordWrap(True)
        style.set_font(self.prompt, "label_strong")
        self.prompt.setVisible(False)
        v.addWidget(self.prompt)
        self.fix_box = QWidget()
        self.fix_lay = FlowLayout(self.fix_box, justify=False)
        self.fix_lay.setContentsMargins(0, 0, 0, 0)
        self.fix_box.setVisible(False)
        self.fix_box.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.narrow: Optional[bool] = None
        self.name_width = 0
        self._arrange(False)

    def set_name_width(self, w: int) -> None:
        """The names of a section share one column width (the rows line up)."""
        self.name_width = w
        self.name.setMinimumWidth(w)
        self._arrange(bool(self.narrow))

    def _arrange(self, narrow: bool) -> None:
        if narrow == self.narrow:
            return
        self.narrow = narrow
        self.grid.removeWidget(self.info)
        self.grid.removeWidget(self.fix_box)
        if narrow:
            self.grid.addWidget(self.info, 1, 1, 1, 3)
            self.grid.addWidget(self.fix_box, 2, 1, 1, 3)
            self.grid.setColumnStretch(1, 1)
            self.grid.setColumnStretch(2, 0)
        else:
            self.grid.addWidget(self.info, 0, 2, Qt.AlignTop)
            self.grid.addWidget(self.fix_box, 0, 3, Qt.AlignTop | Qt.AlignRight)
            self.grid.setColumnStretch(1, 0)
            self.grid.setColumnStretch(2, 1)

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(ev)
        self._fit()

    def _fit(self) -> None:
        if self.width() <= 0:
            return
        need = (self.chip.minimumWidth() + max(self.name_width, self.name.minimumSizeHint().width()) + NARROW_INFO
                + (self.fix_box.sizeHint().width() if self.buttons else 0))
        self._arrange(self.width() < need)

    def status_text(self) -> str:
        """Symbol and word of the current status (what the chip shows)."""
        if self.check is None:
            return ""
        sym, key, _role = STATUS[self.check.status]
        return f"{sym} {T(self.w, key)}"

    def set_check(self, c: CL.LiveCheck, enabled: Callable[[str], tuple[bool, str]]) -> None:
        self.check = c
        sym, key, role = STATUS[c.status]
        self.chip.setText(f"{sym} {T(self.w, key)}")
        if role != self._role:
            self._role = role
            if role:
                style.chip(self.chip, role)
            else:
                style.styled(self.chip, lambda: (f"border:1px solid {style.css('border')}; color:{style.css('muted')};"
                                                 f"padding:{style.SP1}px {style.SP2}px; border-radius:{style.SP1}px;"))
        text = T(self.w, c.detail, **detail_values(self.w, c.detail, c.values)) if c.detail else ""
        if self.detail.text() != text:
            self.detail.setText(text)
        p = T(self.w, c.prompt, **detail_values(self.w, c.prompt, c.values)) if c.prompt else ""
        if self.prompt.text() != p:
            self.prompt.setText(p)
        self.prompt.setVisible(bool(p))
        acc = f"{T(self.w, CL.TITLES[self.rid])}: {T(self.w, key)}. {text} {p}".strip()
        self.setAccessibleName(acc)
        if c.fixes != self._fixes:
            self._fixes = tuple(c.fixes)
            for b in self.buttons.values():
                self.fix_lay.removeWidget(b)
                b.deleteLater()
            self.buttons = {}
            for fx in self._fixes:
                b = QPushButton(fix_text(self.w, fx))
                b.setObjectName(f"fix_{self.rid}_{fx.replace(':', '_')}")
                b.setMinimumHeight(style.TARGET)     # a Service-mode tool at the PC (mouse targets)
                b.clicked.connect(lambda _=False, f=fx: self.on_fix(self.rid, f))
                self.fix_lay.addWidget(b)
                self.buttons[fx] = b
            self.fix_box.setVisible(bool(self.buttons))
            self._fit()
        for fx, b in self.buttons.items():
            ok, why = enabled(fx)
            style.set_enabled(b, ok, why)


class LiveStatusPanel(QWidget):
    """The rows in their sections, and a summary line on top."""

    def __init__(self, window: Any, on_fix: Callable[[str, str], None],
                 enabled: Callable[[str], tuple[bool, str]], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.w = window
        self.setObjectName("commissioning_live")
        self.enabled = enabled
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(style.SP1)
        self.header = QLabel()
        self.header.setObjectName("live_summary")
        self.header.setWordWrap(True)
        style.set_font(self.header, "label_strong")
        v.addWidget(self.header)
        # automatic sections fold to fit the height (no scroll bar); a section the operator
        # opens on a short window stays open and the list of checks scrolls
        self.columns = ColumnsPanel(max_columns=2, scroll_overflow=True)
        self.columns.column_min = lambda: COLUMN_MIN
        self.columns.refill = False          # never reopen a section ranked below a folded one
        self.scroll = QScrollArea()
        self.scroll.setObjectName("commissioning_live_scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.columns)
        mark_list(self.scroll)
        v.addWidget(self.scroll, 1)
        self.rows: dict[str, LiveRow] = {}
        self.sections: list[tuple[CollapsibleSection, tuple]] = []
        for title, prio, ids in SECTIONS:
            body = QWidget()
            bl = QVBoxLayout(body)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(style.SP1)
            for rid in ids:
                r = LiveRow(window, rid, on_fix)
                self.rows[rid] = r
                bl.addWidget(r)
            nw = max(self.rows[rid].name.sizeHint().width() for rid in ids)
            for rid in ids:
                self.rows[rid].set_name_width(min(nw, 220))
            sec = CollapsibleSection(T(window, title), body, priority=prio, fit_title=True)
            self.columns.add_section(sec)
            self.sections.append((sec, ids))

    def set_rows(self, rows: dict) -> None:
        for rid, c in rows.items():
            r = self.rows.get(rid)
            if r is not None:
                r.set_check(c, self.enabled)
        changed = False
        for (sec, ids), (_t, base, _ids) in zip(self.sections, SECTIONS):
            counts = CL.summary({i: rows[i] for i in ids if i in rows})
            sec.set_summary(self._counts(counts))
            # a section with something to fix folds last (its rows carry the fix buttons)
            prio = base + (10 if counts.get(CL.FAIL) or counts.get(CL.WARN) else 0)
            if prio != sec.priority:
                sec.priority = prio
                changed = True
        if changed:
            self.columns.schedule()
        self.header.setText(T(self.w, "wizard.comm.live.summary", counts=self._counts(CL.summary(rows))))

    def _counts(self, counts: dict) -> str:
        parts = []
        for st in (CL.FAIL, CL.WARN, CL.PENDING, CL.PASS, CL.SKIP):
            if counts.get(st):
                sym, key, _role = STATUS[st]
                parts.append(f"{sym} {counts[st]} {T(self.w, key)}")
        return " · ".join(parts)


__all__ = ["LiveStatusPanel", "LiveRow", "STATUS", "SECTIONS", "detail_values", "fix_text"]
