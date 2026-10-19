"""Laser source command channel (Raycus / IPG), CLaserNetHalAPI + CSerialHalAPI.

Observed in Module/NCModule.dll:

**Command builders.**  Two ``ILaserProtocolAdapter`` singletons: Raycus at 0x100aca48
(vtable 0x1008a52c, getter FUN_100267c0) and IPG at 0x100aca4c (vtable 0x1008a4f4, getter
FUN_100267e0).  Each slot fills a ``vector<int>`` with bytes:

====  ==================  ================================  ==============================
slot  IPG                 Raycus                            meaning (INFERRED from vendor docs)
====  ==================  ================================  ==============================
+04   ``EMON\\r``  27260   ``ESC 'O' CR``  10027110          emission on
+08   ``EMOFF\\r`` 272b0   ``ESC 'S' CR``  10027140          emission off
+0c   ``ABN\\r``   27300   (empty)                           aiming beam (red light) on
+10   ``ABF\\r``   27340   (empty)                           aiming beam off
+14   (no-op)             ``ESC 'C' 'P' <p> CR``, p<=100     set power % (raw byte, not ASCII)
                          (FUN_10027170)
+18   (no-op)             18 zero bytes if 50<=f<=50000 and two args <=100 (FUN_100271f0;
                          the body was never filled in - a stub)
+1c   ``EMOD\\r``  27380   (no-op)                           enable modulation
+20   ``SDC<n>\\r`` 27880  (no-op)                           set diode current, ``n`` decimal
+24   ``ELE\\r``   273d0   (no-op)                           enable external emission control
+28   ``DLE\\r``   27410   (no-op)                           disable external emission control
+2c   ``DEABC\\r`` 27450   (no-op)                           disable external aiming control
+30   ``EEABC\\r`` 274a0   (no-op)                           enable external aiming control
====  ==================  ================================  ==============================

Only slots +04/+08 (CNCModule +0x160/+0x164 and +0x184/+0x188, FUN_1002d970/1002db10/
1002deb0/1002dfe0) and the IPG +0c/+10 (CNCModule +0x174/+0x178, FUN_1002dcb0/1002ddb0, paired
with the red-light output) are called in this build; the others are reachable only through
this rebuild.  There is no status/temperature query (no ``STA``/``RCT`` strings anywhere): the
original never reads laser status back.  :meth:`IpgLaser.query` sends any text command if you
want one (INFERRED, vendor protocol, untested against hardware).

**Routing** (vmctLaserWrite FUN_1004cf70, VM slot +0x9c; endpoints FUN_10058b70).  Two
HardPara parameters (names from MainApp): ``LGP.LaserType`` = ``sys+0x49a0`` (0 Raycus, 1 IPG,
2 semiconductor, 3 MaxPhotonics, 4 Super, 5 TXStar, 6 nLight, 7 GZ, 8 others) picks the adapter,
``LGP.LaserControlType`` = ``sys+0x49a4`` (0 none, 1 MCC serial, 2 net, 3 IO, 4 PC serial) the
channel:

* 1 "MCC serial": CLaserNetHalAPI to ``OnBLaserIP:OnBLaserPort`` (10.1.1.168:888) - the motion
  card's serial port, reached over the network; device type 5.  Raycus only.
* 2 "net": CLaserNetHalAPI to ``LaserIP:LaserPort`` (10.1.1.170:10001), type 2.  IPG only.
* 3 "IO": no command channel; the gate is a digital output (``LGP.DOLaserGate``) + DA
  (CNCModule +0x13c/+0x154); with LaserType 7 the DA value is scaled by 0.95 (0x1008ad90).
* 4 "PC serial": CSerialHalAPI (a PC COM port), type 5.  Raycus only.
In the "fire and forget" state (VM+0x11e8) a command is sent 3 times, replies ignored.

**Laser unlock** (MainApp FUN_00482130, button gp122, alarm gp121 "Laser is Locked"; NCModule
VM slot +0xa0 FUN_1004d280): a 32-byte ``AA 55 00 1A 00 FF 14 03 <code> <cs/255> <cs%255> 55 AA``
frame carrying an unlock code to the laser's own rental/expiry lock (types 1/4 only).  This is a
vendor lock, so it is documented and **intentionally not implemented**.

**Reply rules** (FUN_10010cd0 net / FUN_10011170 serial): type 2 and 5 need a non-empty reply
(serial type 5 reads 3 bytes); type 6 needs exactly 14 bytes ``AA 55 .. .. 55 AA``.  Timing from
ipAdd.ini ``LaserTimeout=100``, ``LaserMaxSendTime=5``, ``LaserMaxRecvTime=1``,
``LaserSendInterval=10``.  ``FUN_10010c00`` (read, type 5) only listens, it never sends.
"""
from __future__ import annotations

