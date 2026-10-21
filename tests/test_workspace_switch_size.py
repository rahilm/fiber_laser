"""A workspace switch keeps the window's size when the new page fits it (docs/dev/RESPONSIVE.md).

The page wrapper's minimum is capped by the room the screen leaves (``FitArea._cap``), but the
window first asked for the new page's minimum before the wrapper knew that room and grew to the
uncapped need; it then stayed larger (the Process workspace took the window from 1280 × 800 to
1280 × 892 on the Windows CI runner and to 818 on macOS: test_shell's
test_window_keeps_its_size_with_every_workspace).  Here a screen of 1280 × 700 makes the Process
page's need larger than the room at the default font."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

SCRIPT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from PySide6.QtWidgets import QApplication
app = QApplication([])
from wiring import real_window
w = real_window(user_mode="engineer")
w.resize(1280, 700)
w.show()
for _ in range(5):
    app.processEvents()
out = {}
for ws in w.shell.host.visible_ids():
    w.show_workspace(ws)
    for _ in range(8):
        app.processEvents()
    m = w.minimumSizeHint().expandedTo(w.minimumSize())
    out[ws] = [w.width(), w.height(), m.width(), m.height()]
print(json.dumps(out))
sys.stdout.flush()
"""


def test_switch_keeps_the_window_size_on_a_small_screen(tmp_path):
    (tmp_path / "screen.json").write_text(json.dumps(
        {"screens": [{"name": "s", "x": 0, "y": 0, "width": 1280, "height": 700, "logicalDpi": 96, "dpr": 1}]}),
        encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen:configfile=screen.json",
               MLASER_CONFIG_DIR=str(tmp_path / "cfg"), MLASER_APP_DIR=str(tmp_path / "app"),
               MLASER_DATA_DIR=str(tmp_path / "data"), MLASER_NO_FIRSTRUN="1")
    env.pop("MLASER_LAYOUT_STYLE", None)
    r = subprocess.run([sys.executable, "-c", SCRIPT, str(Path(__file__).parent.resolve())], env=env,
                       capture_output=True, text=True, timeout=600, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr[-3000:]
    sizes = json.loads(r.stdout.strip().splitlines()[-1])
    bad = {ws: s for ws, s in sizes.items() if (s[0], s[1]) != (max(1280, s[2]), max(700, s[3]))}
    assert not bad, bad
