"""mlaser._power: App Nap opt-out (macOS) and keep-awake during jobs (macOS, Windows); a no-op
elsewhere, never raising."""
import sys

import pytest

from mlaser import _power as PW


def test_calls_never_raise_and_are_idempotent():
    a = PW.app_activity()
    assert PW.app_activity() == a
    on = PW.keep_awake(True)
    assert PW.keep_awake(True) == on
    assert PW.keep_awake(False) is False
    if sys.platform not in ("darwin", "win32"):
        assert a is False and on is False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_macos_holds_the_activities():
    assert PW.app_activity() is True               # App Nap off for the process
    assert PW.keep_awake(True) is True and PW._job_token
    assert PW.keep_awake(False) is False and PW._job_token is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_execution_state():
    assert PW.keep_awake(True) is True
    assert PW.keep_awake(False) is False


def test_safety_ui_refresh_follows_the_job_state():
    """SafetyUI.refresh (the window's 100 ms tick) passes job_running() to keep_awake."""
    import inspect

    import mlaser.ui.safety_ui as SU
    src = inspect.getsource(SU.SafetyUI.refresh)
    assert "running = job_running(self.w)" in src and "keep_awake(running)" in src
    assert src.index("running = job_running(self.w)") < src.index("keep_awake(running)")
