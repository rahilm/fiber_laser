"""Logging for the application: rotating files, audit JSON lines, crash handler.

:func:`setup_logging` (called by ``mlaser.ui.__main__``):

* ``mlaser.log`` in the user log directory (``platformdirs.user_log_dir("mlaser")``, override
  with ``$MLASER_LOG_DIR``): every ``mlaser.*`` record at INFO and above, rotating 5 x 5 MB;
* ``audit.log`` next to it: the ``mlaser.audit`` records (:mod:`.audit`) as JSON lines, rotating
  10 x 5 MB;
* ``crash.log``: :mod:`faulthandler` output (segfaults, aborts, also ``SIGUSR1`` dumps the
  stacks on POSIX) and the tracebacks of uncaught exceptions (:mod:`.lifecycle` installs the
  hooks).

The file handlers sit behind a :class:`logging.handlers.QueueHandler`: a record is formatted
on the calling thread (``record.created`` is the time of the call) and put on a queue; one
listener thread (``mlaser-log``) writes the files, in order.  No other thread touches a log
file, so a stalled disk (an antivirus-held write, a rollover on a slow disk) never delays a
halt command or the GUI thread (review fw F2, docs/SAFETY.md §4a).  The queue is drained at
normal exit (atexit, then the handlers are written directly), in the uncaught-exception hooks
and on :func:`flush_logging`; a hard kill can lose the records still queued.

Safe to call more than once (handlers are installed once per directory).
"""
from __future__ import annotations

import faulthandler
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Optional

_INSTALLED: dict[str, list[logging.Handler]] = {}
_ASYNC: dict[str, "_WriteBehind"] = {}
_CRASH_FILE = None
_ATEXIT = False


def log_dir() -> Path:
    env = os.environ.get("MLASER_LOG_DIR")
    if env:
        return Path(env)
    try:
        import platformdirs
        return Path(platformdirs.user_log_dir("mlaser", appauthor=False))
    except ImportError:  # pragma: no cover - platformdirs is a dependency
        return Path.home() / ".mlaser" / "log"


class JsonAuditFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rec = dict(getattr(record, "audit", None) or {"event": "log", "message": record.getMessage()})
        rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)) + \
            f".{int(record.msecs):03d}"
        rec["level"] = record.levelname
        rec["thread"] = record.threadName
        return json.dumps(rec, default=str, ensure_ascii=False)


