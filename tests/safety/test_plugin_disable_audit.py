"""Every disable of a plugin is audited (review 2026-11-09, arch.md A12/A13 left over).

``PluginManager.disable`` had an ``audit_event=False`` switch that uninstall and update passed;
it was never honoured (the record was written anyway), so the code said one thing and did
another.  The switch is gone: a disable that changes the allowlist, the accepted permissions or
the machine-authority grants always writes ``plugin_allowlist``, also when it happens inside an
uninstall or because an update changed the plugin's permissions (a safety reason)."""
from __future__ import annotations

import inspect
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugin_fixtures import RepoServer, build_wheel, index_for  # noqa: E402

from mlaser.services import plugin_manager as PM  # noqa: E402
from mlaser.services import plugins as P  # noqa: E402
from mlaser.services.config import ConfigService  # noqa: E402


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = set(sys.modules)
    yield tmp_path
    for m in set(sys.modules) - before:
        if m.startswith("mlaser_demo"):
            del sys.modules[m]


@pytest.fixture
def repo():
    r = RepoServer()
    yield r
    r.close()


def _manager(tmp: Path, repo: RepoServer, wheels: list[Path]) -> PM.PluginManager:
    for w in wheels:
        repo.add_wheel(w, at="/")
    url = repo.set_index(index_for(wheels, ""))
    cfg = ConfigService(tmp / "config.json")
    cfg.set(PM.REPOSITORIES_KEY, [{"url": url, "name": "test"}])
    return PM.PluginManager(cfg, root=tmp / "data" / "plugins", sleep=lambda s: None)


def _events(caplog, name="plugin_allowlist"):
    return [r.audit for r in caplog.records if getattr(r, "audit", {}).get("event") == name]


def test_disable_has_no_unaudited_variant():
    assert list(inspect.signature(PM.PluginManager.disable).parameters) == ["self", "plugin_id"]


def test_uninstall_of_an_enabled_plugin_with_authority_is_audited(isolated, repo, caplog):
    pm = _manager(isolated, repo, [build_wheel(isolated, permissions={"machine": True})])
    rec = pm.install("demo")
    pm.enable("demo", rec["entry"], rec["permissions"], grant_authority=True)
    assert "demo" in pm.config.get(P.AUTHORITY_KEY, [])
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="mlaser.audit"):
        pm.uninstall("demo")
    ev = _events(caplog)
    assert len(ev) == 1 and "demo" in ev[0]["before"] and "demo" not in ev[0]["enabled"]
    assert "demo" not in ev[0]["authority"] and "demo" not in pm.config.get(P.AUTHORITY_KEY, [])
    assert _events(caplog, "plugin_uninstall")


def test_update_with_new_permissions_disables_with_an_audit_record(isolated, repo, caplog):
    w1 = build_wheel(isolated)
    pm = _manager(isolated, repo, [w1])
    rec = pm.install("demo")
    pm.enable("demo", rec["entry"], rec["permissions"])
    w2 = build_wheel(isolated, version="0.2.0", permissions={"network": ["api.example.com"]})
    for w in (w1, w2):
        repo.add_wheel(w, at="/")
    repo.set_index(index_for([w1, w2], ""))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="mlaser.audit"):
        res = pm.update("demo")
    assert res["needs_enable"]
    ev = _events(caplog)
    assert len(ev) == 1 and "demo" in ev[0]["before"] and "demo" not in ev[0]["enabled"]
    assert _events(caplog, "plugin_update")[0]["kept_enabled"] is False


def test_disable_records_a_change_and_nothing_else(isolated, caplog):
    cfg = ConfigService(isolated / "c.json")
    pm = PM.PluginManager(cfg, root=isolated / "p")
    pm.enable("x", "m:plugin", None, grant_authority=True)
    with caplog.at_level(logging.INFO, logger="mlaser.audit"):
        assert pm.disable("x")["was_enabled"] is True
        assert pm.disable("x")["was_enabled"] is False             # already off: nothing to record
    ev = _events(caplog)
    assert len(ev) == 1 and ev[0]["before"] == ["x"] and ev[0]["authority"] == []
