"""An independent wire-level check of the client and the simulator (review arch.md A4).

The simulator decodes requests and encodes replies with the client's own codec
(``mlaser.protocol.frames``), so a wrong reading of the frame format, or of the card, would be
wrong on both sides and the suite would stay green: that is how the two faults of the first real
Frame (alarm 9001, RO[19] read as ``== 1``) got through.  These tests check the bytes against a
second codec written from docs/re/protocol.md alone (tests/wire_reference.py, which imports
nothing of ``mlaser``), in three ways:

1. the documented example frames (protocol.md §2) decode with the reference codec as documented
   and round-trip byte for byte through the production codec;
2. the client's bytes on the wire for each command class (status read, mailbox commands, FIFO
   frames with the step format, the job start and end, the halt sequence, the heartbeat output)
   decode with the reference codec to what the documentation says;
3. the simulator, driven only through the reference codec (no client code, no client constants),
   behaves the way the documentation records the real card behaving (owner's captures of
   2026-11-07, protocol.md §15): the RO[19] bit field, the job reset, alarm 9001 without the step
   format.

The expected values below are written from the documentation; none is taken from
``mlaser.protocol``.  Where production constants appear it is only to drive the code under test.
"""
from __future__ import annotations

import time

import pytest

import wire_reference as W

pytestmark = pytest.mark.sim


def _hex(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split())


# protocol.md §2 "Worked example from the log" (13:34:44, MC-RecvDataErr ErrCode:502)
DOC_REPLY = _hex("02 be d0 45 00 03 00 b0 02")
# "The request would be 02 be | crc | 00 08 | 00 30 | 10 27 00 00 | 12 00" (CRC not printed)
DOC_REQUEST_NO_CRC = _hex("02 be") + b"??" + _hex("00 08 00 30 10 27 00 00 12 00")
# "Other logged requests" (log field DataEx: func addr count values, hex) and their meaning
DOC_DATAEX = [
    ("40 64 01 22b8", W.WRITE, 100, [8888]),                   # write reg 100 := 8888
    ("30 3e8 02", W.READ, 1000, 2),                            # read 1000 x 2 (checkMCStatus)
    ("30 c350 1a", W.READ, 50000, 26),                         # read 50000 x 26 (system RW block)
    ("40 65 04 270f 02 01 01", W.WRITE, 101, [9999, 2, 1, 1]),  # write 101 := [9999, 2, 1, 1]
    ("30 2710 12", W.READ, 10000, 18),                         # read 18 registers at 10000
]


# =========================================================================== 1. documented frames
def test_reference_codec_reads_the_documented_reply():
    assert W.crc16(DOC_REPLY[4:]) == 0x45D0          # "CRC 0x45d0 (stored LE)"
    f = W.decode_reply(DOC_REPLY)
    assert (f.tid, f.length, f.unit, f.func, f.exception) == (0x02BE, 3, 0, W.READ, 2)
    assert f.crc_order == "little"


def test_reference_codec_builds_the_documented_request():
    b = W.encode_request(0x02BE, W.READ, 10000, count=18)
    assert b[:2] + b"??" + b[4:] == DOC_REQUEST_NO_CRC
    f = W.decode_request(b)
    assert (f.func, f.addr, f.count, f.crc_order) == (W.READ, 10000, 18, "big")


def test_documented_frames_round_trip_through_the_production_codec():
    from mlaser.protocol import frames as F
    req_bytes = W.encode_request(0x02BE, W.READ, 10000, count=18)
    req = F.decode_request(req_bytes)
    assert (req.tid, req.func, req.addr, req.count) == (0x02BE, 0x30, 10000, 18)
    assert F.encode_request(req) == req_bytes
    rep = F.decode_response(DOC_REPLY)
    assert rep.tid == 0x02BE and rep.is_exception and rep.exception == 2 and rep.func == 0x30
    assert rep.as_data() == [0xB0, 2]                          # the log's "Data:000000b0 00000002"
    assert F.check_reply(req, rep) == 502                      # "The result is 500+2 = 502"
    assert F.encode_response(rep) == DOC_REPLY                 # byte for byte, LE CRC included


