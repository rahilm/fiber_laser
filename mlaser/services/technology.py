"""Technology database: material x thickness x gas x nozzle (x laser power class) -> process.

A :class:`Technology` record holds everything a layer needs to cut one sheet: the GP.* layer
parameters of the registry (cut speed, power, frequency, duty, gas, pressure, height, focus,
the pierce stages, slow start ...), the craft defaults (leads, kerf, micro-joints) and the
small-feature and corner rules.  The layer parameters are stored as GP.* keys of the parameter
registry (``formats/data/mlaser_params.json``, 164 per layer), so a record maps onto a layer
without losing anything, and every value has the registry's type, unit and range.

* :func:`apply_to_layer` fills a :class:`mlaser.model.LayerParams` from a record: registry
  defaults plus the record's values, in exactly the form a user typing them into the layer
  parameter dialog produces (the model fields plus ``extra['process']``), so the job compiles
  the same either way (tests/test_technology.py).
* :func:`apply_craft` applies the craft defaults and rules to parts (leads, kerf, micro-joints,
  smaller leads on small features, cooling points at sharp corners).
* :class:`TechnologyStore` keeps the records in SQLite (stdlib) per machine profile under the
  platformdirs data directory (``technology/<profile>.sqlite3``).  Every save is a new version;
  :meth:`TechnologyStore.history`, :meth:`TechnologyStore.diff_factory` (against the version
  that was shipped or imported, else the registry defaults) and :meth:`TechnologyStore.revert`.
* Import/export as JSON, and import of the original's craft / ``LayerPara.xml`` files through
  :mod:`mlaser.formats.params` (one record per layer of the file); export back to that XML.

The example set (:mod:`mlaser.services.technology_examples`) is labelled as examples to verify
on the machine; it is never presented as authoritative.  Qt-free.
"""

from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Optional

from mlaser.model import Document, LayerParams, LeadParams, Part

FORMAT = "mlaser-technology/1"
SCHEMA_VERSION = 1

#: GP.CutGasType index -> gas name (the valve order of MGP.Low*/High* and the gas keys mp43..mp48)
GAS_NAMES = ("Air", "O2", "N2", "High-pressure air", "High-pressure O2", "High-pressure N2")

#: GP.* keys that are model fields of LayerParams (``parity_ports.mainapp.layer.LayerView``)
CORE_FIELDS = {
    "GP.CutSpeed": "speed",
    "GP.CutPower": "duty_pct",
    "GP.CutFreq": "frequency_hz",
    "GP.CutPeakCurrent": "power_pct",
    "GP.CutGasType": "gas",
    "GP.CutAirPressure": "gas_pressure",
    "GP.CutHeight": "cut_height",
}
EXAMPLE_NOTE = "EXAMPLE VALUES: verify on your machine before cutting."

# The registry sections (label groups) the technology editor shows, in this order.
CUT_KEYS = ("GP.CutSpeed", "GP.CutPeakCurrent", "GP.CutPower", "GP.CutFreq", "GP.CutGasType",
            "GP.CutAirPressure", "GP.CutHeight", "GP.CutFocusPos")
STAGE_KEYS = ("DrillHeight", "DrillFocusPos", "DrillPeakCurrent", "DrillPower", "DrillFreq",
              "DrillGasType", "DrillGasPressure", "DrillDelay")
SLOW_KEYS = ("GP.SlowStart", "GP.SlowStartLength", "GP.SlowStartSpeed", "GP.LaserOnDelay",
             "GP.LaserOffBeforeDelay", "GP.LaserOffAfterDelay", "GP.UpHeight")
#: GP.ManuType -> number of pierce stages (FUN_00464e10, parity_ports.mainapp.layer)
PIERCE_STAGES = {0: 0, 1: 0, 2: 1, 3: 2, 4: 3, 5: 0, 6: 4, 7: 5}
STAGES_TO_MANU = {0: 0, 1: 2, 2: 3, 3: 4, 4: 6, 5: 7}


