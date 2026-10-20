"""``ctx.http``: the HTTP client of plugin API 2, and the plugin manager's downloads.

Built on ``urllib`` (no new dependency; the frozen bundle ships it for the notifications).  The
rules (docs/dev/PLUGIN_STANDARD.md §5):

* **https only**; plain http only for ``localhost`` / ``127.0.0.1`` / ``::1``.  Another scheme, a
  URL with credentials in it, or http to another host raises :class:`InsecureURLError`
  (a ``PermissionError``).
* **host allowlist**: the host must be allowed (a plugin: ``permissions.network``, exact host or
  ``*.example.com``; a built-in: any) else :class:`HostNotAllowedError` (a ``PermissionError``).
* **redirects** are followed by this client, never by urllib, and only to allowed hosts (at most
  5); a redirect to another host drops ``Authorization`` and ``Cookie``.
* **size cap**: a body larger than ``max_bytes`` raises ``ValueError`` (``Content-Length``
  checked first, then while reading).
* **retries**: 429 / 502 / 503 / 504 are retried up to 3 times, honouring ``Retry-After``
  (seconds or an HTTP date), within 30 s of waiting in total; after that the last response is
  returned (its ``status`` tells).  Other statuses are returned, never raised.
* never on a machine thread (the streamer's watchdog, the safety monitor, the poll and job
  threads: ``RuntimeError``); callers keep it off the GUI thread (``ctx.run_in_background``).

Nothing here logs a header value, a query string or a body.
"""
from __future__ import annotations

import base64
import email.utils
import ipaddress
import json as _json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator, Mapping, Optional

log = logging.getLogger("mlaser.plugins.http")

DEFAULT_MAX_BYTES = 50_000_000
DEFAULT_TIMEOUT = 20.0
MAX_REDIRECTS = 5
RETRY_STATUSES = (429, 502, 503, 504)
MAX_RETRIES = 3
RETRY_BUDGET = 30.0
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
#: threads that drive the machine: a plugin's network call must never block them
MACHINE_THREAD_PREFIXES = ("mlaser-stream", "mlaser-safety", "mlaser-poll", "mlaser-job", "mlaser-stop",
                           "mlaser-pause")
_CHUNK = 64 * 1024


class InsecureURLError(PermissionError):
    """Not https (and not http to this computer), or credentials inside the URL."""


class HostNotAllowedError(PermissionError):
    """The host is not in the plugin's ``permissions.network`` (or the manager's policy)."""


def is_local_host(host: Optional[str]) -> bool:
    h = (host or "").lower().strip("[]")
    if h in LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def check_url(url: str) -> urllib.parse.SplitResult:
    """Parse and apply the scheme rule; returns the split URL."""
    if not isinstance(url, str) or len(url) > 8192:
        raise ValueError("invalid URL")
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if not parts.hostname:
        raise ValueError(f"URL without a host: {_redact(url)}")
    if parts.username or parts.password:
        raise InsecureURLError("credentials inside a URL are refused (use auth=)")
    if scheme == "https":
        return parts
    if scheme == "http" and is_local_host(parts.hostname):
        return parts
    if scheme == "http":
        raise InsecureURLError(f"http is allowed only to this computer; use https: {_redact(url)}")
    raise InsecureURLError(f"unsupported URL scheme {scheme!r}")


