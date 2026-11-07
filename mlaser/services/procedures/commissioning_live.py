"""Live status of the commissioning checklist (GitHub issue #6), Qt-free.

The checklist (:mod:`.commissioning`) runs its checks once, step by step.  This module adds the
*live* view the wizard shows beside the steps: one row per check, recomputed a few times a second
from what the controller already publishes, so an operator sees the state change while they
work on the machine:

* **connection**: ``state().connected`` / ``link_ok`` (the poll thread's result);
* **card alarms**: ``active_alarms`` (decoded from the polled status), the e-stop apart;
* **scaling**: ``ctl.scaling`` (read once at connect) against the PC's parameters
  (``config.machine()``), recomputed as the parameters change;
* **e-stop** and each **interlock input** (door, gas-low, laser-source alarm): the interlock
  monitor's ``active`` set (evaluated on every polled status) and ``active_alarms`` for the
  e-stop.  A "test it" tracker asks the operator to trigger the sensor and turns to *pass* when
  the input changes and the guard would refuse motion, then asks to release it;
* **homing**: ``state().homed``; **heartbeat**: ``heartbeat_status()`` (the supervisor's
  counters).

Nothing here talks to the card: every value is a cached read of what the poll thread published
(no register read, no write, no motion, no output).  "Would the guard refuse" is answered with
the guard's own classification (:meth:`InterlockMonitor.gated` and
:meth:`InterlockMonitor.blocking_arm`, the calls the start gate and arming use), never a
classification of its own, and the guard is never asked to start anything.

Each row names the fix actions that lead to the one place that fixes it (:data:`FIXES`): the
network self-test, the parameter import of an original install, adopting the card's scaling,
the Safety I/O dialog focused on the input, Clear Alarm, the soft-limit write, the axis
direction.  The UI maps them to its existing commands and dialogs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from mlaser.controller.interlocks import InterlockMonitor

PASS, WARN, FAIL, SKIP, PENDING = "pass", "warn", "fail", "skip", "pending"

SCALE_TOL = 0.001                        # relative (the checklist's tolerance)
ILK_SOURCES = ("door", "gas", "laser")   # monitor sources with a digital input (safety_io.INTERLOCKS)
ROWS = ("connection", "alarms", "scaling", "soft_limits", "estop", "ilk.door", "ilk.gas", "ilk.laser",
        "homing", "heartbeat", "jog_x", "jog_y")
TITLES = {"connection": "wizard.comm.live.connection", "alarms": "wizard.comm.live.alarms",
          "scaling": "wizard.comm.item_scaling", "soft_limits": "wizard.comm.item_soft_limits",
          "estop": "wizard.comm.live.estop", "ilk.door": "wizard.comm.live.ilk_door",
          "ilk.gas": "wizard.comm.live.ilk_gas", "ilk.laser": "wizard.comm.live.ilk_laser",
          "homing": "wizard.comm.item_homing", "heartbeat": "wizard.comm.item_heartbeat",
          "jog_x": "wizard.comm.item_jog_x", "jog_y": "wizard.comm.item_jog_y"}

#: fix action id -> catalogue id of its button.  ``safety_io:<src>`` opens the Safety I/O dialog
#: at that input; ``simulate:<src>`` toggles the dry-run simulator's input (dry run only).
FIXES = {"netcheck": "wizard.comm.fix.netcheck", "reconnect": "wizard.comm.fix.reconnect",
         "import_params": "wizard.comm.fix.import_params", "adopt_scaling": "wizard.comm.fix.adopt_scaling",
         "safety_io": "wizard.comm.fix_safety_io", "clear_alarms": "wizard.comm.fix.clear_alarms",
         "fix_limits": "wizard.comm.fix_limits", "reverse": "wizard.comm.fix_reverse_axis",
         "simulate": "wizard.comm.fix.simulate"}
#: the fix actions that change nothing on the PC or the card (navigation / observation only)
NAVIGATION = ("netcheck", "safety_io", "import_params")
#: the guard's refusals named per started state: (job_like, emits) as the start gate passes them
GUARD_ACTIONS = (("cut", True, True), ("frame", True, False))


@dataclass
class LiveCheck:
    id: str
    status: str = PENDING
    detail: str = ""                                  # catalogue id
    values: dict = field(default_factory=dict)
    fixes: tuple = ()                                 # fix action ids (FIXES, with ``:arg``)
    prompt: str = ""                                  # catalogue id of the "test it" prompt

    @property
    def title(self) -> str:
        return TITLES[self.id]


@dataclass
class SensorTest:
    """The "test it" state of one sensor: ``ready`` (waiting for the operator to trigger it),
    ``held`` (active before the test: release it first), ``triggered`` (seen active and refused
    by the guard: release it), ``tested`` (seen trip and clear)."""

    phase: str = "new"
    refused: tuple = ()                               # what the guard refused while it was active
    input: int = 0

    def observe(self, active: bool, refused: tuple = ()) -> str:
        if self.phase == "new":
            self.phase = "held" if active else "ready"
        elif self.phase == "held" and not active:
            self.phase = "ready"
        elif self.phase == "ready" and active and refused:
            self.phase, self.refused = "triggered", tuple(refused)
        elif self.phase == "triggered" and not active:
            self.phase = "tested"
        return self.phase

    @property
    def seen(self) -> bool:
        return self.phase in ("triggered", "tested")


# =========================================================================== scaling
def compare_scaling(sc: Any, m: Any, tol: float = SCALE_TOL) -> tuple[list[str], dict]:
    """(mismatching quantities ``X`` / ``Y`` / ``period``, the values) of the card's scaling
    ``sc`` (:class:`~mlaser.controller.client.CardScaling`) against the machine parameters
    ``m`` (:class:`~mlaser.nc.machine.Machine`)."""
    card_x, card_y = sc.step_ppm(sc.x_axis), sc.step_ppm(sc.y_axis)
    vals = {"card_x": round(card_x, 6), "card_y": round(card_y, 6), "par_x": m.pulses_per_mm_x,
            "par_y": m.pulses_per_mm_y, "card_period_ms": sc.period_ms, "par_period_ms": m.period_ms}

    def rel(a: float, b: float) -> float:
        return abs(a - b) / max(abs(b), 1e-9)
    bad = []
    if rel(card_x, m.pulses_per_mm_x) > tol:
        bad.append("X")
    if rel(card_y, m.pulses_per_mm_y) > tol:
        bad.append("Y")
    if abs(sc.period_ms - m.period_ms) > 1e-6:
        bad.append("period")
    return bad, vals


def card_scaling_values(sc: Any) -> dict:
    """The machine parameters that describe the card's scaling (what "Adopt the card's values"
    stores): pulses per revolution and lead per axis (``MAC`` = X, ``MAC_1`` = Y, as
    :meth:`Machine.from_params` reads them) and the interpolation period.  Empty when the card
    reported no usable scaling."""
    out: dict = {}
    for pre, axis in (("MAC", sc.x_axis), ("MAC_1", sc.y_axis)):
        a = sc.axes[axis]
        if not (a.ppr and a.lead_mm):
            return {}
        out[f"{pre}.WritePluse"] = int(a.ppr)
        out[f"{pre}.SpeedRatio"] = round(float(a.lead_mm), 6)
    if sc.period_us > 0:
        out["AX.InterpolationCycle"] = int(sc.period_us)
    return out


# =========================================================================== live status
class LiveStatus:
    """Recomputes the live rows (:meth:`update`) and keeps the "test it" trackers."""

    def __init__(self) -> None:
        self.sensors: dict[str, SensorTest] = {}
        self._hb: Optional[tuple[float, int]] = None
        self._hb_rate: Optional[float] = None
        self.rows: dict[str, LiveCheck] = {}

    def reset_tests(self) -> None:
        self.sensors.clear()

    def results(self) -> dict:
        """{sensor: phase} for the report."""
        return {k: v.phase for k, v in self.sensors.items()}

    # ------------------------------------------------------------------ update
    def update(self, machine: Any, config: Any = None, proc: Any = None, dry_run: bool = False) -> dict:
        """Recompute every row from ``machine``'s published state; returns ``{id: LiveCheck}``."""
        st = machine.state()
        connected = bool(st.connected)
        items = getattr(proc, "items", {}) or {}
        rows: dict[str, LiveCheck] = {}
        rows["connection"] = self._connection(machine, st, items)
        rows["alarms"] = self._alarms(machine, connected)
        rows["scaling"] = self._scaling(machine, config, connected, dry_run)
        rows["soft_limits"] = self._from_item("soft_limits", items, connected)
        rows["estop"] = self._estop(machine, connected, dry_run)
        cur, declared = _safety_io(machine, config)
        mon = getattr(machine, "interlocks", None)
        armed = bool(getattr(st, "armed", False))
        for src in ILK_SOURCES:
            rows[f"ilk.{src}"] = self._interlock(src, cur, declared, mon, connected, armed, dry_run)
        rows["homing"] = self._homing(st, items, connected)
        rows["heartbeat"] = self._heartbeat(machine, items, connected)
        for axis in ("x", "y"):
            rows[f"jog_{axis}"] = self._from_item(f"jog_{axis}", items, connected,
                                                  fixes=(f"reverse:{axis.upper()}",))
        self.rows = rows
        return rows

    # ------------------------------------------------------------------ rows
    @staticmethod
    def _connection(machine: Any, st: Any, items: dict) -> LiveCheck:
        ep = machine.endpoint() or ("", 0)
        vals = {"host": ep[0], "port": ep[1]}
        if not st.connected:
            return LiveCheck("connection", FAIL, "wizard.comm.live.not_connected", vals,
                             ("netcheck", "reconnect"))
        if not getattr(st, "link_ok", True):
            return LiveCheck("connection", WARN, "wizard.comm.live.link_slow", vals, ("netcheck",))
        it = items.get("network")
        if it is not None and it.status in (FAIL, WARN):    # connected, but the self-test found problems
            return LiveCheck("connection", WARN, it.detail, {**vals, **dict(it.values)}, ("netcheck",))
        key = "wizard.comm.live.connected_sim" if machine.is_simulator() else "wizard.comm.live.connected"
        return LiveCheck("connection", PASS, key, vals)

    @staticmethod
    def _alarms(machine: Any, connected: bool) -> LiveCheck:
        if not connected:
            return LiveCheck("alarms", SKIP, "wizard.comm.live.needs_connection")
        others = [a for a in (getattr(machine, "active_alarms", None) or []) if getattr(a, "kind", "") != "estop"]
        if others:
            names = ", ".join(str(getattr(a, "text_en", a)) for a in others[:4])
            return LiveCheck("alarms", FAIL, "wizard.comm.live.alarms_active", {"alarms": names},
                             ("clear_alarms",))
        return LiveCheck("alarms", PASS, "wizard.comm.live.alarms_none")

    @staticmethod
    def _scaling(machine: Any, config: Any, connected: bool, dry_run: bool) -> LiveCheck:
        sc = getattr(machine, "scaling", None)
        if sc is None:
            if not connected:
                return LiveCheck("scaling", SKIP, "wizard.comm.live.needs_connection")
            return LiveCheck("scaling", FAIL, "wizard.comm.scale_unread", fixes=("reconnect",))
        if config is not None:
            m = config.machine()
        else:
            from mlaser.nc.machine import Machine
            m = Machine()
        bad, vals = compare_scaling(sc, m)
        if bad:
            vals["which"] = ", ".join(bad)
            fixes: tuple = ("import_params",)
            if card_scaling_values(sc) and config is not None and not dry_run:
                fixes += ("adopt_scaling",)
            return LiveCheck("scaling", FAIL, "wizard.comm.scale_mismatch", vals, fixes)
        return LiveCheck("scaling", PASS, "wizard.comm.scale_ok", vals)

    @staticmethod
    def _from_item(key: str, items: dict, connected: bool, fixes: tuple = ()) -> LiveCheck:
        it = items.get(key)
        if it is None or it.status == PENDING:
            return LiveCheck(key, PENDING if connected else SKIP,
                             "wizard.comm.live.run_step" if connected else "wizard.comm.live.needs_connection")
        fx: tuple = ()
        if it.fix and it.status in (FAIL, WARN):
            fx = fixes or (("fix_limits",) if key == "soft_limits" else ())
        return LiveCheck(key, it.status, it.detail, dict(it.values), fx)

    def _estop(self, machine: Any, connected: bool, dry_run: bool) -> LiveCheck:
        if not connected:
            return LiveCheck("estop", SKIP, "wizard.comm.live.needs_connection")
        active = any(getattr(a, "kind", "") == "estop" for a in (getattr(machine, "active_alarms", None) or []))
        t = self.sensors.setdefault("estop", SensorTest())
        # the start gate refuses every motion while a card alarm is active (gate.py step 2)
        phase = t.observe(active, ("cut", "frame", "move") if active else ())
        sim = ("simulate:estop",) if dry_run else ()
        if phase == "held":
            return LiveCheck("estop", WARN, "wizard.comm.live.estop_held", fixes=("clear_alarms",) + sim)
        if phase == "ready":
            return LiveCheck("estop", PENDING, "wizard.comm.live.estop_ready", fixes=sim,
                             prompt="wizard.comm.live.estop_prompt")
        if phase == "triggered" or active:
            return LiveCheck("estop", PASS, "wizard.comm.live.estop_seen", fixes=("clear_alarms",) + sim,
                             prompt="wizard.comm.live.estop_release")
        return LiveCheck("estop", PASS, "wizard.comm.live.estop_tested")

    def _interlock(self, src: str, cur: Any, declared: frozenset, mon: Any, connected: bool, armed: bool,
                   dry_run: bool) -> LiveCheck:
        rid = f"ilk.{src}"
        fix_cfg = (f"safety_io:{src}",)
        if cur is None or mon is None:
            return LiveCheck(rid, SKIP, "wizard.comm.ilk_no_monitor", fixes=fix_cfg)
        n = cur.input_of(src)
        if n <= 0:
            self.sensors.pop(src, None)
            if src in declared:
                return LiveCheck(rid, SKIP, "wizard.comm.live.ilk_not_fitted", fixes=fix_cfg)
            return LiveCheck(rid, WARN, "wizard.comm.live.ilk_unconfigured", fixes=fix_cfg)
        t = self.sensors.get(src)
        if t is None or t.input != n:                # new, or moved to another input: test again
            t = self.sensors[src] = SensorTest(input=n)
        vals = {"input": n}
        if not connected:
            return LiveCheck(rid, SKIP, "wizard.comm.live.needs_connection", vals, fix_cfg)
        active = src in (getattr(mon, "active", None) or {})
        refused = guard_refusals(mon, src, armed) if active else ()
        phase = t.observe(active, refused)
        sim = (f"simulate:{src}",) if dry_run else ()
        vals["refused"] = refused or t.refused
        if phase == "held":
            return LiveCheck(rid, WARN, f"wizard.comm.live.{src}_held", vals, fix_cfg + sim)
        if phase == "ready":
            return LiveCheck(rid, PENDING, f"wizard.comm.live.{src}_ready", vals, sim,
                             prompt=f"wizard.comm.live.{src}_prompt")
        if active:
            return LiveCheck(rid, PASS, f"wizard.comm.live.{src}_seen", vals, sim,
                             prompt=f"wizard.comm.live.{src}_release")
        return LiveCheck(rid, PASS, "wizard.comm.live.ilk_tested", vals, sim)

    @staticmethod
    def _homing(st: Any, items: dict, connected: bool) -> LiveCheck:
        if not connected:
            return LiveCheck("homing", SKIP, "wizard.comm.live.needs_connection")
        if st.homed:
            return LiveCheck("homing", PASS, "wizard.comm.live.homed", {"x": st.x, "y": st.y})
        it = items.get("homing")
        if it is not None and it.status == FAIL:
            return LiveCheck("homing", FAIL, it.detail, dict(it.values), ("clear_alarms",))
        return LiveCheck("homing", PENDING, "wizard.comm.live.not_homed")

    def _heartbeat(self, machine: Any, items: dict, connected: bool) -> LiveCheck:
        hb = machine.heartbeat_status()
        out = int(hb.get("output") or 0)
        if out <= 0:
            self._hb = self._hb_rate = None
            return LiveCheck("heartbeat", WARN, "wizard.comm.live.hb_unconfigured", fixes=("safety_io:heartbeat",))
        now, n = time.monotonic(), int(hb.get("toggles", 0))
        if self._hb is None or now - self._hb[0] > 5.0:
            self._hb = (now, n)
        elif now - self._hb[0] >= 1.0:
            self._hb_rate = (n - self._hb[1]) / (now - self._hb[0])
            self._hb = (now, n)
        vals = {"output": out, "hz": hb.get("hz", 0.0)}
        if hb.get("hold"):
            return LiveCheck("heartbeat", WARN, "wizard.comm.live.hb_held", vals)
        it = items.get("heartbeat")
        if it is not None and it.status in (FAIL, WARN):
            return LiveCheck("heartbeat", it.status, it.detail, dict(it.values), ("safety_io:heartbeat",))
        if not connected:
            return LiveCheck("heartbeat", SKIP, "wizard.comm.live.needs_connection", vals)
        if self._hb_rate is None:
            return LiveCheck("heartbeat", PENDING, "wizard.comm.live.hb_measuring", vals)
        vals["rate"] = round(self._hb_rate, 1)
        want = 2.0 * float(hb.get("hz") or 0.0)
        if want > 0 and self._hb_rate < 0.6 * want:
            return LiveCheck("heartbeat", FAIL, "wizard.comm.live.hb_slow", vals, ("safety_io:heartbeat",))
        if it is not None and it.status == PASS:
            return LiveCheck("heartbeat", PASS, it.detail, {**dict(it.values), **vals})
        return LiveCheck("heartbeat", PASS, "wizard.comm.live.hb_live", vals)


