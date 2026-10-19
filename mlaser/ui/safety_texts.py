"""Remedy texts for alarms and faults shown in the alarm banner (docs/SAFETY.md §10).

``remedy(alarm)`` returns a short "what to check, then what to press" text for a card alarm
(:class:`mlaser.controller.alarms.Alarm`: by code, then by kind) or a fault reason.  English;
a translator key ``remedy.<code>`` / ``remedy.<kind>`` overrides it when the catalogue has one.
"""
from __future__ import annotations

from typing import Optional

# by alarm kind (controller.alarms.Alarm.kind)
BY_KIND = {
    "estop": "Emergency stop pressed or the e-stop circuit is open. Make the machine safe, release "
             "the e-stop, then press Reset. Home the axes before cutting again.",
    "limit": "An axis reached a limit switch or its soft limit. Jog the axis back into the working "
             "area (opposite direction), check the job position and size, then press Reset.",
    "servo": "Servo drive alarm. Read the code on the drive display, check the drive power and the "
             "encoder/motor cables, reset the drive, then press Reset and home the axes.",
    "fifo": "The motion buffer ran empty during the job (PC or network too slow). Check the network "
            "cable and the PC load, press Reset, then Resume from the breakpoint.",
    "overspeed": "A move was faster than the card allows. Lower the cutting/rapid speed of the layer "
                 "or the machine limits, press Reset, then Resume.",
    "axis": "Axis alarm (dual-drive deviation or drive fault). Check that both gantry drives are "
            "powered and aligned, press Reset and home the axes.",
    "card": "Motion card alarm. Check the card power and the EtherCAT/IO cables, press Reset; if it "
            "comes back, restart the card (Hardware > Restart).",
}

# by alarm code (controller.alarms numbering)
BY_CODE = {
    8025: "EtherCAT bus fault. Check the bus cables between card and drives and the drive power, "
          "then press Reset (the card restarts).",
    8026: "Output fault: an output is shorted or overloaded. Check the output wiring and the "
          "24 V supply, then press Reset.",
    8029: "Servo input alarm (common alarm input). Check the drive alarm outputs, reset the drives, "
          "then press Reset.",
    8030: "Emergency stop. Make the machine safe, release the e-stop, then press Reset and home.",
    9000: "The card refused a command (illegal command). Press Reset; if it repeats, check the "
          "firmware version (Hardware > Version).",
    9001: "Interpolation data length error. Press Reset and restart the job from a breakpoint; "
          "report the job file if it repeats.",
    9003: "Follower (FTC) command failed: the height controller did not answer. Check the follower "
          "cable and nozzle, run a follower calibration, then press Reset.",
    9005: "FIFO starvation: the motion buffer ran empty. Check the network cable and the PC load, "
          "press Reset, then Resume from the breakpoint.",
    9007: "Overspeed. Lower the speed of the layer or the machine limits, press Reset, then Resume.",
}

# by fault reason prefix (SafetySupervisor / UiController.raise_fault)
BY_FAULT = (
    ("link lost", "The PC lost the connection to the motion card. Check the network cable, the "
                  "switch and the card power. The card was halted as far as it could be reached. "
                  "When the link is back press Reset, home if needed, then Resume."),
    ("interlock follower", "The cutting head follower reported an alarm (collision, overrange or "
                           "signal). Lift the head, check the nozzle and the sheet, press Reset."),
    ("interlock gas", "Gas pressure is too low. Check the gas supply and the regulator, press Reset, "
                      "then Resume from the breakpoint."),
    ("interlock laser", "The laser source reports an alarm. Read the source's display or software, "
                        "clear the cause, press Reset."),
    ("interlock door", "A door or guard is open. Close it, press Reset, then Resume."),
    ("card alarm", "A card alarm stopped the job (see the alarm text). Clear the cause, press Reset, "
                   "then Resume from the breakpoint."),
    ("uncaught exception", "The program hit an internal error and halted the machine. Save your "
                           "work, check the log (Help > Log folder), press Reset."),
    ("laser off failed", "Switching the laser off was not acknowledged. Check the link; the laser "
                         "enable chain (watchdog relay) must open. Press Reset when the card answers."),
)


def _tr(tr, key: str) -> Optional[str]:
    if tr is None:
        return None
    try:
        if tr.has(key):
            s = tr.tr(key)
            return s if s and s != key else None
    except Exception:  # noqa: BLE001
        return None
    return None


def remedy(alarm, tr=None) -> str:
    """Remedy for a card alarm object (code/kind) or a fault reason string."""
    if isinstance(alarm, str):
        low = alarm.lower()
        for prefix, text in BY_FAULT:
            if prefix in low:
                return text
        for prefix, text in BY_FAULT:
            if low.startswith(prefix.split()[0]):
                return text
        return ("Check the reason above, make the machine safe, then press Reset. After a reset the "
                "machine is disarmed; continue with Resume from the breakpoint.")
    code = getattr(alarm, "code", None)
    kind = getattr(alarm, "kind", "card")
    t = _tr(tr, f"remedy.{code}") or _tr(tr, f"remedy.{kind}")
    if t:
        return t
    if code in BY_CODE:
        return BY_CODE[code]
    return BY_KIND.get(kind, BY_KIND["card"])


__all__ = ["remedy", "BY_KIND", "BY_CODE", "BY_FAULT"]
