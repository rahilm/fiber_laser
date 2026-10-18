"""Custom IO buttons ("自定义IO[自锁]" newLang301 / "自定义IO[非自锁]" newLang300) and Dedust.

Observed in MainApp:

* ``DO.CustomDOStr`` (HardPara DOParam, pd521, cfg+0x944) is split into up to 20 records
  (FUN_00624a00 / FUN_004dbe..): records separated by ``","`` (literal 0x862ca4), fields by
  ``"#"`` (0x862ca8); a record needs exactly 3 fields ``name#port#flag``; the port goes through
  FUN_004dd240 (``stoi``) into cfg+0x960+0x28*i, the flag is compared with the literal at
  0x862cac (bytes ``2d 00``, i.e. ``"-"``; meaning of the comparison U) and gives the type at
  cfg+0x964+0x28*i: 1 = self-locking, 0 = non-self-locking.  The hardware-config grid shows the
  three columns name / DO port (10 or 26 ports) / self-locking checkbox.
* The Advanced ribbon gets a panel "Custom IO [self-locking]" with the type-1 records and a
  panel "Custom IO [non-self-locking]" with the type-0 records, one button per record with id
  56000+i (FUN_004aa8c0; panels only when a record of that type exists).
* Command 56000..56020 (FUN_004bc460): **only type 1** toggles its DO: ``set(port,
  !get(port))`` through CNCModule +0x148 (get) / +0x13c (set).  Type-0 buttons do nothing in
  the command handler; the momentary (on while pressed) behaviour is not visible in the
  ribbon code (U).  The rebuild drives type-0 outputs on press / off on release.
* Dedust (mf140 "Enable Dedust", id 0x4269, FUN_004bc020): the same toggle on
  ``DO.ClearAsh`` (pd1400, cfg+0x2ec); the button exists only when that port is set and its
  caption follows the state (mf171 "Enable Dedust" / mf170 "Disable Dedust", FUN_004bc090).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

MAX_CUSTOM = 20
SELF_LOCK_FLAGS = ("-", "1", "true", "True")   # "-" = literal 0x862cac (O); "1"/"true" (I)


@dataclass
class CustomIO:
    name: str
    port: int                 # DO number, 1-based
    self_locking: bool        # type 1


def parse_custom_do(text: str) -> list[CustomIO]:
    out: list[CustomIO] = []
    if not text:
        return out
    for rec in text.split(","):
        f = rec.split("#")
        if len(f) != 3:
            continue
        try:
            port = int(f[1].strip())
        except ValueError:
            continue
        if port <= 0:
            continue
        out.append(CustomIO(f[0], port, f[2].strip() in SELF_LOCK_FLAGS))
        if len(out) >= MAX_CUSTOM:
            break
    return out


def format_custom_do(items: list[CustomIO]) -> str:
    return ",".join(f"{c.name}#{c.port}#{'-' if c.self_locking else '0'}" for c in items[:MAX_CUSTOM])


class OutputDriver:
    """Reads / writes digital outputs through the window controller.

    With a card controller (``ctl.mc``) the DO number is written directly (CNCModule
    +0x13c set output); the facade's named outputs are used otherwise (demo controller)."""

    def __init__(self, ctl, log=None):
        self.ctl = ctl
        self.log = log or (lambda m: None)
        self._shadow: dict[int, bool] = {}

    def get(self, port: int) -> bool:
        mc = getattr(self.ctl, "mc", None)
        if mc is not None:
            try:
                return mc.read_status().output(port)
            except Exception:  # noqa: BLE001
                pass
        return self._shadow.get(port, False)

    def set(self, port: int, on: bool) -> bool:
        mc = getattr(self.ctl, "mc", None)
        self._shadow[port] = bool(on)
        if mc is not None:
            try:
                mc.set_output(int(port), bool(on))
                return True
            except Exception as e:  # noqa: BLE001
                self.log(f"DO{port}: {e}")
                return False
        self.log(f"DO{port} = {'on' if on else 'off'} (no card connected)")
        return False

    def toggle(self, port: int) -> bool:
        """FUN_004bc460 / FUN_004bc020: ``set(port, !get(port))``; returns the new state."""
        new = not self.get(port)
        self.set(port, new)
        return new


def dedust_port(settings: dict, ctl=None) -> Optional[int]:
    """DO.ClearAsh, or the controller's ``dedust`` output mapping (rebuild default)."""
    p = settings.get("DO.ClearAsh")
    try:
        if p and int(p) > 0:
            return int(p)
    except (TypeError, ValueError):
        pass
    om = getattr(ctl, "output_map", None) or {}
    return om.get("dedust") or None
