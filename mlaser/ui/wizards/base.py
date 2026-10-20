"""The wizard dialog: shows a :class:`mlaser.services.procedures.Procedure` one step at a time.

Layout: the step list on the left (done ✓ / current ▶ / to do), the step title, instructions
and safety notes, a step page (form, plot, jog pad: :meth:`WizardDialog.page`), the result line,
and the buttons Back · Run · Yes / No · Next / Finish · **Abort** (red, always enabled; Esc and
F12 abort too).  Abort sends ``safe_halt`` in every state (the engine does it).  A "Dry run on
the simulator" box on the first step rebuilds the procedure on a private simulator rig.

Actions run in a worker thread; the procedure's notifications come back through a queued
signal.  Closing the dialog aborts a running step and closes the simulator rig.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from mlaser.services.procedures import engine as PE
from mlaser.services.procedures import simrig

from .. import icons
from ..units_qt import on_units_changed
from .common import MIN_TARGET, Bridge, JogPad, SafetyNotes, T, unit_values


class WizardDialog(QDialog):
    """Generic wizard; subclasses override :meth:`page`, :meth:`refresh_page` and
    :meth:`collect` for their own steps."""

    sim_kind = "card"            # simrig kind of the dry run

    def __init__(self, window: Any, factory: Callable[[Any, bool], PE.Procedure],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent if parent is not None else (window if isinstance(window, QWidget) else None))
        self.w = window
        self.factory = factory
        self.bridge = Bridge(self)
        self.rig = None                      # dry-run simulator controller
        self.pages: dict[str, QWidget] = {}
        self.jog: Optional[JogPad] = None
        self.resize(900, 620)
        self.setMinimumSize(720, 520)
        outer = QHBoxLayout(self)
        self.steps_list = QListWidget()
        self.steps_list.setFocusPolicy(Qt.NoFocus)
        self.steps_list.setMaximumWidth(240)
        self.steps_list.setSpacing(3)
        outer.addWidget(self.steps_list)
        right = QVBoxLayout()
        self.right = right
        outer.addLayout(right, 1)
        self.mode_chip = QLabel()
        self.mode_chip.setAlignment(Qt.AlignRight)
        self.title = QLabel()
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.4)
        f.setBold(True)
        self.title.setFont(f)
        top = QHBoxLayout()
        top.addWidget(self.title, 1)
        top.addWidget(self.mode_chip)
        right.addLayout(top)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.RichText)
        right.addWidget(self.text)
        self.notes = SafetyNotes()
        right.addWidget(self.notes)
        self.stack = QStackedWidget()
        right.addWidget(self.stack, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        right.addWidget(self.status)
        self.dry = QCheckBox(T(window, "wizard.dry_run"))
        self.dry.setToolTip(T(window, "wizard.dry_run_tip"))
        self.dry.toggled.connect(self._dry_toggled)
        right.addWidget(self.dry)
        bl = QHBoxLayout()
        self.b_abort = self._button("wizard.abort", self.abort, "player-stop", "#ffffff")
        self.b_abort.setStyleSheet("QPushButton { color: white; background: #c92a2a; border: 1px solid #8a1c1c; "
                                   "border-radius: 4px; padding: 6px 14px; font-weight: bold; }"
                                   "QPushButton:pressed { background: #a51111; }")
        bl.addWidget(self.b_abort)
        bl.addStretch(1)
        self.b_back = self._button("wizard.back", self.back)
        self.b_run = self._button("wizard.run", self.run, "player-play", icons.GREEN)
        self.b_yes = self._button("wizard.yes", lambda: self.answer(True))
        self.b_no = self._button("wizard.no", lambda: self.answer(False))
        self.b_next = self._button("wizard.next", self.next)
        for b in (self.b_back, self.b_run, self.b_yes, self.b_no, self.b_next):
            bl.addWidget(b)
        right.addLayout(bl)
        for seq in ("Esc", "F12"):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(self.abort)
        self.proc: PE.Procedure = self._make(False)
        self.proc.start()
        self.refresh()
        on_units_changed(self, self._units_changed, now=False)

    def _units_changed(self, _u: Any = None) -> None:
        """Display units switched: the step texts, the action button, the status and the page
        follow (no refresh(): it may auto-run a step)."""
        p, s = self.proc, self.proc.step
        if s is not None and p.state not in PE.FINAL:
            self.text.setText(T(self.w, s.text, **unit_values()) if s.text else "")
            if s.kind == "action":
                self.b_run.setText(T(self.w, s.action_label or "wizard.run", **unit_values()))
            pg = self.pages.get(s.id)
            if pg is not None:
                self.refresh_page(s, pg)
        self.status.setText(self.status_text())

    # ------------------------------------------------------------------ building
    def _button(self, key: str, fn, icon: str = "", color: Optional[str] = None) -> QPushButton:
        b = QPushButton(T(self.w, key))
        if icon:
            b.setIcon(icons.icon(icon, color))
        b.setMinimumHeight(MIN_TARGET)
        b.setMinimumWidth(110)
        b.clicked.connect(lambda _=False: fn())
        return b

    def machine(self) -> Any:
        return self.rig if self.rig is not None else self.w.ctl

    def _make(self, dry: bool) -> PE.Procedure:
        if dry:
            self.rig = self.make_rig()
        p = self.factory(self.machine(), dry)
        p.post = self.bridge.post
        p.listeners.append(lambda _p: self.refresh())
        self.setWindowTitle(T(self.w, p.title) if p.title else p.id)
        self.dry.setVisible(p.supports_dry_run)
        return p

    def make_rig(self) -> Any:
        return simrig.simulator_controller(self.sim_kind)

    def _close_rig(self) -> None:
        if self.rig is not None:
            simrig.close(self.rig)
            self.rig = None

    def _dry_toggled(self, on: bool) -> None:
        if self.proc.busy():
            return
        if self.proc.state not in (PE.READY, PE.WAITING) or self.proc.index > 0:
            self.dry.blockSignals(True)
            self.dry.setChecked(self.proc.dry_run)
            self.dry.blockSignals(False)
            return
        self._close_rig()
        try:
            self.proc = self._make(on)
        except Exception as e:  # noqa: BLE001 - the simulator could not start: stay real
            self._close_rig()
            self.dry.blockSignals(True)
            self.dry.setChecked(False)
            self.dry.blockSignals(False)
            self.proc = self._make(False)
            msg = T(self.w, "wizard.error.dry_run_unavailable", detail=str(e))
            self.status.setText(msg)
            # never let an operator believe this is a dry run when it is not
            QMessageBox.warning(self, T(self.w, "wizard.dry_run"), msg)
        self.pages.clear()
        while self.stack.count():
            wdg = self.stack.widget(0)
            self.stack.removeWidget(wdg)
            wdg.deleteLater()
        self.jog = None
        self.proc.start()
        self.refresh()

    # ------------------------------------------------------------------ pages
    def page(self, step: PE.Step) -> QWidget:
        """The widget for ``step`` (built once).  Default: a jog pad for ``jog`` steps."""
        w = QWidget()
        lay = QVBoxLayout(w)
        if step.jog:
            self.jog = JogPad(self.w, self.machine, z=False)
            lay.addWidget(self.jog)
        lay.addStretch(1)
        return w

    def refresh_page(self, step: PE.Step, page: QWidget) -> None:
        """Update the page of the current step (results, plots)."""

    def collect(self, step: PE.Step) -> bool:
        """Before Next on an input step: copy the form into the procedure; False = stay."""
        return True

    def confirm_run(self, step: PE.Step) -> bool:
        """Before an action runs (e.g. arming asks first)."""
        return True

    # ------------------------------------------------------------------ refresh
    def refresh(self) -> None:
        p = self.proc
        s = p.step
        vis = p.visible_steps()
        self.steps_list.clear()
        for st in vis:
            mark = "✓ " if st.id in p.done or st.id in p.answers else ("▶ " if s is not None and st.id == s.id else "   ")
            it = QListWidgetItem(mark + T(self.w, st.title))
            if s is not None and st.id == s.id:
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            self.steps_list.addItem(it)
        chip = T(self.w, "wizard.chip_dry") if p.dry_run else ""
        self.mode_chip.setText(chip)
        self.mode_chip.setStyleSheet(f"color: white; background: {icons.BLUE}; padding: 2px 8px; border-radius: 4px;"
                                     if chip else "")
        final = p.state in PE.FINAL
        if s is not None and not final:
            n = [x.id for x in vis].index(s.id) + 1 if s in vis else 0
            self.title.setText(T(self.w, "wizard.step_n", n=n, total=len(vis), title=T(self.w, s.title)))
            self.text.setText(T(self.w, s.text, **unit_values()) if s.text else "")
            self.notes.set_notes(self.w, s.safety)
            pg = self.pages.get(s.id)
            if pg is None:
                pg = self.page(s)
                self.pages[s.id] = pg
                self.stack.addWidget(pg)
            self.stack.setCurrentWidget(pg)
            self.refresh_page(s, pg)
        self.status.setText(self.status_text())
        busy = p.busy()
        self.b_back.setEnabled(p.can_back() and not busy)
        is_action = s is not None and s.kind == "action" and not final
        self.b_run.setVisible(is_action)
        if is_action:
            self.b_run.setText(T(self.w, s.action_label or "wizard.run", **unit_values()))
        self.b_run.setEnabled(is_action and p.state == PE.WAITING and s.id not in p.done)
        q = s is not None and s.kind == "question" and not final
        self.b_yes.setVisible(q)
        self.b_no.setVisible(q)
        self.b_yes.setEnabled(q and p.state == PE.WAITING)
        self.b_no.setEnabled(q and p.state == PE.WAITING)
        last = s is None or s not in vis or vis.index(s) == len(vis) - 1
        self.b_next.setText(T(self.w, "wizard.close" if final else ("wizard.finish" if last else "wizard.next")))
        self.b_next.setEnabled(final or p.can_next())
        self.b_abort.setEnabled(True)
        self.dry.setEnabled(p.index == 0 and p.state == PE.WAITING)
        if final:
            self.title.setText(T(self.w, {PE.DONE: "wizard.state_done", PE.ABORTED: "wizard.state_aborted",
                                          PE.FAILED: "wizard.state_failed"}[p.state]))
            self.notes.set_notes(self.w, ())
            if self.jog is not None:
                self.jog.release_all("finished")
        if p.should_auto_run():
            self.run()

    def status_text(self) -> str:
        p = self.proc
        if p.state == PE.RUNNING:
            return T(self.w, "wizard.running")
        if p.state == PE.FAILED and p.error is not None:
            return "⚠ " + T(self.w, p.error.key, **{**unit_values(), **p.error.values})
        if p.state == PE.ABORTED:
            return T(self.w, "wizard.aborted_text")
        r = p.results.get(p.step.id) if p.step else None
        if isinstance(r, dict) and r.get("warnings"):
            return "⚠ " + "; ".join(T(self.w, k) for k in r["warnings"])
        return ""

    # ------------------------------------------------------------------ actions
    def run(self) -> None:
        p, s = self.proc, self.proc.step
        if s is None or s.kind != "action" or p.state != PE.WAITING or s.id in p.done:
            return
        if not self.confirm_run(s):
            return
        p.execute_async()
        self.refresh()

    def next(self) -> None:
        p = self.proc
        if p.state in PE.FINAL:
            self.accept()
            return
        s = p.step
        if s is not None and s.id not in self.pages:     # notifications still queued
            self.refresh()
        if s is not None and s.kind == "input" and not self.collect(s):
            return
        if p.can_next():
            p.next()

    def back(self) -> None:
        self.proc.back()

    def answer(self, value: bool) -> None:
        try:
            self.proc.answer(value)
        except RuntimeError:
            pass

    def abort(self) -> None:
        if self.jog is not None:
            self.jog.release_all("abort")
        self.proc.abort("operator")
        self.refresh()

    def call_on_gui(self, fn: Callable[[], Any], timeout_s: float = 30.0) -> Any:
        """Run ``fn`` on the GUI thread and wait for it (document edits from an action step)."""
        if threading.current_thread() is threading.main_thread():
            return fn()
        box: dict = {}
        ev = threading.Event()

        def run() -> None:
            try:
                box["r"] = fn()
            except BaseException as e:  # noqa: BLE001 - re-raised in the worker
                box["e"] = e
            finally:
                ev.set()
        self.bridge.post(run)
        if not ev.wait(timeout_s):
            raise TimeoutError("GUI did not run the request")
        if "e" in box:
            raise box["e"]
        return box.get("r")

    def _ask(self, key: str, title_key: str = "wizard.confirm") -> bool:
        if getattr(self.w, "_test_no_dialogs", False):
            return True
        r = QMessageBox.question(self, T(self.w, title_key), T(self.w, key),
                                 QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Cancel)
        return r == QMessageBox.Ok

    # ------------------------------------------------------------------ closing
    def done(self, r: int) -> None:
        if self.proc.busy():
            self.proc.abort("dialog closed")
            self.proc.join(5.0)
        if self.jog is not None:
            self.jog.release_all("closed")
        self.on_close()
        self._close_rig()
        super().done(r)

    def reject(self) -> None:
        # Esc is "abort" (shortcut); closing the window aborts a running step
        self.done(QDialog.Rejected)

    def on_close(self) -> None:
        """Subclass clean-up (canvas preview...)."""


__all__ = ["WizardDialog"]
