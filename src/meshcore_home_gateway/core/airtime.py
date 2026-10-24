"""LoRa time on air and the on-air size of MeshCore packets (ADR 0008, ADR 0018).

Time on air follows the Semtech SX126x datasheet (section 6.1.4, "LoRa Time-on-Air"); the
values are checked against independent references in tests/test_airtime.py. Packet sizes follow
the MeshCore packet format (docs/protocol.md, "On-air sizes").
"""

from __future__ import annotations

import math
from dataclasses import dataclass

LDRO_SYMBOL_TIME_S = 0.016  # low data rate optimisation from 16 ms symbol time (datasheet)


@dataclass(frozen=True, slots=True)
class LoRaParams:
    """Modulation parameters. coding_rate is the denominator: 5..8 for 4/5..4/8."""

    bandwidth_hz: float
    spreading_factor: int
    coding_rate: int
    preamble_symbols: int
    explicit_header: bool = True
    crc: bool = True
    low_data_rate_optimize: bool | None = None  # None: on when the symbol time exceeds 16 ms

    def __post_init__(self) -> None:
        if not 5 <= self.spreading_factor <= 12:
            raise ValueError(f"spreading factor must be 5..12, got {self.spreading_factor}")
        if not 5 <= self.coding_rate <= 8:
            raise ValueError(f"coding rate denominator must be 5..8, got {self.coding_rate}")
        if self.bandwidth_hz <= 0 or self.preamble_symbols < 0:
            raise ValueError("bandwidth must be positive and the preamble non-negative")

    @property
    def symbol_time_s(self) -> float:
        return float(2**self.spreading_factor) / self.bandwidth_hz

    @property
    def ldro(self) -> bool:
        if self.low_data_rate_optimize is not None:
            return self.low_data_rate_optimize
        return self.symbol_time_s > LDRO_SYMBOL_TIME_S


def time_on_air_s(payload_bytes: int, p: LoRaParams) -> float:
    """Duration of one LoRa packet with `payload_bytes` of PHY payload (SX126x datasheet)."""
    if payload_bytes < 0:
        raise ValueError("payload size must not be negative")
    sf, cr_index = p.spreading_factor, p.coding_rate - 4
    crc_bits = 16 if p.crc else 0
    header_bits = 20 if p.explicit_header else 0
    if sf <= 6:  # SF5 and SF6: longer preamble tail, no LDRO term
        preamble = p.preamble_symbols + 6.25
        bits = 8 * payload_bytes + crc_bits - 4 * sf + header_bits
        per_block = 4 * sf
    else:
        preamble = p.preamble_symbols + 4.25
        bits = 8 * payload_bytes + crc_bits - 4 * sf + 8 + header_bits
        per_block = 4 * (sf - 2) if p.ldro else 4 * sf
    payload_symbols = 8 + math.ceil(max(bits, 0) / per_block) * (cr_index + 4)
    return (preamble + payload_symbols) * p.symbol_time_s


# --- MeshCore packet sizes (docs/protocol.md, "On-air sizes") ---------------------------------

HEADER_BYTES = 1
PATH_LEN_BYTES = 1
TRANSPORT_CODE_BYTES = 4  # only on floods with an active flood scope; counted to stay safe
CIPHER_BLOCK = 16
# Path packet the radio returns after a flooded acknowledgement, sized for a way back of 4 path
# bytes (4 hops with one-byte hashes); a longer way makes it larger (docs/protocol.md).
RECIPROCAL_PATH_BYTES = 26


def meshcore_preamble(spreading_factor: int) -> int:
    """Preamble symbols MeshCore 1.16+ uses: 32 up to SF 8, 16 above."""
    return 32 if spreading_factor <= 8 else 16


def meshcore_lora(bandwidth_hz: float, spreading_factor: int, coding_rate: int) -> LoRaParams:
    """The modulation of a MeshCore radio with these settings."""
    return LoRaParams(
        bandwidth_hz=bandwidth_hz,
        spreading_factor=spreading_factor,
        coding_rate=coding_rate,
        preamble_symbols=meshcore_preamble(spreading_factor),
    )


def direct_message_bytes(text_bytes: int, path_bytes: int, flood: bool) -> int:
    """On-air size of a direct text message as this radio first transmits it.

    Payload: destination and source hash (1 + 1), MAC (2), AES-128 blocks of timestamp (4),
    flags (1) and text. A flood leaves with an empty path; a direct send carries the stored path.
    """
    payload = 4 + CIPHER_BLOCK * math.ceil((5 + text_bytes) / CIPHER_BLOCK)
    route = TRANSPORT_CODE_BYTES if flood else path_bytes
    return HEADER_BYTES + route + PATH_LEN_BYTES + payload


def channel_message_bytes(text_bytes: int, name_bytes: int) -> int:
    """On-air size of a channel message: channel hash (1), MAC (2), AES-128 blocks of timestamp,
    flags and "<name>: <text>". Channel messages always flood."""
    payload = 3 + CIPHER_BLOCK * math.ceil((7 + name_bytes + text_bytes) / CIPHER_BLOCK)
    return HEADER_BYTES + TRANSPORT_CODE_BYTES + PATH_LEN_BYTES + payload


