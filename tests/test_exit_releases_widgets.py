"""After the main loop the app deletes its window and every other parentless top-level widget
itself, before the interpreter's exit (Python 3.14 CI: left to PySide6's teardown with the
QApplication, the smoke test's process aborted with "double free or corruption", exit code -6,
after it had ended cleanly).  Here: no top-level widget is left when ``main`` returns, and the
process ends with 0."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from crash_tail import crash_tail

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r"""
import sys
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
QDialog.exec = lambda self, *a: 0
QMessageBox.exec = lambda self, *a: int(QMessageBox.No)
from mlaser.__main__ import main
rc = main(["--demo", "--smoke-test", "0.5", "--new-instance"])
left = [type(w).__name__ for w in QApplication.topLevelWidgets()]
print("LEFT", len(left), left[:10], file=sys.stderr, flush=True)
sys.exit(rc or (3 if left else 0))
"""


def test_main_deletes_its_top_level_widgets_before_the_exit(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    for k, sub in (("MLASER_CONFIG_DIR", "cfg"), ("MLASER_DATA_DIR", "data"), ("MLASER_LOG_DIR", "log"),
                   ("MLASER_APP_DIR", "app"), ("MLASER_RUNTIME_DIR", "run")):
        env[k] = str(tmp_path / sub)
    env.update(QT_QPA_PLATFORM="offscreen", MLASER_NO_FIRSTRUN="1", MLASER_SINGLE_INSTANCE="0",
               MLASER_CARD_LOCK="0", MLASER_NO_INHIBIT="1", PYTHONPATH=str(ROOT), PYTHONFAULTHANDLER="1")
    p = subprocess.run([sys.executable, "-c", SCRIPT], cwd=str(tmp_path), env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=150)
    err = p.stderr.decode("utf-8", "replace")
    assert "LEFT 0 " in err and p.returncode == 0, (f"exit code {p.returncode}: {err[-3000:]}"
                                                    + crash_tail(tmp_path / "log"))
