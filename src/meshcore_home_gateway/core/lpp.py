"""CayenneLPP as MeshCore companions answer a telemetry request (ADR 0018).

The firmware pins the CayenneLPP library 1.6.1: sizes and multipliers from CayenneLPP.h:27-111,
signedness from CayenneLPP.cpp:556-589 (voltage and current are signed although the header's
comments say otherwise). The decoder is total: it never raises, stops at channel 0 (the AES zero
padding: the firmware never uses channel 0) and at an unknown type or a value cut short, and keeps
what came before. Values are what the encoder sent: truncated toward zero, unsigned types wrapped.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from meshcore_home_gateway.core.model import Position


@dataclass(frozen=True, slots=True)
class LppType:
    name: str
    sizes: tuple[int, ...]  # bytes of each value, big endian
    divisors: tuple[int, ...]  # the library's multiplier per value
    signed: bool  # two's complement in its size


GPS = 136
SELF_CHANNEL = 1  # TELEM_CHANNEL_SELF: the companion's own readings, its position among them
TYPES: Mapping[int, LppType] = MappingProxyType({
    0: LppType("digital_input", (1,), (1,), False),
    1: LppType("digital_output", (1,), (1,), False),
    2: LppType("analog_input", (2,), (100,), True),
    3: LppType("analog_output", (2,), (100,), True),
    100: LppType("generic", (4,), (1,), False),
    101: LppType("luminosity", (2,), (1,), False),
    102: LppType("presence", (1,), (1,), False),
    103: LppType("temperature", (2,), (10,), True),
    104: LppType("humidity", (1,), (2,), False),
    113: LppType("accelerometer", (2, 2, 2), (1000, 1000, 1000), True),
    115: LppType("barometric_pressure", (2,), (10,), False),
    116: LppType("voltage", (2,), (100,), True),
    117: LppType("current", (2,), (1000,), True),
    118: LppType("frequency", (4,), (1,), False),
    120: LppType("percentage", (1,), (1,), False),
    121: LppType("altitude", (2,), (1,), True),  # barometric, never the position's altitude
    125: LppType("concentration", (2,), (1,), False),
    128: LppType("power", (2,), (1,), False),
    130: LppType("distance", (4,), (1000,), False),
    131: LppType("energy", (4,), (1000,), False),
    132: LppType("direction", (2,), (1,), False),
    133: LppType("unix_time", (4,), (1,), False),
    134: LppType("gyrometer", (2, 2, 2), (100, 100, 100), True),
    135: LppType("colour", (1, 1, 1), (1, 1, 1), False),
    GPS: LppType("gps", (3, 3, 3), (10000, 10000, 100), True),
    142: LppType("switch", (1,), (1,), False),
})  # fmt: skip
MAX_LATITUDE_CELLS, MAX_LONGITUDE_CELLS = 900_000, 1_800_000


@dataclass(frozen=True, slots=True)
class Reading:
    channel: int  # 1..255
    lpp_type: int
    occurrence: int  # earlier readings with the same channel and type in this answer
    raw: tuple[int, ...]  # the integers as sent, signed where the type is

    @property
    def values(self) -> tuple[float, ...]:
        kind = TYPES[self.lpp_type]
        return tuple(r / d for r, d in zip(self.raw, kind.divisors, strict=True))


@dataclass(frozen=True, slots=True)
class LppPayload:
    readings: tuple[Reading, ...]
    lpp_bytes: int  # bytes decoded plus `undecoded`: the size of the answer for airtime
    undecoded: bytes  # from an unknown type or a cut value on, trailing zeros stripped


@dataclass(frozen=True, slots=True)
class LppPosition:
    """A GPS reading in its cells of 1e-4 degrees, as the encoder truncated it."""

    lat_cells: int
    lon_cells: int
    altitude_cm: int

    @property
    def cells(self) -> tuple[int, int]:
        return self.lat_cells, self.lon_cells

    @property
    def position(self) -> Position:
        return centre(self.lat_cells, self.lon_cells)

    @property
    def altitude_m(self) -> float:
        return self.altitude_cm / 100


def decode(data: bytes) -> LppPayload:
    """The readings of an answer (the bytes after the prefix of a 0x8B); never raises."""
    readings: list[Reading] = []
    seen: dict[tuple[int, int], int] = {}
    i = 0
    while i + 2 <= len(data):
        channel, code = data[i], data[i + 1]
        if channel == 0:  # AES zero padding
            break
        kind = TYPES.get(code)
        if kind is None:  # an unknown type has no known size: no resynchronisation
            break
        end = i + 2 + sum(kind.sizes)
        if end > len(data):
            break
        raw, offset = [], i + 2
        for size in kind.sizes:
            raw.append(int.from_bytes(data[offset : offset + size], "big", signed=kind.signed))
            offset += size
        occurrence = seen.get((channel, code), 0)
        seen[(channel, code)] = occurrence + 1
        readings.append(Reading(channel, code, occurrence, tuple(raw)))
        i = end
    rest = data[i:].rstrip(b"\x00")
    return LppPayload(tuple(readings), i + len(rest), rest)


def position(payload: LppPayload) -> LppPosition | None:
    """The companion's position: the one valid GPS reading on its own channel. None without one,
    or with two that differ (never guess). (0, 0) means no fix yet or a cleared position."""
    found: set[tuple[int, int, int]] = set()
    for reading in payload.readings:
        if reading.lpp_type != GPS or reading.channel != SELF_CHANNEL:
            continue
        lat, lon, alt = reading.raw
        if abs(lat) > MAX_LATITUDE_CELLS or abs(lon) > MAX_LONGITUDE_CELLS or lat == lon == 0:
            continue
        found.add((lat, lon, alt))
    if len(found) != 1:
        return None
    ((lat, lon, alt),) = found
    return LppPosition(lat, lon, alt)


def centre(lat_cells: int, lon_cells: int) -> Position:
    """The middle of a cell: the encoder truncates toward zero (CayenneLPP.cpp:847-866)."""
    return Position(_centre(lat_cells, 90.0), _centre(lon_cells, 180.0))


def _centre(cells: int, limit: float) -> float:
    if cells == 0:
        return 0.0
    middle = (cells + (0.5 if cells > 0 else -0.5)) / 10_000
    return round(max(-limit, min(limit, middle)), 5)