# --------------------------------------------------------------------------- registry
def layer_registry() -> dict[str, Any]:
    """{GP key: ParamDef} of the fiber layer table (164 entries)."""
    from mlaser.formats.params import layer_defs

    return {d.key: d for d in layer_defs() if "." in d.key}


def registry_defaults() -> dict[str, Any]:
    return {k: d.default_value for k, d in layer_registry().items()}


def validate_param(key: str, value: Any, strict: bool = True) -> Any:
    """``value`` converted to the registry type of ``key``; ValueError outside its range.
    ``strict=False`` (imports) converts the type only and keeps unknown keys as they are."""
    d = layer_registry().get(key)
    if d is None:
        if not strict:
            return value
        raise KeyError(f"{key} is not a layer parameter")
    t = d.type_name
    if t == "string":
        return "" if value is None else str(value)
    if t == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    num = float(value)
    if num != num:
        raise ValueError(f"{key}: NaN")
    out: Any = int(round(num)) if t in ("int", "enum") else num
    if strict and d.max > d.min and not (d.min <= out <= d.max):
        raise ValueError(f"{key}: {out} outside [{d.min:g}, {d.max:g}] {d.unit}".rstrip())
    return out


def display_unit(key: str, pressure_unit: int = 0) -> str:
    """The unit shown for a layer parameter: the registry's, except gas pressures, which the
    registry tags "V" (the analog output) but the original's UI shows in the unit chosen by
    UN.GasPressureUnit (0 bar, 1 MPa)."""
    d = layer_registry().get(key)
    if d is None:
        return ""
    if "Pressure" in key or key.endswith("GasP"):
        return "MPa" if pressure_unit == 1 else "bar"
    if d.type_name in ("bool", "enum", "string") or d.unit in ('L""',):
        return ""
    if not d.unit and ("Power" in key or "PeakCurrent" in key):
        return "%"
    if not d.unit and "Freq" in key:
        return "Hz"
    return d.unit


def stage_count(params: dict) -> int:
    return PIERCE_STAGES.get(int(params.get("GP.ManuType", 0) or 0), 0)


# --------------------------------------------------------------------------- record
@dataclass(frozen=True)
class TechKey:
    """What a technology is for.  ``power_w`` 0 = any laser power class."""

    material: str
    thickness_mm: float
    gas: str
    nozzle: str
    power_w: int = 0

    def label(self) -> str:
        p = f" · {self.power_w / 1000:g} kW" if self.power_w else ""
        return f"{self.material} {self.thickness_mm:g} mm · {self.gas} · {self.nozzle}{p}"

    def as_tuple(self) -> tuple:
        return (self.material, float(self.thickness_mm), self.gas, self.nozzle, int(self.power_w))


def _lead(d: Optional[dict], default: LeadParams) -> LeadParams:
    if not d:
        return copy.deepcopy(default)
    f = LeadParams.__dataclass_fields__
    return LeadParams(**{k: v for k, v in d.items() if k in f})


