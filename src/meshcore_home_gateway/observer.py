"""The observer (ADR 0014-0016): keeps the view of the mesh and the history, and publishes the
providers' documents.

The bridge hands it what the radio reports (contact tables, adverts, statistics, packet logs),
every received message and every final delivery, and the telemetry poller its answers and the
companions' states. Documents are published retained, and only when they changed; the topic of a
contact that left the table is cleared, also when that happened while the gateway was stopped (the
published keys are remembered in the state file). It holds the contacts the household excluded
from telemetry, for the contact manager and the poller (ADR 0018).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core.budget import RateLimit
from meshcore_home_gateway.core.contacts import Contact
from meshcore_home_gateway.core.mesh import (
    AirtimeMeter,
    Fix,
    Heard,
    Mesh,
    Node,
    Readings,
    Signal,
    TableChange,
    Tracked,
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
from meshcore_home_gateway.core.profile import ProfileReport
from meshcore_home_gateway.core.telemetry import Answer, TelemetryEvent, TelemetryState
from meshcore_home_gateway.history import History, NodeRow
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.protocol.packet import parse_advert, parse_packet
from meshcore_home_gateway.radio.session import (
    AdvertHeard,
    ContactsRead,
    PacketLogged,
    PathReset,
    RadioEvent,
    StatsRead,
)
from meshcore_home_gateway.state import CurrentDocuments, RetainedKeys, TelemetryExclusions
from meshcore_home_gateway.views import (
    SETTING_NAMES,
    HistoryEntry,
    RadioReadings,
    contact_document,
    contact_event,
    contacts_document,
    encode,
    history_document,
    iso,
    look_alike_nodes,
    nodes_document,
    restated,
    setting_value,
    settings_document,
    stats_document,
    telemetry_event,
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
        manage: bool = False,
        wall_clock: Callable[[], float] = time.time,
        exclusions: TelemetryExclusions | None = None,
        current: CurrentDocuments | None = None,
    ) -> None:
        """`manage`: contact add and remove requests are taken (ADR 0017). `exclusions`: the
        contacts excluded from telemetry; the contact manager and the poller read these.
        `current`: the documents of the companions whose answers are current, kept across a
        restart."""
        self.mesh = Mesh()
        # the one holder: an empty set is falsy, so `or` would replace one that was given
        self.exclusions = exclusions if exclusions is not None else TelemetryExclusions(None)
        # the telemetry state of a companion as the poller sees it; None: telemetry is off
        self.telemetry_state: Callable[[str], TelemetryState | None] | None = None
        # called after an exclusion (True) or an inclusion (False) of a key: the poller
        self.on_telemetry_exclusion: list[Callable[[str, bool], None]] = []
        # called with each key a table read added (at the first, only one the last run did not
        # have), before its document is published: the poller
        self.on_contact_added: list[Callable[[str], None]] = []
        self.manage = manage
        self._bus = bus
        self._topics = topics
        self._history = history
        self._retained = retained
        self._current_store = current
        # key -> its last document while its answers count as current: before the first table
        # read the documents of the last run, in their latest state also once expired (ADR 0024)
        self._current = current.documents if current is not None else {}
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
        # called after each table read, before the topics of removed contacts are cleared, and
        # when a contact has its first position
        self.on_contacts: list[Callable[[Mesh], None]] = []
        # called whenever the heard nodes may have changed; True: a contact joined or left
        self.on_nodes: list[Callable[[bool], None]] = []
        self._profiles: dict[str, ProfileReport] = {}  # radio key -> the profile's last report
        # contact requests whose write went out: (event type, key) -> (request, id); the table
        # read that shows the change reports it as the request's result
        self._expected: dict[tuple[str, str], tuple[str, str]] = {}
        self._restore()

    def _restore(self) -> None:
        """What the history knows from earlier runs: recent messages, positions, last heard, the
        readings of each contact's newest answer. An excluded contact gets back neither its
        readings nor a position from telemetry, and while the exclusions cannot be read no
        contact does (they may exclude anyone)."""
        if self._history is None:
            return
        self._recent.extend(reversed(self._history.recent_messages(RECENT_MESSAGES)))
        self._stored_fixes = self._history.last_fixes()
        fixes = dict(self._stored_fixes)
        readings = self._history.last_readings()
        self._heard_rows = self._history.nodes()
        for row in self._heard_rows:
            self._restore_contact(row.key, row.heard, fixes.pop(row.key, None),
                                  readings.pop(row.key, None))  # fmt: skip
        for key, fix in fixes.items():
            self._restore_contact(key, None, fix, readings.pop(key, None))
        for key, kept in readings.items():
            self._restore_contact(key, None, None, kept)

    def _restore_contact(
        self, key: str, heard: Heard | None, fix: Fix | None, readings: Readings | None
    ) -> None:
        if key in self.exclusions or self.exclusions.unreadable:
            readings = None
            fix = None if fix is not None and fix.source == "telemetry" else fix
        self.mesh.restore(key, heard, fix, readings)

    # --- inputs ---------------------------------------------------------------------------------

    def radio_connected(self, max_contacts: int, tuning: codec.TuningParams | None) -> None:
        """The radio's table size and tuning, known once the session has started (its first
        table read may already have been reported)."""
        self.mesh.max_contacts = max_contacts
        self._tuning = tuning
        self._publish_index()
        self._publish(self._topics.stats, stats_document(self._readings, self._meter, tuning))

    def set_manage(self, manage: bool) -> None:
        """Whether add and remove requests are taken now (`[contacts] manage`, and the radio's
        firmware): the documents say so, and Home Assistant's controls follow them. Before the
        first table read the documents of the last run stay as they are."""
        if manage == self.manage:
            return
        self.manage = manage
        if not self._first_read:
            for key in self.mesh.contacts:
                self._publish_contact(key)
            self._publish_index()

    def profile_applied(self, report: ProfileReport) -> None:
        """The managed profile was reconciled at a connect: the `settings` document, and an
        event for each setting written or refused at this connect, and for each that is newly
        given up or newly found changed on the radio (once per run of the gateway)."""
        self._publish(self._topics.settings, settings_document(report, self._wall()), force=True)
        before = self._profiles.get(report.radio)
        self._profiles[report.radio] = report
        events = [
            *(("written", k, report.reasons.get(k)) for k in report.written),
            *(("failed", k, report.reasons.get(k)) for k in report.failed),
            *(("not_taken", k, report.reasons.get(k)) for k in report.not_taken),
            *(("not_converging", k, None) for k in report.not_converging
              if before is None or k not in before.not_converging),
            *(("drift", k, None) for k, v in report.drift.items()
              if before is None or before.drift.get(k) != v),
        ]  # fmt: skip
        for kind, key, reason in events:
            self._event(self._topics.setting_event, {
                "event_type": kind, "setting": SETTING_NAMES[key], "reason": reason,
                "live": setting_value(key, report.live.get(key)),
                "desired": setting_value(key, report.desired.get(key)),
            })  # fmt: skip

    def radio_event(self, event: RadioEvent) -> None:
        if isinstance(event, ContactsRead):
            self._contacts_read(event.records, event.read_at)
        elif isinstance(event, AdvertHeard):
            self._advert(event.advert, event.received_at)
        elif isinstance(event, StatsRead):
            self._stats(event)
        elif isinstance(event, PacketLogged):
            self._packet(event.rx, event.received_at)
        elif isinstance(event, PathReset):  # the documents say so before the next table read
            key = event.public_key.hex()
            if self.mesh.path_reset(key):
                self._publish_contact(key)

    def telemetry(self, key: str, answer: Answer) -> None:
        """An answer the poller matched to the contact it asked (ADR 0018): its position, its
        readings and that the contact was heard. The answer of a contact excluded meanwhile is
        dropped."""
        if key in self.exclusions:
            log.debug("telemetry %s: excluded, its answer dropped", key[:12])
            return
        tracked = self.mesh.contacts.get(key)
        if tracked is None:  # it left the table meanwhile
            return
        at, payload = answer.received_at, answer.payload
        found = lpp.position(payload)
        named = lpp.named(payload)
        had_fix, had = tracked.fix is not None, _values(tracked.readings)
        position, altitude = (found.position, found.altitude_m) if found else (None, None)
        moved = self.mesh.contact_answered(key, at, position, altitude, Readings(at, named))
        values = _values(tracked.readings)
        if values and self._history is not None:
            self._history.add_readings(key, at, values)
        if moved and tracked.heard is not None:  # at most hourly from answers alone
            self._store_heard(key, tracked.node, tracked.heard)
        self._store_fix(key, tracked.fix)
        self._publish_contact(key)
        skipped = sum(not _published(reading) for reading in payload.readings)
        if skipped:
            log.debug("telemetry %s: %d readings not published", key[:12], skipped)
        if (not had_fix and tracked.fix is not None) or not values.keys() <= had.keys():
            # its device gets a tracker or a sensor now, not at the next table read
            self._contacts_changed()

    def telemetry_state_changed(self, key: str) -> None:
        """The poller found the contact's state changed: its document again (the state is
        pulled, `telemetry_state`)."""
        self._publish_contact(key)

    def telemetry_event(self, kind: TelemetryEvent, key: str, state: TelemetryState) -> None:
        """A companion stopped answering or answers again (after its document); nothing for an
        excluded one."""
        if key in self.exclusions:
            return
        tracked = self.mesh.contacts.get(key)
        node = tracked.node if tracked is not None else None
        self._event(self._topics.telemetry_event, telemetry_event(kind, key, node, state))

    def telemetry_excluded(self, key: str, excluded: bool, *, forget: bool = False) -> bool | None:
        """An exclusion or inclusion was stored (or is in effect unsaved): values forgotten (on
        exclusion), the poller told, the document and the index published. With `forget`, the
        history rows deleted: True; False without a working history; None when not asked."""
        forgotten = None
        if excluded:
            self.mesh.forget_telemetry(key, self._wall())
            if forget:
                forgotten = self._forget_history(key)
            tracked = self.mesh.contacts.get(key)
            if tracked is not None:  # the position its record carries takes the place
                self._store_fix(key, tracked.fix)
        for listener in self.on_telemetry_exclusion:
            try:
                listener(key, excluded)
            except Exception:  # the documents and the result still go out
                log.exception("telemetry exclusion listener failed")
        self._publish_contact(key)
        self._publish_index()
        return forgotten

    def _forget_history(self, key: str) -> bool:
        """Delete the contact's readings and telemetry positions; False without a history. The
        stored fix it remembers may name a deleted row: harmless, a fix from an answer never
        comes back equal (it carries the answer's time)."""
        rows = self._history.forget_telemetry(key) if self._history is not None else None
        if rows is None:
            return False
        log.info("telemetry %s: %d rows deleted from the history", key[:12], rows)
        return True

    def restore_position_received(self, key: str, at: float) -> None:
        """When the contact's newest answer with a position came, from the saved schedule (for
        `position_received_at` after a restart); not for an excluded contact, nor for any while
        the exclusions cannot be read."""
        if key in self.exclusions or self.exclusions.unreadable:
            return
        self.mesh.restore_received(key, at)
        self._publish_contact(key)

    def restore_answered(self, key: str, at: float) -> None:
        """When the contact's newest answer came, from the saved schedule (before the first table
        read): readings the history kept of an older answer are not restored. It missed the later
        ones (off or broken for a while, or an older version ran)."""
        self.mesh.restore_answered(key, at)

    def radio_tx_1h_s(self) -> int | None:
        """The radio's own time on air in the last hour, as it counts it; None before two
        readings of its statistics."""
        return self._meter.used_s()

    def message(self, message: InboundMessage, sender: Contact | None) -> None:
        """A received message, after the bridge published it."""
        signal = Signal(message.hops, message.snr_db)
        text, name = message.text, sender.name if sender else None
        if message.kind is MessageKind.CHANNEL:
            name, text = split_sender_name(text)
        elif sender is not None:
            heard = Heard(message.received_at, "message", signal)
            if self.mesh.contact_heard(sender.public_key, heard):
                self._store_heard(sender.public_key, self.mesh.contacts[sender.public_key].node,
                                  heard)  # fmt: skip
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
        connected, and it may have lost them, or the adapter dropped them while it was away. A
        document of the last run follows its contact's state; one that is not current goes out
        also unchanged, as a run that stopped before the broker connected reached nobody."""
        for topic, payload in list(self._published.items()):
            self._bus.publish(topic, payload, retain=True)
        self._restate_all(connect=True)
        self._publish(self._topics.history, history_document(self._recent), force=True)

    def tick(self) -> None:
        """Called about once a minute: forget stale heard nodes, purge the history hourly, expire
        the documents of the last run and store the current ones."""
        now = self._wall()
        if self.mesh.forget_nodes(now):
            self._publish_nodes()
        if self._history is not None and now >= self._next_purge:
            self._history.purge(now)
            self._next_purge = now + PURGE_INTERVAL_S
        self._restate_all()
        self.flush()

    def flush(self) -> None:
        """Store the current documents (also at the stop): a change of their content waits for
        this, to keep the writes of the state file low."""
        if self._current_store is not None:
            self._current_store.replace(self._current)

    # --- contacts and nodes ---------------------------------------------------------------------

    def _contacts_read(self, records: Mapping[str, codec.ContactRecord], at: float) -> None:
        before = {key: tracked.node for key, tracked in self.mesh.contacts.items()}
        change = self.mesh.contacts_read((_node(r) for r in records.values()), at)
        joined_or_left = bool(change.added or change.removed)  # the heard nodes change with it
        left = [key for key in self._current if key not in self.mesh.contacts]
        for key in left:  # also one of the last run, at the first table read
            del self._current[key]
        if left:
            self.flush()
        if self._first_read:
            # compared with the contacts published in the last run, not with nothing: a start
            # announces only what changed while the gateway was stopped
            self._first_read = False
            self._take_heard_nodes()
            known = self._retained.keys if self._retained else frozenset(records)
            gone = tuple(sorted(known - set(records)))
            change = TableChange(tuple(k for k in change.added if k not in known), gone)
        for key in change.added:  # its document carries what the listeners change
            for added in self.on_contact_added:
                try:
                    added(key)
                except Exception:  # the documents still go out
                    log.exception("contact added listener failed")
        self._contacts_changed()
        for key in change.removed:  # after the devices are gone (Home Assistant reads them)
            self._clear(self._topics.contact(key))
        for key, tracked in self.mesh.contacts.items():
            self._store_fix(key, tracked.fix)
            self._publish_contact(key)
        self._publish_index()
        self._publish_nodes(read=joined_or_left)  # an added node left them, a removed one is back
        # last: whoever acts on the event finds the device, the documents and the recipient
        self._contact_events(change, before)
        if self._retained is not None:
            self._retained.replace(set(self.mesh.contacts))

    def _contacts_changed(self) -> None:
        for listener in self.on_contacts:
            try:
                listener(self.mesh)
            except Exception:  # a front end's bug must not stop the documents
                log.exception("contact listener failed")

    def _take_heard_nodes(self) -> None:
        """Nodes from the history that are not contacts become heard nodes again. One already
        heard in this run (before the first table read) keeps when it was heard; of the two
        records, the one of the newer advert stays, also in the history."""
        for row in self._heard_rows:
            if row.key in self.mesh.contacts:
                continue
            node = Node(row.key, row.name, row.node_type, last_advert=row.last_advert)
            tracked = self.mesh.nodes.get(row.key)
            if tracked is None:
                self.mesh.node_heard(node, row.heard)
                tracked = self.mesh.nodes.get(row.key)
            elif row.last_advert > tracked.node.last_advert:  # this run heard a replay first
                tracked.node = node
                if tracked.heard is not None:
                    self._store_heard(row.key, node, tracked.heard)
            if tracked is not None:
                tracked.first_heard = row.first_heard
        self._heard_rows = []
        self.mesh.forget_nodes(self._wall())
        self._publish_nodes(read=True)

    def _contact_events(self, change: TableChange, before: Mapping[str, Node]) -> None:
        for kind, keys in (("added", change.added), ("removed", change.removed)):
            for key in keys:
                tracked = self.mesh.contacts.get(key)
                node = tracked.node if tracked else before.get(key)
                request, request_id = self._expected.pop((kind, key), (None, None))
                on_request = f" on request {request_id}" if request_id else ""
                log.info("contact %s %r %s%s", key[:12], node.name if node else None, kind,
                         on_request)  # fmt: skip
                event = contact_event(kind, key, node, request=request, request_id=request_id)
                self._event(self._topics.contact_event, event)

    # --- contact requests (ADR 0017) -----------------------------------------------------------

    def expect(self, kind: str, key: str, request: str, request_id: str) -> None:
        """A request's write goes out now: the table read that shows the change (`added`,
        `removed`) is its result."""
        self._expected[(kind, key)] = (request, request_id)

    def request_done(self, kind: str, key: str, result: dict[str, Any]) -> None:
        """The request's own result, unless a table read already reported it."""
        if self._expected.pop((kind, key), None) is not None:
            self.contact_result(result)

    def contact_result(self, result: dict[str, Any]) -> None:
        """A request's result that no table read reports (refused, failed)."""
        self._event(self._topics.contact_event, result)

    def _advert(self, advert: codec.Advert, at: float) -> None:
        key = advert.public_key.hex()
        logged_at, signal = self._logged.pop(key, (0.0, Signal()))
        if at - logged_at > LOGGED_ADVERT_S:
            signal = Signal()
        heard = Heard(at, "advert", signal)
        if advert.record is None:  # a contact: its record follows with the next table read
            if self.mesh.contact_heard(key, heard):
                tracked = self.mesh.contacts[key]
                self._store_heard(key, tracked.node, heard)
                self._publish_contact(key)
            return
        node = _node(advert.record)
        new = self.mesh.node_heard(node, heard)
        if key in self.mesh.contacts:
            self._store_heard(key, node, heard)
            self._publish_contact(key)
            return
        # the newest advert's record: a replayed older one does not replace it, also not in the
        # history (its advert time comes back with it after a restart)
        self._store_heard(key, self.mesh.nodes[key].node, heard)
        self._publish_nodes()
        if new and self._node_events.allow(at):
            self._event(self._topics.node_event, {
                "event_type": "new", "key": key, "prefix": key[:12], "name": node.name,
                "type": type_name(node.node_type), "hops": signal.hops, "snr_db": signal.snr_db,
                "rssi_dbm": signal.rssi_dbm, "heard_at": iso(at),
                "look_alike": key in look_alike_nodes(self.mesh, self._protected),
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

    def _store_heard(self, key: str, node: Node, heard: Heard) -> None:
        if self._history is not None:
            self._history.node_heard(key, node.name, node.node_type, heard, node.last_advert)

    def _store_fix(self, key: str, fix: Fix | None) -> None:
        if fix is None or self._history is None or self._stored_fixes.get(key) == fix:
            return
        self._history.add_position(key, fix)
        self._stored_fixes[key] = fix

    def _type(self, contact: Contact) -> int:
        tracked = self.mesh.contacts.get(contact.public_key)
        return tracked.node.node_type if tracked else 0

    def _publish_contact(self, key: str) -> None:
        """Its document; before the first table read, the one of the last run, if it is kept."""
        tracked = self.mesh.contacts.get(key)
        if tracked is not None:
            document = contact_document(
                tracked, protected=key in self._protected, manage=self.manage,
                telemetry_state=self._state_of(tracked), excluded=key in self.exclusions,
            )  # fmt: skip
            self._publish(self._topics.contact(key), document)
            self._keep(key, document)
        elif key in self._current:  # before the first table read: later each kept is a contact
            self._restate(key)

    def _restate_all(self, *, connect: bool = False) -> None:
        """Before the first table read: each document of the last run follows its state."""
        if self._first_read:
            for key in list(self._current):
                self._restate(key, connect=connect)

    def _restate(self, key: str, *, connect: bool = False) -> None:
        """A document of the last run, published again when its contact's state changed (its
        answers expired, it was excluded or included, telemetry is off), also while the radio
        stays away. Kept in every state until the first table read; each change stored at once."""
        stored = self._current[key]
        excluded = key in self.exclusions
        document = restated(stored, self._companion_state(key), excluded=excluded)
        if document != stored:
            self._publish(self._topics.contact(key), document)
            self._current[key] = document
            self.flush()
        elif connect and document["telemetry_state"] != "answering":  # the broker may not have it
            self._publish(self._topics.contact(key), document)

    def _keep(self, key: str, document: dict[str, Any]) -> None:
        """The document of a contact whose answers count as current, for a restart. Stored at once
        when one turns current or no longer (an expiry, an exclusion), else at the next tick."""
        had = key in self._current
        if document["telemetry_current"]:
            self._current[key] = document
        else:
            self._current.pop(key, None)
        if had != (key in self._current):
            self.flush()

    def _state_of(self, tracked: Tracked) -> TelemetryState | None:
        """A companion's telemetry state; None for a contact that is never asked."""
        if tracked.node.node_type != NodeType.CHAT:
            return None
        return self._companion_state(tracked.node.public_key)

    def _companion_state(self, key: str) -> TelemetryState:
        if key in self.exclusions:
            return "excluded"
        if self.telemetry_state is None:
            return "off"
        return self.telemetry_state(key) or "waiting"

    def _publish_index(self) -> None:
        index = contacts_document(self.mesh, protected=self._protected, manage=self.manage,
                                  excluded=self.exclusions.keys)  # fmt: skip
        self._publish(self._topics.contacts, index)

    def _publish_nodes(self, *, read: bool = False) -> None:
        self._publish(self._topics.nodes, nodes_document(self.mesh, protected=self._protected))
        for listener in self.on_nodes:
            try:
                listener(read)
            except Exception:  # a front end's bug must not stop the documents
                log.exception("node listener failed")

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


def _published(reading: lpp.Reading) -> bool:
    """A reading of a published type, or the position (GPS on the companion's own channel)."""
    own_gps = reading.lpp_type == lpp.GPS and reading.channel == lpp.SELF_CHANNEL
    return reading.lpp_type in lpp.UNITS or own_gps


def _values(readings: Readings | None) -> dict[str, float]:
    """An answer's values as the history stores them: battery, MCU temperature, the readings."""
    if readings is None:
        return {}
    named = readings.named
    return {
        **({lpp.BATTERY_V: named.battery_v} if named.battery_v is not None else {}),
        **({lpp.MCU_TEMPERATURE_C: named.mcu_temperature_c}
           if named.mcu_temperature_c is not None else {}),
        **named.readings,
    }  # fmt: skip


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
