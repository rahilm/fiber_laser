"""``swallow(log, what)``: the one way to ignore a failure on purpose (review 2026-11-09, arch A14).

A broad ``except Exception: pass`` hides a failing optional feature until someone notices it is
missing.  New code that must carry on after a failure writes::

    with swallow(log, "the plugin's progress callback"):
        callback(value)

and the failure is logged (by default at debug level, with the traceback), so a bug report's log
shows it.  Not for a halt, disarm or "off" path: those log at error level themselves and never
depend on this helper.  Every other broad handler states its reason after ``noqa: BLE001``
(``tests/test_broad_excepts.py``).
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def swallow(log: logging.Logger, what: str, level: int = logging.DEBUG) -> Iterator[None]:
    """Run the block; an ``Exception`` in it is logged at ``level`` with its traceback ("<what>
    failed (ignored)") and not raised.  ``BaseException`` (KeyboardInterrupt, SystemExit)
    passes through."""
    try:
        yield
    except Exception:  # noqa: BLE001 - the purpose of this helper: logged, then ignored
        log.log(level, "%s failed (ignored)", what, exc_info=True)


__all__ = ["swallow"]
