"""Display units: metric or imperial, everywhere in the application (docs/user/units.md).

**Internal values never change.**  Documents, ConfigService values, the technology database, the
protocol, the planner, the goldens and the parity captures all stay in the internal units
(mm, mm/s, mm/s², bar, kg, l/min, g/cm³ ...).  This module only converts for *display and input*:
a widget asks :func:`current` for the :class:`Units` in force and calls :meth:`Units.fmt`,
:meth:`Units.to_display`, :meth:`Units.from_display` or :meth:`Units.parse`.

* **Settings** (:data:`UNITS_SETTINGS`, registered in the ConfigService schema): ``units.system``
  (``"metric"`` | ``"imperial"``, default metric) and optional per-family overrides
  (``units.length``, ``units.speed``, ``units.accel``, ``units.area``, ``units.mass``,
  ``units.pressure``, ``units.flow``) and ``units.decimals_<family>``; an empty override follows
  the system.
* **Kinds** (:data:`KINDS`) say what a value *is*: ``length``, ``small_length`` (kerf, offsets,
  tolerances: more decimals), ``long_length`` (cut length: m / ft), ``speed``, ``accel``,
  ``jerk``, ``area``, ``large_area`` (sheets: m² / ft²), ``volume``, ``flow``, ``mass``,
  ``density``, ``pressure``, ``time`` and ``angle`` (never converted), and the prices
  ``per_long_length``, ``per_large_area``, ``per_volume``, ``per_mass``.
* **Exact constants**: 1 in = 25.4 mm, 1 lb = 0.45359237 kg, 1 psi = 0.0689475729316836 bar
  (= 6894.757293168361 Pa, from the exact lbf and in definitions).
* **Input** (:meth:`Units.parse`): a number in the display unit, or a number with any unit of
  the kind's family: ``3.2 mm``, ``0.125 in``, ``1/8"``, ``1 1/2 in``, ``2'6"``, ``2 ft 6 in``,
  ``300 mm/min``, ``40 psi``.  A decimal comma is accepted (``3,2``).
* **Change listeners**: :func:`subscribe` (callbacks with the new :class:`Units`), fired when
  the bound ConfigService's ``units.*`` values change (:func:`bind`) or :func:`set_current`.

Qt-free; the widgets are in :mod:`mlaser.ui.units_qt`.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from mlaser.services.config import Setting

MM_PER_IN = 25.4
MM_PER_FT = 304.8                     # 12 in (exact; 12 * 25.4 rounds to 304.79999999999995)
KG_PER_LB = 0.45359237
G_STANDARD = 9806.65                  # mm/s²
L_PER_FT3 = 28.316846592                # exact (0.3048 m)³
BAR_PER_PSI = KG_PER_LB * G_STANDARD / 1e3 / 645.16 * 10.0   # lbf / in²: 0.0689475729316836

SYSTEMS = ("metric", "imperial")

#: family -> {unit name: factor to the family's base unit}.  Every conversion is linear.
UNIT_TABLE: dict[str, dict[str, float]] = {
    "length": {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": MM_PER_IN, "ft": MM_PER_FT, "µm": 1e-3},
    "speed": {"mm/s": 1.0, "mm/min": 1 / 60, "m/min": 1000 / 60, "m/s": 1000.0,
              "in/s": MM_PER_IN, "in/min": MM_PER_IN / 60, "ft/min": MM_PER_FT / 60},
    "accel": {"mm/s²": 1.0, "m/s²": 1000.0, "in/s²": MM_PER_IN, "ft/s²": MM_PER_FT, "g": G_STANDARD},
    "jerk": {"mm/s³": 1.0, "m/s³": 1000.0, "in/s³": MM_PER_IN, "ft/s³": MM_PER_FT},
    "area": {"mm²": 1.0, "cm²": 100.0, "m²": 1e6, "in²": 645.16, "ft²": 92903.04},
    "volume": {"l": 1.0, "m³": 1000.0, "cm³": 1e-3, "ft³": L_PER_FT3, "in³": 0.016387064},
    "flow": {"l/min": 1.0, "l/h": 1 / 60, "m³/h": 1000 / 60, "ft³/h": L_PER_FT3 / 60, "ft³/min": L_PER_FT3},
    "mass": {"kg": 1.0, "g": 1e-3, "t": 1000.0, "lb": KG_PER_LB, "oz": KG_PER_LB / 16},
    "density": {"g/cm³": 1.0, "kg/m³": 1e-3, "lb/in³": KG_PER_LB * 1e3 / (MM_PER_IN ** 3 / 1e3),
                "lb/ft³": KG_PER_LB * 1e3 / (MM_PER_FT ** 3 / 1e3)},
    "pressure": {"bar": 1.0, "MPa": 10.0, "kPa": 0.01, "psi": BAR_PER_PSI},
    "time": {"s": 1.0, "ms": 1e-3, "min": 60.0, "h": 3600.0},
    "angle": {"°": 1.0},
}

#: spellings accepted on input (lower-case key -> canonical unit name)
ALIASES: dict[str, str] = {
    '"': "in", "″": "in", "in.": "in", "inch": "in", "inches": "in", "'": "ft", "′": "ft", "feet": "ft",
    "foot": "ft", "ipm": "in/min", "ips": "in/s", "in/sec": "in/s", "mm/sec": "mm/s", "fpm": "ft/min",
    "sfm": "ft/min", "mm/s2": "mm/s²", "mm/s^2": "mm/s²", "mm / s2": "mm/s²", "mm / s": "mm/s",
    "in/s2": "in/s²", "in/s^2": "in/s²", "m/s2": "m/s²", "m/s^2": "m/s²", "mm/s3": "mm/s³", "in/s3": "in/s³",
    "mm2": "mm²", "mm^2": "mm²", "m2": "m²", "m^2": "m²", "in2": "in²", "in^2": "in²", "sq in": "in²",
    "sqin": "in²", "ft2": "ft²", "ft^2": "ft²", "sq ft": "ft²", "sqft": "ft²", "cm2": "cm²",
    "liter": "l", "litre": "l", "liters": "l", "litres": "l", "l": "l", "m3": "m³", "ft3": "ft³",
    "cu ft": "ft³", "cf": "ft³", "in3": "in³", "cm3": "cm³", "cc": "cm³", "l/min": "l/min", "lpm": "l/min",
    "scfh": "ft³/h", "cfh": "ft³/h", "ft3/h": "ft³/h", "scfm": "ft³/min", "cfm": "ft³/min",
    "ft3/min": "ft³/min", "m3/h": "m³/h", "lbs": "lb", "pound": "lb", "pounds": "lb", "kgs": "kg",
    "g/cm3": "g/cm³", "g/cc": "g/cm³", "kg/m3": "kg/m³", "lb/in3": "lb/in³", "lb/ft3": "lb/ft³",
    "mpa": "MPa", "kpa": "kPa", "psig": "psi", "deg": "°", "um": "µm", "sec": "s", "msec": "ms",
}

#: the display unit of a family per system
SYSTEM_UNITS: dict[str, dict[str, str]] = {
    "metric": {"length": "mm", "speed": "mm/s", "accel": "mm/s²", "jerk": "mm/s³", "area": "mm²",
               "volume": "l", "flow": "l/min", "mass": "kg", "density": "g/cm³", "pressure": "bar",
               "time": "s", "angle": "°"},
    "imperial": {"length": "in", "speed": "in/min", "accel": "in/s²", "jerk": "in/s³", "area": "in²",
                 "volume": "ft³", "flow": "ft³/h", "mass": "lb", "density": "lb/in³", "pressure": "psi",
                 "time": "s", "angle": "°"},
}


@dataclass(frozen=True)
class Kind:
    """What a quantity is: its family, the internal unit the stored value uses, the display
    unit per system (None = the family's system unit, or its override), and the decimals and
    spin step per display unit (``dec`` / ``step``: unit -> value; ``"*"`` the fallback)."""

    name: str
    family: str
    internal: str
    metric: Optional[str] = None
    imperial: Optional[str] = None
    dec: dict = field(default_factory=dict)
    step: dict = field(default_factory=dict)
    inverse: bool = False          # a price per unit (per m -> per ft: the factor inverts)
    override: bool = True          # follows units.<family>


def _k(name, family, internal, metric=None, imperial=None, dec=None, step=None, **kw) -> Kind:
    return Kind(name, family, internal, metric, imperial, dict(dec or {}), dict(step or {}), **kw)


KINDS: dict[str, Kind] = {k.name: k for k in (
    _k("length", "length", "mm", dec={"mm": 2, "cm": 3, "m": 4, "in": 3, "ft": 4, "*": 3},
       step={"mm": 1.0, "cm": 0.1, "m": 0.01, "in": 0.05, "ft": 0.01, "*": 0.1}),
    _k("small_length", "length", "mm", dec={"mm": 3, "in": 4, "*": 4},
       step={"mm": 0.01, "in": 0.001, "*": 0.001}),
    _k("long_length", "length", "mm", "m", "ft", dec={"m": 2, "ft": 1, "*": 2}, step={"*": 1.0}, override=False),
    _k("speed", "speed", "mm/s", dec={"mm/s": 1, "mm/min": 0, "m/min": 2, "m/s": 3, "in/s": 2, "in/min": 1,
                                      "ft/min": 1, "*": 2},
       step={"mm/s": 1.0, "mm/min": 60.0, "m/min": 0.1, "m/s": 0.01, "in/s": 0.1, "in/min": 1.0,
             "ft/min": 1.0, "*": 1.0}),
    _k("accel", "accel", "mm/s²", dec={"mm/s²": 0, "m/s²": 3, "in/s²": 1, "ft/s²": 2, "g": 3, "*": 2},
       step={"mm/s²": 100.0, "m/s²": 0.1, "in/s²": 10.0, "ft/s²": 1.0, "g": 0.01, "*": 1.0}),
    _k("jerk", "jerk", "mm/s³", dec={"mm/s³": 0, "in/s³": 0, "*": 1}, step={"mm/s³": 1000.0, "in/s³": 50.0, "*": 1.0}),
    _k("area", "area", "mm²", dec={"mm²": 0, "cm²": 1, "m²": 4, "in²": 2, "ft²": 3, "*": 2},
       step={"mm²": 100.0, "cm²": 1.0, "m²": 0.01, "in²": 1.0, "ft²": 0.1, "*": 1.0}),
    _k("large_area", "area", "mm²", "m²", "ft²", dec={"m²": 2, "ft²": 1, "*": 2}, step={"*": 1.0}),
    _k("volume", "volume", "l", dec={"l": 1, "m³": 3, "ft³": 2, "*": 2}, step={"*": 1.0}),
    _k("flow", "flow", "l/min", dec={"l/min": 1, "m³/h": 2, "ft³/h": 1, "ft³/min": 2, "*": 2},
       step={"l/min": 1.0, "ft³/h": 1.0, "*": 0.1}),
    _k("mass", "mass", "kg", dec={"kg": 2, "g": 0, "lb": 2, "*": 2}, step={"*": 1.0}),
    _k("density", "density", "g/cm³", dec={"g/cm³": 3, "lb/in³": 4, "kg/m³": 0, "lb/ft³": 1, "*": 4},
       step={"g/cm³": 0.01, "lb/in³": 0.001, "kg/m³": 10.0, "lb/ft³": 1.0, "*": 0.01}),
    _k("pressure", "pressure", "bar", dec={"bar": 2, "MPa": 3, "kPa": 0, "psi": 1, "*": 2},
       step={"bar": 0.1, "MPa": 0.01, "kPa": 10.0, "psi": 1.0, "*": 0.1}),
    _k("time", "time", "s", dec={"*": 2}, step={"*": 0.1}, override=False),
    _k("angle", "angle", "°", dec={"*": 2}, step={"*": 1.0}, override=False),
    # prices per unit: the stored value is per internal-ish unit, the display per display unit
    _k("per_long_length", "length", "m", "m", "ft", dec={"*": 3}, step={"*": 0.1}, inverse=True, override=False),
    _k("per_large_area", "area", "m²", "m²", "ft²", dec={"*": 3}, step={"*": 0.1}, inverse=True, override=False),
    _k("per_volume", "volume", "m³", "m³", "ft³", dec={"*": 3}, step={"*": 0.1}, inverse=True, override=False),
    _k("per_mass", "mass", "kg", "kg", "lb", dec={"*": 3}, step={"*": 0.1}, inverse=True, override=False),
)}

#: families with a ``units.<family>`` override setting (the brief's list, plus flow)
OVERRIDE_FAMILIES = ("length", "speed", "accel", "area", "mass", "pressure", "flow")

#: registry / setting unit strings -> kind (:func:`kind_for_unit`)
_UNIT_KIND = {
    "mm": "length", "mm/s": "speed", "mm / s": "speed", "mm/min": "speed", "m/min": "speed",
    "mm/s2": "accel", "mm / s2": "accel", "mm/s²": "accel", "mm/s^2": "accel", "mm/s3": "jerk", "mm/s³": "jerk",
    "mm²": "area", "mm2": "area", "m²": "large_area", "m2": "large_area", "bar": "pressure", "MPa": "pressure",
    "l/min": "flow", "kg": "mass", "g/cm³": "density", "°": "angle", "s": "time",
    # prices (a rate per unit: per m, per m², per m³, per kg)
    "/m": "per_long_length", "/m²": "per_large_area", "/m2": "per_large_area", "/m³": "per_volume",
    "/kg": "per_mass",
}


def kind_for_unit(unit: str) -> Optional[str]:
    """The kind for a schema / registry unit string ("mm", "mm / s", "mm/s2", "bar" ...), or None
    when the value is not converted (%, Hz, W, ms, p/mm, V ...)."""
    return _UNIT_KIND.get((unit or "").strip())


def _setting(key: str, default: Any = "", options: tuple = (), label: str = "") -> Setting:
    return Setting(key, "str", default, options=options, label=label or f"units.set.{key.split('.', 1)[1]}",
                   section="units", source="app")


#: the ``units.*`` block of the ConfigService schema
UNITS_SETTINGS: tuple[Setting, ...] = (
    _setting("units.system", "metric", SYSTEMS),
    _setting("units.length", "", ("", "mm", "cm", "m", "in", "ft")),
    _setting("units.speed", "", ("", "mm/s", "mm/min", "m/min", "in/s", "in/min", "ipm")),
    _setting("units.accel", "", ("", "mm/s²", "m/s²", "in/s²", "g")),
    _setting("units.area", "", ("", "mm²", "cm²", "m²", "in²", "ft²")),
    _setting("units.mass", "", ("", "kg", "lb")),
    _setting("units.pressure", "", ("", "bar", "MPa", "kPa", "psi")),
    _setting("units.flow", "", ("", "l/min", "m³/h", "ft³/h", "ft³/min")),
) + tuple(_setting(f"units.decimals_{f}", "", ("",) + tuple(str(i) for i in range(7)))
          for f in ("length", "speed", "accel", "area", "pressure"))


def canonical(unit: str) -> str:
    """The canonical spelling of a unit name ("ipm" -> "in/min", '"' -> "in")."""
    u = (unit or "").strip()
    for fam in UNIT_TABLE.values():
        if u in fam:
            return u
    return ALIASES.get(u.lower(), ALIASES.get(u, u))


def _family_of(unit: str) -> Optional[str]:
    for f, tab in UNIT_TABLE.items():
        if unit in tab:
            return f
    return None


def convert(value: float, src: str, dst: str) -> float:
    """``value`` from unit ``src`` to unit ``dst`` (same family; ValueError otherwise)."""
    s, d = canonical(src), canonical(dst)
    if s == d:
        return float(value)
    fs, fd = _family_of(s), _family_of(d)
    if fs is None or fs != fd:
        raise ValueError(f"cannot convert {src!r} to {dst!r}")
    return float(value) * UNIT_TABLE[fs][s] / UNIT_TABLE[fs][d]


# --------------------------------------------------------------------------- parsing
_NUMBER = re.compile(
    r"(?P<sign>[-+]?)\s*(?:"
    r"(?P<whole>\d+)(?:\s+|-)(?P<fn>\d+)\s*/\s*(?P<fd>\d+)"      # 1 1/2, 1-1/2
    r"|(?P<n>\d+)\s*/\s*(?P<d>\d+)"                                 # 1/8
    r"|(?P<dec>(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][-+]?\d+)?)"      # 3.2, 3,2, .5, 1e3
    r")")
_UNIT_TEXT = re.compile(r"\s*(?P<u>[^\d+\-.,\s][^\d]*?)?\s*(?=$|[-+]?\s*[\d.,])")


def _terms(text: str) -> list[tuple[float, str]]:
    """``text`` -> [(value, unit text)], e.g. ``2'6 1/2"`` -> [(2, "'"), (6.5, '"')]."""
    out: list[tuple[float, str]] = []
    pos, s = 0, text
    while pos < len(s):
        while pos < len(s) and s[pos].isspace():
            pos += 1
        if pos >= len(s):
            break
        m = _NUMBER.match(s, pos)
        if m is None or m.end() == pos:
            raise ValueError(f"cannot read {text!r}")
        if m.group("dec") is not None:
            v = float(m.group("dec").replace(",", "."))
        elif m.group("n") is not None:
            if int(m.group("d")) == 0:
                raise ValueError("division by zero")
            v = int(m.group("n")) / int(m.group("d"))
        else:
            if int(m.group("fd")) == 0:
                raise ValueError("division by zero")
            v = int(m.group("whole")) + int(m.group("fn")) / int(m.group("fd"))
        if m.group("sign") == "-":
            v = -v
        pos = m.end()
        u = _UNIT_TEXT.match(s, pos)
        unit = ""
        if u is not None and u.end() > pos:
            unit = (u.group("u") or "").strip()
            pos = u.end()
        out.append((v, unit))
    if not out:
        raise ValueError(f"cannot read {text!r}")
    return out


# --------------------------------------------------------------------------- Units
class Units:
    """The display units in force (immutable; :func:`current` returns the active one)."""

    def __init__(self, system: str = "metric", overrides: Optional[dict] = None,
                 decimals: Optional[dict] = None) -> None:
        self.system = system if system in SYSTEMS else "metric"
        self.overrides = {f: canonical(u) for f, u in (overrides or {}).items()
                          if u and canonical(u) in UNIT_TABLE.get(f, {})}
        self.decimal_overrides = {f: int(d) for f, d in (decimals or {}).items() if d not in ("", None)}

    @classmethod
    def from_config(cls, cfg: Any) -> "Units":
        def g(k: str) -> str:
            try:
                return str(cfg.get(k, "") or "")
            except Exception:  # noqa: BLE001 - a stub config
                return ""
        ov = {f: g(f"units.{f}") for f in OVERRIDE_FAMILIES}
        dec = {}
        for f in ("length", "speed", "accel", "area", "pressure"):
            v = g(f"units.decimals_{f}")
            if v.strip().lstrip("-").isdigit():
                dec[f] = max(0, min(8, int(v)))
        return cls(g("units.system") or "metric", ov, dec)

    # ------------------------------------------------------------------ identity
    @property
    def imperial(self) -> bool:
        return self.system == "imperial"

    def key(self) -> tuple:
        return (self.system, tuple(sorted(self.overrides.items())), tuple(sorted(self.decimal_overrides.items())))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Units) and self.key() == other.key()

    def __hash__(self) -> int:
        return hash(self.key())

    def __repr__(self) -> str:
        return f"Units({self.system!r}, {self.overrides!r}, {self.decimal_overrides!r})"

    # ------------------------------------------------------------------ per kind
    @staticmethod
    def kind(kind: str) -> Kind:
        try:
            return KINDS[kind]
        except KeyError:
            raise ValueError(f"unknown quantity kind {kind!r}") from None

    def unit(self, kind: str) -> str:
        """The display unit of ``kind`` ("in", "in/min", "psi" ...)."""
        k = self.kind(kind)
        fixed = k.imperial if self.imperial else k.metric
        if k.override and k.family in self.overrides:
            return self.overrides[k.family]
        if fixed:
            return fixed
        return SYSTEM_UNITS[self.system][k.family]

    def factor(self, kind: str) -> float:
        """Display value = internal value * factor."""
        k = self.kind(kind)
        tab = UNIT_TABLE[k.family]
        f = tab[k.internal] / tab[self.unit(kind)]
        return 1.0 / f if k.inverse else f

    def to_display(self, value: float, kind: str) -> float:
        k = self.kind(kind)
        tab = UNIT_TABLE[k.family]
        u = self.unit(kind)
        if u == k.internal:
            return float(value)
        if k.inverse:
            return float(value) * tab[u] / tab[k.internal]
        return float(value) * tab[k.internal] / tab[u]

    def from_display(self, value: float, kind: str) -> float:
        k = self.kind(kind)
        tab = UNIT_TABLE[k.family]
        u = self.unit(kind)
        if u == k.internal:
            return float(value)
        if k.inverse:
            return float(value) * tab[k.internal] / tab[u]
        return float(value) * tab[u] / tab[k.internal]

    def suffix(self, kind: str) -> str:
        """The unit text shown after a value ("in", "in/min"; prices: "/ft")."""
        u = self.unit(kind)
        return f"/{u}" if self.kind(kind).inverse else u

    def decimals(self, kind: str) -> int:
        k = self.kind(kind)
        if k.override and k.family in self.decimal_overrides and k.name != "small_length":
            return self.decimal_overrides[k.family]
        u = self.unit(kind)
        d = k.dec.get(u, k.dec.get("*", 2))
        if k.name == "small_length" and k.family in self.decimal_overrides:
            d = max(d, self.decimal_overrides[k.family] + 1)
        return int(d)

    def step(self, kind: str) -> float:
        k = self.kind(kind)
        u = self.unit(kind)
        return float(k.step.get(u, k.step.get("*", 1.0)))

    def resolution(self, kind: str) -> float:
        """One display step of the last decimal, in internal units."""
        return abs(self.from_display(10.0 ** -self.decimals(kind), kind) - self.from_display(0.0, kind))

    # ------------------------------------------------------------------ text
    def number(self, value: float, kind: str, decimals: Optional[int] = None, trim: bool = False) -> str:
        """The display number without the unit."""
        d = self.decimals(kind) if decimals is None else decimals
        v = self.to_display(value, kind)
        s = f"{v:.{d}f}"
        if trim and "." in s:
            s = s.rstrip("0").rstrip(".")
        if s in ("-0", "-0." + "0" * d) or (s.startswith("-") and float(s) == 0.0):
            s = s[1:]
        return s

    def fmt(self, value: Optional[float], kind: str, decimals: Optional[int] = None, trim: bool = False,
            unit: bool = True, missing: str = "-") -> str:
        """``value`` (internal units) as display text: ``fmt(3.175, "length")`` -> "0.125 in"."""
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return missing
        s = self.number(value, kind, decimals, trim)
        if not unit:
            return s
        suf = self.suffix(kind)
        return f"{s}{suf}" if suf in ("°",) else f"{s} {suf}"

    def fmt_xy(self, x: float, y: float, kind: str = "length", decimals: Optional[int] = None) -> str:
        return f"X {self.number(x, kind, decimals)}  Y {self.number(y, kind, decimals)} {self.suffix(kind)}"

    def fmt_size(self, w: float, h: float, kind: str = "length", decimals: Optional[int] = None,
                 trim: bool = False) -> str:
        return f"{self.number(w, kind, decimals, trim)} × {self.number(h, kind, decimals, trim)} {self.suffix(kind)}"

    def header(self, title: str, kind: str) -> str:
        """A table header with the unit: ``header("Width", "length")`` -> "Width (in)"."""
        return f"{title} ({self.suffix(kind)})"

    def parse(self, text: str, kind: str) -> float:
        """Typed input -> internal units.  A bare number is in the display unit; a number with a
        unit of the kind's family is converted (``3.2 mm``, ``1/8"``, ``2'6"``, ``1 1/2 in``,
        ``40 psi``).  Several terms add up (``2 ft 6 in``).  ValueError when nothing parses or a
        unit is of another family."""
        k = self.kind(kind)
        s = (text or "").strip()
        if not s:
            raise ValueError("empty")
        s = (s.replace("\u2212", "-").replace("\u00a0", " ").replace("\u201d", '"').replace("\u2019", "'")
             .replace("\u2033", '"').replace("\u2032", "'"))
        tab = UNIT_TABLE[k.family]
        disp = self.unit(kind)
        terms = _terms(s)
        total, last, sign = 0.0, "", 1.0
        for i, (v, u_raw) in enumerate(terms):
            if u_raw:
                u = canonical(u_raw)
                if u not in tab:
                    u2 = canonical(u_raw.lstrip("/").strip())     # price suffixes: "/ft"
                    if k.inverse and u2 in tab:
                        u = u2
                    else:
                        raise ValueError(f"{u_raw!r} is not a {k.family} unit")
            elif i > 0 and last == "ft":
                u = "in"                                      # 2' 6 -> 2 ft 6 in
            elif i > 0:
                raise ValueError(f"cannot read {text!r}")
            else:
                u = disp
            if i == 0:
                sign = -1.0 if v < 0 else 1.0
            elif v < 0:
                raise ValueError(f"cannot read {text!r}")
            last = u
            mag = abs(v)
            if k.inverse:
                total += mag * tab[k.internal] / tab[u]
            else:
                total += mag * tab[u] / tab[k.internal]
        return sign * float(f"{total:.12g}")   # 2'6" = 762.0, not 761.9999999999999


