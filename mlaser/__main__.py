"""``python -m mlaser`` and the frozen (PyInstaller) entry point.

Options handled here, before the GUI starts; everything else goes to
:func:`mlaser.ui.__main__.main` unchanged:

* ``--selftest [--probe] [--json]``: report the emitter kernel (C or Python, its self-check:
  :func:`mlaser.parity_ports.kernel_status`), run the network self-test (:mod:`mlaser.netcheck`),
  print it and exit (0 = no check failed; 1 also when ``MLASER_NATIVE=1`` and the C kernel
  cannot be used).  ``--host/--port/--ini`` select the card as for the GUI.
  Without ``--probe`` nothing is sent to the card.
* ``--smoke-test SECONDS``: start the app normally (e.g. with ``--demo``) and quit cleanly after
  SECONDS with exit code 0 (CI smoke test of the frozen bundle, ``QT_QPA_PLATFORM=offscreen``).
* ``--bundle-check [FILE.dxf|FILE.svg ...]``: import every bundled library, read the package
  data (icons, translations, parameter registry) and optionally import the given drawings, then
  exit (0 = all required parts present).  For checking a frozen build without a display.
* ``compile`` / ``estimate`` / ``nest`` / ``validate`` / ``convert`` / ``queue``: the headless
  command-line tools (:mod:`mlaser.cli`, docs/user/automation.md); no GUI, no card connection.
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
    from mlaser.cli import is_subcommand
    if is_subcommand(args):
        # headless tools (compile, estimate, nest, validate, convert, queue): no GUI, no card
        from mlaser.cli import main as cli_main
        return cli_main(args)
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--smoke-test", type=float, metavar="SECONDS")
    ap.add_argument("--bundle-check", action="store_true")
    own, rest = ap.parse_known_args(args)

    if own.bundle_check:
        return bundle_check([a for a in rest if not a.startswith("-")])

    if own.selftest:
        from mlaser import netcheck
        kernel_ok = _kernel_line("--json" in rest)
        rc = netcheck.main(rest)
        return rc or (0 if kernel_ok else 1)

    if "-h" in rest or "--help" in rest:
        print("mlaser launcher options: --selftest [--probe] [--json], --smoke-test SECONDS, "
              "--bundle-check [FILE ...]\n")

    from mlaser import netcheck
    netcheck.first_run(**_card_args(rest))

    if own.smoke_test is not None:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        from mlaser.ui import native
        app = QApplication.instance()
        if app is None:
            native.before_qapplication()
            app = native.create_application(sys.argv[:1])
        # the UI's main() reuses this QApplication; exit(0) ends its app.exec()
        QTimer.singleShot(int(own.smoke_test * 1000), lambda: app.exit(0))

    from mlaser.ui.__main__ import main as ui_main
    rc = ui_main(rest)
    if own.smoke_test is not None:
        print(f"mlaser smoke test: GUI ran for {own.smoke_test:g} s, exit code {rc}", file=sys.stderr)
    return int(rc or 0)


# (module, required): required ones are runtime dependencies, the others optional extras.  In a
# frozen bundle the optional backends it ships (its manifest, :func:`_bundle_manifest`) become
# required, and each must also pass its working check (:data:`_BACKEND_CHECKS`).
_BUNDLE_MODULES = [
    ("PySide6.QtCore", True), ("PySide6.QtGui", True), ("PySide6.QtWidgets", True),
    ("PySide6.QtSvg", True), ("PySide6.QtPrintSupport", True), ("numpy", True), ("shapely", True),
    ("ezdxf", True), ("svgelements", True), ("pyclipper", True), ("uharfbuzz", True),
    ("fontTools.ttLib", True), ("segno", True), ("PIL.Image", True), ("platformdirs", False),
]


def _bundle_manifest() -> dict | None:
    """``mlaser/bundle_manifest.json``: written by packaging/mlaser.spec into a bundle, it names
    the optional backends the build shipped (``{"shipped": {"ortools": true, ...}}``).  None when
    running from a source checkout or a wheel."""
    import json
    import os
    import mlaser
    path = os.path.join(os.path.dirname(os.path.abspath(mlaser.__file__)), "bundle_manifest.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _check_ortools() -> str:
    """Sort mode 20's OR-Tools backend, through the app's own entry point
    (:func:`mlaser.cam.order_ortools.shortest_path_ortools` without its fallback): the native
    library must load and the routing modules it imports must be bundled."""
    import ortools

    from mlaser.cam.order_ortools import shortest_path_ortools
    from mlaser.cam.ordering import Node
    pts = [(30.0, 0.0), (10.0, 0.0), (20.0, 10.0), (0.0, 10.0), (5.0, 5.0)]
    nodes = [Node(contour=None, key=i, entry=p, exit=p, bbox=(p[0], p[1], p[0] + 1, p[1] + 1),
                  area=1.0) for i, p in enumerate(pts)]
    out = shortest_path_ortools(nodes, fallback=False, time_limit=0.2)
    if sorted(n.key for n in out) != list(range(len(pts))):
        raise RuntimeError(f"bad order {[n.key for n in out]}")
    return f"{ortools.__version__}, {len(pts)}-contour order solved"


def _check_spyrrow() -> str:
    """The sparrow nester: pack two squares into a strip (constructive only)."""
    import spyrrow
    sq = [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)]
    inst = spyrrow.StripPackingInstance("check", 10.0, [spyrrow.Item("a", sq, demand=2,
                                                                     allowed_orientations=[0.0])])
    sol = inst.solve(spyrrow.StripPackingConfig(early_termination=True, total_computation_time=None,
                                                exploration_time=0, compression_time=0, seed=0))
    if len(sol.placed_items) != 2:
        raise RuntimeError(f"{len(sol.placed_items)} of 2 items placed")
    return f"{getattr(spyrrow, '__version__', '')}, 2 items placed, strip {sol.width:g}".lstrip(", ")


def _check_ruckig() -> str:
    """Ruckig: one jerk-limited trajectory."""
    import ruckig
    inp = ruckig.InputParameter(1)
    inp.current_position, inp.target_position = [0.0], [1.0]
    inp.max_velocity, inp.max_acceleration, inp.max_jerk = [1.0], [1.0], [1.0]
    traj = ruckig.Trajectory(1)
    res = ruckig.Ruckig(1).calculate(inp, traj)
    if res != ruckig.Result.Working:
        raise RuntimeError(f"ruckig returned {res}")
    return f"{getattr(ruckig, '__version__', '')}, trajectory {traj.duration:.3f} s".lstrip(", ")


def _check_import(mod: str):
    def run() -> str:
        import importlib
        return str(getattr(importlib.import_module(mod), "__version__", ""))
    return run


#: manifest key -> (what is reported, check returning a detail line; raises when broken)
_BACKEND_CHECKS = {
    "ortools": ("OR-Tools (sort mode 20)", _check_ortools),
    "spyrrow": ("spyrrow nester", _check_spyrrow),
    "ruckig": ("Ruckig motion profiles", _check_ruckig),
    "hid": ("hidapi (devices)", _check_import("hid")),
    "serial": ("pyserial (devices)", _check_import("serial")),
}


def _check_svgelements() -> str:
    """SVG import's parser on a two-element drawing."""
    import io

    import svgelements
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="20mm" viewBox="0 0 20 20">'
           '<rect x="1" y="1" width="8" height="8"/><circle cx="15" cy="15" r="4"/></svg>')
    shapes = [e for e in svgelements.SVG.parse(io.StringIO(svg)).elements()
              if isinstance(e, svgelements.Shape)]
    if len(shapes) != 2:
        raise RuntimeError(f"{len(shapes)} of 2 shapes parsed")
    return f"{getattr(svgelements, 'SVGELEMENTS_VERSION', '')}, 2 shapes parsed".lstrip(", ")


