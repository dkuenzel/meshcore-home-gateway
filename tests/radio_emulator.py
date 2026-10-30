"""An in-memory companion radio for tests (implements the Transport protocol).

It parses the bridge's frames like the firmware does (an idle parser that ignores everything
but '<') and answers the command subset with the layouts of MeshCore companion-v1.18.0. Tests
steer it: queue messages, push acknowledgements, inject errors or silence, drop the connection.
"""

from __future__ import annotations

import asyncio
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import Command, Push, Response
from meshcore_home_gateway.protocol.framing import TO_RADIO, encode_frame

SELF_KEY = bytes(range(0x10, 0x30))
START_TIME = 1_800_000_000
# voltage 4.12 V, position 50.12345/8.56775 at 120.5 m, MCU temperature 23.4 °C (19 bytes)
LPP_FULL = bytes.fromhex("0174019c018807a5f2014ead002f12016700ea")
# the same with a BME280 on channel 2: 21.3 °C, 55.5 %, 1013.2 hPa, 120 m, before the MCU
LPP_BME280 = bytes.fromhex("0174019c018807a5f2014ead002f12026700d502686f0273279402790078016700ea")
# a T1000-E: battery, its board's temperature 23.4 °C and light level 50 (0-100) on channel 1,
# then the MCU temperature 20.0 °C
LPP_T1000E = bytes.fromhex("0174019c016700ea01650032016700c8")
# battery and a board sensor on channel 1 (21.3 °C, 55.5 %, 1013.2 hPa); no MCU temperature
LPP_WIO_E5 = bytes.fromhex("0174019c016700d501686f01732794")
TELEMETRY_T_MS = 410  # time on air of the request at EU/UK Narrow, as the radio estimates it

# bytes of each single-value type the documents publish (CayenneLPP.h:27-111 of 1.6.1)
_LPP_SIZES = {
    2: 2, 100: 4, 101: 2, 103: 2, 104: 1, 115: 2, 116: 2, 117: 2, 118: 4, 120: 1, 121: 2,
    128: 2, 130: 4,
}  # fmt: skip


def lpp_entry(channel: int, lpp_type: int, raw: int) -> bytes:
    """One single-value CayenneLPP entry: the integer the encoder writes, big endian."""
    return bytes([channel, lpp_type]) + raw.to_bytes(_LPP_SIZES[lpp_type], "big", signed=raw < 0)


# every reading key the documents publish (16 readings, the cap of Home Assistant components):
# the battery first, a channel 1 temperature and light level, one entry of each published type
# on channel 2 and a second channel 2 temperature, the MCU temperature last
LPP_ALL = b"".join(
    lpp_entry(channel, lpp_type, raw)
    for channel, lpp_type, raw in (
        (1, 116, 412), (1, 103, 215), (1, 101, 50), (2, 116, 330), (2, 117, -125), (2, 128, 12),
        (2, 103, 213), (2, 104, 111), (2, 115, 10132), (2, 121, 120), (2, 101, 300), (2, 100, 7),
        (2, 2, 123), (2, 120, 80), (2, 118, 868), (2, 130, 1500), (2, 103, 198), (1, 103, 234),
    )
)  # fmt: skip
# LPP_ALL's readings besides the battery (4.12 V) and the MCU temperature (23.4 °C)
LPP_ALL_READINGS = {
    "ch1_temperature_c": 21.5, "ch1_luminosity": 50.0, "ch2_voltage_v": 3.3,
    "ch2_current_a": -0.125, "ch2_power_w": 12.0, "ch2_temperature_c": 21.3,
    "ch2_humidity_percent": 55.5, "ch2_barometric_pressure_hpa": 1013.2, "ch2_altitude_m": 120.0,
    "ch2_luminosity": 300.0, "ch2_generic": 7.0, "ch2_analog_input": 1.23,
    "ch2_percentage_percent": 80.0, "ch2_frequency_hz": 868.0, "ch2_distance_m": 1.5,
    "ch2_temperature_2_c": 19.8,
}  # fmt: skip


def frame(payload: bytes) -> bytes:
    return b">" + len(payload).to_bytes(2, "little") + payload


