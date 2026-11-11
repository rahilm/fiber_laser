"""Synthetic captures keep the recording order under a coarse clock (Windows CI).

``time.time()`` ticks about every 15 ms on Windows, so a run of recorded exchanges can carry the
same time.  The capture is read back in time order; before the fix the request of one exchange
then sorted before the reply of the exchange before it, and the replay saw no step format before
the FIFO start and no PWM off after the FIFO stop."""
from mlaser.parity import synth as S


def test_equal_clock_readings_keep_request_reply_order():
    recs = [S.Record(5.0, bytes([i]), bytes([100 + i]), 5.0) for i in range(4)]
    recs.append(S.Record(5.0, b"\x09", None, 5.0))           # a request without a reply
    msgs = S.to_messages(recs)
    assert [m.data for m in msgs] == [b"\x00", b"d", b"\x01", b"e", b"\x02", b"f", b"\x03", b"g", b"\x09"]
    ts = [m.ts for m in msgs]
    assert all(b - a >= 0.5e-6 for a, b in zip(ts, ts[1:])), ts
    assert sorted(range(len(msgs)), key=lambda i: (msgs[i].ts, i)) == list(range(len(msgs)))


def test_real_gaps_are_kept():
    recs = [S.Record(1.0, b"a", b"b", 1.002), S.Record(1.5, b"c", b"d", 1.5)]
    ts = [m.ts - S.to_messages(recs)[0].ts for m in S.to_messages(recs)]
    assert abs(ts[1] - 0.002) < 1e-6 and abs(ts[2] - 0.5) < 1e-6
    assert ts[3] > ts[2]
