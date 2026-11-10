"""Update notice: is a newer mlaser release published?  Opt-in, never downloads (review F11).

Help › Check for updates asks once; the setting ``plugin.ext_diag.update_check`` (Settings ›
Network & API › Updates, **off** by default, never asked for automatically) also asks once
at start.  The answer is a notice with a link to the release page.  Nothing is downloaded or
installed: a cutter controller does not replace itself.

The request (docs/user/settings.md, SECURITY.md): one HTTPS ``GET`` of
:data:`API_URL` (the GitHub releases API of the project's own repository) with the headers
``User-Agent: mlaser-update-check`` (no version, no OS, no identifier), ``Accept:
application/vnd.github+json`` and ``Accept-Encoding: identity``; no cookie, no token, no query
other than ``per_page``.  Redirects only to ``api.github.com``; :data:`TIMEOUT` seconds; at most
:data:`MAX_BYTES` read; no retry.  Off the GUI thread (the caller runs :func:`check` in a worker).
A failure is written to the log at INFO and otherwise ignored.

The versions are compared as PEP 440 (:mod:`mlaser.services.pep440`); pre-releases count only
while the running version is itself a pre-release.  The link shown is the release's own page,
accepted only under :data:`RELEASES_PAGE`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .pep440 import InvalidVersion, Version

log = logging.getLogger("mlaser.update_check")

REPOSITORY = "rahilm/fiber_laser"
API_HOST = "api.github.com"
API_URL = f"https://{API_HOST}/repos/{REPOSITORY}/releases?per_page=20"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases"
USER_AGENT = "mlaser-update-check"
HEADERS = {"Accept": "application/vnd.github+json"}
TIMEOUT = 10.0
MAX_BYTES = 2_000_000
#: the setting (plugin ext_diag's namespace): check once at start
SETTING = "update_check"


@dataclass
class UpdateResult:
    current: str
    latest: Optional[str] = None       # newest applicable release, None when none was found
    url: str = RELEASES_PAGE           # its page (under RELEASES_PAGE)
    newer: bool = False                # latest > current
    error: str = ""                    # why the check failed ("" = it worked)

    @property
    def ok(self) -> bool:
        return not self.error


def _version(text: str) -> Optional[Version]:
    t = (text or "").strip()
    if t[:1] in ("v", "V"):
        t = t[1:]
    try:
        return Version.parse(t)
    except InvalidVersion:
        return None


def _prerelease(v: Version) -> bool:
    return v.pre is not None or v.dev is not None


def newest(releases: Any, current: str) -> UpdateResult:
    """The newest applicable release of the API's answer (a list of release objects)."""
    cur = _version(current)
    res = UpdateResult(current)
    if cur is None:
        res.error = f"the running version {current!r} is not a PEP 440 version"
        return res
    if not isinstance(releases, list):
        res.error = "unexpected answer"
        return res
    best: Optional[tuple[Version, str, str]] = None
    for r in releases:
        if not isinstance(r, dict) or r.get("draft"):
            continue
        tag = str(r.get("tag_name") or "")
        v = _version(tag)
        if v is None:
            continue
        if (bool(r.get("prerelease")) or _prerelease(v)) and not _prerelease(cur):
            continue
        if best is None or v > best[0]:
            url = str(r.get("html_url") or "")
            best = (v, tag, url if url.startswith(RELEASES_PAGE + "/") else RELEASES_PAGE)
    if best is not None:
        res.latest, res.url = best[1].lstrip("vV"), best[2]
        res.newer = best[0] > cur
    return res


def fetch_releases(timeout: float = TIMEOUT, opener: Any = None) -> Any:
    """The one GET described in the module docstring (raises on failure).  ``opener``: a
    urllib opener (tests)."""
    from .plugin_http import HttpClient
    client = HttpClient(lambda host: host == API_HOST, user_agent=USER_AGENT, retries=0, max_redirects=2,
                        opener=opener)
    resp = client.get(API_URL, headers=dict(HEADERS), timeout=timeout, max_bytes=MAX_BYTES)
    if resp.status != 200:
        raise OSError(f"HTTP {resp.status}")
    return json.loads(resp.content.decode("utf-8"))


def check(current: Optional[str] = None, fetch: Optional[Callable[[], Any]] = None) -> UpdateResult:
    """Ask once; never raises (a failure is logged at INFO and returned in ``error``)."""
    if current is None:
        from mlaser._buildid import build_identity
        current = build_identity().version
    try:
        data = (fetch or fetch_releases)()
    except Exception as e:  # noqa: BLE001 - offline, proxy, rate limit: silent, in the log only
        log.info("update check: no answer (%s: %s)", type(e).__name__, e)
        return UpdateResult(current, error=f"{type(e).__name__}: {e}")
    res = newest(data, current)
    if res.error:
        log.info("update check: %s", res.error)
    else:
        log.info("update check: running %s, newest release %s%s", current, res.latest or "none",
                 " (newer)" if res.newer else "")
    return res


__all__ = ["check", "newest", "fetch_releases", "UpdateResult", "API_URL", "RELEASES_PAGE", "USER_AGENT",
           "HEADERS", "SETTING", "REPOSITORY"]
