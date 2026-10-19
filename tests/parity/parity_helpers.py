"""Parity tests: the ORIGINAL Mlaser DLLs run under Wine vs the rebuild.

The harness lives outside the package, in ``parity/`` next to the checkout (``$MLASER_WORKSPACE/parity``; override with
MLASER_PARITY_DIR).  These tests run the comparisons when Wine, the built harnesses and the
copy of the original install are available, and skip cleanly otherwise.  Set
MLASER_PARITY=0 to skip them even when Wine is present.

Each test runs one comparison area and
* fails on harness errors and on discrepancies NOT listed in compare/known_diffs.py
  (a regression or a new discrepancy);
* reports listed (known) discrepancies as warnings, and warns when a listed one no longer
  reproduces;
* asserts the cases that are known to match exactly (e.g. the frame encoder) still match.
See docs/re/parity.md.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PARITY_DIR = Path(os.environ.get("MLASER_PARITY_DIR") or Path(os.environ.get("MLASER_WORKSPACE") or Path(__file__).resolve().parents[2].parent) / "parity")
# The harness imports the rebuild from MLASER_REBUILD; point it at THIS checkout so worktrees and CI
# never silently compare another copy of the code.
os.environ.setdefault("MLASER_REBUILD", str(Path(__file__).resolve().parents[2]))
COMPARE = PARITY_DIR / "compare"


def _import_compare():
    if os.environ.get("MLASER_PARITY", "1") == "0":
        pytest.skip("MLASER_PARITY=0")
    if not COMPARE.is_dir():
        pytest.skip(f"parity harness not found at {COMPARE}")
    if str(COMPARE) not in sys.path:
        sys.path.insert(0, str(COMPARE))
    import parity_lib  # noqa: F401
    return parity_lib


_cache: dict = {}


def run_area(module: str, needs_wine: bool = True):
    """Run compare/<module>.run() once per session and return its Result list."""
    lib = _import_compare()
    if needs_wine:
        ok, why = lib.wine_available()
        if not ok:
            pytest.skip(f"Wine parity unavailable: {why}")
    if module not in _cache:
        mod = __import__(module)
        _cache[module] = mod.run()
    return _cache[module]


def assert_no_new_discrepancies(results):
    """Fail on errors and on discrepancies not listed in compare/known_diffs.py.

    Known discrepancies are reported as warnings; a known one that now matches is reported
    too (so the list can be pruned).
    """
    import warnings

    import known_diffs
    new, errors = [], []
    for r in results:
        if r.status == "error":
            errors.append(f"{r.area} / {r.case}: {r.note[:300]}")
        elif r.status == "differ":
            k = known_diffs.lookup(r)
            if k:
                warnings.warn(f"known parity discrepancy {k['id']} ({r.area} / {r.case}): {k['summary']}")
            else:
                new.append(f"{r.area} / {r.case}: {r.metric} {r.note} -> {r.responsible}")
    for k in known_diffs.fixed(results):
        warnings.warn(f"known discrepancy {k} no longer reproduces; remove it from known_diffs.py")
    assert not errors, "harness errors:\n" + "\n".join(errors)
    assert not new, "new parity discrepancies:\n" + "\n".join(new)
