"""XHC wireless handheld pendant (PHBX.dll + MainApp CManuPanel), over HID with ``hidapi``.

Observed (PHBX.dll, addresses 0x1000xxxx; MainApp addresses marked):

* **Device**: VID 0x10CE; accepted PIDs map to a model code (XGetVersion), see
  :data:`PIDS` (XOpen 0x100018c0).  Usage page is not checked: per device the driver opens
  every interface, keeps the one with ``FeatureReportByteLength == 8`` as *control* and the
  one with ``InputReportByteLength == 6`` as *input*.
* **Input report** (FUN_10001380, reader thread 0x10001e10): 6 bytes
  ``04 | S | key1 | key2 | A | C``.  0xFF keys mean 0.  ``A`` (1..0x1e, latched) is never used
  by MainApp (UNKNOWN meaning).  Valid when, with ``c = ~S & 0xff`` and ``d`` = UTC day of month:
  ``((k1 + c) ^ (d | c)) & 0xff == C`` or ``(((d | c) + c) ^ k1) & 0xff == C``.
  Packed event ``key1 | key2 << 8 | A << 16`` -> ``PostMessage(hwnd, 0x4B1, 0x100 down / 0x101
  up, packed)`` and the optional callback (SetGetKeyCallbackFunction 0x10001010).
* **Feature reports** (id 6, 8 bytes, HidD_SetFeature on the control interface):
  day sync ``06 FE FD dd FF FF 00 00`` (FUN_100011d0, on open and after checksum failures);
  LED output ``06 FE FD dd p0 p1 00 00`` (XSendOutput 0x10002130); pairing
  ``06 FA FB dd lo hi 00 00`` (XWritFid 0x10002450); raw XWritDev chunks of 7 bytes.
  Status: HidD_GetFeature id 6 -> DevID ``[1..2]`` LE, channel ``[3]<<8|[4]``, RSSI ``[5]``,
  FID ``[6..7]`` LE (FUN_100012d0, once per second).  XGetInput returns 0x6A "poor signal"
  when RSSI >= 0x50.
* **There is no MPG wheel, axis selector or step multiplier** in this driver: jogging is
  hold-to-move on direction keys.  MainApp key handler FUN_005ab2e0 (see :data:`KEYMAP`):
  key 0x0B is Fn; the effective key is key1, or key2 when key1 == Fn.  While the machine is
  busy only Stop (0x0A) is honoured.  On every key change MainApp sends
  ``XSendOutput([mask, 0x01])`` lighting the LED of keys 1..6 (FUN_005961b0).
* MainApp enables it when ``SOP.RemoteType`` (app+0x4078) == 3 "LCR19" (FUN_005b8d10:
  LoadLibraryW, Xinit, XOpen(hwnd)); XGetInput is polled every 200 ms for status only.
* Nothing here is licensing (the day checksum and FID pairing are link integrity).

``hidapi`` (``pip install hidapi``, module ``hid``) is imported lazily so the application
runs without it.
"""
from __future__ import annotations

import datetime as _dt
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

VID = 0x10CE
PIDS = {  # PID -> model code returned by XGetVersion (XOpen 0x100018c0)
    0xEB68: 0x203, 0xEB62: 0x200, 0xEB8C: 0x2BC, 0xEB7C: 0x1FE, 0xEB61: 0x1FF, 0xEB63: 0x201,
    0xEB6F: 0x1F4, 0xEB66: 0x258, 0xEB7D: 0x262, 0xEB7E: 0x26C, 0xEB6D: 0x26D, 0xEB64: 0x26E,
    0xEB65: 0x263, 0xEB6C: 0x1F5, 0xEB9A: 0x384, 0xEBA0: 0x385, 0xEBAA: 0x3C0, 0xEB88: 0x3D4,
    0xEBA1: 0x3C1, 0xEB87: 0x38E,
}
INPUT_REPORT_ID = 0x04
INPUT_LEN = 6
FEATURE_REPORT_ID = 0x06
FEATURE_LEN = 8
POOR_RSSI = 0x50

