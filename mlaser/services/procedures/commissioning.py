"""First-connection commissioning checklist.

Run once when a machine is connected for the first time (and after a controller swap or a
wiring change).  Every item ends as ``pass`` / ``warn`` / ``fail`` / ``skip`` with the measured
values, and the whole list is saved as a JSON report in mlaser's data directory:

1. **network**: the network self-test (:mod:`mlaser.netcheck`) and the connection.
2. **scaling**: the card's scaling read back at connect (``ctl.scaling``: step pulses/mm per
   axis, interpolation period) against the machine parameters (``nc.Machine``).
3. **soft limits**: the card's soft limits read back against ``MAC.SoftLimitMaxLen`` (what
   CNCModule +0x198 writes); the fix writes them (``MachineOps.write_soft_limits``).
4. **interlocks**: the gas-low, door and laser-source-alarm inputs and the heartbeat output
   (``safety.*``, :mod:`mlaser.services.safety_io`): every interlock that is neither wired nor
   declared "not fitted" is flagged (``warn``); the fix opens the Safety I/O dialog.  The
   **wiring test** then asks the operator to trigger each wired sensor in turn and watches the
   controller's interlock monitor: it must go active and clear again (``fail`` otherwise).  In a
   dry run the simulator's inputs are toggled instead.
5. **homing**: Go Origin X/Y, then the homed bits.
6. **heartbeat**: the "PC alive" output toggles at its frequency (measured), and the operator
   confirms the watchdog relay drops when the heartbeat is held (docs/SAFETY.md §11).
7. **jog direction**: X+ and Y+ steps; the position must increase and the operator confirms
   the direction; the fix toggles the axis direction parameter (``MAC.AxisReverse``).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .engine import Procedure, Step, StepFailed

PASS, WARN, FAIL, SKIP, PENDING = "pass", "warn", "fail", "skip", "pending"
SCALE_TOL = 0.001                        # relative
LIMIT_TOL_MM = 0.5
JOG_STEP_MM = 10.0
AXIS_REVERSE = {"X": "MAC.AxisReverse", "Y": "MAC_1.AxisReverse"}
ITEMS = ("network", "scaling", "soft_limits", "interlocks", "homing", "heartbeat", "jog_x", "jog_y")
INTERLOCK_WINDOW_S = 30.0                # per sensor: time to trigger it, and to release it
TITLES = {i: f"wizard.comm.item_{i}" for i in ITEMS}


@dataclass
class CheckItem:
    id: str
    status: str = PENDING
    detail: str = ""                             # catalogue id
    values: dict = field(default_factory=dict)   # measured numbers / named values for detail
    fix: str = ""                                # catalogue id of the available fix ("" none)


class CommissioningProcedure(Procedure):
    id = "commissioning"
    title = "wizard.comm.title"
    min_mode = "service"

    def __init__(self, machine: Any, config: Any = None, ops: Any = None,
                 report_dir: Optional[Path] = None, netcheck_fn: Optional[Callable[..., Any]] = None,
                 relay_window_s: float = 1.0, interlock_window_s: float = INTERLOCK_WINDOW_S,
                 **kw: Any) -> None:
        self.config = config
        self.interlock_window_s = interlock_window_s
        self.interlock_results: dict[str, str] = {}   # source -> ok | no_signal | stuck | active
        self.ops = ops
        self.report_dir = report_dir
        self.netcheck_fn = netcheck_fn
        self.relay_window_s = relay_window_s
        self.items: dict[str, CheckItem] = {i: CheckItem(i) for i in ITEMS}
        self.report_path: Optional[Path] = None
        self.jog_moved: dict[str, float] = {}
        super().__init__(machine, **kw)

    def build_steps(self) -> list[Step]:
        return [
            Step("intro", "wizard.comm.intro_title", "wizard.comm.intro_text", "info",
                 safety=("wizard.comm.safety_estop", "wizard.comm.safety_source_off",
                         "wizard.safety.clear_area", "wizard.safety.abort")),
            Step("network", TITLES["network"], "wizard.comm.network_text", "action", run=_network, auto=True),
            Step("scaling", TITLES["scaling"], "wizard.comm.scaling_text", "action", run=_scaling, auto=True),
            Step("soft_limits", TITLES["soft_limits"], "wizard.comm.soft_limits_text", "action",
                 run=_soft_limits, auto=True),
            Step("interlocks", TITLES["interlocks"], "wizard.comm.interlocks_text", "action", run=_interlocks,
                 auto=True),
            Step("interlock_test", "wizard.comm.ilk_test_title", "wizard.comm.ilk_test_text", "action",
                 run=_interlock_test, action_label="wizard.comm.ilk_test_button",
                 skip=lambda p: not p.items["interlocks"].values.get("wired")),
            Step("homing", TITLES["homing"], "wizard.comm.homing_text", "action", run=_homing, motion=True,
                 safety=("wizard.safety.motion",), action_label="wizard.comm.homing_button"),
            Step("heartbeat", TITLES["heartbeat"], "wizard.comm.heartbeat_text", "action", run=_heartbeat,
                 auto=True),
            Step("heartbeat_relay", "wizard.comm.relay_title", "wizard.comm.relay_text", "question",
                 skip=lambda p: p.items["heartbeat"].status in (FAIL, SKIP)),
            Step("jog_x", TITLES["jog_x"], "wizard.comm.jog_x_text", "action", run=lambda p: _jog(p, "X"),
                 motion=True, safety=("wizard.safety.motion",), action_label="wizard.comm.jog_x_button"),
            Step("jog_x_dir", "wizard.comm.jog_x_q_title", "wizard.comm.jog_x_q", "question"),
            Step("jog_y", TITLES["jog_y"], "wizard.comm.jog_y_text", "action", run=lambda p: _jog(p, "Y"),
                 motion=True, safety=("wizard.safety.motion",), action_label="wizard.comm.jog_y_button"),
            Step("jog_y_dir", "wizard.comm.jog_y_q_title", "wizard.comm.jog_y_q", "question"),
            Step("summary", "wizard.comm.summary_title", "wizard.comm.summary_text", "info"),
        ]

    # ------------------------------------------------------------------ items
    def set_item(self, i: str, status: str, detail: str = "", fix: str = "", **values: Any) -> CheckItem:
        it = self.items[i]
        it.status, it.detail, it.fix = status, detail, fix
        it.values.update(values)
        return it

    def on_answer(self, step: Step, value: bool) -> None:
        if step.id == "heartbeat_relay":
            self.heartbeat_hold(False)
            it = self.items["heartbeat"]
            if value:
                self.set_item("heartbeat", PASS if it.status == PASS else it.status, "wizard.comm.hb_relay_ok")
            else:
                self.set_item("heartbeat", FAIL, "wizard.comm.hb_relay_fail")
        elif step.id in ("jog_x_dir", "jog_y_dir"):
            axis = "X" if step.id == "jog_x_dir" else "Y"
            key = f"jog_{axis.lower()}"
            it = self.items[key]
            if it.status == FAIL:
                return                          # the position already went the wrong way
            if value:
                self.set_item(key, PASS, "wizard.comm.jog_ok", axis=axis)
            else:
                self.set_item(key, FAIL, "wizard.comm.jog_wrong", fix="wizard.comm.fix_reverse", axis=axis)

    def heartbeat_hold(self, hold: bool) -> None:
        try:
            self.machine.heartbeat_hold(bool(hold))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ fixes
    def recheck_interlocks(self) -> CheckItem:
        """Run the interlock configuration check again (after the Safety I/O dialog)."""
        self.interlock_results.clear()
        _interlocks(self)
        return self.items["interlocks"]

    def fix_soft_limits(self) -> bool:
        if self.dry_run or self.ops is None:
            return False
        self.ops.write_soft_limits()
        self.set_item("soft_limits", WARN, "wizard.comm.limits_written")
        return True

    def fix_axis_direction(self, axis: str) -> Optional[bool]:
        """Toggle ``MAC.AxisReverse`` of the axis (written to the card with the parameters at the
        next connect).  Returns the new value; None in a dry run / without a config."""
        if self.dry_run or self.config is None:
            return None
        key = AXIS_REVERSE[axis]
        new = not bool(self.config.get(key, False))
        self.config.set(key, new)
        self.set_item(f"jog_{axis.lower()}", WARN, "wizard.comm.reverse_set", axis=axis, key=key, value=new)
        return new

    # ------------------------------------------------------------------ report
    def summary(self) -> dict:
        counts = {s: sum(1 for it in self.items.values() if it.status == s) for s in (PASS, WARN, FAIL, SKIP, PENDING)}
        return {"ok": counts[FAIL] == 0 and counts[PENDING] == 0, "counts": counts}

    def report(self) -> dict:
        return {"format": "mlaser-commissioning/1", "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "dry_run": self.dry_run, "simulator": bool(self.machine.is_simulator()),
                "endpoint": list(self.machine.endpoint() or ()),
                "items": [asdict(it) for it in self.items.values()], "summary": self.summary(),
                "history": [asdict(h) for h in self.history]}

    def save_report(self) -> Optional[Path]:
        d = self.report_dir
        if d is None:
            from mlaser.services.config import data_dir
            d = data_dir() / "commissioning"
        try:
            d.mkdir(parents=True, exist_ok=True)
            p = d / time.strftime("commissioning-%Y%m%d-%H%M%S.json")
            p.write_text(json.dumps(self.report(), indent=1, ensure_ascii=False, default=str), "utf-8")
        except OSError:
            return None
        self.report_path = p
        return p

    def on_finish(self) -> None:
        self.heartbeat_hold(False)
        self.save_report()

    def on_abort(self) -> None:
        self.heartbeat_hold(False)
        self.save_report()


# =========================================================================== steps
def _network(p: CommissioningProcedure) -> dict:
    from mlaser import netcheck

    ep = p.machine.endpoint() or (netcheck.DEFAULT_HOST, netcheck.DEFAULT_PORT)
    fn = p.netcheck_fn or netcheck.run
    res = fn(host=ep[0], port=ep[1], probe=False)
    connected = bool(p.machine.state().connected)
    fails = [c.name for c in res.checks if c.status == netcheck.FAIL]
    warns = [c.name for c in res.checks if c.status == netcheck.WARN]
    if not connected:
        status, detail = FAIL, "wizard.comm.net_not_connected"
    elif fails:
        status, detail = FAIL, "wizard.comm.net_failed"
    elif warns:
        status, detail = WARN, "wizard.comm.net_warn"
    else:
        status, detail = PASS, "wizard.comm.net_ok"
    p.set_item("network", status, detail, host=ep[0], port=ep[1], local_ip=res.local_ip,
               failed=", ".join(fails), warnings=", ".join(warns), text=res.text())
    return {"ok": status != FAIL}


def _rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(b), 1e-9)


def _scaling(p: CommissioningProcedure) -> dict:
    sc = p.machine.scaling
    if sc is None:
        p.set_item("scaling", FAIL if p.machine.state().connected else SKIP, "wizard.comm.scale_unread")
        return {}
    # the parameters (ConfigService machine profile), not the controller's machine: that one
    # adopts the card's scaling at connect
    if p.config is not None:
        m = p.config.machine()
    else:
        from mlaser.nc.machine import Machine
        m = Machine()
    card_x, card_y = sc.step_ppm(sc.x_axis), sc.step_ppm(sc.y_axis)
    vals = {"card_x": round(card_x, 6), "card_y": round(card_y, 6), "par_x": m.pulses_per_mm_x,
            "par_y": m.pulses_per_mm_y, "card_period_ms": sc.period_ms, "par_period_ms": m.period_ms}
    bad = []
    if _rel(card_x, m.pulses_per_mm_x) > SCALE_TOL:
        bad.append("X")
    if _rel(card_y, m.pulses_per_mm_y) > SCALE_TOL:
        bad.append("Y")
    if abs(sc.period_ms - m.period_ms) > 1e-6:
        bad.append("period")
    if bad:
        p.set_item("scaling", FAIL, "wizard.comm.scale_mismatch", which=", ".join(bad), **vals)
    else:
        p.set_item("scaling", PASS, "wizard.comm.scale_ok", **vals)
    return vals


def _expected_limits(p: CommissioningProcedure) -> Optional[dict]:
    """{axis: (lo, hi) mm} that CNCModule +0x198 writes (MachineOps.soft_limit_pairs)."""
    ops = p.ops
    if ops is None:
        return None
    ppm = float(p.machine.scaling.position_ppm) if p.machine.scaling is not None else 0.0
    if ppm <= 0:
        return None
    out = {}
    for reg, (lo, hi) in ops.soft_limit_pairs():
        out[(reg - 50202) // 40] = (lo / ppm, hi / ppm)
    return out


def _soft_limits(p: CommissioningProcedure) -> dict:
    sc = p.machine.scaling
    if sc is None:
        p.set_item("soft_limits", SKIP, "wizard.comm.scale_unread")
        return {}
    card = dict(sc.soft_limits)
    try:
        want = _expected_limits(p)
    except Exception:  # noqa: BLE001 - parameters incomplete
        want = None
    vals: dict[str, Any] = {"card": {str(k): v for k, v in card.items()},
            "expected": {str(k): v for k, v in (want or {}).items()}}
    if not card:
        p.set_item("soft_limits", WARN, "wizard.comm.limits_none",
                   fix="wizard.comm.fix_limits" if want else "", **vals)
        return vals
    if want:
        bad = [a for a, (lo, hi) in want.items()
               if a not in card or abs(card[a][0] - lo) > LIMIT_TOL_MM or abs(card[a][1] - hi) > LIMIT_TOL_MM]
        if bad:
            p.set_item("soft_limits", FAIL, "wizard.comm.limits_mismatch", fix="wizard.comm.fix_limits",
                       axes=", ".join(map(str, bad)), **vals)
            return vals
    p.set_item("soft_limits", PASS, "wizard.comm.limits_ok", **vals)
    return vals


def _safety_io(p: CommissioningProcedure):
    """(what the controller runs with, the declared "not fitted" sensors)."""
    from mlaser.services import safety_io as S
    cur = S.current(p.machine)
    declared: frozenset = frozenset()
    if p.config is not None:
        try:
            declared = S.from_config(p.config).not_fitted
        except Exception:  # noqa: BLE001 - a config without the safety schema
            declared = frozenset()
    return cur, declared


def _names(srcs) -> str:
    from mlaser.services.safety_io import NAMES
    return ", ".join(NAMES[s] for s in srcs)


def _interlocks(p: CommissioningProcedure) -> dict:
    from mlaser.services.safety_io import INTERLOCKS
    cur, declared = _safety_io(p)
    if cur is None:
        p.set_item("interlocks", WARN, "wizard.comm.ilk_no_monitor", fix="wizard.comm.fix_safety_io")
        return {}
    wired = [s for s in INTERLOCKS if cur.configured(s)]
    missing = [s for s in INTERLOCKS if not cur.configured(s) and s not in declared]
    nf = [s for s in INTERLOCKS if not cur.configured(s) and s in declared]
    vals = {"wired": _names(wired), "missing": _names(missing), "declared": _names(nf),
            "inputs": {s: cur.input_of(s) for s in wired}, "heartbeat_output": cur.heartbeat_output}
    p.items["interlocks"].values.clear()
    if missing:
        p.set_item("interlocks", WARN, "wizard.comm.ilk_unconfigured", fix="wizard.comm.fix_safety_io", **vals)
    elif wired:
        p.set_item("interlocks", PENDING, "wizard.comm.ilk_configured", fix="wizard.comm.fix_safety_io", **vals)
    else:
        p.set_item("interlocks", PASS, "wizard.comm.ilk_none_wired", fix="wizard.comm.fix_safety_io", **vals)
    return vals


def _sim_input(p: CommissioningProcedure, n: int, alarm: bool, active_level: bool) -> None:
    """Dry run: play the operator's part on the simulator (input ``n`` at its alarm level)."""
    sim = getattr(p.machine, "sim", None)
    if sim is not None and hasattr(sim, "set_input"):
        sim.set_input(int(n), active_level if alarm else not active_level)


