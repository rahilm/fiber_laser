"""Idle wake-ups: UI-only timers stop while the main window is minimised or hidden.

At rest the window woke up 22 times a second, also when minimised (UI evaluation F12): the
status poll (10/s), the Machine workspace's labels (5/s), the Run screen's tick (2/s).  Those
timers only refresh what is on screen, so :class:`HiddenPause` stops them while nobody can see
it and starts them again (with one immediate tick) when the window shows again.

Only *display* timers are registered here.  Anything that watches the machine or its alarms
keeps running: the controller's poller and the safety supervisor are threads, not UI timers,
and the diagnostics service's poll (maintenance counters, alarm history, batch cutting) is not
registered.  A timer may pass ``keep``: while it returns True the timer runs on (the status
poll records the head's track during a job, the Run screen serves the LAN monitor).
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QTimer


class _Entry:
    __slots__ = ("timer", "keep", "on_resume", "paused")

    def __init__(self, timer: QTimer, keep: Optional[Callable[[], bool]],
                 on_resume: Optional[Callable[[], None]]) -> None:
        self.timer = timer
        self.keep = keep
        self.on_resume = on_resume
        self.paused = False


class HiddenPause(QObject):
    """Stops the registered timers while ``window`` is minimised or hidden (see the module
    docstring).  A timer that was not running when the window went away stays stopped."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window
        self._entries: list[_Entry] = []
        self.hidden = False
        window.installEventFilter(self)

    def add(self, timer: QTimer, keep: Optional[Callable[[], bool]] = None,
            on_resume: Optional[Callable[[], None]] = None) -> None:
        """Register a display timer; ``on_resume`` runs when the window shows again (normally
        the timer's own tick, so the display is current at once)."""
        e = _Entry(timer, keep, on_resume)
        self._entries.append(e)
        try:
            timer.destroyed.connect(lambda *_a, e=e: self._entries.remove(e) if e in self._entries else None)
        except (RuntimeError, AttributeError):
            pass
        if self.hidden:
            self._pause(e)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt
        if obj is self.window and ev.type() in (QEvent.WindowStateChange, QEvent.Hide, QEvent.Show):
            self.update()
        return False

    def window_hidden(self) -> bool:
        w = self.window
        try:
            return bool(w.isMinimized() or not w.isVisible())
        except RuntimeError:            # the window is being destroyed
            return False

    def update(self) -> None:
        hidden = self.window_hidden()
        if hidden == self.hidden:
            return
        self.hidden = hidden
        for e in list(self._entries):
            if hidden:
                self._pause(e)
            else:
                self._resume(e)

    def check(self, timer: QTimer) -> None:
        """Called from a timer's own tick: stop it now when the window is hidden and its
        ``keep`` no longer holds (a job ended while the window was minimised)."""
        if not self.hidden:
            return
        for e in self._entries:
            if e.timer is timer:
                self._pause(e)

    def _pause(self, e: _Entry) -> None:
        try:
            if e.paused or not e.timer.isActive():
                return
            if e.keep is not None and e.keep():
                return
            e.timer.stop()
            e.paused = True
        except RuntimeError:            # a deleted timer
            pass

    def _resume(self, e: _Entry) -> None:
        if not e.paused:
            return
        e.paused = False
        try:
            e.timer.start()
            if e.on_resume is not None:
                e.on_resume()
        except RuntimeError:
            pass

    def paused(self, timer: QTimer) -> bool:
        return any(e.paused for e in self._entries if e.timer is timer)


def hidden_pause(window) -> Optional[HiddenPause]:
    """The window's :class:`HiddenPause` (None for a window without one, e.g. a test stub)."""
    return getattr(window, "idle_pause", None)
