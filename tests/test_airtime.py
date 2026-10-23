"""LoRa time on air against independent published values, and MeshCore packet sizes.

Reference values (none computed by us):
* TTN airtime calculator test suite, avbentem/airtime-calculator @ af41bc5,
  src/lora/Airtime.test.ts (lines 7, 11, 40, 44, 49-50, 53);
* mobilefish.com LoRa tutorial part 17 (rounded to whole milliseconds);
* tanupoo/lorawan_toa README (`toa.py 12 64 -v`);
* ifTNT/lora-air-time readme lines 38-45: measured with an SDR on an SX1276;
* Semtech AN1200.13, Table 3 (10-byte packet; explicit header and CRC off reproduce it).
The EU/UK Narrow values come from a second method, RadioLib's SX126x::getTimeOnAir (integer
microseconds), the code MeshCore runs.
"""

import math

import pytest

from meshcore_home_gateway.core.airtime import (
    RECIPROCAL_PATH_BYTES,
    LoRaParams,
    channel_message_bytes,
    direct_attempt_airtime_s,
    direct_message_bytes,
    meshcore_preamble,
    time_on_air_s,
)


def lora(sf: int, bw_khz: float, cr: int, preamble: int = 8, **kw: bool) -> LoRaParams:
    return LoRaParams(bw_khz * 1000, sf, cr, preamble, **kw)


@pytest.mark.parametrize(
    ("params", "payload", "expected_ms", "tolerance_ms"),
    [
        (lora(7, 125, 5), 14, 46.336, 1e-6),  # TTN Airtime.test.ts:7
        (lora(7, 125, 5), 16, 51.456, 1e-6),  # TTN :11
        (lora(8, 125, 5), 14, 82.432, 1e-6),  # TTN :40
        (lora(8, 125, 5), 25, 113.152, 1e-6),  # TTN :44
        (lora(11, 125, 5), 14, 659.456, 1e-6),  # TTN :49-50, LDRO on
        (lora(11, 125, 5), 25, 823.296, 1e-6),  # TTN :53
        (lora(7, 125, 5), 10, 41, 0.5),  # mobilefish part 17
        (lora(9, 125, 5), 10, 144, 0.5),
        (lora(12, 125, 5), 10, 991, 0.5),
        (lora(12, 125, 5), 64, 2793.472, 1e-6),  # tanupoo/lorawan_toa
        (lora(10, 125, 6, crc=False), 10, 264.2, 0.05),  # AN1200.13 Table 3
        # SDR measurement on an SX1276: implicit header, CRC off, LDRO off
        (lora(12, 125, 6, 6, explicit_header=False, crc=False, low_data_rate_optimize=False),
         11, 794.624, 1e-6),
    ],
)  # fmt: skip
def test_time_on_air_matches_published_values(
    params: LoRaParams, payload: int, expected_ms: float, tolerance_ms: float
) -> None:
    assert time_on_air_s(payload, params) * 1000 == pytest.approx(expected_ms, abs=tolerance_ms)


EU_NARROW = lora(8, 62.5, 8, meshcore_preamble(8))


@pytest.mark.parametrize(
    ("payload", "expected_ms"),
    [(20, 377.856), (50, 607.232), (100, 1033.216), (150, 1426.432), (200, 1852.416)],
)
def test_eu_narrow_matches_radiolib(payload: int, expected_ms: float) -> None:
    assert time_on_air_s(payload, EU_NARROW) * 1000 == pytest.approx(expected_ms, abs=1e-6)


def test_ldro_switches_on_above_16_ms_symbols() -> None:
    assert not lora(10, 125, 5).ldro  # 8.2 ms
    assert lora(11, 125, 5).ldro  # 16.4 ms
    assert lora(10, 62.5, 5).ldro  # 16.4 ms
    assert not lora(8, 62.5, 8).ldro  # 4.1 ms (EU/UK Narrow)


def test_spreading_factor_6_uses_the_sx126x_variant() -> None:
    sf6 = lora(6, 125, 5)
    # 6.25 preamble tail, no +8 term: N = 8+6.25+8+ceil((80+16-24+20)/24)*5 = 42.25 symbols
    assert time_on_air_s(10, sf6) == pytest.approx(42.25 * 64 / 125_000)


@pytest.mark.parametrize(("kwargs", "message"), [({"sf": 4}, "spreading"), ({"cr": 9}, "coding")])
def test_invalid_parameters(kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        LoRaParams(125_000, kwargs.get("sf", 7), kwargs.get("cr", 5), 8)


def test_meshcore_preamble_depends_on_spreading_factor() -> None:
    assert [meshcore_preamble(sf) for sf in (7, 8, 9, 12)] == [32, 32, 16, 16]


@pytest.mark.parametrize(
    ("text", "payload"), [(0, 20), (11, 20), (12, 36), (27, 36), (28, 52), (155, 164), (160, 180)]
)
def test_direct_message_payload_grows_in_cipher_blocks(text: int, payload: int) -> None:
    """Payload = 4 + 16 * ceil((5 + L) / 16) (docs/protocol.md, direct text message)."""
    assert direct_message_bytes(text, path_bytes=0, flood=False) == 2 + payload


def test_direct_message_carries_path_or_transport_codes() -> None:
    assert direct_message_bytes(5, path_bytes=3, flood=False) == 2 + 3 + 20
    assert direct_message_bytes(5, path_bytes=3, flood=True) == 2 + 4 + 20


def test_channel_message_includes_the_name_prefix() -> None:
    # name "10111213" (8 bytes) + ": " + 5 bytes text + timestamp + flags = 20 -> 2 blocks
    assert channel_message_bytes(5, name_bytes=8) == 1 + 4 + 1 + 3 + 32


def test_flooded_attempt_counts_the_returned_path_packet() -> None:
    direct = direct_attempt_airtime_s(20, path_bytes=2, flood=False, p=EU_NARROW)
    flooded = direct_attempt_airtime_s(20, path_bytes=0, flood=True, p=EU_NARROW)
    assert flooded > direct
    assert direct == pytest.approx(time_on_air_s(direct_message_bytes(20, 2, False), EU_NARROW))


def test_returned_path_packet_is_sized_for_four_path_bytes() -> None:
    """createPathReturn, sent direct (src/Mesh.cpp:173-178, 448-486): header 1, path length 1,
    path P, hashes 2, MAC 2, AES blocks of path length 1, path P, type 1 and random 4. The bridge
    assumes P = 4 (four hops with one-byte hashes); a longer way back costs more (protocol.md)."""
    path = 4
    firmware = 1 + 1 + path + 2 + 2 + 16 * math.ceil((6 + path) / 16)
    assert firmware == RECIPROCAL_PATH_BYTES