def _interlock_test(p: CommissioningProcedure) -> dict:
    """Trigger each wired sensor in turn: the monitor must report it active, then clear."""
    from mlaser.services.safety_io import INTERLOCKS
    p.require_connected()
    cur, _declared = _safety_io(p)
    mon = getattr(p.machine, "interlocks", None)
    if cur is None or mon is None:
        p.set_item("interlocks", SKIP, "wizard.comm.ilk_no_monitor")
        return {}
    wired = [s for s in INTERLOCKS if cur.configured(s)]
    results: dict[str, str] = {}
    # a few monitor periods: the active set reflects the inputs read after any recent change
    p.sleep(max(0.25, 4.0 * float(getattr(mon.cfg, "period_s", 0.05))))

    def wait_for(cond, seconds: float) -> bool:
        t_end = time.monotonic() + seconds
        while not cond():
            p.check_cancel()
            if time.monotonic() > t_end:
                return False
            p.sleep(0.05)
        return True

    for src in wired:
        n = cur.input_of(src)
        level = bool(getattr(cur.interlocks, INTERLOCKS[src][1]))
        active = lambda s=src: s in (getattr(mon, "active", {}) or {})
        p.set_item("interlocks", PENDING, "wizard.comm.ilk_waiting", name=_names([src]), input=n)
        p.log(f"interlock test: trigger the {src} sensor (input {n})")
        if active():
            results[src] = "stuck"           # alarm before the operator did anything
            continue
        if p.dry_run:
            _sim_input(p, n, True, level)
        if not wait_for(active, p.interlock_window_s):
            results[src] = "no_signal"
            continue
        p.set_item("interlocks", PENDING, "wizard.comm.ilk_release", name=_names([src]), input=n)
        if p.dry_run:
            _sim_input(p, n, False, level)
        results[src] = "ok" if wait_for(lambda s=src: not active(s), p.interlock_window_s) else "active"
    p.interlock_results = results
    failed = [s for s, r in results.items() if r != "ok"]
    vals = dict(p.items["interlocks"].values)
    vals.pop("name", None)
    vals.pop("input", None)
    vals.update(results={s: r for s, r in results.items()}, failed=_names(failed),
                tested=_names([s for s, r in results.items() if r == "ok"]))
    it = p.items["interlocks"]
    it.values.clear()
    if failed:
        p.set_item("interlocks", FAIL, "wizard.comm.ilk_failed", fix="wizard.comm.fix_safety_io", **vals)
    elif vals.get("missing"):
        p.set_item("interlocks", WARN, "wizard.comm.ilk_tested_partial", fix="wizard.comm.fix_safety_io", **vals)
    else:
        p.set_item("interlocks", PASS, "wizard.comm.ilk_tested", **vals)
    return results


