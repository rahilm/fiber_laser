"""The original program's own files, for the tests that check them byte for byte.

The repository ships no file from the original's install (docs/ASSETS.md).  Tests that need
one read it from a local copy of the install: ``$MLASER_VENDOR_SAMPLES`` (the install's root
folder, the one with ``MainApp.exe``, ``File/``, ``Help/``), by default the extracted
download used during the reconstruction if it exists.  Without it those tests skip cleanly,
like the parity tests; the generated replacements in ``tests/data`` (``chf/``, ``appdir/``)
cover the same code paths in CI.  ``MLASER_VENDOR_SAMPLES=`` (empty) disables them.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pytest

DEFAULT = "/home/dev/scratch/mlaser/extracted/Mlaser-v0.0.1.51_SP1/Mlaser-v0.0.1.51_SP1"
#: the generated stand-in of the install folder (same relative layout)
OWN_APPDIR = Path(__file__).parent / "data" / "appdir"


def vendor_root() -> Optional[Path]:
    """The original install folder, or None when it is not available."""
    v = os.environ.get("MLASER_VENDOR_SAMPLES", DEFAULT)
    if not v:
        return None
    p = Path(v)
    return p if p.is_dir() else None


def vendor_path(rel: str) -> Optional[Path]:
    """``<install>/<rel>`` if present, else None (for ``skipif`` / parametrisation)."""
    root = vendor_root()
    if root is None:
        return None
    p = root / rel
    return p if p.exists() else None


def vendor_file(rel: str) -> Path:
    """``<install>/<rel>``; skips the calling test when it is not available."""
    p = vendor_path(rel)
    if p is None:
        pytest.skip(f"original install file {rel} not available (set MLASER_VENDOR_SAMPLES)")
    return p


def requires(rel: str):
    """``@requires("File/autosave.chf")``: skip marker for a test that needs that file."""
    return pytest.mark.skipif(vendor_path(rel) is None,
                              reason=f"original install file {rel} not available (MLASER_VENDOR_SAMPLES)")