# --------------------------------------------------------------------------- current units
_METRIC = Units("metric")
_state: dict[str, Any] = {"units": _METRIC, "config": None, "unsub": None}
_listeners: list[Callable[[Units], None]] = []


def current() -> Units:
    """The display units in force."""
    return _state["units"]


def subscribe(fn: Callable[[Units], None]) -> Callable[[], None]:
    """Call ``fn(units)`` whenever the display units change; returns the unsubscribe function."""
    _listeners.append(fn)
    return lambda: _listeners.remove(fn) if fn in _listeners else None


def set_current(u: Units) -> None:
    """Make ``u`` the units in force and tell the listeners (only when it differs)."""
    if u == _state["units"]:
        return
    _state["units"] = u
    _install_text_formats(u)
    for fn in list(_listeners):
        try:
            fn(u)
        except Exception:  # noqa: BLE001 - one broken listener must not stop the others
            import logging
            logging.getLogger("mlaser.units").exception("units listener failed")


#: the original's unit rows (ManuPara.xml) -> the one units.* setting each maps onto, by option
#: index.  ``UN.GasPressureUnit`` also says in which unit the layers' pressures are *stored*
#: (0 bar, 1 MPa): that meaning stays (technology / costing / the layer table read it); only its
#: display preference is taken over by ``units.pressure``.
LEGACY_UNIT_ROWS: dict[str, tuple[str, tuple[str, ...]]] = {
    "MC.UnitType": ("units.system", ("metric", "imperial")),
    "UN.SpeedUnit": ("units.speed", ("mm/s", "m/min", "in/s", "in/min")),
    "UN.AccUnit": ("units.accel", ("mm/s²", "g", "in/s²")),
    "UN.GasPressureUnit": ("units.pressure", ("bar", "MPa")),
}


