"""Safety I/O configuration: the interlocks and the "PC alive" heartbeat from the ConfigService.

The controller has had the interlock monitor (``controller/interlocks.py``, docs/SAFETY.md §8)
and the heartbeat output (§11) since Wave 2, but the application never configured them: every
real launch ran with the gas-low, door and laser-source-alarm interlocks and the watchdog relay
off (review 2, P0-4).  This module is the one place that turns the typed ``safety.*`` settings
(:data:`mlaser.services.config.SAFETY_SETTINGS`) into the controller's configuration:

* :func:`from_config` reads them into a :class:`SafetyIO` (an ``InterlockConfig`` plus the
  heartbeat output and frequency, and the sensors the service engineer declared "not fitted");
* :func:`problems` lists what cannot be right (two interlocks on one input, an input that does not
  exist, the heartbeat on a process output such as the laser or the gas valve); a refused
  heartbeat stays off (:func:`effective`), the interlocks always apply;
* :func:`notices` lists what is not configured, for the Run screen, the pre-flight dialog and
  the commissioning checklist;
* :func:`apply` hands a configuration to a controller (``ctl.configure_safety``): at start-up (``ui/__main__.py`` passes it
  to the constructor) and after a change, but a change is applied only while the machine is
  disarmed and no job runs (otherwise it stays pending and :func:`apply` is retried).

Defaults stay off (input 0) because the wiring of a given machine is unknown; "off" is never
silent: every unconfigured interlock is reported.

Qt-free.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields, replace
from typing import Any, Optional

from mlaser.controller.interlocks import InterlockConfig

from .config import MAX_INPUT, MAX_OUTPUT, SAFETY_SETTINGS

log = logging.getLogger("mlaser.safety_io")

#: interlock source -> (input field, active-level field) of InterlockConfig
INTERLOCKS: dict[str, tuple[str, str]] = {
    "gas": ("gas_low_input", "gas_low_active"),
    "door": ("door_input", "door_open_active"),
    "laser": ("laser_alarm_input", "laser_alarm_active"),
}
#: plain names for logs and the English fallbacks of the catalogue texts
NAMES = {"gas": "gas-low", "door": "door / guard", "laser": "laser-source alarm", "heartbeat": "PC-alive heartbeat"}
KEYS: tuple[str, ...] = tuple(s.key for s in SAFETY_SETTINGS)


@dataclass(frozen=True)
class SafetyIO:
    """The safety I/O of one machine (see the module docstring)."""

    interlocks: InterlockConfig = field(default_factory=InterlockConfig)
    heartbeat_output: int = 0
    heartbeat_hz: float = 5.0
    not_fitted: frozenset = frozenset()

    def input_of(self, source: str) -> int:
        return int(getattr(self.interlocks, INTERLOCKS[source][0]))

    def configured(self, source: str) -> bool:
        if source == "heartbeat":
            return self.heartbeat_output > 0
        return self.input_of(source) > 0

    def kwargs(self) -> dict:
        """Constructor keywords of ``UiController`` (and of ``configure_safety``)."""
        return {"interlocks": replace(self.interlocks), "heartbeat_output": int(self.heartbeat_output),
                "heartbeat_hz": float(self.heartbeat_hz)}


def from_config(config: Any) -> SafetyIO:
    """The ``safety.*`` values of a ConfigService (schema defaults for the unset ones)."""
    values = {k: config.get(k) for k in KEYS}
    ilk = InterlockConfig.from_values(values)
    nf = frozenset(src for src in INTERLOCKS if bool(values.get(f"safety.{src}_not_fitted")))
    return SafetyIO(ilk, int(values["safety.heartbeat_output"]), float(values["safety.heartbeat_hz"]), nf)


def problems(sio: SafetyIO, output_map: Optional[dict] = None) -> list[str]:
    """What is wrong with this configuration (empty: fine).  Only a bad heartbeat is refused
    (:func:`effective` switches it off): an input number out of range never matches, and two
    interlocks on one input both trip, so neither is dangerous, just wrong."""
    out: list[str] = []
    used: dict[int, str] = {}
    for src in INTERLOCKS:
        n = sio.input_of(src)
        if not n:
            continue
        if not 1 <= n <= MAX_INPUT:
            out.append(f"{NAMES[src]} interlock: input {n} does not exist (1..{MAX_INPUT})")
        if n in used:
            out.append(f"{NAMES[src]} and {NAMES[used[n]]} interlocks are both on input {n}")
        used[n] = src
    out += heartbeat_problems(sio, output_map)
    return out


def heartbeat_problems(sio: SafetyIO, output_map: Optional[dict] = None) -> list[str]:
    """Reasons the heartbeat must stay off: an output that does not exist, or one that drives a
    process function (laser, shutter, gas...): it would switch it 10 times a second."""
    out: list[str] = []
    hb = int(sio.heartbeat_output)
    if not hb:
        return out
    if not 1 <= hb <= MAX_OUTPUT:
        out.append(f"heartbeat: output {hb} does not exist (1..{MAX_OUTPUT})")
    for name, n in (output_map or {}).items():
        try:
            hit = int(n) == hb
        except (TypeError, ValueError):
            hit = False
        if hit:
            out.append(f"heartbeat output {hb} is the {name!r} process output")
    if sio.heartbeat_hz <= 0:
        out.append("heartbeat frequency must be above 0 Hz")
    return out


def effective(sio: SafetyIO, output_map: Optional[dict] = None) -> SafetyIO:
    """``sio`` as it may be used: the heartbeat off when :func:`heartbeat_problems` refuses it
    (the interlocks always apply)."""
    if heartbeat_problems(sio, output_map):
        return replace(sio, heartbeat_output=0)
    return sio


@dataclass(frozen=True)
class Notice:
    source: str            # gas | door | laser | heartbeat
    declared: bool         # the service engineer declared the sensor not fitted
    message: str

    @property
    def key(self) -> str:
        """Catalogue id of the text (``safety.notice.not_configured`` / ``.not_fitted``)."""
        return "safety.notice.not_fitted" if self.declared else "safety.notice.not_configured"


def notices(sio: SafetyIO) -> list[Notice]:
    """Every interlock and the heartbeat that is not configured (declared "not fitted" ones
    included, marked ``declared``)."""
    out = []
    for src in (*INTERLOCKS, "heartbeat"):
        if sio.configured(src):
            continue
        declared = src in sio.not_fitted
        if src == "heartbeat":
            msg = "PC-alive heartbeat not configured: no watchdog relay drops the laser enable if mlaser stops"
        elif declared:
            msg = f"{NAMES[src]} interlock not fitted (declared in Safety I/O): not monitored"
        else:
            msg = f"{NAMES[src]} interlock not configured: not monitored (Machine > Safety I/O)"
        out.append(Notice(src, declared, msg))
    return out


def preflight_issues(sio: SafetyIO, action: str, emits: bool) -> list:
    """Pre-flight warnings (``machine.preflight.Issue``) for a job that fires the laser: one per
    interlock that is neither configured nor declared not fitted, and the heartbeat."""
    if action not in ("start", "resume") or not emits:
        return []
    from mlaser.machine.preflight import WARNING, Issue
    return [Issue(f"interlock_unconfigured_{n.source}" if n.source != "heartbeat" else "heartbeat_unconfigured",
                  WARNING, n.message) for n in notices(sio) if not n.declared]


# --------------------------------------------------------------------------- controller
def controller_supports(ctl: Any) -> bool:
    """Does ``ctl`` have interlocks / a heartbeat to configure (the real controller; the
    protocol-free demo controller has none)?"""
    return callable(getattr(ctl, "configure_safety", None)) or hasattr(getattr(ctl, "interlocks", None), "cfg")


def _interlock_config(v: Any) -> Optional[InterlockConfig]:
    if isinstance(v, InterlockConfig):
        return replace(v)
    if isinstance(v, dict):
        names = {f.name for f in fields(InterlockConfig)}
        return InterlockConfig(**{k: x for k, x in v.items() if k in names})
    return None


def current(ctl: Any) -> Optional[SafetyIO]:
    """The configuration the controller runs with now (None: nothing to configure).  Reads
    ``ctl.safety_config()`` (Safety-core API) when the controller has it."""
    get = getattr(ctl, "safety_config", None)
    if callable(get):
        d = get() or {}
        ilk = _interlock_config(d.get("interlocks"))
        if ilk is not None:
            return SafetyIO(ilk, int(d.get("heartbeat_output") or 0), float(d.get("heartbeat_hz") or 5.0))
    mon: Any = getattr(ctl, "interlocks", None)
    if not hasattr(mon, "cfg"):
        return None
    hb = ctl.heartbeat_status() if callable(getattr(ctl, "heartbeat_status", None)) else {}
    return SafetyIO(replace(mon.cfg), int(hb.get("output", 0) or 0), float(hb.get("hz", 5.0) or 5.0))


def active(ctl: Any) -> dict:
    """The interlock conditions that hold now (source -> message; empty without a monitor)."""
    mon: Any = getattr(ctl, "interlocks", None)
    return dict(getattr(mon, "active", None) or {})


def output_map(ctl: Any) -> Optional[dict]:
    """The controller's process outputs (name -> output number), None when it has none."""
    m = getattr(ctl, "output_map", None)
    return dict(m) if isinstance(m, dict) else None


