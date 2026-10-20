"""The plugin manifest ``mlaser-plugin.toml`` and the permissions it declares.

The manifest lives inside a plugin's top-level import package and is read from the installed
distribution's file list **without importing the plugin** (docs/dev/PLUGIN_STANDARD.md §2).
This module is the schema check (:func:`validate_manifest`), the parser (:func:`parse_manifest`),
the lookup in an installed distribution (:func:`manifest_for_dist`) and :class:`Permissions`,
the frozen view a plugin gets as ``ctx.permissions``.

Qt-free and dependency-free (``tomllib``), so ``python -m mlaser.services.plugin_index`` can
use it in a plugin author's CI without the GUI libraries.
"""
from __future__ import annotations

import ipaddress
import re
import tomllib
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .pep440 import InvalidSpecifier, InvalidVersion, SpecifierSet, Version

MANIFEST_NAME = "mlaser-plugin.toml"
#: a plugin id: the entry-point name, the settings prefix ``plugin.<id>.*``, the data directory
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
ENTRY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?::[A-Za-z_][A-Za-z0-9_]*"
                      r"(?:\.[A-Za-z_][A-Za-z0-9_]*)*)?$")
MAX_MANIFEST_BYTES = 64 * 1024
MIN_MODES = ("operator", "engineer", "service")
#: boolean permissions, in display order (``network`` is a host list)
BOOL_PERMISSIONS = ("files", "document", "orders", "queue", "machine", "secrets")
PERMISSION_KEYS = ("network",) + BOOL_PERMISSIONS
#: [plugin] keys: required ones, then optional ones with their type
REQUIRED_KEYS = ("id", "name", "version", "entry", "api", "mlaser")
STRING_KEYS = ("id", "name", "version", "entry", "api", "mlaser", "summary", "description", "license",
               "homepage", "source", "min_mode", "icon")