def migrate_legacy(cfg: Any) -> list[str]:
    """Copy the original's unit rows onto the ``units.*`` settings: a row the user (or an
    imported ``ManuPara.xml``) set is copied when its ``units.*`` target is not set yet, so there
    is one units setting per quantity.  Returns the keys written."""
    out: list[str] = []
    is_set = getattr(cfg, "is_set", None)
    if is_set is None:
        return out
    for src, (dst, opts) in LEGACY_UNIT_ROWS.items():
        try:
            if not is_set(src) or is_set(dst):
                continue
            i = int(cfg.get(src))
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < len(opts):
            cfg.set(dst, opts[i])
            out.append(dst)
    return out


def _install_text_formats(u: Units) -> None:
    """Lower layers that build messages with lengths (the pre-flight) take formatters from here."""
    from mlaser.cam import tipping
    from mlaser.machine import preflight

    if u.unit("length") == "mm":
        preflight.set_text_format(None, None)
        tipping.SIZE_TEXT[0] = tipping._mm_size
        return
    tipping.SIZE_TEXT[0] = lambda w, h: mm_size(w, h, sep=" x ", units=u)

    def rng(lo: float, hi: float, _fmt: str = "g") -> str:
        return f"{u.number(lo, 'length')}..{u.number(hi, 'length')} {u.suffix('length')}"

    def pt(x: float, y: float) -> str:
        return f"({u.number(x, 'length')}, {u.number(y, 'length')}) {u.suffix('length')}"

    preflight.set_text_format(rng, pt)