def _redact(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
        return f"{p.scheme}://{p.hostname or ''}{(':' + str(p.port)) if p.port else ''}{p.path}"
    except ValueError:
        return "<url>"


def _check_thread() -> None:
    name = threading.current_thread().name
    if name.startswith(MACHINE_THREAD_PREFIXES):
        raise RuntimeError(f"network requests are not allowed on the machine thread {name!r}")


class Headers(Mapping[str, str]):
    """Case-insensitive, read-only response headers (repeated headers joined with ``, ``)."""

    def __init__(self, items: Any = ()) -> None:
        self._d: dict[str, tuple[str, str]] = {}
        for k, v in items:
            low = str(k).lower()
            if low in self._d:
                self._d[low] = (self._d[low][0], self._d[low][1] + ", " + str(v))
            else:
                self._d[low] = (str(k), str(v))

    def __getitem__(self, key: str) -> str:
        return self._d[key.lower()][1]

    def __iter__(self) -> Iterator[str]:
        return (k for k, _ in self._d.values())

    def __len__(self) -> int:
        return len(self._d)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self._d

    def __repr__(self) -> str:
        return f"Headers({list(self)!r})"


class Response:
    def __init__(self, status: int, headers: Headers, content: bytes, url: str) -> None:
        self.status = int(status)
        self.headers = headers
        self.content = content
        self.url = url

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        ctype = self.headers.get("Content-Type", "")
        charset = "utf-8"
        for part in ctype.split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.lower() == "charset" and v:
                charset = v.strip('"')
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return _json.loads(self.content.decode("utf-8-sig"))

    def raise_for_status(self) -> "Response":
        if not self.ok:
            raise ConnectionError(f"HTTP {self.status} from {_redact(self.url)}")
        return self

    def __repr__(self) -> str:
        return f"<Response {self.status} {_redact(self.url)} {len(self.content)} bytes>"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                       # urllib raises HTTPError(3xx): the client decides


def _retry_after(value: Optional[str], now: float) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - now)


def default_opener() -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ctx))