from typing import Optional

from .transport import LoopbackTransport, NetTransport, Transport, UdpServer

ESC, CR = 0x1B, 0x0D

# LGP.LaserControlType (sys+0x49a4)
CONTROL_NONE = 0
CONTROL_MCC_SERIAL = 1
CONTROL_NET = 2
CONTROL_IO = 3
CONTROL_PC_SERIAL = 4
# LGP.LaserType (sys+0x49a0)
LASER_RAYCUS = 0
LASER_IPG = 1
# backwards-friendly aliases used by the UI
LASER_RAYCUS_NET = CONTROL_MCC_SERIAL
LASER_IPG_NET = CONTROL_NET


class LaserError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- builders
class IpgCommands:
    """CIPGProtocolAdapter (vtable 0x1008a4f4)."""

    @staticmethod
    def _t(s: str) -> bytes:
        return s.encode("ascii") + b"\r"

    emission_on = staticmethod(lambda: IpgCommands._t("EMON"))         # FUN_10027260
    emission_off = staticmethod(lambda: IpgCommands._t("EMOFF"))       # FUN_100272b0
    aim_on = staticmethod(lambda: IpgCommands._t("ABN"))               # FUN_10027300
    aim_off = staticmethod(lambda: IpgCommands._t("ABF"))              # FUN_10027340
    modulation_on = staticmethod(lambda: IpgCommands._t("EMOD"))       # FUN_10027380
    external_emission_on = staticmethod(lambda: IpgCommands._t("ELE"))   # FUN_100273d0
    external_emission_off = staticmethod(lambda: IpgCommands._t("DLE"))  # FUN_10027410
    external_aim_off = staticmethod(lambda: IpgCommands._t("DEABC"))     # FUN_10027450
    external_aim_on = staticmethod(lambda: IpgCommands._t("EEABC"))      # FUN_100274a0

    @staticmethod
    def set_current(value: int) -> bytes:
        """``"SDC" + std::to_string(int)`` + CR (FUN_10027880 via FUN_10027780; no space)."""
        return IpgCommands._t("SDC%d" % int(value))


class RaycusCommands:
    """CRaycusProtocolAdapter (vtable 0x1008a52c)."""

    emission_on = staticmethod(lambda: bytes([ESC, ord("O"), CR]))     # FUN_10027110
    emission_off = staticmethod(lambda: bytes([ESC, ord("S"), CR]))    # FUN_10027140

    @staticmethod
    def set_power(percent: int) -> bytes:
        """``ESC 'C' 'P' p CR`` with ``p`` a raw byte 0..100 (FUN_10027170); larger -> empty."""
        p = int(percent)
        return bytes([ESC, ord("C"), ord("P"), p, CR]) if 0 <= p <= 100 else b""

    @staticmethod
    def set_pulse(freq: int, a: int, b: int) -> bytes:
        """FUN_100271f0: 18 zero bytes when ``50 <= freq <= 50000`` and ``a, b <= 100``,
        otherwise nothing.  The original never fills the bytes in (observed stub)."""
        return bytes(18) if (50 <= freq <= 50000 and 0 <= a <= 100 and 0 <= b <= 100) else b""


