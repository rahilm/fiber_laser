"""Time and cost card of a planned job: clearly labelled estimates from configurable rates.

Inputs are the :class:`mlaser.services.preview.TimeStats` of the PlannedJob that Start streams
(times and lengths from its wire), the layers it was compiled with (power %, gas, pressure, and
the technology record of each layer when one was applied) and :class:`Rates`.

* **Gas** per layer: the record's ``gas_flow_l_min`` when known; otherwise, when the nozzle
  diameter is known (the first number of the technology's nozzle name, mm) and the pressure is
  set, an orifice-flow estimate (``orifice_flow_l_min``, ideal gas through a sharp nozzle with
  discharge coefficient ``Rates.nozzle_cd``); otherwise the line asks for the flow
  (``missing``).  Gas time = the cut blocks plus the gas delay / pierce time before each.
* **Energy**: laser electrical power = rated power x power % / wall-plug efficiency while the
  beam is on, plus the machine's own draw (drives, chiller, extraction) for the whole job.
* **Cost** = machine rate x job time + gas + energy + material.  A rate of 0 is "not set" and the
  line says so instead of showing 0.

Every number is an estimate and is labelled as one; nothing here is measured.  Qt-free.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# gas properties at 20 °C: (gamma, specific gas constant J/(kg K), density at 1.01325 bar kg/m³)
GAS_PROPS = {"N2": (1.40, 296.8, 1.165), "O2": (1.40, 259.8, 1.331), "Air": (1.40, 287.0, 1.204)}
P_ATM_BAR = 1.01325
T_GAS_K = 293.15
GAS_KIND = {0: "Air", 1: "O2", 2: "N2", 3: "Air", 4: "O2", 5: "N2"}   # GP.CutGasType -> gas


@dataclass
class Rates:
    """Cost rates and assumptions (plugin settings of the Process workspace)."""

    currency: str = "€"
    machine_per_h: float = 0.0          # machine hour rate (operator, depreciation...)
    power_per_kwh: float = 0.0          # electricity
    gas_per_m3: dict = field(default_factory=lambda: {"O2": 0.0, "N2": 0.0, "Air": 0.0})  # at 1 atm
    material_per_kg: float = 0.0
    laser_rated_w: float = 0.0          # laser source rated optical power; 0 = from the technology
    wall_plug_eff: float = 0.35         # assumption: fiber source electrical efficiency (typ. 0.3-0.5)
    machine_kw: float = 0.0             # machine draw besides the source (drives, chiller, fan)
    nozzle_cd: float = 0.8              # assumption: discharge coefficient of the nozzle bore
    gas_flow_l_min: Optional[float] = None   # user's flow for layers without a known flow
    sheet_w_mm: float = 0.0             # 0 = the bounding box of the cut parts
    sheet_h_mm: float = 0.0


#: the ``costing.*`` keys of the cost card's rates (Settings › Costing)
CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "¥": "JPY", "₹": "INR", "zł": "PLN", "₺": "TRY"}


def iso_currency(text: Any) -> str:
    """A currency as its ISO 4217 code ("€" -> "EUR"); unknown text is kept."""
    t = str(text or "").strip()
    return CURRENCY_SYMBOLS.get(t, t.upper() if len(t) == 3 and t.isalpha() else t)


def rates_from_config(cfg: Any) -> Rates:
    """The cost card's :class:`Rates` from the ConfigService (``costing.*``).  The sheet size
    comes from the job (the cut bounding box) and the gas flow from the technology."""
    def g(key: str, default: float = 0.0) -> float:
        try:
            return float(cfg.get(key, default))
        except (TypeError, ValueError):
            return default
    return Rates(currency=iso_currency(cfg.get("costing.currency", "EUR")) or "EUR",
                 machine_per_h=g("costing.machine_per_h"), power_per_kwh=g("costing.power_per_kwh"),
                 gas_per_m3={"O2": g("costing.gas_o2_per_m3"), "N2": g("costing.gas_n2_per_m3"),
                             "Air": g("costing.gas_air_per_m3")},
                 material_per_kg=g("costing.material_per_kg"), laser_rated_w=g("costing.laser_rated_w"),
                 wall_plug_eff=g("costing.wall_plug_pct", 35.0) / 100.0, machine_kw=g("costing.machine_kw"),
                 nozzle_cd=g("costing.nozzle_cd", 0.8))


@dataclass(frozen=True)
class Line:
    """One line of the card.  ``key`` is the catalogue id suffix (``cost.line.<key>``).
    ``kind``: ``measured`` (from the planned job), ``estimate`` (computed with an assumption,
    see ``note``) or ``missing`` (input needed: ``note`` says which)."""

    key: str
    value: Optional[float]
    unit: str
    kind: str = "estimate"
    note: str = ""              # catalogue id of the explanation (``cost.note.*``), "" = none
    args: tuple = ()            # (name, value) pairs for the note's placeholders


@dataclass
class CostCard:
    lines: list[Line]
    currency: str

    def get(self, key: str) -> Optional[Line]:
        return next((ln for ln in self.lines if ln.key == key), None)

    def value(self, key: str) -> Optional[float]:
        ln = self.get(key)
        return None if ln is None else ln.value

    @property
    def missing(self) -> list[Line]:
        return [ln for ln in self.lines if ln.kind == "missing"]


def orifice_flow_l_min(gauge_bar: float, diameter_mm: float, gas: str = "N2", cd: float = 0.8) -> float:
    """Gas flow through a nozzle bore in normal litres per minute (1.01325 bar, 20 °C): ideal-gas
    orifice flow, choked when the pressure ratio exceeds the critical one."""
    g, R, rho_n = GAS_PROPS.get(gas, GAS_PROPS["Air"])
    p0 = (max(0.0, gauge_bar) + P_ATM_BAR) * 1e5
    pa = P_ATM_BAR * 1e5
    A = math.pi * (diameter_mm * 1e-3) ** 2 / 4
    crit = (2 / (g + 1)) ** (g / (g - 1))
    if pa / p0 <= crit:                                     # choked (sonic) flow
        mdot = cd * A * p0 * math.sqrt(g / (R * T_GAS_K)) * (2 / (g + 1)) ** ((g + 1) / (2 * (g - 1)))
    else:
        r = pa / p0
        mdot = cd * A * p0 * math.sqrt(2 * g / ((g - 1) * R * T_GAS_K) * (r ** (2 / g) - r ** ((g + 1) / g)))
    return mdot / rho_n * 1000.0 * 60.0


def nozzle_diameter(name: str) -> Optional[float]:
    m = re.search(r"(\d+(?:[.,]\d+)?)", name or "")
    return float(m.group(1).replace(",", ".")) if m else None


@dataclass(frozen=True)
class LayerInfo:
    """What the card needs of one layer."""

    layer: int
    power_pct: float            # peak current % (GP.CutPeakCurrent)
    gas: str                    # O2 / N2 / Air
    pressure_bar: float
    flow_l_min: Optional[float] = None
    nozzle: str = ""
    power_w: float = 0.0        # technology power class
    density_g_cm3: Optional[float] = None
    thickness_mm: float = 0.0
    material: str = ""


def layer_info(doc: Any, layer: int, pressure_unit: int = 0) -> LayerInfo:
    """LayerInfo from a document layer (technology reference when one was applied).
    ``pressure_unit``: UN.GasPressureUnit (0 bar, 1 MPa)."""
    from .technology import layer_values

    lp = doc.layers.get(layer) if doc is not None else None
    v = layer_values(lp)
    ref = ((lp.extra or {}).get("technology") if lp is not None else None) or {}
    key = ref.get("key") or {}
    p = float(v.get("GP.CutAirPressure", 0.0) or 0.0) * (10.0 if pressure_unit == 1 else 1.0)
    return LayerInfo(layer, float(v.get("GP.CutPeakCurrent", 100) or 0), GAS_KIND.get(int(v.get("GP.CutGasType", 0) or 0), "Air"),
                     p, ref.get("gas_flow_l_min"), str(key.get("nozzle", "")), float(key.get("power_w", 0) or 0),
                     ref.get("density_g_cm3"), float(key.get("thickness_mm", 0.0) or 0.0), str(key.get("material", "")))


def _units() -> Any:
    from . import units
    return units.current()


def _sheet_size(w: float, h: float) -> str:
    u = _units()
    dec = 0 if u.unit("length") == "mm" else 2
    return u.fmt_size(w, h, decimals=dec, trim=dec > 0)


def cost_card(stats: Any, layers: dict[int, LayerInfo], rates: Rates, pierces: int = 0,
              cut_bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)) -> CostCard:
    """The card for one planned job (see the module docstring)."""
    cur = rates.currency
    L: list[Line] = [
        Line("time.total", stats.total_s, "s", "measured", "cost.note.planned"),
        Line("time.cut", stats.cut_s, "s", "measured"),
        Line("time.travel", stats.travel_s, "s", "measured"),
        Line("time.pierce", stats.process_s + stats.dwell_s, "s", "measured", "cost.note.pierce"),
        Line("count.pierces", float(pierces), "", "measured"),
        Line("length.cut", stats.cut_length_mm / 1000.0, "m", "measured"),
        Line("length.travel", stats.travel_length_mm / 1000.0, "m", "measured"),
    ]
    # ---- gas
    gas_m3: dict[str, float] = {}
    gas_missing = []
    gas_notes = set()
    gas_args: tuple = ()
    by_layer = dict(stats.gas_by_layer or {})
    for ly, secs in sorted(by_layer.items()):
        info = layers.get(ly)
        if info is None or secs <= 0:
            continue
        flow = info.flow_l_min
        if flow is None and rates.gas_flow_l_min:
            flow, note = float(rates.gas_flow_l_min), "cost.note.gas_settings"
        elif flow is None:
            d = nozzle_diameter(info.nozzle)
            if d and info.pressure_bar > 0:
                flow = orifice_flow_l_min(info.pressure_bar, d, info.gas, rates.nozzle_cd)
                note = "cost.note.gas_orifice"
                # note arguments are display text (mlaser.services.units): never read back
                gas_args = (("nozzle", _units().fmt(d, "length", trim=True)), ("cd", f"{rates.nozzle_cd:g}"))
            else:
                gas_missing.append(ly)
                continue
        else:
            note = "cost.note.gas_technology"
        gas_notes.add(note)
        gas_m3[info.gas] = gas_m3.get(info.gas, 0.0) + flow * secs / 60.0 / 1000.0
    if gas_missing:
        L.append(Line("gas.volume", sum(gas_m3.values()) or None, "m³", "missing", "cost.note.gas_missing",
                      (("layers", ", ".join(map(str, gas_missing))),)))
    else:
        note = sorted(gas_notes)[0] if len(gas_notes) == 1 else ("cost.note.gas_mixed" if gas_notes else "")
        L.append(Line("gas.volume", sum(gas_m3.values()), "m³", "estimate", note,
                      gas_args if note == "cost.note.gas_orifice" else ()))
    # ---- energy
    laser_kwh = 0.0
    laser_missing = []
    for ly, secs in sorted((stats.laser_on_by_layer or {}).items()):
        info = layers.get(ly)
        rated = rates.laser_rated_w or (info.power_w if info else 0.0)
        if not rated:
            laser_missing.append(ly)
            continue
        pct = (info.power_pct if info else 100.0) / 100.0
        laser_kwh += rated * pct / max(rates.wall_plug_eff, 1e-3) * secs / 3.6e6
    machine_kwh = rates.machine_kw * stats.total_s / 3600.0
    if laser_missing:
        L.append(Line("energy.laser", laser_kwh or None, "kWh", "missing", "cost.note.laser_missing"))
    else:
        L.append(Line("energy.laser", laser_kwh, "kWh", "estimate", "cost.note.laser",
                      (("eff", f"{rates.wall_plug_eff * 100:.0f}"),)))
    L.append(Line("energy.machine", machine_kwh, "kWh", "estimate" if rates.machine_kw else "missing",
                  "cost.note.machine_kw" if rates.machine_kw else "cost.note.machine_kw_missing"))
    energy = laser_kwh + machine_kwh
    L.append(Line("energy.total", energy, "kWh", "estimate"))
    peak_kw = 0.0
    for info in layers.values():
        rated = rates.laser_rated_w or info.power_w
        peak_kw = max(peak_kw, rated * info.power_pct / 100.0 / max(rates.wall_plug_eff, 1e-3) / 1000.0)
    L.append(Line("power.peak", peak_kw + rates.machine_kw, "kW", "estimate", "cost.note.peak"))
    # ---- material
    x0, y0, x1, y1 = cut_bbox
    w = rates.sheet_w_mm or max(0.0, x1 - x0)
    h = rates.sheet_h_mm or max(0.0, y1 - y0)
    infos = [i for i in layers.values() if i.density_g_cm3 and i.thickness_mm]
    mass = None
    if infos:
        i0 = infos[0]
        mass = w * h * i0.thickness_mm * 1e-3 * float(i0.density_g_cm3 or 0.0) * 1e-3  # mm³ -> cm³ -> kg
        L.append(Line("material.mass", mass, "kg", "estimate",
                      "cost.note.sheet" if rates.sheet_w_mm else "cost.note.sheet_bbox",
                      (("size", _sheet_size(w, h)), ("t", _units().fmt(i0.thickness_mm, "length", trim=True)),
                       ("density", _units().fmt(float(i0.density_g_cm3 or 0.0), "density", trim=True)))))
    else:
        L.append(Line("material.mass", None, "kg", "missing", "cost.note.material_missing"))
    # ---- cost
    def money(key: str, amount: Optional[float], rate: float) -> float:
        if amount is None:
            L.append(Line(key, None, cur, "missing", "cost.note.input_missing"))
            return 0.0
        if not rate:
            L.append(Line(key, None, cur, "missing", "cost.note.rate_missing"))
            return 0.0
        L.append(Line(key, amount * rate, cur, "estimate"))
        return amount * rate

    total = money("cost.machine", stats.total_s / 3600.0, rates.machine_per_h)
    gas_cost = 0.0
    gas_rate_missing = []
    for g, m3 in gas_m3.items():
        r = float((rates.gas_per_m3 or {}).get(g, 0.0) or 0.0)
        if not r:
            gas_rate_missing.append(g)
        gas_cost += m3 * r
    if gas_missing or (gas_rate_missing and gas_m3):
        if gas_rate_missing:
            L.append(Line("cost.gas", gas_cost or None, cur, "missing", "cost.note.gas_rate_missing",
                          (("gas", ", ".join(gas_rate_missing)),)))
        else:
            L.append(Line("cost.gas", gas_cost or None, cur, "missing", "cost.note.input_missing"))
    else:
        L.append(Line("cost.gas", gas_cost, cur, "estimate"))
    total += gas_cost
    total += money("cost.energy", energy if not laser_missing else None, rates.power_per_kwh)
    total += money("cost.material", mass, rates.material_per_kg)
    incomplete = any(ln.kind == "missing" for ln in L if ln.key.startswith("cost."))
    L.append(Line("cost.total", total, cur, "missing" if incomplete else "estimate",
                  "cost.note.incomplete" if incomplete else ""))
    return CostCard(L, cur)


def card_for_plan(plan: Any, timeline: Any, rates: Rates, pressure_unit: int = 0) -> CostCard:
    """The card of a PlannedJob and its :class:`mlaser.services.preview.Timeline`."""
    st = timeline.stats()
    used = {b[2] for b in timeline.blocks} or set(getattr(plan.document, "layers", {}) or {})
    infos = {ly: layer_info(plan.document, ly, pressure_unit) for ly in used}
    return cost_card(st, infos, rates, int(plan.job.pierces), tuple(plan.cut_bounds))