@dataclass
class Technology:
    """One technology record (see the module docstring).

    ``params``: GP.* layer parameters this record sets (the rest are registry defaults).
    ``craft``: ``lead_in`` / ``lead_out`` (LeadParams fields), ``kerf_comp`` (-1 none, 0 inner,
    1 outer), ``kerf_width`` (mm), ``micro_joint`` (``kind`` 0 by count / 1 by distance,
    ``count``, ``distance`` mm, ``length`` mm, ``min_size`` mm).
    ``rules``: ``small_feature`` (``max_size_mm``: contours whose larger bbox side is below it
    get ``lead_kind`` / ``lead_length_mm`` and no micro-joints when ``no_joints``) and
    ``corner`` (``cool_points``: cooling points at corners sharper than ``max_angle_deg``),
    ``lead_place`` (automatic start points, :class:`mlaser.cam.leadplace.LeadRules` fields, applied
    when ``enabled``) and ``tip`` (``strategy`` joint / last / avoid, ``joint_width_mm``: the
    tip-avoiding cut order of :mod:`mlaser.cam.tipping`; docs/user/cutting-strategy.md).
    ``gas_flow_l_min``: the nozzle's gas flow at the cut pressure when known (cost estimate).
    """

    key: TechKey
    params: dict[str, Any] = field(default_factory=dict)
    craft: dict[str, Any] = field(default_factory=dict)
    rules: dict[str, Any] = field(default_factory=dict)
    gas_flow_l_min: Optional[float] = None
    density_g_cm3: Optional[float] = None
    notes: str = ""
    origin: str = "user"            # example | user | import:<file> | json
    verified: bool = False          # "tested OK on this machine"
    id: Optional[int] = None        # store row id
    version: int = 0                # store version (0 = not stored)

    @property
    def is_example(self) -> bool:
        return self.origin == "example"

    def full_params(self) -> dict[str, Any]:
        """Registry defaults overlaid with this record's values (all 164 layer parameters)."""
        out = registry_defaults()
        out.update(self.params)
        return out

    def validated(self, strict: bool = True) -> "Technology":
        """A copy whose params are converted to the registry types (``strict``: ValueError when
        one does not fit its range, KeyError for an unknown key)."""
        return replace(self, params={k: validate_param(k, v, strict) for k, v in self.params.items()})

    # ---------------------------------------------------------------- json
    def to_dict(self) -> dict:
        return {
            "key": asdict(self.key),
            "params": dict(sorted(self.params.items())),
            "craft": self.craft,
            "rules": self.rules,
            "gas_flow_l_min": self.gas_flow_l_min,
            "density_g_cm3": self.density_g_cm3,
            "notes": self.notes,
            "origin": self.origin,
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Technology":
        k = d["key"]
        key = TechKey(str(k["material"]), float(k["thickness_mm"]), str(k["gas"]), str(k["nozzle"]),
                      int(k.get("power_w", 0) or 0))
        return cls(key, dict(d.get("params") or {}), dict(d.get("craft") or {}), dict(d.get("rules") or {}),
                   d.get("gas_flow_l_min"), d.get("density_g_cm3"), str(d.get("notes", "")),
                   str(d.get("origin", "user")), bool(d.get("verified", False)))

    def content(self) -> dict:
        """What versions compare (everything but the key, id and version)."""
        d = self.to_dict()
        d.pop("key")
        return d


# --------------------------------------------------------------------------- diff
@dataclass(frozen=True)
class Change:
    """One difference between two records (or a record and a layer)."""

    key: str            # "GP.CutSpeed", "craft.kerf_width", "rules.corner.max_angle_deg", "notes"
    old: Any
    new: Any

    @property
    def is_param(self) -> bool:
        return self.key.startswith("GP.")


def _flatten(prefix: str, v: Any, out: dict) -> None:
    if isinstance(v, dict):
        for k, x in v.items():
            _flatten(f"{prefix}.{k}" if prefix else str(k), x, out)
    else:
        out[prefix] = v


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-12)
    return a == b


