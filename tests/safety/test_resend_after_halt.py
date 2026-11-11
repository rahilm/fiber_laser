"""A request whose reply did not come is re-sent by the client's retry policy (the original's
FUN_1001d6d0).  A request that moves or fires is never re-sent after a halt that came while it
waited for its reply: the halt's "stop" would otherwise be followed by the very motion it stopped
(deep review 2026-11, wave-1 leftover of arch A5).  Reads, halts and IO keep the retry policy.

No card and no socket: the channel's link is a recording stub (``MotionController.link``)."""
from __future__ import annotations

import pytest

from mlaser.controller.client import ControllerError, TransportConfig
from mlaser.controller.supervisor import CommandRefused, SafetySupervisor
from mlaser.machine.safety import SafetyFSM
from mlaser.protocol import frames as F

pytestmark = pytest.mark.safety


class SilentLink:
    """Records every frame sent; never answers.  ``on_wait`` runs on the first receive."""

    def __init__(self, on_wait=None):
        self.sent: list[bytes] = []
        self.on_wait = on_wait

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def recv(self, timeout_s: float):
        cb, self.on_wait = self.on_wait, None
        if cb is not None:
            cb()
        return None

    def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


def _channel(on_wait_of=None):
    fsm = SafetyFSM()
    fsm.connected()
    sup = SafetySupervisor(fsm)
    links: list[SilentLink] = []

    def factory():
        lk = SilentLink()
        links.append(lk)
        return lk
    sup.link_factory = factory
    cfg = TransportConfig(host="memory", port=1, transport="memory", timeout_ms=5, fifo_time_ms=20)
    mc = sup.open(cfg, 1000.0)
    main = links[0]
    if on_wait_of is not None:
        main.on_wait = lambda: on_wait_of(sup)
    return sup, mc, main


def _halt_meanwhile(sup):
    sup._mark_halt("stop by operator", True)        # what safe_halt / safe_halt_soon do first


def test_motion_is_not_resent_after_a_halt():
    sup, mc, link = _channel(_halt_meanwhile)
    assert mc.cfg.max_send > 1                      # the policy would send it several times
    with pytest.raises(CommandRefused) as ei:
        mc.jog(0, 1000, 10000, 5000)
    assert "not sent again" in ei.value.reason
    assert len(link.sent) == 1                      # the first send only
    assert sup.refused and "not sent again" in sup.refused[-1][2]
    assert "not re-sent" in mc.recent[-1][-1]


def test_fifo_frame_is_not_resent_after_a_halt():
    sup, mc, link = _channel(_halt_meanwhile)
    with pytest.raises(CommandRefused):
        mc.fifo_write_frame([1, F.OP_STEP, 0, 0])
    assert len(link.sent) == 1


def test_motion_without_a_halt_keeps_the_retry_policy():
    sup, mc, link = _channel()
    with pytest.raises(ControllerError) as ei:
        mc.jog(0, 1000, 10000, 5000)
    assert not isinstance(ei.value, CommandRefused)
    assert len(link.sent) == mc.cfg.max_send


@pytest.mark.parametrize("call", [
    pytest.param(lambda mc: mc.read_status(), id="read"),
    pytest.param(lambda mc: mc.stop_motion(4000), id="halt-class stop"),
    pytest.param(lambda mc: mc.fifo_stop(), id="fifo stop"),
])
def test_reads_and_halts_are_still_resent_after_a_halt(call):
    sup, mc, link = _channel(_halt_meanwhile)
    with pytest.raises(ControllerError) as ei:
        call(mc)
    assert not isinstance(ei.value, CommandRefused)
    assert len(link.sent) == mc.cfg.max_send


def test_motion_is_not_resent_once_a_fault_is_latched():
    sup, mc, link = _channel(lambda s: s.fsm.raise_fault("link lost: test"))
    with pytest.raises(CommandRefused):
        mc.jog(0, 1000, 10000, 5000)
    assert len(link.sent) == 1


def test_the_halt_channel_resends_its_halts():
    sup, mc, link = _channel()
    h = sup._halt
    assert h.resend_guard is None
