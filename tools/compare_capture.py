#!/usr/bin/env python3
"""Compare the ORIGINAL Mlaser's cutting-job stream (captured on the wire) with the rebuild.

Capture the PC <-> card traffic with Wireshark/tcpdump while the original runs a job (a Dry Cut
with the laser physically disabled first), then::

    python tools/compare_capture.py job.pcapng part.chf --params NexCut/ [--html report.html]

Inputs: the capture (pcap or pcapng; UDP, or TCP for AccessType=0), the drawing the original
ran (.chf saved from the original right before the run is best, or the .dxf), and the
directory with the machine's parameter XMLs (HardPara.xml, ManuPara.xml, LayerPara.xml from
``%LOCALAPPDATA%\\NexCut\\``).  Optional: ``File/ipAdd.ini`` (--ini) and ``JumpAddTime.txt``
(--jump) from the original's install.

The tool reconstructs the original's FIFO stream from the write(102) frames, takes the start
position from the card's position reads, compiles the same drawing with ``compile_job`` and
reports the verdict, the first divergence with context and statistics.  Exit status: 0 when the
verdict is IDENTICAL or EQUIVALENT, 1 when DIFFERENT, 2 on errors.

``--list`` lists the jobs in the capture; ``--dump FILE`` writes the decoded FIFO stream of the
selected job (no drawing needed for either).  Procedure: docs/parity.md "Verifying against your
machine".  The tool only reads files; it never talks to a card.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mlaser.parity import capture as C  # noqa: E402
from mlaser.parity import report as R  # noqa: E402


def _pair(s: str) -> tuple[float, float]:
    a, b = s.replace(";", ",").split(",")
    return float(a), float(b)


def _cam(items: list[str]) -> dict:
    from mlaser.cam.toolpath import CamParams
    out = {}
    fields = {f.name: f for f in dataclasses.fields(CamParams)}
    for it in items or []:
        k, _, v = it.partition("=")
        k = k.strip()
        if k not in fields:
            raise SystemExit(f"--cam: unknown CamParams field {k!r} (known: {', '.join(fields)})")
        cur = getattr(CamParams(), k)
        if isinstance(cur, bool):
            out[k] = v.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int):
            out[k] = int(v)
        elif isinstance(cur, float):
            out[k] = float(v)
        elif isinstance(cur, tuple):
            out[k] = _pair(v)
        else:
            out[k] = None if v.strip().lower() == "none" else float(v)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("capture", help="pcap / pcapng file")
    p.add_argument("drawing", nargs="?", help=".chf or .dxf the original ran")
    p.add_argument("--params", metavar="DIR", help="directory with HardPara.xml / ManuPara.xml / LayerPara.xml")
    p.add_argument("--ini", help="ipAdd.ini (timeouts, DAMinVal)")
    p.add_argument("--jump", help="JumpAddTime.txt")
    p.add_argument("--card", default=None, metavar="HOST[:PORT]",
                   help=f"card address (default: auto, port {C.CARD_PORT}; usually {C.CARD_HOST})")
    p.add_argument("--job", type=int, default=None, help="job index (default: the job with the most steps; -1 = last)")
    p.add_argument("--list", action="store_true", help="list the jobs in the capture and exit")
    p.add_argument("--dump", metavar="FILE", help="write the decoded FIFO stream of the selected job")
    p.add_argument("--kind", choices=("job", "frame"), default="job", help="compare with compile_job (Start/Dry Cut) "
                   "or with the Frame (边框) trace")
    p.add_argument("--dry-run", choices=("auto", "yes", "no"), default="auto")
    p.add_argument("--start", type=_pair, metavar="X,Y", help="start position in mm (default: from the capture)")
    p.add_argument("--offset", type=_pair, metavar="DX,DY",
                   help="job placement offset in mm (default: estimated; 0,0 = none)")
    p.add_argument("--scaling", choices=("card", "xml"), default="card",
                   help="step scale/period: adopt the card's (as the rebuild UI does) or the XML's")
    p.add_argument("--position-ppm", type=float, help="card position counts per mm (default: from the capture)")
    p.add_argument("--x-axis", type=int, default=0)
    p.add_argument("--y-axis", type=int, default=1)
    p.add_argument("--cam", action="append", metavar="KEY=VALUE", help="CamParams field, e.g. sort_mode=5 (repeatable)")
    p.add_argument("--tol", type=float, default=1.0, help="time-aligned deviation tolerance, pulses (default 1)")
    p.add_argument("--path-tol", type=float, default=2.0, help="spatial path tolerance, pulses (default 2)")
    p.add_argument("--context", type=int, default=5, help="steps/items shown around a divergence")
    p.add_argument("--html", metavar="FILE", help="write an HTML report (plots with matplotlib)")
    p.add_argument("--no-plots", action="store_true", help="HTML report without plots")
    p.add_argument("--json", metavar="FILE", help="write the comparison as JSON")
    p.add_argument("-q", "--quiet", action="store_true", help="print only the verdict")
    return p


def _dump(job: C.CapturedJob, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {job.summary()}\n")
        nstep = 0
        for fr in job.frames:
            f.write(f"# frame seq {fr.seq}: {len(fr.items)} words, tid {fr.tid}, sends {fr.sends}, "
                    f"resends {fr.resends}, {'ok' if fr.ok else 'NO OK REPLY'}\n")
        for i, w in enumerate(job.wire):
            op = w[0] & 0xFFFF
            f.write(f"{i}\t{nstep}\t{C.describe(op, w[1:])}\n")
            if op == C.F.OP_STEP:
                nstep += 1


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    host, port = None, C.CARD_PORT
    if a.card:
        host, _, ps = a.card.partition(":")
        port = int(ps) if ps else C.CARD_PORT
    try:
        tr = C.load_traffic(a.capture, host or None, port)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    an = C.analyse(tr, a.x_axis, a.y_axis)
    if a.list or (a.dump and not a.drawing):
        print(f"card {tr.card[0]}:{tr.card[1]} ({tr.transport}), {len(tr.exchanges)} requests, "
              f"{tr.retransmissions} retransmissions, {len(an.jobs)} job(s)")
        for j in an.jobs:
            print("  " + j.summary())
        if a.dump and an.jobs:
            _dump(C.select_job(an, a.job), a.dump)
            print(f"wrote {a.dump}")
        return 0 if an.jobs else 2
    if not a.drawing:
        print("error: a drawing (.chf/.dxf) is needed for the comparison (or use --list / --dump)", file=sys.stderr)
        return 2
    inp = C.RebuildInputs(a.drawing, a.params, a.ini, a.jump, _cam(a.cam), a.kind,
                          None if a.dry_run == "auto" else a.dry_run == "yes", a.start, a.offset, a.scaling)
    try:
        res = C.run(a.capture, inp, host or None, port, a.job,
                    C.Settings(a.tol, a.path_tol, a.context), a.x_axis, a.y_axis, a.position_ppm, traffic=tr)
    except (OSError, ValueError, IndexError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if a.dump:
        _dump(res.job, a.dump)
    text = R.text_report(res, a.capture, a.drawing)
    print(res.comparison.verdict if a.quiet else text, end="\n" if a.quiet else "")
    if a.html:
        with open(a.html, "w", encoding="utf-8") as f:
            f.write(R.html_report(res, a.capture, a.drawing, plots=not a.no_plots))
        if not a.quiet:
            print(f"HTML report: {a.html}")
    if a.json:
        d = res.comparison.to_dict()
        d.update(notes=res.notes, job=res.job.summary(), start_mm=res.start_mm, offset_mm=res.offset_mm,
                 dry_run=res.dry_run)
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, default=str)
    return 0 if res.comparison.verdict.startswith(("IDENTICAL", "EQUIVALENT")) else 1


if __name__ == "__main__":
    sys.exit(main())