@pytest.mark.parametrize("dataex,func,addr,arg", DOC_DATAEX)
def test_logged_requests_encode_as_documented(dataex, func, addr, arg):
    from mlaser.protocol import frames as F
    req = F.parse_dataex(dataex)
    req.tid = 0x1234
    b = F.encode_request(req)
    f = W.decode_request(b)
    assert (f.tid, f.unit, f.func, f.addr, f.crc_order) == (0x1234, 0, func, addr, "big")
    if func == W.READ:
        assert f.count == arg and f.length == 8
    else:
        assert f.values == arg and f.length == 8 + 4 * len(arg)
    assert b == W.encode_request(0x1234, func, addr, arg if func == W.WRITE else (),
                                 count=arg if func == W.READ else None)


# =========================================================================== 2. client bytes
class Tap:
    """Every datagram the simulator received and answered, raw, in order."""

    def __init__(self, sim):
        self.pairs: list[tuple[bytes, bytes | None]] = []
        orig = sim.handle

        def handle(data: bytes):
            out = orig(data)
            self.pairs.append((bytes(data), out))
            return out

        sim.handle = handle

    def mark(self) -> int:
        return len(self.pairs)

    def requests(self, since: int = 0) -> list[W.Frame]:
        return [W.decode_request(q) for q, _r in self.pairs[since:]]

    def writes(self, since: int = 0) -> list[tuple[int, list[int]]]:
        return [(f.addr, f.values) for f in self.requests(since) if f.func == W.WRITE]


@pytest.fixture
def wire():
    from mlaser.controller.client import MotionController, TransportConfig
    from mlaser.controller.simulator import CardSimulator
    sim = CardSimulator(time_scale=4.0).start()
    tap = Tap(sim)
    mc = MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port))
    mc.open()
    try:
        yield sim, tap, mc
    finally:
        mc.close()
        sim.stop()


def _check_framing(tap: Tap) -> None:
    """Every exchange decodes as §2 says: request CRC big-endian, reply CRC little-endian, LEN,
    unit 0, the reply echoes the request's transaction id; one client numbers its requests
    consecutively (§1 "post-increment")."""
    tids = []
    for q, r in tap.pairs:
        assert len(q) <= W.MAX_DATAGRAM
        fq = W.decode_request(q)
        assert fq.unit == 0 and fq.crc_order == "big", fq
        tids.append(fq.tid)
        assert r is not None
        fr = W.decode_reply(r)
        assert fr.unit == 0 and fr.crc_order == "little" and fr.tid == fq.tid
        assert fr.exception is None and fr.func == fq.func
        if fq.func == W.READ:
            assert (fr.addr, fr.count) == (fq.addr, fq.count) and len(fr.values) == fq.count
        else:
            assert fr.addr == fq.addr
    assert all((b - a) & 0xFFFF == 1 for a, b in zip(tids, tids[1:])), tids


def test_status_read_on_the_wire(wire):
    _sim, tap, mc = wire
    mc.read_status()
    reqs = tap.requests()
    # updateMCStatusRO 1000 x 36, updateMCStatusAxisRO 2000 x 50 (§3)
    assert [(f.func, f.addr, f.count, f.length) for f in reqs] == [
        (W.READ, W.RO_BASE, W.RO_COUNT, 8), (W.READ, W.AXIS_RO_BASE, W.AXIS_RO_COUNT, 8)]
    _check_framing(tap)


