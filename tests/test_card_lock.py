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
print("connected" if ok else "refused " + ctl.connect_error, flush=True)
if sys.argv[3] == "crash":
    sys.stdin.readline()
    os._exit(9)                      # no disconnect, no clean-up: the OS drops the lock
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
        assert p.stdout.readline().strip() == "connected"
        assert not me.connect("127.0.0.1", sim.port)
        assert me.card_busy and f"process {p.pid}" in me.connect_error
        p.stdin.write("\n")
        p.stdin.flush()
        if how == "exit":
            assert p.stdout.readline().strip() == "released"
        else:
            assert p.wait(30) == 9
        t_end = time.monotonic() + 10
        while not me.connect("127.0.0.1", sim.port) and time.monotonic() < t_end:
            time.sleep(0.1)
        assert me.state().connected and not me.card_busy
    finally:
        me.disconnect(quiet=True)
        try:
            p.stdin.write("\n")
            p.stdin.flush()
        except (OSError, ValueError):
            pass
        p.wait(30)


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
