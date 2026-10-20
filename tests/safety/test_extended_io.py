"""Review 2 P0-1: laser outputs on the extended IO board (outputs 11..26).

Outputs 1..10 are on the base board (``[9999, 2, mask, value]``), 11..26 on the extended IO
board (``[9999, 13, mask, value]``, bit k = output 11+k).  The classifier used to shift the sub-13
mask by 16 (bit k = output 17+k), so a guarded output 11..26 was classified IO and went out while
disarmed, ``wire_emits`` missed a laser DO on 11..26, and the halt sent outputs 11..16 with sub 2
and never switched 17..26 off.  The simulator now models the split exactly (sub 2 = outputs
1..10 only), so it can no longer hide it.
"""
from __future__ import annotations

import pytest

from mlaser.controller.client import MotionController
from mlaser.controller.supervisor import CommandRefused, HaltParams, halt_steps
from mlaser.machine.safety import Cmd, classify_request, wire_emits
from mlaser.nc.compiler import _io_item
from mlaser.protocol import frames as F

PORTS = range(1, F.N_OUTPUTS + 1)


def _request_of(port: int, on: bool = True) -> F.Request:
    mc = MotionController()
    reqs: list = []
    mc.transact = reqs.append            # type: ignore[method-assign]
    mc.set_output(port, on)
    return reqs[0]


def test_output_bit_numbering():
    assert F.output_bit(1) == (F.SYS_OUTPUTS, 0)
    assert F.output_bit(10) == (F.SYS_OUTPUTS, 9)
    assert F.output_bit(11) == (F.SYS_OUTPUTS_EXT, 0)
    assert F.output_bit(26) == (F.SYS_OUTPUTS_EXT, 15)
    for bad in (0, 27, -1):
        with pytest.raises(ValueError):
            F.output_bit(bad)
    for p in PORTS:
        sub, bit = F.output_bit(p)
        assert F.ports_of_sub_mask(sub, 1 << bit) == 1 << (p - 1)
        assert F.sub_masks_of_ports(1 << (p - 1)) == [(sub, 1 << bit)]
        # the client, the FIFO encoder and the compiler agree
        assert _request_of(p).values[1:4] == [sub, 1 << bit, 1 << bit]
        assert _io_item(p, True)[1:] == [sub, 1 << bit, 1 << bit]
        assert F.encode_output(p - 1, True)[1:] == [sub, 1 << bit, 1 << bit]


@pytest.mark.parametrize("port", PORTS)
def test_guarded_output_on_every_port_is_emission(port):
    guarded = 1 << (port - 1)
    assert classify_request(_request_of(port, True), guarded) & Cmd.EMISSION
    assert classify_request(_request_of(port, False), guarded) == Cmd.HALT
    # another output on the same board is plain IO
    other = port + 1 if port not in (10, 26) else port - 1
    assert classify_request(_request_of(other, True), guarded) == Cmd.IO
    # a job whose only emission is the laser DO on this port fires the laser
    assert wire_emits([_io_item(port, True)], guarded)
    assert not wire_emits([_io_item(port, False)], guarded)


def test_sub2_high_bits_fail_closed():
    """Sub 2 bits 10..15 address nothing on the base board; a guarded output 11..16 there is
    still classified EMISSION (fails closed for a card that might honour them)."""
    req = F.Request(F.FUNC_WRITE, F.REG_MAILBOX, 4, F.mailbox_sys(F.SYS_OUTPUTS, 1 << 11, 1 << 11))
    assert classify_request(req, 1 << 11) & Cmd.EMISSION


def test_fifo_ctrl_unknown_value_is_not_a_halt():
    for v, want in ((F.FIFO_CLEAR, Cmd.HALT), (F.FIFO_STOP, Cmd.HALT), (F.FIFO_START, Cmd.MOTION)):
        assert classify_request(F.Request(F.FUNC_WRITE, F.REG_FIFO_CTRL, 1, [v])) == want
    cls = classify_request(F.Request(F.FUNC_WRITE, F.REG_FIFO_CTRL, 1, [7]))
    assert cls & Cmd.MOTION and not cls & Cmd.HALT


def test_halt_words_per_board():
    sent: list = []

    class M:
        def command(self, w):
            sent.append(list(w))

        def stop_motion(self, d):
            pass

        def fifo_stop(self):
            pass

        def fifo_clear(self):
            pass

    mask = (1 << 0) | (1 << 9) | (1 << 10) | (1 << 11) | (1 << 25)      # outputs 1, 10, 11, 12, 26
    for _n, fn in halt_steps(M(), HaltParams(outputs_mask=mask)):
        fn()
    outs = [w for w in sent if w[1] in (F.SYS_OUTPUTS, F.SYS_OUTPUTS_EXT)]
    assert outs == [[9999, F.SYS_OUTPUTS, 0b1000000001, 0], [9999, F.SYS_OUTPUTS_EXT, 0b1000000000000011, 0]]