KEY_FN = 0x0B
KEYMAP = {  # effective key -> action (MainApp FUN_005ab2e0)
    0x01: "follow", 0x02: "shutter", 0x03: "red_light", 0x04: "laser_hold", 0x05: "gas",
    0x06: "frame", 0x08: "start", 0x09: "pause_resume", 0x0A: "stop", 0x0D: "toggle_speed",
    0x13: ("jog", "X", +1), 0x11: ("jog", "X", -1),
    0x0F: ("jog", "Y", +1), 0x12: ("jog", "Y", -1),
    0x10: ("jog", "Z", +1), 0x0E: ("jog", "Z", -1),
}
FN_KEYMAP = {  # with Fn held
    0x07: "home", 0x0C: "fn_c", 0x0D: "toggle_step_mode",
    0x10: ("jog", "W", +1), 0x0E: ("jog", "W", -1),
}


def utc_day(now: Optional[_dt.datetime] = None) -> int:
    return (now or _dt.datetime.now(_dt.timezone.utc)).day


def checksum_ok(report: bytes, day: int) -> bool:
    """FUN_10001380 validity test."""
    if len(report) < INPUT_LEN:
        return False
    s, k1, cs = report[1], report[2], report[5]
    k1 = 0 if k1 == 0xFF else k1
    c = ~s & 0xFF
    dc = (day | c) & 0xFF
    return ((k1 + c) & 0xFF) ^ dc == cs or (((dc + c) & 0xFF) ^ k1) == cs


def make_checksum(seed: int, key1: int, day: int) -> int:
    """First form of the check (what a pendant would send; used by the simulator)."""
    c = ~seed & 0xFF
    return (((key1 + c) & 0xFF) ^ ((day | c) & 0xFF)) & 0xFF


@dataclass(frozen=True)
class PendantEvent:
    key1: int
    key2: int
    aux: int

    @property
    def packed(self) -> int:
        return self.key1 | self.key2 << 8 | self.aux << 16

    @property
    def down(self) -> bool:
        return bool(self.key1 or self.key2)

    @property
    def fn(self) -> bool:
        return self.key1 == KEY_FN

    @property
    def key(self) -> int:
        """Effective key: key2 when key1 is Fn (FUN_005ab2e0)."""
        return self.key2 if self.key1 == KEY_FN else self.key1


def decode_input(report: bytes, day: Optional[int] = None) -> Optional[PendantEvent]:
    """6-byte input report -> event, or None when the id/length/checksum is wrong."""
    if len(report) < INPUT_LEN or report[0] != INPUT_REPORT_ID:
        return None
    if not checksum_ok(report, utc_day() if day is None else day):
        return None
    k1 = 0 if report[2] == 0xFF else report[2]
    k2 = 0 if report[3] == 0xFF else report[3]
    a = report[4] if 0 < report[4] < 0x1F else 0
    return PendantEvent(k1, k2, a)


def day_sync_report(day: Optional[int] = None) -> bytes:
    return bytes([FEATURE_REPORT_ID, 0xFE, 0xFD, utc_day() if day is None else day, 0xFF, 0xFF, 0, 0])


def output_report(p0: int, p1: int, day: Optional[int] = None, model: int = 0) -> bytes:
    """XSendOutput (0x10002130 / FUN_10002080), including the model 0x26C bit remap."""
    if model == 0x26C:
        q0, q1 = 0, 0
        for bit, val in ((0, 0x02), (1, 0x40), (2, 0x10), (4, 0x08), (7, 0x80)):
            if p0 >> bit & 1:
                q1 |= val
        if p0 & 0x20:
            q0 = 0x40
        if p1 & 0x10:
            q1 |= 0x20
        if p1 & 0x01:
            q1 |= 0x01
        p0, p1 = q0, q1
    return bytes([FEATURE_REPORT_ID, 0xFE, 0xFD, utc_day() if day is None else day, p0 & 0xFF, p1 & 0xFF, 0, 0])


def fid_report(fid: int, day: Optional[int] = None) -> bytes:
    """XWritFid (0x10002450): pairing / frequency id."""
    return bytes([FEATURE_REPORT_ID, 0xFA, 0xFB, utc_day() if day is None else day, fid & 0xFF, fid >> 8 & 0xFF, 0, 0])