def _check_plugins() -> str:
    """The plugin manager end to end in a temporary directory (services.plugin_manager.self_check)."""
    import tempfile

    from mlaser.services.plugin_manager import self_check
    with tempfile.TemporaryDirectory(prefix="mlaser-plugin-check-") as d:
        return self_check(d)


def _check_fonts() -> str:
    """The bundled cutting-text fonts (mlaser/cad/fontdata): each file present and shaping text
    into outlines (fontTools + uharfbuzz)."""
    import os

    from mlaser.cad import fonts, text
    out = []
    for family, name in fonts.BUNDLED_FONTS:
        path = os.path.join(fonts.BUNDLED_DIR, name)
        if not os.path.isfile(path):
            raise RuntimeError(f"{name} missing in {fonts.BUNDLED_DIR}")
        sample = "مرحبا" if "Arabic" in family else "Aé中"
        polys, _ = text.text_outlines(sample, font_file=path)
        if not polys:
            raise RuntimeError(f"{name}: no outlines for {sample!r}")
        out.append(f"{name} ({len(polys)} contours)")
    return ", ".join(out)


def bundle_check(files: list[str]) -> int:
    """Import the libraries, run the backends and read the package data; print one line per item."""
    import importlib
    import os
    failed = 0
    manifest = _bundle_manifest()
    shipped = (manifest or {}).get("shipped", {})

    def line(ok: bool, what: str, detail: str = "", required: bool = True) -> None:
        nonlocal failed
        failed += (not ok) and required
        tag = "ok" if ok else ("FAIL" if required else "missing")
        print(f"  [{tag:7}] {what}{': ' + detail if detail else ''}")

    def run(what: str, fn, required: bool) -> None:
        try:
            line(True, what, fn(), required)
        except BaseException as e:  # ImportError, a broken native library, a Rust panic
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            line(False, what, f"{type(e).__name__}: {e}", required)

    print("mlaser bundle check" + (" (frozen)" if getattr(sys, "frozen", False) else ""))
    if manifest is not None:
        names = sorted(k for k, v in shipped.items() if v)
        print(f"  bundle manifest: ships {', '.join(names) or 'no optional backends'} "
              "(each one required)")
    for mod, required in _BUNDLE_MODULES:
        try:
            m = importlib.import_module(mod)
            line(True, mod, str(getattr(m, "__version__", "")), required)
        except Exception as e:  # ImportError, or a broken native library
            line(False, mod, f"{type(e).__name__}: {e}", required)
    for key, (what, fn) in _BACKEND_CHECKS.items():
        run(what, fn, bool(shipped.get(key)))
    run("svgelements (SVG import)", _check_svgelements, True)
    run("bundled fonts (cutting text)", _check_fonts, True)
    run("plugin manager (wheel install, site directory, entry points)", _check_plugins, True)
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
        from mlaser.diag import helpdocs
        n = len(helpdocs.topics())
        line(n > 0, "help documents", f"{n} topics in {helpdocs.docs_root()}")
    except Exception as e:
        line(False, "help documents", str(e))
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
    try:
        # the C emitter kernel: optional (the Python port runs without it) unless MLASER_NATIVE=1
        from mlaser.parity_ports import kernel_status
        st = kernel_status()
        # required with MLASER_NATIVE=1, and in a bundle that ships it (unless MLASER_NATIVE=0)
        need = st.mode == "on" or (bool(shipped.get("native_kernel")) and st.mode != "off")
        line(st.native, "emitter kernel (mlaser._native)", st.text(), required=need)
    except Exception as e:
        line(False, "emitter kernel (mlaser._native)", f"{type(e).__name__}: {e}", required=False)
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


def _kernel_line(to_stderr: bool = False) -> bool:
    """Print which emitter kernel runs (C / Python, self-check); False when it was required
    (``MLASER_NATIVE=1``) but cannot be used."""
    try:
        from mlaser.parity_ports import kernel_status
        st = kernel_status()
        text, ok = st.text(), st.active != "error"
    except Exception as e:  # noqa: BLE001 - reported, never fatal on its own
        text, ok = f"Emitter kernel: status unavailable ({type(e).__name__}: {e})", False
    print(text, file=sys.stderr if to_stderr else sys.stdout)
    return ok


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
