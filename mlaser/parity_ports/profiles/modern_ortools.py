"""``"modern-ortools"``: ORIGINAL with sort mode 20 ("shortest path", a rebuild extension) on
OR-Tools routing (``mlaser.cam.order_ortools``) instead of the built-in greedy + 2-opt + or-opt.
No slot changes: mode 20 has no original to be faithful to, so it is an extension option.
Needs the ``ortools`` extra; without it mode 20 falls back to the built-in solver."""
from __future__ import annotations

from ..profile import ORIGINAL, PROFILES, register_profile

MODERN_ORTOOLS = ORIGINAL.derive(
    "modern-ortools",
    "ORIGINAL plus sort mode 20 on OR-Tools routing (children-first precedence, small first)",
    options={"shortest_path": "ortools"},
)
if MODERN_ORTOOLS.name not in PROFILES:
    register_profile(MODERN_ORTOOLS)
