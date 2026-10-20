"""``mlaser plugin ...``: the plugin manager on the command line (docs/user/plugins.md).

=====================================  ==========================================================
``plugin list``                         installed plugins: version, origin, status, permissions
``plugin search [TERMS]``               the repositories' plugins
``plugin info ID``                      one plugin: versions, permissions, requirements
``plugin install SOURCE``               an id (``onshape``, ``onshape==0.2.0``), ``github:owner/repo``,
                                        an ``https://.../x.whl[#sha256=HEX]`` URL or a local ``.whl``
``plugin uninstall ID``                 removes exactly the recorded files (``--purge-data``: its data too)
``plugin update [ID] [--check]``        newer compatible versions; ``--check`` only lists them
``plugin enable ID --yes``              enable (allowlist) with the permissions shown; never grants
                                        machine authority
``plugin disable ID``                   disable
``plugin hosts ID [--add H --yes]``     network hosts granted besides the manifest (``--remove H``)
``plugin repo add|remove|list``         the repositories (an index URL or ``github:owner/repo``)
=====================================  ==========================================================

Exit codes: 0 ok, 2 usage, 3 not found (plugin, file, repository), 4 failed (network,
configuration not writable), 6 refused (verification: hash, RECORD, pure wheel, collision,
compatibility, dependencies; policy: https only, enabling without ``--yes``).

Installing never enables a plugin.  ``plugin enable`` is the command-line form of the Service-mode
allowlist: it needs ``--yes`` after the permissions are printed, it is audited, and it cannot grant
machine authority (that is done at the machine, in the GUI, in Service mode).  Rationale: the
CLI runs with the same OS account that owns ``config.json`` (anyone who can run it can edit that
file), so refusing it would add no protection, while provisioning shop PCs needs it; the grant
that lets a plugin act on the machine stays a deliberate act at the machine.  Changes apply at
the next start of the application (close it first: a running application keeps its own copy of
the configuration).
"""
from __future__ import annotations

import argparse
import os
from typing import Any, Callable, Optional

from .common import EXIT_FAILED, EXIT_INPUT, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, CliError

Result = tuple[int, dict, Callable[[dict], str]]


def add_parser(sub: Any, common: Callable[[argparse.ArgumentParser], None], parser_class: Any) -> None:
    q = sub.add_parser("plugin", help="plugins: list, search, install, update, remove, repositories")
    qs = q.add_subparsers(dest="plugin_command", parser_class=parser_class)

    def p(name: str, help_: str) -> argparse.ArgumentParser:
        x = qs.add_parser(name, help=help_)
        common(x)
        x.add_argument("--data-dir", metavar="DIR", help="mlaser data directory (default: the user's)")
        return x

    p("list", "installed plugins")
    x = p("search", "search the repositories")
    x.add_argument("terms", nargs="*")
    x.add_argument("--refresh", action="store_true", help="fetch the indexes again")
    x = p("info", "one plugin of the repositories")
    x.add_argument("id")
    x = p("install", "install (never enables)")
    x.add_argument("source", help="ID[==VERSION], github:OWNER/REPO[@TAG], https://.../x.whl[#sha256=HEX] or x.whl")
    x.add_argument("--version", default="")
    x.add_argument("--sha256", default="", help="expected sha256 of a wheel URL or file")
    x.add_argument("--accept-hash", action="store_true",
                   help="accept the downloaded hash of a wheel URL without --sha256 (it is printed)")
    x.add_argument("--plugin", default="", help="the plugin id, when a release has several")
    x.add_argument("--pre", action="store_true", help="allow pre-release versions")
    x = p("uninstall", "remove a plugin the manager installed")
    x.add_argument("id")
    x.add_argument("--purge-data", action="store_true", help="also delete its data directory and secrets file")
    x = p("update", "update plugins (or --check)")
    x.add_argument("id", nargs="?")
    x.add_argument("--check", action="store_true", help="only list the available updates")
    x.add_argument("--pre", action="store_true")
    x = p("enable", "enable a plugin (Service authority; needs --yes)")
    x.add_argument("id")
    x.add_argument("--yes", action="store_true", help="confirm the permissions printed")
    x = p("disable", "disable a plugin")
    x.add_argument("id")
    x = p("hosts", "network hosts granted to a plugin besides its manifest")
    x.add_argument("id")
    x.add_argument("--add", action="append", default=[], metavar="HOST", help="grant an exact host (needs --yes)")
    x.add_argument("--remove", action="append", default=[], metavar="HOST")
    x.add_argument("--yes", action="store_true")
    r = qs.add_parser("repo", help="plugin repositories")
    rs = r.add_subparsers(dest="repo_command", parser_class=parser_class)
    for name, help_ in (("add", "add an index URL or github:OWNER/REPO"), ("remove", "remove a repository"),
                        ("list", "list the repositories")):
        x = rs.add_parser(name, help=help_)
        common(x)
        x.add_argument("--data-dir", metavar="DIR")
        if name != "list":
            x.add_argument("url")
        if name == "add":
            x.add_argument("--name", default="")


