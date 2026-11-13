"""Puts the gateway together (ARC-009): the adapters, the parts of the application, and who hears
of what.

The parts tell one another through lists of callables, each one a seam for the tests. All of them
are connected here (`gateway`). Who tells, when, and the listeners in the order they are called:

* `observer.on_contacts`, the radio's contact table was read: `devices.update`;
  `homie.contacts_read`, with `[homie] enabled`;
* `observer.on_nodes`, the nodes heard changed: `manage.nodes_changed`;
* `observer.on_telemetry_exclusion`, a contact was excluded from telemetry or included again:
  `poller.exclusion_changed`; with telemetry off, the saved schedule forgets the contact;
* `observer.on_contact_added`, the radio added a contact: `poller.contact_added`; with telemetry
  off, the saved schedule forgets it;
* `observer.telemetry_state`, a document shows a companion's telemetry: `poller.state_of`;
* `poller.on_status`, the telemetry's status changed: `status.publish`;
* the announcer's `publish_state`, an announcement went out: `status.publish`;
* `bus.on_connected`, the broker connected: `announcer.connected`, `observer.publish_all`;
* `bus.on_stopping`, the bus stops, before its `offline`: `outbox.bus_stopping`, which refuses
  the requests that came after the stop's last step (`stopping`), then `drafts.stop` (below),
  then `homie.stopping`, with `[homie] enabled`, which takes no text any more;
* `bus.on_ha_online`, Home Assistant's birth message: `announcer.schedule`;
* `bus.on_ha_device`, a message on one of the gateway's discovery topics:
  `announcer.device_message`;
* `bus.on_ha_offline`, Home Assistant went offline: `devices.ha_offline`;
* `bus.on_show_hidden`, a request to show the hidden contacts' devices: `devices.show_hidden`;
* `bus.on_draft_request`, a text or a press of a chat in Home Assistant: `drafts.request`, which
  hands a press to `outbox.accept` at once; `bus.on_draft_retained`, the broker's copy of a draft:
  `drafts.retained_copy`; the drafts are published again after `bus.on_connected`, and cleared
  at `bus.on_stopping`;
* `devices.on_writable`, the companions whose device has a draft: `drafts.contacts`;
* the announcer, before the gateway's discovery: `drafts.announcing`, which cuts a draft longer
  than its new `max` and publishes it; after the discovery that follows Home Assistant's birth
  messages, at most one per ANNOUNCE_MIN_INTERVAL_S however many come: `drafts.republish`;
* `observer.on_document` and `status.on_document`, a document was published (or cleared):
  `homie.document`, with `[homie] enabled`;
* a radio session's `on_contact_key`, the radio named a key: `inbox.contact_key`;
* a radio session's `profile`, the connect reads the radio's settings: the radio profile
  (ADR 0020).

The bridge hands `bus.contact_requests` to `contacts.accept` and `bus.advert_requests` to
`advertiser.accept` (ADR 0033).

The radio's events go through one queue, which the bridge hands out (bridge.py).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from meshcore_home_gateway.advertiser import Advertiser
from meshcore_home_gateway.announcer import Announcer, ha_device_topics
from meshcore_home_gateway.bridge import Bridge
from meshcore_home_gateway.config import Config
from meshcore_home_gateway.config_checks import protected_keys, protected_names
from meshcore_home_gateway.contact_manager import ContactManager
from meshcore_home_gateway.core.telemetry import LaneFactory
from meshcore_home_gateway.drafts import Drafts
from meshcore_home_gateway.history import History, Retention
from meshcore_home_gateway.inbox import Inbox, channel_published, published_channels
from meshcore_home_gateway.mqtt.client import MqttBus
from meshcore_home_gateway.mqtt.contact_devices import ContactDevices
from meshcore_home_gateway.mqtt.discovery import gateway_device_name, gateway_names
from meshcore_home_gateway.mqtt.homie import HomieDevice
from meshcore_home_gateway.mqtt.homie_client import homie_adapter
from meshcore_home_gateway.mqtt.manage_entities import OBJECT_ID, ManageEntities
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer, observe
from meshcore_home_gateway.outbox import Delivery
from meshcore_home_gateway.ports import (
    Bus,
    RadioEvent,
    RadioPort,
    RadioProfileHook,
    SendRequest,
    SessionFactory,
    Transport,
)
from meshcore_home_gateway.radio.profile import RadioProfile
from meshcore_home_gateway.radio.session import default_session_factory
from meshcore_home_gateway.radio.transport import SerialTransport, TcpTransport
from meshcore_home_gateway.radio_link import RadioLink
from meshcore_home_gateway.state import (
    EXCLUSIONS_FILE,
    AdvertChecks,
    AnnouncedComponents,
    CommandLedger,
    ContactDeviceRecords,
    CurrentDocuments,
    JsonStateFile,
    ProfileRecords,
    RetainedKeys,
    TelemetryExclusions,
    TelemetryRecords,
)
from meshcore_home_gateway.status import Status
from meshcore_home_gateway.systemd import notify
from meshcore_home_gateway.telemetry_poller import TelemetryPoller


def event_expiries(config: Config) -> tuple[int, int]:
    """With MQTT 5, how long the broker keeps a `delivery` or `received` event, and a `command`
    event, for a subscriber that is away: a command older than `max_age_s` would be refused now,
    so its event must not come later either."""
    ttl = round(config.messages.queue_ttl_s)
    return ttl, round(config.commands.max_age_s) or ttl


async def serve(config: Config, state_dir: Path, stop: asyncio.Event) -> None:
    """The service until `stop` is set: the adapters (the MQTT client, the radio's transport, the
    state file and the history in `state_dir`) and the bridge with its parts."""
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    ha = config.homeassistant
    ha_status = ha.status_topic if ha.discovery else None
    events, commands = event_expiries(config)
    bus = MqttBus(
        config.mqtt,
        topics,
        ha_status,
        ha_devices=ha_device_topics(config),
        event_expiry_s=events,
        command_expiry_s=commands,
    )
    store = JsonStateFile(state_dir / "state.json")
    history = None
    if config.history.enabled:
        days = config.history
        retention = Retention(
            days.messages_days, days.positions_days, readings_days=days.readings_days
        )
        history = History.open(state_dir / "history.sqlite3", retention)
    protected = protected_keys(config)
    exclusions = TelemetryExclusions(  # also while telemetry is off
        state_dir / EXCLUSIONS_FILE,
        household=config.telemetry.ask == "household",
        protected=protected,
    )
    observer = Observer(
        bus,
        topics,
        history=history,
        retained=RetainedKeys(store, "contacts"),
        protected=protected,
        exclusions=exclusions,
        current=CurrentDocuments(store),
        advert_checks=AdvertChecks(store),
        names=gateway_names(config),
    )
    profile = RadioProfile(
        config.radio.profile, ProfileRecords(store), protected=protected_names(config)
    )
    devices = None
    if ha.discovery:  # also with no devices allowed: those of an earlier run are removed
        records = ContactDeviceRecords(store)
        devices = ContactDevices(config, topics, bus, records, protected=protected)
    homie = None
    if config.homie.enabled:  # its device has the name of the gateway's in Home Assistant
        homie = homie_adapter(
            config.mqtt,
            config.homie.contacts,
            gateway_device_name(config),
            topics,
            send=bus.requests.put_nowait,
        )
    bridge = gateway(
        config,
        bus,
        lambda: transport_for(config),
        CommandLedger(store),
        session_factory=default_session_factory,
        components=AnnouncedComponents(store),
        observer=observer,
        devices=devices,
        profile=profile,
        manage_components=AnnouncedComponents(store, OBJECT_ID),
        telemetry_records=TelemetryRecords(store),
        state_dir=state_dir,
        homie=homie.device if homie is not None else None,
    )
    try:
        if homie is not None:
            await homie.start()
        await bridge.run(stop)
    finally:
        if homie is not None:
            await homie.stop()
        if history is not None:
            history.close()


def _drafts(
    config: Config,
    topics: Topics,
    bus: Bus,
    outbox: Delivery,
    devices: ContactDevices | None,
    *,
    monotonic: Callable[[], float],
) -> Drafts | None:
    """The drafts of the chats in Home Assistant, with its discovery: those of the companions
    as their devices were announced last."""
    if not config.homeassistant.discovery:
        return None
    contacts = devices.writable() if devices is not None else frozenset[str]()
    return Drafts(
        config,
        topics,
        bus,
        outbox.accept,
        monotonic=monotonic,
        call_later=_call_later,
        contacts=contacts,
    )


def _hear_drafts(bus: Bus, drafts: Drafts, devices: ContactDevices | None) -> None:
    """The drafts' part of the table above; after the announcer's, so that a connect publishes
    the drafts after the discovery."""
    bus.on_connected.append(lambda: observe(drafts.republish))
    bus.on_draft_request.append(lambda *request: observe(drafts.request, *request))
    bus.on_draft_retained.append(lambda *copy: observe(drafts.retained_copy, *copy))
    bus.on_stopping.append(lambda: observe(drafts.stop))
    if devices is not None:
        devices.on_writable.append(drafts.contacts)


def _call_later(delay_s: float, callback: Callable[[], None]) -> asyncio.TimerHandle:
    """A timer of the running event loop (the drafts' echo limiter and expiry)."""
    return asyncio.get_running_loop().call_later(delay_s, callback)


def transport_for(config: Config) -> Transport:
    radio = config.radio
    if radio.transport == "serial":
        assert radio.device is not None  # noqa: S101 - validated by the configuration
        return SerialTransport(radio.device)
    assert radio.host is not None  # noqa: S101 - validated by the configuration
    return TcpTransport(radio.host, radio.port)


def _observer(config: Config, observer: Observer) -> Observer:
    """The observer, set as the configuration says, before it publishes."""
    observer.manage, observer.manage_reason, observer.heard_nodes = (
        config.contacts.manage,
        "no_radio" if config.contacts.manage else "management_off",  # until the radio connects
        config.contacts.heard_nodes,
    )
    observer.telemetry_types = config.telemetry.node_types
    observer.clean_names = config.messages.clean_received
    if config.messages.publish_channels == "targets":  # #136: the others' rows go for good
        observer.forget_unpublished_channels(published_channels(config))
    observer.show_channels(lambda index: channel_published(config, index))
    return observer


def gateway(
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
    homie: HomieDevice | None = None,
) -> Bridge:
    """The bridge with its parts, each connected to what it hears of (the table above).
    `components`, `manage_components`: what earlier runs announced to Home Assistant;
    `devices`: the contacts' devices in Home Assistant; `profile`: the managed radio profile
    (ADR 0020); `telemetry_lanes`: builds the telemetry lanes (tests); `state_dir`: where the
    state and the history are kept, for the free space in the health section; `homie`: the
    contacts as a Homie device (EXT-011)."""
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    protected = protected_keys(config)
    observer = _observer(
        config, observer or Observer(bus, topics, protected=protected, wall_clock=wall_clock)
    )
    radio = RadioLink(monotonic)
    events: asyncio.Queue[RadioEvent] = asyncio.Queue()
    outbox = Delivery(
        config,
        bus,
        topics,
        radio=radio,
        observer=observer,
        store=ledger.store,
        monotonic=monotonic,
        wall_clock=wall_clock,
    )
    poller = None
    if config.telemetry.enabled:
        poller = TelemetryPoller(
            observer,
            radio.live,
            outbox.activity,  # it asks after the messages
            records=telemetry_records,
            tcp=config.radio.transport == "tcp",
            monotonic=monotonic,
            wall_clock=wall_clock,
            lanes=telemetry_lanes,
        )
    manage = None  # Home Assistant's controls to add contacts
    if config.homeassistant.discovery:
        manage = ManageEntities(
            config,
            topics,
            bus,
            observer.mesh,
            records=manage_components,
            protected=protected,
            monotonic=monotonic,
            wall_clock=wall_clock,
            connected=lambda: bus.connected,
        )
    contacts = ContactManager(
        config, observer, events, radio.live, protected=protected, monotonic=monotonic
    )
    advertiser = Advertiser(
        bus, topics, radio=radio, outbox=outbox, monotonic=monotonic, wall_clock=wall_clock
    )
    inbox = Inbox(
        config,
        bus,
        topics,
        observer=observer,
        ledger=ledger,
        activity=outbox.activity,
        telemetry=poller,
        # the answer to a command goes as any message (queue, airtime budget, events), but it
        # is no duplicate: the inbox judged each command once
        reply=lambda key, text: outbox.accept(
            SendRequest(key, text.encode(), False, contact=True), deduplicate=False
        ),
        monotonic=monotonic,
        wall_clock=wall_clock,
    )
    drafts = _drafts(config, topics, bus, outbox, devices, monotonic=monotonic)  # chats in HA
    status = Status(
        config,
        bus,
        topics,
        radio=radio,
        outbox=outbox,
        observer=observer,
        store=ledger.store,
        telemetry=poller,
        notifier=notifier,
        monotonic=monotonic,
        wall_clock=wall_clock,
        state_dir=state_dir,
    )
    announcer = Announcer(
        config,
        topics,
        bus,
        radio,
        publish_state=status.publish,
        components=components,
        manage=manage,
        devices=devices,
        drafts=drafts,
        monotonic=monotonic,
        wall_clock=wall_clock,
    )

    # who hears of what: the table above
    if devices is not None:
        observer.on_contacts.append(devices.update)
    if manage is not None:
        observer.on_nodes.append(manage.nodes_changed)
    if poller is not None:
        observer.telemetry_state = poller.state_of
        observer.on_telemetry_exclusion.append(poller.exclusion_changed)
        observer.on_contact_added.append(poller.contact_added)
        poller.on_status = status.publish
    elif (records := telemetry_records) is not None:
        # so when telemetry is on again, the saved schedule has forgotten the answers
        # (design §6.11)
        observer.on_telemetry_exclusion.append(lambda key, _: records.forget(key))
        observer.on_contact_added.append(records.forget)
    bus.on_connected.extend((announcer.connected, lambda: observe(observer.publish_all)))
    bus.on_stopping.append(lambda: observe(outbox.bus_stopping))
    bus.on_ha_online.append(lambda: announcer.schedule(birth=True))
    bus.on_ha_device.append(announcer.device_message)
    if drafts is not None:
        _hear_drafts(bus, drafts, devices)
    if (shown := devices) is not None:
        bus.on_ha_offline.append(lambda: observe(shown.ha_offline))
        bus.on_show_hidden.append(lambda: observe(shown.show_hidden))
    if (device := homie) is not None:
        observer.on_contacts.append(lambda _: device.contacts_read())
        observer.on_document.append(device.document)
        status.on_document.append(device.document)
        bus.on_stopping.append(device.stopping)  # its connection stops after the bus

    def session(
        transport: Transport, radio_events: asyncio.Queue[RadioEvent], set_clock: bool
    ) -> RadioPort:
        new = session_factory(transport, radio_events, set_clock)
        new.on_contact_key = inbox.contact_key  # keys with a command sender's prefix (ADR 0009)
        new.profile = profile
        return new

    return Bridge(
        config,
        bus,
        topics,
        ledger,
        transport_factory=transport_factory,
        session_factory=session,
        radio=radio,
        events=events,
        observer=observer,
        outbox=outbox,
        inbox=inbox,
        announcer=announcer,
        status=status,
        contacts=contacts,
        advertiser=advertiser,
        telemetry=poller,
        profile=profile,
        notifier=notifier,
        monotonic=monotonic,
    )
