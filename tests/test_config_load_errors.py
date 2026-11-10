"""A config.json that cannot be used is never silently replaced (architecture review A3) and
its format version is read (fw review F11): ``mlaser.services.config``, Qt-free."""
from __future__ import annotations

import json
import logging
import os
import sys

import pytest

from mlaser.machine.demo import DemoController
from mlaser.services import config as C
from mlaser.services.config import ConfigService, ConfigWriteHeld


def _good(path):
    c = ConfigService(path)
    c.set("MAC.WritePluse", 20000)
    c.set("safety.door_input", 5)
    c.set_profile("params")
    c.save()
    ConfigService(path)                      # one clean start: config.json.bak
    return path.read_bytes()


def _bad_copies(path):
    return sorted(p.name for p in path.parent.glob(path.name + ".bad-*"))


BAD = {
    "truncated": None,                       # half of a good file
    "empty": b"",
    "not_utf8": b"\xff\xfe\x00\x81" * 50,
    "list": b"[]",
    "null": b"null",
    "values_list": b'{"version":1,"values":[1,2]}',
    "profile_str": b'{"version":1,"profile":"params","values":{}}',
    "bad_profile": b'{"version":1,"profile":{"base":"x"},"values":{}}',
    "version_str": b'{"version":"1","values":{}}',
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_damaged_file_is_kept_and_never_overwritten(tmp_path, case, caplog):
    p = tmp_path / "config.json"
    good = _good(p)
    data = BAD[case] if BAD[case] is not None else good[: len(good) // 2]
    p.write_bytes(data)
    with caplog.at_level(logging.ERROR, logger="mlaser.config"):
        c = ConfigService(p)                 # [] / null used to raise AttributeError here
    assert c.load_error and c.load_error_kind == "damaged" and c.load_blocked
    assert c.values == {} and c.profile.base == "bench"          # the defaults, in memory only
    assert any("could not be loaded" in r.getMessage() for r in caplog.records)
    copies = _bad_copies(p)
    assert len(copies) == 1 and (tmp_path / copies[0]).read_bytes() == data
    assert c.load_error_evidence == tmp_path / copies[0]
    # a close of the window (flush), the counters (background flush), an explicit save: refused
    c.set("shell.welcome_done", True)
    c.flush()
    c.flush(background=True)
    with pytest.raises(ConfigWriteHeld):
        c.save()
    assert p.read_bytes() == data
    # the same damage seen again (another start, a CLI command): no second copy
    ConfigService(p)
    assert _bad_copies(p) == copies
    # the clean load before kept the backup that the notice offers
    assert c.backup_usable() and c.backup_path.read_bytes() == good


def test_absent_file_is_a_fresh_install(tmp_path):
    c = ConfigService(tmp_path / "config.json")
    assert not c.load_error and not c.load_blocked
    c.set("shell.welcome_done", True)
    c.save()
    assert (tmp_path / "config.json").exists()


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "getuid") and os.getuid() == 0),
                    reason="chmod 0 does not stop reading here")
def test_unreadable_file_stays_in_place_until_acknowledged(tmp_path):
    p = tmp_path / "config.json"
    good = _good(p)
    os.chmod(p, 0)
    try:
        c = ConfigService(p)
        assert c.load_error_kind == "unreadable" and c.load_error_evidence is None
        c.set("shell.welcome_done", True)
        c.flush()
    finally:
        os.chmod(p, 0o600)
    assert p.read_bytes() == good             # the intact file was not replaced
    # continuing with the defaults moves the file aside first, then writes
    assert c.acknowledge_load_error()
    assert c.load_error_evidence is not None and c.load_error_evidence.read_bytes() == good
    c.set("shell.welcome_done", False)
    c.save()
    assert json.loads(p.read_text())["values"] == {"shell.welcome_done": False}


def test_acknowledge_then_writes(tmp_path):
    p = tmp_path / "config.json"
    _good(p)
    p.write_bytes(b"{")
    c = ConfigService(p)
    assert c.acknowledge_load_error() and not c.load_blocked
    c.set("shell.welcome_done", True)
    c.flush()
    assert json.loads(p.read_text())["values"] == {"shell.welcome_done": True}
    assert (tmp_path / _bad_copies(p)[0]).read_bytes() == b"{"


