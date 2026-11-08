"""Run the real application (``mlaser.ui.__main__.main``) as a subprocess of a test and report.

    python tests/single_instance_driver.py REPORT.json [mlaser arguments ...]

Writes ``{"ready": true, "pid": N}`` to REPORT.json once the main window is shown, then, every
100 ms, the drawings it has opened (``opened``: the paths ``load_path`` was given), the recent
files and how many hand-over requests the instance served.  It quits by itself after
``MLASER_DRIVER_SECONDS`` (default 60) or when ``REPORT.json.quit`` appears; with
``MLASER_DRIVER_CRASH=1`` it ends with ``os._exit`` (no clean-up, like a crash) on quit.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def main() -> int:
    report = sys.argv[1]
    args = sys.argv[2:]
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from mlaser.ui import mainwindow as MW
    from mlaser.ui import safety_ui
    safety_ui.recovery_prompt = lambda *a, **k: None
    state: dict = {"ready": False, "pid": os.getpid(), "opened": [], "recent": [], "requests": 0}
    wins: list = []
    orig_load = MW.MainWindow.load_path
    orig_init = MW.MainWindow.__init__

    def load_path(self, path):
        state["opened"].append(os.path.abspath(path))
        return orig_load(self, path)

    def init(self, *a, **k):
        orig_init(self, *a, **k)
        self._test_no_dialogs = True
        wins.append(self)

    MW.MainWindow.load_path = load_path
    MW.MainWindow.__init__ = init

    def write() -> None:
        tmp = report + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, report)

    def tick() -> None:
        if wins and wins[0].isVisible():
            state["ready"] = True
            native = getattr(wins[0].shell, "native", None)
            state["recent"] = list(native.recent.paths) if native is not None else []
        from mlaser.ui import __main__ as UM
        inst = getattr(UM, "_instance", None)
        state["requests"] = getattr(inst, "requests", 0)
        write()
        if os.path.exists(report + ".quit"):
            if os.environ.get("MLASER_DRIVER_CRASH") == "1":
                os._exit(3)
            QApplication.instance().exit(0)

    def arm() -> None:
        app = QApplication.instance()
        t = QTimer(app)
        t.timeout.connect(tick)
        t.start(100)
        app._driver_timer = t
        QTimer.singleShot(int(float(os.environ.get("MLASER_DRIVER_SECONDS", "60")) * 1000),
                          lambda: app.exit(0))

    MW._driver_arm = arm
    orig_show = MW.MainWindow.show

    def show(self):
        orig_show(self)
        if not getattr(MW, "_driver_armed", False):
            MW._driver_armed = True
            arm()

    MW.MainWindow.show = show
    from mlaser.ui.__main__ import main as ui_main
    rc = ui_main(args)
    return int(rc or 0)


if __name__ == "__main__":
    sys.exit(main())
