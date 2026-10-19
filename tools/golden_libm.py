#!/usr/bin/env python3
"""Why do the goldens differ on this platform?  Record the libm results, replay them elsewhere.

The goldens (tools/make_goldens.py) are bit-exact on the reference platform (Linux x86-64,
glibc, CPython 3.12).  On another platform the last bits of a few results can differ.  This tool
tells whether **only** the platform's math library is responsible (docs/dev/TESTING.md,
"Goldens on Windows and macOS")::

    python tools/golden_libm.py record --out trace.json.gz      # on Windows / macOS
    python tools/golden_libm.py replay trace.json.gz            # on the reference platform

``record`` wraps the functions whose result comes from the platform's libm or LAPACK and that
the golden computations call (``math.sin cos tan asin acos atan atan2 exp log log10 log2 pow
fmod``, ``numpy.hypot``, ``numpy.linalg.solve``, shapely's ``buffer``, whose round joins GEOS
computes with the platform's sin / cos, and every ``a ** b`` in mlaser, which is libm ``pow``:
the modules are compiled with the operator rewritten to a wrapped call), runs every golden group with the default
profile, and writes every distinct call (arguments and result as ``float.hex``) plus the
recomputed golden records of this platform.

``replay`` (on the reference platform):

1. recomputes every recorded call with the local library and lists the calls whose result
   differs (function, arguments, both results, distance in ulps);
2. reruns the golden groups with the wrapped functions returning the **recorded** results, and
   compares the records with the recorded platform's: identical records prove that the
   platform's golden differences come from those library results alone (calls the recording
   did not see are counted as misses and computed locally);
3. lists which golden cases differ between the recorded platform and tests/golden.

Exit status of ``replay``: 0 when the recorded platform's records are reproduced exactly and
nothing was missed, 1 otherwise.  CPython's own ``math.hypot`` / ``math.dist`` / ``math.fsum``
and ``sqrt`` are not libm-dependent (correctly rounded / CPython's own code) and are not
wrapped.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import platform
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_EXC = {"ValueError": ValueError, "OverflowError": OverflowError,
        "ZeroDivisionError": ZeroDivisionError}
MATH_FUNCS = ("sin", "cos", "tan", "asin", "acos", "atan", "atan2", "exp", "log", "log10",
              "log2", "pow", "fmod",
              # CPython's own (not libm), wrapped to show that they do NOT differ:
              "hypot", "dist", "sqrt", "fsum")


def _key(args) -> str:
    """Exact text of the arguments: ``float.hex`` for floats (numpy float64 too: the math
    functions convert to a C double anyway), ``#<n>`` for ints, ``(...)`` for a tuple / list of
    them (math.dist); any other type raises TypeError and the call is not wrapped."""
    out = []
    for a in args:
        t = type(a)
        if t is float:
            out.append(a.hex())
        elif t is int:
            out.append(f"#{a}")
        elif t is tuple or t is list:
            out.append("(" + _key(a).replace(",", ";") + ")")
        elif t.__name__ == "float64":
            out.append(float(a).hex())
        else:
            raise TypeError(t)
    return ",".join(out)


def _args(k: str) -> list:
    out: list = []
    for a in k.split(","):
        if a.startswith("("):
            out.append(tuple(_args(a[1:-1].replace(";", ","))) if len(a) > 2 else ())
        elif a.startswith("#"):
            out.append(int(a[1:]))
        else:
            out.append(float.fromhex(a))
    return out


def _arr_key(*arrays) -> str:
    import numpy as np
    h = hashlib.sha256()
    for a in arrays:
        a = np.ascontiguousarray(np.asarray(a, dtype=float))
        h.update(repr(a.shape).encode())
        h.update(a.tobytes())
    return h.hexdigest()


def _arr_out(a) -> list:
    import numpy as np
    a = np.asarray(a, dtype=float)
    return [list(a.shape), a.tobytes().hex()]


def _arr_in(rec):
    import numpy as np
    shape, hx = rec
    return np.frombuffer(bytes.fromhex(hx), dtype=float).reshape(shape).copy()


def _pow_op(a, b):
    return a ** b


def install_pow_hook() -> None:
    """``float ** float`` calls the C library's ``pow`` and cannot be wrapped like a module
    function: mlaser's modules are compiled with every ``a ** b`` rewritten to
    ``__golden_pow__(a, b)`` (a builtin that computes ``a ** b``, wrapped by :class:`Hooks`).
    Must run before mlaser is imported; nothing is written to ``__pycache__``."""
    import ast
    import builtins
    import importlib.abc
    import importlib.machinery

    builtins.__golden_pow__ = _pow_op     # type: ignore[attr-defined]
    sys.dont_write_bytecode = True

    class Rewrite(ast.NodeTransformer):
        def visit_BinOp(self, node):
            self.generic_visit(node)
            if isinstance(node.op, ast.Pow):
                return ast.copy_location(ast.Call(ast.Name("__golden_pow__", ast.Load()),
                                                  [node.left, node.right], []), node)
            return node

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            src = self.get_source(fullname)
            tree = ast.fix_missing_locations(Rewrite().visit(ast.parse(src, self.path)))
            return compile(tree, self.path, "exec", dont_inherit=True)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name != "mlaser" and not name.startswith("mlaser."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = Loader(spec.loader.name, spec.loader.path)
            return spec

    assert "mlaser" not in sys.modules, "install_pow_hook() must run before mlaser is imported"
    sys.meta_path.insert(0, Finder())


def _where() -> str:
    """The innermost mlaser frames of the current call (where a replay first diverged)."""
    import traceback
    fr = [f for f in traceback.extract_stack()[:-2] if "/mlaser/" in f.filename.replace("\\", "/")]
    return " <- ".join(f"{Path(f.filename).name}:{f.lineno}" for f in reversed(fr[-3:]))


class Hooks:
    """Wraps the library functions; ``table`` is None to record, a trace to replay."""

    def __init__(self, table: dict | None = None):
        self.table = table
        self.calls: dict[str, dict[str, str]] = {f: {} for f in (*MATH_FUNCS, "**")}
        self.arrays: dict[str, dict[str, list]] = {"np.hypot": {}, "np.linalg.solve": {}}
        self.geoms: dict[str, dict[str, str]] = {"shapely.buffer": {}}
        self.hits = self.misses = 0
        self.missed: list[str] = []
        self.trace_cases: set[str] = set()       # cases whose ordered call sequence is kept
        self.seq_case: str | None = None
        self.seqs: dict[str, list[str]] = {}
        self._saved: list = []

    def install(self) -> None:
        import numpy as np
        for name in MATH_FUNCS:
            self._saved.append((math, name, getattr(math, name)))
            setattr(math, name, self._scalar(name, getattr(math, name)))
        self._saved.append((np, "hypot", np.hypot))
        np.hypot = self._array("np.hypot", np.hypot)
        self._saved.append((np.linalg, "solve", np.linalg.solve))
        np.linalg.solve = self._array("np.linalg.solve", np.linalg.solve)
        import builtins
        if hasattr(builtins, "__golden_pow__"):
            self._saved.append((builtins, "__golden_pow__", _pow_op))
            builtins.__golden_pow__ = self._scalar("**", _pow_op)
        from shapely.geometry.base import BaseGeometry
        self._saved.append((BaseGeometry, "buffer", BaseGeometry.buffer))
        BaseGeometry.buffer = self._geom("shapely.buffer", BaseGeometry.buffer)

    def remove(self) -> None:
        for mod, name, fn in reversed(self._saved):
            setattr(mod, name, fn)
        self._saved.clear()

    def _scalar(self, name, fn):
        rec = self.calls[name]
        table = None if self.table is None else self.table["calls"].get(name, {})

        def wrapped(*args):
            try:
                k = _key(args)
            except (TypeError, ValueError, OverflowError):
                return fn(*args)
            if self.seq_case is not None:
                self.seqs[self.seq_case].append(f"math.{name}({k}) @ {_where()}")
            if table is not None:
                hx = table.get(k)
                if hx is not None:
                    self.hits += 1
                    if hx.startswith("raise "):
                        raise _EXC.get(hx[6:], ArithmeticError)(f"recorded: {hx[6:]}")
                    return float.fromhex(hx)
                self.misses += 1
                if len(self.missed) < 50:
                    self.missed.append(f"math.{name}({k}) at {_where()}")
            try:
                r = fn(*args)
            except (ValueError, OverflowError, ZeroDivisionError) as e:
                rec[k] = f"raise {type(e).__name__}"
                raise
            if type(r) is float:                # (int ** int, a complex power: not libm)
                rec[k] = r.hex()
            return r
        return wrapped

    def _array(self, name, fn):
        rec = self.arrays[name]
        table = None if self.table is None else self.table["arrays"].get(name, {})

        def wrapped(*args, **kw):
            if kw or len(args) != 2:
                return fn(*args, **kw)
            k = _arr_key(*args)
            if self.seq_case is not None:
                self.seqs[self.seq_case].append(f"{name}({k[:16]}) @ {_where()}")
            if table is not None:
                hx = table.get(k)
                if hx is not None:
                    self.hits += 1
                    if isinstance(hx, str):            # the call raised (a singular matrix)
                        import numpy as np
                        raise np.linalg.LinAlgError(f"recorded: {hx}")
                    out = _arr_in(hx)
                    return out if out.shape else out[()]          # numpy scalar, as the original
                self.misses += 1
                if len(self.missed) < 50:
                    self.missed.append(f"{name}(shapes {[getattr(x, 'shape', ()) for x in args]}) at {_where()}")
            try:
                r = fn(*args)
            except Exception as e:  # noqa: BLE001 - recorded, then raised as before
                rec[k] = f"raise {type(e).__name__}"
                raise
            rec[k] = _arr_out(r)
            return r
        return wrapped


    def _geom(self, name, fn):
        """GEOS buffer (round joins and caps: GEOS's own sin / cos, from the platform libm)."""
        rec = self.geoms[name]
        table = None if self.table is None else self.table.get("geoms", {}).get(name, {})

        def wrapped(geom, *args, **kw):
            import shapely
            h = hashlib.sha256(shapely.to_wkb(geom, hex=False, include_srid=False))
            h.update(repr((args, sorted(kw.items()))).encode())
            k = h.hexdigest()
            if self.seq_case is not None:
                self.seqs[self.seq_case].append(f"{name}({k[:16]}) @ {_where()}")
            if table is not None:
                hx = table.get(k)
                if hx is not None:
                    self.hits += 1
                    return shapely.from_wkb(bytes.fromhex(hx))
                self.misses += 1
                if len(self.missed) < 50:
                    self.missed.append(f"{name}({geom.geom_type}) at {_where()}")
            r = fn(geom, *args, **kw)
            rec[k] = shapely.to_wkb(r, hex=True, include_srid=False)
            return r
        return wrapped


def _run_groups(groups, h: "Hooks | None" = None):
    import make_goldens as G
    orig = G.random_case

    def random_case(seed):                 # keeps the call sequence of the traced cases
        label = f"jobs_random/seed{seed:03d}"
        if h is not None and label in h.trace_cases:
            h.seq_case, h.seqs[label] = label, []
        try:
            return orig(seed)
        finally:
            if h is not None:
                h.seq_case = None

    G.random_case = random_case
    try:
        return {g: G.GENERATORS[g]({}) for g in groups}
    finally:
        G.random_case = orig


def _env() -> dict:
    import numpy as np
    return {"python": platform.python_version(), "platform": platform.platform(),
            "machine": platform.machine(), "numpy": np.__version__,
            "native": os.environ.get("MLASER_NATIVE", "")}


def record(out: Path, groups, trace=()) -> int:
    h = Hooks()
    h.trace_cases = set(trace)
    h.install()
    try:
        cases = _run_groups(groups, h)
    finally:
        h.remove()
    data = {"env": _env(), "groups": list(groups), "calls": h.calls, "arrays": h.arrays,
            "geoms": h.geoms, "cases": cases, "seqs": h.seqs}
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump(data, f)
    n = sum(len(v) for d in (h.calls, h.arrays, h.geoms) for v in d.values())
    print(f"{out}: {n} distinct library calls, groups {', '.join(groups)} ({data['env']})")
    return 0


def _ulps(a: float, b: float) -> int:
    def ordered(x: float) -> int:
        i = struct.unpack("<q", struct.pack("<d", x))[0]
        return i if i >= 0 else -(i & 0x7FFFFFFFFFFFFFFF)
    if math.isnan(a) or math.isnan(b):
        return 0 if math.isnan(a) and math.isnan(b) else -1
    return abs(ordered(a) - ordered(b))


def replay(path: Path, show: int) -> int:
    import make_goldens as G
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    groups = data["groups"]
    print(f"recorded on: {data['env']}")
    print(f"replayed on: {_env()}")

    # 1. the library results that differ
    print("\n1. library results that differ from this platform's")
    for name, calls in data["calls"].items():
        fn = _pow_op if name == "**" else getattr(math, name)
        diffs = []
        for k, hx in calls.items():
            args = _args(k)
            try:
                mine = fn(*args).hex()
            except (ValueError, OverflowError, ZeroDivisionError) as e:
                mine = f"raise {type(e).__name__}"
            if mine != hx and not (mine == hx == "nan"):
                diffs.append((args, hx, mine))
        if calls:
            ul = sorted({_ulps(float.fromhex(t), float.fromhex(m)) for _a, t, m in diffs
                         if not t.startswith("raise") and not m.startswith("raise")})
            label = "float ** (libm pow)" if name == "**" else f"math.{name}"
            print(f"  {label}: {len(calls)} distinct calls, {len(diffs)} differ"
                  + (f" (ulps: {ul[:8]})" if diffs else ""))
            for args, theirs, mine in diffs[:show]:
                print(f"      {name}({', '.join(repr(a) for a in args)}) = {theirs} there, {mine} here")
    for name, calls in {**data["arrays"], **data.get("geoms", {})}.items():
        if calls:
            print(f"  {name}: {len(calls)} distinct calls (inputs hashed: their effect shows in step 2)")

    # 2. replay with the recorded results
    print("\n2. golden records recomputed here with the recorded library results")
    h = Hooks(data)
    h.trace_cases = set(data.get("seqs", {}))
    h.install()
    try:
        got = _run_groups(groups, h)
    finally:
        h.remove()
    unexplained = []
    for g in groups:
        theirs = data["cases"][g]
        for k in sorted(set(theirs) | set(got[g])):
            a, b = theirs.get(k), got[g].get(k)
            if a is None or b is None or G._same(a, b, True):
                unexplained.append(f"{g}/{k}")
    print(f"  {h.hits} recorded results used, {h.misses} calls not in the recording")
    for m in h.missed[:show]:
        print(f"      not recorded: {m}")
    print(f"  records not reproduced: {len(unexplained)}")
    for k in unexplained[:show]:
        print(f"      {k}")
    for case, theirs in data.get("seqs", {}).items():
        mine = h.seqs.get(case, [])
        n = next((i for i, (a, b) in enumerate(zip(theirs, mine)) if a.split(" @ ")[0] != b.split(" @ ")[0]),
                 min(len(theirs), len(mine)))
        print(f"  {case}: {len(theirs)} library calls there, {len(mine)} here, first difference at #{n}")
        for i in range(max(0, n - 3), min(n + 2, max(len(theirs), len(mine)))):
            a = theirs[i] if i < len(theirs) else "-"
            b = mine[i] if i < len(mine) else "-"
            print(f"      #{i} there: {a}\n      #{i} here:  {b}")

    # 3. what differs from the committed goldens on the recorded platform
    print("\n3. golden cases that differ on the recorded platform (vs tests/golden)")
    for g in groups:
        want = G._load(G.GOLDEN / f"{g}.json")["cases"]
        theirs = data["cases"][g]
        bad = [k for k in sorted(want) if k in theirs and G._same(want[k], theirs[k], False)]
        bad_strict = [k for k in sorted(want) if k in theirs and G._same(want[k], theirs[k], True)]
        print(f"  {g}: {len(bad)} differ ({len(bad_strict)} with --strict)")
        for k in bad[:show]:
            print(f"      {k}: {', '.join(G._same(want[k], theirs[k], False))}")
    ok = not unexplained and h.misses == 0
    print("\nresult: " + ("the recorded platform's records are reproduced exactly from its library "
                          "results" if ok else "NOT fully explained by the recorded library results"))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record the library calls of the golden groups")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--only", nargs="+", help="golden groups (default: all)")
    r.add_argument("--trace", nargs="+", default=[],
                   help="jobs_random cases (jobs_random/seedNNN) whose ordered call sequence is kept, "
                        "to locate where a replay diverges")
    p = sub.add_parser("replay", help="replay a recording on the reference platform")
    p.add_argument("trace", type=Path)
    p.add_argument("--show", type=int, default=20, help="examples to print per item")
    a = ap.parse_args(argv)
    install_pow_hook()
    if a.cmd == "record":
        import make_goldens as G
        return record(a.out, a.only or list(G.GROUPS), a.trace)
    return replay(a.trace, a.show)


if __name__ == "__main__":
    sys.exit(main())
