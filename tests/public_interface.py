"""The public interface as data (docs/development.md#compatibility), computed from the code
without a radio or a broker; tests/public_interface.json is its snapshot.

    python -m tests.public_interface           # compare; exit 1 on a difference
    python -m tests.public_interface --write   # write the snapshot again (make interface)

It records the structure only: topics with their retained flag and QoS, the subscriptions and the
last will, the key paths of the documents, the value lists the code declares in one place or
emits where Home Assistant is told of them, every setting (with the bounds and choices the parser
takes), command, option (with its names and bounds) and option group, the machine-readable
outputs with the values `probe` prints, and the answers, what Home Assistant refers to (with the
slugs of contact entity IDs), and the firmware versions tested.
The fixture's names are words that no real name contains (the slug "s" once turned "sensor" into
"<slug>ensor"); `placeholders` turns them into the snapshot's placeholders, which belong to its
format and are never renamed. Each has every kind of character its setting allows, so that a
change in how the code builds a topic or an ID from it leaves a fixture value in the snapshot,
which is refused.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import dataclasses
import functools
import importlib.util
import inspect
import io
import json
import logging
import math
import os
import re
import sys
import tempfile
import tomllib
import typing
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest import mock

import paho.mqtt.client as paho

from meshcore_home_gateway import __version__, cli, views
from meshcore_home_gateway import config as configuration
from meshcore_home_gateway.bridge import Bridge, ha_device_topics
from meshcore_home_gateway.config import Config, ProfileConfig, parse_config
from meshcore_home_gateway.contact_manager import Job
from meshcore_home_gateway.core import profile
from meshcore_home_gateway.core.commands import Rejection
from meshcore_home_gateway.core.contacts import Contact, ContactBook
from meshcore_home_gateway.core.mesh import (
    NODE_TYPES,
    AirtimeMeter,
    Fix,
    Heard,
    Mesh,
    Node,
    Signal,
    Tracked,
    type_name,
)
from meshcore_home_gateway.core.model import (
    DeliveryEvent,
    DeliveryState,
    InboundMessage,
    MessageKind,
    Position,
    TextType,
)
from meshcore_home_gateway.core.profile import ProfileReport
from meshcore_home_gateway.mqtt import client as mqtt_client
from meshcore_home_gateway.mqtt.client import ContactRequest
from meshcore_home_gateway.mqtt.contact_devices import (
    ContactDevices,
    contact_device_id,
    contact_discovery_topic,
    contact_payload,
)
from meshcore_home_gateway.mqtt.discovery import (
    device_id,
    discovery_payload,
    discovery_topic,
)
from meshcore_home_gateway.mqtt.manage_entities import manage_payload, manage_topic
from meshcore_home_gateway.mqtt.payloads import received_payload
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import AutoAdd, ContactFlag, NodeType, TelemetryMode
from meshcore_home_gateway.radio.session import (
    AdvertHeard,
    CommandFailedError,
    CompanionSession,
    ContactsRead,
    MessageReceived,
    PacketLogged,
    StatsRead,
)
from meshcore_home_gateway.state import (
    CommandLedger,
    ContactDeviceRecords,
    JsonStateFile,
    ProfileRecords,
)
from tests.radio_emulator import EmulatedContact, EmulatedRadio

if TYPE_CHECKING:
    from meshcore_home_gateway.observer import Publisher
    from meshcore_home_gateway.radio.transport import Transport
    from public_interface import Finding  # tools/public_interface.py, loaded below by its path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "tests" / "public_interface.json"


def _load(path: Path) -> ModuleType:
    """A tool by its path, as tools/release_plan.py loads it (tools/ is no package)."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module  # its dataclasses look their module up
    spec.loader.exec_module(module)
    return module


tool = _load(ROOT / "tools" / "public_interface.py")

# --- the fixture ----------------------------------------------------------------------------------

# q…q in each segment, which `_LEFTOVER` finds; '-', '_', digits (also at the end) and, where the
# setting allows them, upper-case letters and '/' (config.ID_PATTERN, config.TOPIC_SEGMENT;
# a command word is any word, taken in lower case; a slug has a-z, 0-9 and '_')
BASE, NODE = "qBa-se_q1/qMe-sh_q2", "qno-de_q3"
PREFIX, STATUS = "qPre-fix_q4", "qSta-tus_q5/qBir-th_q6"
ALIAS, CHANNEL_ALIAS = "qali-as_q7", "qchan-nel_q8"
WORD, SLUG = "qWo-rd_q9", "qsl_ug_q0"
KEY, KEY_B, KEY_N = "a1" * 32, "b2" * 32, "c3" * 32  # a contact, one that goes, a heard node
T = 1_800_000_000.0  # the scenario's clock

# only what a file must set: a setting the parser reads for it is read for every file
MINIMAL = f"""
[radio]
transport = "serial"
device = "/dev/ttyACM0"
[mqtt]
host = "broker.example.org"
node_id = "{NODE}"
"""
# everything that adds to the interface: tcp, discovery with contact devices, a target of each
# kind, a command word, the packet log; and the settings read only when present
MAXIMAL_TEXT = f"""
[radio]
transport = "tcp"
host = "192.0.2.10"
[radio.profile]
duty_cycle_percent = 20
[mqtt]
host = "broker.example.org"
base_topic = "{BASE}"
node_id = "{NODE}"
[homeassistant]
prefix = "{PREFIX}"
status_topic = "{STATUS}"
[targets.{ALIAS}]
contact = "{KEY}"
[targets.{CHANNEL_ALIAS}]
channel = 1
[commands]
senders = ["{KEY}"]
words = ["{WORD}"]
[diagnostics]
publish_rx_log = true
"""
# every setting with an answer type, for `config answers` (MINIMAL adds radio.device)
ANSWERED = f"""
[radio]
transport = "tcp"
host = "192.0.2.10"
port = 5000
set_clock = false
[radio.profile]
manage = "observe"
name = "Gateway"
duty_cycle_percent = 25.0
protect_contacts = false
[mqtt]
host = "broker.example.org"
port = 8883
username = "gateway"
password = "secret"
tls = true
ca_file = "/etc/ssl/certs/ca.pem"
cert_file = "/etc/ssl/certs/client.pem"
key_file = "/etc/ssl/private/client.pem"
client_id = "gateway"
keepalive_s = 30
base_topic = "{BASE}"
node_id = "{NODE}"
[homeassistant]
discovery = true
prefix = "{PREFIX}"
device_name = "Gateway"
status_topic = "{STATUS}"
contact_devices = 10
[messages]
ascii_only = true
truncate = false
dedup_window_s = 120
queue_size = 5
queue_ttl_s = 60
max_attempts = 4
flood_after = 3
ignore_channels = [0]
[airtime]
budget_s_per_hour = 72
burst_s = 12
[commands]
senders = ["{KEY}"]
words = ["{WORD}"]
max_age_s = 300
[targets.{ALIAS}]
contact = "{KEY}"
[targets.{CHANNEL_ALIAS}]
channel = 1
[history]
enabled = false
messages_days = 7
positions_days = 90
[contacts]
manage = false
[diagnostics]
publish_rx_log = true
log_level = "debug"
"""


@functools.cache
def maximal() -> Config:
    """MAXIMAL_TEXT as the parser reads it; on first use, so that a refusal fails the tests of
    the interface rather than their collection."""
    return parse_config(MAXIMAL_TEXT)


