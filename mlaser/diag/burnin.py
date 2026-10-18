"""Burn-in test ("烤机测试" newLang1, command id 0x4271, dialog FUN_005db330).

Parameters (StressTest table, ManuPara.xml, O): ``ST.Type`` newLang2 {0 XY axis empty travel
test newLang3, 1 Z-axis newLang4, 2 XYZ newLang5}, ``ST.xyCycle`` newLang10 cycle count,
``ST.xySleep`` newLang11 loop interval, ``ST.STIsReturnAfterManu`` newLang6 return point after
one cycle {dock, start, end, origin, mark P1..P4}, ``ST.zStartPos`` newLang12 / ``ST.zEndPos``
newLang13, ``ST.EnableTestAlarm`` newLang39 "shield capacitance alarm".  The dialog shows
``newLang7`` "Number of baking tests: %d/%d" and warns ``newLang24`` that leaving the page stops
the test.  MainApp sets cfg+0x43a4 while the dialog is open.

Run (``CManuPanel::OnEmptyMoveBtn`` variant FUN_005aaa60, O):

1. with ``ST.EnableTestAlarm`` the follower capacitance alarm is masked with mailbox
   ``[118, 5, 1]`` (vector 0x76, 5, 1 written through NC +0x84); stopping sends ``[118, 5, 0]``
   (FUN_005ab070, only for the step card cfg+0x4728 == 0);
2. types 0/2 need a loaded graphic (FUN_005b0740) and add the dry run of it to the flow
   (type 2 first adds flow item 0x17, U);
3. types 1/2 add Z moves: flow item 0x16 to ``zStartPos``, item 2 with 7, item 0x16 to
   ``zEndPos``, item 2 with 7 (I: absolute Z move + wait for follower state);
4. flow item 6 = delay ``xySleep * 1000`` ms (so the interval is in seconds), item 7 = loop
   ``xyCycle`` times.

Rebuild: :func:`build_cycle` turns one cycle into :mod:`mlaser.diag.program` steps;
:class:`BurnIn` repeats it ``xyCycle`` times (0 = until stopped, I).
"""
from __future__ import annotations

from typing import Callable, Optional

from .paramset import ParamSet
from .program import Dwell, DryRun, Move, MoveZ, ProgramRunner

KEYS = ("ST.Type", "ST.xyCycle", "ST.xySleep", "ST.STIsReturnAfterManu", "ST.zStartPos",
        "ST.zEndPos", "ST.EnableTestAlarm")
TYPE_XY, TYPE_Z, TYPE_XYZ = 0, 1, 2
RETURN_DOCK, RETURN_START, RETURN_END, RETURN_ORIGIN = 0, 1, 2, 3
CMD_MASK_CAP_ALARM = [118, 5, 1]
CMD_UNMASK_CAP_ALARM = [118, 5, 0]


def params(settings: Optional[dict] = None) -> ParamSet:
    return ParamSet(KEYS, settings, overrides={"ST.xyCycle": 1})


def _return_target(p: ParamSet, document, marks: Optional[dict] = None):
    mode = int(p["ST.STIsReturnAfterManu"])
    if mode == RETURN_ORIGIN:
        return (0.0, 0.0)
    if mode == RETURN_START and document is not None:
        for part in getattr(document, "parts", []):
            for c in part.contours:
                if c.segments:
                    return c.start_point()
    if mode == RETURN_DOCK and marks and "dock" in marks:
        return marks["dock"]
    if mode >= 4 and marks and f"P{mode - 3}" in marks:
        return marks[f"P{mode - 3}"]
    return None            # end point: stay


def build_cycle(p: ParamSet, document, marks: Optional[dict] = None) -> list:
    t = int(p["ST.Type"])
    steps: list = []
    if t in (TYPE_XY, TYPE_XYZ):
        steps.append(DryRun(document, "xy"))
    if t in (TYPE_Z, TYPE_XYZ):
        steps += [MoveZ(float(p["ST.zStartPos"]), "z start"), MoveZ(float(p["ST.zEndPos"]), "z end")]
    tgt = _return_target(p, document, marks)
    if tgt is not None:
        steps.append(Move(tgt[0], tgt[1], label="return"))
    sleep = float(p["ST.xySleep"])
    if sleep > 0:
        steps.append(Dwell(sleep, "interval"))
    return steps


class BurnIn:
    """Repeats the cycle; ``progress()`` gives (done, total) for newLang7."""

    def __init__(self, ctl, p: ParamSet, document, marks: Optional[dict] = None,
                 log: Callable[[str], None] = lambda m: None, clock=None):
        self.ctl, self.p, self.document, self.marks, self.log = ctl, p, document, marks, log
        self.total = max(0, int(p["ST.xyCycle"]))
        self.done = 0
        self.runner: Optional[ProgramRunner] = None
        self.state = "idle"
        self._clock = clock

    def _mc(self):
        return getattr(self.ctl, "mc", None)

    def can_start(self) -> Optional[str]:
        t = int(self.p["ST.Type"])
        if t in (TYPE_XY, TYPE_XYZ) and not getattr(self.document, "parts", None):
            return "mp74: graphic data is empty, cannot start"
        st = self.ctl.state()
        if not st.connected:
            return "gp0: hardware is not connected"
        return None

    def start(self) -> bool:
        why = self.can_start()
        if why:
            self.log(why)
            return False
        if bool(self.p["ST.EnableTestAlarm"]) and self._mc() is not None:
            try:
                self._mc().command(CMD_MASK_CAP_ALARM)
            except Exception as e:  # noqa: BLE001
                self.log(f"burn-in: {e}")
        self.done = 0
        self.state = "running"
        self._new_cycle()
        return True

    def _new_cycle(self) -> None:
        kw = {"clock": self._clock} if self._clock else {}
        self.runner = ProgramRunner(self.ctl, build_cycle(self.p, self.document, self.marks), log=self.log, **kw)
        self.runner.start()

    def tick(self) -> bool:
        if self.state != "running" or self.runner is None:
            return False
        if self.runner.tick():
            return True
        if self.runner.state != "done":
            self._end("stopped")
            return False
        self.done += 1
        if self.total and self.done >= self.total:
            self._end("done")
            return False
        self._new_cycle()
        return True

    def stop(self) -> None:
        """newLang24: leaving the page stops the test."""
        if self.state == "running":
            if self.runner is not None:
                self.runner.stop()
            self._end("stopped")

    def _end(self, state: str) -> None:
        self.state = state
        if self._mc() is not None and bool(self.p["ST.EnableTestAlarm"]):
            try:
                self._mc().command(CMD_UNMASK_CAP_ALARM)
            except Exception:  # noqa: BLE001
                pass

    def progress(self) -> tuple[int, int]:
        return self.done, self.total
