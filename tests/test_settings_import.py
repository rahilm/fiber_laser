"""Settings import from an original installation: preview (nothing changes), apply (chosen
areas), and the install is never written."""
import hashlib
import json
from pathlib import Path

import pytest

from mlaser.formats.params import MachineParams, param_defs
from mlaser.services import settings_import as SI
from mlaser.services.alarm_history import AlarmHistory
from mlaser.services.config import ConfigService

IP_INI = """[IP]
CardIP=192.168.5.20
CardSubnet=255.255.255.0
CardGateway=10.1.1.1
CardPort=502
ZFIP=10.1.1.169
LaserIP=10.1.1.170
MCTimeout=500
"""


def make_install(root: Path, nexcut_inside: bool = True) -> tuple[Path, Path]:
    inst = root / "Mlaser"
    (inst / "File").mkdir(parents=True)
    (inst / "File" / "ipAdd.ini").write_bytes(IP_INI.encode("gbk"))
    (inst / "JumpAddTime.txt").write_text("[Jump]\nAddTime=150\n[Axis4Freq]\nIs4Freq=0\n")
    (inst / "Help").mkdir()
    (inst / "Help" / "calib.csv").write_text("413023, 0\r\n0, 406507\r\n200, 410000\r\n")
    (inst / "Lang").mkdir()
    (inst / "Lang" / "German.txt").write_text("mf15#Referenzfahrt\nmf16#Nur X\n", encoding="utf-8")
    pd = (inst / "NexCut") if nexcut_inside else (root / "LocalAppData" / "NexCut")
    mp = MachineParams.defaults()
    for d in param_defs():
        if d.key == "MAC.SoftLimitMaxLen":
            mp.hard.set(d, 3050.0)
        if d.key == "GC.GasDelay":
            (mp.hard if d.file == "HardPara.xml" else mp.manu).set(d, 250)
    mp.save_dir(pd)
    return inst, pd


def tree_digest(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def cfg(tmp_path):
    return ConfigService(tmp_path / "cfg" / "config.json")


def test_preview_lists_changes_and_changes_nothing(tmp_path, cfg):
    inst, pd = make_install(tmp_path)
    plan = SI.preview(inst, cfg)
    assert plan.param_dir == pd.resolve()
    by = plan.by_area()
    keys = {c.key: c for c in by["params"]}
    assert keys["MAC.SoftLimitMaxLen"].old == 1000.0 and keys["MAC.SoftLimitMaxLen"].new == 3050.0
    assert keys["GC.GasDelay"].new == 250
    assert {c.key: c.new for c in by["ip"]} == {"ip.pd445": "192.168.5.20"}
    assert by["jump"][0].old == 200.0 and by["jump"][0].new == 150.0
    assert by["calib"][0].new == 2
    assert plan.layers == 11
    assert [(c, n) for c, _, n in plan.languages] == [("de", 2)]
    assert set(plan.available()) == {"params", "layers", "ip", "jump", "calib", "lang"}
    assert not cfg.values and not cfg.path.exists()        # preview wrote nothing


def test_apply_takes_the_chosen_areas_and_never_writes_the_install(tmp_path, cfg):
    inst, pd = make_install(tmp_path)
    before = tree_digest(inst)
    cfg.save()
    plan = SI.preview(inst, cfg)
    data, app, lang = tmp_path / "data", tmp_path / "app", tmp_path / "lang"
    res = SI.apply(plan, cfg, data_root=data, app_root=app, lang_dest=lang)
    assert res.applied == {"params": 4, "layers": 11, "ip": 1, "jump": 1, "calib": 1, "lang": 1}
    assert tree_digest(inst) == before                     # read only
    assert cfg.get("MAC.SoftLimitMaxLen") == 3050.0 and cfg.profile.base == "params"
    assert cfg.get("ip")["pd445"] == "192.168.5.20"
    assert cfg.machine().jump_add_time_ms == 150.0
    assert cfg.get("machine_ops.param_dir") == str(data / "params")
    assert (data / "params" / "LayerPara.xml").exists() and (app / "File" / "ipAdd.ini").exists()
    assert cfg.get("machine.calib_csv") == str(data / "calib.csv")
    assert json.loads((lang / "de.json").read_text("utf-8"))["legacy_strings"]["mf15"] == "Referenzfahrt"
    assert res.config_backup is not None and res.config_backup.exists()
    saved = json.loads(cfg.path.read_text())
    assert saved["values"]["MAC.SoftLimitMaxLen"] == 3050.0


def test_apply_selected_areas_only(tmp_path, cfg):
    inst, _ = make_install(tmp_path, nexcut_inside=False)
    plan = SI.preview(inst, cfg, tmp_path / "LocalAppData" / "NexCut")
    res = SI.apply(plan, cfg, ["ip"], data_root=tmp_path / "d", app_root=tmp_path / "a", lang_dest=tmp_path / "l")
    assert res.applied == {"ip": 1}
    assert not cfg.is_set("MAC.SoftLimitMaxLen") and cfg.machine().jump_add_time_ms == 200.0


def test_refuses_to_write_back_into_the_install(tmp_path, cfg):
    inst, _ = make_install(tmp_path)
    before = tree_digest(inst)
    plan = SI.preview(inst, cfg)
    with pytest.raises(SI.WriteBackError):
        SI.apply(plan, cfg, data_root=inst / "mlaser-data", app_root=tmp_path / "a")
    with pytest.raises(SI.WriteBackError):
        SI.apply(plan, cfg, data_root=tmp_path / "d", app_root=inst)          # app dir = the install
    assert tree_digest(inst) == before


def test_missing_parameter_folder_is_reported(tmp_path, cfg):
    inst = tmp_path / "Bare"
    (inst / "File").mkdir(parents=True)
    plan = SI.preview(inst, cfg)
    assert "no_params" in plan.warnings and plan.available() == []
    assert SI.preview(tmp_path / "nope", cfg).errors


# ---------------------------------------------------------------------------- alarm history
def test_alarm_history_records_raise_clear_fault(tmp_path):
    from mlaser.controller.alarms import Alarm
    from mlaser.machine import events as EV
    bus = EV.EventBus()
    h = AlarmHistory(tmp_path / "h.jsonl")
    h.attach(bus)
    a = Alarm(8104, "", "X servo input alarm", 0, "servo")
    bus.publish(EV.AlarmRaised(alarms=(a,), active=(a,)))
    bus.publish(EV.AlarmRaised(alarms=(a,), active=(a,)))           # still active: one record
    bus.publish(EV.StateChanged("disarmed", "fault", "link lost", "link lost"))
    assert [r.kind for r in h.active()] == ["fault", "alarm"]
    bus.publish(EV.AlarmCleared(alarms=(a,), active=()))
    bus.publish(EV.StateChanged("fault", "disarmed", "reset"))
    recs = h.entries(newest_first=False)
    assert [(r.kind, r.code, r.alarm_kind) for r in recs] == [("alarm", 8104, "servo"), ("fault", None, "card")]
    assert all(not r.active for r in recs) and recs[0].duration_s >= 0
    h.detach()
    h2 = AlarmHistory(tmp_path / "h.jsonl")
    assert [r.text for r in h2.entries(False)] == ["X servo input alarm", "link lost"]
    h2.clear()
    assert AlarmHistory(tmp_path / "h.jsonl").entries() == []
