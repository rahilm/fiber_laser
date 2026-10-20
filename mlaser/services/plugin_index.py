"""Build, update and check an ``mlaser-plugin-index.json`` (docs/dev/PLUGIN_STANDARD.md §6).

::

    python -m mlaser.services.plugin_index build dist/*.whl -o mlaser-plugin-index.json \\
        [--base-url https://github.com/<owner>/<repo>/releases/download/<tag>/] [--name "My plugins"] \\
        [--homepage URL] [--update existing.json] [--sha256sums SHA256SUMS]
    python -m mlaser.services.plugin_index check mlaser-plugin-index.json | mlaser-plugin.toml | x.whl

``build`` reads each wheel exactly as the plugin manager verifies it (:func:`inspect_wheel`:
RECORD hashes, pure wheel, one entry point, a valid manifest that matches it) and writes one
version entry per wheel (sha256, size, the manifest's api / mlaser / permissions / entry and the
wheel's Requires-Dist).  Without ``--base-url`` the ``url`` is the bare file name, which resolves
against the index URL (the right thing when the index and the wheels are assets of the same
release: ``.../releases/latest/download/mlaser-plugin-index.json``).  ``--update`` merges into
an existing index (other plugins and versions are kept, a version that is built again is
replaced).  ``tools/plugin_index.py`` is the same tool in a checkout.

Only the standard library and Qt-free mlaser modules: it runs in a plugin's CI after
``pip install --no-deps mlaser`` (or a checkout on ``PYTHONPATH``).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any, Iterable, Optional

from .pep440 import Version
from .plugin_manifest import ManifestError, parse_manifest
from .plugin_manager import INDEX_FORMAT, INDEX_VERSION, PluginError, inspect_wheel, sha256_hex, validate_index


def version_entry(path: str, base_url: str = "", published: str = "") -> tuple[dict, dict]:
    """(plugin fields, version entry) of one wheel."""
    with open(path, "rb") as fh:
        data = fh.read()
    info = inspect_wheel(data, os.path.basename(path))
    p = info.manifest.get("plugin", {})
    url = (base_url.rstrip("/") + "/" if base_url else "") + os.path.basename(path)
    entry = {"version": info.version, "url": url, "sha256": sha256_hex(data), "size": len(data),
             "api": p.get("api", ""), "mlaser": p.get("mlaser", ""), "entry": info.entry_value,
             "requires": info.requires, "permissions": info.permissions,
             "published": published or datetime.date.today().isoformat()}
    plugin = {"id": info.id, "name": p.get("name", info.id), "summary": p.get("summary", ""),
              "description": p.get("description", ""), "homepage": p.get("homepage", ""),
              "source": p.get("source", ""), "authors": list(p.get("authors", [])), "license": p.get("license", "")}
    return {k: v for k, v in plugin.items() if v not in ("", [])} | {"id": info.id}, entry


def build_index(wheels: Iterable[str], base_url: str = "", name: str = "", homepage: str = "",
                existing: Optional[dict] = None, published: str = "") -> dict:
    index: dict[str, Any] = dict(existing or {"format": INDEX_FORMAT, "version": INDEX_VERSION, "plugins": []})
    index["format"], index["version"] = INDEX_FORMAT, INDEX_VERSION
    if name:
        index["name"] = name
    if homepage:
        index["homepage"] = homepage
    plugins = {p["id"]: p for p in index.get("plugins", []) if isinstance(p, dict) and "id" in p}
    for w in wheels:
        fields, entry = version_entry(w, base_url, published)
        p = plugins.setdefault(fields["id"], {"id": fields["id"], "versions": []})
        p.update({k: v for k, v in fields.items() if k != "id"})
        p["versions"] = [v for v in p.get("versions", []) if v.get("version") != entry["version"]] + [entry]
        p["versions"].sort(key=lambda v: Version.parse(v["version"]), reverse=True)
    index["plugins"] = [plugins[k] for k in sorted(plugins)]
    return index


def sha256sums(wheels: Iterable[str]) -> str:
    out = []
    for w in wheels:
        with open(w, "rb") as fh:
            out.append(f"{sha256_hex(fh.read())}  {os.path.basename(w)}")
    return "\n".join(out) + "\n"


def check(path: str) -> list[str]:
    """Problems of an index (.json), a manifest (.toml) or a wheel (.whl); empty: valid."""
    try:
        if path.endswith(".whl"):
            with open(path, "rb") as fh:
                inspect_wheel(fh.read(), os.path.basename(path))
            return []
        if path.endswith(".toml"):
            with open(path, "rb") as fh:
                parse_manifest(fh.read())
            return []
        with open(path, encoding="utf-8") as fh:
            return validate_index(json.load(fh))
    except ManifestError as exc:
        return exc.errors
    except PluginError as exc:
        return [str(exc)]
    except (OSError, ValueError) as exc:
        return [f"{path}: {exc}"]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="plugin_index", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build or update an index from wheels")
    b.add_argument("wheels", nargs="+")
    b.add_argument("-o", "--out", default="mlaser-plugin-index.json")
    b.add_argument("--base-url", default="", help="prefix of the wheel URLs (default: relative to the index)")
    b.add_argument("--name", default="")
    b.add_argument("--homepage", default="")
    b.add_argument("--update", metavar="INDEX", help="merge into this existing index")
    b.add_argument("--sha256sums", metavar="FILE", help="also write a SHA256SUMS file")
    b.add_argument("--published", default="", help="publication date (default: today)")
    c = sub.add_parser("check", help="check an index, a manifest or a wheel")
    c.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        existing = None
        if args.update and os.path.exists(args.update):
            with open(args.update, encoding="utf-8") as fh:
                existing = json.load(fh)
        try:
            idx = build_index(args.wheels, args.base_url, args.name, args.homepage, existing, args.published)
        except PluginError as exc:
            print(f"plugin_index: {exc}", file=sys.stderr)
            return 1
        problems = validate_index(idx)
        if problems:
            print("plugin_index: the result is invalid:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 1
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(idx, fh, indent=1, sort_keys=True)
            fh.write("\n")
        if args.sha256sums:
            with open(args.sha256sums, "w", encoding="utf-8") as fh:
                fh.write(sha256sums(args.wheels))
        n = sum(len(p["versions"]) for p in idx["plugins"])
        print(f"{args.out}: {len(idx['plugins'])} plugin(s), {n} version(s)")
        return 0
    bad = 0
    for p in args.paths:
        problems = check(p)
        print(f"{p}: " + ("ok" if not problems else "\n  " + "\n  ".join(problems)))
        bad += bool(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
