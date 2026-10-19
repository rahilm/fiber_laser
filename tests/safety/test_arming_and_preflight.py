"""Arming on every outgoing command, the pre-flight check, interlocks, dead-man, heartbeat.

=====================================  =======================================  ====================
scenario                               before (review)                          asserted now
=====================================  =======================================  ====================
laser / PWM / DA / gas / shutter       44 direct ``mc`` paths, laser on by       refused by the
commands while disarmed                default, ``--laser-off`` compile-only      supervisor, nothing
                                                                                on the wire
undefined layer                        silently 100 % power, 5000 Hz            refused (error)
job at x = 50,000 / -5,000 mm          compiled and would run                   refused, nothing sent
interlocks (follower, gas, laser,      never polled during a job                FAULT + halt < 0.5 s
door)
continuous jog without keep-alive      runs to the end of travel                stopped < 1 s
=====================================  =======================================  ====================
"""
from __future__ import annotations

import time

import pytest

from mlaser.controller.supervisor import CommandRefused
from mlaser.devices import laser as L
from mlaser.machine.safety import SafetyState
from mlaser.protocol import frames as F

from safety_helpers import rect_doc, safe, start_and_run, wait_until


def _emitted(sim) -> list:
    """Mailbox commands that could make the beam: PWM with a duty, DA > 0, laser sync."""
    out = []
    for c in sim.state.commands:
        if c[0] == 9999 and len(c) > 3 and c[1] in (3, 17) and c[3]:
            out.append(c)
        elif c[0] == 9999 and len(c) > 3 and c[1] == 4 and c[3]:
            out.append(c)
    return out


def test_laser_commands_are_refused_while_disarmed(make_ctl):
    ctl = make_ctl(arm=False)
    ctl.output_map["shutter"] = 2              # a configured shutter / gate output (DO2)
    sim = ctl.sim
    assert ctl.safety().state == SafetyState.DISARMED and not ctl.safety().armed
    card = ctl.card()
    for words in ([9999, 3, 5000, 50, 1], [9999, 17, 5000, 50, 1], [9999, 4, 0, 3000], [9999, 18, 1000],
                  [9999, 2, 2, 2]):                     # output 2 = shutter (guarded)
        with pytest.raises(CommandRefused):
            card.command(words)
    with pytest.raises(CommandRefused):
        card.set_pwm(5000, 40)
    with pytest.raises(CommandRefused):
        card.set_da(1, 5000)
    # a raw FIFO frame with a laser duty, written behind the controller's back
    with pytest.raises(CommandRefused):
        card.fifo_write_frame([sim.state.fifo_seq + 1] + F.encode_step(1, 0, 5000, 60, 100))
    ctl.set_output("shutter", True)
    ctl.laser_press()
    ctl.laser_release()
    ctl.laser_test(0.2, 50)
    ctl.set_laser_power(80)
    ctl.start(rect_doc())                                # a cut needs arming (pre-flight error)
    time.sleep(0.2)
    assert "not_armed" in ctl.last_preflight.codes()
    assert _emitted(sim) == [] and not sim.state.outputs & 0b10 and not sim.state.fifo
    # manual gas (and other non-laser outputs) is allowed disarmed: operators test the valves
    ctl.set_output("gas", True)
    assert sim.state.outputs & 0b1
    ctl.set_output("gas", False)
    # the laser source (devices.laser) is guarded too
    sim_l = L.LaserSimulator("ipg")
    with pytest.raises(L.LaserError):
        L.IpgLaser(sim_l.loopback()).emission(True)
    assert not sim_l.emission
    # a laser-off run is allowed disarmed
    ctl.frame(rect_doc(w=50, h=50))
    assert wait_until(lambda: sim.state.fifo_running, 5) is not None
    assert ctl.wait_job(20) and _emitted(sim) == []
    # armed: the same commands go out
    assert ctl.arm()
    ctl.card().set_pwm(5000, 40)
    assert [9999, 3, 5000, 40] in sim.state.commands


def test_dry_run_mode_cannot_be_armed(make_ctl):
    ctl = make_ctl(arm=False, laser_enabled=False)
    assert not ctl.arm()
    assert ctl.safety().banner == "DRY-RUN"


def test_undefined_layer_is_refused(make_ctl):
    ctl = make_ctl()
    sent = ctl.sup.fifo_frames
    for doc in (rect_doc(w=50, h=50, define=False),
                rect_doc(w=50, h=50, layer=3)):
        if doc.layers:
            doc.layers[3].extra["placeholder"] = True     # what the UI shows for a new layer
        ctl.start(doc)
        rep = ctl.last_preflight
        assert not rep.ok and "layer_undefined" in {i.code for i in rep.errors}
    time.sleep(0.2)
    assert ctl.sup.fifo_frames == sent and not ctl.sim.state.fifo