def diff(old: Optional[Technology], new: Technology) -> list[Change]:
    """Differences ``old`` -> ``new``: GP parameters over the full set (a missing key is its
    registry default), then craft, rules and the other fields.  ``old`` None = registry defaults
    and no craft/rules."""
    out: list[Change] = []
    a = old.full_params() if old is not None else registry_defaults()
    b = new.full_params()
    reg = layer_registry()
    order = {k: i for i, k in enumerate(reg)}
    for k in sorted(set(a) | set(b), key=lambda k: (order.get(k, 1 << 30), k)):
        if not _same(a.get(k), b.get(k)):
            out.append(Change(k, a.get(k), b.get(k)))
    fa: dict = {}
    fb: dict = {}
    if old is not None:
        _flatten("craft", old.craft, fa)
        _flatten("rules", old.rules, fa)
        for n in ("gas_flow_l_min", "density_g_cm3", "notes", "verified"):
            fa[n] = getattr(old, n)
    else:
        fa.update(gas_flow_l_min=None, density_g_cm3=None, notes="", verified=False)
    _flatten("craft", new.craft, fb)
    _flatten("rules", new.rules, fb)
    for n in ("gas_flow_l_min", "density_g_cm3", "notes", "verified"):
        fb[n] = getattr(new, n)
    for k in sorted(set(fa) | set(fb)):
        if not _same(fa.get(k), fb.get(k)):
            out.append(Change(k, fa.get(k), fb.get(k)))
    return out


# --------------------------------------------------------------------------- layers
def layer_values(lp: Optional[LayerParams]) -> dict[str, Any]:
    """The effective GP.* values of a layer, as the job compiler reads them (LayerView)."""
    from mlaser.formats.params import layer_default_values
    from mlaser.parity_ports.mainapp.layer import LayerView, set_layer_defaults

    set_layer_defaults(layer_default_values, replace_existing=False)   # the registry's, unless set
    v = dict(LayerView(lp).values)
    reg = layer_registry()
    return {k: v.get(k, d.default_value) for k, d in reg.items()}


def apply_to_layer(lp: LayerParams, tech: Technology) -> LayerParams:
    """Fill ``lp`` from ``tech`` (in place; returns ``lp``): every layer parameter becomes the
    registry default overlaid with the record's values.  The model fields carry the core cut
    values, ``extra['process']`` every other value that differs from the default (what the layer
    parameter dialog stores), ``extra['technology']`` names the record.  Earlier values loaded
    from a LayerPara.xml (``extra['values']``) and the placeholder mark are dropped, and the
    pierce stages are taken from the GP keys (``pierce`` cleared), so nothing of the previous
    setting shadows the technology."""
    full = tech.validated(strict=False).full_params()
    defaults = registry_defaults()
    for gp, attr in CORE_FIELDS.items():
        cur = getattr(lp, attr)
        setattr(lp, attr, type(cur)(full[gp]))
    process = {k: v for k, v in full.items() if k not in CORE_FIELDS and not _same(v, defaults.get(k))}
    extra = {k: v for k, v in (lp.extra or {}).items()
             if k not in ("values", "gp", "process", "placeholder", "technology", "category", "co2")}
    extra["process"] = process
    extra["technology"] = {"id": tech.id, "version": tech.version, "key": asdict(tech.key),
                           "example": tech.is_example, "gas_flow_l_min": tech.gas_flow_l_min,
                           "density_g_cm3": tech.density_g_cm3}
    lp.extra = extra
    lp.pierce = {}
    return lp


def layer_technology_ref(lp: Optional[LayerParams]) -> Optional[dict]:
    if lp is None:
        return None
    ref = (lp.extra or {}).get("technology")
    return ref if isinstance(ref, dict) else None


def layer_changes(lp: LayerParams, tech: Technology) -> list[Change]:
    """How the layer's effective values differ from the record (edits after applying it)."""
    cur = layer_values(lp)
    want = tech.full_params()
    return [Change(k, want.get(k), cur.get(k)) for k in cur if not _same(cur.get(k), want.get(k))]


def technology_from_layer(lp: Optional[LayerParams], key: TechKey, origin: str = "user",
                          notes: str = "") -> Technology:
    """A record holding a layer's effective values (only those that differ from the defaults)."""
    defaults = registry_defaults()
    vals = layer_values(lp)
    params = {k: v for k, v in vals.items() if not _same(v, defaults.get(k))}
    return Technology(key, params, origin=origin, notes=notes)


