"""Diagnostics › Dedust switches the controller's dedust output through the controller's command
path (``ctl.set_output("dedust", ...)``: the safety guard, the state under its lock, the log)
and never writes into the live ``ctl.state()`` (deep review 2026-11, arch A6 remainder).  A
DO.ClearAsh port that is not the controller's dedust output goes through the supervised card
handle as before; the controller's state then follows the card on the next status poll."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from mlaser.diag import customio as CIO  # noqa: E402
from mlaser.machine.api import MachineState  # noqa: E402
from mlaser.ui.ext_diag import DiagExtension  # noqa: E402


class Card:
    """The supervised card handle: output words and set_output calls."""

    def __init__(self):
        self.on: dict[int, bool] = {}
        self.calls: list = []

    def read_status(self):
        return SimpleNamespace(output=lambda n: self.on.get(n, False))

    def set_output(self, n, on):
        self.calls.append((n, on))
        self.on[n] = on


class Ctl:
    output_map = {"dedust": 4}

    def __init__(self, card):
        self._card = card
        self.calls: list = []
        self.states = 0
        self._s = MachineState(connected=True)

    def card(self):
        return self._card

    def set_output(self, name, on):
        self.calls.append((name, on))
        self._card.set_output(self.output_map[name], on)

    def state(self):
        self.states += 1
        return self._s


def _ext(settings, card=None):
    card = Card() if card is None else card
    ctl = Ctl(card)
    log: list = []
    ctx = SimpleNamespace(controller=ctl, action=lambda _cmd: None, log=log.append)    # ExtensionContext
    return SimpleNamespace(settings=settings, ctx=ctx, outputs=CIO.OutputDriver(ctl)), ctl, card


def test_the_controllers_dedust_output_goes_through_its_command_path():
    ext, ctl, card = _ext({})
    assert DiagExtension.dedust(ext, True) is True
    assert ctl.calls == [("dedust", True)]
    assert card.calls == [(4, True)]                  # once, through the controller
    assert ctl.states == 0 and ctl._s.outputs["dedust"] is False
    assert DiagExtension.dedust(ext) is False         # toggle: reads the card's output
    assert ctl.calls[-1] == ("dedust", False)
    assert ctl.states == 0


def test_another_dedust_port_uses_the_card_handle_and_leaves_the_state_alone():
    ext, ctl, card = _ext({"DO.ClearAsh": 6})
    assert DiagExtension.dedust(ext, True) is True
    assert ctl.calls == [] and card.calls == [(6, True)]
    assert ctl.states == 0 and ctl._s.outputs["dedust"] is False
