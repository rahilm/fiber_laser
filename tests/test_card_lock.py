"""One process drives a card at a time (UI evaluation F6, mlaser/controller/cardlock.py): a
second controller cannot connect to a card another one holds, in this process or another;
the lock goes with its owner's disconnect, exit or crash."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from mlaser.controller import cardlock
from mlaser.controller.simulator import CardSimulator
from mlaser.controller.ui_adapter import UiController

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOLDER = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
from mlaser.controller.ui_adapter import UiController
ctl = UiController(laser_enabled=False)
ok = ctl.connect("127.0.0.1", int(sys.argv[2]))
# its own PID: on Windows a venv's python.exe is a launcher that runs the interpreter as a child,
# so the test's Popen.pid is not the process that holds the lock
print(("connected " + str(os.getpid())) if ok else "refused " + ctl.connect_error, flush=True)
if sys.argv[3] == "crash":
    sys.stdin.readline()
    # a crash: no disconnect, no clean-up, the OS drops the lock.  Windows: TerminateProcess
    # (os._exit runs the DLLs' detach code; the exit code would not be ours)
    if sys.platform.startswith("win"):
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.TerminateProcess(k32.GetCurrentProcess(), 9)
    os._exit(9)
sys.stdin.readline()
ctl.disconnect()
print("released", flush=True)
sys.stdin.readline()
"""


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("MLASER_CARD_LOCK", raising=False)
    return tmp_path / "rt"


@pytest.fixture
def sim():
    s = CardSimulator("127.0.0.1", 0).start()
    yield s
    s.stop()


def _ctl() -> UiController:
    return UiController(laser_enabled=False)


def test_second_controller_in_this_process_is_refused(runtime, sim):
    a, b = _ctl(), _ctl()
    try:
        assert a.connect("127.0.0.1", sim.port)
        assert not b.connect("127.0.0.1", sim.port)
        assert b.card_busy and "in use by another mlaser" in b.connect_error
        assert f"process {os.getpid()}" in b.connect_error
        assert not b._sup.connected, "the refused controller opened a channel to the card"
        assert not b.state().connected
        a.disconnect()
        assert b.connect("127.0.0.1", sim.port) and not b.card_busy and b.connect_error == ""
        assert not a.connect("127.0.0.1", sim.port) and a.card_busy
    finally:
        a.disconnect(quiet=True)
        b.disconnect(quiet=True)


def test_a_failed_connect_does_not_keep_the_lock(runtime):
    a = _ctl()
    # nothing listens on this port: the connect fails (no answer), and the lock is not kept
    assert not a.connect("127.0.0.1", 9)
    assert not a.card_busy and a.connect_error
    lock = cardlock.FileLock(cardlock.lock_path("127.0.0.1", 9))
    lock.acquire()
    lock.release()


@pytest.mark.parametrize("how", ["exit", "crash"])
def test_another_process_holds_the_card_until_it_lets_go(runtime, sim, how):
    env = dict(os.environ, MLASER_RUNTIME_DIR=str(runtime))
    p = subprocess.Popen([sys.executable, "-c", HOLDER, ROOT, str(sim.port), how], env=env,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    me = _ctl()
    try:
        word, _, pid = p.stdout.readline().strip().partition(" ")
        assert word == "connected", (word, pid)
        assert not me.connect("127.0.0.1", sim.port)
        assert me.card_busy and f"process {pid}" in me.connect_error
        p.stdin.write("\n")
        p.stdin.flush()
        if how == "exit":
            assert p.stdout.readline().strip() == "released"
        else:
            assert p.wait(30) == 9
        # Windows releases the locks of a terminated process "depending on available system
        # resources" (LockFileEx): soon, not necessarily at once
        t_end = time.monotonic() + 30
        while not me.connect("127.0.0.1", sim.port) and time.monotonic() < t_end:
            time.sleep(0.1)
        assert me.state().connected and not me.card_busy
    finally:
        me.disconnect(quiet=True)
        try:
            p.stdin.close()                   # EOF: every readline of the holder returns, it exits
        except (OSError, ValueError):
            pass
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(10)


def test_simulators_on_their_own_ports_do_not_collide(runtime):
    a, b = _ctl(), _ctl()
    try:
        assert a.connect_simulator() and b.connect_simulator()
    finally:
        a.disconnect(quiet=True)
        b.disconnect(quiet=True)


def test_escape_hatch_and_keys(runtime, sim, monkeypatch):
    assert cardlock.card_key("LOCALHOST", 502) == cardlock.card_key("127.0.0.1", 502) == "127.0.0.1-502"
    assert cardlock.card_key("10.001.1.168", 502) != "" and cardlock.card_key("Card.Local", 1) == "card.local-1"
    monkeypatch.setenv("MLASER_CARD_LOCK", "0")
    a, b = _ctl(), _ctl()
    try:
        assert a.connect("127.0.0.1", sim.port) and b.connect("127.0.0.1", sim.port)
    finally:
        a.disconnect(quiet=True)
        b.disconnect(quiet=True)
