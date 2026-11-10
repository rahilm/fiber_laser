"""A stalled log disk never delays a halt (review fw F2, docs/SAFETY.md §7).

The halt channel audits every command it sends.  The audit and log file handlers sit behind a
queue (``mlaser.machine.logsetup``): the calling thread only formats and enqueues, one writer
thread writes the files.  Here every write of ``audit.log`` is stalled by STALL seconds (an
antivirus-held write, a rollover on a slow disk): ``safe_halt`` stays fast, the first halt
command reaches the simulated card before the first audit write is done, and afterwards the
audit trail is complete and in order.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from mlaser.controller.client import TransportConfig
from mlaser.controller.simulator import CardSimulator
from mlaser.controller.supervisor import SafetySupervisor
from mlaser.machine import logsetup
from mlaser.machine.audit import audit
from mlaser.machine.safety import SafetyFSM
from mlaser.protocol import frames as F

pytestmark = pytest.mark.safety

STALL = 0.25


def _audit_handler(d):
    hs = logsetup._INSTALLED[str(d.resolve())]
    return next(h for h in hs if getattr(h, "baseFilename", "").endswith("audit.log"))


def test_safe_halt_does_not_wait_for_a_stalled_audit_write(tmp_path):
    d = logsetup.setup_logging(tmp_path)
    sim = CardSimulator().start()
    sup = SafetySupervisor(SafetyFSM())
    card_t: list[float] = []            # perf_counter of every mailbox write the card got
    written: list[tuple[float, dict]] = []
    try:
        real_handle = sim.handle

        def handle(data: bytes):
            try:
                req = F.decode_request(data)
                if req.func == F.FUNC_WRITE:
                    card_t.append(time.perf_counter())
            except F.FrameError:
                pass
            return real_handle(data)
        sim.handle = handle
        sup.open(TransportConfig(host="127.0.0.1", port=sim.port), 1000.0)
        assert logsetup.flush_logging(5.0)

        aud = _audit_handler(d)
        real_emit = aud.emit
        writer = []

        def slow_emit(record):
            writer.append(threading.current_thread().name)
            time.sleep(STALL)                      # the stalled disk
            real_emit(record)
            written.append((time.perf_counter(), getattr(record, "audit", {})))
        aud.emit = slow_emit

        card_t.clear()
        t0 = time.perf_counter()
        res = sup.safe_halt("stalled disk test")
        dt = time.perf_counter() - t0
        assert res.ok, res.steps
        halt_cmds = sum(1 for n, ok, _e in res.steps if ok)
        # before the fix: one stall per halt command (6 x 0.25 s = 1.5 s); now the sequence
        # takes what the card takes (a few ms on the simulator)
        assert dt < STALL, f"safe_halt took {dt * 1000:.0f} ms behind a stalled audit write"
        assert card_t, "no halt command reached the card"
        assert logsetup.flush_logging(STALL * (halt_cmds + 4) + 5.0)
        assert written, "nothing was written to audit.log"
        halt_written = [t for t, rec in written if rec.get("channel") == "halt"]
        assert halt_written
        # the first halt command was on the card before the first halt audit write was done
        assert card_t[0] < halt_written[0]
        # no thread but the log writer touched the file
        assert set(writer) == {"mlaser-log"}
    finally:
        sup.close()
        sim.stop()
        logsetup.teardown_logging(tmp_path)

    recs = [json.loads(line) for line in (d / "audit.log").read_text("utf-8").splitlines()]
    halt_cmds_logged = [r for r in recs if r.get("event") == "command" and r.get("channel") == "halt"]
    halt_summary = [i for i, r in enumerate(recs) if r.get("event") == "halt"
                    and r.get("reason") == "stalled disk test"]
    assert len(halt_cmds_logged) >= halt_cmds
    assert halt_summary
    # complete and ordered: each command record of the sequence comes before its summary, and
    # time stamps are those of the calls (non-decreasing in file order)
    last_cmd = max(i for i, r in enumerate(recs) if r.get("event") == "command" and r.get("channel") == "halt")
    assert last_cmd < halt_summary[-1]
    ts = [r["ts"] for r in recs if r.get("channel") == "halt" or r.get("event") == "halt"]
    assert ts == sorted(ts)


def test_records_keep_call_time_and_order_and_are_written_off_thread(tmp_path):
    d = logsetup.setup_logging(tmp_path)
    try:
        aud = _audit_handler(d)
        real_emit = aud.emit
        gate = threading.Event()

        def held_emit(record):
            gate.wait(5.0)                         # the disk is stuck until released
            real_emit(record)
        aud.emit = held_emit
        t0 = time.perf_counter()
        for i in range(50):
            audit("command", seq=i, channel="halt")
        assert time.perf_counter() - t0 < 0.5       # the callers never waited for the disk
        logging.getLogger("mlaser.test").warning("plain log line")
        assert not logsetup.flush_logging(0.05)     # still stuck: reported, bounded
        gate.set()
        assert logsetup.flush_logging(5.0)
    finally:
        logsetup.teardown_logging(tmp_path)
    recs = [json.loads(line) for line in (d / "audit.log").read_text("utf-8").splitlines()]
    seqs = [r["seq"] for r in recs if r.get("event") == "command"]
    assert seqs == list(range(50))
    assert "plain log line" in (d / "mlaser.log").read_text("utf-8")


def test_exit_drain_then_direct_writes(tmp_path):
    """At interpreter exit the queue is drained and later records (the exit hooks' halts and
    audits) are written directly."""
    d = logsetup.setup_logging(tmp_path)
    try:
        audit("before_exit")
        logsetup._at_exit()
        audit("after_exit_hook")
        recs = [json.loads(line)["event"] for line in (d / "audit.log").read_text("utf-8").splitlines()]
        assert recs[-2:] == ["before_exit", "after_exit_hook"]
    finally:
        logsetup.teardown_logging(tmp_path)