# --------------------------------------------------------------------------- craft
DEFAULT_CRAFT: dict[str, Any] = {
    "lead_in": asdict(LeadParams(kind=1, angle_deg=90.0, length=3.0, radius=2.0)),
    "lead_out": asdict(LeadParams(kind=0)),
    "kerf_comp": -1,
    "kerf_width": 0.0,
    "micro_joint": {"kind": 0, "count": 0, "distance": 100.0, "length": 0.5, "min_size": 0.0},
}


def apply_craft(doc: Document, tech: Technology, parts: Optional[Iterable[Part]] = None,
                leads: bool = True, kerf: bool = True, joints: bool = True, rules: bool = True) -> int:
    """Apply the record's craft defaults and rules to ``parts`` (default: the parts on the layers
    that use ``tech``... callers pass the parts).  Uses the same operations as the ribbon
    commands (:mod:`mlaser.ops.craft`, :mod:`mlaser.ops.cam`).  Returns the parts changed."""
    from mlaser.cam.manual import contour_craft
    from mlaser.ops import craft as C
    from mlaser.ops.cam import CoolParams, cool_auto

    targets = list(parts if parts is not None else doc.parts)
    if not targets:
        return 0
    cr = {**DEFAULT_CRAFT, **(tech.craft or {})}
    sel = [p.selected for p in doc.parts]
    tset = {id(p) for p in targets}
    for p in doc.parts:                       # the ops act on the selection: select the targets
        p.selected = id(p) in tset
    try:
        if leads:
            C.apply_lead(doc, _lead(cr.get("lead_in"), LeadParams(kind=1)), _lead(cr.get("lead_out"), LeadParams()))
        if kerf:
            C.apply_kerf(doc, int(cr.get("kerf_comp", -1)), float(cr.get("kerf_width", 0.0)))
        mj = cr.get("micro_joint") or {}
        if joints and int(mj.get("count", 0) or 0) + (1 if int(mj.get("kind", 0)) == 1 else 0) > 0:
            C.auto_microjoints(doc, C.MicroJointParams(
                kind=int(mj.get("kind", 0)), count=int(mj.get("count", 0)),
                distance=float(mj.get("distance", 100.0)), length=float(mj.get("length", 0.5)),
                min_size=float(mj.get("min_size", 0.0))))
        sf = (tech.rules or {}).get("small_feature") or {}
        lim = float(sf.get("max_size_mm", 0.0) or 0.0)
        if rules and lim > 0:
            small_lead = LeadParams(kind=int(sf.get("lead_kind", 1)), angle_deg=90.0,
                                    length=float(sf.get("lead_length_mm", 1.0)),
                                    radius=float(sf.get("lead_length_mm", 1.0)) / 2)
            for p in targets:
                if not p.contours:
                    continue
                outer = max(range(len(p.contours)), key=lambda i: p.contours[i].length())
                for ci, c in enumerate(p.contours):
                    if ci == outer and len(p.contours) > 1:
                        continue                   # the outer contour of a part with holes
                    pts = list(c.points())
                    xs = [pt[0] for pt in pts]
                    ys = [pt[1] for pt in pts]
                    if not xs or max(max(xs) - min(xs), max(ys) - min(ys)) >= lim:
                        continue
                    k = contour_craft(p, ci, create=True)
                    k.lead_in = copy.deepcopy(small_lead)
                    k.lead_out = LeadParams(kind=0)
                    if sf.get("no_joints", True):
                        k.micro_joints = []
        corner = (tech.rules or {}).get("corner") or {}
        if rules and corner.get("cool_points"):
            cool_auto(doc, CoolParams(sharp=True, max_angle=float(corner.get("max_angle_deg", 90.0))))
        lp = (tech.rules or {}).get("lead_place") or {}
        if rules and lp.get("enabled"):
            from mlaser.cam.leadplace import LeadRules, place_leads
            place_leads(doc, LeadRules.from_dict(lp), parts=targets)
    finally:
        for p, s in zip(doc.parts, sel):
            p.selected = s
    return len(targets)


