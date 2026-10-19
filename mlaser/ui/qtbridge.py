"""Qt side of the machine event bus: deliver controller events on the GUI thread.

The controllers publish :mod:`mlaser.machine.events` from their worker threads (poller, job
worker, safety monitor).  :class:`QtDispatcher` is installed as the bus dispatcher
(``ctl.events.set_dispatcher``): a delivery published on the GUI thread runs at once, one from
any other thread is queued to the GUI thread through a queued signal.  This replaces the
``_Bridge`` QObject the UI adapter used to carry.
"""
from __future__ import annotations

import threading
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal


class QtDispatcher(QObject):
    call = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gui = threading.current_thread()
        self.call.connect(self._run, Qt.QueuedConnection)
        self.alive = True

    def _run(self, thunk) -> None:
        thunk()

    def __call__(self, thunk: Callable[[], None]) -> None:
        if threading.current_thread() is self._gui:
            thunk()
        elif self.alive:
            self.call.emit(thunk)
        else:
            raise RuntimeError("GUI gone")


def install(ctl, dispatcher: QtDispatcher) -> None:
    """Route ``ctl``'s events (``MachineAPI.events``) through ``dispatcher``."""
    ctl.events.set_dispatcher(dispatcher)


__all__ = ["QtDispatcher", "install"]