def _manager(args: Any) -> Any:
    if getattr(args, "data_dir", None):
        os.environ["MLASER_DATA_DIR"] = os.path.abspath(args.data_dir)
    from mlaser.services.config import ConfigService
    from mlaser.services.plugin_manager import PluginManager
    try:
        cfg = ConfigService(args.config) if getattr(args, "config", None) else ConfigService()
    except Exception as exc:  # noqa: BLE001
        raise CliError(f"configuration: {exc}", EXIT_INPUT, "config") from exc
    from mlaser.services.config import _test_run
    if not _test_run():
        try:
            from mlaser.machine.logsetup import setup_logging
            setup_logging()                  # plugin_install ... go to audit.log
        except Exception:  # noqa: BLE001 - no log directory: the command still runs
            pass
    return PluginManager(cfg)


def _wrap(fn: Callable[..., Any], *a: Any, **kw: Any) -> Any:
    from mlaser.services.plugin_manager import FetchError, NotFoundError, PluginError, RefusedError
    try:
        return fn(*a, **kw)
    except RefusedError as exc:
        raise CliError(str(exc), EXIT_REFUSED, exc.code, **_jsonable(exc.detail)) from exc
    except NotFoundError as exc:
        raise CliError(str(exc), EXIT_INPUT, "not_found") from exc
    except FetchError as exc:
        raise CliError(str(exc), EXIT_FAILED, "fetch") from exc
    except PluginError as exc:
        code = {"usage": EXIT_USAGE, "installed": EXIT_REFUSED, "cancelled": EXIT_REFUSED}.get(exc.code, EXIT_FAILED)
        raise CliError(str(exc), code, exc.code, **_jsonable(exc.detail)) from exc


def _jsonable(d: dict) -> dict:
    return {k: v for k, v in d.items() if isinstance(v, (str, int, float, bool, list, dict, type(None)))}


def _perm_text(perms: Optional[dict]) -> str:
    if perms is None:
        return "legacy (no manifest)"
    from mlaser.services.plugin_manifest import Permissions
    return "; ".join(Permissions.from_dict(perms).lines())