class HttpClient:
    """See the module docstring.  ``allow_host(host) -> bool`` decides every host (the first one
    and each redirect target); ``redirect_ok(from_host, to_host)`` may widen the redirect rule
    (the plugin manager: GitHub's release-asset hosts)."""

    def __init__(self, allow_host: Callable[[str], bool], *, owner: str = "",
                 redirect_ok: Optional[Callable[[str, str], bool]] = None,
                 opener: Optional[urllib.request.OpenerDirector] = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 user_agent: str = "", max_redirects: int = MAX_REDIRECTS, retries: int = MAX_RETRIES,
                 retry_budget: float = RETRY_BUDGET) -> None:
        self.allow_host = allow_host
        self.owner = owner
        self.redirect_ok = redirect_ok
        self._opener = opener
        self.sleep, self.clock = sleep, clock
        if not user_agent:
            from .plugin_manifest import app_version
            user_agent = f"mlaser/{app_version()}" + (f" plugin/{owner}" if owner else "")
        self.user_agent = user_agent
        self.max_redirects, self.retries, self.retry_budget = max_redirects, retries, retry_budget

    @property
    def opener(self) -> urllib.request.OpenerDirector:
        if self._opener is None:
            self._opener = default_opener()
        return self._opener

    # ------------------------------------------------------------------ API
    def get(self, url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None,
            timeout: float = DEFAULT_TIMEOUT, max_bytes: int = DEFAULT_MAX_BYTES,
            auth: Optional[tuple[str, str]] = None) -> Response:
        return self.request("GET", url, params=params, headers=headers, timeout=timeout,
                            max_bytes=max_bytes, auth=auth)

    def post(self, url: str, *, json: Any = None, data: Any = None, headers: Optional[dict] = None,
             timeout: float = DEFAULT_TIMEOUT, max_bytes: int = DEFAULT_MAX_BYTES,
             auth: Optional[tuple[str, str]] = None) -> Response:
        return self.request("POST", url, json=json, data=data, headers=headers, timeout=timeout,
                            max_bytes=max_bytes, auth=auth)

    def _allowed(self, parts: urllib.parse.SplitResult, prev_host: Optional[str]) -> None:
        host = (parts.hostname or "").lower()
        if prev_host is not None and self.redirect_ok is not None and self.redirect_ok(prev_host, host):
            return
        if not self.allow_host(host):
            what = "redirect to" if prev_host is not None else "host"
            raise HostNotAllowedError(f"{what} {host!r} is not allowed"
                                      + (f" for plugin {self.owner!r} (permissions.network)" if self.owner else ""))

    def request(self, method: str, url: str, *, params: Optional[dict] = None, json: Any = None,
                data: Any = None, headers: Optional[dict] = None, timeout: float = DEFAULT_TIMEOUT,
                max_bytes: int = DEFAULT_MAX_BYTES, auth: Optional[tuple[str, str]] = None) -> Response:
        _check_thread()
        method = method.upper()
        if params:
            sep = "&" if urllib.parse.urlsplit(url).query else "?"
            url = url + sep + urllib.parse.urlencode(params, doseq=True)
        hdrs: dict[str, str] = {"User-Agent": self.user_agent, "Accept-Encoding": "identity"}
        body: Optional[bytes] = None
        if json is not None:
            body = _json.dumps(json).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif data is not None:
            if isinstance(data, dict):
                body = urllib.parse.urlencode(data, doseq=True).encode("ascii")
                hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            elif isinstance(data, str):
                body = data.encode("utf-8")
            else:
                body = bytes(data)
        for k, v in (headers or {}).items():
            hdrs[str(k)] = str(v)
        if auth is not None:
            user, secret = auth
            hdrs["Authorization"] = "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode("ascii")
        parts = check_url(url)
        self._allowed(parts, None)
        hops = attempt = 0
        waited = 0.0
        while True:
            req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
            try:
                resp: Any = self.opener.open(req, timeout=timeout)
                status = int(resp.status)
            except urllib.error.HTTPError as exc:
                resp, status = exc, int(exc.code)
            except urllib.error.URLError as exc:
                raise ConnectionError(f"{_redact(url)}: {exc.reason}") from None
            except (TimeoutError, OSError) as exc:
                raise ConnectionError(f"{_redact(url)}: {exc}") from None
            try:
                rh = Headers(resp.headers.items() if resp.headers is not None else ())
                if status in REDIRECT_STATUSES and "Location" in rh:
                    hops += 1
                    if hops > self.max_redirects:
                        raise ConnectionError(f"more than {self.max_redirects} redirects from {_redact(url)}")
                    new = urllib.parse.urljoin(url, rh["Location"])
                    old_host = (parts.hostname or "").lower()
                    parts = check_url(new)
                    self._allowed(parts, old_host)
                    if (parts.hostname or "").lower() != old_host:
                        for k in [k for k in hdrs if k.lower() in ("authorization", "cookie")]:
                            del hdrs[k]
                    if status == 303 or (status in (301, 302) and method not in ("GET", "HEAD")):
                        method, body = "GET", None
                        hdrs.pop("Content-Type", None)
                    url = new
                    continue
                content = _read_capped(resp, rh, max_bytes, url)
            finally:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
            if status in RETRY_STATUSES and attempt < self.retries:
                delay = _retry_after(rh.get("Retry-After"), time.time())
                if delay is None:
                    delay = float(2 ** attempt)
                if waited + delay <= self.retry_budget:
                    log.info("http %s %s: %d, retry in %.1f s", method, parts.hostname, status, delay)
                    self.sleep(delay)
                    waited += delay
                    attempt += 1
                    continue
            return Response(status, rh, content, url)


def _read_capped(resp: Any, headers: Headers, max_bytes: int, url: str) -> bytes:
    length = headers.get("Content-Length")
    if length and length.strip().isdigit() and int(length) > max_bytes:
        raise ValueError(f"{_redact(url)}: the response is {int(length)} bytes, more than the cap {max_bytes}")
    out = bytearray()
    while True:
        chunk = resp.read(_CHUNK)
        if not chunk:
            break
        out += chunk
        if len(out) > max_bytes:
            raise ValueError(f"{_redact(url)}: the response is larger than the cap {max_bytes} bytes")
    return bytes(out)


__all__ = ["HttpClient", "Response", "Headers", "InsecureURLError", "HostNotAllowedError", "check_url",
           "is_local_host", "DEFAULT_MAX_BYTES"]
