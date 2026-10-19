"""First-run network and firewall self-test (Qt-free, standard library only).

The MCC100 card is reached over plain UDP (``mlaser.controller.client``: ``socket(AF_INET,
SOCK_DGRAM)``, no ``bind``, ``sendto CardIP:CardPort``, default 10.1.1.168:502).  What usually
goes wrong on a new PC is not the protocol but the host: no network card on the 10.1.1.x subnet,
or a firewall that drops the card's replies.  This module checks, without talking to the card
unless asked:

1. ``udp_socket``: a UDP socket can be created and bound to an ephemeral local port (what the
   OS does implicitly on the client's first ``sendto``);
2. ``route``: the OS has a route to the card address.  A UDP ``connect()`` sends no packet; it
   only selects the outgoing interface, whose address ``getsockname()`` returns;
3. ``subnet``: that interface (or another local address) is on the card's subnet (/24 by
   default, 10.1.1.0/24), so traffic does not go out through the default gateway;
4. ``probe`` (optional, ``probe=True``): one harmless status read (func 0x30 of RO[0..1], the
   frame of the original's checkMCStatus, :func:`mlaser.controller.nettest.mc_probe`); any reply
   counts as reachable;
5. ``firewall``: per-OS hints (Windows Defender Firewall, macOS application firewall,
   Linux ufw/firewalld), never a pass/fail.

The card address is read the way the app reads it: ``--ini`` (ipAdd.ini, ``CardIP``/``CardPort``
via :meth:`mlaser.controller.client.TransportConfig.from_ini`), else ``--host``/``--port``, else
``<app dir>/File/ipAdd.ini`` when it exists (``$MLASER_APP_DIR`` or ``~/.mlaser``, as
:func:`mlaser.controller.machine_ops.default_app_dir`), else the default 10.1.1.168:502.

First run (:func:`first_run`): a marker file in the per-user config directory
(:func:`user_config_dir`) records that the check ran and holds the last result as JSON
(``netcheck.json``), so a GUI can show it later (:func:`last_result`).

``python -m mlaser --selftest [--probe] [--host H --port P | --ini ipAdd.ini] [--json]``.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

DEFAULT_HOST = "10.1.1.168"   # File/ipAdd.ini CardIP
DEFAULT_PORT = 502            # CardPort
APP_NAME = "mlaser"
MARKER_NAME = "netcheck.json"

OK, WARN, FAIL, INFO, SKIP = "ok", "warn", "fail", "info", "skip"


@dataclass
class Check:
    name: str
    status: str            # ok / warn / fail / info / skip
    detail: str
    hint: str = ""


@dataclass
class NetCheckResult:
    card_host: str
    card_port: int
    source: str                     # where the address came from (ini path, args, default)
    checks: list[Check] = field(default_factory=list)
    local_ip: str = ""              # interface address the OS would use towards the card
    platform: str = sys.platform
    frozen: bool = False
    duration_ms: float = 0.0
    timestamp: float = 0.0

    @property
    def ok(self) -> bool:
        """No check failed (warnings allowed)."""
        return all(c.status != FAIL for c in self.checks)

    def check(self, name: str) -> Optional[Check]:
        return next((c for c in self.checks if c.name == name), None)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "NetCheckResult":
        d = dict(d)
        d.pop("ok", None)
        checks = [Check(**c) for c in d.pop("checks", [])]
        return cls(checks=checks, **d)

    def text(self) -> str:
        lines = [f"mlaser network self-test: card {self.card_host}:{self.card_port} ({self.source})"]
        for c in self.checks:
            lines.append(f"  [{c.status.upper():4}] {c.name}: {c.detail}")
            if c.hint:
                for h in c.hint.splitlines():
                    lines.append(f"         {h}")
        lines.append(f"  result: {'OK' if self.ok else 'FAILED'}"
                     f"{' (with warnings)' if self.ok and any(c.status == WARN for c in self.checks) else ''}"
                     f", {self.duration_ms:.0f} ms")
        return "\n".join(lines)


# ---------------------------------------------------------------------------- configuration

def default_app_dir() -> Path:
    """Same rule as :func:`mlaser.controller.machine_ops.default_app_dir` (not imported: that
    module pulls in the machine layer)."""
    return Path(os.environ.get("MLASER_APP_DIR") or (Path.home() / ".mlaser"))


def resolve_card(host: Optional[str] = None, port: Optional[int] = None,
                 ini: Optional[str] = None) -> tuple[str, int, str]:
    """(host, port, source) of the motion card, read the way the app does."""
    if ini:
        return (*_from_ini(ini), ini)
    if host or port:
        return host or DEFAULT_HOST, int(port or DEFAULT_PORT), "command line"
    default_ini = default_app_dir() / "File" / "ipAdd.ini"
    if default_ini.is_file():
        try:
            return (*_from_ini(str(default_ini)), str(default_ini))
        except (OSError, ValueError):
            pass
    return DEFAULT_HOST, DEFAULT_PORT, "default"


def _from_ini(path: str) -> tuple[str, int]:
    from mlaser.controller.client import TransportConfig   # ipAdd.ini reader of the app
    cfg = TransportConfig.from_ini(path)
    return cfg.host, cfg.port


# ---------------------------------------------------------------------------- checks

def check_udp_socket(bind_host: str = "0.0.0.0") -> Check:
    """Create a UDP socket and bind it to an ephemeral port (the card client does not bind; the
    OS picks the port on the first ``sendto``)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as e:
        return Check("udp_socket", FAIL, f"cannot create a UDP socket: {e}",
                     "Security software may block the program from using the network.")
    try:
        s.bind((bind_host, 0))
        port = s.getsockname()[1]
    except OSError as e:
        return Check("udp_socket", FAIL, f"cannot bind a UDP socket on {bind_host}: {e}")
    finally:
        s.close()
    return Check("udp_socket", OK, f"UDP socket bound to {bind_host}:{port}")


