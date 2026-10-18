"""Trigger every ribbon command headlessly and list the ones still unimplemented.

Modal dialogs, message boxes, file pickers and menus are stubbed so nothing blocks.  A command
counts as unimplemented when it reaches ``MainWindow.not_implemented``; a command that raises
is listed separately.

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/command_coverage.py [--json]
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# extensions write logs/reports/state to the data dir; keep coverage runs out of ~/.mlaser
if "MLASER_DATA_DIR" not in os.environ:
    import tempfile
    os.environ["MLASER_DATA_DIR"] = tempfile.mkdtemp(prefix="mlaser-coverage-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _stub_dialogs() -> None:
    from PySide6.QtWidgets import QDialog, QFileDialog, QInputDialog, QMenu, QMessageBox
    QDialog.exec = lambda self, *a: 0
    QMessageBox.exec = lambda self, *a: 0
    for n in ("information", "warning", "critical", "question"):
        setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ("", ""))
    QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([], ""))
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
    QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: "")
    for n in ("getInt", "getDouble", "getText", "getItem"):
        setattr(QInputDialog, n, staticmethod(lambda *a, **k: (None, False)))
    QMenu.exec = lambda self, *a: None


def _stub_network() -> None:
    """hw.net_test probes the configured device IPs; never send that traffic from a coverage run."""
    try:
        from mlaser.controller import nettest
    except ImportError:
        return
    nettest.network_test = lambda *a, **k: []


def coverage() -> dict:
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    _stub_dialogs()
    _stub_network()
    from mlaser.ui import docops
    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import EXTENSIONS, MainWindow
    w = MainWindow(DemoController(), extensions=EXTENSIONS)
    w.set_document(docops.demo_document())
    missing: list[str] = []
    errors: dict[str, str] = {}
    w.not_implemented = lambda c: missing.append(c)
    for cmd, act in sorted(w.ribbon.actions.items()):
        try:
            act.trigger()
        except Exception as e:  # noqa: BLE001 - report, keep going
            errors[cmd] = f"{type(e).__name__}: {e}"
        app.processEvents()
    total = len(w.ribbon.actions)
    w.close()
    return {"total": total, "unimplemented": sorted(set(missing)), "errors": errors,
            "extensions": sorted(w.extensions)}


if __name__ == "__main__":
    r = coverage()
    if "--json" in sys.argv:
        print(json.dumps(r, indent=1))
    else:
        print(f"{r['total']} commands, {len(r['unimplemented'])} unimplemented, {len(r['errors'])} raising")
        print("extensions loaded:", ", ".join(r["extensions"]))
        for c in r["unimplemented"]:
            print("  NI ", c)
        for c, e in r["errors"].items():
            print("  ERR", c, e)
