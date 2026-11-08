"""Operator commands off the GUI thread: one ordered card-command executor (issue #5).

A card request waits out the retry policy when the card does not answer (seconds: the
machine's e-stop switches the card off).  Done on the GUI thread, every panel command then froze
the window - jog release 29 s, Gas 7 s, Clear alarm 6 s, Disconnect 10 s.

:class:`CommandExecutor` is a single worker thread with a FIFO queue.  The UI controller's
operator commands (:func:`card_command`) called **on the GUI thread** (the thread of the event
dispatcher the window installed, ``ctl.events.dispatcher.ui_thread``) are queued there and the
call returns at once with a :class:`CommandTicket`; called on any other thread (scripts, the API
server, tests without a window, the executor itself) they run inline as before.

Ordering and safety:

* one worker: the commands reach the wire in the order the operator gave them;
* a command that can move the machine or fire the beam remembers the controller's start
  generation (:meth:`mlaser.controller.parts.gate.StartGatePart.start_generation`, advanced by
  every Stop, halt, fault, disarm, connect and disconnect) when it is queued: a stale one is
  dropped when its turn comes, and the supervisor's guard refuses its motion / emission
  requests while it runs (:meth:`CommandExecutor.stale_reason`) - a Stop always wins over
  queued and in-flight commands;
* halts never wait in this queue: Stop, Hold, the supervisor halt and the halt channel stay
  direct (immediate threads), and off-commands (jog release, outputs off, laser off, alarm
  reset) are never dropped.
"""
from __future__ import annotations

import collections
import functools
import logging
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger("mlaser.controller.commands")


class CommandTicket:
    """A queued command: ``done()``, ``wait()``, ``result`` / ``error``, ``then(fn)``.

    ``then(fn)`` calls ``fn(ticket)`` once the command ended (also when it was dropped), on
    the thread of the executor's dispatcher (the GUI thread for a window).  A ticket is truthy
    (the command was accepted); its outcome is ``result``."""

    __slots__ = ("what", "fn", "gen", "drop_if_stale", "t_submit", "result", "error", "dropped",
                 "_done", "_then", "_lock", "_dispatch")

    def __init__(self, what: str, fn: Callable[[], Any], gen: Optional[int], drop_if_stale: bool,
                 dispatch: Optional[Callable[[Callable[[], None]], None]]):
        self.what = what
        self.fn = fn
        self.gen = gen
        self.drop_if_stale = drop_if_stale
        self.t_submit = time.monotonic()
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.dropped = ""              # why the command was not run (stale)
        self._done = threading.Event()
        self._then: list = []
        self._lock = threading.Lock()
        self._dispatch = dispatch

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        st = "dropped" if self.dropped else ("done" if self.done() else "pending")
        return f"<CommandTicket {self.what} {st}>"

    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._done.wait(timeout)

    def then(self, fn: Callable[["CommandTicket"], None]) -> "CommandTicket":
        with self._lock:
            if not self._done.is_set():
                self._then.append(fn)
                return self
        self._call(fn)
        return self

    def _finish(self) -> None:
        with self._lock:
            self._done.set()
            fns, self._then = self._then, []
        for fn in fns:
            self._call(fn)

    def _call(self, fn) -> None:
        d = self._dispatch
        if d is not None:
            try:
                d(lambda: fn(self))
                return
            except Exception:  # noqa: BLE001 - dispatcher gone (window closed): call here
                pass
        try:
            fn(self)
        except Exception:  # noqa: BLE001
            log.exception("command %s: completion callback", self.what)


