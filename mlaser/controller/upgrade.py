"""Firmware upgrade of the motion card (*.mcf), the autofocus head (*.afb) and the extension
card (*.efb), plus "Restart" (mf131).  **Can brick a card**: callers (the UI) must confirm
strongly and must only call this on a connected card.

Recovered from MainApp.exe / NCModule.dll (O = observed, I = inferred, U = unknown):

* mf132 Upgrade, command 0x32cd, FUN_004c0500 (O): CFileDialog "mcf", confirm mf175, read the
  whole file, refuse when it is shorter than 14 bytes (``cmp ..,0xe`` at 0x4c0a3e; mf176), push
  every byte into a vector, NC +0x248 -> FUN_10046020: MC HAL receive timeout := 2000 ms,
  post VM event 9.  Event 9 FUN_10055840: chunks of 1400 bytes (0x578), each sent by
  FUN_10053930 as **func 0x26 [addr = file offset, n, n bytes]** through the MC HAL; a failed
  chunk logs "false fillDownFile" and stops the loop (no resend).  Progress = offset·100/size
  (VM +0x108).  At 100 %: NC +0x24c restores MCTimeout, NC +0x1ec -> write(100, 8888)
  (reboot), the dialog counts down 10 s, then mf179.  No erase / start / commit command (O).
* The same at connect when RO[1] < MinHardwareVer: ``Update\\MCC100_V%.2f.mcf`` (FUN_00594fe0).
* mf440 AF upgrade 0x32d4 FUN_004c0d00 ("afb") and mf625 extension card 0x32d5 FUN_004c1500
  ("efb") (O): same file check; write device reg 100 := 8888 (RTU), Sleep(500), then NC
  +0x260 / +0x26c: device HAL timeout 2000, chunks of MaxAFDownItem / MaxECDownItem (ini 200)
  as RTU vendor function 0x26 ``[addr = offset, n, bytes]``; the AF loop sleeps 2000 ms after
  the first chunk (I: flash erase) and AFUpdateInterval between chunks.  Finish: reg 100 :=
  6666, Sleep(1000), mf439 / mf624, reconnect (events 0x11 / 0x15).
* The on-board follower (.zfb) loop exists in NC (FUN_10055ec0, func 0x26 on the MC HAL,
  MaxZFDownItem 512) but MainApp has no caller in this build (I): not offered.
* mf131 Restart 0x32cc FUN_004b8740 (O): only NC +0x1ec = write(100, 8888).

File headers: the PC checks only the size.  ``MCC100_V201.77.mcf`` starts with the u32 LE file
length (0x00019200 = 102912, I); :func:`check_file` also checks that word for .mcf files and
refuses a mismatch (rebuild safety check, documented as such).
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union

from ..protocol import frames as F

MIN_FILE_SIZE = 14             # 0x4c0a3e
MC_CHUNK = 1400                # 0x578, FUN_10055840
MC_UPDATE_TIMEOUT_MS = 2000    # FUN_10046020
DEV_CHUNK = 200                # MaxAFDownItem / MaxECDownItem (ipAdd.ini)
AF_FIRST_CHUNK_SLEEP_S = 2.0   # FUN_10055a60 Sleep(2000) after the first chunk
AF_UPDATE_INTERVAL_S = 0.0     # ipAdd.ini AFUpdateInterval (U default; 0 here)
REBOOT_MC = 8888               # write(100, 8888) = FUN_10039300
DEV_ENTER_UPDATE = 8888        # AF/EC reg 100 := 8888 before the download
DEV_FINISH = 6666              # AF/EC reg 100 := 6666 after it (FUN_005603f0)
EXTENSIONS = {"mc": ".mcf", "af": ".afb", "ec": ".efb"}

PathLike = Union[str, Path]
Progress = Callable[[int], None]


class UpgradeError(RuntimeError):
    pass


@dataclass
class FirmwareFile:
    path: Path
    data: bytes
    kind: str

    @property
    def declared_length(self) -> Optional[int]:
        return struct.unpack_from("<I", self.data, 0)[0] if len(self.data) >= 4 else None


def check_file(path: PathLike, kind: str = "mc") -> FirmwareFile:
    """Read and check a firmware file (raises UpgradeError with the reason).

    O: size >= 14 (mf176 otherwise).  Rebuild checks on top: the extension matches the target
    (mcf / afb / efb, the original's CFileDialog filters) and, for .mcf, the first u32 equals the
    file length (I, true for the shipped MCC100_V201.77.mcf)."""
    p = Path(path)
    ext = EXTENSIONS.get(kind)
    if ext is None:
        raise UpgradeError(f"unknown upgrade target {kind!r}")
    if p.suffix.lower() != ext:
        raise UpgradeError(f"{p.name}: expected a {ext} file for this upgrade")
    try:
        data = p.read_bytes()
    except OSError as e:
        raise UpgradeError(f"mf176: cannot open {p}: {e}") from e
    if len(data) < MIN_FILE_SIZE:
        raise UpgradeError(f"mf176: {p.name} is too short ({len(data)} bytes, need >= {MIN_FILE_SIZE})")
    fw = FirmwareFile(p, data, kind)
    if kind == "mc" and fw.declared_length != len(data):
        raise UpgradeError(f"{p.name}: header length {fw.declared_length} != file size {len(data)}"
                           " (not an MCC100 firmware image?)")
    return fw


def chunks(data: bytes, size: int):
    for off in range(0, len(data), size):
        yield off, data[off:off + size]


def upload_mc(mc, fw: FirmwareFile, progress: Optional[Progress] = None,
              cancel: Optional[Callable[[], bool]] = None, reboot: bool = True) -> int:
    """VM event 9 (FUN_10055840) + finish: func-0x26 chunks of 1400 bytes at their file offset,
    then write(100, 8888).  Returns the bytes sent.  Stops (UpgradeError) on the first failed
    chunk, like "false fillDownFile"."""
    if fw.kind != "mc":
        raise UpgradeError("upload_mc needs an .mcf file")
    old = getattr(mc, "_timeout_ms", None)
    mc.set_timeout(MC_UPDATE_TIMEOUT_MS)
    sent = 0
    try:
        for off, blob in chunks(fw.data, MC_CHUNK):
            if cancel is not None and cancel():
                raise UpgradeError("upgrade cancelled")
            try:
                mc.transact(F.Request(F.FUNC_WRITE_BYTES_AT, off, len(blob), list(blob)))
            except Exception as e:  # noqa: BLE001
                raise UpgradeError(f"false fillDownFile: {off} ({e})") from e
            sent += len(blob)
            if progress:
                progress(round(sent * 100 / len(fw.data)))
    finally:
        if old is not None:
            mc.set_timeout(old)
    if reboot:
        mc.write_reg(F.REG_CONTROL, REBOOT_MC)
    return sent


def upload_device(rtu, fw: FirmwareFile, progress: Optional[Progress] = None,
                  cancel: Optional[Callable[[], bool]] = None, chunk: int = DEV_CHUNK,
                  first_sleep_s: Optional[float] = None, interval_s: float = AF_UPDATE_INTERVAL_S,
                  sleep: Callable[[float], None] = time.sleep) -> int:
    """AF / EC upgrade (FUN_10055a60 / FUN_10055ca0) on an :class:`mlaser.devices.modbus_rtu.RtuClient`:
    reg 100 := 8888, 500 ms, RTU 0x26 chunks, reg 100 := 6666, 1 s."""
    if fw.kind not in ("af", "ec"):
        raise UpgradeError("upload_device needs an .afb or .efb file")
    if first_sleep_s is None:
        first_sleep_s = AF_FIRST_CHUNK_SLEEP_S if fw.kind == "af" else 0.0
    rtu.write_u32(100, [DEV_ENTER_UPDATE])
    sleep(0.5)
    sent = 0
    for i, (off, blob) in enumerate(chunks(fw.data, chunk)):
        if cancel is not None and cancel():
            raise UpgradeError("upgrade cancelled")
        try:
            rtu.request([0x26, off, len(blob), *blob])
        except Exception as e:  # noqa: BLE001
            raise UpgradeError(f"false fill{fw.kind.upper()}DownFile: {off} ({e})") from e
        sent += len(blob)
        if progress:
            progress(round(sent * 100 / len(fw.data)))
        sleep(first_sleep_s if i == 0 else interval_s)
    rtu.write_u32(100, [DEV_FINISH])
    sleep(1.0)
    return sent


def restart_card(mc) -> None:
    """mf131 Restart (FUN_004b8740 -> NC +0x1ec -> VM +0x168 FUN_10039300): write(100, 8888)."""
    mc.write_reg(F.REG_CONTROL, REBOOT_MC)


def needs_upgrade(version: int, min_version: int = 20177) -> bool:
    """FUN_00594fe0: ``0 < RO[1] < MinHardwareVer`` triggers the automatic prompt (mf232)."""
    return 0 < version < min_version


def bundled_firmware_name(min_version: int = 20177) -> str:
    """``Update\\MCC100_V`` + "%.2f" % (MinHardwareVer / 100) + ".mcf" (O)."""
    return "MCC100_V%.2f.mcf" % (min_version / 100.0)
