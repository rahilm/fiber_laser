"""The diagnostic bundle (review A8, F11 item 1, UX 14): contents, redaction, bounds, no card
commands, the CLI."""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from mlaser.diag import bundle as B
from mlaser.services import diagnostics as D
from mlaser.services.config import ConfigService

HOME = os.path.expanduser("~")


def _src(**kw) -> B.Sources:
    kw.setdefault("log_dirs", [])
    return B.Sources(**kw)


# =========================================================================== redaction
def test_secrets_and_personal_data_are_redacted():
    cfg = {"values": {
        "plugin.ext_automation.token": "s3cr3t-token-value-xyz",
        "plugin.run.monitor_token": "abc",
        "plugin.ext_notify.smtp_user": "operator@example.com",
        "plugin.ext_notify.email_to": "boss@example.com",
        "plugin.ext_notify.email_from": "cutter@example.com",
        "plugin.ext_notify.ntfy_topic": "my-private-topic",
        "plugin.ext_notify.webhook_url": "https://hooks.example.com/T000/B000/XXXX",
        "plugin.foo.api_key": "k",
        "plugin.foo.password": "p",
        "plugin.foo.empty_token": "",                         # nothing to hide: left as it is
        "nest.spacing": 3.0,
    }}
    red, changes = B.redact_config(cfg)
    v = red["values"]
    for k in ("plugin.ext_automation.token", "plugin.run.monitor_token", "plugin.ext_notify.smtp_user",
              "plugin.ext_notify.email_to", "plugin.ext_notify.email_from", "plugin.ext_notify.ntfy_topic",
              "plugin.ext_notify.webhook_url", "plugin.foo.api_key", "plugin.foo.password"):
        assert v[k] == B.REDACTED, k
    assert v["plugin.foo.empty_token"] == "" and v["nest.spacing"] == 3.0
    blob = json.dumps(red)
    for secret in ("s3cr3t", "example.com/T000", "operator@", "boss@", "my-private-topic"):
        assert secret not in blob
    keys = {c["key"] for c in changes}
    assert "values.plugin.ext_automation.token" in keys
    assert not any(secret in json.dumps(changes) for secret in ("s3cr3t", "operator@"))   # names only


def test_paths_under_home_are_shortened():
    p = os.path.join(HOME, "jobs", "customer", "part.dxf")
    red, changes = B.redact_config({"values": {"app.last_file": p, "x.recent": [p, "/opt/shared/a.dxf"],
                                               "x.home": HOME}})
    v = red["values"]
    assert v["app.last_file"] == "~/…/part.dxf"
    assert v["x.recent"] == ["~/…/part.dxf", "/opt/shared/a.dxf"]
    assert v["x.home"] == "~"
    assert {c["reason"] for c in changes} == {"path under the home directory"}
    # another user's home (a value copied from another computer) and free text
    assert B.redact_path("/home/alice/cuts/x.dxf") == "~/…/x.dxf"
    assert B.redact_path("C:\\Users\\Bob\\Desktop\\y.dxf") == "~/…/y.dxf"
    # free text keeps the platform's own separators after the "~" (a backslash on Windows)
    assert B.redact_text(f"opened {p} ok") == f"opened {os.path.join('~', 'jobs', 'customer', 'part.dxf')} ok"


def test_network_hosts_are_kept_but_marked_and_the_card_is_plain():
    cfg = {"values": {"plugins.repositories": [{"url": "https://github.com/o/r/releases/x.json?token=1",
                                                "name": "mlaser"}],
                      "plugin.ext_notify.smtp_host": "smtp.example.org",
                      "plugin.ext_notify.ntfy_server": "https://ntfy.sh",
                      "card.host": "10.1.1.168", "plugin.ext_automation.host": "127.0.0.1"}}
    red, _ = B.redact_config(cfg, card_host="10.1.1.168")
    v = red["values"]
    assert v["plugins.repositories"][0]["url"] == "[network host] https://github.com/…"
    assert v["plugin.ext_notify.smtp_host"] == "[network host] smtp.example.org"
    assert v["plugin.ext_notify.ntfy_server"] == "[network host] https://ntfy.sh"
    assert v["card.host"] == "10.1.1.168"                       # the card: kept as it is
    assert v["plugin.ext_automation.host"] == "127.0.0.1"       # this computer
    assert "token=1" not in json.dumps(red)


