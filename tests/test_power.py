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


def test_linux_inhibit_command_prefers_logind():
    have = {"systemd-inhibit": "/usr/bin/systemd-inhibit", "gnome-session-inhibit": "/usr/bin/gnome-session-inhibit"}
    cmd = PW.linux_inhibit_command("a laser job is running", which=have.get)
    assert cmd[0] == "systemd-inhibit" and "--what=sleep:idle" in cmd and "--mode=block" in cmd
    cmd = PW.linux_inhibit_command("x", which={"gnome-session-inhibit": "/x"}.get)
    assert cmd[0] == "gnome-session-inhibit" and "suspend:idle" in cmd
    assert PW.linux_inhibit_command("x", which=lambda n: None) is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_linux_holds_an_inhibitor_child_while_on(monkeypatch, tmp_path):
    """keep_awake(True) starts one inhibitor child for the job (one attempt, not one per tick),
    keep_awake(False) ends it."""
    script = tmp_path / "systemd-inhibit"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(0o755)
    monkeypatch.delenv("MLASER_NO_INHIBIT", raising=False)
    monkeypatch.setattr(PW.shutil, "which", lambda n: str(script) if n == "systemd-inhibit" else None)
    calls = []
    real = PW.subprocess.Popen
    monkeypatch.setattr(PW.subprocess, "Popen", lambda cmd, **k: calls.append(cmd) or real([str(script)], **k))
    try:
        assert PW.keep_awake(True) is True
        proc = PW._linux_inhibitor
        assert PW.keep_awake(True) is True and len(calls) == 1
        assert calls[0][1] == "--what=sleep:idle"
    finally:
        assert PW.keep_awake(False) is False
    assert proc.poll() is not None and PW._linux_inhibitor is None