# --------------------------------------------------------------------------- store
def store_path(profile: str = "default") -> Path:
    from .config import data_dir

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", profile or "default")
    return data_dir() / "technology" / f"{safe}.sqlite3"


@dataclass(frozen=True)
class VersionInfo:
    version: int
    created: float
    comment: str
    origin: str


class TechnologyStore:
    """SQLite store of technology records with a version history (see the module docstring)."""

    def __init__(self, path: Optional[Path | str] = None, profile: str = "default",
                 seed_examples: bool = True) -> None:
        self.profile = profile
        self.path = Path(path) if path is not None else store_path(profile)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self._schema()
        if seed_examples and self._meta("examples_seeded") is None:
            self.seed_examples()

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------------- schema
    def _schema(self) -> None:
        c = self.db
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS tech (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material TEXT NOT NULL, thickness REAL NOT NULL, gas TEXT NOT NULL,
                nozzle TEXT NOT NULL, power_w INTEGER NOT NULL DEFAULT 0,
                head INTEGER NOT NULL, factory INTEGER, deleted INTEGER NOT NULL DEFAULT 0,
                UNIQUE (material, thickness, gas, nozzle, power_w));
            CREATE TABLE IF NOT EXISTS version (
                tech_id INTEGER NOT NULL REFERENCES tech(id), version INTEGER NOT NULL,
                created REAL NOT NULL, comment TEXT NOT NULL DEFAULT '', data TEXT NOT NULL,
                PRIMARY KEY (tech_id, version));
            """
        )
        if self._meta("schema") is None:
            self._set_meta("schema", str(SCHEMA_VERSION))
        c.commit()

    def _meta(self, key: str) -> Optional[str]:
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if r is None else str(r[0])

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def seed_examples(self) -> int:
        from .technology_examples import examples

        n = 0
        for t in examples():
            if self.find(t.key) is None:
                self.save(t, "example set", factory=True)
                n += 1
        self._set_meta("examples_seeded", "1")
        self.db.commit()
        return n

    # ---------------------------------------------------------------- read
    def _load(self, tech_id: int, version: int) -> Technology:
        r = self.db.execute("SELECT data FROM version WHERE tech_id=? AND version=?", (tech_id, version)).fetchone()
        if r is None:
            raise KeyError(f"technology {tech_id} has no version {version}")
        t = Technology.from_dict(json.loads(r[0]))
        return replace(t, id=tech_id, version=version)

    def get(self, tech_id: int, version: Optional[int] = None) -> Technology:
        r = self.db.execute("SELECT head FROM tech WHERE id=?", (tech_id,)).fetchone()
        if r is None:
            raise KeyError(f"no technology {tech_id}")
        return self._load(tech_id, int(version if version is not None else r[0]))

    def find(self, key: TechKey) -> Optional[Technology]:
        r = self.db.execute(
            "SELECT id, head FROM tech WHERE material=? AND thickness=? AND gas=? AND nozzle=? AND power_w=?"
            " AND deleted=0", key.as_tuple()).fetchone()
        return None if r is None else self._load(int(r[0]), int(r[1]))

    def all(self) -> list[Technology]:
        rows = self.db.execute(
            "SELECT id, head FROM tech WHERE deleted=0 ORDER BY material, thickness, gas, nozzle, power_w").fetchall()
        return [self._load(int(r[0]), int(r[1])) for r in rows]

    def materials(self) -> list[str]:
        return [str(r[0]) for r in self.db.execute(
            "SELECT DISTINCT material FROM tech WHERE deleted=0 ORDER BY material")]

    def query(self, material: Optional[str] = None, thickness: Optional[float] = None,
              gas: Optional[str] = None, nozzle: Optional[str] = None) -> list[Technology]:
        out = []
        for t in self.all():
            k = t.key
            if material is not None and k.material != material:
                continue
            if thickness is not None and not math.isclose(k.thickness_mm, thickness):
                continue
            if gas is not None and k.gas != gas:
                continue
            if nozzle is not None and k.nozzle != nozzle:
                continue
            out.append(t)
        return out

    def history(self, tech_id: int) -> list[VersionInfo]:
        rows = self.db.execute("SELECT version, created, comment, data FROM version WHERE tech_id=? ORDER BY version",
                               (tech_id,)).fetchall()
        return [VersionInfo(int(r[0]), float(r[1]), str(r[2]), str(json.loads(r[3]).get("origin", ""))) for r in rows]

    def factory(self, tech_id: int) -> Optional[Technology]:
        """The version that was shipped (example set) or imported, None for a user's own record."""
        r = self.db.execute("SELECT factory FROM tech WHERE id=?", (tech_id,)).fetchone()
        if r is None or r[0] is None:
            return None
        return self._load(tech_id, int(r[0]))

    # ---------------------------------------------------------------- write
    def save(self, tech: Technology, comment: str = "", factory: bool = False,
             strict: bool = True) -> Technology:
        """Store ``tech`` as a new version (validated).  The record is found by ``tech.id`` or,
        for a new record, by its key.  Saving unchanged content returns the current version.
        ``factory`` marks the version as the factory reference (example set, import)."""
        t = tech.validated(strict)
        data = json.dumps(t.to_dict(), sort_keys=True)
        c = self.db
        tid = t.id
        if tid is None:
            cur = self.find(t.key)
            tid = cur.id if cur is not None else None
        if tid is None:
            row = c.execute("SELECT id FROM tech WHERE material=? AND thickness=? AND gas=? AND nozzle=? AND power_w=?",
                            t.key.as_tuple()).fetchone()
            if row is not None:                      # a deleted record with this key: revive it
                tid = int(row[0])
                c.execute("UPDATE tech SET deleted=0 WHERE id=?", (tid,))
        if tid is None:
            cur_ = c.execute(
                "INSERT INTO tech (material, thickness, gas, nozzle, power_w, head, factory) VALUES (?,?,?,?,?,0,NULL)",
                t.key.as_tuple())
            tid = int(cur_.lastrowid or 0)
            head = 0
        else:
            head = int(c.execute("SELECT head FROM tech WHERE id=?", (tid,)).fetchone()[0])
            if head:
                old = self._load(tid, head)
                if old.content() == t.content() and old.key == t.key:
                    c.commit()
                    return old
            c.execute("UPDATE tech SET material=?, thickness=?, gas=?, nozzle=?, power_w=? WHERE id=?",
                      (*t.key.as_tuple(), tid))
        v = head + 1
        c.execute("INSERT INTO version (tech_id, version, created, comment, data) VALUES (?,?,?,?,?)",
                  (tid, v, time.time(), comment, data))
        c.execute("UPDATE tech SET head=? WHERE id=?", (v, tid))
        if factory:
            c.execute("UPDATE tech SET factory=? WHERE id=?", (v, tid))
        c.commit()
        return replace(t, id=tid, version=v)

    def revert(self, tech_id: int, version: Optional[int] = None) -> Technology:
        """A new version with the content of ``version`` (default: the factory version, else 1)."""
        if version is None:
            f = self.factory(tech_id)
            version = f.version if f is not None else 1
        old = self.get(tech_id, version)
        cur = self.get(tech_id)
        return self.save(replace(old, id=tech_id, key=cur.key), f"revert to version {version}")

    def delete(self, tech_id: int) -> None:
        self.db.execute("UPDATE tech SET deleted=1 WHERE id=?", (tech_id,))
        self.db.commit()

    def diff_factory(self, tech_id: int, version: Optional[int] = None) -> list[Change]:
        """``version`` (default: current) against the factory version (registry defaults for a
        user's own record)."""
        return diff(self.factory(tech_id), self.get(tech_id, version))

    def diff_versions(self, tech_id: int, a: int, b: int) -> list[Change]:
        return diff(self.get(tech_id, a), self.get(tech_id, b))

    # ---------------------------------------------------------------- JSON
    def export_json(self, path: Path | str, techs: Optional[Iterable[Technology]] = None) -> int:
        items = [t.to_dict() for t in (techs if techs is not None else self.all())]
        data = {"format": FORMAT, "profile": self.profile, "exported": time.time(), "technologies": items}
        p = Path(path)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")
        tmp.replace(p)
        return len(items)

    def import_json(self, path: Path | str, comment: str = "") -> list[Technology]:
        data = json.loads(Path(path).read_text("utf-8"))
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise ValueError(f"{path}: not a {FORMAT} file")
        out = []
        for d in data.get("technologies", []):
            t = Technology.from_dict(d)
            new = self.find(t.key) is None
            out.append(self.save(t, comment or f"import {Path(path).name}", factory=new, strict=False))
        return out

    # ---------------------------------------------------------------- original XML
    def import_xml(self, path: Path | str, key: Optional[TechKey] = None,
                   layers: Optional[Iterable[int]] = None) -> list[Technology]:
        """Import the original's craft / ``LayerPara.xml`` file (formats.params): one record per
        layer the file defines (``layers``: 0-based layer indexes to take).  ``key`` names the
        record (for a file with several layers the nozzle gets " L<n>"); by default the material
        is the file name, the thickness the first number in it, the gas from GP.CutGasType."""
        recs = read_layer_xml(path, key, layers)
        return [self.save(t, f"import {Path(path).name}", factory=self.find(t.key) is None, strict=False)
                for t in recs]

    def export_xml(self, path: Path | str, techs: Iterable[Technology]) -> None:
        """Write records as a LayerPara.xml (record n -> layer n, at most 11)."""
        from mlaser.formats.params import N_LAYERS, save_layer_params

        layers = {}
        for i, t in enumerate(list(techs)[:N_LAYERS]):
            lp = apply_to_layer(LayerParams(index=i), t)
            lp.extra["values"] = layer_values(lp)
            layers[i] = lp
        save_layer_params(layers, path)


