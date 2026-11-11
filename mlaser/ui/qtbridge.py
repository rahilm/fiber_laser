"""Qt side of the machine event bus: deliver controller events on the GUI thread.

The controllers publish :mod:`mlaser.machine.events` from their worker threads (poller, job
worker, safety monitor).  :class:`QtDispatcher` is installed as the bus dispatcher
(``ctl.events.set_dispatcher``): a delivery published on the GUI thread runs at once, one from
any other thread is queued to the GUI thread through a queued signal.  This replaces the
``_Bridge`` QObject the UI adapter used to carry.

Once the GUI is torn down (:func:`close_children`, run on the GUI thread after the main loop and
before the window is deleted), a delivery from another thread is dropped: it is never run on
the publishing thread instead.  A handler of the window run there touched widgets from a worker
thread while the GUI thread deleted them (the queued disconnect's events at exit: "Could not
parse stylesheet" on the safety banner, then heap corruption, exit codes -6 and -11).  A lock
makes the close wait for an emit in progress, so no emit reaches an object being deleted.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal

log = logging.getLogger(__name__)


class QtDispatcher(QObject):
    call = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gui = threading.current_thread()
        self._emit_lock = threading.Lock()
        self.call.connect(self._run, Qt.QueuedConnection)
        self.alive = True

    def close(self) -> None:
        """The GUI is going away (GUI thread, before the window is deleted): deliveries from
        other threads are dropped from now on; an emit in progress has ended when this returns."""
        with self._emit_lock:
            self.alive = False

    def _run(self, thunk) -> None:
        thunk()

    @property
    def ui_thread(self) -> threading.Thread | None:
        """The GUI thread while this dispatcher (and its window) lives: the controller queues the
        card I/O of operator commands given on it (``controller.commands``, issue #5)."""
        if not self.alive or not _valid(self):
            return None
        return self._gui

    def pump(self) -> None:
        """Process the GUI thread's pending events (``ctl.wait_commands`` on the GUI thread)."""
        from PySide6.QtCore import QCoreApplication
        if QCoreApplication.instance() is not None and threading.current_thread() is self._gui:
            QCoreApplication.processEvents()

    def __call__(self, thunk: Callable[[], None]) -> None:
        """Run ``thunk`` on the GUI thread: at once there, queued from any other thread, dropped
        from another thread once the GUI is gone (never run on the caller's thread: the window's
        handlers must not touch widgets off the GUI thread)."""
        if threading.current_thread() is self._gui:
            thunk()
            return
        with self._emit_lock:
            if self.alive:
                try:
                    self.call.emit(thunk)
                    return
                except RuntimeError:            # the window (and this object) was deleted
                    self.alive = False
        log.debug("GUI gone: a delivery from %s dropped", threading.current_thread().name)


def _valid(obj) -> bool:
    try:
        import shiboken6
        return bool(shiboken6.isValid(obj))
    except Exception:  # noqa: BLE001
        return False


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
        self._closed = False
        self._ready.connect(self._deliver, Qt.QueuedConnection)

    def close(self) -> None:
        """The GUI is going away (GUI thread, before this object is deleted): a call still
        running delivers nothing; an emit in progress has ended when this returns."""
        with self._lock:
            self._closed = True

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
                if self._closed:
                    return
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


def when_done(value, fn: Callable[[object], None]) -> None:
    """``fn(result)`` for a controller call's return value: at once for a plain value, when the
    command ended for a queued command's ticket (``controller.commands.CommandTicket``: an
    operator command given on the GUI thread runs on the command worker), on the GUI thread."""
    then = getattr(value, "then", None)
    if callable(then) and hasattr(value, "dropped"):
        then(lambda t: fn(None if t.dropped or t.error is not None else t.result))
    else:
        fn(value)


def close_children(obj: QObject) -> None:
    """Close every :class:`QtDispatcher` and :class:`BackgroundCall` in ``obj`` and below it
    (GUI thread, before ``obj`` is deleted): no worker thread emits into them or runs a GUI
    handler on its own thread while the GUI thread deletes them."""
    for kind in (QtDispatcher, BackgroundCall):
        found = list(obj.findChildren(kind))
        if isinstance(obj, kind):
            found.append(obj)
        for o in found:
            o.close()


def install(ctl, dispatcher: QtDispatcher) -> None:
    """Route ``ctl``'s events (``MachineAPI.events``) through ``dispatcher``."""
    ctl.events.set_dispatcher(dispatcher)


__all__ = ["BackgroundCall", "QtDispatcher", "close_children", "install", "when_done"]
