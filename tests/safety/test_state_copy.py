"""``state()`` returns a copy taken under the state lock, not the controller's live object
(review A6, the rest).  The UI, the services and the diagnostics call ``ctl.state()`` from the
GUI thread and from workers while the status poll writes the state on its own thread: with the
live object a reader could see X from one poll and Y from the next, and ``outputs`` (a dict)
could change while it was iterated.  A write made before the call (a command, the poll) is in
the copy, so ``ctl.state().mode`` right after a command still shows it.
"""
from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.safety


def test_state_is_a_copy_from_one_moment(make_ctl):
    ctl = make_ctl(arm=False)
    s = ctl.state()
    assert s is not ctl._s and s.outputs is not ctl._s.outputs and s.alarms is not ctl._s.alarms
    with ctl._slock:                            # a later write (the poll) ...
        ctl._s.x, ctl._s.y = s.x + 7.0, s.y + 7.0
        ctl._s.outputs["gas"] = not s.outputs["gas"]
    assert ctl.state().x == s.x + 7.0           # ... is seen by the next call
    assert ctl.state().outputs["gas"] is not s.outputs["gas"]
    s2 = ctl.state()
    s2.outputs["laser"] = True                  # changing the copy leaves the controller's alone
    s2.mode = "process"
    assert ctl._s.outputs["laser"] is False and ctl._s.mode != "process"


def test_state_fields_agree_while_the_poll_writes(make_ctl):
    ctl = make_ctl(arm=False)
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            i += 1
            with ctl._slock:
                ctl._s.x = float(i)
                ctl._s.y = float(i)
                ctl._s.outputs["gas"] = bool(i & 1)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        end = time.monotonic() + 0.3
        n = 0
        while time.monotonic() < end:
            s = ctl.state()
            x = s.x
            time.sleep(0)                       # let the writer run between the two reads
            assert s.y == x
            seen = dict(s.outputs)
            time.sleep(0)
            assert dict(s.outputs.items()) == seen   # nobody writes into the reader's dict
            n += 1
        assert n > 10
    finally:
        stop.set()
        t.join(2.0)


def test_a_command_is_seen_at_once(make_ctl):
    ctl = make_ctl(arm=False)
    ctl.set_gas_type(2)
    ctl.set_laser_power(30.0)
    ctl.set_output("red_light", True)
    assert ctl.wait_commands(5.0)
    s = ctl.state()
    assert s.gas_type == 2 and s.laser_power_pct == 30.0 and s.outputs["red_light"] is True
    ctl.jog_start("X", 1)
    assert ctl.wait_commands(5.0)
    assert ctl.state().mode == "jog"
    ctl.jog_stop("X")
    assert ctl.wait_commands(5.0)
