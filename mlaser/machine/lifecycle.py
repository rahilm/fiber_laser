"""Process-exit and crash paths that must leave the machine safe (docs/SAFETY.md §3).

Every connected controller registers itself (:func:`register`, weakly referenced).  The hooks
installed by :func:`install` call ``safe_halt`` on each of them:

* ``atexit`` (normal interpreter exit, ``sys.exit``);
* ``SIGINT`` / ``SIGTERM`` (and ``SIGBREAK``/``SIGHUP`` where they exist): halt, then the
  previous handler runs (``KeyboardInterrupt`` / exit);
* ``sys.excepthook`` and ``threading.excepthook``: an uncaught exception anywhere latches a
  FAULT (``raise_fault``, which halts) and is written to the log and ``crash.log``.  Qt routes
  exceptions raised in slots through ``sys.excepthook`` too.

A SIGKILL or a power cut cannot be caught: that is what the streaming child's parent check
and the external "PC alive" watchdog relay are for.
"""
from __future__ import annotations

import atexit
import logging
import signal
import sys
import threading
import traceback
import weakref
from typing import Any

from .audit import audit

log = logging.getLogger("mlaser.lifecycle")

_targets: "weakref.WeakSet[Any]" = weakref.WeakSet()
_lock = threading.Lock()
_installed = False
_prev_signal: dict[int, Any] = {}
_prev_excepthook = None
_prev_thread_hook = None
calls: list[tuple[str, str]] = []           # (path, reason): diagnostics and tests


def register(ctl: Any) -> None:
    with _lock:
        _targets.add(ctl)


def unregister(ctl: Any) -> None:
    with _lock:
        _targets.discard(ctl)


def targets() -> list:
    with _lock:
        return list(_targets)


def halt_all(path: str, reason: str, fault: bool = False) -> int:
    """``safe_halt`` (or ``raise_fault`` with ``fault``) on every registered controller."""
    n = 0
    calls.append((path, reason))
    del calls[:-50]
    for ctl in targets():
        try:
            if fault and hasattr(ctl, "raise_fault"):
                ctl.raise_fault(reason)
            else:
                ctl.safe_halt(reason)
            n += 1
        except Exception:  # noqa: BLE001 - keep halting the others
            log.exception("safe_halt from %s failed for %r", path, ctl)
    if n:
        audit("halt_all", logging.WARNING, path=path, reason=reason, controllers=n)
    return n


# ------------------------------------------------------------------------------ hooks
def _atexit() -> None:
    halt_all("atexit", "interpreter exit")


def _on_signal(signum, frame) -> None:
    name = signal.Signals(signum).name if signum in signal.Signals._value2member_map_ else str(signum)
    halt_all("signal", f"signal {name}")
    prev = _prev_signal.get(signum)
    if callable(prev):
        prev(signum, frame)
    elif prev == signal.SIG_DFL or prev is None:
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + int(signum))


def _write_crash(text: str) -> None:
    try:
        from .logsetup import crash_file
        f = crash_file()
        if f is not None:
            f.write(text)
            f.flush()
    except Exception:  # noqa: BLE001
        pass


def _excepthook(exc_type, exc, tb) -> None:
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    log.critical("uncaught exception:\n%s", text)
    _write_crash(text)
    audit("crash", logging.CRITICAL, thread="main", error=f"{exc_type.__name__}: {exc}")
    if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        halt_all("excepthook", f"uncaught exception: {exc_type.__name__}: {exc}", fault=True)
    if _prev_excepthook is not None and _prev_excepthook is not _excepthook:
        _prev_excepthook(exc_type, exc, tb)


def _thread_hook(args) -> None:
    if args.exc_type is SystemExit:
        return
    name = getattr(args.thread, "name", "?")
    text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    log.critical("uncaught exception in thread %s:\n%s", name, text)
    _write_crash(f"thread {name}:\n{text}")
    audit("crash", logging.CRITICAL, thread=name, error=f"{args.exc_type.__name__}: {args.exc_value}")
    halt_all("threading.excepthook",
             f"uncaught exception in thread {name}: {args.exc_type.__name__}: {args.exc_value}", fault=True)
    if _prev_thread_hook is not None and _prev_thread_hook is not _thread_hook:
        _prev_thread_hook(args)


def install(signals: bool = True) -> None:
    """Install the atexit, signal and exception hooks once (idempotent)."""
    global _installed, _prev_excepthook, _prev_thread_hook
    if _installed:
        return
    _installed = True
    atexit.register(_atexit)
    _prev_excepthook = sys.excepthook
    sys.excepthook = _excepthook
    _prev_thread_hook = threading.excepthook
    threading.excepthook = _thread_hook
    if signals and threading.current_thread() is threading.main_thread():
        for nm in ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"):
            sig = getattr(signal, nm, None)
            if sig is None:
                continue
            try:
                _prev_signal[sig] = signal.signal(sig, _on_signal)
            except (OSError, ValueError, RuntimeError):
                pass


def uninstall() -> None:
    """Restore the previous hooks (tests)."""
    global _installed
    if not _installed:
        return
    _installed = False
    try:
        atexit.unregister(_atexit)
    except Exception:  # noqa: BLE001
        pass
    if sys.excepthook is _excepthook and _prev_excepthook is not None:
        sys.excepthook = _prev_excepthook
    if threading.excepthook is _thread_hook and _prev_thread_hook is not None:
        threading.excepthook = _prev_thread_hook
    if threading.current_thread() is threading.main_thread():
        for sig, prev in list(_prev_signal.items()):
            try:
                signal.signal(sig, prev if prev is not None else signal.SIG_DFL)
            except (OSError, ValueError, RuntimeError):
                pass
    _prev_signal.clear()


def installed() -> bool:
    return _installed


__all__ = ["register", "unregister", "targets", "halt_all", "install", "uninstall", "installed", "calls"]
