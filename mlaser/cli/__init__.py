"""``mlaser <command>``: headless, scriptable tools on the same services as the GUI.

=============  =====================================================================
command        what it does
=============  =====================================================================
``compile``    drawing -> planned job: summary (time, pierces, lengths, extent), frames to a file
``estimate``   drawing -> time breakdown and the cost card
``nest``       parts list / CSV -> nest result and one file per sheet
``validate``   pre-flight-like checks without a machine (layers, geometry, travel, sheet)
``convert``    between ``.dxf`` / ``.svg`` / ``.chf`` / ``.mlz``
``queue add``  submit a job for the machine's queue: it waits for the operator's approval
``queue list`` the queue and the jobs waiting for approval (read-only)
=============  =====================================================================

``--json`` prints one JSON object on stdout (``{"command", "ok", "exit_code", ...}``; on an
error ``"error": {"code", "message"}``); without it the output is short text.  Exit codes:
0 ok, 1 checked and failed (validate errors, parts left unplaced), 2 usage, 3 input file,
4 processing failed, 5 output not written, 6 queue submission refused
(docs/user/automation.md).

Safety: the CLI never connects to a card.  It has no controller, so it cannot move, arm or fire
anything; ``queue add`` only submits a job that the operator must approve at the machine
(:mod:`mlaser.services.intake`), and even an approved job starts only from the Run screen.

Entry points: ``mlaser <command> ...`` (the GUI launcher hands these words over),
``python -m mlaser <command> ...`` and ``mlaser-cli <command> ...``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Optional

from .common import (EXIT_CHECK, EXIT_FAILED, EXIT_INPUT, EXIT_OK, EXIT_OUTPUT, EXIT_REFUSED, EXIT_USAGE,
                     CliError)

SUBCOMMANDS = ("compile", "estimate", "nest", "validate", "convert", "queue")
EXIT_CODES = {EXIT_OK: "ok", EXIT_CHECK: "check failed", EXIT_USAGE: "usage", EXIT_INPUT: "input",
              EXIT_FAILED: "failed", EXIT_OUTPUT: "output", EXIT_REFUSED: "refused"}


def is_subcommand(argv: list[str]) -> bool:
    """``argv`` (without the program name) starts with a CLI command (not a file to open)."""
    return bool(argv) and argv[0] in SUBCOMMANDS and not os.path.exists(argv[0])


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of exiting, so ``--json`` can report usage errors."""

    def error(self, message: str) -> Any:
        raise CliError(f"{self.prog}: {message}", EXIT_USAGE, "usage")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="print one JSON object on stdout")
    p.add_argument("--config", metavar="PATH", help="config.json to use (default: the user's)")


def _tech_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tech", metavar="MAT/THICK[/GAS[/NOZZLE]]",
                   help='apply this technology to every used layer first, e.g. "S235/3/O2"')
    p.add_argument("--tech-db", metavar="PATH", help="technology database (default: the user's)")


def _start(text: Optional[str]) -> tuple[float, float]:
    if not text:
        return (0.0, 0.0)
    try:
        x, y = (float(v) for v in text.replace(";", ",").split(","))
    except ValueError as exc:
        raise CliError(f"--start {text!r}: use X,Y in mm", EXIT_USAGE, "usage") from exc
    return (x, y)


