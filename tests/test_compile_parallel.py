"""Per-chain parallel planning (Review 2 P2-5, mlaser.nc.parallel): bit for bit the serial compile.

The pool is real (spawned worker processes) for the identity tests; the failure paths use an
in-process stand-in for the executor.
"""
from __future__ import annotations

import sys
from concurrent.futures import Future
from pathlib import Path

import pytest

from mlaser.cam.toolpath import CamParams
from mlaser.nc import Machine, compile_items
from mlaser.nc import compiler as C
from mlaser.nc import parallel as PL
from mlaser.parity_ports import ORIGINAL_PYTHON, use_profile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "bench"))

import native_parity as NP  # noqa: E402
from bench_throughput import fingerprint, make_doc  # noqa: E402


def _items(n=12):
    placed, prm, items = C.plan_toolpath(make_doc(n), CamParams(), (0.0, 0.0), None)
    return placed, prm, items


def _both(items, placed, prm, m=None, **kw):
    m = m or Machine()
    a = compile_items(items, m, prm.origin, placed, False, workers=0, **kw)
    b = compile_items(items, m, prm.origin, placed, False, workers=2, **kw)
    return a, b


def test_parallel_compile_is_the_serial_one():
    placed, prm, items = _items(12)
    before = dict(PL.STATS)
    a, b = _both(items, placed, prm)
    assert PL.STATS["pool_groups"] > before["pool_groups"] and PL.STATS["pool_failures"] == before["pool_failures"]
    assert fingerprint(a) == fingerprint(b)
    assert NP.fingerprint(a) == NP.fingerprint(b)


def test_parallel_compile_on_the_python_port_and_without_smoothing():
    placed, prm, items = _items(6)
    with use_profile(ORIGINAL_PYTHON):
        a, b = _both(items, placed, prm)
    assert NP.fingerprint(a) == NP.fingerprint(b)
    a, b = _both(items, placed, prm, Machine(smooth=False))
    assert NP.fingerprint(a) == NP.fingerprint(b)


@pytest.mark.parametrize("seed", [3, 17, 41, 77, 150, 233])
def test_parallel_compile_on_golden_random_jobs(seed):
    import make_goldens as G
    doc, cam, machine, dry = G.random_doc(seed)
    try:
        placed, prm, items = C.plan_toolpath(doc, dict(cam), None, None)
    except Exception:   # noqa: BLE001 - a job whose toolpath fails has nothing to plan
        return
    outs = []
    for w in (0, 2):
        try:
            outs.append(NP.fingerprint(compile_items(items, machine, prm.origin, placed, dry, workers=w)))
        except Exception as e:  # noqa: BLE001 - then the same exception
            outs.append(f"{type(e).__name__}: {e}")
    assert outs[0] == outs[1]


def test_parallel_compile_of_spline_glyphs():
    import make_goldens as G
    docs = {name: doc for name, doc, _h in G.sample_docs()}
    doc = docs["dxf/spline_ellipse"]
    placed, prm, items = C.plan_toolpath(doc, None, None, None)
    a, b = _both(items, placed, prm)
    assert NP.fingerprint(a) == NP.fingerprint(b)


# ----------------------------------------------------------------------------- failures
class _Inline:
    """An executor stand-in that runs plan_group in this process (or fails on demand)."""

    def __init__(self, fail_at=None, drop_chain=None, broken=False):
        self.n = 0
        self.fail_at, self.drop_chain, self.broken = fail_at, drop_chain, broken

    def submit(self, fn, task):
        if self.broken:
            raise RuntimeError("cannot submit")
        f: Future = Future()
        if self.n == self.fail_at:
            f.set_exception(OSError("worker died"))
        else:
            out, ev = fn(task)
            if self.n == self.drop_chain:
                out = [None] + out[1:]       # the worker could not plan the first chain
                ev = None
            f.set_result((out, ev))
        self.n += 1
        return f


