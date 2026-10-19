"""ConfigService: one typed configuration store for the application.

Before Wave 2 configuration lived in about nine places (architecture.md §1.8): the parameter
registry, a free-text ``window.settings`` dict that only some dialogs and extensions read (and
that the controller and NC compiler never saw), ``nc.Machine()`` always built with defaults,
ad-hoc dicts (``mj_params``, lead state, ``ext_machine.values``), ``diag_state.json`` and several
app-data directory schemes.  This service replaces them:

* **Schema generated from the registry.**  Every entry of ``formats/data/mlaser_params.json``
  (1,129 descriptors recovered from MainApp.exe) becomes a :class:`Setting` with its type,
  unit, range, options and default; application and plugin settings are registered the same
  way (:meth:`ConfigService.register`).  :meth:`ConfigService.set` validates.
* **One store of explicit values** (``values``): what the user set.  Everything else reads the
  schema default, so an untouched installation behaves exactly as before.
* **Persistence** under platformdirs (``config.json`` in the user config directory;
  ``MLASER_CONFIG_DIR`` / ``MLASER_DATA_DIR`` override it, tests use a temporary directory),
  plus import/export of the original's ``HardPara.xml`` / ``ManuPara.xml`` / ``LayerPara.xml``
  through :mod:`mlaser.formats.params`.
* **Machine profile.**  :meth:`ConfigService.machine` builds the :class:`mlaser.nc.Machine`
  the job compiler, the motion planner (``MotionConfig``) and the process sequencer
  (``ProcessConfig``) use: the rebuild's bench machine with every parameter the user changed
  applied (``profile = "bench"``, default), or the machine described entirely by the parameter
  set (``profile = "params"``, chosen automatically after importing a ``dat`` directory).

Qt-free.  :meth:`ConfigService.mapping` is a ``MutableMapping`` view used where code expects the
old ``settings`` dict (diag ``ParamSet``, extensions).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import MutableMapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

MISSING: Any = object()

# registry type -> setting kind
_KIND = {
    "int": "int",
    "double": "float",
    "pulse_equiv": "float",
    "string": "str",
    "enum": "enum",
    "bool": "bool",
    "do_port": "int",
    "di_port": "int",
    "ipv4": "str",
    "da_port": "int",
    "gas_ratio": "int",
    "do_port_ext": "int",
    "serial_port": "int",
    "color": "int",
}


@dataclass(frozen=True)
class Setting:
    """One typed configuration entry."""

    key: str
    kind: str = "str"  # int | float | bool | str | enum | json
    default: Any = None
    unit: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    options: tuple = ()  # enum: labels (index = value)
    label: str = ""  # lang key or text
    section: str = ""  # registry category (ManuParam, AxisParam...) or namespace
    file: Optional[str] = None  # HardPara.xml / ManuPara.xml / LayerPara.xml / None
    source: str = "registry"  # registry | app | plugin:<name>

    def validate(self, value: Any) -> Any:
        """``value`` converted to this setting's type; ValueError when it does not fit."""
        k = self.kind
        if k == "json":
            return value
        if k == "bool":
            if isinstance(value, str):
                v = value.strip().lower()
                if v in ("1", "true", "yes", "on"):
                    return True
                if v in ("0", "false", "no", "off", ""):
                    return False
                raise ValueError(f"{self.key}: not a boolean: {value!r}")
            return bool(value)
        if k == "str":
            return "" if value is None else str(value)
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{self.key}: not a number: {value!r}") from None
        if num != num:
            raise ValueError(f"{self.key}: NaN")
        if k in ("int", "enum"):
            if num != int(num):
                raise ValueError(f"{self.key}: not an integer: {value!r}")
            out: Any = int(num)
        else:
            out = num
        lo, hi = self.min, self.max
        if k == "enum" and self.options:
            lo, hi = 0, len(self.options) - 1
        if lo is not None and hi is not None and hi > lo and not (lo <= out <= hi):
            raise ValueError(f"{self.key}: {out} outside [{lo:g}, {hi:g}] {self.unit}".rstrip())
        return out


_XML_KEYS: dict[tuple[str, str], str] = {}  # (registry category, key) -> setting key


