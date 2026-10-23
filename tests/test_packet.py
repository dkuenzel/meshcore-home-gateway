"""On-air packets from LOG_RX_DATA and the adverts in them, against frames built from the wire
format (src/Packet.cpp:65-85, src/Mesh.cpp:252-290, src/helpers/AdvertDataHelpers.cpp at
MeshCore companion-v1.18.0)."""

import struct

import pytest

from meshcore_home_gateway.protocol.codec import Position
from meshcore_home_gateway.protocol.packet import AdvertData, parse_advert, parse_packet

KEY = bytes(range(0x40, 0x60))
TS = 1_760_000_000
SIGNATURE = bytes(64)


def advert_payload(app_data: bytes) -> bytes:
    return KEY + struct.pack("<I", TS) + SIGNATURE + app_data


def app_data(name: bytes = b"Alice", position: tuple[int, int] | None = None) -> bytes:
    flags, body = 0x80 | 0x01, b""  # name, chat
    if position is not None:
        flags |= 0x10
        body += struct.pack("<ii", *position)
    return bytes([flags]) + body + name


def test_a_flooded_advert_with_position() -> None:
    raw = bytes([0x11, 0x02, 0xAA, 0xBB]) + advert_payload(
        app_data(position=(53_550_000, 9_990_000))
    )
    packet = parse_packet(raw)
    assert packet is not None
    assert (packet.route, packet.payload_type, packet.version, packet.flood) == (1, 4, 0, True)
    assert packet.path_hashes == (b"\xaa", b"\xbb")
    advert = parse_advert(packet)
    assert advert == AdvertData(KEY, TS, 1, "Alice", 53_550_000, 9_990_000)
    assert advert is not None
    assert advert.shares_position
    assert advert.position == Position(53.55, 9.99)


def test_an_advert_without_position_with_transport_codes_and_two_byte_hashes() -> None:
    raw = bytes([0x10]) + b"\x01\x02\x03\x04" + bytes([0x41, 0xAA, 0xBB])
    packet = parse_packet(raw + advert_payload(app_data()))
    assert packet is not None
    assert packet.path_hashes == (b"\xaa\xbb",)
    advert = parse_advert(packet)
    assert advert is not None
    assert not advert.shares_position
    assert advert.position is None
    assert advert.name == "Alice"


def test_feature_fields_are_skipped_and_the_name_is_cut_like_the_firmware() -> None:
    data = bytes([0x80 | 0x20 | 0x40 | 0x02]) + b"\x01\x00\x02\x00" + b"R" * 40
    packet = parse_packet(bytes([0x12, 0x00]) + advert_payload(data))  # direct, no path
    assert packet is not None
    assert not packet.flood
    advert = parse_advert(packet)
    assert advert is not None
    assert advert.node_type == 2  # repeater
    assert advert.name == "R" * 27  # 32 bytes of app data: flags, 4 feature bytes, 27 of name


def test_a_name_stops_at_a_nul_and_an_advert_may_have_none() -> None:
    packet = parse_packet(bytes([0x11, 0x00]) + advert_payload(app_data(b"Bob\x00junk")))
    assert packet is not None
    advert = parse_advert(packet)
    assert advert is not None
    assert advert.name == "Bob"
    packet = parse_packet(bytes([0x11, 0x00]) + advert_payload(b"\x01"))
    assert packet is not None
    advert = parse_advert(packet)
    assert advert is not None
    assert advert.name is None


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        bytes([0x11]),  # no path length
        bytes([0x10, 1, 2]),  # transport codes cut
        bytes([0x11, 0xC1, 0xAA]),  # hash size 4 is reserved
        bytes([0x11, 0x7F]) + bytes(140),  # 63 two-byte hashes: more than 64 bytes of path
        bytes([0x11, 0x02, 0xAA]),  # path cut
        bytes([0x11, 0x01, 0xAA]),  # no payload
    ],
)
def test_bad_encodings_are_none(raw: bytes) -> None:
    assert parse_packet(raw) is None


@pytest.mark.parametrize(
    "payload",
    [
        advert_payload(b""),  # no app data
        advert_payload(bytes([0x91]) + b"\x00\x00"),  # position cut
        advert_payload(bytes([0xA1]) + b"\x00"),  # feature field cut
    ],
)
def test_short_adverts_are_none(payload: bytes) -> None:
    packet = parse_packet(bytes([0x11, 0x00]) + payload)
    assert packet is not None
    assert parse_advert(packet) is None


def test_other_packets_are_no_adverts() -> None:
    packet = parse_packet(bytes([0x09, 0x00]) + b"\x12\x34" + bytes(20))  # text message
    assert packet is not None
    assert packet.payload_type == 2
    assert parse_advert(packet) is None