class CommandExecutor:
    """The ordered command worker (see the module doc).

    ``stale(gen)`` returns a reason when generation ``gen`` is out of date; ``dispatch`` runs a
    thunk where completion callbacks belong (the controller's event dispatcher);
    ``on_change(pending, what)`` is told about the queue length (busy indication)."""

    def __init__(self, name: str = "mlaser-commands", *,
                 stale: Optional[Callable[[int], Optional[str]]] = None,
                 dispatch: Optional[Callable[[], Optional[Callable]]] = None,
                 on_change: Optional[Callable[[int, str], None]] = None,
                 on_dropped: Optional[Callable[[CommandTicket], None]] = None):
        self.name = name
        self._stale = stale
        self._dispatch = dispatch
        self.on_change = on_change
        self.on_dropped = on_dropped
        self._q: collections.deque[CommandTicket] = collections.deque()
        self._cv = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._current: Optional[CommandTicket] = None
        self._local = threading.local()
        self.completed = 0

    # ------------------------------------------------------------------ worker
    def start(self) -> None:
        """Start the worker (idempotent).  The controller does it at connect, so a GUI-thread
        submit never pays for a thread start."""
        with self._cv:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
            self._thread.start()

    def on_worker(self) -> bool:
        return threading.current_thread() is self._thread

    def current(self) -> Optional[CommandTicket]:
        """The command running on the calling thread (None off the worker)."""
        return getattr(self._local, "ticket", None)

    def pending(self) -> int:
        """Queued + running commands."""
        with self._cv:
            return len(self._q) + (self._current is not None)

    def idle(self) -> bool:
        return self.pending() == 0

    def submit(self, what: str, fn: Callable[[], Any], *, gen: Optional[int] = None,
               drop_if_stale: bool = False) -> CommandTicket:
        d = self._dispatch() if self._dispatch is not None else None
        t = CommandTicket(what, fn, gen, drop_if_stale, d)
        self.start()
        with self._cv:
            self._q.append(t)
            n = len(self._q) + (self._current is not None)
            self._cv.notify()
        self._changed(n, what)
        return t

    def stale_reason(self) -> Optional[str]:
        """Why the command running on this thread must not move the machine or fire the beam
        any more (a Stop, halt, fault, disarm, connect or disconnect since it was queued), or
        None.  Off the worker (and for a command without a generation): None."""
        t = self.current()
        if t is None or t.gen is None or self._stale is None:
            return None
        why = self._stale(t.gen)
        return f"{t.what}: {why}" if why else None

    def _changed(self, n: int, what: str) -> None:
        cb = self.on_change
        if cb is not None:
            try:
                cb(n, what)
            except Exception:  # noqa: BLE001
                log.debug("on_change", exc_info=True)

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._q:
                    self._cv.wait()
                t = self._q.popleft()
                self._current = t
            self._local.ticket = t
            try:
                why = None
                if t.drop_if_stale and t.gen is not None and self._stale is not None:
                    why = self._stale(t.gen)
                if why:
                    t.dropped = why
                    if self.on_dropped is not None:
                        self.on_dropped(t)
                else:
                    t.result = t.fn()
            except BaseException as e:  # noqa: BLE001 - kept on the ticket, logged
                t.error = e
                log.exception("command %s failed", t.what)
            finally:
                self._local.ticket = None
                with self._cv:
                    self._current = None
                    n = len(self._q)
                    self.completed += 1
                    self._cv.notify_all()
                t._finish()
                self._changed(n, t.what)

    # ------------------------------------------------------------------ waiting
    def wait_idle(self, timeout: float = 30.0, pump: Optional[Callable[[], None]] = None) -> bool:
        """Block until every queued command ended (tests, scripting, connect after a queued
        disconnect).  ``pump`` runs while waiting (the GUI thread passes its event processing:
        a command may wait for an answer from it, e.g. a confirmation)."""
        if self.on_worker():
            return True
        end = time.monotonic() + timeout
        while True:
            with self._cv:
                if not self._q and self._current is None:
                    break
                left = end - time.monotonic()
                if left <= 0:
                    return False
                if pump is None:
                    self._cv.wait(left)
                    continue
            pump()
            with self._cv:
                if self._q or self._current is not None:
                    self._cv.wait(min(0.005, max(0.0, end - time.monotonic())))
        if pump is not None:
            pump()                     # deliver the completion callbacks / events now
        return True


def card_command(what: str, *, drop_if_stale: Any = False):
    """Mark a UI controller method as an operator card command (see the module doc).

    ``drop_if_stale``: True (or a predicate of the call's arguments) for a command that moves
    the machine or turns something on - dropped when a Stop or halt happened since the
    operator gave it; False for off-commands, which always run."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *a, **kw):
            if not self._offload():
                return fn(self, *a, **kw)
            drop = drop_if_stale(*a, **kw) if callable(drop_if_stale) else bool(drop_if_stale)
            kw = {k: (self._ui_marshal(v) if k == "confirm" and callable(v) else v) for k, v in kw.items()}
            return self._commands.submit(what, functools.partial(fn, self, *a, **kw),
                                         gen=self.start_generation(), drop_if_stale=drop)
        wrapper.card_command = what          # type: ignore[attr-defined]
        return wrapper
    return deco


__all__ = ["CommandExecutor", "CommandTicket", "card_command"]
