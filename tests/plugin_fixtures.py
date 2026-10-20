"""Test helpers of the plugin manager: build plugin wheels from sources in a temp directory, and
serve a repository (index + wheels) from a threaded ``http.server`` on 127.0.0.1.

Nothing here touches the network beyond the loopback interface.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import io
import json
import threading
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

PLUGIN_SOURCE = '''
PLUGIN_API = 2
SEEN = {}


class Plugin:
    def register(self, ctx):
        SEEN["ctx"] = ctx
        ctx.register_command("{pid}.hello", lambda: SEEN.setdefault("ran", True), label="Hello")
        return self


plugin = Plugin()
'''


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v))


def manifest_toml(plugin: dict, permissions: dict) -> str:
    lines = ["[plugin]"] + [f"{k} = {_toml_value(v)}" for k, v in plugin.items()]
    lines += ["", "[permissions]"] + [f"{k} = {_toml_value(v)}" for k, v in permissions.items()]
    return "\n".join(lines) + "\n"


def _hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def build_wheel(dest: Path, *, dist: str = "mlaser_demo", version: str = "0.1.0", pid: str = "demo",
                module: str = "", plugin: Optional[dict] = None, permissions: Optional[dict] = None,
                requires: tuple = (), extra_files: Optional[dict] = None, source: str = PLUGIN_SOURCE,
                tag: str = "py3-none-any", purelib: bool = True, manifest_text: Optional[str] = None,
                tamper: Optional[Callable[[dict], None]] = None, filename: str = "") -> Path:
    """A wheel with one ``mlaser.plugins`` entry point ``<pid> = <module>:plugin`` and its manifest.
    ``tamper(files)`` may change the files after RECORD was computed (RECORD tests)."""
    module = module or dist.replace("-", "_")
    entry = f"{module}:plugin"
    p = {"id": pid, "name": f"Demo {pid}", "version": version, "entry": entry, "api": ">=2,<3",
         "mlaser": ">=0.1.0rc1", "summary": f"The {pid} test plugin", "license": "MIT",
         "homepage": "https://example.org/" + pid}
    p.update(plugin or {})
    perms = {"network": [], "files": False, "document": True, "orders": False, "queue": False,
             "machine": False, "secrets": False}
    perms.update(permissions or {})
    di = f"{dist}-{version}.dist-info"
    files: dict[str, bytes] = {
        f"{module}/__init__.py": source.replace("{pid}", pid).encode(),
        f"{module}/mlaser-plugin.toml": (manifest_text if manifest_text is not None
                                         else manifest_toml(p, perms)).encode(),
    }
    for k, v in (extra_files or {}).items():
        files[k] = v if isinstance(v, bytes) else str(v).encode()
    meta = [f"Metadata-Version: 2.1", f"Name: {dist.replace('_', '-')}", f"Version: {version}",
            "Requires-Python: >=3.12"] + [f"Requires-Dist: {r}" for r in requires]
    files[f"{di}/METADATA"] = ("\n".join(meta) + "\n").encode()
    files[f"{di}/WHEEL"] = (f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: {'true' if purelib else 'false'}\n"
                            f"Tag: {tag}\n").encode()
    files[f"{di}/entry_points.txt"] = f"[mlaser.plugins]\n{pid} = {entry}\n".encode()
    record = "".join(f"{n},{_hash(d)},{len(d)}\n" for n, d in files.items()) + f"{di}/RECORD,,\n"
    files[f"{di}/RECORD"] = record.encode()
    if tamper:
        tamper(files)
    name = filename or f"{dist}-{version}-{tag}.whl"
    path = Path(dest) / name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, d in files.items():
            zf.writestr(n, d)
    path.write_bytes(buf.getvalue())
    return path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RepoServer:
    """A threaded HTTP server on 127.0.0.1 serving ``routes``: path -> bytes, or
    ``(status, headers, body)``, or a callable ``(handler) -> (status, headers, body)``."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.hits: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):          # quiet
                pass

            def _serve(self) -> None:
                path = self.path.split("?", 1)[0]
                outer.hits.append(self.path)
                route = outer.routes.get(path)
                if route is None:
                    status, headers, body = 404, {}, b"not found"
                elif callable(route):
                    status, headers, body = route(self)
                elif isinstance(route, tuple):
                    status, headers, body = route
                else:
                    status, headers, body = 200, {}, route
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if "Content-Length" not in headers:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _serve

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.body = self.rfile.read(n)
                self._serve()

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                       name="test-plugin-repo", daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def url(self, path: str) -> str:
        return self.base + path

    def add_wheel(self, path: Path, at: str = "/wheels/") -> str:
        self.routes[at + path.name] = path.read_bytes()
        return self.url(at + path.name)

    def set_index(self, index: dict, at: str = "/mlaser-plugin-index.json") -> str:
        self.routes[at] = json.dumps(index).encode()
        return self.url(at)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def index_for(wheels: list[Path], base_url: str = "") -> dict:
    """An index of ``wheels`` (the project's own builder)."""
    from mlaser.services.plugin_index import build_index
    return build_index([str(w) for w in wheels], base_url=base_url, name="test repo", published="2026-10-20")


def wheel_from_project(project: Path, dest: Path) -> Path:
    """A wheel of a plugin source tree (pyproject.toml [project] + [tool.setuptools] packages and
    package-data), built without a build backend (no network, no setuptools needed): what
    ``uv build --wheel`` puts in the wheel of a simple pure-Python plugin."""
    import tomllib
    project = Path(project)
    with open(project / "pyproject.toml", "rb") as fh:
        py = tomllib.load(fh)
    proj = py["project"]
    dist = proj["name"].replace("-", "_")
    version = proj["version"]
    st = py.get("tool", {}).get("setuptools", {})
    pkgs = st.get("packages") or [dist]
    data = st.get("package-data", {})
    files: dict[str, bytes] = {}
    for pkg in pkgs:
        base = project / pkg.replace(".", "/")
        pats = ["*.py"] + list(data.get(pkg, []))
        for pat in pats:
            for f in sorted(base.glob(pat)):
                if f.is_file():
                    files[f"{pkg.replace('.', '/')}/{f.name}"] = f.read_bytes()
    di = f"{dist}-{version}.dist-info"
    meta = ["Metadata-Version: 2.4", f"Name: {proj['name']}", f"Version: {version}"]
    if proj.get("requires-python"):
        meta.append(f"Requires-Python: {proj['requires-python']}")
    meta += [f"Requires-Dist: {r}" for r in proj.get("dependencies", [])]
    files[f"{di}/METADATA"] = ("\n".join(meta) + "\n").encode()
    files[f"{di}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    eps = proj.get("entry-points", {})
    files[f"{di}/entry_points.txt"] = "".join(
        f"[{g}]\n" + "".join(f"{k} = {v}\n" for k, v in grp.items()) + "\n" for g, grp in eps.items()).encode()
    record = "".join(f"{n},{_hash(d)},{len(d)}\n" for n, d in files.items()) + f"{di}/RECORD,,\n"
    files[f"{di}/RECORD"] = record.encode()
    path = Path(dest) / f"{dist}-{version}-py3-none-any.whl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, d in files.items():
            zf.writestr(n, d)
    return path
