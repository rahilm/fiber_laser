"""The compiled C kernel of the per-sample emitter loop (``_emitter``, docs/dev/NATIVE_KERNEL.md).

``_emitter`` is an optional setuptools extension of the mlaser distribution (setup.py): it is
built whenever a C compiler is available and is missing otherwise.  Nothing calls it directly:
:mod:`mlaser.parity_ports.profiles.native` wraps it, checks it against the Python port at first
use and selects it for the default ParityProfile (``MLASER_NATIVE``).  This package imports
nothing from mlaser.
"""

from __future__ import annotations

import importlib.util
from types import ModuleType
from typing import Optional

#: bumped whenever the C entry points or their argument tuples change
KERNEL_VERSION = 2

_emitter: Optional[ModuleType]
#: why the extension could not be imported ("" when it was)
IMPORT_ERROR = ""
if importlib.util.find_spec(f"{__name__}._emitter") is None:
    _emitter = None
    IMPORT_ERROR = "no compiled module: installed without a C compiler or with MLASER_BUILD_NATIVE=0"
else:
    try:
        from . import _emitter
    except ImportError as e:  # a broken build (wrong Python ABI, missing symbol, ...)
        _emitter = None
        IMPORT_ERROR = f"{type(e).__name__}: {e}"

__all__ = ["KERNEL_VERSION", "IMPORT_ERROR", "_emitter"]
