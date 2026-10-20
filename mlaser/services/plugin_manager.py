"""The plugin manager: repositories, verified installs, updates and removal of plugins.

Normative rules: docs/dev/PLUGIN_STANDARD.md.  In short:

* **Repositories** (``plugins.repositories`` in the ConfigService) are URLs of an index JSON
  (``mlaser-plugin-index``, version 1) or the shortcut ``github:<owner>/<repo>`` (the latest
  release's ``mlaser-plugin-index.json`` asset, else its ``*.whl`` assets with ``SHA256SUMS`` or
  GitHub's asset digests).  An index is untrusted input: size, entry counts and every field are
  checked, invalid entries are dropped with a reason; fetched indexes are cached
  (``<data dir>/plugins/cache``) and the cache is used, marked stale, when the network fails.
* **Install sources**: a plugin id from the repositories (``onshape``, ``onshape==0.2.0``),
  ``github:<owner>/<repo>[@tag]``, an ``https://.../*.whl`` URL (with ``#sha256=<hex>`` or an
  explicit confirmation of the downloaded hash), or a local ``.whl`` file.
* **Verification** before anything is written: the sha256 (and size) of the index entry, a safe
  zip (no absolute or ``..`` paths, links, encryption; file count and unpacked size capped), a
  pure wheel (``Root-Is-Purelib``, ``py3-none-any`` tags, no ``.data`` directory, no binary
  modules, no ``.pth``), every file hashed in ``RECORD``, ``METADATA`` = the file name, exactly
  one ``mlaser.plugins`` entry point, the manifest (valid, = the entry point and version, and
  = the index entry's id / version / entry / permissions / ranges), the ``api`` / ``mlaser``
  ranges, ``Requires-Python``, no top-level module that collides with ``mlaser``, the standard
  library, any importable module or another plugin, and every ``Requires-Dist`` already
  satisfied in the application's environment.  Any failure: :class:`RefusedError` with the
  reason; nothing is written.
* **Install location**: the wheel is unpacked into ``<data dir>/plugins/site/`` (staged, then
  moved; an update keeps a backup until the new files are in place), recorded with its exact
  file list in ``<data dir>/plugins/installed.json``.  At start-up :func:`activate_site` appends
  the site directory to the END of ``sys.path`` (core modules always win) before the entry-point
  discovery, so installed plugins appear as ordinary ``mlaser.plugins`` entry points.
* **Installing never enables.**  Enabling is the Service-mode allowlist (``plugins.allowlist``)
  with the accepted permissions (``plugins.accepted``); an update keeps a plugin enabled only
  when its entry point and permissions are unchanged, otherwise it is disabled and must be
  enabled again.  Uninstall removes exactly the recorded files and the plugin's allowlist,
  authority and accepted entries.  Every change is an audit record (``plugin_install``,
  ``plugin_update``, ``plugin_uninstall``, ``plugin_refused``, ``plugin_repo_add``,
  ``plugin_repo_remove``, ``plugin_allowlist``).  Changes apply at the next start.

Qt-free; the GUI (``mlaser.ui.plugin_manager``) and the CLI (``mlaser plugin ...``) use it.
"""
from __future__ import annotations

import base64
import configparser
import csv
import hashlib
import importlib
import importlib.metadata
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import stat
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from email.parser import BytesHeaderParser
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .pep440 import (InvalidRequirement, InvalidSpecifier, InvalidVersion, Requirement, SpecifierSet, Version,
                     canonical_name)
from .plugin_http import HostNotAllowedError, HttpClient, InsecureURLError, check_url, is_local_host
from .plugin_manifest import (ID_RE, MANIFEST_NAME, ManifestError, Permissions, app_version, check_identity,
                              compatibility_errors, manifest_path_for, normalize_permissions, parse_manifest,
                              permission_errors)

log = logging.getLogger("mlaser.plugins.manager")

# ----------------------------------------------------------------------------- constants
INDEX_FORMAT = "mlaser-plugin-index"
INDEX_VERSION = 1
INDEX_ASSET = "mlaser-plugin-index.json"
SUMS_ASSET = "SHA256SUMS"
INSTALLED_FORMAT = "mlaser-plugins-installed"
REPOSITORIES_KEY = "plugins.repositories"
GITHUB_API = "https://api.github.com"
#: hosts a download from github.com / api.github.com may be redirected to (release assets)
GITHUB_HOSTS = ("github.com", "api.github.com")
GITHUB_ASSET_HOSTS = ("objects.githubusercontent.com", "release-assets.githubusercontent.com",
                      "github-releases.githubusercontent.com", "codeload.github.com", "github.com")
GITHUB_RE = re.compile(r"^github:(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]{1,100})"
                       r"(?:@(?P<tag>[A-Za-z0-9._/+-]{1,100}))?$")
