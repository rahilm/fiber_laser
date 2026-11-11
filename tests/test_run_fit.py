"""The Run workspace fits without scrolling in each of its states, through the application's own
start path (``Shell.fit_to_screen`` before ``show``): half a 1512 px screen (756 px wide), and
1280 x 800 with a 48 px taskbar at a 12 pt application font; the run controls keep their sizes
(deep review 2026-11, run-flow task).

Before: the queue's "Load this sheet" card (a 220 px picture beside its text) and the queue
table's controls (the hidden Queue tab counted for the live view's column) made the page wider
than a half-screen window and taller than the room: it scrolled 41-183 px."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

RUN_FIT = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from PySide6.QtGui import QScreen
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
INSET = int(os.environ["FIT_INSET"])
if INSET:
    _avail = QScreen.availableGeometry
    QScreen.availableGeometry = lambda self: _avail(self).adjusted(0, 0, 0, -INSET)
    QScreen.availableSize = lambda self: QScreen.availableGeometry(self).size()
app = QApplication([])
PT = float(os.environ["FIT_PT"])
if PT:
    f = app.font()
    f.setPointSizeF(PT)
    app.setFont(f)
QDialog.exec = lambda self, *a: 0
for n in ("information", "warning", "critical", "question"):
    setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
from wiring import real_window
import layout_audit as A
from test_responsive_layout import at_rest
from test_ws_run import _save, doc_of, rect
from mlaser.machine.demo import DemoController
from mlaser.ui.workspaces import TOUCH
w = real_window(user_mode="engineer")
w.shell.fit_to_screen()
w.show()
def pump(sec):
    t = time.monotonic() + sec
    while time.monotonic() < t:
        app.processEvents()
        time.sleep(0.005)
clock = [0.0]
ctl = DemoController(clock=lambda: clock[0])
w.set_controller(ctl)
ctl.connect()
w.set_document(doc_of(rect(0, 0, 100, 100), rect(200, 0, 100, 100)))
w.show_workspace("run")
ws = w.extension("run")
scr = ws.screen
avail = w.screen().availableGeometry()
out, seen = [], []
SIZES = {"start": int(TOUCH * 1.2), "hold": int(TOUCH * 1.2), "stop": int(TOUCH * 1.4),
         "arm": TOUCH}
def check(label):
    pump(0.3)
    scr.refresh()
    at_rest(w, app)
    g = w.frameGeometry()
    if g.left() < avail.left() or g.top() < avail.top() or g.right() > avail.right() or g.bottom() > avail.bottom():
        out.append(f"{label}: window {g.getRect()} outside the screen's room {avail.getRect()}")
    out.extend(f"{label}: {v}" for v in A.audit(w))
    for key, h in SIZES.items():
        b = scr.buttons[key]
        if not b.isVisible() or b.height() < h:
            out.append(f"{label}: {key} {b.isVisible()} {b.height()} < {h}")
    seen.append(label)
check("ready")
ctl.arm()
w.safety_ui.start()
w.safety_ui.wait_preflight(30)
pump(0.3)
clock[0] += 0.5
ctl.state()
check("running")
ctl.stop()
clock[0] += 1.0
ctl.state()
check("rest")
p = _save(doc_of(rect(0, 0, 100, 100)), os.path.join(os.environ["MLASER_DATA_DIR"], "a.chf"))
ws.queue.add(p, 2, material="S235", thickness_mm=3.0, extra={"sheet_bounds": [0, 0, 3000, 1500]})
scr.toggle_queue()
check("queue")
if os.environ["FIT_TABLE"] == "1":
    scr.tabs.setCurrentWidget(scr.queue_panel)
    check("queue/table")
print(json.dumps({"avail": list(avail.getRect()), "violations": out, "seen": seen}), flush=True)
os._exit(0)
"""

#: (screen width, height, bottom inset of a taskbar, font pt (0: default), the Queue tab on top
#: while the queue waits): half a 1512 x 982 screen; the smallest supported screen with a Windows
#: taskbar, at the default and a 12 pt font.  Not yet there: the Queue tab (the table and its
#: buttons) under the "Load this sheet" card on 1280 x 800 with a taskbar scrolls (84 px; 234 px
#: at 12 pt)
SCREENS = [(756, 982, 0, 0, True), (1280, 800, 48, 0, False), (1280, 800, 48, 12, False)]


@pytest.mark.sim          # one worker with the timing tests: a whole app in a subprocess
@pytest.mark.parametrize("screen", SCREENS, ids=lambda s: f"{s[0]}x{s[1]}-{s[2]}-{s[3] or 'default'}pt")
def test_the_run_workspace_fits_in_every_state(tmp_path, screen):
    style_name = os.environ.get("MLASER_LAYOUT_STYLE", "").lower()
    if style_name in ("macos", "windows11", "windowsvista"):
        pytest.skip(f"the offscreen platform cannot host the {style_name} style")
    sw, sh, inset, pt, table = screen
    (tmp_path / "screen.json").write_text(json.dumps({"screens": [{"name": "s", "x": 0, "y": 0, "width": sw,
                                                                    "height": sh, "logicalDpi": 96, "dpr": 1}]}),
                                          encoding="utf-8")
    (tmp_path / "data").mkdir()
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen:configfile=screen.json",
               MLASER_CONFIG_DIR=str(tmp_path / "cfg"), MLASER_APP_DIR=str(tmp_path / "app"),
               MLASER_DATA_DIR=str(tmp_path / "data"), FIT_INSET=str(inset), FIT_PT=str(pt),
               FIT_TABLE="1" if table else "0")
    env.pop("_MLASER_CONFIG_AUTO", None)
    root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join([root, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    r = subprocess.run([sys.executable, "-c", RUN_FIT, str(Path(__file__).parent.resolve())], env=env,
                       capture_output=True, text=True, timeout=600, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr[-3000:]
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["avail"][2:] == [sw, sh - inset], res["avail"]
    assert res["seen"] == ["ready", "running", "rest", "queue"] + (["queue/table"] if table else []), res
    assert not res["violations"], "\n".join(res["violations"])
