"""The plugin manager (mlaser.services.plugin_manager), the manifest, PEP 440 helpers, the index
builder and the ``mlaser plugin`` CLI, against a local HTTP repository (127.0.0.1) serving an
index and wheels built here from temp sources (docs/dev/PLUGIN_STANDARD.md)."""
from __future__ import annotations

import io
import json
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from plugin_fixtures import RepoServer, build_wheel, index_for, sha256_file  # noqa: E402

from mlaser.services import plugin_manager as PM  # noqa: E402
from mlaser.services import plugins as P  # noqa: E402
from mlaser.services.config import ConfigService  # noqa: E402
from mlaser.services.pep440 import Requirement, SpecifierSet, Version, evaluate_marker  # noqa: E402
from mlaser.services.plugin_manifest import ManifestError, Permissions, parse_manifest, unknown_keys  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A data dir of its own; sys.path and the imported test plugins restored afterwards."""
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = set(sys.modules)
    yield tmp_path
    for m in set(sys.modules) - before:
        if m.startswith("mlaser_demo") or m.startswith("pmtest_"):
            del sys.modules[m]


@pytest.fixture
def repo():
    r = RepoServer()
    yield r
    r.close()


def manager(tmp, repos=(), **kw) -> PM.PluginManager:
    cfg = ConfigService(tmp / "config.json")
    cfg.set(PM.REPOSITORIES_KEY, [{"url": u, "name": "test"} for u in repos])
    return PM.PluginManager(cfg, root=tmp / "data" / "plugins", sleep=lambda s: None, **kw)


def serve(repo: RepoServer, tmp: Path, wheels: list[Path], relative: bool = True) -> str:
    for w in wheels:
        repo.add_wheel(w, at="/")
    return repo.set_index(index_for(wheels, "" if relative else repo.base))


# ----------------------------------------------------------------------------- basics
def test_version_matches_pyproject():
    import mlaser
    with open(ROOT / "pyproject.toml", "rb") as fh:
        assert mlaser.__version__ == tomllib.load(fh)["project"]["version"]
    assert P.PLUGIN_API == 2


def test_pep440_against_packaging():
    packaging = pytest.importorskip("packaging.specifiers")
    from packaging.version import Version as PV
    vs = ["0.1", "0.1.0rc1", "0.1.0", "0.1.0.post1", "1.0.dev0", "1.0a1", "1.0rc1", "1.0", "1.0+loc", "2",
          "2.0rc1", "2.1", "3.0a1", "1!0.5"]
    for a in vs:
        for b in vs:
            assert (Version.parse(a) < Version.parse(b)) == (PV(a) < PV(b)), (a, b)
    for spec in (">=2,<3", ">=0.1.0rc1", "~=1.0", "==1.*", "!=1.0", "<3", ">2", "<=2.0", ">1.0", "!=2.*"):
        for v in vs:
            for pre in (False, True):
                assert SpecifierSet(spec).contains(v, prereleases=pre) == \
                    packaging.SpecifierSet(spec).contains(v, prereleases=pre), (spec, v, pre)
    r = Requirement.parse('ezdxf>=1.1; python_version >= "3.8"')
    assert r.key == "ezdxf" and r.applies() and not Requirement.parse("x; extra == 'test'").applies()
    assert evaluate_marker('os_name == "nt" or os_name != "nt"')


def test_app_prerelease_satisfies_ranges():
    from mlaser.services.plugin_manifest import compatibility_errors
    assert compatibility_errors({"api": ">=2,<3", "mlaser": ">=0.0.1"}, 2, "0.1.0rc1") == []
    assert compatibility_errors({"api": ">=3"}, 2, "0.1.0rc1")
    assert compatibility_errors({"mlaser": ">=0.2"}, 2, "0.1.0rc1")


def test_manifest_schema_check():
    good = (ROOT / "examples/mlaser_hello/mlaser_hello/mlaser-plugin.toml").read_bytes()
    m = parse_manifest(good)
    assert m["plugin"]["id"] == "hello" and Permissions.from_dict(m["permissions"]).document
    with pytest.raises(ManifestError) as e:
        parse_manifest(b'[plugin]\nid = "X"\nversion = "nope"\n[permissions]\nnetwork = ["Bad Host"]\nfiles = 1\n')
    errs = " ".join(e.value.errors)
    for frag in ("plugin.id", "plugin.version", "plugin.entry is required", "invalid host", "permissions.files"):
        assert frag in errs
    m2 = parse_manifest(good.decode() + '\n[extra]\nx = 1\n')
    assert "extra" in unknown_keys(m2)                       # unknown keys: kept and shown, ignored
    p = Permissions(network=("*.example.com", "api.x.org"))
    assert p.allows_host("a.example.com") and p.allows_host("a.b.example.com") and not p.allows_host("example.com")
    assert p.allows_host("api.x.org") and not p.allows_host("evil.org") and not p.allows("secrets")


def test_activate_site_appends_at_the_end(isolated):
    site = PM.site_dir()
    assert PM.activate_site() is None                         # no site dir yet
    site.mkdir(parents=True)
    (site / ".staging-old").mkdir()
    s = PM.activate_site()
    assert sys.path[-1] == s and not (site / ".staging-old").exists()
    PM.activate_site()
    assert sys.path.count(s) == 1


# ----------------------------------------------------------------------------- happy path
def test_install_discover_enable_load_uninstall(isolated, repo):
    tmp = isolated
    whl = build_wheel(tmp, permissions={"network": ["127.0.0.1"], "secrets": True})
    pm = manager(tmp, [serve(repo, tmp, [whl])])
    assert [p.id for p in pm.search("demo")] == ["demo"]
    rec = pm.install("demo")
    assert rec["version"] == "0.1.0" and rec["sha256"] == sha256_file(whl)
    site = pm.site
    for f in rec["files"]:
        assert (site / f).is_file()
    inst = json.loads(pm.installed_file.read_text())
    assert inst["format"] == "mlaser-plugins-installed" and "demo" in inst["plugins"]
    # installing never enables
    assert pm.config.get(P.ALLOWLIST_KEY, {}) == {}
    row = next(r for r in pm.catalog() if r["id"] == "demo")
    assert row["status"] == "disabled" and row["origin"] == "site"
    # start-up: the site dir at the end of sys.path, then the normal entry-point discovery
    PM.activate_site()
    info = next(i for i in P.discover((), entry_points=True) if i.name == "demo")
    assert info.manifest["plugin"]["id"] == "demo" and info.location == "site" and not info.legacy
    pm.enable("demo", rec["entry"], rec["permissions"])
    assert P.plugin_status(info, pm.config.get(P.ALLOWLIST_KEY), pm.config.get(P.ACCEPTED_KEY)) == "enabled"
    host = FakeHost(pm.config)
    ctx, obj = P.register_plugin(host, info)
    assert ctx is not None and ctx.manifest["plugin"]["id"] == "demo"
    host.commands.run("demo.hello")
    assert sys.modules["mlaser_demo"].SEEN["ran"] is True
    # uninstall: exactly the recorded files, and the allowlist entry
    res = pm.uninstall("demo")
    assert res["files_removed"] == len(rec["files"])
    assert not any((site / f).exists() for f in rec["files"])
    assert not (site / "mlaser_demo").exists() and list(site.iterdir()) == []
    assert "demo" not in pm.config.get(P.ALLOWLIST_KEY, {}) and pm.installed() == {}


class FakeHost:
    def __init__(self, config=None):
        from mlaser.services.commands import CommandRegistry
        from mlaser.services.document import DocumentService
        self.commands = CommandRegistry()
        self.hooks = P.Hooks()
        self.docs = DocumentService()
        self.config = config or ConfigService(load=False)
        self.jobs = None
        self.ctl = object()
        self.tr_ = None
        self.logs: list[str] = []

    def log(self, msg):
        self.logs.append(msg)


# ----------------------------------------------------------------------------- refusals
def _refused(pm, source, code, **kw):
    with pytest.raises(PM.RefusedError) as e:
        pm.install(source, **kw)
    assert e.value.code == code, str(e.value)
    assert not pm.site.exists() or not [p for p in pm.site.iterdir() if not p.name.startswith(".")]
    return e.value


def test_refused_hash_mismatch(isolated, repo):
    whl = build_wheel(isolated)
    idx = index_for([whl])
    idx["plugins"][0]["versions"][0]["sha256"] = "0" * 64
    repo.add_wheel(whl, at="/")
    pm = manager(isolated, [repo.set_index(idx)])
    _refused(pm, "demo", "hash_mismatch")


def test_refused_record_tamper(isolated, repo):
    def tamper(files):
        files["mlaser_demo/__init__.py"] += b"\nimport os  # added after RECORD\n"
    good = build_wheel(isolated)
    idx = index_for([good])                       # the index of the published wheel ...
    whl = build_wheel(isolated, tamper=tamper)    # ... replaced by a tampered one with its own hash
    idx["plugins"][0]["versions"][0].update(sha256=sha256_file(whl), size=whl.stat().st_size)
    repo.add_wheel(whl, at="/")
    pm = manager(isolated, [repo.set_index(idx)])
    assert "RECORD hash mismatch" in str(_refused(pm, "demo", "record"))

    def unlisted(files):
        files["mlaser_demo/extra.py"] = b"x = 1\n"
    whl2 = build_wheel(isolated, dist="mlaser_demo2", pid="demo2", tamper=unlisted)
    _refused(pm, str(whl2), "record")


def test_refused_collision_with_mlaser_and_stdlib(isolated):
    pm = manager(isolated)
    _refused(pm, str(build_wheel(isolated, dist="mlaser", module="mlaser", pid="core")), "collision")
    _refused(pm, str(build_wheel(isolated, dist="jsonx", module="json", pid="jsonx")), "collision")
    # an importable module of the environment (pytest is installed in the test venv)
    _refused(pm, str(build_wheel(isolated, dist="pytestx", module="pytest", pid="pytestx")), "collision")


def test_refused_api_and_mlaser_ranges(isolated):
    pm = manager(isolated)
    _refused(pm, str(build_wheel(isolated, plugin={"api": ">=3"})), "incompatible")
    _refused(pm, str(build_wheel(isolated, plugin={"mlaser": ">=99"})), "incompatible")


def test_refused_missing_dependency(isolated):
    pm = manager(isolated)
    e = _refused(pm, str(build_wheel(isolated, requires=("no-such-package-xyz>=1", "numpy>=1"))), "dependency")
    assert e.detail["missing"] == ["no-such-package-xyz>=1"]
    # satisfied: an installed library, mlaser itself, a marker that does not apply
    build_wheel(isolated, requires=("numpy>=1", "mlaser>=0.1.0rc1", "nothere; extra == 'docs'"))
    assert pm.install(str(isolated / "mlaser_demo-0.1.0-py3-none-any.whl"))["id"] == "demo"


def test_refused_manifest_index_mismatch(isolated, repo):
    whl = build_wheel(isolated)
    idx = index_for([whl])
    idx["plugins"][0]["versions"][0]["permissions"]["machine"] = True     # the index claims less/more
    repo.add_wheel(whl, at="/")
    pm = manager(isolated, [repo.set_index(idx)])
    assert "permissions" in str(_refused(pm, "demo", "manifest_mismatch"))
    # a manifest that does not match its own entry point
    bad = build_wheel(isolated, dist="mlaser_demo3", pid="demo3", plugin={"entry": "elsewhere:plugin"})
    _refused(pm, str(bad), "manifest_mismatch")


def test_refused_http_url_and_redirect_and_size(isolated, repo):
    pm = manager(isolated)
    _refused(pm, "http://example.com/x-0.1.0-py3-none-any.whl#sha256=" + "0" * 64, "insecure_url")
    with pytest.raises(PM.RefusedError):
        pm.add_repository("http://example.com/mlaser-plugin-index.json")
    # a redirect to another host (127.0.0.1 -> localhost)
    whl = build_wheel(isolated)
    port = repo.httpd.server_address[1]
    repo.add_wheel(whl, at="/")
    repo.routes["/moved.whl"] = (302, {"Location": f"http://localhost:{port}/{whl.name}"}, b"")
    _refused(pm, repo.url("/moved.whl") + "#sha256=" + sha256_file(whl), "redirect")
    # a redirect on the same host is followed
    repo.routes["/same/mlaser_demo-0.1.0-py3-none-any.whl"] = (302, {"Location": f"/{whl.name}"}, b"")
    assert pm.install(repo.url("/same/mlaser_demo-0.1.0-py3-none-any.whl") + "#sha256=" + sha256_file(whl))
    pm.uninstall("demo")
    # size cap: the index says fewer bytes than the server sends
    idx = index_for([whl])
    idx["plugins"][0]["versions"][0]["size"] = 100
    pm2 = manager(isolated, [repo.set_index(idx, at="/small.json")])
    _refused(pm2, "demo", "size")


def test_refused_not_pure_and_unsafe_layout(isolated):
    pm = manager(isolated)
    _refused(pm, str(build_wheel(isolated, tag="cp312-cp312-linux_x86_64")), "not_pure")
    _refused(pm, str(build_wheel(isolated, purelib=False)), "not_pure")
    _refused(pm, str(build_wheel(isolated, extra_files={"mlaser_demo/_x.so": b"\x7fELF"})), "not_pure")
    _refused(pm, str(build_wheel(isolated, extra_files={"evil.pth": b"import os"})), "layout")
    _refused(pm, str(build_wheel(isolated, extra_files={"../escape.py": b"x=1"})), "layout")
    _refused(pm, str(build_wheel(isolated, manifest_text='[plugin]\nid = "demo"\n')), "manifest")


def test_index_size_cap_and_invalid_entries(isolated, repo, monkeypatch):
    whl = build_wheel(isolated)
    url = serve(repo, isolated, [whl])
    idx = json.loads(repo.routes["/mlaser-plugin-index.json"])
    idx["plugins"].append({"id": "BAD"})
    idx["plugins"].append({"id": "noversions", "versions": [{"version": "1", "url": "x.whl", "sha256": "zz"}]})
    repo.set_index(idx)
    pm = manager(isolated, [url])
    got = pm.fetch_index(url)
    assert [p.id for p in got.plugins] == ["demo"] and len(got.problems) == 2
    monkeypatch.setattr(PM, "MAX_INDEX_BYTES", 100)
    with pytest.raises(PM.RefusedError):
        pm.fetch_index(url, refresh=True)
    with pytest.raises(PM.PluginError):
        PM.parse_index({"format": "other"}, url)


def test_index_cache_used_when_offline(isolated, repo):
    whl = build_wheel(isolated)
    url = serve(repo, isolated, [whl])
    pm = manager(isolated, [url])
    assert pm.fetch_index(url).stale is False
    repo.close()
    stale = pm.fetch_index(url, refresh=True)
    assert stale.stale and [p.id for p in stale.plugins] == ["demo"]


def test_index_json_schema_and_builder(isolated, tmp_path):
    schema = json.loads((ROOT / "docs/dev/plugin-index.schema.json").read_text())
    whl = build_wheel(isolated)
    idx = index_for([whl])
    assert PM.validate_index(idx) == []
    try:
        import jsonschema
    except ImportError:
        jsonschema = None
    if jsonschema is not None:
        jsonschema.validate(idx, schema)
    assert schema["properties"]["format"]["const"] == "mlaser-plugin-index"
    v = idx["plugins"][0]["versions"][0]
    assert v["url"] == whl.name and v["sha256"] == sha256_file(whl) and v["entry"] == "mlaser_demo:plugin"
    # the tool: build with SHA256SUMS, update merges, check validates
    from mlaser.services import plugin_index as T
    out = tmp_path / "idx.json"
    sums = tmp_path / "SHA256SUMS"
    assert T.main(["build", str(whl), "-o", str(out), "--sha256sums", str(sums)]) == 0
    whl2 = build_wheel(isolated, version="0.2.0")
    assert T.main(["build", str(whl2), "-o", str(out), "--update", str(out)]) == 0
    vers = [x["version"] for x in json.loads(out.read_text())["plugins"][0]["versions"]]
    assert vers == ["0.2.0", "0.1.0"]
    assert PM.parse_sha256sums(sums.read_text()) == {whl.name: sha256_file(whl)}
    assert T.main(["check", str(out), str(whl),
                   str(ROOT / "examples/mlaser_hello/mlaser_hello/mlaser-plugin.toml")]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text('{"format": "mlaser-plugin-index", "version": 2, "plugins": []}')
    assert T.main(["check", str(bad)]) == 1


def test_example_plugin_follows_the_standard(isolated, tmp_path):
    """examples/mlaser_hello: its pyproject, entry point and manifest agree; the wheel passes
    every check of the manager and installs."""
    from plugin_fixtures import wheel_from_project
    whl = wheel_from_project(ROOT / "examples/mlaser_hello", tmp_path / "dist")
    info = PM.inspect_wheel(whl.read_bytes(), whl.name)
    assert info.id == "hello" and info.entry_value == "mlaser_hello:plugin" and info.version == "0.2.0"
    assert info.permissions["document"] is True and info.permissions["network"] == []
    pm = manager(isolated)
    assert pm.install(str(whl))["id"] == "hello"


# ----------------------------------------------------------------------------- sources
def test_install_from_url_needs_a_hash(isolated, repo):
    whl = build_wheel(isolated)
    url = repo.add_wheel(whl)
    pm = manager(isolated)
    e = _refused(pm, url, "unverified")
    assert e.detail["sha256"] == sha256_file(whl)
    _refused(pm, url + "#sha256=" + "1" * 64, "hash_mismatch")
    seen = []
    rec = pm.install(url, confirm_hash=lambda h, n: seen.append((h, n)) or True)
    assert seen == [(sha256_file(whl), whl.stat().st_size)] and rec["source"]["kind"] == "url"


def test_install_from_github_release(isolated, repo):
    w1 = build_wheel(isolated)
    w1_url = repo.add_wheel(w1, at="/dl/")
    sums = f"{sha256_file(w1)}  {w1.name}\n".encode()
    repo.routes["/dl/SHA256SUMS"] = sums
    release = {"tag_name": "v0.1.0", "published_at": "2026-10-20T00:00:00Z",
               "assets": [{"name": w1.name, "browser_download_url": w1_url, "size": w1.stat().st_size},
                          {"name": "SHA256SUMS", "browser_download_url": repo.url("/dl/SHA256SUMS")}]}
    repo.routes["/repos/acme/plugs/releases/latest"] = json.dumps(release).encode()
    pm = manager(isolated, github_api=repo.base)
    rec = pm.install("github:acme/plugs")
    assert rec["id"] == "demo" and rec["source"]["kind"] == "github"
    pm.uninstall("demo")
    # the asset digest instead of SHA256SUMS
    release["assets"] = [{"name": w1.name, "browser_download_url": w1_url, "digest": "sha256:" + sha256_file(w1)}]
    repo.routes["/repos/acme/plugs/releases/latest"] = json.dumps(release).encode()
    assert pm.install("github:acme/plugs")["id"] == "demo"
    pm.uninstall("demo")
    # a wrong digest is refused
    release["assets"][0]["digest"] = "sha256:" + "2" * 64
    repo.routes["/repos/acme/plugs/releases/latest"] = json.dumps(release).encode()
    _refused(pm, "github:acme/plugs", "hash_mismatch")
    # the release's index asset, as a repository
    idx_url = repo.set_index(index_for([w1]), at="/dl/mlaser-plugin-index.json")
    release["assets"].append({"name": "mlaser-plugin-index.json", "browser_download_url": idx_url})
    repo.routes["/repos/acme/plugs/releases/latest"] = json.dumps(release).encode()
    assert pm.add_repository("https://github.com/acme/plugs")["url"] == "github:acme/plugs"
    assert [p.id for p in pm.search("")] == ["demo"]
    assert pm.install("demo")["source"]["repository"] == "github:acme/plugs"
    with pytest.raises(PM.PluginError):
        pm.add_repository("github:not a repo")


# ----------------------------------------------------------------------------- updates
def test_update_keeps_or_drops_the_enabled_state(isolated, repo):
    tmp = isolated
    w1 = build_wheel(tmp)
    pm = manager(tmp, [serve(repo, tmp, [w1])])
    rec = pm.install("demo")
    pm.enable("demo", rec["entry"], rec["permissions"])
    assert pm.check_updates(refresh=True) == []
    # 0.2.0: same entry and permissions -> stays enabled
    w2 = build_wheel(tmp, version="0.2.0")
    serve(repo, tmp, [w1, w2])
    ups = pm.check_updates(refresh=True)
    assert ups == [{"id": "demo", "installed": "0.1.0", "available": "0.2.0", "repository": ups[0]["repository"],
                    "permissions_changed": False, "permissions": ups[0]["permissions"]}]
    res = pm.update("demo")
    assert res["updated"] and res["kept_enabled"] and not res["needs_enable"]
    assert pm.config.get(P.ALLOWLIST_KEY) == {"demo": "mlaser_demo:plugin"}
    assert pm.installed()["demo"]["version"] == "0.2.0"
    assert (pm.site / "mlaser_demo-0.2.0.dist-info").is_dir() and not (pm.site / "mlaser_demo-0.1.0.dist-info").exists()
    # 0.3.0 asks for the network: disabled, must be enabled again
    w3 = build_wheel(tmp, version="0.3.0", permissions={"network": ["api.example.com"]})
    serve(repo, tmp, [w1, w2, w3])
    assert pm.check_updates(refresh=True)[0]["permissions_changed"] is True
    res = pm.update("demo")
    assert res["needs_enable"] and not res["kept_enabled"]
    assert "demo" not in pm.config.get(P.ALLOWLIST_KEY, {})
    assert pm.update("demo")["updated"] is False                       # newest already


def test_changed_permissions_need_enabling_again():
    info = P.PluginInfo("x", "m:plugin", "entry-point", manifest={"plugin": {}, "permissions": {"document": True}})
    allow = {"x": "m:plugin"}
    assert P.plugin_status(info, allow, {"x": {"document": True}}) == "enabled"
    assert P.plugin_status(info, allow, {"x": {"document": False}}) == "changed"
    assert P.plugin_status(info, allow, {}) == "enabled"                # enabled before the record existed


def test_repositories_default_and_audit(isolated, caplog):
    import logging
    cfg = ConfigService(isolated / "c.json")
    pm = PM.PluginManager(cfg, root=isolated / "p")
    assert pm.repositories()[0]["url"] == \
        "https://github.com/rahilm/fiber_laser/releases/latest/download/mlaser-plugin-index.json"
    with caplog.at_level(logging.INFO, logger="mlaser.audit"):
        pm.add_repository("https://plugins.example.org/mlaser-plugin-index.json", "example")
        pm.remove_repository("https://plugins.example.org/mlaser-plugin-index.json")
    events = [getattr(r, "audit", {}).get("event") for r in caplog.records]
    assert "plugin_repo_add" in events and "plugin_repo_remove" in events
    with pytest.raises(PM.NotFoundError):
        pm.remove_repository("https://nope.example.org/x.json")


# ----------------------------------------------------------------------------- CLI
def cli(*args, data=None):
    from mlaser.cli import main
    out, err = io.StringIO(), io.StringIO()
    argv = list(args)
    code = main(argv, out=out, err=err)
    text = out.getvalue()
    return code, (json.loads(text) if "--json" in argv and text.strip() else text), err.getvalue()


def test_cli_plugin_commands(isolated, repo):
    tmp = isolated
    whl = build_wheel(tmp)
    url = serve(repo, tmp, [whl])
    cfg = str(tmp / "cli-config.json")
    common = ["--json", "--config", cfg]
    code, res, _ = cli("plugin", "repo", "remove",
                       "https://github.com/rahilm/fiber_laser/releases/latest/download/mlaser-plugin-index.json", *common)
    assert code == 0 and res["ok"]
    code, res, _ = cli("plugin", "repo", "add", url, *common)
    assert code == 0 and res["repository"]["added"] is True
    code, res, _ = cli("plugin", "repo", "list", *common)
    assert [r["url"] for r in res["repositories"]] == [url]
    code, res, _ = cli("plugin", "search", "demo", *common)
    assert code == 0 and [p["id"] for p in res["plugins"]] == ["demo"]
    code, res, _ = cli("plugin", "info", "demo", *common)
    assert res["plugin"]["versions"][0]["permissions"]["document"] is True
    code, res, _ = cli("plugin", "install", "demo", *common)
    assert code == 0 and res["installed"]["version"] == "0.1.0" and res["enabled"] is False
    code, res, _ = cli("plugin", "list", *common)
    assert [(p["id"], p["status"]) for p in res["plugins"] if p["id"] == "demo"] == [("demo", "disabled")]
    code, res, _ = cli("plugin", "install", "demo", *common)
    assert code == 6 and res["error"]["code"] == "installed"
    code, res, _ = cli("plugin", "enable", "demo", *common)                  # needs --yes
    assert code == 6 and res["error"]["code"] == "confirm"
    code, res, _ = cli("plugin", "enable", "demo", "--yes", *common)
    assert code == 0 and res["enabled"]["enabled"] is True and res["enabled"]["authority"] is False
    assert ConfigService(cfg).get(P.ALLOWLIST_KEY) == {"demo": "mlaser_demo:plugin"}
    code, res, _ = cli("plugin", "update", "--check", *common)
    assert code == 0 and res["updates"] == []
    code, res, _ = cli("plugin", "disable", "demo", *common)
    assert code == 0 and ConfigService(cfg).get(P.ALLOWLIST_KEY) == {}
    code, res, _ = cli("plugin", "uninstall", "demo", *common)
    assert code == 0 and res["uninstalled"]["files_removed"] > 0
    code, res, _ = cli("plugin", "uninstall", "demo", *common)
    assert code == 3 and res["error"]["code"] == "not_found"
    code, res, _ = cli("plugin", "install", str(tmp / "missing.whl"), *common)
    assert code == 3
    code, res, _ = cli("plugin", "install", "http://example.com/a-1-py3-none-any.whl", *common)
    assert code == 6 and res["error"]["code"] == "insecure_url"
    code, text, _ = cli("plugin", "list", "--config", cfg)
    assert code == 0 and isinstance(text, str)
    code, res, _ = cli("plugin", *common)
    assert code == 2
