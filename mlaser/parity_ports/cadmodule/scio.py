"""Low-level line reader/writer shared by every Mlaser "scFlie" text file.

Mirrors the ``CSerialEng`` class in CADModule.dll (vftable ``CSerialEng::vftable``):

====================  ==========  ===========================================================
operation             address     behaviour
====================  ==========  ===========================================================
open for read         100e7b00    whole file read; first line must equal ``scFlie`` (err 5);
                                  file must end with ``eof`` (trailing bytes <= 0x0d ignored)
ReadLine              100e7350    reads up to CR or LF, swallows one following CR/LF, and
                                  **drops every space and TAB** in the line
ReadInt               100e7770    line must match ``[-0-9]*`` then ``atoi``
ReadDouble            100e77b0    line must match ``[-.0-9]*`` (<2 dots) then ``atof``
ReadBool              100e77f0    int-like line, true iff the line is exactly ``1``
ReadPoint             100e7840    ``x,y`` -> two ``atof``
ReadWString           100e7d30    ANSI (CP_ACP, i.e. GBK on Chinese Windows) -> UTF-16
check eof             100e76f0    reads one line, must be ``eof``
open for write        100e7e10    ``_wfopen(path, "wb")`` then writes ``scFlie``
WriteStr              100e7220    ``"%s\\r\\n"``
WriteTagInt           100e7460    ``"%s%d\\r\\n"`` (e.g. ``####graph NO:1``)
WriteInt              100e74d0    ``"%d\\r\\n"``
WriteDouble           100e7530    ``|x|<1e-10`` -> ``0.0``; ``|x|<1e-6`` -> ``%12.10f``; else ``%f``
                                  (thresholds DAT_10122268=1e-10, DAT_1011fe30=1e-6)
WriteBool             100e7630    ``1`` / ``0``
WritePoint            100e7680    ``"%f,%f\\r\\n"`` (no small-value rule)
WriteWString          100e7a30    UTF-16 -> CP_ACP
WriteEof              100e73f0    ``eof``
====================  ==========  ===========================================================
"""
from __future__ import annotations

from typing import Optional

__port__ = {
    "source": "CADModule",
    "class": "CSerialEng (the scFlie text format)",
    "functions": {"100e7b00": "ScReader (open for read)", "100e7350": "ScReader.line",
                  "100e7770": "ScReader.int", "100e77b0": "ScReader.double",
                  "100e77f0": "ScReader.bool", "100e7840": "ScReader.point",
                  "100e7d30": "ScReader.wstring_raw", "100e76f0": "ScReader.eof",
                  "100e7e10": "ScWriter (open for write)", "100e7220": "ScWriter.str",
                  "100e7460": "ScWriter.tag_int", "100e74d0": "ScWriter.int",
                  "100e7530": "ScWriter.double", "100e7630": "ScWriter.bool",
                  "100e7680": "ScWriter.point"},
    "parity": ["chf"],
}

ENCODING = "gbk"   # CP_ACP of the Chinese Windows builds the samples come from (INFERRED)


class ScFormatError(ValueError):
    pass


def _strip_ws(b: bytes) -> bytes:
    return b.replace(b" ", b"").replace(b"\t", b"")


def split_lines(data: bytes) -> list[bytes]:
    """Split like ReadLine (100e7350): a line ends at CR or LF; one extra CR/LF is swallowed."""
    out: list[bytes] = []
    i, n = 0, len(data)
    while i < n:
        j = i
        while j < n and data[j] not in (13, 10):
            j += 1
        out.append(data[i:j])
        if j >= n:
            break
        j += 1
        if j < n and data[j] in (13, 10):
            j += 1
        i = j
    return out


def fmt_double(x: float) -> str:
    """WriteDouble (100e7530) formatting."""
    a = abs(x)
    if a < 1e-10:
        return "0.0"
    if a < 1e-6:
        return "%12.10f" % x
    return "%f" % x


class ScReader:
    def __init__(self, data: bytes, check_header: bool = True):
        self.lines = split_lines(data)
        self.pos = 0
        if check_header:
            if not self.lines or _strip_ws(self.lines[0]) != b"scFlie":
                raise ScFormatError("not an scFlie file (header line != 'scFlie')")
            self.pos = 1

    # -- primitive reads ---------------------------------------------------
    def raw(self) -> bytes:
        if self.pos >= len(self.lines):
            raise ScFormatError("unexpected end of file")
        b = self.lines[self.pos]
        self.pos += 1
        return b

    def line(self) -> str:
        return _strip_ws(self.raw()).decode(ENCODING, "surrogateescape")

    def peek(self) -> Optional[str]:
        if self.pos >= len(self.lines):
            return None
        return _strip_ws(self.lines[self.pos]).decode(ENCODING, "surrogateescape")

    def tag(self, expected: Optional[str] = None) -> str:
        """Skip one line (the original reader never checks tag text)."""
        s = self.line()
        if expected is not None and s != expected.replace(" ", ""):
            raise ScFormatError(f"line {self.pos}: expected {expected!r}, got {s!r}")
        return s

    def int(self) -> int:
        s = self.line()
        try:
            return int(s)
        except ValueError:
            raise ScFormatError(f"line {self.pos}: expected int, got {s!r}") from None

    def double(self) -> float:
        s = self.line()
        try:
            return float(s)
        except ValueError:
            raise ScFormatError(f"line {self.pos}: expected double, got {s!r}") from None

    def bool(self) -> bool:
        s = self.line()
        if s not in ("0", "1") and not s.lstrip("-").isdigit():
            raise ScFormatError(f"line {self.pos}: expected bool, got {s!r}")
        return s == "1"

    def point(self) -> tuple[float, float]:
        s = self.line()
        try:
            a, b = s.split(",")
            return (float(a), float(b))
        except ValueError:
            raise ScFormatError(f"line {self.pos}: expected point, got {s!r}") from None

    def wstring_raw(self) -> str:
        """Text line *without* the space stripping (the original strips; we keep fidelity)."""
        return self.raw().decode(ENCODING, "surrogateescape")

    def skip(self, n: int) -> list[str]:  # type: ignore[valid-type]
        return [self.raw().decode(ENCODING, "surrogateescape") for _ in range(n)]

    def eof(self) -> None:
        s = self.line()
        if s != "eof":
            raise ScFormatError(f"line {self.pos}: expected 'eof', got {s!r}")


class ScWriter:
    def __init__(self, header: bool = True):
        self.parts: list[str] = []
        if header:
            self.str("scFlie")

    def str(self, s: str) -> None:
        self.parts.append(s + "\r\n")

    def tag_int(self, s: str, n: int) -> None:  # type: ignore[valid-type]
        self.parts.append("%s%d\r\n" % (s, n))

    def int(self, n: int) -> None:
        self.parts.append("%d\r\n" % int(n))

    def double(self, x: float) -> None:
        self.parts.append(fmt_double(float(x)) + "\r\n")

    def bool(self, b: bool) -> None:
        self.parts.append("1\r\n" if b else "0\r\n")

    def point(self, p) -> None:
        self.parts.append("%f,%f\r\n" % (float(p[0]), float(p[1])))

    def eof(self) -> None:
        self.str("eof")

    def getvalue(self) -> bytes:
        return "".join(self.parts).encode(ENCODING, "surrogateescape")
