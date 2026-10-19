#!/usr/bin/env python3
"""Golden artefacts: freeze today's behaviour so refactors can prove they changed nothing.

The goldens are the contract of docs/ROADMAP.md: every later change must keep them identical,
or declare (and review) a new behaviour.  They are captured from the current code and stored in
``tests/golden/`` as compact hashes plus a few small exemplars:

=================  =====================================================================
file               content
=================  =====================================================================
jobs_samples.json  ``compile_job`` wire stream and ``Job.frames()`` of the sample .chf
                   files (tests/data) and the parity sample DXFs (tests/data/dxf), two
                   configurations each
jobs_random.json   the same for 300 seeded random jobs (rectangles, circles, polylines
                   with arcs, holes, leads, kerf, micro-joints, overcut/gap)
orders.json        toolpath order (part, contour) for every sort mode x small-first
chf.json           ``.chf`` save bytes of loaded samples, DXF imports and random documents
motion.json        velocity-planner samples and smoother nodes for a set of paths
exemplars.json     the first wire items / frames / samples of a few cases (for diffs)
manifest.json      environment the goldens were made in, the text-font fingerprint
=================  =====================================================================

Usage::

    python tools/make_goldens.py            # regenerate everything (review the git diff!)
    python tools/make_goldens.py --check    # recompute and compare; exit 1 on a difference
    python tools/make_goldens.py --check --only jobs_random orders

Hashes are SHA-256 over a canonical text form (integers in decimal, floats as ``repr``, which
round-trips exactly).  Two tiers:

* exact: wire items, frames, markers, orders, ``.chf`` bytes, counts, planner samples and
  smoother nodes must be bit-identical;
* ``approx``: float aggregates (time estimate, pulse remainder, lengths, plan duration) are
  compared with a relative/absolute tolerance of 1e-9, because they are sums whose last bits
  change between Python versions (3.12's ``sum()`` uses compensated summation).  The MotionItem
  stream hash (``items_hash``) rounds coordinates to 1e-6 mm for the same reason.
  ``--strict`` compares them exactly too.

The reference interpreter is the one in manifest.json (CPython 3.12).  On 3.11 the tolerant tier
passes but one random job (seed 257) plans differently: 3.11's plain ``sum()`` changes a start
point in the last bit and the smoother amplifies it.  tests/golden/test_goldens.py therefore
skips on other minor versions (``MLASER_GOLDEN_ANY_PYTHON=1`` forces it).

DXF samples with TEXT depend on the installed fonts (the outline comes from the font engine), so
cases flagged ``host_dependent`` are compared only when the text-font fingerprint matches the
one in manifest.json, and skipped otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

GOLDEN = ROOT / "tests" / "golden"
DATA = ROOT / "tests" / "data"
FORMAT = 1  # bump when the record layout below changes
N_RANDOM = 300
SORT_MODES = (0, 1, 2, 3, 4, 5, 6, 7, 20, 21)
GROUPS = ("jobs_samples", "jobs_random", "orders", "chf", "motion")


# ----------------------------------------------------------------------------- hashing
class _H:
    def __init__(self):
        self.h = hashlib.sha256()

    def ints(self, seq: Iterable[int]) -> _H:
        self.h.update((",".join(str(int(v)) for v in seq) + ";").encode())
        return self

    def floats(self, seq: Iterable[float]) -> _H:
        self.h.update((",".join(repr(float(v)) for v in seq) + ";").encode())
        return self

    def text(self, s: str) -> _H:
        self.h.update(s.encode() + b"\n")
        return self

    def hex(self) -> str:
        return self.h.hexdigest()


def h_rows(rows: Iterable[Iterable[int]]) -> str:
    h = _H()
    for r in rows:
        h.ints(r)
    return h.hex()


def h_floats(xs: Iterable[float]) -> str:
    return _H().floats(xs).hex()


def h_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _f(x) -> float | None:
    return None if x is None else float(x)


APPROX_TOL = 1e-9


def _r6(v):
    """Round a float to 1e-6 (and turn -0.0 into 0.0) for version-independent hashes."""
    return round(v, 6) + 0.0 if isinstance(v, float) else v


def _round_tree(v):
    if isinstance(v, float):
        return _r6(v)
    if isinstance(v, (list, tuple)):
        return type(v)(_round_tree(x) for x in v) if isinstance(v, tuple) else [_round_tree(x) for x in v]
    if isinstance(v, dict):
        return {k: _round_tree(x) for k, x in v.items()}
    return v


# ----------------------------------------------------------------------------- records
def job_record(job) -> dict:
    frames = job.frames(first_seq=1)
    e = job.estimate or {}
    return {
        "wire": h_rows(job.wire),
        "items": len(job.wire),
        "steps": job.steps,
        "frames": h_rows(frames),
        "n_frames": len(frames),
        "start_pulses": list(job.start_pulses),
        "end_pulses": list(job.end_pulses),
        "markers": h_rows(job.markers),
        "cut_blocks": h_rows(job.cut_blocks),
        "overspeed_periods": job.overspeed_periods,
        "pierces": job.pierces,
        "process_items": job.process_items,
        "approx": {
            "pulse_carry": [float(v) for v in job.pulse_carry],
            "laser_on_length_mm": float(job.laser_on_length_mm),
            "rapid_length_mm": float(job.rapid_length_mm),
            "process_time_s": float(job.process_time_s),
            "estimate_total_s": _f(e.get("total")),
        },
    }


def job_exemplar(job, n: int = 48) -> dict:
    fr = job.frames(first_seq=1)
    return {
        "wire_head": [list(w) for w in job.wire[:n]],
        "wire_tail": [list(w) for w in job.wire[-8:]],
        "frames_head": [list(f) for f in fr[:2]],
        "markers_head": [list(m) for m in job.markers[:8]],
    }


def toolpath_order(items) -> list[list[int]]:
    from mlaser.toolpath.items import Marker

    out = []
    for it in items:
        if isinstance(it, Marker) and it.kind in ("contour_start", "start", "contour"):
            out.append([it.part, it.contour])
    if not out:  # marker kinds differ: fall back to every marker that names a contour
        seen = []
        for it in items:
            if isinstance(it, Marker) and it.contour >= 0:
                k = [it.part, it.contour]
                if not seen or seen[-1] != k:
                    seen.append(k)
        out = seen
    return out


def items_hash(items) -> str:
    """Hash of the full MotionItem stream (types and every field, floats rounded to 1e-6)."""
    import dataclasses

    h = _H()
    for it in items:
        if dataclasses.is_dataclass(it):
            h.text(type(it).__name__ + repr(_round_tree(dataclasses.astuple(it))))
        else:
            h.text(repr(it))
    return h.hex()


def _error(e: BaseException) -> dict:
    return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


# ----------------------------------------------------------------------------- inputs
def text_font_fingerprint() -> dict:
    """Font the DXF TEXT import resolves to on this host, and a hash of a probe outline made the
    way the import makes it (GGO_BEZIER records from the glyf table, else the shaped fallback)."""
    try:
        from mlaser.io.dxf_import import _text_contours, _text_family, _tt_text_contours

        fam = _text_family()
        if fam is None:
            return {"family": None, "probe": None}
        t = {"text": "Ag8&", "height": 10.0, "insert": (0.0, 0.0), "rotation": 0.0}
        cs = _tt_text_contours(t, fam)
        if cs is None:
            cs = _text_contours(t, fam)
        h = _H()
        for c in cs:
            for s in c.segments:
                h.floats(s.start + s.end)
        return {"family": fam, "probe": h.hex()}
    except Exception as e:  # noqa: BLE001
        return {"family": None, "probe": f"{type(e).__name__}"}


def sample_docs() -> list[tuple[str, object, bool]]:
    """(name, Document, host_dependent) for the .chf samples and the sample DXFs."""
    from mlaser.formats.chf import load_chf
    from mlaser.io.dxf_import import import_dxf

    out = []
    for p in sorted(DATA.glob("*.chf")):
        out.append((f"chf/{p.stem}", load_chf(p), False))
    for p in sorted((DATA / "dxf").glob("*.dxf")):
        doc = import_dxf(str(p))
        host = bool(doc.extra.get("texts")) or any(
            isinstance(c.extra, dict) and "text" in c.extra for part in doc.parts for c in part.contours
        )
        out.append((f"dxf/{p.stem}", doc, host))
    return out


def _rect(x, y, w, h):
    from mlaser.model import Contour, Line

    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def _circle(cx, cy, r, ccw=True):
    from mlaser.model import Arc, Contour

    s = (cx + r, cy)
    return Contour([Arc(s, s, (cx, cy), ccw)], True)


def _polyline(rnd: random.Random, cx, cy, r, closed=True):
    """Star-shaped polygon around (cx, cy); some edges replaced by arcs."""
    from mlaser.model import Arc, Contour, Line

    n = rnd.randint(3, 9)
    angs = sorted(rnd.uniform(0, 2 * math.pi) for _ in range(n))
    pts = [
        (cx + r * rnd.uniform(0.45, 1.0) * math.cos(a), cy + r * rnd.uniform(0.45, 1.0) * math.sin(a))
        for a in angs
    ]
    segs = []
    m = n if closed else n - 1
    for i in range(m):
        a, b = pts[i], pts[(i + 1) % n]
        L = math.dist(a, b)
        if L > 1.0 and rnd.random() < 0.25:
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            nx, ny = -(b[1] - a[1]) / L, (b[0] - a[0]) / L
            d = rnd.uniform(0.3, 1.5) * L / 2 * rnd.choice((-1, 1))
            segs.append(Arc(a, b, (mx + nx * d, my + ny * d), rnd.random() < 0.5))
        else:
            segs.append(Line(a, b))
    return Contour(segs, closed)


def _craft(rnd: random.Random, layer: int, length: float):
    from mlaser.model import Craft, LeadParams

    cr = Craft(layer=layer)
    cr.lead_in = LeadParams(
        kind=rnd.choice((0, 1, 1, 2, 3)),
        angle_deg=rnd.uniform(30, 150),
        length=rnd.uniform(0.5, 5.0),
        radius=rnd.uniform(0.5, 3.0),
        at_corner=rnd.random() < 0.2,
    )
    cr.lead_out = LeadParams(
        kind=rnd.choice((0, 0, 0, 1, 2)),
        angle_deg=rnd.uniform(30, 150),
        length=rnd.uniform(0.3, 3.0),
        radius=rnd.uniform(0.3, 2.0),
    )
    cr.kerf_comp = rnd.choice((-1, -1, 0, 1))
    cr.kerf_width = rnd.uniform(0.05, 0.4) if cr.kerf_comp >= 0 else 0.0
    if rnd.random() < 0.4 and length > 5:
        cr.micro_joints = sorted(rnd.uniform(0, length) for _ in range(rnd.randint(1, 3)))
        cr.extra["micro_joint_len"] = rnd.uniform(0.3, 1.5)
    oc = rnd.random()
    if oc < 0.2:
        cr.extra["overcut"] = rnd.uniform(0.3, 2.0)
    elif oc < 0.35:
        cr.extra["overcut"] = -rnd.uniform(0.3, 1.5)
    return cr


def random_doc(seed: int):
    """A seeded random Document plus cam params and machine for :func:`compile_job`."""
    from mlaser.model import Document, LayerParams, Part
    from mlaser.nc import Machine

    rnd = random.Random(seed)
    layers = {}
    for li in range(1, rnd.randint(1, 3) + 1):
        layers[li] = LayerParams(
            li,
            speed=rnd.uniform(20, 250),
            power_pct=rnd.uniform(10, 100),
            frequency_hz=rnd.choice((1000.0, 5000.0, 20000.0)),
            duty_pct=rnd.uniform(20, 100),
        )
    parts = []
    for _ in range(rnd.randint(1, 5)):
        layer = rnd.randint(1, len(layers))
        x, y = rnd.uniform(0, 250), rnd.uniform(0, 160)
        kind = rnd.random()
        contours = []
        if kind < 0.35:
            w, h = rnd.uniform(4, 60), rnd.uniform(4, 40)
            contours.append(_rect(x, y, w, h))
            if rnd.random() < 0.4 and min(w, h) > 8:  # a hole
                r = rnd.uniform(1.0, min(w, h) / 4)
                contours.append(_circle(x + w / 2, y + h / 2, r, rnd.random() < 0.5))
        elif kind < 0.65:
            contours.append(_circle(x, y, rnd.uniform(0.6, 25), rnd.random() < 0.7))
        else:
            contours.append(_polyline(rnd, x, y, rnd.uniform(3, 30), closed=rnd.random() < 0.85))
        part = Part(contours, _craft(rnd, layer, contours[0].length()))
        parts.append(part)
    doc = Document(parts, layers)
    cam = {
        "sort_mode": rnd.choice(SORT_MODES),
        "small_first": rnd.random() < 0.3,
        "start_mode": rnd.choice((0, 1, 2, 3)),
        "start_percent": rnd.uniform(0, 1),
        "return_to_origin": rnd.random() < 0.3,
        "leads_on_open": rnd.random() < 0.2,
    }
    ppm = rnd.choice((100.0, 100.0, 1000.0))
    machine = Machine(pulses_per_mm_x=ppm, pulses_per_mm_y=ppm, smooth=rnd.random() < 0.7)
    dry = rnd.random() < 0.1
    return doc, cam, machine, dry


# ----------------------------------------------------------------------------- groups
SAMPLE_CONFIGS = {
    "default": ({}, {}),
    "bands_nosmooth_ppm1000": (
        {"sort_mode": 1, "start_mode": 0, "return_to_origin": True},
        {"smooth": False, "pulses_per_mm_x": 1000.0, "pulses_per_mm_y": 1000.0},
    ),
}


def gen_jobs_samples(ex: dict) -> dict:
    from mlaser.nc import Machine, compile_job

    out = {}
    for name, doc, host in sample_docs():
        for cname, (cam, mach) in SAMPLE_CONFIGS.items():
            key = f"{name}#{cname}"
            try:
                job = compile_job(doc, dict(cam), Machine(**mach))
                rec = job_record(job)
                if name in ("chf/tempGraph", "dxf/basic_r2000") and cname == "default":
                    ex[f"jobs_samples/{key}"] = job_exemplar(job)
            except Exception as e:  # noqa: BLE001 - the failure is the behaviour
                rec = _error(e)
            rec["host_dependent"] = host
            out[key] = rec
    return out


def random_case(seed: int) -> tuple[dict, object | None]:
    from mlaser.nc import compile_job

    doc, cam, machine, dry = random_doc(seed)
    try:
        job = compile_job(doc, cam, machine, dry_run=dry)
    except Exception as e:  # noqa: BLE001
        return _error(e), None
    return job_record(job), job


def gen_jobs_random(ex: dict) -> dict:
    out = {}
    for seed in range(N_RANDOM):
        rec, job = random_case(seed)
        out[f"seed{seed:03d}"] = rec
        if job is not None and seed < 3:
            ex[f"jobs_random/seed{seed:03d}"] = job_exemplar(job, 24)
    return out


def order_docs() -> list[tuple[str, object, bool]]:
    docs = sample_docs()
    for seed in range(1000, 1020):
        doc, *_ = random_doc(seed)
        docs.append((f"random/seed{seed}", doc, False))
    return docs


def gen_orders(ex: dict) -> dict:
    from mlaser.cam.toolpath import CamParams, build_toolpath

    out = {}
    for name, doc, host in order_docs():
        for mode in SORT_MODES:
            for sf in (False, True):
                key = f"{name}#mode{mode}{'_small' if sf else ''}"
                try:
                    items = build_toolpath(doc, CamParams(sort_mode=mode, small_first=sf))
                    order = toolpath_order(items)
                    rec = {"n": len(order), "order_hash": h_rows(order), "items_hash": items_hash(items)}
                    if len(order) <= 64:
                        rec["order"] = order
                    else:
                        rec["order_head"] = order[:32]
                except Exception as e:  # noqa: BLE001
                    rec = _error(e)
                rec["host_dependent"] = host
                out[key] = rec
    return out


def gen_chf(ex: dict) -> dict:
    from mlaser.formats.chf import dumps_chf

    out = {}
    for name, doc, host in sample_docs():
        try:
            if name.startswith("chf/"):
                b = dumps_chf(doc, version=doc.version)
                src = (DATA / (name[4:] + ".chf")).read_bytes()
                rec = {"bytes": h_bytes(b), "size": len(b), "identical_to_source": b == src}
            else:
                b = dumps_chf(doc)
                rec = {"bytes": h_bytes(b), "size": len(b)}
        except Exception as e:  # noqa: BLE001
            rec = _error(e)
        rec["host_dependent"] = host
        out[name] = rec
    for seed in range(0, N_RANDOM, 6):  # 50 random documents
        doc, *_ = random_doc(seed)
        try:
            b = dumps_chf(doc)
            rec = {"bytes": h_bytes(b), "size": len(b)}
        except Exception as e:  # noqa: BLE001
            rec = _error(e)
        out[f"random/seed{seed:03d}"] = rec
    return out


def motion_paths() -> dict[str, list]:
    """Named chains of CutLine/CutArc moves (feed set per config)."""
    from mlaser.toolpath.items import CutArc, CutLine

    P: dict[str, list] = {}

    def poly(pts, closed=False):
        n = len(pts)
        m = n if closed else n - 1
        return [CutLine(pts[i], pts[(i + 1) % n], 100.0) for i in range(m)]

    for L in (0.05, 0.4, 3.0, 25.0, 400.0):
        P[f"line_{L}"] = poly([(0.0, 0.0), (L, 0.0)])
    P["square_50"] = poly([(0, 0), (50, 0), (50, 50), (0, 50)], True)
    P["triangle_sharp"] = poly([(0, 0), (40, 0), (0, 3)], True)
    P["zigzag"] = poly([(i * 1.7, (i % 2) * (0.5 + 0.3 * (i % 5))) for i in range(24)])
    P["obtuse_chain"] = poly([(0, 0), (10, 0), (20, 2), (30, 6), (38, 14), (42, 25)])
    for r in (0.5, 2.0, 10.0, 60.0):
        P[f"circle_{r}"] = [
            CutArc((r, 0.0), (-r, 0.0), (0.0, 0.0), True, 100.0),
            CutArc((-r, 0.0), (r, 0.0), (0.0, 0.0), True, 100.0),
        ]
    P["slot"] = [
        CutLine((0, 0), (30, 0), 100.0),
        CutArc((30, 0), (30, 10), (30, 5), True, 100.0),
        CutLine((30, 10), (0, 10), 100.0),
        CutArc((0, 10), (0, 0), (0, 5), True, 100.0),
    ]
    P["arc_tangent_line"] = [
        CutArc((0, 0), (10, 10), (0, 10), True, 100.0),
        CutLine((10, 10), (10, 40), 100.0),
        CutArc((10, 40), (14, 44), (14, 40), False, 100.0),
    ]
    for seed in range(6):
        rnd = random.Random(500 + seed)
        pts = [(0.0, 0.0)]
        for _ in range(rnd.randint(4, 14)):
            a = rnd.uniform(0, 2 * math.pi)
            d = rnd.uniform(0.2, 20.0)
            pts.append((pts[-1][0] + d * math.cos(a), pts[-1][1] + d * math.sin(a)))
        P[f"random_poly_{seed}"] = poly(pts)
    return P


def _with_feed(moves, feed):
    import dataclasses

    return [dataclasses.replace(m, feed=feed) for m in moves]


def _segments(moves):
    from mlaser.toolpath.items import CutArc
    from mlaser.model import Arc, Line

    return [
        Arc(m.start, m.end, m.center, m.ccw) if isinstance(m, CutArc) else Line(m.start, m.end) for m in moves
    ]


def motion_configs():
    from mlaser.motion.planner import PlannerParams
    from mlaser.nc import Machine

    base = Machine().motion_config()
    import dataclasses

    yield "smooth_f100", dataclasses.replace(base, smooth=True), 100.0
    yield "plain_f100", dataclasses.replace(base, smooth=False), 100.0
    yield "smooth_f30", dataclasses.replace(base, smooth=True), 30.0
    yield "smooth_f400", dataclasses.replace(base, smooth=True), 400.0
    slow = PlannerParams(**base.cut.__dict__)
    slow.start_slow_len, slow.start_slow_vel, slow.end_slow_len, slow.end_slow_vel = 2.0, 20.0, 1.5, 15.0
    yield "slowends_f150", dataclasses.replace(base, smooth=True, cut=slow), 150.0
    yield (
        "smallcircle_f200",
        dataclasses.replace(base, smooth=True, small_circle_limit=True, small_circle_ratio=0.6),
        200.0,
    )


def gen_motion(ex: dict) -> dict:
    from mlaser.motion.interp import plan_path
    from mlaser.motion.smooth import smooth_nodes

    out = {}
    paths = motion_paths()
    for cname, cfg, feed in motion_configs():
        for pname, moves in paths.items():
            key = f"plan/{pname}#{cname}"
            try:
                mv = _with_feed(moves, feed)
                plan, geom = plan_path(mv, cfg, return_geom=True)
                s = plan.sample()
                xy = []
                for v in s:
                    (x, y), _las = geom.at(v)
                    xy += (x, y)
                rec = {
                    "n": len(s),
                    "samples": h_floats(s),
                    "xy": h_floats(xy),
                    "point_vel": h_floats(plan.point_vel),
                    "blocks": len(plan.blocks),
                    "approx": {"duration": float(plan.duration), "length": float(plan.length)},
                }
                if cname == "smooth_f100" and pname in ("square_50", "circle_2.0", "slot"):
                    ex[f"motion/{key}"] = {"samples_head": s[:32], "samples_tail": s[-8:]}
            except Exception as e:  # noqa: BLE001
                rec = _error(e)
            out[key] = rec
    for pname, moves in paths.items():
        for tol in (0.01, 0.05, 0.3):
            key = f"smooth/{pname}#tol{tol}"
            try:
                nodes = smooth_nodes(_segments(moves), tol=tol)
                flat = [v for n in nodes for v in n]
                rec = {"n": len(nodes), "nodes": h_floats(flat)}
                if pname in ("square_50", "zigzag") and tol == 0.05:
                    ex[f"motion/{key}"] = {"nodes_head": [list(n) for n in nodes[:24]]}
            except Exception as e:  # noqa: BLE001
                rec = _error(e)
            out[key] = rec
    return out


GENERATORS: dict[str, Callable[[dict], dict]] = {
    "jobs_samples": gen_jobs_samples,
    "jobs_random": gen_jobs_random,
    "orders": gen_orders,
    "chf": gen_chf,
    "motion": gen_motion,
}


# ----------------------------------------------------------------------------- io
def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def environment() -> dict:
    import ezdxf
    import numpy
    import shapely

    try:
        import PySide6

        qt = PySide6.__version__
    except ImportError:
        qt = None
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "numpy": numpy.__version__,
        "shapely": shapely.__version__,
        "ezdxf": ezdxf.__version__,
        "pyside6": qt,
    }


def generate(groups: Iterable[str]) -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    exp = GOLDEN / "exemplars.json"
    ex = _load(exp) if exp.exists() else {}
    for g in groups:
        t0 = time.monotonic()
        mine: dict = {}
        cases = GENERATORS[g](mine)
        ex = {k: v for k, v in ex.items() if not k.startswith(g + "/")}
        ex.update(mine)
        _dump(GOLDEN / f"{g}.json", {"format": FORMAT, "group": g, "cases": cases})
        print(f"{g}: {len(cases)} cases in {time.monotonic() - t0:.1f} s")
    _dump(exp, ex)
    _dump(
        GOLDEN / "manifest.json",
        {
            "format": FORMAT,
            "generated_with": environment(),
            "text_font": text_font_fingerprint(),
            "groups": {
                g: len(_load(GOLDEN / f"{g}.json")["cases"])
                for g in GROUPS
                if (GOLDEN / f"{g}.json").exists()
            },
        },
    )


def _close(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return math.isclose(a, b, rel_tol=APPROX_TOL, abs_tol=APPROX_TOL)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_close(a[k], b[k]) for k in a)
    return a == b


def _same(a: dict, b: dict, strict: bool) -> list[str]:
    """Names of the fields that differ (``approx`` within the tolerance unless ``strict``)."""
    out = []
    for f in sorted(set(a) | set(b)):
        x, y = a.get(f), b.get(f)
        if f == "approx" and not strict:
            if not _close(x, y):
                out.append(f)
        elif x != y:
            out.append(f)
    return out


def compare_group(
    g: str, font_ok: bool | None = None, strict: bool = False
) -> tuple[list[str], list[str]]:
    """Recompute group ``g``; returns (differences, skipped case names)."""
    want = _load(GOLDEN / f"{g}.json")
    if want.get("format") != FORMAT:
        return [f"{g}: golden format {want.get('format')} != {FORMAT} (regenerate)"], []
    if font_ok is None:
        font_ok = text_font_fingerprint() == _load(GOLDEN / "manifest.json").get("text_font")
    got = GENERATORS[g]({})
    diffs, skipped = [], []
    for k in sorted(set(want["cases"]) | set(got)):
        a, b = want["cases"].get(k), got.get(k)
        if a is None or b is None:
            diffs.append(f"{g}/{k}: {'new case' if a is None else 'case missing'}")
            continue
        if (a.get("host_dependent") or b.get("host_dependent")) and not font_ok:
            skipped.append(f"{g}/{k}")
            continue
        fields = _same(a, b, strict)
        if fields:
            diffs.append(f"{g}/{k}: " + ", ".join(f"{f}: {a.get(f)!r} -> {b.get(f)!r}"[:240] for f in fields))
    return diffs, skipped


def check(groups: Iterable[str], strict: bool = False) -> int:
    man = _load(GOLDEN / "manifest.json")
    ref = ".".join(man["generated_with"]["python"].split(".")[:2])
    here = ".".join(platform.python_version().split(".")[:2])
    if ref != here:
        print(
            f"warning: goldens were made on Python {ref}, this is {here}; float sums differ "
            "between versions (3.12 sum() is compensated), so some cases can differ"
        )
    font_ok = text_font_fingerprint() == man.get("text_font")
    if not font_ok:
        print("text font differs from the goldens' host: TEXT-bearing samples are skipped")
    bad = 0
    for g in groups:
        t0 = time.monotonic()
        diffs, skipped = compare_group(g, font_ok, strict)
        print(
            f"{g}: {'OK' if not diffs else f'{len(diffs)} DIFFERENCES'} "
            f"({len(skipped)} skipped, {time.monotonic() - t0:.1f} s)"
        )
        for d in diffs[:50]:
            print("  " + d)
        bad += len(diffs)
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="compare with tests/golden instead of writing")
    ap.add_argument("--only", nargs="+", choices=GROUPS, help="limit to these groups")
    ap.add_argument("--strict", action="store_true", help="compare the float aggregates exactly too")
    a = ap.parse_args(argv)
    groups = a.only or list(GROUPS)
    if a.check:
        return check(groups, a.strict)
    generate(groups)
    return 0


if __name__ == "__main__":
    sys.exit(main())