def _registry_settings() -> dict[str, Setting]:
    """One Setting per registry entry.  The key is the parameter name (``GC.GasDelay``); the 13
    names that occur in two categories get ``"<category>:<name>"`` for the second one."""
    from mlaser.formats.params import param_defs

    out: dict[str, Setting] = {}
    for d in param_defs():
        if "." not in d.key:  # struct sub-records (no own value)
            continue
        key = d.key if d.key not in out else f"{d.category}:{d.key}"
        if key in out:
            continue
        _XML_KEYS[(d.category, d.key)] = key
        kind = _KIND.get(d.type_name, "str")
        default = d.default_value
        if kind == "str" and default == 'L""':
            default = ""
        unit = d.unit if d.unit not in ('L""',) else ""
        has_range = d.max > d.min
        out[key] = Setting(
            key,
            kind,
            default,
            unit,
            d.min if has_range else None,
            d.max if has_range else None,
            tuple(o[0] or o[1] for o in d.options),
            d.lang,
            d.category,
            d.file,
        )
    return out


_REGISTRY_CACHE: dict[str, Setting] = {}


def registry_settings() -> dict[str, Setting]:
    """The registry part of the schema (cached; shared by every ConfigService)."""
    if not _REGISTRY_CACHE:
        _REGISTRY_CACHE.update(_registry_settings())
    return _REGISTRY_CACHE


# --------------------------------------------------------------------------- paths
def _test_run() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


def config_dir() -> Path:
    """Directory of ``config.json``: ``$MLASER_CONFIG_DIR``, a per-process temp directory under
    pytest, else the platform's user config directory (platformdirs)."""
    d = os.environ.get("MLASER_CONFIG_DIR")
    if d:
        return Path(d)
    if _test_run():
        return Path(tempfile.gettempdir()) / f"mlaser-test-{os.getpid()}" / "config"
    import platformdirs

    return Path(platformdirs.user_config_dir("mlaser", appauthor=False))


def data_dir() -> Path:
    """Directory for logs, reports, counters and backups: ``$MLASER_DATA_DIR``, a per-process temp
    directory under pytest, else the platform's user data directory (platformdirs).  Replaces
    the ``~/.mlaser`` / ``%LOCALAPPDATA%/NexCut`` schemes (architecture.md §1.8 #9)."""
    d = os.environ.get("MLASER_DATA_DIR")
    if d:
        return Path(d)
    if _test_run():
        return Path(tempfile.gettempdir()) / f"mlaser-test-{os.getpid()}"
    import platformdirs

    return Path(platformdirs.user_data_dir("mlaser", appauthor=False))


# --------------------------------------------------------------------------- safety I/O
#: card input numbers are 1-based (``MCStatus.input``: 1..12 RO[4], 13..28 RO[21]); outputs
#: 1..26 (``set_output``: 1..10 sub 2, 11..26 sub 13).  0 = not wired / off.
MAX_INPUT = 28
MAX_OUTPUT = 26


def _safety_settings() -> list[Setting]:
    """The interlocks and the "PC alive" heartbeat (docs/SAFETY.md §8, §11), typed and persisted.
    The keys are the ``safety.<field>`` names :meth:`InterlockConfig.from_values` reads.  Every
    input defaults to 0 (not wired): the hardware is unknown until a service engineer
    commissions it, and the Run screen, the pre-flight and the commissioning checklist say so
    (:mod:`mlaser.services.safety_io`)."""
    out: list[Setting] = []
    for src, inp, act in (("gas", "gas_low_input", "gas_low_active"),
                          ("door", "door_input", "door_open_active"),
                          ("laser", "laser_alarm_input", "laser_alarm_active")):
        out += [
            Setting(f"safety.{inp}", "int", 0, "", 0, MAX_INPUT, label=f"safety.set.{inp}",
                    section="safety", source="app"),
            Setting(f"safety.{act}", "bool", True, label=f"safety.set.{act}", section="safety", source="app"),
            # the service engineer declares that this machine has no such sensor (silences the
            # pre-flight warning; the Run screen and the checklist still list it)
            Setting(f"safety.{src}_not_fitted", "bool", False, label="safety.set.not_fitted",
                    section="safety", source="app"),
        ]
    out += [
        Setting("safety.follower", "bool", True, label="safety.set.follower", section="safety", source="app"),
        Setting("safety.heartbeat_output", "int", 0, "", 0, MAX_OUTPUT, label="safety.set.heartbeat_output",
                section="safety", source="app"),
        Setting("safety.heartbeat_hz", "float", 5.0, "Hz", 0.5, 50.0, label="safety.set.heartbeat_hz",
                section="safety", source="app"),
    ]
    return out