def test_mailbox_commands_on_the_wire(wire):
    _sim, tap, mc = wire
    mc.reset_alarms()
    mc.stop_motion(4000)
    mc.set_pwm(5000, 0)
    mc.set_output(3, True)
    mc.set_output(12, False)
    assert tap.writes() == [
        (W.REG_MAILBOX, [W.SYS, W.SYS_ALARM_RESET, 0, 0]),                 # §4 alarm reset
        (W.REG_MAILBOX, [W.STOP_AXES, 0x1F, 2, 4000, W.STOP_AXES_JERK]),  # §4 decelerated stop
        (W.REG_MAILBOX, [W.SYS, W.SYS_PWM, 5000, 0]),                     # §4 PWM
        (W.REG_MAILBOX, [W.SYS, W.SYS_OUTPUTS, 1 << 2, 1 << 2]),          # output 3 on
        (W.REG_MAILBOX, [W.SYS, W.SYS_OUTPUTS_EXT, 1 << 1, 0]),           # output 12 off (11..26)
    ]
    for f in tap.requests():
        assert f.length == 8 + 4 * f.count
    _check_framing(tap)


STEPS = [(10, 0, 0, 5000), (-127, 127, 100, 5000), (3, -4, 255, 20000), (0, 0, 0, 0), (-1, -1, 7, 300)]


def test_fifo_frame_and_step_items_on_the_wire(wire):
    from mlaser.protocol import frames as F
    _sim, tap, mc = wire
    mc.fifo_job_reset()
    mc.fifo_clear()
    items = [F.encode_step(dx, dy, freq, duty) for dx, dy, duty, freq in STEPS]
    fr = F.build_fifo_frames(items, 1)[0]
    m = tap.mark()
    mc.fifo_write_frame(fr)
    (addr, values), = tap.writes(m)
    assert addr == W.REG_FIFO and values[0] == 1                # write 102 := [seq, items...]
    got = W.fifo_items(values[1:])
    assert len(got) == len(STEPS)
    for (op, params), (dx, dy, duty, freq) in zip(got, STEPS):
        assert op == W.STEP_OP and len(params) == 2
        assert values[1 + 3 * STEPS.index((dx, dy, duty, freq))] == W.STEP_HEADER
        f = W.step_fields(*params)
        # §5: freq 0 -> 5000; the high half of w2 is v8 (0xFFFF) only while the duty is non-zero
        assert (f["dx"], f["dy"], f["duty"], f["freq"]) == (dx, dy, duty, freq or 5000)
        assert f["v8"] == (0xFFFF if duty else 0)
    _check_framing(tap)


def _short_job(n: int = 600):
    from mlaser.controller.stream import WireJob
    from mlaser.protocol import frames as F
    return WireJob([F.encode_step(10, 0, 5000, 0) for _ in range(n)], period_ms=1.0)


def test_job_start_and_end_on_the_wire(wire):
    """§15 (owner's captures of the original on an MCC100): [9999, 16] -> 103 := 1 -> the prefill
    frames numbered from 1 -> [9999, 1, 0x66800003, 0] -> 103 := 2; at the normal end 103 := 3
    and then the PWM off ([9999, 3, freq, 0, 0], [9999, 17, ...])."""
    from mlaser.controller.stream import JobStreamer
    sim, tap, mc = wire
    sim.state.fifo_seq = 41                      # a stale frame identification from an earlier job
    JobStreamer(mc, prefill_ms=200, pwm_freq=5000).run_job(_short_job(), timeout_s=30)
    w = tap.writes()
    assert w[0] == (W.REG_MAILBOX, [W.SYS, W.SYS_JOB_RESET])
    assert w[1] == (W.REG_FIFO_CTRL, [W.FIFO_CLEAR])
    start = w.index((W.REG_FIFO_CTRL, [W.FIFO_START]))
    assert w[start - 1] == (W.REG_MAILBOX, [W.SYS, W.SYS_STEP_FORMAT, W.STEP_CARD_FORMAT | 0x3, 0])
    seqs = [v[0] for a, v in w if a == W.REG_FIFO]
    assert seqs == list(range(1, len(seqs) + 1)) and all(a == W.REG_FIFO for a, _v in w[2:start - 1])
    stop = w.index((W.REG_FIFO_CTRL, [W.FIFO_STOP]))
    assert stop > start and not any(a == W.REG_FIFO for a, _v in w[stop:])
    assert w[stop + 1:stop + 3] == [(W.REG_MAILBOX, [W.SYS, W.SYS_PWM, 5000, 0, 0]),
                                    (W.REG_MAILBOX, [W.SYS, W.SYS_PWM_BUS, 5000, 0, 0])]
    steps = sum(1 for a, v in w if a == W.REG_FIFO for op, _p in W.fifo_items(v[1:]) if op == W.STEP_OP)
    assert steps == 600
    _check_framing(tap)