def gateway_device() -> str:
    """The gateway's device in Home Assistant, which holds the node ID. No configuration is parsed
    for it: `placeholders` runs inside the recorder on the parser (_reads)."""
    return device_id(cast(Config, SimpleNamespace(mqtt=SimpleNamespace(node_id=NODE))))


_DOCUMENTS = "<base_topic>/<node_id>/"  # the topics of the documents and events
_LEFTOVER = re.compile(r"q[A-Za-z0-9_-]+q")  # a fixture name, also one the code transformed


def placeholders(text: str) -> str:
    """`text` with each fixture value replaced by its placeholder."""
    for value, placeholder in (
        (gateway_device(), "<device>"),  # first: it holds the node ID
        (BASE, "<base_topic>"),
        (NODE, "<node_id>"),
        (PREFIX, "<prefix>"),
        (STATUS, "<status_topic>"),
        (ALIAS, "<alias>"),
        (CHANNEL_ALIAS, "<alias>"),
        (SLUG, "<slug>"),
        (WORD.casefold(), "<commands.words>"),  # as the code takes command words
        *((key, "<key>") for key in (KEY, KEY_B, KEY_N)),
    ):
        text = text.replace(value, placeholder)
    return text


# --- the snapshot ---------------------------------------------------------------------------------


def surface() -> dict[str, Any]:
    """The snapshot of the code, with every list of names sorted."""
    with tempfile.TemporaryDirectory() as tmp, refusing_errors():
        doc = {
            "format": tool.FORMAT,
            "mqtt": mqtt(Path(tmp)),
            "config": config(),
            "cli": command_line(Path(tmp)),
            "homeassistant": homeassistant(),
            "platforms": {"tested_firmware": sorted(profile.TESTED_FIRMWARE)},  # firmware codes
        }
    refuse_leftovers(doc)
    return cast(dict[str, Any], _sorted(doc))


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def refusing_errors() -> Iterator[None]:
    """Fail on an error logged inside: the bridge and the managers log an exception instead of
    raising it, so a broken path would only show as a topic missing from the snapshot; also when
    a later guard fails because of it, which then shows as the context. Warnings of the scenario
    stay off standard error."""
    handler = _Records()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        errors = [handler.format(r) for r in handler.records if r.levelno >= logging.ERROR]
        if errors:
            message = "logged while computing the public interface:\n" + "\n".join(errors)
            raise AssertionError(message)


def refuse_leftovers(doc: Any) -> None:
    """A fixture value that `placeholders` missed would tie the snapshot to the fixture; one the
    code transformed is a topic or an ID that changes for users."""
    text = json.dumps(doc)
    keys = {key[:8] for key in (KEY, KEY_B, KEY_N) if key[:8] in text}
    if left := sorted(set(_LEFTOVER.findall(text)) | keys):
        raise AssertionError(
            f"fixture values left in the snapshot: {left}: add to placeholders(), or the code"
            " transforms a configured name"
        )


def _sorted(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sorted(item) for key, item in value.items()}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return sorted(set(value))  # a list in the snapshot is a set of names
    return value


def _key_paths(value: Any, prefix: str = "", maps: tuple[str, ...] = ()) -> set[str]:
    """Every key path of a JSON value: `a`, `a.b`, `a[].b` inside a list, and `*` for the keys of
    the objects at `maps`, which are data."""
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{'*' if prefix in maps else key}" if prefix else key
            paths |= {path, *_key_paths(item, path, maps)}
    elif isinstance(value, list):
        for item in value:
            paths |= _key_paths(item, f"{prefix}[]", maps)
    return paths


# --- MQTT -----------------------------------------------------------------------------------------


