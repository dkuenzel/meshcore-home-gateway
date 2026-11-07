"""The outbox (design §6.2, ADR 0028, ARC-009): the queue of outgoing messages, and their delivery.

Messages wait in the queue for the radio and for airtime, alerts before the others (IMP-032), each
kind in the order it came. A message taken out keeps its progress (its radio timestamp, the
attempts sent, their acknowledgement codes), so that it can be put back: paused for an alert, or
kept for the next start when the service stops (ARC-013).

The delivery takes the send requests from the bus, refuses or queues each, and delivers one message
at a time: an attempt when the radio is there and the airtime budget allows it, until it is
confirmed, sent to a channel, or out of attempts or time. Every step goes out on the delivery
topic and to the observer, and the messages accepted and not final yet, the one being delivered
included, go to the observer as the `pending` document after each step (#112).
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core.airtime import (
    channel_message_bytes,
    direct_attempt_airtime_s,
    shortest_acknowledgement_s,
    time_on_air_s,
)
from meshcore_home_gateway.core.budget import HOUR_S, AirtimeBudget, Deduplicator, RateLimit
from meshcore_home_gateway.core.contacts import PREFIX_HEX_DIGITS, Contact, is_key
from meshcore_home_gateway.core.model import DeliveryEvent, DeliveryState, MessageKind, Target
from meshcore_home_gateway.core.text import (
    TextPolicy,
    TextRejectedError,
    prepare_text,
    text_limit,
    with_nonce,
    without_nonce,
)
from meshcore_home_gateway.health import FAILURES
from meshcore_home_gateway.mqtt.payloads import delivery_payload, encode
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer, observe
from meshcore_home_gateway.ports import (
    Bus,
    CommandFailedError,
    RadioError,
    RadioInfo,
    RadioPort,
    SendRequest,
)
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codec import MAX_TEXT_BYTES
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.radio_link import RadioLink
from meshcore_home_gateway.state import AirtimeLevel, JsonStateFile, OutboxRecords, SavedMessage
from meshcore_home_gateway.telemetry_poller import OutboxActivity
from meshcore_home_gateway.views import PendingEntry

log = logging.getLogger(__name__)
log.addFilter(FAILURES)  # caught bugs counted, their tracebacks thinned out

MAX_REQUEST_BYTES = 1024
# on stop, the acknowledgement of the message in flight is awaited this long, the longest wait of
# an attempt; within the unit's TimeoutStopSec (90 s) with the rest of the stop (ARC-013)
STOP_OUTBOX_S = 60.0
ACK_TIMEOUT_FACTOR = 1.2  # like the MeshCore Python client
ACK_TIMEOUT_MIN_S, ACK_TIMEOUT_MAX_S = 5.0, 60.0
RECENT_ACK_S = 120.0
REFUSED_EVENTS_PER_S, REFUSED_EVENTS_BURST = 20.0, 100.0  # events of requests refused at once
REFUSED_REPORT_INTERVAL_S = 60.0  # the log counts the events left out at most this often
MAX_ECHOED_ALIAS = 64  # characters of a refused request's topic level in its event (SEC-017)


@dataclass
class Expected:
    """The acknowledgement codes of a message's attempts, and the shortest round trip a genuine
    acknowledgement of any of them takes (SEC-001): 0 where it cannot be told."""

    codes: list[bytes] = field(default_factory=list)
    shortest_ms: float | None = None  # None: no attempt sent yet

    def add(self, code: bytes, shortest_ms: float) -> None:
        self.codes.append(code)
        if self.shortest_ms is None or shortest_ms < self.shortest_ms:
            self.shortest_ms = shortest_ms

    def suspect(self, round_trip_ms: int) -> bool:
        return self.shortest_ms is not None and round_trip_ms < self.shortest_ms


@dataclass
class Outgoing:
    id: str
    target: Target
    text: str
    created: float  # monotonic
    raw: str | None = None  # a channel text accepted before the radio's name was known
    alert: bool = False  # it overtakes the messages that are not (`[messages] alert_targets`)
    timestamp: int | None = None  # of every attempt, from the first on: retries stay recognisable
    sent: int = 0  # attempts the radio has sent
    acks: Expected = field(default_factory=Expected)
    # its key in the duplicate check and when it was accepted there, if it holds one: forgotten
    # when it fails or expires, so that it can be sent again at once
    dedup: tuple[tuple[str, str], float] | None = None
    queued_at: float | None = None  # wall clock at its acceptance, kept over a stop (history)
    # its latest delivery event while it is on its way, for `pending`: `queued` (again after a
    # start), then `sent`; pausing it for an alert sends none and leaves it `sent`
    step: DeliveryState = DeliveryState.QUEUED


class Outbox:
    """At most `size` messages: `put` refuses one more. A message put back (paused, or kept at
    a stop) goes first among its kind, and may exceed the size: it had its place."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._heap: list[tuple[int, int, Outgoing]] = []  # (0 for an alert, order, message)
        self._order = itertools.count()
        self._front = itertools.count(-1, -1)
        self._ready = asyncio.Event()

    def qsize(self) -> int:
        return len(self._heap)

    @property
    def alert_waiting(self) -> bool:
        return bool(self._heap) and self._heap[0][0] == 0

    def put(self, item: Outgoing) -> None:
        """Raises asyncio.QueueFull when the queue holds `size` messages."""
        if len(self._heap) >= self._size:
            raise asyncio.QueueFull
        self._push(item, next(self._order))

    def put_back(self, item: Outgoing) -> None:
        self._push(item, next(self._front))

    def restore(self, item: Outgoing) -> None:
        """A message kept by the last run, in its order, also beyond the size."""
        self._push(item, next(self._order))

    async def get(self) -> Outgoing:
        while not self._heap:
            self._ready.clear()
            await self._ready.wait()
        return heapq.heappop(self._heap)[2]

    def take_all(self) -> list[Outgoing]:
        """Every message, in the order they would leave; the queue is empty then."""
        return [heapq.heappop(self._heap)[2] for _ in range(len(self._heap))]

    def _push(self, item: Outgoing, order: int) -> None:
        heapq.heappush(self._heap, (0 if item.alert else 1, order, item))
        self._ready.set()


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
            async with asyncio.timeout(timeout_s):  # not wait_for: see RadioLink.until
                return await future
        except TimeoutError:
            return None
        finally:
            for code in codes:
                if self._waiting.get(code) is future:
                    del self._waiting[code]