def bind(cfg: Any) -> None:
    """Follow ``cfg``'s ``units.*`` values (the application's ConfigService).  Rebinding replaces
    the previous binding."""
    unsub = _state.get("unsub")
    if unsub is not None:
        try:
            unsub()
        except Exception:  # noqa: BLE001
            pass
    _state["config"] = cfg
    _state["unsub"] = None
    if cfg is None:
        set_current(_METRIC)
        return
    migrate_legacy(cfg)
    if hasattr(cfg, "subscribe"):
        def changed(key: str) -> None:
            if key == "*" or key in LEGACY_UNIT_ROWS:
                migrate_legacy(cfg)          # an imported ManuPara.xml carries the original's rows
            if key == "*" or key.startswith("units."):
                set_current(Units.from_config(cfg))
        _state["unsub"] = cfg.subscribe(changed)
    set_current(Units.from_config(cfg))


def reset() -> None:
    """Back to metric with no binding (tests)."""
    bind(None)


def set_system(system: str) -> None:
    """Switch the system: through the bound ConfigService when there is one (persisted)."""
    if system not in SYSTEMS:
        raise ValueError(f"unknown unit system {system!r}")
    cfg = _state.get("config")
    if cfg is not None and hasattr(cfg, "set"):
        cfg.set("units.system", system)
    else:
        set_current(Units(system))


