"""The bridge service: connects the radio session and the MQTT bus (design §5 and §6).

Tasks on one event loop:
* radio supervisor: connect, run the session, reconnect with exponential back-off;
* request reader: MQTT send requests -> validation, deduplication, queue (or rejection);
* outbox: one message at a time: wait for radio and airtime, send, wait for the acknowledgement,
  retry, flood, report every step on the delivery topic;
* radio events: acknowledgements and packet log at once, received messages in order
  (deduplicated; command policy with a fresh contact table and a memory of look-alikes);
* status: retained state document, systemd watchdog and status; an exclusion from telemetry
  that could not be stored is written again;
* telemetry, if enabled: asks the radio's companions one at a time, after the messages
  (telemetry_poller.py, ADR 0018); it gives the observer each companion's state and hears of
  every exclusion;
* the observer gets every report of the radio, every received message and every delivery, for
  the documents of the providers and the history (observer.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from meshcore_home_gateway import __version__
from meshcore_home_gateway.config import Config
from meshcore_home_gateway.contact_manager import ContactManager
from meshcore_home_gateway.core.airtime import (
    channel_message_bytes,
    direct_attempt_airtime_s,
    time_on_air_s,
)
from meshcore_home_gateway.core.budget import AirtimeBudget, Deduplicator, RateLimit
from meshcore_home_gateway.core.commands import (
    CommandDecision,
    Rejection,
    command_word,
    evaluate_command,
)
from meshcore_home_gateway.core.contacts import PREFIX_HEX_DIGITS, Contact, is_key
from meshcore_home_gateway.core.model import (
    DeliveryEvent,
    DeliveryState,
    InboundMessage,
    MessageKind,
    Target,
)
from meshcore_home_gateway.core.telemetry import LaneFactory, TelemetryStatus
from meshcore_home_gateway.core.text import TextPolicy, TextRejectedError, prepare_text
from meshcore_home_gateway.mqtt.client import ContactRequest, SendRequest
from meshcore_home_gateway.mqtt.contact_devices import (
    ContactDevices,
    contact_discovery_subscription,
)
from meshcore_home_gateway.mqtt.discovery import discovery_payload, discovery_topic
from meshcore_home_gateway.mqtt.manage_entities import ManageEntities, manage_topic
from meshcore_home_gateway.mqtt.payloads import (
    command_payload,
    delivery_payload,
    encode,
    received_payload,
    rx_payload,
)
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codec import MAX_TEXT_BYTES, radio_name
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.protocol.links import contact_link
from meshcore_home_gateway.radio.profile import RadioProfile
from meshcore_home_gateway.radio.session import (
    AckReceived,
    AdvertHeard,
    Barrier,
    CommandFailedError,
    CompanionSession,
    MessageReceived,
    PacketLogged,
    PathChanged,
    RadioError,
    RadioEvent,
    RadioInfo,
    TelemetryReceived,
)
from meshcore_home_gateway.radio.transport import Transport
from meshcore_home_gateway.state import AnnouncedComponents, CommandLedger, TelemetryRecords
from meshcore_home_gateway.systemd import notify, watchdog_interval_s
from meshcore_home_gateway.telemetry_poller import OutboxActivity, TelemetryPoller

log = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 1024
RECONNECT_MIN_S, RECONNECT_MAX_S = 1.0, 60.0
STATUS_INTERVAL_S = 60.0
STOP_DRAIN_S = 2.0  # on stop, received messages already out of the radio are handled this long
ACK_TIMEOUT_FACTOR = 1.2  # like the MeshCore Python client
ACK_TIMEOUT_MIN_S, ACK_TIMEOUT_MAX_S = 5.0, 60.0
RECENT_ACK_S = 120.0
REFUSED_EVENTS_PER_S, REFUSED_EVENTS_BURST = 20.0, 100.0  # events of requests refused at once
REFUSED_REPORT_INTERVAL_S = 60.0  # the log counts the events left out at most this often
INBOUND_DEDUP_S = 3600.0  # retries reach us as duplicates with the same timestamp and text
LOOK_ALIKE_MEMORY_S = 86_400.0  # a key with a sender's prefix counts this long after last seen
_FINAL_FROM_CACHE = frozenset(
    {Rejection.SENDER_AMBIGUOUS, Rejection.SENDER_NOT_ALLOWED, Rejection.REPLAY,
     Rejection.TOO_OLD, Rejection.FROM_THE_FUTURE}
)  # fmt: skip
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
    contact_requests: asyncio.Queue[ContactRequest]
    connected: bool
    on_connected: list[Callable[[], None]]
    on_ha_online: list[Callable[[], None]]
    on_ha_offline: list[Callable[[], None]]
    on_ha_device: list[Callable[[str, bytes, bool], None]]
    on_show_hidden: list[Callable[[], None]]

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def publish(self, topic: str, payload: bytes, retain: bool = False, qos: int = 1) -> None: ...


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
    raw: str | None = None  # a channel text accepted before the radio's name was known


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
        components: AnnouncedComponents | None = None,
        observer: Observer | None = None,
        devices: ContactDevices | None = None,
        profile: RadioProfile | None = None,
        manage_components: AnnouncedComponents | None = None,
        telemetry_records: TelemetryRecords | None = None,
        telemetry_lanes: LaneFactory | None = None,
    ) -> None:
        """`telemetry_lanes`: builds the telemetry lanes (tests)."""
        self.config = config
        self._components = components
        self._bus = bus
        self._transport_factory = transport_factory
        self._ledger = ledger
        self._session_factory = session_factory
        self._wall = wall_clock
        self._mono = monotonic
        self._notify = notifier
        self.topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
        self._targets = {t.alias: t for t in config.targets}
        self.observer = observer or Observer(
            bus, self.topics, protected=protected_keys(config), wall_clock=wall_clock
        )
        self.observer.manage = config.contacts.manage
        self._devices = devices  # the contacts' devices in Home Assistant
        self._profile = profile  # the managed radio profile (ADR 0020)
        if devices is not None:
            self.observer.on_contacts.append(devices.update)
        self._manage: ManageEntities | None = None  # Home Assistant's controls to add contacts
        if config.homeassistant.discovery:
            self._manage = ManageEntities(
                config, self.topics, bus, self.observer.mesh, records=manage_components,
                protected=protected_keys(config), monotonic=monotonic, wall_clock=wall_clock,
                connected=lambda: bus.connected,
            )  # fmt: skip
            self.observer.on_nodes.append(self._manage.nodes_changed)
        self._events: asyncio.Queue[RadioEvent] = asyncio.Queue()
        self._contacts = ContactManager(
            config, self.observer, self._events, self.live_session,
            protected=protected_keys(config), monotonic=monotonic,
        )  # fmt: skip
        self._messages: asyncio.Queue[MessageReceived] = asyncio.Queue()
        # keys with the 6-byte prefix of an allowed command sender, as the radio named them:
        # key -> when last seen; only these are kept, so the memory stays small (ADR 0009)
        self._watched = frozenset(key[:PREFIX_HEX_DIGITS] for key in config.commands.senders)
        self._seen: dict[str, float] = {}
        self._queue: asyncio.Queue[Outgoing] = asyncio.Queue(maxsize=config.messages.queue_size)
        self._activity = OutboxActivity(monotonic, self._queue.qsize)  # telemetry yields to it
        self._telemetry = self._telemetry_poller(telemetry_records, telemetry_lanes)
        if self._telemetry is None and (records := telemetry_records) is not None:
            # D13 holds when telemetry is on again: the saved schedule forgets the answers
            self.observer.on_telemetry_exclusion.append(lambda key, _: records.forget(key))
        self._budget = AirtimeBudget(
            config.airtime.budget_s_per_hour, config.airtime.burst_s, now=monotonic()
        )
        self._dedup = Deduplicator(config.messages.dedup_window_s)
        self._refusals = RateLimit(REFUSED_EVENTS_PER_S, REFUSED_EVENTS_BURST, now=monotonic())
        self._unpublished = 0  # events of refused requests left out since the last report
        self._next_report = monotonic()
        # a copy passes the age check until 2 x max_age_s after the original (the sender's clock
        # may be max_age_s ahead): the duplicate memory must last as long (ADR 0009)
        max_age_s = config.commands.max_age_s
        self._inbound_dedup = Deduplicator(max(INBOUND_DEDUP_S, 2 * max_age_s + 60.0))
        self._acks = AckWaiter(monotonic)
        self._session: CompanionSession | None = None
        self._radio_info: RadioInfo | None = None
        self._announced: tuple[str, str, str] | None = None  # radio identity in the discovery
        self._removed: dict[str, str] | None = None  # earlier components, removed in this run
        self._reannounce: asyncio.TimerHandle | None = None  # a scheduled announcement
        self._reannounce_birth = False  # it follows Home Assistant's birth message
        self._radio_ready = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._telemetry_task: asyncio.Task[None] | None = None

    def _telemetry_poller(
        self, records: TelemetryRecords | None, lanes: LaneFactory | None
    ) -> TelemetryPoller | None:
        """The task that asks the companions, if telemetry is enabled: it gives the observer each
        companion's state and hears of every exclusion and every contact added."""
        if not self.config.telemetry.enabled:
            return None
        poller = TelemetryPoller(
            self.observer, self.live_session, self._activity, records=records,
            tcp=self.config.radio.transport == "tcp", monotonic=self._mono, wall_clock=self._wall,
            lanes=lanes,
        )  # fmt: skip
        self.observer.telemetry_state = poller.state_of
        self.observer.on_telemetry_exclusion.append(poller.exclusion_changed)
        self.observer.on_contact_added.append(poller.contact_added)
        poller.on_status = self._publish_state
        return poller

    # --- life cycle -----------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        if self._telemetry is not None:  # the states of the first documents come from it
            self._telemetry.restore()
        for warning in protected_look_alikes(self.config):
            log.warning("%s", warning)
        if self.config.contacts.manage and self.config.radio.transport == "tcp":
            log.warning("contact management over TCP: heard nodes are best effort, the radio "
                        "drops its pushes while no client is connected")  # fmt: skip
        self._bus.on_connected.append(self._connected)
        self._bus.on_connected.append(lambda: self._observe(self.observer.publish_all))
        self._bus.on_ha_online.append(lambda: self._schedule_announcements(birth=True))
        self._bus.on_ha_device.append(self._ha_device_message)
        if (devices := self._devices) is not None:
            self._bus.on_ha_offline.append(lambda: self._observe(devices.ha_offline))
            self._bus.on_show_hidden.append(lambda: self._observe(devices.show_hidden))
        await self._bus.start()
        self._notify("READY=1")
        try:
            async with asyncio.TaskGroup() as group:
                for name, coroutine in (
                    ("radio", self._radio_loop()),
                    ("requests", self._request_loop()),
                    ("contact requests", self._contact_request_loop()),
                    ("contacts", self._contacts.run()),
                    ("outbox", self._outbox_loop()),
                    ("events", self._event_loop()),
                    ("messages", self._message_loop()),
                    ("status", self._status_loop()),
                ):
                    self._tasks.append(group.create_task(coroutine, name=name))
                if self._telemetry is not None:
                    task = group.create_task(self._telemetry.run(), name="telemetry")
                    self._telemetry_task = task
                    self._tasks.append(task)
                await stop.wait()
                self._notify("STOPPING=1")
                if self._telemetry_task is not None:  # no request after this; it saves its schedule
                    self._telemetry_task.cancel()
                    await asyncio.wait([self._telemetry_task])
                await self._finish_inbound(self._tasks[0])  # the radio task
                for task in self._tasks:
                    task.cancel()
        finally:
            if self._session is not None:
                await self._session.close()
            self._radio_ready.clear()
            self._observe(self.observer.flush)  # what changed since its last tick
            self._publish_state()
            await self._bus.stop()

    async def _finish_inbound(self, radio: asyncio.Task[None]) -> None:
        """Stop the radio first, then handle the messages it has already handed over, for at most
        STOP_DRAIN_S: once fetched, they are no longer in the radio. A command waiting for its
        table read is rejected, as the closed session cannot read it."""
        radio.cancel()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(STOP_DRAIN_S):
                await asyncio.wait([radio])
                await self._events.join()
                await self._messages.join()

    # --- radio ----------------------------------------------------------------------------------

    async def _radio_loop(self) -> None:
        delay = RECONNECT_MIN_S
        while True:
            session = self._session_factory(
                self._transport_factory(), self._events, self.config.radio.set_clock
            )
            session.on_contact_key = self._contact_key
            session.profile = self._profile
            try:
                self._radio_info = await session.start()
            except (RadioError, OSError) as exc:
                log.warning("radio: cannot connect (%s); retry in %.0f s", exc, delay)
            except Exception:  # a bug while connecting: keep the service up, retry later
                log.exception("radio: cannot connect; retry in %.0f s", delay)
            else:
                self._session = session
                self._radio_ready.set()
                if self._telemetry is not None:
                    self._observe(self._telemetry.radio_connected)
                delay = RECONNECT_MIN_S
                max_contacts = self._radio_info.device.max_contacts
                self._observe(self.observer.radio_connected, max_contacts, session.tuning)
                # untested firmware takes no request: the documents and the controls say so
                takes = self._contacts.takes_requests(self._radio_info)
                self._observe(self.observer.set_manage, takes)
                for report in self._profile.take_reports() if self._profile else []:
                    self._observe(self.observer.profile_applied, report)
                self._check_radio(session)
                if self.config.homeassistant.discovery and _identity(self._radio_info) != (
                    self._announced
                ):
                    self._publish_announcements()  # the device page shows the radio
                else:
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
            longest = direct_attempt_airtime_s(MAX_TEXT_BYTES, 0, True, info.lora)
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

    async def _contact_request_loop(self) -> None:
        while True:
            self._contacts.accept(await self._bus.contact_requests.get())

    def live_session(self) -> CompanionSession | None:
        """The session, while the radio is connected."""
        session = self._session
        if session is None or session.closed or not self._radio_ready.is_set():
            return None
        return session

    def _accept(self, request: SendRequest) -> None:
        event_id = secrets.token_hex(4)
        try:
            target, text, raw = self._validate(request)
        except _FinalError as exc:
            event = DeliveryEvent(event_id, request.alias, exc.state, reason=exc.reason)
            self._deliver_refusal(event)
            return
        state, reason = DeliveryState.QUEUED, None
        key = (target.alias, text)
        if self._dedup.is_duplicate(key, self._mono()):
            state = DeliveryState.DUPLICATE
        else:
            try:
                self._queue.put_nowait(Outgoing(event_id, target, text, self._mono(), raw))
                self._activity.touch()
            except asyncio.QueueFull:
                self._dedup.forget(key)  # not taken, so a retry must not count as duplicate
                state, reason = DeliveryState.REJECTED, "queue_full"
        event = DeliveryEvent(event_id, target.alias, state, text=text, reason=reason)
        if state is DeliveryState.QUEUED:
            self._deliver_event(event)
        else:
            self._deliver_refusal(event)

    def _deliver_refusal(self, event: DeliveryEvent) -> None:
        """The event of a request refused at once (rejected, duplicate), at a bounded rate: a
        flood of requests must not become a flood of events, which would push other events out
        of a subscriber's queue on the broker. Queued requests keep all their events."""
        if self._refusals.allow(self._mono()):
            self._deliver_event(event)
            return
        self._unpublished += 1
        self._report_unpublished()

    def _report_unpublished(self) -> None:
        now = self._mono()
        if self._unpublished and now >= self._next_report:
            log.warning(
                "delivery events of refused send requests not published (limit %g per second, "
                "bursts of %g): %d", REFUSED_EVENTS_PER_S, REFUSED_EVENTS_BURST, self._unpublished,
            )  # fmt: skip
            self._unpublished = 0
            self._next_report = now + REFUSED_REPORT_INTERVAL_S

    def _validate(self, request: SendRequest) -> tuple[Target, str, str | None]:
        """The target, the prepared text and, if the text is limited only when it is sent (a
        channel text before the radio's name is known), the request text."""
        if request.retained:
            raise _FinalError(DeliveryState.REJECTED, "retained")
        if len(request.payload) > MAX_REQUEST_BYTES:
            raise _FinalError(DeliveryState.REJECTED, "too_large")
        try:
            raw = request.payload.decode("utf-8")
        except UnicodeDecodeError:
            raise _FinalError(DeliveryState.REJECTED, "invalid_utf8") from None
        if request.contact:
            target = self._contact_target(request.alias)
        elif (configured := self._targets.get(request.alias)) is not None:
            target = configured
        else:
            raise _FinalError(DeliveryState.REJECTED, "unknown_target")
        try:
            text = prepare_text(raw, self._text_policy(target, self._radio_info))
        except TextRejectedError as exc:
            raise _FinalError(DeliveryState.REJECTED, exc.reason) from None
        name_unknown = target.kind is MessageKind.CHANNEL and self._radio_info is None
        return target, text, raw if name_unknown else None

    def _contact_target(self, key: str) -> Target:
        """A companion of the radio's table as last read, as a recipient (ADR 0017); its alias
        is its full key. Repeaters, room servers and sensors do not take texts from it."""
        tracked = self.observer.mesh.contacts.get(key) if is_key(key) else None
        if tracked is None:  # also a key in upper case or a prefix: keys as the gateway writes them
            raise _FinalError(DeliveryState.REJECTED, "unknown_contact")
        if tracked.node.node_type != NodeType.CHAT:
            raise _FinalError(DeliveryState.REJECTED, "not_a_companion")
        return Target(key, MessageKind.CONTACT, public_key=key)

    def _target_of(self, alias: str) -> Target | None:
        target = self._targets.get(alias)
        if target is None and is_key(alias):
            return Target(alias, MessageKind.CONTACT, public_key=alias)
        return target

    def _text_policy(self, target: Target, info: RadioInfo | None) -> TextPolicy:
        """Without the radio's name, a channel text gets the limit of the shortest one."""
        limit = MAX_TEXT_BYTES
        if target.kind is MessageKind.CHANNEL:
            name = len(info.name.encode()) if info else 0
            limit = MAX_TEXT_BYTES - name - 2  # the firmware prepends "<name>: "
        messages = self.config.messages
        return TextPolicy(limit, ascii_only=messages.ascii_only, truncate=messages.truncate)

    # --- outbox ---------------------------------------------------------------------------------

    async def _outbox_loop(self) -> None:
        while True:
            item = await self._queue.get()
            self._activity.delivering(True)  # no await before: telemetry sees it at once
            try:
                await self._deliver(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("delivery %s failed unexpectedly", item.id)
                failed = self._event(item, DeliveryState.FAILED, reason="internal_error")
                self._deliver_event(failed)
            finally:
                self._activity.delivering(False)

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

    async def _read_changed_contacts(self, session: CompanionSession, target: Target) -> bool:
        """For a contact target, read the table again if the radio reported a change: it
        encrypts for the first contact with the prefix, so the target must be resolved on a
        table that shows a look-alike added in an earlier slot. False for a channel target."""
        if target.kind is not MessageKind.CONTACT:
            return False
        if session.contacts_outdated:
            await session.refresh_contacts()
        return True

    async def _attempt(
        self, session: CompanionSession, item: Outgoing, *, attempt: int, timestamp: int,
        deadline: float, acks: list[bytes],
    ) -> DeliveryEvent | None:  # fmt: skip
        """One transmission. Returns the final event, or None when another attempt should follow."""
        info = session.info
        if info is None:
            raise RadioError("radio not identified")
        await self._read_changed_contacts(session, item.target)
        airtime, reset_path = self._plan(session, item, attempt, info)
        target, text = item.target, item.text  # after planning, which may limit the text
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
        if wait > 0 and await self._read_changed_contacts(session, target):
            self._resolve(session, target)  # a look-alike may have come while waiting
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
            if item.raw is not None:  # accepted before the radio's name was known: limit it now
                try:
                    final = prepare_text(item.raw, self._text_policy(target, info))
                except TextRejectedError as exc:
                    raise _FinalError(DeliveryState.FAILED, exc.reason) from None
                if final != item.text:  # deduplicate on the text that is sent
                    self._dedup.rekey(
                        (target.alias, item.text), (target.alias, final), self._mono()
                    )
                item.text = final
                size = len(item.text.encode())
            name_bytes = len(info.name.encode())
            if size > MAX_TEXT_BYTES - name_bytes - 2:  # the radio was renamed after queueing
                raise _FinalError(DeliveryState.FAILED, "too_long")
            return time_on_air_s(channel_message_bytes(size, name_bytes), info.lora), None
        contact = self._resolve(session, target)
        record = session.contact_records[contact.public_key]
        known_flood = record.out_path_len == 0xFF
        flood_after = self.config.messages.flood_after
        reset = None
        if attempt == flood_after and not known_flood:
            reset = bytes.fromhex(contact.public_key)
        # from the reset on, assume a flood: the cached record does not show the reset
        flood = known_flood or attempt >= flood_after
        path_bytes = 0 if flood else record.path_bytes or 0
        return direct_attempt_airtime_s(size, path_bytes, flood, info.lora), reset

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
        # %r: the alias of a rejected request comes from its topic, which may hold any character
        log.info("delivery %s to %r: %s%s", event.id, event.target, event.state.value,
                 f" ({event.reason})" if event.reason else "")  # fmt: skip
        self._bus.publish(self.topics.delivery, encode(delivery_payload(event)))
        self._observe(self.observer.delivery, event, self._target_of(event.target))

    # --- radio events ---------------------------------------------------------------------------

    async def _event_loop(self) -> None:
        while True:
            event = await self._events.get()
            telemetry = self._telemetry
            if isinstance(event, MessageReceived):
                if not self._ignored(event.message):  # an automation's answer goes first
                    self._activity.touch()
                self._messages.put_nowait(event)  # in order, without delaying acks
            elif isinstance(event, AckReceived):
                self._acks.resolve(event.ack, event.round_trip_ms)
            elif isinstance(event, Barrier):
                event.done.set()
            elif isinstance(event, TelemetryReceived):  # only the poller knows whom it asked
                if telemetry is not None:
                    self._observe(telemetry.answer, event)
            elif isinstance(event, PathChanged):
                if telemetry is not None:
                    self._observe(telemetry.path_changed, event.public_key)
            else:
                if isinstance(event, PacketLogged) and self.config.diagnostics.publish_rx_log:
                    rx = event.rx
                    payload = rx_payload(rx.snr_db, rx.rssi_dbm, rx.raw, event.received_at)
                    self._bus.publish(self.topics.rx, encode(payload))
                if telemetry is not None:
                    self._telemetry_sees(telemetry, event)
                self._observe(self.observer.radio_event, event)
            self._events.task_done()  # for the stop (_finish_inbound)

    def _telemetry_sees(self, telemetry: TelemetryPoller, event: RadioEvent) -> None:
        if isinstance(event, PacketLogged):
            self._observe(telemetry.packet_logged, event.rx)
        elif isinstance(event, AdvertHeard) and _may_be_asked(event.advert):
            self._observe(telemetry.heard, event.advert.public_key.hex())

    async def _message_loop(self) -> None:
        while True:
            event = await self._messages.get()
            try:
                await self._on_message(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("received message could not be handled")
            self._messages.task_done()

    def _ignored(self, message: InboundMessage) -> bool:
        """A message of a channel the configuration ignores: neither published nor answered."""
        return (
            message.kind is MessageKind.CHANNEL
            and message.channel_index in self.config.messages.ignore_channels
        )

    async def _on_message(self, event: MessageReceived) -> None:
        message, session = event.message, event.session
        if self._ignored(message):
            return
        origin = (
            message.sender_prefix if message.kind is MessageKind.CONTACT else message.channel_index
        )
        key = (message.kind, origin, message.sender_timestamp, message.text)
        # also after a rejection: an identical copy is the sender's retry after a lost
        # acknowledgement or a replay, which cannot be told apart; a command sent anew has a newer
        # timestamp
        if self._inbound_dedup.is_duplicate(key, self._mono()):
            log.debug("duplicate of a received message dropped (sender retry or replay)")
            return
        decision = await self._judge(event)
        command = decision.command if decision else None
        if decision is not None and command is not None:
            try:
                # first; also when the broker is unreachable (below): the sender was verified
                self._ledger.record(command.sender_key, command.sender_timestamp)
            except OSError:
                log.exception("command not published: the state file cannot be written")
                command = None
                decision = CommandDecision(decision.word, rejection=Rejection.STATE_UNWRITABLE)
        if decision is not None and command is not None and not self._bus.connected:
            # queued until the reconnect, it would trigger automations at any later time
            log.warning("command %s refused: the broker is unreachable", decision.word)
            command = None
            decision = CommandDecision(decision.word, rejection=Rejection.BROKER_UNAVAILABLE)
        sender = None
        if message.sender_prefix is not None:
            matches = session.contacts.resolve(message.sender_prefix)
            sender = matches[0] if len(matches) == 1 else None
        target = self._target_for(message, sender)
        # %r: names come from the mesh and must not forge log lines or terminal escapes
        log.info("received %s message from %r (%d bytes)%s", message.kind.value,
                 sender.name if sender else message.sender_prefix or message.channel_index,
                 len(message.text.encode()),
                 f", command {decision.word}: {'accepted' if command else decision.rejection}"
                 if decision else "")  # fmt: skip
        log.debug("text: %r", message.text)
        payload = received_payload(message, sender, target, decision)
        self._bus.publish(self.topics.received, encode(payload))
        if sender is not None and message.kind is MessageKind.CONTACT:
            self._bus.publish(self.topics.contact_received(sender.public_key), encode(payload))
        self._observe(self.observer.message, message, sender)
        if (
            self._telemetry is not None and sender is not None and not event.at_connect
            and message.kind is MessageKind.CONTACT
        ):  # fmt: skip
            # as it arrived; one fetched at connect may have waited in the radio for hours
            self._observe(self._telemetry.heard, sender.public_key)
        if command is not None:
            name = sender.name if sender else None
            # QoS 0: an event that cannot be delivered now is lost, never delivered late (after
            # a broker loss that paho has not noticed yet); the sender gets no answer and can
            # send the command again
            self._bus.publish(
                self.topics.command,
                encode(command_payload(command, name, message.received_at)),
                qos=0,
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

    def _observe(self, call: Callable[..., None], *args: Any) -> None:
        """Hand something to the observer: a bug in the views must not stop messages."""
        try:
            call(*args)
        except Exception:
            log.exception("observer failed")

    # --- status ---------------------------------------------------------------------------------

    async def _status_loop(self) -> None:
        watchdog = watchdog_interval_s()
        tick = min(STATUS_INTERVAL_S, watchdog / 2) if watchdog else STATUS_INTERVAL_S
        next_state = self._mono() + STATUS_INTERVAL_S
        while True:
            if watchdog:
                self._notify("WATCHDOG=1")
            self._report_unpublished()  # the end of a flood of refused requests
            self._contacts.report_unpublished()
            self._observe(self.observer.exclusions.flush)  # an exclusion not stored yet
            self._observe(self.observer.tick)
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
            # adds the gateway in the MeshCore app (as a QR code): it needs no write, no airtime
            "contact_link": contact_link(info.name, info.public_key) if info else None,
            "version": __version__,
            **self._telemetry_state(),
        }

    def _telemetry_state(self) -> dict[str, Any]:
        off: TelemetryStatus = "off"
        state: dict[str, Any] = {
            "telemetry": off, "telemetry_reason": None, "telemetry_airtime_1h_s": None,
            "telemetry_mesh_airtime_1h_s": None,
        }  # fmt: skip
        if self._telemetry is None:
            return state
        try:
            own, mesh = self._telemetry.used_s()
            return {
                "telemetry": self._telemetry.status,
                "telemetry_reason": self._telemetry.reason,  # why it is paused
                "telemetry_airtime_1h_s": round(own, 3),
                "telemetry_mesh_airtime_1h_s": round(mesh, 3),
            }
        except Exception:  # the rest of the state goes out
            log.exception("telemetry: state unavailable")
            return {**state, "telemetry": None}

    def _publish_state(self) -> None:
        state = self.state_payload()
        self._bus.publish(self.topics.state, encode(state), retain=True)
        self._notify(f"STATUS=radio {state['radio']}, queue {state['queue']}")

    def _connected(self) -> None:
        """The broker connected: Home Assistant may have restarted unseen meanwhile."""
        if self._devices is not None:
            self._observe(self._devices.ha_offline)
        self._publish_announcements()

    def _ha_device_message(self, topic: str, payload: bytes, retained: bool) -> None:
        """A message on a discovery topic of the gateway. An empty one on a topic of its own
        device (the main document, the controls that add contacts) comes from Home Assistant,
        which clears it when it removes a disabled entity (or the user deletes the device): the
        entities come back with the next announcement."""
        if topic not in (discovery_topic(self.config), manage_topic(self.config)):
            if self._devices is not None:
                self._observe(self._devices.discovery_message, topic, payload, retained)
            return
        if payload or retained or self._reannounce is not None:
            return
        log.info("Home Assistant cleared the gateway's device; announcing it again")
        self._schedule_announcements()

    def _publish_announcements(self, *, birth: bool = False) -> None:
        if self.config.homeassistant.discovery:
            if self._removed is None:  # first announcement: what earlier runs had and this lacks
                current = discovery_payload(self.config, self.topics)["cmps"]
                platforms = {key: component["p"] for key, component in current.items()}
                self._removed = (
                    self._components.replace(platforms, now=self._wall())
                    if self._components else {}
                )  # fmt: skip
                if self._removed:
                    log.info("Home Assistant: removing %s", ", ".join(sorted(self._removed)))
            topic, info = discovery_topic(self.config), self._radio_info
            if self._removed:  # Home Assistant removes only what it discovered since its start
                bind = discovery_payload(self.config, self.topics, info, self._removed, bind=True)
                self._bus.publish(topic, encode(bind))
            document = discovery_payload(self.config, self.topics, info, self._removed)
            self._bus.publish(topic, encode(document))
            if (manage := self._manage) is not None:
                self._observe(manage.announce)
            self._announced = _identity(self._radio_info)
            if (devices := self._devices) is not None:
                self._observe(lambda: devices.announce_all(birth=birth))
        self._publish_state()

    def _schedule_announcements(self, *, birth: bool = False) -> None:
        if self._reannounce is not None:  # one is due: it counts as after a birth if one was
            birth = birth or self._reannounce_birth
            self._reannounce.cancel()
        self._reannounce_birth = birth
        delay = random.uniform(*DISCOVERY_DELAY_S)  # noqa: S311 - spreading load only
        self._reannounce = asyncio.get_running_loop().call_later(delay, self._announce_due)

    def _announce_due(self) -> None:
        birth, self._reannounce = self._reannounce_birth, None
        self._publish_announcements(birth=birth)


def ha_device_topics(config: Config) -> list[str]:
    """The gateway's discovery topics it watches, to notice what Home Assistant clears there:
    its own, and either the contact devices' (whose subscription takes the controls' topic, one
    level below the gateway's device too) or the controls' alone."""
    if not config.homeassistant.discovery:
        return []
    below = contact_discovery_subscription(config) if config.homeassistant.contact_devices else None
    return [discovery_topic(config), below or manage_topic(config)]


def protected_keys(config: Config) -> frozenset[str]:
    """Contacts the gateway must keep: configured contact targets and command senders
    (ADR 0017)."""
    targets = {t.public_key for t in config.targets if t.public_key is not None}
    return frozenset(targets | set(config.commands.senders))


def protected_look_alikes(config: Config) -> list[str]:
    """Warnings about protected keys with one 6-byte prefix: the radio names a sender by its
    prefix only, so it can hold one of them (ADR 0009); the managed profile adds them in key
    order."""
    roles: dict[str, list[str]] = {}
    for target in config.targets:
        if target.public_key is not None:
            roles.setdefault(target.public_key, []).append(f"target {target.alias}")
    for key in config.commands.senders:
        roles.setdefault(key, []).append("command sender")
    keys = sorted(roles)

    def named(key: str) -> str:
        return f"{key[:12]}…{key[-8:]} ({', '.join(roles[key])})"

    profile = config.radio.profile
    writes = profile.protect_contacts and profile.manage != "observe"
    warnings = []
    for i, a in enumerate(keys):
        for b in (k for k in keys[i + 1 :] if k[:12] == a[:12]):
            if writes:  # in key order, and never next to the other
                outcome = (f"the managed profile, when it writes, adds {a[:12]}…{a[-8:]} unless "
                           "the other is on the radio, never both")  # fmt: skip
            else:
                outcome = "the gateway adds neither, and refuses requests to add either"
            warnings.append(
                f"protected contacts {named(a)} and {named(b)} have the same first 6 bytes: the "
                f"radio names a sender by them only (ADR 0009); {outcome}"
            )
    return warnings


def protected_names(config: Config) -> dict[str, str]:
    """The protected contacts, each with a name for the radio profile to add it under while the
    radio lacks it, until its next advert names it (ADR 0020)."""
    names = {k: f"sender-{k[:4]}" for k in config.commands.senders}
    targets = {t.public_key: t.alias for t in config.targets if t.public_key is not None}
    return names | {key: radio_name(alias) for key, alias in targets.items()}


def _identity(info: RadioInfo | None) -> tuple[str, str, str] | None:
    return (info.device.model, info.device.version, info.public_key) if info else None


def _may_be_asked(advert: codec.Advert) -> bool:
    """A checked advert of a contact, or of a companion the radio did not add: added later,
    it is asked for telemetry while its advert is fresh."""
    record = advert.record
    return not advert.new or (record is not None and record.node_type == NodeType.CHAT)


def _key(target: Target) -> str:
    if target.public_key is None:
        raise ValueError(f"target {target.alias} has no public key")
    return target.public_key