def raw_reports(data: bytes) -> list[bytes]:
    """XWritDev (0x10002300): 7-byte chunks behind report id 6, last one zero padded."""
    return [bytes([FEATURE_REPORT_ID]) + data[i:i + 7].ljust(7, b"\0") for i in range(0, len(data), 7)]


def led_mask(key: int, dev_id: int) -> int:
    """FUN_005961b0: LED bit for keys 1..6 (two layouts by DevID range 0x9268..0x9361)."""
    if 0x9268 <= dev_id <= 0x9361:
        table = {1: 0x01, 2: 0x02, 3: 0x04, 4: 0x10, 5: 0x20, 6: 0x40}
    else:
        table = {4: 0x01, 5: 0x02, 6: 0x04, 1: 0x10, 2: 0x20, 3: 0x40}
    return table.get(key, 0)


@dataclass
class PendantStatus:
    dev_id: int = 0
    channel: int = 0
    rssi: int = 0
    fid: int = 0

    @classmethod
    def parse(cls, feat: bytes) -> "PendantStatus":
        f = bytes(feat).ljust(8, b"\0")
        return cls(f[1] | f[2] << 8, f[3] << 8 | f[4], f[5], f[6] | f[7] << 8)

    @property
    def poor_signal(self) -> bool:
        return self.rssi >= POOR_RSSI


# --------------------------------------------------------------------------- device
def hid_available() -> bool:
    try:
        import hid  # noqa: F401
        return True
    except Exception:
        return False


def find_pendants() -> list[dict]:
    """hidapi enumeration filtered on VID/PIDs (empty when hidapi is missing)."""
    try:
        import hid
    except Exception:
        return []
    return [d for d in hid.enumerate(VID, 0) if d.get("product_id") in PIDS]


class Pendant:
    """Opens the pendant's input and control interfaces and runs a reader thread that calls
    ``on_event(PendantEvent)`` for every valid report whose packed value changed.

    ``devices`` (input, control) can be injected for tests; otherwise hidapi is used."""

    def __init__(self, on_event: Optional[Callable[[PendantEvent], None]] = None,
                 devices: Optional[tuple] = None):
        self.on_event = on_event
        self.input = self.control = None
        self.model = 0
        self.status = PendantStatus()
        self._injected = devices
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last: Optional[int] = None
        self.bad_reports = 0

    def open(self) -> bool:
        if self._injected:
            self.input, self.control = self._injected
        else:
            try:
                import hid
            except Exception:
                return False
            for info in find_pendants():
                dev = hid.device()
                try:
                    dev.open_path(info["path"])
                except OSError:
                    continue
                self.model = PIDS[info["product_id"]]
                # tell the interfaces apart the way XOpen does: feature report length 8
                try:
                    feat = dev.get_feature_report(FEATURE_REPORT_ID, FEATURE_LEN)
                    is_ctrl = len(feat) >= FEATURE_LEN
                except (OSError, ValueError):
                    is_ctrl = False
                if is_ctrl and self.control is None:
                    self.control = dev
                elif self.input is None:
                    self.input = dev
                else:
                    dev.close()
            if self.control is None:
                self.control = self.input        # single-interface variants
        if self.input is None:
            return False
        self.sync_day()
        self.read_status()
        return True

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        for d in {id(self.input): self.input, id(self.control): self.control}.values():
            if d is not None and not self._injected:
                d.close()
        self.input = self.control = None

    # outputs
    def _feature(self, data: bytes) -> bool:
        if self.control is None:
            return False
        try:
            return self.control.send_feature_report(list(data)) >= 0
        except (OSError, ValueError):
            return False

    def sync_day(self) -> bool:
        return self._feature(day_sync_report())

    def set_leds(self, p0: int, p1: int = 0x01) -> bool:
        return self._feature(output_report(p0, p1, model=self.model))

    def write_fid(self, fid: int) -> bool:
        return self._feature(fid_report(fid))

    def read_status(self) -> PendantStatus:
        if self.control is not None:
            try:
                self.status = PendantStatus.parse(bytes(self.control.get_feature_report(FEATURE_REPORT_ID, FEATURE_LEN)))
            except (OSError, ValueError):
                pass
        return self.status

    # input
    def poll(self, timeout_ms: int = 100) -> Optional[PendantEvent]:
        data = self.input.read(INPUT_LEN, timeout_ms)
        if not data:
            return None
        ev = decode_input(bytes(data))
        if ev is None:
            self.bad_reports += 1
            if self.bad_reports % 3 == 0:
                self.sync_day()
            return None
        if ev.packed == self._last:
            return None
        self._last = ev.packed
        if self.on_event:
            self.on_event(ev)
        return ev

    def start(self) -> "Pendant":
        self._stop.clear()

        def loop():
            last_status = 0.0
            while not self._stop.is_set():
                try:
                    self.poll(80)
                except (OSError, ValueError):
                    time.sleep(0.4)
                if time.monotonic() - last_status > 1.0:
                    self.read_status()
                    last_status = time.monotonic()

        self._thread = threading.Thread(target=loop, daemon=True, name="pendant")
        self._thread.start()
        return self


