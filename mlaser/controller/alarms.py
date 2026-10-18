"""MCC100 alarm words and their messages (docs/protocol.md §11).

Observed in MainApp.exe ``FUN_00598280`` (status timer, disassembly 0x59a265..0x59a5cf):

* **RO[6]** (reg 1006, CNCModule slot +0x228 = getReg(0, 6)) is alarm word 1. For bit ``b``:

  - ``b < 16`` (and ``b == 24``): axis ``b`` has an axis alarm.  The PC then reads that axis'
    status word (getReg(3, b*10) = axis RO record ``b`` word 0, reg 2000+10b) and raises one
    alarm per set bit ``k`` (0..5) with code ``8100 + 32*b + k``.  ``b`` in 16..23 is handled the
    same way.  The text table at 0x00a4f4d0 (6 entries) is ``EtherCATAxisErrorInfo_k``:
    0 hard +limit, 1 hard -limit, 2 soft +limit, 3 soft -limit, 4 servo input alarm,
    5 dual-drive alarm ("%s ..." with the axis name).
  - otherwise code ``8000 + b``; text table at 0x00a4f070 (32 entries), of which the
    non-empty ones are 25 ``EtherCATErrorInfo_1_25`` bus fault, 26 ``_1_26`` output fault,
    29 ``_1_29`` servo input alarm, 30 ``_1_30`` emergency stop.
* **RO[7]** (reg 1007, slot +0x22c) is alarm word 2, bits 0..7 -> code ``9000 + b``, text table at
  0x00a4f3f0: ``EtherCATErrorInfo_2_00`` illegal command, ``_2_01`` interpolation data length,
  ``_2_02`` axis command failed, ``_2_03`` FTC command failed, ``_2_04`` PLC command failed,
  ``_2_05`` **FIFO starvation**, ``A250822_0`` immediate-command error, ``A250822_1`` overspeed.
* Any non-zero word marks "alarm present" (app+0x1e88 = -1) and, while a job runs, MainApp
  pauses/stops it (``FUN_005b9a60`` "Sys Pause" / ``FUN_005ba0d0`` OnStopBtn).
* Reset ("gp90" Clear Alarm, ``FUN_004b8690``) = CNCModule +0x1e4 = ``FUN_100315f0`` =
  mailbox ``[9999, 5, 0, 0]``.  Servo alarm codes 1..4 additionally offer a go-origin
  (``mf223``), dual-drive codes 9..12 ``mf435``.

The legacy table of the older firmware (MainApp static initialiser ``FUN_006f29e0``: ``gp1`` ..
``gp32`` = X/Y1/Y2/W servo, encoder, dual drive, network, FPGA, e-stop, hard/soft limits; ``gp33``..
follower; ``gp44``.. laser) is kept in :data:`LEGACY_KEYS` for reference; the decode above is the
one this build runs.

The English/Chinese texts below are the ``Lang/lang.txt`` entries for those keys (UTF-16,
``key#中文#English``); the UI translates the key when it has a translator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

REG_ALARM1 = 1006          # RO[6]
REG_ALARM2 = 1007          # RO[7]
RO_ALARM1 = 6
RO_ALARM2 = 7

CODE_WORD1 = 8000          # 8000 + bit
CODE_AXIS = 8100           # 8100 + 32*axis + kind
CODE_WORD2 = 9000          # 9000 + bit

# axis status word bits (axis RO k0) raised as per-axis alarms
AX_HARD_POS, AX_HARD_NEG, AX_SOFT_POS, AX_SOFT_NEG, AX_SERVO, AX_DUAL = range(6)
AXIS_ALARM_MASK = 0x3F

AXIS_KEYS = [f"EtherCATAxisErrorInfo_{k}" for k in range(6)]
AXIS_TEXT = ["%s hard positive limit alarm", "%s hard negative limit alarm",
             "%s soft positive limit alarm", "%s soft negative limit alarm",
             "%s servo input alarm", "%s dual drive alarm"]

WORD1_KEYS = {25: "EtherCATErrorInfo_1_25", 26: "EtherCATErrorInfo_1_26",
              29: "EtherCATErrorInfo_1_29", 30: "EtherCATErrorInfo_1_30"}
WORD1_TEXT = {25: "Bus Fault", 26: "Output Fault", 29: "Servo input alarm",
              30: "Emergency stop alarm"}
W1_BUS_FAULT, W1_OUTPUT_FAULT, W1_SERVO, W1_ESTOP = 25, 26, 29, 30

WORD2_KEYS = ["EtherCATErrorInfo_2_00", "EtherCATErrorInfo_2_01", "EtherCATErrorInfo_2_02",
              "EtherCATErrorInfo_2_03", "EtherCATErrorInfo_2_04", "EtherCATErrorInfo_2_05",
              "A250822_0", "A250822_1"]
WORD2_TEXT = ["Illegal command", "The interpolation data content length is abnormal",
              "Axis control command execution exception", "FTC order execution exception",
              "PLC command execution exception", "FIFO starvation",
              "Immediate instruction exception", "Overspeed"]
W2_FIFO_STARVATION = 5
W2_OVERSPEED = 7

# Older table (gp<n>, n = legacy alarm code); not decoded from a word in this build.
LEGACY_KEYS = {n: f"gp{n}" for n in range(1, 57)}

DEFAULT_AXIS_NAMES = {0: "X", 1: "Y1", 2: "Y2", 3: "W", 4: "Z"}


@dataclass(frozen=True)
class Alarm:
    """One active alarm (MainApp alarm code numbering)."""
    code: int
    key: str                   # lang.txt key
    text_en: str               # English text from lang.txt (axis name substituted)
    axis: Optional[int] = None
    kind: str = "card"         # card | axis | estop | limit | servo | fifo | overspeed

    def text(self, tr=None) -> str:
        """Translated text; ``tr`` is any object with ``tr(key)`` (falls back to English)."""
        if tr is None:
            return self.text_en
        try:
            if hasattr(tr, "format"):     # mlaser.ui.i18n.Translator: named {axis} placeholder
                s = tr.format(self.key, axis=self._axis_name)
            else:
                s = tr.tr(self.key)
        except Exception:  # noqa: BLE001
            return self.text_en
        if not s or s == self.key:
            return self.text_en
        if "%s" in s:
            s = s.replace("%s", self._axis_name)
        return s

    @property
    def _axis_name(self) -> str:
        return DEFAULT_AXIS_NAMES.get(self.axis, f"axis {self.axis}") if self.axis is not None else ""


def _axis_kind(k: int) -> str:
    return "limit" if k <= AX_SOFT_NEG else ("servo" if k == AX_SERVO else "axis")


def decode(word1: int, word2: int, axis_status: Sequence[int] = (),
           axis_names: Optional[dict] = None) -> list[Alarm]:
    """Decode RO[6]/RO[7] (+ axis status words) the way FUN_00598280 does."""
    names = dict(DEFAULT_AXIS_NAMES)
    if axis_names:
        names.update(axis_names)
    out: list[Alarm] = []
    w1, w2 = word1 & 0xFFFFFFFF, word2 & 0xFFFFFFFF
    for b in range(32):
        if not (w1 >> b) & 1:
            continue
        if b < 24 or b == 24:
            st = axis_status[b] if b < len(axis_status) else 0
            for k in range(6):
                if (st >> k) & 1:
                    nm = names.get(b, f"axis {b}")
                    out.append(Alarm(CODE_AXIS + 32 * b + k, AXIS_KEYS[k], AXIS_TEXT[k] % nm,
                                     axis=b, kind=_axis_kind(k)))
        else:
            kind = {W1_ESTOP: "estop", W1_SERVO: "servo"}.get(b, "card")
            out.append(Alarm(CODE_WORD1 + b, WORD1_KEYS.get(b, f"EtherCATErrorInfo_1_{b:02d}"),
                             WORD1_TEXT.get(b, f"Card alarm 1.{b}"), kind=kind))
    for b in range(8):
        if (w2 >> b) & 1:
            kind = {W2_FIFO_STARVATION: "fifo", W2_OVERSPEED: "overspeed"}.get(b, "card")
            out.append(Alarm(CODE_WORD2 + b, WORD2_KEYS[b], WORD2_TEXT[b], kind=kind))
    return out


def from_status(st) -> list[Alarm]:
    """Decode an :class:`mlaser.controller.client.MCStatus`."""
    return decode(st.ro[RO_ALARM1], st.ro[RO_ALARM2], [a.status for a in st.axes])


def diff(old: Iterable[Alarm], new: Iterable[Alarm]) -> tuple[list[Alarm], list[Alarm]]:
    """(raised, cleared) between two alarm lists (gp88 "Alarm" / gp88-1 "Alarm Clear")."""
    o = {a.code: a for a in old}
    n = {a.code: a for a in new}
    return [a for c, a in n.items() if c not in o], [a for c, a in o.items() if c not in n]
