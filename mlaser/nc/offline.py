"""A241115_1 "Offline Process": export the compiled job for a controller that runs it
without the PC.

Recovered from MainApp.exe / NCModule.dll (O = observed, I = inferred, U = unknown):

* Command 0x26b1, FUN_00487f30 (O): save dialog ``*.enc`` (SP.m_iEnableLaserType == 0) or
  ``*.encex`` (otherwise); sets the offline flag sys+0x4730 and runs the normal process
  generation.  While the flag is set, the ManuItem download NC +0x68 (FUN_100352b0) writes
  ``<dir>\\_tempManuItem.olpf`` instead of sending to the card::

      Manu_Begin <begin> <end>
      <+0x18> <+0x16> <+0x14> <+0x12> <+0x10> <+0x0> <+0x4> <+0x8> <+0xc>     (one per item)
      Manu_End
      Size <n>                         (after the last block)

* The container (FUN_0048e590 / FUN_0048e980, O) concatenates the parts, each followed by an
  ASCII tag: ``_tempProcessInfo.olpi NEXCUT_OLPI_END _tempManuItem.olpf NEXCUT_OLPF_END
  _tempthumbnail.jpg NEXCUT_JPG_END _tempLayer.xml NEXCUT_LAYER_XML_END _tempSource.chf
  NEXCUT_CHF_END``.  Whether .enc/.encex are additionally encrypted is U (the rebuild writes
  the plain container); the content of the .olpi part is U (the rebuild writes a key=value
  summary).
* Optional upload (O): multipart ``POST /NexCut/File/UploadGCode`` then ``POST
  /NexCut/File/LoadFile {"fileName": ...}`` to a NexCut network controller -- **not
  implemented** (cloud/HTTP endpoint, host U).
* The MCC100 itself has an NC-side offline path (NC +0x318..+0x324, events 0x19/0x1a: UDP
  discovery, ``[7, "name&extra"]`` to reg 101, 1400-byte func-0x26 chunks, ``[8]``), but MainApp
  never calls it in this build (O: no caller).  :func:`upload_offline_to_card` implements that
  wire sequence for completeness; the UI uses the file export like the original.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Union

from ..protocol import frames as F
from .process import ManuItem

PathLike = Union[str, Path]

PARTS = (("_tempProcessInfo.olpi", b"NEXCUT_OLPI_END"), ("_tempManuItem.olpf", b"NEXCUT_OLPF_END"),
         ("_tempthumbnail.jpg", b"NEXCUT_JPG_END"), ("_tempLayer.xml", b"NEXCUT_LAYER_XML_END"),
         ("_tempSource.chf", b"NEXCUT_CHF_END"))
CARD_CHUNK = 1400
CARD_START = 7
CARD_STOP = 8


def manu_items(job) -> list[ManuItem]:
    """ManuItems of a compiled job in wire order: the process items the compiler recorded
    (``Job.manu``) and one class-0 sample item per op-3000 step (dx, dy, power, duty, freq)."""
    by_index = {}  # type: ignore[var-annotated]
    for idx, it in getattr(job, "manu", []) or []:
        by_index.setdefault(idx, []).append(it)
    out: list[ManuItem] = []
    seen = set()
    for i, w in enumerate(job.wire):
        if i in by_index and i not in seen:
            out.extend(by_index[i])
            seen.add(i)
        if w and (w[0] & 0xFFFF) == F.OP_STEP:
            dx, dy, freq, duty, power = F.decode_step(w[1:3])
            out.append(ManuItem(0, dx, dy, power, 0, 0, duty, freq, 0))
    return out


def olpf_text(items: list[ManuItem], begin: int = 1, end: int = 1, last: bool = True) -> str:
    """FUN_100352b0 text (field order O)."""
    lines = [f"Manu_Begin {begin} {end}"]
    for m in items:
        lines.append(" ".join(str(int(v)) for v in (m.value, m.cls & 0xFF, m.freq & 0xFFFF,
                                                    m.duty & 0xFF, m.w10 & 0xFFFF, m.a, m.b,
                                                    m.power, m.c)))
    lines.append("Manu_End")
    txt = "\n".join(lines) + "\n"
    if last:
        txt += f"Size {len(items)}"
    return txt


def pack_container(parts: dict[str, bytes]) -> bytes:
    out = bytearray()
    for name, tag in PARTS:
        if parts.get(name) is not None:
            out += parts[name]
            out += tag
    return bytes(out)


def unpack_container(data: bytes) -> dict[str, bytes]:
    out, pos = {}, 0
    for name, tag in PARTS:
        i = data.find(tag, pos)
        if i < 0:
            continue
        out[name] = data[pos:i]
        pos = i + len(tag)
    return out


def export_offline(path: PathLike, job, doc=None, layer_xml: bytes = b"", thumbnail_jpg: bytes = b"",
                   laser_type: int = 0) -> Path:
    """Write the offline container (.enc for fiber, .encex otherwise)."""
    import tempfile
    p = Path(path)
    want = ".enc" if int(laser_type) == 0 else ".encex"
    if p.suffix.lower() not in (".enc", ".encex"):
        p = p.with_suffix(want)
    chf = b""
    if doc is not None:
        from ..formats.chf import save_chf
        with tempfile.TemporaryDirectory() as td:
            cp = Path(td) / "_tempSource.chf"
            save_chf(doc, str(cp))
            chf = cp.read_bytes()
    items = manu_items(job)
    info = (f"Version=1\nSteps={job.steps}\nItems={len(items)}\nDuration={job.total_time_s:.3f}\n"
            f"LaserType={int(laser_type)}\n").encode()
    p.write_bytes(pack_container({"_tempProcessInfo.olpi": info,
                                  "_tempManuItem.olpf": olpf_text(items).encode(),
                                  "_tempthumbnail.jpg": thumbnail_jpg, "_tempLayer.xml": layer_xml,
                                  "_tempSource.chf": chf}))
    return p


def upload_offline_to_card(mc, name: str, data: bytes, extra: str = "",
                           progress: Optional[Callable[[int], None]] = None) -> None:
    """NC event 0x1a (FUN_10048ad0, O): reg 101 := [7, bytes of "name&extra"], 1400-byte func
    0x26 chunks at their offset, reg 101 := [8].  The original resends failed chunks
    ("Offline upload data error"); here one retry per chunk."""
    if len(data) < 14:
        raise ValueError("open error: offline file shorter than 14 bytes")
    mc.command([CARD_START, *f"{name}&{extra}".encode()])
    for off in range(0, len(data), CARD_CHUNK):
        blob = data[off:off + CARD_CHUNK]
        req = F.Request(F.FUNC_WRITE_BYTES_AT, off, len(blob), list(blob))
        try:
            mc.transact(req)
        except Exception:  # noqa: BLE001
            mc.transact(F.Request(F.FUNC_WRITE_BYTES_AT, off, len(blob), list(blob)))
        if progress:
            progress(round(min(off + len(blob), len(data)) * 100 / len(data)))
    mc.command([CARD_STOP])