def test_simulator_models_the_io_split():
    from mlaser.controller.simulator import CardSimulator
    sim = CardSimulator("127.0.0.1", 0)
    sim._sys(F.SYS_OUTPUTS, [0xFFFF, 0xFFFF], immediate=True)
    assert sim.state.outputs == 0x3FF                    # sub 2 reaches outputs 1..10 only
    sim._sys(F.SYS_OUTPUTS_EXT, [1 << 1, 1 << 1], immediate=True)
    assert sim.state.outputs >> 11 & 1                   # bit 1 of sub 13 = output 12
    assert sim._ro(F.RO_OUTPUTS_EXT) == 1 << 1
    sim._sys(F.SYS_OUTPUTS, [1 << 11, 0], immediate=True)
    assert sim.state.outputs >> 11 & 1                   # sub 2 cannot switch output 12 off
    sim._sys(F.SYS_OUTPUTS_EXT, [1 << 1, 0], immediate=True)
    assert not sim.state.outputs >> 11 & 1


@pytest.fixture
def io_ctl():
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.02, stream_process=False,
                       output_map={"gas": 1, "shutter": 2, "red_light": 3})
    assert ctl.connect_simulator(time_scale=1.0)
    yield ctl
    ctl.disconnect()


def _sim_out(ctl, port: int) -> bool:
    return bool(ctl.sim.state.outputs >> (port - 1) & 1)


def test_every_guarded_port_refused_disarmed_and_off_after_every_halt(io_ctl):
    """For each output 1..26 configured as the shutter: switching it on is refused while
    disarmed (mailbox through the card handle and a FIFO frame), and every kind of halt
    (supervisor halt, pause halt, fault, disconnect) switches it off on the simulated card."""
    ctl = io_ctl
    card = ctl.card()
    halts = ("halt", "pause", "fault")
    for port in PORTS:
        if port in (1, 3):
            continue                      # gas and red light keep their own numbers here
        ctl.output_map["shutter"] = port
        assert ctl._sup.guarded_outputs >> (port - 1) & 1
        assert ctl._process_outputs_mask() >> (port - 1) & 1
        with pytest.raises(CommandRefused):
            card.set_output(port, True)
        frame = F.build_fifo_frames([[_io_item(port, True)]], 1)[0]
        with pytest.raises(CommandRefused):
            card.fifo_write_frame(frame)
        assert not _sim_out(ctl, port), f"output {port} switched on while disarmed"
        for kind in halts:
            assert ctl.arm(), (port, kind)
            ctl.operator_action("test")        # clears the previous halt's latch
            card.set_output(port, True)
            assert _sim_out(ctl, port)
            if kind == "halt":
                ctl._sup.safe_halt(f"test {port}")
            elif kind == "pause":
                ctl._sup.safe_halt(f"test {port}", pause=True)
            else:
                ctl.raise_fault(f"test fault {port}")
            assert not _sim_out(ctl, port), f"output {port} still on after the {kind} halt"
            if ctl.fsm.faulted:
                assert ctl.reset_fault()
            ctl.disarm("test")
    # disconnect
    ctl.output_map["shutter"] = 21
    assert ctl.arm()
    ctl.operator_action("test")
    card.set_output(21, True)
    sim = ctl.sim
    ctl.sim = None                         # keep the simulator running across the disconnect
    try:
        ctl.disconnect()
        assert not sim.state.outputs >> 20 & 1
    finally:
        sim.stop()


def test_heartbeat_on_the_extended_board_is_recognised():
    from mlaser.controller.supervisor import SafetySupervisor
    from mlaser.machine.safety import SafetyFSM
    sup = SafetySupervisor(SafetyFSM(), heartbeat_output=14)
    req = _request_of(14, True)
    assert sup._is_heartbeat(req)
    assert not sup._is_heartbeat(_request_of(4, True))


# ----------------------------------------------------------------------------- fail closed (P1-11)
def test_default_machine_mask_is_never_zero():
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController()
    m = ctl._guarded_outputs_mask()
    assert m != 0
    for name in ("gas", "red_light", "dedust", "lube"):
        n = ctl.output_map.get(name)
        if n:
            assert not m >> (n - 1) & 1, name          # named non-laser outputs stay testable
    assert m >> 25 & 1                                  # an unnamed ext output is guarded


def test_unreadable_laser_parameter_guards_every_output():
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(output_map={"gas": 1, "shutter": 2, "red_light": 3}, heartbeat_output=9)

    def broken(key, default=0):
        if key.startswith("LGP."):
            raise KeyError(key)
        return default
    ctl.machine.iparam = broken
    m = ctl._guarded_outputs_mask()
    assert m == F.ALL_OUTPUTS_MASK & ~(1 << 8)          # all but the heartbeat


def test_supervisor_mask_error_fails_closed():
    from mlaser.controller.supervisor import SafetySupervisor
    from mlaser.machine.safety import SafetyFSM
    sup = SafetySupervisor(SafetyFSM())

    def boom():
        raise RuntimeError("config gone")
    sup._guarded_fn = boom
    assert sup.guarded_outputs == F.ALL_OUTPUTS_MASK