class _Paho:
    """paho's client under MqttBus: records the last will, the subscriptions and each message."""

    on_connect: Any = None
    on_connect_fail: Any = None
    on_disconnect: Any = None
    on_message: Any = None

    def __init__(self) -> None:
        self.will: tuple[str, bytes, bool, int] | None = None
        self.subscribed: list[tuple[str, int]] = []
        self.published: list[tuple[str, bytes, bool, int]] = []

    def __getattr__(self, name: str) -> Callable[..., None]:
        return lambda *_, **__: None  # connecting and the network loop: there is no broker

    def will_set(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        self.will = (topic, payload, retain, qos)

    def subscribe(self, topic: list[tuple[str, int]]) -> None:
        self.subscribed += topic

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> Any:
        self.published.append((topic, payload, retain, qos))
        return SimpleNamespace(rc=paho.MQTT_ERR_SUCCESS)


@dataclass
class _Topic:
    """What was published on one topic, in its placeholder form."""

    retained: set[bool] = field(default_factory=set)
    qos: set[int] = field(default_factory=set)
    payloads: set[str] = field(default_factory=set)  # "json", "text"
    keys: set[str] = field(default_factory=set)
    texts: set[str] = field(default_factory=set)
    cleared: bool = False  # an empty message: a retained topic cleared, a discovery removed

    def record(self, topic: str) -> dict[str, Any]:
        try:
            [retained], [qos], [payload] = self.retained, self.qos, self.payloads
        except ValueError:
            raise AssertionError(f"{topic}: published in more than one way: {self}") from None
        entry: dict[str, Any] = {"retained": retained, "qos": qos, "payload": payload}
        return entry | ({"cleared": True} if self.cleared else {})


def mqtt(tmp: Path) -> dict[str, Any]:
    client = asyncio.run(_published(tmp))
    seen: dict[str, _Topic] = {}
    for topic, payload, retained, qos in client.published:
        entry = seen.setdefault(placeholders(topic), _Topic())
        entry.retained.add(retained)
        entry.qos.add(qos)
        if not payload:
            entry.cleared = True
        elif payload.startswith(b"{"):
            entry.payloads.add("json")
            entry.keys |= _key_paths(json.loads(payload))
        else:
            entry.payloads.add("text")
            entry.texts.add(payload.decode())
    refuse_leftovers(sorted(seen))  # a topic built otherwise: before the documents go missing
    if client.will is None:
        raise AssertionError("MqttBus set no last will")
    topic, payload, retained, qos = client.will
    seen[placeholders(topic)].texts.add(payload.decode())
    will = {"payload": payload.decode(), "retained": retained, "qos": qos}
    ours = {t.removeprefix(_DOCUMENTS): e for t, e in seen.items() if t.startswith(_DOCUMENTS)}
    texts = {name: list(e.texts) for name, e in ours.items() if "text" in e.payloads}
    return {
        "publish": {topic: entry.record(topic) for topic, entry in seen.items()},
        "subscribe": {placeholders(topic): {"qos": qos} for topic, qos in client.subscribed},
        "last_will": {placeholders(topic): will},
        "documents": {name: list(e.keys) for name, e in ours.items() if "json" in e.payloads},
        "values": texts | _vocabularies(client.published),
    }


def _no_radio() -> Transport:
    raise AssertionError("the scenario connects no radio")


async def _refreshed(newer_than: int) -> None:
    """The stub session's contact table is current."""


def _record(key: str, name: str, node_type: int) -> codec.ContactRecord:
    return codec.ContactRecord(
        bytes.fromhex(key), node_type, 0x01, 2, name, int(T) - 60, int(T) - 60,
        latitude_e6=1_000_000, longitude_e6=2_000_000,
    )  # fmt: skip


async def _refused_write() -> None:
    raise CommandFailedError("add_contact", "table_full")


EVENTS_S = 10.0  # the bridge's event loop handles the scenario's events within this


async def _until(events: asyncio.Task[None], step: Awaitable[None]) -> None:
    """`step`, which waits for the bridge's event loop: the exception that ended the loop is
    raised, and a loop that never gets there fails after EVENTS_S instead of hanging."""
    waiting = asyncio.ensure_future(step)
    done, _ = await asyncio.wait(
        {events, waiting}, timeout=EVENTS_S, return_when=asyncio.FIRST_COMPLETED
    )
    if waiting in done:
        waiting.result()
        return
    waiting.cancel()
    if events in done:
        events.result()  # raises what ended it
        raise AssertionError("the bridge's event loop ended")
    raise AssertionError(f"the bridge's event loop did not get there in {EVENTS_S} s")


async def _published(tmp: Path) -> _Paho:
    """Every publish path of the gateway once, through the MQTT adapter, and every value of the
    lists that the producers build where they publish them: the bridge never runs, its observer
    and a few of its steps are called directly."""
    client = _Paho()
    topics = Topics(BASE, NODE)
    config = maximal()
    bus = mqtt_client.MqttBus(
        config.mqtt, topics, config.homeassistant.status_topic,
        client_factory=lambda _: client, ha_devices=ha_device_topics(config),
    )  # fmt: skip
    await bus.start()
    client.on_connect(client, None, None, SimpleNamespace(is_failure=False), None)  # as paho
    await asyncio.sleep(0)  # the connection reaches the event loop
    store = JsonStateFile(tmp / "state.json")
    records = ContactDeviceRecords(store)
    devices = ContactDevices(config, topics, bus, records, wall_clock=lambda: T)
    bridge = Bridge(
        config, bus, _no_radio, CommandLedger(store), notifier=lambda _: True,
        wall_clock=lambda: T, devices=devices,
    )  # fmt: skip
    observer = bridge.observer
    alice, bob = _record(KEY, "Alice", NodeType.CHAT), _record(KEY_B, "Bob", NodeType.CHAT)
    observer.radio_connected(350, codec.TuningParams(0, 9000))
    observer.radio_event(ContactsRead({KEY: alice}, T))
    observer.radio_event(ContactsRead({KEY: alice, KEY_B: bob}, T))  # added
    observer.radio_event(ContactsRead({KEY: alice}, T))  # removed: its document and device cleared
    relay = _record(KEY_N, "Relay", NodeType.REPEATER)
    observer.radio_event(AdvertHeard(codec.Advert(bytes.fromhex(KEY_N), True, relay), T + 1))
    observer.radio_event(
        StatsRead(
            codec.Battery(4000, 1, 2), codec.CoreStats(4000, 1, 1, 0),
            codec.RadioStats(-118, -97, 6.5, 1, 2), codec.PacketStats(1, 2, 3, 4, 5, 6, 7), T,
        )
    )  # fmt: skip
    live: dict[str, profile.Value | None] = {
        profile.MANUAL_ADD: False, profile.AUTOADD: 0, profile.TELEMETRY: 0,
        profile.ADVERT_LOCATION: 0, profile.AIRTIME_FACTOR: 9000, profile.NAME: "Old",
    }  # fmt: skip
    wanted = cast(dict[str, profile.Value], {**live, profile.MANUAL_ADD: True, profile.NAME: "New"})
    observer.profile_applied(  # the first connect: an event of each kind
        ProfileReport(
            "auto", 14, True, False, live, wanted,
            written={profile.MANUAL_ADD: True}, reasons={profile.MANUAL_ADD: "initial"},
            failed=(profile.NAME,), not_taken=(profile.AUTOADD,),
            not_converging=(profile.TELEMETRY,), drift={profile.ADVERT_LOCATION: 1},
        )
    )  # fmt: skip
    bridge._deliver_event(DeliveryEvent("id", ALIAS, DeliveryState.CONFIRMED, text="Hello"))
    bridge._publish_announcements()  # discovery, the controls, the contacts' devices; state
    bridge._radio_ready.set()
    bridge._publish_state()  # the radio connected
    bridge._radio_ready.clear()
    devices.discovery_message(contact_discovery_topic(config, KEY), b"", False)  # hidden
    devices.show_hidden()
    message = InboundMessage(MessageKind.CONTACT, f"{WORD} now", int(T), T, sender_prefix=KEY[:12])
    book = ContactBook([Contact(KEY, "Alice")])
    session = cast(CompanionSession, SimpleNamespace(contacts=book, refresh_contacts=_refreshed))
    await bridge._on_message(MessageReceived(message, session, 0, False))
    bridge._contacts.accept(ContactRequest("add", KEY_N.encode(), retained=True))  # refused
    bridge._contacts.accept(ContactRequest("remove", b"", retained=True, key=KEY))  # refused
    events = asyncio.create_task(bridge._event_loop())
    added = Job("add", KEY_N, "id", "key", "Relay", NodeType.REPEATER)
    await _until(events, bridge._contacts._write(session, added, _refused_write()))  # failed
    bridge._events.put_nowait(PacketLogged(codec.RxLog(1.0, -90, b"\x00"), T))
    await _until(events, bridge._events.join())
    events.cancel()
    await bus.stop()
    return client


def _vocabularies(published: Sequence[tuple[str, bytes, bool, int]]) -> dict[str, list[str]]:
    """The value lists, as "<document>:<key path>": those that the code declares in one place,
    and those that the producers build where they publish them, as `published` carries them
    (each equal to the list Home Assistant gets). Values built in several places without a
    declaration (the reasons, a setting's state) wait for an enum of their own."""
    gateway = discovery_payload(maximal(), Topics(BASE, NODE))["cmps"]
    radio = gateway["radio"]
    emitted = {
        name: _declared(name, values, declared)
        for name, values, declared in (
            ("state:radio", _emitted(published, "state", "radio"),
             [radio["payload_on"], radio["payload_off"]]),
            ("contact:event_type", _emitted(published, "contact", "event_type"),
             gateway["contact"]["event_types"]),
            ("node:event_type", _emitted(published, "node", "event_type"),
             gateway["node"]["event_types"]),
            ("setting:event_type", _emitted(published, "setting", "event_type"),
             gateway["setting"]["event_types"]),
            ("settings:status", _statuses(), gateway["profile"]["options"]),
        )
    }  # fmt: skip
    every_type = sum(AutoAdd)  # the overwrite flag and every node type
    telemetry = [views.setting_value(profile.TELEMETRY, modes) for modes in range(4)]
    texts = [InboundMessage(MessageKind.CONTACT, "", 0, T, text_type=kind) for kind in TextType]
    errors = codec.CoreStats(0, 0, -1, 0)  # every error bit
    readings = views.RadioReadings(codec.Battery(0, 0, 0), errors, None, None, T)
    return emitted | {
        "delivery:event_type": [state.value for state in DeliveryState],
        "received:event_type": [kind.value for kind in MessageKind],
        "received:text_type": [received_payload(m, None, None)["text_type"] for m in texts],
        "received:command.reason": [reason.value for reason in Rejection],
        "contact:request": [r for r in _emitted(published, "contact", "request") if r is not None],
        "settings:mode": [mode.value for mode in profile.Mode],
        "settings:settings": list(views.SETTING_NAMES.values()),
        "settings:settings.autoadd.live": cast(
            list[str], views.setting_value(profile.AUTOADD, every_type)
        ),
        "settings:settings.telemetry.live.*": [
            mode for kinds in telemetry for mode in cast(dict[str, str], kinds).values()
        ],
        "settings:settings.advert_location.live": [
            cast(str, views.setting_value(profile.ADVERT_LOCATION, shared)) for shared in (0, 1)
        ],
        "contacts/<key>:type": list(NODE_TYPES.values()),
        "contacts/<key>:flags": views.flag_names(sum(ContactFlag)),
        "stats:errors": views.stats_document(readings, AirtimeMeter(), None)["errors"],
    }


def _emitted(published: Sequence[tuple[str, bytes, bool, int]], name: str, key: str) -> set[Any]:
    """The values of `key` in what was published on the document or event topic `name`."""
    topic = _DOCUMENTS + name
    return {
        json.loads(payload)[key]
        for published_on, payload, _, _ in published
        if placeholders(published_on) == topic and payload
    }


def _statuses() -> set[str]:
    """`settings.status` for a report of each kind: views.settings_document decides it."""
    live: dict[str, profile.Value | None] = {profile.NAME: "Old"}
    ok = ProfileReport("auto", 14, True, False, live, {profile.NAME: "Old"})
    reports = (
        ok,
        dataclasses.replace(ok, failed=(profile.NAME,)),  # problem
        dataclasses.replace(ok, observing=True, desired={profile.NAME: "New"}),  # differs
        dataclasses.replace(ok, drift={profile.NAME: "Old"}),
        dataclasses.replace(ok, written={profile.NAME: "New"}),
    )
    return {views.settings_document(report, T)["status"] for report in reports}


def _declared(name: str, emitted: set[str], declared: list[str]) -> list[str]:
    """`emitted`, which must be what Home Assistant is told of: it refuses another event type or
    option, and offers one that never comes."""
    problem = advice = ""
    if emitted - set(declared):
        problem = "the producer emits values Home Assistant does not know"
    elif set(declared) - emitted:
        problem = "Home Assistant declares values no path emits"
        advice = (
            ": extend the scenario in tests/public_interface.py (_published, _statuses) so that a"
            " path emits it, check why the code stopped emitting it, or drop it from discovery"
        )
    if problem:
        raise AssertionError(
            f"{name}: {problem}: emitted {sorted(emitted)}, declared {sorted(declared)}{advice}"
        )
    return list(emitted)


# --- configuration --------------------------------------------------------------------------------

# config._Section's typed methods: the type each reads, and the parameters _setting takes from it
_KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "string": ("string", ("default", "required")),
    "boolean": ("boolean", ("default",)),
    "integer": ("integer", ("default", "lo", "hi")),
    "number": ("number", ("default", "lo", "hi")),
    "strings": ("list[string]", ()),
    "integers": ("list[integer]", ("lo", "hi")),
    "section": ("table", ()),
}