def read_layer_xml(path: Path | str, key: Optional[TechKey] = None,
                   layers: Optional[Iterable[int]] = None) -> list[Technology]:
    """The records of a craft / LayerPara.xml file without storing them (see ``import_xml``)."""
    from mlaser.formats.params import N_LAYERS, ParamStore, layer_category

    st = ParamStore.load(path)
    reg = layer_registry()
    defaults = registry_defaults()
    present = []
    for i in range(N_LAYERS):
        cat = layer_category(i)
        if any(f.startswith("P" + cat + ".") for f in st.values):
            present.append(i)
    if layers is not None:
        want = set(layers)
        present = [i for i in present if i in want]
    stem = Path(path).stem
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*mm", stem) or re.search(r"(\d+(?:[.,]\d+)?)", stem)
    thick = float(m.group(1).replace(",", ".")) if m else 0.0
    out = []
    for i in present:
        cat = layer_category(i)
        vals = {}
        for k, d in reg.items():
            raw = st.get_raw(cat, k)
            if raw is None:
                continue
            v = d.parse(raw)
            if not _same(v, defaults.get(k)):
                vals[k] = v
        gas_i = int(vals.get("GP.CutGasType", defaults.get("GP.CutGasType", 0)) or 0)
        if key is not None:
            k2 = key if len(present) == 1 else replace(key, nozzle=f"{key.nozzle} L{i + 1}")
        else:
            k2 = TechKey(stem, thick, GAS_NAMES[gas_i % len(GAS_NAMES)], f"layer {i + 1}")
        out.append(Technology(k2, vals, origin=f"import:{Path(path).name}",
                              notes=str(vals.get("GP.Note", "") or "")))
    return out