def cmd_plugin(args: Any) -> Result:
    sub = args.plugin_command
    if sub == "repo":
        return _repo(args)
    pm = _manager(args)
    if sub == "list":
        rows = _wrap(pm.catalog)
        return EXIT_OK, {"plugins": rows}, lambda d: "\n".join(
            [f"{r['id']:<20} {r['version'] or '-':<10} {r['origin']:<12} {r['status']:<9} "
             f"{_perm_text(r['permissions'])}" for r in d["plugins"]] or ["no plugin installed"])
    if sub == "search":
        found = _wrap(pm.search, " ".join(args.terms), refresh=args.refresh)
        res = {"plugins": [_brief(p) for p in found], "problems": pm.errors}
        return EXIT_OK, res, lambda d: "\n".join(
            [f"{p['id']:<20} {p['latest'] or '-':<10} {p['summary']}" for p in d["plugins"]]
            + [f"warning: {e}" for e in d["problems"]] or ["no plugin found"])
    if sub == "info":
        p = _wrap(pm.find, args.id)
        best = p.best(pm.plugin_api)
        res = {"plugin": p.to_dict(), "latest_compatible": best.version if best else None}
        return EXIT_OK, res, lambda d: _info_text(d)
    if sub == "install":
        if args.accept_hash and not args.sha256:
            confirm_hash: Optional[Callable[[str, int], bool]] = lambda h, n: True
        else:
            confirm_hash = None
        rec = _wrap(pm.install, args.source, version=args.version, sha256=args.sha256, plugin_id=args.plugin,
                    pre=args.pre, confirm_hash=confirm_hash)
        res = {"installed": rec, "enabled": False, "restart_required": True}
        return EXIT_OK, res, lambda d: (
            f"installed {d['installed']['id']} {d['installed']['version']} (sha256 {d['installed']['sha256']})\n"
            f"permissions: {_perm_text(d['installed']['permissions'])}\n"
            "not enabled: a service engineer enables it (Plugin manager, or `mlaser plugin enable "
            f"{d['installed']['id']} --yes`); it loads at the next start")
    if sub == "uninstall":
        res = _wrap(pm.uninstall, args.id, purge_data=args.purge_data)
        return EXIT_OK, {"uninstalled": res, "restart_required": True}, lambda d: (
            f"removed {d['uninstalled']['id']} ({d['uninstalled']['files_removed']} files); restart to apply")
    if sub == "update":
        if args.check or not args.id:
            ups = _wrap(pm.check_updates, refresh=True, pre=args.pre)
            if args.check:
                return EXIT_OK, {"updates": ups}, lambda d: "\n".join(
                    [f"{u['id']}: {u['installed']} -> {u['available']}"
                     + (" (permissions change: must be enabled again)" if u["permissions_changed"] else "")
                     for u in d["updates"]] or ["everything is up to date"])
            ids = [u["id"] for u in ups]
        else:
            ids = [args.id]
        done = [_wrap(pm.update, i, pre=args.pre) for i in ids]
        return EXIT_OK, {"updated": done, "restart_required": any(u["updated"] for u in done)}, lambda d: "\n".join(
            [f"{u['id']}: " + (f"{u['before']} -> {u['version']}" if u["updated"] else "up to date")
             + (" - disabled: its entry point or permissions changed; enable it again" if u["needs_enable"] else "")
             for u in d["updated"]] or ["nothing to update"])
    if sub == "enable":
        row = next((r for r in _wrap(pm.catalog) if r["id"] == args.id), None)
        if row is None:
            raise CliError(f"no installed plugin {args.id!r}", EXIT_INPUT, "not_found")
        if row.get("manifest_error"):
            raise CliError(f"{args.id}: invalid manifest: {row['manifest_error']}", EXIT_REFUSED, "manifest")
        warn = ("Enabling a plugin needs Service authority: it runs inside mlaser with your account's rights. "
                f"Permissions: {_perm_text(row['permissions'])}.  Machine authority is not granted here "
                "(GUI, Service mode).")
        if not args.yes:
            raise CliError(warn + "  Repeat with --yes to enable.", EXIT_REFUSED, "confirm",
                           permissions=row["permissions"])
        res = _wrap(pm.enable, args.id, row["entry"], row["permissions"], grant_authority=False)
        return EXIT_OK, {"enabled": res, "permissions": row["permissions"], "restart_required": True,
                         "warning": warn}, lambda d: f"enabled {d['enabled']['id']} (at the next start)\n{d['warning']}"
    if sub == "disable":
        res = _wrap(pm.disable, args.id)
        return EXIT_OK, {"disabled": res, "restart_required": True}, lambda d: (
            f"disabled {d['disabled']['id']}" + ("" if d["disabled"]["was_enabled"] else " (was not enabled)"))
    if sub == "hosts":
        if args.add and not args.yes:
            raise CliError(f"granting {', '.join(args.add)} to {args.id} widens its network permission; "
                           "repeat with --yes", EXIT_REFUSED, "confirm")
        res = _wrap(pm.grant_hosts, args.id, add=args.add, remove=args.remove)
        return EXIT_OK, {"hosts": res}, lambda d: f"{d['hosts']['id']}: " + (", ".join(d["hosts"]["hosts"]) or "none")
    raise CliError("plugin: a command is required (list, search, info, install, uninstall, update, enable, "
                   "disable, hosts, repo add|remove|list)", EXIT_USAGE, "usage")


def _repo(args: Any) -> Result:
    sub = getattr(args, "repo_command", None)
    if sub is None:
        raise CliError("plugin repo: add, remove or list", EXIT_USAGE, "usage")
    pm = _manager(args)
    if sub == "add":
        res = _wrap(pm.add_repository, args.url, args.name)
        return EXIT_OK, {"repository": res}, lambda d: (
            ("added " if d["repository"]["added"] else "already listed: ") + d["repository"]["url"])
    if sub == "remove":
        res = _wrap(pm.remove_repository, args.url)
        return EXIT_OK, {"repository": res}, lambda d: "removed " + d["repository"]["url"]
    repos = pm.repositories()
    return EXIT_OK, {"repositories": repos}, lambda d: "\n".join(
        [f"{r['url']}" + (f"  ({r['name']})" if r["name"] else "") for r in d["repositories"]] or ["no repository"])


def _brief(p: Any) -> dict:
    from mlaser.services.plugins import PLUGIN_API
    best = p.best(PLUGIN_API)
    return {"id": p.id, "name": p.name, "summary": p.summary, "latest": best.version if best else None,
            "repository": p.repository, "homepage": p.homepage}


def _info_text(d: dict) -> str:
    p = d["plugin"]
    out = [f"{p['id']}: {p['name']}", p["summary"] or "", f"repository: {p['repository']}"]
    if p["homepage"]:
        out.append(f"homepage: {p['homepage']}")
    for v in p["versions"]:
        out.append(f"  {v['version']:<10} api {v['api'] or '-'}  mlaser {v['mlaser'] or '-'}  "
                   f"requires {', '.join(v['requires']) or '-'}")
        out.append(f"             permissions: {_perm_text(v['permissions']) if not v['synthetic'] else 'in the wheel'}")
    out.append(f"latest compatible: {d['latest_compatible'] or 'none'}")
    return "\n".join(x for x in out if x)


__all__ = ["add_parser", "cmd_plugin"]
