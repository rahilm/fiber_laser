"""Test setup: the plugin package and the test helpers on sys.path (no install needed)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import pytest  # noqa: E402


@pytest.fixture
def fake():
    from fake_onshape import FakeOnshape
    with FakeOnshape() as f:
        yield f


@pytest.fixture
def ctx(tmp_path):
    from stubctx import StubCtx
    return StubCtx(tmp_path)
