"""The CayenneLPP decoder (core/lpp.py) against frames encoded by the library 1.6.1 as the
firmware pins it (CayenneLPP.cpp, float32 arithmetic ported), independently of the decoder."""

import json
import random
import re

import pytest

from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core.lpp import LppPosition, Named, Reading
from meshcore_home_gateway.core.model import Position
from tests.radio_emulator import LPP_ALL, LPP_ALL_READINGS, LPP_BME280, LPP_T1000E, LPP_WIO_E5


def h(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


# voltage 4.12 V, GPS 50.1234/8.5677 at 120.5 m, MCU temperature 23.4 °C: what a 1.18 companion
# with a GPS answers (19 bytes)
LPP_FULL = h("01 74 01 9c 01 88 07 a5 f2 01 4e ad 00 2f 12 01 67 00 ea")
LPP_BASE = h("01 74 01 8b 01 67 00 d9")  # 3.95 V, 21.7 °C: no location permission
LPP_NEGATIVE = h("01 88 fa d5 00 f5 38 4d ff fb 32 02 67 ff cb")  # south, west, below sea level


def test_a_full_answer() -> None:
    payload = lpp.decode(LPP_FULL)
    assert payload.readings == (
        Reading(1, 116, 0, (412,)),
        Reading(1, 136, 0, (501234, 85677, 12050)),
        Reading(1, 103, 0, (234,)),
    )
    assert [r.values for r in payload.readings] == [(4.12,), (50.1234, 8.5677, 120.5), (23.4,)]
    assert (payload.lpp_bytes, payload.undecoded) == (19, b"")
    found = lpp.position(payload)
    assert found == LppPosition(501234, 85677, 12050)
    assert found is not None
    assert found.position == Position(50.12345, 8.56775)  # centred in its cell
    assert found.altitude_m == 120.5
    assert found.cells == (501234, 85677)


@pytest.mark.parametrize("padding", [0, 4, 6, 9, 15])
def test_padding_ends_the_answer(padding: int) -> None:
    """AES pads with zeros (direct answer 9 here, flooded path return 6): channel 0 stops."""
    payload = lpp.decode(LPP_FULL + bytes(padding))
    assert len(payload.readings) == 3
    assert (payload.lpp_bytes, payload.undecoded) == (19, b"")


def test_negative_values_are_twos_complement() -> None:
    payload = lpp.decode(LPP_NEGATIVE)
    assert payload.readings[0].raw == (-338688, -706483, -1230)
    assert payload.readings[1] == Reading(2, 103, 0, (-53,))
    assert payload.readings[1].values == (-5.3,)
    found = lpp.position(payload)
    assert found is not None
    assert found.position == Position(-33.86885, -70.64835)
    assert found.altitude_m == -12.3


def test_signed_voltage_and_current() -> None:
    """The header calls 116 and 117 unsigned; the code writes them signed (CayenneLPP.cpp)."""
    payload = lpp.decode(h("01 74 ff 9c 01 75 ff 38"))
    assert [r.values for r in payload.readings] == [(-1.0,), (-0.2,)]


def test_repeated_channel_and_type_stay_apart() -> None:
    payload = lpp.decode(LPP_T1000E)  # two temperatures on one channel
    assert payload.readings == (
        Reading(1, 116, 0, (412,)), Reading(1, 103, 0, (234,)), Reading(1, 101, 0, (50,)),
        Reading(1, 103, 1, (200,)),
    )  # fmt: skip


def test_an_answer_without_location() -> None:
    payload = lpp.decode(LPP_BASE + bytes(4))
    assert [r.values for r in payload.readings] == [(3.95,), (21.7,)]
    assert lpp.position(payload) is None
    assert payload.lpp_bytes == 8


@pytest.mark.parametrize(
    ("data", "kept", "undecoded"),
    [
        (h("01 74 01 9c 01 f0 12 34 01 67 00 ea"), 1, h("01 f0 12 34 01 67 00 ea")),  # unknown
        (h("01 74 01 9c 01 88 07 a5"), 1, h("01 88 07 a5")),  # GPS cut short
        (h("01 74 01"), 0, h("01 74 01")),
        (h("01"), 0, h("01")),
        (h("01 74 01 9c 00 00 05"), 1, h("00 00 05")),  # not padding after all: kept, unread
        (b"", 0, b""),
    ],
)
def test_the_rest_after_a_stop_is_kept_undecoded(data: bytes, kept: int, undecoded: bytes) -> None:
    payload = lpp.decode(data)
    assert len(payload.readings) == kept
    assert payload.undecoded == undecoded
    assert payload.lpp_bytes == len(data)  # none of them ends in zeros


@pytest.mark.parametrize(
    "gps",
    [
        "00 00 00 00 00 00 00 00 00",  # no fix yet, or a cleared position
        "0d bb a1 00 00 01 00 00 00",  # latitude 90.0001
        "00 00 01 1b 77 41 00 00 00",  # longitude 180.0001
    ],
)
def test_impossible_positions_are_none(gps: str) -> None:
    assert lpp.position(lpp.decode(h("01 88 " + gps))) is None


def test_two_different_positions_are_none_and_the_same_twice_is_one() -> None:
    first, second = h("01 88 07 a5 f2 01 4e ad 00 2f 12"), h("01 88 07 a5 f3 01 4e ad 00 2f 12")
    assert lpp.position(lpp.decode(first + second)) is None
    assert lpp.position(lpp.decode(first + first)) == LppPosition(501234, 85677, 12050)
    # an invalid one beside a valid one does not count
    assert lpp.position(lpp.decode(first + h("01 88" + "00" * 9))) is not None


def test_a_position_on_another_channel_is_a_reading_only() -> None:
    """Every addGPS of the firmware uses the companion's own channel 1 (SensorManager.h)."""
    payload = lpp.decode(h("02 88 07 a5 f2 01 4e ad 00 2f 12"))
    assert len(payload.readings) == 1
    assert lpp.position(payload) is None


@pytest.mark.parametrize(
    ("cells", "expected"),
    [
        ((0, 0), Position(0.0, 0.0)),
        ((1, -1), Position(0.00015, -0.00015)),
        ((899_999, 1_799_999), Position(89.99995, 179.99995)),
        ((900_000, -1_800_000), Position(90.0, -180.0)),  # clamped
    ],
)
def test_centring(cells: tuple[int, int], expected: Position) -> None:
    assert lpp.centre(*cells) == expected


def test_every_fixed_size_type_is_known_with_its_size() -> None:
    """Sizes from CayenneLPP.h:27-111; polyline (240) has no fixed size and stops the decoder."""
    sizes = {code: sum(kind.sizes) for code, kind in lpp.TYPES.items()}
    assert sizes == {
        0: 1, 1: 1, 2: 2, 3: 2, 100: 4, 101: 2, 102: 1, 103: 2, 104: 1, 113: 6, 115: 2, 116: 2,
        117: 2, 118: 4, 120: 1, 121: 2, 125: 2, 128: 2, 130: 4, 131: 4, 132: 2, 133: 4, 134: 6,
        135: 3, 136: 9, 142: 1,
    }  # fmt: skip
    assert 240 not in lpp.TYPES
    assert lpp.decode(h("01 68 7d")).readings[0].values == (62.5,)  # humidity in half percent


def test_the_decoder_never_raises() -> None:
    rng = random.Random(18)  # noqa: S311 - test data
    for _ in range(5000):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(40)))
        payload = lpp.decode(data)
        assert payload.lpp_bytes <= len(data)
        lpp.position(payload)
    for size in range(len(LPP_FULL)):
        lpp.decode(LPP_FULL[:size])


