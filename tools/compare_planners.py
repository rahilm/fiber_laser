#!/usr/bin/env python3
"""Cycle time of the faithful planner vs the optional "modern-ruckig" profile.

The modern planner is NOT validated on hardware; these are planner numbers only (the time
the 1 ms sample stream takes), to validate against the simulator and real traces before cutting.

Usage::

    python tools/compare_planners.py             # golden motion paths x configs + sample jobs
    python tools/compare_planners.py --random 50  # also N seeded random jobs (tools/make_goldens)

Prints Markdown tables.  For every modern plan it also checks the sampled path parameter against
the limits (speed, acceleration, jerk by finite differences on the 1 ms samples) and reports the
worst ratio to the limit.
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("make_goldens", ROOT / "tools" / "make_goldens.py")
G = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(G)  # type: ignore[union-attr]

MODERN = "modern-ruckig"


def _ratios(s: list[float], vmax: float, A: float, J: float) -> tuple[float, float, float]:
    x = np.asarray(s)
    dt = 1e-3
    if len(x) < 4:
        return 0.0, 0.0, 0.0
    v = np.diff(x) / dt
    a = np.diff(x, 2) / dt ** 2
    j = np.diff(x, 3) / dt ** 3
    return float(v.max() / vmax), float(np.abs(a).max() / A), float(np.abs(j).max() / J)


def paths_table() -> None:
    from mlaser.motion.interp import plan_path
    from mlaser.parity_ports import use_profile

    paths = G.motion_paths()
    names = ["square_50", "triangle_sharp", "zigzag", "obtuse_chain", "circle_2.0", "circle_10.0",
             "slot", "arc_tangent_line", "line_400.0", "random_poly_0", "random_poly_3"]
    print("| path | config | length mm | faithful s | modern s | modern/faithful | "
          "plan ms faithful / modern | modern max v/feed, |a|/A, |j|/J |")
    print("|---|---|---:|---:|---:|---:|---:|---|")
    worst = [0.0, 0.0, 0.0]
    for cname, cfg, feed in G.motion_configs():
        prm = cfg.cut.clamped()
        for pname in names:
            mv = G._with_feed(paths[pname], feed)
            res = {}
            for prof in ("original", MODERN):
                with use_profile(prof):
                    t0 = time.perf_counter()
                    plan = plan_path(mv, cfg)
                    res[prof] = (plan, time.perf_counter() - t0)
            fo, tf = res["original"]
            mo, tm = res[MODERN]
            r = _ratios(mo.sample(1.0), feed, prm.max_acc, prm.jerk())
            worst = [max(a, b) for a, b in zip(worst, r)]
            print(f"| {pname} | {cname} | {mo.length:.2f} | {fo.duration:.4f} | {mo.duration:.4f} | "
                  f"{mo.duration / fo.duration:.3f} | {tf * 1e3:.1f} / {tm * 1e3:.1f} | "
                  f"{r[0]:.3f}, {r[1]:.3f}, {r[2]:.3f} |")
    print(f"\nworst over all modern plans: v {worst[0]:.4f}, a {worst[1]:.4f}, j {worst[2]:.4f} "
          "(of the limit; finite differences on the 1 ms samples)\n")


def jobs_table(n_random: int) -> None:
    from mlaser.nc import Machine, compile_job
    from mlaser.parity_ports import use_profile

    cases = [(name, doc, None, Machine(), False) for name, doc, _h in G.sample_docs()]
    for seed in range(n_random):
        doc, cam, machine, dry = G.random_doc(seed)
        cases.append((f"random/{seed}", doc, cam, machine or Machine(), dry))
    print("| job | faithful s | modern s | modern/faithful | compile s faithful / modern |")
    print("|---|---:|---:|---:|---:|")
    tot = [0.0, 0.0]
    errors = []
    for name, doc, cam, machine, dry in cases:
        out = {}
        for prof in ("original", MODERN):
            with use_profile(prof):
                t0 = time.perf_counter()
                try:
                    job = compile_job(doc, cam, machine, dry_run=dry)
                except Exception as e:  # noqa: BLE001
                    out[prof] = (None, repr(e))
                    continue
                out[prof] = (job, time.perf_counter() - t0)
        jf, cf = out["original"]
        jm, cm = out[MODERN]
        if jf is None or jm is None:
            if jm is None and jf is not None:
                errors.append((name, cm))
            continue
        tot[0] += jf.duration_s
        tot[1] += jm.duration_s
        if not name.startswith("random/") or n_random <= 10:
            print(f"| {name} | {jf.duration_s:.3f} | {jm.duration_s:.3f} | "
                  f"{jm.duration_s / max(jf.duration_s, 1e-9):.3f} | {cf:.2f} / {cm:.2f} |")
    print(f"| **total ({len(cases)} jobs)** | {tot[0]:.2f} | {tot[1]:.2f} | "
          f"{tot[1] / max(tot[0], 1e-9):.3f} | |")
    if errors:
        print("\nmodern profile errors (faithful compiled):")
        for name, e in errors:
            print(f"* {name}: {e}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--random", type=int, default=0, help="also N seeded random jobs")
    ap.add_argument("--no-jobs", action="store_true", help="paths table only")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    paths_table()
    if not a.no_jobs:
        jobs_table(a.random)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
