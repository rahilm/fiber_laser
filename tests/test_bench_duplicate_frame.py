"""tools/bench_duplicate_frame.py: the bench check of a repeated FIFO frame (docs/dev/BENCH_CHECKS.md)
gives the right verdict for each simulator model, and refuses a real card without the laser
confirmation."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.sim

ROOT = Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location("bench_duplicate_frame", ROOT / "tools" / "bench_duplicate_frame.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                  # dataclasses look their module up there
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("run", [False, True], ids=["no-motion", "run"])
@pytest.mark.parametrize("model", ["accept-twice", "dedupe", "reject"])
def test_the_bench_check_identifies_each_model(model, run, capsys):
    from mlaser.controller.simulator import CardSimulator
    t = _tool()
    sim = CardSimulator(duplicate_fifo=model).start()
    try:
        res = t.check("127.0.0.1", sim.port, run=run)
        assert res.verdict == model and t.model_of(res) == model, (res.replies, res.free, res.notes)
        if run:
            assert res.travel == res.expected_once * (2 if model == "accept-twice" else 1)
        else:
            assert res.travel is None and sim.position(0) == 0          # nothing moved
        st = sim.state
        assert not st.fifo and not st.fifo_running and not st.program_active and not sim.beam_on
        assert st.fifo_duplicates == 1
    finally:
        sim.stop()
    assert t.main(["--sim", model]) == 0
    assert f"duplicate_fifo={model!r}" in capsys.readouterr().out


def test_a_real_card_needs_the_laser_confirmation(capsys):
    assert _tool().main(["--card", "127.0.0.1:9"]) == 2
    assert "refused" in capsys.readouterr().err


def test_a_busy_card_is_not_tested():
    from mlaser.controller.simulator import CardSimulator
    t = _tool()
    sim = CardSimulator().start()
    try:
        sim.set_estop(True)
        res = t.check("127.0.0.1", sim.port)
        assert res.verdict == "not ready" and sim.state.fifo_frames == 0
    finally:
        sim.stop()
