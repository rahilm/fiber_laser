"""Fixtures of the safety suite (helpers and the scenario notes: safety_helpers.py)."""
from __future__ import annotations

import pytest

@pytest.fixture
def make_ctl():
    """Factory: an armed UiController on a pessimistic simulator (thread or process streamer)."""
    made = []

    def make(process: bool = False, arm: bool = True, time_scale: float = 1.0, **kw):
        from mlaser.controller.ui_adapter import UiController
        sim_kw = {k: kw.pop(k) for k in list(kw) if k in ("fifo_capacity", "halt_lag_ms", "card_soft_limits",
                                                          "pessimistic", "homed")}
        sim_kw.setdefault("pessimistic", True)
        ctl = UiController(poll_s=0.02, stream_process=process, **kw)
        made.append(ctl)
        assert ctl.connect_simulator(time_scale=time_scale, **sim_kw)
        if arm:
            assert ctl.arm()
        return ctl

    yield make
    for c in made:
        try:
            if c.sim is not None:
                c.sim.clear_faults()
            c.disconnect()
        except Exception:  # noqa: BLE001
            pass
