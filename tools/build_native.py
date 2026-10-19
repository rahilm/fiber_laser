#!/usr/bin/env python3
"""Build and install the optional native emitter kernel (native/, distribution mlaser-native).

Usage::

    python tools/build_native.py              # editable install into this interpreter, then check
    python tools/build_native.py --wheel      # build a wheel into native/dist/ instead
    python tools/build_native.py --check      # only check the installed module

The build uses the interpreter's own setuptools (no build isolation, no network) and the
flags in native/setup.py (-O2 -ffp-contract=off -fno-fast-math, no -march).  After the build
the module's machine code is disassembled (objdump, when available) and refused if it holds a
fused multiply-add instruction, then a short parity smoke test runs.  ``uv sync`` removes the
editable install (mlaser-native is not a dependency of mlaser); run this again afterwards.
docs/dev/NATIVE_KERNEL.md.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native"

# x86 FMA3/FMA4, AArch64 / ARM, PowerPC fused multiply-add mnemonics
_FMA = re.compile(r"\b(v?fn?m(add|sub)[0-9a-z]*|fn?madd[a-z0-9.]*|fn?msub[a-z0-9.]*|vfma[a-z0-9.]*|"
                  r"vfms[a-z0-9.]*|fmla|fmls)\b", re.I)


def fma_instructions(asm: str) -> list[str]:
    """Fused multiply-add instructions in an ``objdump -d`` listing."""
    out = []
    for line in asm.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and _FMA.search(parts[2].split(" ")[0] if parts[2] else ""):
            out.append(line.strip())
    return out


def _installer() -> list[str]:
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable]
    return [sys.executable, "-m", "pip", "install"]


def build(wheel: bool) -> None:
    if wheel:
        cmd = [sys.executable, "-m", "pip", "wheel", "--no-build-isolation", "--no-deps", "-w",
               str(NATIVE / "dist"), str(NATIVE)]
        if shutil.which("uv"):
            cmd = [shutil.which("uv"), "build", "--wheel", "--no-build-isolation", "--python", sys.executable,  # type: ignore[list-item]
                   "-o", str(NATIVE / "dist"), str(NATIVE)]
    else:
        cmd = _installer() + ["--no-build-isolation", "-e", str(NATIVE)]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def check() -> int:
    import importlib
    importlib.invalidate_caches()
    try:
        from mlaser_native import _emitter as K
    except ImportError as e:
        print(f"mlaser_native is not importable: {e}")
        return 1
    print(f"module {K.__file__}")
    objdump = shutil.which("objdump")
    if objdump:
        asm = subprocess.run([objdump, "-d", K.__file__], capture_output=True, text=True, check=True).stdout
        bad = fma_instructions(asm)
        if bad:
            print("REFUSED: fused multiply-add in the kernel (FP contraction was not disabled):")
            print("\n".join(bad[:20]))
            return 1
        print("no fused multiply-add instructions (objdump)")
    else:
        print("objdump not found: FMA check skipped (check the compiler flags by hand)")
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tools"))
    import native_parity as P
    n, steps, bad = P.compare({"jobs_random"}, range(0, 300, 50))
    print(f"smoke parity: {n} golden jobs, {steps} steps, {len(bad)} differ")
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--wheel", action="store_true", help="build a wheel into native/dist/")
    ap.add_argument("--check", action="store_true", help="only check the installed module")
    a = ap.parse_args(argv)
    if not a.check:
        build(a.wheel)
        if a.wheel:
            return 0
        # a fresh interpreter: the editable install's .pth is read at start-up only
        return subprocess.run([sys.executable, __file__, "--check"]).returncode
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
