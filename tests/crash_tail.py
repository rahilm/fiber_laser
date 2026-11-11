"""What a test that starts the app as a child process reports when the child died: the end of
its ``crash.log`` (the app's faulthandler writes the Python and C stacks of every thread there,
not to stderr: ``mlaser.machine.logsetup``)."""
from __future__ import annotations

from pathlib import Path


def crash_tail(log_dir: Path, limit: int = 8000) -> str:
    """The last ``limit`` characters of ``log_dir/crash.log`` with a heading, or ''."""
    try:
        text = (Path(log_dir) / "crash.log").read_text("utf-8", "replace")
    except OSError:
        return ""
    return f"\n--- crash.log:\n{text[-limit:]}" if text.strip() else ""