# --- what the documents call the readings (named) ---------------------------------------------


def test_named_takes_battery_first_and_mcu_last() -> None:
    assert lpp.named(lpp.decode(LPP_FULL)) == Named(4.12, 23.4, {})
    assert lpp.named(lpp.decode(b"")) == Named(None, None, {})


def test_named_bme280() -> None:
    named = lpp.named(lpp.decode(LPP_BME280))
    assert (named.battery_v, named.mcu_temperature_c) == (4.12, 23.4)
    assert list(named.readings.items()) == [  # in the answer's order
        ("ch2_temperature_c", 21.3), ("ch2_humidity_percent", 55.5),
        ("ch2_barometric_pressure_hpa", 1013.2), ("ch2_altitude_m", 120.0),
    ]  # fmt: skip


def test_the_t1000e_keeps_its_board_temperature_apart_from_the_mcu() -> None:
    """Its light level is 0-100, not lux: the key has no unit."""
    named = lpp.named(lpp.decode(LPP_T1000E))
    assert named == Named(4.12, 20.0, {"ch1_temperature_c": 23.4, "ch1_luminosity": 50.0})


def test_no_mcu_when_the_last_entry_is_no_channel_1_temperature() -> None:
    assert lpp.named(lpp.decode(LPP_WIO_E5)) == Named(4.12, None, {
        "ch1_temperature_c": 21.3, "ch1_humidity_percent": 55.5,
        "ch1_barometric_pressure_hpa": 1013.2,
    })  # fmt: skip
    last_on_channel_2 = lpp.decode(h("01 74 01 9c 02 67 00 d5"))
    assert lpp.named(last_on_channel_2) == Named(4.12, None, {"ch2_temperature_c": 21.3})