# module-level shortcuts on the current units
def fmt(value: Optional[float], kind: str = "length", **kw) -> str:
    return current().fmt(value, kind, **kw)


def suffix(kind: str = "length") -> str:
    return current().suffix(kind)


def to_display(value: float, kind: str = "length") -> float:
    return current().to_display(value, kind)


def from_display(value: float, kind: str = "length") -> float:
    return current().from_display(value, kind)


def parse(text: str, kind: str = "length") -> float:
    return current().parse(text, kind)


class using:
    """``with units.using(Units("metric")):`` - these units for the block, without telling the
    listeners (message keys, tests).  Not for the UI thread's display."""

    def __init__(self, u: Units) -> None:
        self._u = u
        self._prev: Optional[Units] = None

    def __enter__(self) -> Units:
        self._prev = _state["units"]
        _state["units"] = self._u
        return self._u

    def __exit__(self, *exc: Any) -> None:
        _state["units"] = self._prev


class messages_in:
    """``with units.messages_in(u):`` - the lower layers' messages (pre-flight, tipping) and the
    services' message helpers use ``u`` for the block, then go back (the CLI's text output)."""

    def __init__(self, u: Units) -> None:
        self._u = u
        self._using = using(u)

    def __enter__(self) -> Units:
        self._using.__enter__()
        _install_text_formats(self._u)
        return self._u

    def __exit__(self, *exc: Any) -> None:
        self._using.__exit__(*exc)
        _install_text_formats(_state["units"])


def _metric_shown(u: Units, kind: str) -> bool:
    return u.unit(kind) == KINDS[kind].internal


def mm_text(value: float, spec: str = "g", kind: str = "length", units: Optional[Units] = None) -> str:
    """Message text for a value in internal units: exactly ``f"{value:{spec}} mm"`` when the
    display unit is the internal one (metric messages stay byte for byte as they were), else
    the display units (``0.118 in``).  For services that build messages (repair, parts,
    material check, remnants)."""
    u = units or current()
    if _metric_shown(u, kind):
        return f"{value:{spec}} {u.suffix(kind)}"
    return u.fmt(value, kind, trim=spec.endswith("g"))


def mm_size(w: float, h: float, spec: str = ".0f", sep: str = " × ", units: Optional[Units] = None) -> str:
    """``"3000 × 1500 mm"`` in metric (``spec`` as given), ``"118.11 × 59.06 in"`` in imperial."""
    u = units or current()
    if _metric_shown(u, "length"):
        return f"{w:{spec}}{sep}{h:{spec}} {u.suffix('length')}"
    dec = 2 if spec in (".0f", "g") else None
    return f"{u.number(w, 'length', dec)}{sep}{u.number(h, 'length', dec)} {u.suffix('length')}"
