#!/usr/bin/env python3
"""Every golden job with the Python ports and with the C kernel (emitter loop, packed wire,
smoother: crtmath, curve tables, path and plan sampling), bit for bit.

Usage::

    python tools/native_parity.py                 # jobs_samples + the 300 jobs_random
    python tools/native_parity.py --only jobs_random --seeds 0-49

The reference run uses profile ``original-python`` (the port); the other run uses the DEFAULT
profile ``original``, which must have selected the C kernel (``MLASER_NATIVE`` unset or ``1``;
the script refuses to run otherwise), so this checks the default as it is shipped.  With
``--profile native-sampling`` the second run uses that profile instead.

The jobs are exactly those of ``tools/make_goldens.py`` (``sample_docs`` x ``SAMPLE_CONFIGS``,
``random_doc(seed)`` for seed < N_RANDOM).  For each, both compiles must give identical
``Job.wire``, ``Job.frames()``, wire group starts, markers, cut blocks, ManuItem indexes, steps,
overspeed periods, start / end pulses, end position, and, compared EXACTLY (``float.hex``, not
the 1e-9 tier of the goldens), ``pulse_carry``, ``laser_on_length_mm``, ``duration_s`` and the
time estimate; or the same exception
(type and message).  Exit status 1 on any difference.  docs/dev/NATIVE_KERNEL.md.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_goldens as G  # noqa: E402

from mlaser.parity_ports import ORIGINAL, ORIGINAL_PYTHON, use_profile  # noqa: E402
import mlaser.parity_ports.profiles.native as N  # noqa: E402
import mlaser.parity_ports.profiles.native_smooth as NS  # noqa: E402


def _fx(v) -> str:
    return float(v).hex()


def fingerprint(job) -> dict:
    return {
        "wire": [list(w) for w in job.wire],
        "frames": job.frames(first_seq=1),
        "group_starts": tuple(job._groups[2]) if job._groups is not None else None,
        "markers": list(job.markers),
        "cut_blocks": list(job.cut_blocks),
        "manu": [i for i, _ in job.manu],
        "steps": job.steps,
        "overspeed": job.overspeed_periods,
        "process_items": job.process_items,
        "start_pulses": tuple(job.start_pulses),
        "end_pulses": tuple(job.end_pulses),
        "end_mm": tuple(_fx(v) for v in job.end_mm),
        "pulse_carry": tuple(_fx(v) for v in job.pulse_carry),
        "laser_on_length_mm": _fx(job.laser_on_length_mm),
        "duration_s": _fx(job.duration_s),
        "estimate": sorted((k, _fx(v)) for k, v in (job.estimate or {}).items()),
    }


def run(build) -> dict:
    try:
        return fingerprint(build())
    except Exception as e:  # noqa: BLE001 - the failure is the behaviour
        return {"error": f"{type(e).__name__}: {e}"}


def cases(only: set[str], seeds: range):
    """(key, build(workers)): every golden job, its cut chains planned serially (workers=0) or
    by a pool of that many processes (mlaser.nc.parallel)."""
    from mlaser.nc import Machine, compile_job
    if "jobs_samples" in only:
        for name, doc, _host in G.sample_docs():
            for cname, (cam, mach) in G.SAMPLE_CONFIGS.items():
                yield (f"jobs_samples/{name}#{cname}",
                       lambda w, doc=doc, cam=cam, mach=mach: compile_job(doc, dict(cam), Machine(**mach), workers=w))
    if "jobs_random" in only:
        for seed in seeds:
            def build(w, seed=seed):
                doc, cam, machine, dry = G.random_doc(seed)
                return compile_job(doc, cam, machine, dry_run=dry, workers=w)
            yield f"jobs_random/seed{seed:03d}", build


def compare(only: set[str], seeds: range, verbose: bool = False,
            profile: str = ORIGINAL.name, workers: int = 0) -> tuple[int, int, list[str]]:
    """(jobs, steps, differences) of ``original-python`` (serial) against ``profile`` with
    ``workers`` planning processes (0: serial)."""
    n = steps = 0
    bad: list[str] = []
    for key, build in cases(only, seeds):
        with use_profile(ORIGINAL_PYTHON):
            a = run(lambda: build(0))
        with use_profile(profile):
            b = run(lambda: build(workers))
        n += 1
        steps += a.get("steps", 0)
        if a != b:
            diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
            bad.append(f"{key}: {diff}")
            print(f"DIFF {key}: {diff}", flush=True)
        elif verbose:
            print(f"ok   {key} {a.get('steps', a.get('error'))}", flush=True)
    return n, steps, bad


def _seeds(s: str) -> range:
    a, _, b = s.partition("-")
    return range(int(a), int(b or a) + 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", nargs="+", default=["jobs_samples", "jobs_random"],
                    choices=["jobs_samples", "jobs_random"])
    ap.add_argument("--seeds", default=f"0-{G.N_RANDOM - 1}")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--profile", default=ORIGINAL.name, choices=[ORIGINAL.name, N.NAME],
                    help="the profile compared with original-python (default: the default one)")
    ap.add_argument("--workers", type=int, default=0,
                    help="plan the compared run's cut chains in a pool of N processes "
                         "(mlaser.nc.parallel; default 0: serial)")
    a = ap.parse_args(argv)
    N.require()
    st = N.kernel_status()
    print(st.text())
    if a.profile == ORIGINAL.name and not st.native:
        print("the default profile does not run the C kernel here: nothing to compare")
        return 2
    t = time.perf_counter()
    n, steps, bad = compare(set(a.only), _seeds(a.seeds), a.verbose, a.profile, a.workers)
    print(f"{n} jobs, {steps} steps: {len(bad)} differ ({time.perf_counter() - t:.0f} s)")
    print("kernel coverage (calls by the kernel / handed to the port):", dict(N.STATS))
    print("smoother coverage (C / handed to the port):", dict(NS.STATS))
    if a.workers:
        from mlaser.nc import parallel
        print("parallel planning:", dict(parallel.STATS))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