def test_no_mcu_when_bytes_after_it_were_not_decoded() -> None:
    """Something after it that the decoder could not read: it may not be the last entry."""
    payload = lpp.decode(h("01 74 01 9c 01 67 00 ea 01 f0 12 34"))  # an unknown type after it
    assert payload.undecoded
    assert lpp.named(payload) == Named(4.12, None, {"ch1_temperature_c": 23.4})
    padded = lpp.decode(h("01 74 01 9c 01 67 00 ea") + bytes(9))  # zero padding is no entry
    assert lpp.named(padded) == Named(4.12, 23.4, {})


def test_no_battery_when_the_first_entry_is_no_channel_1_voltage() -> None:
    payload = lpp.decode(h("01 67 00 ea 01 74 01 9c 01 67 00 c8"))
    assert lpp.named(payload) == Named(None, 20.0, {
        "ch1_temperature_c": 23.4, "ch1_voltage_v": 4.12,
    })  # fmt: skip
    first_on_channel_2 = lpp.decode(h("02 74 01 4a 01 74 01 9c"))
    assert lpp.named(first_on_channel_2) == Named(None, None, {
        "ch2_voltage_v": 3.3, "ch1_voltage_v": 4.12,
    })  # fmt: skip


def test_further_occurrences_are_numbered_from_2() -> None:
    payload = lpp.decode(h("01 74 01 9c 02 67 00 d5 02 67 00 d6 02 67 00 d7 01 67 00 ea"))
    assert lpp.named(payload).readings == {
        "ch2_temperature_c": 21.3, "ch2_temperature_2_c": 21.4, "ch2_temperature_3_c": 21.5,
    }  # fmt: skip
    # counted after the battery and the MCU temperature are taken out
    again = lpp.decode(h("01 74 01 9c 01 74 01 4a 01 67 00 d5 01 67 00 ea"))
    assert lpp.named(again) == Named(4.12, 23.4, {"ch1_voltage_v": 3.3, "ch1_temperature_c": 21.3})


def test_gps_and_unpublished_types_are_no_readings() -> None:
    # battery, GPS on channel 2, presence, accelerometer, a temperature
    payload = lpp.decode(h(
        "01 74 01 9c 02 88 07 a5 f2 01 4e ad 00 2f 12 02 66 01 02 71 00 01 00 02 00 03"
        " 02 67 00 d5"
    ))  # fmt: skip
    assert len(payload.readings) == 5
    assert lpp.named(payload) == Named(4.12, None, {"ch2_temperature_c": 21.3})


def test_types_without_a_unit_have_no_suffix() -> None:
    payload = lpp.decode(h("02 65 01 2c 02 64 00 00 00 07 02 02 00 7b"))
    assert lpp.named(payload).readings == {
        "ch2_luminosity": 300.0, "ch2_generic": 7.0, "ch2_analog_input": 1.23,
    }  # fmt: skip


def test_the_vector_of_every_key() -> None:
    """LPP_ALL (tests/radio_emulator.py) holds every published type on channel 2."""
    named = lpp.named(lpp.decode(LPP_ALL))
    assert named == Named(4.12, 23.4, LPP_ALL_READINGS)
    assert len(named.readings) == 16
    on_channel_2 = {lpp.parse_component(lpp.component_of(k) or "") for k in named.readings}
    assert {(2, t, 1) for t in lpp.UNITS} <= on_channel_2