def _literal(dataclass_type: type, name: str) -> list[str]:
    """The values of the Literal that annotates `name`, evaluated alone: the other annotations
    may name types imported for type checking only."""
    annotation = dataclass_type.__annotations__[name]
    if isinstance(annotation, str):  # from __future__ import annotations
        names = vars(sys.modules[dataclass_type.__module__])
        annotation = eval(annotation, names)  # noqa: S307 - an annotation of the code's own
    return list(typing.get_args(annotation))


def _transport(value: str) -> dict[str, str | None]:
    """What a file with this transport sets besides: its device or its host, not the other."""
    return {
        "radio.device": "/dev/ttyACM0" if value == "serial" else None,
        "radio.host": "192.0.2.10" if value == "tcp" else None,
    }


def _choice_keys() -> dict[str, tuple[Sequence[str], Callable[[str], dict[str, str | None]]]]:
    """The strings that the parser checks against a list after reading them: the candidates (a
    Literal of the dataclass, or the module's list), and what a file sets besides each value."""
    return {
        "radio.transport": (_literal(configuration.RadioConfig, "transport"), _transport),
        "radio.profile.manage": (_literal(ProfileConfig, "manage"), lambda _: {}),
        "diagnostics.log_level": (configuration.LOG_LEVELS, lambda _: {}),
    }


# brokers, ACLs and Home Assistant refer to the names these defaults make up
_IDENTITY = (
    "mqtt.base_topic",
    "mqtt.client_id",
    "homeassistant.prefix",
    "homeassistant.status_topic",
)


def config() -> dict[str, dict[str, Any]]:
    """Every setting as the parser reads it (every read goes through a typed method of
    config._Section), with the choices it takes and its answer type for the installer."""
    always = _reads(MINIMAL)
    settings = {}
    for key, entry in (_reads(MAXIMAL_TEXT) | always).items():
        if key in always:
            settings[key] = entry
        else:  # read only when present: the default passed is no default; `required` stays,
            # read as "required when its table is present"
            kept = {name: value for name, value in entry.items() if name != "default"}
            settings[key] = kept | {"conditional": True}
    for key, (candidates, besides) in _choice_keys().items():
        settings[key]["choices"] = _choices(key, candidates, besides)
    for key, entry in settings.items():
        _parsed_bounds(key, entry)
    settings["mqtt.client_id"]["default"] = _client_id()
    for key in _IDENTITY:
        settings[key]["identity"] = True
    for key, answer in configuration._ANSWER_TYPES.items():
        if key not in settings:
            raise AssertionError(
                f"{key}: has an answer type, but neither MINIMAL nor MAXIMAL_TEXT reads it through"
                " a typed method of config._Section: set it in MAXIMAL_TEXT if it is read only"
                " when present, read it with a typed method, or remove the answer type of a"
                " setting that is gone"
            )
        settings[key]["answer"] = answer.__name__
    return settings


def _reads(text: str) -> dict[str, dict[str, Any]]:
    """What parse_config asks of its tables for `text`, by setting."""
    reads: dict[str, dict[str, Any]] = {}

    def recording(method: str) -> Callable[..., Any]:
        original = getattr(configuration._Section, method)
        signature = inspect.signature(original)
        for parameter in _KINDS[method][1]:  # else its bound or flag would vanish unnoticed
            if parameter not in signature.parameters:
                raise AssertionError(
                    f"config._Section.{method} no longer takes {parameter}: update _KINDS and"
                    " _setting in tests/public_interface.py"
                )

        def read(section: Any, name: str, *args: Any, **kwargs: Any) -> Any:
            given = signature.bind(section, name, *args, **kwargs)
            given.apply_defaults()
            key, entry = placeholders(section.key(name)), _setting(method, given.arguments)
            if reads.setdefault(key, entry) != entry:
                raise AssertionError(f"{key}: read as {reads[key]} and as {entry}")
            return original(section, name, *args, **kwargs)

        return read

    with contextlib.ExitStack() as patches:
        for method in _KINDS:
            patches.enter_context(
                mock.patch.object(configuration._Section, method, recording(method))
            )
        parse_config(text)
    return reads


