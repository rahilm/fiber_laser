"""Machine configuration used by the NC compiler (``mlaser.nc.compile_job``).

Every number here is a machine parameter in the original (HardPara.xml / ManuPara.xml via
ParaModule, plus ``JumpAddTime.txt``).  The shipped install does not contain a machine's real
values, so the defaults are a plausible bench setup.  Tags follow the other docs:

* **observed**: ``period_ms`` = 1.0 (CVelocityPlanning p5 default, MotionCtrl 0x1001c288),
  ``jump_add_time_ms`` = 200 (shipped ``JumpAddTime.txt`` ``[Jump] AddTime``), the int8 step
  limit (FUN_10043c20 packs dx/dy as one byte each).
* **process items**: ``process`` (:class:`mlaser.nc.process.ProcessConfig`) holds the
  follower, gas, laser-DA and delay parameters; :meth:`Machine.from_dat_dir` loads them from
  HardPara.xml / ManuPara.xml.  ``Machine()`` uses ``ProcessConfig.bench()`` (on-board follower,
  gas valves on outputs 1..3, proportional valve on DA 1; INFERRED bench wiring).
* **observed (agent B, docs/protocol.md §11)**: step pulses/mm = ``WritePluse / SpeedRatio`` =
  pulses-per-revolution / lead (MainApp FUN_00458b60, config +0xbcb0 / +0xbca8), defaults
  10000 / 100 mm = **100 pulses/mm**, giving a 1270 mm/s step-speed cap at 1 ms.  Position counts
  (``position_ppm``, config +0xbc28) are a separate scale the card reports at connect (system
  word 17); 1000 is INFERRED from softPara.ini.
* **INFERRED**: ``power_scale`` (layer power % -> the optional layer DA value, 0..10000, only used with
  ``emit_layer_commands``; the step items' power word is always 0xFFFF, parity JB-02),
  rapid speed/acceleration (MainApp's jump profile parameters are UNKNOWN, docs/motion.md §8).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .process import ProcessConfig
from ..motion.interp import MotionConfig
from ..motion.planner import PlannerParams


@dataclass
class Machine:
    """Axis scaling, servo period and dynamics for compiling a job."""
    pulses_per_mm_x: float = 100.0      # step pulses/mm = ppr / lead (observed defaults 10000 / 100)
    pulses_per_mm_y: float = 100.0
    ppr: int = 10000                    # MAC.WritePluse, pulses per motor revolution (observed default)
    lead_mm: float = 100.0              # MAC.SpeedRatio, mm per revolution (observed default)
    position_ppm: float = 1000.0        # card position counts/mm (+0xbc28, read from the card; INFERRED default)
    period_ms: float = 1.0              # observed default servo/interpolation period
    max_step: int = 127                 # observed: signed int8 dx/dy per step item
    rapid_vel: float = 500.0            # mm/s, INFERRED
    rapid_acc: float = 5000.0           # mm/s², INFERRED
    rapid_acc_time: float = 0.1         # s, INFERRED (jerk = f(acc, acc_time) as for cuts)
    jump_add_time_ms: float = 200.0     # observed (JumpAddTime.txt); emitted as a settle dwell
    cut: PlannerParams = field(default_factory=PlannerParams)   # CVelocityPlanning defaults
    corner_tol: float = 0.05            # INFERRED corner rounding tolerance (docs/motion.md §5)
    smooth: bool = True                 # CContoutSmooth corner blending before planning (motion.md §9):
    #                                     always on for contours in the original (FUN_100c6fe0, O)
    power_scale: float = 100.0          # layer DA = power_pct * power_scale with emit_layer_commands (I);
    #                                     the power word itself is always 0xFFFF (JB-02)
    emit_layer_commands: bool = False   # emit DA/PWM FIFO items at LayerChange (INFERRED, off by default)
    gas_output: Optional[int] = None    # legacy: one output for every gas type (overrides process.gas_valves)
    process: ProcessConfig = field(default_factory=ProcessConfig.bench)  # process items (pierce/gas/ZF)
    frame_speed: float = 100.0          # mm/s for "Frame" (pd528, default unverified)
    # manual motion (O, MainApp jog FUN_005bdae0 / move FUN_005c97f0 / stop FUN_10038de0;
    # defaults = the ParaModule defaults of the named parameters)
    jog_speed: float = 50.0             # MC.JogSlowSpeed (+0x4810), mm/s
    jog_fast_speed: float = 200.0       # MC.JogFastSpeed (+0x4808), mm/s
    move_speed: float = 100.0           # MC.XFastMoveSpeed (+0x4858), mm/s (return / mark moves)
    move_acc: float = 4000.0            # MC.XFastMoveAcc (+0x4868): sent UNSCALED as the acc word
    jog_stop_factor: int = 1            # MP.JogStopDccFactor (+0x4a4c)
    max_acc: float = 20000.0            # FCP.MaxAcc (+0x4a40), clamp of the stop deceleration
    pt_laser_freq: int = 1234           # LC.PtLaserFreq (+0x48bc): PWM frequency of laser on/off
    table: tuple[float, float] = (3000.0, 1500.0)
    params: dict = field(default_factory=dict)   # raw HardPara/ManuPara values when loaded from files

    def param(self, key: str, default=None):
        """A HardPara/ManuPara value by key: the loaded value (``params``) or, when the machine
        was not loaded from files, the parameter's ParaModule default (ParamDef.default)."""
        if key in self.params:
            return self.params[key]
        from ..formats.params import param_defs
        for d in param_defs():
            if d.key == key:
                v = d.default
                try:
                    if d.type_name in ("double", "float"):
                        return float(v)
                    if d.type_name == "bool":
                        return str(v).strip().lower() in ("1", "true")
                    if d.type_name != "string":
                        return int(float(v))
                except (TypeError, ValueError):
                    break
                return v
        return default

    def iparam(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.param(key, default)))
        except (TypeError, ValueError):
            return default

    def fparam(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.param(key, default))
        except (TypeError, ValueError):
            return default

    @property
    def max_step_speed(self) -> float:
        """Fastest axis speed (mm/s) one int8 step item per period can express."""
        return self.max_step / min(self.pulses_per_mm_x, self.pulses_per_mm_y) / (self.period_ms * 1e-3)

    def motion_config(self) -> MotionConfig:
        """The mlaser.motion configuration matching this machine."""
        # CInterpMrg+8..+0x48 as MainApp FUN_004572f0 fills it (loaded values, else the
        # ParaModule defaults of the same parameters; motion.interp.MotionConfig)
        f, b = self.fparam, (lambda k, d: bool(self.iparam(k, int(d))))
        return MotionConfig(cut=PlannerParams(**{**self.cut.__dict__, "period_ms": self.period_ms}),
                            rapid_vel=self.rapid_vel, rapid_acc=self.rapid_acc,
                            rapid_acc_time=self.rapid_acc_time,
                            jump_add_time_ms=self.jump_add_time_ms, corner_tol=self.corner_tol,
                            period_ms=self.period_ms, smooth=self.smooth,
                            micro_link_redrill=b("GP.IsDrillInMicoLink", 0),
                            micro_link_decel=b("GP.EnableMicroLinkDecc", 0),
                            micro_link_vel=f("GP.MicoLinkSlowDownVel", 10.0),
                            small_circle_limit=b("MP.EnableSmallCircleSpeedLimit", 0),
                            small_circle_ratio=f("MP.SmallCircleSpeedLimitRatio", 0.6),
                            scan_circle_vel_ratio=f("FCP.FlycutCircleVelRatio", 1.0),
                            scan_circle_pwm_delay=f("FCP.FlycutCirclePwmDelayTime", 0.0),
                            scan_line_open_pwm=f("FCP.FlycutLineOpenPwmForwardCycle", -10.0),
                            scan_line_close_pwm=f("FCP.FlycutLineColsePwmForwardCycle", -10.0))

    def process_config(self) -> ProcessConfig:
        """``process`` with the legacy ``gas_output`` applied to all six gas valves."""
        if self.gas_output is None:
            return self.process
        from dataclasses import replace
        return replace(self.process, gas_valves=(int(self.gas_output),) * 6)

    @classmethod
    def from_params(cls, mp, ini: Optional[dict] = None, **kw) -> "Machine":
        """Machine from a :class:`mlaser.formats.params.MachineParams`.

        Taken from the files: servo period = ``AX.InterpolationCycle`` (us, HardPara +0x4910;
        FUN_00464d60 inserts 1000/cycle samples per ms) and every process parameter (:meth:`ProcessConfig.from_params`).  Pulses/mm
        stay INFERRED (config +0xbc28 / +0xbcb0.. are not in the parameter tables).
        """
        from ..formats.params import param_defs
        vals = {}
        for d in param_defs():
            if d.file in ("HardPara.xml", "ManuPara.xml") and d.key and d.key not in vals:
                st = mp.hard if d.file == "HardPara.xml" else mp.manu
                try:
                    vals[d.key] = st.get(d)
                except Exception:  # noqa: BLE001
                    continue
        args = {}
        cyc = vals.get("AX.InterpolationCycle")
        if cyc:
            args["period_ms"] = float(cyc) / 1000.0
        # step scale per axis = WritePluse / SpeedRatio (FUN_00458b60); X, Y = machine axes 0, 1 (I)
        for axis, pre in (("x", "MAC"), ("y", "MAC_1")):
            ppr, lead = vals.get(f"{pre}.WritePluse"), vals.get(f"{pre}.SpeedRatio")
            if ppr and lead:
                args[f"pulses_per_mm_{axis}"] = float(ppr) / float(lead)
                if axis == "x":
                    args["ppr"], args["lead_mm"] = int(ppr), float(lead)
        # manual motion (O): jog speeds, move speed/acc, stop deceleration, point-shot PWM
        for attr, key, typ in (("jog_speed", "MC.JogSlowSpeed", float),
                               ("jog_fast_speed", "MC.JogFastSpeed", float),
                               ("move_speed", "MC.XFastMoveSpeed", float),
                               ("move_acc", "MC.XFastMoveAcc", float),
                               ("jog_stop_factor", "MP.JogStopDccFactor", int),
                               ("max_acc", "FCP.MaxAcc", float),
                               ("pt_laser_freq", "LC.PtLaserFreq", int)):
            v = vals.get(key)
            if v not in (None, ""):
                try:
                    args[attr] = typ(float(v))
                except (TypeError, ValueError):
                    pass
        args.update(kw)
        m = cls(**args)
        m.process = ProcessConfig.from_params(mp, ini)
        m.params = vals
        return m

    @classmethod
    def from_dat_dir(cls, dat_dir, ini_path: Optional[str] = None, jump_add_time: Optional[str] = None,
                     **kw) -> "Machine":
        """Load ``<dat>/HardPara.xml`` + ``ManuPara.xml`` (via mlaser.formats.params), optionally
        ``File/ipAdd.ini`` (timeouts, DAMinVal) and ``JumpAddTime.txt``."""
        from ..formats.params import MachineParams
        mp = MachineParams.load_dir(dat_dir)
        ini = _read_ini_values(ini_path) if ini_path else None
        if jump_add_time:
            from ..formats.scfile import JumpAddTime
            kw.setdefault("jump_add_time_ms", float(JumpAddTime.load(jump_add_time).add_time_ms))
        return cls.from_params(mp, ini, **kw)

    @classmethod
    def from_jump_add_time(cls, path: str, **kw) -> "Machine":
        """Machine with ``jump_add_time_ms`` taken from a JumpAddTime.txt file."""
        from ..formats.scfile import JumpAddTime
        j = JumpAddTime.load(path)
        return cls(jump_add_time_ms=float(j.add_time_ms), **kw)


def _read_ini_values(path: str) -> dict:
    """``key=value`` pairs of an ini file (any section), GBK/UTF-8 tolerant."""
    out: dict = {}
    raw = open(path, "rb").read()
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    for line in text.splitlines():
        line = line.strip()
        if "=" in line and not line.startswith((";", "#", "[")):
            k, v = line.split("=", 1)
            try:
                out[k.strip()] = int(v.strip())
            except ValueError:
                out[k.strip()] = v.strip()
    return out