def test_excluded_registers_never_leave_the_process():
    frames = [("main", time.time(), 0x30, 59500, b"\x01", b"\x02", "ok"),
              ("main", time.time(), 0x40, 59503, b"\x01", b"\x02", "ok"),
              ("main", time.time(), 0x30, 1000, b"\x01", b"\x02", "ok")]
    out = B.frame_records(frames)
    assert [f["addr"] for f in out] == [1000]
    assert B.EXCLUDED_REGISTERS == {59500, 59503}


# =========================================================================== contents and bounds
def test_bundle_holds_every_section_and_no_drawing_by_default(tmp_path):
    cfg = ConfigService(tmp_path / "config.json", load=False)
    cfg.set("plugin.x.token", "zzz-secret") if cfg.setting("plugin.x.token") else cfg.values.update(
        {"plugin.x.token": "zzz-secret"})
    doc = SimpleNamespace(parts=[SimpleNamespace(contours=[SimpleNamespace(layer=1, closed=True)])], layers={},
                          extra={}, bbox=lambda: (0, 0, 10, 20))
    src = D.snapshot(cfg, None, doc, drawing=b"GEOMETRY-BYTES", drawing_name="part.mlz")
    src.log_dirs = []
    out = tmp_path / "b.zip"
    man = D.write(out, src)
    files = B.read_bundle(out)
    for n in ("README.txt", "manifest.json", "system.json", "config.json", "machine_parameters.json",
              "controller.json", "alarm_history.json", "plan.json"):
        assert n in files, n
    assert not any(n.startswith("drawing/") for n in files) and not man["drawing_included"]
    assert b"GEOMETRY-BYTES" not in b"".join(files.values())
    assert b"zzz-secret" not in files["config.json"]
    system = json.loads(files["system.json"])
    assert system["app"]["version"] and "commit" in system["app"]
    assert {"python", "qt", "os", "cpu", "locale", "kernel", "dependencies", "screens"} <= set(system)
    params = json.loads(files["machine_parameters.json"])
    assert len(params["sha256"]) == 64 and params["values"]["HardPara.xml"]
    assert params["sha256"] == B.params_hash(params["values"])
    plan = json.loads(files["plan.json"])
    assert plan["document"]["parts"] == 1 and plan["document"]["bbox_mm"] == [0, 0, 10, 20]
    assert not (tmp_path / "b.zip.part").exists()
    # ticked: the drawing goes in
    man2 = D.write(tmp_path / "c.zip", src, include_drawing=True)
    assert man2["drawing_included"] and B.read_bundle(tmp_path / "c.zip")["drawing/part.mlz"] == b"GEOMETRY-BYTES"


def test_contents_list_matches_what_is_written(tmp_path):
    src = _src(drawing=b"x")
    names = {i.name for i in B.contents(src, include_drawing=False)}
    files = B.build(src, include_drawing=False)
    assert {n for n in files if "/" not in n} == {n for n in names if not n.endswith("/")} | {"manifest.json"}
    assert any(i.name.startswith("drawing/") for i in B.contents(src, include_drawing=True))


def test_logs_newest_first_bounded_and_home_scrubbed(tmp_path, monkeypatch):
    d = tmp_path / "log"
    d.mkdir()
    ui = tmp_path / "data" / "Log"
    ui.mkdir(parents=True)
    now = time.time()
    for i, name in enumerate(("mlaser.log.2", "mlaser.log.1", "mlaser.log", "audit.log", "crash.log")):
        p = d / name
        p.write_text((f"line in {name} {HOME}/x\n") * 2000, "utf-8")
        os.utime(p, (now - 100 + i, now - 100 + i))
    (d / "unrelated.txt").write_text("not a log", "utf-8")
    (ui / "2026-11-10.log").write_text("ui line\n", "utf-8")
    os.utime(ui / "2026-11-10.log", (now, now))
    monkeypatch.setattr(B, "LOG_FILE_CAP", 30_000)
    monkeypatch.setattr(B, "LOG_BUDGET", 100_000)
    files = B.build(_src(log_dirs=[str(d)], ui_log_dirs=[str(ui)]))
    man = json.loads(files["manifest.json"])
    order = [g["name"] for g in man["logs"]]
    assert order[0] == "logs/ui/2026-11-10.log" and order[1] == "logs/app/crash.log"
    assert "logs/app/unrelated.txt" not in order
    included = [g for g in man["logs"] if g["included"]]
    assert sum(g["bytes"] for g in included) <= 100_000 + 30_000
    assert any(not g["included"] and g["reason"] == "size budget reached" for g in man["logs"])
    assert all(g["truncated_to_newest"] for g in included if g["name"] != "logs/ui/2026-11-10.log")
    text = files["logs/app/crash.log"].decode()
    assert HOME not in text and "~/x" in text


