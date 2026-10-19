"""LAN monitor (mlaser.web): read-only (no state-changing endpoints), token auth, binding."""
from __future__ import annotations

import http.client
import json
import re

import pytest

from mlaser.model import Arc, Contour, Document, Line, Part
from mlaser.web import MonitorServer, new_token, sheet_svg
from mlaser.web.page import LABEL_IDS, render_page


@pytest.fixture
def server():
    # the rate limit is tested in test_web_hardening.py; these tests send bursts of requests
    s = MonitorServer("127.0.0.1", 0, new_token(), rate_burst=1000).start()
    s.publish({"state": "RUNNING", "kind": "running", "progress": 0.5, "job": "flange.chf",
               "alarms": [{"text": "X servo", "remedy": "check the drive"}], "queue": ["a 1/2"]},
              "<svg xmlns='http://www.w3.org/2000/svg'/>")
    yield s
    s.stop()


def _req(s, method, path, body=None):
    h, p = s.address
    c = http.client.HTTPConnection(h, p, timeout=5)
    try:
        c.request(method, path, body=body)
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        c.close()


def test_token_required(server):
    t = server.token
    assert _req(server, "GET", "/")[0] == 403
    assert _req(server, "GET", "/status.json")[0] == 403
    assert _req(server, "GET", "/status.json?t=wrong" + t[5:])[0] == 403
    assert _req(server, "GET", "/sheet.svg?t=")[0] == 403
    code, hdr, body = _req(server, "GET", f"/status.json?t={t}")
    assert code == 200 and hdr["Cache-Control"] == "no-store"
    snap = json.loads(body)
    assert snap["state"] == "RUNNING" and snap["published"] > 0
    assert _req(server, "GET", "/health")[2] == b"ok\n"            # carries no data
    assert server.refused == 4


def test_no_state_changing_endpoints(server):
    t = server.token
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
        for path in ("/", f"/?t={t}", f"/status.json?t={t}", f"/start?t={t}", f"/stop?t={t}"):
            code, hdr, _ = _req(server, method, path, body=b"{}")
            assert code == 405, (method, path)
            assert hdr.get("Allow") == "GET, HEAD"
    for path in ("/start", "/stop", "/hold", "/api/run", "/../etc/passwd"):
        assert _req(server, "GET", f"{path}?t={t}")[0] == 404
    # HEAD works and sends no body
    code, hdr, body = _req(server, "HEAD", f"/status.json?t={t}")
    assert code == 200 and body == b""


def test_the_server_holds_no_controller():
    s = MonitorServer("127.0.0.1", 0, new_token())
    # only data: the published snapshot and the svg; nothing callable that acts on a machine
    assert not [k for k, v in vars(s).items() if callable(v) and not k.startswith("_")]


def test_page_is_self_contained_and_has_no_controls(server):
    code, hdr, body = _req(server, "GET", f"/?t={server.token}")
    page = body.decode()
    assert code == 200 and "text/html" in hdr["Content-Type"]
    csp = hdr["Content-Security-Policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'<script nonce="{nonce}">' in page and "form-action 'none'" in csp
    assert "<form" not in page and "<button" not in page and "<input" not in page
    assert "method:'POST'" not in page and "http://" not in page and "https://" not in page
    assert 'name="viewport"' in page and hdr["Referrer-Policy"] == "no-referrer"
    assert hdr["X-Frame-Options"] == "DENY"


def test_svg_endpoint(server):
    code, hdr, body = _req(server, "GET", f"/sheet.svg?t={server.token}")
    assert code == 200 and hdr["Content-Type"].startswith("image/svg+xml") and body.startswith(b"<svg")


def test_token_and_binding_rules():
    with pytest.raises(ValueError):
        MonitorServer("127.0.0.1", 0, "short")
    s = MonitorServer("127.0.0.1", 0, new_token()).start()
    try:
        assert s.address[0] == "127.0.0.1"          # bound to the configured interface only
        assert s.url().startswith("http://127.0.0.1:") and s.token in s.url()
    finally:
        s.stop()
    assert not s.running


def test_labels_are_catalogue_texts():
    from mlaser.i18n import catalogue
    cat = catalogue("en")
    for k in LABEL_IDS:
        assert k in cat.strings, k
    page = render_page({"state": "<b>x</b>", "labels": {"web.title": "Mon <i>"}}, "tok", "n")
    assert "<b>x</b>" not in page and "Mon &lt;i&gt;" in page


def test_sheet_svg_marks_parts():
    sq = lambda x: Contour([Line((x, 0), (x + 10, 0)), Line((x + 10, 0), (x + 10, 10)),
                            Line((x + 10, 10), (x, 10)), Line((x, 10), (x, 0))], True)
    circ = Contour([Arc((50, 5), (50, 5), (45, 5), True)], True)
    doc = Document(parts=[Part([sq(0)]), Part([sq(20)]), Part([circ])])
    svg = sheet_svg(doc, done=[0], current=1, head=(25.0, 5.0), sheet=(0, 0, 100, 50))
    assert svg.count('class="done"') == 1 and svg.count('class="cur"') == 1 and svg.count('class="todo"') == 1
    assert 'class="head"' in svg and 'class="sheet"' in svg and "<script" not in svg