def test_restore_backup(tmp_path):
    p = tmp_path / "config.json"
    good = _good(p)
    p.write_bytes(good[:40])
    c = ConfigService(p)
    seen = []
    c.subscribe(seen.append)
    assert c.restore_backup()
    assert not c.load_blocked and "*" in seen
    assert c.get("MAC.WritePluse") == 20000 and c.get("safety.door_input") == 5 and c.profile.base == "params"
    assert c.machine().pulses_per_mm_x == 200.0
    assert json.loads(p.read_text())["values"]["safety.door_input"] == 5   # written back
    assert ConfigService(p).get("safety.door_input") == 5


def test_restore_without_backup_changes_nothing(tmp_path):
    p = tmp_path / "config.json"
    p.write_bytes(b"null")
    c = ConfigService(p)
    assert not c.backup_usable() and not c.restore_backup() and c.load_blocked


def test_backup_follows_the_last_clean_load(tmp_path):
    p = tmp_path / "config.json"
    _good(p)
    c = ConfigService(p)
    c.set("safety.door_input", 7)
    c.save()
    assert json.loads(c.backup_path.read_text())["values"]["safety.door_input"] == 5   # not yet loaded
    ConfigService(p)
    assert json.loads(c.backup_path.read_text())["values"]["safety.door_input"] == 7


# ---------------------------------------------------------------- format version (F11)
def test_version_is_written_and_read(tmp_path):
    p = tmp_path / "config.json"
    _good(p)
    assert json.loads(p.read_text())["version"] == C.CONFIG_VERSION
    d = json.loads(p.read_text())
    del d["version"]                          # never written without one, but read as format 1
    p.write_text(json.dumps(d))
    assert not ConfigService(p).load_error


def test_newer_format_is_refused_not_trimmed(tmp_path):
    p = tmp_path / "config.json"
    _good(p)
    d = json.loads(p.read_text())
    d["version"] = C.CONFIG_VERSION + 1
    d["values"]["future.key"] = {"a": 1}
    raw = json.dumps(d).encode()
    p.write_bytes(raw)
    c = ConfigService(p)
    assert c.load_error_kind == "newer" and c.load_blocked and c.values == {}
    assert str(C.CONFIG_VERSION + 1) in c.load_error
    c.set("shell.welcome_done", True)
    c.flush()
    assert p.read_bytes() == raw                       # the newer file is untouched
    assert c.load_error_evidence.read_bytes() == raw   # and a copy kept for when it is replaced


def test_migration_hook(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"version": 1, "profile": {"base": "bench"}, "values": {"old.door": 4}}))

    def one_to_two(d):
        v = dict(d["values"])
        v["safety.door_input"] = v.pop("old.door")
        return {**d, "version": 2, "values": v}
    monkeypatch.setattr(C, "CONFIG_VERSION", 2)
    monkeypatch.setitem(C.MIGRATIONS, 1, one_to_two)
    c = ConfigService(p)
    assert not c.load_error and c.get("safety.door_input") == 4 and not c.is_set("old.door")
    c.save()
    assert json.loads(p.read_text())["version"] == 2


def test_missing_migration_is_a_load_error(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"version": 1, "values": {}}))
    monkeypatch.setattr(C, "CONFIG_VERSION", 3)
    c = ConfigService(p)
    assert c.load_error_kind == "damaged" and "migration" in c.load_error


# ---------------------------------------------------------------- saves are atomic
def test_save_goes_through_the_atomic_writer(tmp_path, monkeypatch):
    calls = []
    real = C.atomic_write_text
    monkeypatch.setattr(C, "atomic_write_text", lambda p, t, **k: (calls.append(p), real(p, t, **k)))
    c = ConfigService(tmp_path / "config.json")
    c.set("shell.welcome_done", True)
    c.save()
    assert calls == [tmp_path / "config.json"]


# ---------------------------------------------------------------- the arm hold
def test_arm_hold_refuses_arming_and_lifts():
    ctl = DemoController()
    ctl.connect()
    ctl.set_arm_hold("config", "settings not loaded")
    assert not ctl.arm() and not ctl.fsm.armed
    ctl.set_arm_hold("config", None)
    assert ctl.arm_holds() == {}
    assert ctl.arm()
    ctl.disarm()
