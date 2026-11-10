"""A latched FAULT is never overwritten by a racing transition (review A2, docs/SAFETY.md §2).

Every :class:`SafetyFSM` transition holds the lock from the read of the state to the write, and
FAULT is left only through ``reset()`` / ``offline()``.  Two kinds of test:

* a deterministic interleaving: the FSM's lock is wrapped so that ``raise_fault`` runs on
  another thread exactly at the k-th lock acquisition of the transition under test (before the
  fix ``job_paused`` read the state without the lock and ``job_ended`` released it between
  decision and transition, so a fault latched there was overwritten by PAUSED / DISARMED);
* the reviewer's barrier race (``arch/core/fsm_race.py``) with a tiny switch interval.
"""
from __future__ import annotations

import sys
import threading

import pytest

from mlaser.machine.safety import SafetyFSM, SafetyState

pytestmark = pytest.mark.safety


def _running(arm: bool = True) -> SafetyFSM:
    f = SafetyFSM()
    f.connected()
    if arm:
        assert f.arm() is None
    assert f.job_started(arm) is None
    return f


def _paused() -> SafetyFSM:
    f = _running()
    f.job_paused()
    return f


class _HookLock:
    """An RLock that, on the ``at``-th acquisition by ``owner``, first lets ``hook`` run to
    completion on another thread (bounded: a hook blocked by the lock just waits for it)."""

    def __init__(self, at: int, hook) -> None:
        self._l = threading.RLock()
        self.at, self.hook, self.n, self.owner = at, hook, 0, None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if threading.current_thread() is self.owner:
            n, self.n = self.n, self.n + 1
            if n == self.at:
                t = threading.Thread(target=self.hook)
                t.start()
                t.join(timeout=0.3)
        return self._l.acquire(blocking, timeout)

    def release(self) -> None:
        self._l.release()

    __enter__ = acquire

    def __exit__(self, *exc) -> None:
        self.release()


TRANSITIONS = {
    "job_paused": (_running, lambda f: f.job_paused()),
    "job_resumed": (_paused, lambda f: f.job_resumed()),
    "job_ended": (_running, lambda f: f.job_ended("job ended")),
    "job_ended_disarm": (_running, lambda f: f.job_ended("job ended", disarm=True)),
    "job_started": (_paused, lambda f: f.job_started(True)),
    "disarm": (lambda: _running(), lambda f: f.disarm()),
    "connected": (lambda: _running(), lambda f: f.connected()),
}


@pytest.mark.parametrize("at", [0, 1, 2, 3])
@pytest.mark.parametrize("name", sorted(TRANSITIONS))
def test_fault_latched_between_decision_and_transition_stays(name, at):
    make, step = TRANSITIONS[name]
    f = make()
    lock = _HookLock(at, lambda: f.raise_fault("interlock door"))
    lock.owner = threading.current_thread()
    f._lock = lock
    step(f)
    lock.owner = None
    if lock.n <= at:                    # the transition took fewer lock acquisitions: fault after it
        lock.hook()
    t_end = 0.0
    while f.state != SafetyState.FAULT and t_end < 1.0:   # a hook that waited for the lock
        threading.Event().wait(0.01)
        t_end += 0.01
    assert f.fault == "interlock door"
    assert f.state == SafetyState.FAULT and f.faulted, (name, at, f.transitions)
    assert not f.armed


@pytest.mark.parametrize("step", [
    lambda f: f.job_paused(), lambda f: f.job_resumed(), lambda f: f.job_ended("x"),
    lambda f: f.job_ended("x", disarm=True), lambda f: f.job_started(False), lambda f: f.disarm(),
    lambda f: f.connected(), lambda f: f.arm(), lambda f: f._go(SafetyState.PAUSED),
    lambda f: f._go(SafetyState.DISARMED), lambda f: f._go(SafetyState.ARMED),
    lambda f: f._go(SafetyState.RUNNING),
])
def test_only_reset_and_offline_leave_fault(step):
    f = _running()
    assert f.raise_fault("estop")
    step(f)
    assert f.state == SafetyState.FAULT and f.fault == "estop" and not f.armed


def test_reset_and_offline_still_leave_fault():
    seen = []
    f = SafetyFSM(on_change=lambda o, n, st, r: seen.append((o, n, st.state)))
    f.connected()
    f.raise_fault("door")
    f.reset()
    assert f.state == SafetyState.DISARMED and not f.fault
    f.raise_fault("door")
    f.offline()
    assert f.state == SafetyState.OFFLINE and f.fault == "door"
    f.connected()                       # the latched text puts it back into FAULT
    assert f.state == SafetyState.FAULT
    f.reset(paused=True)
    assert f.state == SafetyState.PAUSED and not f.armed
    # on_change saw every transition, with the status as of the transition
    assert (SafetyState.FAULT, SafetyState.DISARMED, SafetyState.DISARMED) in seen
    assert (SafetyState.FAULT, SafetyState.OFFLINE, SafetyState.OFFLINE) in seen


def test_on_change_is_called_outside_the_lock():
    """A listener may call back into the FSM from another thread without a deadlock."""
    f = SafetyFSM()
    out = []

    def on_change(old, new, st, reason):
        t = threading.Thread(target=lambda: out.append(f.status()))
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive(), "on_change ran with the FSM lock held"
    f.on_change = on_change
    f.connected()
    f.arm()
    f.job_started(True)
    f.job_ended("done", disarm=True)
    f.raise_fault("x")
    assert len(out) >= 4


def test_job_ended_disarm_is_one_step():
    seen = []
    f = _running()
    f.on_change = lambda o, n, st, r: seen.append((o, n, st.armed, r))
    f.job_ended("done", disarm=True)
    assert f.state == SafetyState.DISARMED and not f.armed
    assert seen == [(SafetyState.RUNNING, SafetyState.DISARMED, False, "disarmed: done")]
    g = _running()
    g.job_ended("done")                 # without disarm: armed stays (the queue's sheets)
    assert g.state == SafetyState.ARMED and g.armed


@pytest.mark.parametrize("other", ["job_paused", "job_ended", "job_resumed"])
def test_barrier_race_does_not_lose_the_fault(other):
    """The reviewer's script (arch/core/fsm_race.py): two threads released by a barrier, a
    tiny switch interval.  Before the fix job_paused lost the fault in ~1 of 10,000 trials."""
    fn = {"job_paused": lambda f: f.job_paused(),
          "job_ended": lambda f: f.job_ended("job ended"),
          "job_resumed": lambda f: (f.job_paused(), f.job_resumed())}[other]
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    lost = []
    try:
        for _ in range(1500):
            f = _running()
            go = threading.Barrier(2)

            def a(f=f, go=go):
                go.wait()
                fn(f)

            def b(f=f, go=go):
                go.wait()
                f.raise_fault("interlock door")
            ta, tb = threading.Thread(target=a), threading.Thread(target=b)
            ta.start()
            tb.start()
            ta.join()
            tb.join()
            if f.fault and f.state != SafetyState.FAULT:
                lost.append(f.transitions)
    finally:
        sys.setswitchinterval(old)
    assert sys.getswitchinterval() == old
    assert not lost, lost[:1]
