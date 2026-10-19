"""Build of mlaser_native._emitter.  The flags are part of the parity contract: no FMA
contraction, no fast-math, no host-specific -march (docs/dev/NATIVE_KERNEL.md)."""
import sys

from setuptools import Extension, setup

if sys.platform == "win32":
    # MSVC: /fp:precise is the default and does not contract a*b+c unless /fp:contract is given
    # (VS 2022); the source also carries #pragma fp_contract(off)
    cflags = ["/O2", "/fp:precise"]
else:
    # (32-bit x86 would also need -msse2 -mfpmath=sse against x87 excess precision; not built)
    cflags = ["-O2", "-ffp-contract=off", "-fno-fast-math"]

setup(ext_modules=[Extension("mlaser_native._emitter", ["src/mlaser_native/_emitter.c"],
                             extra_compile_args=cflags)])