# =========================================================================== controller: cached state only
@pytest.mark.sim
def test_controller_section_reads_cached_state_and_sends_nothing(tmp_path):
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.05, stream_process=False)
    assert ctl.connect_simulator()
    try:
        ctl.fsm.raise_fault("test fault for the bundle")
        ctl._sup.safe_halt("test halt")
        time.sleep(0.1)
        ctl._poll_stop.set()                                   # quiet the poller: count only the bundle
        if ctl._poll_thread is not None:
            ctl._poll_thread.join(2.0)
        ctl._sup.stop_monitor()
        main, halt = ctl._sup._mc, ctl._sup._halt
        before = (main.requests, halt.requests, ctl.sim.requests)
        src = D.snapshot(None, ctl, None)
        D.write(tmp_path / "b.zip", src)
        after = (main.requests, halt.requests, ctl.sim.requests)
        assert after == before                                  # no card command for the bundle
        c = json.loads(B.read_bundle(tmp_path / "b.zip")["controller.json"])
    finally:
        ctl.disconnect()
    assert c["card"]["simulator"] and c["card"]["firmware_version"] and c["card"]["config_word"].startswith("0x")
    assert any(t["to"] == "fault" for t in c["safety"]["transitions"])
    assert any(h["reason"] == "test halt" for h in c["supervisor"]["halts"])
    assert c["frames"] and all(f["addr"] not in B.EXCLUDED_REGISTERS for f in c["frames"])
    assert c["events"]


def test_alarm_history_is_read_from_disk(tmp_path):
    from mlaser.services.alarm_history import AlarmHistory, AlarmRecord
    h = AlarmHistory(tmp_path / "alarm_history.jsonl")
    h.add(AlarmRecord("alarm", "X axis servo alarm", time.time() - 5, code=3, cleared=time.time()))
    h.flush()
    recs = D.alarm_records(h)
    assert recs and recs[0]["text"] == "X axis servo alarm" and recs[0]["raised_at"]


# =========================================================================== CLI
def test_cli_writes_a_bundle(tmp_path, capsys):
    from mlaser import cli
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "mlaser.log").write_bytes(b"hello\n")    # bytes: the bundle copies the file as it is
    out = tmp_path / "out" / "bundle.zip"
    assert cli.is_subcommand(["diag-bundle", str(out)])
    rc = cli.main(["diag-bundle", str(out), "--log-dir", str(logs), "--data-dir", str(tmp_path / "data"), "--json"])
    assert rc == 0
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] and res["command"] == "diag-bundle" and not res["drawing_included"]
    files = B.read_bundle(out)
    assert files["logs/app/mlaser.log"] == b"hello\n"
    assert json.loads(files["controller.json"]) == {"available": False}


def test_cli_drawing_only_when_given(tmp_path, capsys):
    from mlaser import cli
    dr = tmp_path / "part.dxf"
    dr.write_bytes(b"0\nSECTION\n")
    out = tmp_path / "b.zip"
    assert cli.main(["diag-bundle", str(out), "--drawing", str(dr), "--log-dir", str(tmp_path)]) == 0
    assert B.read_bundle(out)["drawing/part.dxf"] == b"0\nSECTION\n"
    assert cli.main(["diag-bundle", str(tmp_path / "c.zip"), "--drawing", str(tmp_path / "missing.dxf")]) == 3