class PendantJogMapper:
    """MainApp FUN_005ab2e0 semantics on top of a controller facade.

    ``ctl`` needs ``jog_start(axis, direction, fast)`` / ``jog_stop(axis)`` (mlaser.ui.facade);
    ``actions`` maps action names of :data:`KEYMAP`/:data:`FN_KEYMAP` to callables;
    ``busy()`` returns True while a program runs (then only Stop works)."""

    def __init__(self, ctl, actions: Optional[dict] = None, busy: Callable[[], bool] = lambda: False,
                 leds: Optional[Callable[[int], None]] = None, dev_id: Callable[[], int] = lambda: 0):
        self.ctl, self.actions, self.busy = ctl, dict(actions or {}), busy
        self.leds, self.dev_id = leds, dev_id
        self.fast = False
        self.step_mode = False
        self._jogging: Optional[str] = None
        self._laser_held = False

    def _release(self) -> None:
        if self._jogging is not None:
            self.ctl.jog_stop(self._jogging)
            self._jogging = None
        if self._laser_held:
            self._laser_held = False
            fn = self.actions.get("laser_release")
            if fn:
                fn()

    def handle(self, ev: PendantEvent) -> Optional[object]:
        key = ev.key
        if self.leds is not None:
            self.leds(led_mask(key, self.dev_id()) if ev.down else 0)
        if self.busy():
            self._release()
            if key == 0x0A and self.actions.get("stop"):
                self.actions["stop"]()
            return "stop" if key == 0x0A else None
        self._release()
        if not ev.down:
            return None
        act = (FN_KEYMAP if ev.fn else KEYMAP).get(key)
        if act is None:
            return None
        if isinstance(act, tuple):
            _, axis, d = act
            self.ctl.jog_start(axis, d, self.fast)
            self._jogging = axis
            return act
        if act == "toggle_speed":
            self.fast = not self.fast
        elif act == "toggle_step_mode":
            self.step_mode = not self.step_mode
        elif act == "laser_hold":
            self._laser_held = True
        fn = self.actions.get(act)
        if fn:
            fn()
        return act


# --------------------------------------------------------------------------- simulator
class FakeHidDevice:
    """Minimal hidapi ``device`` stand-in: queue input reports with :meth:`press`."""

    def __init__(self, dev_id: int = 0x1234, rssi: int = 0x20, day: Optional[int] = None):
        self.day = day
        self.queue: list[bytes] = []
        self.features: list[bytes] = []
        self.status = bytes([FEATURE_REPORT_ID, dev_id & 0xFF, dev_id >> 8, 0, 5, rssi, 0x34, 0x12])
        self._seed = 0x5A

    def press(self, key1: int, key2: int = 0, aux: int = 0, bad: bool = False) -> None:
        day = utc_day() if self.day is None else self.day
        self._seed = (self._seed * 37 + 11) & 0xFF
        cs = make_checksum(self._seed, key1, day) ^ (0xFF if bad else 0)
        self.queue.append(bytes([INPUT_REPORT_ID, self._seed, key1, key2, aux, cs]))

    def read(self, n: int, timeout_ms: int = 0) -> list[int]:
        return list(self.queue.pop(0)) if self.queue else []

    def send_feature_report(self, data) -> int:
        self.features.append(bytes(data))
        return len(data)

    def get_feature_report(self, rid: int, n: int) -> list[int]:
        return list(self.status[:n])

    def close(self) -> None:
        pass
