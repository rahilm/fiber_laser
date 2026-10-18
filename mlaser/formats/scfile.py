"""Small "scFlie" state files written by MainApp.exe with its own copy of CSerialEng
(MainApp ``FUN_00403ed0`` open/read, ``FUN_004043a0`` ReadInt, ``FUN_00404700`` open/write,
``FUN_004049b0`` WriteInt, ``FUN_00404820`` WriteEof; same rules as CADModule, see scio.py).

All of them are ``scFlie`` + one value per line + ``eof`` with CRLF line ends.

=====================  =====================================================================
file                   content (evidence)
=====================  =====================================================================
AutosaveParam1/2.ini   breakpoint ("resume cut") record, ``FUN_004693c0``: written only while
                       machining (app+0x484c == 2); a global counter DAT_009f2354 picks the file
                       (even -> 1, odd -> 2) so the two files alternate and the one with the
                       larger counter is the newest.  Fields: counter, X*1000, Y*1000 (rounded
                       to int, i.e. micrometres; position from ``FUN_00457b80``), then two ints
                       returned by NC interface vtable+0x278 (INFERRED: graph/contour index and
                       path-segment index of the interruption point).
ManuContour.dat        count + int list (``FUN_00457de0`` writes, ``FUN_0045e180`` reads):
                       indices of graphs processed so far / to resume (INFERRED).
tempIsBreak.ini        two ints read in ``FUN_0048c550``: [0] break flag (1 = an interrupted job
                       is saved in File/Temp), [1] laser type at save time, compared with app+0x4728;
                       a mismatch shows lang key ``A250421_0`` ("processing task does not match
                       the current laser type").
PithCompensate.pcf     leadscrew pitch-error compensation table (dialog ``FUN_0052e250`` saves via
                       vtable+0x2c4 of the motion interface).  Only an empty table (count 0) is
                       available, record layout UNKNOWN (dialog rows are 6 doubles: nominal pos,
                       pos-, pos+, err+, err-, backlash; INFERRED from ``FUN_0052e370``).
=====================  =====================================================================
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

from .scio import ScReader, ScWriter, split_lines, _strip_ws

PathLike = Union[str, Path]


# --------------------------------------------------------------------------- generic
def read_values(path_or_bytes: Union[PathLike, bytes]) -> list[str]:
    """Return the value lines between ``scFlie`` and ``eof`` (spaces stripped like ReadLine)."""
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else Path(path_or_bytes).read_bytes()
    r = ScReader(data)
    out = []
    while True:
        s = r.line()
        if s == "eof":
            return out
        out.append(s)


def write_values(values, path: PathLike | None = None) -> bytes:
    w = ScWriter()
    for v in values:
        if isinstance(v, bool):
            w.bool(v)
        elif isinstance(v, int):
            w.int(v)
        elif isinstance(v, float):
            w.double(v)
        else:
            w.str(str(v))
    w.eof()
    data = w.getvalue()
    if path is not None:
        Path(path).write_bytes(data)
    return data


# --------------------------------------------------------------------------- typed
@dataclass
class AutosaveParam:
    counter: int = 0          # DAT_009f2354 at save time
    x_um: int = 0             # machine X * 1000
    y_um: int = 0             # machine Y * 1000
    graph_index: int = 0      # INFERRED
    path_index: int = 0       # INFERRED

    @property
    def x(self) -> float:
        return self.x_um / 1000.0

    @property
    def y(self) -> float:
        return self.y_um / 1000.0

    @classmethod
    def load(cls, path: PathLike) -> "AutosaveParam":
        v = [int(s) for s in read_values(path)]
        v += [0] * (5 - len(v))
        return cls(*v[:5])

    def dumps(self) -> bytes:
        return write_values([self.counter, self.x_um, self.y_um, self.graph_index, self.path_index])

    def save(self, path: PathLike) -> None:
        Path(path).write_bytes(self.dumps())


def newest_autosave(dir_: PathLike) -> AutosaveParam | None:
    """Pick the newer of AutosaveParam1.ini / AutosaveParam2.ini (largest counter)."""
    best = None
    for n in ("AutosaveParam1.ini", "AutosaveParam2.ini"):
        p = Path(dir_) / n
        if p.exists():
            a = AutosaveParam.load(p)
            if best is None or a.counter > best.counter:
                best = a
    return best


@dataclass
class IntListFile:
    """count + ints (ManuContour.dat; also PithCompensate.pcf when empty)."""
    values: list[int] = field(default_factory=list)

    @classmethod
    def load(cls, path: PathLike) -> "IntListFile":
        v = read_values(path)
        n = int(v[0]) if v else 0
        return cls([int(x) for x in v[1:1 + n]])

    def dumps(self) -> bytes:
        return write_values([len(self.values), *self.values])

    def save(self, path: PathLike) -> None:
        Path(path).write_bytes(self.dumps())


ManuContour = IntListFile


@dataclass
class TempIsBreak:
    is_break: int = 0
    laser_type: int = 0      # compared with app+0x4728 (INFERRED: laser type)

    @classmethod
    def load(cls, path: PathLike) -> "TempIsBreak":
        v = [int(s) for s in read_values(path)] + [0, 0]
        return cls(v[0], v[1])

    def dumps(self) -> bytes:
        return write_values([self.is_break, self.laser_type])

    def save(self, path: PathLike) -> None:
        Path(path).write_bytes(self.dumps())


@dataclass
class PitchCompensation:
    """PithCompensate.pcf. Only ``count`` is known; any further lines are kept verbatim."""
    count: int = 0
    raw: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: PathLike) -> "PitchCompensation":
        v = read_values(path)
        return cls(int(v[0]) if v else 0, v[1:])

    def dumps(self) -> bytes:
        return write_values([self.count, *self.raw])

    def save(self, path: PathLike) -> None:
        Path(path).write_bytes(self.dumps())


# --------------------------------------------------------------------------- INI-ish
def read_ini(path: PathLike, encoding: str = "gbk") -> dict[str, dict[str, str]]:
    """Minimal Windows-profile reader (GetPrivateProfile* semantics: case-insensitive, first wins).

    Used for softPara.ini ([SC2000] NormalExit, X/Y/Z/WAxis in micrometres), JumpAddTime.txt,
    LanFormatEStr.txt, CustomerInformation.ini, ipAdd.ini.
    """
    out: dict[str, dict[str, str]] = {}
    sec = None
    for ln in Path(path).read_bytes().decode(encoding, "replace").splitlines():
        s = ln.strip()
        if s.startswith("[") and "]" in s:
            sec = s[1:s.index("]")]
            out.setdefault(sec, {})
        elif "=" in s and sec is not None:
            k, v = s.split("=", 1)
            out[sec].setdefault(k.strip(), v.strip())
    return out


def write_ini(data: dict[str, dict[str, str]], path: PathLike, encoding: str = "gbk") -> None:
    lines = []
    for sec, kv in data.items():
        lines.append(f"[{sec}]")
        lines += [f"{k}={v}" for k, v in kv.items()]
    Path(path).write_bytes(("\r\n".join(lines) + "\r\n").encode(encoding, "replace"))


@dataclass
class SoftPara:
    """File/softPara.ini, [SC2000] (MainApp ``FUN_00596320`` reads at start-up, ``FUN_00481c70`` writes the axes, ``FUN_0047e150`` sets NormalExit=1).

    NormalExit is set to 0 at start-up and back to 1 on a clean exit; X/Y/Z/WAxis are the
    machine positions at exit as integers in micrometres (read with GetPrivateProfileInt into
    the per-axis position table app+0xbccc+axis*0xa0).
    """
    normal_exit: int = 1
    x_um: int = 0
    y_um: int = 0
    z_um: int = 0
    w_um: int = 0

    @classmethod
    def load(cls, path: PathLike) -> "SoftPara":
        s = {k.lower(): v for k, v in read_ini(path).get("SC2000", {}).items()}
        g = lambda k, d: int(s.get(k, d))
        return cls(g("normalexit", 1), g("xaxis", 0), g("yaxis", 0), g("zaxis", 0), g("waxis", 0))

    def save(self, path: PathLike) -> None:
        write_ini({"SC2000": {"NormalExit": str(self.normal_exit), "XAxis": str(self.x_um),
                              "YAxis": str(self.y_um), "ZAxis": str(self.z_um),
                              "WAxis": str(self.w_um)}}, path)


@dataclass
class JumpAddTime:
    """JumpAddTime.txt (despite the name, an INI file).

    [Jump] AddTime (default 100, ms, MainApp ``FUN_004657e0``: added *0.001 s to the computed jump
    time), [Axis4Freq] Is4Freq (default 0, MainApp), [Arc2SegVelK] K_X/K_Y (default 100, scaled
    *0.01 in CADModule ``FUN_101041d0``), [LimitSamllCircleVel] IsLimit/SlowRatio
    (present in the shipped file but not referenced by any binary).
    """
    add_time_ms: int = 100
    is_4freq: int = 0
    limit_small_circle: int = 0
    slow_ratio: int = 3
    k_x: int = 100
    k_y: int = 100

    @classmethod
    def load(cls, path: PathLike) -> "JumpAddTime":
        d = {s.lower(): {k.lower(): v for k, v in kv.items()} for s, kv in read_ini(path).items()}
        g = lambda s, k, dv: int(d.get(s, {}).get(k, dv))
        return cls(g("jump", "addtime", 100), g("axis4freq", "is4freq", 0),
                   g("limitsamllcirclevel", "islimit", 0), g("limitsamllcirclevel", "slowratio", 3),
                   g("arc2segvelk", "k_x", 100), g("arc2segvelk", "k_y", 100))


def read_calib_csv(path: PathLike) -> list[tuple[int, int]]:
    """Help/calib.csv: capacitive height-sensor calibration export (MainApp ``FUN_00633da0``,
    ``fprintf("%d, %d\\n")`` over the pairs returned by the height-controller interface vtable+0x1b8,
    then opens the folder in Explorer).  Columns: (height, raw capacitance count); INFERRED units
    micrometres / sensor counts."""
    out = []
    for ln in Path(path).read_text("ascii", "replace").splitlines():
        if "," in ln:
            a, b = ln.split(",", 1)
            out.append((int(a), int(b)))
    return out


def write_calib_csv(rows, path: PathLike) -> None:
    Path(path).write_bytes("".join("%d, %d\r\n" % (a, b) for a, b in rows).encode())