def route_source(host: str, port: int) -> str:
    """Local address the OS would send from towards ``host:port`` (no packet is sent).
    Raises OSError when there is no route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, port))
        return s.getsockname()[0]
    finally:
        s.close()


def local_ipv4_addresses() -> list[str]:
    """Best-effort list of local IPv4 addresses (stdlib only: host name lookup)."""
    out: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        ip = str(info[4][0])
        if ip not in out:
            out.append(ip)
    return out


def check_route(host: str, port: int) -> tuple[Check, str]:
    try:
        ip = route_source(host, port)
    except OSError as e:
        return Check("route", FAIL, f"no route to {host}: {e}",
                     "Connect the network cable to the card and give that network card an address "
                     "on the card's subnet."), ""
    return Check("route", OK, f"{host} is routed via local address {ip}"), ip


def check_subnet(host: str, local_ip: str, prefix: int = 24,
                 local_addrs: Optional[list[str]] = None) -> Check:
    try:
        card = ipaddress.ip_address(host)
    except ValueError:
        return Check("subnet", SKIP, f"{host} is a host name, subnet not checked")
    if card.is_loopback:
        return Check("subnet", OK, f"{host} is this computer (simulator)")
    net = ipaddress.ip_network(f"{host}/{prefix}", strict=False)
    if local_ip and ipaddress.ip_address(local_ip) in net:
        return Check("subnet", OK, f"local address {local_ip} is on {net}")
    others = [a for a in (local_addrs if local_addrs is not None else local_ipv4_addresses())
              if ipaddress.ip_address(a) in net]
    if others:
        return Check("subnet", WARN, f"{', '.join(others)} is on {net} but the route to {host} uses "
                     f"{local_ip or 'another interface'}",
                     "Check the network card settings: two interfaces may share that subnet.")
    example = str(net.network_address + (100 if int(card) & 0xFF != 100 else 101))
    return Check("subnet", WARN,
                 f"no local interface on {net} (traffic to {host} would leave via "
                 f"{local_ip or 'the default gateway'})",
                 f"Give the network card wired to the laser a static address such as {example}, "
                 f"mask {net.netmask}, no gateway.")


def check_probe(host: str, port: int, timeout_s: float = 0.5) -> Check:
    """One harmless read of the card's status registers (nettest's checkMCStatus frame)."""
    from mlaser.controller import nettest   # Safety's module, used read-only
    r = nettest.probe("MC", host, port, nettest.mc_probe(), count=1, timeout_s=timeout_s)
    if r.ok:
        return Check("probe", OK, f"card answered in {r.rtts_ms[0]:.1f} ms")
    return Check("probe", WARN, f"no answer from {host}:{port} within {timeout_s * 1000:.0f} ms"
                 f"{' (' + r.error + ')' if r.error else ''}",
                 "Is the card powered and cabled? If it is, a firewall may drop its UDP replies "
                 "(see the firewall hints).")


def firewall_hints(platform: Optional[str] = None, exe: Optional[str] = None,
                   frozen: Optional[bool] = None, port: int = DEFAULT_PORT,
                   host: str = DEFAULT_HOST, which: Callable[[str], Optional[str]] = shutil.which) -> Check:
    platform = platform or sys.platform
    exe = exe or sys.executable
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    try:
        net = str(ipaddress.ip_network(f"{host}/24", strict=False))
    except ValueError:
        net = "<card subnet>"
    prog = exe if frozen else f"{exe} (the Python interpreter running mlaser)"
    if platform.startswith("win"):
        hint = ("Windows Defender Firewall: allow the program on the network the card is on. If the "
                "card never answers, add an inbound rule for UDP from the card, e.g. in an "
                "administrator PowerShell:\n"
                f"  New-NetFirewallRule -DisplayName mlaser -Direction Inbound -Program \"{exe}\" "
                "-Protocol UDP -Action Allow -Profile Any\n"
                "The card's network is usually 'Unidentified' (Public profile): allow Public too.")
    elif platform == "darwin":
        hint = ("macOS application firewall: answer 'Allow' when macOS asks whether the program "
                "may accept incoming network connections, or add it under System Settings > "
                "Network > Firewall > Options. Signed and notarised builds keep the permission "
                "across updates.")
    else:
        tools = [t for t in ("ufw", "firewall-cmd", "nft", "iptables") if which(t)]
        hint = ("Linux: if a firewall is active, allow UDP from the card, e.g.\n"
                f"  sudo ufw allow from {net} proto udp\n"
                f"  sudo firewall-cmd --zone=trusted --add-source={net} --permanent && "
                f"sudo firewall-cmd --reload"
                + (f"\nFirewall tools found: {', '.join(tools)}." if tools else ""))
    return Check("firewall", INFO, f"program: {prog}; card port UDP {port}", hint)


# ---------------------------------------------------------------------------- driver

def run(host: Optional[str] = None, port: Optional[int] = None, ini: Optional[str] = None,
        probe: bool = False, timeout_s: float = 0.5, prefix: int = 24) -> NetCheckResult:
    """Run all checks.  Without ``probe`` nothing is sent to the card."""
    t0 = time.perf_counter()
    try:
        h, p, source = resolve_card(host, port, ini)
    except (OSError, ValueError) as e:
        h, p, source = host or DEFAULT_HOST, int(port or DEFAULT_PORT), f"default ({ini}: {e})"
    res = NetCheckResult(h, p, source, frozen=bool(getattr(sys, "frozen", False)),
                         timestamp=time.time())
    res.checks.append(check_udp_socket())
    route, local_ip = check_route(h, p)
    res.local_ip = local_ip
    res.checks.append(route)
    res.checks.append(check_subnet(h, local_ip, prefix) if local_ip else
                      Check("subnet", SKIP, "no route, subnet not checked"))
    if probe and local_ip:
        res.checks.append(check_probe(h, p, timeout_s))
    elif probe:
        res.checks.append(Check("probe", SKIP, "no route, card not probed"))
    res.checks.append(firewall_hints(port=p, host=h))
    res.duration_ms = (time.perf_counter() - t0) * 1000.0
    return res


# ---------------------------------------------------------------------------- first run

def user_config_dir() -> Path:
    """Per-user config directory: ``$MLASER_CONFIG_DIR``, else platformdirs when installed,
    else %APPDATA%\\mlaser, ~/Library/Application Support/mlaser or $XDG_CONFIG_HOME/mlaser."""
    env = os.environ.get("MLASER_CONFIG_DIR")
    if env:
        return Path(env)
    try:
        import platformdirs
        return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))
    except ImportError:
        pass
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / APP_NAME


def marker_path() -> Path:
    return user_config_dir() / MARKER_NAME


def save_result(res: NetCheckResult, path: Optional[Path] = None) -> Path:
    path = path or marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(res.to_dict(), indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def last_result(path: Optional[Path] = None) -> Optional[NetCheckResult]:
    """The result saved by the first run or the last ``--selftest`` (None if there is none)."""
    try:
        return NetCheckResult.from_dict(json.loads((path or marker_path()).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def first_run(report: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
              wait_s: float = 1.0, **kw) -> Optional[threading.Thread]:
    """Run the self-test once per user (no card traffic) in a daemon thread.

    Returns None when it ran before (the marker exists) or ``$MLASER_NO_FIRSTRUN`` is set,
    else the thread, after waiting at most ``wait_s`` for it: startup is never held longer.
    The result is printed through ``report`` and saved to :func:`marker_path`."""
    if os.environ.get("MLASER_NO_FIRSTRUN"):
        return None
    path = marker_path()
    if path.exists():
        return None

    def work() -> None:
        try:
            res = run(probe=False, **kw)
            try:
                save_result(res, path)
            except OSError as e:
                report(f"mlaser: cannot save the self-test result to {path}: {e}")
            report(res.text())
        except Exception as e:  # never break startup
            report(f"mlaser: network self-test failed to run: {e}")

    t = threading.Thread(target=work, name="mlaser-netcheck", daemon=True)
    t.start()
    t.join(max(0.0, wait_s))
    return t


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="mlaser --selftest", description=__doc__.split("\n")[0])
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--ini")
    ap.add_argument("--probe", action="store_true", help="send one harmless status read to the card")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    a, _ = ap.parse_known_args(argv)
    res = run(a.host, a.port, a.ini, probe=a.probe)
    try:
        save_result(res)
    except OSError:
        pass
    print(json.dumps(res.to_dict(), indent=1) if a.json else res.text())
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
