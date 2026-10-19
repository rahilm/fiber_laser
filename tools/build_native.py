#!/usr/bin/env python3
"""Rebuild and check the C emitter kernel ``mlaser._native._emitter`` (docs/dev/NATIVE_KERNEL.md).

The extension is part of the mlaser distribution (setup.py): ``uv sync`` / ``pip install -e .``
compile it whenever a C compiler is present (``tool.uv.cache-keys`` makes uv rebuild it when
the C source changes).  This script is for working on the kernel and for CI::

    python tools/build_native.py              # rebuild in place (required: fail on a compile error), then --check
    python tools/build_native.py --wheel      # build a wheel into dist/ (uv build)
    python tools/build_native.py --check      # only check the importable module

``--check``: the module imports; its machine code holds no fused multiply-add instruction
(disassembled with objdump / llvm-objdump, or dumpbin on Windows when it is on PATH; skipped
with a note otherwise); the built-in self-check passes; the default profile selects it; and a
short parity run over golden jobs shows no difference.  Exit status 1 on any failure.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# x86 FMA3/FMA4, AArch64 / ARM, PowerPC fused multiply-add mnemonics
_FMA = re.compile(r"\b(v?fn?m(add|sub)[0-9a-z]*|fn?madd[a-z0-9.]*|fn?msub[a-z0-9.]*|vfma[a-z0-9.]*|"
                  r"vfms[a-z0-9.]*|fmla|fmls)\b", re.I)


def fma_instructions(asm: str) -> list[str]:
    """Fused multiply-add instructions in a disassembly listing.  Formats:

    * GNU objdump: ``addr:<TAB>bytes<TAB>mnemonic operands``;
    * llvm-objdump (macOS ``objdump``): ``addr: bytes<TAB>mnemonic<TAB>operands``;
    * dumpbin /disasm: ``addr: bytes  mnemonic  operands``.

    Symbol names (``<...>``) are ignored."""
    out = []
    for line in asm.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].strip().endswith(":"):     # GNU
            text = parts[2]
        elif len(parts) >= 2 and ":" in parts[0]:                   # llvm
            text = parts[1]
        else:                                                       # dumpbin
            m = re.match(r"\s*[0-9A-Fa-f]+:\s+(?:[0-9A-Fa-f]{2}\s)+\s*(\S+)", line)
            if not m:
                continue
            text = m.group(1)
        word = re.sub(r"<[^>]*>", "", text).strip().split(" ")[0]
        if word and _FMA.fullmatch(word.lower()):
            out.append(line.strip())
    return out


def disassemble(path: str) -> str | None:
    for tool, args in (("objdump", ["-d"]), ("llvm-objdump", ["-d"]), ("dumpbin", ["/disasm"])):
        exe = shutil.which(tool)
        if exe:
            r = subprocess.run([exe, *args, path], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout:
                return r.stdout
    return None


def build(wheel: bool) -> None:
    env = dict(os.environ, MLASER_BUILD_NATIVE="1")     # a compile error fails the build
    uv = shutil.which("uv")
    if wheel:
        cmd = ([uv, "build", "--wheel", "-o", str(ROOT / "dist"), str(ROOT)] if uv else
               [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(ROOT / "dist"), str(ROOT)])
    elif uv:
        cmd = [uv, "pip", "install", "--python", sys.executable, "--no-deps", "--reinstall-package",
               "mlaser", "-e", str(ROOT)]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--no-deps", "--force-reinstall", "-e", str(ROOT)]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, env=env)


def check(parity_seeds: range = range(0, 300, 50)) -> int:
    import importlib
    importlib.invalidate_caches()
    sys.path.insert(0, str(ROOT))
    from mlaser import _native
    if _native._emitter is None:
        print(f"mlaser._native._emitter is not importable: {_native.IMPORT_ERROR}")
        return 1
    path = _native._emitter.__file__ or ""
    print(f"module {path}")
    asm = disassemble(path)
    if asm is None:
        print("no disassembler (objdump / llvm-objdump / dumpbin) found: FMA check skipped "
              "(the self-check below still runs)")
    else:
        bad = fma_instructions(asm)
        if bad:
            print("REFUSED: fused multiply-add in the kernel (FP contraction was not disabled):")
            print("\n".join(bad[:20]))
            return 1
        print("no fused multiply-add instructions")
    import mlaser.parity_ports.profiles.native as N
    err = N.selfcheck()
    if err:
        print(f"self-check FAILED: {err}")
        return 1
    st = N.kernel_status()
    print(st.text())
    if not st.native:
        print("the default profile did not select the kernel")
        return 1
    sys.path.insert(0, str(ROOT / "tools"))
    import native_parity as P
    n, steps, diff = P.compare({"jobs_random"}, parity_seeds)
    print(f"smoke parity: {n} golden jobs, {steps} steps, {len(diff)} differ")
    return 1 if diff else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--wheel", action="store_true", help="build a wheel into dist/")
    ap.add_argument("--check", action="store_true", help="only check the importable module")
    a = ap.parse_args(argv)
    if not a.check:
        build(a.wheel)
        if a.wheel:
            return 0
        # a fresh interpreter: the rebuilt module is loaded at import
        return subprocess.run([sys.executable, __file__, "--check"]).returncode
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
