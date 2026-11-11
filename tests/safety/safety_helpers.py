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

import pytest


from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part

SPEED = 100.0                 # mm/s cutting speed of the test job

#: ``make_ctl`` keywords of the two streaming paths: the in-thread streamer at the simulator's
#: link deadline (1 s), and the configuration a real card runs, the process streamer with the
#: real 150 ms link deadline (review arch.md A5).  Tests of the second may rerun once
#: (``@pytest.mark.flaky(reruns=1)``, docs/dev/TESTING.md "Timing-sensitive tests").
REAL_AND_THREAD = [pytest.param(dict(process=False), id="thread"),
                   pytest.param(dict(process=True, link_deadline_ms=150.0), id="process-150ms")]


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
    """Head travel and time from a reference moment until the card is safe.

    ``from_halt``: the travel is measured from where the head was when the card received the
    halt's stop command (the halt's own evidence), not from where the simulator's motion thread
    last left it when the Watch was made: under load (Windows CI) that thread or the test
    thread can be descheduled for a few hundred ms, and the motion of that gap was counted as
    travel "after" the call under test (46 mm once).  ``halt_delay_s`` is the time from the
    Watch to the halt's arrival (perf_counter, not the 15.6 ms Windows monotonic clock)."""

    def __init__(self, sim, from_halt: bool = False):
        self.sim = sim
        self.halt_at: tuple[float, tuple[float, float]] | None = None
        if from_halt:
            real = sim._mailbox

            def spy(words, real=real):
                if self.halt_at is None and list(words[:2]) == [1, 0x1F]:     # stop motion
                    self.halt_at = (time.perf_counter(), head_mm(sim))
                return real(words)
            sim._mailbox = spy
        self.p0 = head_mm(sim)
        self.t0 = time.monotonic()
        self.pc0 = time.perf_counter()

    @property
    def halt_delay_s(self) -> float | None:
        return None if self.halt_at is None else self.halt_at[0] - self.pc0

    def until_safe(self, timeout: float) -> tuple[float | None, float]:
        dt = wait_until(lambda: safe(self.sim), timeout)
        p1 = head_mm(self.sim)
        p0 = self.halt_at[1] if self.halt_at is not None else self.p0
        return dt, math.dist(p0, p1)


def start_and_run(ctl, doc=None, run_s: float = 0.6):
    """Start a long job and wait until the card is cutting for ``run_s``.  Like an operator, it
    waits for the card to come to rest first (the start gate refuses a start while the axes
    still move, e.g. the pessimistic simulator's halt lag after a stop)."""
    wait_until(lambda: stopped(ctl.sim), 5)
    ctl.start(doc or rect_doc())
    assert wait_until(lambda: ctl.sim.state.fifo_running and ctl.sim.state.laser, 20), "job did not start"
    time.sleep(run_s)


def thread_stacks() -> str:
    """Every thread's stack, for the message of a timed-out wait (CI-only hangs)."""
    import sys
    import threading
    import traceback
    names = {t.ident: t.name for t in threading.enumerate()}
    return "\n".join(f"--- {names.get(tid, tid)}\n" + "".join(traceback.format_stack(f))
                     for tid, f in sys._current_frames().items())



def _sim_server(conn, kw: dict) -> None:
    """Spawned: a CardSimulator in a process of its own (port out, any message in = stop)."""
    from mlaser.controller.simulator import CardSimulator
    sim = CardSimulator(**kw).start()
    conn.send(sim.port)
    try:
        conn.recv()
    except EOFError:
        pass
    sim.stop()


class SimProcess:
    """The card simulator in its own process, for tests that load the test process on purpose
    (CPU-bound threads): the card then answers on time however starved the parent is, as a real
    card would.  ``with SimProcess() as port: ...``"""

    def __init__(self, **kw) -> None:
        self.kw = kw

    def __enter__(self) -> int:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        self._conn, child = ctx.Pipe()
        self._proc = ctx.Process(target=_sim_server, args=(child, self.kw), daemon=True)
        self._proc.start()
        if not self._conn.poll(60):
            self._proc.kill()
            raise RuntimeError("the simulator process did not start")
        return self._conn.recv()

    def __exit__(self, *exc) -> None:
        try:
            self._conn.send("stop")
        except OSError:
            pass
        self._proc.join(5)
        if self._proc.is_alive():
            self._proc.kill()
