"""On-air packets as the radio logs them (`LOG_RX_DATA`), and the advert inside one.

The radio pushes every packet it receives, before it handles it (src/Dispatcher.cpp:199), in the
wire format of src/Packet.cpp:65-85. An advert parsed from such a log is **unverified**: the
bridge cannot check its signature (Ed25519 is not in the standard library). The radio checks it
and then pushes `ADVERT` or `NEW_ADVERT` for the same key; only that push makes a logged advert
trustworthy, and only if the logged timestamp matches the record the radio then reports.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from meshcore_home_gateway.core.model import Position
from meshcore_home_gateway.protocol.codec import position

PUB_KEY_BYTES = 32  # src/MeshCore.h:8
SIGNATURE_BYTES = 64  # src/MeshCore.h:11
MAX_PATH_BYTES = 64  # src/MeshCore.h:22
MAX_ADVERT_DATA_BYTES = 32  # src/MeshCore.h:12; the radio ignores app data beyond (Mesh.cpp:269)
PAYLOAD_TYPE_RESPONSE = 0x01  # src/Packet.h:20
PAYLOAD_TYPE_ADVERT = 0x04  # src/Packet.h:23
_TRANSPORT_ROUTES = (0x00, 0x03)  # ROUTE_TYPE_TRANSPORT_FLOOD and _DIRECT, src/Packet.h:13-16
_FLOOD_ROUTES = (0x00, 0x01)
# advert app data flags, src/helpers/AdvertDataHelpers.h:14-17
_HAS_POSITION, _HAS_FEATURE_1, _HAS_FEATURE_2, _HAS_NAME = 0x10, 0x20, 0x40, 0x80


@dataclass(frozen=True, slots=True)
class Packet:
    route: int  # bits 0-1 of the header
    payload_type: int  # bits 2-5
    version: int  # bits 6-7
    path_hashes: tuple[bytes, ...]  # repeaters passed so far (flood) or still ahead (direct)
    payload: bytes

    @property
    def flood(self) -> bool:
        return self.route in _FLOOD_ROUTES


@dataclass(frozen=True, slots=True)
class AdvertData:
    public_key: bytes
    timestamp: int  # the sender's clock
    node_type: int  # NodeType
    name: str | None
    latitude_e6: int | None  # None: the advert carries no position
    longitude_e6: int | None

    @property
    def shares_position(self) -> bool:
        return self.latitude_e6 is not None

    @property
    def position(self) -> Position | None:
        if self.latitude_e6 is None or self.longitude_e6 is None:
            return None
        return position(self.latitude_e6, self.longitude_e6)


def parse_packet(raw: bytes) -> Packet | None:
    """A packet in wire format; None for anything the firmware would reject as bad encoding."""
    if not raw:
        return None
    header, i = raw[0], 1
    route = header & 0x03
    if route in _TRANSPORT_ROUTES:
        i += 4  # two uint16 transport codes
    if len(raw) <= i:
        return None
    path_len = raw[i]
    i += 1
    count, size = path_len & 0x3F, (path_len >> 6) + 1
    if size == 4 or count * size > MAX_PATH_BYTES:  # Packet::isValidPathLen, src/Packet.cpp:13-18
        return None
    path = raw[i : i + count * size]
    i += count * size
    if len(path) < count * size or i >= len(raw):  # no payload: bad encoding (src/Packet.cpp:80)
        return None
    hashes = tuple(path[k * size : (k + 1) * size] for k in range(count))
    return Packet(route, (header >> 2) & 0x0F, header >> 6, hashes, raw[i:])


def parse_advert(packet: Packet) -> AdvertData | None:
    """The advert in a packet (src/Mesh.cpp:252-290, AdvertDataHelpers.cpp:39-68); None if the
    packet is none or too short."""
    if packet.payload_type != PAYLOAD_TYPE_ADVERT:
        return None
    p = packet.payload
    start = PUB_KEY_BYTES + 4 + SIGNATURE_BYTES
    if len(p) <= start:  # at least the flags byte of the app data
        return None
    key, timestamp = p[:PUB_KEY_BYTES], struct.unpack_from("<I", p, PUB_KEY_BYTES)[0]
    data = p[start : start + MAX_ADVERT_DATA_BYTES]
    flags, i = data[0], 1
    latitude = longitude = None
    if flags & _HAS_POSITION:
        if len(data) < i + 8:
            return None
        latitude, longitude = struct.unpack_from("<ii", data, i)
        i += 8
    i += 2 * bool(flags & _HAS_FEATURE_1) + 2 * bool(flags & _HAS_FEATURE_2)
    if len(data) < i:
        return None
    name = None
    if flags & _HAS_NAME:  # the firmware copies the rest and ends it with a NUL
        name = data[i:].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    return AdvertData(key, timestamp, flags & 0x0F, name or None, latitude, longitude)


def flooded_response_hashes(raw: bytes) -> tuple[int, int] | None:
    """Destination and source hash of a flooded RESPONSE packet, the first two payload bytes
    before MAC and ciphertext (src/Mesh.cpp:488-509); None for any other packet. Unverified: the
    radio logs it before it decrypts it."""
    packet = parse_packet(raw)
    if packet is None or not packet.flood or packet.payload_type != PAYLOAD_TYPE_RESPONSE:
        return None
    if len(packet.payload) < 4:  # the two hashes and the MAC
        return None
    return packet.payload[0], packet.payload[1]
