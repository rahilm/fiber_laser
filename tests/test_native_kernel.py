"""The optional native emitter kernel (mlaser_native, profile ``native-sampling``) against the
Python port: hypothesis property tests on random sample streams, a sample of golden jobs, the
build flags (no FMA), and the behaviour without the kernel.  docs/dev/NATIVE_KERNEL.md.

The full comparison over every golden job is ``tools/native_parity.py`` (test below, marker
``golden``).
"""
from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import mlaser.parity_ports.profiles.native as N
from mlaser.parity_ports import PROFILES, use_profile
from mlaser.parity_ports.mainapp import sampling as S

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------- without the kernel
def test_profile_registered_and_clear_error_without_kernel(monkeypatch):
    assert PROFILES[N.NAME] is N.NATIVE
    assert N.NATIVE.motion is PROFILES["original"].motion      # only the sampling slot differs
    monkeypatch.setattr(N, "_K", None)
    assert not N.available()
    from mlaser.nc import compile_job
    from mlaser.model import Contour, Document, Line, Part
    doc = Document(parts=[Part([Contour([Line((0, 0), (5, 0)), Line((5, 0), (0, 0))], True)])])
    with use_profile(N.NAME), pytest.raises(N.NativeKernelMissing, match="tools/build_native.py"):
        compile_job(doc)
    assert issubclass(N.NativeKernelMissing, ImportError)


# ----------------------------------------------------------------------------- with the kernel
try:
    from mlaser_native import _emitter as K
except ImportError:
    K = None
needs_kernel = pytest.mark.skipif(K is None, reason="mlaser_native not built (tools/build_native.py)")
hyp = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

from mlaser.nc.process import SpeedCurve  # noqa: E402


def _machine(ppx, ppy, max_step=127, period=1.0):
    return SimpleNamespace(pulses_per_mm_x=ppx, pulses_per_mm_y=ppy, max_step=max_step, period_ms=period)


def _state(em):
    return {
        "wire": em.wire, "starts": em.starts, "steps": em.steps, "overspeed": em.overspeed,
        "cur": list(em.cur), "pos": tuple(float(v).hex() for v in em.pos),
        "carry": tuple(float(v).hex() for v in em.carry), "on_len": float(em.on_len).hex(),
    }


def _run(native: bool, m, start, dry, carry, ops):
    em = (N.NativeEmitter if native else S.Emitter)(m, start, dry, carry)
    emit = N.emit_samples if native else S.emit_samples
    out = []
    for op in ops:
        kind = op[0]
        try:
            if kind == "emit":
                _, laser, duty, freq, pts, ss, kw = op
                em.laser, em.duty, em.freq = laser, duty, freq
                emit(em, pts, ss, **kw)
            elif kind == "to":
                _, x, y, laser, kw = op
                em.to(x, y, laser, **kw)
            elif kind == "dwell":
                _, sec, laser, duty, freq = op
                em.duty, em.freq = duty, freq
                em.dwell(sec, laser)
            elif kind == "pos":
                em.pos = op[1]
            out.append(("ok",))
        except Exception as e:  # noqa: BLE001 - the error is part of the behaviour
            out.append((type(e).__name__, str(e)))
            break
        out.append(_state(em))
    return out


carry_v = st.one_of(
    st.sampled_from([0.0, -0.0, 0.5, -0.5, 0.4999999999999999, 0.5000000000000001, 0.999, 0.9999999999999999,
                     -0.9999999999999999, 1e-17, -1e-17]),
    st.floats(-0.9999999999, 0.9999999999))
ppm_v = st.one_of(st.sampled_from([1000.0, 1000, 409.6, 2500.0 / 3, 12345.678, 100000.0, 1.0, 3.0]),
                  st.floats(0.5, 2e4))
delta = st.one_of(st.floats(-0.03, 0.03), st.floats(-0.5, 0.5), st.floats(-1e-6, 1e-6),
                  st.just(0.0), st.sampled_from([0.0005, -0.0005, 0.12345, 1e-4 * 0.5]))
