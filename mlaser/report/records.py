"""Process statistics records and the files the original keeps them in.

Evidence (docs/diag.md, section "Reports"):

* MainApp keeps a vector of per-job statistic records at cfg+0x43e4.  At job completion the
  status timer FUN_00598280 stamps the end time into the newest record (+0x40), increments its
  run count (+0x48) and calls FUN_004c5380, which appends the record to
  ``Report\\TotalReport.txt`` (O, MainApp 0x4c5380, literal 0x85ae50).
* "Work Report [New]/[All]" (mf252/mf253, ids 0x3aa6/0x3aa7, handler FUN_004b96c0) rewrites
  ``Report\\report.txt`` with the newest record (0x3aa6) or all records (0x3aa7), renders a
  thumbnail ``Report\\<name>.jpg`` per record, writes ``Report\\lang.txt`` (``lang==0`` Chinese,
  ``lang==1`` otherwise) and launches ``Report\\report.exe`` (O, strings 0x85a0d8..0x85a214).
  Each launch is also logged to ``Report\\LogReport.txt`` as ``<timestamp>  <record line>``.
* Record line (report.txt / TotalReport.txt, observed in the shipped files and in the format
  strings 0x85a100 ``,%.2fx%.2f mm``, 0x85a144 ``,%.2f m``, 0x85a184 ``,%.2f Sec``)::

      name,W x H mm,start time,cut m,move m,pierces,duration,cut s Sec,move s Sec,pierce s Sec

  The duration uses lang key ``gp146`` (``%d分%02d秒`` / ``%02dMin%02dSec``), so its text
  depends on the UI language.  report.exe accepts only lines with exactly 10 comma fields
  (report.exe FUN_00407220).  The size separator is ``x`` in report.txt and ``×`` in
  TotalReport.txt; both are accepted.  report.txt lines end in ``\\r\\r\\n`` (text-mode CRLF
  written through a CRLF string), TotalReport/LogReport in ``\\r\\n``.  Encoding UTF-8.
* ``File\\ProcessesStatistic.txt`` (shipped sample ``未命名-1,0.0X0.0mm,0mm,0mm,0,0秒,1970-01-01
  08:00:00``): ``name,WXHmm,cut mm,move mm,pierces,<s>秒,start``, GBK (INFERRED: the
  persisted copy of the newest record, written by FUN_005e6d40).
* Older LogReport lines (2024) use the 7-field layout ``name,WxHmm,cut m,move m,pierces,
  duration,start``; :func:`parse_line` reads both.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional

TIME_FMT = "%Y-%m-%d %H:%M:%S"        # literal 0x85a11c
EPOCH0 = "1970-01-01 08:00:00"        # what the original writes for a never-started record
UNTITLED = "未命名-1"                  # the original's untitled document name (shipped files)
DUR_ZH = "%d分%02d秒"                  # gp146 zh
DUR_EN = "%02dMin%02dSec"             # gp146 en


@dataclass
class JobRecord:
    """One process-statistics record (MainApp cfg+0x43e4 vector element)."""
    name: str = UNTITLED
    width: float = 0.0                 # mm (record +0x20 double)
    height: float = 0.0                # mm (record +0x28 double)
    start: str = EPOCH0                # "%Y-%m-%d %H:%M:%S"
    cut_m: float = 0.0                 # record +0x30 int * 0.001
    move_m: float = 0.0                # record +0x34 int * 0.001
    pierces: int = 0
    duration_s: float = 0.0            # record +0x3c in 0.1 s
    cut_s: float = 0.0                 # estimated cutting time
    move_s: float = 0.0                # estimated empty-move time
    pierce_s: float = 0.0              # estimated piercing time
    end: str = ""                      # record +0x40 (completion time), not in the line
    count: int = 0                     # record +0x48 (completions)
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ text
    def duration_text(self, lang: str = "zh") -> str:
        s = int(round(self.duration_s))
        fmt = DUR_ZH if lang == "zh" else DUR_EN
        return fmt % (s // 60, s % 60)

    def size_text(self, sep: str = "x") -> str:
        return f"{self.width:.2f}{sep}{self.height:.2f} mm"

    def to_line(self, lang: str = "zh", size_sep: str = "x") -> str:
        """report.txt / TotalReport.txt line (without the line end)."""
        return (f"{self.name},{self.size_text(size_sep)},{self.start},{self.cut_m:.2f} m,"
                f"{self.move_m:.2f} m,{self.pierces},{self.duration_text(lang)},"
                f"{self.cut_s:.2f} Sec,{self.move_s:.2f} Sec,{self.pierce_s:.2f} Sec")

    def fields(self, lang: str = "zh") -> list[str]:
        """The 10 report.exe table columns after the check column (header 0x40da18)."""
        return self.to_line(lang).split(",")

    def to_statistic_line(self) -> str:
        """File/ProcessesStatistic.txt layout (shipped sample)."""
        return (f"{self.name},{self.width:.1f}X{self.height:.1f}mm,{self.cut_m * 1000:.0f}mm,"
                f"{self.move_m * 1000:.0f}mm,{self.pierces},{int(round(self.duration_s))}秒,{self.start}")

    @property
    def total_est_s(self) -> float:
        return self.cut_s + self.move_s + self.pierce_s


_NUM = r"(-?[0-9.eE+]+|-?inf|nan)"
_SIZE = re.compile(r"\s*" + _NUM + r"\s*[x×X]\s*" + _NUM + r"\s*mm\s*$")


def _num(text: str) -> float:
    m = re.search(r"-?[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?", text)
    if not m:
        return 0.0
    try:
        v = float(m.group())
    except ValueError:
        return 0.0
    # the shipped files contain garbage like -9.2e61 for never-initialised doubles
    return v if abs(v) < 1e12 else 0.0


def parse_duration(text: str) -> float:
    """``0分43秒`` / ``01Min05Sec`` / ``12秒`` -> seconds."""
    nums = [int(n) for n in re.findall(r"\d+", text)]
    if len(nums) >= 2:
        return nums[0] * 60 + nums[1]
    return float(nums[0]) if nums else 0.0


def _size(text: str) -> tuple[float, float]:
    m = _SIZE.match(text.replace(" ", " "))
    if not m:
        parts = re.split(r"[x×X]", text)
        if len(parts) >= 2:
            return _num(parts[0]), _num(parts[1])
        return 0.0, 0.0
    return _num(m.group(1)), _num(m.group(2))


def parse_line(line: str) -> Optional[JobRecord]:
    """Parse a record line (10-field current layout or the 7-field 2024 layout, with or
    without the LogReport ``<timestamp>  `` prefix).  Returns None for anything else."""
    line = line.strip("\r\n").strip()
    if not line:
        return None
    m = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s{2}(.*)$", line)
    stamp = None
    if m:
        stamp, line = m.group(1), m.group(2)
    f = [x.strip() for x in line.split(",")]
    if len(f) == 10:
        w, h = _size(f[1])
        r = JobRecord(f[0], w, h, f[2], _num(f[3]), _num(f[4]), int(_num(f[5])),
                      parse_duration(f[6]), _num(f[7]), _num(f[8]), _num(f[9]))
    elif len(f) == 7:
        w, h = _size(f[1])
        r = JobRecord(f[0], w, h, f[6], _num(f[2]), _num(f[3]), int(_num(f[4])), parse_duration(f[5]))
    else:
        return None
    if stamp:
        r.extra["logged"] = stamp
    return r


def parse_statistic_line(line: str) -> Optional[JobRecord]:
    """File/ProcessesStatistic.txt line."""
    f = [x.strip() for x in line.strip("\r\n").split(",")]
    if len(f) != 7:
        return None
    w, h = _size(f[1].replace("mm", " mm"))
    return JobRecord(f[0], w, h, f[6], _num(f[2]) / 1000.0, _num(f[3]) / 1000.0, int(_num(f[4])),
                     parse_duration(f[5]))


def read_records(path: str) -> list[JobRecord]:
    """All parseable records of a report.txt / TotalReport.txt / LogReport.txt file."""
    if not os.path.exists(path):
        return []
    raw = open(path, "rb").read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gbk", "replace")
    out = []
    for ln in text.replace("\r", "\n").split("\n"):
        r = parse_line(ln)
        if r is not None:
            out.append(r)
    return out


def now_text(t: Optional[_dt.datetime] = None) -> str:
    return (t or _dt.datetime.now()).strftime(TIME_FMT)


class ReportStore:
    """The ``Report`` and ``File`` directories of one installation (rebuild data dir).

    ``report.txt``      current selection handed to the viewer (rewritten per launch)
    ``LogReport.txt``   one ``<timestamp>  <line>`` per viewer launch
    ``TotalReport.txt`` history: one line per completed job
    ``File/ProcessesStatistic.txt`` newest record (GBK)
    """

    def __init__(self, root: str, lang: str = "zh"):
        self.root = root
        self.lang = lang
        self.report_dir = os.path.join(root, "Report")
        self.file_dir = os.path.join(root, "File")

    # paths
    @property
    def report_txt(self) -> str:
        return os.path.join(self.report_dir, "report.txt")

    @property
    def log_report(self) -> str:
        return os.path.join(self.report_dir, "LogReport.txt")

    @property
    def total_report(self) -> str:
        return os.path.join(self.report_dir, "TotalReport.txt")

    @property
    def statistic_file(self) -> str:
        return os.path.join(self.file_dir, "ProcessesStatistic.txt")

    def thumbnail_path(self, name: str) -> str:
        """``Report\\<name>.jpg`` (report.exe builds ``dir + "/" + name + ".jpg"``, 0x40dce8)."""
        return os.path.join(self.report_dir, f"{name}.jpg")

    # history
    def history(self) -> list[JobRecord]:
        return read_records(self.total_report)

    def append_history(self, rec: JobRecord) -> None:
        """FUN_004c5380: append the finished job to TotalReport.txt (size separator ``×``)."""
        os.makedirs(self.report_dir, exist_ok=True)
        with open(self.total_report, "ab") as f:
            f.write((rec.to_line(self.lang, "×") + "\r\n").encode("utf-8"))
        self.write_statistic(rec)

    def write_statistic(self, rec: JobRecord) -> None:
        os.makedirs(self.file_dir, exist_ok=True)
        with open(self.statistic_file, "wb") as f:
            f.write(rec.to_statistic_line().encode("gbk", "replace"))

    def read_statistic(self) -> Optional[JobRecord]:
        if not os.path.exists(self.statistic_file):
            return None
        return parse_statistic_line(open(self.statistic_file, "rb").read().decode("gbk", "replace"))

    # viewer hand-off
    def write_report_txt(self, records: Iterable[JobRecord]) -> list[JobRecord]:
        """FUN_004b96c0: rewrite report.txt, append the launch to LogReport.txt and write
        ``lang.txt`` for the viewer."""
        recs = list(records)
        os.makedirs(self.report_dir, exist_ok=True)
        stamp = now_text()
        with open(self.report_txt, "wb") as f:
            for r in recs:
                f.write((r.to_line(self.lang) + "\r\r\n").encode("utf-8"))
        with open(self.log_report, "ab") as f:
            for r in recs:
                f.write((f"{stamp}  {r.to_line(self.lang)}\r\n").encode("utf-8"))
        with open(os.path.join(self.report_dir, "lang.txt"), "wb") as f:
            f.write(b"lang==0" if self.lang == "zh" else b"lang==1")
        return recs

    def read_report_txt(self) -> list[JobRecord]:
        return read_records(self.report_txt)

    def rewrite_report_txt(self, records: Iterable[JobRecord]) -> None:
        """After a viewer delete (report.exe FUN_00403b80 rewrites report.txt)."""
        os.makedirs(self.report_dir, exist_ok=True)
        with open(self.report_txt, "wb") as f:
            for r in records:
                f.write((r.to_line(self.lang) + "\r\r\n").encode("utf-8"))

    def viewer_lang(self) -> str:
        p = os.path.join(self.report_dir, "lang.txt")
        try:
            return "zh" if open(p, "rb").read().strip() == b"lang==0" else "en"
        except OSError:
            return self.lang


def totals(records: Iterable[JobRecord]) -> dict:
    """Statistics & billing sums (report.exe countFee: cumulative empty-move length,
    pierce count, cut length, processing time)."""
    t = dict(cut_m=0.0, move_m=0.0, pierces=0, time_s=0.0, jobs=0)
    for r in records:
        t["cut_m"] += r.cut_m
        t["move_m"] += r.move_m
        t["pierces"] += r.pierces
        t["time_s"] += r.duration_s
        t["jobs"] += 1
    return t


def cost(tot: dict, price_cut: float = 0.0, price_move: float = 0.0, price_pierce: float = 0.0,
         price_time: float = 0.0, base: float = 0.0) -> float:
    """Total cost = sum of quantity x unit price (report.exe ``cost_cal``; the exact formula is
    INFERRED from the dialog: four quantities each with a price field + total)."""
    return (base + tot["cut_m"] * price_cut + tot["move_m"] * price_move
            + tot["pierces"] * price_pierce + tot["time_s"] * price_time)


__all__ = ["JobRecord", "ReportStore", "parse_line", "parse_statistic_line", "read_records",
           "parse_duration", "totals", "cost", "now_text", "replace", "UNTITLED", "EPOCH0"]
