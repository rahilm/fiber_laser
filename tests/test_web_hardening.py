"""LAN monitor hardening (code review P1-4, P2-4, P2-5; performance review P1-7).

The monitor runs in the safety process: idle, half-open or slow LAN clients must not exhaust its
threads or file descriptors, one client must not take every slot, a client is rate limited, the
token never reaches the log, and the sheet SVG is rebuilt only when it changes.
"""
from __future__ import annotations

import http.client
import logging
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from mlaser.model import Contour, Document, Line, Part
from mlaser.web import MonitorServer, new_token, sheet_svg
from mlaser.web.page import render_page
from mlaser.web.snapshot import SheetSvgCache

LINUX_PROC = os.path.isdir("/proc/self/fd")
# socket timeouts and thread counts: with the safety / sim timing tests on one xdist worker
# (tests/conftest.py, TIMING_GROUP), not next to 60 busy workers
pytestmark = pytest.mark.xdist_group("timing")


def _get(s, path, timeout=5.0):
    h, p = s.address
    c = http.client.HTTPConnection(h, p, timeout=timeout)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        c.close()


def _fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def _wait(cond, timeout=5.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


@pytest.mark.skipif(not LINUX_PROC, reason="counts file descriptors in /proc")
def test_idle_clients_cannot_exhaust_threads_or_fds():
    s = MonitorServer("127.0.0.1", 0, new_token(), timeout_s=2.0, max_connection_s=10.0,
                      max_connections=8, max_per_client=100).start()
    socks = []
    try:
        threads0, fds0 = threading.active_count(), _fds()
        for _ in range(200):                      # half-open requests, never finished
            c = socket.create_connection(s.address, timeout=2)
            c.sendall(b"GET /status.json?t=")
            socks.append(c)
        assert _wait(lambda: s.rejected >= 200 - 8)
        # at most the cap of handler threads, at most the cap of server-side sockets
        assert threading.active_count() - threads0 <= 8
        assert _fds() - fds0 - len(socks) <= 8 + 2
        # the over-cap connections were told so and closed
        rejected = 0
        for c in socks:
            c.settimeout(0.05)
            try:
                if c.recv(64).startswith(b"HTTP/1.1 503"):
                    rejected += 1
            except OSError:
                pass
        assert rejected >= 200 - 8 - 2
        # the idle ones time out: a real client is served again, new sockets and files open fine
        assert _wait(lambda: s.connections == 0, 6.0)
        assert _get(s, f"/status.json?t={s.token}")[0] == 200
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.close()
        with open(os.devnull, "rb"):
            pass
    finally:
        for c in socks:
            c.close()
        s.stop()


def test_slow_client_is_closed_at_the_connection_lifetime():
    s = MonitorServer("127.0.0.1", 0, new_token(), timeout_s=0.6, max_connection_s=1.0).start()
    c = socket.create_connection(s.address, timeout=3)
    try:
        t0 = time.monotonic()
        closed = False
        for ch in b"GET /status.json?t=" + s.token.encode():     # one byte every 0.2 s
            try:
                c.sendall(bytes([ch]))
            except OSError:
                closed = True
                break
            c.settimeout(0.2)
            try:
                if c.recv(64) == b"":
                    closed = True
                    break
            except socket.timeout:
                pass
            except OSError:
                closed = True
                break
            if time.monotonic() - t0 > 4.0:
                break
        assert closed and time.monotonic() - t0 < 3.0
        assert _wait(lambda: s.connections == 0, 2.0) and s.reaped >= 1
    finally:
        c.close()
        s.stop()


def test_one_client_cannot_take_every_slot():
    s = MonitorServer("127.0.0.1", 0, new_token(), timeout_s=2.0, max_connections=16, max_per_client=2).start()
    socks = []
    try:
        for _ in range(4):
            c = socket.create_connection(s.address, timeout=2)
            c.sendall(b"GET /")
            socks.append(c)
        assert _wait(lambda: s.rejected >= 2)
        assert s.connections == 2
    finally:
        for c in socks:
            c.close()
        s.stop()


def test_rate_limit_per_client():
    s = MonitorServer("127.0.0.1", 0, new_token(), rate_per_s=1.0, rate_burst=3).start()
    try:
        codes = [_get(s, f"/status.json?t={s.token}") for _ in range(6)]
        assert [c[0] for c in codes[:3]] == [200, 200, 200]
        limited = [c for c in codes[3:] if c[0] == 429]
        assert limited and limited[0][1].get("Retry-After") == "1"
        assert s.limited >= 1
        time.sleep(1.1)                           # the bucket refills
        assert _get(s, f"/status.json?t={s.token}")[0] == 200
    finally:
        s.stop()


def test_token_is_not_logged(caplog):
    s = MonitorServer("127.0.0.1", 0, new_token()).start()
    try:
        with caplog.at_level(logging.DEBUG, logger="mlaser.web"):
            assert _get(s, f"/status.json?t={s.token}")[0] == 200
            assert _get(s, f"/nothing?t={s.token}")[0] == 404
            c = socket.create_connection(s.address, timeout=2)   # a malformed request line
            c.sendall(f"GET /?t={s.token} HTTP/9 x\r\n\r\n".encode())
            c.recv(256)
            c.close()
            time.sleep(0.2)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "/status.json" in text and s.token not in text
    finally:
        s.stop()


def test_page_script_cannot_be_closed_by_a_label():
    evil = "</script><script>alert(1)</script><!--"
    page = render_page({"labels": {"web.title": evil, "web.offline": evil}}, "tok", "n")
    body = page.split('<script nonce="n">', 1)[1]
    assert body.count("</script>") == 1                 # only the page's own closing tag
    assert "<!--" not in body and "\\u003c/script>" in body


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_NOFILE and /proc")
def test_fd_exhaustion_probe_with_a_low_limit(tmp_path):
    """The review's probe (work/review2/probes/lan_fd_exhaust.py): RLIMIT_NOFILE 256, a LAN peer
    opens 300 idle connections; the process must still open sockets and files."""
    script = textwrap.dedent("""
        import multiprocessing as mp, os, resource, socket, sys, time
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
        from mlaser.web.server import MonitorServer, new_token

        def attacker(port, n, q):
            ss = []
            for _ in range(n):
                try:
                    s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                    s.sendall(b"GET /")
                    ss.append(s)
                except OSError:
                    pass
            q.put(len(ss))
            time.sleep(3)

        if __name__ == "__main__":
            ms = MonitorServer("127.0.0.1", 0, new_token()).start()
            q = mp.get_context("spawn").Queue()
            p = mp.get_context("spawn").Process(target=attacker, args=(ms.address[1], 300, q))
            p.start()
            n = q.get(timeout=60)
            time.sleep(0.5)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.close()
            open(os.devnull).close()
            print("OK", n, ms.connections, ms.rejected, flush=True)
            p.kill()
            os._exit(0)
    """)
    f = tmp_path / "probe.py"
    f.write_text(script)
    r = subprocess.run([sys.executable, str(f)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    ok, n, conns, rejected = r.stdout.split()
    assert ok == "OK" and int(n) >= 200 and int(conns) <= 16 and int(rejected) >= 150


# --------------------------------------------------------------------------- sheet SVG cache
def _sq(x, y=0.0):
    return Contour([Line((x, y), (x + 10, y)), Line((x + 10, y), (x + 10, y + 10)),
                    Line((x + 10, y + 10), (x, y + 10)), Line((x, y + 10), (x, y))], True)


def _doc(n=6):
    return Document(parts=[Part([_sq(20.0 * k)]) for k in range(n)])


def test_svg_cache_matches_sheet_svg_and_reuses():
    doc = _doc()
    cache = SheetSvgCache()
    a = cache.render(doc, done=[0, 1], current=2, head=(5.0, 5.0), sheet=(0, 0, 200, 50))
    assert a == sheet_svg(doc, done=[0, 1], current=2, head=(5.0, 5.0), sheet=(0, 0, 200, 50))
    assert cache.builds == 1
    b = cache.render(doc, done=[1, 0], current=2, head=(5.0, 5.0), sheet=(0, 0, 200, 50))
    assert b is a and cache.builds == 1                     # nothing changed: the same text
    c = cache.render(doc, done=[0, 1], current=2, head=(6.0, 5.0), sheet=(0, 0, 200, 50))
    assert cache.builds == 1 and c == sheet_svg(doc, [0, 1], 2, (6.0, 5.0), sheet=(0, 0, 200, 50))
    d = cache.render(doc, done=[0, 1, 2], current=3, head=(6.0, 5.0), sheet=(0, 0, 200, 50))
    assert cache.builds == 2 and d == sheet_svg(doc, [0, 1, 2], 3, (6.0, 5.0), sheet=(0, 0, 200, 50))
    misses = cache.misses
    cache.render(doc, done=[0, 1, 2, 3], current=4, head=(6.0, 5.0))
    assert cache.misses == misses                          # no part re-flattened for progress


def test_svg_cache_sees_an_edit_and_a_new_document():
    doc = _doc()
    cache = SheetSvgCache(verify_s=0.0)
    a = cache.render(doc)
    doc.parts[3].contours = [_sq(60.0, 30.0)]             # an edit replaces the segments
    b = cache.render(doc)
    assert b != a and b == sheet_svg(doc) and cache.builds == 2
    doc.parts.append(Part([_sq(200.0)]))
    assert cache.render(doc) == sheet_svg(doc) and cache.builds == 3
    other = _doc(2)
    assert cache.render(other) == sheet_svg(other)


def test_svg_cache_verifies_edits_at_most_every_verify_s():
    doc = _doc()
    cache = SheetSvgCache(verify_s=0.3)
    a = cache.render(doc)
    doc.parts[0].contours = [_sq(0.0, 40.0)]
    assert cache.render(doc) is a                           # within verify_s: not re-checked
    time.sleep(0.35)
    assert cache.render(doc) == sheet_svg(doc)


def test_svg_cache_is_cheap_between_changes():
    doc = Document(parts=[Part([_sq(12.0 * (k % 100), 12.0 * (k // 100))]) for k in range(3000)])
    cache = SheetSvgCache()
    t0 = time.perf_counter()
    cache.render(doc, done=range(100), current=100, head=(1.0, 1.0))
    first = time.perf_counter() - t0
    t0 = time.perf_counter()
    for k in range(20):
        cache.render(doc, done=range(100), current=100, head=(1.0 + k, 1.0))
    again = (time.perf_counter() - t0) / 20
    assert cache.builds == 1 and again < first / 5