duty_v = st.one_of(st.integers(0, 120), st.integers(-300, 300))
freq_v = st.one_of(st.integers(1, 0xFFFF), st.integers(-5, 0x1FFFF))


@st.composite
def curve(draw, base_v):
    if draw(st.integers(0, 4)) == 0:
        k = draw(st.integers(-3, 3))
        return lambda v, k=k: int(v * 0.1) + k           # a generic callable (called back)
    nodes = sorted(draw(st.lists(st.tuples(st.integers(0, 120), st.integers(0, 150)), max_size=6)))
    return SpeedCurve(draw(st.floats(1.0, 500.0)), draw(base_v), nodes, draw(st.booleans()))


@st.composite
def emit_op(draw, start):
    n = draw(st.integers(0, 40))
    x, y = start
    pts, ss, s = [], [], draw(st.floats(0.0, 5.0))
    for _ in range(n):
        x += draw(delta)
        y += draw(delta)
        s += draw(st.one_of(st.floats(0.0, 0.5), st.just(0.0), st.floats(0.0, 80.0)))
        pts.append((x, y))
        ss.append(s)
    kw: dict = {}
    if ss and draw(st.booleans()):
        kw["gaps"] = draw(st.lists(st.tuples(st.floats(-1.0, s + 1), st.floats(-1.0, s + 2)), max_size=3))
    if draw(st.booleans()):
        kw["dcurve"] = draw(curve(st.integers(0, 120)))
        kw["fcurve"] = draw(curve(st.integers(0, 70000)))
    for side in ("up", "down"):
        if draw(st.booleans()):
            kw[side] = (draw(st.floats(0.0, 10.0)), 50.0, draw(duty_v), draw(freq_v), draw(st.booleans()))
    if draw(st.booleans()):
        kw["micro_pct"] = draw(st.integers(0, 100))
    if draw(st.booleans()):
        kw["total"] = draw(st.floats(0.0, s + 5))
    if draw(st.booleans()):
        kw["period_ms"] = draw(st.sampled_from([1.0, 0.5, 2.0, 0.25]))
    return ("emit", draw(st.booleans()), draw(st.integers(1, 100)), draw(st.integers(1, 0xFFFF)), pts, ss, kw)


@st.composite
def ops_v(draw):
    start = (draw(st.floats(-500.0, 500.0)), draw(st.floats(-500.0, 500.0)))
    ops = []
    pos = start
    for _ in range(draw(st.integers(1, 6))):
        k = draw(st.integers(0, 5))
        if k <= 2:
            op = draw(emit_op(pos))
            if op[4]:
                pos = op[4][-1]
        elif k == 3:
            pos = (pos[0] + draw(delta), pos[1] + draw(delta))
            kw = {}
            if draw(st.booleans()):
                kw["duty"] = draw(duty_v)
            if draw(st.booleans()):
                kw["freq"] = draw(freq_v)
            op = ("to", pos[0], pos[1], draw(st.booleans()), kw)
        elif k == 4:
            op = ("dwell", draw(st.floats(0.0, 0.05)), draw(st.booleans()), draw(duty_v), draw(freq_v))
        else:
            pos = (pos[0] + draw(delta), pos[1] + draw(delta))
            op = ("pos", pos)
        ops.append(op)
    return start, ops


@needs_kernel
@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(ppx=ppm_v, ppy=ppm_v, c0=carry_v, c1=carry_v, dry=st.booleans(),
       max_step=st.sampled_from([127, 127, 127, 40, 127.0, 200]), case=ops_v())
def test_kernel_matches_port_on_random_streams(ppx, ppy, c0, c1, dry, max_step, case):
    start, ops = case
    m = _machine(ppx, ppy, max_step)
    assert _run(True, m, start, dry, (c0, c1), ops) == _run(False, m, start, dry, (c0, c1), ops)


