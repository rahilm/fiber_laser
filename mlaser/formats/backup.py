"""Parameter backup / restore and task (.aut) containers.

Recovered from MainApp.exe (base 0x400000), NCModule.dll and ParaModule.dll.  Tags: **O**
observed (address given), **I** inferred, **U** unknown.  docs/re/protocol.md §19-§20.

Parameter files (O, FUN_00451c30 at 0x4d91e2..0x4d9301): ``HardPara.xml``, ``ManuPara.xml``,
``LayerPara.xml`` (and legacy ``SystemPara.xml``) live in
``SHGetSpecialFolderPathW(CSIDL_LOCAL_APPDATA)\\NexCut\\``; the backups ``BkHardPara.xml``,
``BkManuPara.xml``, ``BkLayerPara.xml`` and ``SecondBkManuPara.xml`` in ``<exe>\\File\\``
(path block 0xadfde8 +0x411c..+0x41c4).  The rebuild's cross-platform equivalent of the first is
:func:`default_param_dir` (``$MLASER_PARAM_DIR`` or ``<user data dir>/NexCut``).

Commands (O):

=================================  ======  ===========  ============================================
mf480 Export Parameters to Hardware 0xbba   FUN_00489540 password; hard table -> card slots 0/1; save
                                                         + copy Hard/Layer/Manu XML to the Bk files
mf234 Export Parameters to File     0x2724  FUN_0048a710 every table into one chosen *.xml
mf481 Import Parameters From HW     0xbbd   FUN_00489dd0 password + mf238; card -> hard table;
                                                         saveHardParam; layer/manu from Bk files
mf235 Import Parameters From File   0x2725  FUN_0048ae20 *.xml -> hard + manu tables; A250107_0
                                                         "also restore layer parameters?"
=================================  ======  ===========  ============================================

**Card blocks** (writeHardParam2Card FUN_10050fb0 = CNCModule +0x29c, readParamFromCard
FUN_10051320 = +0x2a0, O): only the 457-entry HardPara table (0xafb4d8, descriptor order) goes to
the card, in 250-entry chunks: slot 0 = entries 0..249 at register 59600, slot 1 = entries
250..456 at 59601.  Write ``[0xffffffff, 0xe0, v0, v1, ...]``; read ``count + 2`` words, the reply
must start with ``0xffffffff, 0xe0``.  Value encoding by descriptor type: 1, 4, 6, 7, 10, 11,
12, 13 int32; 2, 9 double as trunc(v·10000) (cvttsd2si), read back ·0.0001; 5 bool; anything
else (3 = string) is sent as 0 and skipped on read.  No checksum.

**Default check** FUN_00636ca0 (O): the hardware parameters count as "default" (not saved, and
read from the card at start-up, mf441) when the X/Y axis indices (cfg +0xbbd4/+0xbbd8),
ZF.ZFType and LGP.LaserControlType are all 0.  The rebuild checks the two file parameters it has
(``ZF.ZFType``, ``LGP.LaserControlType``) plus ``A1.AxisIndex`` (I: the Y axis index field).

**Task file** ``*.aut`` (save FUN_0048d250, import FUN_0048c550 / unpacker FUN_0048eb40, O):
the parts are concatenated raw, each followed by an ASCII tag (no lengths)::

    tempGraph.chf TASK_GRAPH tempLayer.xml TASK_LAYER tempManu.ini TASK_MANU
    AutosaveParam1.ini TASK_PARAM_ONE AutosaveParam2.ini TASK_PARAM_TWO tempIsBreak.ini TASK_IS_BREAK

On import the parts are written to ``<exe>\\File\\Temp\\`` as tempGraph.chf, tempLayer.xml,
ManuContour.dat (the save side calls it tempManu.ini), AutosaveParam1/2.ini and
tempIsBreak.ini; a laser type different from SP.m_iEnableLaserType aborts with A250421_0.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

from .params import (FILES, MachineParams, ParamDef, ParamStore, defs_for, layer_category,
                     layer_defs, N_LAYERS)

PathLike = Union[str, Path]

HW_REG = 59600                 # F.REG_HWPARAM
HW_HEADER = (0xFFFFFFFF, 0xE0)
HW_CHUNK = 250                 # entries per slot (MainApp FUN_00489540 / FUN_005ba6e0)
INT_TYPES = {1, 4, 6, 7, 10, 11, 12, 13}
DOUBLE_TYPES = {2, 9}
BOOL_TYPES = {5}
DOUBLE_SCALE = 10000.0
BACKUP_NAMES = {"HardPara.xml": "BkHardPara.xml", "ManuPara.xml": "BkManuPara.xml",
                "LayerPara.xml": "BkLayerPara.xml"}
SECOND_MANU_BACKUP = "SecondBkManuPara.xml"
# The original gates hardware export/import behind a fixed vendor password (prompt ss11,
# FUN_0061d230). Vendor passwords are intentionally not reproduced: the rebuild asks for an
# explicit confirmation, or for a password the user configured themselves (values["param_password"]).
PARAM_PASSWORD = None


def default_param_dir() -> Path:
    """Cross-platform stand-in for ``%LOCALAPPDATA%\\NexCut`` (O location, see module doc)."""
    env = os.environ.get("MLASER_PARAM_DIR")
    if env:
        return Path(env)
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "NexCut"
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "NexCut"


# --------------------------------------------------------------------------- card blocks
def hard_table() -> list[ParamDef]:
    """The 457 HardPara descriptors in table order (0xafb4d8)."""
    return sorted(defs_for("hard"), key=lambda d: d.index)


def encode_value(d: ParamDef, value) -> int:
    """One word of writeHardParam2Card (FUN_10050fb0, O type switch)."""
    if d.type in INT_TYPES:
        return int(value) & 0xFFFFFFFF
    if d.type in DOUBLE_TYPES:
        return int(float(value) * DOUBLE_SCALE) & 0xFFFFFFFF        # cvttsd2si truncates
    if d.type in BOOL_TYPES:
        return 1 if value else 0
    return 0


def decode_value(d: ParamDef, word: int):
    """Inverse (readParamFromCard FUN_10051320); None = skipped (strings)."""
    v = word - (1 << 32) if word & 0x80000000 else word
    if d.type in INT_TYPES:
        return v
    if d.type in DOUBLE_TYPES:
        return v / DOUBLE_SCALE
    if d.type in BOOL_TYPES:
        return bool(word & 0xFF)
    return None


def hard_blocks(store: ParamStore) -> list[tuple[int, list[int]]]:
    """``[(register, words)]`` for slots 0 and 1."""
    table = hard_table()
    out = []
    for slot, start in enumerate(range(0, len(table), HW_CHUNK)):
        chunk = table[start:start + HW_CHUNK]
        out.append((HW_REG + slot, [*HW_HEADER, *(encode_value(d, store.get(d)) for d in chunk)]))
    return out


def apply_hard_block(store: ParamStore, slot: int, words: Sequence[int]) -> int:
    """Store the values of one read block; returns the number of parameters set."""
    if tuple(words[:2]) != HW_HEADER:
        raise ValueError("hardware parameter block: bad header (expected ffffffff e0)")
    table = hard_table()[slot * HW_CHUNK:(slot + 1) * HW_CHUNK]
    n = 0
    for d, w in zip(table, words[2:]):
        v = decode_value(d, w & 0xFFFFFFFF)
        if v is not None:
            store.set(d, v)
            n += 1
    return n


def write_hard_to_card(mc, store: ParamStore) -> None:
    """mf480 card part: write both slots (MotionController.write_regs, func 0x40)."""
    for reg, words in hard_blocks(store):
        mc.write_regs(reg, words)


def read_hard_from_card(mc, store: ParamStore) -> int:
    """mf481 / start-up recovery card part: read both slots into ``store``."""
    table = hard_table()
    n = 0
    for slot, start in enumerate(range(0, len(table), HW_CHUNK)):
        cnt = len(table[start:start + HW_CHUNK]) + 2
        n += apply_hard_block(store, slot, mc.read_regs(HW_REG + slot, cnt))
    return n


def is_default_hard(store: ParamStore) -> bool:
    """FUN_00636ca0 (see module doc)."""
    want = {"ZF.ZFType", "LGP.LaserControlType", "A1.AxisIndex"}
    vals = [store.get(d) for d in hard_table() if d.key in want]
    return bool(vals) and all(not v for v in vals)


# --------------------------------------------------------------------------- XML files
def _file_of_category(cat: str) -> Optional[str]:
    if cat.startswith("LayerParam") or cat.startswith("CO2LayerParam"):
        return "LayerPara.xml"
    for d in hard_table():
        if d.category == cat:
            return "HardPara.xml"
    return "ManuPara.xml"


def export_params_file(mp: MachineParams, path: PathLike) -> Path:
    """mf234 (FUN_0048a710, O): ParaModule +0x28 writes every table (hard, the six manu tables,
    all fiber and CO2 layer tables) into the one chosen XML -> a single ParameterRoot."""
    st = ParamStore({**mp.hard.values, **mp.manu.values, **mp.layer.values})
    p = Path(path)
    st.save(p)
    return p


def import_params_file(path: PathLike, mp: MachineParams, layers: bool = False) -> dict[str, int]:
    """mf235 (FUN_0048ae20, O): hard + manu tables from the XML; layer tables only when the user
    answers A250107_0 with yes.  Returns the number of values taken per file.  Raises
    ValueError (A250105_1) when the file has no parameter of the hard table."""
    st = ParamStore.load(path)
    counts = {"HardPara.xml": 0, "ManuPara.xml": 0, "LayerPara.xml": 0}
    targets = {"HardPara.xml": mp.hard, "ManuPara.xml": mp.manu, "LayerPara.xml": mp.layer}
    cat_cache: dict[str, Optional[str]] = {}
    for full, text in st.values.items():
        cat = full[1:].split(".", 1)[0]
        f = cat_cache.setdefault(cat, _file_of_category(cat))
        if f == "LayerPara.xml" and not layers:
            continue
        targets[f].values[full] = text
        counts[f] += 1
    if counts["HardPara.xml"] == 0:
        raise ValueError("A250105_1: parameter file has no hardware parameters")
    return counts


def save_with_backup(mp: MachineParams, param_dir: PathLike, backup_dir: PathLike,
                     which: Sequence[str] = FILES) -> list[Path]:
    """saveHardParam FUN_00481240 & co (O): save each XML, copy it to its Bk file, re-read the
    backup to verify it.  Returns the backup paths."""
    pd, bd = Path(param_dir), Path(backup_dir)
    pd.mkdir(parents=True, exist_ok=True)
    bd.mkdir(parents=True, exist_ok=True)
    stores = dict(zip(FILES, (mp.hard, mp.manu, mp.layer)))
    out = []
    for name in which:
        stores[name].save(pd / name)
        bk = bd / BACKUP_NAMES[name]
        shutil.copyfile(pd / name, bk)
        ParamStore.load(bk)                      # "saveHardParam backup read failed" check
        out.append(bk)
    return out


def restore_from_backups(mp: MachineParams, backup_dir: PathLike,
                         which: Sequence[str] = ("ManuPara.xml", "LayerPara.xml")) -> None:
    """mf481 tail (O): reload the manu and layer tables from BkManuPara/BkLayerPara."""
    bd = Path(backup_dir)
    for name in which:
        p = bd / BACKUP_NAMES[name]
        if p.exists():
            st = ParamStore.load(p)
            target = mp.manu if name == "ManuPara.xml" else mp.layer
            target.values.update(st.values)


@dataclass
class ParamBackup:
    """The four mf210/mf211 commands bound to a parameter directory and backup directory."""
    params: MachineParams
    param_dir: Path = field(default_factory=default_param_dir)
    backup_dir: Path = field(default_factory=lambda: Path("File"))

    def export_to_hardware(self, mc) -> None:
        if is_default_hard(self.params.hard):
            raise ValueError("saveHardParam: parameters are default, not saved")
        write_hard_to_card(mc, self.params.hard)
        save_with_backup(self.params, self.param_dir, self.backup_dir)

    def import_from_hardware(self, mc) -> int:
        n = read_hard_from_card(mc, self.params.hard)
        if is_default_hard(self.params.hard):
            raise ValueError("OnParamRestore Hardware save param is wrong")
        save_with_backup(self.params, self.param_dir, self.backup_dir, ("HardPara.xml",))
        restore_from_backups(self.params, self.backup_dir)
        return n

    def export_to_file(self, path: PathLike) -> Path:
        return export_params_file(self.params, path)

    def import_from_file(self, path: PathLike, layers: bool = False) -> dict[str, int]:
        counts = import_params_file(path, self.params, layers)
        save_with_backup(self.params, self.param_dir, self.backup_dir,
                         FILES if layers else ("HardPara.xml", "ManuPara.xml"))
        return counts


# --------------------------------------------------------------------------- task file (.aut)
TASK_PARTS = (("tempGraph.chf", b"TASK_GRAPH"), ("tempLayer.xml", b"TASK_LAYER"),
              ("tempManu.ini", b"TASK_MANU"), ("AutosaveParam1.ini", b"TASK_PARAM_ONE"),
              ("AutosaveParam2.ini", b"TASK_PARAM_TWO"), ("tempIsBreak.ini", b"TASK_IS_BREAK"))
TASK_IMPORT_NAMES = ("tempGraph.chf", "tempLayer.xml", "ManuContour.dat", "AutosaveParam1.ini",
                     "AutosaveParam2.ini", "tempIsBreak.ini")


def pack_task(parts: dict[str, bytes]) -> bytes:
    """FUN_0048dfb0 per part: ``out << ifstream(file).rdbuf(); out << TAG`` (missing parts are
    skipped, as the original logs "Error opening output file!" and goes on)."""
    out = bytearray()
    for name, tag in TASK_PARTS:
        if name in parts and parts[name] is not None:
            out += parts[name]
            out += tag
    return bytes(out)


def unpack_task(data: bytes) -> dict[str, bytes]:
    """FUN_0048eb40: read until the buffer ends with the next tag; returns {import name: bytes}
    (import names: ManuContour.dat for the TASK_MANU part)."""
    out: dict[str, bytes] = {}
    pos = 0
    for (name, tag), imp in zip(TASK_PARTS, TASK_IMPORT_NAMES):
        i = data.find(tag, pos)
        if i < 0:
            continue
        out[imp] = data[pos:i]
        pos = i + len(tag)
    return out


@dataclass
class TaskFile:
    """Typed view of an .aut task."""
    chf: bytes = b""
    layer_xml: bytes = b""
    manu: list[int] = field(default_factory=list)      # contour order (CAD +0x1a8)
    only_selected: int = 0                              # sys+0x4841
    autosave1: Optional[bytes] = None
    autosave2: Optional[bytes] = None
    is_break: int = 0
    laser_type: int = 0                                 # SP.m_iEnableLaserType (sys+0x4728)

    def dumps(self) -> bytes:
        from .scfile import TempIsBreak, write_values
        parts = {"tempGraph.chf": self.chf, "tempLayer.xml": self.layer_xml,
                 "tempManu.ini": write_values([len(self.manu) + 1, *self.manu, self.only_selected]),
                 "AutosaveParam1.ini": self.autosave1, "AutosaveParam2.ini": self.autosave2,
                 "tempIsBreak.ini": TempIsBreak(self.is_break, self.laser_type).dumps()}
        return pack_task(parts)

    @classmethod
    def loads(cls, data: bytes) -> "TaskFile":
        from .scfile import read_values
        p = unpack_task(data)
        t = cls(chf=p.get("tempGraph.chf", b""), layer_xml=p.get("tempLayer.xml", b""),
                autosave1=p.get("AutosaveParam1.ini"), autosave2=p.get("AutosaveParam2.ini"))
        if p.get("ManuContour.dat"):
            v = [int(x) for x in read_values(p["ManuContour.dat"])]
            if v:
                n = v[0]
                body = v[1:1 + n]
                t.manu, t.only_selected = body[:-1], (body[-1] if body else 0)
        if p.get("tempIsBreak.ini"):
            v = [int(x) for x in read_values(p["tempIsBreak.ini"])] + [0, 0]
            t.is_break, t.laser_type = v[0], v[1]
        return t

    def unpack_to(self, temp_dir: PathLike, data: Optional[bytes] = None) -> list[Path]:
        """Write the parts to ``<exe>\\File\\Temp\\`` like the importer (O)."""
        d = Path(temp_dir)
        d.mkdir(parents=True, exist_ok=True)
        out = []
        for name, blob in unpack_task(data if data is not None else self.dumps()).items():
            (d / name).write_bytes(blob)
            out.append(d / name)
        return out


def save_task(path: PathLike, doc, layer_store: Optional[ParamStore] = None, laser_type: int = 0,
              autosave_dir: Optional[PathLike] = None, is_break: int = 0,
              order: Optional[list[int]] = None, only_selected: bool = False) -> Path:
    """A250320_2 Save task (FUN_0048d250): drawing + layer table + contour order + breakpoint."""
    import tempfile
    from .chf import save_chf
    with tempfile.TemporaryDirectory() as td:
        cp = Path(td) / "tempGraph.chf"
        save_chf(doc, str(cp))
        chf = cp.read_bytes()
    a1 = a2 = None
    if autosave_dir is not None:
        ad = Path(autosave_dir)
        a1 = (ad / "AutosaveParam1.ini").read_bytes() if (ad / "AutosaveParam1.ini").exists() else None
        a2 = (ad / "AutosaveParam2.ini").read_bytes() if (ad / "AutosaveParam2.ini").exists() else None
    t = TaskFile(chf=chf, layer_xml=(layer_store or ParamStore()).dumps(),
                 manu=list(order if order is not None else range(len(doc.parts))),
                 only_selected=int(only_selected), autosave1=a1, autosave2=a2,
                 is_break=int(is_break), laser_type=int(laser_type))
    p = Path(path)
    if p.suffix.lower() != ".aut":
        p = p.with_suffix(".aut")
    p.write_bytes(t.dumps())
    return p


def load_task(path: PathLike, temp_dir: PathLike, laser_type: int = 0):
    """A250320_1 Import task (FUN_0048c550): unpack to ``temp_dir``, check the laser type
    (A250421_0), load the drawing; returns ``(document, TaskFile)``."""
    from .chf import load_chf
    data = Path(path).read_bytes()
    t = TaskFile.loads(data)
    if t.laser_type != int(laser_type):
        raise ValueError("A250421_0: the processing task does not match the current laser type")
    t.unpack_to(temp_dir, data)
    doc = load_chf(str(Path(temp_dir) / "tempGraph.chf"))
    return doc, t