#: ``icon``: a file inside the plugin's top-level package (SVG drawn with ``currentColor`` like
#: the app's own icons, or PNG), relative to the package directory
ICON_RE = re.compile(r"^(?:[A-Za-z0-9_.-]+/){0,4}[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}\.(?:svg|png)$")
MAX_TEXT = {"summary": 200, "description": 20000, "name": 80}
_HOST_RE = re.compile(r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

PERMISSION_TEXT = {
    "network": "contacts these hosts: {hosts}",
    "files": "reads and writes files you choose, and its own data folder",
    "document": "edits or replaces the open drawing",
    "orders": "adds order lines and nest parts",
    "queue": "adds jobs to the queue (never starts them)",
    "machine": "uses the machine controller (also needs the machine-authority grant)",
    "secrets": "stores credentials (keyring or a private file)",
}


class ManifestError(ValueError):
    """An invalid manifest; ``errors`` lists every problem found."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = list(errors)


def valid_host_pattern(h: Any) -> bool:
    """An exact host name, an IP address, or ``*.example.com`` (subdomains only)."""
    if not isinstance(h, str) or not h or len(h) > 253:
        return False
    if h != h.lower():
        return False
    try:
        ipaddress.ip_address(h.strip("[]"))
        return True
    except ValueError:
        pass
    return bool(_HOST_RE.match(h)) and h.count("*") <= 1 and (not h.startswith("*.") or "." in h[2:])


def host_matches(host: str, pattern: str) -> bool:
    host = (host or "").lower().rstrip(".").strip("[]")
    pattern = pattern.lower().strip("[]")
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and len(host) > len(pattern) - 1
    return host == pattern


@dataclass(frozen=True)
class Permissions:
    """``ctx.permissions``: the manifest's ``[permissions]`` (``all``: a built-in plugin, which may
    use everything; ``legacy``: an entry-point plugin without a manifest, API-1 behaviour)."""

    network: tuple[str, ...] = ()
    files: bool = False
    document: bool = False
    orders: bool = False
    queue: bool = False
    machine: bool = False
    secrets: bool = False
    all: bool = False
    legacy: bool = False

    @classmethod
    def everything(cls) -> "Permissions":
        return cls(all=True)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "Permissions":
        data = data or {}
        errs = permission_errors(data)
        if errs:
            raise ManifestError(errs)
        return cls(network=tuple(data.get("network", ()) or ()),
                   **{k: bool(data.get(k, False)) for k in BOOL_PERMISSIONS})

    def allows(self, name: str) -> bool:
        if self.all:
            return True
        if name == "network":
            return bool(self.network)
        return bool(getattr(self, name, False))

    def allows_host(self, host: str) -> bool:
        if self.all:
            return True
        return any(host_matches(host, p) for p in self.network)

    def to_dict(self) -> dict:
        if self.all:
            return {"all": True}
        return {"network": sorted(self.network), **{k: getattr(self, k) for k in BOOL_PERMISSIONS}}

    def lines(self) -> list[str]:
        """One readable line per granted permission (the install and enable confirmations)."""
        if self.all:
            return ["built in: everything"]
        out = []
        if self.network:
            out.append(PERMISSION_TEXT["network"].format(hosts=", ".join(self.network)))
        out += [PERMISSION_TEXT[k] for k in BOOL_PERMISSIONS if getattr(self, k)]
        return out or ["none"]


def normalize_permissions(data: Optional[dict]) -> dict:
    """The comparable form of a ``[permissions]`` table (index entry vs manifest)."""
    return Permissions.from_dict(data or {}).to_dict()


def permission_errors(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ["[permissions] must be a table"]
    errs = []
    net = data.get("network", [])
    if not isinstance(net, list) or len(net) > 64:
        errs.append("permissions.network must be a list of at most 64 host names")
    else:
        for h in net:
            if not valid_host_pattern(h):
                errs.append(f"permissions.network: invalid host {h!r} (lower-case host name, IP, or *.domain)")
    for k in BOOL_PERMISSIONS:
        if k in data and not isinstance(data[k], bool):
            errs.append(f"permissions.{k} must be true or false")
    return errs


def validate_manifest(data: Any) -> list[str]:
    """Every schema problem of a parsed manifest (empty list: valid).  Unknown keys are not
    errors (kept and shown, ignored)."""
    if not isinstance(data, dict):
        return ["the manifest must be a TOML document"]
    errs: list[str] = []
    plugin = data.get("plugin")
    if not isinstance(plugin, dict):
        return ["missing [plugin] table"]
    for k in REQUIRED_KEYS:
        if k not in plugin:
            errs.append(f"plugin.{k} is required")
    for k in STRING_KEYS:
        if k in plugin and not isinstance(plugin[k], str):
            errs.append(f"plugin.{k} must be a string")
    for k, n in MAX_TEXT.items():
        if isinstance(plugin.get(k), str) and len(plugin[k]) > n:
            errs.append(f"plugin.{k} is longer than {n} characters")
    pid = plugin.get("id")
    if isinstance(pid, str) and not ID_RE.match(pid):
        errs.append(f"plugin.id {pid!r} must match {ID_RE.pattern}")
    ver = plugin.get("version")
    if isinstance(ver, str):
        try:
            Version.parse(ver)
        except InvalidVersion:
            errs.append(f"plugin.version {ver!r} is not a PEP 440 version")
    entry = plugin.get("entry")
    if isinstance(entry, str) and not ENTRY_RE.match(entry):
        errs.append(f"plugin.entry {entry!r} must be 'module' or 'module:attribute'")
    for k in ("api", "mlaser"):
        if isinstance(plugin.get(k), str):
            try:
                SpecifierSet(plugin[k])
            except InvalidSpecifier:
                errs.append(f"plugin.{k} {plugin[k]!r} is not a PEP 440 version specifier")
    icon = plugin.get("icon")
    if isinstance(icon, str) and (not ICON_RE.match(icon) or ".." in icon.split("/")):
        errs.append(f"plugin.icon {icon!r} must be a relative .svg or .png path inside the package")
    mm = plugin.get("min_mode")
    if isinstance(mm, str) and mm not in MIN_MODES:
        errs.append(f"plugin.min_mode must be one of {', '.join(MIN_MODES)}")
    authors = plugin.get("authors")
    if authors is not None and not (isinstance(authors, list) and all(isinstance(a, str) for a in authors)):
        errs.append("plugin.authors must be a list of strings")
    for k in ("homepage", "source"):
        v = plugin.get(k)
        if isinstance(v, str) and v and not v.startswith(("https://", "http://")):
            errs.append(f"plugin.{k} must be an http(s) URL")
    errs += permission_errors(data.get("permissions", {}))
    return errs


def parse_manifest(raw: "bytes | str") -> dict:
    """Parse and validate; raises :class:`ManifestError`."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ManifestError([f"the manifest is larger than {MAX_MANIFEST_BYTES} bytes"])
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ManifestError(["the manifest is not UTF-8"]) from None
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError([f"the manifest is larger than {MAX_MANIFEST_BYTES} bytes"])
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError([f"TOML: {exc}"]) from None
    errs = validate_manifest(data)
    if errs:
        raise ManifestError(errs)
    data.setdefault("permissions", {})
    return data


