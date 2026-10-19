"""Companion protocol codec against golden frames.

The frames were constructed from the byte layouts in the firmware source (MeshCore
companion-v1.18.0, examples/companion_radio/MyMesh.cpp), independently of this codec; see
docs/protocol.md, "Golden frames". The 3-byte frame header is stripped where payloads are compared.
"""

import pytest

from meshcore_bus.core.model import MessageKind, TextType
from meshcore_bus.protocol import codec
from meshcore_bus.protocol.codes import ErrorCode
from meshcore_bus.protocol.framing import FrameDecoder, encode_frame

TS = 1_760_000_000
PEER = bytes.fromhex("11223344556677") + bytes(range(0x40, 0x59))
SELF = bytes(range(0x10, 0x30))


def h(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


def payload(frame_hex: str) -> bytes:
    (single,) = FrameDecoder().feed(h(frame_hex))
    return single


# --- requests --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_bytes", "frame_hex"),
    [
        (codec.app_start("bridge"), "3C 0E 00 01 00 00 00 00 00 00 00 62 72 69 64 67 65"),
        (codec.device_query(), "3C 02 00 16 03"),
        (codec.get_device_time(), "3C 01 00 05"),
        (codec.set_device_time(TS), "3C 05 00 06 00 78 E7 68"),
        (
            codec.send_text(h("112233445566"), "Hello", TS, attempt=0),
            "3C 12 00 02 00 00 00 78 E7 68 11 22 33 44 55 66 48 65 6C 6C 6F",
        ),
        (
            codec.send_text(h("112233445566"), "Hello", TS, attempt=1),
            "3C 12 00 02 00 01 00 78 E7 68 11 22 33 44 55 66 48 65 6C 6C 6F",
        ),
        (codec.send_channel_text(0, "Hello", TS), "3C 0C 00 03 00 00 00 78 E7 68 48 65 6C 6C 6F"),
        (codec.sync_next_message(), "3C 01 00 0A"),
        (codec.get_contacts(), "3C 01 00 04"),
        (codec.get_contacts(since=TS), "3C 05 00 04 00 78 E7 68"),
        (codec.get_channel(0), "3C 02 00 1F 00"),
        (codec.get_battery(), "3C 01 00 14"),
        (codec.reset_path(PEER), "3C 21 00 0D " + PEER.hex()),
    ],
)
def test_request_frames(request_bytes: bytes, frame_hex: str) -> None:
    assert encode_frame(request_bytes) == h(frame_hex)


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: codec.send_text(bytes(6), "x", TS, 0), "prefix"),
        (lambda: codec.send_text(h("1122"), "x", TS, 0), "prefix"),
        (lambda: codec.send_text(h("112233445566"), "x", TS, 4), "attempt"),
        (lambda: codec.send_text(h("112233445566"), "", TS, 0), "text"),
        (lambda: codec.send_text(h("112233445566"), "a\x00b", TS, 0), "text"),
        (lambda: codec.send_text(h("112233445566"), "x" * 161, TS, 0), "text"),
        (lambda: codec.send_channel_text(0, "", TS), "text"),
        (lambda: codec.send_channel_text(256, "x", TS), "channel"),
        (lambda: codec.reset_path(PEER[:6]), "public key"),
        (lambda: codec.set_device_time(-1), "timestamp"),
    ],
)
def test_requests_refuse_values_the_firmware_mishandles(call: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        call()  # type: ignore[operator]


# --- responses and pushes --------------------------------------------------------------------


def test_ok_err_time_battery() -> None:
    assert codec.decode(payload("3E 01 00 00")) == codec.Ok()
    assert codec.decode(payload("3E 02 00 01 02")) == codec.Err(ErrorCode.NOT_FOUND)
    assert codec.decode(payload("3E 05 00 09 00 78 E7 68")) == codec.CurrentTime(TS)
    battery = codec.decode(payload("3E 0B 00 0C 36 10 0C 00 00 00 D0 03 00 00"))
    assert battery == codec.Battery(millivolts=4150, storage_used_kb=12, storage_total_kb=976)


def test_sent_and_confirmed() -> None:
    sent = codec.decode(payload("3E 0A 00 06 01 AA BB CC DD 34 16 00 00"))
    assert sent == codec.Sent(flood=True, expected_ack=h("AABBCCDD"), timeout_ms=5684)
    confirmed = codec.decode(payload("3E 09 00 82 AA BB CC DD D2 04 00 00"))
    assert confirmed == codec.SendConfirmed(ack=h("AABBCCDD"), round_trip_ms=1234)


def test_simple_pushes() -> None:
    assert codec.decode(payload("3E 01 00 83")) == codec.MessageWaiting()
    assert codec.decode(payload("3E 01 00 0A")) == codec.NoMoreMessages()
    advert = codec.decode(payload("3E 21 00 80 " + PEER.hex()))
    assert advert == codec.Advert(public_key=PEER, new=False)
    assert codec.decode(payload("3E 21 00 81 " + PEER.hex())) == codec.PathUpdated(PEER)


def test_receive_log() -> None:
    rx = codec.decode(payload("3E 0B 00 88 1F 9F 0D 00 DE AD BE EF 00 A5"))
    assert rx == codec.RxLog(snr_db=7.75, rssi_dbm=-97, raw=h("0D 00 DE AD BE EF 00 A5"))


def test_direct_message_v3_flood_two_hops() -> None:
    frame = "3E 15 00 10 19 00 00 11 22 33 44 55 66 02 00 00 78 E7 68 48 65 6C 6C 6F"
    msg = codec.decode_message(payload(frame), received_at=TS + 3)
    assert msg.kind is MessageKind.CONTACT
    assert (msg.sender_prefix, msg.text, msg.sender_timestamp) == ("112233445566", "Hello", TS)
    assert (msg.snr_db, msg.hops, msg.text_type) == (6.25, 2, TextType.PLAIN)
    assert msg.received_at == TS + 3


def test_direct_message_legacy_arrived_direct() -> None:
    frame = "3E 12 00 07 11 22 33 44 55 66 FF 00 00 78 E7 68 48 65 6C 6C 6F"
    msg = codec.decode_message(payload(frame), received_at=TS)
    assert (msg.kind, msg.text, msg.snr_db, msg.hops) == (MessageKind.CONTACT, "Hello", None, None)


def test_signed_message_skips_the_author_prefix() -> None:
    frame = "3E 16 00 10 14 00 00 11 22 33 44 55 66 FF 02 00 78 E7 68 A1 B2 C3 D4 48 69"
    msg = codec.decode_message(payload(frame), received_at=TS)
    assert (msg.text_type, msg.text) == (TextType.SIGNED_PLAIN, "Hi")


def test_channel_message_v3_and_legacy() -> None:
    v3 = "3E 14 00 11 EA 00 00 00 41 00 00 78 E7 68 41 6C 69 63 65 3A 20 48 69"
    msg = codec.decode_message(payload(v3), received_at=TS)
    assert (msg.kind, msg.channel_index, msg.text) == (MessageKind.CHANNEL, 0, "Alice: Hi")
    assert (msg.snr_db, msg.hops) == (-5.5, 1)
    legacy = "3E 11 00 08 01 00 00 00 78 E7 68 41 6C 69 63 65 3A 20 48 69"
    msg = codec.decode_message(payload(legacy), received_at=TS)
    assert (msg.channel_index, msg.hops, msg.snr_db) == (1, 0, None)


def test_invalid_utf8_in_a_message_is_replaced_not_fatal() -> None:
    frame = "3E 13 00 07 11 22 33 44 55 66 FF 00 00 78 E7 68 48 FF 6C 6C 6F 21"
    assert codec.decode_message(payload(frame), received_at=TS).text == "H�llo!"


def test_contacts_stream() -> None:
    assert codec.decode(payload("3E 05 00 02 02 00 00 00")) == codec.ContactsStart(2)
    assert codec.decode(payload("3E 05 00 04 C4 77 E7 68")) == codec.EndOfContacts(TS - 60)
    bob = (
        "3E 94 00 03 " + PEER.hex() + " 01 00 FF " + "00" * 64 + " 42 6F 62 " + "00" * 29
        + " F0 69 E7 68  B0 1B 31 03  70 6F 98 00  C4 77 E7 68"
    )  # fmt: skip
    contact = codec.decode(payload(bob))
    assert contact == codec.ContactRecord(
        public_key=PEER, node_type=1, flags=0, out_path_len=0xFF, name="Bob",
        last_advert=TS - 3600, last_modified=TS - 60,
    )  # fmt: skip
    new_advert = codec.decode(payload(bob.replace("3E 94 00 03", "3E 94 00 8A", 1)))
    assert new_advert == codec.Advert(public_key=PEER, new=True)


def test_channel_info_ignores_bytes_after_the_name() -> None:
    frame = (
        "3E 32 00 12 00 50 75 62 6C 69 63 00 " + "EE" * 25
        + " 8B 33 87 E9 C5 CD EA 6A C9 E5 ED BA A1 15 CD 72"
    )  # fmt: skip
    assert codec.decode(payload(frame)) == codec.ChannelInfo(index=0, name="Public")


def test_device_info() -> None:
    frame = (
        "3E 52 00 0D 0E AF 28 00 00 00 00 31 35 20 4F 63 74 20 32 30 32 36 00"
        " 4C 69 6C 79 47 6F 20 54 2D 45 63 68 6F " + "00" * 27
        + " 76 31 2E 31 38 2E 30 " + "00" * 13 + " 00 00"
    )  # fmt: skip
    assert codec.decode(payload(frame)) == codec.DeviceInfo(
        firmware_code=14, max_contacts=350, max_channels=40, build_date="15 Oct 2026",
        model="LilyGo T-Echo", version="v1.18.0", path_hash_mode=0,
    )  # fmt: skip


def test_self_info() -> None:
    frame = (
        "3E 42 00 05 01 16 16 " + SELF.hex() + " 00 00 00 00 00 00 00 00 00 00 00 00"
        " F2 44 0D 00 24 F4 00 00 08 05 31 30 31 31 31 32 31 33"
    )  # fmt: skip
    info = codec.decode(payload(frame))
    assert info == codec.SelfInfo(
        public_key=SELF, name="10111213", tx_power_dbm=22, max_tx_power_dbm=22,
        frequency_khz=869_618, bandwidth_hz=62_500, spreading_factor=8, coding_rate=5,
        manual_add_contacts=False,
    )  # fmt: skip


def test_unknown_codes_are_kept_raw() -> None:
    assert codec.decode(b"\x8e\x01\x02") == codec.Unknown(code=0x8E, payload=b"\x8e\x01\x02")


@pytest.mark.parametrize(
    "frame", [b"\x06\x01", b"\x82\x00", b"\x05" + bytes(10), b"\x0d" + bytes(5)]
)
def test_truncated_frames_raise_protocol_errors(frame: bytes) -> None:
    with pytest.raises(codec.ProtocolError):
        codec.decode(frame)


@pytest.mark.parametrize(
    "frame", [b"\x10\x00", b"\x07" + bytes(5), b"\x11\x00\x00\x00\x01", b"\x0c"]
)
def test_truncated_or_foreign_message_frames_raise_protocol_errors(frame: bytes) -> None:
    with pytest.raises(codec.ProtocolError):
        codec.decode_message(frame, received_at=0.0)


def test_message_with_unknown_text_type_is_a_protocol_error() -> None:
    frame = bytes.fromhex("07 112233445566 FF 09 0078E768 48".replace(" ", ""))
    with pytest.raises(codec.ProtocolError):
        codec.decode_message(frame, received_at=0.0)


def test_expected_ack_code_can_be_recomputed() -> None:
    """Cross-check of SENT: SHA256(ts || attempt&3 || text || own key)[0:4] (firmware 5.4)."""
    expected = {0: "A37A44B9", 1: "C6F86F73", 2: "694BDB88", 3: "F6B32387"}
    for attempt, code in expected.items():
        assert codec.expected_ack(TS, attempt, "Hello", SELF) == h(code)
