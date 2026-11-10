#!/usr/bin/env python3
"""Bench check: what does the motion card do with a FIFO frame it receives twice?

When a reply is lost the client sends the identical request again (the original's retry policy,
docs/re/protocol.md §1).  For a FIFO frame (write 102) the card has already queued the first
copy; what it does with the second one is not known (docs/dev/BENCH_CHECKS.md).  This tool sends
one small FIFO frame twice, as the same datagram (same transaction id, same sequence number),
and reads the card's answers and its free FIFO bytes (RO[16]) after each copy:

* the second copy queued again (free bytes drop twice)    -> ``accept-twice``
* the second copy acknowledged and not queued              -> ``dedupe``
* the second copy answered with an exception               -> ``reject``

By default NOTHING MOVES: the FIFO is never started and is cleared at the end.  ``--run`` then
also starts the FIFO and measures the X travel (the frame's steps once or twice), every step
with duty 0 (beam off), PWM off before and after.

READ docs/dev/BENCH_CHECKS.md FIRST.  On a real card: laser key off (laser source switched off),
no sheet, head in the middle of the table, the emergency stop within reach, the original program
and mlaser closed, a packet capture running.  The tool refuses a real card without
``--laser-disabled``.

Examples::

    python tools/bench_duplicate_frame.py --sim dedupe              # rehearsal on the simulator
    python tools/bench_duplicate_frame.py --card 10.1.1.168 --laser-disabled
    python tools/bench_duplicate_frame.py --card 10.1.1.168 --laser-disabled --run

Exit status: 0 with a verdict, 1 when the result fits no model, 2 when the card was not ready.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mlaser.controller.client import ControllerError, MotionController, TransportConfig  # noqa: E402
from mlaser.protocol import frames as F  # noqa: E402

STEPS = 50                    # step items in the frame
DX = 4                        # pulses per step on X (50 x 4 = 200 pulses: 2 mm at 100 pulses/mm)
PWM_FREQ = 5000


@dataclass
class Result:
    verdict: str = "unknown"
    replies: list = field(default_factory=list)       # "ok" / "exception N" / "no reply"
    free: list = field(default_factory=list)          # RO[16] before, after copy 1, after copy 2
    seq: list = field(default_factory=list)           # RO[15] at the same moments
    travel: Optional[int] = None                      # X pulses with --run
    expected_once: int = STEPS * DX
    notes: list = field(default_factory=list)


def _send_raw(host: str, port: int, frame: bytes, timeout_s: float) -> str:
    """One datagram, one answer (no retry): 'ok', 'exception N' or 'no reply'."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout_s)
        s.sendto(frame, (host, port))
        try:
            data, _ = s.recvfrom(2048)
        except OSError:
            return "no reply"
    rep = F.decode_response(data)
    if rep.is_exception:
        return f"exception {rep.exception}"
    return "ok" if rep.func == F.FUNC_WRITE and rep.addr == F.REG_FIFO_DATA else f"unexpected reply {rep.as_data()}"


def _safe_end(mc: MotionController) -> None:
    for fn in (mc.fifo_stop, lambda: mc.command(F.mailbox_sys(F.SYS_PWM, PWM_FREQ, 0, 0)),
               lambda: mc.command(F.mailbox_sys(F.SYS_PWM_BUS, PWM_FREQ, 0, 0)), mc.fifo_clear):
        try:
            fn()
        except ControllerError as e:
            print(f"warning: {e}", file=sys.stderr)