def test_halt_sequence_on_the_wire(wire):
    """§14 "vm stop manu": decelerated stop, PWM off (both variants), process outputs off,
    follower stop; the rebuild's stop then adds 103 := 3 and 103 := 1.  Each command on its own."""
    from mlaser.controller.supervisor import HaltParams, run_halt
    _sim, tap, mc = wire
    res = run_halt(mc, HaltParams(decel=4000, pwm_freq=5000, outputs_mask=(1 << 0) | (1 << 12)), "test")
    assert res.ok
    assert tap.writes() == [
        (W.REG_MAILBOX, [W.STOP_AXES, 0x1F, 2, 4000, W.STOP_AXES_JERK]),
        (W.REG_MAILBOX, [W.SYS, W.SYS_PWM, 5000, 0, 0]),
        (W.REG_MAILBOX, [W.SYS, W.SYS_PWM_BUS, 5000, 0, 0]),
        (W.REG_MAILBOX, [W.SYS, W.SYS_OUTPUTS, 1 << 0, 0]),              # output 1
        (W.REG_MAILBOX, [W.SYS, W.SYS_OUTPUTS_EXT, 1 << 2, 0]),          # output 13 = ext bit 2
        (W.REG_MAILBOX, [W.FOLLOWER, 5, 0]),
        (W.REG_FIFO_CTRL, [W.FIFO_STOP]),
        (W.REG_FIFO_CTRL, [W.FIFO_CLEAR]),
    ]
    _check_framing(tap)


def test_heartbeat_output_on_the_wire():
    """The "PC alive" heartbeat toggles its output with [9999, 2, mask16, value16] (§4)."""
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.05, heartbeat_output=8, heartbeat_hz=10.0)
    try:
        assert ctl.connect_simulator(time_scale=1.0)
        tap = Tap(ctl.sim)
        time.sleep(0.6)
        hb = [v for a, v in tap.writes() if a == W.REG_MAILBOX and v[:2] == [W.SYS, W.SYS_OUTPUTS]]
        assert len(hb) >= 3, hb
        assert all(v[2] == 1 << 7 and v[3] in (0, 1 << 7) and len(v) == 4 for v in hb), hb
        assert all(a[3] != b[3] for a, b in zip(hb, hb[1:])), "the heartbeat did not alternate"
        for q, r in tap.pairs:                                   # several clients: framing only
            assert W.decode_request(q).crc_order == "big"
            assert r is None or W.decode_reply(r).crc_order == "little"
    finally:
        ctl.disconnect()


# =========================================================================== 3. simulator vs card
@pytest.fixture
def card():
    from mlaser.controller.simulator import CardSimulator
    sim = CardSimulator(time_scale=1.0).start()
    try:
        with W.RefClient(sim.port) as c:
            yield c
    finally:
        sim.stop()


def _frame(seq: int, n: int, dx: int = 5) -> list[int]:
    """A FIFO frame of n step items built by hand from §5 (duty 0, freq 5000)."""
    w1 = ((5000 & 0xFF) << 24) | (0 << 16) | (0 << 8) | (dx & 0xFF)
    w2 = 5000 >> 8
    return [seq] + [W.STEP_HEADER, w1, w2] * n


