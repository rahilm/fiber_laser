"""The Settings catalogue (mlaser.services.settings_catalog): one-time migration of the
original's Options values, no orphan setting (every visible key has a reader), every entry
documented, and the audit (docs/dev/settings-audit.md) decides every original parameter."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mlaser.services import settings_catalog as C
from mlaser.services.config import ConfigService

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path, name="config.json") -> ConfigService:
    return ConfigService(tmp_path / name)


# --------------------------------------------------------------------------- migration
def test_changed_option_value_is_taken_once(tmp_path):
    p = tmp_path / "config.json"
    # what the old Options grid stored: the original's keys (and the stray fine-tune key)
    p.write_text(json.dumps({"version": 1, "values": {
        "SP.Lang": 3, "GRP.IsShowNO": False, "IGP.MicoGraphGate": 0.5, "GRP.SortType": 0,
        "GRP.StdSheetWidth": 2500.0, "SP.KeyboardMoveStep": 2.5, "GRP.GuideLineLength": 5.0}}), "utf-8")
    c = ConfigService(p)
    assert c.get("shell.language") == "de"
    assert c.get("display.show_index") is False
    assert c.get("import.small_gate") == 0.5
    assert c.get("nest.sheet_width") == 2500.0
    assert c.get("design.nudge_step") == 2.5
    # per-job values are not settings: the original's lead and order rows are only kept
    assert c.setting("cam.lead_in_length") is None and "GRP.GuideLineLength" not in c.get(C.MIGRATED_KEY)
    assert c.get(C.MIGRATED_KEY)["SP.Lang"] == 3
    # the user changes the new key: a later start does not take the old value again
    c.set("shell.language", "fr")
    c.save()
    c2 = ConfigService(p)
    assert c2.get("shell.language") == "fr"
    assert c2.migrate_legacy() == []


def test_migration_only_takes_values_the_user_changed(tmp_path):
    c = _cfg(tmp_path)
    assert c.migrate_legacy() == []
    # the original's defaults (SP.Lang 0 = Chinese) never reach the new keys
    assert c.get("shell.language") == "" and c.get("nest.sheet_width") == 3000.0


def test_reimported_profile_with_a_new_value_is_taken_again(tmp_path):
    c = _cfg(tmp_path)
    c.values["IGP.ConnectGate"] = 0.3
    c.migrate_legacy()
    assert c.get("import.connect_gate") == 0.3
    c.set("import.connect_gate", 0.2)                  # the user's own choice
    c.migrate_legacy()
    assert c.get("import.connect_gate") == 0.2
    c.values["IGP.ConnectGate"] = 0.4                  # a new profile import
    c.migrate_legacy()
    assert c.get("import.connect_gate") == 0.4


def test_import_dat_reaches_the_settings_keys(tmp_path):
    """The original's profile import (ManuPara.xml) feeds the kept keys."""
    src = _cfg(tmp_path, "src.json")
    src.values.update({"GRP.IsShowEnvelopBox": True, "IGP.KeyboardMoveStepLength": 4.0})
    dat = tmp_path / "dat"
    src.export_dat(dat)
    dst = _cfg(tmp_path, "dst.json")
    dst.import_dat(dat)
    assert dst.get("display.show_bbox") is True
    assert dst.get("design.nudge_step") == 4.0


def test_unmappable_values_are_skipped(tmp_path):
    c = _cfg(tmp_path)
    from mlaser.services.config import Setting
    c.register([Setting("shell.theme", "str", "light", source="app")])
    c.values["SP.SoftwareThemeType"] = 1               # "Gray": no equivalent theme
    c.migrate_legacy()
    assert c.get("shell.theme", "light") != "gray"
    assert c.get(C.MIGRATED_KEY)["SP.SoftwareThemeType"] == 1   # recorded: not tried again


def test_unit_keys_wait_for_the_units_service(tmp_path):
    c = _cfg(tmp_path)
    c.values["MC.UnitType"] = 1
    c.migrate_legacy()
    if c.setting("units.system") is None:              # the Units service has not landed
        assert "MC.UnitType" not in (c.get(C.MIGRATED_KEY) or {})
    else:
        assert c.get("units.system") == "imperial"


# --------------------------------------------------------------------------- no orphan
def _reads(src: str, key: str) -> bool:
    cands = [key]
    if key.startswith("plugin."):
        cands.append(key.split(".", 2)[2])
    for c in cands:
        if re.search(r"""["']""" + re.escape(c) + r"""["']""", src):
            return True
        # an f-string template (f"event_{kind}") reads a family of keys
        for i in [m.start() for m in re.finditer("[_.]", c)]:
            if re.search(r"""f["']""" + re.escape(c[:i + 1]) + r"\{", src):
                return True
    return False


@pytest.mark.parametrize("entry", C.ENTRIES, ids=lambda e: e.key)
def test_every_visible_setting_has_a_reader(entry):
    src = (ROOT / entry.consumer).read_text("utf-8")
    assert _reads(src, entry.key), f"{entry.key}: {entry.consumer} does not read it"


