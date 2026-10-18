"""Device report ("运行报告" / Device Report, ribbon ``mf801``, command id 0x3aa5).

Observed (MainApp handler FUN_004b9260, NCModule VM +0x1a4 = FUN_1003d3a0):

* the report is a property grid of the nine SoftParam counters ``SOP.Device*`` (``pd880..888``,
  ManuPara.xml, struct offsets 0x4428..0x4468) with "Set Zero" / "All Set Zero" (``ap20`` /
  ``ap21``, confirmation ``ap22``);
* before showing it, when the card is connected, MainApp reads the card's run information:
  3 words at register **1050** and the axis RO word **k6** ("Total mileage",
  AxisRORegName_7) of the X axis, the Y axis and, when a follower is configured, the Z axis
  (``getReg(3, axis*10 + 6)``), and adds them to the counters:
  ``DeviceTotalLaserOnTime += word[2] / 3600`` (seconds -> hours, constant 0x83a7c0 = 3600.0),
  ``Device{X,Y}AxisTotalMoveLength += 0.001 * mileage / ppm`` (counts -> mm -> m, 0x84cc00 =
  0.001; ppm = cfg+0xbc28).  When the card is not connected a message box is shown instead.
  The Z sum target was not visible in the decompilation (I: DeviceZAxisTotalMoveLength).
* words 0/1 at 1050 are I: power-on and communication time (RORegName_26/27 name the same
  quantities in the RO block); MainApp does not add them.  Whether the card clears its
  counters after the read (otherwise every opening adds the full totals again) is U; the
  rebuild reproduces the observed additions.  ``DeviceControllerTotalUseTime`` is accumulated
  by the rebuild while a card is connected (I).

The remaining counters are kept by the software: total use time (I: hours the program ran),
total / last processing time (hours / seconds, I), processing count (one per completed job,
O: FUN_00598280 increments the record count and the counters).  Units of pd880..888 are not
in lang.txt; the rebuild uses hours for times and metres for lengths (I).
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, fields
from typing import Optional

from .registers import AX_MILEAGE, REG_RUN_INFO, s32

KEYS = {   # attribute -> (param name, lang key)
    "software_use_h": ("SOP.DeviceSoftwareTotalUseTime", "pd880"),
    "controller_use_h": ("SOP.DeviceControllerTotalUseTime", "pd881"),
    "total_process_h": ("SOP.DeviceTotalManuTime", "pd882"),
    "last_process_s": ("SOP.DeviceLastManuTime", "pd883"),
    "laser_on_h": ("SOP.DeviceTotalLaserOnTime", "pd884"),
    "process_count": ("SOP.DeviceTotalManuCount", "pd885"),
    "x_move_m": ("SOP.DeviceXAxisTotalMoveLength", "pd886"),
    "y_move_m": ("SOP.DeviceYAxisTotalMoveLength", "pd887"),
    "z_move_m": ("SOP.DeviceZAxisTotalMoveLength", "pd888"),
}
LASER_TIME_DIV = 3600.0      # MainApp _PTR_0083a7c0
MILEAGE_SCALE = 0.001        # MainApp _DAT_0084cc00


@dataclass
class DeviceCounters:
    software_use_h: float = 0.0
    controller_use_h: float = 0.0
    total_process_h: float = 0.0
    last_process_s: float = 0.0
    laser_on_h: float = 0.0
    process_count: int = 0
    x_move_m: float = 0.0
    y_move_m: float = 0.0
    z_move_m: float = 0.0

    # ------------------------------------------------------------------ persistence
    def to_settings(self, settings: dict) -> None:
        for attr, (name, _key) in KEYS.items():
            settings[name] = getattr(self, attr)

    @classmethod
    def from_settings(cls, settings: dict) -> "DeviceCounters":
        c = cls()
        for attr, (name, _key) in KEYS.items():
            if name in settings:
                try:
                    setattr(c, attr, type(getattr(c, attr))(settings[name]))
                except (TypeError, ValueError):
                    pass
        return c

    # ------------------------------------------------------------------ updates
    def reset(self, attr: Optional[str] = None) -> None:
        """ap20 Set Zero (one counter) / ap21 All Set Zero."""
        for f in fields(self):
            if attr is None or f.name == attr:
                setattr(self, f.name, type(getattr(self, f.name))(0))

    def add_card_info(self, run_info: list[int], mileage: dict, ppm: float) -> None:
        """FUN_004b9260 after VM +0x1a4: laser time and axis mileage from the card."""
        if len(run_info) >= 3:
            self.laser_on_h += s32(run_info[2]) / LASER_TIME_DIV
        ppm = ppm or 1.0
        for axis, attr in (("x", "x_move_m"), ("y", "y_move_m"), ("z", "z_move_m")):
            if axis in mileage:
                setattr(self, attr, getattr(self, attr) + MILEAGE_SCALE * s32(mileage[axis]) / ppm)

    def add_job(self, duration_s: float) -> None:
        """One completed job (FUN_00598280)."""
        self.process_count += 1
        self.last_process_s = float(duration_s)
        self.total_process_h += duration_s / 3600.0

    def as_rows(self, tr=None) -> list[tuple[str, str, str]]:
        """(attr, label, value) for the dialog grid."""
        from .registers import label
        en = {"pd880": "Software Total Use Time (h)", "pd881": "Controller Total Use Time (h)",
              "pd882": "Device Total Processing Time (h)", "pd883": "Device Last Processing Time (s)",
              "pd884": "Device Total Laser On Time (h)", "pd885": "Device Total Processing Count",
              "pd886": "X Axis Total Move Length (m)", "pd887": "Y Axis Total Move Length (m)",
              "pd888": "Z Axis Total Move Length (m)"}
        rows = []
        for attr, (_name, key) in KEYS.items():
            txt = label(tr, key, en[key])
            if tr is not None and "." in txt:
                txt = txt.split(".", 1)[1]
            v = getattr(self, attr)
            rows.append((attr, txt, str(v) if isinstance(v, int) else f"{v:.3f}"))
        return rows

    def asdict(self) -> dict:
        return asdict(self)


def read_card_info(mc, axes=(0, 1), z_axis: Optional[int] = None) -> tuple[list[int], dict]:
    """VM +0x1a4 FUN_1003d3a0: ``read(1050, 3)`` and ``getReg(3, axis*10 + 6)`` for X, Y (and
    Z when a follower is configured).  Read-only."""
    info = list(mc.read_regs(REG_RUN_INFO, 3))
    st = mc.read_status()
    mileage = {"x": st.axes[axes[0]].raw[AX_MILEAGE], "y": st.axes[axes[1]].raw[AX_MILEAGE]}
    if z_axis is not None:
        mileage["z"] = st.axes[z_axis].raw[AX_MILEAGE]
    return info, mileage


class UseTimer:
    """Accumulates SOP.DeviceSoftwareTotalUseTime while the program runs (I)."""

    def __init__(self, counters: DeviceCounters, clock=time.monotonic):
        self.c = counters
        self.clock = clock
        self.t = clock()

    def tick(self, connected: bool = False) -> None:
        now = self.clock()
        dh = max(0.0, now - self.t) / 3600.0
        self.c.software_use_h += dh
        if connected:
            self.c.controller_use_h += dh
        self.t = now