@dataclass
class EmulatedContact:
    public_key: bytes
    name: str
    node_type: int = 1
    out_path_len: int = 0xFF  # unknown path: the radio floods
    flags: int = 0
    out_path: bytes = b""
    latitude_e6: int = 0
    longitude_e6: int = 0
    last_advert: int = START_TIME - 3600

    def record(self, code: int = Response.CONTACT) -> bytes:
        """The contact layout (MyMesh.cpp:171-192), also of the NEW_ADVERT push."""
        name = self.name.encode()[:31].ljust(32, b"\x00")
        return (
            bytes([code]) + self.public_key + bytes([self.node_type, self.flags, self.out_path_len])
            + self.out_path.ljust(64, b"\x00") + name
            + struct.pack("<IiiI", self.last_advert, self.latitude_e6, self.longitude_e6,
                          START_TIME - 60)
        )  # fmt: skip


@dataclass
class RemoteCompanion:
    """The companion behind a contact, as it answers a telemetry request. `lpp=None` is
    silence: no permission, the gateway not its contact, or out of range (MyMesh.cpp:666, 687)."""

    lpp: bytes | None = LPP_FULL
    hops: int = 0  # the path the radio learns from a flood (one-byte hashes)
    auto: bool = True  # answer by itself; False: the test calls EmulatedRadio.answer_telemetry()
    flood_reply: bool = False  # floods its answer to a direct request: the packet log shows it
    same_write: bool = False  # answer in the same write as SENT, not a moment later
    delay_s: float = 0.05  # how long after SENT it answers (the firmware's is 300 ms and more)


@dataclass
class SentText:
    prefix: bytes
    text: str
    timestamp: int
    attempt: int


