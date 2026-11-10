"""Update notice (review F11 item 4): opt-in, one plain GET, compare, never download."""
from __future__ import annotations

import io
import json
import logging

import pytest

from mlaser.services import update_check as U


def _rel(tag, pre=False, draft=False, url=None):
    return {"tag_name": tag, "prerelease": pre, "draft": draft,
            "html_url": url or f"{U.RELEASES_PAGE}/tag/{tag}"}


def test_newer_release_is_found_and_linked():
    r = U.newest([_rel("v0.1.0"), _rel("v0.2.0"), _rel("v0.1.5")], "0.1.0")
    assert r.ok and r.newer and r.latest == "0.2.0" and r.url == f"{U.RELEASES_PAGE}/tag/v0.2.0"


def test_same_or_older_is_not_newer():
    assert not U.newest([_rel("v0.1.0")], "0.1.0").newer
    assert not U.newest([_rel("v0.0.9")], "0.1.0").newer
    assert U.newest([], "0.1.0").latest is None


def test_prereleases_count_only_while_running_one():
    rels = [_rel("v0.1.0rc2", pre=True), _rel("v0.0.9")]
    assert U.newest(rels, "0.1.0rc1").latest == "0.1.0rc2"       # rc1 -> rc2 is offered
    assert not U.newest(rels, "0.1.0").newer                      # a final release ignores rcs
    assert U.newest([_rel("v0.1.0")], "0.1.0rc1").newer           # rc -> final


def test_drafts_bad_tags_and_foreign_links_are_ignored():
    rels = [_rel("v9.0.0", draft=True), {"tag_name": "nightly"}, "junk",
            _rel("v0.3.0", url="https://evil.example/download.exe")]
    r = U.newest(rels, "0.1.0")
    assert r.latest == "0.3.0" and r.url == U.RELEASES_PAGE      # only links under the releases page
    assert U.newest({"message": "rate limited"}, "0.1.0").error


def test_failure_is_silent_and_logged(caplog):
    def boom():
        raise ConnectionError("offline")
    with caplog.at_level(logging.INFO, logger="mlaser.update_check"):
        r = U.check("0.1.0", fetch=boom)
    assert not r.ok and not r.newer and "offline" in r.error
    assert any("update check" in rec.message and rec.levelno == logging.INFO for rec in caplog.records)


class _Resp:
    def __init__(self, body: bytes, status=200, headers=None):
        self.status, self._b = status, io.BytesIO(body)
        self.headers = _H(headers or {"Content-Type": "application/json"})

    def read(self, n=-1):
        return self._b.read(n)

    def close(self):
        pass


class _H(dict):
    def items(self):
        return list(super().items())


class _Opener:
    def __init__(self, resp):
        self.resp, self.requests = resp, []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        return self.resp


def test_the_request_is_one_plain_get_without_identifiers():
    op = _Opener(_Resp(json.dumps([_rel("v0.2.0")]).encode()))
    data = U.fetch_releases(opener=op)
    assert data[0]["tag_name"] == "v0.2.0"
    assert len(op.requests) == 1                                   # no retry, no second request
    req, timeout = op.requests[0]
    assert req.get_method() == "GET" and req.full_url == U.API_URL
    assert req.full_url.startswith("https://api.github.com/repos/rahilm/fiber_laser/releases")
    hdrs = {k.lower(): v for k, v in req.header_items()}
    assert hdrs == {"user-agent": "mlaser-update-check", "accept": "application/vnd.github+json",
                    "accept-encoding": "identity"}
    assert req.data is None and timeout == U.TIMEOUT


def test_redirect_elsewhere_is_refused():
    op = _Opener(_Resp(b"", status=302, headers={"Location": "https://elsewhere.example/x"}))
    with pytest.raises(PermissionError):
        U.fetch_releases(opener=op)


def test_nothing_is_downloaded_or_installed():
    # the module has no code path that writes a file or runs anything
    import inspect
    src = inspect.getsource(U)
    for word in ("open(", "subprocess", "urlretrieve", "pip", "os.replace", "write_bytes"):
        assert word not in src.replace("opener", ""), word


def test_setting_is_opt_in_and_documented():
    from pathlib import Path

    from mlaser.services import settings_catalog as C
    from mlaser.ui import ext_diag
    assert ext_diag.UPDATE_SETTING == U.SETTING
    assert "plugin.ext_diag.update_check" in C.BY_KEY
    doc = (Path(__file__).resolve().parents[1] / "docs/user/settings.md").read_text("utf-8")
    assert "`plugin.ext_diag.update_check` | off" in doc and "mlaser-update-check" in doc
    sec = (Path(__file__).resolve().parents[1] / "SECURITY.md").read_text("utf-8")
    assert U.API_URL in sec and U.USER_AGENT in sec
