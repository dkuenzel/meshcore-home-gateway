"""Received messages (design §6.4, ARC-009): in the order the radio hands them over, without
delaying its acknowledgements; copies dropped, commands judged (ADR 0009), each published and
handed to the observer and the telemetry poller; per sender and per channel at a bounded rate
(SEC-012, SEC-013)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace

from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core.budget import Deduplicator, KeyedRateLimit
from meshcore_home_gateway.core.commands import (
    Command,
    CommandDecision,
    Rejection,
    command_word,
    evaluate_command,
)
from meshcore_home_gateway.core.contacts import PREFIX_HEX_DIGITS, Contact
from meshcore_home_gateway.core.model import InboundMessage, MessageKind
from meshcore_home_gateway.core.text import clean_received
from meshcore_home_gateway.health import FAILURES
from meshcore_home_gateway.mqtt.payloads import command_payload, encode, received_payload
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer, observe
from meshcore_home_gateway.ports import Bus, MessageReceived, RadioError, RadioPort
from meshcore_home_gateway.state import CommandLedger
from meshcore_home_gateway.telemetry_poller import OutboxActivity, TelemetryPoller

log = logging.getLogger(__name__)
log.addFilter(FAILURES)  # caught bugs counted, their tracebacks thinned out

REPORT_INTERVAL_S = 60.0  # the log counts the messages left out at most this often
# received messages that become events and history rows, per sender and per channel: a contact or
# a channel that floods cannot fill the bus, the history and Home Assistant's recorder; an
# accepted command always goes out (SEC-012, SEC-013)
RECEIVED_PER_SENDER_PER_S, RECEIVED_PER_SENDER_BURST = 0.5, 10.0
RECEIVED_PER_CHANNEL_PER_S, RECEIVED_PER_CHANNEL_BURST = 1.0, 20.0
INBOUND_DEDUP_S = 3600.0  # retries reach us as duplicates with the same timestamp and text
LOOK_ALIKE_MEMORY_S = 86_400.0  # a key with a sender's prefix counts this long after last seen
_FINAL_FROM_CACHE = frozenset(
    {
        Rejection.SENDER_AMBIGUOUS,
        Rejection.SENDER_NOT_ALLOWED,
        Rejection.REPLAY,
        Rejection.TOO_OLD,
        Rejection.FROM_THE_FUTURE,
        Rejection.CODE_MISSING,
        Rejection.CODE_INVALID,
        Rejection.CODE_REUSED,
    }
)


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
            self._next_report = now + REPORT_INTERVAL_S


class Inbox:
    """Handles the received messages one at a time, in the order `put` got them."""

    def __init__(
        self,
        config: Config,
        bus: Bus,
        topics: Topics,
        *,
        observer: Observer,
        ledger: CommandLedger,
        activity: OutboxActivity,
        telemetry: TelemetryPoller | None = None,
        reply: Callable[[str, str], None] | None = None,
        monotonic: Callable[[], float],
        wall_clock: Callable[[], float],
    ) -> None:
        """`activity`: a message that may get an answer keeps the telemetry poller waiting;
        `telemetry`: it hears of every companion that sent a message; `reply` sends a text to a
        contact's key (`[commands] reply`, EXT-009)."""
        self.config = config
        self._bus, self._topics, self._observer = bus, topics, observer
        self._ledger, self._activity, self._telemetry = ledger, activity, telemetry
        self._reply = reply
        self._mono, self._wall = monotonic, wall_clock
        self._targets = {t.alias: t for t in config.targets}
        self._messages: asyncio.Queue[MessageReceived] = asyncio.Queue()
        # keys with the 6-byte prefix of an allowed command sender, as the radio named them:
        # key -> when last seen; only these are kept, so the memory stays small (ADR 0009)
        self._watched = frozenset(key[:PREFIX_HEX_DIGITS] for key in config.commands.senders)
        self._seen: dict[str, float] = {}
        self._received = ReceivedLimit(monotonic())
        # a copy passes the age check until 2 x max_age_s after the original (the sender's clock
        # may be max_age_s ahead): the duplicate memory must last as long (ADR 0009)
        self._inbound_dedup = Deduplicator(
            max(INBOUND_DEDUP_S, 2 * config.commands.max_age_s + 60.0)
        )

    def put(self, event: MessageReceived) -> None:
        """A message from the radio's events: handled in order, after those before it."""
        if not self._ignored(event.message):  # an automation's answer goes first
            self._activity.touch()
        self._messages.put_nowait(event)

    async def drained(self) -> None:
        """Every message put is handled."""
        await self._messages.join()

    def report(self) -> None:
        """The end of a flood of received messages, in the log."""
        self._received.report(self._mono())

    async def run(self) -> None:
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
            message.channel_index not in target_channels(self.config)
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
                self._ledger.record(command.sender_key, command.sender_timestamp, command.code_step)
            except OSError:
                log.exception("command not published: the state file cannot be written")
                decision = CommandDecision(
                    decision.word,
                    rejection=Rejection.STATE_UNWRITABLE,
                    sender_key=command.sender_key,
                )
                command = None
        if decision is not None and command is not None and not self._bus.connected:
            # queued until the reconnect, it would trigger automations at any later time
            log.warning("command %s refused: the broker is unreachable", decision.word)
            decision = CommandDecision(
                decision.word, rejection=Rejection.BROKER_UNAVAILABLE, sender_key=command.sender_key
            )
            command = None
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
        self._bus.publish(self._topics.received, encode(payload))
        if sender is not None and message.kind is MessageKind.CONTACT:
            self._bus.publish(self._topics.contact_received(sender.public_key), encode(payload))
        observe(self._observer.message, message, sender, target)
        if (
            self._telemetry is not None
            and sender is not None
            and not event.at_connect
            and message.kind is MessageKind.CONTACT
        ):
            # as it arrived; one fetched at connect may have waited in the radio for hours
            observe(self._telemetry.heard, sender.public_key)
        if command is not None:
            name = sender.name if sender else None
            # QoS 0: an event that cannot be delivered now is lost, never delivered late (after
            # a broker loss that paho has not noticed yet); the sender gets no answer and can
            # send the command again
            self._bus.publish(
                self._topics.command,
                encode(command_payload(command, name, message.received_at)),
                qos=0,
            )
        if decision is not None:
            self._answer(decision, command)

    def _answer(self, decision: CommandDecision, command: Command | None) -> None:
        """`[commands] reply`: the sender learns whether its command was published."""
        text = self.config.commands.reply_text(decision)
        reply_to = command.sender_key if command is not None else decision.sender_key
        if self._reply is not None and text is not None and reply_to is not None:
            self._reply(reply_to, text)

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
            last_codes=self._ledger.code_steps,
        )

    def contact_key(self, key: str, held: bool) -> None:
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


def target_channels(config: Config) -> frozenset[int]:
    """The channels of `[targets.*]` entries."""
    return frozenset(t.channel_index for t in config.targets if t.channel_index is not None)
