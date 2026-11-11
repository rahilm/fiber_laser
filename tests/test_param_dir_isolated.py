"""No test reads or writes the user's own machine-parameter folder (``~/.local/share/NexCut``,
``%LOCALAPPDATA%\\NexCut``): the suite gives every test its own (conftest ``_isolated_config``).
A parameter restore in one test had written its travel limit there, for the next run's tests
and for the application on the same PC to find."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

_SEEN: set[str] = set()


def _param_dir() -> Path:
    from mlaser.formats.backup import default_param_dir
    return default_param_dir().resolve()


def test_the_parameter_folder_is_the_tests_own(monkeypatch):
    d = _param_dir()
    assert d.is_relative_to(Path(tempfile.gettempdir()).resolve()), d
    # and not what the application would take without the suite's setting
    monkeypatch.delenv("MLASER_PARAM_DIR")
    assert _param_dir() != d


@pytest.mark.parametrize("n", range(3))
def test_each_test_gets_another_folder(n):
    d = str(_param_dir())
    assert d not in _SEEN
    _SEEN.add(d)
