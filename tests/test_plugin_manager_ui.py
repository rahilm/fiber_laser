"""The Plugin manager dialog (mlaser.ui.plugin_manager), driven offscreen against a local HTTP
repository, and the whole path through the application: install in the dialog, enable in
Service mode, the next MainWindow discovers and loads the plugin, uninstall."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

sys.path.insert(0, str(Path(__file__).resolve().parent))

from plugin_fixtures import RepoServer, build_wheel, index_for  # noqa: E402
from wiring import real_window  # noqa: E402

from mlaser.services import plugins as P  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
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


def _publish(repo, wheels):
    for w in wheels:
        repo.add_wheel(w, at="/")
    return repo.set_index(index_for(wheels))


def _dialog(w):
    from mlaser.ui.plugin_manager import PluginManagerDialog
    d = PluginManagerDialog(w, sync=True)
    d.answer = lambda title, text: True
    return d


def test_install_enable_restart_load_uninstall(app, env, repo):
    url = _publish(repo, [build_wheel(env, permissions={"network": ["127.0.0.1"]})])
    w = real_window(entry_points=True)
    try:
        assert w.commands.get("app.plugin_manager").min_mode == "engineer"
        d = _dialog(w)
        assert not d.banner.isVisibleTo(d)
        # Repositories tab: add the local repository (keep the default one too)
        assert d.add_repository(url) and d.repos.count() == 2
        # Browse: search and the details with the permissions
        d.remove_repository(d.pm.repositories()[0]["url"])                   # no network in tests
        assert [r["url"] for r in d.pm.repositories()] == [url]
        d.load_browse(refresh=True)
        d.search.setText("demo")
        assert d.browse.count() == 1 and "contacts these hosts: 127.0.0.1" in d.browse_detail.text()
        assert d.install_selected()
        assert any("Install Demo demo 0.1.0?" in m for m in d.messages)        # the permissions were shown
        assert "demo" in d.pm.installed() and d.banner.isVisibleTo(d)          # restart to apply
        assert d.selected()["id"] == "demo" and d.selected()["status"] == "disabled"
        assert d.enable_selected()
        assert w.config.get(P.ALLOWLIST_KEY) == {"demo": "mlaser_demo:plugin"}
        assert d.selected()["status"] == "enabled (restart)"
        assert "machine authority" not in " ".join(d.messages)                  # not asked: no machine
        d.deleteLater()
        w.config.save()
    finally:
        w.close()
    # the next start: site dir on sys.path, discovered, enabled, loaded after the safety UI
    w2 = real_window(entry_points=True)
    try:
        status = {i.name: s for i, s in w2.plugin_catalog}
        assert status.get("demo") == "loaded", w2.plugin_catalog
        w2.commands.run("demo.hello")
        assert sys.modules["mlaser_demo"].SEEN["ran"] is True
        ctx = sys.modules["mlaser_demo"].SEEN["ctx"]
        assert ctx.permissions.network == ("127.0.0.1",) and not ctx.permissions.machine
        d = _dialog(w2)
        assert d.select("demo") and d.selected()["status"] == "loaded"
        assert d.uninstall_selected()
        assert d.pm.installed() == {} and "demo" not in w2.config.get(P.ALLOWLIST_KEY, {})
        assert not (d.pm.site / "mlaser_demo").exists()
        d.deleteLater()
    finally:
        w2.close()


def test_refusal_is_shown_and_nothing_installed(app, env):
    bad = build_wheel(env, plugin={"api": ">=3"})
    w = real_window()
    try:
        d = _dialog(w)
        d.install_file(str(bad))
        assert d.pm.installed() == {} and "not compatible" in d.messages[-1]
        d.deleteLater()
    finally:
        w.close()


def test_machine_plugin_asks_for_authority_separately(app, env):
    whl = build_wheel(env, permissions={"machine": True})
    w = real_window()
    try:
        d = _dialog(w)
        d.install_file(str(whl))
        asked = []
        d.answer = lambda t, text: asked.append(text) or ("machine authority" not in text)   # enable, no grant
        d.select("demo")
        assert d.enable_selected()
        assert any("machine authority" in a for a in asked)
        assert "demo" in w.config.get(P.ALLOWLIST_KEY) and "demo" not in w.config.get(P.AUTHORITY_KEY, [])
        assert d.disable_selected() and "demo" not in w.config.get(P.ALLOWLIST_KEY, {})
        d.deleteLater()
    finally:
        w.close()


def test_update_from_the_dialog(app, env, repo):
    w1 = build_wheel(env)
    url = _publish(repo, [w1])
    w = real_window()
    try:
        d = _dialog(w)
        d.pm.config.set("plugins.repositories", [{"url": url, "name": "t"}])
        d.install("demo")
        d.select("demo")
        d.enable_selected()
        _publish(repo, [w1, build_wheel(env, version="0.2.0")])
        d.check_updates()
        assert d.select("demo") and d.selected()["update"] == "0.2.0" and d.btn_update.isEnabled()
        assert d.update_selected()
        assert d.pm.installed()["demo"]["version"] == "0.2.0"
        assert w.config.get(P.ALLOWLIST_KEY) == {"demo": "mlaser_demo:plugin"}       # same permissions: kept
        d.deleteLater()
    finally:
        w.close()


def test_engineer_mode_views_only(app, env):
    w = real_window(user_mode="engineer")
    try:
        d = _dialog(w)
        assert not d.service
        for b in (d.btn_file, d.btn_url, d.btn_install, d.btn_enable, d.btn_uninstall, d.btn_repo_add):
            assert not b.isEnabled()
        assert d.btn_check.isEnabled() and not d.install_file("x.whl") and not d.add_repository("github:a/b")
        for b in (d.btn_file, d.btn_check, d.btn_enable):
            assert b.minimumHeight() >= 44                                       # touch targets
        d.deleteLater()
        w.commands.run("app.plugin_manager")                                     # opens (exec stubbed)
        assert "app.plugin_manager" not in w.commands.failures
    finally:
        w.close()


def test_background_work_comes_back_on_the_gui_thread(app, env):
    """The dialog's worker thread (not ``sync``): results and errors are delivered via the
    queued signal on the GUI thread."""
    import threading
    import time

    from mlaser.ui.plugin_manager import PluginManagerDialog
    w = real_window()
    try:
        d = PluginManagerDialog(w)
        got = []
        d.run(lambda: threading.current_thread().name, lambda r: got.append((r, threading.current_thread().name)))
        d.run(lambda: 1 / 0, lambda r: None, lambda e: got.append(type(e).__name__))
        end = time.monotonic() + 10
        while len(got) < 2 and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
        assert ("mlaser-plugin-manager", threading.current_thread().name) in got and "ZeroDivisionError" in got
        assert d.status.text() == ""                                          # not busy any more
        d.deleteLater()
    finally:
        w.close()


def test_network_hosts_and_icons(app, env):
    from mlaser.ui.icons import icon
    svg = env / "i.svg"
    svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path stroke="currentColor" '
                    b'd="M2 2h20"/></svg>')
    assert not icon(str(svg)).isNull() and icon(str(env / "none.svg")).isNull()
    w = real_window()
    try:
        d = _dialog(w)
        d.install_file(str(build_wheel(env)))
        assert d.select("demo") and d.btn_hosts.isEnabled()
        assert d.set_hosts("acme.onshape.com, other.example.org")
        assert w.config.get(P.NETWORK_KEY) == {"demo": ["acme.onshape.com", "other.example.org"]}
        assert "granted hosts: acme.onshape.com" in d.installed_detail.text()
        assert d.set_hosts("acme.onshape.com")
        assert w.config.get(P.NETWORK_KEY) == {"demo": ["acme.onshape.com"]}
        assert not d.set_hosts("*.bad.org") and w.config.get(P.NETWORK_KEY) == {"demo": ["acme.onshape.com"]}
        d.deleteLater()
        ctx = P.PluginContext(w, "iconplug", "entry-point", {"plugin": {}, "permissions": {}})
        ctx.register_command("iconplug.x", lambda: None, label="X", icon=str(svg))
        from mlaser.ui.icons import COMMAND_ICONS
        assert COMMAND_ICONS["iconplug.x"] == str(svg)
    finally:
        w.close()


def _bundled_plugins():
    import os
    root = Path(os.environ.get("MLASER_TEST_PLUGINS_DIR") or Path(__file__).resolve().parent.parent / "plugins")
    return sorted(p for p in root.glob("*/pyproject.toml")) if root.is_dir() else []


@pytest.mark.parametrize("pyproject", _bundled_plugins() or [None], ids=lambda p: p.parent.name if p else "none")
def test_project_plugins_install_and_load(app, env, pyproject):
    """Every plugin of plugins/*/ (what package.yml publishes) passes the manager's checks,
    installs, and loads through the real start-up path once enabled."""
    if pyproject is None:
        pytest.skip("no plugin in plugins/")
    from plugin_fixtures import wheel_from_project

    from mlaser.services.plugin_manager import PluginManager
    whl = wheel_from_project(pyproject.parent, env / "dist")
    import importlib.util
    tops = {n.split("/", 1)[0] for n in __import__("zipfile").ZipFile(whl).namelist() if "dist-info" not in n}
    if any(importlib.util.find_spec(t) is not None for t in tops):
        pytest.skip(f"{pyproject.parent.name} is already importable here (an editable install): the manager "
                    "would rightly refuse the collision")
    w = real_window()
    try:
        pm = PluginManager(w.config)
        rec = pm.install(str(whl))
        pm.enable(rec["id"], rec["entry"], rec["permissions"])
        w.config.save()
    finally:
        w.close()
    w2 = real_window(entry_points=True)
    try:
        status = {i.name: s for i, s in w2.plugin_catalog}
        assert status.get(rec["id"]) == "loaded", (status, [m for m in w2.log_panel.findChildren(object)][:0])
        assert w2.plugin_contexts[rec["id"]].manifest["plugin"]["id"] == rec["id"]
    finally:
        w2.close()
        for m in [m for m in sys.modules if m.split(".")[0] in rec["top_level"]]:
            del sys.modules[m]