def raycus_status_ok(frame: bytes) -> bool:
    """Type-6 reply check: 14 bytes framed ``AA 55 ... 55 AA`` (FUN_10010cd0 / FUN_10011170).
    The 10 payload bytes are not interpreted anywhere in NCModule (UNKNOWN)."""
    return len(frame) == 14 and frame[:2] == b"\xaa\x55" and frame[12:] == b"\x55\xaa"


# ----------------------------------------------------------------------------- arming guard
# Emission ON needs the machine armed (docs/SAFETY.md §3).  Two ways to say who decides:
#
# * per device instance: ``client.bind_guard(fn)`` - the controller that owns this laser source
#   checks it (``UiController.laser_source_guard``); nothing else can override it;
# * the process-wide registry: every connected controller (and the window) registers its check
#   under its own owner (:func:`register_emission_guard`); emission is allowed only when there
#   is at least one and **every** registered guard allows it.  A second controller (the
#   procedures' simulator rig) therefore can never let a real laser source fire while the real
#   machine is disarmed, and closing it removes only its own entry.
#
# Without any guard, emission is refused.
_GUARDS: dict = {}            # owner key -> fn(on)
_DEFAULT_OWNER = "default"


def register_emission_guard(fn, owner) -> None:
    """Register ``fn(on)`` (raises :class:`LaserError` to refuse) for ``owner`` (any hashable;
    the controller registers itself).  Replaces only that owner's entry."""
    _GUARDS[_owner_key(owner)] = fn


def unregister_emission_guard(owner) -> None:
    _GUARDS.pop(_owner_key(owner), None)


def _owner_key(owner):
    return owner if isinstance(owner, str) else ("id", id(owner))


def set_emission_guard(fn) -> None:
    """The window-level guard (``ui.safety_ui``): ``fn(on)`` raises :class:`LaserError` to
    refuse; None removes it.  Other owners' guards stay registered."""
    if fn is None:
        unregister_emission_guard(_DEFAULT_OWNER)
    else:
        register_emission_guard(fn, _DEFAULT_OWNER)


def emission_guard():
    """The window-level guard (None when not set)."""
    return _GUARDS.get(_DEFAULT_OWNER)


def emission_guards() -> list:
    return list(_GUARDS.values())


def check_emission(on: bool, guard=None) -> None:
    """Refuse (LaserError) an emission ON unless ``guard`` (a device's bound guard) allows it,
    or - without one - every registered guard allows it (and there is at least one)."""
    if not on:
        return
    if guard is not None:
        guard(on)
        return
    guards = list(_GUARDS.values())
    if not guards:
        raise LaserError("laser source emission refused: no safety supervisor is armed")
    for g in guards:
        g(on)


# IPG commands that switch the beam on or hand emission to an external signal: refused unless
# armed wherever they appear in a payload.  The raw query also guards the aiming laser
# (``ABN``, ``EEABC``); the aiming-beam button (``aiming_beam``) stays usable disarmed, like the
# red-light output it is paired with in the original.
IPG_EMISSION_COMMANDS = ("EMON", "EMOD", "ELE")
IPG_QUERY_GUARDED = IPG_EMISSION_COMMANDS + ("ABN", "EEABC")
IPG_SEPARATORS = ("\r", "\n", ";")


def ipg_tokens(text: str) -> list[str]:
    """The commands of an IPG payload (split at CR, LF and ``;``), upper case, stripped."""
    t = text
    for sep in IPG_SEPARATORS:
        t = t.replace(sep, "\x00")
    return [c.strip().upper() for c in t.split("\x00") if c.strip()]