@pytest.mark.parametrize("x0", [50_000.0, -5_000.0])
def test_out_of_travel_job_is_refused(make_ctl, x0):
    ctl = make_ctl()
    sent = ctl.sup.fifo_frames
    ctl.start(rect_doc(x0=x0, w=100, h=100))
    assert "out_of_travel" in ctl.last_preflight.codes()
    ctl.frame(rect_doc(x0=x0, w=100, h=100))
    assert "out_of_travel" in ctl.last_preflight.codes()
    n = len(ctl.sim.state.commands)
    ctl.move_to(x0, 0.0)
    assert "out_of_travel" in ctl.last_preflight.codes()
    time.sleep(0.2)
    assert ctl.sup.fifo_frames == sent and not any(c[0] == 5 for c in ctl.sim.state.commands[n:])


def test_card_soft_limits_read_back_define_the_travel(make_ctl):
    ctl = make_ctl(card_soft_limits={0: (0.0, 300.0), 1: (0.0, 200.0)})
    lim = ctl.travel_limits()
    assert lim.source == "card soft limits" and lim.x == (0.0, 300.0) and lim.y == (0.0, 200.0)
    ctl.start(rect_doc(w=400, h=100))
    assert "out_of_travel" in ctl.last_preflight.codes()


def test_unhomed_machine_is_refused_when_required(make_ctl):
    ctl = make_ctl()
    ctl.require_homed = True                          # the real-card default
    ctl.start(rect_doc(w=50, h=50))
    assert "not_homed" in ctl.last_preflight.codes()
    ctl.home("XY")
    assert wait_until(lambda: ctl.homed_xy(), 10) is not None
    ctl.start(rect_doc(w=50, h=50))
    assert ctl.last_preflight.ok
    assert ctl.stop().wait(10)


@pytest.mark.parametrize("source", ["gas", "door", "laser", "follower"])
def test_interlock_trips_fault_and_halt(make_ctl, source):
    from mlaser.controller.interlocks import InterlockConfig
    cfg = InterlockConfig(gas_low_input=5, door_input=6, laser_alarm_input=7)
    kw = {"interlocks": cfg}
    ctl = make_ctl(**kw)
    if source == "follower":
        ctl.machine.params["ZF.ZFType"] = 1
        ctl.sim.onboard_zf = True
    start_and_run(ctl, run_s=0.3)
    sim = ctl.sim
    if source == "follower":
        sim.state.zf_regs[1] = 0x4                     # follower status word 1: alarm (collision)
    else:
        sim.set_input({"gas": 5, "door": 6, "laser": 7}[source], True)
    t = wait_until(lambda: ctl.fsm.state == SafetyState.FAULT, 1.0)
    assert t is not None and t < 0.5, f"interlock {source} not handled ({t})"
    assert f"interlock {source}" in ctl.fsm.fault
    assert wait_until(lambda: safe(sim), 1.0) is not None
    assert ctl.wait_job(5)
    assert not ctl.reset_fault()                       # the interlock still holds
    if source == "follower":
        sim.state.zf_regs[1] = 0
    else:
        sim.set_input({"gas": 5, "door": 6, "laser": 7}[source], False)
    time.sleep(0.2)
    assert ctl.reset_fault()


def test_follower_alarm_holds_through_a_reset(make_ctl):
    """A reset while the follower alarm still holds is refused, whatever the timing of the
    interlock evaluation (period 0: every status is evaluated).  Before, a status evaluated
    without follower words dropped the alarm from the active set and the reset passed
    (Windows runner, test_interlock_trips_fault_and_halt[follower])."""
    from mlaser.controller.interlocks import InterlockConfig
    ctl = make_ctl(interlocks=InterlockConfig(period_s=0.0))
    ctl.machine.params["ZF.ZFType"] = 1
    ctl.sim.onboard_zf = True
    start_and_run(ctl, run_s=0.3)
    ctl.sim.state.zf_regs[1] = 0x4
    assert wait_until(lambda: ctl.fsm.state == SafetyState.FAULT, 1.0) is not None
    assert ctl.wait_job(5)
    for _ in range(5):
        assert not ctl.reset_fault()                   # the follower alarm still holds
        time.sleep(0.05)
    ctl.sim.state.zf_regs[1] = 0
    assert wait_until(lambda: ctl.reset_fault(), 2.0) is not None


