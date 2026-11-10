"""``mlaser diag-bundle OUT.zip``: the diagnostic bundle without the GUI (docs/user/faq.md).

The same zip as Help › Save diagnostic bundle… (:mod:`mlaser.diag.bundle`), from what is on
disk: the configuration (redacted), the machine parameters, the alarm history, the logs.  The
CLI has no controller, so the controller section says "not available"; it never connects to a
card and never opens a network connection.  ``--drawing FILE`` adds that drawing file (only
when given).

Exit codes: 0 written, 2 usage, 3 the drawing file cannot be read, 5 the bundle cannot be written.
"""
from __future__ import annotations

import argparse
import os
from typing import Any, Callable

from .common import EXIT_INPUT, EXIT_OK, EXIT_OUTPUT, CliError

Result = tuple[int, dict, Callable[[dict], str]]


def add_parser(sub: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    p = sub.add_parser("diag-bundle", help="write a diagnostic bundle (zip) for a bug report; nothing is sent")
    common(p)
    p.add_argument("out", help="the zip file to write")
    p.add_argument("--data-dir", metavar="DIR", help="mlaser data directory (default: the user's)")
    p.add_argument("--log-dir", metavar="DIR", help="log directory (default: the user log directory)")
    p.add_argument("--drawing", metavar="FILE", help="also include this drawing file (off by default)")


def cmd_diag_bundle(args: Any) -> Result:
    from mlaser.diag import bundle as B
    from mlaser.services import diagnostics as D
    from mlaser.services.alarm_history import FILE, AlarmHistory

    from .common import config_service
    cfg = config_service(getattr(args, "config", None))
    drawing = None
    if args.drawing:
        try:
            with open(args.drawing, "rb") as fh:
                drawing = fh.read()
        except OSError as e:
            raise CliError(f"{args.drawing}: {e.strerror or e}", EXIT_INPUT, "input") from e
    hist = None
    if args.data_dir:
        from pathlib import Path
        hist = AlarmHistory(Path(args.data_dir) / FILE)
        hist.load()
    src = D.snapshot(cfg, None, None, drawing=drawing, drawing_name=os.path.basename(args.drawing or ""),
                     data_dir=args.data_dir, alarm_history=hist)
    if args.log_dir:
        src.log_dirs = [args.log_dir]
    try:
        manifest = D.write(args.out, src, include_drawing=drawing is not None)
    except OSError as e:
        raise CliError(f"{args.out}: {e.strerror or e}", EXIT_OUTPUT, "output") from e
    items = [i.name for i in B.contents(src, drawing is not None)]
    res = {"path": os.path.abspath(args.out), "files": manifest["files"], "contents": items,
           "logs": manifest["logs"], "redactions": manifest["redactions"],
           "drawing_included": manifest["drawing_included"], "app": manifest["app"]}

    def text(d: dict) -> str:
        logs = sum(1 for g in d["logs"] if g.get("included"))
        out = [f"diagnostic bundle written: {d['path']}",
               f"  {len(d['files'])} files, {logs} log file(s), {d['redactions']} setting(s) redacted"
               + (", drawing included" if d["drawing_included"] else ", no drawing"),
               "  nothing was sent: send the file yourself if you want to"]
        return "\n".join(out)
    return EXIT_OK, res, text


__all__ = ["add_parser", "cmd_diag_bundle"]