def ipg_emits(text: str, commands: tuple = IPG_EMISSION_COMMANDS) -> bool:
    """Does any command of the payload start with one of ``commands``?"""
    return any(c.split()[0].startswith(commands) for c in ipg_tokens(text) if c.split())


# ----------------------------------------------------------------------------- clients
class LaserClient:
    """Common pass-through client: send bytes, require a non-empty reply."""

    commands = None

    def __init__(self, transport: Transport, guard=None):
        self.t = transport
        self.last_reply: bytes = b""
        self._guard = guard

    def bind_guard(self, fn) -> None:
        """Bind this laser source to its owning controller's check (``fn(on)`` raises
        LaserError): from now on only that controller's arming decides for this device."""
        self._guard = fn

    def check_emission(self, on: bool) -> None:
        check_emission(on, self._guard)

    def close(self) -> None:
        self.t.close()

    def _payload_emits(self, data: bytes) -> bool:
        """Does the raw payload contain an emission-on command of this protocol?"""
        return False

    def send(self, data: bytes, require_reply: bool = True) -> bytes:
        """Send a raw payload.  A payload that contains an emission-on command anywhere (not only
        at its start) needs the machine armed, like :meth:`emission`."""
        if not data:
            return b""
        if self._payload_emits(data):
            self.check_emission(True)
        if not require_reply:
            self.t.send_only(data)
            return b""
        rx = self.t.exchange(data, None, lambda r: len(r) > 0)
        if rx is None:
            raise LaserError(f"laser: no reply to {data!r}")
        self.last_reply = rx
        return rx

    def emission(self, on: bool) -> bytes:
        """Emission on (needs the machine armed, :func:`check_emission`) / off (always)."""
        self.check_emission(on)
        return self.send(self.commands.emission_on() if on else self.commands.emission_off())


class IpgLaser(LaserClient):
    """IPG YLR-style ASCII channel (LaserType 1, control type 2 "net")."""
    commands = IpgCommands

    @classmethod
    def connect(cls, host: str = "10.1.1.170", port: int = 10001, access: str = "udp",
                timeout_ms: int = 100, max_send: int = 5) -> "IpgLaser":
        return cls(NetTransport(host, port, access, timeout_ms, max_send, 1, 10))

    def _payload_emits(self, data: bytes) -> bool:
        return ipg_emits(data.decode("ascii", "replace"))

    def aiming_beam(self, on: bool) -> bytes:
        return self.send(IpgCommands.aim_on() if on else IpgCommands.aim_off())

    def set_current(self, percent: float) -> bytes:
        return self.send(IpgCommands.set_current(round(percent)))

    def modulation(self) -> bytes:
        return self.send(IpgCommands.modulation_on())

    def external_emission(self, on: bool) -> bytes:
        self.check_emission(on)
        return self.send(IpgCommands.external_emission_on() if on else IpgCommands.external_emission_off())

    def external_aiming(self, on: bool) -> bytes:
        return self.send(IpgCommands.external_aim_on() if on else IpgCommands.external_aim_off())

    def query(self, text: str) -> str:
        """Send one ASCII command (e.g. ``STA``/``RCT`` of the vendor protocol - NOT used by
        Mlaser, INFERRED) and return the reply text.  One command only: CR, LF, ``;`` and other
        control characters are refused (a second command could hide in them); an emission
        command (``EMON``, ``EMOD``, ``ELE``, ``ABN``, ``EEABC``) needs the machine armed."""
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text) or ";" in text:
            raise LaserError("laser query refused: one command only (no CR, LF, ';' or control characters)")
        try:
            raw = text.strip().encode("ascii")
        except UnicodeEncodeError:
            raise LaserError("laser query refused: ASCII commands only") from None
        if not raw:
            raise LaserError("laser query refused: empty command")
        if ipg_emits(raw.decode("ascii"), IPG_QUERY_GUARDED):
            self.check_emission(True)
        return self.send(raw + b"\r").decode("ascii", "replace").strip()