def test_jog_dead_man(make_ctl):
    ctl = make_ctl(arm=False)
    sim = ctl.sim
    ctl.jog_start("X", 1)
    assert wait_until(lambda: sim.state.axes[0].busy, 1.0) is not None
    for _ in range(8):                                  # held: keep-alive every 100 ms
        time.sleep(0.1)
        ctl.jog_keepalive()
    assert sim.state.axes[0].busy
    t0 = time.monotonic()                               # released without a stop (UI hung)
    assert wait_until(lambda: not sim.state.axes[0].busy, 2.0) is not None
    assert time.monotonic() - t0 < ctl.jog_deadman_s + 0.5


def test_pendant_read_error_stops_the_jog():
    from mlaser.devices.pendant import Pendant, PendantJogMapper

    class Ctl:
        def __init__(self):
            self.calls = []

        def jog_start(self, axis, d, fast=False):
            self.calls.append(("start", axis))

        def jog_stop(self, axis=None):
            self.calls.append(("stop", axis))

        def jog_keepalive(self):
            self.calls.append(("alive", None))

    class BadDev:
        def read(self, n, timeout_ms=0):
            raise OSError("HID unplugged")

        def get_feature_report(self, *a):
            raise OSError("HID unplugged")

        def send_feature_report(self, *a):
            raise OSError("HID unplugged")

        def close(self):
            pass

    ctl = Ctl()
    m = PendantJogMapper(ctl)
    m._jogging = "X"
    p = Pendant(devices=(BadDev(), BadDev()))
    m.attach(p)
    assert p.open()
    p.start()
    try:
        assert wait_until(lambda: ("stop", "X") in ctl.calls, 1.0) is not None
    finally:
        p.close()


def test_heartbeat_output_toggles_and_stops_on_fault(make_ctl):
    ctl = make_ctl(heartbeat_output=8, heartbeat_hz=10.0)
    sim = ctl.sim
    seen = set()
    t0 = time.monotonic()
    t_end = t0 + 0.6
    while time.monotonic() < t_end:
        seen.add(bool(sim.state.outputs >> 7 & 1))
        time.sleep(0.01)
    sup = ctl.sup
    assert seen == {True, False} and sup.heartbeat_toggles >= 8, (
        f"{sup.heartbeat_toggles} toggles, {sup.heartbeat_errors} failed writes, "
        f"{time.monotonic() - t0:.2f} s")
    ctl.raise_fault("test fault")
    time.sleep(0.3)
    assert not sim.state.outputs >> 7 & 1
    n = ctl.sup.heartbeat_toggles
    time.sleep(0.3)
    assert ctl.sup.heartbeat_toggles == n             # stopped: the watchdog relay drops out


OFF_COMMANDS = [
    [9999, 3, 5000, 0, 0], [9999, 17, 5000, 0, 0],         # PWM duty 0
    [9999, 4, 0, 0], [9999, 4, 1, 0],                      # DA 0
    [9999, 2, 0xFFFF, 0], [9999, 2, 2, 0], [9999, 13, 0xFFFF, 0],   # outputs (shutter) to 0
    [9999, 18, 0],                                         # laser sync off
    [1, 0x1F, 2, 4000, 200000], [1, 0x1F], [118, 5, 0], [118, 4, 0, 0], [101], [9999, 5, 0, 0],
]


@pytest.mark.parametrize("state", ["disarmed", "fault"])
def test_off_commands_always_pass(make_ctl, state):
    """Anything that turns something OFF is never blocked by the guard, in any state."""
    ctl = make_ctl(arm=False)
    if state == "fault":
        ctl.raise_fault("test: latched fault")
        assert ctl.fsm.state == SafetyState.FAULT
    card = ctl.card()
    for w in OFF_COMMANDS:
        card.command(w)                                   # must not raise
        assert w in ctl.sim.state.commands
    card.fifo_stop()
    card.fifo_clear()
    card.set_pwm(5000, 0)
    card.set_da(1, 0)
    card.set_output(2, False)
    n = len(ctl.sim.state.commands)
    ctl.laser_press()                                     # refused (not armed) ...
    ctl.laser_release()                                   # ... but the release sends the laser-off
    f = ctl.machine.pt_laser_freq
    assert ctl.sim.state.commands[n:] == [[9999, 3, f, 0, 0], [9999, 17, f, 0, 0]]
    sim_l = L.LaserSimulator("ipg")
    L.IpgLaser(sim_l.loopback()).emission(False)          # emission off always goes out
