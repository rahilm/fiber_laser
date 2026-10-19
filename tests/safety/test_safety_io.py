"""Safety I/O from the ConfigService (review 2, P0-4): the gas-low, door and laser-alarm
interlocks and the "PC alive" heartbeat are typed, persisted settings, reach the controller at
start-up and after a change (never under a running job or an armed machine), and every one that
is not configured is reported (Run screen, pre-flight, commissioning checklist)."""
from __future__ import annotations

import json
import threading
import time

import pytest

from mlaser.services import safety_io as S
from mlaser.services.config import ConfigService


def wait_for(pred, timeout=5.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def cfg(tmp_path):
    return ConfigService(tmp_path / "config.json")


@pytest.fixture
def sim_ctl():
    from mlaser.controller.ui_adapter import UiController
    made = []

    def make(**kw):
        ctl = UiController(poll_s=0.02, **kw)
        made.append(ctl)
        assert ctl.connect_simulator(time_scale=1.0, pessimistic=True)
        return ctl

    yield make
    for c in made:
        try:
            c.disconnect()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------- schema
def test_settings_are_typed_validated_and_persisted(cfg, tmp_path):
    for key in ("safety.gas_low_input", "safety.door_input", "safety.laser_alarm_input",
                "safety.heartbeat_output", "safety.heartbeat_hz", "safety.door_open_active",
                "safety.door_not_fitted", "safety.follower"):
        assert cfg.setting(key) is not None and cfg.setting(key).section == "safety", key
    # defaults: nothing wired (the hardware is unknown), the follower interlock on
    assert cfg.get("safety.door_input") == 0 and cfg.get("safety.heartbeat_output") == 0
    assert cfg.get("safety.follower") is True and cfg.get("safety.heartbeat_hz") == 5.0
    with pytest.raises(ValueError):
        cfg.set("safety.door_input", 29)              # inputs are 1..28
    with pytest.raises(ValueError):
        cfg.set("safety.heartbeat_output", 27)        # outputs are 1..26
    with pytest.raises(ValueError):
        cfg.set("safety.gas_low_input", 2.5)
    cfg.set("safety.door_input", "5")
    cfg.set("safety.door_open_active", "false")
    cfg.set("safety.heartbeat_output", 7)
    cfg.save()
    again = ConfigService(tmp_path / "config.json")
    sio = S.from_config(again)
    assert sio.interlocks.door_input == 5 and sio.interlocks.door_open_active is False
    assert sio.heartbeat_output == 7 and sio.heartbeat_hz == 5.0
    # an out-of-range value in a hand-edited file falls back to the default (not wired)
    data = json.loads((tmp_path / "config.json").read_text())
    data["values"]["safety.gas_low_input"] = 99
    (tmp_path / "config.json").write_text(json.dumps(data))
    assert S.from_config(ConfigService(tmp_path / "config.json")).interlocks.gas_low_input == 0


def test_notices_for_every_unconfigured_interlock(cfg):
    sio = S.from_config(cfg)
    assert [n.source for n in S.notices(sio)] == ["gas", "door", "laser", "heartbeat"]
    assert not any(n.declared for n in S.notices(sio))
    cfg.set("safety.door_input", 3)
    cfg.set("safety.laser_not_fitted", True)
    sio = S.from_config(cfg)
    ns = {n.source: n for n in S.notices(sio)}
    assert set(ns) == {"gas", "laser", "heartbeat"}
    assert ns["laser"].declared and not ns["gas"].declared
    assert "not configured" in ns["gas"].message and "not fitted" in ns["laser"].message
    # the pre-flight warns for a laser job, not for frame / dry run, and not for a declared sensor
    codes = {i.code for i in S.preflight_issues(sio, "start", True)}
    assert codes == {"interlock_unconfigured_gas", "heartbeat_unconfigured"}
    assert all(i.severity == "warning" for i in S.preflight_issues(sio, "start", True))
    assert S.preflight_issues(sio, "frame", False) == [] and S.preflight_issues(sio, "start", False) == []


def test_problems_and_heartbeat_on_a_process_output_stays_off(cfg):
    cfg.set("safety.door_input", 4)
    cfg.set("safety.gas_low_input", 4)
    cfg.set("safety.heartbeat_output", 3)
    sio = S.from_config(cfg)
    probs = S.problems(sio, {"laser": 3, "gas": 1})
    assert any("both on input 4" in p for p in probs)
    assert any("'laser' process output" in p for p in probs)
    eff = S.effective(sio, {"laser": 3})
    assert eff.heartbeat_output == 0 and eff.interlocks == sio.interlocks


# ---------------------------------------------------------------------------- controller
def test_start_up_passes_the_configuration_to_the_controller(cfg):
    from mlaser.ui.__main__ import make_controller
    cfg.set("safety.door_input", 5)
    cfg.set("safety.gas_low_input", 6)
    cfg.set("safety.laser_alarm_input", 7)
    cfg.set("safety.laser_alarm_active", False)
    cfg.set("safety.heartbeat_output", 9)
    cfg.set("safety.heartbeat_hz", 2.5)
    ctl = make_controller("127.0.0.1", 502, None, True, cfg)
    c = ctl.interlocks.cfg
    assert (c.door_input, c.gas_low_input, c.laser_alarm_input, c.laser_alarm_active) == (5, 6, 7, False)
    hb = ctl.heartbeat_status()
    assert hb["output"] == 9 and hb["hz"] == 2.5
    # nothing configured: the controller runs with everything off, as before
    ctl0 = make_controller("127.0.0.1", 502, None, True, ConfigService(load=False))
    assert ctl0.interlocks.cfg.door_input == 0 and ctl0.heartbeat_status()["output"] == 0


def test_start_up_refuses_a_heartbeat_on_the_laser_output(cfg):
    from mlaser.ui.__main__ import make_controller
    probe = make_controller("127.0.0.1", 502, None, True, cfg)
    laser_out = next(int(v) for k, v in probe.output_map.items() if int(v) > 0)
    cfg.set("safety.heartbeat_output", laser_out)
    cfg.set("safety.door_input", 2)
    ctl = make_controller("127.0.0.1", 502, None, True, cfg)
    assert ctl.heartbeat_status()["output"] == 0          # never toggled at 5 Hz
    assert ctl.interlocks.cfg.door_input == 2             # the interlocks still apply


def test_apply_on_change_and_the_heartbeat_moves(sim_ctl, cfg):
    ctl = sim_ctl(heartbeat_output=7)
    assert wait_for(lambda: ctl.heartbeat_status()["toggles"] > 2)
    cfg.set("safety.heartbeat_output", 8)
    cfg.set("safety.door_input", 3)
    assert S.apply(ctl, S.from_config(cfg)) == "applied"
    assert ctl.interlocks.cfg.door_input == 3
    assert wait_for(lambda: not ctl.sim.state.outputs >> 6 & 1)            # old output 7 off
    n = ctl.heartbeat_status()["toggles"]
    assert wait_for(lambda: ctl.heartbeat_status()["toggles"] > n + 2)     # toggling on 8
    assert ctl.heartbeat_status()["output"] == 8
    assert S.apply(ctl, S.from_config(cfg)) == "unchanged"


def test_change_is_pending_while_armed_or_running(sim_ctl, cfg):
    ctl = sim_ctl()
    assert ctl.arm()
    cfg.set("safety.door_input", 3)
    assert S.apply(ctl, S.from_config(cfg)) == "pending"
    assert ctl.interlocks.cfg.door_input == 0                  # nothing changed under the armed machine
    ctl.disarm("test")
    assert S.apply(ctl, S.from_config(cfg)) == "applied"
    assert ctl.interlocks.cfg.door_input == 3


def test_configured_door_interlock_refuses_arming(sim_ctl, cfg):
    """End to end: config -> controller -> the door input holds -> arming is refused."""
    ctl = sim_ctl()
    cfg.set("safety.door_input", 4)
    assert S.apply(ctl, S.from_config(cfg)) == "applied"
    ctl.sim.set_input(4, True)
    assert wait_for(lambda: "door" in ctl.interlocks.active)
    assert ctl.arm() is False
    ctl.sim.set_input(4, False)
    assert wait_for(lambda: "door" not in ctl.interlocks.active)
    assert ctl.arm() is True
    ctl.disarm("test")


def test_controller_api_is_used_when_present(cfg):
    """The Safety-core API (``apply_safety_config``) takes precedence over the fallback."""
    calls = []

    class Ctl:
        output_map = {"laser": 1}

        def apply_safety_config(self, **kw):
            calls.append(kw)
            return True

        def job_active(self):
            return False

        def safety(self):
            class St:
                armed = False
            return St()

    cfg.set("safety.door_input", 2)
    cfg.set("safety.heartbeat_output", 1)            # the laser output: refused, kept off
    assert S.apply(Ctl(), S.from_config(cfg)) == "applied"
    assert calls and calls[0]["interlocks"].door_input == 2 and calls[0]["heartbeat_output"] == 0
    assert S.apply(object(), S.from_config(cfg)) == "unsupported"


# ---------------------------------------------------------------------------- commissioning
def _netcheck_ok(host, port, probe=False):
    from mlaser import netcheck
    return netcheck.NetCheckResult(host, port, "test", [netcheck.Check("route", netcheck.OK, "ok")],
                                   local_ip="127.0.0.1")


def _run_until(p, step_id):
    from mlaser.services.procedures import engine as PE
    if p.state == PE.READY:
        p.start()
    for _ in range(40):
        s = p.step
        if s is None or s.id == step_id or p.state in PE.FINAL:
            return
        if s.kind == "action" and s.id not in p.done:
            assert p.execute(), p.error
            continue
        if s.kind == "question":
            p.answer(True)
        p.next()


def test_commissioning_flags_unconfigured_interlocks(sim_ctl, cfg, tmp_path):
    from mlaser.services.procedures import commissioning as CM
    ctl = sim_ctl()
    p = CM.CommissioningProcedure(ctl, config=cfg, report_dir=tmp_path, netcheck_fn=_netcheck_ok)
    _run_until(p, "homing")
    it = p.items["interlocks"]
    assert it.status == CM.WARN and it.detail == "wizard.comm.ilk_unconfigured"
    assert "gas-low" in it.values["missing"] and "door" in it.values["missing"]
    assert it.fix == "wizard.comm.fix_safety_io"
    assert "interlock_test" not in p.done                       # nothing wired: no wiring test
    # declared "not fitted" for all three: no longer flagged
    for src in ("gas", "door", "laser"):
        cfg.set(f"safety.{src}_not_fitted", True)
    assert p.recheck_interlocks().status == CM.PASS


def test_commissioning_wiring_test(sim_ctl, cfg, tmp_path):
    from mlaser.services.procedures import commissioning as CM
    ctl = sim_ctl()
    cfg.set("safety.door_input", 3)
    cfg.set("safety.gas_low_input", 5)
    cfg.set("safety.gas_low_active", False)           # normally closed: input off = alarm
    cfg.set("safety.laser_not_fitted", True)
    ctl.sim.set_input(5, True)                        # the gas switch is closed (pressure ok)
    assert S.apply(ctl, S.from_config(cfg)) == "applied"
    time.sleep(0.2)
    assert wait_for(lambda: not ctl.interlocks.active)
    p = CM.CommissioningProcedure(ctl, config=cfg, report_dir=tmp_path, netcheck_fn=_netcheck_ok,
                                  interlock_window_s=5.0)
    _run_until(p, "interlock_test")
    assert p.items["interlocks"].status == CM.PENDING

    def operator():                                   # gas: drop, restore; door: open, close
        for n, alarm, idle in ((5, False, True), (3, True, False)):
            assert wait_for(lambda n=n: p.items["interlocks"].values.get("input") == n, 5.0)
            time.sleep(0.2)
            ctl.sim.set_input(n, alarm)
            time.sleep(0.3)
            ctl.sim.set_input(n, idle)
    t = threading.Thread(target=operator, daemon=True)
    t.start()
    assert p.execute(), p.error
    t.join(5)
    it = p.items["interlocks"]
    assert it.status == CM.PASS, (it.detail, p.interlock_results)
    assert p.interlock_results == {"gas": "ok", "door": "ok"}
    assert not ctl.safety().fault                     # disarmed and idle: a test never faults


def test_commissioning_wiring_test_no_signal_fails(sim_ctl, cfg, tmp_path):
    from mlaser.services.procedures import commissioning as CM
    ctl = sim_ctl()
    cfg.set("safety.door_input", 3)
    for src in ("gas", "laser"):
        cfg.set(f"safety.{src}_not_fitted", True)
    S.apply(ctl, S.from_config(cfg))
    p = CM.CommissioningProcedure(ctl, config=cfg, report_dir=tmp_path, netcheck_fn=_netcheck_ok,
                                  interlock_window_s=0.4)
    _run_until(p, "interlock_test")
    p.execute()
    it = p.items["interlocks"]
    assert it.status == CM.FAIL and p.interlock_results == {"door": "no_signal"}
    assert not p.summary()["ok"]


def test_commissioning_dry_run_toggles_the_simulator(sim_ctl, cfg, tmp_path):
    from mlaser.services.procedures import commissioning as CM
    ctl = sim_ctl()
    cfg.set("safety.door_input", 2)
    cfg.set("safety.door_open_active", False)
    for src in ("gas", "laser"):
        cfg.set(f"safety.{src}_not_fitted", True)
    S.apply(ctl, S.from_config(cfg))
    ctl.sim.set_input(2, True)                         # door closed (NC contact)
    p = CM.CommissioningProcedure(ctl, config=cfg, report_dir=tmp_path, netcheck_fn=_netcheck_ok,
                                  interlock_window_s=3.0, dry_run=True)
    _run_until(p, "interlock_test")
    assert p.execute(), p.error
    assert p.items["interlocks"].status == CM.PASS and p.interlock_results == {"door": "ok"}
