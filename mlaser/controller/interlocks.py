"""Interlock monitor: follower, gas pressure, laser source and door (docs/SAFETY.md §8).

Evaluated on every status update (the poll thread, or the streamer's status relay during a
job), throttled to ``period_s`` (50 ms).  A trip latches a FAULT through the controller, which
halts the card.  Sources:

* **follower** (on-board FTC, ``ZF.ZFType`` != 0): follower status word 1 ("alarm status",
  register 10001, docs/re/devices.md §3; collision and overrange are card-side alarms configured by
  follower properties 18..26).  Any non-zero value while a job runs or the machine is armed;
* **gas pressure**: a digital input wired to a low-pressure switch (``gas_low_input``), while a
  job that fires the laser runs;
* **laser source alarm**: a digital input from the source's alarm output
  (``laser_alarm_input``).  The original never reads the laser source back (devices.md §4),
  so a wired input is the only reliable source; any time the machine is armed or a job runs;
* **door**: a door/guard switch input (``door_input``) while a job that fires the laser runs.

Inputs are 1-based card input numbers (``MCStatus.input``), 0 = not wired.  ``*_active`` is the
input level that means "alarm" (True: input on).  An active interlock also refuses arming.
The configuration comes from ``values`` keys ``safety.gas_low_input`` etc.
(:meth:`InterlockConfig.from_values`).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, fields
from typing import Optional, Sequence

FOLLOWER_ALARM_WORD = 1        # follower status word 1 (register 10001)


@dataclass
class InterlockConfig:
    follower: bool = True
    gas_low_input: int = 0
    gas_low_active: bool = True
    laser_alarm_input: int = 0
    laser_alarm_active: bool = True
    door_input: int = 0
    door_open_active: bool = True
    period_s: float = 0.05

    @classmethod
    def from_values(cls, values: Optional[dict]) -> "InterlockConfig":
        c = cls()
        for f in fields(cls):
            k = f"safety.{f.name}"
            if values and k in values:
                v = values[k]
                setattr(c, f.name, type(getattr(c, f.name))(v) if not isinstance(getattr(c, f.name), bool)
                        else str(v).lower() in ("1", "true", "yes", "on"))
        return c


@dataclass(frozen=True)
class Trip:
    source: str              # follower | gas | laser | door
    message: str


class InterlockMonitor:
    def __init__(self, cfg: Optional[InterlockConfig] = None):
        self.cfg = cfg or InterlockConfig()
        self.active: dict[str, str] = {}      # source -> message while the condition holds
        self.tripped: set[str] = set()        # sources that tripped and still hold
        self.trips: list[tuple[float, Trip]] = []
        self._last = 0.0

    def _input(self, st, n: int) -> Optional[bool]:
        if not n:
            return None
        try:
            return bool(st.input(int(n)))
        except (ValueError, IndexError, AttributeError):
            return None

    def conditions(self, st, zf_words: Optional[Sequence[int]], *, follower_present: bool,
                   job_active: bool, emitting: bool, armed: bool) -> dict[str, str]:
        """Interlock conditions that hold now (source -> message), ignoring the state gating."""
        c = self.cfg
        out: dict[str, str] = {}
        if c.follower and follower_present and zf_words is not None and len(zf_words) > FOLLOWER_ALARM_WORD:
            w = int(zf_words[FOLLOWER_ALARM_WORD]) & 0xFFFFFFFF
            if w:
                out["follower"] = f"follower alarm 0x{w:08x} (collision / overrange / signal)"
        v = self._input(st, c.gas_low_input)
        if v is not None and v == c.gas_low_active:
            out["gas"] = f"gas pressure low (input {c.gas_low_input})"
        v = self._input(st, c.laser_alarm_input)
        if v is not None and v == c.laser_alarm_active:
            out["laser"] = f"laser source alarm (input {c.laser_alarm_input})"
        v = self._input(st, c.door_input)
        if v is not None and v == c.door_open_active:
            out["door"] = f"door / guard open (input {c.door_input})"
        return out

    @staticmethod
    def gated(source: str, *, job_active: bool, emitting: bool, armed: bool) -> bool:
        """Does the condition of ``source`` trip in the current machine state?"""
        if source == "follower":
            return job_active or armed
        if source in ("gas", "door"):
            return job_active and emitting
        if source == "laser":
            return job_active or armed
        return True

    def evaluate(self, st, zf_words: Optional[Sequence[int]] = None, *, follower_present: bool = False,
                 job_active: bool = False, emitting: bool = False, armed: bool = False,
                 force: bool = False) -> list[Trip]:
        """New trips since the last call (edge-triggered); ``self.active`` keeps the holding ones."""
        now = time.monotonic()
        if not force and now - self._last < self.cfg.period_s:
            return []
        self._last = now
        cond = self.conditions(st, zf_words, follower_present=follower_present, job_active=job_active,
                               emitting=emitting, armed=armed)
        new: list[Trip] = []
        for src, msg in cond.items():
            if src not in self.tripped and self.gated(src, job_active=job_active, emitting=emitting, armed=armed):
                t = Trip(src, msg)
                new.append(t)
                self.tripped.add(src)
                self.trips.append((time.time(), t))
                del self.trips[:-100]
        self.tripped &= set(cond)
        self.active = dict(cond)
        return new

    def blocking_arm(self) -> Optional[str]:
        """Reason to refuse arming (an interlock holds), or None."""
        if not self.active:
            return None
        return "; ".join(self.active.values())


__all__ = ["InterlockConfig", "InterlockMonitor", "Trip"]
