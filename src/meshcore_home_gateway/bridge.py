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
import shutil
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from meshcore_home_gateway import __version__
from meshcore_home_gateway.config import Config
from meshcore_home_gateway.contact_manager import ContactManager
from meshcore_home_gateway.core.airtime import (
    channel_message_bytes,
    direct_attempt_airtime_s,
    shortest_acknowledgement_s,
    time_on_air_s,
)
from meshcore_home_gateway.core.budget import (
    HOUR_S,
    AirtimeBudget,
    Deduplicator,
    KeyedRateLimit,
    RateLimit,
)
from meshcore_home_gateway.core.commands import (
    CommandDecision,
    Rejection,
    command_word,
    evaluate_command,
)
from meshcore_home_gateway.core.contacts import PREFIX_HEX_DIGITS, Contact, is_key
from meshcore_home_gateway.core.link import LinkWatch
from meshcore_home_gateway.core.model import (
    DeliveryEvent,
    DeliveryState,
    InboundMessage,
    MessageKind,
    Target,
)
from meshcore_home_gateway.core.telemetry import LaneFactory, TelemetryStatus
from meshcore_home_gateway.core.text import (
    CONFIRM_NONCE_BYTES,
    TextPolicy,
    TextRejectedError,
    clean_received,
    prepare_text,
    with_nonce,
    without_nonce,
)
from meshcore_home_gateway.health import (
    DISK_LOW_MB,
    FAILURES,
    LAG_TICK_S,
    LAG_WARN_S,
    LoopLag,
)
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
from meshcore_home_gateway.outbox import Outbox, Outgoing
from meshcore_home_gateway.ports import (
    AckReceived,
    AdvertHeard,
    Barrier,
    Bus,
    CommandFailedError,
    MessageReceived,
    PacketLogged,
    PathChanged,
    RadioError,
    RadioEvent,
    RadioInfo,
    RadioPort,
    RadioProfileHook,
    SendRequest,
    SessionFactory,
    TelemetryReceived,
    Transport,
)
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codec import MAX_TEXT_BYTES, radio_name
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.protocol.links import contact_link
from meshcore_home_gateway.state import (
    AirtimeLevel,
    AnnouncedComponents,
    CommandLedger,
    OutboxRecords,
    SavedMessage,
    TelemetryRecords,
)
from meshcore_home_gateway.systemd import notify, watchdog_interval_s
from meshcore_home_gateway.telemetry_poller import OutboxActivity, TelemetryPoller
from meshcore_home_gateway.texts import Text

log = logging.getLogger(__name__)
log.addFilter(FAILURES)  # caught bugs counted, their tracebacks thinned out

MAX_REQUEST_BYTES = 1024
RECONNECT_MIN_S, RECONNECT_MAX_S = 1.0, 60.0
STATUS_INTERVAL_S = 60.0
STOP_DRAIN_S = 2.0  # on stop, received messages already out of the radio are handled this long
# on stop, the acknowledgement of the message in flight is awaited this long, the longest wait of
# an attempt; within the unit's TimeoutStopSec (90 s) with the rest of the stop (ARC-013)
STOP_OUTBOX_S = 60.0
ACK_TIMEOUT_FACTOR = 1.2  # like the MeshCore Python client
ACK_TIMEOUT_MIN_S, ACK_TIMEOUT_MAX_S = 5.0, 60.0
RECENT_ACK_S = 120.0
REFUSED_EVENTS_PER_S, REFUSED_EVENTS_BURST = 20.0, 100.0  # events of requests refused at once
REFUSED_REPORT_INTERVAL_S = 60.0  # the log counts the events left out at most this often
# received messages that become events and history rows, per sender and per channel: a contact or
# a channel that floods cannot fill the bus, the history and Home Assistant's recorder; an
# accepted command always goes out (SEC-012, SEC-013)
RECEIVED_PER_SENDER_PER_S, RECEIVED_PER_SENDER_BURST = 0.5, 10.0
RECEIVED_PER_CHANNEL_PER_S, RECEIVED_PER_CHANNEL_BURST = 1.0, 20.0
MAX_ECHOED_ALIAS = 64  # characters of a refused request's topic level in its event (SEC-017)
INBOUND_DEDUP_S = 3600.0  # retries reach us as duplicates with the same timestamp and text
LOOK_ALIKE_MEMORY_S = 86_400.0  # a key with a sender's prefix counts this long after last seen
_FINAL_FROM_CACHE = frozenset(
    {
        Rejection.SENDER_AMBIGUOUS,
        Rejection.SENDER_NOT_ALLOWED,
        Rejection.REPLAY,
        Rejection.TOO_OLD,
        Rejection.FROM_THE_FUTURE,
    }
)
DISCOVERY_DELAY_S = (0.5, 2.0)  # random delay after Home Assistant's birth message
ANNOUNCE_MIN_INTERVAL_S = 30  # announcements after births and clears, at most this often
BIRTHS_PER_MINUTE = 5  # more come from another client than Home Assistant: logged