def _size(text: Optional[str], what: str = "--sheet") -> Optional[tuple[float, float]]:
    if not text:
        return None
    try:
        w, h = (float(v) for v in text.lower().replace("mm", "").replace("*", "x").split("x"))
    except ValueError as exc:
        raise CliError(f"{what} {text!r}: use WIDTHxHEIGHT in mm, e.g. 3000x1500", EXIT_USAGE, "usage") from exc
    if w <= 0 or h <= 0:
        raise CliError(f"{what} {text!r}: width and height must be positive", EXIT_USAGE, "usage")
    return (w, h)


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="mlaser", description="mlaser headless tools (no machine connection)")
    sub = ap.add_subparsers(dest="command", parser_class=_Parser)

    p = sub.add_parser("compile", help="compile a drawing into the job Start would stream")
    _common(p)
    _tech_args(p)
    p.add_argument("file")
    p.add_argument("--frames", metavar="OUT", help="write the card frames (JSON lines) to OUT")
    p.add_argument("--start", metavar="X,Y", help="head position the job starts from (mm, default 0,0)")
    p.add_argument("--dry-run", action="store_true", help="compile laser off (as Dry run)")
    p.add_argument("--mode", choices=("process", "frame"), default="process")

    p = sub.add_parser("estimate", help="time and cost of a drawing")
    _common(p)
    _tech_args(p)
    p.add_argument("file")
    p.add_argument("--start", metavar="X,Y")
    p.add_argument("--rates", metavar="JSON", help="cost rates file (default: Process > Rates of the config)")

    p = sub.add_parser("nest", help="nest parts onto sheets")
    _common(p)
    _tech_args(p)
    p.add_argument("parts", nargs="+", help="FILE[:QTY] ... or a parts list .csv (file,quantity,name,...)")
    p.add_argument("--sheet", required=True, metavar="WxH", help="sheet size in mm, e.g. 3000x1500")
    p.add_argument("--sheets", type=int, default=1, help="number of such sheets (default 1)")
    p.add_argument("--spacing", type=float, default=3.0, help="part spacing in mm")
    p.add_argument("--margin", type=float, default=5.0, help="sheet edge margin in mm")
    p.add_argument("--rotation", type=float, default=90.0, help="rotation step in degrees (0 = none)")
    p.add_argument("--time-limit", type=float, default=30.0, help="seconds")
    p.add_argument("--engine", default="", help="nest engine (default: the built-in raster engine)")
    p.add_argument("--out", metavar="DIR", help="write one file per used sheet into DIR")
    p.add_argument("--format", choices=("mlz", "chf", "dxf", "svg"), default="mlz",
                   help="sheet files: mlz (default, keeps the layer parameters), chf (no layer table), dxf, svg")
    p.add_argument("--name", default="nest", help="file name prefix of the sheets")
    p.add_argument("--material", default="", help="material (recorded in the sheet files' summary)")
    p.add_argument("--thickness", type=float, default=0.0)

    p = sub.add_parser("validate", help="pre-flight-like checks without a machine")
    _common(p)
    _tech_args(p)
    p.add_argument("file")
    p.add_argument("--start", metavar="X,Y")
    p.add_argument("--travel", metavar="WxH", help="machine travel (default: from the machine parameters)")
    p.add_argument("--sheet", metavar="WxH", help="the cut must fit on a sheet of this size at the origin")
    p.add_argument("--dry-run", action="store_true", help="check as a laser-off run")

    p = sub.add_parser("convert", help="convert between .dxf, .svg, .chf and .mlz")
    _common(p)
    _tech_args(p)
    p.add_argument("input")
    p.add_argument("output")

    q = sub.add_parser("queue", help="the machine's job queue (submit for approval, list)")
    qs = q.add_subparsers(dest="queue_command", parser_class=_Parser)
    p = qs.add_parser("add", help="submit a job; the operator approves it at the machine")
    _common(p)
    p.add_argument("file")
    p.add_argument("--quantity", "-n", type=int, default=1, help="sheets to cut")
    p.add_argument("--name", default="")
    p.add_argument("--material", default="")
    p.add_argument("--thickness", type=float, default=0.0)
    p.add_argument("--sheet", default="", help="sheet description, e.g. 3000 x 1500 mm")
    p.add_argument("--note", default="", help="a note for the operator")
    p.add_argument("--reference", default="", help="your own id (order number)")
    p.add_argument("--data-dir", metavar="DIR", help="mlaser data directory (default: the user's)")
    p = qs.add_parser("list", help="the queue and the jobs waiting for approval")
    _common(p)
    p.add_argument("--data-dir", metavar="DIR")
    return ap


# ------------------------------------------------------------------------------ output
def _emit(result: dict, as_json: bool, text: Callable[[dict], str], out: Any = None) -> None:
    out = out or sys.stdout
    if as_json:
        out.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
    else:
        out.write(text(result).rstrip("\n") + "\n")
    out.flush()


def main(argv: Optional[list[str]] = None, out: Any = None, err: Any = None) -> int:
    """Run one command; returns the exit code (never raises SystemExit itself)."""
    args_list = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    err = err or sys.stderr
    as_json = "--json" in args_list
    parser = build_parser()
    if not args_list or args_list[0] in ("-h", "--help"):
        out.write(parser.format_help())
        return EXIT_OK if args_list else EXIT_USAGE
    try:
        try:
            args = parser.parse_args(args_list)
        except SystemExit as exc:            # --help of a subcommand
            return int(exc.code or 0)
        if args.command is None or (args.command == "queue" and getattr(args, "queue_command", None) is None):
            raise CliError("a command is required (compile, estimate, nest, validate, convert, queue add|list)",
                           EXIT_USAGE, "usage")
        from . import commands
        fn = {"compile": commands.cmd_compile, "estimate": commands.cmd_estimate, "nest": commands.cmd_nest,
              "validate": commands.cmd_validate, "convert": commands.cmd_convert}.get(args.command)
        if args.command == "queue":
            fn = {"add": commands.cmd_queue_add, "list": commands.cmd_queue_list}[args.queue_command]
        name = args.command + (f" {args.queue_command}" if args.command == "queue" else "")
        assert fn is not None
        code, result, text = fn(args)
        result = {"command": name, "ok": code == EXIT_OK, "exit_code": code, **result}
        _emit(result, as_json, text, out)
        return code
    except CliError as exc:
        res = {"command": args_list[0] if args_list else "", "ok": False, "exit_code": exc.exit_code,
               "error": {"code": exc.code, "message": str(exc), **exc.detail}}
        if as_json:
            out.write(json.dumps(res, ensure_ascii=False, default=str) + "\n")
        else:
            err.write(f"mlaser: {exc}\n")
            if exc.detail.get("candidates"):
                err.write("  candidates: " + "; ".join(exc.detail["candidates"]) + "\n")
        return exc.exit_code


__all__ = ["main", "build_parser", "is_subcommand", "SUBCOMMANDS", "EXIT_CODES", "CliError"]
