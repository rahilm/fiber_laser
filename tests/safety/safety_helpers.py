"""Helpers of the safety suite (marker ``safety``, tests/conftest.py assigns it by directory).

The tests drive the real protocol stack (:class:`UiController` -> SafetySupervisor ->
MotionController) against the in-process card simulator in its **pessimistic** mode: after a
FIFO or decelerated stop the card keeps executing buffered items for ``halt_lag_ms`` and the beam
stays in its last state until an explicit PWM off (docs/SAFETY.md §9).  Every test ends with an
assertion on a safe state within a stated time / distance bound (the "before" numbers are the
measurements of docs/review/architecture.md §2.1).
"""
from __future__ import annotations

import math
import time


from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part

SPEED = 100.0                 # mm/s cutting speed of the test job


def rect_doc(x0=0.0, y0=0.0, w=800.0, h=600.0, layer=1, define=True, speed=SPEED):
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    layers = {layer: LayerParams(layer, speed=speed, power_pct=50)} if define else {}
    return Document(parts=[Part([Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)],
                                Craft(layer=layer))], layers=layers)


def wait_until(pred, timeout: float, step: float = 0.002) -> float | None:
    """Seconds until ``pred()`` held, None on timeout."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return time.monotonic() - t0
        time.sleep(step)
    return None


def head_mm(sim) -> tuple[float, float]:
    return sim.position(0) / sim.ppm, sim.position(1) / sim.ppm


def stopped(sim) -> bool:
    st = sim.state
    return not st.fifo_running and all(abs(a.vel) < 1e-9 for a in st.axes[:2]) and \
        all(a.target is None for a in st.axes[:2])


def safe(sim) -> bool:
    """Card stopped and the beam off (as far as the card reports it)."""
    return stopped(sim) and not sim.beam_on


class Watch:
    """Head travel and time from a reference moment until the card is safe."""

    def __init__(self, sim):
        self.sim = sim
        self.p0 = head_mm(sim)
        self.t0 = time.monotonic()

    def until_safe(self, timeout: float) -> tuple[float | None, float]:
        dt = wait_until(lambda: safe(self.sim), timeout)
        p1 = head_mm(self.sim)
        return dt, math.dist(self.p0, p1)


def start_and_run(ctl, doc=None, run_s: float = 0.6):
    """Start a long job and wait until the card is cutting for ``run_s``.  Like an operator, it
    waits for the card to come to rest first (the start gate refuses a start while the axes
    still move, e.g. the pessimistic simulator's halt lag after a stop)."""
    wait_until(lambda: stopped(ctl.sim), 5)
    ctl.start(doc or rect_doc())
    assert wait_until(lambda: ctl.sim.state.fifo_running and ctl.sim.state.laser, 20), "job did not start"
    time.sleep(run_s)