def test_every_entry_is_documented():
    strings = json.loads((ROOT / "mlaser/i18n/en.json").read_text("utf-8"))["strings"]
    for e in C.ENTRIES:
        assert e.help_key in strings or e.page in C.PAGE_NOTES, e.help_key
        assert e.group in strings, e.group
        if e.home:
            assert e.home_label in strings, e.home_label
    for _p, key in C.PAGES:
        assert key in strings
    for _p, _cmd, key in C.LINKS:
        assert key in strings
    doc = (ROOT / "docs/user/settings.md").read_text("utf-8")
    for e in C.ENTRIES:
        assert f"`{e.key}`" in doc, f"{e.key} is not in docs/user/settings.md"


def test_entries_are_unique_and_pages_known():
    keys = [e.key for e in C.ENTRIES]
    assert len(keys) == len(set(keys))
    assert {e.page for e in C.ENTRIES} <= set(C.PAGE_IDS)


def test_safety_values_are_never_settings_entries():
    assert not [e for e in C.ENTRIES if e.key.startswith(("safety.", "plugins.", "shell.pin."))]


# --------------------------------------------------------------------------- audit
def test_audit_decides_every_original_option_parameter():
    import importlib.util
    spec = importlib.util.spec_from_file_location("settings_audit", ROOT / "tools/settings_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = mod.option_settings()
    assert len(rows) > 400
    doc = (ROOT / "docs/dev/settings-audit.md").read_text("utf-8")
    for _t, s in rows:
        kind, text = mod.decision(s.key)
        assert kind in ("keep", "move", "drop", "parity-only"), (s.key, kind)
        assert f"`{s.key}`" in doc, f"{s.key} missing from docs/dev/settings-audit.md"
    # every kept parameter is a migration source, and its new key exists
    c = ConfigService(load=False)
    for _t, s in rows:
        kind, text = mod.decision(s.key)
        if kind == "keep":
            new = text.split("`")[1]
            assert c.setting(new) is not None or new.startswith("shell."), new


def test_machine_control_keys_have_a_reader_and_a_home():
    """The machine-side Options parameters moved to Machine settings (hw.options)."""
    from mlaser.ui import params
    assert "app.options" not in params.OPTION_SECTIONS
    assert "MachineControl" in params.OPTION_SECTIONS["hw.options"]
    sec = params.section("MachineControl")
    assert len(sec) == len(params.MACHINE_CONTROL)


def test_export_dat_is_unchanged_by_the_settings_keys(tmp_path):
    """Dead carried-over rows are hidden, not deleted: the original's files round-trip and the
    migration does not change what export_dat writes."""
    c = _cfg(tmp_path)
    c.values.update({"GRP.IsShowNO": False, "SP.Lang": 3, "IGP.MicoGraphGate": 0.5, "SP.Skin": 1})
    a, b = tmp_path / "a", tmp_path / "b"
    c.export_dat(a)
    c.migrate_legacy()
    c.set("display.show_index", True)                  # a later change of the new key
    c.export_dat(b)
    for f in sorted(p.name for p in a.iterdir()):
        assert (a / f).read_bytes() == (b / f).read_bytes(), f


def test_every_key_has_a_home(tmp_path):
    from mlaser.services.config import ConfigService
    c = ConfigService(tmp_path / "c.json")
    homes = {"settings", "machine", "process", "nest", "run", "hidden"}
    assert all(s.home in homes for s in c.schema.values())
    # the original's Options rows are never a Settings value; card rows belong to Machine › Setup
    assert c.schema["GRP.IsShowNO"].home == "hidden" and c.schema["SP.Lang"].home == "hidden"
    assert c.schema["MC.JogFastSpeed"].home == "machine" and c.schema["MC.JogFastSpeed"].live
    assert c.schema["bed.slat_pitch"].home == "machine" and c.schema["safety.door_input"].home == "machine"
    assert c.schema["shell.language"].home == "settings" and c.schema["costing.currency"].home == "settings"
    for e in C.ENTRIES:
        s = c.setting(e.key)
        if s is not None and not e.home:
            assert s.home == "settings", e.key


def test_costing_migration_normalises_the_currency(tmp_path):
    c = _cfg(tmp_path)
    c.values.update({"plugin.ext_process.currency": "€", "plugin.report.currency": "EUR",
                     "plugin.report.machine_per_h": 50.0, "plugin.ext_process.machine_per_h": 90.0,
                     "plugin.report.per_pierce": 0.02})
    c.migrate_legacy()
    assert c.get("costing.currency") == "EUR"
    assert c.get("costing.machine_per_h") == 90.0      # the Process value wins
    assert c.get("costing.per_pierce") == 0.02
    from mlaser.services.costing import iso_currency, rates_from_config
    assert iso_currency("£") == "GBP" and iso_currency("chf") == "CHF"
    r = rates_from_config(c)
    assert r.machine_per_h == 90.0 and r.currency == "EUR" and r.sheet_w_mm == 0.0
