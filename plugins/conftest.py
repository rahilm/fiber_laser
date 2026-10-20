"""Shared test set-up of the project's own plugins (``plugins/<name>/tests``), run by CI with the
plugin installed editable (.github/workflows/ci.yml, "Plugin tests").

* the checkout's ``tests/`` directory is importable, so a plugin's tests can use the core's
  helpers (``from wiring import real_window``, ``plugin_fixtures``);
* every test gets its own configuration, data and app directories (as tests/conftest.py does
  for the core), so nothing is written to the user's real mlaser directories.

A plugin's own ``tests/conftest.py`` still runs after this one.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TESTS = Path(__file__).resolve().parent.parent / "tests"
if TESTS.is_dir() and str(TESTS) not in sys.path:
    sys.path.append(str(TESTS))


@pytest.fixture(autouse=True)
def _isolated_mlaser_dirs(tmp_path_factory, monkeypatch):
    for var, name in (("MLASER_CONFIG_DIR", "config"), ("MLASER_DATA_DIR", "data"), ("MLASER_APP_DIR", "app")):
        monkeypatch.setenv(var, str(tmp_path_factory.mktemp(name)))
