"""NCModule FUN_10043c20: one ManuItem -> its FIFO items, bit for bit (moved from
``mlaser.controller.process``, Wave 1; docs/protocol.md "Process items").

Helpers: FUN_1003ec20 wait, FUN_1003f3e0 crash protect, FUN_1003f470 focus, FUN_1003f830 focus
wait, FUN_1003f990 peak-current DA.  Motion samples (class 0) are packed by
``mlaser.parity_ports.mainapp.sampling``.
"""
from __future__ import annotations

from typing import Any, Iterable

from ...protocol import frames as F
from ...protocol.manu import (CLS_ADV_FIX_HEIGHT, CLS_AFTER_GRADUAL, CLS_DWELL_3002, CLS_FINAL_LIFT,
                              CLS_FIX_HEIGHT, CLS_FOLLOW, CLS_LIFT, CLS_MARKER, CLS_PEAK,
                              CLS_PIERCE_END, CLS_PIERCE_START, CLS_PRE_JUMP, CLS_SYS, CLS_WAIT_ZF,
                              STAGE_CLEAN, STAGE_SMOOTH, SUB_CRASH, SUB_DA, SUB_DELAY, SUB_DO,
                              SUB_LASER_SYNC, ManuItem)
from .._registry import port
from ..mainapp.layer import LayerView

# the process configuration (mlaser.nc.process.ProcessConfig) is read by attribute only
ProcessConfig = Any

__port__ = {
    "source": "NCModule",
    "functions": {"FUN_10043c20": "ManuEncoder.encode / _encode", "FUN_1003ec20": "_wait_zf (wait items)",
                  "FUN_1003f3e0": "_crash", "FUN_1003f470": "_focus", "FUN_1003f830": "_focus_wait",
                  "FUN_1003f990": "_peak", "0x1004442a": "_pierce_start", "0x100447fc": "_pierce_end",
                  "FUN_10056850 / FUN_10047c50": "ManuEncoder.__init__ (program start state)"},
    "parity": ["jobnc", "jobmainapp"],
}


# --------------------------------------------------------------------------- NC layer
# classes FUN_10043c20 skips without output (explicit test before the class chain)
_SILENT_CLASSES = (0xC, 0xD, 0xF, 0x10)
# follower classes gated by ``cfg+0x4994 != 0`` (ZF.ZFType); without a follower they fall
# through to the final else-branch, i.e. they are packed as an op-3000 step from their fields
_ZF_CLASSES = (CLS_PIERCE_START, CLS_PIERCE_END, CLS_FOLLOW, CLS_LIFT, CLS_FINAL_LIFT,
               CLS_FIX_HEIGHT, CLS_PEAK, CLS_ADV_FIX_HEIGHT)
FOCUS_WAIT_ADD_MS = 50.0     # FUN_1003f470: ftol(|dpos| / v * 1000 + 50.0) (0x1003f7df.., 50.0 at 0x1008b948)
AF5_AXIS = 0x10              # FUN_1003f470 0x1003f724 / FUN_1003f830 case 5: AF type 5 uses axis 0x10


