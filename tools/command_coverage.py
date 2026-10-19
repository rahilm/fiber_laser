"""Trigger every ribbon command headlessly and list the ones still unimplemented.

Modal dialogs, message boxes, file pickers and menus are stubbed so nothing blocks.  The window
is built with the application's real wiring (``MainWindow`` + ``EXTENSIONS`` through the plugin
API).  A command counts as unimplemented when it reaches ``MainWindow.not_implemented``; a
command whose handler raises (recorded by the CommandRegistry, which keeps the application
alive) is listed separately, and so is every duplicate registration (a second plugin binding a
command id that is already bound).  The contract is 0 / 0 / 0.

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
for _var in ("MLASER_CONFIG_DIR", "MLASER_APP_DIR"):      # no user config / softPara.ini
    if _var not in os.environ:
        import tempfile
        os.environ[_var] = tempfile.mkdtemp(prefix="mlaser-coverage-")
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
    from mlaser import ops
    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import EXTENSIONS, MainWindow
    w = MainWindow(DemoController(), extensions=EXTENSIONS)
    w.set_user_mode("service")          # every command enabled (the shell's user modes)
    w.set_document(ops.demo_document())
    missing: list[str] = []
    errors: dict[str, str] = {}
    w.not_implemented = lambda c: missing.append(c)
    w.commands.failures.clear()
    # name each command before it runs (to find one that crashes the process): "1" = stderr,
    # otherwise a file to append to (survives a crash, and pytest's output capture)
    trace = os.environ.get("MLASER_COVERAGE_TRACE", "")
    tf = None if trace in ("", "1") else open(trace, "a", encoding="utf-8")  # noqa: SIM115
    for cmd, act in sorted(w.ribbon.actions.items()):
        if tf is not None:
            tf.write(f"command_coverage: {cmd}\n")
            tf.flush()
        elif trace:
            print(f"command_coverage: {cmd}", file=sys.stderr, flush=True)
        try:
            act.trigger()
        except Exception as e:  # noqa: BLE001 - report, keep going
            errors[cmd] = f"{type(e).__name__}: {e}"
        if cmd.startswith("shell.mode_"):
            w.set_user_mode("service")    # a user-mode command ran: the next ones stay enabled
        app.processEvents()
    errors.update({c: e for c, e in w.commands.failures.items() if c not in errors})
    total = len(w.ribbon.actions)
    duplicates = [f"{c} ({a} / {b})" for c, a, b in w.commands.duplicates]
    hook_errors = [f"{h} in {o}: {e}" for h, o, e in w.hooks.errors]
    w.close()
    if tf is not None:
        tf.close()
    return {"total": total, "unimplemented": sorted(set(missing)), "errors": errors,
            "duplicates": duplicates, "hook_errors": hook_errors,
            "extensions": sorted(w.extensions)}


if __name__ == "__main__":
    r = coverage()
    if "--json" in sys.argv:
        print(json.dumps(r, indent=1))
    else:
        print(f"{r['total']} commands, {len(r['unimplemented'])} unimplemented, {len(r['errors'])} raising, "
              f"{len(r['duplicates'])} duplicate registrations")
        print("extensions loaded:", ", ".join(r["extensions"]))
        for c in r["unimplemented"]:
            print("  NI ", c)
        for c, e in r["errors"].items():
            print("  ERR", c, e)
        for c in r["duplicates"]:
            print("  DUP", c)
        for c in r["hook_errors"]:
            print("  HOOK", c)
        if r["unimplemented"] or r["errors"] or r["duplicates"]:
            sys.exit(1)
