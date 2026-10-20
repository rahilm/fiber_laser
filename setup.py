"""setuptools build of mlaser's optional C extension ``mlaser._native._emitter``.

Everything else is declared in pyproject.toml; this file only adds the extension, because its
compiler flags are part of the bit-identity contract with the Python port
(docs/dev/NATIVE_KERNEL.md, "Building"):

* gcc / clang: ``-O2 -ffp-contract=off -fno-fast-math``, no ``-march``: no fused multiply-add
  contraction (GCC contracts by default in GNU C mode, clang on AArch64 / Apple silicon), no
  value-changing optimisations;
* MSVC: ``/O2 /fp:precise`` (no ``/fp:fast``, no ``/fp:contract``); the source also carries
  ``#pragma fp_contract(off)``.

``MLASER_BUILD_NATIVE`` selects what happens when the extension cannot be compiled:

* unset / ``auto``: the extension is optional; without a working compiler the build warns and
  produces mlaser without it (the Python port runs; ``MLASER_NATIVE`` then reports "python");
* ``1``: required, a compile error fails the build (CI, cibuildwheel, release builds);
* ``0``: not built at all.
"""

import os
import sys

from setuptools import Extension, setup

mode = os.environ.get("MLASER_BUILD_NATIVE", "auto").strip().lower()
if mode not in ("auto", "", "0", "1"):
    raise SystemExit(f"MLASER_BUILD_NATIVE={mode!r}: expected auto, 0 or 1")

if sys.platform == "win32" and "GCC" not in sys.version:
    # MSVC (or clang-cl): /fp:precise is the default and does not contract a*b+c without /fp:contract
    cflags = ["/O2", "/fp:precise"]
else:
    # (32-bit x86 would also need -msse2 -mfpmath=sse against x87 excess precision; the source
    # refuses FLT_EVAL_METHOD != 0 with #error.  No 32-bit build is made.)
    cflags = ["-O2", "-ffp-contract=off", "-fno-fast-math"]

ext_modules = []
if mode != "0":
    ext_modules.append(
        Extension(
            "mlaser._native._emitter",
            # one module: the emitter loop and packed sink (_emitter.c), crtmath / CurveTable /
            # smoothed-path sampling (_geom.c), ring sort (_ring.c)
            ["mlaser/_native/_emitter.c", "mlaser/_native/_geom.c", "mlaser/_native/_ring.c"],
            depends=["mlaser/_native/_geom.h"],
            extra_compile_args=cflags,
            optional=mode != "1",
        )
    )

setup(ext_modules=ext_modules)
