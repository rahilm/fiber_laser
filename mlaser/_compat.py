"""Old import paths kept after the Wave 1 structure moves (docs/dev/ARCHITECTURE.md).

A module that moved leaves a two-line shim at its old path::

    from mlaser._compat import alias_module
    alias_module(__name__, "mlaser.protocol.frames")

:func:`alias_module` imports the new module and registers it under the old name as well, so
``import mlaser.controller.frames`` returns *the same module object* as
``import mlaser.protocol.frames``.  Private names, module globals and monkeypatching keep
working through either path, which a ``from new import *`` re-export would not guarantee.

The first import of an old path emits a :class:`DeprecationWarning` naming the new path.  The
warning is attributed to the code that imported the old path (frames of this package and of the
import machinery are skipped).  The test suite silences it (pyproject.toml, ``filterwarnings``);
``python -W error::DeprecationWarning`` shows where a script still uses an old path.
"""
from __future__ import annotations

import importlib
import os
import sys
import warnings
from types import ModuleType

_PKG_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep
_SKIP = (_PKG_DIR, "<frozen importlib")


def moved_message(old: str, new: str) -> str:
    return f"{old} moved to {new} (Wave 1); import it from the new path"


def warn_moved(old: str, new: str) -> None:
    warnings.warn(moved_message(old, new), DeprecationWarning, stacklevel=2,
                  skip_file_prefixes=_SKIP)


def alias_module(old: str, new: str, *, deprecated: bool = True) -> ModuleType:
    """Make the module named ``old`` an alias of the module ``new`` (see the module docstring).

    Call it from the body of the old module; ``old`` is that module's ``__name__``.  With
    ``deprecated=False`` the old path stays a supported name for the same module (no warning).
    """
    mod = importlib.import_module(new)
    if deprecated:
        warn_moved(old, new)
    sys.modules[old] = mod
    return mod
