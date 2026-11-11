"""The tree stays at 0 ruff findings ([tool.ruff] in pyproject.toml).

CI's *lint* job runs the pinned ``uvx ruff@0.17.1 check .`` and fails on any finding; this test
runs the ruff of the dev environment the same way, so a new finding shows up in the default suite
before a push.  Skipped when ruff is not installed (no network: it never downloads ruff).
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _ruff() -> str | None:
    try:
        from ruff.__main__ import find_ruff_bin
        return find_ruff_bin()
    except (ImportError, FileNotFoundError):
        return shutil.which("ruff", path=str(pathlib.Path(sys.executable).parent)) or shutil.which("ruff")


def test_ruff_reports_no_findings():
    exe = _ruff()
    if exe is None:
        pytest.skip("ruff not installed")
    r = subprocess.run([exe, "check", "--no-cache", "--output-format=concise", "."], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