WHEEL_RE = re.compile(r"^(?P<dist>[A-Za-z0-9](?:[A-Za-z0-9._]*[A-Za-z0-9])?)-(?P<ver>[^-]+)"
                      r"(?:-(?P<build>\d[^-]*))?-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

#: limits on untrusted input (docs/dev/PLUGIN_STANDARD.md §8)
MAX_INDEX_BYTES = 5_000_000
MAX_RELEASE_BYTES = 2_000_000
MAX_SUMS_BYTES = 1_000_000
MAX_WHEEL_BYTES = 100_000_000
MAX_WHEEL_FILES = 5000
MAX_UNPACKED_BYTES = 300_000_000
MAX_PLUGINS = 1000
MAX_VERSIONS = 200
MAX_REQUIRES = 100
MAX_STRING = 20_000
CACHE_SECONDS = 6 * 3600
LOCK_STALE_SECONDS = 600
BINARY_SUFFIXES = (".so", ".pyd", ".dll", ".dylib", ".exe", ".bin", ".a", ".lib")
FORBIDDEN_TOP = ("sitecustomize", "usercustomize", "__init__", "__main__")


# ----------------------------------------------------------------------------- errors
class PluginError(Exception):
    """A plugin-manager failure with a stable ``code`` (CLI JSON) and details."""

    code = "failed"

    def __init__(self, message: str, code: str = "", **detail: Any) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.detail = detail


class RefusedError(PluginError):
    """Verification or policy refused the install / update (nothing was written)."""

    code = "refused"


class NotFoundError(PluginError):
    code = "not_found"


class FetchError(PluginError):
    """A repository or download could not be fetched (network, HTTP status)."""

    code = "fetch"


# ----------------------------------------------------------------------------- paths
def plugins_root() -> Path:
    from .config import data_dir
    return data_dir() / "plugins"


def site_dir() -> Path:
    return plugins_root() / "site"


def installed_path() -> Path:
    return plugins_root() / "installed.json"


def cache_dir() -> Path:
    return plugins_root() / "cache"


def plugin_data_dir(plugin_id: str) -> Path:
    if not ID_RE.match(plugin_id or "") and not re.match(r"^[A-Za-z0-9_]{1,64}$", plugin_id or ""):
        raise ValueError(f"invalid plugin id {plugin_id!r}")
    return plugins_root() / "data" / plugin_id


def activate_site(path: Optional[os.PathLike | str] = None) -> Optional[str]:
    """Append the plugin site directory to the END of ``sys.path`` (once; only when it exists),
    so the entry-point discovery finds what the manager installed and core modules always win.
    Works in the PyInstaller bundle too (its path finder and ``importlib.metadata`` scan
    ``sys.path`` like a normal interpreter).  Returns the directory added or already present."""
    d = Path(path) if path is not None else site_dir()
    if not d.is_dir():
        return None
    s = str(d.resolve())
    for stale in d.glob(".staging-*"):
        shutil.rmtree(stale, ignore_errors=True)
    if s not in sys.path:
        sys.path.append(s)
        importlib.invalidate_caches()
    return s


# ----------------------------------------------------------------------------- index model
@dataclass
class IndexVersion:
    version: str
    url: str
    sha256: str
    size: int = 0
    api: str = ""
    mlaser: str = ""
    requires: list[str] = field(default_factory=list)
    permissions: dict = field(default_factory=dict)
    published: str = ""
    entry: str = ""
    synthetic: bool = False          # from a GitHub release without an index: no manifest data

    @property
    def filename(self) -> str:
        return urllib.parse.unquote(urllib.parse.urlsplit(self.url).path.rsplit("/", 1)[-1])

    def compatible(self, plugin_api: int) -> list[str]:
        return compatibility_errors({"api": self.api, "mlaser": self.mlaser}, plugin_api)


@dataclass
class IndexPlugin:
    id: str
    name: str = ""
    summary: str = ""
    description: str = ""
    homepage: str = ""
    source: str = ""
    authors: list[str] = field(default_factory=list)
    license: str = ""
    versions: list[IndexVersion] = field(default_factory=list)
    repository: str = ""

    def best(self, plugin_api: int, version: str = "", pre: bool = False) -> Optional[IndexVersion]:
        """The newest compatible version (``version``: that one; pre-releases only when asked
        for or named)."""
        cands = []
        for v in self.versions:
            try:
                pv = Version.parse(v.version)
            except InvalidVersion:
                continue
            if version:
                if pv == Version.parse(version):
                    return v
                continue
            if pv.is_prerelease and not pre:
                continue
            if v.synthetic or not v.compatible(plugin_api):
                cands.append((pv, v))
        return max(cands, key=lambda t: t[0])[1] if cands else None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Index:
    url: str
    name: str = ""
    homepage: str = ""
    plugins: list[IndexPlugin] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    fetched: float = 0.0
    stale: bool = False
    repository: str = ""


def _s(v: Any, n: int = 2000) -> str:
    return v[:n] if isinstance(v, str) else ""


def validate_index(data: Any) -> list[str]:
    """Strict check of an index document (``tools/plugin_index.py check``): every problem."""
    try:
        idx = parse_index(data, "https://example.invalid/mlaser-plugin-index.json")
    except PluginError as exc:
        return [str(exc)]
    return idx.problems


def parse_index(data: Any, base_url: str, repository: str = "") -> Index:
    """An :class:`Index` from untrusted JSON: the document must be an ``mlaser-plugin-index``
    version 1; invalid plugins / versions are dropped and listed in ``problems``."""
    if not isinstance(data, dict):
        raise PluginError("the index is not a JSON object", "index")
    if data.get("format") != INDEX_FORMAT:
        raise PluginError(f"not an mlaser plugin index (format {data.get('format')!r})", "index")
    if data.get("version") != INDEX_VERSION:
        raise PluginError(f"unsupported index version {data.get('version')!r} (this is {INDEX_VERSION})", "index")
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        raise PluginError("index: 'plugins' must be a list", "index")
    idx = Index(base_url, _s(data.get("name"), 200), _s(data.get("homepage"), 2000), repository=repository)
    if len(plugins) > MAX_PLUGINS:
        idx.problems.append(f"index: more than {MAX_PLUGINS} plugins; the rest is ignored")
    seen: set[str] = set()
    for i, p in enumerate(plugins[:MAX_PLUGINS]):
        where = f"plugins[{i}]"
        if not isinstance(p, dict):
            idx.problems.append(f"{where}: not an object")
            continue
        pid = p.get("id")
        if not isinstance(pid, str) or not ID_RE.match(pid):
            idx.problems.append(f"{where}: invalid id {pid!r}")
            continue
        if pid in seen:
            idx.problems.append(f"{where}: duplicate id {pid!r}")
            continue
        seen.add(pid)
        authors = p.get("authors", [])
        ip = IndexPlugin(pid, _s(p.get("name"), 80) or pid, _s(p.get("summary"), 200),
                         _s(p.get("description"), MAX_STRING), _https(p.get("homepage")), _https(p.get("source")),
                         [a[:200] for a in authors[:20] if isinstance(a, str)] if isinstance(authors, list) else [],
                         _s(p.get("license"), 100), repository=repository)
        vers = p.get("versions")
        if not isinstance(vers, list) or not vers:
            idx.problems.append(f"{pid}: no versions")
            continue
        for j, v in enumerate(vers[:MAX_VERSIONS]):
            iv, why = _parse_version_entry(v, base_url)
            if iv is None:
                idx.problems.append(f"{pid} versions[{j}]: {why}")
            else:
                ip.versions.append(iv)
        if ip.versions:
            idx.plugins.append(ip)
    return idx


def _https(v: Any) -> str:
    return v[:2000] if isinstance(v, str) and v.startswith(("https://", "http://")) else ""


def _parse_version_entry(v: Any, base_url: str) -> tuple[Optional[IndexVersion], str]:
    if not isinstance(v, dict):
        return None, "not an object"
    ver = v.get("version")
    try:
        Version.parse(ver if isinstance(ver, str) else "")
    except InvalidVersion:
        return None, f"invalid version {ver!r}"
    url = v.get("url")
    if not isinstance(url, str) or not url or len(url) > 4000:
        return None, "missing url"
    url = urllib.parse.urljoin(base_url, url)
    if not urllib.parse.urlsplit(url).path.endswith(".whl"):
        return None, "url is not a .whl"
    sha = v.get("sha256")
    if not isinstance(sha, str) or not SHA_RE.match(sha.lower()):
        return None, "sha256 must be 64 hex digits"
    size = v.get("size", 0)
    if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > MAX_WHEEL_BYTES:
        return None, f"invalid size {size!r}"
    for k in ("api", "mlaser"):
        if k in v:
            if not isinstance(v[k], str):
                return None, f"{k} must be a string"
            try:
                SpecifierSet(v[k])
            except InvalidSpecifier:
                return None, f"invalid {k} specifier {v[k]!r}"
    req = v.get("requires", [])
    if not isinstance(req, list) or len(req) > MAX_REQUIRES or not all(isinstance(r, str) for r in req):
        return None, "requires must be a list of strings"
    for r in req:
        try:
            Requirement.parse(r)
        except InvalidRequirement:
            return None, f"invalid requirement {r!r}"
    perms = v.get("permissions", {})
    errs = permission_errors(perms)
    if errs:
        return None, "; ".join(errs)
    entry = v.get("entry", "")
    if not isinstance(entry, str):
        return None, "entry must be a string"
    return IndexVersion(str(ver), url, sha.lower(), size, v.get("api", ""), v.get("mlaser", ""), list(req),
                        dict(perms), _s(v.get("published"), 40), entry), ""


# ----------------------------------------------------------------------------- wheels
@dataclass
class WheelInfo:
    filename: str
    dist: str
    version: str
    sha256: str
    size: int
    entry_name: str
    entry_value: str
    manifest: dict
    requires: list[str]
    requires_python: str
    top_level: list[str]
    files: list[str]
    dist_info: str

    @property
    def id(self) -> str:
        return self.entry_name

    @property
    def permissions(self) -> dict:
        return normalize_permissions(self.manifest.get("permissions", {}))

    def summary(self) -> dict:
        p = self.manifest.get("plugin", {})
        return {"id": self.id, "name": p.get("name", self.id), "version": self.version, "dist": self.dist,
                "entry": self.entry_value, "sha256": self.sha256, "size": self.size,
                "permissions": self.permissions, "api": p.get("api", ""), "mlaser": p.get("mlaser", ""),
                "requires": self.requires, "summary": p.get("summary", ""), "homepage": p.get("homepage", ""),
                "source": p.get("source", ""), "license": p.get("license", ""),
                "authors": p.get("authors", []), "min_mode": p.get("min_mode", "")}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(name: str) -> bool:
    if not name or len(name) > 400 or "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    parts = name.rstrip("/").split("/")
    return all(p not in ("", ".", "..") and ":" not in p for p in parts)


def _record_digest(data: bytes, spec: str) -> Optional[bool]:
    algo, _, want = spec.partition("=")
    if algo not in ("sha256", "sha384", "sha512") or not want:
        return None
    got = base64.urlsafe_b64encode(hashlib.new(algo, data).digest()).rstrip(b"=").decode("ascii")
    return got == want.rstrip("=")


def _pure_tag(tag: str) -> bool:
    py, _, rest = tag.partition("-")
    abi, _, plat = rest.partition("-")
    return abi == "none" and plat == "any" and all(re.match(r"^py3\d*$", t) or t == "py2" for t in py.split("."))\
        and any(t.startswith("py3") for t in py.split("."))


def inspect_wheel(data: bytes, filename: str) -> WheelInfo:
    """Check a wheel's structure without installing it (see the module docstring); raises
    :class:`RefusedError` with the first problem."""
    filename = os.path.basename(filename)
    if len(data) > MAX_WHEEL_BYTES:
        raise RefusedError(f"{filename}: larger than {MAX_WHEEL_BYTES} bytes", "size")
    m = WHEEL_RE.match(filename)
    if not m:
        raise RefusedError(f"{filename}: not a wheel file name", "layout")
    for t in (f"{m.group('py')}-{m.group('abi')}-{m.group('plat')}",):
        if not _pure_tag(t):
            raise RefusedError(f"{filename}: not a pure-Python wheel (tag {t}; py3-none-any required)", "not_pure")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise RefusedError(f"{filename}: not a zip archive ({exc})", "layout") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_WHEEL_FILES:
            raise RefusedError(f"{filename}: more than {MAX_WHEEL_FILES} files", "size")
        total = 0
        names: list[str] = []
        lower: set[str] = set()
        for zi in infos:
            if not _safe_name(zi.filename):
                raise RefusedError(f"{filename}: unsafe path {zi.filename!r}", "layout")
            if zi.flag_bits & 0x1:
                raise RefusedError(f"{filename}: encrypted entry {zi.filename!r}", "layout")
            mode = (zi.external_attr >> 16) & 0o170000
            if mode and mode not in (stat.S_IFREG, stat.S_IFDIR):
                raise RefusedError(f"{filename}: {zi.filename!r} is a link or device", "layout")
            if zi.is_dir():
                continue
            if zi.filename.lower() in lower:
                raise RefusedError(f"{filename}: duplicate entry {zi.filename!r}", "layout")
            lower.add(zi.filename.lower())
            total += zi.file_size
            if total > MAX_UNPACKED_BYTES:
                raise RefusedError(f"{filename}: unpacks to more than {MAX_UNPACKED_BYTES} bytes", "size")
            names.append(zi.filename)
        di_dirs = sorted({n.split("/", 1)[0] for n in names if n.split("/", 1)[0].endswith(".dist-info")})
        if len(di_dirs) != 1:
            raise RefusedError(f"{filename}: needs exactly one .dist-info directory, found {len(di_dirs)}", "layout")
        di = di_dirs[0]
        di_name, _, di_ver = di[: -len(".dist-info")].rpartition("-")
        if canonical_name(di_name) != canonical_name(m.group("dist")) or di_ver != m.group("ver"):
            raise RefusedError(f"{filename}: {di} does not match the file name", "layout")

        def read(name: str, limit: int = 2_000_000) -> bytes:
            try:
                with zf.open(name) as fh:
                    raw = fh.read(limit + 1)
            except KeyError:
                raise RefusedError(f"{filename}: {name} is missing", "layout") from None
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:   # CRC error
                raise RefusedError(f"{filename}: {name}: {exc}", "layout") from None
            if len(raw) > limit:
                raise RefusedError(f"{filename}: {name} is too large", "size")
            return raw

        # WHEEL: pure
        wheel_meta = BytesHeaderParser().parsebytes(read(f"{di}/WHEEL", 100_000))
        if str(wheel_meta.get("Root-Is-Purelib", "")).strip().lower() != "true":
            raise RefusedError(f"{filename}: Root-Is-Purelib is not true (not a pure wheel)", "not_pure")
        for tag in wheel_meta.get_all("Tag") or []:
            if not _pure_tag(str(tag).strip()):
                raise RefusedError(f"{filename}: WHEEL tag {tag} is not pure (py3-none-any)", "not_pure")
        for n in names:
            top = n.split("/", 1)[0]
            if top.endswith(".data"):
                raise RefusedError(f"{filename}: a .data directory ({top}) is not allowed", "not_pure")
            if n.lower().endswith(BINARY_SUFFIXES):
                raise RefusedError(f"{filename}: binary file {n!r} (pure Python only)", "not_pure")
            if n.endswith(".pth"):
                raise RefusedError(f"{filename}: .pth files are not allowed ({n})", "layout")
        # RECORD: every file hashed
        record_name = f"{di}/RECORD"
        rec_raw = read(record_name, 5_000_000).decode("utf-8", errors="replace")
        rows = list(csv.reader(io.StringIO(rec_raw)))
        recorded: dict[str, tuple[str, str]] = {}
        for row in rows:
            if not row:
                continue
            path = row[0]
            recorded[path] = (row[1] if len(row) > 1 else "", row[2] if len(row) > 2 else "")
        unsigned = {record_name, f"{di}/RECORD.jws", f"{di}/RECORD.p7s"}
        for n in names:
            if n in unsigned:
                continue
            if n not in recorded:
                raise RefusedError(f"{filename}: {n} is not listed in RECORD", "record")
            spec, size = recorded[n]
            content = read(n, MAX_UNPACKED_BYTES)
            ok = _record_digest(content, spec)
            if ok is None:
                raise RefusedError(f"{filename}: {n} has no sha256 hash in RECORD", "record")
            if not ok:
                raise RefusedError(f"{filename}: RECORD hash mismatch for {n}", "record")
            if size and size.isdigit() and int(size) != len(content):
                raise RefusedError(f"{filename}: RECORD size mismatch for {n}", "record")
        for n in recorded:
            if n not in names and n not in unsigned and not n.endswith("/"):
                raise RefusedError(f"{filename}: RECORD lists {n!r}, which is not in the wheel", "record")
        # METADATA
        meta = BytesHeaderParser().parsebytes(read(f"{di}/METADATA", 2_000_000))
        name, version = str(meta.get("Name", "")).strip(), str(meta.get("Version", "")).strip()
        if canonical_name(name) != canonical_name(m.group("dist")):
            raise RefusedError(f"{filename}: METADATA Name {name!r} does not match", "layout")
        try:
            if Version.parse(version) != Version.parse(m.group("ver")):
                raise RefusedError(f"{filename}: METADATA Version {version!r} does not match", "layout")
        except InvalidVersion:
            raise RefusedError(f"{filename}: invalid version {version!r}", "layout") from None
        requires = [str(r).strip() for r in (meta.get_all("Requires-Dist") or [])]
        requires_python = str(meta.get("Requires-Python", "") or "").strip()
        # entry point
        eps_raw = read(f"{di}/entry_points.txt", 200_000) if f"{di}/entry_points.txt" in names else b""
        cp = configparser.ConfigParser(interpolation=None, delimiters=("=",))
        cp.optionxform = str  # type: ignore[assignment,method-assign]
        try:
            cp.read_string(eps_raw.decode("utf-8", errors="replace"))
        except configparser.Error as exc:
            raise RefusedError(f"{filename}: entry_points.txt: {exc}", "layout") from None
        if not cp.has_section("mlaser.plugins"):
            raise RefusedError(f"{filename}: no mlaser.plugins entry point (not an mlaser plugin)", "layout")
        eps = [(k.strip(), v.strip()) for k, v in cp.items("mlaser.plugins")]
        if len(eps) != 1:
            raise RefusedError(f"{filename}: exactly one mlaser.plugins entry point is required, found {len(eps)}",
                               "layout")
        ep_name, ep_value = eps[0]
        if not ID_RE.match(ep_name):
            raise RefusedError(f"{filename}: entry-point name {ep_name!r} is not a valid plugin id", "layout")
        # top-level names
        tops: set[str] = set()
        for n in names:
            first = n.split("/", 1)[0]
            if first == di:
                continue
            mod = first[:-3] if "/" not in n and first.endswith(".py") else first
            if "/" not in n and not first.endswith(".py"):
                raise RefusedError(f"{filename}: top-level file {first!r} is not a module", "layout")
            if not mod.isidentifier():
                raise RefusedError(f"{filename}: top-level name {mod!r} is not importable", "layout")
            if mod in FORBIDDEN_TOP:
                raise RefusedError(f"{filename}: top-level module {mod!r} is not allowed", "collision")
            tops.add(mod)
        # manifest
        mpath = manifest_path_for(ep_value)
        if mpath not in names:
            raise RefusedError(f"{filename}: {mpath} is missing (the manifest, PLUGIN_STANDARD.md §2)", "manifest")
        try:
            manifest = parse_manifest(read(mpath, 70_000))
        except ManifestError as exc:
            raise RefusedError(f"{filename}: invalid manifest: {exc}", "manifest", errors=exc.errors) from None
        bad = check_identity(manifest, ep_name, ep_value, version)
        if bad:
            raise RefusedError(f"{filename}: manifest mismatch: {'; '.join(bad)}", "manifest_mismatch")
        icon = manifest.get("plugin", {}).get("icon")
        if icon and f"{mpath.rsplit('/', 1)[0]}/{icon}" not in names:
            raise RefusedError(f"{filename}: the manifest icon {icon!r} is not in the package", "manifest")
    return WheelInfo(filename, name, version, sha256_hex(data), len(data), ep_name, ep_value, manifest, requires,
                     requires_python, sorted(tops), names, di)


def compare_with_index(info: WheelInfo, entry: IndexVersion, plugin_id: str = "") -> list[str]:
    """The manifest must equal the index entry (id, version, entry, permissions, ranges)."""
    p = info.manifest.get("plugin", {})
    errs = []
    if plugin_id and info.id != plugin_id:
        errs.append(f"id {info.id!r} != index id {plugin_id!r}")
    if Version.parse(info.version) != Version.parse(entry.version):
        errs.append(f"version {info.version} != index version {entry.version}")
    if entry.synthetic:
        return errs
    if entry.entry and entry.entry != info.entry_value:
        errs.append(f"entry {info.entry_value!r} != index entry {entry.entry!r}")
    try:
        if normalize_permissions(entry.permissions) != info.permissions:
            errs.append(f"permissions {info.permissions} != index permissions "
                        f"{normalize_permissions(entry.permissions)}")
    except ManifestError as exc:
        errs.append(f"index permissions: {exc}")
    for k in ("api", "mlaser"):
        iv = getattr(entry, k)
        if iv and str(SpecifierSet(iv)) != str(SpecifierSet(str(p.get(k, "")))):
            errs.append(f"{k} {p.get(k)!r} != index {k} {iv!r}")
    return errs


@dataclass
class Prepared:
    """A verified wheel, ready for :meth:`PluginManager.commit`."""

    info: WheelInfo
    data: bytes
    origin: dict
    replacing: Optional[str] = None
    verified_hash: bool = True      # False: a wheel URL whose hash the user confirms


# ----------------------------------------------------------------------------- environment
def _dist_version(name: str, extra_paths: Iterable[str] = ()) -> Optional[str]:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        pass
    for d in importlib.metadata.distributions(path=[str(p) for p in extra_paths]):
        if canonical_name(d.metadata.get("Name", "") or "") == canonical_name(name):
            return str(d.version)
    return None


def missing_dependencies(requires: Iterable[str], extra_paths: Iterable[str] = ()) -> list[str]:
    """The ``Requires-Dist`` entries the application's environment does not satisfy (markers
    evaluated, no extra requested).  ``mlaser`` is checked against the running application.  In
    the frozen bundle, where bundled libraries may have no metadata, an importable top-level
    module of the same name counts (its version cannot be checked)."""
    out = []
    for text in requires:
        try:
            req = Requirement.parse(text)
            if not req.applies():
                continue
        except InvalidRequirement:
            out.append(f"{text} (unreadable)")
            continue
        if req.url:
            out.append(f"{text} (direct references are not supported)")
            continue
        if req.key == "mlaser":
            have: Optional[str] = app_version()
        else:
            have = _dist_version(req.name, extra_paths)
            if have is None and getattr(sys, "frozen", False):
                mod = req.name.replace("-", "_").lower()
                try:
                    if importlib.util.find_spec(mod) is not None:
                        continue
                except (ImportError, ValueError):
                    pass
        if have is None:
            out.append(text)
            continue
        try:
            if req.specifier and not req.specifier.contains(have, prereleases=True):
                out.append(f"{text} (installed: {have})")
        except InvalidVersion:
            out.append(f"{text} (installed: {have})")
    return out


def stdlib_names() -> frozenset[str]:
    return frozenset(getattr(sys, "stdlib_module_names", ())) | frozenset(sys.builtin_module_names)


def _top_level_origin(name: str) -> Optional[str]:
    """Where an importable top-level module comes from (None: not importable)."""
    if name in sys.modules:
        mod = sys.modules[name]
        return str(getattr(mod, "__file__", None) or "<builtin>")
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    if spec.origin:
        return str(spec.origin)
    locs = list(spec.submodule_search_locations or [])
    return locs[0] if locs else "<namespace>"


# ----------------------------------------------------------------------------- manager
class _Lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: Optional[int] = None

    def __enter__(self) -> "_Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS:
                self.path.unlink()
        except OSError:
            pass
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise PluginError("another plugin install or removal is running (plugins/.lock)", "busy") from None
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except OSError:
            pass


def _audit(event: str, **fields: Any) -> None:
    try:
        from mlaser.machine.audit import audit
        audit(event, **fields)
    except Exception:  # noqa: BLE001 - best effort
        log.info("%s %s", event, fields)


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


class PluginManager:
    """See the module docstring.  ``config``: the ConfigService (repositories, allowlist);
    ``root``: the plugins directory (default ``<data dir>/plugins``); ``reserved``: plugin names
    that are taken (the built-in extensions, from the GUI); ``opener``: a urllib opener (tests)."""

    def __init__(self, config: Any = None, root: Optional[os.PathLike | str] = None,
                 reserved: Iterable[str] = (), opener: Any = None, plugin_api: Optional[int] = None,
                 github_api: Optional[str] = None, sleep: Callable[[float], None] = time.sleep) -> None:
        if config is None:
            from .config import ConfigService
            config = ConfigService()
        self.config = config
        self.root = Path(root) if root is not None else plugins_root()
        self.reserved = set(reserved)
        self.opener = opener
        self.sleep = sleep
        if plugin_api is None:
            from .plugins import PLUGIN_API
            plugin_api = PLUGIN_API
        self.plugin_api = int(plugin_api)
        self.github_api = (github_api or os.environ.get("MLASER_GITHUB_API") or GITHUB_API).rstrip("/")
        self.errors: list[str] = []          # repository problems of the last indexes() call
        self.changed = False                 # something changed this session: restart to apply

    # ------------------------------------------------------------------ paths
    @property
    def site(self) -> Path:
        return self.root / "site"

    @property
    def installed_file(self) -> Path:
        return self.root / "installed.json"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    def data_dir(self, plugin_id: str) -> Path:
        return self.root / "data" / plugin_id

    # ------------------------------------------------------------------ network
    def _client(self, url: str) -> HttpClient:
        first = (urllib.parse.urlsplit(url).hostname or "").lower()

        def redirect_ok(frm: str, to: str) -> bool:
            return to == frm or (frm in GITHUB_HOSTS + GITHUB_ASSET_HOSTS and to in GITHUB_ASSET_HOSTS + GITHUB_HOSTS)

        return HttpClient(lambda h: h == first, redirect_ok=redirect_ok, opener=self.opener, sleep=self.sleep)

    def fetch(self, url: str, max_bytes: int, headers: Optional[dict] = None) -> tuple[bytes, str]:
        """Download ``url`` (https; http and file only for this computer) with the manager's
        redirect rule (same host, or GitHub's asset hosts); returns (content, final URL)."""
        parts = urllib.parse.urlsplit(url)
        if parts.scheme == "file":
            if parts.netloc not in ("", "localhost"):
                raise RefusedError(f"file URL on another host: {url}", "insecure_url")
            path = Path(urllib.request.url2pathname(parts.path))
            try:
                if path.stat().st_size > max_bytes:
                    raise RefusedError(f"{path}: larger than {max_bytes} bytes", "size")
                return path.read_bytes(), url
            except OSError as exc:
                raise NotFoundError(f"{path}: {exc}") from None
        try:
            check_url(url)
        except InsecureURLError as exc:
            raise RefusedError(str(exc), "insecure_url") from None
        except ValueError as exc:
            raise PluginError(str(exc), "usage") from None
        try:
            resp = self._client(url).get(url, max_bytes=max_bytes, headers=headers, timeout=30)
        except HostNotAllowedError as exc:
            raise RefusedError(str(exc), "redirect") from None
        except InsecureURLError as exc:
            raise RefusedError(str(exc), "insecure_url") from None
        except ValueError as exc:
            raise RefusedError(str(exc), "size") from None
        except (ConnectionError, OSError) as exc:
            raise FetchError(str(exc)) from None
        if resp.status == 404:
            raise NotFoundError(f"HTTP 404 from {_short_url(url)}")
        if not resp.ok:
            raise FetchError(f"HTTP {resp.status} from {_short_url(url)}")
        return resp.content, resp.url

    def _fetch_json(self, url: str, max_bytes: int, headers: Optional[dict] = None) -> tuple[Any, str]:
        raw, final = self.fetch(url, max_bytes, headers)
        try:
            return json.loads(raw.decode("utf-8-sig")), final
        except (ValueError, RecursionError) as exc:
            raise PluginError(f"{_short_url(url)}: not valid JSON ({exc})", "index") from None

    # ------------------------------------------------------------------ repositories
    def repositories(self) -> list[dict]:
        out = []
        for r in self.config.get(REPOSITORIES_KEY, []) or []:
            if isinstance(r, dict) and isinstance(r.get("url"), str):
                out.append({"url": r["url"], "name": str(r.get("name", "") or "")})
            elif isinstance(r, str):
                out.append({"url": r, "name": ""})
        return out

    @staticmethod
    def normalize_repository(url: str) -> str:
        url = (url or "").strip()
        if url.startswith("https://github.com/") and not url.endswith(".json"):
            parts = url[len("https://github.com/"):].strip("/").split("/")
            if len(parts) == 2:
                url = f"github:{parts[0]}/{parts[1]}"
        m = GITHUB_RE.match(url)
        if url.startswith("github:"):
            if not m:
                raise PluginError(f"{url!r}: use github:<owner>/<repo>", "usage")
            return url
        if urllib.parse.urlsplit(url).scheme == "file":
            return url
        try:
            check_url(url)
        except InsecureURLError as exc:
            raise RefusedError(str(exc), "insecure_url") from None
        except ValueError as exc:
            raise PluginError(str(exc), "usage") from None
        return url

    def add_repository(self, url: str, name: str = "") -> dict:
        url = self.normalize_repository(url)
        repos = self.repositories()
        if any(r["url"] == url for r in repos):
            return {"url": url, "name": name, "added": False}
        repos.append({"url": url, "name": name[:80]})
        self._save_config(REPOSITORIES_KEY, repos)
        _audit("plugin_repo_add", url=url, name=name)
        return {"url": url, "name": name, "added": True}

    def remove_repository(self, url: str) -> dict:
        repos = self.repositories()
        keep = [r for r in repos if r["url"] != url.strip()]
        if len(keep) == len(repos):
            raise NotFoundError(f"no repository {url!r}")
        self._save_config(REPOSITORIES_KEY, keep)
        _audit("plugin_repo_remove", url=url)
        return {"url": url, "removed": True}

    def _save_config(self, key: str, value: Any) -> None:
        self.config.set(key, value)
        try:
            self.config.save()
        except OSError as exc:
            raise PluginError(f"the configuration could not be saved ({exc})", "output") from None

    # ------------------------------------------------------------------ indexes
    def _cache_file(self, url: str) -> Path:
        return self.cache / f"index-{hashlib.sha256(url.encode()).hexdigest()[:20]}.json"

    def fetch_index(self, repository: str, refresh: bool = False, max_age: float = CACHE_SECONDS) -> Index:
        """The repository's index (cached; the cache, marked stale, when the network fails)."""
        cf = self._cache_file(repository)
        cached = None
        try:
            cached = json.loads(cf.read_text("utf-8"))
        except (OSError, ValueError):
            cached = None
        if cached and not refresh and time.time() - float(cached.get("fetched", 0)) < max_age:
            return self._from_cache(cached, repository, stale=False)
        try:
            data, base = self._resolve_index(repository)
        except FetchError:
            if cached:
                log.warning("plugin repository %s unreachable; using the cached index", _short_url(repository))
                return self._from_cache(cached, repository, stale=True)
            raise
        idx = parse_index(data, base, repository)
        idx.fetched = time.time()
        try:
            _atomic_write_json(cf, {"url": repository, "base": base, "fetched": idx.fetched, "data": data})
        except OSError:
            pass
        return idx

    def _from_cache(self, cached: dict, repository: str, stale: bool) -> Index:
        idx = parse_index(cached.get("data"), str(cached.get("base") or repository), repository)
        idx.fetched = float(cached.get("fetched", 0))
        idx.stale = stale
        return idx

    def _resolve_index(self, repository: str) -> tuple[Any, str]:
        m = GITHUB_RE.match(repository)
        if m:
            return self._github_index(m.group("owner"), m.group("repo"), m.group("tag"))
        return self._fetch_json(repository, MAX_INDEX_BYTES)

    def _github_index(self, owner: str, repo: str, tag: Optional[str] = None) -> tuple[Any, str]:
        """``github:<owner>/<repo>``: the release's index asset, else its wheels with their sha256
        from ``SHA256SUMS`` or the assets' ``digest``."""
        api = f"{self.github_api}/repos/{owner}/{repo}/releases/" + (f"tags/{urllib.parse.quote(tag)}" if tag
                                                                     else "latest")
        rel, _ = self._fetch_json(api, MAX_RELEASE_BYTES, {"Accept": "application/vnd.github+json"})
        assets = rel.get("assets") if isinstance(rel, dict) else None
        if not isinstance(assets, list):
            raise PluginError(f"github:{owner}/{repo}: no release assets", "index")
        by_name: dict[str, dict] = {str(a["name"]): a for a in assets
                                    if isinstance(a, dict) and isinstance(a.get("name"), str)}
        idx_asset = by_name.get(INDEX_ASSET)
        if idx_asset and isinstance(idx_asset.get("browser_download_url"), str):
            return self._fetch_json(idx_asset["browser_download_url"], MAX_INDEX_BYTES)
        sums: dict[str, str] = {}
        sums_asset = by_name.get(SUMS_ASSET)
        if sums_asset and isinstance(sums_asset.get("browser_download_url"), str):
            raw, _ = self.fetch(sums_asset["browser_download_url"], MAX_SUMS_BYTES)
            sums = parse_sha256sums(raw.decode("utf-8", errors="replace"))
        plugins: dict[str, dict] = {}
        for name, a in sorted(by_name.items()):
            if not name.endswith(".whl"):
                continue
            wm = WHEEL_RE.match(name)
            url = a.get("browser_download_url")
            if not wm or not isinstance(url, str):
                continue
            digest = sums.get(name) or ""
            d = a.get("digest")
            if not digest and isinstance(d, str) and d.startswith("sha256:"):
                digest = d[len("sha256:"):]
            if not SHA_RE.match(digest.lower()):
                log.warning("github:%s/%s: %s has no sha256 (SHA256SUMS or digest); skipped", owner, repo, name)
                continue
            key = re.sub(r"[^a-z0-9_]", "_", canonical_name(wm.group("dist")))[:32]
            key = key if ID_RE.match(key) else "plugin_" + key[:24]
            p = plugins.setdefault(key, {"id": key, "name": wm.group("dist"), "versions": [],
                                         "source": f"https://github.com/{owner}/{repo}"})
            size = a.get("size") if isinstance(a.get("size"), int) else 0
            p["versions"].append({"version": wm.group("ver"), "url": url, "sha256": digest.lower(), "size": size,
                                  "published": _s(rel.get("published_at"), 10)})
        data = {"format": INDEX_FORMAT, "version": INDEX_VERSION, "name": f"github:{owner}/{repo}",
                "homepage": f"https://github.com/{owner}/{repo}", "plugins": list(plugins.values()),
                "synthetic": True}
        return data, api

    def indexes(self, refresh: bool = False) -> list[Index]:
        out = []
        self.errors = []
        for r in self.repositories():
            try:
                idx = self.fetch_index(r["url"], refresh=refresh)
            except PluginError as exc:
                self.errors.append(f"{r['url']}: {exc}")
                continue
            if self._is_synthetic(idx):
                for p in idx.plugins:
                    for v in p.versions:
                        v.synthetic = True
            out.append(idx)
            self.errors += [f"{r['url']}: {p}" for p in idx.problems]
        return out

    def _is_synthetic(self, idx: Index) -> bool:
        try:
            cached = json.loads(self._cache_file(idx.repository).read_text("utf-8"))
            return bool((cached.get("data") or {}).get("synthetic"))
        except (OSError, ValueError, AttributeError):
            return False

    def available(self, refresh: bool = False) -> list[IndexPlugin]:
        """Every plugin of every repository (the first repository listing an id wins)."""
        seen: dict[str, IndexPlugin] = {}
        for idx in self.indexes(refresh):
            for p in idx.plugins:
                seen.setdefault(p.id, p)
        return list(seen.values())

    def search(self, query: str = "", refresh: bool = False) -> list[IndexPlugin]:
        terms = [t.lower() for t in (query or "").split()]
        out = []
        for p in self.available(refresh):
            hay = " ".join([p.id, p.name, p.summary, p.description, " ".join(p.authors)]).lower()
            if all(t in hay for t in terms):
                out.append(p)
        return sorted(out, key=lambda p: p.id)

    def find(self, plugin_id: str, refresh: bool = False) -> IndexPlugin:
        for p in self.available(refresh):
            if p.id == plugin_id:
                return p
        raise NotFoundError(f"no plugin {plugin_id!r} in the repositories")

    # ------------------------------------------------------------------ installed
    def installed(self) -> dict[str, dict]:
        try:
            data = json.loads(self.installed_file.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise PluginError(f"{self.installed_file}: unreadable ({exc})", "failed") from None
        plugins = data.get("plugins") if isinstance(data, dict) else None
        return dict(plugins) if isinstance(plugins, dict) else {}

    def _write_installed(self, plugins: dict) -> None:
        _atomic_write_json(self.installed_file, {"format": INSTALLED_FORMAT, "version": 1, "plugins": plugins})

    def status(self, plugin_id: str, entry: str, permissions: Optional[dict]) -> str:
        from .plugins import ACCEPTED_KEY, ALLOWLIST_KEY, PluginInfo, plugin_status
        info = PluginInfo(plugin_id, entry, "entry-point",
                          manifest={"permissions": permissions} if permissions is not None else None)
        return plugin_status(info, self.config.get(ALLOWLIST_KEY, {}), self.config.get(ACCEPTED_KEY, {}))

    def catalog(self) -> list[dict]:
        """Every entry-point plugin: installed by the manager (site) or in the environment
        (pip), with its manifest, permissions and enabled status."""
        from .plugins import AUTHORITY_KEY, discover
        rows: dict[str, dict] = {}
        inst = self.installed()
        granted = set(self.config.get(AUTHORITY_KEY, []) or [])
        for pid, rec in inst.items():
            rows[pid] = {"id": pid, "name": rec.get("name", pid), "version": rec.get("version", ""),
                         "dist": rec.get("dist", ""), "entry": rec.get("entry", ""), "origin": "site",
                         "permissions": rec.get("permissions"), "manifest": True, "summary": rec.get("summary", ""),
                         "homepage": rec.get("homepage", ""), "source": rec.get("source", {}),
                         "status": self.status(pid, rec.get("entry", ""), rec.get("permissions")),
                         "authority": pid in granted, "installed_by_manager": True,
                         "granted_hosts": self.granted_hosts(pid)}
        try:
            infos = discover((), entry_points=True)
        except Exception:  # noqa: BLE001
            infos = []
        site = str(self.site.resolve()) if self.site.exists() else ""
        for info in infos:
            if info.name in rows:
                continue
            perms = info.manifest.get("permissions", {}) if info.manifest else None
            p = (info.manifest or {}).get("plugin", {})
            rows[info.name] = {"id": info.name, "name": p.get("name", info.name), "version": info.version,
                               "dist": info.dist_name, "entry": info.target,
                               "origin": "site" if info.location == "site" and site else "environment",
                               "permissions": normalize_permissions(perms) if perms is not None else None,
                               "manifest": info.manifest is not None, "manifest_error": info.manifest_error,
                               "summary": p.get("summary", ""), "homepage": p.get("homepage", ""), "source": {},
                               "status": self.status(info.name, info.target,
                                                     normalize_permissions(perms) if perms is not None else None),
                               "authority": info.name in granted, "installed_by_manager": False,
                               "granted_hosts": self.granted_hosts(info.name)}
        return sorted(rows.values(), key=lambda r: r["id"])

    # ------------------------------------------------------------------ install
    def install(self, source: str, *, version: str = "", sha256: str = "", plugin_id: str = "",
                pre: bool = False, confirm_hash: Optional[Callable[[str, int], bool]] = None,
                confirm: Optional[Callable[[WheelInfo], bool]] = None, _update_of: Optional[str] = None) -> dict:
        """Install from a repository id, ``github:``, a wheel URL or a local wheel (see the module
        docstring).  ``confirm(info)`` sees the verified wheel (permissions) before anything is
        written; returning False cancels.  Returns the installed record."""
        prep = self.prepare(source, version=version, sha256=sha256, plugin_id=plugin_id, pre=pre,
                            confirm_hash=confirm_hash, _update_of=_update_of)
        if confirm is not None and not confirm(prep.info):
            raise PluginError("cancelled", "cancelled")
        return self.commit(prep)

    def prepare(self, source: str, *, version: str = "", sha256: str = "", plugin_id: str = "",
                pre: bool = False, confirm_hash: Optional[Callable[[str, int], bool]] = None,
                _update_of: Optional[str] = None) -> "Prepared":
        """Download and verify everything; nothing is written (:meth:`commit` installs it)."""
        try:
            data, filename, entry, origin = self._obtain(source, version=version, sha256=sha256,
                                                         plugin_id=plugin_id, pre=pre, confirm_hash=confirm_hash)
            info = inspect_wheel(data, filename)
            if entry is not None:
                bad = compare_with_index(info, entry, plugin_id or origin.get("id", ""))
                if bad:
                    raise RefusedError(f"{filename}: the wheel does not match the index entry: {'; '.join(bad)}",
                                       "manifest_mismatch")
            self.check_environment(info, replacing=_update_of)
        except RefusedError as exc:
            _audit("plugin_refused", source=_short_url(source), reason=str(exc), code=exc.code)
            raise
        return Prepared(info, data, origin, _update_of, verified_hash=entry is not None or bool(sha256)
                        or "sha256=" in source or origin.get("kind") == "file")

    def commit(self, prep: "Prepared") -> dict:
        # the environment may have changed between prepare and commit (another install)
        self.check_environment(prep.info, replacing=prep.replacing)
        return self._unpack(prep.info, prep.data, prep.origin, replacing=prep.replacing)

    def _obtain(self, source: str, *, version: str, sha256: str, plugin_id: str, pre: bool,
                confirm_hash: Optional[Callable[[str, int], bool]]
                ) -> tuple[bytes, str, Optional[IndexVersion], dict]:
        source = (source or "").strip()
        if not source:
            raise PluginError("nothing to install", "usage")
        # a local wheel
        local = Path(source[len("file://"):] if source.startswith("file://") else source)
        if source.endswith(".whl") and "://" not in source.replace("file://", "", 1) and local.is_file():
            if local.stat().st_size > MAX_WHEEL_BYTES:
                raise RefusedError(f"{local}: larger than {MAX_WHEEL_BYTES} bytes", "size")
            data = local.read_bytes()
            if sha256 and sha256_hex(data) != sha256.lower():
                raise RefusedError(f"{local.name}: sha256 mismatch", "hash_mismatch")
            return data, local.name, None, {"kind": "file", "url": local.name}
        if source.endswith(".whl") and "://" not in source:
            raise NotFoundError(f"{source}: file not found")
        # github shortcut
        m = GITHUB_RE.match(source)
        if m:
            repo = source
            idx_data, base = self._github_index(m.group("owner"), m.group("repo"), m.group("tag"))
            idx = parse_index(idx_data, base, repo)
            if idx_data.get("synthetic"):
                for p in idx.plugins:
                    for v in p.versions:
                        v.synthetic = True
            cands = [p for p in idx.plugins if not plugin_id or p.id == plugin_id]
            if not cands:
                raise NotFoundError(f"{source}: no plugin" + (f" {plugin_id!r}" if plugin_id else "")
                                    + " in the release")
            if len(cands) > 1:
                raise PluginError(f"{source} has several plugins ({', '.join(p.id for p in cands)}): name one",
                                  "usage", candidates=[p.id for p in cands])
            return self._from_entry(cands[0], version, pre, {"kind": "github", "repository": repo})
        # a wheel URL
        split = urllib.parse.urlsplit(source)
        if split.scheme in ("https", "http", "file") and split.path.endswith(".whl"):
            frag = urllib.parse.parse_qs(split.fragment)
            want = (sha256 or (frag.get("sha256") or [""])[0]).lower()
            url = urllib.parse.urlunsplit(split._replace(fragment=""))
            data, _ = self.fetch(url, MAX_WHEEL_BYTES)
            got = sha256_hex(data)
            if want:
                if not SHA_RE.match(want):
                    raise PluginError("sha256 must be 64 hex digits", "usage")
                if got != want:
                    raise RefusedError(f"{split.path.rsplit('/', 1)[-1]}: sha256 mismatch (expected {want}, got {got})",
                                       "hash_mismatch")
            elif confirm_hash is None or not confirm_hash(got, len(data)):
                raise RefusedError(f"the downloaded wheel's sha256 is {got}; give it with #sha256=<hex> (or --sha256) "
                                   "or confirm it", "unverified", sha256=got)
            return data, urllib.parse.unquote(split.path.rsplit("/", 1)[-1]), None, {"kind": "url", "url": url}
        if split.scheme:
            if split.scheme == "http":
                raise RefusedError("http is allowed only to this computer; use https", "insecure_url")
            raise PluginError(f"{source}: not a wheel URL (to add a repository: plugin repo add)", "usage")
        # a plugin id from the repositories: "id" or "id==version"
        pid, _, ver = source.partition("==")
        pid = pid.strip()
        if not ID_RE.match(pid):
            raise PluginError(f"{source!r}: not a plugin id, wheel, URL or github:<owner>/<repo>", "usage")
        p = self.find(pid)
        return self._from_entry(p, ver.strip() or version, pre, {"kind": "repository", "repository": p.repository,
                                                                  "id": pid})

    def _from_entry(self, p: IndexPlugin, version: str, pre: bool, origin: dict
                    ) -> tuple[bytes, str, Optional[IndexVersion], dict]:
        entry = p.best(self.plugin_api, version, pre)
        if entry is None:
            newest = max(p.versions, key=lambda v: Version.parse(v.version))
            why = "; ".join(newest.compatible(self.plugin_api)) or "only pre-releases (use --pre)"
            raise RefusedError(f"{p.id}: no compatible version" + (f" {version}" if version else "") + f" ({why})",
                               "incompatible")
        if not entry.synthetic:
            bad = entry.compatible(self.plugin_api)
            if bad:
                raise RefusedError(f"{p.id} {entry.version}: {'; '.join(bad)}", "incompatible")
        data, _ = self.fetch(entry.url, min(MAX_WHEEL_BYTES, entry.size or MAX_WHEEL_BYTES))
        if entry.size and len(data) != entry.size:
            raise RefusedError(f"{entry.filename}: size {len(data)} != index size {entry.size}", "hash_mismatch")
        if sha256_hex(data) != entry.sha256:
            raise RefusedError(f"{entry.filename}: sha256 mismatch (expected {entry.sha256}, got {sha256_hex(data)})",
                               "hash_mismatch")
        origin = dict(origin, url=entry.url, id=origin.get("id") or (p.id if not entry.synthetic else ""))
        return data, entry.filename, entry, origin

    def check_environment(self, info: WheelInfo, replacing: Optional[str] = None) -> None:
        """Compatibility, collisions and dependencies against this application; raises
        :class:`RefusedError`."""
        bad = compatibility_errors(info.manifest, self.plugin_api)
        if bad:
            raise RefusedError(f"{info.id} {info.version} is not compatible: {'; '.join(bad)}", "incompatible")
        if info.requires_python:
            try:
                if not SpecifierSet(info.requires_python).contains(".".join(map(str, sys.version_info[:3])),
                                                                   prereleases=True):
                    raise RefusedError(f"{info.id} needs Python {info.requires_python}", "incompatible")
            except InvalidSpecifier:
                raise RefusedError(f"invalid Requires-Python {info.requires_python!r}", "layout") from None
        inst = self.installed()
        if info.id in inst and info.id != replacing:
            raise RefusedError(f"{info.id} is already installed (use update)", "installed")
        if info.id in self.reserved:
            raise RefusedError(f"the plugin id {info.id!r} is taken by a built-in plugin", "collision")
        own_tops: set[str] = set()
        if replacing and replacing in inst:
            own_tops = set(inst[replacing].get("top_level", []))
        for pid, rec in inst.items():
            if pid == replacing:
                continue
            if canonical_name(rec.get("dist", "")) == canonical_name(info.dist):
                raise RefusedError(f"the distribution {info.dist} is already installed as {pid!r}", "collision")
            clash = set(rec.get("top_level", [])) & set(info.top_level)
            if clash:
                raise RefusedError(f"top-level {', '.join(sorted(clash))} collides with plugin {pid!r}", "collision")
        std = stdlib_names()
        site = str(self.site.resolve()) if self.site.exists() else None
        for top in info.top_level:
            if top == "mlaser" or top.startswith("mlaser.") or top in std:
                raise RefusedError(f"top-level module {top!r} collides with the core", "collision")
            if top in own_tops:
                continue
            origin = _top_level_origin(top)
            if origin is not None:
                raise RefusedError(f"top-level module {top!r} collides with an importable module ({origin})",
                                   "collision")
            if site and (Path(site) / top).exists() or site and (Path(site) / f"{top}.py").exists():
                raise RefusedError(f"{top!r} already exists in the plugin site directory", "collision")
        # an entry-point name already used by a plugin of the environment (pip)
        try:
            for ep in importlib.metadata.entry_points(group="mlaser.plugins"):
                if ep.name == info.id and info.id != replacing:
                    raise RefusedError(f"the plugin id {info.id!r} is already provided by "
                                       f"{getattr(ep.dist, 'name', 'another package')}", "collision")
        except RefusedError:
            raise
        except Exception:  # noqa: BLE001 - broken metadata elsewhere does not block an install
            pass
        missing = missing_dependencies(info.requires, [site] if site else [])
        if missing:
            raise RefusedError(f"{info.id} needs packages this installation does not have: {', '.join(missing)}",
                               "dependency", missing=missing)

    def _unpack(self, info: WheelInfo, data: bytes, origin: dict, replacing: Optional[str]) -> dict:
        site = self.site
        with _Lock(self.root / ".lock"):
            site.mkdir(parents=True, exist_ok=True)
            inst = self.installed()
            old = inst.get(replacing) if replacing else None
            stage = site / f".staging-{uuid.uuid4().hex[:12]}"
            backup = site / f".backup-{uuid.uuid4().hex[:12]}"
            stage.mkdir()
            moved_new: list[Path] = []
            moved_old: list[tuple[Path, Path]] = []
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for name in info.files:
                        target = stage.joinpath(*name.split("/"))
                        if not str(target.resolve()).startswith(str(stage.resolve()) + os.sep):
                            raise RefusedError(f"unsafe path {name!r}", "layout")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(name) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                entries = sorted(p.name for p in stage.iterdir())
                old_entries = sorted({f.split("/", 1)[0] for f in (old or {}).get("files", [])})
                for e in entries:
                    if (site / e).exists() and e not in old_entries:
                        raise RefusedError(f"{e!r} already exists in the plugin site directory", "collision")
                if old_entries:
                    backup.mkdir()
                    for e in old_entries:
                        if (site / e).exists():
                            os.replace(site / e, backup / e)
                            moved_old.append((backup / e, site / e))
                for e in entries:
                    os.replace(stage / e, site / e)
                    moved_new.append(site / e)
            except BaseException:
                for newp in moved_new:
                    if newp.is_dir():
                        shutil.rmtree(newp, ignore_errors=True)
                    else:
                        newp.unlink(missing_ok=True)
                for bak, orig in moved_old:
                    os.replace(bak, orig)
                raise
            finally:
                shutil.rmtree(stage, ignore_errors=True)
                shutil.rmtree(backup, ignore_errors=True)
            p = info.manifest.get("plugin", {})
            rec = {"id": info.id, "name": p.get("name", info.id), "dist": info.dist, "version": info.version,
                   "entry": info.entry_value, "sha256": info.sha256, "size": info.size, "filename": info.filename,
                   "files": list(info.files), "top_level": info.top_level, "permissions": info.permissions,
                   "api": p.get("api", ""), "mlaser": p.get("mlaser", ""), "requires": info.requires,
                   "summary": p.get("summary", ""), "homepage": p.get("homepage", ""), "source": origin,
                   "installed": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if replacing and replacing != info.id:
                inst.pop(replacing, None)
            inst[info.id] = rec
            self._write_installed(inst)
        self.changed = True
        importlib.invalidate_caches()
        if old is None:
            _audit("plugin_install", id=info.id, version=info.version, sha256=info.sha256, dist=info.dist,
                   source=origin.get("kind"), url=_short_url(str(origin.get("url", ""))), permissions=info.permissions)
        return rec

    # ------------------------------------------------------------------ uninstall
    def uninstall(self, plugin_id: str, purge_data: bool = False) -> dict:
        """Remove exactly the recorded files (and directories they leave empty), and the
        plugin's allowlist, authority and accepted entries."""
        inst = self.installed()
        rec = inst.get(plugin_id)
        if rec is None:
            raise NotFoundError(f"{plugin_id!r} was not installed by the plugin manager")
        site = self.site.resolve()
        removed = 0
        with _Lock(self.root / ".lock"):
            dirs: set[Path] = set()
            for rel in rec.get("files", []):
                if not isinstance(rel, str) or not _safe_name(rel):
                    log.warning("plugin %s: unsafe recorded path %r ignored", plugin_id, rel)
                    continue
                p = site.joinpath(*rel.split("/"))
                try:
                    rp = p.resolve()
                except OSError:
                    continue
                if not str(rp).startswith(str(site) + os.sep):
                    log.warning("plugin %s: recorded path %r outside the site directory ignored", plugin_id, rel)
                    continue
                try:
                    p.unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
                q = p.parent
                while q != site and str(q).startswith(str(site)):
                    dirs.add(q)
                    q = q.parent
                # interpreter caches next to the removed module
                cache = p.parent / "__pycache__"
                if p.suffix == ".py" and cache.is_dir():
                    for c in cache.glob(p.stem + ".*.pyc"):
                        c.unlink(missing_ok=True)
                    dirs.add(cache)
            for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
            del inst[plugin_id]
            self._write_installed(inst)
        self.disable(plugin_id, audit_event=False)
        if self.granted_hosts(plugin_id):
            self.grant_hosts(plugin_id, remove=self.granted_hosts(plugin_id))
        if purge_data:
            shutil.rmtree(self.data_dir(plugin_id), ignore_errors=True)
        self.changed = True
        importlib.invalidate_caches()
        _audit("plugin_uninstall", id=plugin_id, version=rec.get("version"), files=removed, purge_data=purge_data)
        return {"id": plugin_id, "version": rec.get("version"), "files_removed": removed, "purged": purge_data}

    # ------------------------------------------------------------------ updates
    def check_updates(self, refresh: bool = False, pre: bool = False) -> list[dict]:
        """Newer compatible versions in the repositories for the plugins the manager installed."""
        inst = self.installed()
        if not inst:
            return []
        avail = {p.id: p for p in self.available(refresh)}
        out = []
        for pid, rec in sorted(inst.items()):
            p = avail.get(pid)
            if p is None:
                continue
            best = p.best(self.plugin_api, pre=pre)
            if best is None:
                continue
            try:
                newer = Version.parse(best.version) > Version.parse(str(rec.get("version", "0")))
            except InvalidVersion:
                newer = False
            if newer:
                perms_changed = (not best.synthetic
                                 and normalize_permissions(best.permissions) != rec.get("permissions"))
                out.append({"id": pid, "installed": rec.get("version"), "available": best.version,
                            "repository": p.repository, "permissions_changed": perms_changed,
                            "permissions": normalize_permissions(best.permissions) if not best.synthetic else None})
        return out

    def update(self, plugin_id: str, *, version: str = "", pre: bool = False,
               confirm: Optional[Callable[[WheelInfo], bool]] = None) -> dict:
        """Install the newest compatible version over the installed one.  The plugin stays
        enabled only when its entry point and permissions are unchanged; otherwise it is
        disabled and must be enabled again (``needs_enable``)."""
        from .plugins import ALLOWLIST_KEY
        inst = self.installed()
        old = inst.get(plugin_id)
        if old is None:
            raise NotFoundError(f"{plugin_id!r} was not installed by the plugin manager")
        p = self.find(plugin_id)
        best = p.best(self.plugin_api, version, pre)
        if best is None:
            raise RefusedError(f"{plugin_id}: no compatible version in the repositories", "incompatible")
        if not version and Version.parse(best.version) <= Version.parse(str(old.get("version", "0"))):
            return {"id": plugin_id, "version": old.get("version"), "updated": False, "needs_enable": False}
        was_enabled = (self.config.get(ALLOWLIST_KEY, {}) or {}).get(plugin_id) == old.get("entry")
        rec = self.install(f"{plugin_id}=={best.version}", plugin_id=plugin_id, confirm=confirm, _update_of=plugin_id)
        same = rec["entry"] == old.get("entry") and rec["permissions"] == old.get("permissions")
        needs_enable = False
        if was_enabled and not same:
            self.disable(plugin_id, audit_event=False)
            needs_enable = True
        _audit("plugin_update", id=plugin_id, before=old.get("version"), version=rec["version"],
               sha256=rec["sha256"], kept_enabled=was_enabled and same, permissions=rec["permissions"])
        return {"id": plugin_id, "before": old.get("version"), "version": rec["version"], "updated": True,
                "kept_enabled": was_enabled and same, "needs_enable": needs_enable}

    # ------------------------------------------------------------------ enabling
    def enable(self, plugin_id: str, entry: str, permissions: Optional[dict], grant_authority: bool = False) -> dict:
        """Put the plugin in the allowlist with the permissions accepted now (the caller has
        shown them and has the Service authority).  ``grant_authority``: machine authority too
        (only when the plugin declares ``machine``, or is a legacy plugin)."""
        from .plugins import ACCEPTED_KEY, ALLOWLIST_KEY, AUTHORITY_KEY
        allow = dict(self.config.get(ALLOWLIST_KEY, {}) or {})
        before = sorted(allow)
        allow[plugin_id] = entry
        accepted = dict(self.config.get(ACCEPTED_KEY, {}) or {})
        if permissions is not None:
            accepted[plugin_id] = normalize_permissions(permissions)
        else:
            accepted.pop(plugin_id, None)
        auth = [n for n in (self.config.get(AUTHORITY_KEY, []) or []) if n != plugin_id]
        if grant_authority and (permissions is None or Permissions.from_dict(permissions).machine):
            auth.append(plugin_id)
        self.config.set(ALLOWLIST_KEY, allow)
        self.config.set(ACCEPTED_KEY, accepted)
        self.config.set(AUTHORITY_KEY, sorted(n for n in auth if n in allow))
        self._save_all()
        self.changed = True
        _audit("plugin_allowlist", enabled=sorted(allow), before=before,
               authority=list(self.config.get(AUTHORITY_KEY, [])), permissions=accepted.get(plugin_id))
        return {"id": plugin_id, "enabled": True, "authority": plugin_id in self.config.get(AUTHORITY_KEY, [])}

    def disable(self, plugin_id: str, audit_event: bool = True) -> dict:
        from .plugins import ACCEPTED_KEY, ALLOWLIST_KEY, AUTHORITY_KEY
        allow = dict(self.config.get(ALLOWLIST_KEY, {}) or {})
        before = sorted(allow)
        was = allow.pop(plugin_id, None) is not None
        accepted = dict(self.config.get(ACCEPTED_KEY, {}) or {})
        accepted.pop(plugin_id, None)
        auth = [n for n in (self.config.get(AUTHORITY_KEY, []) or []) if n != plugin_id]
        if was or plugin_id in (self.config.get(AUTHORITY_KEY, []) or []) or plugin_id in \
                (self.config.get(ACCEPTED_KEY, {}) or {}):
            self.config.set(ALLOWLIST_KEY, allow)
            self.config.set(ACCEPTED_KEY, accepted)
            self.config.set(AUTHORITY_KEY, auth)
            self._save_all()
            self.changed = True
            _audit("plugin_allowlist", enabled=sorted(allow), before=before, authority=auth)
        return {"id": plugin_id, "enabled": False, "was_enabled": was}

    def grant_hosts(self, plugin_id: str, add: Iterable[str] = (), remove: Iterable[str] = ()) -> dict:
        """Network hosts granted to a plugin in addition to its manifest (``plugins.network``):
        exact host names only (no wildcard), https still required; the caller has the Service
        authority.  Audited (``plugin_network_grant``); applies at once."""
        from .plugin_manifest import valid_host_pattern
        from .plugins import NETWORK_KEY
        if not ID_RE.match(plugin_id or ""):
            raise PluginError(f"invalid plugin id {plugin_id!r}", "usage")
        grants = {k: list(v) for k, v in (self.config.get(NETWORK_KEY, {}) or {}).items() if isinstance(v, list)}
        hosts = list(grants.get(plugin_id, []))
        for h in add:
            h = str(h).strip().lower().rstrip(".")
            if not valid_host_pattern(h) or "*" in h:
                raise PluginError(f"{h!r} is not a host name (an exact host, e.g. acme.onshape.com)", "usage")
            if h not in hosts:
                hosts.append(h)
        rm = {str(h).strip().lower() for h in remove}
        hosts = [h for h in hosts if h not in rm]
        before = grants.get(plugin_id, [])
        if hosts:
            grants[plugin_id] = sorted(hosts)
        else:
            grants.pop(plugin_id, None)
        self.config.set(NETWORK_KEY, grants)
        self._save_all()
        if sorted(before) != sorted(hosts):
            _audit("plugin_network_grant", id=plugin_id, hosts=sorted(hosts), before=sorted(before))
        return {"id": plugin_id, "hosts": sorted(hosts)}

    def granted_hosts(self, plugin_id: str) -> list[str]:
        from .plugins import NETWORK_KEY
        v = (self.config.get(NETWORK_KEY, {}) or {}).get(plugin_id, [])
        return [h for h in v if isinstance(h, str)] if isinstance(v, list) else []

    def _save_all(self) -> None:
        try:
            self.config.save()
        except OSError as exc:
            raise PluginError(f"the configuration could not be saved ({exc})", "output") from None


def parse_sha256sums(text: str) -> dict[str, str]:
    """``<hex>  <file>`` lines (``sha256sum`` output, ``*file`` binary marker allowed)."""
    out = {}
    for line in text.splitlines()[:10000]:
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and SHA_RE.match(parts[0].lower()):
            out[parts[1].lstrip("*").strip()] = parts[0].lower()
    return out


def _short_url(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
        if p.scheme in ("http", "https"):
            return f"{p.scheme}://{p.hostname}{p.path}"
    except ValueError:
        pass
    return url if len(url) < 200 else url[:197] + "..."


__all__ = ["PluginManager", "PluginError", "RefusedError", "NotFoundError", "FetchError", "Index", "IndexPlugin",
           "IndexVersion", "WheelInfo", "inspect_wheel", "parse_index", "validate_index", "activate_site", "site_dir",
           "plugin_data_dir", "plugins_root", "missing_dependencies", "parse_sha256sums", "MANIFEST_NAME",
           "INDEX_FORMAT", "INDEX_VERSION", "INDEX_ASSET", "is_local_host"]
