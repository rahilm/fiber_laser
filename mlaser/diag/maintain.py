"""Machine maintenance reminders ("机床保养" Maintain A251113_0, command id 0x4273).

Observed: dialog FUN_00419d10 (MainApp 0x41a9..0x41c9, strings 0x839930..0x839e84) shows a grid
with groups Clean (A251113_1), Check (A251113_2), Operation (A251113_3) and Customized
maintenance items (A251113_13, name column hp40, rows pd680 "Custom"); the predefined items are
A251113_20..25 (clean), A251113_40..46 (check), A251113_60..63 (operation: FTC calibration,
lubrication, water-cooler water, air filter).  Columns: prompt cycle in days (A251113_7), time
mode (A251113_8: World Time A251113_11 / Processing time A251113_12), early warning in days
(A251113_9) and prompt type (A251113_10: pop-up A251113_4, top error bar A251113_5, log bar
A251113_6).  The settings are stored in ``MC.MaintainParamStr`` (cfg+0xbf88); its encoding and
the default cycles are U (the rebuild stores JSON and uses the defaults below, I).

Due logic (I): an item is due when the elapsed time since it was last done reaches the cycle;
it is announced from ``cycle - early warning`` on.  World time counts calendar days; processing
time counts accumulated processing time (24 h of processing = 1 day).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

PARAM_KEY = "MC.MaintainParamStr"
GROUPS = {"A251113_1": [f"A251113_{i}" for i in range(20, 26)],
          "A251113_2": [f"A251113_{i}" for i in range(40, 47)],
          "A251113_3": [f"A251113_{i}" for i in range(60, 64)]}
CUSTOM_GROUP = "A251113_13"
MODE_WORLD, MODE_PROCESS = 0, 1                  # A251113_11 / A251113_12
PROMPT_POPUP, PROMPT_ERRBAR, PROMPT_LOG = 0, 1, 2  # A251113_4 / _5 / _6
DEFAULT_CYCLE = {"A251113_1": 7, "A251113_2": 30, "A251113_3": 90}   # days (I)
DAY_S = 86400.0


@dataclass
class MaintainItem:
    key: str                         # lang key or custom name
    group: str
    enabled: bool = True
    cycle_days: float = 7.0
    mode: int = MODE_WORLD
    warn_days: float = 1.0
    prompt: int = PROMPT_LOG
    last_done: float = 0.0           # time.time() of the last confirmation (world mode)
    last_done_process_s: float = 0.0  # processing seconds at the last confirmation
    custom: bool = False

    def elapsed_days(self, now: float, process_s: float) -> float:
        if self.mode == MODE_PROCESS:
            return max(0.0, process_s - self.last_done_process_s) / DAY_S
        return max(0.0, now - self.last_done) / DAY_S

    def status(self, now: float, process_s: float) -> str:
        """"ok", "warn" (within the early-warning window) or "due"."""
        if not self.enabled or self.cycle_days <= 0:
            return "ok"
        e = self.elapsed_days(now, process_s)
        if e >= self.cycle_days:
            return "due"
        if e >= self.cycle_days - max(0.0, self.warn_days):
            return "warn"
        return "ok"

    def mark_done(self, now: float, process_s: float) -> None:
        self.last_done = now
        self.last_done_process_s = process_s


def default_items(now: Optional[float] = None) -> list[MaintainItem]:
    now = time.time() if now is None else now
    out = []
    for g, keys in GROUPS.items():
        for k in keys:
            out.append(MaintainItem(k, g, cycle_days=DEFAULT_CYCLE[g], last_done=now))
    return out


@dataclass
class MaintainPlan:
    items: list[MaintainItem] = field(default_factory=default_items)

    def to_text(self) -> str:
        return json.dumps([asdict(i) for i in self.items], ensure_ascii=False)

    @classmethod
    def from_text(cls, text: Optional[str]) -> "MaintainPlan":
        if not text:
            return cls()
        try:
            rows = json.loads(text)
            return cls([MaintainItem(**r) for r in rows if isinstance(r, dict)])
        except (ValueError, TypeError):
            return cls()

    def add_custom(self, name: str, cycle_days: float = 30.0) -> MaintainItem:
        it = MaintainItem(name, CUSTOM_GROUP, cycle_days=cycle_days, last_done=time.time(), custom=True)
        self.items.append(it)
        return it

    def check(self, now: Optional[float] = None, process_s: float = 0.0) -> list[tuple[MaintainItem, str]]:
        now = time.time() if now is None else now
        out = []
        for it in self.items:
            s = it.status(now, process_s)
            if s != "ok":
                out.append((it, s))
        return out
