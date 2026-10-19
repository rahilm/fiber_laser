"""``python -m mlaser`` and the frozen (PyInstaller) entry point.

Options handled here, before the GUI starts; everything else goes to
:func:`mlaser.ui.__main__.main` unchanged:

* ``--selftest [--probe] [--json]``: run the network self-test (:mod:`mlaser.netcheck`), print
  it and exit (0 = no check failed).  ``--host/--port/--ini`` select the card as for the GUI.
  Without ``--probe`` nothing is sent to the card.
* ``--smoke-test SECONDS``: start the app normally (e.g. with ``--demo``) and quit cleanly after
  SECONDS with exit code 0 (CI smoke test of the frozen bundle, ``QT_QPA_PLATFORM=offscreen``).
* ``--bundle-check [FILE.dxf|FILE.svg ...]``: import every bundled library, read the package
  data (icons, translations, parameter registry) and optionally import the given drawings, then
  exit (0 = all required parts present).  For checking a frozen build without a display.
* first run: the first time a user starts the app the self-test runs once without card
  traffic, in a background thread that holds startup for at most 1 s; the result is printed to
  stderr and saved in the user config directory (``mlaser.netcheck.last_result()``).  Set
  ``MLASER_NO_FIRSTRUN=1`` to skip it.

Imports are absolute: PyInstaller runs this file as a top-level script.
"""
from __future__ import annotations

import sys


def _is_multiprocessing_child(argv: list[str]) -> bool:
    """A spawn child (or resource tracker) of a frozen app: it re-runs this executable."""
    return any(a == "--multiprocessing-fork" or a.startswith("from multiprocessing") for a in argv)


def main(argv: list[str] | None = None) -> int:
    # spawn-based ProcessStreamer: in a frozen app the child re-runs this executable.  The app's
    # own entry point (mlaser.ui.__main__.main) calls multiprocessing.freeze_support() as its
    # first statement (Safety track), so a child goes there before anything here runs.
    if _is_multiprocessing_child(sys.argv[1:] if argv is None else argv):
        from mlaser.ui.__main__ import main as ui_main
        return ui_main(argv)
    import argparse

    args = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--smoke-test", type=float, metavar="SECONDS")
    ap.add_argument("--bundle-check", action="store_true")
    own, rest = ap.parse_known_args(args)

    if own.bundle_check:
        return bundle_check([a for a in rest if not a.startswith("-")])

    if own.selftest:
        from mlaser import netcheck
        return netcheck.main(rest)

    if "-h" in rest or "--help" in rest:
        print("mlaser launcher options: --selftest [--probe] [--json], --smoke-test SECONDS, "
              "--bundle-check [FILE ...]\n")

    from mlaser import netcheck
    netcheck.first_run(**_card_args(rest))

    if own.smoke_test is not None:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv[:1])
        # the UI's main() reuses this QApplication; exit(0) ends its app.exec()
        QTimer.singleShot(int(own.smoke_test * 1000), lambda: app.exit(0))

    from mlaser.ui.__main__ import main as ui_main
    rc = ui_main(rest)
    if own.smoke_test is not None:
        print(f"mlaser smoke test: GUI ran for {own.smoke_test:g} s, exit code {rc}", file=sys.stderr)
    return int(rc or 0)


# (module, required): required ones are runtime dependencies, the others optional extras
_BUNDLE_MODULES = [
    ("PySide6.QtCore", True), ("PySide6.QtGui", True), ("PySide6.QtWidgets", True),
    ("PySide6.QtSvg", True), ("PySide6.QtPrintSupport", True), ("numpy", True), ("shapely", True),
    ("ezdxf", True), ("svgelements", True), ("pyclipper", True), ("uharfbuzz", True),
    ("fontTools.ttLib", True), ("segno", True), ("PIL.Image", True),
    ("ortools.constraint_solver.pywrapcp", False), ("spyrrow", False), ("ruckig", False),
    ("hid", False), ("serial", False), ("platformdirs", False),
]


def bundle_check(files: list[str]) -> int:
    """Import the libraries and read the package data; print one line per item."""
    import importlib
    import os
    failed = 0

    def line(ok: bool, what: str, detail: str = "", required: bool = True) -> None:
        nonlocal failed
        failed += (not ok) and required
        tag = "ok" if ok else ("FAIL" if required else "missing")
        print(f"  [{tag:7}] {what}{': ' + detail if detail else ''}")

    print("mlaser bundle check" + (" (frozen)" if getattr(sys, "frozen", False) else ""))
    for mod, required in _BUNDLE_MODULES:
        try:
            m = importlib.import_module(mod)
            line(True, mod, str(getattr(m, "__version__", "")), required)
        except Exception as e:  # ImportError, or a broken native library
            line(False, mod, f"{type(e).__name__}: {e}", required)
    try:
        from PySide6.QtCore import QLibraryInfo, qVersion
        line(True, "Qt", f"{qVersion()}, libraries in "
             f"{QLibraryInfo.path(QLibraryInfo.LibraryPath.LibrariesPath)}")
    except Exception as e:
        line(False, "Qt", str(e))
    try:
        from mlaser.ui import icons
        n = len([f for f in os.listdir(icons.ICON_DIR) if f.endswith(".svg")])
        line(n > 0, "mlaser.ui icons", f"{n} SVG files")
    except Exception as e:
        line(False, "mlaser.ui icons", str(e))
    try:
        from mlaser import i18n
        i18n.catalogue("en")
        line(True, "mlaser.i18n", f"languages {i18n.shipped_languages()}")
    except Exception as e:
        line(False, "mlaser.i18n", str(e))
    try:
        from mlaser.formats import params
        line(len(params.param_defs()) > 0, "mlaser.formats parameter registry",
             f"{len(params.param_defs())} definitions")
    except Exception as e:
        line(False, "mlaser.formats parameter registry", str(e))
    try:
        # the ProcessStreamer's start method: in a frozen app the child re-runs this executable
        # and freeze_support() in main() must turn it into the worker
        import multiprocessing as mp
        import time as _time
        ctx = mp.get_context("spawn")
        t0 = _time.perf_counter()
        proc = ctx.Process(target=_time.sleep, args=(0.01,), daemon=True)
        proc.start()
        proc.join(30)
        line(proc.exitcode == 0, "multiprocessing spawn",
             f"child exit code {proc.exitcode}, {(_time.perf_counter() - t0) * 1000:.0f} ms")
    except Exception as e:
        line(False, "multiprocessing spawn", f"{type(e).__name__}: {e}")
    for path in files:
        try:
            if path.lower().endswith(".svg"):
                from mlaser.io import svg_import
                doc = svg_import.import_svg(path)
            else:
                from mlaser.io import dxf_import
                doc = dxf_import.import_dxf(path)
            line(True, f"import {os.path.basename(path)}", f"{len(getattr(doc, 'parts', []) or [])} parts")
        except Exception as e:
            line(False, f"import {os.path.basename(path)}", f"{type(e).__name__}: {e}")
    print(f"  result: {'OK' if not failed else f'{failed} required item(s) failed'}")
    return 1 if failed else 0


def _card_args(rest: list[str]) -> dict:
    """--host/--port/--ini from the GUI arguments, for the first-run check."""
    import argparse
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--ini")
    try:
        a, _ = ap.parse_known_args(rest)
    except SystemExit:
        return {}
    return {"host": a.host, "port": a.port, "ini": a.ini}


if __name__ == "__main__":
    sys.exit(main())
