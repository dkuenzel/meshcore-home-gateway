"""The observer (ADR 0014-0016): keeps the view of the mesh and the history, and publishes the
providers' documents.

The bridge hands it what the radio reports (contact tables, adverts, statistics, packet logs),
every received message and every final delivery. Documents are published retained, and only when
they changed; the topic of a contact that left the table is cleared, also when that happened while
the gateway was stopped (the published keys are remembered in the state file).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from meshcore_home_gateway.core.budget import RateLimit
from meshcore_home_gateway.core.contacts import Contact
from meshcore_home_gateway.core.mesh import (
    AirtimeMeter,
    Fix,
    Heard,
    Mesh,
    Node,
    Signal,
    TableChange,
    type_name,
)
from meshcore_home_gateway.core.model import (
    DeliveryEvent,
    DeliveryState,
    InboundMessage,
    MessageKind,
    Target,
    split_sender_name,
)
from meshcore_home_gateway.history import History, NodeRow
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.packet import parse_advert, parse_packet
from meshcore_home_gateway.radio.session import (
    AdvertHeard,
    ContactsRead,
    PacketLogged,
    RadioEvent,
    StatsRead,
)
from meshcore_home_gateway.state import RetainedKeys
from meshcore_home_gateway.views import (
    HistoryEntry,
    RadioReadings,
    contact_document,
    contacts_document,
    encode,
    history_document,
    iso,
    nodes_document,
    stats_document,
)

log = logging.getLogger(__name__)

RECENT_MESSAGES = 50  # kept in memory for the history document
LOGGED_ADVERT_S = 10.0  # a logged advert is matched to the radio's push this long
PURGE_INTERVAL_S = 3600.0
NODE_EVENTS_PER_S, NODE_EVENTS_BURST = 0.2, 10.0  # spoofed keys must not flood the bus
_KEPT = frozenset({DeliveryState.CONFIRMED, DeliveryState.FAILED, DeliveryState.EXPIRED})


class Publisher(Protocol):
    def publish(self, topic: str, payload: bytes, retain: bool = False, qos: int = 1) -> None: ...


class Observer:
    def __init__(
        self,
        bus: Publisher,
        topics: Topics,
        *,
        history: History | None = None,
        retained: RetainedKeys | None = None,
        protected: frozenset[str] = frozenset(),
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.mesh = Mesh()
        self._bus = bus
        self._topics = topics
        self._history = history
        self._retained = retained
        self._protected = protected
        self._wall = wall_clock
        self._meter = AirtimeMeter()
        self._readings: RadioReadings | None = None
        self._tuning: codec.TuningParams | None = None
        self._recent: deque[HistoryEntry] = deque(maxlen=RECENT_MESSAGES)
        self._logged: dict[str, tuple[float, Signal]] = {}  # key -> logged advert's signal
        self._published: dict[str, bytes] = {}  # topic -> payload last published
        self._first_read = True
        self._node_events = RateLimit(NODE_EVENTS_PER_S, NODE_EVENTS_BURST, now=wall_clock())
        self._next_purge = 0.0
        self._heard_rows: list[NodeRow] = []  # from the history, until the first table read
        self._stored_fixes: dict[str, Fix] = {}  # the newest position in the history per contact
        # called after each table read, before the topics of removed contacts are cleared
        self.on_contacts: list[Callable[[Mesh], None]] = []
        self._restore()

    def _restore(self) -> None:
        """What the history knows from earlier runs: recent messages, positions, last heard."""
        if self._history is None:
            return
        self._recent.extend(reversed(self._history.recent_messages(RECENT_MESSAGES)))
        self._stored_fixes = self._history.last_fixes()
        fixes = dict(self._stored_fixes)
        self._heard_rows = self._history.nodes()
        for row in self._heard_rows:
            self.mesh.restore(row.key, row.heard, fixes.pop(row.key, None))
        for key, fix in fixes.items():
            self.mesh.restore(key, None, fix)

    # --- inputs ---------------------------------------------------------------------------------

    def radio_connected(self, max_contacts: int, tuning: codec.TuningParams | None) -> None:
        """The radio's table size and tuning, known once the session has started (its first
        table read may already have been reported)."""
        self.mesh.max_contacts = max_contacts
        self._tuning = tuning
        self._publish(
            self._topics.contacts, contacts_document(self.mesh, protected=self._protected)
        )
        self._publish(self._topics.stats, stats_document(self._readings, self._meter, tuning))

    def radio_event(self, event: RadioEvent) -> None:
        if isinstance(event, ContactsRead):
            self._contacts_read(event.records, event.read_at)
        elif isinstance(event, AdvertHeard):
            self._advert(event.advert, event.received_at)
        elif isinstance(event, StatsRead):
            self._stats(event)
        elif isinstance(event, PacketLogged):
            self._packet(event.rx, event.received_at)

    def message(self, message: InboundMessage, sender: Contact | None) -> None:
        """A received message, after the bridge published it."""
        signal = Signal(message.hops, message.snr_db)
        text, name = message.text, sender.name if sender else None
        if message.kind is MessageKind.CHANNEL:
            name, text = split_sender_name(text)
        elif sender is not None:
            heard = Heard(message.received_at, "message", signal)
            if self.mesh.contact_heard(sender.public_key, heard):
                self._store_heard(sender.public_key, sender.name, self._type(sender), heard)
                self._publish_contact(sender.public_key)
        self._record(
            HistoryEntry(
                message.received_at, "in", message.kind.value, text,
                contact_key=sender.public_key if sender else None,
                channel_index=message.channel_index, name=name, hops=message.hops,
                snr_db=message.snr_db,
            )
        )  # fmt: skip

    def delivery(self, event: DeliveryEvent, target: Target | None) -> None:
        """A delivery event; final ones of messages that may have left go into the history."""
        channel = target is not None and target.kind is MessageKind.CHANNEL
        final = event.state in _KEPT or (channel and event.state is DeliveryState.SENT)
        if not final or event.text is None or target is None:
            return
        key = target.public_key
        contact = self.mesh.contacts.get(key) if key else None
        self._record(
            HistoryEntry(
                self._wall(), "out", target.kind.value, event.text, contact_key=key,
                channel_index=target.channel_index,
                name=contact.node.name if contact else target.alias, state=event.state.value,
            )
        )  # fmt: skip

    def publish_all(self) -> None:
        """Publish every document again, and clear the topics of contacts removed: the broker
        connected, and it may have lost them, or the adapter dropped them while it was away."""
        for topic, payload in list(self._published.items()):
            self._bus.publish(topic, payload, retain=True)
        self._publish(self._topics.history, history_document(self._recent), force=True)

    def tick(self) -> None:
        """Called about once a minute: forget stale heard nodes, purge the history hourly."""
        now = self._wall()
        if self.mesh.forget_nodes(now):
            self._publish(self._topics.nodes, nodes_document(self.mesh))
        if self._history is not None and now >= self._next_purge:
            self._history.purge(now)
            self._next_purge = now + PURGE_INTERVAL_S

    # --- contacts and nodes ---------------------------------------------------------------------

    def _contacts_read(self, records: Mapping[str, codec.ContactRecord], at: float) -> None:
        before = {key: tracked.node for key, tracked in self.mesh.contacts.items()}
        change = self.mesh.contacts_read((_node(r) for r in records.values()), at)
        if self._first_read:
            # compared with the contacts published in the last run, not with nothing: a start
            # announces only what changed while the gateway was stopped
            self._first_read = False
            self._take_heard_nodes()
            known = self._retained.keys if self._retained else frozenset(records)
            gone = tuple(sorted(known - set(records)))
            change = TableChange(tuple(k for k in change.added if k not in known), gone)
        for listener in self.on_contacts:
            try:
                listener(self.mesh)
            except Exception:  # a front end's bug must not stop the documents
                log.exception("contact listener failed")
        self._contact_events(change, before)
        for key in change.removed:
            self._clear(self._topics.contact(key))
        for key, tracked in self.mesh.contacts.items():
            self._store_fix(key, tracked.fix)
            self._publish_contact(key)
        self._publish(
            self._topics.contacts, contacts_document(self.mesh, protected=self._protected)
        )
        if self._retained is not None:
            self._retained.replace(set(self.mesh.contacts))

    def _take_heard_nodes(self) -> None:
        """Nodes from the history that are not contacts become heard nodes again."""
        for row in self._heard_rows:
            if row.key not in self.mesh.contacts:
                node = Node(row.key, row.name, row.node_type)
                self.mesh.node_heard(node, row.heard)
                tracked = self.mesh.nodes.get(row.key)
                if tracked is not None:
                    tracked.first_heard = row.first_heard
        self._heard_rows = []
        self.mesh.forget_nodes(self._wall())
        self._publish(self._topics.nodes, nodes_document(self.mesh))

    def _contact_events(self, change: TableChange, before: Mapping[str, Node]) -> None:
        for kind, keys in (("added", change.added), ("removed", change.removed)):
            for key in keys:
                tracked = self.mesh.contacts.get(key)
                node = tracked.node if tracked else before.get(key)
                name = node.name if node else None
                node_type = type_name(node.node_type) if node else None
                log.info("contact %s: %r (%s)", kind, name, key[:12])
                self._event(self._topics.contact_event, {
                    "event_type": kind, "key": key, "name": name, "type": node_type,
                })  # fmt: skip

    def _advert(self, advert: codec.Advert, at: float) -> None:
        key = advert.public_key.hex()
        logged_at, signal = self._logged.pop(key, (0.0, Signal()))
        if at - logged_at > LOGGED_ADVERT_S:
            signal = Signal()
        heard = Heard(at, "advert", signal)
        if advert.record is None:  # a contact: its record follows with the next table read
            if self.mesh.contact_heard(key, heard):
                tracked = self.mesh.contacts[key]
                self._store_heard(key, tracked.node.name, tracked.node.node_type, heard)
                self._publish_contact(key)
            return
        node = _node(advert.record)
        new = self.mesh.node_heard(node, heard)
        self._store_heard(key, node.name, node.node_type, heard)
        if key in self.mesh.contacts:
            self._publish_contact(key)
            return
        self._publish(self._topics.nodes, nodes_document(self.mesh))
        if new and self._node_events.allow(at):
            self._event(self._topics.node_event, {
                "event_type": "new", "key": key, "prefix": key[:12], "name": node.name,
                "type": type_name(node.node_type), "hops": signal.hops, "snr_db": signal.snr_db,
                "rssi_dbm": signal.rssi_dbm, "heard_at": iso(at),
            })  # fmt: skip

    def _packet(self, rx: codec.RxLog, at: float) -> None:
        """An advert in the packet log: its signal, for the radio's push that follows."""
        packet = parse_packet(rx.raw)
        advert = parse_advert(packet) if packet else None
        if packet is None or advert is None:
            return
        hops = len(packet.path_hashes) if packet.flood else None
        self._logged = {k: v for k, v in self._logged.items() if at - v[0] <= LOGGED_ADVERT_S}
        self._logged[advert.public_key.hex()] = (at, Signal(hops, rx.snr_db, rx.rssi_dbm))

    def _store_heard(self, key: str, name: str, node_type: int, heard: Heard) -> None:
        if self._history is not None:
            self._history.node_heard(key, name, node_type, heard)

    def _store_fix(self, key: str, fix: Fix | None) -> None:
        if fix is None or self._history is None or self._stored_fixes.get(key) == fix:
            return
        self._history.add_position(key, fix)
        self._stored_fixes[key] = fix

    def _type(self, contact: Contact) -> int:
        tracked = self.mesh.contacts.get(contact.public_key)
        return tracked.node.node_type if tracked else 0

    def _publish_contact(self, key: str) -> None:
        tracked = self.mesh.contacts.get(key)
        if tracked is not None:
            document = contact_document(tracked, protected=key in self._protected)
            self._publish(self._topics.contact(key), document)

    # --- radio ----------------------------------------------------------------------------------

    def _stats(self, event: StatsRead) -> None:
        self._readings = RadioReadings(
            event.battery, event.core, event.radio, event.packets, event.read_at
        )
        if event.radio is not None:
            self._meter.add(event.read_at, event.radio.tx_airtime_s)
        self._publish(self._topics.stats, stats_document(self._readings, self._meter, self._tuning))

    # --- history and publishing ---------------------------------------------------------------

    def _record(self, entry: HistoryEntry) -> None:
        self._recent.append(entry)
        if self._history is not None:
            self._history.add_message(entry)
        self._publish(self._topics.history, history_document(self._recent))

    def _publish(self, topic: str, document: dict[str, Any], *, force: bool = False) -> None:
        payload = encode(document)
        if not force and self._published.get(topic) == payload:
            return
        self._published[topic] = payload
        self._bus.publish(topic, payload, retain=True)

    def _clear(self, topic: str) -> None:
        self._published[topic] = b""  # an empty retained message clears the topic (again)
        self._bus.publish(topic, b"", retain=True)

    def _event(self, topic: str, payload: dict[str, Any]) -> None:
        self._bus.publish(topic, encode(payload))


def _node(record: codec.ContactRecord) -> Node:
    return Node(
        public_key=record.public_key.hex(),
        name=record.name,
        node_type=record.node_type,
        flags=record.flags,
        path_hops=record.path_hops,
        last_advert=record.last_advert,
        position=record.position,
    )
