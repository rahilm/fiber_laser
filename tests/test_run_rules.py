"""The run rules live in a Qt-free module (review 2026-11-09, arch A15): which run controls are
enabled and why, and the run state, decided without a widget; the old names stay importable from
``mlaser.ui.safety_ui`` (the same objects)."""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace as NS

from mlaser.ui import run_rules as R


def test_the_rules_import_without_qt():
    code = ("import sys; sys.modules['PySide6'] = None; sys.modules['shiboken6'] = None\n"
            "import mlaser.ui.run_rules as R\n"
            "assert R.run_state(None, None) == 'offline'\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr


def test_safety_ui_reexports_the_same_rules():
    from mlaser.ui import safety_ui as S
    for name in ("run_state", "run_enabled", "active_alarms", "alarm_text", "job_running", "JOB_MODES", "_tr"):
        assert getattr(S, name) is getattr(R, name), name


def _st(**kw):
    d = dict(banner="DISARMED", armed=False, dry_run=False)
    d.update(kw)
    return NS(**d)


def _ms(mode="ready", connected=True):
    return NS(mode=mode, connected=connected)


def test_run_state_order():
    assert R.run_state(_st(banner="FAULT"), _ms(), [NS(kind="estop")]) == "fault"
    assert R.run_state(_st(), _ms(), [NS(kind="estop")]) == "estop"
    assert R.run_state(_st(), _ms(), [NS(kind="servo")]) == "alarm"
    assert R.run_state(_st(), _ms(connected=False)) == "offline"
    assert R.run_state(_st(), _ms("pause")) == "hold"
    assert R.run_state(_st(), _ms("process")) == "running"
    assert R.run_state(_st(), _ms("origin")) == "homing"
    assert R.run_state(_st(), _ms("jog")) == "moving"
    assert R.run_state(_st(), _ms(), need_home=True) == "not_homed"
    assert R.run_state(_st(), _ms()) == "ready"


def test_run_enabled_without_a_widget():
    win = NS(ctl=None)                       # no catalogue: the default texts
    on = {k: (ok, why) for k, ok, why in R.run_enabled(win, _st(), _ms(), False, alarms=[])}
    assert all(on[k][0] for k in ("start", "stop", "frame", "dry_run", "arm")) and not on["hold"][0]
    off = {k: (ok, why) for k, ok, why in R.run_enabled(win, _st(), _ms(connected=False), False, alarms=[])}
    assert off["stop"] == (True, "") and not any(off[k][0] for k in ("start", "hold", "frame", "dry_run", "arm"))
    alarm = NS(kind="estop", text=lambda tr=None: "E-stop pressed")
    al = {k: (ok, why) for k, ok, why in R.run_enabled(win, _st(), _ms(), False, alarms=[alarm])}
    assert not al["start"][0] and al["start"][1] == "Reset the alarm first: E-stop pressed"
    assert not al["arm"][0] and al["stop"][0]
    run = {k: ok for k, ok, _ in R.run_enabled(win, _st(armed=True), _ms("process"), True, alarms=[])}
    assert run == {"start": False, "hold": True, "stop": True, "frame": False, "dry_run": False, "arm": True}
