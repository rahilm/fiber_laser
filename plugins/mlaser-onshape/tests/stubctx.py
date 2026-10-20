"""A stub plugin context implementing plugin API 2 as the shared contract describes it
(docs/dev/PLUGIN_STANDARD.md): ``http`` (urllib, host allow-list, https except localhost,
Basic auth, 429/502/503/504 retries with Retry-After, size cap), ``secrets`` in memory,
``data_dir`` in a temporary folder, ``run_in_background`` (synchronous by default), ``services``
with a real OrderBook and TechnologyStore, plus the API-1 members the plugin uses."""
from __future__ import annotations

import base64
import fnmatch
import json
import time
import types
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from mlaser.services.config import ConfigService
from mlaser.services.plugins import PluginSettings


class CIMap(dict):
    def __init__(self, items):
        super().__init__({k.lower(): v for k, v in items})

    def get(self, k, d=None):
        return super().get(k.lower(), d)

    def __getitem__(self, k):
        return super().__getitem__(k.lower())


class Response:
    def __init__(self, status, headers, content, url):
        self.status, self.headers, self.content, self.url = status, headers, content, url

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content.decode("utf-8"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class StubHttp:
    LOCAL = ("127.0.0.1", "localhost", "::1")

    def __init__(self, allowed):
        self.allowed = list(allowed)
        self.log = []            # every URL asked for, also refused ones
        self.sleeps = []
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _check(self, url):
        u = urlsplit(url)
        host = (u.hostname or "").lower()
        if u.scheme != "https" and not (u.scheme == "http" and host in self.LOCAL):
            raise PermissionError(f"{url}: https only")
        if not any(host == a or fnmatch.fnmatch(host, a) for a in self.allowed):
            raise PermissionError(f"{host} is not in permissions.network")

    def get(self, url, *, params=None, headers=None, timeout=20, max_bytes=50_000_000, auth=None):
        self.log.append(url)
        self._check(url)
        assert params is None or isinstance(params, dict)
        hdrs = dict(headers or {})
        if auth:
            hdrs["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        waited = 0.0
        for attempt in range(4):
            req = urllib.request.Request(url, headers=hdrs, method="GET")
            try:
                with self.opener.open(req, timeout=timeout) as r:
                    status, rh, body = r.status, CIMap(r.headers.items()), r.read(max_bytes + 1)
            except urllib.error.HTTPError as e:
                status, rh, body = e.code, CIMap(e.headers.items()), e.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ValueError("response larger than max_bytes")
            if status in (429, 502, 503, 504) and attempt < 3:
                ra = rh.get("retry-after") or "1"
                try:
                    delay = float(ra)
                except ValueError:
                    delay = 1.0
                if waited + delay > 30:
                    break
                self.sleeps.append(delay)
                time.sleep(delay)
                waited += delay
                continue
            break
        return Response(status, rh, body, url)

    def post(self, url, **kw):
        raise AssertionError("the plugin never posts")


class Secrets:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v):
        self.d[k] = str(v)

    def delete(self, k):
        self.d.pop(k, None)


class Handle:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class Commands:
    def __init__(self):
        self.ran = []
        self.handlers = {}

    def run(self, cid, *args):
        self.ran.append((cid, args))
        fn = self.handlers.get(cid)
        return fn(*args) if fn else None


class StubCtx:
    def __init__(self, tmp: Path, allowed=("127.0.0.1",), with_tech=True):
        from mlaser.services.orders import OrderBook
        self.name = "onshape"
        self.manifest = {"plugin": {"id": "onshape"}, "permissions": {"network": list(allowed)}}
        self.permissions = types.MappingProxyType({"network": tuple(allowed), "secrets": True, "orders": True,
                                                   "files": True, "machine": False})
        self.data_dir = Path(tmp) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.http = StubHttp(allowed)
        self.secrets = Secrets()
        self.config = ConfigService(load=False)
        tech = None
        if with_tech:
            from mlaser.services.technology import TechnologyStore
            tech = TechnologyStore(Path(tmp) / "tech.sqlite3")
            tech.seed_examples()
        self.services = types.SimpleNamespace(orders=OrderBook(None), technology=tech, queue=None, intake=None,
                                              config=self.config)
        self.commands = Commands()
        self.messages, self.logs, self.workspaces, self.docks, self.registered = [], [], [], [], {}
        self.background = []
        self.window = None
        self.sync = True

    # API 1
    def settings_schema(self, settings):
        from dataclasses import replace
        items = [replace(s, key="plugin.onshape." + s.key, section="plugin.onshape", source="plugin:onshape")
                 for s in settings]
        self.config.register(items)
        return PluginSettings(self.config, "plugin.onshape.", [s.key for s in settings])

    def register_command(self, cid, handler, label="", **kw):
        self.registered[cid] = handler

    def add_workspace(self, name, widget, title, **options):
        self.workspaces.append((name, widget, title, options))
        return widget

    def add_dock(self, widget, title, area="right", **kw):
        self.docks.append((widget, title))
        return widget

    def log(self, msg):
        self.logs.append(msg)

    def on_close(self, fn):
        pass

    # API 2
    def show_message(self, text, level="info"):
        self.messages.append((level, text))

    def run_in_background(self, fn, *, on_done=None, on_error=None):
        h = Handle()
        self.background.append(h)
        try:
            r = fn()
        except Exception as exc:  # noqa: BLE001
            if on_error:
                on_error(exc)
            return h
        if on_done:
            on_done(r)
        return h
