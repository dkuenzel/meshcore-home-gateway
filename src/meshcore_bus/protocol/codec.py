"""Encoders for the commands and decoders for the responses and pushes the bridge uses.

Layouts follow MeshCore companion-v1.18.0 (docs/protocol.md cites every field). All integers are
little endian. Encoders refuse values the firmware would mishandle (empty or NUL-containing text,
zero key prefixes, attempt numbers above 3).
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable
from dataclasses import dataclass

from meshcore_bus.core.model import InboundMessage, MessageKind, TextType
from meshcore_bus.protocol.codes import (
    PROTOCOL_VERSION,
    AutoAdd,
    Command,
    ErrorCode,
    Push,
    Response,
)

MAX_TEXT_BYTES = 160  # MAX_TEXT_LEN, src/helpers/BaseChatMesh.h:8
MAX_ATTEMPT = 3  # only attempt & 3 enters the ACK code; above 3 the text limit drops to 158
PREFIX_BYTES = 6
KEY_BYTES = 32
DIRECT_PATH = 0xFF  # path_len of a message that arrived by direct route


class ProtocolError(ValueError):
    """A frame from the radio does not match the expected layout."""


# --- requests --------------------------------------------------------------------------------


def app_start(app_name: str) -> bytes:
    return bytes([Command.APP_START]) + bytes(7) + app_name.encode("utf-8")[:32]


def device_query(version: int = PROTOCOL_VERSION) -> bytes:
    return bytes([Command.DEVICE_QUERY, version])


def get_device_time() -> bytes:
    return bytes([Command.GET_DEVICE_TIME])


def set_device_time(timestamp: int) -> bytes:
    return bytes([Command.SET_DEVICE_TIME]) + _u32(timestamp, "timestamp")


def send_text(prefix: bytes, text: str, timestamp: int, attempt: int) -> bytes:
    """Plain direct message (txt_type 0) to the contact whose key starts with `prefix`."""
    if len(prefix) != PREFIX_BYTES or not any(prefix):
        raise ValueError("recipient prefix must be 6 bytes and not all zero")
    if not 0 <= attempt <= MAX_ATTEMPT:
        raise ValueError(f"attempt must be 0..{MAX_ATTEMPT}, got {attempt}")
    head = struct.pack("<BBB", Command.SEND_TXT_MSG, TextType.PLAIN, attempt)
    return head + _u32(timestamp, "timestamp") + prefix + _text(text, MAX_TEXT_BYTES)


def send_channel_text(index: int, text: str, timestamp: int) -> bytes:
    if not 0 <= index <= 0xFF:
        raise ValueError(f"channel index must be 0..255, got {index}")
    head = struct.pack("<BBB", Command.SEND_CHANNEL_TXT_MSG, TextType.PLAIN, index)
    return head + _u32(timestamp, "timestamp") + _text(text, MAX_TEXT_BYTES)


def sync_next_message() -> bytes:
    return bytes([Command.SYNC_NEXT_MESSAGE])


def get_contacts(since: int | None = None) -> bytes:
    head = bytes([Command.GET_CONTACTS])
    return head if since is None else head + _u32(since, "timestamp")


def get_channel(index: int) -> bytes:
    if not 0 <= index <= 0xFF:
        raise ValueError(f"channel index must be 0..255, got {index}")
    return bytes([Command.GET_CHANNEL, index])


def get_battery() -> bytes:
    return bytes([Command.GET_BATT_AND_STORAGE])


def get_autoadd_config() -> bytes:
    return bytes([Command.GET_AUTOADD_CONFIG])


def reset_path(public_key: bytes) -> bytes:
    if len(public_key) != KEY_BYTES:
        raise ValueError("reset_path needs the full 32-byte public key")
    return bytes([Command.RESET_PATH]) + public_key


def expected_ack(timestamp: int, attempt: int, text: str, own_public_key: bytes) -> bytes:
    """ACK code the recipient will send: SHA256(ts LE32 || attempt&3 || text || own key)[0:4]."""
    data = _u32(timestamp, "timestamp") + bytes([attempt & 3]) + text.encode("utf-8")
    return hashlib.sha256(data + own_public_key).digest()[:4]


def _u32(value: int, name: str) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{name} must fit in 32 bits, got {value}")
    return struct.pack("<I", value)


def _text(text: str, limit: int) -> bytes:
    data = text.encode("utf-8")
    if not data or b"\x00" in data or len(data) > limit:
        raise ValueError(f"text must be 1..{limit} bytes without NUL, got {len(data)} bytes")
    return data


# --- responses and pushes --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ok:
    pass


@dataclass(frozen=True, slots=True)
class Err:
    code: int

    @property
    def name(self) -> str:
        try:
            return ErrorCode(self.code).name.lower()
        except ValueError:
            return f"error_{self.code}"


@dataclass(frozen=True, slots=True)
class Disabled:
    pass


@dataclass(frozen=True, slots=True)
class SelfInfo:
    public_key: bytes
    name: str
    tx_power_dbm: int
    max_tx_power_dbm: int
    frequency_khz: int
    bandwidth_hz: int
    spreading_factor: int
    coding_rate: int
    manual_add_contacts: bool


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    firmware_code: int
    max_contacts: int
    max_channels: int
    build_date: str
    model: str
    version: str
    path_hash_mode: int


@dataclass(frozen=True, slots=True)
class CurrentTime:
    timestamp: int


@dataclass(frozen=True, slots=True)
class Sent:
    flood: bool
    expected_ack: bytes
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class SendConfirmed:
    ack: bytes
    round_trip_ms: int


@dataclass(frozen=True, slots=True)
class ContactsStart:
    count: int


@dataclass(frozen=True, slots=True)
class ContactRecord:
    public_key: bytes
    node_type: int  # 1 chat, 2 repeater, 3 room server, 4 sensor
    flags: int
    out_path_len: int  # 0xFF: no known path, sends flood
    name: str
    last_advert: int
    last_modified: int


@dataclass(frozen=True, slots=True)
class EndOfContacts:
    last_modified: int


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    index: int
    name: str  # empty: slot not configured (sending there would use an all-zero key)


@dataclass(frozen=True, slots=True)
class Battery:
    millivolts: int
    storage_used_kb: int
    storage_total_kb: int


@dataclass(frozen=True, slots=True)
class NoMoreMessages:
    pass


@dataclass(frozen=True, slots=True)
class MessageWaiting:
    pass


@dataclass(frozen=True, slots=True)
class Advert:
    public_key: bytes
    new: bool  # NEW_ADVERT: from a node that is not in the contact table


@dataclass(frozen=True, slots=True)
class PathUpdated:
    public_key: bytes


@dataclass(frozen=True, slots=True)
class ContactDeleted:
    """The radio overwrote this contact to make room for a new one (MyMesh.cpp:342-349)."""

    public_key: bytes


@dataclass(frozen=True, slots=True)
class AutoAddConfig:
    """Response 25: `AutoAdd` flags and the hop limit for automatic adding (0: no limit)."""

    flags: int
    max_hops: int

    @property
    def overwrites_oldest(self) -> bool:
        return bool(self.flags & AutoAdd.OVERWRITE_OLDEST)


@dataclass(frozen=True, slots=True)
class RxLog:
    snr_db: float
    rssi_dbm: int
    raw: bytes


@dataclass(frozen=True, slots=True)
class Unknown:
    code: int
    payload: bytes


MESSAGE_CODES = frozenset(
    {
        Response.CONTACT_MSG_RECV,
        Response.CHANNEL_MSG_RECV,
        Response.CONTACT_MSG_RECV_V3,
        Response.CHANNEL_MSG_RECV_V3,
        Response.CHANNEL_DATA_RECV,
    }
)

Decoded = (
    Ok | Err | Disabled | SelfInfo | DeviceInfo | CurrentTime | Sent | SendConfirmed
    | ContactsStart | ContactRecord | EndOfContacts | ChannelInfo | Battery | NoMoreMessages
    | MessageWaiting | Advert | PathUpdated | ContactDeleted | AutoAddConfig | RxLog | Unknown
)  # fmt: skip


def decode(payload: bytes) -> Decoded:
    """Decode one frame payload (first byte = code). Messages go through `decode_message`."""
    if not payload:
        raise ProtocolError("empty frame")
    decoder = _DECODERS.get(payload[0])
    if decoder is None:
        return Unknown(code=payload[0], payload=payload)
    try:
        return decoder(payload)
    except (struct.error, IndexError) as exc:
        raise ProtocolError(f"frame 0x{payload[0]:02x} too short ({len(payload)} bytes)") from exc


def _sent(p: bytes) -> Sent:
    _need(p, 10)
    return Sent(flood=p[1] == 1, expected_ack=p[2:6], timeout_ms=struct.unpack_from("<I", p, 6)[0])


def _send_confirmed(p: bytes) -> SendConfirmed:
    _need(p, 9)
    return SendConfirmed(ack=p[1:5], round_trip_ms=struct.unpack_from("<I", p, 5)[0])


def _channel_info(p: bytes) -> ChannelInfo:
    _need(p, 34)
    return ChannelInfo(index=p[1], name=_cstring(p[2:34]))


def _battery(p: bytes) -> Battery:
    mv, used, total = struct.unpack_from("<HII", p, 1)
    return Battery(millivolts=mv, storage_used_kb=used, storage_total_kb=total)


def _contact_deleted(p: bytes) -> ContactDeleted:
    _need(p, 33)
    return ContactDeleted(public_key=p[1:33])


def _advert(p: bytes) -> Advert:
    _need(p, 33)
    return Advert(public_key=p[1:33], new=p[0] == Push.NEW_ADVERT)


def _path_updated(p: bytes) -> PathUpdated:
    _need(p, 33)
    return PathUpdated(public_key=p[1:33])


def _rx_log(p: bytes) -> RxLog:
    snr, rssi = struct.unpack_from("<bb", p, 1)
    return RxLog(snr_db=snr / 4, rssi_dbm=rssi, raw=p[3:])


def _self_info(p: bytes) -> SelfInfo:
    _need(p, 58)
    tx_power, max_tx = struct.unpack_from("<bB", p, 2)
    freq_khz, bw_hz, sf, cr = struct.unpack_from("<IIBB", p, 48)
    if not (5 <= sf <= 12 and 5 <= cr <= 8 and bw_hz > 0):  # the firmware clamps to these
        raise ProtocolError(f"impossible modulation: SF {sf}, CR 4/{cr}, {bw_hz} Hz")
    return SelfInfo(
        public_key=p[4:36],
        name=p[58:].decode("utf-8", errors="replace"),
        tx_power_dbm=tx_power,
        max_tx_power_dbm=max_tx,
        frequency_khz=freq_khz,
        bandwidth_hz=bw_hz,
        spreading_factor=sf,
        coding_rate=cr,
        manual_add_contacts=bool(p[47] & 1),
    )


def _device_info(p: bytes) -> DeviceInfo:
    _need(p, 80)  # 81 from firmware code 9 (repeat flag), 82 from code 10 (path hash mode)
    return DeviceInfo(
        firmware_code=p[1],
        max_contacts=p[2] * 2,
        max_channels=p[3],
        build_date=_cstring(p[8:20]),
        model=_cstring(p[20:60]),
        version=_cstring(p[60:80]),
        path_hash_mode=p[81] if len(p) > 81 else 0,
    )


def _contact(p: bytes) -> ContactRecord:
    _need(p, 148)
    last_advert, _lat, _lon, last_modified = struct.unpack_from("<IiiI", p, 132)
    return ContactRecord(
        public_key=p[1:33],
        node_type=p[33],
        flags=p[34],
        out_path_len=p[35],
        name=_cstring(p[100:132]),
        last_advert=last_advert,
        last_modified=last_modified,
    )


def decode_message(p: bytes, received_at: float) -> InboundMessage:
    """Decode a message returned by SYNC_NEXT_MESSAGE, in the legacy or the V3 layout."""
    try:
        return _message(p, received_at)
    except (struct.error, IndexError, ValueError) as exc:
        raise ProtocolError(f"message frame 0x{p[0]:02x} malformed ({len(p)} bytes)") from exc


def _message(p: bytes, received_at: float) -> InboundMessage:
    code = p[0]
    v3 = code in (Response.CONTACT_MSG_RECV_V3, Response.CHANNEL_MSG_RECV_V3)
    snr = struct.unpack_from("<b", p, 1)[0] / 4 if v3 else None
    o = 4 if v3 else 1  # offset after the code (and the V3 SNR + reserved bytes)
    if code in (Response.CONTACT_MSG_RECV, Response.CONTACT_MSG_RECV_V3):
        prefix, path_len, text_type = p[o : o + 6], p[o + 6], TextType(p[o + 7])
        timestamp = struct.unpack_from("<I", p, o + 8)[0]
        text_start = o + 12 + (4 if text_type is TextType.SIGNED_PLAIN else 0)
        _need(p, o + 12)
        return InboundMessage(
            kind=MessageKind.CONTACT,
            text=p[text_start:].decode("utf-8", errors="replace"),
            sender_timestamp=timestamp,
            received_at=received_at,
            text_type=text_type,
            sender_prefix=prefix.hex(),
            snr_db=snr,
            hops=_hops(path_len),
        )
    if code in (Response.CHANNEL_MSG_RECV, Response.CHANNEL_MSG_RECV_V3):
        index, path_len, text_type = p[o], p[o + 1], TextType(p[o + 2])
        timestamp = struct.unpack_from("<I", p, o + 3)[0]
        return InboundMessage(
            kind=MessageKind.CHANNEL,
            text=p[o + 7 :].decode("utf-8", errors="replace"),
            sender_timestamp=timestamp,
            received_at=received_at,
            text_type=text_type,
            channel_index=index,
            snr_db=snr,
            hops=_hops(path_len),
        )
    raise ProtocolError(f"frame 0x{code:02x} is not a text message")


def _hops(path_len: int) -> int | None:
    """Hop count of a flooded message; None if it arrived by direct route (count unknown)."""
    return None if path_len == DIRECT_PATH else path_len & 0x3F


def _need(p: bytes, size: int) -> None:
    if len(p) < size:
        raise IndexError(size)


def _cstring(data: bytes) -> str:
    """NUL-terminated string; bytes after the NUL may be stale buffer contents."""
    return data.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


_DECODERS: dict[int, Callable[[bytes], Decoded]] = {
    Response.OK: lambda _: Ok(),
    Response.ERR: lambda p: Err(p[1]),
    Response.DISABLED: lambda _: Disabled(),
    Response.SELF_INFO: _self_info,
    Response.DEVICE_INFO: _device_info,
    Response.CURR_TIME: lambda p: CurrentTime(*struct.unpack_from("<I", p, 1)),
    Response.SENT: _sent,
    Response.CONTACTS_START: lambda p: ContactsStart(*struct.unpack_from("<I", p, 1)),
    Response.CONTACT: _contact,
    Response.END_OF_CONTACTS: lambda p: EndOfContacts(*struct.unpack_from("<I", p, 1)),
    Response.CHANNEL_INFO: _channel_info,
    Response.BATT_AND_STORAGE: _battery,
    Response.NO_MORE_MESSAGES: lambda _: NoMoreMessages(),
    Push.SEND_CONFIRMED: _send_confirmed,
    Push.MSG_WAITING: lambda _: MessageWaiting(),
    Push.ADVERT: _advert,
    Push.NEW_ADVERT: _advert,
    Push.PATH_UPDATED: _path_updated,
    Push.CONTACT_DELETED: _contact_deleted,
    Response.AUTOADD_CONFIG: lambda p: AutoAddConfig(flags=p[1], max_hops=p[2]),
    Push.LOG_RX_DATA: _rx_log,
}
