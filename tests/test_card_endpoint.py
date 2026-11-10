"""The card endpoint is one record (architecture review A10): the settings import stores the
port, transport and timing it reports (and the process values of ipAdd.ini), and the controller
made at start, the Machine workspace and the network self-test read that record."""
from __future__ import annotations

import pytest

from mlaser.nc.machine import Machine
from mlaser.services import endpoint as EP
from mlaser.services import settings_import as SI
from mlaser.services.config import ConfigService

INI = (b"CardIP=192.168.0.50\r\nCardPort=1502\r\nAccessType=0\r\nMCTimeout=900\r\n"
       b"FollowOvertime=5000\r\nSectionDrillOvertime=7000\r\nDAMinVal=80\r\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, d in (("MLASER_CONFIG_DIR", "config"), ("MLASER_DATA_DIR", "data"), ("MLASER_APP_DIR", "app"),
                 ("MLASER_PARAM_DIR", "param"), ("MLASER_LANG_DIR", "lang")):
        monkeypatch.setenv(k, str(tmp_path / d))
    return tmp_path


def _install(root, cfg):
    inst = root / "Mlaser"
    (inst / "File").mkdir(parents=True)
    (inst / "dat").mkdir()
    cfg.export_dat(inst / "dat")
    (inst / "File" / "ipAdd.ini").write_bytes(INI)
    return inst


def test_import_stores_what_it_reports(env):
    c = ConfigService()
    inst = _install(env, c)
    plan = SI.preview(inst, c)
    ip_changes = {ch.key: ch.new for ch in plan.changes if ch.area == "ip"}
    assert ip_changes == {"ip.pd445": "192.168.0.50", "ip.port": 1502, "ip.transport": "tcp",
                          "ip.timeout_ms": 900, "process.follow_timeout_ms": 5000,
                          "process.drill_timeout_ms": 7000, "process.da_min": 80}
    res = SI.apply(plan, c)
    assert res.applied["ip"] == len(ip_changes)
    c2 = ConfigService()                                   # the next start
    rec = c2.get("ip")
    assert (rec["pd445"], rec["port"], rec["transport"], rec["timeout_ms"]) == ("192.168.0.50", 1502, "tcp", 900)
    # the GUI's machine has the original's process values (as Machine.from_dat_dir with the ini)
    ref = Machine.from_dat_dir(str(inst / "dat"), str(inst / "File" / "ipAdd.ini"))
    p = c2.machine().process
    assert (p.follow_timeout_ms, p.drill_timeout_ms, p.da_min) == (
        ref.process.follow_timeout_ms, ref.process.drill_timeout_ms, ref.process.da_min) == (5000, 7000, 80)
    # every reader of the endpoint gets the same one
    tc = EP.transport_config(EP.record_of(c2))
    assert (tc.host, tc.port, tc.transport, tc.timeout_ms) == ("192.168.0.50", 1502, "tcp", 900)
    from mlaser import netcheck
    assert netcheck.resolve_card() == ("192.168.0.50", 1502, "configuration")
    from mlaser.ui.__main__ import make_controller
    ctl = make_controller(None, None, None, True, c2)
    assert (ctl.cfg.host, ctl.cfg.port, ctl.cfg.transport, ctl.cfg.timeout_ms) == ("192.168.0.50", 1502, "tcp", 900)
    # the command line still wins over the record, --ini over both
    ctl = make_controller("127.0.0.1", None, None, True, c2)
    assert (ctl.cfg.host, ctl.cfg.port, ctl.cfg.transport) == ("127.0.0.1", 1502, "tcp")
    ini = env / "other.ini"
    ini.write_bytes(b"CardIP=10.9.9.9\r\nCardPort=503\r\n")
    ctl = make_controller(None, None, str(ini), True, c2)
    assert (ctl.cfg.host, ctl.cfg.port, ctl.cfg.transport, ctl.cfg.timeout_ms) == ("10.9.9.9", 503, "udp", 500)


def test_nothing_stored_is_the_default(env):
    c = ConfigService()
    tc, src = EP.resolve(EP.record_of(c))
    assert (tc.host, tc.port, tc.transport, src) == ("10.1.1.168", 502, "udp", "default")
    from mlaser.ui.__main__ import make_controller
    ctl = make_controller(None, None, None, True, c)
    assert (ctl.cfg.host, ctl.cfg.port) == ("10.1.1.168", 502)


def test_bad_record_values_are_ignored():
    tc = EP.transport_config({"pd445": "", "port": 70000, "transport": "serial", "timeout_ms": "x"})
    assert (tc.host, tc.port, tc.transport, tc.timeout_ms) == ("10.1.1.168", 502, "udp", 500)


def test_ip_dialog_and_machine_workspace_use_the_record(env, monkeypatch):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from wiring import dispose, real_window

    from mlaser.ui import dialogs
    c = ConfigService()
    c.set("ip", {"pd445": "192.168.0.50", "port": 1502, "transport": "tcp"})
    w = real_window(config=c)
    try:
        monkeypatch.setattr(dialogs.IPDialog, "exec", lambda self: 1)
        w.ip_dialog()
        assert w.config.get("ip")["port"] == 1502 and w.config.get("ip")["transport"] == "tcp"
        # the Machine workspace's connection fields start from the record
        assert w.show_workspace("machine")
        ws = w.extension("ext_machine").workspace
        if not hasattr(ws, "port"):
            ws.show_section("connection")
        assert (ws.host.text(), ws.port.value()) == ("192.168.0.50", 1502)
    finally:
        dispose(w)