def direct_attempt_airtime_s(text_bytes: int, path_bytes: int, flood: bool, p: LoRaParams) -> float:
    """Airtime this radio spends on one attempt of a direct message, including the path packet
    it sends back after a flooded acknowledgement (estimated as RECIPROCAL_PATH_BYTES)."""
    airtime = time_on_air_s(direct_message_bytes(text_bytes, path_bytes, flood), p)
    if flood:
        airtime += time_on_air_s(RECIPROCAL_PATH_BYTES, p)
    return airtime


# --- telemetry requests (ADR 0018) --------------------------------------------------------------

REQUEST_PLAINTEXT_BYTES = 13  # tag 4, type 1, reserved 4, random 4 (BaseChatMesh.cpp:693-698)
FLOOD_REPEATERS = 20  # repeaters assumed to retransmit a flood: an estimate, see protocol.md


@dataclass(frozen=True, slots=True)
class Costs:
    own_s: float  # the gateway radio's own transmit time
    mesh_s: float  # estimated airtime of every transmission a request causes, in all the mesh


def telemetry_request_bytes(path_bytes: int, flood: bool) -> int:
    """A telemetry request as this radio sends it: 22 bytes plus the path direct, 26 flooded."""
    payload = 4 + CIPHER_BLOCK * math.ceil(REQUEST_PLAINTEXT_BYTES / CIPHER_BLOCK)
    route = TRANSPORT_CODE_BYTES if flood else path_bytes
    return HEADER_BYTES + route + PATH_LEN_BYTES + payload


def telemetry_reply_bytes(lpp_bytes: int, path_bytes: int) -> int:
    """The companion's direct answer: tag and LPP in AES blocks (BaseChatMesh.cpp:321-326)."""
    payload = 4 + CIPHER_BLOCK * math.ceil((4 + lpp_bytes) / CIPHER_BLOCK)
    return HEADER_BYTES + path_bytes + PATH_LEN_BYTES + payload


def path_return_bytes(lpp_bytes: int, return_path_bytes: int) -> int:
    """The answer to a flooded request as it leaves the companion: path length, path, type,
    tag and LPP in AES blocks (Mesh.cpp:452-480)."""
    payload = 4 + CIPHER_BLOCK * math.ceil((6 + return_path_bytes + lpp_bytes) / CIPHER_BLOCK)
    return HEADER_BYTES + TRANSPORT_CODE_BYTES + PATH_LEN_BYTES + payload


def return_path_retry_bytes(flood_path_bytes: int, out_path_bytes: int) -> int:
    """The path this radio sends back 3 s after a flooded answer (BaseChatMesh.cpp:385-390):
    path length, the answer's path, a type and 4 random bytes (Mesh.cpp:466-479)."""
    payload = 4 + CIPHER_BLOCK * math.ceil((6 + flood_path_bytes) / CIPHER_BLOCK)
    return HEADER_BYTES + out_path_bytes + PATH_LEN_BYTES + payload


def direct_poll_costs(hops: int, path_bytes: int, lpp_bytes: int, p: LoRaParams) -> Costs:
    """A direct request and its direct answer, every transmission both ways: each repeater
    removes its hash before it forwards (Mesh.cpp:102-107), so transmission k carries
    hops - k hashes."""
    per_hop = path_bytes // hops if hops else 0
    mesh = sum(
        time_on_air_s(telemetry_request_bytes((hops - k) * per_hop, False), p)
        + time_on_air_s(telemetry_reply_bytes(lpp_bytes, (hops - k) * per_hop), p)
        for k in range(hops + 1)
    )
    return Costs(time_on_air_s(telemetry_request_bytes(path_bytes, False), p), mesh)


def flood_poll_costs(lpp_bytes: int, p: LoRaParams, repeaters: int | None = None) -> Costs:
    """A flooded request, its flooded path return, both repeated by `repeaters` (None:
    FLOOD_REPEATERS), and the path this radio sends back 500 ms later (Mesh.cpp:174-177)."""
    repeaters = FLOOD_REPEATERS if repeaters is None else repeaters
    request = time_on_air_s(telemetry_request_bytes(0, True), p)
    answer = time_on_air_s(path_return_bytes(lpp_bytes, 0), p)
    reciprocal = time_on_air_s(RECIPROCAL_PATH_BYTES, p)
    return Costs(request + reciprocal, (repeaters + 1) * (request + answer) + reciprocal)


def flooded_reply_costs(lpp_bytes: int, hops: int, path_bytes: int, p: LoRaParams) -> Costs:
    """An answer to a direct request that the companion flooded (it had no path back), repeated
    by FLOOD_REPEATERS, and the path this radio sends back over its own path 3 s later."""
    reply = time_on_air_s(telemetry_reply_bytes(lpp_bytes, 0) + TRANSPORT_CODE_BYTES, p)
    retry = time_on_air_s(return_path_retry_bytes(path_bytes, path_bytes), p)
    return Costs(retry, (FLOOD_REPEATERS + 1) * reply + (hops + 1) * retry)