def unknown_keys(data: dict) -> list[str]:
    known_plugin = set(STRING_KEYS) | {"authors"}
    out = [f"plugin.{k}" for k in data.get("plugin", {}) if k not in known_plugin]
    out += [f"permissions.{k}" for k in data.get("permissions", {}) if k not in PERMISSION_KEYS]
    out += [k for k in data if k not in ("plugin", "permissions")]
    return out


def manifest_path_for(entry: str) -> str:
    """Where the manifest of the entry point ``module[:attr]`` must be: ``<top>/mlaser-plugin.toml``."""
    top = entry.split(":", 1)[0].split(".", 1)[0]
    return f"{top}/{MANIFEST_NAME}"


def manifest_for_dist(dist: Any, entry: str) -> tuple[Optional[dict], str]:
    """``(manifest, error)`` of an installed distribution, from its file list, without importing
    it.  ``(None, "")``: no manifest (a legacy plugin); ``(None, reason)``: an invalid one."""
    if dist is None:
        return None, ""
    want = manifest_path_for(entry)
    try:
        files = list(dist.files or ())
    except Exception:  # noqa: BLE001 - broken metadata: treated as no manifest
        return None, ""
    hit = next((f for f in files if str(f).replace("\\", "/") == want), None)
    if hit is None:
        return None, ""
    try:
        path = dist.locate_file(hit)
        with open(path, "rb") as fh:
            raw = fh.read(MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        return None, f"{want}: {exc}"
    try:
        return parse_manifest(raw), ""
    except ManifestError as exc:
        return None, f"{want}: {exc}"


def icon_for_dist(dist: Any, entry: str, manifest: Optional[dict]) -> str:
    """The absolute path of the manifest's ``icon`` in the installed distribution ("" when there
    is none or it is not in the distribution's file list); nothing is imported."""
    icon = (manifest or {}).get("plugin", {}).get("icon")
    if not icon or dist is None:
        return ""
    want = manifest_path_for(entry).rsplit("/", 1)[0] + "/" + icon
    try:
        hit = next((f for f in (dist.files or ()) if str(f).replace("\\", "/") == want), None)
        return str(dist.locate_file(hit)) if hit is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def check_identity(manifest: dict, name: str, entry: str, version: str = "") -> list[str]:
    """The manifest must name the entry point it ships with (id = name, entry = value) and the
    distribution's version."""
    p = manifest.get("plugin", {})
    errs = []
    if p.get("id") != name:
        errs.append(f"manifest id {p.get('id')!r} != entry-point name {name!r}")
    if p.get("entry") != entry:
        errs.append(f"manifest entry {p.get('entry')!r} != entry-point value {entry!r}")
    if version:
        try:
            if Version.parse(str(p.get("version"))) != Version.parse(version):
                errs.append(f"manifest version {p.get('version')!r} != distribution version {version!r}")
        except InvalidVersion:
            errs.append(f"invalid version {version!r}")
    return errs


def app_version() -> str:
    import mlaser
    return str(getattr(mlaser, "__version__", "0"))


def compatibility_errors(manifest_or_entry: dict, plugin_api: int, mlaser_version: Optional[str] = None
                         ) -> list[str]:
    """The ``api`` / ``mlaser`` ranges against the running application (a manifest's ``[plugin]``
    table, or an index version entry).  Pre-releases of the application count by their order."""
    p = manifest_or_entry.get("plugin", manifest_or_entry)
    errs = []
    app = mlaser_version or app_version()
    for key, have in (("api", str(plugin_api)), ("mlaser", app)):
        spec = p.get(key)
        if not spec:
            continue
        try:
            if not SpecifierSet(str(spec)).contains(have, prereleases=True):
                errs.append(f"needs {key} {spec}, this is {have}")
        except (InvalidSpecifier, InvalidVersion) as exc:
            errs.append(f"{key}: {exc}")
    return errs


def summary_lines(manifest: dict) -> Iterable[str]:
    p = manifest.get("plugin", {})
    yield f"{p.get('name', p.get('id'))} {p.get('version', '')}"
    for k in ("summary", "authors", "license", "homepage", "source", "api", "mlaser", "min_mode"):
        if p.get(k):
            v = p[k]
            yield f"{k}: {', '.join(v) if isinstance(v, list) else v}"


__all__ = ["MANIFEST_NAME", "ID_RE", "Permissions", "ManifestError", "validate_manifest", "parse_manifest",
           "manifest_for_dist", "check_identity", "compatibility_errors", "normalize_permissions",
           "host_matches", "valid_host_pattern", "unknown_keys", "PERMISSION_KEYS", "BOOL_PERMISSIONS"]