def test_every_published_type_round_trips() -> None:
    assert set(lpp.UNITS) <= set(lpp.TYPES)
    assert lpp.GPS not in lpp.UNITS
    keys = set()
    for lpp_type in lpp.UNITS:
        for channel in (1, 255):
            for n in (1, 2):
                key = lpp.reading_key(channel, lpp_type, n)
                assert key is not None
                assert re.fullmatch("[a-z0-9_]+", key), key
                component = lpp.component_of(key)
                assert component is not None
                assert key.startswith(component)
                assert re.fullmatch("[a-z0-9_]+", component), component
                assert lpp.parse_component(component) == (channel, lpp_type, n)
                keys.add(key)
    assert len(keys) == 4 * len(lpp.UNITS)
    assert lpp.reading_key(2, 103, 1) == "ch2_temperature_c"
    assert lpp.reading_key(1, 103, 2) == "ch1_temperature_2_c"
    assert lpp.reading_key(2, 115, 1) == "ch2_barometric_pressure_hpa"
    assert lpp.component_of("ch1_temperature_2_c") == "ch1_temperature_2"
    assert lpp.component_of("ch2_luminosity_2") == "ch2_luminosity_2"
    assert lpp.component_of("ch2_percentage_percent") == "ch2_percentage"
    assert (lpp.BATTERY_V, lpp.MCU_TEMPERATURE_C) == ("battery_v", "mcu_temperature_c")


@pytest.mark.parametrize(
    ("channel", "lpp_type", "n"),
    [(2, lpp.GPS, 1), (2, 102, 1), (2, 240, 1), (0, 103, 1), (256, 103, 1), (2, 103, 0)],
)
def test_no_key_for_what_is_not_published(channel: int, lpp_type: int, n: int) -> None:
    assert lpp.reading_key(channel, lpp_type, n) is None


@pytest.mark.parametrize(
    "component",
    [
        "ch0_temperature", "ch256_temperature", "ch02_temperature", "ch2_temperature_1",
        "ch2_temperature_02", "ch2_temperature_c", "ch2_gps", "ch2_presence", "battery", "ch2_",
        "ch_temperature", "ch2temperature", "ch\u0662_temperature", "",
    ],
)  # fmt: skip
def test_parse_component_refuses_what_component_of_never_makes(component: str) -> None:
    assert lpp.parse_component(component) is None


@pytest.mark.parametrize(
    "key",
    [
        lpp.BATTERY_V, lpp.MCU_TEMPERATURE_C, "ch2_temperature", "ch2_temperature_2",
        "ch2_luminosity_lx", "ch2_voltage_c", "ch0_temperature_c", "ch2_gps", "",
    ],
)  # fmt: skip
def test_component_of_refuses_what_reading_key_never_makes(key: str) -> None:
    assert lpp.component_of(key) is None


def test_values_are_the_short_decimals() -> None:
    """raw / divisor, never an encoder's float: the documents show what the companion sent."""
    payload = lpp.decode(h("02 74 01 9c 02 68 6f 02 75 ff ff 02 02 30 39"))
    assert json.dumps(lpp.named(payload).readings) == (
        '{"ch2_voltage_v": 4.12, "ch2_humidity_percent": 55.5, "ch2_current_a": -0.001, '
        '"ch2_analog_input": 123.45}'
    )


def test_named_is_total() -> None:
    rng = random.Random(6)  # noqa: S311 - test data
    codes = list(lpp.TYPES)
    for _ in range(20_000):
        data = b""
        for _ in range(rng.randrange(12)):
            code = rng.choice(codes) if rng.random() < 0.95 else rng.randrange(256)
            size = sum(lpp.TYPES[code].sizes) if code in lpp.TYPES else rng.randrange(4)
            channel = rng.choice((1, 2, 255)) if rng.random() < 0.7 else rng.randrange(256)
            data += bytes([channel, code]) + bytes(rng.randrange(256) for _ in range(size))
        if data and rng.random() < 0.2:
            data = data[: rng.randrange(len(data))]
        named = lpp.named(lpp.decode(data))
        for key, value in named.readings.items():
            component = lpp.component_of(key)
            assert component is not None, key
            parsed = lpp.parse_component(component)
            assert parsed is not None, component
            assert lpp.reading_key(*parsed) == key
            assert isinstance(value, float)