@dataclass
class EmulatedRadio:
    name: str = "Bridge"
    clock: int = START_TIME
    contacts: list[EmulatedContact] = field(default_factory=list)
    channels: dict[int, str] = field(default_factory=lambda: {0: "Public"})
    max_channels: int = 4
    manual_add: bool = True
    autoadd_config: int = 0  # AUTO_ADD_* flags (MyMesh.cpp:147-151)
    autoadd_hops: int | None = 0  # None: firmware 1.12 and 1.13 answer without the hop limit
    spreading_factor: int = 8
    coding_rate: int = 8
    latitude_e6: int = 0
    longitude_e6: int = 0
    telemetry_modes: int = 0  # bits 0-1 base, 2-3 location, 4-5 environment
    advert_location: int = 0
    tuning: tuple[int, int] | None = (0, 1000)  # rx delay base, airtime factor, x 1000
    stats: bool = True  # firmware with GET_STATS (1.11 and later)
    packets_received: int = 120
    tx_airtime_s: int = 41  # the radio's own time on air, as GET_STATS counts it
    remotes: dict[bytes, RemoteCompanion] = field(default_factory=dict)  # key -> its companion
    auto_ack: bool = True
    ack_round_trip_ms: int = 1500
    errors: dict[int, int] = field(default_factory=dict)  # command code -> error code, once
    silent: set[int] = field(default_factory=set)  # command codes left unanswered
    drop_on: set[int] = field(default_factory=set)  # command codes that unplug the radio, once
    disabled: set[int] = field(default_factory=set)  # command codes answered with DISABLED
    unknown: set[int] = field(default_factory=set)  # older firmware: ERR "unsupported", always
    ignored: set[int] = field(default_factory=set)  # setters answered OK without effect
    firmware_code: int = 14  # FIRMWARE_VER_CODE of companion 1.18
    max_contacts: int = 100  # DEVICE_INFO reports half of it
    frequency_khz: int = 869_618  # in the 10 % sub-band
    description: str = "emulated radio"
    needs_flush: bool = True  # parses like the USB serial interface

    def __post_init__(self) -> None:
        # own copies: the radio changes them (path reset, one-time errors), tests share originals
        self.contacts = [replace(contact) for contact in self.contacts]
        self.errors = dict(self.errors)
        self.silent, self.drop_on = set(self.silent), set(self.drop_on)
        self.disabled, self.unknown = set(self.disabled), set(self.unknown)
        self.ignored = set(self.ignored)
        self.opened = False
        self.received: list[bytes] = []  # every command payload, in order
        self.raw_written = bytearray()
        self.texts: list[SentText] = []
        self.channel_texts: list[tuple[int, str, int]] = []
        self.path_resets: list[bytes] = []
        self.telemetry_requests: list[tuple[bytes, bool]] = []  # key, flooded
        self._asked: tuple[bytes, bool] | None = None  # the pending telemetry request
        self._tag = 0x11223344
        self.queue: list[bytes] = []
        self._out: asyncio.Queue[bytes | Exception] = asyncio.Queue()
        self._buffer = bytearray()

    # --- Transport ------------------------------------------------------------------------------

    async def open(self) -> None:
        self.opened = True

    async def read(self) -> bytes:
        item = await self._out.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def write(self, data: bytes) -> None:
        if not self.opened:
            raise ConnectionError("emulated radio: not open")
        self.raw_written += data
        self._buffer += data
        while True:
            start = self._buffer.find(b"<")
            if start < 0:
                self._buffer.clear()
                return
            del self._buffer[:start]
            if len(self._buffer) < 3:
                return
            length = int.from_bytes(self._buffer[1:3], "little")
            if len(self._buffer) < 3 + length:
                return
            payload = bytes(self._buffer[3 : 3 + length])
            del self._buffer[: 3 + length]
            if payload:
                self._handle(payload)

    async def close(self) -> None:
        self.opened = False

    # --- test controls -------------------------------------------------------------------------

    def emit(self, payload: bytes) -> None:
        self._out.put_nowait(frame(payload))

    def emit_raw(self, data: bytes) -> None:
        self._out.put_nowait(data)

    def disconnect(self) -> None:
        self._out.put_nowait(ConnectionError("emulated radio: unplugged"))

    def queue_direct_message(
        self, prefix: bytes, text: str, timestamp: int, *, v3: bool = True, snr_q: int = 24,
        path_len: int = 0xFF, text_type: int = 0, push: bool = True,
    ) -> None:  # fmt: skip
        body = prefix + bytes([path_len, text_type]) + struct.pack("<I", timestamp) + text.encode()
        payload = bytes([0x10, snr_q & 0xFF, 0, 0]) + body if v3 else bytes([0x07]) + body
        self._queue(payload, push)

    def queue_channel_message(
        self, index: int, text: str, timestamp: int, *, v3: bool = True, push: bool = True
    ) -> None:
        body = bytes([index, 0, 0]) + struct.pack("<I", timestamp) + text.encode()
        payload = bytes([0x11, 8, 0, 0]) + body if v3 else bytes([0x08]) + body
        self._queue(payload, push)

    def _queue(self, payload: bytes, push: bool) -> None:
        self.queue.append(payload)
        if push:
            self.emit(bytes([Push.MSG_WAITING]))

    def advertise(self, contact: EmulatedContact) -> None:
        """A checked advert: ADVERT with the key for a contact, else NEW_ADVERT with the record
        (MyMesh.cpp:356-367)."""
        known = any(c.public_key == contact.public_key for c in self.contacts)
        if known:
            self.emit(bytes([Push.ADVERT]) + contact.public_key)
        else:
            self.emit(contact.record(Push.NEW_ADVERT))

    def overwrite(self, contact: EmulatedContact) -> None:
        """A full table with AUTO_ADD_OVERWRITE_OLDEST drops this contact (MyMesh.cpp:342-349)."""
        self.contacts = [c for c in self.contacts if c.public_key != contact.public_key]
        self.emit(bytes([Push.CONTACT_DELETED]) + contact.public_key)

    def acknowledge(self, ack: bytes, round_trip_ms: int = 1500) -> None:
        self.emit(bytes([Push.SEND_CONFIRMED]) + ack + struct.pack("<I", round_trip_ms))

    def commands(self) -> list[int]:
        return [p[0] for p in self.received]

    def answer_telemetry(self) -> None:
        """The pending request's companion answers (MyMesh.cpp:739-749): a direct request
        directly (or by flood, if it has no path back), a flooded one by a path return, whose
        path the radio stores and reports first (BaseChatMesh.cpp:353-367)."""
        if self._asked is None:
            return
        key, flood = self._asked
        remote = self.remotes.get(key)
        if remote is None or remote.lpp is None:
            return
        self._asked = None  # the radio clears its slot on a match
        if flood:
            contact = next(c for c in self.contacts if c.public_key == key)
            contact.out_path_len, contact.out_path = remote.hops, bytes(range(1, remote.hops + 1))
            self.emit(bytes([Push.PATH_UPDATED]) + key)
            padding = (16 - (6 + remote.hops + len(remote.lpp)) % 16) % 16
        else:
            if remote.flood_reply:  # a flooded RESPONSE to this radio, logged before decryption
                packet = bytes([0x05, 0x00, SELF_KEY[0], key[0], 0xBE, 0xEF]) + bytes(32)
                self.emit(bytes([Push.LOG_RX_DATA, 24, 0xA0]) + packet)
            padding = (16 - (4 + len(remote.lpp)) % 16) % 16
        self.emit(telemetry_push(key, remote.lpp + bytes(padding)))

    # --- firmware model -------------------------------------------------------------------------

    def _handle(self, p: bytes) -> None:  # noqa: PLR0912 - one branch per command
        code = p[0]
        self.received.append(p)
        if code in self.silent:
            return
        if code in self.drop_on:
            self.drop_on.discard(code)
            self.disconnect()
            return
        if code in self.disabled:
            self.emit(bytes([Response.DISABLED]))
            return
        if code in self.errors:
            self.emit(bytes([Response.ERR, self.errors.pop(code)]))
            return
        if code in self.unknown:
            self.emit(bytes([Response.ERR, 1]))  # ERR_CODE_UNSUPPORTED_CMD
            return
        if code == Command.APP_START:
            self.emit(self._self_info())
        elif code == Command.DEVICE_QUERY:
            self.emit(self._device_info())
        elif code == Command.GET_DEVICE_TIME:
            self.emit(bytes([Response.CURR_TIME]) + struct.pack("<I", self.clock))
        elif code == Command.SET_DEVICE_TIME:
            value = struct.unpack_from("<I", p, 1)[0]
            if value >= self.clock:
                self.clock = value
                self.emit(bytes([Response.OK]))
            else:
                self.emit(bytes([Response.ERR, 6]))
        elif code == Command.GET_CONTACTS:
            self.emit(bytes([Response.CONTACTS_START]) + struct.pack("<I", len(self.contacts)))
            for contact in self.contacts:
                self.emit(contact.record())
            self.emit(bytes([Response.END_OF_CONTACTS]) + struct.pack("<I", START_TIME - 60))
        elif code == Command.GET_CHANNEL:
            index = p[1]
            if index >= self.max_channels:
                self.emit(bytes([Response.ERR, 2]))
            else:
                name = self.channels.get(index, "").encode().ljust(32, b"\x00")
                self.emit(bytes([Response.CHANNEL_INFO, index]) + name + bytes(16))
        elif code == Command.GET_BATT_AND_STORAGE:
            self.emit(bytes([Response.BATT_AND_STORAGE]) + struct.pack("<HII", 4012, 12, 976))
        elif code == Command.SYNC_NEXT_MESSAGE:
            self.emit(self.queue.pop(0) if self.queue else bytes([Response.NO_MORE_MESSAGES]))
        elif code == Command.SEND_TXT_MSG:
            self._send_text(p)
        elif code == Command.SEND_CHANNEL_TXT_MSG:
            ts = struct.unpack_from("<I", p, 3)[0]
            self.channel_texts.append((p[2], p[7:].decode(), ts))
            self.emit(bytes([Response.OK]))
        elif code == Command.GET_AUTOADD_CONFIG:
            hops = b"" if self.autoadd_hops is None else bytes([self.autoadd_hops])
            self.emit(bytes([Response.AUTOADD_CONFIG, self.autoadd_config]) + hops)
        elif code == Command.GET_TUNING_PARAMS and self.tuning is not None:
            self.emit(bytes([Response.TUNING_PARAMS]) + struct.pack("<II", *self.tuning))
        elif code == Command.GET_STATS and self.stats:
            self._stats(p[1])
        elif code in _SETTERS:
            if code not in self.ignored:
                self._set(code, p)
            self.emit(bytes([Response.OK]))
        elif code == Command.ADD_UPDATE_CONTACT:
            self._add_update_contact(p)
        elif code == Command.REMOVE_CONTACT:  # by the full key, no length check (MyMesh.cpp:1349)
            gone = next((c for c in self.contacts if c.public_key == p[1:33]), None)
            if gone is None:
                self.emit(bytes([Response.ERR, 2]))
            else:
                if code not in self.ignored:
                    self.contacts.remove(gone)
                self.emit(bytes([Response.OK]))
        elif code == Command.SEND_TELEMETRY_REQ and len(p) >= 36:
            self._telemetry(p[4:36])
        elif code == Command.RESET_PATH:
            self.path_resets.append(p[1:33])
            for contact in self.contacts:
                if contact.public_key == p[1:33]:
                    contact.out_path_len = 0xFF
            self.emit(bytes([Response.OK]))
        else:
            self.emit(bytes([Response.ERR, 1]))

    def _set(self, code: int, p: bytes) -> None:
        """The setters of the managed profile (MyMesh.cpp:1259-1265, 1328-1346, 1470-1503,
        2023-2029)."""
        if code == Command.SET_OTHER_PARAMS:
            self.manual_add = bool(p[1])
            if len(p) >= 3:
                self.telemetry_modes = p[2] & 0x3F
            if len(p) >= 4:
                self.advert_location = p[3]
        elif code == Command.SET_AUTOADD_CONFIG:
            self.autoadd_config = p[1]
            if len(p) >= 3:
                self.autoadd_hops = min(p[2], 64)
        elif code == Command.SET_TUNING_PARAMS:
            self.tuning = struct.unpack_from("<II", p, 1)
        elif code == Command.SET_ADVERT_NAME:
            self.name = p[1:32].decode("utf-8", errors="replace")

    def _add_update_contact(self, p: bytes) -> None:
        """Every field of an existing key is overwritten; a new one needs a free slot, or with
        overwrite-oldest on takes the oldest non-favourite's (BaseChatMesh.cpp:88-103); type 0
        lands in one that is never listed (MyMesh.cpp:194-217, 1328-1348)."""
        key, node_type, flags, path_len = p[1:33], p[33], p[34], p[35]
        name = p[100:132].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        last_advert, latitude, longitude = (
            struct.unpack_from("<Iii", p, 132) if len(p) >= 144 else (0, 0, 0)
        )
        existing = next((c for c in self.contacts if c.public_key == key), None)
        if existing is None and len(self.contacts) >= self.max_contacts:
            oldest = next((c for c in self.contacts if not c.flags & 0x01), None)
            if not self.autoadd_config & 0x01 or oldest is None:
                self.emit(bytes([Response.ERR, 3]))
                return
            self.overwrite(oldest)
        self.emit(bytes([Response.OK]))
        if Command.ADD_UPDATE_CONTACT in self.ignored or (existing is None and node_type == 0):
            return
        contact = existing or EmulatedContact(key, name)
        contact.name, contact.node_type, contact.flags, contact.out_path_len = (
            name,
            node_type,
            flags,
            path_len,
        )
        contact.out_path = p[36 : 36 + (path_len if path_len != 0xFF else 0)]
        contact.last_advert, contact.latitude_e6, contact.longitude_e6 = (
            last_advert,
            latitude,
            longitude,
        )
        if existing is None:
            self.contacts.append(contact)

    def _send_text(self, p: bytes) -> None:
        attempt, ts, prefix, text = (
            p[2],
            struct.unpack_from("<I", p, 3)[0],
            p[7:13],
            p[13:].decode(),
        )
        contact = next((c for c in self.contacts if c.public_key.startswith(prefix)), None)
        if contact is None:
            self.emit(bytes([Response.ERR, 2]))
            return
        self.texts.append(SentText(prefix, text, ts, attempt))
        ack = codec.expected_ack(ts, attempt, text, SELF_KEY)
        flood = contact.out_path_len == 0xFF
        self.emit(bytes([Response.SENT, int(flood)]) + ack + struct.pack("<I", 5684))
        if self.auto_ack:
            self.acknowledge(ack, self.ack_round_trip_ms)

    def _telemetry(self, key: bytes) -> None:
        """SEND_TELEMETRY_REQ (MyMesh.cpp:1680-1699): SENT with the route and the estimate
        (:888-896); a new request replaces the pending one."""
        contact = next((c for c in self.contacts if c.public_key == key), None)
        if contact is None:
            self.emit(bytes([Response.ERR, 2]))
            return
        flood = contact.out_path_len == 0xFF
        hops = 0 if flood else contact.out_path_len & 0x3F
        t = TELEMETRY_T_MS
        timeout = 500 + 16 * t if flood else 500 + (6 * t + 250) * (hops + 1)
        self._tag += 1
        self.emit(bytes([Response.SENT, int(flood)]) + struct.pack("<II", self._tag, timeout))
        self.telemetry_requests.append((key, flood))
        self._asked = (key, flood)
        remote = self.remotes.get(key)
        if remote is not None and remote.auto:
            if remote.same_write:
                self.answer_telemetry()
            else:
                asyncio.get_running_loop().call_later(remote.delay_s, self._answer_if, key)

    def _answer_if(self, key: bytes) -> None:
        """The delayed answer, if this request is still the radio's pending one."""
        if self._asked is not None and self._asked[0] == key:
            self.answer_telemetry()

    def _stats(self, kind: int) -> None:
        """GET_STATS (MyMesh.cpp:1914-1964)."""
        if kind == 0:
            body = struct.pack("<HIHB", 4012, 3600, 0, 0)
        elif kind == 1:
            body = struct.pack("<hbbII", -118, -97, 26, self.tx_airtime_s, 300)
        elif kind == 2:
            body = struct.pack("<7I", self.packets_received, 30, 10, 20, 70, 50, 2)
        else:
            self.emit(bytes([Response.ERR, 6]))
            return
        self.emit(bytes([Response.STATS, kind]) + body)

    def _self_info(self) -> bytes:
        return (
            bytes([Response.SELF_INFO, 1, 22, 22]) + SELF_KEY
            + struct.pack("<ii", self.latitude_e6, self.longitude_e6)
            + bytes([0, self.advert_location, self.telemetry_modes, int(self.manual_add)])
            + struct.pack("<IIBB", self.frequency_khz, 62_500, self.spreading_factor,
                          self.coding_rate)
            + self.name.encode()
        )  # fmt: skip

    def _device_info(self) -> bytes:
        return (
            bytes([Response.DEVICE_INFO, self.firmware_code, self.max_contacts // 2,
                   self.max_channels]) + bytes(4)
            + b"15 Oct 2026".ljust(12, b"\x00") + b"Example Radio".ljust(40, b"\x00")
            + b"v1.18.0".ljust(20, b"\x00") + bytes([0, 0])
        )  # fmt: skip


