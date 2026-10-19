"""The EXAMPLE technology set seeded into a new technology store.

These records are a starting point for the operator's own tuning, NOT tested cutting data:
every record has ``origin = "example"``, ``verified = False`` and the note
``"EXAMPLE VALUES: verify on your machine before cutting."``, and the UI marks them as examples.
They are in the range of published starting tables for a 1.5 kW fiber source (mild steel with
oxygen, stainless steel and aluminium with nitrogen) and were not measured on any machine.

Units are the registry's: speed mm/s, power (peak current) and duty %, frequency Hz, gas
pressure bar (UN.GasPressureUnit default), heights and focus mm, pierce time ms.  The densities
are the usual handbook values of the materials (g/cm³), used for the material cost estimate.
"""

from __future__ import annotations

from .technology import EXAMPLE_NOTE, TechKey, Technology

GAS_INDEX = {"O2": 1, "N2": 2, "Air": 0}

# material, density, gas, nozzle, rows of (thickness mm, speed mm/s, peak %, pressure bar,
# cut height mm, focus mm, pierce stages [(height mm, peak %, duty %, freq Hz, time ms)])
_TABLE = (
    ("Mild steel S235", 7.85, "O2", "1.0 double", (
        (1.0, 100.0, 60, 1.0, 1.0, 1.0, [(3.0, 50, 50, 1000, 200)]),
        (3.0, 45.0, 100, 0.8, 1.0, 2.0, [(4.0, 70, 60, 1000, 400)]),
        (6.0, 22.0, 100, 0.6, 1.0, 3.0, [(6.0, 80, 60, 500, 800), (3.0, 100, 80, 1000, 400)]),
    )),
    ("Stainless steel 1.4301", 7.93, "N2", "1.5 single", (
        (1.0, 300.0, 100, 10.0, 0.8, -1.0, [(2.0, 80, 100, 5000, 100)]),
        (2.0, 100.0, 100, 12.0, 0.8, -2.0, [(3.0, 100, 100, 5000, 200)]),
        (3.0, 40.0, 100, 14.0, 0.8, -3.0, [(4.0, 100, 80, 3000, 400)]),
    )),
    ("Aluminium AlMg3", 2.66, "N2", "1.5 single", (
        (1.0, 220.0, 100, 10.0, 0.8, -1.0, [(2.0, 80, 100, 5000, 100)]),
        (2.0, 70.0, 100, 12.0, 0.8, -2.0, [(3.0, 100, 100, 5000, 300)]),
        (3.0, 25.0, 100, 14.0, 0.8, -3.0, [(4.0, 100, 80, 3000, 600)]),
    )),
)


def examples() -> list[Technology]:
    out = []
    for material, density, gas, nozzle, rows in _TABLE:
        for thick, speed, peak, bar, height, focus, stages in rows:
            p: dict = {
                "GP.CutSpeed": speed, "GP.CutPeakCurrent": peak, "GP.CutPower": 100, "GP.CutFreq": 5000,
                "GP.CutGasType": GAS_INDEX[gas], "GP.CutAirPressure": bar, "GP.CutHeight": height,
                "GP.CutFocusPos": focus,
                "GP.ManuType": {1: 2, 2: 3}[len(stages)],
            }
            # stage 0 is the last one before cutting (LayerView / FUN_00464e10 runs n-1 .. 0)
            for s, (h, pk, duty, freq, ms) in enumerate(reversed(stages)):
                p.update({f"GP.DrillHeight{s}": h, f"GP.DrillPeakCurrent{s}": pk, f"GP.DrillPower{s}": duty,
                          f"GP.DrillFreq{s}": freq, f"GP.DrillDelay{s}": ms, f"GP.DrillGasType{s}": GAS_INDEX[gas],
                          f"GP.DrillGasPressure{s}": bar, f"GP.DrillFocusPos{s}": focus})
            kerf = round(0.1 + 0.02 * thick, 2)
            craft = {
                "lead_in": {"kind": 1, "angle_deg": 90.0, "length": round(2.0 + thick / 2, 1), "radius": 1.0,
                            "at_corner": False},
                "lead_out": {"kind": 0, "angle_deg": 90.0, "length": 0.0, "radius": 0.0, "at_corner": False},
                "kerf_comp": 1, "kerf_width": kerf,
                "micro_joint": {"kind": 0, "count": 0, "distance": 100.0, "length": round(0.3 + 0.1 * thick, 1),
                                "min_size": 0.0},
            }
            rules = {"small_feature": {"max_size_mm": round(max(3.0, 2 * thick), 1), "lead_kind": 1,
                                       "lead_length_mm": round(max(0.5, thick / 2), 1), "no_joints": True},
                     "corner": {"cool_points": thick >= 6.0, "max_angle_deg": 60.0}}
            out.append(Technology(TechKey(material, thick, gas, nozzle, 1500), p, craft, rules,
                                  None, density, EXAMPLE_NOTE, "example", False))
    return out
