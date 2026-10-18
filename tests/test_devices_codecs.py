"""Codec round trips; byte layouts from NCModule encoders (see mlaser/devices/codecs.py)."""
import pytest

from mlaser.devices import codecs as C


def test_rtu_crc_standard_vector():
    # classic Modbus example: 01 03 00 00 00 02 -> C4 0B (tables 0x1008a580/0x1008a680)
    assert C.rtu_crc(bytes.fromhex("010300000002")) == bytes.fromhex("c40b")


def test_serial_modbus_read_frame():
    # syncReadAFReg FUN_1004bee0: [3, addr, 2] with CSerialModbus (slave 1)
    assert C.SERIAL_MODBUS.encode([3, 1000, 28]).hex() == "010303e8001c" + C.rtu_crc(bytes.fromhex("010303e8001c")).hex()


def test_ext_card_write_u32_pairs():
    # syncWriteECReg FUN_1004c280: [0x10, 101, 2, v & 0xffff, v >> 16]
    vec = [0x10, 101, 2] + C.split_u32([0x12345678])
    f = C.EXT_CARD_MODBUS.encode(vec)
    assert f[:-2].hex() == "02100065000204" + "5678" + "1234"
    assert C.EXT_CARD_MODBUS.decode_request(f) == vec


def test_rtu_reply_decode_and_errors():
    c = C.SERIAL_MODBUS
    rep = c.encode_reply([3, 0x1111, 0x2222])
    assert c.decode(rep) == [3, 4, 0x1111, 0x2222]
    assert c.decode(c.encode_reply([0x83, 2])) == [0x83, 2]
    with pytest.raises(C.CodecError):
        c.decode(rep[:-1] + bytes([rep[-1] ^ 1]))
    with pytest.raises(C.CodecError):
        C.EXT_CARD_MODBUS.decode(rep)                 # wrong slave


def test_bytes_at_little_endian():
    f = C.SERIAL_MODBUS.encode([0x26, 0x01020304, 3, 9, 8, 7])
    assert f[:-2].hex() == "0126" "04030201" "0300" "090807"


def test_u32_split_join_signed():
    assert C.join_u32(C.split_u32([-5, 7])) == [-5, 7]


def test_std_modbus_quirk_length():
    b = C.encode_std_modbus(0x03, 0x0010, count=2)
    assert b == bytes.fromhex("0000000000060003" "00100002")
    w = C.encode_std_modbus(0x10, 5, [1, 2])
    assert w[4:6] == b"\x00\x0a"          # 2*len([f,a,c,v,v]) = 10, real Modbus/TCP would be 11
    assert C.STD_MODBUS.decode_request(w) == [0x10, 5, 2, 1, 2]
    assert C.STD_MODBUS.decode(C.STD_MODBUS.encode_reply([3, 7, 8])) == [3, 4, 7, 8]


def test_ftc61_tunnel():
    f = C.FTC61_MODBUS.encode([3, 1000, 2])
    rtu = C.SERIAL_MODBUS.encode([3, 1000, 2])
    assert f[:8] == bytes([0, 0, 0, 0, 0, len(rtu) + 4, 0, 0x21]) and f[8:10] == bytes([len(rtu), 0])
    assert C.Ftc61Codec.tunnel_payload(f) == rtu
    reply = C.Ftc61Codec.encode_tunnel_reply(b"\x01\x02\x03")
    assert C.FTC61_MODBUS.decode(reply) == [0x20, 3, 1, 2, 3]


def test_raw_codecs():
    assert C.IPG_RAW.encode(list(b"EMON\r")) == b"EMON\r"
    with pytest.raises(C.CodecError):
        C.RAYCUS_RAW.encode([0x1b, 0x4f])          # FUN_100290e0 needs >= 3 elements
    with pytest.raises(C.CodecError):
        C.IPG_RAW.decode(b"ok")                     # FUN_1002aac0 needs >= 3 bytes