def _homing(p: CommissioningProcedure) -> dict:
    p.require_connected()
    p.operator_action("home XY")
    t0 = time.monotonic()
    p.machine.home("XY")
    p.sleep(0.2)
    try:
        p.wait(lambda: p.machine.state().homed, 120.0, "homing", poll_s=0.05)
    except StepFailed:
        p.set_item("homing", FAIL, "wizard.comm.home_timeout")
        raise
    dt = time.monotonic() - t0
    st = p.machine.state()
    p.set_item("homing", PASS, "wizard.comm.home_ok", seconds=round(dt, 2), x=round(st.x, 3), y=round(st.y, 3))
    return {"seconds": dt}


def _heartbeat(p: CommissioningProcedure) -> dict:
    hb = p.machine.heartbeat_status()
    if not hb.get("output"):
        p.set_item("heartbeat", FAIL, "wizard.comm.hb_off")
        return hb
    n0 = hb["toggles"]
    t0 = time.monotonic()
    p.sleep(p.relay_window_s)
    hb2 = p.machine.heartbeat_status()
    rate = (hb2["toggles"] - n0) / max(1e-6, time.monotonic() - t0)
    want = 2.0 * float(hb["hz"])
    vals = {"output": hb["output"], "hz": hb["hz"], "toggles_per_s": round(rate, 2), "expected": want}
    if want <= 0 or rate < 0.6 * want:
        p.set_item("heartbeat", FAIL, "wizard.comm.hb_slow", **vals)
    else:
        p.set_item("heartbeat", PASS, "wizard.comm.hb_ok", **vals)
    return vals


