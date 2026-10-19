"""One connection to a MeshCore companion radio (design §6.1, docs/protocol.md).

The firmware answers one command at a time, without request ids, and sends unsolicited push
frames (codes >= 0x80) in between. The session therefore

* sends one command at a time (`asyncio.Lock`) and hands the waiting command only the frames
  with the response codes it expects; anything else is logged and dropped;
* routes push frames: new messages trigger a sync, acknowledgements and received-packet logs go
  to the event queue, adverts mark the contact table for a refresh;
* reports every full key the radio names to `on_contact_key`, with whether the radio holds or
  held that contact: pushes (advert, path, overwrite; a new advert is from a node it did not add)
  before any later frame is handled, a table read record by record before the read returns. The
  bridge's memory of look-alikes is therefore never behind a verdict (ADR 0009);
* closes itself on a transport error or a command timeout; the bridge then reconnects.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from typing import TypeVar

from meshcore_bus.core.contacts import Contact, ContactBook
from meshcore_bus.core.model import InboundMessage
from meshcore_bus.protocol import codec
from meshcore_bus.protocol.codes import AutoAdd, Push, Response
from meshcore_bus.protocol.framing import FrameDecoder, encode_frame
from meshcore_bus.radio.transport import Transport

log = logging.getLogger(__name__)

APP_NAME = "meshcore-bus"
COMMAND_TIMEOUT_S = 5.0  # the firmware documentation recommends about 5 s per command
FLUSH_BYTES = 256  # completes any partial frame left in the firmware parser (docs/protocol.md)
FLUSH_SETTLE_S = 0.5
CLOCK_SET_THRESHOLD_S = 2
CLOCK_AHEAD_WARNING_S = 60
SYNC_INTERVAL_S = 300.0  # safety net in case a MSG_WAITING push was lost
HEARTBEAT_INTERVAL_S = 60.0
CONTACTS_REFRESH_DELAY_S = 2.0

ResponseCodes = frozenset[int]
_SIMPLE = frozenset({Response.OK, Response.ERR, Response.DISABLED})
T = TypeVar("T")


class RadioError(Exception):
    """The radio cannot be used right now (disconnected, timed out, protocol violation)."""


class CommandFailedError(RadioError):
    """The radio refused a command: ERR with an error name, or `disabled`."""

    def __init__(self, command: str, reason: str) -> None:
        super().__init__(f"{command}: {reason}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RadioInfo:
    self_info: codec.SelfInfo
    device: codec.DeviceInfo

    @property
    def public_key(self) -> str:
        return self.self_info.public_key.hex()

    @property
    def name(self) -> str:
        return self.self_info.name


@dataclass(frozen=True, slots=True)
class MessageReceived:
    message: InboundMessage
    session: CompanionSession  # judge the sender on this session's contact table
    fetched_at: int  # the session's command count when the message was fetched
    at_connect: bool  # fetched at connect: it may have arrived while no session listened


@dataclass(frozen=True, slots=True)
class AckReceived:
    ack: bytes
    round_trip_ms: int


@dataclass(frozen=True, slots=True)
class PacketLogged:
    rx: codec.RxLog
    received_at: float


RadioEvent = MessageReceived | AckReceived | PacketLogged


@dataclass
class _Pending:
    expected: ResponseCodes
    frames: asyncio.Queue[bytes] = field(default_factory=asyncio.Queue)


class CompanionSession:
    def __init__(
        self,
        transport: Transport,
        events: asyncio.Queue[RadioEvent],
        *,
        wall_clock: Callable[[], float] = time.time,
        command_timeout_s: float = COMMAND_TIMEOUT_S,
        flush_settle_s: float = FLUSH_SETTLE_S,
        set_clock: bool = True,
    ) -> None:
        self._transport = transport
        self._events = events
        self._wall = wall_clock
        self._timeout = command_timeout_s
        self._flush_settle = flush_settle_s
        self._set_clock = set_clock
        self._decoder = FrameDecoder()
        self._lock = asyncio.Lock()
        self._pending: _Pending | None = None
        self._discarding = False
        self._closed = asyncio.Event()
        self._failure: BaseException | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._sync_wanted = asyncio.Event()
        self._contacts_dirty = asyncio.Event()
        self.info: RadioInfo | None = None
        self.autoadd: codec.AutoAddConfig | None = None
        self.on_contact_key: Callable[[str, bool], None] | None = None  # key, held as contact
        self.contacts_read_at = -1  # command count of the read behind contact_records
        self._commands_sent = 0
        self.contact_records: dict[str, codec.ContactRecord] = {}
        self.channels: dict[int, str] = {}
        self.battery: codec.Battery | None = None

    # --- life cycle -----------------------------------------------------------------------------

    async def start(self, *, fetch_messages: bool = True) -> RadioInfo:
        """Open the transport, resynchronise, identify the radio and fetch its tables.

        `fetch_messages=False` leaves waiting messages in the radio (a probe while the service is
        stopped must not take them away from it).
        """

        await self._transport.open()
        self._tasks.append(asyncio.create_task(self._read_loop(), name="radio-reader"))
        await self._flush()
        self_info = await self._expect(
            codec.app_start(APP_NAME), Response.SELF_INFO, codec.SelfInfo, "app start"
        )
        device = await self._expect(
            codec.device_query(), Response.DEVICE_INFO, codec.DeviceInfo, "device query"
        )
        self.info = RadioInfo(self_info, device)
        log.info(
            "radio %s: %s, firmware %s (code %d), %s",
            self._transport.description, self_info.name, device.version,
            device.firmware_code, device.model,
        )  # fmt: skip
        self.autoadd = await self._read_autoadd()
        _warn_about_contact_adding(self_info, self.autoadd)
        if self._set_clock:
            await self._sync_clock()
        await self.refresh_contacts()
        await self.refresh_channels(device.max_channels)
        self.battery = await self.get_battery()
        if fetch_messages:
            await self.sync_messages(at_connect=True)
            self._tasks.append(asyncio.create_task(self._maintenance(), name="radio-maintenance"))
        return self.info

    async def wait_closed(self) -> None:
        """Return when the session ends; re-raise the error that ended it."""
        await self._closed.wait()
        if self._failure is not None:
            raise self._failure

    async def close(self) -> None:
        self._end(None)
        current = asyncio.current_task()
        others = [task for task in self._tasks if task is not current]
        for task in others:
            task.cancel()
        try:
            # gather, not `await task` in a suppress block: that would also swallow a
            # cancellation of the caller (a shutdown) and keep it running
            await asyncio.gather(*others, return_exceptions=True)
        finally:
            await self._transport.close()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def _end(self, failure: BaseException | None) -> None:
        if not self._closed.is_set():
            self._failure = failure
            self._closed.set()

    # --- commands -------------------------------------------------------------------------------

    @property
    def contacts(self) -> ContactBook:
        return ContactBook(Contact(key, rec.name) for key, rec in self.contact_records.items())

    async def get_time(self) -> int:
        frame = await self._expect(
            codec.get_device_time(), Response.CURR_TIME, codec.CurrentTime, "get time"
        )
        return frame.timestamp

    async def set_time(self, timestamp: int) -> None:
        await self._expect_ok("set time", codec.set_device_time(timestamp))

    async def get_battery(self) -> codec.Battery:
        return await self._expect(
            codec.get_battery(), Response.BATT_AND_STORAGE, codec.Battery, "get battery"
        )

    async def send_text(self, prefix: bytes, text: str, timestamp: int, attempt: int) -> codec.Sent:
        request = codec.send_text(prefix, text, timestamp, attempt)
        return await self._expect(request, Response.SENT, codec.Sent, "send text")

    async def send_channel_text(self, index: int, text: str, timestamp: int) -> None:
        await self._expect_ok("send channel text", codec.send_channel_text(index, text, timestamp))

    async def reset_path(self, public_key: bytes) -> None:
        await self._expect_ok("reset path", codec.reset_path(public_key))

    async def refresh_contacts(self, *, newer_than: int | None = None) -> None:
        """Read the contact table. With `newer_than`, a read that started after that command count
        is enough: one in progress is awaited instead of sending another."""
        if newer_than is not None:
            async with self._lock:  # wait for the command in progress, perhaps such a read
                pass
            if self.contacts_read_at > newer_than:
                return
        records: dict[str, codec.ContactRecord] = {}
        async with self._command(codec.get_contacts(), _CONTACT_CODES) as pending:
            started = self._commands_sent
            while True:
                decoded = self._decode(await self._next(pending), "get contacts")
                if isinstance(decoded, codec.Err):
                    raise CommandFailedError("get contacts", decoded.name)
                if isinstance(decoded, codec.ContactRecord):
                    records[decoded.public_key.hex()] = decoded
                    self._report_key(decoded.public_key)
                elif isinstance(decoded, codec.EndOfContacts):
                    break
        self.contact_records = records
        self.contacts_read_at = started
        log.debug("radio: %d contacts", len(records))

    async def refresh_channels(self, max_channels: int) -> None:
        channels: dict[int, str] = {}
        for index in range(max_channels):
            frame = await self._expect(
                codec.get_channel(index), Response.CHANNEL_INFO, codec.ChannelInfo, "get channel"
            )
            if frame.name:
                channels[index] = frame.name
        self.channels = channels
        log.debug("radio: channels %s", channels)

    async def sync_messages(self, *, at_connect: bool = False) -> int:
        """Fetch queued messages until the radio reports none; returns how many arrived.

        The messages go out as events only after the batch, so one contact read by the bridge
        can serve all of them; also on an error, because the radio has already handed them over.
        `at_connect` marks the batch fetched while connecting: those messages may have arrived
        while no session saw the radio's pushes.
        """
        fetched: list[MessageReceived] = []
        try:
            while True:
                async with self._command(codec.sync_next_message(), _SYNC_CODES) as pending:
                    frame = await self._next(pending)
                    fetched_at = self._commands_sent
                code = frame[0]
                if code == Response.NO_MORE_MESSAGES:
                    return len(fetched)
                if code == Response.ERR:
                    error = codec.Err(frame[1] if len(frame) > 1 else 0)
                    raise CommandFailedError("sync", error.name)
                if code == Response.CHANNEL_DATA_RECV:
                    log.debug("radio: channel data frame ignored")
                    continue
                try:
                    message = codec.decode_message(frame, received_at=self._wall())
                except codec.ProtocolError as exc:
                    log.warning("radio: undecodable message skipped: %s", exc)
                    continue
                fetched.append(MessageReceived(message, self, fetched_at, at_connect))
        finally:
            for event in fetched:
                await self._events.put(event)

    # --- internals ------------------------------------------------------------------------------

    async def _flush(self) -> None:
        """Complete any partial frame in the firmware parser, then drop whatever comes back."""
        self._discarding = True
        try:
            await self._transport.write(bytes(FLUSH_BYTES))
            await asyncio.sleep(self._flush_settle)
        finally:
            self._discarding = False
            self._decoder.reset()

    async def _read_autoadd(self) -> codec.AutoAddConfig | None:
        """The auto-add settings; None when the firmware does not know the command."""
        try:
            return await self._expect(
                codec.get_autoadd_config(), Response.AUTOADD_CONFIG, codec.AutoAddConfig,
                "get auto-add settings",
            )  # fmt: skip
        except CommandFailedError:
            return None

    async def _sync_clock(self) -> None:
        radio_time = await self.get_time()
        now = int(self._wall())
        if now - radio_time > CLOCK_SET_THRESHOLD_S:
            await self.set_time(now)
            log.info("radio clock moved forward by %d s", now - radio_time)
        elif radio_time - now > CLOCK_AHEAD_WARNING_S:
            log.warning(
                "radio clock is %d s ahead and cannot be set back; command age checks may fail",
                radio_time - now,
            )

    async def _maintenance(self) -> None:
        """Refresh contacts after adverts, then sync on MSG_WAITING; the battery is the heartbeat.

        Contacts come first, so a message from a contact added a moment ago finds it.
        """
        loop = asyncio.get_running_loop()
        next_sync = loop.time() + SYNC_INTERVAL_S
        next_beat = loop.time() + HEARTBEAT_INTERVAL_S
        try:
            while not self.closed:
                timeout = max(0.0, min(next_sync, next_beat) - loop.time())
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(timeout):  # not wait_for: cancellation on 3.11
                        await self._wanted()
                if self._contacts_dirty.is_set():
                    if not self._sync_wanted.is_set():
                        await asyncio.sleep(CONTACTS_REFRESH_DELAY_S)  # adverts come in bursts
                    self._contacts_dirty.clear()
                    await self.refresh_contacts()
                if self._sync_wanted.is_set() or loop.time() >= next_sync:
                    self._sync_wanted.clear()
                    await self.sync_messages()
                    next_sync = loop.time() + SYNC_INTERVAL_S
                if loop.time() >= next_beat:
                    self.battery = await self.get_battery()
                    next_beat = loop.time() + HEARTBEAT_INTERVAL_S
        except RadioError as exc:
            self._end(exc)
        except Exception as exc:  # a bug must not leave a session that looks alive
            log.exception("radio: maintenance failed")
            self._end(RadioError(f"maintenance failed: {exc!r}"))

    async def _wanted(self) -> None:
        sync = asyncio.create_task(self._sync_wanted.wait())
        contacts = asyncio.create_task(self._contacts_dirty.wait())
        try:
            await asyncio.wait({sync, contacts}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sync.cancel()
            contacts.cancel()

    async def _read_loop(self) -> None:
        try:
            while True:
                data = await self._transport.read()
                for frame in self._decoder.feed(data):
                    if not self._discarding:
                        await self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure ends the session
            self._end(exc if isinstance(exc, RadioError) else RadioError(str(exc) or repr(exc)))

    async def _dispatch(self, frame: bytes) -> None:
        code = frame[0]
        if code >= 0x80:
            await self._push(frame)
        elif self._pending is not None and code in self._pending.expected:
            self._pending.frames.put_nowait(frame)
        else:
            log.debug("radio: unexpected frame 0x%02x dropped", code)

    async def _push(self, frame: bytes) -> None:
        try:
            decoded = codec.decode(frame)
        except codec.ProtocolError as exc:
            log.warning("radio: malformed push ignored: %s", exc)
            return
        if isinstance(decoded, codec.MessageWaiting):
            self._sync_wanted.set()
        elif isinstance(decoded, codec.SendConfirmed):
            await self._events.put(AckReceived(decoded.ack, decoded.round_trip_ms))
        elif isinstance(decoded, codec.RxLog):
            await self._events.put(PacketLogged(decoded, self._wall()))
        elif isinstance(decoded, codec.Advert):
            self._report_key(decoded.public_key, held=not decoded.new)  # before later frames
            self._contacts_dirty.set()
        elif isinstance(decoded, codec.PathUpdated | codec.ContactDeleted):
            self._report_key(decoded.public_key)
            self._contacts_dirty.set()
        elif frame[0] == Push.CONTACTS_FULL:
            self._contacts_dirty.set()
        else:
            log.debug("radio: push 0x%02x ignored", frame[0])

    def _report_key(self, public_key: bytes, *, held: bool = True) -> None:
        if self.on_contact_key is not None:
            self.on_contact_key(public_key.hex(), held)

    @contextlib.asynccontextmanager
    async def _command(self, request: bytes, expected: Iterable[int]) -> AsyncIterator[_Pending]:
        """Send one command; the yielded queue receives the frames with the expected codes."""
        if self.closed:
            raise RadioError("session closed")
        async with self._lock:
            pending = _Pending(frozenset(expected) | {Response.ERR})
            self._pending = pending
            try:
                try:
                    await self._transport.write(encode_frame(request))
                except (ConnectionError, OSError) as exc:
                    error = RadioError(f"write failed: {exc}")
                    self._end(error)
                    raise error from exc
                self._commands_sent += 1
                yield pending
            finally:
                self._pending = None

    async def _next(self, pending: _Pending) -> bytes:
        closed = asyncio.create_task(self._closed.wait())
        frame = asyncio.create_task(pending.frames.get())
        try:
            done, _ = await asyncio.wait(
                {closed, frame}, timeout=self._timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            closed.cancel()
            if not frame.done():  # also when the caller is cancelled: no task left behind
                frame.cancel()
        if frame in done:
            return frame.result()
        if self._failure is not None:
            raise RadioError(f"connection lost: {self._failure}")
        error = RadioError("no answer from the radio")
        self._end(error)
        raise error

    def _decode(self, frame: bytes, name: str) -> codec.Decoded:
        """Decode an answer; a malformed one ends the session (the bridge resynchronises)."""
        try:
            return codec.decode(frame)
        except codec.ProtocolError as exc:
            error = RadioError(
                f"{name}: malformed answer from the radio ({exc}); unsupported firmware?"
            )
            self._end(error)
            raise error from exc

    async def _expect(self, request: bytes, code: int, kind: type[T], name: str) -> T:
        async with self._command(request, {code}) as pending:
            decoded = self._decode(await self._next(pending), name)
        if isinstance(decoded, codec.Err):
            raise CommandFailedError(name, decoded.name)
        if not isinstance(decoded, kind):
            error = RadioError(f"{name}: unexpected answer {type(decoded).__name__}")
            self._end(error)  # out of step with the radio: reconnect and resynchronise
            raise error
        return decoded

    async def _expect_ok(self, name: str, request: bytes) -> None:
        async with self._command(request, _SIMPLE) as pending:
            decoded = self._decode(await self._next(pending), name)
        if isinstance(decoded, codec.Err):
            raise CommandFailedError(name, decoded.name)
        if isinstance(decoded, codec.Disabled):
            raise CommandFailedError(name, "disabled")


_CONTACT_CODES = frozenset({Response.CONTACTS_START, Response.CONTACT, Response.END_OF_CONTACTS})
_SYNC_CODES = codec.MESSAGE_CODES | {Response.NO_MORE_MESSAGES}


AUTO_ADD_TYPES = {
    AutoAdd.CHAT: "chat",
    AutoAdd.REPEATER: "repeater",
    AutoAdd.ROOM_SERVER: "room",
    AutoAdd.SENSOR: "sensor",
}
_EVERY_TYPE = AutoAdd.CHAT | AutoAdd.REPEATER | AutoAdd.ROOM_SERVER | AutoAdd.SENSOR


def added_types(self_info: codec.SelfInfo, autoadd: codec.AutoAddConfig | None) -> AutoAdd:
    """Contact types the radio adds on its own (MyMesh.cpp:308-332).

    Every type without manual adding; with it, the types enabled in the auto-add settings, none
    on firmware without those settings. Bits of types this code does not know count as well.
    """
    if not self_info.manual_add_contacts:
        return _EVERY_TYPE
    if autoadd is None:
        return AutoAdd(0)
    # int arithmetic: inverting the flag itself would also clear the unknown bits
    return AutoAdd(autoadd.flags & 0xFF & ~int(AutoAdd.OVERWRITE_OLDEST))


def type_names(types: AutoAdd) -> list[str]:
    """Names of the contact types; bits of types this code does not know as one hex value."""
    names = [name for flag, name in AUTO_ADD_TYPES.items() if types & flag]
    unknown = int(types) & ~int(_EVERY_TYPE)
    return [*names, f"0x{unknown:02x}"] if unknown else names


def _warn_about_contact_adding(
    self_info: codec.SelfInfo, autoadd: codec.AutoAddConfig | None
) -> None:
    """Automatic adding lets a look-alike of an allowed command sender into the table, and the
    bridge then rejects that sender's commands as ambiguous (ADR 0009). The radio passes on
    direct messages from contacts of every type, so every type counts."""
    if not self_info.manual_add_contacts:
        log.warning(
            "radio adds every new contact automatically; a node with the key prefix of a command "
            "sender would block that sender's commands: switch on manual adding and switch off "
            "every type in the auto-add settings (ADR 0009)"
        )
    elif types := added_types(self_info, autoadd):
        log.warning(
            "radio adds new contacts of type %s automatically despite manual adding; a node with "
            "the key prefix of a command sender would block that sender's commands: switch these "
            "types off in the auto-add settings (ADR 0009)",
            ", ".join(type_names(types)),
        )
