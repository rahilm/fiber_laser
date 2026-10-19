"""Logging for the application: rotating files, audit JSON lines, crash handler.

:func:`setup_logging` (called by ``mlaser.ui.__main__``):

* ``mlaser.log`` in the user log directory (``platformdirs.user_log_dir("mlaser")``, override
  with ``$MLASER_LOG_DIR``): every ``mlaser.*`` record at INFO and above, rotating 5 x 5 MB;
* ``audit.log`` next to it: the ``mlaser.audit`` records (:mod:`.audit`) as JSON lines, rotating
  10 x 5 MB;
* ``crash.log``: :mod:`faulthandler` output (segfaults, aborts, also ``SIGUSR1`` dumps the
  stacks on POSIX) and the tracebacks of uncaught exceptions (:mod:`.lifecycle` installs the
  hooks).

Safe to call more than once (handlers are installed once per directory).
"""
from __future__ import annotations

import faulthandler
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Optional

_INSTALLED: dict[str, list[logging.Handler]] = {}
_CRASH_FILE = None


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


def setup_logging(directory: Optional[os.PathLike] = None, level: int = logging.INFO,
                  console: bool = False) -> Path:
    """Install the file handlers on the ``mlaser`` logger; returns the log directory."""
    global _CRASH_FILE
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
    for h in handlers:
        root.addHandler(h)
    root.setLevel(min(level, logging.INFO))
    logging.getLogger("mlaser.audit").setLevel(logging.INFO)
    _INSTALLED[key] = handlers
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
    hs = _INSTALLED.pop(str(d.resolve()), [])
    root = logging.getLogger("mlaser")
    for h in hs:
        root.removeHandler(h)
        h.close()


__all__ = ["setup_logging", "teardown_logging", "log_dir", "crash_file", "JsonAuditFormatter"]