def busy(ctl: Any) -> bool:
    """A job runs or the machine is armed: the safety I/O must not change under it."""
    try:
        if ctl.job_active():
            return True
        st = ctl.safety()
        return bool(getattr(st, "armed", False))
    except Exception:  # noqa: BLE001 - a controller that cannot say counts as busy
        return True


def same(a: Optional[SafetyIO], b: SafetyIO) -> bool:
    return (a is not None and a.interlocks == b.interlocks and a.heartbeat_output == b.heartbeat_output
            and abs(a.heartbeat_hz - b.heartbeat_hz) < 1e-9)


def apply(ctl: Any, sio: SafetyIO) -> str:
    """Hand ``sio`` to the controller.  Returns ``"applied"``, ``"unchanged"``, ``"pending"``
    (a job runs or the machine is armed: retried later, the controller is not even asked),
    ``"refused"`` (the controller said no while idle; not retried until the next change) or
    ``"unsupported"`` (no interlocks to configure).  A heartbeat :func:`heartbeat_problems`
    refuses is applied as off.

    Uses ``ctl.configure_safety(interlocks=, heartbeat_output=, heartbeat_hz=,
    source="operator") -> bool`` (the Safety-core API: refused while a job is active, audited).
    A controller without it gets :func:`_apply_direct` (temporary shim, see there)."""
    if not controller_supports(ctl):
        return "unsupported"
    omap = output_map(ctl)
    bad = heartbeat_problems(sio, omap)
    if bad:
        log.warning("safety I/O: heartbeat refused, kept off: %s", "; ".join(bad))
    sio = effective(sio, omap)
    if same(current(ctl), sio):
        return "unchanged"
    if busy(ctl):
        return "pending"
    fn = getattr(ctl, "configure_safety", None)
    if callable(fn):
        if not fn(**sio.kwargs(), source="operator"):
            return "pending" if busy(ctl) else "refused"
        return "applied"                 # configure_safety audits itself
    _apply_direct(ctl, sio)
    _audit(sio)
    return "applied"


