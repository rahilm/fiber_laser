"""Golden artefacts (tools/make_goldens.py): today's behaviour, frozen.

Each group is recomputed from the current code and compared with tests/golden/<group>.json.
A difference means behaviour changed: either fix the change, or regenerate the goldens with
``python tools/make_goldens.py`` and justify the diff in review (docs/ROADMAP.md, the contract).

The goldens are bit-exact on the interpreter they were generated with (manifest.json): Python
3.11's ``sum()`` adds floats differently from 3.12's compensated ``sum()``, and a few random jobs
then plan differently.  On another minor version the test is skipped unless
``MLASER_GOLDEN_ANY_PYTHON=1``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
from pathlib import Path

import pytest

pytestmark = pytest.mark.golden

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("make_goldens", ROOT / "tools" / "make_goldens.py")
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)

MANIFEST = json.loads((G.GOLDEN / "manifest.json").read_text(encoding="utf-8"))


def _reference_python() -> str:
    return ".".join(MANIFEST["generated_with"]["python"].split(".")[:2])


@pytest.fixture(scope="module")
def font_ok():
    return G.text_font_fingerprint() == MANIFEST.get("text_font")


@pytest.mark.parametrize("group", G.GROUPS)
def test_golden(group, font_ok):
    here = ".".join(platform.python_version().split(".")[:2])
    if here != _reference_python() and os.environ.get("MLASER_GOLDEN_ANY_PYTHON") != "1":
        pytest.skip(f"goldens are bit-exact on Python {_reference_python()}, this is {here}")
    diffs, _skipped = G.compare_group(group, font_ok)
    assert not diffs, f"{len(diffs)} golden differences in {group}:\n" + "\n".join(diffs[:30])


def test_golden_folder_is_compact():
    size = sum(p.stat().st_size for p in G.GOLDEN.iterdir() if p.is_file())
    assert size < 20 * 1024 * 1024, size
    assert MANIFEST["groups"]["jobs_random"] == G.N_RANDOM
