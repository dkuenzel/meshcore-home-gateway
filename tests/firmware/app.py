"""A phone app for a companion node: the companion protocol over the node's serial port, on the
gateway's own transport, framing and codec.

It does what the MeshCore app does for its user, as docs/companions.md describes it: set the
clock, allow telemetry, grant the gateway its permissions, switch the GPS on, advertise, reset a
path, send a message. It only ever does what a test tells it: the app's own policies (its
retries, when it floods) are scripted by the test. Commands the gateway never sends (ADR 0020)
are built here, each after its handler in examples/companion_radio/MyMesh.cpp of the pinned
release. Every frame the radio writes is kept in `frames`, in order, with the time it came.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from types import TracebackType

from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import Command, ContactFlag, Push, Response
from meshcore_home_gateway.protocol.framing import FrameDecoder, encode_frame
from meshcore_home_gateway.radio.transport import SerialTransport
from tests.firmware.node import Node

COMMAND_S = 5.0  # the radio answers a command at once
# MyMesh.cpp:1297-1316: a second byte 1 floods the advert, otherwise it goes zero hop
SEND_SELF_ADVERT = 7
SET_ADVERT_LATLON = 14  # :1266-1280: latitude and longitude in 10^-6 degrees, int32 each
SET_CUSTOM_VAR = 41  # :1867-1889: "name:value", such as "gps:1"
# the app's "From Specific Contacts" for every kind (NodePrefs.h:7-9: 1 is by contact)
BY_CONTACT = 0b01_01_01
# what docs/companions.md step 6 grants the gateway: favourite, base, location, environment
GATEWAY_FLAGS = (
    ContactFlag.FAVOURITE | ContactFlag.TELEMETRY_BASE | ContactFlag.TELEMETRY_LOCATION
    | ContactFlag.TELEMETRY_ENVIRONMENT
)  # fmt: skip


class AppError(Exception):
    pass


@dataclass(frozen=True)
class Frame:
    at: float  # time.monotonic() when it was read
    payload: bytes

    @property
    def code(self) -> int:
        return self.payload[0]


class App:
    def __init__(self, node: Node) -> None:
        self.node = node
        self.frames: list[Frame] = []
        self._transport = SerialTransport(str(node.serial))
        self._decoder = FrameDecoder()
        self._changed = asyncio.Condition()
        self._lock = asyncio.Lock()  # one command at a time, as the radio answers them in order
        self._reader: asyncio.Task[None] | None = None

    async def __aenter__(self) -> App:
        await self._transport.open()
        self._reader = asyncio.create_task(self._read(), name=f"app {self.node.name}")
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        await self._transport.close()

    async def _read(self) -> None:
        while True:
            data = await self._transport.read()
            frames = [Frame(time.monotonic(), f) for f in self._decoder.feed(data)]
            async with self._changed:
                self.frames.extend(frames)
                self._changed.notify_all()

    # --- frames ---------------------------------------------------------------------------------

    def mark(self) -> int:
        """The number of frames so far: a later wait looks at the frames after it."""
        return len(self.frames)

    async def wait(
        self, since: int, found: Callable[[Frame], bool], what: str, timeout_s: float = COMMAND_S
    ) -> Frame:
        """The first frame after `since` that `found` accepts."""

        def first() -> Frame | None:
            return next((f for f in self.frames[since:] if found(f)), None)

        try:
            async with asyncio.timeout(timeout_s), self._changed:
                await self._changed.wait_for(lambda: first() is not None)
        except TimeoutError:
            raise AppError(f"{self.node.name}: no {what} within {timeout_s:g} s") from None
        frame = first()
        assert frame is not None  # wait_for found it
        return frame

    async def push(
        self, code: int, since: int, key: bytes | None = None, timeout_s: float = COMMAND_S
    ) -> Frame:
        """A push with this code after `since`; with `key`, one that names that contact (its
        public key, or the 6-byte prefix of a telemetry answer)."""

        def found(frame: Frame) -> bool:
            if frame.code != code:
                return False
            if key is None:
                return True
            if code == Push.TELEMETRY_RESPONSE:
                return frame.payload[2:8] == key[:6]
            return frame.payload[1:33] == key

        return await self.wait(since, found, f"push 0x{code:02x}", timeout_s)

    async def command(self, request: bytes, codes: Collection[int], name: str) -> bytes:
        """Send a command and return its answer: the first frame of one of `codes`. An ERR
        raises, unless ERR is one of `codes`."""
        wanted = {*codes, Response.ERR}
        async with self._lock:
            since = self.mark()
            await self._transport.write(encode_frame(request))
            answer = await self.wait(since, lambda f: f.code in wanted, name)
        if answer.code == Response.ERR and Response.ERR not in codes:
            raise AppError(f"{self.node.name}: {name}: {codec.Err(answer.payload[1]).name}")
        return answer.payload

    async def ok(self, request: bytes, name: str) -> None:
        await self.command(request, {Response.OK}, name)

    # --- what the app does ----------------------------------------------------------------------

    async def start(self) -> codec.SelfInfo:
        """Connect as the app does: its own info, then the radio's clock set to the phone's."""
        found = codec.decode(
            await self.command(codec.app_start("app"), {Response.SELF_INFO}, "start")
        )
        assert isinstance(found, codec.SelfInfo)  # by its code
        clock = codec.set_device_time(int(time.time()))
        await self.command(clock, {Response.OK, Response.ERR}, "set time")  # ERR: never back
        return found

    async def battery(self) -> codec.Battery:
        request = codec.get_battery()
        found = codec.decode(await self.command(request, {Response.BATT_AND_STORAGE}, "battery"))
        assert isinstance(found, codec.Battery)  # by its code
        return found

    async def contacts(self) -> dict[bytes, codec.ContactRecord]:
        async with self._lock:
            since = self.mark()
            await self._transport.write(encode_frame(codec.get_contacts()))
            await self.wait(since, lambda f: f.code == Response.END_OF_CONTACTS, "contacts")
        records = [codec.decode(f.payload) for f in self.frames[since:]]
        return {r.public_key: r for r in records if isinstance(r, codec.ContactRecord)}

    async def contact(self, key: bytes) -> codec.ContactRecord:
        found = (await self.contacts()).get(key)
        if found is None:
            raise AppError(f"{self.node.name} has no contact {key[:6].hex()}")
        return found

    async def wait_for_contact(self, key: bytes, timeout_s: float = 30.0) -> codec.ContactRecord:
        """Until the radio holds the contact (an advert added it)."""
        deadline = time.monotonic() + timeout_s
        while (found := (await self.contacts()).get(key)) is None:
            if time.monotonic() > deadline:
                raise AppError(f"{self.node.name} did not add {key[:6].hex()}")
            await asyncio.sleep(0.5)
        return found

    async def advert(self, *, flood: bool) -> None:
        await self.ok(bytes([SEND_SELF_ADVERT, int(flood)]), "advert")

    async def settings(
        self, telemetry_modes: int, *, manual_add: bool = False, share_position: bool = False
    ) -> None:
        """Telemetry Settings (modes: bits 0-1 base, 2-3 location, 4-5 environment), manual
        adding, and Share Position in Advert."""
        request = codec.set_other_params(manual_add, telemetry_modes, int(share_position))
        await self.ok(request, "other settings")

    async def set_var(self, name: str, value: str) -> None:
        await self.ok(bytes([SET_CUSTOM_VAR]) + f"{name}:{value}".encode(), f"set {name}")

    async def grant(self, key: bytes, flags: int) -> None:
        """A contact's permissions (MyMesh.cpp:194-215 takes the record with other flags); the
        gateway's codec refuses to grant telemetry, the app does not."""
        raw = (await self.contact(key)).raw
        request = bytes([Command.ADD_UPDATE_CONTACT]) + raw[:33] + bytes([flags]) + raw[34:143]
        await self.ok(request, "contact permissions")

    async def set_position(self, latitude: float, longitude: float) -> None:
        values = struct.pack("<ii", round(latitude * 1e6), round(longitude * 1e6))
        await self.ok(bytes([SET_ADVERT_LATLON]) + values, "position")

    async def reset_path(self, key: bytes) -> None:
        await self.ok(codec.reset_path(key), "reset path")

    async def remove_contact(self, key: bytes) -> None:
        await self.ok(codec.remove_contact(key), "remove contact")

    async def send_text(self, key: bytes, text: str) -> codec.Sent:
        request = codec.send_text(key[:6], text, int(time.time()), 0)
        found = codec.decode(await self.command(request, {Response.SENT}, "send text"))
        assert isinstance(found, codec.Sent)  # by its code
        return found

    async def request_telemetry(self, key: bytes) -> codec.Sent:
        request = codec.send_telemetry_request(key)
        found = codec.decode(await self.command(request, {Response.SENT}, "telemetry request"))
        assert isinstance(found, codec.Sent)  # by its code
        return found
