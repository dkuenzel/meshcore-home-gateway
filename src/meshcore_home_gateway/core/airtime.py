"""LoRa time on air and the on-air size of MeshCore text packets (ADR 0008).

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
