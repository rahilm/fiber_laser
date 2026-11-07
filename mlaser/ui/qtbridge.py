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


class BackgroundCall(QObject):
    """Blocking I/O (card or device requests, a connect) off the GUI thread.

    ``start(fn, on_done)`` runs ``fn`` in a daemon thread and calls ``on_done(result, error)``
    on the GUI thread; it returns False (and does nothing) while the previous call still runs,
    so a timer that fires faster than the card answers never piles requests up.  With the card
    switched off a single register read waits out the retry policy (seconds): done on the GUI
    thread it froze the window (issue #5).  ``wait()`` blocks until the running call ended and
    delivers its result at once (tests, scripting).  A result arriving after the owner was
    deleted is dropped (the signal's receiver is gone)."""
    _ready = Signal()

    def __init__(self, parent=None, name: str = "mlaser-io"):
        super().__init__(parent)
        self._name = name
        self._thread: threading.Thread | None = None
        self._pending: tuple | None = None
        self._lock = threading.Lock()
        self._ready.connect(self._deliver, Qt.QueuedConnection)

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def start(self, fn: Callable[[], object], on_done: Callable[[object, BaseException | None], None]) -> bool:
        if self._thread is not None:
            return False

        def work() -> None:
            try:
                res: tuple = (fn(), None)
            except Exception as e:  # noqa: BLE001 - handed to on_done
                res = (None, e)
            with self._lock:
                self._pending = (on_done, *res)
            try:
                self._ready.emit()
            except RuntimeError:            # the owner (and this object) was deleted
                pass

        self._thread = threading.Thread(target=work, name=self._name, daemon=True)
        self._thread.start()
        return True

    def wait(self, timeout: float = 60.0) -> bool:
        th = self._thread
        if th is not None:
            th.join(timeout)
            if th.is_alive():
                return False
        self._deliver()
        return True

    def _deliver(self) -> None:
        with self._lock:
            item, self._pending = self._pending, None
        if item is None:
            return
        self._thread = None
        on_done, res, err = item
        on_done(res, err)


def install(ctl, dispatcher: QtDispatcher) -> None:
    """Route ``ctl``'s events (``MachineAPI.events``) through ``dispatcher``."""
    ctl.events.set_dispatcher(dispatcher)


__all__ = ["BackgroundCall", "QtDispatcher", "install"]
