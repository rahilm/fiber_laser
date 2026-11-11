"""``--smoke-test`` ends the GUI also when the start takes longer than its time.

The launcher armed its timer before the window was built.  On a slow start (Windows and macOS
CI) the time was up while the start still ran: ``app.exit()`` came before the main loop (it does
nothing then, or ends only a nested wait) and the app ran until it was killed
(test_runtime_dependencies timed out after 200 s).  Here the start is made slower than the
smoke time on purpose: the window's ``show`` sleeps and processes the events, as a slow start
does."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r"""
import sys, time
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
QDialog.exec = lambda self, *a: 0
QMessageBox.exec = lambda self, *a: int(QMessageBox.No)
from mlaser.ui import mainwindow
orig = mainwindow.MainWindow.show
def slow_show(self):
    orig(self)
    time.sleep(0.6)                 # the smoke time (0.2 s) is up during the start
    QApplication.processEvents()
mainwindow.MainWindow.show = slow_show
from mlaser.__main__ import main
sys.exit(main(["--demo", "--smoke-test", "0.2", "--new-instance"]))
"""


def test_smoke_test_ends_after_a_start_slower_than_its_time(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    for k, sub in (("MLASER_CONFIG_DIR", "cfg"), ("MLASER_DATA_DIR", "data"), ("MLASER_LOG_DIR", "log"),
                   ("MLASER_APP_DIR", "app"), ("MLASER_RUNTIME_DIR", "run")):
        env[k] = str(tmp_path / sub)
    env.update(QT_QPA_PLATFORM="offscreen", MLASER_NO_FIRSTRUN="1", MLASER_SINGLE_INSTANCE="0",
               MLASER_CARD_LOCK="0", MLASER_NO_INHIBIT="1", PYTHONPATH=str(ROOT))
    err = tmp_path / "err.txt"
    with open(err, "wb") as fe:
        p = subprocess.Popen([sys.executable, "-c", SCRIPT], cwd=str(tmp_path), env=env,
                             stdout=subprocess.DEVNULL, stderr=fe)
        try:
            rc = p.wait(timeout=150)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            pytest.fail("the smoke test did not end: " + err.read_text("utf-8", "replace")[-3000:])
    assert rc == 0, err.read_text("utf-8", "replace")[-3000:]
