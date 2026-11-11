"""Practice mode plugin: scripted lessons on the simulator with a non-modal coach window.

Logic in :mod:`mlaser.services.practice`; this module holds the coach window (a tool window
next to the main window, so the Run screen stays usable and an exclusive workspace does not hide
it), the command ``practice.open`` (Operator mode), the progress setting
``plugin.ext_practice.progress`` and the provider ``practice.active`` that makes the Report
record the jobs of a lesson as ``practice`` (:mod:`mlaser.services.runsource`).
User documentation: docs/user/practice.md.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                               QProgressBar, QPushButton, QVBoxLayout, QWidget)

from mlaser.services import practice as PR
from mlaser.services import units as U
from mlaser.services.config import Setting
from mlaser.services.operator import fmt_duration
from mlaser.services.procedures import engine as PE

from mlaser.ui import style
from mlaser.ui.units_qt import on_units_changed

from .extension_context import ExtensionContext

#: the practice sheet (mlaser.services.practice.practice_document): 300 x 200 mm
PRACTICE_SHEET_MM = (300.0, 200.0)


def _t(ctx: ExtensionContext, key: str, **kw: Any) -> str:
    """A lesson text (catalogue id with its English default from :data:`PR.TEXTS`)."""
    # the sheet size in the display units (texts that have no {sheet} ignore it)
    kw.setdefault("sheet", U.current().fmt_size(*PRACTICE_SHEET_MM, decimals=None if U.current().imperial else 0,
                                                trim=True))
    return ctx.text(key, PR.TEXTS.get(key, key), **kw)


class WindowHost:
    """:class:`mlaser.services.practice.PracticeHost` on the main window: the practice sheet
    replaces the open document (undoable) on the GUI thread."""

    def __init__(self, plugin: "PracticePlugin") -> None:
        self.plugin = plugin

    def load_document(self, doc: Any) -> bool:
        return bool(self.plugin.call_on_gui(lambda: self.plugin.load_sheet(doc)))

    def document(self) -> Any:
        return self.plugin.ctx.document


class CoachWindow(QWidget):
    """Lesson list with progress, the current step and its buttons (never modal)."""

    def __init__(self, plugin: "PracticePlugin") -> None:
        super().__init__(plugin.ctx.parent, Qt.Tool)
        self.plugin = plugin
        cx = plugin.ctx
        self.setObjectName("practice_coach")
        self.setWindowTitle(cx.text("practice.title", "Practice (simulator)"))
        self.setMinimumWidth(380)
        v = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setObjectName("practice_summary")
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)
        self.sim_note = QLabel(_t(cx, "practice.error.not_simulator"))
        self.sim_note.setObjectName("practice_sim_note")
        self.sim_note.setWordWrap(True)
        style.chip(self.sim_note, "warning")
        v.addWidget(self.sim_note)
        self.connect_btn = self._button("practice_connect", cx.text("practice.btn.connect", "Connect the simulator"),
                                        plugin.connect_simulator)
        v.addWidget(self.connect_btn)
        self.lessons = QListWidget()
        self.lessons.setObjectName("practice_lessons")
        self.lessons.setMinimumHeight(150)
        v.addWidget(self.lessons)
        self.start_btn = self._button("practice_start", cx.text("practice.btn.start", "Start the lesson"),
                                      self._start_selected)
        v.addWidget(self.start_btn)
        # current lesson
        self.lesson_title = QLabel()
        self.lesson_title.setObjectName("practice_lesson_title")
        style.set_font(self.lesson_title, "title")
        v.addWidget(self.lesson_title)
        self.progress = QProgressBar()
        self.progress.setObjectName("practice_progress")
        v.addWidget(self.progress)
        self.step_title = QLabel()
        self.step_title.setObjectName("practice_step_title")
        style.set_font(self.step_title, "label_strong")
        self.step_title.setWordWrap(True)
        v.addWidget(self.step_title)
        self.step_text = QLabel()
        self.step_text.setObjectName("practice_step_text")
        self.step_text.setWordWrap(True)
        self.step_text.setMinimumHeight(60)
        v.addWidget(self.step_text)
        self.status = QLabel()
        self.status.setObjectName("practice_status")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        row = QHBoxLayout()
        self.run_btn = self._button("practice_run", "", self._run)
        self.next_btn = self._button("practice_next", cx.text("practice.btn.next", "Next"), self._next)
        self.abort_btn = self._button("practice_abort", cx.text("practice.btn.abort", "Abort the lesson"),
                                      self._abort)
        # Abort stops the lesson's machine: the danger role (text, not a second STOP)
        pal = self.abort_btn.palette()
        pal.setColor(pal.ColorRole.ButtonText, style.color("danger"))
        self.abort_btn.setPalette(pal)
        for b in (self.run_btn, self.next_btn, self.abort_btn):
            row.addWidget(b)
        v.addLayout(row)
        v.addStretch(1)
        self.lessons.currentRowChanged.connect(lambda _r: self._buttons())
        on_units_changed(self, lambda _u: self.refresh(), now=False)

    def _button(self, name: str, text: str, fn: Callable[[], Any]) -> QPushButton:
        b = style.button(text, size="touch", name=name)
        b.clicked.connect(lambda _=False: fn())
        return b

    # ------------------------------------------------------------------ actions
    def _selected_id(self) -> Optional[str]:
        it = self.lessons.currentItem()
        return it.data(Qt.UserRole) if it is not None else None

    def _start_selected(self) -> None:
        lid = self._selected_id()
        if lid:
            self.plugin.begin(lid)

    def _run(self) -> None:
        lesson = self.plugin.service.active
        if lesson is None or lesson.state != PE.WAITING:
            return
        s = lesson.step
        if s is not None and s.kind == "action" and s.id not in lesson.done:
            lesson.execute()                         # the load step: quick, on this thread

    def _next(self) -> None:
        lesson = self.plugin.service.active
        if lesson is not None and lesson.can_next():
            lesson.next()

    def _abort(self) -> None:
        self.plugin.service.abort("operator")

    # ------------------------------------------------------------------ refresh
    def refresh(self) -> None:
        p = self.plugin
        cx = p.ctx
        svc = p.service
        n = len(PR.LESSONS)
        self.summary.setText(cx.text("practice.summary", "{passed} of {n} lessons passed. Practice jobs never "
                                "count as production.", passed=svc.passed_count(), n=n))
        sim = PR.Lesson.allowed(cx.controller)
        self.sim_note.setVisible(not sim)
        self.connect_btn.setVisible(not sim)
        keep = self._selected_id()
        self.lessons.blockSignals(True)
        self.lessons.clear()
        for cls in svc.lessons():
            prog = svc.progress.get(cls.id) or PR.LessonProgress()
            mark = "✓" if prog.passed else "○"
            bits = [f"{mark}  {_t(cx, cls.title)}"]
            if prog.attempts:
                bits.append(cx.plural("practice.attempts", prog.attempts))
            if prog.best_s:
                bits.append(cx.text("practice.best", "best {time}", time=fmt_duration(prog.best_s)))
            it = QListWidgetItem("  ·  ".join(bits))
            it.setData(Qt.UserRole, cls.id)
            self.lessons.addItem(it)
            if cls.id == keep:
                self.lessons.setCurrentItem(it)
        if self.lessons.currentRow() < 0 and self.lessons.count():
            nxt = next((i for i, c in enumerate(svc.lessons()) if not svc.progress[c.id].passed), 0)
            self.lessons.setCurrentRow(nxt)
        self.lessons.blockSignals(False)
        lesson = svc.active
        if lesson is None:
            self.lesson_title.setText(cx.text("practice.none", "Choose a lesson and press Start."))
            self.progress.setValue(0)
            self.step_title.setText("")
            self.step_text.setText("")
            self.status.setText("")
        else:
            self._show_lesson(lesson)
        self._buttons()

    def _show_lesson(self, lesson: PR.Lesson) -> None:
        cx = self.plugin.ctx
        steps = lesson.visible_steps()
        self.lesson_title.setText(_t(cx, lesson.title))
        s = lesson.step
        k = steps.index(s) if s in steps else len(steps)
        self.progress.setRange(0, max(1, len(steps)))
        self.progress.setValue(len(steps) if lesson.state == PE.DONE else k)
        if s is not None:
            self.step_title.setText(_t(cx, s.title))
            self.step_text.setText(_t(cx, s.text))
        state = lesson.state
        if state == PE.DONE:
            text = cx.text("practice.state.done", "Lesson passed in {time}.", time=fmt_duration(lesson.elapsed_s))
        elif state == PE.ABORTED:
            text = cx.text("practice.state.aborted", "Lesson aborted (the simulator was stopped).")
        elif state == PE.FAILED:
            err = lesson.error
            detail = _t(cx, err.key, **err.values) if err is not None else ""
            text = cx.text("practice.state.failed", "Lesson not passed: {detail}", detail=detail)
        elif state == PE.RUNNING:
            text = cx.text("practice.state.watching", "Your turn: do it on the Run screen. This panel checks it.")
        else:
            text = ""
        self.status.setText(text)

    def _buttons(self) -> None:
        svc = self.plugin.service
        lesson = svc.active
        running = svc.running
        self.start_btn.setEnabled(not running and self._selected_id() is not None and PR.Lesson.allowed(
            self.plugin.ctx.controller))
        s = lesson.step if lesson is not None else None
        manual = (running and lesson is not None and s is not None and lesson.state == PE.WAITING
                  and s.kind == "action" and not s.auto and s.id not in lesson.done)
        self.run_btn.setVisible(bool(manual))
        if manual and s is not None:
            self.run_btn.setText(_t(self.plugin.ctx, s.action_label or "practice.btn.run"))
        self.next_btn.setEnabled(bool(running and lesson is not None and lesson.can_next()))
        self.abort_btn.setEnabled(running)


class PracticePlugin:
    def __init__(self, ctx: ExtensionContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings_schema([Setting("progress", "json", {}, label="practice.set.progress")])
        self.service = PR.PracticeService(load=lambda: self.settings["progress"], save=self._save)
        self.host = WindowHost(self)
        self.panel: Optional[CoachWindow] = None
        self._gui_thread = threading.current_thread()
        from .wizards.common import Bridge
        self.bridge = Bridge(ctx.parent)
        self.service.listeners.append(self._changed)
        ctx.provide("practice.active", lambda: self.service.running)
        ctx.register_command("practice.open", self.open, label="practice.cmd.open", min_mode="operator")
        ctx.on_controller_changed(lambda _c: self.service.abort("controller changed"))
        ctx.on_close(lambda: self.service.abort("closing"))

    # ------------------------------------------------------------------ helpers
    def _save(self, data: dict) -> None:
        self.settings["progress"] = data
        self.ctx.config.flush()

    def call_on_gui(self, fn: Callable[[], Any], timeout_s: float = 30.0) -> Any:
        if threading.current_thread() is self._gui_thread:
            return fn()
        box: dict = {}
        done = threading.Event()

        def run() -> None:
            try:
                box["r"] = fn()
            finally:
                done.set()
        self.bridge.post(run)
        done.wait(timeout_s)
        return box.get("r")

    def load_sheet(self, doc: Any) -> bool:
        try:
            self.ctx.replace_document(doc, self.ctx.text("practice.undo.load", "practice: load the sheet"))
        except Exception as exc:  # noqa: BLE001 - DocumentLockedError: shown as the step's failure
            self.ctx.log(self.ctx.text("practice.log.error", "Practice: {error}", error=exc))
            return False
        return True

    def connect_simulator(self) -> bool:
        ok = bool(self.ctx.connect_simulator())
        self._changed()
        return ok

    # ------------------------------------------------------------------ lessons
    def open(self) -> CoachWindow:
        if self.panel is None:
            self.panel = CoachWindow(self)
        self.panel.refresh()
        self.panel.show()
        self.panel.raise_()
        return self.panel

    def begin(self, lesson_id: str) -> Optional[PR.Lesson]:
        cx = self.ctx
        if not PR.Lesson.allowed(cx.controller):
            if not cx.dialogs_suppressed and QMessageBox.question(
                    cx.parent, cx.text("practice.title", "Practice (simulator)"),
                    cx.text("practice.ask_connect", "Practice runs on the simulator only. Connect the simulator "
                       "now? (The real machine is disconnected.)"),
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
                self.connect_simulator()
            if not PR.Lesson.allowed(cx.controller):
                self.ctx.log(_t(cx, "practice.error.not_simulator"))
                return None
        try:
            lesson = self.service.begin(lesson_id, cx.controller, self.host, log_fn=self.ctx.log, post=self.bridge.post)
        except PR.PracticeError as exc:
            self.ctx.log(_t(cx, exc.key))
            return None
        self._changed()
        return lesson

    def _changed(self) -> None:
        lesson = self.service.active
        if lesson is not None and lesson.should_auto_run():
            lesson.execute_async()                   # a task: watch the events in a worker
        if self.panel is not None and self.panel.isVisible():
            self.panel.refresh()


def register(ctx: ExtensionContext) -> PracticePlugin:
    return PracticePlugin(ctx)


__all__ = ["register", "PracticePlugin", "CoachWindow", "WindowHost"]
