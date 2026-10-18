"""Pure-Python pcap / pcapng reader and writer for the card traffic (no scapy).

Reading:

* containers: classic pcap (µs and ns magic, either byte order) and pcapng (SHB / IDB / EPB /
  SPB / OPB blocks, per-interface ``if_tsresol`` and ``if_tsoffset``, several sections);
* link layers: Ethernet (with 802.1Q / QinQ tags), Linux cooked capture v1 (113) and v2 (276),
  BSD loopback NULL (0) / LOOP (108, Npcap's loopback adapter), raw IPv4 (12, 14, 101, 228);
* IPv4 with fragment reassembly (IPv6 is skipped: the card is IPv4 only), UDP, TCP;
* TCP: per-direction stream reassembly (SYN, out-of-order segments, retransmitted and
  overlapping segments, 32-bit sequence wrap), then split into card frames by the frame's
  LEN field (``AccessType=0``; docs/protocol.md §1-§2).  Checksums are not verified, because
  Windows captures with checksum offload carry wrong ones.

Writing (synthetic captures for the tests): :class:`PcapWriter` / :class:`PcapngWriter` with
Ethernet + IPv4 + UDP/TCP framing and correct checksums.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Optional, Sequence, Union

PathLike = Union[str, Path]

LINKTYPE_NULL = 0
LINKTYPE_ETHERNET = 1
LINKTYPE_RAW_OPENBSD = 12      # DLT_RAW on some systems
LINKTYPE_RAW_14 = 14
LINKTYPE_RAW = 101
LINKTYPE_LOOP = 108
LINKTYPE_LINUX_SLL = 113
LINKTYPE_IPV4 = 228
LINKTYPE_LINUX_SLL2 = 276
RAW_IP_LINKTYPES = (LINKTYPE_RAW_OPENBSD, LINKTYPE_RAW_14, LINKTYPE_RAW, LINKTYPE_IPV4)

ETH_IPV4 = 0x0800
ETH_VLAN = (0x8100, 0x88A8, 0x9100)
PROTO_TCP = 6
PROTO_UDP = 17

PCAP_MAGIC_US = 0xA1B2C3D4
PCAP_MAGIC_NS = 0xA1B23C4D
PCAPNG_SHB = 0x0A0D0D0A
PCAPNG_BOM = 0x1A2B3C4D

TCP_FIN, TCP_SYN, TCP_RST, TCP_PSH, TCP_ACK = 0x01, 0x02, 0x04, 0x08, 0x10

CARD_MAX_FRAME = 0x5A8          # 1448-byte buffers of CMCHalAPI (frames.MAX_FRAME)


class CaptureFormatError(ValueError):
    """The file is not a pcap/pcapng capture, or it is truncated in a header."""


@dataclass
class Packet:
    """One captured link-layer frame."""
    ts: float              # seconds since the epoch
    linktype: int
    data: bytes
    iface: int = 0
    index: int = 0         # running packet number in the file (1-based, as Wireshark)


@dataclass
class IPv4Packet:
    ts: float
    src: str
    dst: str
    proto: int
    payload: bytes
    index: int = 0


@dataclass
class Message:
    """One application message (UDP datagram or one card frame cut from a TCP stream)."""
    ts: float
    src: tuple[str, int]
    dst: tuple[str, int]
    data: bytes
    transport: str = "udp"     # "udp" or "tcp"
    index: int = 0             # packet number of the (last) packet that carried it


# =============================================================================== reading
def read_packets(path_or_file: Union[PathLike, BinaryIO]) -> Iterator[Packet]:
    """Yield the packets of a pcap or pcapng file (format detected from the magic)."""
    if hasattr(path_or_file, "read"):
        data = path_or_file.read()
    else:
        data = Path(path_or_file).read_bytes()
    if len(data) < 4:
        raise CaptureFormatError("file too short for a capture header")
    magic_le = struct.unpack_from("<I", data, 0)[0]
    if magic_le == PCAPNG_SHB:
        yield from _read_pcapng(data)
    elif magic_le in (PCAP_MAGIC_US, PCAP_MAGIC_NS) or struct.unpack_from(">I", data, 0)[0] in (
            PCAP_MAGIC_US, PCAP_MAGIC_NS):
        yield from _read_pcap(data)
    else:
        raise CaptureFormatError(f"unknown capture magic {data[:4].hex()} (need pcap or pcapng)")


def _read_pcap(data: bytes) -> Iterator[Packet]:
    if len(data) < 24:
        raise CaptureFormatError("truncated pcap header")
    for bo in ("<", ">"):
        magic = struct.unpack_from(bo + "I", data, 0)[0]
        if magic in (PCAP_MAGIC_US, PCAP_MAGIC_NS):
            break
    else:  # pragma: no cover - checked by the caller
        raise CaptureFormatError("bad pcap magic")
    div = 1e9 if magic == PCAP_MAGIC_NS else 1e6
    _vmaj, _vmin, _zone, _sig, _snap, network = struct.unpack_from(bo + "HHiIII", data, 4)
    linktype = network & 0x0FFFFFFF        # the upper bits may carry FCS information
    off, n = 24, 0
    while off + 16 <= len(data):
        sec, frac, incl, _orig = struct.unpack_from(bo + "IIII", data, off)
        off += 16
        if off + incl > len(data):
            break                          # truncated last record (capture still running)
        n += 1
        yield Packet(sec + frac / div, linktype, data[off:off + incl], 0, n)
        off += incl


def _tsresol_div(v: int) -> float:
    return float(2 ** (v & 0x7F)) if v & 0x80 else float(10 ** v)


def _read_pcapng(data: bytes) -> Iterator[Packet]:
    off, n = 0, 0
    bo = "<"
    ifaces: list[dict] = []
    last_ts = 0.0
    while off + 12 <= len(data):
        btype_le = struct.unpack_from("<I", data, off)[0]
        if btype_le == PCAPNG_SHB:
            bom = data[off + 8:off + 12]
            bo = "<" if struct.unpack_from("<I", bom)[0] == PCAPNG_BOM else ">"
            ifaces = []                    # interface ids are per section
        btype, blen = struct.unpack_from(bo + "II", data, off)
        if blen < 12 or off + blen > len(data):
            break
        body = data[off + 8:off + blen - 4]
        off += blen
        if btype == PCAPNG_SHB:
            continue
        if btype == 1:                     # Interface Description Block
            lt, _res, _snap = struct.unpack_from(bo + "HHI", body, 0)
            div, tsoff = 1e6, 0.0
            for code, val in _options(body[8:], bo):
                if code == 9 and val:
                    div = _tsresol_div(val[0])
                elif code == 14 and len(val) >= 8:
                    tsoff = float(struct.unpack_from(bo + "q", val)[0])
            ifaces.append({"lt": lt, "div": div, "off": tsoff})
        elif btype == 6:                   # Enhanced Packet Block
            iid, hi, lo, cap, _orig = struct.unpack_from(bo + "IIIII", body, 0)
            iface = ifaces[iid] if iid < len(ifaces) else {"lt": LINKTYPE_ETHERNET, "div": 1e6, "off": 0.0}
            ts = ((hi << 32) | lo) / iface["div"] + iface["off"]
            last_ts = ts
            n += 1
            yield Packet(ts, iface["lt"], body[20:20 + cap], iid, n)
        elif btype == 3:                   # Simple Packet Block (no timestamp, interface 0)
            orig = struct.unpack_from(bo + "I", body, 0)[0]
            iface = ifaces[0] if ifaces else {"lt": LINKTYPE_ETHERNET}
            n += 1
            yield Packet(last_ts, iface["lt"], body[4:4 + min(orig, len(body) - 4)], 0, n)
        elif btype == 2:                   # obsolete Packet Block
            iid, _drops, hi, lo, cap, _orig = struct.unpack_from(bo + "HHIIII", body, 0)
            iface = ifaces[iid] if iid < len(ifaces) else {"lt": LINKTYPE_ETHERNET, "div": 1e6, "off": 0.0}
            ts = ((hi << 32) | lo) / iface["div"] + iface["off"]
            last_ts = ts
            n += 1
            yield Packet(ts, iface["lt"], body[20:20 + cap], iid, n)
        # other blocks (name resolution, statistics, custom, ...) are ignored


def _options(buf: bytes, bo: str) -> Iterator[tuple[int, bytes]]:
    off = 0
    while off + 4 <= len(buf):
        code, ln = struct.unpack_from(bo + "HH", buf, off)
        off += 4
        if code == 0:
            return
        yield code, buf[off:off + ln]
        off += (ln + 3) & ~3


def link_payload(pkt: Packet) -> Optional[bytes]:
    """The IPv4 packet inside a link-layer frame, or None for anything else."""
    d, lt = pkt.data, pkt.linktype
    if lt == LINKTYPE_ETHERNET:
        if len(d) < 14:
            return None
        et, off = struct.unpack_from(">H", d, 12)[0], 14
        while et in ETH_VLAN and len(d) >= off + 4:
            et = struct.unpack_from(">H", d, off + 2)[0]
            off += 4
        return d[off:] if et == ETH_IPV4 else None
    if lt == LINKTYPE_LINUX_SLL:
        return d[16:] if len(d) >= 16 and struct.unpack_from(">H", d, 14)[0] == ETH_IPV4 else None
    if lt == LINKTYPE_LINUX_SLL2:
        return d[20:] if len(d) >= 20 and struct.unpack_from(">H", d, 0)[0] == ETH_IPV4 else None
    if lt in (LINKTYPE_NULL, LINKTYPE_LOOP):
        if len(d) < 4:
            return None
        fam_le, fam_be = struct.unpack_from("<I", d)[0], struct.unpack_from(">I", d)[0]
        fam = fam_be if lt == LINKTYPE_LOOP else (fam_le if fam_le < 0x10000 else fam_be)
        return d[4:] if fam == 2 else None
    if lt in RAW_IP_LINKTYPES:
        return d if d and d[0] >> 4 == 4 else None
    return None


def _ip(b: bytes) -> str:
    return ".".join(str(x) for x in b)


class _Defrag:
    """IPv4 fragment reassembly keyed by (src, dst, proto, id)."""

    def __init__(self):
        self.pending: dict = {}

    def add(self, key, offset: int, more: bool, payload: bytes) -> Optional[bytes]:
        e = self.pending.setdefault(key, {"parts": {}, "total": None})
        e["parts"][offset] = payload
        if not more:
            e["total"] = offset + len(payload)
        if e["total"] is None:
            return None
        buf = bytearray(e["total"])
        have = 0
        for off in sorted(e["parts"]):
            p = e["parts"][off]
            if off > have:
                return None                # a hole remains
            buf[off:off + len(p)] = p
            have = max(have, off + len(p))
        if have < e["total"]:
            return None
        del self.pending[key]
        return bytes(buf[:e["total"]])


def iter_ipv4(packets: Iterable[Packet]) -> Iterator[IPv4Packet]:
    """IPv4 packets (fragments reassembled) from link-layer packets."""
    defrag = _Defrag()
    for pkt in packets:
        d = link_payload(pkt)
        if d is None or len(d) < 20 or d[0] >> 4 != 4:
            continue
        ihl = (d[0] & 0x0F) * 4
        total = struct.unpack_from(">H", d, 2)[0]
        if total < ihl or ihl < 20:
            continue
        ident, frag = struct.unpack_from(">HH", d, 4)
        proto = d[9]
        src, dst = _ip(d[12:16]), _ip(d[16:20])
        payload = d[ihl:total] if total <= len(d) else d[ihl:]   # strips Ethernet padding
        more, foff = bool(frag & 0x2000), (frag & 0x1FFF) * 8
        if more or foff:
            payload = defrag.add((src, dst, proto, ident), foff, more, payload)
            if payload is None:
                continue
        yield IPv4Packet(pkt.ts, src, dst, proto, payload, pkt.index)


class _TcpStream:
    """One direction of a TCP connection: reassembled bytes, cut into card frames."""

    def __init__(self):
        self.next: Optional[int] = None
        self.pending: dict[int, tuple[bytes, float, int]] = {}
        self.buf = bytearray()
        self.retransmitted = 0
        self.resyncs = 0

    def feed(self, seq: int, flags: int, payload: bytes, ts: float, index: int) -> list[tuple[bytes, float, int]]:
        if flags & TCP_SYN:
            self.next = (seq + 1) & 0xFFFFFFFF
            self.buf.clear()
            self.pending.clear()
            seq = self.next
        if not payload:
            return []
        if self.next is None:
            self.next = seq                # capture started mid-connection
        rel = ((seq - self.next + 0x80000000) & 0xFFFFFFFF) - 0x80000000
        if rel > 0:
            self.pending[seq] = (payload, ts, index)
            return []
        if rel < 0:
            if -rel >= len(payload):
                self.retransmitted += 1
                return []
            payload = payload[-rel:]
        self.buf += payload
        self.next = (self.next + len(payload)) & 0xFFFFFFFF
        while self.next in self.pending:   # drain segments that are now in order
            p, _t, _i = self.pending.pop(self.next)
            self.buf += p
            self.next = (self.next + len(p)) & 0xFFFFFFFF
        for s in [s for s in self.pending
                  if ((s - self.next + 0x80000000) & 0xFFFFFFFF) - 0x80000000 < 0]:
            p, t2, i2 = self.pending.pop(s)  # overlapping segment that now starts behind
            r = ((s - self.next + 0x80000000) & 0xFFFFFFFF) - 0x80000000
            if -r < len(p):
                self.buf += p[-r:]
                self.next = (self.next + len(p) + r) & 0xFFFFFFFF
            else:
                self.retransmitted += 1
        return self._frames(ts, index)

    def _frames(self, ts: float, index: int) -> list[tuple[bytes, float, int]]:
        out = []
        while len(self.buf) >= 8:
            ln = struct.unpack_from(">H", self.buf, 4)[0]
            if ln < 2 or 6 + ln > CARD_MAX_FRAME:
                del self.buf[0]             # not a frame boundary: resynchronise byte-wise
                self.resyncs += 1
                continue
            if len(self.buf) < 6 + ln:
                break
            out.append((bytes(self.buf[:6 + ln]), ts, index))
            del self.buf[:6 + ln]
        return out


@dataclass
class ReadStats:
    packets: int = 0
    ipv4: int = 0
    udp: int = 0
    tcp_segments: int = 0
    messages: int = 0
    tcp_retransmitted_segments: int = 0
    tcp_resync_bytes: int = 0
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    linktypes: set = field(default_factory=set)


def read_messages(path_or_file, port: int = 502, host: Optional[str] = None,
                  stats: Optional[ReadStats] = None) -> list[Message]:
    """UDP datagrams and reassembled TCP card frames to or from ``host:port``.

    ``host`` None accepts any address on ``port`` (both directions).  Messages are returned in
    capture order.
    """
    st = stats if stats is not None else ReadStats()
    streams: dict[tuple, _TcpStream] = {}

    def counted(pkts):
        for p in pkts:
            st.packets += 1
            st.linktypes.add(p.linktype)
            st.first_ts = p.ts if st.first_ts is None else min(st.first_ts, p.ts)
            st.last_ts = p.ts if st.last_ts is None else max(st.last_ts, p.ts)
            yield p

    out: list[Message] = []
    for ip in iter_ipv4(counted(read_packets(path_or_file))):
        st.ipv4 += 1
        if ip.proto == PROTO_UDP and len(ip.payload) >= 8:
            sp, dp, ln = struct.unpack_from(">HHH", ip.payload, 0)
            if not _match(ip, sp, dp, port, host):
                continue
            st.udp += 1
            data = ip.payload[8:ln] if 8 <= ln <= len(ip.payload) else ip.payload[8:]
            out.append(Message(ip.ts, (ip.src, sp), (ip.dst, dp), bytes(data), "udp", ip.index))
        elif ip.proto == PROTO_TCP and len(ip.payload) >= 20:
            sp, dp, seq, _ack, offres, flags = struct.unpack_from(">HHIIBB", ip.payload, 0)
            if not _match(ip, sp, dp, port, host):
                continue
            st.tcp_segments += 1
            doff = (offres >> 4) * 4
            key = (ip.src, sp, ip.dst, dp)
            s = streams.setdefault(key, _TcpStream())
            for data, ts, idx in s.feed(seq, flags, ip.payload[doff:], ip.ts, ip.index):
                out.append(Message(ts, (ip.src, sp), (ip.dst, dp), data, "tcp", idx))
    st.tcp_retransmitted_segments = sum(s.retransmitted for s in streams.values())
    st.tcp_resync_bytes = sum(s.resyncs for s in streams.values())
    st.messages = len(out)
    out.sort(key=lambda m: (m.ts, m.index))
    return out


def _match(ip: IPv4Packet, sp: int, dp: int, port: int, host: Optional[str]) -> bool:
    if host is None:
        return port in (sp, dp)
    return (ip.dst == host and dp == port) or (ip.src == host and sp == port)


# =============================================================================== writing
def _csum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def _aton(a: str) -> bytes:
    return bytes(int(x) for x in a.split("."))


def ipv4_packet(src: str, dst: str, proto: int, payload: bytes, ident: int = 0,
                frag_offset: int = 0, more_fragments: bool = False) -> bytes:
    """An IPv4 header (no options, TTL 128, DF unset) followed by ``payload``."""
    flags = (0x2000 if more_fragments else 0) | (frag_offset // 8)
    hdr = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), ident & 0xFFFF, flags, 128,
                      proto, 0, _aton(src), _aton(dst))
    hdr = hdr[:10] + struct.pack(">H", _csum(hdr)) + hdr[12:]
    return hdr + payload


def udp_segment(src: str, dst: str, sport: int, dport: int, payload: bytes) -> bytes:
    ln = 8 + len(payload)
    pseudo = _aton(src) + _aton(dst) + struct.pack(">BBH", 0, PROTO_UDP, ln)
    hdr = struct.pack(">HHHH", sport, dport, ln, 0)
    c = _csum(pseudo + hdr + payload) or 0xFFFF
    return struct.pack(">HHHH", sport, dport, ln, c) + payload


def tcp_segment(src: str, dst: str, sport: int, dport: int, seq: int, ack: int, flags: int,
                payload: bytes = b"", window: int = 64240) -> bytes:
    hdr = struct.pack(">HHIIBBHHH", sport, dport, seq & 0xFFFFFFFF, ack & 0xFFFFFFFF, 5 << 4,
                      flags, window, 0, 0)
    pseudo = _aton(src) + _aton(dst) + struct.pack(">BBH", 0, PROTO_TCP, len(hdr) + len(payload))
    c = _csum(pseudo + hdr + payload)
    return hdr[:16] + struct.pack(">H", c) + hdr[18:] + payload


def ethernet_frame(ip_packet: bytes, src_mac: bytes = b"\x02\x00\x00\x00\x00\x01",
                   dst_mac: bytes = b"\x02\x00\x00\x00\x00\x02") -> bytes:
    f = dst_mac + src_mac + struct.pack(">H", ETH_IPV4) + ip_packet
    return f + b"\0" * max(0, 60 - len(f))          # minimum frame size: padding as on the wire


def frame_for_linktype(ip_packet: bytes, linktype: int) -> bytes:
    """Wrap an IPv4 packet for the given link type (writer side of :func:`link_payload`)."""
    if linktype == LINKTYPE_ETHERNET:
        return ethernet_frame(ip_packet)
    if linktype == LINKTYPE_LINUX_SLL:
        return struct.pack(">HHH8sH", 0, 1, 6, b"\x02\0\0\0\0\x01\0\0", ETH_IPV4) + ip_packet
    if linktype == LINKTYPE_LINUX_SLL2:
        return struct.pack(">HHIHBB8s", ETH_IPV4, 0, 1, 1, 0, 6, b"\x02\0\0\0\0\x01\0\0") + ip_packet
    if linktype == LINKTYPE_NULL:
        return struct.pack("<I", 2) + ip_packet
    if linktype == LINKTYPE_LOOP:
        return struct.pack(">I", 2) + ip_packet
    if linktype in RAW_IP_LINKTYPES:
        return ip_packet
    raise ValueError(f"unsupported link type {linktype}")


class PcapWriter:
    """Classic pcap writer (little-endian, µs or ns timestamps)."""

    def __init__(self, path: PathLike, linktype: int = LINKTYPE_ETHERNET, nanosecond: bool = False):
        self.f = open(path, "wb")
        self.linktype = linktype
        self.ns = nanosecond
        self.f.write(struct.pack("<IHHiIII", PCAP_MAGIC_NS if nanosecond else PCAP_MAGIC_US,
                                 2, 4, 0, 0, 65535, linktype))

    def write(self, ts: float, frame: bytes) -> None:
        sec = int(ts)
        frac = int(round((ts - sec) * (1e9 if self.ns else 1e6)))
        if frac >= (10 ** 9 if self.ns else 10 ** 6):
            sec, frac = sec + 1, 0
        self.f.write(struct.pack("<IIII", sec, frac, len(frame), len(frame)) + frame)

    def close(self) -> None:
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class PcapngWriter:
    """pcapng writer: one section, one interface, Enhanced Packet Blocks."""

    def __init__(self, path: PathLike, linktype: int = LINKTYPE_ETHERNET, tsresol: int = 6):
        self.f = open(path, "wb")
        self.linktype = linktype
        self.div = _tsresol_div(tsresol)
        self._block(PCAPNG_SHB, struct.pack("<IHHq", PCAPNG_BOM, 1, 0, -1))
        opts = b""
        if tsresol != 6:
            opts = struct.pack("<HHB3x", 9, 1, tsresol) + struct.pack("<HH", 0, 0)
        self._block(1, struct.pack("<HHI", linktype, 0, 65535) + opts)

    def _block(self, btype: int, body: bytes) -> None:
        body += b"\0" * (-len(body) % 4)
        n = 12 + len(body)
        self.f.write(struct.pack("<II", btype, n) + body + struct.pack("<I", n))

    def write(self, ts: float, frame: bytes) -> None:
        t = int(round(ts * self.div))
        self._block(6, struct.pack("<IIIII", 0, t >> 32, t & 0xFFFFFFFF, len(frame), len(frame)) + frame)

    def close(self) -> None:
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def write_capture(path: PathLike, messages: Sequence[Message], fmt: str = "pcap",
                  transport: str = "udp", linktype: int = LINKTYPE_ETHERNET,
                  tcp_mss: Optional[int] = None, nanosecond: bool = False) -> None:
    """Write card messages as a capture file (tests and demos).

    ``transport="udp"``: one datagram per message.  ``"tcp"``: a connection per (client,
    server) pair with a SYN handshake, each message as one segment, or cut into ``tcp_mss``
    byte segments when given.  IP ids increase per packet.
    """
    w = (PcapngWriter(path, linktype, 9 if nanosecond else 6) if fmt == "pcapng"
         else PcapWriter(path, linktype, nanosecond))
    ident = 1
    seqs: dict[tuple, int] = {}
    with w:
        for m in messages:
            (sa, sp), (da, dp) = m.src, m.dst
            if transport == "udp":
                w.write(m.ts, frame_for_linktype(
                    ipv4_packet(sa, da, PROTO_UDP, udp_segment(sa, da, sp, dp, m.data), ident), linktype))
                ident += 1
                continue
            key, rkey = (sa, sp, da, dp), (da, dp, sa, sp)
            if key not in seqs:
                seqs[key] = 1000 + 7919 * len(seqs)
                seqs.setdefault(rkey, 5000 + 7919 * len(seqs))
                w.write(m.ts - 1e-6, frame_for_linktype(ipv4_packet(
                    sa, da, PROTO_TCP, tcp_segment(sa, da, sp, dp, seqs[key] - 1, 0, TCP_SYN), ident), linktype))
                ident += 1
                w.write(m.ts - 5e-7, frame_for_linktype(ipv4_packet(
                    da, sa, PROTO_TCP, tcp_segment(da, sa, dp, sp, seqs[rkey] - 1, seqs[key],
                                                   TCP_SYN | TCP_ACK), ident), linktype))
                ident += 1
            step = tcp_mss or len(m.data)
            for off in range(0, len(m.data), step):
                chunk = m.data[off:off + step]
                w.write(m.ts, frame_for_linktype(ipv4_packet(
                    sa, da, PROTO_TCP, tcp_segment(sa, da, sp, dp, seqs[key], seqs.get(rkey, 0),
                                                   TCP_ACK | TCP_PSH, chunk), ident), linktype))
                ident += 1
                seqs[key] = (seqs[key] + len(chunk)) & 0xFFFFFFFF
