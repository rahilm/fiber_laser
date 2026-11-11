"""The plugin API offers what its documents name, and a plugin's job data has a ``ctx`` route
(review 2026-11-09, arch.md A12/A13 left over: plugins were sent to
``window.extension("ext_project").register_section`` and ``window.show_workspace``).

* every ``ctx.<member>`` and ``ctx.services.<member>`` in docs/dev/PLUGINS.md and
  PLUGIN_STANDARD.md exists on :class:`PluginContext` / :class:`PluginServices`;
* the guides no longer send plugins to the window for what ``ctx`` does;
* ``ctx.register_project_section``: a plugin that is not built in gets a section named after
  itself and needs ``permissions.document``; in the real window the section is saved with the
  project.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from mlaser.services import plugins as P
from mlaser.services.commands import CommandRegistry
from mlaser.services.config import ConfigService

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "docs" / "dev" / "PLUGINS.md", ROOT / "docs" / "dev" / "PLUGIN_STANDARD.md"]


def _members(pattern: str) -> set[str]:
    out: set[str] = set()
    for doc in DOCS:
        out |= set(re.findall(pattern, doc.read_text(encoding="utf-8")))
    return out


def test_every_documented_ctx_member_exists():
    ctx_names = _members(r"`ctx\.([a-z_]+)") - {"window"}        # window: see the gap table
    missing = sorted(n for n in ctx_names if not hasattr(P.PluginContext, n))
    assert missing == []
    services = _members(r"ctx\.services\.([a-z_]+)") | _members(r"`ctx\.services`, `\.([a-z_]+)`")
    assert sorted(n for n in services if not hasattr(P.PluginServices, n)) == []
    assert {"register_project_section", "show_workspace", "user_mode"} <= ctx_names


def test_the_guides_use_ctx_where_it_exists():
    text = (ROOT / "docs" / "dev" / "PLUGINS.md").read_text(encoding="utf-8")
    assert 'window.extension("ext_project").register_section' not in text
    assert 'window.hooks.provider("workspace.show")' not in text
    assert "What the plugin API does not cover yet" in text      # the gaps are listed
    fmt = (ROOT / "docs" / "dev" / "project-format.md").read_text(encoding="utf-8")
    assert "ctx.register_project_section" in fmt and 'window.extension("ext_project")' not in fmt


class _Host:
    def __init__(self, with_projects: bool = True):
        self.commands = CommandRegistry()
        self.hooks = P.Hooks()
        self.config = ConfigService(load=False)
        self.docs = self.jobs = self.ctl = self.tr_ = None
        self.sections: dict = {}
        if with_projects:
            self.hooks.provide("project.register_section",
                               lambda name, get, set: self.sections.__setitem__(name, (get, set)), "ext_project")

    def log(self, msg):
        pass


def _ctx(host, origin="entry-point", document=True):
    manifest = {"plugin": {"id": "acme"}, "permissions": {"document": document}}
    return P.PluginContext(host, "acme", origin=origin, manifest=None if origin == "builtin" else manifest)


def test_a_third_party_section_is_named_after_the_plugin():
    host = _Host()
    ctx = _ctx(host)
    assert ctx.register_project_section("acme", dict, lambda v: None) is True
    assert ctx.register_project_section("acme.jobs", dict, lambda v: None) is True
    for other in ("orders", "technology", "queue", "acmex", "other.acme"):
        with pytest.raises(PermissionError):
            ctx.register_project_section(other, dict, lambda v: None)
    assert sorted(host.sections) == ["acme", "acme.jobs"]


def test_a_section_needs_the_document_permission():
    host = _Host()
    with pytest.raises(PermissionError):
        _ctx(host, document=False).register_project_section("acme", dict, lambda v: None)
    assert host.sections == {}


def test_builtins_name_any_section_and_no_project_support_is_false():
    host = _Host()
    assert _ctx(host, origin="builtin").register_project_section("orders", dict, lambda v: None) is True
    assert "orders" in host.sections
    assert _ctx(_Host(with_projects=False)).register_project_section("acme", dict, lambda v: None) is False


def test_the_real_window_saves_a_plugin_section(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from wiring import dispose, real_window
    w = real_window()
    try:
        ctx = P.PluginContext(w, "acme", origin="entry-point",
                              manifest={"plugin": {"id": "acme"}, "permissions": {"document": True}})
        got: list = []
        assert ctx.register_project_section("acme", lambda: {"n": 3}, got.append) is True
        proj = w.extension("ext_project")
        assert "acme" in proj.sections
        assert proj.current().sections.get("acme") == {"n": 3}
    finally:
        dispose(w)