@port("FUN_10043c20", source="NCModule", parity=("jobnc", "jobmainapp"))
class ManuEncoder:
    """Port of NCModule FUN_10043c20 (one ManuItem -> its FIFO items).

    Start state = NC program start (ctor FUN_10056850, FUN_10047c50): ``prev_cls`` (VM+0x117c)
    = -1, so the first process item of a job gets no op 3001; ``peak`` (VM+0x11c8) = 0, so a
    first peak of 0 % sends no DA; ``focus`` (VM+0x11d0) = the focus-axis position read from
    the card at program start (``focus_pos``, 0.0 by default), so a first focus target equal
    to it is not moved to.

    Classes: 1 system commands, 3 op 3002, 0xe op 3001, 0xc/0xd/0xf/0x10 nothing; 6 (+0 == 3:
    focus wait + follower wait), 0x12 (pre-jump focus / peak) and 0x13 (wait, focus wait) are
    encoded with or without a follower; the follower classes 4, 5, 7, 8, 9, 0xa, 0xb, 0x11 only
    with ZF.ZFType != 0.  Everything else (class 0 motion, 2, 0x14..0xff, and the follower classes
    without a follower) is the final else-branch: an op-3000 step built from the item's own
    dx/dy/freq/duty/+8 fields (:func:`frames.encode_step`).
    """

    def __init__(self, cfg: ProcessConfig, layers=None, dry_run: bool = False, focus_pos: float = 0.0):
        self.cfg = cfg
        self.layers = layers if layers is not None else {}
        self.dry = dry_run
        self.prev_cls = -1           # VM+0x117c (-1 at program start, FUN_10047c50 0x10047f00)
        self.height = 0.0            # global 0x100b4fa8: current pierce height
        self.focus: float = float(focus_pos)   # VM+0x11d0 last focus position (card value at start)
        self.peak: int = 0           # VM+0x11c8 last peak-current argument (0 at program start)
        self.process_ms = 0.0        # estimated card time spent in process items

    def view(self, layer: int) -> LayerView:
        lp = self.layers.get(layer) if hasattr(self.layers, "get") else None
        v = LayerView(lp)
        v.index = layer
        return v

    # ---------------------------------------------------------------- helpers
    def _crash(self, value: int) -> list[list[int]]:
        """FUN_1003f3e0."""
        c = self.cfg
        if c.crash_protect and c.has_follower:
            return [F.encode_crash_protect(value, c.crash_up_height)]
        return []

    def _peak(self, pct: int) -> list[list[int]]:
        """FUN_1003f990: DA for the laser peak power, only when it changes."""
        if self.dry or self.peak == pct:
            return []
        v = self.cfg.peak_da_value(pct)
        if v is None:
            return []
        self.peak = pct
        return [F.encode_da(self.cfg.laser_da_port - 1, v)]

    def _focus_axis(self) -> int:
        """AF type 5: the constant axis 0x10; AF type 6: AF.AFAxis (+0x4ad8)."""
        return AF5_AXIS if self.cfg.af_type == 5 else self.cfg.af_axis

    def _focus(self, speed: float, pos: float) -> tuple[list[list[int]], int]:
        """FUN_1003f470 for AF types 5/6 (focus axis move).  Returns (items, wait_ms or -1);
        the wait is ``ftol(|pos - VM+0x11d0| / v * 1000 + 50)``."""
        c = self.cfg
        if c.af_type not in (5, 6) or pos == self.focus:
            return [], -1
        speed = max(1.0, speed)
        ppm = c.z_pulses_per_mm
        ms = F.ftol(abs(pos - self.focus) / speed * 1000.0 + FOCUS_WAIT_ADD_MS)
        self.focus = pos
        return [F.encode_axis_move(self._focus_axis(), F.ftol(ppm * speed), c.af_axis_acc,
                                   F.ftol(ppm * pos))], ms

    def _focus_wait(self, ms: int) -> list[list[int]]:
        """FUN_1003f830 for AF types 5/6: wait for the focus axis (timeout VM+0x860 + ms)."""
        if ms < 0 or self.cfg.af_type not in (5, 6):
            return []
        return [F.encode_wait(F.WAIT_AXIS, b=self._focus_axis(), c=0,
                              timeout_ms=self.cfg.follow_timeout_ms + ms)]

    def _wait_zf(self, state: int, timeout: int) -> list[list[int]]:
        return [F.wait_zf(state, timeout)]

    @staticmethod
    def _clamp(v, lo, hi):
        return lo if v < lo else hi if v > hi else v

    def _zest(self, dist_mm: float, speed: float) -> None:
        if speed > 0:
            self.process_ms += abs(dist_mm) / speed * 1000.0

    # ---------------------------------------------------------------- encode
    def encode(self, it: ManuItem) -> list[list[int]]:
        out: list[list[int]] = []
        cls = it.cls
        if 0 < cls < 0x14 and cls != CLS_MARKER and self.prev_cls == 0:
            out.append(F.encode_mark())
        out += self._encode(it)
        self.prev_cls = cls
        return out

    def encode_all(self, items: Iterable[ManuItem]) -> list[list[int]]:
        out: list[list[int]] = []
        for it in items:
            out += self.encode(it)
        return out

    def _encode(self, it: ManuItem) -> list[list[int]]:
        c = self.cfg
        zf = c.has_follower
        cls = it.cls
        if cls == CLS_SYS:
            sub = it.a
            if sub in (1, 2):
                return [F.encode_laser(it.freq, it.duty)]
            if sub == SUB_DA:
                return [F.encode_da(it.b, it.value)]
            if sub == SUB_DO:
                w = F.encode_output(it.b, bool(it.value))
                return [w] if w else []
            if sub == SUB_DELAY:
                if it.value > 0:
                    self.process_ms += it.value
                    return [F.wait_ms(it.value)]
                return []
            if sub == SUB_CRASH:
                return self._crash(it.value)
            if sub == SUB_LASER_SYNC:
                return [F.encode_laser_sync(1000 if it.b == 1 else 0)]
            return []
        if cls == CLS_DWELL_3002:
            return [F.fifo_item(F.OP_3002, it.value)]
        if cls == CLS_MARKER:
            return [F.encode_mark()]
        if cls in _SILENT_CLASSES:
            return []
        lv = self.view(it.layer)
        if cls == CLS_WAIT_ZF:
            if it.a == 3:
                return self._focus_wait(10000) + self._wait_zf(it.b, c.follow_timeout_ms)
            return []
        if cls == CLS_PRE_JUMP:
            if lv.b("EnableSmoothPierce"):
                return self._focus(c.af_speed, lv.f("SmoothPierceDrillFocusPos"))[0]
            n = lv.n_stages
            if n:
                return self._focus(c.af_speed, lv.stage(n - 1).focus)[0]
            return self._peak(lv.i("CutPeakCurrent", 100)) + self._focus(c.af_speed, lv.f("CutFocusPos"))[0]
        if cls == CLS_AFTER_GRADUAL:
            return (self._wait_zf(F.ZF_ARRIVED, c.drill_timeout_ms) if zf else []) + self._focus_wait(0)
        if cls not in _ZF_CLASSES or not zf:
            return [self._step_item(it)]
        if cls == CLS_PIERCE_START:
            return self._pierce_start(it, lv)
        if cls == CLS_PIERCE_END:
            return self._pierce_end(it, lv)
        if cls == CLS_FOLLOW:
            out = self._crash(0)
            fi, ms = self._focus(c.af_speed, lv.f("CutFocusPos"))
            out += fi + self._peak(lv.i("CutPeakCurrent", 100))
            h = max(lv.cut_height, 0.5)
            thin, thick = lv.vib
            out.append(F.encode_follow_on(c.zf_follow_speed, F.ftol(h * c.z_pulses_per_mm), thin, thick))
            out += self._wait_zf(F.ZF_FOLLOWING, c.follow_timeout_ms)
            self._zest(c.zf_dock_height, c.zf_follow_speed)
            self.height = lv.cut_height
            return out + self._focus_wait(ms)
        if cls == CLS_LIFT:
            out = [F.encode_z_rel(c.zf_follow_speed, -lv.f("UpHeight", 20.0)),
                   *self._wait_zf(F.ZF_ARRIVED, c.follow_timeout_ms)]
            self._zest(lv.f("UpHeight", 20.0), c.zf_follow_speed)
            return out + self._crash(F.ftol(c.crash_min_height))
        if cls == CLS_FINAL_LIFT:
            self._zest(c.zf_dock_height, c.zf_up_speed)
            return [F.encode_z_rel(c.zf_up_speed, -c.zf_dock_height),
                    *self._wait_zf(F.ZF_ARRIVED, c.follow_timeout_ms)]
        if cls == CLS_PEAK:
            return self._peak(lv.i("CutPeakCurrent", 100))
        if cls == CLS_FIX_HEIGHT:
            fi, ms = self._focus(c.af_speed, lv.f("CutFocusPos"))
            out = fi + self._peak(lv.i("CutPeakCurrent", 100))
            out.append(F.fifo_item(F.OP_FOLLOW_ON, F.ftol(c.zf_follow_speed * 10.0), 1000))
            out += self._wait_zf(F.ZF_FOLLOWING, c.follow_timeout_ms)
            out.append(F.encode_z_rel(c.zf_follow_speed, -lv.cut_height - 1.0))
            out += self._wait_zf(F.ZF_ARRIVED, c.follow_timeout_ms)
            return out + self._focus_wait(ms)
        if cls == CLS_ADV_FIX_HEIGHT:
            fi, ms = self._focus(c.af_speed, lv.f("CutFocusPos"))
            out = fi + self._peak(lv.i("CutPeakCurrent", 100))
            out.append(F.encode_z_abs(c.zf_follow_speed, lv.f("AdvFixHeightCutPos", 20.0)))
            out += self._wait_zf(F.ZF_ARRIVED, c.follow_timeout_ms)
            return out + self._focus_wait(ms)
        return []

    @staticmethod
    def _step_item(it: ManuItem) -> list[int]:
        """FUN_10043c20's final else-branch (non-bus card): op 3000 from +0 / +4 (dx, dy), +0x14
        (freq, 0 -> 5000), +0x12 (duty) and +8 (sent only with a duty).  |d| > 127 never comes
        from the rebuild (the original packs |low byte| with the per-axis direction word)."""
        return F.encode_step(int(it.a), int(it.b), int(it.freq) & 0xFFFF, int(it.duty) & 0xFF,
                             int(it.power) & 0xFFFF)

    def _pierce_start(self, it: ManuItem, lv: LayerView) -> list[list[int]]:
        """Class 4 (0x1004442a): follow to the stage height (first stage, op 105) or move
        relative from the previous stage height (op 109), then wait for the follower."""
        c = self.cfg
        out = self._crash(0)
        thin, thick = lv.vib
        if it.stage == STAGE_SMOOTH:
            raw = lv.f("SmoothPierceDrillHeight", 5.0)
            h = 1.0 if raw > 10.0 else raw
            out.append(F.encode_follow_to(c.zf_follow_speed, h, c.zf_up_speed, raw - h, thin, thick))
            out += self._wait_zf(F.ZF_FOLLOW_TO_DONE, c.drill_timeout_ms)
            self._zest(c.zf_dock_height, c.zf_follow_speed)
            self.height = raw
            return out
        st = lv.stage(it.stage)
        out += self._peak(st.peak)
        fi, ms = self._focus(c.af_speed, st.focus)
        out += fi
        if it.first == 1:
            h = 1.0 if st.height > c.direct_drill_max_height else st.height
            out.append(F.encode_follow_to(c.zf_follow_speed, h, c.zf_up_speed, st.height - h, thin, thick))
            out += self._wait_zf(F.ZF_FOLLOW_TO_DONE, c.drill_timeout_ms)
            self._zest(c.zf_dock_height, c.zf_follow_speed)
        else:
            out.append(F.encode_z_rel(c.zf_up_speed, -(st.height - self.height)))
            out += self._wait_zf(F.ZF_ARRIVED, c.drill_timeout_ms)
            self._zest(st.height - self.height, c.zf_up_speed)
        out += self._focus_wait(ms)
        self.height = st.height
        return out

    def _pierce_end(self, it: ManuItem, lv: LayerView) -> list[list[int]]:
        """Class 5 (0x100447fc): gradual height change during the beam-on time (op 108 to the
        cut height after the last stage, op 109 to the next stage), speed = |dh| / time clamped
        to 0.1..200 mm/s."""
        c = self.cfg
        s = it.stage
        if s == STAGE_CLEAN:
            return []
        out: list[list[int]] = []
        if s == STAGE_SMOOTH:
            out += self._peak(lv.i("SmoothPierceDrillPeakCurrent", 100))
            t = lv.i("SmoothPierceDrillTime_ms", 100)
            ts = max(t, 1) * 0.001
            fpos, cpos = lv.f("SmoothPierceDrillFocusPos"), lv.f("CutFocusPos")
            fi, ms = self._focus(self._clamp(abs(fpos - cpos) / ts, 0.01, 30.0), cpos)
            out += fi
            h0, h1 = lv.f("SmoothPierceDrillHeight", 5.0), lv.cut_height
            delta = h0 - h1
            v = self._clamp(abs(delta) / ts, 0.1, 200.0)
            out.append(F.encode_z_gradual(v, delta, h1))
            out += self._wait_zf(F.ZF_ARRIVED, c.drill_timeout_ms + t)
            self.height = h1
            return out + self._focus_wait(ms)
        st = lv.stage(s)
        out += self._peak(st.peak)
        ts = max(st.time_ms, 1) * 0.001
        if st.focus_gradual:
            fi, ms = self._focus(self._clamp(abs(st.focus_end - st.focus) / ts, 0.01, 30.0), st.focus_end)
        else:
            fi, ms = self._focus(c.af_speed, st.focus)
        out += fi
        if s == 0:
            nxt, last = lv.cut_height, True
        else:
            nxt, last = lv.stage(s - 1).height, False
        delta = st.height - nxt
        v = self._clamp(abs(delta) / ts, 0.1, 200.0)
        if last:
            out.append(F.encode_z_gradual(v, delta, nxt))
        else:
            out.append(F.encode_z_rel(v, delta))
        if not st.bolt:
            out += self._wait_zf(F.ZF_ARRIVED, c.drill_timeout_ms + st.time_ms)
            out += self._focus_wait(ms)
        self.process_ms += st.time_ms
        self.height = nxt
        return out