def guard_refusals(mon: Any, src: str, armed: bool) -> tuple:
    """What the guard refuses while interlock ``src`` holds: ``cut`` (a job that fires),
    ``frame`` (a job that does not) per :meth:`InterlockMonitor.gated` (the start gate's call),
    ``arm`` per the monitor's :meth:`~InterlockMonitor.blocking_arm` (arming's call)."""
    out = [name for name, job_like, emits in GUARD_ACTIONS
           if InterlockMonitor.gated(src, job_active=job_like, emitting=emits, armed=armed)]
    if mon is not None and mon.blocking_arm() is not None:
        out.append("arm")
    return tuple(out)


def _safety_io(machine: Any, config: Any) -> tuple[Any, frozenset]:
    from mlaser.services import safety_io as S
    cur = S.current(machine)
    declared: frozenset = frozenset()
    if config is not None:
        try:
            declared = S.from_config(config).not_fitted
        except Exception:  # noqa: BLE001 - a config without the safety schema
            declared = frozenset()
    return cur, declared


def summary(rows: dict) -> dict:
    """Counts per status of the live rows (the header line)."""
    out = {s: 0 for s in (PASS, WARN, FAIL, SKIP, PENDING)}
    for r in rows.values():
        out[r.status] = out.get(r.status, 0) + 1
    return out


__all__ = ["LiveStatus", "LiveCheck", "SensorTest", "compare_scaling", "card_scaling_values", "guard_refusals",
           "ROWS", "TITLES", "FIXES", "NAVIGATION", "summary"]
