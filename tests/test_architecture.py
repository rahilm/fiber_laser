"""The layering rules of docs/dev/ARCHITECTURE.md ([tool.importlinter] in pyproject.toml).

CI runs ``lint-imports`` as its own job; this test runs the same contracts locally so a layering
violation shows up in the default suite.  Skipped when import-linter is not installed.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_import_contracts():
    pytest.importorskip("importlinter")
    exe = shutil.which("lint-imports", path=str(pathlib.Path(sys.executable).parent)) or shutil.which("lint-imports")
    if exe is None:
        pytest.skip("lint-imports not on PATH")
    r = subprocess.run([exe, "--no-cache"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
