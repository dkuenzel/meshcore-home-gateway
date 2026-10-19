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

from meshcore_bus.protocol import codec
from meshcore_bus.protocol.codes import Command, Push, Response

SELF_KEY = bytes(range(0x10, 0x30))
START_TIME = 1_800_000_000


def frame(payload: bytes) -> bytes:
    return b">" + len(payload).to_bytes(2, "little") + payload


@dataclass
class EmulatedContact:
    public_key: bytes
    name: str
    node_type: int = 1
    out_path_len: int = 0xFF  # unknown path: the radio floods

    def record(self, code: int = Response.CONTACT) -> bytes:
        name = self.name.encode()[:31].ljust(32, b"\x00")
        return (
            bytes([code]) + self.public_key + bytes([self.node_type, 0, self.out_path_len])
            + bytes(64) + name + struct.pack("<IiiI", START_TIME - 3600, 0, 0, START_TIME - 60)
        )  # fmt: skip


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
    auto_ack: bool = True
    ack_round_trip_ms: int = 1500
    errors: dict[int, int] = field(default_factory=dict)  # command code -> error code, once
    silent: set[int] = field(default_factory=set)  # command codes left unanswered
    drop_on: set[int] = field(default_factory=set)  # command codes that unplug the radio, once
    disabled: set[int] = field(default_factory=set)  # command codes answered with DISABLED
    unknown: set[int] = field(default_factory=set)  # older firmware: ERR "unsupported", always
    description: str = "emulated radio"

    def __post_init__(self) -> None:
        # own copies: the radio changes them (path reset, one-time errors), tests share originals
        self.contacts = [replace(contact) for contact in self.contacts]
        self.errors = dict(self.errors)
        self.silent, self.drop_on = set(self.silent), set(self.drop_on)
        self.disabled, self.unknown = set(self.disabled), set(self.unknown)
        self.opened = False
        self.received: list[bytes] = []  # every command payload, in order
        self.raw_written = bytearray()
        self.texts: list[SentText] = []
        self.channel_texts: list[tuple[int, str, int]] = []
        self.path_resets: list[bytes] = []
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

    def overwrite(self, contact: EmulatedContact) -> None:
        """A full table with AUTO_ADD_OVERWRITE_OLDEST drops this contact (MyMesh.cpp:342-349)."""
        self.contacts = [c for c in self.contacts if c.public_key != contact.public_key]
        self.emit(bytes([Push.CONTACT_DELETED]) + contact.public_key)

    def acknowledge(self, ack: bytes, round_trip_ms: int = 1500) -> None:
        self.emit(bytes([Push.SEND_CONFIRMED]) + ack + struct.pack("<I", round_trip_ms))

    def commands(self) -> list[int]:
        return [p[0] for p in self.received]

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
        elif code == Command.RESET_PATH:
            self.path_resets.append(p[1:33])
            for contact in self.contacts:
                if contact.public_key == p[1:33]:
                    contact.out_path_len = 0xFF
            self.emit(bytes([Response.OK]))
        else:
            self.emit(bytes([Response.ERR, 1]))

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

    def _self_info(self) -> bytes:
        return (
            bytes([Response.SELF_INFO, 1, 22, 22]) + SELF_KEY + bytes(8)
            + bytes([0, 0, 0, int(self.manual_add)])
            + struct.pack("<IIBB", 869_618, 62_500, self.spreading_factor, self.coding_rate)
            + self.name.encode()
        )  # fmt: skip

    def _device_info(self) -> bytes:
        return (
            bytes([Response.DEVICE_INFO, 14, 50, self.max_channels]) + bytes(4)
            + b"15 Oct 2026".ljust(12, b"\x00") + b"Example Radio".ljust(40, b"\x00")
            + b"v1.18.0".ljust(20, b"\x00") + bytes([0, 0])
        )  # fmt: skip


class TcpRadioServer:
    """Serves one EmulatedRadio over TCP from a background thread, like a WiFi companion.

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
        try:
            while data := await reader.read(4096):
                await radio.write(data)
        except (ConnectionError, OSError):
            pass
        finally:
            task.cancel()
            writer.close()
