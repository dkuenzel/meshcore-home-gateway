"""The CayenneLPP decoder (core/lpp.py) against frames encoded by the library 1.6.1 as the
firmware pins it (CayenneLPP.cpp, float32 arithmetic ported), independently of the decoder."""

import random

import pytest

from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core.lpp import LppPosition, Reading
from meshcore_home_gateway.core.model import Position


def h(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


# voltage 4.12 V, GPS 50.1234/8.5677 at 120.5 m, MCU temperature 23.4 °C: what a 1.18 companion
# with a GPS answers (19 bytes)
LPP_FULL = h("01 74 01 9c 01 88 07 a5 f2 01 4e ad 00 2f 12 01 67 00 ea")
LPP_BASE = h("01 74 01 8b 01 67 00 d9")  # 3.95 V, 21.7 °C: no location permission
LPP_NEGATIVE = h("01 88 fa d5 00 f5 38 4d ff fb 32 02 67 ff cb")  # south, west, below sea level
LPP_T1000E = h("01 67 00 ea 01 65 00 32 01 67 00 c8")  # two temperatures on one channel


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
    payload = lpp.decode(LPP_T1000E)
    assert payload.readings == (
        Reading(1, 103, 0, (234,)), Reading(1, 101, 0, (50,)), Reading(1, 103, 1, (200,)),
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
