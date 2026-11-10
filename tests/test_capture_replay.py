"""Replay of real card captures through the production decoder (review arch.md A4).

The repository holds NO capture (public repository: no capture, parameter file or vendor data is
ever committed).  The owner runs this against their own captures, locally::

    MLASER_CAPTURE_DIR=/path/to/captures uv run pytest tests/test_capture_replay.py -q

Every ``*.pcap`` / ``*.pcapng`` there (UDP, or TCP for AccessType=0; the motion card on port 502)
is read with the parity tooling (``mlaser.parity.pcap`` / ``mlaser.parity.capture``, the reader of
tools/compare_capture.py), and each request / reply pair goes through the production decoder
(``mlaser.protocol.frames``) and the status interpretation (``MCStatus``, ``alarms``).  The test
fails on a decode error, on a function, register, mailbox command or FIFO opcode outside the
classes docs/re/protocol.md documents, and on a broken documented invariant:

* transaction ids: one client numbers its requests consecutively (protocol.md §1);
* FIFO frame sequence numbers: consecutive within a job, from 1 after the job reset [9999, 16]
  (§5, §15), no gaps, no conflicting re-sends;
* job start: the step format [9999, 1, mask|0x66800000, 0] before the FIFO start (§15);
* RO[19]: only the documented bits (low byte 0/1, 0x100 data, 0x200 executing), executing implies
  the low byte 1, low byte 0 between a job's FIFO clear and its start and after its FIFO stop
  (§15; 100 ms grace after the stop);
* RO[6] / RO[7]: only the documented alarm bits (§13);
* normal end: 103 := 3, then the PWM off [9999, 3, f, 0, ...] (§15);
* a stop / pause: it starts with the decelerated stop [1, 0x1f, ...] or 103 := 3, the PWM off
  comes before the follower stop and the FIFO clear, no FIFO start and no FIFO frame inside it
  (§14).

Out of scope: registers 59500 and 59503 (licence / authorisation traffic).  A request whose
register range touches them, and its reply, are dropped before anything is decoded; they are
never decoded, printed or asserted on.  The skip reads only the function byte and the address and
count fields of the request header, and the transaction id of the reply.

When ``MLASER_CAPTURE_DIR`` is unset the owner test is skipped.  Tiny synthetic captures recorded
from the simulator by the tests below keep the harness itself exercised in CI.
Procedure: docs/dev/TESTING.md "Replaying your own captures".
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

import wire_reference as W
from mlaser.parity import capture as C
from mlaser.parity import pcap as P
from mlaser.parity import synth as S

pytestmark = pytest.mark.sim

CAPTURE_ENV = "MLASER_CAPTURE_DIR"
CARD_PORT = 502
#: licence / authorisation traffic: out of scope, skipped entirely (never decoded, printed or
#: asserted on), docs/re/protocol.md §8
OUT_OF_SCOPE_REGS = (59500, 59503)
STOP_GRACE_S = 0.1

# ------------------------------------------------------------- documented classes (protocol.md)
#: §2: functions of the motion card's frame format
FUNCTIONS = {0x30: "read", 0x40: "write", 0x20: "bytes", 0x21: "bytes", 0x26: "bytes at"}
#: §3 register map: the first register of a request must fall in one of these
REGISTER_CLASSES = [
    (100, 103, "control 100, mailbox 101, FIFO data 102, FIFO control 103"),
    (150, 151, "150 / 151 (use unknown)"),
    (1000, 1099, "status RO block, FIFO capacity 1032, 1050..1052"),
    (2000, 2099, "axis RO"),
    (5000, 5008, "RW word 5000, 5001"),
    (10000, 10017, "on-board follower status"),
    (12000, 12999, "VM +0x250 read (use unknown)"),
    (50000, 50399, "system and axis parameters"),
    (59501, 59511, "59501 / 59510 / 59511 (use unknown)"),
    (59600, 59631, "hardware parameter blocks"),
    (60000, 60144, "fast-status map and fast status"),
    (63000, 63127, "servo info"),
]
#: §4 mailbox commands (first word) and the system sub-commands of [9999, sub, ...] (§4, §15)
MAILBOX_OPS = {9999, 1, 2, 3, 5, 101, 103, 117, 118}
MAILBOX_SYS = {1, 2, 3, 4, 5, 8, 13, 16, 17}
#: §5 FIFO opcodes and the [9999, sub, ...] items (§5, §10.2)
FIFO_OPS = {3, 103, 104, 105, 106, 108, 109, 118, 2001, 2004, 3000, 3001, 3002, 9999}
FIFO_SYS = {2, 3, 4, 13, 17, 18}
#: §13 alarm bits: RO[6] b <= 24 axis b, 25, 26, 29, 30; RO[7] bits 0..7
ALARM1_BITS = set(range(25)) | {25, 26, 29, 30}
ALARM2_BITS = set(range(8))
#: §15 RO[19]
PROC_BITS = 0x1 | 0x100 | 0x200


# ------------------------------------------------------------- the out-of-scope skip
def _touches_out_of_scope(raw: bytes) -> bool:
    """True when a request addresses register 59500 or 59503.  Reads only the function byte and
    the address / count fields of the header (protocol.md §2): nothing else of the frame."""
    if len(raw) < 14 or raw[7] not in (0x30, 0x40, 0x26):
        return False
    addr = int.from_bytes(raw[8:12], "little")
    count = int.from_bytes(raw[12:14], "little")
    span = 2 * max(1, count)          # 32-bit or 16-bit addressing: the wider span
    return any(addr <= r < addr + span for r in OUT_OF_SCOPE_REGS)


def in_scope_messages(messages, card_port: int = CARD_PORT) -> tuple[list, dict]:
    """The capture's card messages without the out-of-scope exchanges.  Also returns, per client,
    the positions of the skipped requests (``{client: [index in that client's requests]}``) so
    that the transaction-id check can count them without looking at them."""
    out = []
    skipped_tids: dict = {}
    skipped_pos: dict = {}
    seen: dict = {}
    for m in messages:
        if m.dst[1] == card_port:
            n = seen.get(m.src, 0)
            seen[m.src] = n + 1
            if _touches_out_of_scope(m.data):
                skipped_tids.setdefault(m.src, set()).add(bytes(m.data[:2]))
                skipped_pos.setdefault(m.src, []).append((n, bytes(m.data)))
                continue
        elif m.src[1] == card_port:
            tids = skipped_tids.get(m.dst)
            if tids and bytes(m.data[:2]) in tids:
                tids.discard(bytes(m.data[:2]))
                continue
        out.append(m)
    return out, skipped_pos


# ------------------------------------------------------------- the checks
@dataclass
class Replay:
    path: str
    problems: list = field(default_factory=list)
    exchanges: int = 0
    jobs: int = 0
    ro_samples: int = 0

    def fail(self, msg: str) -> None:
        if len(self.problems) < 200:
            self.problems.append(msg)


def _reg_class(addr: int) -> Optional[str]:
    for lo, hi, name in REGISTER_CLASSES:
        if lo <= addr <= hi:
            return name
    return None


def _check_classes(rp: Replay, tr) -> None:
    from mlaser.protocol import frames as F
    for ex in tr.exchanges:
        rq = ex.request
        if rq is None:
            rp.fail(f"packet {ex.packet}: request not decodable ({ex.decode_error})")
            continue
        if rq.func not in FUNCTIONS:
            rp.fail(f"packet {ex.packet}: function {rq.func:#x} not documented")
            continue
        if rq.func in (0x30, 0x40) and _reg_class(rq.addr) is None:
            rp.fail(f"packet {ex.packet}: register {rq.addr} (func {rq.func:#x}) outside the documented map")
        if ex.reply is not None and not ex.reply.is_exception and ex.result != F.ERR_OK:
            rp.fail(f"packet {ex.packet}: reply does not match the request (result {ex.result})")
        if rq.func != 0x40:
            continue
        if rq.addr == 101 and rq.values:
            op = rq.values[0]
            if op not in MAILBOX_OPS:
                rp.fail(f"packet {ex.packet}: mailbox command {rq.values[:3]} not documented")
            elif op == 9999 and (len(rq.values) < 2 or rq.values[1] not in MAILBOX_SYS):
                rp.fail(f"packet {ex.packet}: system command {rq.values[:3]} not documented")
        elif rq.addr == 102:
            try:
                items = F.parse_fifo_items(rq.values[1:])
            except F.FrameError as e:
                rp.fail(f"packet {ex.packet}: FIFO frame not decodable ({e})")
                continue
            for op, params in items:
                if op not in FIFO_OPS:
                    rp.fail(f"packet {ex.packet}: FIFO opcode {op} not documented")
                elif op == 9999 and (not params or params[0] not in FIFO_SYS):
                    rp.fail(f"packet {ex.packet}: FIFO system item {params[:2]} not documented")


def _check_tids(rp: Replay, messages, skipped_pos: dict, card_port: int) -> None:
    """One client numbers its requests consecutively (§1); an identical re-send keeps its id."""
    by_client: dict = {}
    for m in messages:
        if m.dst[1] == card_port:
            by_client.setdefault(m.src, []).append(m)
    for client, reqs in by_client.items():
        skipped = dict(skipped_pos.get(client, []))
        n_total = len(reqs) + len(skipped)
        prev_tid: Optional[int] = None
        prev_raw = b""
        gap = 0                       # skipped (out-of-scope) requests since the last checked one
        prev_skip = b""
        it = iter(reqs)
        for k in range(n_total):
            if k in skipped:
                raw = skipped[k]
                if raw != prev_skip:  # an identical re-send of a skipped request is not new
                    gap += 1
                prev_skip = raw
                continue
            m = next(it)
            tid = int.from_bytes(m.data[:2], "big")
            if prev_tid is not None and not (m.data == prev_raw and gap == 0):
                if tid != (prev_tid + 1 + gap) & 0xFFFF:
                    rp.fail(f"client {client}: transaction id {tid} after {prev_tid}"
                            f" ({gap} skipped between): not consecutive")
            prev_tid, prev_raw, gap, prev_skip = tid, bytes(m.data), 0, b""


def _status(ro: dict):
    from mlaser.controller.client import MCStatus
    return MCStatus([ro.get(i, 0) for i in range(36)], [], 1000.0)


def _check_status(rp: Replay, an) -> None:
    from mlaser.controller import alarms as A
    for s in an.ro_samples:
        if not all(i in s.ro for i in (6, 7, 19)):
            continue
        rp.ro_samples += 1
        st = _status(s.ro)
        v = st.ro[19]
        if v & ~PROC_BITS:
            rp.fail(f"t={s.ts:.3f}: RO[19] = {v:#x} has bits outside the documented 0x301")
        if st.running and not st.program_active:
            rp.fail(f"t={s.ts:.3f}: RO[19] = {v:#x}: executing (0x200) without the low byte 1")
        bad1 = [b for b in range(32) if st.alarm_word1 >> b & 1 and b not in ALARM1_BITS]
        bad2 = [b for b in range(32) if st.alarm_word2 >> b & 1 and b not in ALARM2_BITS]
        if bad1 or bad2:
            rp.fail(f"t={s.ts:.3f}: undocumented alarm bits RO[6] {bad1} RO[7] {bad2}")
        try:
            st.machine_state()
            A.decode(st.alarm_word1, st.alarm_word2, [])
        except Exception as e:  # noqa: BLE001 - the interpretation must take any status word
            rp.fail(f"t={s.ts:.3f}: status interpretation failed: {type(e).__name__}: {e}")


def _proc_after(an, t0: float, t1: float) -> list:
    return [(s.ts, s.ro[19]) for s in an.ro_samples if 19 in s.ro and t0 < s.ts < t1]


def _check_job(rp: Replay, an, job) -> None:
    from mlaser.protocol import frames as F
    tag = f"job {job.index}"
    if job.seq_gaps:
        rp.fail(f"{tag}: FIFO sequence gaps {job.seq_gaps[:5]}")
    if job.conflicts:
        rp.fail(f"{tag}: {len(job.conflicts)} FIFO sequence numbers re-sent with other content")
    seqs = [fr.seq for fr in job.frames]
    if any((b - a) & 0xFFFFFFFF != 1 for a, b in zip(seqs, seqs[1:])):
        rp.fail(f"{tag}: FIFO sequence numbers not consecutive: {seqs[:8]}...")
    before = [e for e in an.events if e.ts < job.clear_ts and e.kind in ("mailbox", "fifo_clear", "fifo_start",
                                                                         "fifo_stop", "frame")]
    if before and before[-1].kind == "mailbox" and before[-1].words[:2] == [9999, 16]:
        if seqs and seqs[0] != 1:
            rp.fail(f"{tag}: first frame {seqs[0]} after the job reset [9999, 16] (documented: 1)")
    elif job.first_seq_expected is not None and seqs and seqs[0] != job.first_seq_expected:
        rp.fail(f"{tag}: first frame {seqs[0]}, RO[15] + 1 was {job.first_seq_expected}")
    clear_t = job.events[0].exchange.data_ts if job.events else job.clear_ts
    if job.start_ts is not None:
        fmt = [e for e in job.events if e.kind == "mailbox" and e.ts < job.start_ts
               and e.words[:2] == [9999, 1] and len(e.words) > 2
               and (e.words[2] & 0xFFF00000) == F.STEP_FORMAT_STEP_CARD]
        if not fmt:
            rp.fail(f"{tag}: no step format [9999, 1, mask|0x66800000, 0] before the FIFO start")
        for ts, v in _proc_after(an, clear_t, job.start_ts):
            if v & 0xFF or v & 0x200:
                rp.fail(f"{tag}: t={ts:.3f}: RO[19] = {v:#x} between the FIFO clear and the start")
    stop_seq = job.stop_sequence()
    stops = [e for e in job.events if e.kind == "fifo_stop" and (job.start_ts is None or e.ts >= job.start_ts)]
    if stops:
        st0 = stops[0]
        nxt = [e.ts for e in job.events if e.kind == "fifo_start" and e.ts > st0.ts]
        t_end = min(nxt) if nxt else job.end_window_ts
        for ts, v in _proc_after(an, st0.exchange.data_ts + STOP_GRACE_S, t_end):
            if v & 0xFF or v & 0x200:
                rp.fail(f"{tag}: t={ts:.3f}: RO[19] = {v:#x} after the FIFO stop (documented: 0)")
        if not stop_seq:
            # normal end: 103 := 3, then the PWM off (§15)
            after = [e for e in job.events if e.ts > st0.ts and e.kind == "mailbox"]
            if not any(e.words[:2] == [9999, 3] and len(e.words) > 3 and e.words[3] == 0 for e in after):
                rp.fail(f"{tag}: no PWM off [9999, 3, f, 0, ...] after the end-of-job FIFO stop")
    if stop_seq:
        _check_stop(rp, tag, job, stop_seq)


def _check_stop(rp: Replay, tag: str, job, stop_seq: list) -> None:
    reg0, w0 = stop_seq[0]
    if not ((reg0 == 101 and w0[:2] == [1, 0x1F]) or (reg0 == 103 and w0 == [3])):
        rp.fail(f"{tag}: the stop starts with {reg0} := {w0} (documented: [1, 0x1f, ...] or 103 := 3)")
    idx = {k: i for i, k in reversed(list(enumerate(
        ("pwm" if (r == 101 and w[:2] == [9999, 3] and len(w) > 3 and w[3] == 0) else
         "follower" if (r == 101 and w[:2] == [118, 5]) else
         "clear" if (r == 103 and w == [1]) else
         "start" if (r == 103 and w == [2]) else "other") for r, w in stop_seq)))}
    if "pwm" not in idx:
        rp.fail(f"{tag}: no PWM off [9999, 3, f, 0, ...] in the stop sequence")
    else:
        for later in ("follower", "clear"):
            if later in idx and idx[later] < idx["pwm"]:
                rp.fail(f"{tag}: {later} before the PWM off in the stop sequence")
    if "start" in idx:
        rp.fail(f"{tag}: a FIFO start (103 := 2) inside the stop sequence")
    first = next(e.ts for e in job.events if (e.reg, e.words) == (reg0, w0) and
                 (job.start_ts is None or e.ts >= job.start_ts))
    late = [fr.seq for fr in job.frames if fr.ts > first]
    if late:
        rp.fail(f"{tag}: FIFO frames {late[:5]} sent after the stop began")


def replay(path, card_port: int = CARD_PORT) -> Replay:
    """Read one capture with the parity tooling and check it.  Never prints anything."""
    rp = Replay(str(path))
    msgs = P.read_messages(path, card_port)
    msgs, skipped = in_scope_messages(msgs, card_port)
    if not msgs:
        rp.fail("no motion-card traffic in the capture")
        return rp
    tr = C.parse_traffic(msgs, card_port=card_port)
    rp.exchanges = len(tr.exchanges)
    if tr.undecodable_requests:
        rp.fail(f"{tr.undecodable_requests} requests not decodable")
    if tr.undecodable_replies:
        rp.fail(f"{tr.undecodable_replies} replies not decodable")
    if tr.orphan_replies > 1:              # one at the very start: the capture began mid-exchange
        rp.fail(f"{tr.orphan_replies} replies without a request")
    _check_classes(rp, tr)
    _check_tids(rp, msgs, skipped, card_port)
    an = C.analyse(tr)
    _check_status(rp, an)
    rp.jobs = len(an.jobs)
    for job in an.jobs:
        _check_job(rp, an, job)
    return rp


# ------------------------------------------------------------- the owner's captures
def _owner_captures() -> list:
    d = os.environ.get(CAPTURE_ENV, "")
    if not d:
        return [pytest.param(None, id="no-captures", marks=pytest.mark.skip(
            reason=f"set {CAPTURE_ENV} to a directory with your own .pcap/.pcapng captures "
                   "to replay them (docs/dev/TESTING.md)"))]
    files = sorted(p for p in Path(d).iterdir() if p.suffix.lower() in (".pcap", ".pcapng"))
    if not files:
        return [pytest.param(None, id="no-captures", marks=pytest.mark.skip(
            reason=f"no .pcap/.pcapng file in {CAPTURE_ENV}={d}"))]
    return [pytest.param(p, id=p.name) for p in files]


@pytest.mark.parametrize("path", _owner_captures())
def test_owner_capture_replays_cleanly(path):
    rp = replay(path)
    assert not rp.problems, f"{len(rp.problems)} problems in {Path(path).name}:\n" + "\n".join(rp.problems)
    assert rp.exchanges > 0


# ------------------------------------------------------------- synthetic captures (CI)
def _record(scenario, **sim_kw) -> list:
    """Run ``scenario(mc)`` against a fresh simulator and return the exchanges as capture
    messages with the machine's addresses (mlaser.parity.synth)."""
    import time

    from mlaser.controller.client import MotionController, TransportConfig
    from mlaser.controller.simulator import CardSimulator
    sim = CardSimulator(time_scale=2.0, **sim_kw)
    log: list = []
    lock = threading.Lock()
    orig = sim.handle

    def handle(data: bytes):
        t0 = time.time()
        out = orig(data)
        with lock:
            log.append(S.Record(t0, bytes(data), out, time.time()))
        return out

    sim.handle = handle
    sim.start()
    try:
        with MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port)) as mc:
            mc.connect()
            mc.read_status()
            scenario(mc)
            for _ in range(3):
                mc.read_status()
    finally:
        sim.stop()
    return S.to_messages(log)


def _job(n: int = 900):
    from mlaser.controller.stream import WireJob
    from mlaser.protocol import frames as F
    wire = [F.encode_step(8, 3, 5000, 40) for _ in range(n)]
    return WireJob(wire, period_ms=1.0)


def _run_to_end(mc) -> None:
    from mlaser.controller.stream import JobStreamer
    JobStreamer(mc, prefill_ms=300, pwm_freq=5000).run_job(_job(), timeout_s=30)


def _run_and_stop(mc) -> None:
    from mlaser.controller.stream import JobStreamer
    from mlaser.controller.supervisor import HaltParams, run_halt
    cancel = threading.Event()
    js = JobStreamer(mc, prefill_ms=300, pwm_freq=5000)
    timer = threading.Timer(0.25, cancel.set)
    timer.start()
    js.run_job(_job(3000), cancel=cancel, timeout_s=30)
    timer.cancel()
    assert run_halt(mc, HaltParams(decel=4000, pwm_freq=5000, outputs_mask=1), "test stop").ok


@pytest.fixture(scope="module")
def synthetic():
    return {"end": _record(_run_to_end), "stop": _record(_run_and_stop)}


def _write(tmp_path, msgs, name, **kw) -> Path:
    p = tmp_path / name
    P.write_capture(p, msgs, **kw)
    return p


@pytest.mark.parametrize("which,fmt,transport", [("end", "pcap", "udp"), ("stop", "pcapng", "udp"),
                                                 ("end", "pcapng", "tcp")])
def test_synthetic_capture_replays_cleanly(synthetic, tmp_path, which, fmt, transport):
    msgs = synthetic[which]
    if transport == "tcp":
        from dataclasses import replace
        msgs = [replace(m, transport="tcp") for m in msgs]
    rp = replay(_write(tmp_path, msgs, f"{which}.{fmt}", fmt=fmt, transport=transport))
    assert not rp.problems, "\n".join(rp.problems)
    assert rp.jobs == 1 and rp.ro_samples > 5 and rp.exchanges > 20


def _req(m) -> Optional[W.Frame]:
    if m.dst[1] != CARD_PORT:
        return None
    try:
        return W.decode_request(m.data)
    except W.WireError:
        return None


def _drop_pair(msgs, pred) -> list:
    """Remove the first request matching ``pred`` and its reply."""
    for i, m in enumerate(msgs):
        f = _req(m)
        if f is not None and pred(f):
            out = msgs[:i] + msgs[i + 1:]
            for j in range(i, len(out)):
                if out[j].src[1] == CARD_PORT and out[j].data[:2] == m.data[:2]:
                    return out[:j] + out[j + 1:]
            return out
    raise AssertionError("no such request")


def _problems(tmp_path, msgs) -> str:
    return "\n".join(replay(_write(tmp_path, msgs, "edited.pcap")).problems)


def test_harness_sees_a_missing_fifo_frame(synthetic, tmp_path):
    msgs = _drop_pair(synthetic["end"], lambda f: f.func == W.WRITE and f.addr == W.REG_FIFO and f.values[0] == 2)
    p = _problems(tmp_path, msgs)
    assert "sequence gaps" in p and "transaction id" in p, p


def test_harness_sees_a_missing_step_format(synthetic, tmp_path):
    msgs = _drop_pair(synthetic["end"], lambda f: f.func == W.WRITE and f.addr == W.REG_MAILBOX
                      and f.values[:2] == [W.SYS, W.SYS_STEP_FORMAT])
    assert "no step format" in _problems(tmp_path, msgs)


def test_harness_sees_a_missing_pwm_off_at_the_end(synthetic, tmp_path):
    def pwm_off(f):
        return f.func == W.WRITE and f.addr == W.REG_MAILBOX and f.values[:2] == [W.SYS, W.SYS_PWM] \
            and f.values[3] == 0
    msgs = synthetic["end"]
    while any((f := _req(m)) is not None and pwm_off(f) for m in msgs):
        msgs = _drop_pair(msgs, pwm_off)
    assert "no PWM off" in _problems(tmp_path, msgs)


def test_harness_sees_a_halt_out_of_order(synthetic, tmp_path):
    """Swap the PWM off and the follower stop of the stop sequence (requests and replies)."""
    msgs = list(synthetic["stop"])
    i = next(k for k, m in enumerate(msgs) if (f := _req(m)) is not None and f.func == W.WRITE
             and f.addr == W.REG_MAILBOX and f.values[:2] == [W.SYS, W.SYS_PWM] and f.values[3] == 0)
    j = next(k for k, m in enumerate(msgs) if (f := _req(m)) is not None and f.func == W.WRITE
             and f.addr == W.REG_MAILBOX and f.values[:2] == [W.FOLLOWER, 5])
    from dataclasses import replace
    msgs[i], msgs[j] = replace(msgs[j], ts=msgs[i].ts), replace(msgs[i], ts=msgs[j].ts)
    p = _problems(tmp_path, msgs)
    assert "follower before the PWM off" in p, p


def test_harness_sees_undocumented_status_bits_and_registers(synthetic, tmp_path):
    from dataclasses import replace
    msgs = list(synthetic["end"])
    # one status reply with an RO[19] bit the card never showed, re-encoded by the reference codec
    k = next(i for i, m in enumerate(msgs) if m.src[1] == CARD_PORT
             and (f := W.decode_reply(m.data)).func == W.READ and f.addr == W.RO_BASE and f.count == 36)
    f = W.decode_reply(msgs[k].data)
    vals = list(f.values)
    vals[W.RO_PROC] |= 0x400
    msgs[k] = replace(msgs[k], data=W.encode_reply(f.tid, W.READ, W.RO_BASE, vals))
    # a write to a register outside the documented map
    tid = 0x7777
    t = msgs[-1].ts + 0.01
    msgs += [P.Message(t, S.PC_ADDR, S.CARD_ADDR, W.encode_request(tid, W.WRITE, 7777, [1]), "udp"),
             P.Message(t + 0.001, S.CARD_ADDR, S.PC_ADDR, W.encode_reply(tid, W.WRITE, 7777, count=1), "udp")]
    p = _problems(tmp_path, msgs)
    assert "RO[19] = 0x" in p and "register 7777" in p, p


def test_out_of_scope_registers_are_never_decoded(synthetic, tmp_path, monkeypatch):
    """Requests to registers 59500 / 59503 and their replies are dropped before any decoding:
    the production decoder never sees them and nothing about them is reported.  The frames here
    carry no content (zero words, a deliberately malformed one): a decode would have failed."""
    from dataclasses import replace

    from mlaser.protocol import frames as F
    msgs = list(synthetic["end"])
    n = next(i for i in range(len(msgs) // 3, len(msgs)) if msgs[i].dst[1] == CARD_PORT)
    pc_tid = int.from_bytes(msgs[n].data[:2], "big")
    a = W.encode_request(pc_tid, W.WRITE, OUT_OF_SCOPE_REGS[0], [0, 0, 0, 0])
    b = bytearray(W.encode_request(pc_tid + 1, W.READ, OUT_OF_SCOPE_REGS[1], count=1))
    b[5] ^= 0x7F                                      # LEN no longer matches: not decodable
    t = msgs[n].ts - 0.0005
    inject = [P.Message(t, S.PC_ADDR, S.CARD_ADDR, a, "udp"),
              P.Message(t + 0.0001, S.CARD_ADDR, S.PC_ADDR, bytes(a[:2]) + b"\xff" * 5, "udp"),
              P.Message(t + 0.0002, S.PC_ADDR, S.CARD_ADDR, bytes(b), "udp")]
    # the later requests of this client move up by the two skipped ids
    shifted = [replace(m, data=((int.from_bytes(m.data[:2], "big") + 2) & 0xFFFF).to_bytes(2, "big") + m.data[2:])
               for m in msgs[n:]]
    msgs = msgs[:n] + inject + shifted
    seen: list[bytes] = []
    for name in ("decode_request", "decode_response"):
        orig = getattr(F, name)

        def spy(data, *a, _orig=orig, **kw):
            seen.append(bytes(data))
            return _orig(data, *a, **kw)
        monkeypatch.setattr(F, name, spy)
    rp = replay(_write(tmp_path, msgs, "scope.pcap"))
    assert not rp.problems, "\n".join(rp.problems)
    assert seen and not any(x in seen for x in (a, bytes(b), bytes(a[:2]) + b"\xff" * 5))
    assert not any(str(r) in p for r in OUT_OF_SCOPE_REGS for p in rp.problems)


def test_no_capture_is_committed():
    """The repository holds no capture (public repository; owners keep their own)."""
    import subprocess
    root = Path(__file__).resolve().parents[1]
    patterns = ["*.pcap", "*.pcapng", "*.cap", "*.pcap.gz", "*.pcapng.gz"]
    try:
        out = subprocess.run(["git", "ls-files", *patterns], cwd=root, capture_output=True, text=True,
                             timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout")
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    assert not out.stdout.split(), out.stdout
