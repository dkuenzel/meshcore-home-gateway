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

from meshcore_home_gateway.core import airtime
from meshcore_home_gateway.core.airtime import (
    RECIPROCAL_PATH_BYTES,
    LoRaParams,
    channel_message_bytes,
    direct_attempt_airtime_s,
    direct_message_bytes,
    direct_poll_costs,
    flood_poll_costs,
    flooded_reply_costs,
    meshcore_lora,
    meshcore_preamble,
    path_return_bytes,
    return_path_retry_bytes,
    telemetry_reply_bytes,
    telemetry_request_bytes,
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


# --- telemetry requests (ADR 0018; sizes in docs/protocol.md) ------------------------------------

EU_NARROW = meshcore_lora(62_500.0, 8, 8)


def test_meshcore_modulation_takes_its_preamble() -> None:
    assert LoRaParams(62_500.0, 8, 8, 32) == EU_NARROW
    assert meshcore_lora(125_000.0, 11, 5).preamble_symbols == 16


def test_telemetry_packet_sizes() -> None:
    """Request: 13 bytes of plaintext in one AES block (BaseChatMesh.cpp:693-698); answer: tag
    and LPP in blocks; a flooded request is answered by a path return (Mesh.cpp:452-480)."""
    assert (telemetry_request_bytes(0, False), telemetry_request_bytes(3, False)) == (22, 25)
    assert telemetry_request_bytes(3, True) == 26  # transport codes instead of the path
    assert [telemetry_reply_bytes(lpp, 0) for lpp in (8, 12, 13, 19, 34)] == [22, 22, 38, 38, 54]
    assert telemetry_reply_bytes(19, 2) == 40
    assert (path_return_bytes(19, 0), path_return_bytes(34, 0)) == (42, 58)
    assert return_path_retry_bytes(0, 0) == 22


@pytest.mark.parametrize(
    ("lpp_bytes", "mesh_s"),
    [(8, (0.756, 1.577, 2.398, 3.219)), (19, (0.887, 1.839, 2.791, 3.744)),
     (34, (1.018, 2.101, 3.185, 4.268))],
)  # fmt: skip
def test_direct_poll_costs_count_every_hop_both_ways(
    lpp_bytes: int, mesh_s: tuple[float, ...]
) -> None:
    costs = [direct_poll_costs(hops, hops, lpp_bytes, EU_NARROW) for hops in range(4)]
    assert tuple(round(c.mesh_s, 3) for c in costs) == mesh_s
    assert [round(c.own_s, 3) for c in costs] == [0.378, 0.411, 0.411, 0.411]


def test_flood_poll_costs_at_the_repeaters_assumed() -> None:
    costs = flood_poll_costs(19, EU_NARROW)
    assert (round(costs.own_s, 3), round(costs.mesh_s, 2)) == (0.821, 20.41)
    assert round(flood_poll_costs(34, EU_NARROW).mesh_s, 2) == 23.16
    assert round(flood_poll_costs(19, EU_NARROW, repeaters=0).mesh_s, 3) == round(
        2 * time_on_air_s(26, EU_NARROW) + time_on_air_s(42, EU_NARROW), 3
    )


def test_flood_repeaters_are_read_when_called(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(airtime, "FLOOD_REPEATERS", 0)
    assert flood_poll_costs(19, EU_NARROW).mesh_s < 2.0


def test_a_flooded_reply_costs_its_flood_and_the_path_sent_back() -> None:
    costs = flooded_reply_costs(19, 0, 0, EU_NARROW)
    assert (round(costs.own_s, 3), round(costs.mesh_s, 2)) == (0.378, 11.75)
