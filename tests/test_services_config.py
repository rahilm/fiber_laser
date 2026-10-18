"""ConfigService (mlaser.services.config, Qt-free): the schema generated from the parameter
registry, validation, persistence, XML import/export and the machine profile."""
from __future__ import annotations

import json

import pytest

from mlaser.formats.params import MachineParams, param_defs
from mlaser.nc.machine import Machine
from mlaser.services.config import ConfigService, Setting


@pytest.fixture()
def cfg(tmp_path):
    return ConfigService(tmp_path / "config.json")


def test_schema_from_registry(cfg):
    keyed = {d.key for d in param_defs() if "." in d.key}
    assert keyed <= {k.split(":", 1)[-1] for k in cfg.schema}
    s = cfg.setting("GC.GasDelay")
    assert (s.kind, s.default, s.unit, s.min, s.max, s.file) == ("int", 100, "ms", 0.0, 10000.0, "ManuPara.xml")
    assert cfg.setting("A0.EnableType").kind == "enum" and len(cfg.setting("A0.EnableType").options) == 3
    assert cfg.setting("A0.DoubleDriver").kind == "bool"
    assert "ManuParam" in cfg.sections() and cfg.section("GasParam")


def test_validation(cfg):
    assert cfg.set("GC.GasDelay", "250") == 250
    with pytest.raises(ValueError):
        cfg.set("GC.GasDelay", 20000)
    with pytest.raises(ValueError):
        cfg.set("GC.GasDelay", 1.5)
    with pytest.raises(ValueError):
        cfg.set("A0.EnableType", 7)
    assert cfg.set("A0.DoubleDriver", "1") is True
    assert cfg.get("GC.GasDelay") == 250 and cfg.get("GC.DirectGasDelay") == 100   # default
    cfg.reset("GC.GasDelay")
    assert cfg.get("GC.GasDelay") == 100 and not cfg.is_set("GC.GasDelay")
    with pytest.raises(KeyError):
        cfg.register([Setting("GC.GasDelay", "int", 1)])


def test_mapping_view_behaves_like_the_old_settings_dict(cfg):
    m = cfg.mapping()
    assert "SP.KeyboardMoveStep" not in m and m.get("SP.KeyboardMoveStep", 1.0) == 1.0
    m["ip"] = {"pd445": "10.1.1.2"}                    # free-form application value
    d = m.setdefault("machine_ops", {})
    d["x"] = 1
    assert cfg.get("machine_ops") == {"x": 1}
    m.update({"SOP.PlanManuCount": 3})
    assert cfg.get("SOP.PlanManuCount") == 3 and "SOP.PlanManuCount" in m
    with pytest.raises(ValueError):
        m["GC.GasDelay"] = -5
    ns = cfg.namespace("machine_ops")
    cfg.set("MC.XFastMoveSpeed", 150.0)
    assert ns.get("MC.XFastMoveSpeed") == 150.0         # Options reach the component
    ns["coord"] = 3
    assert cfg.get("machine_ops.coord") == 3


def test_persistence(tmp_path):
    a = ConfigService(tmp_path / "c.json")
    a.set("GC.GasDelay", 300)
    a.set("ip", {"pd445": "1.2.3.4"})
    a.set_profile("params")
    a.save()
    data = json.loads((tmp_path / "c.json").read_text())
    assert data["values"]["GC.GasDelay"] == 300 and data["profile"]["base"] == "params"
    b = ConfigService(tmp_path / "c.json")
    assert b.get("GC.GasDelay") == 300 and b.get("ip") == {"pd445": "1.2.3.4"} and b.profile.base == "params"


def test_xml_import_export_round_trip(cfg, tmp_path):
    mp = MachineParams.defaults()
    d = next(x for x in param_defs() if x.key == "GC.GasDelay")
    mp.manu.set(d, 420)
    dz = next(x for x in param_defs() if x.key == "ZF.ZFType")
    mp.hard.set(dz, 0)
    src = tmp_path / "dat"
    mp.save_dir(src)
    n = cfg.import_dat(src)
    assert n >= 1 and cfg.get("GC.GasDelay") == 420 and cfg.profile.base == "params"
    out = tmp_path / "out"
    cfg.export_dat(out)
    back = MachineParams.load_dir(out)
    assert back.manu.get(d) == 420
    assert sorted(p.name for p in out.iterdir()) == ["HardPara.xml", "LayerPara.xml", "ManuPara.xml"]


def test_machine_profile_feeds_machine_motion_and_process(cfg):
    assert cfg.machine() == Machine()                 # nothing set: exactly today's machine
    cfg.set("GC.GasDelay", 500)                       # a ManuPara option (GasParam section)
    m = cfg.machine()
    assert m.process.gas_delay_ms == 500 and m.process.zf_type == Machine().process.zf_type
    cfg.set("AX.InterpolationCycle", 2000)            # HardPara: servo period
    assert cfg.machine().period_ms == 2.0 and cfg.motion_config().period_ms == 2.0
    cfg.set("MP.EnableSmallCircleSpeedLimit", True)   # read by Machine.motion_config()
    assert cfg.motion_config().small_circle_limit is True
    assert cfg.process_config().gas_delay_ms == 500
    cfg.set_profile("params")                         # the machine described by the parameters
    assert cfg.machine().process.gas_valves == (0, 0, 0, 0, 0, 0)


def test_data_and_config_dirs(monkeypatch, tmp_path):
    from mlaser.services import config as C
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("MLASER_CONFIG_DIR", str(tmp_path / "c"))
    assert C.data_dir() == tmp_path / "d" and C.config_dir() == tmp_path / "c"
    assert ConfigService().path == tmp_path / "c" / "config.json"