def telemetry_push(key: bytes, data: bytes) -> bytes:
    """Push 0x8B: reserved byte, the prefix of the contact that answered, its LPP."""
    return bytes([Push.TELEMETRY_RESPONSE, 0]) + key[:6] + data


_SETTERS = frozenset({
    Command.SET_OTHER_PARAMS, Command.SET_AUTOADD_CONFIG, Command.SET_TUNING_PARAMS,
    Command.SET_ADVERT_NAME,
})  # fmt: skip


class WifiFrameParser:
    """Frame parser of the WiFi companion firmware (src/helpers/wifi/SerialWifiInterface.cpp
    at companion-v1.18.0, :102-161), unlike the serial one: while no header is pending it
    consumes exactly 3 bytes as a header; a header with type 0 or length 0 is not one
    (:46-48), and the next 3 bytes are read as a header again. Then it waits for the announced
    length and keeps the frame only if the type is '<'. A new connection resets it (:57-70).
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._header: tuple[int, int] | None = None

    def feed(self, data: bytes) -> list[bytes]:
        """Payloads of the complete frames in `data`."""
        self._buffer += data
        payloads: list[bytes] = []
        while True:
            if self._header is None:
                if len(self._buffer) < 3:
                    return payloads
                kind, length = self._buffer[0], int.from_bytes(self._buffer[1:3], "little")
                del self._buffer[:3]
                if kind != 0 and length != 0:
                    self._header = (kind, length)
                continue
            kind, length = self._header
            if len(self._buffer) < length:
                return payloads
            payload = bytes(self._buffer[:length])
            del self._buffer[:length]
            self._header = None
            if kind == TO_RADIO:
                payloads.append(payload)


class TcpRadioServer:
    """Serves one EmulatedRadio over TCP from a background thread, like a WiFi companion: the
    bytes pass the WiFi firmware's frame parser (`WifiFrameParser`) before the radio sees them.

    The radio lives in the server's event loop; tests read its records after the fact.
    """

    def __init__(self, radio: EmulatedRadio) -> None:
        self.radio = radio
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> TcpRadioServer:
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("radio server did not start")
        return self

    def __exit__(self, *_: object) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    def call(self, function: Callable[[], None]) -> None:
        """Run a test control (e.g. queue a message) inside the server loop."""
        assert self._loop is not None
        self._loop.call_soon_threadsafe(function)

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        server = loop.run_until_complete(asyncio.start_server(self._client, "127.0.0.1", 0))
        self.port = server.sockets[0].getsockname()[1]
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            server.close()
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.run_until_complete(asyncio.sleep(0))
            loop.close()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        radio = self.radio
        radio._out = asyncio.Queue()  # a new client does not see frames meant for the previous one
        await radio.open()

        async def pump() -> None:
            while True:
                writer.write(await radio.read())
                await writer.drain()

        task = asyncio.create_task(pump())
        parser = WifiFrameParser()  # one per connection, as in the firmware
        try:
            while data := await reader.read(4096):
                for payload in parser.feed(data):
                    await radio.write(encode_frame(payload))
        except (ConnectionError, OSError):
            pass
        finally:
            task.cancel()
            writer.close()