class _AuditOnly(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return hasattr(record, "audit")


class _WriteBehind:
    """The ``mlaser`` logger's file handlers behind a queue and one writer thread."""

    def __init__(self, root: logging.Logger, handlers: list[logging.Handler]) -> None:
        self.root = root
        self.handlers = handlers
        self.q: queue.Queue = queue.Queue(-1)
        self.qh = logging.handlers.QueueHandler(self.q)
        self.listener = logging.handlers.QueueListener(self.q, *handlers, respect_handler_level=True)
        self.direct = False
        self._lock = threading.Lock()

    def start(self) -> None:
        self.listener.start()
        t = getattr(self.listener, "_thread", None)
        if t is not None:
            t.name = "mlaser-log"
        self.root.addHandler(self.qh)

    def flush(self, timeout: float) -> bool:
        """Wait (bounded) until every queued record is written; True when it was."""
        end = time.monotonic() + max(0.0, timeout)
        while self.q.unfinished_tasks:
            if time.monotonic() >= end or getattr(self.listener, "_thread", None) is None:
                return not self.q.unfinished_tasks
            time.sleep(0.002)
        for h in self.handlers:
            try:
                h.flush()
            except Exception:  # noqa: BLE001 - a flush must never raise into a crash path
                pass
        return True

    def stop(self, timeout: float, direct: bool) -> bool:
        """Drain and stop the writer thread.  ``direct``: write later records on the calling
        thread (interpreter exit: the halts and audits of the exit hooks still reach the
        files).  A writer that does not drain in ``timeout`` is left alone (a daemon thread)."""
        with self._lock:
            if self.direct or getattr(self.listener, "_thread", None) is None:
                return True
            drained = self.flush(timeout)
            if drained:
                self.listener.stop()
                if direct:
                    self.root.removeHandler(self.qh)
                    for h in self.handlers:
                        self.root.addHandler(h)
                    self.direct = True
            return drained

    def remove(self) -> None:
        self.stop(5.0, direct=False)
        self.root.removeHandler(self.qh)
        for h in self.handlers:
            self.root.removeHandler(h)
            h.close()


def flush_logging(timeout: float = 1.0) -> bool:
    """Write every queued log and audit record now (bounded wait; never raises).  Called by
    the crash hooks; True when everything was written."""
    ok = True
    for wb in list(_ASYNC.values()):
        try:
            ok = wb.flush(timeout) and ok
        except Exception:  # noqa: BLE001
            ok = False
    return ok


def _at_exit() -> None:
    for wb in list(_ASYNC.values()):
        try:
            wb.stop(5.0, direct=True)
        except Exception:  # noqa: BLE001
            pass


def setup_logging(directory: Optional[os.PathLike] = None, level: int = logging.INFO,
                  console: bool = False) -> Path:
    """Install the file handlers on the ``mlaser`` logger; returns the log directory."""
    global _CRASH_FILE, _ATEXIT
    d = Path(directory) if directory else log_dir()
    d.mkdir(parents=True, exist_ok=True)
    key = str(d.resolve())
    root = logging.getLogger("mlaser")
    if key in _INSTALLED:
        return d
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s")
    main = logging.handlers.RotatingFileHandler(d / "mlaser.log", maxBytes=5_000_000, backupCount=5,
                                                encoding="utf-8")
    main.setFormatter(fmt)
    main.setLevel(level)
    aud = logging.handlers.RotatingFileHandler(d / "audit.log", maxBytes=5_000_000, backupCount=10,
                                               encoding="utf-8")
    aud.setFormatter(JsonAuditFormatter())
    aud.addFilter(_AuditOnly())
    aud.setLevel(logging.DEBUG)
    handlers: list[logging.Handler] = [main, aud]
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
        ch.setLevel(logging.WARNING)
        handlers.append(ch)
    wb = _WriteBehind(root, handlers)
    wb.start()
    root.setLevel(min(level, logging.INFO))
    logging.getLogger("mlaser.audit").setLevel(logging.INFO)
    _INSTALLED[key] = handlers
    _ASYNC[key] = wb
    if not _ATEXIT:
        import atexit
        atexit.register(_at_exit)
        _ATEXIT = True
    try:
        _CRASH_FILE = open(d / "crash.log", "a", encoding="utf-8")   # noqa: SIM115 - process lifetime
        _CRASH_FILE.write(f"\n=== mlaser started {time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()}\n")
        _CRASH_FILE.flush()
        faulthandler.enable(_CRASH_FILE, all_threads=True)
        if hasattr(faulthandler, "register") and hasattr(__import__("signal"), "SIGUSR1"):
            import signal
            faulthandler.register(signal.SIGUSR1, _CRASH_FILE, all_threads=True)
    except (OSError, ValueError, RuntimeError):
        pass
    logging.getLogger("mlaser").info("logging to %s", d)
    return d


def crash_file():
    """The open crash.log (or None) for tracebacks of uncaught exceptions."""
    return _CRASH_FILE


def teardown_logging(directory: Optional[os.PathLike] = None) -> None:
    """Remove the handlers installed for ``directory`` (tests)."""
    d = Path(directory) if directory else log_dir()
    key = str(d.resolve())
    hs = _INSTALLED.pop(key, [])
    wb = _ASYNC.pop(key, None)
    if wb is not None:
        wb.remove()
        return
    root = logging.getLogger("mlaser")
    for h in hs:
        root.removeHandler(h)
        h.close()


__all__ = ["setup_logging", "teardown_logging", "flush_logging", "log_dir", "crash_file", "JsonAuditFormatter"]