class Delivery:
    """Takes the send requests and delivers the queued messages one at a time (above): `budget`
    is the airtime budget, `acks` matches the acknowledgements the radio reports, `activity`
    tells the telemetry poller when the outbox has work."""

    def __init__(
        self,
        config: Config,
        bus: Bus,
        topics: Topics,
        *,
        radio: RadioLink,
        observer: Observer,
        store: JsonStateFile,
        monotonic: Callable[[], float],
        wall_clock: Callable[[], float],
    ) -> None:
        """`store`: the state file, for the messages a stop keeps and the budget's level."""
        self.config = config
        self._bus, self._topics, self._radio = bus, topics, radio
        self._observer, self._store = observer, store
        self._mono, self._wall = monotonic, wall_clock
        self._targets = {t.alias: t for t in config.targets}
        self._queue = Outbox(config.messages.queue_size)  # alerts first (IMP-032)
        self.activity = OutboxActivity(monotonic, self._queue.qsize)  # telemetry yields to it
        self.budget, self._airtime = self._message_budget()
        self._dedup = Deduplicator(config.messages.dedup_window_s)
        self._refusals = RateLimit(REFUSED_EVENTS_PER_S, REFUSED_EVENTS_BURST, now=monotonic())
        # events of refused requests left out since the last report, and when the next is due
        self._unpublished, self._next_report = 0, monotonic()
        self.acks = AckWaiter(monotonic)
        self._flight = _Flight()
        # MQTT 5: the correlation data of a request, by its id, until its final event (ARC-014)
        self._correlations: dict[str, bytes] = {}
        # the messages accepted and not final, by id, the oldest first: in the queue or in flight
        self._pending: dict[str, Outgoing] = {}
        self._pending_due = False  # `pending` goes out once this moment's steps are done
        self._kept = False  # the stop kept the queue: a request now would be lost (ADR 0028)

    def queued(self) -> int:
        return self._queue.qsize()

    def save_level(self) -> None:
        """The budget's level, for the next start (ARC-005)."""
        self._airtime.save(self.budget.available_s(self._mono()), self._wall())

    def _message_budget(self) -> tuple[AirtimeBudget, AirtimeLevel]:
        """The budget at the level the last run left (ARC-005), refilled since; at least what a
        long message needs, so that the first alert after a restart goes out at once."""
        airtime, level = self.config.airtime, AirtimeLevel(self._store)
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

    async def take_requests(self) -> None:
        while True:
            self.accept(await self._bus.requests.get())

    def accept(self, request: SendRequest, *, deduplicate: bool = True) -> DeliveryState:
        """Queue the request or refuse it, at once; its state: `queued`, `rejected` or
        `duplicate` (a draft's press keeps the draft unless it was queued). `deduplicate=False`:
        the answer to a command, which the inbox judged once; the same command sent again later
        deserves its answer again (EXT-009). Once the stop kept the queue, every request is
        `rejected` with `stopping`: nothing would keep it."""
        event_id = secrets.token_hex(4)
        if request.correlation is not None:
            self._correlations[event_id] = request.correlation
        try:
            target, text, raw = self._validate(request)
        except _FinalError as exc:
            # the topic level may be any length: the event echoes the start of it
            alias = request.alias[:MAX_ECHOED_ALIAS]
            event = DeliveryEvent(event_id, alias, exc.state, reason=exc.reason)
            self._deliver_refusal(event)
            return exc.state
        state, reason, queued_at = DeliveryState.QUEUED, None, None
        key, now = (target.alias, text), self._mono()
        if deduplicate and self._dedup.is_duplicate(key, now):
            state = DeliveryState.DUPLICATE
        else:
            nonce = target.kind is MessageKind.CONTACT and self.config.messages.confirm_nonce
            sent = with_nonce(text) if nonce else text  # duplicates are judged without it
            alert = target.alias in self.config.messages.alert_targets
            dedup = (key, now) if deduplicate else None
            queued_at = self._wall()  # for the history: `now` is monotonic
            item = Outgoing(
                event_id, target, sent, now, raw, alert=alert, dedup=dedup, queued_at=queued_at
            )
            try:
                self._queue.put(item)
                self._pending[event_id] = item
                self.activity.touch()
                text = sent
            except asyncio.QueueFull:
                if deduplicate:  # not taken, so a retry must not count as duplicate
                    self._dedup.forget(key)
                state, reason, queued_at = DeliveryState.REJECTED, "queue_full", None
        event = DeliveryEvent(
            event_id, target.alias, state, text=text, reason=reason, queued_at=queued_at
        )
        if state is DeliveryState.QUEUED:
            self._deliver_event(event)
        else:
            self._deliver_refusal(event)
        return state

    def _deliver_refusal(self, event: DeliveryEvent) -> None:
        """The event of a request refused at once (rejected, duplicate), at a bounded rate: a
        flood of requests must not become a flood of events, which would push other events out
        of a subscriber's queue on the broker. Queued requests keep all their events."""
        if self._refusals.allow(self._mono()):
            self._deliver_event(event)
            return
        self._correlations.pop(event.id, None)
        self._unpublished += 1
        self.report_unpublished()

    def report_unpublished(self) -> None:
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
        if self._kept:  # a draft's press or a command's answer can still come in the stop
            raise _FinalError(DeliveryState.REJECTED, "stopping")
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
            text = prepare_text(
                raw,
                self._text_policy(target, self._radio.info),
                prefix=target.prefix,
                suffix=target.suffix,
            )
        except TextRejectedError as exc:
            raise _FinalError(DeliveryState.REJECTED, exc.reason) from None
        name_unknown = target.kind is MessageKind.CHANNEL and self._radio.info is None
        return target, text, raw if name_unknown else None

    def _contact_target(self, key: str) -> Target:
        """A companion of the radio's table as last read, as a recipient (ADR 0017); its alias
        is its full key. Repeaters, room servers and sensors do not take texts from it."""
        tracked = self._observer.mesh.contacts.get(key) if is_key(key) else None
        if tracked is None:  # also a key in upper case or a prefix: keys as the gateway writes them
            raise _FinalError(DeliveryState.REJECTED, "unknown_contact")
        if tracked.node.node_type != NodeType.CHAT:
            raise _FinalError(DeliveryState.REJECTED, "not_a_companion")
        return Target(key, MessageKind.CONTACT, public_key=key)

    def target_of(self, alias: str) -> Target | None:
        target = self._targets.get(alias)
        if target is None and is_key(alias):
            return Target(alias, MessageKind.CONTACT, public_key=alias)
        return target

    def _text_policy(self, target: Target, info: RadioInfo | None) -> TextPolicy:
        """Without the radio's name, a channel text gets the limit of the shortest one. A direct
        text leaves room for the random suffix of `confirm_nonce` (SEC-001)."""
        messages = self.config.messages
        limit = text_limit(
            MAX_TEXT_BYTES,
            channel=target.kind is MessageKind.CHANNEL,
            radio_name=info.name if info else None,
            nonce=messages.confirm_nonce,
        )
        return TextPolicy(limit, ascii_only=messages.ascii_only, truncate=messages.truncate)

    async def run(self) -> None:
        flight = self._flight
        flight.idle.set()
        while True:
            item = await self._queue.get()
            if flight.stopping:  # no attempt starts any more: kept for the next start
                self._queue.put_back(item)
                return
            flight.item = item
            flight.idle.clear()
            self.activity.delivering(True)  # no await before: telemetry sees it at once
            try:
                await self._deliver(item)
            except asyncio.CancelledError:
                self._queue.put_back(item)  # the stop: kept for the next start
                raise
            except Exception:
                log.exception("delivery %s failed unexpectedly", item.id)
                self._end(item, self._event(item, DeliveryState.FAILED, reason="internal_error"))
            finally:
                flight.item = None
                flight.idle.set()
                self.activity.delivering(False)

    async def _deliver(self, item: Outgoing) -> None:
        """Attempts until the message is confirmed, ends with a reason, or runs out of time.

        `expired` means the message never left: once an attempt reached the radio, running out
        of time or airtime ends it as `failed` (no acknowledgement). An attempt interrupted by a
        radio error is repeated, with the same number, when the radio is back. `attempt` in a
        final event counts the transmissions so far. Between two attempts the message is put
        back, with its progress, when an alert waits behind it (IMP-032) or the service stops
        (ARC-013).
        """
        deadline = item.created + self.config.messages.queue_ttl_s
        if item.timestamp is None:  # one timestamp for all attempts: retries stay recognisable
            item.timestamp = int(self._wall())
        attempts = self._attempts(item.target)
        while item.sent < attempts:
            if item.sent and (
                self._flight.stopping or (not item.alert and self._queue.alert_waiting)
            ):
                log.info("delivery %s: paused after attempt %d", item.id, item.sent)
                self._queue.put_back(item)
                return
            session = await self._radio.until(deadline)
            if session is None:
                self._end(item, self._out_of_time(item, item.sent))
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
                self._end(item, outcome)
                return
        failed = self._event(
            item, DeliveryState.FAILED, attempt=item.sent, reason="no_acknowledgement"
        )
        self._end(item, failed)

    def _attempts(self, target: Target) -> int:
        """A direct message is sent until it is acknowledged; a channel message once."""
        return self.config.messages.max_attempts if target.kind is MessageKind.CONTACT else 1

    def _end(self, item: Outgoing, event: DeliveryEvent) -> None:
        """The last event of a message. One that failed or expired no longer counts as a
        duplicate: "send it again" goes out at once. A confirmed one still does."""
        if event.state in (DeliveryState.FAILED, DeliveryState.EXPIRED) and item.dedup:
            self._dedup.forget(*item.dedup)
        self._deliver_event(event)

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
            reason = "queue_ttl" if self._radio.ready.is_set() else "no_radio"
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
        if airtime > self.budget.capacity_s:
            raise _FinalError(DeliveryState.FAILED, "exceeds_airtime_burst")
        wait = self.budget.wait_s(airtime, self._mono())
        if self._mono() + wait > deadline:
            raise _FinalError(DeliveryState.EXPIRED, "airtime_budget")
        round_trip = None
        if wait > 0:
            log.info("delivery %s waits %.1f s for airtime", item.id, wait)
            if acks.codes:  # an earlier attempt may still be acknowledged meanwhile
                round_trip = await self.acks.wait(acks.codes, wait)
            else:
                await asyncio.sleep(wait)
        if acks.codes and round_trip is None:
            round_trip = self.acks.take(acks.codes)
        if round_trip is not None:  # an earlier attempt arrived after all: send nothing more
            return self._confirmed(item, round_trip)
        if wait > 0 and await self._read_changed_contacts(session, target):
            self._resolve(session, target)  # a look-alike may have come while waiting
        if reset_path is not None:
            await session.reset_path(reset_path)  # only now: this attempt will be sent and flood
        self.budget.consume(airtime, self._mono())
        # at once: a crash before the next start must not give it this airtime again (ARC-005)
        self._airtime.save(self.budget.available_s(self._mono()), self._wall())
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
            round_trip = await self.acks.wait(item.acks.codes, timeout)
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
                    final = prepare_text(
                        item.raw,
                        self._text_policy(target, info),
                        prefix=target.prefix,
                        suffix=target.suffix,
                    )
                except TextRejectedError as exc:
                    raise _FinalError(DeliveryState.FAILED, exc.reason) from None
                if final != item.text:  # deduplicate on the text that is sent
                    key, now = (target.alias, final), self._mono()
                    moved = self._dedup.rekey((target.alias, item.text), key, now)
                    if item.dedup is not None:
                        item.dedup = (key, now) if moved else None
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
            queued_at=item.queued_at,
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
        self._bus.publish(
            self._topics.delivery,
            encode(delivery_payload(event)),
            correlation=self._correlation(event),
        )
        observe(self._observer.delivery, event, self.target_of(event.target))  # the history
        if (item := self._pending.get(event.id)) is not None:  # a step of a message on its way
            if _is_final(event.state, item.target):
                del self._pending[event.id]
            else:
                item.step = event.state
            self._pending_changed()

    def _correlation(self, event: DeliveryEvent) -> bytes | None:
        """The correlation data of the event's request; forgotten with its final event. A message
        kept across a restart has none: the data was the requester's, not the gateway's."""
        if event.id not in self._correlations:
            return None
        final = _is_final(event.state, self.target_of(event.target))
        return self._correlations.pop(event.id) if final else self._correlations[event.id]

    def _pending_changed(self) -> None:
        """`pending` goes out when the steps of this moment are done: after the history entry of
        a message that became final, so that no reader misses it between the two documents
        (MQTT keeps the order only within a topic), and once for a burst of requests."""
        if not self._pending_due:
            self._pending_due = True
            asyncio.get_running_loop().call_soon(self.publish_pending)

    def publish_pending(self) -> None:
        """The messages on their way, the oldest first, to the observer for `pending`."""
        self._pending_due = False
        entries = [self._pending_entry(item) for item in self._pending.values()]
        observe(self._observer.pending, entries)

    def _pending_entry(self, item: Outgoing) -> PendingEntry:
        """As the history will name it: a message to `contacts/<key>/send` has no target (its
        alias is the key), and the recipient is the contact's name, else the alias."""
        target, key = item.target, item.target.public_key
        contact = self._observer.mesh.contacts.get(key) if key else None
        return PendingEntry(
            item.id,
            item.queued_at,
            None if target.alias == key else target.alias,
            target.kind.value,
            key,
            target.channel_index,
            contact.node.name if contact else target.alias,
            item.text,
            item.step.value,
            item.sent,
            self._attempts(target),
        )

    async def finish(self, loop: asyncio.Task[None]) -> None:
        """No attempt starts any more; one that waits for its acknowledgement gets it, for at
        most STOP_OUTBOX_S, then `loop` (the task that runs `run`) ends. The messages still
        queued, that one too if it needs another attempt, are kept for the next start (ARC-013):
        requests are taken until then, and refused after."""
        flight = self._flight
        flight.stopping = True
        if flight.acking:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(STOP_OUTBOX_S):
                    await flight.idle.wait()
        loop.cancel()
        await asyncio.wait([loop])
        self._kept = True
        now, kept = self._mono(), self._queue.take_all()
        OutboxRecords(self._store).save(
            [
                SavedMessage(
                    item.id,
                    item.target.alias,
                    item.text,
                    item.raw,
                    now - item.created,
                    item.timestamp,
                    item.sent,
                    item.queued_at,
                )
                for item in kept
            ],
            self._wall(),
        )
        if kept:
            log.info("%d messages kept for the next start", len(kept))

    def restore(self) -> None:
        """The messages the last stop kept: those with time left are queued again, `queued`
        again with their `id`; the others end as they would have (ARC-013). Their texts count as
        sent for the duplicate check."""
        messages, elapsed = OutboxRecords(self._store).take(self._wall())
        config, now, queued = self.config.messages, self._mono(), 0
        for saved in messages:
            target = self.target_of(saved.target)
            age = saved.age_s + elapsed if elapsed is not None else config.queue_ttl_s
            if target is None:  # its target left the configuration
                event = DeliveryEvent(
                    saved.id,
                    saved.target,
                    DeliveryState.FAILED,
                    text=saved.text,
                    attempt=saved.sent,
                    reason="unknown_target",
                    queued_at=saved.queued_at,
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
                queued_at=saved.queued_at,
            )
            if age >= config.queue_ttl_s:  # it would have expired meanwhile
                self._deliver_event(self._out_of_time(item, item.sent, reason="queue_ttl"))
                continue
            nonce = target.kind is MessageKind.CONTACT and config.confirm_nonce
            key = (target.alias, without_nonce(item.text) if nonce else item.text)
            if not self._dedup.is_duplicate(key, now):
                item.dedup = (key, now)
            self._queue.restore(item)
            self._pending[item.id] = item
            queued += 1
            self._deliver_event(self._event(item, DeliveryState.QUEUED, attempt=item.sent))
        # the oldest first, as accepted: the queue keeps alerts and a message put back in front
        # (one of 2.0 has no acceptance time: it is older); published also when none is left
        self._pending = dict(sorted(self._pending.items(), key=lambda i: i[1].queued_at or 0.0))
        self._pending_changed()
        if messages:
            log.info(
                "messages kept by the last stop: %d queued again, %d at an end",
                queued,
                len(messages) - queued,
            )

    def check_radio(self, session: RadioPort) -> None:
        """At a connect: warn about targets the radio does not know and a burst that is too
        small."""
        info = session.info
        if info is not None:
            longest = direct_attempt_airtime_s(MAX_TEXT_BYTES, 0, True, info.lora)
            self._airtime.longest_s = longest  # the least a restart gets (ARC-005)
            if longest > self.budget.capacity_s:
                log.warning(
                    "a long direct message takes %.1f s of airtime at the radio's settings, more "
                    "than airtime.burst_s (%.1f s): such messages fail (exceeds_airtime_burst)",
                    longest,
                    self.budget.capacity_s,
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


def _is_final(state: DeliveryState, target: Target | None) -> bool:
    """The last event of a request: a refusal, an end, or `sent` to a channel."""
    if state is DeliveryState.SENT:
        return target is not None and target.kind is MessageKind.CHANNEL
    return state is not DeliveryState.QUEUED


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
