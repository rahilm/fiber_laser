"""``@port`` markers: which original function a symbol reproduces bit for bit.

A port is code whose output is pinned to the original program (Wine parity harness, docs/parity.md)
and to the goldens (tests/golden/).  It changes only together with a parity re-run; new
behaviour goes into a new :class:`~mlaser.parity_ports.profile.ParityProfile` instead.

Two kinds of record:

* ``@port("FUN_1003f010", source="CADModule")`` on a class or function: the symbol is an entry
  point of a port and is reachable through the default profile (tests/test_parity_ports.py checks
  that).  The decorator returns the object unchanged apart from a ``__port__`` attribute.
* a module-level ``__port__`` dict on every port module: the original functions the module covers,
  helpers included, ``{"source": ..., "functions": {"FUN_xxx": "python_name", ...}}``.

This module imports nothing from mlaser, so foundation code could use it too.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, TypeVar

T = TypeVar("T")

#: binaries the ports come from (Mlaser v0.0.1.51_SP1, docs/parity.md §2)
SOURCES = ("CADModule", "MotionCtrl", "MainApp", "NCModule", "CircleFitDLL", "DxfParse",
           "splineAnalyer", "msvcr100")
STATUSES = ("verified", "observed", "inferred")


@dataclass(frozen=True)
class PortRecord:
    """One ported entry point."""
    module: str
    qualname: str
    functions: tuple[str, ...]          # original function addresses (FUN_xxxxxxxx / 0x...)
    source: str                         # binary, one of SOURCES
    status: str = "verified"            # verified (parity harness) / observed (decomp) / inferred
    parity: tuple[str, ...] = field(default=())   # parity areas or case ids that check it
    note: str = ""

    @property
    def name(self) -> str:
        return f"{self.module}.{self.qualname}"


_REGISTRY: list[PortRecord] = []


def port(*functions: str, source: str, status: str = "verified", parity: tuple[str, ...] = (),
         note: str = "") -> Callable[[T], T]:
    """Mark a class or function as a bit-exact port of ``functions`` in ``source``."""
    if source not in SOURCES:
        raise ValueError(f"unknown port source {source!r} (one of {SOURCES})")
    if status not in STATUSES:
        raise ValueError(f"unknown port status {status!r} (one of {STATUSES})")

    def mark(obj: T) -> T:
        rec = PortRecord(getattr(obj, "__module__", "?"), getattr(obj, "__qualname__", repr(obj)),
                         tuple(functions), source, status, tuple(parity), note)
        obj.__port__ = rec  # type: ignore[attr-defined]
        _REGISTRY.append(rec)
        return obj

    return mark


def registered() -> list[PortRecord]:
    """Every ``@port`` record of the port modules imported so far."""
    return list(_REGISTRY)
