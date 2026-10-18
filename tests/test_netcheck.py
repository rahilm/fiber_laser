"""mlaser.netcheck: first-run network self-test (localhost sockets only, no card)."""
import json
import os
import socket
import threading
import time

import pytest

from mlaser import netcheck as N

pytestmark = pytest.mark.sim      # opens UDP sockets on localhost


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("MLASER_NO_FIRSTRUN", raising=False)
    monkeypatch.setenv("MLASER_APP_DIR", str(tmp_path / "app"))
    return tmp_path / "cfg"


@pytest.fixture
def echo_server():
    """UDP server on localhost that answers every datagram (stands in for the card)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(0.1)
    got = []
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                data, addr = s.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            got.append(data)
            s.sendto(b"\x00" * 8, addr)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    yield s.getsockname()[1], got
    stop.set()
    t.join(1)
    s.close()


def test_udp_socket_check():
    c = N.check_udp_socket("127.0.0.1")
    assert c.status == N.OK and "127.0.0.1:" in c.detail


def test_route_and_subnet_loopback():
    route, ip = N.check_route("127.0.0.1", 502)
    assert route.status == N.OK and ip == "127.0.0.1"
    assert N.check_subnet("127.0.0.1", ip).status == N.OK


def test_subnet_rules():
    assert N.check_subnet("10.1.1.168", "10.1.1.100").status == N.OK
    c = N.check_subnet("10.1.1.168", "192.168.1.5", local_addrs=["192.168.1.5"])
    assert c.status == N.WARN and "10.1.1.0/24" in c.detail and "10.1.1.100" in c.hint
    c = N.check_subnet("10.1.1.168", "192.168.1.5", local_addrs=["10.1.1.7"])
    assert c.status == N.WARN and "10.1.1.7" in c.detail
    assert N.check_subnet("card.local", "192.168.1.5").status == N.SKIP


def test_run_sends_nothing_without_probe(echo_server, cfgdir):
    port, got = echo_server
    res = N.run(host="127.0.0.1", port=port)
    time.sleep(0.2)
    assert got == []                                    # no traffic to the "card"
    assert res.ok and res.check("probe") is None
    assert [c.name for c in res.checks] == ["udp_socket", "route", "subnet", "firewall"]
    assert res.source == "command line" and res.local_ip == "127.0.0.1"


def test_probe_answered(echo_server, cfgdir):
    port, got = echo_server
    res = N.run(host="127.0.0.1", port=port, probe=True)
    assert res.check("probe").status == N.OK
    assert len(got) == 1                                # exactly one harmless read
    from mlaser.controller import nettest
    assert got[0] == nettest.mc_probe()


def test_probe_unanswered_is_warning(cfgdir):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))        # bound, never answers
    try:
        res = N.run(host="127.0.0.1", port=s.getsockname()[1], probe=True, timeout_s=0.2)
    finally:
        s.close()
    assert res.check("probe").status == N.WARN
    assert res.ok                                        # a silent card is not a host failure
    assert "WARN" in res.text()


def test_resolve_card_from_ini(tmp_path, cfgdir):
    ini = tmp_path / "ipAdd.ini"
    ini.write_bytes("[IP]\r\nCardIP=10.1.2.50\r\nCardPort=6000\r\n".encode("gbk"))
    assert N.resolve_card(ini=str(ini)) == ("10.1.2.50", 6000, str(ini))
    assert N.resolve_card() == (N.DEFAULT_HOST, N.DEFAULT_PORT, "default")
    # <app dir>/File/ipAdd.ini, like the app
    f = cfgdir.parent / "app" / "File"
    f.mkdir(parents=True)
    (f / "ipAdd.ini").write_bytes(ini.read_bytes())
    assert N.resolve_card()[:2] == ("10.1.2.50", 6000)
    assert N.resolve_card(host="127.0.0.1")[:2] == ("127.0.0.1", 502)


@pytest.mark.parametrize("plat,needle", [("win32", "New-NetFirewallRule"),
                                         ("darwin", "application firewall"),
                                         ("linux", "ufw allow from 10.1.1.0/24")])
def test_firewall_hints(plat, needle):
    c = N.firewall_hints(platform=plat, exe="/x/mlaser", frozen=True, which=lambda t: None)
    assert c.status == N.INFO and needle in c.hint and "/x/mlaser" in c.detail


def test_result_roundtrip(tmp_path):
    res = N.run(host="127.0.0.1", port=502)
    p = N.save_result(res, tmp_path / "r.json")
    back = N.last_result(p)
    assert back is not None and back.to_dict() == res.to_dict()
    assert json.loads(p.read_text())["ok"] is True
    assert N.last_result(tmp_path / "missing.json") is None


def test_first_run_once(cfgdir):
    lines = []
    t = N.first_run(report=lines.append, host="127.0.0.1", port=502)
    assert t is not None
    t.join(5)
    assert N.marker_path().is_file() and N.marker_path().parent == cfgdir
    assert lines and "network self-test" in lines[0]
    assert N.first_run(report=lines.append) is None      # second start: nothing
    assert N.last_result().card_host == "127.0.0.1"


def test_first_run_never_blocks(cfgdir, monkeypatch):
    def slow(**kw):
        time.sleep(3)
        return N.run(host="127.0.0.1", port=502)
    monkeypatch.setattr(N, "run", slow)
    t0 = time.perf_counter()
    t = N.first_run(report=lambda s: None, wait_s=0.2)
    assert time.perf_counter() - t0 < 1.0
    assert t is not None and t.daemon


def test_first_run_disabled(cfgdir, monkeypatch):
    monkeypatch.setenv("MLASER_NO_FIRSTRUN", "1")
    assert N.first_run(report=lambda s: None) is None
    assert not N.marker_path().exists()


def test_user_config_dir_fallbacks(monkeypatch, tmp_path):
    monkeypatch.delenv("MLASER_CONFIG_DIR", raising=False)
    assert N.user_config_dir().name == "mlaser"
    monkeypatch.setenv("MLASER_CONFIG_DIR", str(tmp_path))
    assert N.user_config_dir() == tmp_path


def test_main_exit_codes(cfgdir, capsys, monkeypatch):
    assert N.main(["--host", "127.0.0.1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    monkeypatch.setattr(N, "check_udp_socket", lambda *a: N.Check("udp_socket", N.FAIL, "blocked"))
    assert N.main(["--host", "127.0.0.1"]) == 1
    assert "FAILED" in capsys.readouterr().out


def test_qt_free():
    import subprocess
    import sys
    code = ("import sys, mlaser.netcheck as n; n.run(host='127.0.0.1', port=502); "
            "sys.exit(any(m.startswith(('PySide6', 'shiboken6')) for m in sys.modules))")
    env = dict(os.environ, MLASER_CONFIG_DIR=os.devnull)
    assert subprocess.run([sys.executable, "-c", code], env=env).returncode == 0
