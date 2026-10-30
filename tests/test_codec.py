"""Companion protocol codec against golden frames.

The frames were constructed from the byte layouts in the firmware source (MeshCore
companion-v1.18.0, examples/companion_radio/MyMesh.cpp), independently of this codec; the
layouts are in docs/protocol.md, sections 1-4. The 3-byte frame header is stripped where payloads
are compared.
"""

from dataclasses import replace

import pytest

from meshcore_home_gateway.core.model import MessageKind, Position, TextType
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import ErrorCode, StatsType, TelemetryMode
from meshcore_home_gateway.protocol.framing import FrameDecoder, encode_frame

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
        (codec.get_autoadd_config(), "3C 01 00 3B"),
        (codec.get_tuning_params(), "3C 01 00 2B"),
        (codec.get_stats(StatsType.CORE), "3C 02 00 38 00"),
        (codec.get_stats(StatsType.PACKETS), "3C 02 00 38 02"),
        # telemetry request (MyMesh.cpp:1680-1682): three unread bytes, then the full key
        (codec.send_telemetry_request(h("a1" * 32)), "3C 24 00 27 00 00 00 " + "a1" * 32),
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
        # a 4-byte request would ask the radio itself (MyMesh.cpp:1700)
        (lambda: codec.send_telemetry_request(b""), "public key"),
        (lambda: codec.send_telemetry_request(PEER[:6]), "public key"),
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


LPP_FULL = h("01 74 01 9c 01 88 07 a5 f2 01 4e ad 00 2f 12 01 67 00 ea")  # 19 bytes


@pytest.mark.parametrize(
    ("frame", "flood", "timeout_ms"),
    [
        ("06 00 44 33 22 11 8a 0c 00 00", False, 3210),  # direct: 500 + (6 t + 250) (hops + 1)
        ("06 01 45 33 22 11 94 1b 00 00", True, 7060),  # flood: 500 + 16 t
    ],
)
def test_sent_after_a_telemetry_request(frame: str, flood: bool, timeout_ms: int) -> None:
    """SENT carries the route and the estimated timeout; the tag is not needed (MyMesh.cpp:1691)."""
    sent = codec.decode(h(frame))
    assert isinstance(sent, codec.Sent)
    assert (sent.flood, sent.timeout_ms) == (flood, timeout_ms)


@pytest.mark.parametrize("padding", [9, 6, 0, 15])  # direct 9, flooded path return 6
def test_telemetry_response(padding: int) -> None:
    """Push 0x8B (MyMesh.cpp:739-749): reserved byte, the 6-byte prefix of the contact whose key
    decrypted the reply, then the LPP with its AES zero padding, which the LPP decoder stops at."""
    response = codec.decode(h("8b 00 a1 b2 c3 d4 e5 f6") + LPP_FULL + bytes(padding))
    assert response == codec.TelemetryResponse(h("a1b2c3d4e5f6"), LPP_FULL + bytes(padding))
    assert codec.decode(h("8b 00 a1 b2 c3 d4 e5 f6")) == codec.TelemetryResponse(
        h("a1b2c3d4e5f6"), b""
    )


def test_a_binary_response_is_not_decoded() -> None:
    """Push 0x8C carries a tag but no sender (MyMesh.cpp:750-776): never taken (ADR 0018)."""
    frame = h("8c 00 44 33 22 11") + LPP_FULL + bytes(9)
    assert codec.decode(frame) == codec.Unknown(0x8C, frame)


def test_a_short_telemetry_response_is_a_protocol_error() -> None:
    with pytest.raises(codec.ProtocolError):
        codec.decode(h("8b 00 a1 b2 c3 d4 e5"))


@pytest.mark.parametrize(
    ("path_len", "path_bytes"), [(0xFF, None), (0x00, 0), (0x03, 3), (0x42, 4), (0x81, 3)]
)
def test_stored_path_size(path_len: int, path_bytes: int | None) -> None:
    record = codec.ContactRecord(PEER, 1, 0, path_len, "Bob", 0, 0)
    assert record.path_bytes == path_bytes


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
        last_advert=TS - 3600, last_modified=TS - 60, out_path=b"",
        latitude_e6=53_550_000, longitude_e6=9_990_000, raw=payload(bob)[1:],
    )  # fmt: skip
    assert isinstance(contact, codec.ContactRecord)
    assert contact.position == Position(53.55, 9.99)
    assert (contact.path_known, contact.path_hops, contact.path_hashes) == (False, None, [])
    new_advert = codec.decode(payload(bob.replace("3E 94 00 03", "3E 94 00 8A", 1)))
    assert new_advert == codec.Advert(public_key=PEER, new=True, record=contact)


@pytest.mark.parametrize(
    ("path_len", "path", "hashes"),
    [
        ("02", "AA BB", [h("AA"), h("BB")]),  # 1-byte hashes: bits 6-7 are 0
        ("42", "AA BB CC DD", [h("AABB"), h("CCDD")]),  # 2-byte hashes
        ("00", "", []),  # a neighbour: known path without repeaters
    ],
)
def test_contact_with_a_known_path(path_len: str, path: str, hashes: list[bytes]) -> None:
    """out_path_len: bits 0-5 hops, bits 6-7 hash size - 1 (src/Packet.h:78-80); the stored path
    is always 64 bytes, of which the radio uses hops x size (MyMesh.cpp:177-178)."""
    used = h(path)
    record = (
        "03" + PEER.hex() + "01 01 " + path_len + used.ljust(64, b"\xee").hex()
        + "42 6F 62" + "00" * 29 + "F0 69 E7 68 00 00 00 00 00 00 00 00 C4 77 E7 68"
    )  # fmt: skip
    contact = codec.decode(h(record))
    assert isinstance(contact, codec.ContactRecord)
    assert contact.path_known
    assert contact.path_hops == len(hashes)
    assert contact.path_hashes == hashes
    assert contact.out_path == used  # not the stale rest of the buffer
    assert contact.position is None  # 0, 0 means no position
    assert contact.flags == 0x01  # favourite