def test_simulator_processing_status_like_the_real_card(card):
    """§15: 0 idle -> 0x100 after the first frames -> 0x301 while the job runs -> 0x001 once the
    FIFO drained -> 0 after 103 := 3; the original's machine state: low byte == 1 -> 4."""
    assert card.ro()[W.RO_PROC] == 0
    card.write(W.REG_MAILBOX, W.SYS, W.SYS_JOB_RESET)
    card.write(W.REG_FIFO_CTRL, W.FIFO_CLEAR)
    for seq in (1, 2):
        card.write(W.REG_FIFO, *_frame(seq, 80))
    ro = card.ro()
    assert ro[W.RO_PROC] == 0x100 and W.proc_state(ro[W.RO_PROC]) == 0
    assert ro[W.RO_FIFO_SEQ] == 2                     # "sequence number of the last FIFO frame"
    card.write(W.REG_MAILBOX, W.SYS, W.SYS_STEP_FORMAT, W.STEP_CARD_FORMAT | 0x3, 0)
    card.write(W.REG_FIFO_CTRL, W.FIFO_START)
    seen = []
    t_end = time.monotonic() + 5.0
    while time.monotonic() < t_end:
        v = card.ro()[W.RO_PROC]
        seen.append(v)
        if v == 0x001:
            break
        time.sleep(0.01)
    assert seen[0] == 0x301 and W.proc_state(seen[0]) == 4
    assert seen[-1] == 0x001 and W.proc_state(0x001) == 4, seen
    assert set(seen) <= set(W.PROC_DOCUMENTED_VALUES), sorted(set(seen))
    pos = card.read(W.AXIS_RO_BASE + 2, 1)[0]            # axis 0 record word 2: command position
    assert pos == 160 * 5 * 1                             # 160 steps of 5 pulses (ppm 1000 = step ppm)
    card.write(W.REG_FIFO_CTRL, W.FIFO_STOP)
    v = card.ro()[W.RO_PROC]
    assert v == 0 and W.proc_state(v) == 0


def test_simulator_job_reset_puts_the_frame_identification_back_to_0(card):
    """§15: [9999, 16] puts RO[15] back to 0, so the next job's frames are numbered from 1."""
    card.write(W.REG_MAILBOX, W.SYS, W.SYS_JOB_RESET)
    card.write(W.REG_FIFO_CTRL, W.FIFO_CLEAR)
    for seq in (1, 2, 3):
        card.write(W.REG_FIFO, *_frame(seq, 4))
    assert card.ro()[W.RO_FIFO_SEQ] == 3
    card.write(W.REG_MAILBOX, W.SYS, W.SYS_JOB_RESET)
    assert card.ro()[W.RO_FIFO_SEQ] == 0


def test_simulator_rejects_a_start_without_the_step_format_with_alarm_9001(card):
    """§15: without [9999, 1, mask|0x66800000, 0] the card raises alarm 9001 "interpolation data
    content length is abnormal" (RO[7] bit 1, §13: 9000 + b) and nothing moves."""
    card.write(W.REG_MAILBOX, W.SYS, W.SYS_JOB_RESET)
    card.write(W.REG_FIFO_CTRL, W.FIFO_CLEAR)
    card.write(W.REG_FIFO, *_frame(1, 50))
    card.write(W.REG_FIFO_CTRL, W.FIFO_START)
    time.sleep(0.1)
    ro = card.ro()
    codes = [W.ALARM2_BASE + b for b in range(8) if ro[W.RO_ALARM2] >> b & 1]
    assert codes == [W.ALARM2_BASE + W.ALARM2_INTERP_LENGTH_BIT] == [9001]
    assert not ro[W.RO_PROC] & W.PROC_EXEC
    assert card.read(W.AXIS_RO_BASE + 2, 1)[0] == 0
    card.write(W.REG_MAILBOX, W.SYS, W.SYS_ALARM_RESET, 0, 0)        # §4/§13: the reset clears it
    assert card.ro()[W.RO_ALARM2] == 0

