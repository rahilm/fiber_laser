#!/usr/bin/env python3
"""Emitter loop, Python port vs the C kernel (docs/dev/NATIVE_KERNEL.md, PERFORMANCE.md).

Usage::

    python tools/bench_native.py [tests/data/chf/sample_v9.chf] [--parts 300] [--repeat 3] [--smooth]

Without ``--smooth`` the machine is ``Machine(smooth=False)`` (the emitter in isolation, the
figures of NATIVE_KERNEL.md); ``--smooth`` uses the default machine (smoothing on), which is
what the application compiles.

For each job (a .chf file and an N-part synthetic job as tools/bench/bench_large_compile.py
builds it) it prints

* ``compile_job`` end to end (document -> toolpath -> job, what "compile" in the app costs)
  and ``compile_items`` wall time and us/step (the compiler: planning, sampling, process
  items, emitter), with the default profile ``original`` on the C kernel and on the Python
  port (``original-python``, what ``MLASER_NATIVE=0`` gives);
* the emitter alone: every ``emit_samples`` call of the job is recorded once, then replayed on
  a fresh emitter with the port and with the kernel (us/step and s per million steps), with a
  check that the replays produce the same wire items.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mlaser.parity_ports import ORIGINAL, ORIGINAL_PYTHON, use_profile  # noqa: E402
from mlaser.parity_ports.mainapp import sampling as S  # noqa: E402
import mlaser.parity_ports.profiles.native as N  # noqa: E402

SMOOTH = False      # --smooth: the default machine (CContoutSmooth corner blending on)


def synthetic(n: int):
    from mlaser.model import Arc, Contour, Craft, Document, LeadParams, Line, Part
    cols = int(math.ceil(math.sqrt(n * 2)))

    def rect(x, y, w, h):
        p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)

    def circ(cx, cy, r):
        s = (cx + r, cy)
        return Contour([Arc(s, s, (cx, cy), True)], True)

    return Document(parts=[Part([circ((k % cols) * 30 + 12, (k // cols) * 30 + 12, 5),
                                 rect((k % cols) * 30, (k // cols) * 30, 24, 24)],
                                Craft(layer=1, lead_in=LeadParams(kind=1, length=2.0))) for k in range(n)])


def prepare(doc):
    from mlaser.cam.toolpath import CamParams, build_toolpath
    from mlaser.nc import compiler as C
    d2 = C.placed_document(C.with_layers(doc, None))
    prm = CamParams()
    return d2, prm, build_toolpath(d2, prm)


def compile_time(d2, prm, items, profile: str, repeat: int):
    from mlaser.nc import compiler as C
    from mlaser.nc.machine import Machine
    best, job = math.inf, None
    for _ in range(repeat):
        with use_profile(profile):
            t = time.perf_counter()
            job = C.compile_items(items, Machine(smooth=SMOOTH), prm.origin, d2, False)
            best = min(best, time.perf_counter() - t)
    return best, job


def compile_job_time(doc, profile: str, repeat: int) -> float:
    from mlaser.nc import compile_job
    from mlaser.nc.machine import Machine
    best = math.inf
    for _ in range(repeat):
        with use_profile(profile):
            t = time.perf_counter()
            compile_job(doc, None, Machine(smooth=SMOOTH))
            best = min(best, time.perf_counter() - t)
    return best


def record_calls(d2, prm, items):
    """The emit_samples calls of one compile (default profile)."""
    from mlaser.nc import compiler as C
    from mlaser.nc.machine import Machine
    calls = []
    impl = ORIGINAL.sampling
    real = impl.emit_samples

    def spy(em, pts, ss, **kw):
        calls.append((list(pts), list(ss), dict(kw), bool(em.laser), em.duty, em.freq, em.pos))
        return real(em, pts, ss, **kw)

    impl._cache["emit_samples"] = spy
    try:
        m = Machine(smooth=SMOOTH)
        C.compile_items(items, m, prm.origin, d2, False)
    finally:
        impl._cache["emit_samples"] = real
    return m, calls


def replay(m, calls, native: bool, repeat: int):
    best, em = math.inf, None
    for _ in range(repeat):
        em = (N.NativeEmitter if native else S.Emitter)(m, (0.0, 0.0), False)
        fn = N.emit_samples if native else S.emit_samples
        t = time.perf_counter()
        for pts, ss, kw, laser, duty, freq, pos in calls:
            em.laser, em.duty, em.freq, em.pos = laser, duty, freq, pos
            fn(em, pts, ss, **kw)
        best = min(best, time.perf_counter() - t)
    return best, em


def bench(name: str, doc, repeat: int) -> None:
    d2, prm, items = prepare(doc)
    tp, jp = compile_time(d2, prm, items, ORIGINAL_PYTHON.name, repeat)
    tn, jn = compile_time(d2, prm, items, ORIGINAL.name, repeat)
    same = jp.wire == jn.wire and jp.pulse_carry == jn.pulse_carry and jp.laser_on_length_mm == jn.laser_on_length_mm
    print(f"{name}: {jp.steps} steps, {len(jp.wire)} wire items")
    cp = compile_job_time(doc, ORIGINAL_PYTHON.name, repeat)
    cn = compile_job_time(doc, ORIGINAL.name, repeat)
    print(f"  compile_job    python {cp:.3f} s   native {cn:.3f} s   x{cp / cn:.2f}")
    print(f"  compile_items  python {tp:.3f} s ({1e6 * tp / jp.steps:.2f} us/step)   "
          f"native {tn:.3f} s ({1e6 * tn / jn.steps:.2f} us/step)   x{tp / tn:.2f}   identical={same}")
    m, calls = record_calls(d2, prm, items)
    rp, ep = replay(m, calls, False, repeat)
    rn, en = replay(m, calls, True, repeat)
    st = ep.steps
    same = ep.wire == en.wire and ep.carry == en.carry and ep.on_len == en.on_len
    print(f"  emitter only   {len(calls)} runs, {st} steps: python {1e6 * rp / st:.3f} us/step "
          f"({rp * 1e6 / st:.2f} s/M steps)   native {1e6 * rn / st:.3f} us/step ({rn * 1e6 / st:.3f} s/M steps)"
          f"   x{rp / rn:.1f}   identical={same}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("chf", nargs="?", default=str(ROOT / "tests" / "data" / "chf" / "sample_v9.chf"))
    ap.add_argument("--parts", type=int, default=300)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--smooth", action="store_true",
                    help="the default machine (smoothing on, what the app compiles); default: smooth=False")
    a = ap.parse_args(argv)
    global SMOOTH
    SMOOTH = a.smooth
    print(f"Machine(smooth={SMOOTH})")
    N.require()
    st = N.kernel_status()
    print(st.text())
    if not st.native:
        print("the default profile does not run the C kernel: nothing to compare")
        return 2
    from mlaser.formats.chf import load_chf
    bench(Path(a.chf).name, load_chf(a.chf), a.repeat)
    bench(f"synthetic {a.parts} parts", synthetic(a.parts), a.repeat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