def test_a_new_advert_from_older_firmware_without_the_record_has_none() -> None:
    assert codec.decode(h("8A" + PEER.hex())) == codec.Advert(PEER, new=True, record=None)


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
    assert isinstance(info, codec.SelfInfo)
    assert info.position is None
    assert info.telemetry_base is TelemetryMode.DENY


def test_self_info_position_policies_and_telemetry_modes() -> None:
    """Bytes 36-47 (MyMesh.cpp:1092-1103): position, extra acknowledgements, advert location
    policy, telemetry modes (env << 4 | location << 2 | base), manual adding."""
    frame = (
        "05 01 16 16 " + SELF.hex() + " B0 1B 31 03 90 90 67 FF 01 01 26 01"
        " F2 44 0D 00 24 F4 00 00 08 08 47 57"
    )  # fmt: skip
    info = codec.decode(h(frame))
    assert isinstance(info, codec.SelfInfo)
    assert info.position == Position(53.55, -9.99)
    assert (info.multi_acks, info.advert_location, info.manual_add_contacts) == (1, 1, True)
    assert info.telemetry_base is TelemetryMode.ALLOW_ALL
    assert info.telemetry_location is TelemetryMode.BY_CONTACT
    assert info.telemetry_environment is TelemetryMode.ALLOW_ALL
    assert replace(info, telemetry_modes=0x03).telemetry_base == 3  # undefined: kept as number


def test_statistics() -> None:
    """GET_STATS answers (MyMesh.cpp:1914-1964); 1.17 and older send packets without errors."""
    core = codec.decode(h("18 00 AC 0F 10 0E 00 00 05 00 03"))
    assert core == codec.CoreStats(battery_mv=4012, uptime_s=3600, error_flags=5, queue_length=3)
    radio = codec.decode(h("18 01 8A FF 9F 1A 29 00 00 00 2C 01 00 00"))
    assert radio == codec.RadioStats(
        noise_floor_dbm=-118, last_rssi_dbm=-97, last_snr_db=6.5, tx_airtime_s=41,
        rx_airtime_s=300,
    )  # fmt: skip
    counters = "78 00 00 00 1E 00 00 00 0A 00 00 00 14 00 00 00 46 00 00 00 32 00 00 00"
    packets = codec.decode(h("18 02 " + counters + " 02 00 00 00"))
    assert packets == codec.PacketStats(120, 30, 10, 20, 70, 50, receive_errors=2)
    older = codec.decode(h("18 02 " + counters))
    assert older == codec.PacketStats(120, 30, 10, 20, 70, 50, receive_errors=None)
    with pytest.raises(codec.ProtocolError, match="statistics type 7"):
        codec.decode(h("18 07 00 00"))
    with pytest.raises(codec.ProtocolError):
        codec.decode(h("18 01 8A FF"))


def test_tuning_parameters() -> None:
    """Response 23 (MyMesh.cpp:1481-1487): rx delay base and airtime factor, floats x 1000."""
    tuning = codec.decode(h("17 00 00 00 00 28 23 00 00"))
    assert tuning == codec.TuningParams(rx_delay_base_milli=0, airtime_factor_milli=9000)


SELF_INFO_HEAD = "3E 42 00 05 01 16 16 " + SELF.hex() + " 00" * 12 + " F2 44 0D 00"


@pytest.mark.parametrize(
    "modulation",
    ["24 F4 00 00 0D 05", "24 F4 00 00 04 05", "24 F4 00 00 08 09", "00 00 00 00 08 05"],
    ids=["sf13", "sf4", "cr9", "bw0"],
)
def test_self_info_with_impossible_modulation_is_a_protocol_error(modulation: str) -> None:
    """MeshCore 1.18 clamps these (MyMesh.cpp:991-992); anything else is a corrupt frame."""
    with pytest.raises(codec.ProtocolError, match="modulation"):
        codec.decode(payload(SELF_INFO_HEAD + " " + modulation + " 31 30 31 31 31 32 31 33"))


def test_contact_deleted_push_and_auto_add_settings() -> None:
    """Push 0x8F carries the overwritten contact's full key (MyMesh.cpp:342-349); response 25
    the auto-add flags and the hop limit (MyMesh.cpp:2030-2035, flags at lines 147-151)."""
    assert codec.decode(payload("3E 21 00 8F " + PEER.hex())) == codec.ContactDeleted(PEER)
    settings = codec.decode(payload("3E 03 00 19 03 40"))
    assert settings == codec.AutoAddConfig(flags=0x03, max_hops=64)
    assert isinstance(settings, codec.AutoAddConfig)
    assert settings.overwrites_oldest
    assert not codec.AutoAddConfig(flags=0x1C, max_hops=0).overwrites_oldest
    # firmware 1.12 and 1.13 answer without the hop limit (companion-v1.12.0 MyMesh.cpp:1733-1737)
    assert codec.decode(payload("3E 02 00 19 03")) == codec.AutoAddConfig(flags=0x03, max_hops=None)


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
    """Cross-check of SENT: SHA256(ts || attempt&3 || text || own key)[0:4].

    docs/protocol.md section 5, "ACK code" (BaseChatMesh.cpp:451-471).
    """
    expected = {0: "A37A44B9", 1: "C6F86F73", 2: "694BDB88", 3: "F6B32387"}
    for attempt, code in expected.items():
        assert codec.expected_ack(TS, attempt, "Hello", SELF) == h(code)