def _setting(method: str, arguments: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": _KINDS[method][0]}
    if arguments.get("default") is not None:
        entry["default"] = arguments["default"]
    if arguments.get("required"):
        entry["required"] = True
    for parameter, name in (("lo", "min"), ("hi", "max")):
        bound = arguments.get(parameter)
        if bound is not None and not math.isinf(bound):
            entry[name] = bound
    return entry


def _choices(
    key: str, candidates: Sequence[str], besides: Callable[[str], dict[str, str | None]]
) -> list[str]:
    """The candidates that the parser takes for `key`; it must refuse any other value."""

    def taken(value: str) -> bool:
        try:
            parse_config(_file({key: value, **besides(value)}))
        except configuration.ConfigError:
            return False
        return True

    if taken("other"):
        raise AssertionError(f"{key}: the parser checks no choices")
    return [value for value in candidates if taken(value)]


# a setting that another one allows: what a file sets besides it
_BESIDES: dict[str, dict[str, Any]] = {
    "radio.port": {"radio.transport": "tcp", **_transport("tcp")}
}


def _parsed_bounds(key: str, entry: dict[str, Any]) -> None:
    """The parser takes each bound recorded for `key` and refuses the next value beyond it, as
    `_choices` checks the choices: a range narrowed after the read would pass unseen. In MINIMAL,
    where a bound that depends on another setting has that one's default; a setting read only
    when present in its table as MAXIMAL_TEXT has it."""
    bounds = [(name, outward) for name, outward in (("min", -1), ("max", 1)) if name in entry]
    if not bounds:
        return
    besides, written = _BESIDES.get(key, {}), key
    if entry.get("conditional"):
        maximal_text = tomllib.loads(MAXIMAL_TEXT)
        written = next(k for k in _keys(maximal_text) if placeholders(k) == key)
        table = written.rpartition(".")[0]
        besides = {table: _table(maximal_text, table)}

    def refused(value: float) -> str | None:
        listed = entry["type"].startswith("list")
        try:
            parse_config(_file({**besides, written: [value] if listed else value}))
        except configuration.ConfigError as exc:
            return str(exc)
        return None

    for name, outward in bounds:
        bound = entry[name]
        if "integer" in entry["type"]:
            beyond = bound + outward
        else:
            beyond = math.nextafter(bound, outward * math.inf)
        if problem := refused(bound):
            raise AssertionError(
                f"{key}: its {name} is not {bound}: the parser refuses it ({problem}); if"
                " another setting must allow it, set that in _BESIDES"
            )
        if refused(beyond) is None:
            raise AssertionError(f"{key}: its {name} is not {bound}: the parser takes {beyond}")


def _keys(tables: dict[str, Any], prefix: str = "") -> Iterator[str]:
    """Every key of these tables, as `a.b.c`."""
    for name, value in tables.items():
        key = f"{prefix}.{name}" if prefix else name
        yield key
        if isinstance(value, dict):
            yield from _keys(value, key)


def _table(tables: dict[str, Any], key: str) -> dict[str, Any]:
    for name in key.split("."):
        tables = tables[name]
    return tables


def _file(settings: dict[str, Any]) -> str:
    """MINIMAL with these settings (a table as a dict), or without those set to None."""
    tables: dict[str, Any] = tomllib.loads(MINIMAL)
    for key, value in settings.items():
        *path, name = key.split(".")
        table = tables
        for part in path:
            table = table.setdefault(part, {})
        if value is None:
            table.pop(name, None)
        else:
            table[name] = copy.deepcopy(value)  # a table given stays as it was
    return _toml(tables)


def _toml(tables: dict[str, Any], name: str = "") -> str:
    """These tables as TOML, a table in a table as [a.b]."""
    lines = [f"[{name}]"] if name else []
    lines += [
        f"{key} = {configuration._toml_value(value)}"
        for key, value in tables.items()
        if not isinstance(value, dict)
    ]
    inner = (
        _toml(value, f"{name}.{key}" if name else key)
        for key, value in tables.items()
        if isinstance(value, dict)
    )
    return "\n".join(lines) + "\n" + "".join(inner)


def _client_id() -> str:
    """The client ID the broker sees when the file sets none (mqtt/client.py)."""
    with mock.patch.object(paho, "Client") as client:
        mqtt_client.make_paho_client(parse_config(MINIMAL).mqtt)
    return placeholders(client.call_args.kwargs["client_id"])


# --- command line ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Type:
    """What a type function of options takes: its name, and its bounds (of the port, for
    host[:port]), which _bounds checks against the function."""

    name: str
    low: float | None = None
    high: float | None = None
    argument: Callable[[float], str] = str  # the text that gives this number


@functools.cache
def _types() -> dict[Any, _Type]:
    """The type functions of options, by the name the snapshot gives them; on first use, so that
    a renamed one fails the tests of the interface rather than their collection."""
    return {
        Path: _Type("path"),
        cli.tcp_address: _Type("host[:port]", 1, 65535, lambda port: f"192.0.2.10:{port:g}"),
        cli._wait_seconds: _Type("seconds", 0.0, cli.MAX_WAIT_S),
        cli._count: _Type("count", 0),
    }


_FAR = 10**9  # beyond every bound: a number type without a bound takes it
# how few values a positional takes, by nargs (an integer: that many); it is required unless none
_FEWEST: dict[Any, int] = {None: 1, "?": 0, "*": 0, "+": 1, argparse.REMAINDER: 0}


def command_line(tmp: Path) -> dict[str, Any]:
    commands: dict[str, Any] = {}
    _commands(cli._parser(), "", commands)
    report = _probe_report(tmp)
    probe = ["probe", "--tcp", "192.0.2.10", "--state-dir", str(tmp)]
    with _probing(report):
        printed = json.loads(_printed([*probe, "--json"]))
        listings = {
            f"probe --list {listing}": _printed([*probe, "--list", listing])
            for listing in commands["probe"]["options"]["--list"]["choices"]
        }
    paths = {"config": str(cli.DEFAULT_CONFIG), **_state_directory(tmp, report)}
    keyed_by_data = ("profile.changes", "profile.contacts")
    exit_codes = {
        "ok": cli.EX_OK, "failure": cli.EX_FAILURE, "usage": cli.EX_USAGE,
        "config_error": cli.EX_CONFIG,
    }  # fmt: skip
    contacts = json.loads(_contacts_list(tmp, "--json", session=_Marked))
    return {
        "commands": commands,
        "exit_codes": exit_codes,
        "paths": paths,
        "columns": _columns(listings | {"contacts list": _contacts_list(tmp)}),
        "json": {
            "probe --json": list(_key_paths(printed, maps=keyed_by_data)),
            "contacts list --json": list(_key_paths(contacts)),
            "listen": list(_key_paths(_listened(tmp))),
        },
        "values": _probe_values(tmp, probe),
        "answers": _answers(tmp),
    }


def _commands(parser: argparse.ArgumentParser, path: str, out: dict[str, Any]) -> None:
    options: dict[str, Any] = {}
    positionals: dict[str, Any] = {}  # "1", "2", …: their order is the interface
    commands: argparse._SubParsersAction[argparse.ArgumentParser] | None = None
    for action in parser._actions:
        place = str(len(positionals) + 1)
        if isinstance(action, argparse._HelpAction):
            continue
        if isinstance(action, argparse._SubParsersAction):  # the command's name, a positional
            commands = action
            positionals[place] = {"name": _named(action), "takes_value": True}
            positionals[place] |= {"required": True} if action.required else {}
        elif isinstance(action, argparse._VersionAction):
            output = {"output": str(action.version).replace(__version__, "{version}")}
            options.update((name, output | _names(action)) for name in action.option_strings)
        elif action.option_strings:
            options.update(
                (name, _argument(action, action.required)) for name in action.option_strings
            )
        else:  # by name, so that another order shows
            positionals[place] = _argument(action, _fewest(action) > 0) | {"name": _named(action)}
    entry: dict[str, Any] = {"options": options, "positionals": positionals}
    if groups := [_group(group) for group in parser._mutually_exclusive_groups]:
        entry["groups"] = sorted(groups, key=lambda group: cast(list[list[str]], group["options"]))
    out[path or parser.prog] = entry
    if commands is not None:
        for name, command in commands.choices.items():
            _commands(command, f"{path} {name}".strip(), out)


def _named(action: argparse.Action) -> str:
    """A positional as the usage line names it; a command's subcommand without one: "command"."""
    name = action.metavar or (action.dest if action.dest != argparse.SUPPRESS else "command")
    return " ".join(name) if isinstance(name, tuple) else name


def _fewest(action: argparse.Action) -> int:
    """How few values a positional takes; argparse before 3.13 calls `*` required."""
    if isinstance(action.nargs, int):
        return action.nargs
    if action.nargs not in _FEWEST:
        raise AssertionError(f"{action.dest}: name the nargs {action.nargs!r} in _FEWEST")
    return _FEWEST[action.nargs]


def _group(group: argparse._MutuallyExclusiveGroup) -> dict[str, Any]:
    """A mutually exclusive group: each member by all its names (an option and its aliases), a
    positional by its name."""
    members = [
        sorted(action.option_strings) if action.option_strings else [_named(action)]
        for action in group._group_actions
    ]
    return {"options": sorted(members), "required": group.required}


def _names(action: argparse.Action) -> dict[str, list[str]]:
    """An option's names, in the record of each: a new one is an alias, not a new option."""
    return {"names": sorted(action.option_strings)} if action.option_strings else {}


def _argument(action: argparse.Action, required: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {"takes_value": action.nargs != 0, **_names(action)}
    if required:
        entry["required"] = True
    if action.choices is not None:
        entry["choices"] = list(action.choices)
    if action.nargs not in (None, 0):
        entry["nargs"] = action.nargs
    if action.type is not None:
        if (kind := _types().get(action.type)) is None:
            raise AssertionError(f"{action.dest}: name the type {action.type!r} in _types()")
        entry |= {"type": kind.name, **_bounds(cast(Callable[[str], Any], action.type), kind)}
    default = action.default  # `is`: a default of 0 or 0.0 equals False
    if default is not None and default is not False and default is not argparse.SUPPRESS:
        entry["default"] = str(default) if isinstance(default, Path) else default
    return entry


def _bounds(function: Callable[[str], Any], kind: _Type) -> dict[str, float]:
    """The bounds of a number type as the snapshot records them: the type function takes each
    and refuses the next number beyond it, and takes a number far out where there is none."""
    if kind.low is None and kind.high is None:
        return {}

    def taken(value: float) -> bool:
        try:
            function(kind.argument(value))
        except (argparse.ArgumentTypeError, TypeError, ValueError):
            return False
        return True

    bounds: dict[str, float] = {}
    for name, bound, outward in (("min", kind.low, -1), ("max", kind.high, 1)):
        if bound is None:
            if not taken(outward * _FAR):
                raise AssertionError(
                    f"{kind.name}: refuses {outward * _FAR}: give it its {name} in _types()"
                )
            continue
        beyond = (
            math.nextafter(bound, outward * math.inf)
            if isinstance(bound, float)
            else bound + outward
        )
        if not taken(bound) or taken(beyond):
            raise AssertionError(f"{kind.name}: its {name} is not {bound}: correct it in _types()")
        bounds[name] = bound
    return bounds


def _probe_report(tmp: Path) -> dict[str, Any]:
    """What `probe` reports: from a WiFi companion (no flush, no wait), with settings the profile
    changes and a protected contact it adds."""
    radio = EmulatedRadio(
        name="Old", contacts=[EmulatedContact(bytes.fromhex(KEY), "Alice", flags=0x01)],
        manual_add=False, latitude_e6=1_000_000, longitude_e6=2_000_000, tuning=(0, 9000),
        needs_flush=False,
    )  # fmt: skip
    records = ProfileRecords(JsonStateFile(tmp / "probe.json", read_only=True))
    protected = {KEY_B: "Bob"}
    return asyncio.run(
        cli._probe_radio(radio, ProfileConfig(name="New"), records, protected, configured=True)
    )


# the key paths of `probe --json` that hold values of a list
_PROBE_VALUES = (
    "advert_location", "auto_add.types", "contacts[].flags", "contacts[].type", "telemetry.*",
)  # fmt: skip


def _probe_values(tmp: Path, probe: list[str]) -> dict[str, list[str]]:
    """The value lists of probe's machine-readable output, as "<output>:<key path>" (a field of a
    list by its number), as `probe --json` and `probe --list contacts` print them: for a radio
    with each advert setting and each telemetry mode, whose contacts have every node type and
    every flag, and which adds every type."""
    contacts = [
        EmulatedContact(
            bytes([node_type]) * 32, f"Node {node_type}", node_type, flags=sum(ContactFlag)
        )
        for node_type in NodeType
        if node_type != NodeType.NONE
    ]
    records = ProfileRecords(JsonStateFile(tmp / "probe.json", read_only=True))
    values: dict[str, list[str]] = {}
    for advert_location in (0, 1):  # the firmware's two: without and with the radio's position
        for mode in TelemetryMode:
            radio = EmulatedRadio(
                contacts=contacts, manual_add=False, needs_flush=False,
                advert_location=advert_location, telemetry_modes=mode * 0b010101,  # each kind
            )  # fmt: skip
            report = asyncio.run(
                cli._probe_radio(radio, ProfileConfig(), records, {}, configured=False)
            )
            with _probing(report):
                printed = json.loads(_printed([*probe, "--json"]))
                listed = _printed([*probe, "--list", "contacts"])
            for path in _PROBE_VALUES:
                values.setdefault(f"probe --json:{path}", []).extend(_values_at(printed, path))
            lines = [line.split("\t") for line in listed.splitlines()]
            types = [fields[2] for fields in lines if len(fields) > 2]  # columns: a finding
            values.setdefault("probe --list contacts:3", []).extend(types)
    return values


def _values_at(value: Any, path: str) -> list[Any]:
    """What `value` holds at a key path as `_key_paths` writes it, `*` for every key, each list
    taken apart; nothing where the path is missing (the key paths show that)."""
    if not path:
        return list(value) if isinstance(value, list) else [value]
    step, _, rest = path.partition(".")
    name = step.removesuffix("[]")
    if not isinstance(value, dict):
        return []
    items = list(value.values()) if name == "*" else [value[name]] if name in value else []
    if step.endswith("[]"):
        items = [item for listed in items if isinstance(listed, list) for item in listed]
    return [found for item in items for found in _values_at(item, rest)]


@contextlib.contextmanager
def _probing(report: dict[str, Any]) -> Iterator[None]:
    """`probe` reports `report` without a radio."""

    async def probed(*_: Any, **__: Any) -> dict[str, Any]:
        return report

    with mock.patch.object(cli, "_probe_radio", probed):
        yield


def _printed(argv: list[str], *, empty: bool = False) -> str:
    """What the command prints on standard output; it must succeed and print something."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            status = cli.main(argv)
        except SystemExit as exc:  # argparse refuses the command line
            status = exc.code if isinstance(exc.code, int) else cli.EX_USAGE
    if status != cli.EX_OK or not (out.getvalue() or empty):
        raise AssertionError(f"{' '.join(argv)}: exit {status}, {len(out.getvalue())} characters")
    return out.getvalue()


_STATE_DIRECTORY = "STATE_DIRECTORY"


def _state_directory(tmp: Path, report: dict[str, Any]) -> dict[str, str]:
    """The state directory of `run` and `probe` without --state-dir: the default, and the
    variable of the environment that names another (systemd's StateDirectory= sets it). `probe`
    reports `report`, without a radio."""
    config = _written(tmp / "state.toml", MINIMAL)
    used: dict[str, Path] = {}

    async def serve(_: Config, state_dir: Path) -> int:
        used["run"] = state_dir
        return cli.EX_OK

    def store(path: Path, **_: Any) -> JsonStateFile:
        used["probe"] = path.parent
        return JsonStateFile(tmp / "probe.json", read_only=True)

    found: list[Path] = []
    for named in (None, tmp / "named"):
        with contextlib.ExitStack() as patches:
            patches.enter_context(mock.patch.dict(os.environ))
            os.environ.pop(_STATE_DIRECTORY, None)
            if named:
                os.environ[_STATE_DIRECTORY] = str(named)
            patches.enter_context(mock.patch.object(cli, "setup_logging"))
            patches.enter_context(mock.patch.object(cli, "_serve", serve))
            patches.enter_context(_probing(report))
            patches.enter_context(mock.patch("meshcore_home_gateway.state.JsonStateFile", store))
            used.clear()
            _printed(["run", "--config", str(config)], empty=True)
            _printed(["probe", "--tcp", "192.0.2.10", "--json"])
        run, probe = used.get("run"), used.get("probe")
        if run is None or probe != run:  # both document the same default
            raise AssertionError(f"run uses the state directory {run}, probe {probe}: record each")
        found.append(run)
    default, by_environment = found
    if by_environment not in (default, tmp / "named"):
        raise AssertionError(f"{_STATE_DIRECTORY}={tmp / 'named'}: state in {by_environment}")
    named_by = {"state_dir_env": _STATE_DIRECTORY} if by_environment != default else {}
    return {"state_dir": str(default), **named_by}


def _written(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class _Documents:
    """The broker connection of the `contacts` commands, with a contact and a heard node."""

    online = True
    documents: typing.ClassVar[dict[str, Any]] = {
        "contacts": {"contacts": [{"key": KEY, "type": "chat", "name": "Alice"}], "manage": True},
        "nodes": {"nodes": [{"key": KEY_N, "type": "repeater", "name": "Relay"}]},
    }

    def __init__(self, *_: Any) -> None:
        pass

    def open(self) -> str | None:
        return None

    def close(self) -> None:
        pass

    def document(self, name: str) -> Any:
        return self.documents.get(name)


class _Marked(_Documents):
    """Documents that name themselves: `contacts list --json` prints them whole, and their keys
    are those under `mqtt > documents`."""

    documents: typing.ClassVar[dict[str, Any]] = {
        name: {f"<document {name}>": True} for name in ("contacts", "nodes")
    }


def _contacts_list(tmp: Path, *more: str, session: type[_Documents] = _Documents) -> str:
    config = _written(tmp / "contacts.toml", MINIMAL)
    with mock.patch.object(cli, "_ContactSession", session):
        return _printed(["contacts", "list", "--config", str(config), *more])


class _Broker(_Paho):
    """The paho client of `listen`: it connects at once and delivers one message on the first
    topic subscribed, a marker for the documents and events of `mqtt > documents`."""

    def loop_start(self) -> None:
        self.on_connect(self, None, None, SimpleNamespace(is_failure=False), None)

    def subscribe(self, topic: list[tuple[str, int]]) -> None:
        message = SimpleNamespace(topic=topic[0][0], payload=b'{"<message>": true}', retain=False)
        self.on_message(self, None, message)


def _listened(tmp: Path) -> Any:
    """The line `listen` prints for a message."""
    config = _written(tmp / "listen.toml", MINIMAL)
    with mock.patch.object(cli, "_mqtt_client", return_value=_Broker()):
        return json.loads(_printed(["listen", "--config", str(config), "--count", "1"]))


def _answers(tmp: Path) -> dict[str, list[str]]:
    """The keys that `config answers` prints (`config render` and the installer read them back),
    which must be those of every setting with an answer type, and the forms of a target's
    value."""
    keys, targets = [], []
    for name, text in (("answered", ANSWERED), ("minimal", MINIMAL)):
        config = _written(tmp / f"{name}.toml", text)
        for line in _printed(["config", "answers", "--config", str(config)]).splitlines():
            key, _, value = placeholders(line).partition("=")
            keys.append(key)
            if key == "target.<alias>":
                targets.append(re.sub(r"\d+", "<index>", value))
    expected = {*configuration._ANSWER_TYPES, "target.<alias>"}
    if missing := sorted(expected - set(keys)):
        raise AssertionError(
            f"config answers prints no line for {missing}: set each in ANSWERED in"
            " tests/public_interface.py, or find why config_answers no longer prints it"
        )
    if extra := sorted(set(keys) - expected):
        raise AssertionError(f"config answers prints {extra}, which have no answer type")
    return {"keys": keys, "target.<alias>": targets}


_BY_KIND = ("probe --list profile", "contacts list")  # their first field names the line's kind


def _columns(listings: dict[str, str]) -> dict[str, int]:
    """Fields per line of the tab-separated lists, which shell scripts read with `read` (the
    installer does); by line kind where the first field names it."""
    columns: dict[str, int] = {}
    for listing, text in listings.items():
        for fields in (line.split("\t") for line in text.splitlines()):
            name = f"{listing}: {fields[0]}" if listing in _BY_KIND else listing
            if columns.setdefault(name, len(fields)) != len(fields):
                raise AssertionError(f"{name}: lines of {columns[name]}, {len(fields)} fields")
    return columns


# --- Home Assistant -------------------------------------------------------------------------------


def homeassistant() -> dict[str, Any]:
    config, device, topics = maximal(), gateway_device(), Topics(BASE, NODE)
    contact = contact_device_id(config, KEY)
    contacts: dict[str, dict[str, Any]] = {}
    for node_type in NodeType:  # the components of a contact depend on its type
        tracked = _tracked(node_type)
        found = _components(contact_payload(config, topics, tracked, SLUG)["cmps"], contact)
        for key, entry in found.items():
            known = contacts.setdefault(key, entry | {"node_types": []})
            if {name: value for name, value in known.items() if name != "node_types"} != entry:
                raise AssertionError(f"contact component {key}: other facts for {node_type!r}")
            known["node_types"].append(type_name(node_type))  # as `type` names it on the wire
    return {
        "devices": {
            "gateway": {
                "id": device.replace(NODE, "<node_id>"),
                "topic": placeholders(discovery_topic(config)),
            },
            "manage_contacts": {"topic": placeholders(manage_topic(config))},
            "contact": {
                "id": placeholders(contact),
                "topic": placeholders(contact_discovery_topic(config, KEY)),
            },
        },
        "components": {
            "gateway": _components(discovery_payload(config, topics)["cmps"], device),
            "manage_contacts": _components(manage_payload(config, topics, [])["cmps"], device),
            "contact": contacts,
        },
        "slugs": _slugs(),
    }


# contact names: plain, with an umlaut and an apostrophe, with a letter Unicode does not
# decompose, longer than a slug, without letters; then a second contact named like the first
NAMED = ("Alice", "Jürgen's Handy", "Groß", "Repeater on the hill behind the old mill", "🙂")
TAKEN = NAMED[0]


def _named_key(index: int) -> str:
    """The key of the example contact at `index`: digits only, which no example name has."""
    return f"{index:02}{'0123456789' * 7}"[:64]


def _slugs() -> dict[str, str]:
    """The slug the code gives each example contact, which its entity IDs
    `<platform>.mesh_<slug>_<component>` keep (docs/home-assistant.md): the contacts come in a
    table read, then a second contact named like the first in another."""
    nodes = [Node(_named_key(index), name, NodeType.CHAT) for index, name in enumerate(NAMED)]
    second = Node(_named_key(len(NAMED)), TAKEN, NodeType.CHAT)
    bus = cast("Publisher", SimpleNamespace(publish=lambda *_, **__: None))
    with tempfile.TemporaryDirectory() as tmp:
        records = ContactDeviceRecords(JsonStateFile(Path(tmp) / "state.json"))
        devices = ContactDevices(maximal(), Topics(BASE, NODE), bus, records, wall_clock=lambda: T)
        mesh = Mesh()
        for table in (nodes, [*nodes, second]):
            mesh.contacts_read(table, T)
            devices.update(mesh)
    slug = {key: record.slug for key, record in records.announced.items()}
    slugs = {node.name: _key_part(slug[node.public_key], node.public_key) for node in nodes}
    taken = _key_part(slug[second.public_key], second.public_key)
    return slugs | {f"{TAKEN} (a second contact)": taken}


def _key_part(slug: str, key: str) -> str:
    """`slug` with the longest start of `key` in it, of 4 digits or more, as <key[:n]>."""
    for size in range(len(key), 3, -1):
        if key[:size] in slug:
            return slug.replace(key[:size], f"<key[:{size}]>")
    return slug


def _tracked(node_type: int) -> Tracked:
    """A contact with all that it can have: a component that exists only with data is there."""
    position = Position(1.0, 2.0)
    node = Node(
        KEY, "Alice", node_type, flags=sum(ContactFlag), path_hops=1, last_advert=int(T) - 60,
        position=position,
    )  # fmt: skip
    heard = Heard(T, "message", Signal(hops=1, snr_db=5.0, rssi_dbm=-90))
    tracked = Tracked(node, T - 60, heard, Fix(position, T, "advert", altitude_m=10.0))
    _filled(tracked)
    return tracked


def _filled(value: Any) -> None:
    """Every field of `value` and of the dataclasses in it differs from its default."""
    if unfilled := _unfilled(value):
        raise AssertionError(
            f"fill {', '.join(unfilled)} in _tracked() in tests/public_interface.py, so that"
            " components that need them are recorded"
        )


def _unfilled(value: Any) -> list[str]:
    """The fields of `value` and of the dataclasses in it that are at their default."""
    found = []
    for item in dataclasses.fields(value):
        given = getattr(value, item.name)
        if item.default_factory is not dataclasses.MISSING:
            default = item.default_factory()
        else:
            default = item.default
        if given == default:
            found.append(f"{type(value).__name__}.{item.name}")
        elif dataclasses.is_dataclass(given):
            found += _unfilled(given)
    return found


def _components(found: dict[str, dict[str, Any]], device: str) -> dict[str, dict[str, Any]]:
    """What automations, dashboards and statistics refer to, by the end of the unique ID."""
    components: dict[str, dict[str, Any]] = {}
    for key, component in found.items():
        if component["unique_id"] != f"{device}_{key}":
            raise AssertionError(f"{key}: the unique ID {component['unique_id']} is not by key")
        entry = _component(component)
        if components.setdefault(placeholders(key), entry) != entry:  # one per target alias
            raise AssertionError(f"{key}: other facts than {placeholders(key)}")
    return components


def _component(component: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "platform": component["p"],
        "entity_id": placeholders(component["default_entity_id"]),
    }
    if "event_types" in component:
        entry["event_types"] = [placeholders(kind) for kind in component["event_types"]]
    if "options" in component:  # the select of heard nodes offers data
        entry["options"] = component["options"] or ["<heard nodes>"]
    if "unit_of_measurement" in component:
        entry["unit"] = component["unit_of_measurement"]
    event = component["p"] == "event"
    if template := component.get("json_attributes_template"):
        if not (keys := re.findall(r"'(\w+)':", template)):
            raise AssertionError(f"{component['unique_id']}: no attribute keys in {template!r}")
        entry["attributes"] = keys
    elif topic := component.get("json_attributes_topic", component["state_topic"] if event else ""):
        entry["attributes"] = f"<document {placeholders(topic).removeprefix(_DOCUMENTS)}>"
    return entry


# --- the comparison and the command line ----------------------------------------------------------

_ADVICE = {
    tool.BREAKING: (
        "Breaking - removed or narrowed. If intended, add a `**Breaking:**` entry under "
        "[Unreleased] in CHANGELOG.md that says what users must change (the next release is then "
        "a major one; a false alarm: a waiver, docs/development.md#compatibility):"
    ),
    tool.CHANGED: (
        "Changed - compatible, but users notice it (a default, a wider range); describe it "
        "under ### Changed (the next release is then a minor one):"
    ),
    tool.ADDED: (
        "Added - describe it under ### Added and in the interface's documentation (the next "
        "release is then a minor one):"
    ),
}


# the release plan compares with the last release, `make interface` with the committed snapshot
_AGAINST = (
    "These kinds are against the committed snapshot. What needs a changelog entry is what `git"
    " fetch --tags origin && make release-plan` reports against the last release: a removal of"
    " something no release had needs none."
)


def _findings(findings: Sequence[Finding]) -> list[str]:
    lines: list[str] = []
    for kind, advice in _ADVICE.items():
        if found := [f"  {finding}" for finding in findings if finding.kind == kind]:
            lines += [advice, *found]
    return [*lines, _AGAINST] if lines else []


def explain(findings: Sequence[Finding]) -> str:
    """What the code changed against the committed snapshot, and what each kind of change needs."""
    head = f"{_shown(SNAPSHOT)} differs from the public interface of the code."
    if not findings:
        return f"{head} It differs only in its form: `make interface` writes it again."
    tail = (
        "Then `make interface` writes the snapshot again; commit it. A removed nested key can "
        "also mean that the fixture in tests/public_interface.py no longer fills a list."
    )
    return "\n".join([head, *_findings(findings), tail])


def _shown(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def _report(committed: str, current: dict[str, Any], *, written: bool) -> str:
    """What the code changed against the committed snapshot, also if that cannot be read."""
    try:
        findings = tool.compare(json.loads(committed), current)
    except ValueError as exc:  # not JSON (a merge conflict), or not this format (FormatError)
        unreadable = f"{_shown(SNAPSHOT)} cannot be compared with the code: {exc}"
        return unreadable if written else f"{unreadable}; `make interface` writes it again"
    if not written:
        return explain(findings)
    return "\n".join(_findings(findings)) or f"{_shown(SNAPSHOT)}: only its form changes"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the public interface of the code (snapshot)")
    parser.add_argument("--write", action="store_true", help="write the snapshot (make interface)")
    args = parser.parse_args(argv)
    current = surface()
    text = tool.render(current)
    committed = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.exists() else None
    if args.write:
        if committed is not None and committed != text:  # in the review, the diff shows it too
            sys.stdout.write(_report(committed, current, written=True) + "\n")
        SNAPSHOT.write_text(text, encoding="utf-8")
        sys.stdout.write(f"wrote {_shown(SNAPSHOT)}\n")
        return 0
    if committed == text:
        sys.stdout.write(f"{_shown(SNAPSHOT)} is the public interface of the code\n")
        return 0
    if committed is None:
        sys.stderr.write(f"{_shown(SNAPSHOT)} is missing: make interface\n")
    else:
        sys.stderr.write(_report(committed, current, written=False) + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