def _jog(p: CommissioningProcedure, axis: str) -> dict:
    p.require_connected()
    key = f"jog_{axis.lower()}"
    p.operator_action(f"jog {axis}+")
    st0 = p.machine.state()
    a0 = st0.x if axis == "X" else st0.y
    p.machine.jog_step(axis, +1, JOG_STEP_MM)
    p.sleep(0.15)
    last_v: list[Optional[float]] = [None]
    last_t = [time.monotonic()]

    def settled() -> bool:
        st = p.machine.state()
        v = st.x if axis == "X" else st.y
        prev = last_v[0]
        if prev is None or abs(v - prev) > 1e-3:
            last_v[0], last_t[0] = v, time.monotonic()
            return False
        return time.monotonic() - last_t[0] > 0.3 and st.mode != "jog"
    p.wait(settled, 30.0, f"jog {axis}", poll_s=0.05)
    st1 = p.machine.state()
    d = (st1.x if axis == "X" else st1.y) - a0
    p.jog_moved[axis] = d
    if d < 0.5 * JOG_STEP_MM:
        p.set_item(key, FAIL, "wizard.comm.jog_position", fix="wizard.comm.fix_reverse", axis=axis,
                   moved=round(d, 3), step=JOG_STEP_MM)
    else:
        p.set_item(key, PENDING, "wizard.comm.jog_moved", axis=axis, moved=round(d, 3), step=JOG_STEP_MM)
    return {"moved": d}


__all__ = ["CommissioningProcedure", "CheckItem", "ITEMS", "PASS", "WARN", "FAIL", "SKIP", "PENDING",
           "AXIS_REVERSE"]