def check(host: str, port: int, run: bool = False, timeout_s: float = 0.6) -> Result:
    res = Result()
    with MotionController(config=TransportConfig(host=host, port=port)) as mc:
        st = mc.read_status()
        if st.machine_state() != 0 or st.alarms() or st.running:
            res.verdict = "not ready"
            res.notes.append(f"card not idle (state {st.machine_state()}, RO[19] {st.ro[F.RO_RUNNING]:#x}, "
                             f"alarms {[a.code for a in st.alarms()]}): home, clear alarms, try again")
            return res
        mc.command(F.mailbox_sys(F.SYS_PWM, PWM_FREQ, 0, 0))        # beam off before anything
        mc.fifo_job_reset()
        mc.fifo_clear()
        st = mc.read_status()
        res.free.append(st.fifo_free)
        res.seq.append(st.fifo_seq)
        x0 = st.axes[0].position
        frame = F.build_fifo_frames([F.encode_step(DX, 0, PWM_FREQ, 0) for _ in range(STEPS)],
                                    st.fifo_seq + 1)[0]
        raw = F.encode_request(F.Request(F.FUNC_WRITE, F.REG_FIFO_DATA, len(frame), list(frame), tid=0x5A5A))
        try:
            for _copy in (1, 2):                                      # the same datagram twice
                res.replies.append(_send_raw(host, port, raw, timeout_s))
                st = mc.read_status()
                res.free.append(st.fifo_free)
                res.seq.append(st.fifo_seq)
            q1, q2 = res.free[0] - res.free[1], res.free[1] - res.free[2]
            if res.replies[0] != "ok" or q1 <= 0:
                res.verdict = "unknown"
                res.notes.append("the first copy was not acknowledged and queued: the card did not "
                                 "take the frame at all (see the capture)")
            elif res.replies[1].startswith("exception") and q2 == 0:
                res.verdict = "reject"
            elif res.replies[1] == "ok" and q2 == 0:
                res.verdict = "dedupe"
            elif res.replies[1] == "ok" and q2 == q1:
                res.verdict = "accept-twice"
            else:
                res.notes.append(f"second copy: {res.replies[1]}, {q2} bytes queued (first: {q1})")
            if run and res.verdict != "not ready":
                mc.fifo_step_format(0x3)
                mc.fifo_start()
                t_end = time.monotonic() + 10.0
                time.sleep(0.2)
                while time.monotonic() < t_end:
                    st = mc.read_status()
                    if not st.running and st.fifo_free >= res.free[0]:
                        break
                    time.sleep(0.05)
                res.travel = mc.read_status().axes[0].position - x0
                once, twice = res.expected_once, 2 * res.expected_once
                seen = {once: "once", twice: "twice"}.get(res.travel, f"{res.travel} pulses")
                res.notes.append(f"--run: the card executed the frame {seen} "
                                 f"(once = {once} pulses, twice = {twice})")
        finally:
            _safe_end(mc)
    return res


def model_of(res: Result) -> Optional[str]:
    """The simulator model (``CardSimulator(duplicate_fifo=...)``) the result calls for."""
    if res.verdict in ("accept-twice", "dedupe", "reject"):
        if res.travel is not None and res.travel not in (res.expected_once, 2 * res.expected_once):
            return None
        if res.travel is not None and (res.travel == 2 * res.expected_once) != (res.verdict == "accept-twice"):
            return None                       # the free bytes and the travel disagree
        return res.verdict
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--card", metavar="HOST[:PORT]", help="the real card (UDP, default port 502)")
    g.add_argument("--sim", choices=("accept-twice", "dedupe", "reject"),
                   help="rehearsal: a simulator with this duplicate-frame model")
    ap.add_argument("--laser-disabled", action="store_true",
                    help="confirm: laser key off, laser source off, no sheet, e-stop within reach")
    ap.add_argument("--run", action="store_true", help="also start the FIFO and measure the X travel (moves X)")
    a = ap.parse_args(argv)
    sim = None
    if a.sim:
        from mlaser.controller.simulator import CardSimulator
        sim = CardSimulator(duplicate_fifo=a.sim).start()
        host, port = "127.0.0.1", sim.port
    else:
        if not a.laser_disabled:
            print("refused: read docs/dev/BENCH_CHECKS.md, switch the laser off and pass --laser-disabled",
                  file=sys.stderr)
            return 2
        host, _, p = a.card.partition(":")
        port = int(p or 502)
    try:
        res = check(host, port, run=a.run)
    finally:
        if sim is not None:
            sim.stop()
    print(f"replies to the two copies: {res.replies}")
    print(f"free FIFO bytes (RO[16]) before / after copy 1 / after copy 2: {res.free}")
    print(f"frame sequence (RO[15]) at the same moments: {res.seq}")
    for n in res.notes:
        print(n)
    model = model_of(res)
    print(f"verdict: {res.verdict}" + (f"  ->  simulator default: duplicate_fifo={model!r}" if model else ""))
    if res.verdict == "not ready":
        return 2
    return 0 if model else 1


if __name__ == "__main__":
    sys.exit(main())
