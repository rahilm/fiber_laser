"""Run log files ("运行日志" mf124, "Open Log Directory" mf431, "Open Dog Log" mf431-1).

Observed:

* mf431 (id 0x36b5, FUN_004bae30) runs ``explorer.exe <CSIDL_LOCAL_APPDATA>\\NexCut\\Log\\``
  (SHGetSpecialFolderPathA(.., 0x1c) in FUN_004cc320, literal 0x85a254);
* mf431-1 (id 0x36b8, FUN_004baf70) opens ``<exe dir>\\Log\\Code.txt`` with explorer;
  Code.txt is the dongle / "Check code" log of the licensing code, which the rebuild does not
  have (brief: licensing omitted), so the viewer only shows such a file when one exists;
* log lines (``Log\\2026-09-16.log``, one file per day, GBK)::

      2026-09-16 13:34:41.379 [Info] -> MC-RecvErr_selectFunc ErrCode:10060 ...

  levels ``Info``/``Warning``/``Error`` (strings 0x86262c/0x86263c), session marker
  ``--- Start Sys ---`` (0x862680) / ``---...NC Start...---``.

The rebuild writes the UI log in the same line format (UTF-8) to ``<data dir>/Log/<date>.log``
(:class:`LogFileWriter`) and the viewer lists ``*.log`` / ``*.txt`` of every log directory.
"""
from __future__ import annotations

import datetime as _dt
import glob
import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional

LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?) \[(\w+)\] -> (.*)$")


def decode(raw: bytes) -> str:
    """Log files are GBK in the original, UTF-8 in the rebuild."""
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


@dataclass
class LogLine:
    time: str
    level: str
    text: str


def parse_log(text: str) -> list[LogLine]:
    out = []
    for ln in text.splitlines():
        m = LINE_RE.match(ln.strip("\r"))
        if m:
            out.append(LogLine(m.group(1), m.group(2), m.group(3)))
        elif ln.strip() and out:
            out[-1] = LogLine(out[-1].time, out[-1].level, out[-1].text + "\n" + ln)
        elif ln.strip():
            out.append(LogLine("", "", ln))
    return out


def read_log(path: str) -> str:
    with open(path, "rb") as f:
        return decode(f.read())


def list_logs(dirs: Iterable[str]) -> list[str]:
    """``*.log`` and ``*.txt`` (Code.txt, VelDecc.txt) of the given directories, newest first."""
    files: list[str] = []
    for d in dirs:
        if d and os.path.isdir(d):
            for pat in ("*.log", "*.txt"):
                files.extend(glob.glob(os.path.join(d, pat)))
    files = sorted(set(files), key=lambda p: (os.path.getmtime(p), p), reverse=True)
    return files


def filter_lines(lines: list[LogLine], text: str = "", level: str = "") -> list[LogLine]:
    t = text.lower()
    return [ln for ln in lines if (not t or t in ln.text.lower()) and (not level or ln.level == level)]


class LogFileWriter:
    """Daily log file ``<dir>/<YYYY-MM-DD>.log`` in the original line format."""

    def __init__(self, directory: str, clock=_dt.datetime.now):
        self.dir = directory
        self.clock = clock

    def path(self, day: Optional[_dt.date] = None) -> str:
        d = day or self.clock().date()
        return os.path.join(self.dir, f"{d:%Y-%m-%d}.log")

    def write(self, msg: str, level: str = "Info") -> None:
        now = self.clock()
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.path(now.date()), "a", encoding="utf-8") as f:
                for i, ln in enumerate(str(msg).splitlines() or [""]):
                    stamp = f"{now:%Y-%m-%d %H:%M:%S}.{now.microsecond // 1000:03d}"
                    f.write(f"{stamp} [{level}] -> {ln}\n" if i == 0 else f"    {ln}\n")
        except OSError:
            pass

    def session_start(self) -> None:
        self.write("--- Start Sys ---")