@pytest.mark.parametrize("kind", ["fail_at", "drop_chain", "broken"])
def test_pool_failure_falls_back_to_serial(monkeypatch, kind, caplog):
    placed, prm, items = _items(5)
    ref = compile_items(items, Machine(), prm.origin, placed, False, workers=0)
    ex = _Inline(**({kind: 2} if kind != "broken" else {"broken": True}))
    monkeypatch.setattr(PL, "_get_pool", lambda n: ex)
    before = dict(PL.STATS)
    job = compile_items(items, Machine(), prm.origin, placed, False, workers=3)
    assert NP.fingerprint(job) == NP.fingerprint(ref)
    if kind != "drop_chain":
        assert PL.STATS["pool_failures"] == before["pool_failures"] + 1
        assert any("planning serially" in r.getMessage() for r in caplog.records)


def test_cancel_during_a_parallel_compile(monkeypatch):
    placed, prm, items = _items(5)
    monkeypatch.setattr(PL, "_get_pool", lambda n: _Inline())

    def cancel(f):
        if f > 0.5:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        compile_items(items, Machine(), prm.origin, placed, False, progress=cancel, workers=2)
    from mlaser.parity_ports.cadmodule import smooth as S
    assert getattr(S._MEMO, "d", None) is None
    ref = compile_items(items, Machine(), prm.origin, placed, False, workers=0)
    assert NP.fingerprint(compile_items(items, Machine(), prm.origin, placed, False, workers=2)) == NP.fingerprint(ref)


# ----------------------------------------------------------------------------- configuration
def test_worker_setting(monkeypatch):
    monkeypatch.delenv(PL.ENV_WORKERS, raising=False)
    monkeypatch.delenv(PL.ENV_MIN, raising=False)
    monkeypatch.setattr(PL, "_main_is_safe", lambda: True)
    monkeypatch.setattr(PL.os, "cpu_count", lambda: 16)
    assert PL.workers_for(PL.AUTO_MIN_CHAINS - 1) == 0
    assert PL.workers_for(PL.AUTO_MIN_CHAINS) == PL.AUTO_MAX_WORKERS
    for v, want in (("0", 0), ("off", 0), ("1", 0), ("3", 3), ("auto", 8), ("bogus", 8)):
        monkeypatch.setenv(PL.ENV_WORKERS, v)
        assert PL.workers_for(10_000) == want, v
    monkeypatch.setenv(PL.ENV_WORKERS, "3")
    assert PL.workers_for(1) == 0                       # one chain: nothing to share
    monkeypatch.setenv(PL.ENV_WORKERS, "auto")
    monkeypatch.setenv(PL.ENV_MIN, "50")
    assert PL.workers_for(60) == 8
    PL.configure(0)
    try:
        assert PL.workers_for(10_000) == 0
        PL.configure("4", min_chains=10)
        assert PL.workers_for(20) == 4
    finally:
        PL.configure(None)
    monkeypatch.setattr(PL.os, "cpu_count", lambda: 1)
    assert PL.workers_for(10_000) == 0                  # one core: serial
    monkeypatch.setattr(PL.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(PL, "_main_is_safe", lambda: False)
    assert PL.workers_for(10_000) == 0                  # auto never re-runs an unguarded script
    monkeypatch.setenv(PL.ENV_WORKERS, "4")
    assert PL.workers_for(10_000) == 4                  # an explicit setting does


def test_main_script_check(tmp_path, monkeypatch):
    import types
    guarded, bare = tmp_path / "g.py", tmp_path / "b.py"
    guarded.write_text("x = 1\nif __name__ == '__main__':\n    pass\n")
    bare.write_text("x = 1\n")
    for path, want in ((guarded, True), (bare, False), (tmp_path / "missing.py", False)):
        monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace(__file__=str(path)))
        assert PL._main_is_safe() is want
    monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace())
    assert PL._main_is_safe() is True                   # interactive / -c: no main file


def test_no_pool_inside_a_worker(monkeypatch):
    import multiprocessing as mp
    monkeypatch.setenv(PL.ENV_WORKERS, "4")
    monkeypatch.setattr(mp, "parent_process", lambda: object())
    assert PL.workers_for(10_000) == 0