SAFETY_SETTINGS: tuple[Setting, ...] = tuple(_safety_settings())

#: application settings outside the registry (``plugins.allowlist``: entry-point plugins the
#: service engineer enabled, name -> entry-point target; docs/dev/PLUGINS.md §6)
APP_SETTINGS: tuple[Setting, ...] = (
    Setting("plugins.allowlist", "json", {}, label="plugins.allowlist", section="plugins", source="app"),
)


# --------------------------------------------------------------------------- service
@dataclass
class MachineProfile:
    """How :meth:`ConfigService.machine` builds the machine."""

    base: str = "bench"  # "bench": Machine() + changed parameters; "params": all from the set
    overrides: dict = field(default_factory=dict)  # Machine attribute overrides (e.g. smooth)


class ConfigService:
    """The typed configuration store (see the module docstring)."""

    FILE = "config.json"

    def __init__(self, path: Optional[os.PathLike | str] = None, load: bool = True) -> None:
        self.schema: dict[str, Setting] = dict(registry_settings())
        self.schema.update({s.key: s for s in SAFETY_SETTINGS + APP_SETTINGS})
        self.values: dict[str, Any] = {}
        self.profile = MachineProfile()
        self.path = Path(path) if path is not None else config_dir() / self.FILE
        self._listeners: list[Callable[[str], None]] = []
        self._machine_cache: Any = None
        self._dirty = False
        if load:
            self.load()
        install_layer_defaults(self)

    # ------------------------------------------------------------------ schema
    def register(self, settings: Iterable[Setting], replace_existing: bool = False) -> None:
        """Add application or plugin settings; an existing key is refused unless
        ``replace_existing`` (duplicates are an error, not a silent override)."""
        for s in settings:
            if s.key in self.schema and not replace_existing:
                raise KeyError(f"setting {s.key!r} is already defined ({self.schema[s.key].source})")
            self.schema[s.key] = s

    def setting(self, key: str) -> Optional[Setting]:
        return self.schema.get(key)

    def section(self, name: str) -> list[Setting]:
        return [s for s in self.schema.values() if s.section == name]

    def sections(self) -> list[str]:
        return sorted({s.section for s in self.schema.values() if s.section})

    # ------------------------------------------------------------------ values
    def get(self, key: str, default: Any = MISSING) -> Any:
        if key in self.values:
            return self.values[key]
        s = self.schema.get(key)
        if s is not None:
            return s.default
        if default is MISSING:
            raise KeyError(key)
        return default

    def is_set(self, key: str) -> bool:
        return key in self.values

    def set(self, key: str, value: Any) -> Any:
        s = self.schema.get(key)
        v = s.validate(value) if s is not None else value
        if self.values.get(key, MISSING) == v and key in self.values:
            return v
        self.values[key] = v
        self._changed(key)
        return v

    def reset(self, key: str) -> None:
        if key in self.values:
            del self.values[key]
            self._changed(key)

    def update(self, values: dict) -> None:
        for k, v in values.items():
            self.set(k, v)

    def _changed(self, key: str) -> None:
        self._dirty = True
        self._machine_cache = None
        for fn in list(self._listeners):
            fn(key)

    def subscribe(self, fn: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def mapping(self) -> "SettingsView":
        return SettingsView(self)

    def namespace(self, prefix: str, registry_fallback: bool = True) -> "NamespaceView":
        """A ``dict``-like store for one component (``"machine_ops"``): keys are kept as
        ``"<prefix>.<key>"``; reading a key that is not stored there falls back to the value
        the user set for the registry parameter of that name (so Options reach the component)."""
        return NamespaceView(self, prefix, registry_fallback)

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        prof = data.get("profile") or {}
        self.profile = MachineProfile(prof.get("base", "bench"), dict(prof.get("overrides", {})))
        for k, v in (data.get("values") or {}).items():
            s = self.schema.get(k)
            try:
                self.values[k] = s.validate(v) if s is not None else v
            except ValueError:
                continue  # an out-of-range stored value falls back to the default
        self._dirty = False
        self._machine_cache = None

    def save(self, path: Optional[os.PathLike | str] = None) -> Path:
        p = Path(path) if path is not None else self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        vals = {k: v for k, v in self.values.items() if _jsonable(v)}
        data = {
            "version": 1,
            "profile": {"base": self.profile.base, "overrides": self.profile.overrides},
            "values": vals,
        }
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")
        os.replace(tmp, p)
        self._dirty = False
        return p

    def flush(self) -> None:
        if self._dirty:
            try:
                self.save()
            except OSError:
                pass

    # ------------------------------------------------------------------ original XML files
    def machine_params(self):
        """A :class:`mlaser.formats.params.MachineParams` (the three ParaModule slots) with the
        registry defaults and every set value."""
        from mlaser.formats.params import MachineParams, param_defs

        mp = MachineParams.defaults()
        by_key = {}
        for d in param_defs():
            if d.file in ("HardPara.xml", "ManuPara.xml") and "." in d.key:
                by_key.setdefault(d.key, []).append(d)
        for k, v in self.values.items():
            for d in by_key.get(k.split(":", 1)[-1], ()):
                st = mp.hard if d.file == "HardPara.xml" else mp.manu
                try:
                    st.set(d, v)
                except (TypeError, ValueError):
                    continue
        return mp

    def import_dat(self, dat_dir: os.PathLike | str, profile: str = "params") -> int:
        """Load ``HardPara.xml`` / ``ManuPara.xml`` (and the legacy ``SystemPara.xml``) of an
        original install; every value that differs from the default becomes a set value.  The
        machine profile switches to ``profile`` (default: the machine described by the files).
        Returns the number of values taken."""
        from mlaser.formats.params import MachineParams, param_defs

        mp = MachineParams.load_dir(Path(dat_dir), fill_defaults=False)
        n = 0
        for d in param_defs():
            if d.file not in ("HardPara.xml", "ManuPara.xml") or "." not in d.key:
                continue
            st = mp.hard if d.file == "HardPara.xml" else mp.manu
            raw = st.get_raw(d.category, d.key)
            if raw is None:
                continue
            key = _XML_KEYS.get((d.category, d.key), d.key)
            v = d.parse(raw)
            if v == d.default_value:
                self.values.pop(key, None)
                continue
            try:
                self.values[key] = self.schema[key].validate(v) if key in self.schema else v
            except ValueError:
                self.values[key] = v  # keep the machine's own value even when out of range
            n += 1
        self.profile = replace(self.profile, base=profile)
        self._changed("*")
        return n

    def export_dat(self, dat_dir: os.PathLike | str) -> None:
        """Write the three XML files (registry defaults + set values) like ParaModule does."""
        self.machine_params().save_dir(dat_dir)

    # ------------------------------------------------------------------ machine profile
    def set_profile(self, base: str, **overrides) -> None:
        if base not in ("bench", "params"):
            raise ValueError(f"unknown machine profile {base!r}")
        self.profile = MachineProfile(base, {**self.profile.overrides, **overrides})
        self._changed("profile")

    def machine(self):
        """The :class:`mlaser.nc.Machine` for compiling jobs (cached until a value changes).

        ``bench``: ``Machine()`` with each parameter the user changed applied the way
        :meth:`Machine.from_params` applies it (attribute, ``ProcessConfig`` field and the raw
        ``params`` the motion config reads); with nothing changed this is exactly ``Machine()``.
        ``params``: ``Machine.from_params`` of the full parameter set."""
        if self._machine_cache is not None:
            return self._machine_cache
        from mlaser.nc.machine import Machine

        mp = self.machine_params()
        if self.profile.base == "params":
            m = Machine.from_params(mp)
        else:
            m = Machine()
            changed = {
                k.split(":", 1)[-1]
                for k in self.values
                if self.schema.get(k) is not None and self.schema[k].file in ("HardPara.xml", "ManuPara.xml")
            }
            if changed:
                cur = Machine.from_params(mp)
                ref = _reference_machine()
                for f in fields(Machine):
                    if f.name in ("process", "params", "cut"):
                        continue
                    a, b = getattr(cur, f.name), getattr(ref, f.name)
                    if a != b:
                        setattr(m, f.name, a)
                pc = {
                    f.name: getattr(cur.process, f.name)
                    for f in fields(cur.process)
                    if getattr(cur.process, f.name) != getattr(ref.process, f.name)
                }
                if pc:
                    m.process = replace(m.process, **pc)
                m.params = {k: v for k, v in cur.params.items() if k in changed}
        for k, v in self.profile.overrides.items():
            if hasattr(m, k):
                setattr(m, k, v)
        self._machine_cache = m
        return m

    def motion_config(self):
        return self.machine().motion_config()

    # ------------------------------------------------------------------ layer defaults
    def layer_defaults(self, co2: bool = False) -> dict[str, Any]:
        """The ``GP.*`` layer descriptor defaults the NC layer view starts from
        (``parity_ports.mainapp.layer``).  These are the registry defaults exactly: a layer's own
        values come from the document (LayerPara.xml / the layer table), so the compiled job does
        not depend on which ConfigService is open (the goldens prove it)."""
        from mlaser.formats.params import layer_default_values

        return layer_default_values(co2)

    def process_config(self):
        return self.machine().process_config()


def install_layer_defaults(svc: Optional["ConfigService"] = None) -> None:
    """Hand the layer defaults to the parity fence (``set_layer_defaults``): the ConfigService's
    provider, or the plain registry reader without one."""
    from mlaser.formats.params import layer_default_values
    from mlaser.parity_ports.mainapp.layer import set_layer_defaults

    set_layer_defaults(svc.layer_defaults if svc is not None else layer_default_values)


_REF: list = []


def _reference_machine():
    """``Machine.from_params`` of the pure registry defaults (what "unchanged" means)."""
    if not _REF:
        from mlaser.formats.params import MachineParams
        from mlaser.nc.machine import Machine

        _REF.append(Machine.from_params(MachineParams.defaults()))
    return _REF[0]


def _jsonable(v: Any) -> bool:
    try:
        json.dumps(v)
        return True
    except (TypeError, ValueError):
        return False


class SettingsView(MutableMapping):
    """``dict``-like view of the explicitly set values: it behaves like the old
    ``window.settings`` dict (``key in view`` only for set keys, ``get(key, default)``), but
    writes go through :meth:`ConfigService.set` (validated, persisted, listeners told)."""

    def __init__(self, svc: ConfigService) -> None:
        self._svc = svc

    def __getitem__(self, key: str) -> Any:
        if key in self._svc.values:
            return self._svc.values[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._svc.values:
            return self._svc.values[key]
        return default

    def __setitem__(self, key: str, value: Any) -> None:
        self._svc.set(key, value)

    def __delitem__(self, key: str) -> None:
        if key not in self._svc.values:
            raise KeyError(key)
        self._svc.reset(key)

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._svc.values))

    def __len__(self) -> int:
        return len(self._svc.values)

    def __repr__(self) -> str:
        return f"SettingsView({self._svc.values!r})"


class NamespaceView(MutableMapping):
    """See :meth:`ConfigService.namespace`."""

    def __init__(self, svc: ConfigService, prefix: str, registry_fallback: bool = True) -> None:
        self._svc, self._p, self._fallback = svc, prefix.rstrip(".") + ".", registry_fallback

    def _own(self) -> list[str]:
        return [k[len(self._p) :] for k in self._svc.values if k.startswith(self._p)]

    def __getitem__(self, key: str) -> Any:
        full = self._p + key
        if full in self._svc.values:
            return self._svc.values[full]
        if self._fallback and key in self._svc.values and key in self._svc.schema:
            return self._svc.values[key]
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self._svc.set(self._p + key, value)

    def __delitem__(self, key: str) -> None:
        self._svc.reset(self._p + key)

    def __iter__(self) -> Iterator[str]:
        own = self._own()
        extra = [k for k in self._svc.values if self._fallback and k in self._svc.schema and k not in own]
        return iter(own + extra)

    def __len__(self) -> int:
        return len(list(iter(self)))
