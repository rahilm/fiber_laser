"""``"original-reference"``: ORIGINAL with the statement-level reference of every kernel that
the default profile runs as a fast drop-in (the ordering drop-ins, and the C kernel: here the
emitter loop and the smoother are always the Python ports).

The drop-ins (``cadmodule.ordering_fast``) give the reference's output bit for bit; this profile
keeps the slow, literal ports reachable so tests can compare them (tests/test_ordering_fast.py)
and a doubtful result can be re-run on the code that was checked against the original.
"""
from __future__ import annotations

from ..profile import ORIGINAL, PROFILES, PYTHON_SAMPLING, PYTHON_SMOOTHER, register_profile

__port__ = {
    "source": "CADModule",
    "functions": {
        "0x1003d300": "cadmodule.ordering.local_optimal_elems (reference)",
        "0x1003c480": "cadmodule.ordering.containers_last_faithful (reference)",
        "FUN_100b57c0": "cadmodule.ordering.import_sort (reference)",
    },
    "note": "profile module: no code of its own, it points the ordering slot at the references",
}

_PP = "mlaser.parity_ports"

REFERENCE = ORIGINAL.derive(
    "original-reference",
    "ORIGINAL with the statement-level reference kernels instead of the fast drop-ins",
    ordering=ORIGINAL.ordering.with_(
        import_sort=f"{_PP}.cadmodule.ordering:import_sort",
        local_optimal_elems=f"{_PP}.cadmodule.ordering:local_optimal_elems",
        containers_last_faithful=f"{_PP}.cadmodule.ordering:containers_last_faithful",
        ring_sort=f"{_PP}.cadmodule.ordering:ring_sort",
    ),
    sampling=PYTHON_SAMPLING,
    smoother=PYTHON_SMOOTHER,
)
if REFERENCE.name not in PROFILES:
    register_profile(REFERENCE)