class _FinalError(Exception):
    """Ends a delivery with this state and reason."""

    def __init__(self, state: DeliveryState, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason


@dataclass
class _Flight:
    """The message the outbox delivers, for the stop (ARC-013)."""

    item: Outgoing | None = None
    acking: bool = False  # its attempt waits for the acknowledgement
    stopping: bool = False  # the service stops: no attempt starts any more
    idle: asyncio.Event = field(default_factory=asyncio.Event)  # set while none is delivered


@dataclass
class _Health:
    """What the health section reads that no other part keeps (ARC-012)."""

    state_dir: Path | None  # its free space; None: not measured (tests)
    lag: LoopLag = field(default_factory=LoopLag)  # how late the event loop ran (CODE-001)


@dataclass
class _Announcing:
    """When the gateway announces itself to Home Assistant again (SEC-006)."""

    due: asyncio.TimerHandle | None = None  # a scheduled announcement
    birth: bool = False  # it follows Home Assistant's birth message
    last: float | None = None  # monotonic time of the last announcement
    births: deque[float] = field(default_factory=deque)  # of the last minute, monotonic
    warned: float | None = None  # monotonic time of the last warning about births


class ReceivedLimit:
    """Which received messages become events and history rows: per sender and per channel at a
    bounded rate (SEC-012, SEC-013). The log counts those left out at most once a minute."""

    def __init__(self, now: float) -> None:
        self._limits = {
            MessageKind.CONTACT: KeyedRateLimit(
                RECEIVED_PER_SENDER_PER_S, RECEIVED_PER_SENDER_BURST
            ),
            MessageKind.CHANNEL: KeyedRateLimit(
                RECEIVED_PER_CHANNEL_PER_S, RECEIVED_PER_CHANNEL_BURST
            ),
        }
        self._left_out: dict[MessageKind, int] = {}  # since the last report
        self._next_report = now

    def allow(self, kind: MessageKind, origin: object, now: float) -> bool:
        """`origin`: the sender's key prefix, or the channel index."""
        if self._limits[kind].allow(origin, now):
            return True
        self._left_out[kind] = self._left_out.get(kind, 0) + 1
        self.report(now)
        return False

    def report(self, now: float) -> None:
        if self._left_out and now >= self._next_report:
            log.warning(
                "received messages not published nor recorded (limit one every %g s per sender, "
                "bursts of %g, and %g per second per channel, bursts of %g): %d direct, %d "
                "channel",
                1 / RECEIVED_PER_SENDER_PER_S,
                RECEIVED_PER_SENDER_BURST,
                RECEIVED_PER_CHANNEL_PER_S,
                RECEIVED_PER_CHANNEL_BURST,
                self._left_out.get(MessageKind.CONTACT, 0),
                self._left_out.get(MessageKind.CHANNEL, 0),
            )
            self._left_out = {}
            self._next_report = now + REFUSED_REPORT_INTERVAL_S


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
        # No count bound, and none needed (CODE-012): the radio pushes SEND_CONFIRMED only for a
        # code in its table of the 8 latest expected acknowledgements (docs/protocol.md,
        # MyMesh.cpp:424-434), all of them codes of this gateway's own attempts; the keys are
        # those of the attempts within RECENT_ACK_S, which the airtime budget limits.
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
        session_factory: SessionFactory,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        notifier: Callable[[str], bool] = notify,
        components: AnnouncedComponents | None = None,
        observer: Observer | None = None,
        devices: ContactDevices | None = None,
        profile: RadioProfileHook | None = None,
        manage_components: AnnouncedComponents | None = None,
        telemetry_records: TelemetryRecords | None = None,
        telemetry_lanes: LaneFactory | None = None,
        state_dir: Path | None = None,
    ) -> None:
        """`telemetry_lanes`: builds the telemetry lanes (tests). `state_dir`: where the state
        and the history are kept, for the free space in the health section."""
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
        self.observer.manage, self.observer.manage_reason, self.observer.heard_nodes = (
            config.contacts.manage,
            "no_radio" if config.contacts.manage else "management_off",  # until the radio connects
            config.contacts.heard_nodes,
        )
        self._devices = devices  # the contacts' devices in Home Assistant
        self._profile = profile  # the managed radio profile (ADR 0020)
        if devices is not None:
            self.observer.on_contacts.append(devices.update)
        self._manage: ManageEntities | None = None  # Home Assistant's controls to add contacts
        if config.homeassistant.discovery:
            self._manage = ManageEntities(
                config,
                self.topics,
                bus,
                self.observer.mesh,
                records=manage_components,
                protected=protected_keys(config),
                monotonic=monotonic,
                wall_clock=wall_clock,
                connected=lambda: bus.connected,
            )
            self.observer.on_nodes.append(self._manage.nodes_changed)
        self._events: asyncio.Queue[RadioEvent] = asyncio.Queue()
        self._contacts = ContactManager(
            config,
            self.observer,
            self._events,
            self.live_session,
            protected=protected_keys(config),
            monotonic=monotonic,
        )
        self._messages: asyncio.Queue[MessageReceived] = asyncio.Queue()
        # keys with the 6-byte prefix of an allowed command sender, as the radio named them:
        # key -> when last seen; only these are kept, so the memory stays small (ADR 0009)
        self._watched = frozenset(key[:PREFIX_HEX_DIGITS] for key in config.commands.senders)
        self._seen: dict[str, float] = {}
        self._queue = Outbox(config.messages.queue_size)  # alerts first (IMP-032)
        self._activity = OutboxActivity(monotonic, self._queue.qsize)  # telemetry yields to it
        self._telemetry = self._telemetry_poller(telemetry_records, telemetry_lanes)
        if self._telemetry is None and (records := telemetry_records) is not None:
            # so when telemetry is on again, the saved schedule has forgotten the answers
            # (design §6.11)
            self.observer.on_telemetry_exclusion.append(lambda key, _: records.forget(key))
            self.observer.on_contact_added.append(records.forget)
        self._budget, self._airtime = self._message_budget(ledger)
        self._dedup = Deduplicator(config.messages.dedup_window_s)
        self._refusals = RateLimit(REFUSED_EVENTS_PER_S, REFUSED_EVENTS_BURST, now=monotonic())
        # events of refused requests left out since the last report, and when the next is due
        self._unpublished, self._next_report = 0, monotonic()
        self._received = ReceivedLimit(monotonic())
        # a copy passes the age check until 2 x max_age_s after the original (the sender's clock
        # may be max_age_s ahead): the duplicate memory must last as long (ADR 0009)
        self._inbound_dedup = Deduplicator(
            max(INBOUND_DEDUP_S, 2 * config.commands.max_age_s + 60.0)
        )
        self._acks = AckWaiter(monotonic)
        self._session: RadioPort | None = None
        self._radio_info: RadioInfo | None = None
        self._link = LinkWatch()  # over TCP: whether another client took the link (SEC-003)
        self._announced: tuple[str, str, str] | None = None  # radio identity in the discovery
        self._removed: dict[str, str] | None = None  # earlier components, removed in this run
        self._announcing = _Announcing()
        self._radio_ready, self._flight = asyncio.Event(), _Flight()
        self._tasks: list[asyncio.Task[None]] = []
        self._telemetry_task: asyncio.Task[None] | None = None
        self._health = _Health(state_dir)

    def _telemetry_poller(
        self, records: TelemetryRecords | None, lanes: LaneFactory | None
    ) -> TelemetryPoller | None:
        """The task that asks the companions, if telemetry is enabled: it gives the observer each
        companion's state and hears of every exclusion and every contact added."""
        if not self.config.telemetry.enabled:
            return None
        poller = TelemetryPoller(
            self.observer,
            self.live_session,
            self._activity,
            records=records,
            tcp=self.config.radio.transport == "tcp",
            monotonic=self._mono,
            wall_clock=self._wall,
            lanes=lanes,
        )
        self.observer.telemetry_state = poller.state_of
        self.observer.on_telemetry_exclusion.append(poller.exclusion_changed)
        self.observer.on_contact_added.append(poller.contact_added)
        poller.on_status = self._publish_state
        return poller

    def _message_budget(self, ledger: CommandLedger) -> tuple[AirtimeBudget, AirtimeLevel]:
        """The budget at the level the last run left (ARC-005), refilled since; at least what a
        long message needs, so that the first alert after a restart goes out at once."""
        airtime, level = self.config.airtime, AirtimeLevel(ledger.store)
        saved, tokens = level.load(self._wall()), None
        if saved is not None:
            stored, elapsed = saved
            refilled = stored + (elapsed or 0.0) * airtime.budget_s_per_hour / HOUR_S
            floor = airtime.burst_s if level.longest_s is None else level.longest_s
            tokens = max(refilled, min(floor, airtime.burst_s))
        budget = AirtimeBudget(
            airtime.budget_s_per_hour, airtime.burst_s, now=self._mono(), tokens=tokens
        )
        return budget, level

    # --- life cycle -----------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        if self._telemetry is not None:  # the states of the first documents come from it
            self._telemetry.restore()
        for warning in start_warnings(self.config):
            log.warning("%s", warning)
        if self.config.contacts.manage and self.config.radio.transport == "tcp":
            log.warning(
                "contact management over TCP: heard nodes are best effort, the radio "
                "drops its pushes while no client is connected"
            )
        if self.config.messages.publish_channels == "targets":
            channels = ", ".join(map(str, sorted(_target_channels(self.config)))) or "none"
            log.info("channel messages published only from the channels of targets: %s", channels)
        self._bus.on_connected.append(self._connected)
        self._bus.on_connected.append(lambda: self._observe(self.observer.publish_all))
        self._bus.on_ha_online.append(lambda: self._schedule_announcements(birth=True))
        self._bus.on_ha_device.append(self._ha_device_message)
        if (devices := self._devices) is not None:
            self._bus.on_ha_offline.append(lambda: self._observe(devices.ha_offline))
            self._bus.on_show_hidden.append(lambda: self._observe(devices.show_hidden))
        await self._bus.start()
        self._restore_outbox()
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
                    ("lag", self._lag_loop()),
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
                await self._finish_outbox()  # before the radio's task: it may await an ack
                await self._finish_inbound(self._tasks[0])  # the radio task
                for task in self._tasks:
                    task.cancel()
        finally:
            if self._session is not None:
                await self._session.close()
            self._radio_ready.clear()
            self._observe(self.observer.flush)  # what changed since its last tick
            self._airtime.save(self._budget.available_s(self._mono()), self._wall())
            self._ledger.store.flush()
            self._publish_state()
            await self._bus.stop()

    async def _finish_outbox(self) -> None:
        """No attempt starts any more; one that waits for its acknowledgement gets it, for at
        most STOP_OUTBOX_S. The messages still queued, that one too if it needs another attempt,
        are kept for the next start (ARC-013): requests are taken until then."""
        flight = self._flight
        flight.stopping = True
        if flight.acking:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(STOP_OUTBOX_S):
                    await flight.idle.wait()
        outbox = [task for task in self._tasks if task.get_name() == "outbox"]
        for task in outbox:
            task.cancel()
        await asyncio.wait(outbox)
        now, kept = self._mono(), self._queue.take_all()
        OutboxRecords(self._ledger.store).save(
            [
                SavedMessage(
                    item.id,
                    item.target.alias,
                    item.text,
                    item.raw,
                    now - item.created,
                    item.timestamp,
                    item.sent,
                )
                for item in kept
            ],
            self._wall(),
        )
        if kept:
            log.info("%d messages kept for the next start", len(kept))

    def _restore_outbox(self) -> None:
        """The messages the last stop kept: those with time left are queued again, `queued`
        again with their `id`; the others end as they would have (ARC-013). Their texts count as
        sent for the duplicate check."""
        messages, elapsed = OutboxRecords(self._ledger.store).take(self._wall())
        config, now, queued = self.config.messages, self._mono(), 0
        for saved in messages:
            target = self._target_of(saved.target)
            age = saved.age_s + elapsed if elapsed is not None else config.queue_ttl_s
            if target is None:  # its target left the configuration
                event = DeliveryEvent(
                    saved.id,
                    saved.target,
                    DeliveryState.FAILED,
                    text=saved.text,
                    attempt=saved.sent,
                    reason="unknown_target",
                )
                self._deliver_event(event)
                continue
            item = Outgoing(
                saved.id,
                target,
                saved.text,
                now - age,
                saved.raw,
                alert=target.alias in config.alert_targets,
                timestamp=saved.timestamp,
                sent=saved.sent,
            )
            if age >= config.queue_ttl_s:  # it would have expired meanwhile
                self._deliver_event(self._out_of_time(item, item.sent, reason="queue_ttl"))
                continue
            nonce = target.kind is MessageKind.CONTACT and config.confirm_nonce
            self._dedup.is_duplicate(
                (target.alias, without_nonce(item.text) if nonce else item.text), now
            )
            self._queue.restore(item)
            queued += 1
            self._deliver_event(self._event(item, DeliveryState.QUEUED, attempt=item.sent))
        if messages:
            log.info(
                "messages kept by the last stop: %d queued again, %d at an end",
                queued,
                len(messages) - queued,
            )

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
            session.set_clock_max_days = self.config.radio.set_clock_max_days
            try:
                self._radio_info = await session.start()
            except (RadioError, OSError) as exc:
                log.warning("radio: cannot connect (%s); retry in %.0f s", exc, delay)
            except Exception:  # a bug while connecting: keep the service up, retry later
                log.exception("radio: cannot connect; retry in %.0f s", delay)
            else:
                self._session = session
                self._radio_ready.set()
                if self.config.radio.transport == "tcp":
                    self._link_started(session)
                if self._telemetry is not None:
                    self._observe(self._telemetry.radio_connected)
                delay = RECONNECT_MIN_S
                max_contacts = self._radio_info.device.max_contacts
                # untested firmware takes no request: the documents and the controls say so, and
                # why (OPS-007); before the index the connect publishes
                takes = self._contacts.takes_requests(self._radio_info)
                self._observe(
                    self.observer.set_manage, takes, self._contacts.refusal(self._radio_info)
                )
                self._observe(self.observer.radio_connected, max_contacts, session.tuning)
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
                    self._link.ended(*_uptime(session), by_radio=session.closed_by_radio)
                    # requests are refused until the radio is back: the index says why
                    self._observe(
                        self.observer.set_manage, self.observer.manage, self._contacts.refusal(None)
                    )
                    self._publish_state()
            finally:
                await session.close()
            await asyncio.sleep(delay * random.uniform(1.0, 1.2))  # noqa: S311 - jitter only
            delay = min(delay * 2, RECONNECT_MAX_S)

    def _link_started(self, session: RadioPort) -> None:
        """Over TCP: whether another client took the link before this one (core.link)."""
        uptime_s, read_at = _uptime(session)
        if self._link.started(uptime_s, read_at):
            log.warning(
                "radio: the radio closed the TCP connection and kept running (uptime %s "
                "s): another client connected to it, which can control the radio and "
                "read its private key; the gateway took the link back "
                "(docs/deployment.md, Troubleshooting)",
                uptime_s,
            )

    def _check_radio(self, session: RadioPort) -> None:
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
            self._airtime.longest_s = longest  # the least a restart gets (ARC-005)
            if longest > self._budget.capacity_s:
                log.warning(
                    "a long direct message takes %.1f s of airtime at the radio's settings, more "
                    "than airtime.burst_s (%.1f s): such messages fail (exceeds_airtime_burst)",
                    longest,
                    self._budget.capacity_s,
                )
        for target in self._targets.values():
            if target.kind is MessageKind.CHANNEL and target.channel_index not in session.channels:
                log.warning(
                    "target %s: channel %s is not configured on the radio",
                    target.alias,
                    target.channel_index,
                )
            if target.kind is MessageKind.CONTACT and target.public_key is not None:
                matches = session.contacts.resolve(target.public_key[:PREFIX_HEX_DIGITS])
                if [c.public_key for c in matches] != [target.public_key]:
                    log.warning(
                        "target %s: contact missing on the radio or prefix ambiguous", target.alias
                    )

    async def _radio_until(self, deadline: float) -> RadioPort | None:
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

    def live_session(self) -> RadioPort | None:
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
            # the topic level may be any length: the event echoes the start of it
            alias = request.alias[:MAX_ECHOED_ALIAS]
            event = DeliveryEvent(event_id, alias, exc.state, reason=exc.reason)
            self._deliver_refusal(event)
            return
        state, reason = DeliveryState.QUEUED, None
        key = (target.alias, text)
        if self._dedup.is_duplicate(key, self._mono()):
            state = DeliveryState.DUPLICATE
        else:
            nonce = target.kind is MessageKind.CONTACT and self.config.messages.confirm_nonce
            sent = with_nonce(text) if nonce else text  # duplicates are judged without it
            alert = target.alias in self.config.messages.alert_targets
            try:
                self._queue.put(Outgoing(event_id, target, sent, self._mono(), raw, alert=alert))
                self._activity.touch()
                text = sent
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
                "bursts of %g): %d",
                REFUSED_EVENTS_PER_S,
                REFUSED_EVENTS_BURST,
                self._unpublished,
            )
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
        """Without the radio's name, a channel text gets the limit of the shortest one. A direct
        text leaves room for the random suffix of `confirm_nonce` (SEC-001)."""
        limit = MAX_TEXT_BYTES
        messages = self.config.messages
        if target.kind is MessageKind.CHANNEL:
            name = len(info.name.encode()) if info else 0
            limit = MAX_TEXT_BYTES - name - 2  # the firmware prepends "<name>: "
        elif messages.confirm_nonce:
            limit = MAX_TEXT_BYTES - CONFIRM_NONCE_BYTES
        return TextPolicy(limit, ascii_only=messages.ascii_only, truncate=messages.truncate)

    # --- outbox ---------------------------------------------------------------------------------

    async def _outbox_loop(self) -> None:
        flight = self._flight
        flight.idle.set()
        while True:
            item = await self._queue.get()
            if flight.stopping:  # no attempt starts any more: kept for the next start
                self._queue.put_back(item)
                return
            flight.item = item
            flight.idle.clear()
            self._activity.delivering(True)  # no await before: telemetry sees it at once
            try:
                await self._deliver(item)
            except asyncio.CancelledError:
                self._queue.put_back(item)  # the stop: kept for the next start
                raise
            except Exception:
                log.exception("delivery %s failed unexpectedly", item.id)
                failed = self._event(item, DeliveryState.FAILED, reason="internal_error")
                self._deliver_event(failed)
            finally:
                flight.item = None
                flight.idle.set()
                self._activity.delivering(False)

    async def _deliver(self, item: Outgoing) -> None:
        """Attempts until the message is confirmed, ends with a reason, or runs out of time.

        `expired` means the message never left: once an attempt reached the radio, running out
        of time or airtime ends it as `failed` (no acknowledgement). An attempt interrupted by a
        radio error is repeated, with the same number, when the radio is back. `attempt` in a
        final event counts the transmissions so far. Between two attempts the message is put
        back, with its progress, when an alert waits behind it (IMP-032) or the service stops
        (ARC-013).
        """
        messages = self.config.messages
        deadline = item.created + messages.queue_ttl_s
        if item.timestamp is None:  # one timestamp for all attempts: retries stay recognisable
            item.timestamp = int(self._wall())
        direct = item.target.kind is MessageKind.CONTACT
        attempts = messages.max_attempts if direct else 1
        while item.sent < attempts:
            if item.sent and (
                self._flight.stopping or (not item.alert and self._queue.alert_waiting)
            ):
                log.info("delivery %s: paused after attempt %d", item.id, item.sent)
                self._queue.put_back(item)
                return
            session = await self._radio_until(deadline)
            if session is None:
                self._deliver_event(self._out_of_time(item, item.sent))
                return
            try:
                outcome = await self._attempt(session, item, deadline=deadline)
            except _FinalError as exc:
                outcome = self._final(item, exc.state, exc.reason, item.sent)
            except CommandFailedError as exc:
                outcome = self._final(item, DeliveryState.FAILED, exc.reason, item.sent)
            except RadioError as exc:
                log.info("delivery %s: attempt %d interrupted (%s)", item.id, item.sent + 1, exc)
                item.acks.shortest_ms = 0.0  # its repeat has the same code: which copy is answered?
                # no attempt is counted, and the loop still ends (CODE-017): once `deadline` has
                # passed, _radio_until returns None, and _attempt refuses to send after it
                continue
            if outcome is not None:
                self._deliver_event(outcome)
                return
        failed = self._event(
            item, DeliveryState.FAILED, attempt=item.sent, reason="no_acknowledgement"
        )
        self._deliver_event(failed)

    def _final(self, item: Outgoing, state: DeliveryState, reason: str, sent: int) -> DeliveryEvent:
        if state is DeliveryState.EXPIRED and sent:  # it left before: it was not acknowledged
            state, reason = DeliveryState.FAILED, "no_acknowledgement"
        return self._event(item, state, attempt=sent, reason=reason)

    def _out_of_time(
        self, item: Outgoing, sent: int, *, reason: str | None = None
    ) -> DeliveryEvent:
        if sent:
            return self._event(
                item, DeliveryState.FAILED, attempt=sent, reason="no_acknowledgement"
            )
        if reason is None:
            reason = "queue_ttl" if self._radio_ready.is_set() else "no_radio"
        return self._event(item, DeliveryState.EXPIRED, reason=reason)

    async def _read_changed_contacts(self, session: RadioPort, target: Target) -> bool:
        """For a contact target, read the table again if the radio reported a change: it
        encrypts for the first contact with the prefix, so the target must be resolved on a
        table that shows a look-alike added in an earlier slot. False for a channel target."""
        if target.kind is not MessageKind.CONTACT:
            return False
        if session.contacts_outdated:
            await session.refresh_contacts()
        return True

    async def _attempt(
        self, session: RadioPort, item: Outgoing, *, deadline: float
    ) -> DeliveryEvent | None:
        """One transmission, the attempt numbered `item.sent`. Returns the final event, or None
        when another attempt should follow."""
        info = session.info
        if info is None:
            raise RadioError("radio not identified")
        attempt, acks = item.sent, item.acks
        await self._read_changed_contacts(session, item.target)
        airtime, reset_path = self._plan(session, item, attempt, info)
        target = item.target
        if airtime > self._budget.capacity_s:
            raise _FinalError(DeliveryState.FAILED, "exceeds_airtime_burst")
        wait = self._budget.wait_s(airtime, self._mono())
        if self._mono() + wait > deadline:
            raise _FinalError(DeliveryState.EXPIRED, "airtime_budget")
        round_trip = None
        if wait > 0:
            log.info("delivery %s waits %.1f s for airtime", item.id, wait)
            if acks.codes:  # an earlier attempt may still be acknowledged meanwhile
                round_trip = await self._acks.wait(acks.codes, wait)
            else:
                await asyncio.sleep(wait)
        if acks.codes and round_trip is None:
            round_trip = self._acks.take(acks.codes)
        if round_trip is not None:  # an earlier attempt arrived after all: send nothing more
            return self._confirmed(item, round_trip)
        if wait > 0 and await self._read_changed_contacts(session, target):
            self._resolve(session, target)  # a look-alike may have come while waiting
        if reset_path is not None:
            await session.reset_path(reset_path)  # only now: this attempt will be sent and flood
        self._budget.consume(airtime, self._mono())
        # at once: a crash before the next start must not give it this airtime again (ARC-005)
        self._airtime.save(self._budget.available_s(self._mono()), self._wall())
        return await self._transmit(session, item, airtime, info)

    async def _transmit(
        self, session: RadioPort, item: Outgoing, airtime: float, info: RadioInfo
    ) -> DeliveryEvent | None:
        """Hand the attempt to the radio; a direct message's waits for its acknowledgement."""
        target, text, timestamp = item.target, item.text, item.timestamp or int(self._wall())
        if target.channel_index is not None:
            await session.send_channel_text(target.channel_index, text, timestamp)
            item.sent = 1
            return self._event(item, DeliveryState.SENT, attempt=1, airtime_s=airtime)
        prefix = bytes.fromhex(_key(target)[:PREFIX_HEX_DIGITS])
        record = session.contact_records.get(_key(target))  # the path the radio will take
        sent = await session.send_text(prefix, text, timestamp, item.sent)
        item.sent += 1
        sent_event = self._event(item, DeliveryState.SENT, attempt=item.sent, airtime_s=airtime)
        self._deliver_event(sent_event)
        item.acks.add(sent.expected_ack, _shortest_ms(session, record, sent, text, info))
        timeout = sent.timeout_ms / 1000 * ACK_TIMEOUT_FACTOR
        timeout = min(max(timeout, ACK_TIMEOUT_MIN_S), ACK_TIMEOUT_MAX_S)
        self._flight.acking = True  # a stop waits for it (ARC-013)
        try:
            round_trip = await self._acks.wait(item.acks.codes, timeout)
        finally:
            self._flight.acking = False
        if round_trip is None:
            return None
        return self._confirmed(item, round_trip)

    def _confirmed(self, item: Outgoing, round_trip: int) -> DeliveryEvent:
        """`suspect` if the acknowledgement came sooner than a genuine one can (SEC-001)."""
        acks = item.acks
        suspect = acks.suspect(round_trip)
        if suspect:
            log.warning(
                "delivery %s: acknowledged after %d ms, sooner than the %.0f ms a genuine "
                "acknowledgement takes over the path at least: it may be forged (SEC-001)",
                item.id,
                round_trip,
                acks.shortest_ms,
            )
        return self._event(
            item, DeliveryState.CONFIRMED, attempt=item.sent, rtt_ms=round_trip, suspect=suspect
        )

    def _plan(
        self, session: RadioPort, item: Outgoing, attempt: int, info: RadioInfo
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

    def _resolve(self, session: RadioPort, target: Target) -> Contact:
        key = _key(target)
        matches = session.contacts.resolve(key[:PREFIX_HEX_DIGITS])
        if len(matches) > 1:
            raise _FinalError(DeliveryState.FAILED, "ambiguous_target")
        if not matches or matches[0].public_key != key:
            raise _FinalError(DeliveryState.FAILED, "unknown_contact")
        return matches[0]

    def _event(
        self,
        item: Outgoing,
        state: DeliveryState,
        *,
        attempt: int = 0,
        reason: str | None = None,
        rtt_ms: int | None = None,
        airtime_s: float | None = None,
        suspect: bool | None = None,
    ) -> DeliveryEvent:
        return DeliveryEvent(
            item.id,
            item.target.alias,
            state,
            text=item.text,
            attempt=attempt,
            reason=reason,
            rtt_ms=rtt_ms,
            airtime_ms=None if airtime_s is None else round(airtime_s * 1000),
            suspect=suspect,
        )

    def _deliver_event(self, event: DeliveryEvent) -> None:
        # %r: the alias of a rejected request comes from its topic, which may hold any character
        log.info(
            "delivery %s to %r: %s%s",
            event.id,
            event.target,
            event.state.value,
            f" ({event.reason})" if event.reason else "",
        )
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
        """A message of a channel the configuration ignores, or that is no target's while only
        those are published (SEC-012): neither published nor answered."""
        if message.kind is not MessageKind.CHANNEL:
            return False
        messages = self.config.messages
        if message.channel_index in messages.ignore_channels:
            return True
        return messages.publish_channels == "targets" and (
            message.channel_index not in _target_channels(self.config)
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
        if command is None and not self._received.allow(message.kind, origin, self._mono()):
            return  # judged, but neither published nor recorded: one sender or channel floods
        if self.config.messages.clean_received:  # judged as it came, published cleaned
            message = replace(message, text=clean_received(message.text))
        sender = None
        if message.sender_prefix is not None:
            matches = session.contacts.resolve(message.sender_prefix)
            sender = matches[0] if len(matches) == 1 else None
        target = self._target_for(message, sender)
        # %r: names come from the mesh and must not forge log lines or terminal escapes
        log.info(
            "received %s message from %r (%d bytes)%s",
            message.kind.value,
            sender.name if sender else message.sender_prefix or message.channel_index,
            len(message.text.encode()),
            f", command {decision.word}: {'accepted' if command else decision.rejection}"
            if decision
            else "",
        )
        log.debug("text: %r", message.text)
        payload = received_payload(message, sender, target, decision)
        self._bus.publish(self.topics.received, encode(payload))
        if sender is not None and message.kind is MessageKind.CONTACT:
            self._bus.publish(self.topics.contact_received(sender.public_key), encode(payload))
        self._observe(self.observer.message, message, sender)
        if (
            self._telemetry is not None
            and sender is not None
            and not event.at_connect
            and message.kind is MessageKind.CONTACT
        ):
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

    def _decide(self, message: InboundMessage, session: RadioPort) -> CommandDecision | None:
        now = self._mono()
        self._seen = {k: at for k, at in self._seen.items() if now - at < LOOK_ALIKE_MEMORY_S}
        return evaluate_command(
            message,
            self.config.commands,
            session.contacts,
            self._ledger.view,
            self._wall(),
            seen=self._seen,
        )

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
            self._received.report(self._mono())  # the end of a flood of received messages
            self._contacts.report_unpublished()
            self._observe(self.observer.exclusions.flush)  # an exclusion not stored yet
            self._observe(self.observer.tick)
            self._ledger.store.flush()  # what the sections changed since the last tick (ARC-004)
            if self._mono() >= next_state:
                self._publish_state()
                next_state = self._mono() + STATUS_INTERVAL_S
            await asyncio.sleep(tick)

    async def _lag_loop(self) -> None:
        """How late the event loop runs (CODE-001): a host that gives the gateway no CPU, or a
        disk that holds it up, shows here before it shows as a radio that does not answer."""
        loop = asyncio.get_running_loop()
        while True:
            due = loop.time() + LAG_TICK_S
            await asyncio.sleep(LAG_TICK_S)
            now = loop.time()
            if self._health.lag.record(now - due, now):
                log.warning(
                    "the event loop was held up for %.1f s: the host gave the gateway no CPU, or "
                    "its disk was slow (a backup, swapping)",
                    now - due,
                )

    def state_payload(self) -> dict[str, Any]:
        info = self._radio_info
        connected = self._radio_ready.is_set()
        battery = self._session.battery if (self._session and connected) else None
        offset = self._session.clock_offset_s if (self._session and connected) else None
        now = self._mono()
        return {
            "radio": "connected" if connected else "disconnected",
            "battery_v": round(battery.millivolts / 1000, 3) if battery else None,
            # the radio's clock minus the host's: it cannot be set back (SEC-002)
            "radio_clock_offset_s": offset,
            # over TCP: another client took the link in the last hour (SEC-003); null over USB
            "radio_link_contested": self._link.contested(self._wall()),
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
            "health": self._health_section(),
        }

    def _health_section(self) -> dict[str, Any]:
        """How the gateway itself is doing (ARC-012): `degraded` while any part is not as it
        should be, which the log tells in detail (docs/mqtt-api.md, State)."""
        history = self.observer.history
        history_state = (
            "off"
            if not self.config.history.enabled
            else "broken"
            if history is None or history.broken
            else "ok"
        )
        state_file = "unwritable" if self._ledger.store.failed else "ok"
        exclusions = self.observer.exclusions.health
        errors, lag = FAILURES.last_hour(), self._health.lag.worst()
        disk = _free_mb(self._health.state_dir)
        degraded = (
            history_state == "broken"
            or state_file != "ok"
            or exclusions != "ok"
            or errors > 0
            or (lag is not None and lag >= LAG_WARN_S)
            or (disk is not None and disk < DISK_LOW_MB)
        )
        return {
            "degraded": degraded,
            "history": history_state,
            "state_file": state_file,
            "exclusions": exclusions,
            "errors_1h": errors,
            "loop_lag_max_s": None if lag is None else round(lag, 3),
            "events_dropped_1h": self._bus.dropped_1h(),
            "disk_free_mb": disk,
        }

    def _telemetry_state(self) -> dict[str, Any]:
        off: TelemetryStatus = "off"
        state: dict[str, Any] = {
            "telemetry": off,
            "telemetry_reason": None,
            "telemetry_airtime_1h_s": None,
            "telemetry_mesh_airtime_1h_s": None,
        }
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
        degraded = ", degraded" if state["health"]["degraded"] else ""
        self._notify(f"STATUS=radio {state['radio']}, queue {state['queue']}{degraded}")

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
        if payload or retained or self._announcing.due is not None:
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
                    if self._components
                    else {}
                )
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
            self._announcing.last = self._mono()
            if (devices := self._devices) is not None:
                self._observe(lambda: devices.announce_all(birth=birth))
        self._publish_state()

    def _schedule_announcements(self, *, birth: bool = False) -> None:
        """Announce after a random delay. One already due is never postponed (a storm of birth
        messages would keep it from ever coming); it only learns that a birth came (SEC-006)."""
        if birth:
            self._count_birth()
        state = self._announcing
        if state.due is not None:
            state.birth = state.birth or birth
            return
        state.birth = birth
        delay = random.uniform(*DISCOVERY_DELAY_S)  # noqa: S311 - spreading load only
        state.due = asyncio.get_running_loop().call_later(delay, self._announce_due)

    def _announce_due(self) -> None:
        """At most one announcement every ANNOUNCE_MIN_INTERVAL_S: a later one waits for the
        rest of the interval, with whatever came meanwhile."""
        state = self._announcing
        wait = 0.0 if state.last is None else state.last + ANNOUNCE_MIN_INTERVAL_S - self._mono()
        if wait > 0:
            state.due = asyncio.get_running_loop().call_later(wait, self._announce_due)
            return
        birth, state.due = state.birth, None
        self._publish_announcements(birth=birth)

    def _count_birth(self) -> None:
        """Home Assistant sends one birth message per start: many in a minute come from another
        client, which the log names once a minute."""
        now, state = self._mono(), self._announcing
        births = state.births
        births.append(now)
        while births and now - births[0] > 60:
            births.popleft()
        if len(births) > BIRTHS_PER_MINUTE and (state.warned is None or now - state.warned > 60):
            state.warned = now
            log.warning(
                "%d birth messages on %s in a minute: another client writes there; the gateway "
                "announces itself at most every %d s",
                len(births),
                self.config.homeassistant.status_topic,
                ANNOUNCE_MIN_INTERVAL_S,
            )


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


def broker_warnings(config: Config) -> list[Text]:
    """What the broker connection lacks (SEC-004): the gateway's topics carry messages,
    positions and command events, and only the broker guards them. None once `insecure = true`
    accepts it. Logged in English, shown by `check-config` in the command line's language."""
    mqtt = config.mqtt
    warnings: list[Text] = []
    if mqtt.insecure:
        return warnings
    if mqtt.username is None:
        # Translators: security text (docs/translations.md); insecure = true is a setting
        warnings.append(
            Text(
                "MQTT without a user name: unless the broker refuses clients without one, any "
                "client that reaches it can read and write the gateway's topics (docs/mqtt-api.md, "
                "Securing the broker; insecure = true accepts it)"
            )
        )
    if not mqtt.tls and not mqtt.local:
        # Translators: security text (docs/translations.md); insecure = true is a setting
        warnings.append(
            Text(
                "MQTT without TLS: the password, the messages, the positions and the command "
                "events cross the network in the clear (insecure = true accepts it)"
            )
        )
    return warnings


def log_level_warnings(config: Config) -> list[Text]:
    """`debug` logs what the gateway sees: texts, positions, keys, every frame (OPS-017)."""
    if config.diagnostics.log_level != "debug":
        return []
    return [
        Text(
            "log level debug: the journal gets the texts of the messages, the positions, the keys "
            "and every frame of the radio, which journald keeps for weeks; set it back to info "
            "after the search (docs/configuration.md, diagnostics)"
        )
    ]


def start_warnings(config: Config) -> list[Text]:
    """What the service logs at its start and `check-config` shows."""
    return protected_look_alikes(config) + broker_warnings(config) + log_level_warnings(config)


def protected_look_alikes(config: Config) -> list[Text]:
    """Warnings about protected keys with one 6-byte prefix: the radio names a sender by its
    prefix only, so it can hold one of them (ADR 0009); the managed profile adds them in key
    order. Logged in English, shown by `check-config` in the command line's language."""
    roles: dict[str, list[Text]] = {}
    for target in config.targets:
        if target.public_key is not None:
            roles.setdefault(target.public_key, []).append(
                Text("target {alias}", alias=target.alias)
            )
    for key in config.commands.senders:
        roles.setdefault(key, []).append(Text("command sender"))
    keys = sorted(roles)

    def short(key: str) -> str:
        return f"{key[:12]}…{key[-8:]}"

    def named(key: str) -> Text:
        # Translators: a contact's key, shortened, and its roles, such as "target gate".
        return Text("{key} ({roles})", key=short(key), roles=tuple(roles[key]))

    profile = config.radio.profile
    writes = profile.protect_contacts and profile.manage != "observe"
    warnings = []
    for i, a in enumerate(keys):
        for b in (k for k in keys[i + 1 :] if k[:12] == a[:12]):
            if writes:  # in key order, and never next to the other
                # Translators: first and second are contacts, each a shortened key and its roles;
                # key is the first one's key. A security warning: the radio may hold the wrong one.
                warning = Text(
                    "protected contacts {first} and {second} have the same first 6 bytes: the "
                    "radio names a sender by them only (ADR 0009); the managed profile, when it "
                    "writes, adds {key} unless the other is on the radio, never both",
                    first=named(a),
                    second=named(b),
                    key=short(a),
                )
            else:
                # Translators: first and second are contacts, each a shortened key and its roles.
                warning = Text(
                    "protected contacts {first} and {second} have the same first 6 bytes: the "
                    "radio names a sender by them only (ADR 0009); the gateway adds neither, and "
                    "refuses requests to add either",
                    first=named(a),
                    second=named(b),
                )
            warnings.append(warning)
    return warnings


def protected_names(config: Config) -> dict[str, str]:
    """The protected contacts, each with a name for the radio profile to add it under while the
    radio lacks it, until its next advert names it (ADR 0020)."""
    names = {k: f"sender-{k[:4]}" for k in config.commands.senders}
    targets = {t.public_key: t.alias for t in config.targets if t.public_key is not None}
    return names | {key: radio_name(alias) for key, alias in targets.items()}


def _identity(info: RadioInfo | None) -> tuple[str, str, str] | None:
    return (info.device.model, info.device.version, info.public_key) if info else None


def _uptime(session: RadioPort) -> tuple[int | None, float | None]:
    """The radio's uptime in the session's last statistics, and when they were read."""
    stats = session.stats
    if stats is None or stats.core is None:
        return None, None
    return stats.core.uptime_s, stats.read_at


def _target_channels(config: Config) -> frozenset[int]:
    """The channels of `[targets.*]` entries."""
    return frozenset(t.channel_index for t in config.targets if t.channel_index is not None)


def _may_be_asked(advert: codec.Advert) -> bool:
    """A checked advert of a contact, or of a companion the radio did not add: added later,
    it is asked for telemetry while its advert is fresh."""
    record = advert.record
    return not advert.new or (record is not None and record.node_type == NodeType.CHAT)


def _free_mb(directory: Path | None) -> int | None:
    """Free space in MB where the state and the history are kept; None unknown."""
    if directory is None:
        return None
    try:
        return shutil.disk_usage(directory).free // 1_000_000
    except OSError:
        return None


def _key(target: Target) -> str:
    if target.public_key is None:
        raise ValueError(f"target {target.alias} has no public key")
    return target.public_key


def _shortest_ms(
    session: RadioPort,
    record: codec.ContactRecord | None,
    sent: codec.Sent,
    text: str,
    info: RadioInfo,
) -> float:
    """The shortest round trip of a genuine acknowledgement of an attempt the radio sent over a
    path through repeaters (SEC-001). 0 if it cannot be told: a flood or a neighbour, which a
    forger in range answers as soon, or a table that changed since it was read."""
    if record is None or sent.flood or not record.path_hops or session.contacts_outdated:
        return 0.0
    hops, path_bytes = record.path_hops, record.path_bytes or 0
    return 1000 * shortest_acknowledgement_s(len(text.encode()), hops, path_bytes, info.lora)