class RaycusLaser(LaserClient):
    """Raycus ESC-sequence channel (LaserType 0, control types 1 "MCC serial" and 4 "PC serial")."""
    commands = RaycusCommands

    def _payload_emits(self, data: bytes) -> bool:
        return RaycusCommands.emission_on() in bytes(data)

    @classmethod
    def connect(cls, host: str = "10.1.1.168", port: int = 888, access: str = "udp",
                timeout_ms: int = 100, max_send: int = 5) -> "RaycusLaser":
        return cls(NetTransport(host, port, access, timeout_ms, max_send, 1, 10))

    def set_power(self, percent: float) -> bytes:
        return self.send(RaycusCommands.set_power(round(percent)))


def laser_for(laser_type: int, control_type: int, settings=None, access: str = "udp") -> Optional[LaserClient]:
    """Pick the client like CNCModule FUN_1002d970 / FUN_10058b70: Raycus with control type 1
    (or 4 = PC serial: build ``RaycusLaser(SerialTransport(...))`` yourself), IPG with 2."""
    from .transport import DeviceSettings
    s = settings or DeviceSettings()
    if laser_type == LASER_RAYCUS and control_type == CONTROL_MCC_SERIAL:
        return RaycusLaser.connect(*s.onb_laser, access=access, timeout_ms=s.laser_timeout_ms,
                                   max_send=s.laser_max_send)
    if laser_type == LASER_IPG and control_type == CONTROL_NET:
        return IpgLaser.connect(*s.laser, access=access, timeout_ms=s.laser_timeout_ms,
                                max_send=s.laser_max_send)
    return None


# ----------------------------------------------------------------------------- simulator
class LaserSimulator:
    """Laser model for tests.  IPG: echoes known commands (``EMON`` -> ``EMON``), answers
    ``STA`` with ``STA: <bits>`` (bit 2 emission, bit 8 aiming; the bit layout follows the IPG
    manual convention, INFERRED) and ``SDC n`` with the echo.  Raycus: echoes the 3-5 byte ESC
    frames and keeps power."""

    def __init__(self, kind: str = "ipg"):
        self.kind = kind
        self.emission = False
        self.aiming = False
        self.current = 0.0
        self.power = 0
        self.log: list[bytes] = []
        self.drop = 0
        self._server: Optional[UdpServer] = None

    def handle(self, data: bytes) -> Optional[bytes]:
        self.log.append(data)
        if self.drop > 0:
            self.drop -= 1
            return None
        if self.kind == "raycus":
            if data == RaycusCommands.emission_on():
                self.emission = True
            elif data == RaycusCommands.emission_off():
                self.emission = False
            elif len(data) == 5 and data[:3] == b"\x1bCP" and data[4] == CR:
                self.power = data[3]
            else:
                return None
            return data
        text = data.decode("ascii", "replace").strip()
        if text == "EMON":
            self.emission = True
        elif text == "EMOFF":
            self.emission = False
        elif text == "ABN":
            self.aiming = True
        elif text == "ABF":
            self.aiming = False
        elif text.startswith("SDC"):
            try:
                self.current = float(text[3:])
            except ValueError:
                return b"ERR\r"
            return f"SDC: {self.current:g}\r".encode()
        elif text == "STA":
            return f"STA: {(4 if self.emission else 0) | (256 if self.aiming else 0)}\r".encode()
        elif text in ("EMOD", "ELE", "DLE", "DEABC", "EEABC"):
            pass
        else:
            return b"BCMD\r"
        return (text + "\r").encode()

    def loopback(self) -> LoopbackTransport:
        return LoopbackTransport(self.handle)

    def serve(self, host: str = "127.0.0.1", port: int = 0, transport: str = "udp") -> UdpServer:
        self._server = UdpServer(self.handle, host, port, transport).start()
        return self._server

    def stop(self) -> None:
        if self._server:
            self._server.stop()
