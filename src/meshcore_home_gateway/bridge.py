"""The bridge service: connects the radio session and the MQTT bus (design §5 and §6).

Tasks on one event loop:
* radio supervisor: connect, run the session, reconnect with exponential back-off;
* request reader: MQTT send requests -> validation, deduplication, queue (or rejection);
* outbox: one message at a time: wait for radio and airtime, send, wait for the acknowledgement,
  retry, flood, report every step on the delivery topic;
* radio events: acknowledgements and packet log at once, received messages in order
  (deduplicated; command policy with a fresh contact table and a memory of look-alikes);
* status: retained state document, systemd watchdog and status.
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from meshcore_home_gateway import __version__
from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core.airtime import (
    LoRaParams,
    channel_message_bytes,
    direct_attempt_airtime_s,
    meshcore_preamble,
    time_on_air_s,
)
from meshcore_home_gateway.core.budget import AirtimeBudget, Deduplicator
from meshcore_home_gateway.core.commands import (
    CommandDecision,
    Rejection,
    command_word,
    evaluate_command,
)
from meshcore_home_gateway.core.contacts import PREFIX_HEX_DIGITS, Contact
from meshcore_home_gateway.core.model import (
    DeliveryEvent,
    DeliveryState,
    InboundMessage,
    MessageKind,
    Target,
)
from meshcore_home_gateway.core.text import TextPolicy, TextRejectedError, prepare_text
from meshcore_home_gateway.mqtt.client import SendRequest
from meshcore_home_gateway.mqtt.discovery import discovery_payload, discovery_topic
from meshcore_home_gateway.mqtt.payloads import (
    command_payload,
    delivery_payload,
    encode,
    received_payload,
    rx_payload,
)
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.protocol.codec import MAX_TEXT_BYTES
from meshcore_home_gateway.radio.session import (
    AckReceived,
    CommandFailedError,
    CompanionSession,
    MessageReceived,
    PacketLogged,
    RadioError,
    RadioEvent,
    RadioInfo,
)
from meshcore_home_gateway.radio.transport import Transport
from meshcore_home_gateway.state import CommandLedger
from meshcore_home_gateway.systemd import notify, watchdog_interval_s

log = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 1024
MAX_NAME_BYTES = 31  # longest radio name; before the radio is known, channel texts assume it
RECONNECT_MIN_S, RECONNECT_MAX_S = 1.0, 60.0
STATUS_INTERVAL_S = 60.0
ACK_TIMEOUT_FACTOR = 1.2  # like the MeshCore Python client
ACK_TIMEOUT_MIN_S, ACK_TIMEOUT_MAX_S = 5.0, 60.0
RECENT_ACK_S = 120.0
INBOUND_DEDUP_S = 3600.0  # retries reach us as duplicates with the same timestamp and text
LOOK_ALIKE_MEMORY_S = 86_400.0  # a key with a sender's prefix counts this long after last seen
_FINAL_FROM_CACHE = frozenset(
    {Rejection.SENDER_AMBIGUOUS, Rejection.SENDER_NOT_ALLOWED, Rejection.REPLAY,
     Rejection.TOO_OLD, Rejection.FROM_THE_FUTURE}
)  # fmt: skip
_TRANSIENT = frozenset(
    {Rejection.SENDER_UNVERIFIED, Rejection.STATE_UNWRITABLE, Rejection.BROKER_UNAVAILABLE}
)
DISCOVERY_DELAY_S = (0.5, 2.0)  # random delay after Home Assistant's birth message


class _FinalError(Exception):
    """Ends a delivery with this state and reason."""

    def __init__(self, state: DeliveryState, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason


class Bus(Protocol):
    """What the bridge needs from the message bus (implemented by mqtt.client.MqttBus)."""

    requests: asyncio.Queue[SendRequest]
    connected: bool
    on_connected: list[Callable[[], None]]
    on_ha_online: list[Callable[[], None]]

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def publish(self, topic: str, payload: bytes, retain: bool = False) -> None: ...


SessionFactory = Callable[[Transport, "asyncio.Queue[RadioEvent]", bool], CompanionSession]


def default_session_factory(
    transport: Transport, events: asyncio.Queue[RadioEvent], set_clock: bool
) -> CompanionSession:
    return CompanionSession(transport, events, set_clock=set_clock)


@dataclass
class Outgoing:
    id: str
    target: Target
    text: str
    created: float  # monotonic


class AckWaiter:
    """Matches SEND_CONFIRMED pushes to waiting messages; keeps late ones for a while."""

    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._now = monotonic
        self._waiting: dict[bytes, asyncio.Future[int]] = {}
        self._recent: dict[bytes, tuple[float, int]] = {}

    def resolve(self, ack: bytes, round_trip_ms: int) -> None:
        future = self._waiting.pop(ack, None)
        if future is not None and not future.done():
            future.set_result(round_trip_ms)
            return
        self._recent[ack] = (self._now(), round_trip_ms)
        cutoff = self._now() - RECENT_ACK_S
        self._recent = {k: v for k, v in self._recent.items() if v[0] >= cutoff}

    def take(self, codes: list[bytes]) -> int | None:
        """The round trip of an acknowledgement of one of `codes` that has already arrived."""
        for code in codes:
            if code in self._recent:
                return self._recent.pop(code)[1]
        return None

    async def wait(self, codes: list[bytes], timeout_s: float) -> int | None:
        if (round_trip := self.take(codes)) is not None:
            return round_trip
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        for code in codes:
            self._waiting[code] = future
        try:
            async with asyncio.timeout(timeout_s):  # not wait_for: see _radio_until
                return await future
        except TimeoutError:
            return None
        finally:
            for code in codes:
                if self._waiting.get(code) is future:
                    del self._waiting[code]


class Bridge:
    def __init__(
        self,
        config: Config,
        bus: Bus,
        transport_factory: Callable[[], Transport],
        ledger: CommandLedger,
        *,
        session_factory: SessionFactory = default_session_factory,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        notifier: Callable[[str], bool] = notify,
    ) -> None:
        self.config = config
        self._bus = bus
        self._transport_factory = transport_factory
        self._ledger = ledger
        self._session_factory = session_factory
        self._wall = wall_clock
        self._mono = monotonic
        self._notify = notifier
        self.topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
        self._targets = {t.alias: t for t in config.targets}
        self._events: asyncio.Queue[RadioEvent] = asyncio.Queue()
        self._messages: asyncio.Queue[MessageReceived] = asyncio.Queue()
        # keys with the 6-byte prefix of an allowed command sender, as the radio named them:
        # key -> when last seen; only these are kept, so the memory stays small (ADR 0009)
        self._watched = frozenset(key[:PREFIX_HEX_DIGITS] for key in config.commands.senders)
        self._seen: dict[str, float] = {}
        self._queue: asyncio.Queue[Outgoing] = asyncio.Queue(maxsize=config.messages.queue_size)
        self._budget = AirtimeBudget(
            config.airtime.budget_s_per_hour, config.airtime.burst_s, now=monotonic()
        )
        self._dedup = Deduplicator(config.messages.dedup_window_s)
        self._inbound_dedup = Deduplicator(INBOUND_DEDUP_S)
        self._acks = AckWaiter(monotonic)
        self._session: CompanionSession | None = None
        self._radio_info: RadioInfo | None = None
        self._radio_ready = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    # --- life cycle -----------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        self._bus.on_connected.append(self._publish_announcements)
        self._bus.on_ha_online.append(self._schedule_announcements)
        await self._bus.start()
        self._notify("READY=1")
        try:
            async with asyncio.TaskGroup() as group:
                for name, coroutine in (
                    ("radio", self._radio_loop()),
                    ("requests", self._request_loop()),
                    ("outbox", self._outbox_loop()),
                    ("events", self._event_loop()),
                    ("messages", self._message_loop()),
                    ("status", self._status_loop()),
                ):
                    self._tasks.append(group.create_task(coroutine, name=name))
                await stop.wait()
                self._notify("STOPPING=1")
                for task in self._tasks:
                    task.cancel()
        finally:
            if self._session is not None:
                await self._session.close()
            self._radio_ready.clear()
            self._publish_state()
            await self._bus.stop()

    # --- radio ----------------------------------------------------------------------------------

    async def _radio_loop(self) -> None:
        delay = RECONNECT_MIN_S
        while True:
            session = self._session_factory(
                self._transport_factory(), self._events, self.config.radio.set_clock
            )
            session.on_contact_key = self._contact_key
            try:
                self._radio_info = await session.start()
            except (RadioError, OSError) as exc:
                log.warning("radio: cannot connect (%s); retry in %.0f s", exc, delay)
            except Exception:  # a bug while connecting: keep the service up, retry later
                log.exception("radio: cannot connect; retry in %.0f s", delay)
            else:
                self._session = session
                self._radio_ready.set()
                delay = RECONNECT_MIN_S
                self._check_radio(session)
                self._publish_state()
                try:
                    await session.wait_closed()
                except RadioError as exc:
                    log.warning("radio: connection lost (%s)", exc)
                finally:
                    self._radio_ready.clear()
                    self._session = None
                    self._publish_state()
            finally:
                await session.close()
            await asyncio.sleep(delay * random.uniform(1.0, 1.2))  # noqa: S311 - jitter only
            delay = min(delay * 2, RECONNECT_MAX_S)

    def _check_radio(self, session: CompanionSession) -> None:
        """Warn about targets the radio does not know, a burst that is too small, and commands
        over TCP."""
        if self.config.radio.transport == "tcp" and self.config.commands.senders:
            log.warning(
                "commands over TCP: the radio drops push frames when its send queue is full, so "
                "the bridge can miss a look-alike of a command sender; prefer USB (ADR 0009)"
            )
        info = session.info
        if info is not None:
            longest = direct_attempt_airtime_s(MAX_TEXT_BYTES, 0, True, _lora(info))
            if longest > self._budget.capacity_s:
                log.warning(
                    "a long direct message takes %.1f s of airtime at the radio's settings, more "
                    "than airtime.burst_s (%.1f s): such messages fail (exceeds_airtime_burst)",
                    longest, self._budget.capacity_s,
                )  # fmt: skip
        for target in self._targets.values():
            if target.kind is MessageKind.CHANNEL and target.channel_index not in session.channels:
                log.warning("target %s: channel %s is not configured on the radio",
                            target.alias, target.channel_index)  # fmt: skip
            if target.kind is MessageKind.CONTACT and target.public_key is not None:
                matches = session.contacts.resolve(target.public_key[:PREFIX_HEX_DIGITS])
                if [c.public_key for c in matches] != [target.public_key]:
                    log.warning("target %s: contact missing on the radio or prefix ambiguous",
                                target.alias)  # fmt: skip

    async def _radio_until(self, deadline: float) -> CompanionSession | None:
        """The connected session, waiting for it until the deadline; never a closed one."""
        while True:
            remaining = deadline - self._mono()
            if remaining <= 0:
                return None
            try:
                # asyncio.timeout, not wait_for: on Python 3.11 wait_for can swallow a
                # cancellation that arrives as the awaited event fires, and shutdown then hangs
                async with asyncio.timeout(remaining):
                    await self._radio_ready.wait()
            except TimeoutError:
                return None
            session = self._session
            if session is not None and not session.closed:
                return session
            await asyncio.sleep(0.05)  # the radio loop is replacing a closed session

    # --- requests -------------------------------------------------------------------------------

    async def _request_loop(self) -> None:
        while True:
            self._accept(await self._bus.requests.get())

    def _accept(self, request: SendRequest) -> None:
        event_id = secrets.token_hex(4)
        try:
            target, text = self._validate(request)
        except _FinalError as exc:
            event = DeliveryEvent(event_id, request.alias, exc.state, reason=exc.reason)
            self._deliver_event(event)
            return
        state, reason = DeliveryState.QUEUED, None
        key = (target.alias, text)
        if self._dedup.is_duplicate(key, self._mono()):
            state = DeliveryState.DUPLICATE
        else:
            try:
                self._queue.put_nowait(Outgoing(event_id, target, text, self._mono()))
            except asyncio.QueueFull:
                self._dedup.forget(key)  # not taken, so a retry must not count as duplicate
                state, reason = DeliveryState.REJECTED, "queue_full"
        self._deliver_event(DeliveryEvent(event_id, target.alias, state, text=text, reason=reason))

    def _validate(self, request: SendRequest) -> tuple[Target, str]:
        if request.retained:
            raise _FinalError(DeliveryState.REJECTED, "retained")
        if len(request.payload) > MAX_REQUEST_BYTES:
            raise _FinalError(DeliveryState.REJECTED, "too_large")
        try:
            raw = request.payload.decode("utf-8")
        except UnicodeDecodeError:
            raise _FinalError(DeliveryState.REJECTED, "invalid_utf8") from None
        target = self._targets.get(request.alias)
        if target is None:
            raise _FinalError(DeliveryState.REJECTED, "unknown_target")
        try:
            return target, prepare_text(raw, self._text_policy(target))
        except TextRejectedError as exc:
            raise _FinalError(DeliveryState.REJECTED, exc.reason) from None

    def _text_policy(self, target: Target) -> TextPolicy:
        limit = MAX_TEXT_BYTES
        if target.kind is MessageKind.CHANNEL:
            name = len(self._radio_info.name.encode()) if self._radio_info else MAX_NAME_BYTES
            limit = MAX_TEXT_BYTES - name - 2  # the firmware prepends "<name>: "
        messages = self.config.messages
        return TextPolicy(limit, ascii_only=messages.ascii_only, truncate=messages.truncate)

    # --- outbox ---------------------------------------------------------------------------------

    async def _outbox_loop(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._deliver(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("delivery %s failed unexpectedly", item.id)
                failed = self._event(item, DeliveryState.FAILED, reason="internal_error")
                self._deliver_event(failed)

    async def _deliver(self, item: Outgoing) -> None:
        """Attempts until the message is confirmed, ends with a reason, or runs out of time.

        `expired` means the message never left: once an attempt reached the radio, running out
        of time or airtime ends it as `failed` (no acknowledgement). An attempt interrupted by a
        radio error is repeated, with the same number, when the radio is back. `attempt` in a
        final event counts the transmissions so far.
        """
        messages = self.config.messages
        deadline = item.created + messages.queue_ttl_s
        timestamp = int(self._wall())  # one timestamp for all attempts: retries stay recognisable
        direct = item.target.kind is MessageKind.CONTACT
        attempts = messages.max_attempts if direct else 1
        acks: list[bytes] = []
        sent = 0
        while sent < attempts:
            session = await self._radio_until(deadline)
            if session is None:
                self._deliver_event(self._out_of_time(item, sent))
                return
            try:
                outcome = await self._attempt(
                    session, item, attempt=sent, timestamp=timestamp, deadline=deadline, acks=acks
                )
            except _FinalError as exc:
                outcome = self._final(item, exc.state, exc.reason, sent)
            except CommandFailedError as exc:
                outcome = self._final(item, DeliveryState.FAILED, exc.reason, sent)
            except RadioError as exc:
                log.info("delivery %s: attempt %d interrupted (%s)", item.id, sent + 1, exc)
                continue
            if outcome is not None:
                self._deliver_event(outcome)
                return
            sent += 1
        failed = self._event(item, DeliveryState.FAILED, attempt=sent, reason="no_acknowledgement")
        self._deliver_event(failed)

    def _final(self, item: Outgoing, state: DeliveryState, reason: str, sent: int) -> DeliveryEvent:
        if state is DeliveryState.EXPIRED and sent:  # it left before: it was not acknowledged
            state, reason = DeliveryState.FAILED, "no_acknowledgement"
        return self._event(item, state, attempt=sent, reason=reason)

    def _out_of_time(self, item: Outgoing, sent: int) -> DeliveryEvent:
        if sent:
            return self._event(
                item, DeliveryState.FAILED, attempt=sent, reason="no_acknowledgement"
            )
        reason = "queue_ttl" if self._radio_ready.is_set() else "no_radio"
        return self._event(item, DeliveryState.EXPIRED, reason=reason)

    async def _attempt(
        self, session: CompanionSession, item: Outgoing, *, attempt: int, timestamp: int,
        deadline: float, acks: list[bytes],
    ) -> DeliveryEvent | None:  # fmt: skip
        """One transmission. Returns the final event, or None when another attempt should follow."""
        info = session.info
        if info is None:
            raise RadioError("radio not identified")
        target, text = item.target, item.text
        airtime, reset_path = self._plan(session, item, attempt, info)
        if airtime > self._budget.capacity_s:
            raise _FinalError(DeliveryState.FAILED, "exceeds_airtime_burst")
        wait = self._budget.wait_s(airtime, self._mono())
        if self._mono() + wait > deadline:
            raise _FinalError(DeliveryState.EXPIRED, "airtime_budget")
        round_trip = None
        if wait > 0:
            log.info("delivery %s waits %.1f s for airtime", item.id, wait)
            if acks:  # an earlier attempt may still be acknowledged meanwhile
                round_trip = await self._acks.wait(acks, wait)
            else:
                await asyncio.sleep(wait)
        if acks and round_trip is None:
            round_trip = self._acks.take(acks)
        if round_trip is not None:  # an earlier attempt arrived after all: send nothing more
            return self._event(item, DeliveryState.CONFIRMED, attempt=attempt, rtt_ms=round_trip)
        if reset_path is not None:
            await session.reset_path(reset_path)  # only now: this attempt will be sent and flood
        self._budget.consume(airtime, self._mono())
        if target.channel_index is not None:
            await session.send_channel_text(target.channel_index, text, timestamp)
            return self._event(item, DeliveryState.SENT, attempt=1, airtime_s=airtime)
        prefix = bytes.fromhex(_key(target)[:PREFIX_HEX_DIGITS])
        sent = await session.send_text(prefix, text, timestamp, attempt)
        sent_event = self._event(item, DeliveryState.SENT, attempt=attempt + 1, airtime_s=airtime)
        self._deliver_event(sent_event)
        acks.append(sent.expected_ack)
        timeout = sent.timeout_ms / 1000 * ACK_TIMEOUT_FACTOR
        timeout = min(max(timeout, ACK_TIMEOUT_MIN_S), ACK_TIMEOUT_MAX_S)
        round_trip = await self._acks.wait(acks, timeout)
        if round_trip is None:
            return None
        return self._event(item, DeliveryState.CONFIRMED, attempt=attempt + 1, rtt_ms=round_trip)

    def _plan(
        self, session: CompanionSession, item: Outgoing, attempt: int, info: RadioInfo
    ) -> tuple[float, bytes | None]:
        """Check the target on the radio and estimate the airtime; also the key whose stored
        path must be reset before this attempt, so that it floods."""
        target, size = item.target, len(item.text.encode())
        if target.channel_index is not None:
            if target.channel_index not in session.channels:
                raise _FinalError(DeliveryState.FAILED, "channel_not_configured")
            name_bytes = len(info.name.encode())
            if size > MAX_TEXT_BYTES - name_bytes - 2:  # the radio was renamed after queueing
                raise _FinalError(DeliveryState.FAILED, "too_long")
            return time_on_air_s(channel_message_bytes(size, name_bytes), _lora(info)), None
        contact = self._resolve(session, target)
        record = session.contact_records[contact.public_key]
        known_flood = record.out_path_len == 0xFF
        flood_after = self.config.messages.flood_after
        reset = None
        if attempt == flood_after and not known_flood:
            reset = bytes.fromhex(contact.public_key)
        # from the reset on, assume a flood: the cached record does not show the reset
        flood = known_flood or attempt >= flood_after
        path_bytes = 0 if flood else _path_bytes(record.out_path_len)
        return direct_attempt_airtime_s(size, path_bytes, flood, _lora(info)), reset

    def _resolve(self, session: CompanionSession, target: Target) -> Contact:
        key = _key(target)
        matches = session.contacts.resolve(key[:PREFIX_HEX_DIGITS])
        if len(matches) > 1:
            raise _FinalError(DeliveryState.FAILED, "ambiguous_target")
        if not matches or matches[0].public_key != key:
            raise _FinalError(DeliveryState.FAILED, "unknown_contact")
        return matches[0]

    def _event(
        self, item: Outgoing, state: DeliveryState, *, attempt: int = 0, reason: str | None = None,
        rtt_ms: int | None = None, airtime_s: float | None = None,
    ) -> DeliveryEvent:  # fmt: skip
        return DeliveryEvent(
            item.id, item.target.alias, state, text=item.text, attempt=attempt, reason=reason,
            rtt_ms=rtt_ms, airtime_ms=None if airtime_s is None else round(airtime_s * 1000),
        )  # fmt: skip

    def _deliver_event(self, event: DeliveryEvent) -> None:
        log.info("delivery %s to %s: %s%s", event.id, event.target, event.state.value,
                 f" ({event.reason})" if event.reason else "")  # fmt: skip
        self._bus.publish(self.topics.delivery, encode(delivery_payload(event)))

    # --- radio events ---------------------------------------------------------------------------

    async def _event_loop(self) -> None:
        while True:
            event = await self._events.get()
            if isinstance(event, MessageReceived):
                self._messages.put_nowait(event)  # in order, without delaying acks
            elif isinstance(event, AckReceived):
                self._acks.resolve(event.ack, event.round_trip_ms)
            elif isinstance(event, PacketLogged) and self.config.diagnostics.publish_rx_log:
                rx = event.rx
                payload = rx_payload(rx.snr_db, rx.rssi_dbm, rx.raw, event.received_at)
                self._bus.publish(self.topics.rx, encode(payload))

    async def _message_loop(self) -> None:
        while True:
            event = await self._messages.get()
            try:
                await self._on_message(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("received message could not be handled")

    async def _on_message(self, event: MessageReceived) -> None:
        message, session = event.message, event.session
        if (
            message.kind is MessageKind.CHANNEL
            and message.channel_index in self.config.messages.ignore_channels
        ):
            return
        origin = (
            message.sender_prefix if message.kind is MessageKind.CONTACT else message.channel_index
        )
        key = (message.kind, origin, message.sender_timestamp, message.text)
        if self._inbound_dedup.is_duplicate(key, self._mono()):
            log.debug("duplicate of a received message dropped (sender retry)")
            return
        decision = await self._judge(event)
        command = decision.command if decision else None
        if decision is not None and command is not None and not self._bus.connected:
            # queued until the reconnect, it would trigger automations at any later time
            log.warning("command %s refused: the broker is unreachable", decision.word)
            command = None
            decision = CommandDecision(decision.word, rejection=Rejection.BROKER_UNAVAILABLE)
        if decision is not None and command is not None:
            try:
                self._ledger.record(command.sender_key, command.sender_timestamp)  # first
            except OSError:
                log.exception("command not published: the state file cannot be written")
                command = None
                decision = CommandDecision(decision.word, rejection=Rejection.STATE_UNWRITABLE)
        if decision is not None and decision.rejection in _TRANSIENT:
            self._inbound_dedup.forget(key)  # the sender's retry may get through
        sender = None
        if message.sender_prefix is not None:
            matches = session.contacts.resolve(message.sender_prefix)
            sender = matches[0] if len(matches) == 1 else None
        target = self._target_for(message, sender)
        log.info("received %s message from %s (%d bytes)%s", message.kind.value,
                 sender.name if sender else message.sender_prefix or message.channel_index,
                 len(message.text.encode()),
                 f", command {decision.word}: {'accepted' if command else decision.rejection}"
                 if decision else "")  # fmt: skip
        log.debug("text: %r", message.text)
        payload = received_payload(message, sender, target, decision)
        self._bus.publish(self.topics.received, encode(payload))
        if command is not None:
            name = sender.name if sender else None
            self._bus.publish(
                self.topics.command, encode(command_payload(command, name, message.received_at))
            )

    async def _judge(self, event: MessageReceived) -> CommandDecision | None:
        """The command verdict; None if the message is no command at all (ADR 0009).

        Messages carry only a 6-byte sender prefix. The sender must be the only contact with that
        prefix in the table of the session that fetched the message, and no other key with it may
        have been a contact of the radio within LOOK_ALIKE_MEMORY_S. A rejection by the cached
        table stands, unless the sender was unknown: a removal is remembered, so nothing the radio
        does turns it into an acceptance. A command fetched at connect is refused: a look-alike
        may have come and gone while nobody listened. Before an acceptance (or for an unknown
        sender) the table is read again, unless a read started after the message was fetched;
        that read then serves a whole batch.
        """
        message, session = event.message, event.session
        word = command_word(message, self.config.commands)
        if word is None:
            return None
        decision = self._decide(message, session)
        if decision is not None and decision.rejection in _FINAL_FROM_CACHE:
            return decision
        if event.at_connect:
            log.warning(
                "command %s was fetched while the bridge connected (it waited in the radio or "
                "arrived meanwhile) and is rejected: the sender has to send it again (ADR 0009)",
                word,
            )
            return CommandDecision(word, rejection=Rejection.SENDER_UNVERIFIED)
        try:
            await session.refresh_contacts(newer_than=event.fetched_at)
        except RadioError as exc:
            log.warning("command sender cannot be verified: %s", exc)
            return CommandDecision(word, rejection=Rejection.SENDER_UNVERIFIED)
        return self._decide(message, session)

    def _decide(self, message: InboundMessage, session: CompanionSession) -> CommandDecision | None:
        now = self._mono()
        self._seen = {k: at for k, at in self._seen.items() if now - at < LOOK_ALIKE_MEMORY_S}
        return evaluate_command(
            message, self.config.commands, session.contacts, self._ledger.view, self._wall(),
            seen=self._seen,
        )  # fmt: skip

    def _contact_key(self, key: str, held: bool) -> None:
        """Every key the radio names, from a table read or a push; keep those with a sender's
        prefix. A node the radio did not add (held False) cannot send it anything it decrypts, so
        its key only renews one already remembered: a look-alike that merely advertises does not
        block the sender."""
        if key[:PREFIX_HEX_DIGITS] not in self._watched:
            return
        now, last = self._mono(), self._seen.get(key)
        if held or (last is not None and now - last < LOOK_ALIKE_MEMORY_S):
            self._seen[key] = now

    def _target_for(self, message: InboundMessage, sender: Contact | None) -> str | None:
        for target in self._targets.values():
            if (
                message.kind is MessageKind.CHANNEL
                and target.channel_index == message.channel_index
            ):
                return target.alias
            if sender is not None and target.public_key == sender.public_key:
                return target.alias
        return None

    # --- status ---------------------------------------------------------------------------------

    async def _status_loop(self) -> None:
        watchdog = watchdog_interval_s()
        tick = min(STATUS_INTERVAL_S, watchdog / 2) if watchdog else STATUS_INTERVAL_S
        next_state = self._mono() + STATUS_INTERVAL_S
        while True:
            if watchdog:
                self._notify("WATCHDOG=1")
            if self._mono() >= next_state:
                self._publish_state()
                next_state = self._mono() + STATUS_INTERVAL_S
            await asyncio.sleep(tick)

    def state_payload(self) -> dict[str, Any]:
        info = self._radio_info
        connected = self._radio_ready.is_set()
        battery = self._session.battery if (self._session and connected) else None
        now = self._mono()
        return {
            "radio": "connected" if connected else "disconnected",
            "battery_v": round(battery.millivolts / 1000, 3) if battery else None,
            "airtime_1h_s": round(self._budget.used_s(now), 3),
            "airtime_available_s": round(self._budget.available_s(now), 3),
            "airtime_budget_s_per_hour": self._budget.per_hour_s,
            "queue": self._queue.qsize(),
            "radio_name": info.name if info else None,
            "public_key": info.public_key if info else None,
            "model": info.device.model if info else None,
            "firmware": info.device.version if info else None,
            "frequency_mhz": info.self_info.frequency_khz / 1000 if info else None,
            "bandwidth_khz": info.self_info.bandwidth_hz / 1000 if info else None,
            "spreading_factor": info.self_info.spreading_factor if info else None,
            "coding_rate": info.self_info.coding_rate if info else None,
            "tx_power_dbm": info.self_info.tx_power_dbm if info else None,
            "version": __version__,
        }

    def _publish_state(self) -> None:
        state = self.state_payload()
        self._bus.publish(self.topics.state, encode(state), retain=True)
        self._notify(f"STATUS=radio {state['radio']}, queue {state['queue']}")

    def _publish_announcements(self) -> None:
        if self.config.homeassistant.discovery:
            document = discovery_payload(self.config, self.topics)
            self._bus.publish(discovery_topic(self.config), encode(document))
        self._publish_state()

    def _schedule_announcements(self) -> None:
        delay = random.uniform(*DISCOVERY_DELAY_S)  # noqa: S311 - spreading load only
        asyncio.get_running_loop().call_later(delay, self._publish_announcements)


def _lora(info: RadioInfo) -> LoRaParams:
    sf = info.self_info.spreading_factor
    return LoRaParams(
        bandwidth_hz=float(info.self_info.bandwidth_hz),
        spreading_factor=sf,
        coding_rate=info.self_info.coding_rate,
        preamble_symbols=meshcore_preamble(sf),
    )


def _key(target: Target) -> str:
    if target.public_key is None:
        raise ValueError(f"target {target.alias} has no public key")
    return target.public_key


def _path_bytes(out_path_len: int) -> int:
    """Stored path size: bits 0-5 hop count, bits 6-7 hash size minus one."""
    return (out_path_len & 0x3F) * ((out_path_len >> 6) + 1)
