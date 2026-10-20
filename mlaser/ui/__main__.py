"""``mlaser [file] [--demo] [--sim] [--host H --port P | --ini ipAdd.ini] [--lang en]``.

``mlaser compile|estimate|nest|validate|convert|queue ...`` runs the headless command-line tools
instead (:mod:`mlaser.cli`, docs/user/automation.md): no window, no card connection.

* ``--demo``: load the sample document and connect to the in-process MCC100 simulator through
  the real protocol stack (``mlaser.controller.ui_adapter.UiController``); falls back to the
  dependency-free ``DemoController`` if that fails (with a message).
* ``--sim``: connect to the in-process simulator (with any document / file).
* ``--real``: connect to a real card at ``--host/--port`` or the address in ``--ini``.  If the
  controller cannot be built or the card does not answer, mlaser **exits with an error**
  (exit code 2) instead of falling back to a demo controller.
* ``--laser-off``: dry-run mode, the laser byte of every step item is cleared and arming is
  impossible (banner DRY-RUN).
* ``--log-dir PATH``: where mlaser.log / audit.log / crash.log go (default: the user log directory).

Every connection starts **disarmed** (docs/SAFETY.md): the operator arms the machine in the
state banner or the run bar before anything can fire.
* ``--fake``: use the time-integrated DemoController (no protocol at all).
* ``--import-lang PATH``: import the translations of your own Mlaser install (see
  :mod:`mlaser.i18n.importer`); ``--lang-dir PATH`` reads them from there without importing.
"""
from __future__ import annotations

import argparse
import multiprocessing
import sys


def make_controller(host: str, port: int, ini, laser_enabled: bool, config):
    """The real-card controller with the machine's safety I/O from ``config``
    (:mod:`mlaser.services.safety_io`): the interlocks at construction, the heartbeat through
    ``safety_io.apply``, which checks it against the controller's output map (a heartbeat on a
    process output stays off).  Later changes reach it through ``ui/safety_io.py``."""
    from mlaser.controller.ui_adapter import UiController
    from mlaser.services import safety_io
    sio = safety_io.from_config(config)
    ctl = UiController(host, port, ini_path=ini, laser_enabled=laser_enabled,
                       interlocks=sio.kwargs()["interlocks"])
    bad = safety_io.problems(sio, ctl.output_map)
    if bad:
        print("safety I/O: " + "; ".join(bad) + " (Machine > Settings > Safety I/O)", file=sys.stderr)
    safety_io.apply(ctl, sio)
    return ctl


def main(argv=None) -> int:
    # a frozen Windows build re-runs the executable for the streaming child (spawn): let
    # multiprocessing take over there instead of starting a second GUI
    multiprocessing.freeze_support()
    from mlaser.cli import is_subcommand
    words = list(sys.argv[1:] if argv is None else argv)
    if is_subcommand(words):
        # ``mlaser compile ...`` etc.: the headless tools (mlaser.cli), never the GUI
        from mlaser.cli import main as cli_main
        return cli_main(words)
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
    ap.add_argument("--log-dir", help="log directory (default: the user log directory)")
    args = ap.parse_args(argv)

    from mlaser.machine import lifecycle, logsetup
    try:
        logdir = logsetup.setup_logging(args.log_dir)
    except OSError as exc:
        print(f"log files unavailable ({exc})", file=sys.stderr)
        logdir = None
    lifecycle.install()          # atexit, SIGINT/SIGTERM, excepthooks -> safe_halt

    from PySide6.QtWidgets import QApplication

    from . import docops
    from .facade import DemoController
    from .i18n import Translator
    from .mainwindow import EXTENSIONS, MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    from mlaser._power import app_activity
    app_activity()                     # macOS: no App Nap (timers and priority kept)
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
    # the configuration first: the controller is built with the machine's safety I/O
    # (interlocks, "PC alive" heartbeat: services.safety_io, docs/SAFETY.md §8, §11)
    from mlaser.services.config import ConfigService
    config = ConfigService()
    ctl = None
    if not args.fake:
        try:
            ctl = make_controller(args.host, args.port, args.ini, not args.laser_off, config)
        except Exception as exc:  # noqa: BLE001 - controller package broken
            if args.real:
                print(f"ERROR: the real controller cannot be built ({exc}); not starting "
                      "(no fallback to the demo controller with --real)", file=sys.stderr)
                return 2
            print(f"real controller unavailable ({exc}); using the demo controller", file=sys.stderr)
    win = MainWindow(ctl or DemoController(laser_enabled=not args.laser_off), tr, extensions=EXTENSIONS,
                     config=config, entry_points=True)
    if ctl is not None:
        ctl.machine, ctl.cam_params = win.machine, win.cam_params
    if args.file:
        win.load_path(args.file)
    elif args.demo:
        win.set_document(docops.demo_document())
    if ctl is None:
        if args.demo or args.sim:
            win.ctl.connect()
    elif args.real:
        if not ctl.connect():
            msg = (f"ERROR: no answer from the motion card at {ctl.cfg.host}:{ctl.cfg.port} "
                   f"({ctl.cfg.transport}); not starting.  Check the cable and the address "
                   "(--host/--port/--ini).  mlaser does not fall back to a demo controller with --real.")
            print(msg, file=sys.stderr)
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(win, "Motion card not connected", msg)
            win._test_no_dialogs = True        # closing: no "job running" questions
            win.close()
            return 2
    elif args.demo or args.sim:
        if not ctl.connect_simulator(time_scale=args.sim_speed):
            print("the in-process simulator could not start; using the protocol-free demo controller",
                  file=sys.stderr)
            win.set_controller(DemoController(laser_enabled=not args.laser_off))
            win.ctl.connect()
            win.log_panel.log("simulator unavailable: using the protocol-free demo controller")
    if logdir is not None:
        win.log_panel.log(f"log files: {logdir}")
    win.show()
    win.shell.maybe_show_welcome()       # first run: simulator, imports, safety model, docs
    return app.exec()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