@needs_kernel
@settings(max_examples=300, deadline=None)
@given(c=carry_v, ppm=ppm_v, d=st.floats(-1.0, 1.0), laser=st.booleans())
def test_carry_boundaries(c, ppm, d, laser):
    """One axis landing on / next to an integer pulse count: the truncation and the carry."""
    m = _machine(ppm, ppm)
    q = (math.floor(d * ppm) - c) / ppm if ppm else d
    ops = [("to", q, -q, laser, {}), ("to", q + d, -q - d, laser, {"duty": 50})]
    assert _run(True, m, (0.0, 0.0), False, (c, -c), ops) == _run(False, m, (0.0, 0.0), False, (c, -c), ops)


@needs_kernel
def test_kernel_really_runs_and_falls_back():
    m = _machine(1000.0, 1000.0)
    before = dict(N.STATS)
    pts = [(0.01 * i, -0.02 * i) for i in range(1, 50)]
    ss = [0.0223 * i for i in range(1, 50)]
    ops = [("emit", True, 50, 5000, pts, ss, {}),
           ("emit", True, 50, 5000, [list(p) for p in pts], ss, {}),      # lists: port fallback
           ("to", 7, -3, True, {}),                                       # ints: exact in the kernel
           ("to", 7.5, -3, True, {"power": 5}),                           # power override: port
           ("to", 400.0, 0.0, True, {}),                                  # big delta, split
           ("to", -1234.5678, 77.7, False, {"duty": 3})]
    assert _run(True, m, (0.0, 0.0), False, (0, 0), ops) == _run(False, m, (0.0, 0.0), False, (0, 0), ops)
    assert N.STATS["emit"] > before["emit"] and N.STATS["emit_port"] > before["emit_port"]
    assert N.STATS["to"] >= before["to"] + 2 and N.STATS["to_port"] > before["to_port"]


@needs_kernel
@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_kernel_errors_are_the_ports(bad):
    m = _machine(1000.0, 1000.0)
    ops = [("to", 1.0, 1.0, True, {}), ("to", bad, 0.0, True, {})]
    a = _run(True, m, (0.0, 0.0), False, (0, 0), ops)
    assert a == _run(False, m, (0.0, 0.0), False, (0, 0), ops)
    assert a[-1][0] in ("ValueError", "OverflowError")


@needs_kernel
def test_step_over_int8_raises_like_the_port():
    m = _machine(1000.0, 1000.0, max_step=200)
    ops = [("to", 0.15, 0.0, True, {})]
    a = _run(True, m, (0.0, 0.0), False, (0, 0), ops)
    assert a == _run(False, m, (0.0, 0.0), False, (0, 0), ops) and a[-1][0] == "ValueError"


@needs_kernel
@given(st.one_of(st.floats(allow_nan=False, allow_infinity=False, min_value=-2.0**51, max_value=2.0**51),
                 st.integers(-2**20, 2**20).map(lambda k: k + 0.5),
                 st.integers(-2**20, 2**20).map(lambda k: k / 7.0)))
def test_round_half_even(x):
    assert K.round_half_even(x) == round(x)


@needs_kernel
def test_golden_job_sample_identical():
    """A few golden jobs (the full set: tools/native_parity.py / the golden-marked test)."""
    sys.path.insert(0, str(ROOT / "tools"))
    import native_parity as P
    n, steps, bad = P.compare({"jobs_random"}, range(0, 300, 37))
    assert n == 9 and steps > 0 and not bad


@needs_kernel
@pytest.mark.golden
def test_every_golden_job_identical():
    sys.path.insert(0, str(ROOT / "tools"))
    import native_parity as P
    n, steps, bad = P.compare({"jobs_samples", "jobs_random"}, range(300))
    assert n > 300 and not bad, bad


@needs_kernel
def test_no_fma_in_the_kernel():
    """-ffp-contract=off held: no fused multiply-add instruction in the built module."""
    objdump = shutil.which("objdump")
    if objdump is None:
        pytest.skip("objdump not available")
    so = K.__file__
    asm = subprocess.run([objdump, "-d", so], capture_output=True, text=True, check=True).stdout
    sys.path.insert(0, str(ROOT / "tools"))
    import build_native
    assert not build_native.fma_instructions(asm)