def _apply_direct(ctl: Any, sio: SafetyIO) -> None:
    """TEMPORARY SHIM, remove when r2-safety-core is merged (``configure_safety`` then exists on
    every controller and ``ctl.sup`` becomes private): sets the monitor's configuration and the
    supervisor's heartbeat directly, switching the old heartbeat output off first."""
    ctl.interlocks.cfg = replace(sio.interlocks)
    ctl.heartbeat_output = int(sio.heartbeat_output)
    ctl.heartbeat_hz = float(sio.heartbeat_hz)
    sup = getattr(ctl, "sup", None)
    if sup is None:
        return
    old = int(getattr(sup, "heartbeat_output", 0) or 0)
    if old != sio.heartbeat_output and getattr(sup, "_hb_state", False):
        try:
            sup._heartbeat_write(False)          # the old output off before it is forgotten
        except Exception:  # noqa: BLE001 - best effort; the watchdog relay drops anyway
            pass
    sup.heartbeat_output = int(sio.heartbeat_output)
    sup.heartbeat_hz = float(sio.heartbeat_hz)
    if hasattr(sup, "_hb_next"):
        sup._hb_next = 0.0


def _audit(sio: SafetyIO) -> None:
    c = sio.interlocks
    try:
        from mlaser.machine.audit import audit
        audit("safety_io", gas_low_input=c.gas_low_input, door_input=c.door_input,
              laser_alarm_input=c.laser_alarm_input, follower=c.follower,
              heartbeat_output=sio.heartbeat_output, heartbeat_hz=sio.heartbeat_hz)
    except Exception:  # noqa: BLE001 - the audit log is best effort here
        log.info("safety I/O applied: %s", sio)


__all__ = ["SafetyIO", "Notice", "INTERLOCKS", "NAMES", "KEYS", "from_config", "problems", "notices",
           "preflight_issues", "heartbeat_problems", "effective", "apply", "current", "busy", "controller_supports"]
