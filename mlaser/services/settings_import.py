"""Import the settings of an existing Mlaser installation, with a preview.  Qt-free.

The user points at their install folder (``C:/Program Files/Mlaser``) and, optionally, at the
parameter folder (``%LOCALAPPDATA%\\NexCut`` or a copy of it).  :func:`preview` lists what would
change without changing anything; :func:`apply` takes the chosen areas:

=========  ==================================================  ==================================
area       read from                                           goes to (mlaser's own folders)
=========  ==================================================  ==================================
params     ``HardPara.xml`` / ``ManuPara.xml`` (legacy          ConfigService values (machine
           ``SystemPara.xml``)                                  profile ``params``, as ``import_dat``)
layers     ``LayerPara.xml``                                    a copy in ``<data>/params``
                                                                (``machine_ops.param_dir``)
ip         ``File/ipAdd.ini`` (CardIP, ZFIP, LaserIP ...)        the ``ip`` setting, a copy in
                                                                ``<app>/File/ipAdd.ini``
jump       ``JumpAddTime.txt`` ``[Jump] AddTime``               machine profile override
                                                                ``jump_add_time_ms``
calib      ``Help/calib.csv``                                   ``<data>/calib.csv``
lang       ``Lang/*.txt``                                       imported catalogues (the i18n
                                                                importer, ``<data>/lang``)
=========  ==================================================  ==================================

**Nothing is ever written to the original installation**: every destination is checked to lie
outside the install folder and the parameter folder (:class:`WriteBackError`), and the files are
read only.  Before applying, ``config.json`` is backed up next to itself.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

PARAM_FILES = ("HardPara.xml", "ManuPara.xml", "LayerPara.xml", "SystemPara.xml")
AREAS = ("params", "layers", "ip", "jump", "calib", "lang")
#: ipAdd.ini key -> key in the ``ip`` setting (the IP dialog's pd445..pd453)
IP_KEYS = {"CardIP": "pd445", "CardSubnet": "pd446", "CardGateway": "pd447",
           "ZFIP": "pd448", "ZFSubnet": "pd449", "ZFGateway": "pd450",
           "LaserIP": "pd451", "LaserSubnet": "pd452", "LaserGateway": "pd453"}
IP_DEFAULTS = {"pd445": "10.1.1.168", "pd446": "255.255.255.0", "pd447": "10.1.1.1",
               "pd448": "10.1.1.169", "pd449": "255.255.255.0", "pd450": "10.1.1.1",
               "pd451": "10.1.1.170", "pd452": "255.255.255.0", "pd453": "10.1.1.1"}


class WriteBackError(RuntimeError):
    """A destination lies inside the original installation."""


@dataclass
class Change:
    area: str
    key: str
    label: str = ""                 # catalogue id or text
    old: Any = None
    new: Any = None
    unit: str = ""


@dataclass
class ImportPlan:
    install: Optional[Path]
    param_dir: Optional[Path] = None
    files: dict[str, Path] = field(default_factory=dict)      # role -> source file
    changes: list[Change] = field(default_factory=list)
    languages: list[tuple[str, str, int]] = field(default_factory=list)   # (code, name, texts)
    layers: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def by_area(self) -> dict[str, list[Change]]:
        out: dict[str, list[Change]] = {a: [] for a in AREAS}
        for c in self.changes:
            out.setdefault(c.area, []).append(c)
        return out

    def available(self) -> list[str]:
        """Areas that have something to import."""
        have = {c.area for c in self.changes}
        if self.layers:
            have.add("layers")
        if self.languages:
            have.add("lang")
        if "calib" in self.files:
            have.add("calib")
        return [a for a in AREAS if a in have]

    def roots(self) -> list[Path]:
        return [p for p in (self.install, self.param_dir) if p is not None]


@dataclass
class ImportResult:
    applied: dict[str, int] = field(default_factory=dict)      # area -> count
    written: list[Path] = field(default_factory=list)
    config_backup: Optional[Path] = None
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- locating
def _has_params(d: Path) -> bool:
    return d.is_dir() and any((d / f).is_file() for f in PARAM_FILES)


def find_param_dir(install: Optional[Path], param_dir: Optional[Path] = None) -> Optional[Path]:
    """The folder with the parameter XMLs: the explicit one, else the install folder, its
    ``dat`` / ``NexCut`` subfolders (a copy of ``%LOCALAPPDATA%\\NexCut`` put next to it)."""
    cands: list[Path] = []
    if param_dir is not None:
        cands += [param_dir, param_dir / "NexCut", param_dir / "dat"]
    if install is not None:
        cands += [install, install / "dat", install / "NexCut", install / "NexCut" / "dat"]
    for d in cands:
        if _has_params(d):
            return d
    return None


def localappdata_nexcut() -> Optional[Path]:
    """``%LOCALAPPDATA%\\NexCut`` when it exists (Windows), to offer as the parameter folder."""
    la = os.environ.get("LOCALAPPDATA")
    if la and (Path(la) / "NexCut").is_dir():
        return Path(la) / "NexCut"
    return None


def _first(*paths: Path) -> Optional[Path]:
    return next((p for p in paths if p.is_file()), None)


def locate(install: Optional[os.PathLike | str], param_dir: Optional[os.PathLike | str] = None) -> ImportPlan:
    inst = Path(install).resolve() if install else None
    pd = find_param_dir(inst, Path(param_dir).resolve() if param_dir else None)
    plan = ImportPlan(inst, pd)
    if inst is not None and not inst.is_dir():
        plan.errors.append(f"not a folder: {inst}")
        return plan
    if pd is not None:
        for f in PARAM_FILES:
            if (pd / f).is_file():
                plan.files[f] = pd / f
    if inst is not None:
        for role, p in (("ipAdd.ini", _first(inst / "File" / "ipAdd.ini", inst / "ipAdd.ini")),
                        ("JumpAddTime.txt", _first(inst / "JumpAddTime.txt", inst / "File" / "JumpAddTime.txt")),
                        ("calib.csv", _first(inst / "Help" / "calib.csv", inst / "calib.csv"))):
            if p is not None:
                plan.files[role] = p
    if pd is None:
        plan.warnings.append("no_params")
    return plan


# --------------------------------------------------------------------------- preview
def _param_changes(plan: ImportPlan, config: Any) -> None:
    from mlaser.formats.params import MachineParams, param_defs
    from mlaser.services.config import _XML_KEYS, registry_settings

    if plan.param_dir is None or not any(f in plan.files for f in ("HardPara.xml", "ManuPara.xml", "SystemPara.xml")):
        return
    registry_settings()                                   # fills _XML_KEYS
    mp = MachineParams.load_dir(plan.param_dir, fill_defaults=False)
    for d in param_defs():
        if d.file not in ("HardPara.xml", "ManuPara.xml") or "." not in d.key:
            continue
        st = mp.hard if d.file == "HardPara.xml" else mp.manu
        raw = st.get_raw(d.category, d.key)
        if raw is None:
            continue
        key = _XML_KEYS.get((d.category, d.key), d.key)
        try:
            new = d.parse(raw)
        except (TypeError, ValueError):
            plan.warnings.append(f"unreadable:{key}")
            continue
        old = config.get(key, None)
        if new == old or (new == d.default_value and not config.is_set(key)):
            continue
        s = config.setting(key)
        plan.changes.append(Change("params", key, s.label if s else "", old, new, s.unit if s else ""))


def _ip_changes(plan: ImportPlan, config: Any) -> None:
    p = plan.files.get("ipAdd.ini")
    if p is None:
        return
    vals = read_ip_ini(p)
    cur = dict(IP_DEFAULTS)
    cur.update(config.get("ip", None) or {})
    for ini_key, k in IP_KEYS.items():
        if ini_key in vals and vals[ini_key] != cur.get(k):
            plan.changes.append(Change("ip", f"ip.{k}", k, cur.get(k), vals[ini_key]))
    if "CardPort" in vals and vals["CardPort"] != "502":
        plan.changes.append(Change("ip", "ip.CardPort", "CardPort", "502", vals["CardPort"]))


def read_ip_ini(path: Path) -> dict[str, str]:
    vals: dict[str, str] = {}
    for raw in Path(path).read_bytes().decode("gbk", "replace").splitlines():
        if "=" in raw and not raw.lstrip().startswith((";", "#", "[")):
            k, v = raw.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


def _jump_changes(plan: ImportPlan, config: Any) -> None:
    p = plan.files.get("JumpAddTime.txt")
    if p is None:
        return
    from mlaser.formats.scfile import JumpAddTime

    try:
        j = JumpAddTime.load(p)
    except (OSError, ValueError) as e:
        plan.warnings.append(f"jump:{e}")
        return
    old = float(config.machine().jump_add_time_ms)
    if float(j.add_time_ms) != old:
        plan.changes.append(Change("jump", "jump_add_time_ms", "wizard.import.jump_label", old,
                                   float(j.add_time_ms), "ms"))


def _calib_info(plan: ImportPlan) -> None:
    p = plan.files.get("calib.csv")
    if p is None:
        return
    from mlaser.devices.follower import CalibrationTable

    try:
        t = CalibrationTable.load(str(p))
    except OSError:
        del plan.files["calib.csv"]
        return
    plan.changes.append(Change("calib", "calib.csv", "wizard.import.calib_label", None, len(t.points), "points"))


def _lang_info(plan: ImportPlan) -> None:
    if plan.install is None:
        return
    from mlaser.i18n import importer

    for f in importer.find_lang_files(plan.install):
        parsed = importer.parse_file(f)
        if parsed is not None and parsed[2]:
            plan.languages.append((parsed[0], parsed[1], len(parsed[2])))


def _layer_info(plan: ImportPlan) -> None:
    p = plan.files.get("LayerPara.xml")
    if p is None:
        return
    from mlaser.formats.params import ParamStore

    try:
        st = ParamStore.load(p)
    except Exception as e:  # noqa: BLE001 - a broken file is reported, not raised
        plan.warnings.append(f"layers:{e}")
        return
    import re

    cats = {m.group(1) for k in st.values for m in [re.search(r"(?:CO2)?LayerParam(\d+)\.", k)] if m}
    plan.layers = len(cats)


def preview(install: Optional[os.PathLike | str], config: Any,
            param_dir: Optional[os.PathLike | str] = None) -> ImportPlan:
    """What an import would change (nothing is written)."""
    plan = locate(install, param_dir)
    if plan.errors:
        return plan
    _param_changes(plan, config)
    _layer_info(plan)
    _ip_changes(plan, config)
    _jump_changes(plan, config)
    _calib_info(plan)
    _lang_info(plan)
    return plan


# --------------------------------------------------------------------------- apply
def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def check_destination(dest: Path, plan: ImportPlan) -> Path:
    for r in plan.roots():
        if _inside(dest, r):
            raise WriteBackError(f"refusing to write {dest}: it is inside the original installation {r}")
    return dest


def _copy(src: Path, dest: Path, plan: ImportPlan, res: ImportResult) -> Path:
    check_destination(dest, plan)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        bak = dest.with_name(dest.name + ".bak")
        check_destination(bak, plan)
        shutil.copy2(dest, bak)
    shutil.copy2(src, dest)
    res.written.append(dest)
    return dest


def apply(plan: ImportPlan, config: Any, areas: Optional[Iterable[str]] = None,
          data_root: Optional[Path] = None, app_root: Optional[Path] = None,
          lang_dest: Optional[Path] = None) -> ImportResult:
    """Apply the chosen ``areas`` (default: every available one).  ``data_root`` /
    ``app_root`` / ``lang_dest`` default to mlaser's data directory, its app folder
    (``machine_ops`` ``app_dir``) and the language store."""
    from mlaser.services.config import data_dir

    want = set(plan.available() if areas is None else areas)
    res = ImportResult()
    data = Path(data_root) if data_root is not None else data_dir()
    if app_root is None:
        from mlaser.controller.machine_ops import default_app_dir
        app_root = Path(config.get("app_dir", None) or default_app_dir())
    for root in (data, app_root):
        check_destination(root, plan)
    # backup of the configuration first
    try:
        if config.path.exists():
            bak = config.path.with_name(config.path.name + time.strftime(".%Y%m%d-%H%M%S.bak"))
            check_destination(bak, plan)
            shutil.copy2(config.path, bak)
            res.config_backup = bak
    except OSError as e:
        res.errors.append(f"config backup: {e}")
    by = plan.by_area()
    copy_dir = data / "params"
    if ("params" in want and by.get("params")) or ("layers" in want and plan.layers):
        for f, src in plan.files.items():
            if f in PARAM_FILES:
                _copy(src, copy_dir / f, plan, res)
    if "params" in want and by.get("params"):
        res.applied["params"] = config.import_dat(copy_dir)
    if "layers" in want and plan.layers:
        config.set("machine_ops.param_dir", str(copy_dir))
        res.applied["layers"] = plan.layers
    if "ip" in want and by.get("ip"):
        ip = dict(IP_DEFAULTS)
        ip.update(config.get("ip", None) or {})
        for c in by["ip"]:
            k = c.key.split(".", 1)[1]
            if k in IP_DEFAULTS:
                ip[k] = str(c.new)
        config.set("ip", ip)
        _copy(plan.files["ipAdd.ini"], Path(app_root) / "File" / "ipAdd.ini", plan, res)
        res.applied["ip"] = len(by["ip"])
    if "jump" in want and by.get("jump"):
        config.set_profile(config.profile.base, jump_add_time_ms=float(by["jump"][0].new))
        res.applied["jump"] = 1
    if "calib" in want and "calib.csv" in plan.files:
        dest = data / "calib.csv"
        _copy(plan.files["calib.csv"], dest, plan, res)
        config.set("machine.calib_csv", str(dest))
        res.applied["calib"] = 1
    if "lang" in want and plan.languages and plan.install is not None:
        from mlaser.i18n import importer

        dest = Path(lang_dest) if lang_dest is not None else importer.lang_store()
        check_destination(dest, plan)
        try:
            codes = importer.import_path(plan.install, dest)
            res.applied["lang"] = len(codes)
        except FileNotFoundError as e:
            res.errors.append(str(e))
    try:
        config.save()
    except OSError as e:
        res.errors.append(f"config save: {e}")
    return res


__all__ = ["preview", "apply", "locate", "find_param_dir", "ImportPlan", "ImportResult", "Change",
           "WriteBackError", "AREAS", "IP_KEYS", "read_ip_ini", "localappdata_nexcut"]
