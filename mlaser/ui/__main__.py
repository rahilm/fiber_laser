"""``mlaser [file] [--demo] [--sim] [--host H --port P | --ini ipAdd.ini] [--lang en]``.

* ``--demo``: load the sample document and connect to the in-process MCC100 simulator through
  the real protocol stack (``mlaser.controller.ui_adapter.UiController``); falls back to the
  dependency-free ``DemoController`` if that fails.
* ``--sim``: connect to the in-process simulator (with any document / file).
* ``--real``: connect to a real card at ``--host/--port`` or the address in ``--ini``.
* ``--laser-off``: dry-run mode, the laser byte of every step item is cleared.
* ``--fake``: use the time-integrated DemoController (no protocol at all).
* ``--import-lang PATH``: import the translations of your own Mlaser install (see
  :mod:`mlaser.i18n.importer`); ``--lang-dir PATH`` reads them from there without importing.
"""
from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mlaser")
    ap.add_argument("file", nargs="?", help=".chf / .dxf / .svg / .nc to open")
    ap.add_argument("--lang", default="en", help="language code (en; zh, ru, de, es, pt, fr, it, pl, vi, tr "
                    "after importing them from an Mlaser install)")
    ap.add_argument("--import-lang", metavar="PATH",
                    help="import the translations of your own Mlaser install (folder or Lang/*.txt file)")
    ap.add_argument("--lang-dir", metavar="PATH",
                    help="read translations directly from an Mlaser install without importing them")
    ap.add_argument("--demo", action="store_true", help="sample document + in-process card simulator")
    ap.add_argument("--sim", action="store_true", help="connect to the in-process card simulator")
    ap.add_argument("--sim-speed", type=float, default=1.0, help="simulator time scale")
    ap.add_argument("--real", action="store_true", help="connect to a real MCC100 card (UNTESTED)")
    ap.add_argument("--host", default="10.1.1.168")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--ini", help="ipAdd.ini with CardIP/CardPort/timeouts")
    ap.add_argument("--laser-off", action="store_true", help="never set the laser byte (dry run)")
    ap.add_argument("--fake", action="store_true", help="use the protocol-free DemoController")
    args = ap.parse_args(argv)

    from PySide6.QtWidgets import QApplication

    from . import docops
    from .facade import DemoController
    from .i18n import Translator
    from .mainwindow import EXTENSIONS, MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    from . import i18n
    tr = i18n._default = Translator(lang_dir=args.lang_dir)
    if args.import_lang:
        try:
            print("imported languages:", ", ".join(tr.import_from(args.import_lang)), file=sys.stderr)
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
    try:
        tr.set_language(args.lang)
    except KeyError:
        print(f"unknown language {args.lang!r}; using English", file=sys.stderr)
    if tr.language not in {li.code for li in tr.available()}:
        print(f"no texts for {args.lang!r}: import them with --import-lang <your Mlaser folder>; "
              "showing English", file=sys.stderr)
    ctl = None
    if not args.fake:
        try:
            from mlaser.controller.ui_adapter import UiController
            ctl = UiController(args.host, args.port, ini_path=args.ini, laser_enabled=not args.laser_off)
        except Exception as exc:  # pragma: no cover - controller package broken
            print(f"real controller unavailable ({exc}); using the demo controller", file=sys.stderr)
    win = MainWindow(ctl or DemoController(), tr, extensions=EXTENSIONS)
    if ctl is not None:
        ctl.machine, ctl.cam_params = win.machine, win.cam_params
    if args.file:
        win.load_path(args.file)
    elif args.demo:
        win.set_document(docops.demo_document())
    if ctl is None:
        if args.demo or args.sim:
            win.ctl.connect()
    elif args.demo or args.sim:
        if not ctl.connect_simulator(time_scale=args.sim_speed):
            win.set_controller(DemoController())
            win.ctl.connect()
    elif args.real:
        ctl.connect()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
