"""Onshape REST client over the plugin context's ``ctx.http`` (Qt-free).

* Authentication: Onshape API keys as HTTP Basic (access key = user, secret key = password), the
  scheme ``BasicAuth`` of Onshape's OpenAPI description.  The keys come from ``ctx.secrets``.
* Server: ``https://cad.onshape.com`` or an Onshape Enterprise domain (``https://<company>.onshape.com``);
  ``http`` only for a local test server.  Any other host is refused here, before ctx.http
  (whose manifest permission allows ``cad.onshape.com`` and ``*.onshape.com`` only) is asked.
* Quota: Onshape limits API calls per user and day.  Immutable answers (a version, a document
  microversion) are cached as JSON in ``<data_dir>/cache`` under a key that names the document
  microversion, element, part and configuration; at most two requests run at the same time;
  ``ctx.http`` retries 429 / 503 honouring ``Retry-After``; a 429 that persists raises
  :class:`QuotaError`.  :attr:`OnshapeClient.calls` counts the requests that went out.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlencode, urlsplit

DEFAULT_SERVER = "https://cad.onshape.com"
API_PATH = "/api/v17"            # the version of the OpenAPI description this plugin was written against
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_CACHE_FILES = 4000


class OnshapeError(RuntimeError):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class AuthError(OnshapeError):
    pass


class QuotaError(OnshapeError):
    pass


class NotFound(OnshapeError):
    pass


def check_server(url: str) -> str:
    """The normalised server URL (scheme://host[:port]) or ValueError."""
    u = urlsplit(str(url or "").strip().rstrip("/"))
    host = (u.hostname or "").lower()
    if not host:
        raise ValueError("the Onshape server needs a full address, e.g. https://cad.onshape.com")
    if u.scheme == "http" and host in LOCAL_HOSTS:
        pass
    elif u.scheme != "https":
        raise ValueError("the Onshape server must use https")
    elif not (host == "cad.onshape.com" or host.endswith(".onshape.com")):
        raise ValueError(f"{host} is not an Onshape server (cad.onshape.com or <company>.onshape.com)")
    netloc = host if u.port is None else f"{host}:{u.port}"
    if ":" in host and u.port is not None:
        netloc = f"[{host}]:{u.port}"
    return f"{u.scheme}://{netloc}"


def wvm_path(wvm: str, wvmid: str) -> str:
    if wvm not in ("w", "v", "m"):
        raise ValueError(f"wvm must be w, v or m, not {wvm!r}")
    return f"{wvm}/{quote(wvmid, safe='')}"


class OnshapeClient:
    def __init__(self, http: Any, server: str = DEFAULT_SERVER, auth: Optional[tuple[str, str]] = None,
                 cache_dir: Optional[os.PathLike | str] = None, concurrency: int = 2, api_path: str = API_PATH,
                 timeout: float = 30.0) -> None:
        if http is None:
            raise OnshapeError("this mlaser has no plugin HTTP client (plugin API 2 is needed)")
        self.http = http
        self.server = check_server(server)
        self.api = self.server + api_path
        self.auth = auth
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.gate = threading.BoundedSemaphore(max(1, min(2, int(concurrency))))
        self.lock = threading.Lock()
        self.calls = 0
        self.cache_hits = 0
        self.timeout = timeout
        self._microversions: dict[tuple[str, str, str], str] = {}

    # ------------------------------------------------------------------ transport
    def url(self, path: str, params: Optional[dict] = None) -> str:
        q: list[tuple[str, str]] = []
        for k, v in (params or {}).items():
            if v is None or v == "":
                continue
            if isinstance(v, bool):
                v = "true" if v else "false"
            if isinstance(v, (list, tuple)):
                q.extend((k, str(x)) for x in v)
            else:
                q.append((k, str(v)))
        return self.api + path + (("?" + urlencode(q)) if q else "")

    def get(self, path: str, params: Optional[dict] = None, cache_key: Optional[str] = None,
            accept: str = "application/json") -> Any:
        if cache_key is not None:
            hit = self._cache_get(cache_key)
            if hit is not None:
                with self.lock:
                    self.cache_hits += 1
                return hit
        url = self.url(path, params)
        with self.gate:
            with self.lock:
                self.calls += 1
            resp = self.http.get(url, headers={"Accept": accept}, auth=self.auth, timeout=self.timeout)
        status = int(getattr(resp, "status", 0))
        if status == 401 or status == 403:
            raise AuthError(f"Onshape refused the request ({status}): check the API keys and that "
                            "their account can open this document", status)
        if status == 404:
            raise NotFound(f"Onshape: not found ({path})", status)
        if status == 429:
            raise QuotaError("Onshape's API limit is reached (429): wait and try again later, "
                             "or use another account's keys", status)
        if status >= 400 or status == 0:
            raise OnshapeError(f"Onshape answered {status}: {_message(resp)}", status)
        try:
            data = resp.json()
        except ValueError as exc:
            raise OnshapeError(f"Onshape sent something that is not JSON ({exc})", status) from None
        if cache_key is not None:
            self._cache_put(cache_key, data)
        return data

    # ------------------------------------------------------------------ cache
    def _cache_file(self, key: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        h = hashlib.sha256((self.server + "|" + key).encode()).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def _cache_get(self, key: str) -> Any:
        p = self._cache_file(key)
        if p is None or not p.exists():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d.get("data") if isinstance(d, dict) and d.get("key") == key else None
        except (OSError, ValueError):
            return None

    def _cache_put(self, key: str, data: Any) -> None:
        p = self._cache_file(key)
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".c-", dir=str(p.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"key": key, "saved": time.time(), "data": data}, f)
            os.replace(tmp, p)
        except OSError:
            pass

    def prune_cache(self, keep: int = MAX_CACHE_FILES) -> int:
        if self.cache_dir is None or not self.cache_dir.exists():
            return 0
        files = sorted(self.cache_dir.glob("*/*.json"), key=lambda p: p.stat().st_mtime)
        n = 0
        for p in files[:max(0, len(files) - keep)]:
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        return n

    def clear_cache(self) -> None:
        if self.cache_dir is None:
            return
        for p in self.cache_dir.glob("*/*.json"):
            try:
                p.unlink()
            except OSError:
                pass
        self._microversions.clear()

    # ------------------------------------------------------------------ API
    def session(self) -> dict:
        """``GET /users/sessioninfo``: the account the keys belong to (the connection test)."""
        return self.get("/users/sessioninfo")

    def documents(self, query: str = "", filter: Optional[int] = None, parent: str = "", project: str = "",
                  offset: int = 0, limit: int = 20, sort: str = "modifiedAt") -> dict:
        """``GET /documents``: a page of documents, folders and projects (``items``, ``next``).
        filter: 0 my documents, 1 created, 2 shared, 4 public, 5 recent, 7 by company, 9 by team."""
        return self.get("/documents", {"q": query, "filter": filter, "parentId": parent, "project": project,
                                       "sortColumn": sort, "sortOrder": "desc", "offset": int(offset),
                                       "limit": max(1, min(20, int(limit)))})

    def document(self, did: str) -> dict:
        return self.get(f"/documents/{quote(did, safe='')}")

    def workspaces(self, did: str) -> list:
        return self.get(f"/documents/d/{quote(did, safe='')}/workspaces")

    def versions(self, did: str) -> list:
        return self.get(f"/documents/d/{quote(did, safe='')}/versions")

    def microversion(self, did: str, wvm: str, wvmid: str, refresh: bool = False) -> str:
        """The document microversion a workspace is at (a version and a microversion are
        themselves immutable); remembered for this client until ``refresh``."""
        if wvm in ("v", "m"):
            return f"{wvm}:{wvmid}"
        k = (did, wvm, wvmid)
        if refresh or k not in self._microversions:
            d = self.get(f"/documents/d/{quote(did, safe='')}/{wvm_path(wvm, wvmid)}/currentmicroversion")
            self._microversions[k] = "m:" + str((d or {}).get("microversion") or "")
        return self._microversions[k]

    def elements(self, did: str, wvm: str, wvmid: str, element_type: str = "ASSEMBLY") -> list:
        return self.get(f"/documents/d/{quote(did, safe='')}/{wvm_path(wvm, wvmid)}/elements",
                        {"elementType": element_type})

    def bom(self, did: str, wvm: str, wvmid: str, eid: str, configuration: str = "", mv: str = "",
            column_ids: Optional[list[str]] = None) -> dict:
        """The flattened BOM (``indented=false``) with every property column."""
        params: dict[str, Any] = {"indented": False, "onlyVisibleColumns": False, "configuration": configuration,
                                  "bomColumnIds": list(column_ids or [])}
        key = f"bom|{did}|{mv}|{eid}|{configuration}|{','.join(column_ids or [])}" if mv else None
        return self.get(f"/assemblies/d/{quote(did, safe='')}/{wvm_path(wvm, wvmid)}/e/{quote(eid, safe='')}/bom",
                        params, cache_key=key)

    def _part(self, src: dict, what: str, params: dict, mv: str) -> Any:
        did, wvm, wvmid, eid, pid = src["did"], src["wvm"], src["wvmid"], src["eid"], src["pid"]
        cfg = src.get("configuration") or ""
        params = {**params, "configuration": cfg, "linkDocumentId": src.get("link") or ""}
        key = f"{what}|{did}|{mv}|{eid}|{pid}|{cfg}|{json.dumps(params, sort_keys=True)}" if mv else None
        return self.get(f"/parts/d/{quote(did, safe='')}/{wvm_path(wvm, wvmid)}/e/{quote(eid, safe='')}"
                        f"/partid/{quote(pid, safe='')}/{what}", params, cache_key=key)

    def body_details(self, src: dict, mv: str = "") -> dict:
        return self._part(src, "bodydetails", {"includeGeometricData": True}, mv)

    def mass_properties(self, src: dict, mv: str = "") -> dict:
        return self._part(src, "massproperties", {}, mv)

    def tessellated_edges(self, src: dict, edge_ids: list[str], mv: str = "", chord_mm: float = 0.02) -> dict:
        return self._part(src, "tessellatededges", {"chordTolerance": chord_mm / 1000.0,
                                                    "edgeId": sorted(edge_ids)}, mv)

    def shaded_view(self, src: dict, mv: str = "", size: int = 64) -> Optional[bytes]:
        """A small isometric PNG of the part (``/shadedviews``), or None."""
        import base64
        d = self._part(src, "shadedviews", {"viewMatrix": "isometric", "outputHeight": size, "outputWidth": size,
                                            "pixelSize": 0}, mv)
        imgs = (d or {}).get("images") or []
        first = imgs[0] if imgs else None
        if isinstance(first, list):
            first = first[0] if first else None
        if not isinstance(first, str):
            return None
        try:
            return base64.b64decode(first)
        except ValueError:
            return None


def _message(resp: Any) -> str:
    try:
        d = resp.json()
        if isinstance(d, dict) and d.get("message"):
            return str(d["message"])[:300]
    except Exception:  # noqa: BLE001 - only the message text
        pass
    try:
        return str(resp.text)[:300]
    except Exception:  # noqa: BLE001
        return ""
