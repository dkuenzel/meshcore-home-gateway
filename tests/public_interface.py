"""The public interface as data (docs/development.md#compatibility), computed from the code
without a radio or a broker; tests/public_interface.json is its snapshot.

    python -m tests.public_interface           # compare; exit 1 on a difference
    python -m tests.public_interface --write   # write the snapshot again (make interface)

It records the structure only: topics with their retained flag and QoS, the key paths of the
documents, the value lists the code declares in one place, every setting, command, option and
machine-readable output, and what Home Assistant refers to. The fixture's names are words that no
real name contains (the slug "s" once turned "sensor" into "<slug>ensor"); `placeholders` turns
them into the snapshot's placeholders, which belong to its format and are never renamed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import inspect
import io
import json
import logging
import math
import re
import sys
import tempfile
import typing
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest import mock

import paho.mqtt.client as paho

from meshcore_home_gateway import __version__, cli, views
from meshcore_home_gateway import config as configuration
from meshcore_home_gateway.bridge import Bridge, ha_device_topics
from meshcore_home_gateway.config import ProfileConfig, parse_config
from meshcore_home_gateway.core import profile
from meshcore_home_gateway.core.commands import Rejection
from meshcore_home_gateway.core.contacts import Contact, ContactBook
from meshcore_home_gateway.core.mesh import NODE_TYPES, Fix, Node, Tracked
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
from meshcore_home_gateway.mqtt.contact_devices import (
    ContactDevices,
    contact_device_id,
    contact_discovery_topic,
    contact_payload,
)
from meshcore_home_gateway.mqtt.discovery import (
    PROFILE_STATES,
    device_id,
    discovery_payload,
    discovery_topic,
)
from meshcore_home_gateway.mqtt.manage_entities import manage_payload, manage_topic
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import AutoAdd, NodeType
from meshcore_home_gateway.radio.session import (
    AdvertHeard,
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

BASE, NODE, PREFIX, STATUS = "qbaseq", "qnodeq", "qprefixq", "qstatusq"
ALIAS, CHANNEL_ALIAS, WORD, SLUG = "qaliasq", "qchannelq", "qwordq", "qslugq"
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
MAXIMAL = parse_config(MAXIMAL_TEXT)
DEVICE = device_id(MAXIMAL)

_PLACEHOLDERS = (
    (DEVICE, "<device>"),  # first: it holds the node ID
    (BASE, "<base_topic>"),
    (NODE, "<node_id>"),
    (PREFIX, "<prefix>"),
    (STATUS, "<status_topic>"),
    (ALIAS, "<alias>"),
    (CHANNEL_ALIAS, "<alias>"),
    (SLUG, "<slug>"),
    (WORD, "<commands.words>"),
    *((key, "<key>") for key in (KEY, KEY_B, KEY_N)),
)
_DOCUMENTS = "<base_topic>/<node_id>/"  # the topics of the documents and events
_LEFTOVER = re.compile(r"q[a-z]+q")


def placeholders(text: str) -> str:
    """`text` with each fixture value replaced by its placeholder."""
    for value, placeholder in _PLACEHOLDERS:
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
    raising it, so a broken path would only show as a topic missing from the snapshot. Warnings
    of the scenario stay off standard error."""
    handler = _Records()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
    errors = [handler.format(r) for r in handler.records if r.levelno >= logging.ERROR]
    if errors:
        raise AssertionError("logged while computing the public interface:\n" + "\n".join(errors))


def refuse_leftovers(doc: dict[str, Any]) -> None:
    """A fixture value that `placeholders` missed would tie the snapshot to the fixture."""
    text = json.dumps(doc)
    keys = {key[:8] for key in (KEY, KEY_B, KEY_N) if key[:8] in text}
    if left := sorted(set(_LEFTOVER.findall(text)) | keys):
        raise AssertionError(f"fixture values left in the snapshot: {left}")


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
        self.will: tuple[str, bytes] | None = None
        self.subscribed: list[str] = []
        self.published: list[tuple[str, bytes, bool, int]] = []

    def __getattr__(self, name: str) -> Callable[..., None]:
        return lambda *_, **__: None  # connecting and the network loop: there is no broker

    def will_set(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        self.will = (topic, payload)

    def subscribe(self, topic: list[tuple[str, int]]) -> None:
        self.subscribed += [name for name, _ in topic]

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
    if client.will is None:
        raise AssertionError("MqttBus set no last will")
    will = {placeholders(client.will[0]): client.will[1].decode()}
    for topic, text in will.items():
        seen[topic].texts.add(text)
    ours = {t.removeprefix(_DOCUMENTS): e for t, e in seen.items() if t.startswith(_DOCUMENTS)}
    texts = {name: list(e.texts) for name, e in ours.items() if "text" in e.payloads}
    return {
        "publish": {topic: entry.record(topic) for topic, entry in seen.items()},
        "subscribe": [placeholders(topic) for topic in client.subscribed],
        "last_will": will,
        "documents": {name: list(e.keys) for name, e in ours.items() if "json" in e.payloads},
        "values": texts | _vocabularies(),
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


async def _published(tmp: Path) -> _Paho:
    """Every publish path of the gateway once, through the MQTT adapter: the bridge never runs,
    its observer and a few of its steps are called directly."""
    client = _Paho()
    topics = Topics(BASE, NODE)
    bus = mqtt_client.MqttBus(
        MAXIMAL.mqtt, topics, MAXIMAL.homeassistant.status_topic,
        client_factory=lambda _: client, ha_devices=ha_device_topics(MAXIMAL),
    )  # fmt: skip
    await bus.start()
    bus._paho_connected(client, None, None, SimpleNamespace(is_failure=False), None)
    await asyncio.sleep(0)  # the connection reaches the event loop
    store = JsonStateFile(tmp / "state.json")
    records = ContactDeviceRecords(store)
    devices = ContactDevices(MAXIMAL, topics, bus, records, wall_clock=lambda: T)
    bridge = Bridge(
        MAXIMAL, bus, _no_radio, CommandLedger(store), notifier=lambda _: True,
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
    wanted = cast(dict[str, profile.Value], {**live, profile.MANUAL_ADD: True})
    observer.profile_applied(
        ProfileReport(
            "auto", 14, True, False, live, wanted,
            written={profile.MANUAL_ADD: True}, reasons={profile.MANUAL_ADD: "initial"},
        )
    )  # fmt: skip
    bridge._deliver_event(DeliveryEvent("id", ALIAS, DeliveryState.CONFIRMED, text="Hello"))
    bridge._publish_announcements()  # discovery, the controls, the contacts' devices; state
    message = InboundMessage(MessageKind.CONTACT, f"{WORD} now", int(T), T, sender_prefix=KEY[:12])
    book = ContactBook([Contact(KEY, "Alice")])
    session = SimpleNamespace(contacts=book, refresh_contacts=_refreshed)
    await bridge._on_message(MessageReceived(message, cast(CompanionSession, session), 0, False))
    bridge._events.put_nowait(PacketLogged(codec.RxLog(1.0, -90, b"\x00"), T))
    events = asyncio.create_task(bridge._event_loop())
    await bridge._events.join()
    events.cancel()
    await bus.stop()
    return client


def _vocabularies() -> dict[str, list[str]]:
    """The value lists that the code declares in one place, as "<document>:<key path>". Values
    built in several places (the reasons, a setting's state) wait for an enum of their own."""
    gateway = discovery_payload(MAXIMAL, Topics(BASE, NODE))["cmps"]
    every_type = sum(AutoAdd)  # the overwrite flag and every node type
    telemetry = [views.setting_value(profile.TELEMETRY, modes) for modes in range(4)]
    return {
        "state:radio": [gateway["radio"]["payload_on"], gateway["radio"]["payload_off"]],
        "delivery:event_type": [state.value for state in DeliveryState],
        "received:event_type": [kind.value for kind in MessageKind],
        "received:text_type": [kind.name.lower() for kind in TextType],
        "received:command.reason": [reason.value for reason in Rejection],
        "contact:event_type": gateway["contact"]["event_types"],
        "node:event_type": gateway["node"]["event_types"],
        "setting:event_type": gateway["setting"]["event_types"],
        "settings:status": PROFILE_STATES,
        "settings:mode": [mode.value for mode in profile.Mode],
        "settings:settings": list(views.SETTING_NAMES.values()),
        "settings:settings.autoadd.live": cast(
            list[str], views.setting_value(profile.AUTOADD, every_type)
        ),
        "settings:settings.telemetry.live.*": [
            mode for kinds in telemetry for mode in cast(dict[str, str], kinds).values()
        ],
        "contacts/<key>:type": list(NODE_TYPES.values()),
        "contacts/<key>:flags": list(views._FLAGS.values()),
        "stats:errors": list(views._ERRORS.values()),
    }


# --- configuration --------------------------------------------------------------------------------

_KINDS = {
    "string": "string", "boolean": "boolean", "integer": "integer", "number": "number",
    "strings": "list[string]", "integers": "list[integer]", "section": "table",
}  # fmt: skip
_UNCONDITIONAL = ("default", "required")  # facts of a setting that every file has
# brokers, ACLs and Home Assistant refer to the names these defaults make up
_IDENTITY = (
    "mqtt.base_topic",
    "mqtt.client_id",
    "homeassistant.prefix",
    "homeassistant.status_topic",
)


def config() -> dict[str, dict[str, Any]]:
    """Every setting as the parser reads it (every read goes through a typed method of
    config._Section), with its choices and its answer type for the installer."""
    always = _reads(MINIMAL)
    settings = {}
    for key, entry in (_reads(MAXIMAL_TEXT) | always).items():
        if key in always:
            settings[key] = entry
        else:  # read only when present: the default passed is no default
            kept = {name: value for name, value in entry.items() if name not in _UNCONDITIONAL}
            settings[key] = kept | {"conditional": True}
    # choices that the parser checks after reading the string
    hints = (
        ("radio.transport", typing.get_type_hints(configuration.RadioConfig)["transport"]),
        ("radio.profile.manage", typing.get_type_hints(configuration.ProfileConfig)["manage"]),
    )
    for key, hint in hints:
        settings[key]["choices"] = list(typing.get_args(hint))
    settings["diagnostics.log_level"]["choices"] = list(configuration.LOG_LEVELS)
    settings["mqtt.client_id"]["default"] = _client_id()
    for key in _IDENTITY:
        settings[key]["identity"] = True
    for key, answer in configuration._ANSWER_TYPES.items():
        if key not in settings:
            raise AssertionError(f"{key}: an answer type for a setting that is never read")
        settings[key]["answer"] = answer.__name__
    return settings


def _reads(text: str) -> dict[str, dict[str, Any]]:
    """What parse_config asks of its tables for `text`, by setting."""
    reads: dict[str, dict[str, Any]] = {}

    def recording(method: str) -> Callable[..., Any]:
        original = getattr(configuration._Section, method)
        signature = inspect.signature(original)

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
    entry: dict[str, Any] = {"type": _KINDS[method]}
    if arguments.get("default") is not None:
        entry["default"] = arguments["default"]
    if arguments.get("required"):
        entry["required"] = True
    for parameter, name in (("lo", "min"), ("hi", "max")):
        bound = arguments.get(parameter)
        if bound is not None and not math.isinf(bound):
            entry[name] = bound
    return entry


def _client_id() -> str:
    """The client ID the broker sees when the file sets none (mqtt/client.py)."""
    with mock.patch.object(paho, "Client") as client:
        mqtt_client.make_paho_client(parse_config(MINIMAL).mqtt)
    return placeholders(client.call_args.kwargs["client_id"])


# --- command line ---------------------------------------------------------------------------------

_TYPES: dict[Any, str] = {
    Path: "path", cli.tcp_address: "host[:port]", cli._wait_seconds: "seconds", cli._count: "count"
}  # fmt: skip


def command_line(tmp: Path) -> dict[str, Any]:
    commands: dict[str, Any] = {}
    _commands(cli._parser(), "", commands)
    report = _probe_report(tmp)
    keyed_by_data = ("profile.changes", "profile.contacts")
    exit_codes = {
        "ok": cli.EX_OK, "failure": cli.EX_FAILURE, "usage": cli.EX_USAGE,
        "config_error": cli.EX_CONFIG,
    }  # fmt: skip
    return {
        "commands": commands,
        "exit_codes": exit_codes,
        "paths": {"config": str(cli.DEFAULT_CONFIG), "state_dir": str(cli.DEFAULT_STATE_DIR)},
        "columns": _columns(report, tmp),
        "json": {"probe --json": list(_key_paths(report, maps=keyed_by_data))},
    }


def _commands(parser: argparse.ArgumentParser, path: str, out: dict[str, Any]) -> None:
    options: dict[str, Any] = {}
    positionals: dict[str, Any] = {}  # "1", "2", …: their order is the interface
    commands: argparse._SubParsersAction[argparse.ArgumentParser] | None = None
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if isinstance(action, argparse._SubParsersAction):
            commands = action
        elif isinstance(action, argparse._VersionAction):
            output = str(action.version).replace(__version__, "{version}")
            options.update((name, {"output": output}) for name in action.option_strings)
        elif action.option_strings:
            options.update((name, _argument(action)) for name in action.option_strings)
        else:
            positionals[str(len(positionals) + 1)] = _argument(action)
    entry: dict[str, Any] = {"options": options, "positionals": positionals}
    out[path or parser.prog] = entry
    if commands is not None:
        entry["commands"] = list(commands.choices)
        for name, command in commands.choices.items():
            _commands(command, f"{path} {name}".strip(), out)


def _argument(action: argparse.Action) -> dict[str, Any]:
    entry: dict[str, Any] = {"takes_value": action.nargs != 0}
    if action.required:
        entry["required"] = True
    if action.choices is not None:
        entry["choices"] = list(action.choices)
    if action.nargs not in (None, 0):
        entry["nargs"] = action.nargs
    if action.type is not None:
        if action.type not in _TYPES:
            raise AssertionError(f"{action.dest}: name the type {action.type!r} in _TYPES")
        entry["type"] = _TYPES[action.type]
    default = action.default  # `is`: a default of 0 or 0.0 equals False
    if default is not None and default is not False and default is not argparse.SUPPRESS:
        entry["default"] = str(default) if isinstance(default, Path) else default
    return entry


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


def _columns(report: dict[str, Any], tmp: Path) -> dict[str, int]:
    """Fields per line of the tab-separated lists, which shell scripts read with `read` (the
    installer does); `probe --list profile` by line kind."""

    async def probed(*_: Any, **__: Any) -> dict[str, Any]:
        return report

    columns: dict[str, int] = {}
    with mock.patch.object(cli, "_probe_radio", probed):
        for listing in ("channels", "contacts", "profile"):
            out = io.StringIO()
            argv = ["probe", "--tcp", "192.0.2.10", "--state-dir", str(tmp), "--list", listing]
            with contextlib.redirect_stdout(out):
                status = cli.main(argv)
            lines = [line.split("\t") for line in out.getvalue().splitlines()]
            if status != cli.EX_OK or not lines:
                raise AssertionError(f"probe --list {listing}: exit {status}, {len(lines)} lines")
            for fields in lines:
                kind = f": {fields[0]}" if listing == "profile" else ""
                name = f"probe --list {listing}{kind}"
                if columns.setdefault(name, len(fields)) != len(fields):
                    raise AssertionError(f"{name}: lines of {columns[name]}, {len(fields)} fields")
    columns["contacts list"] = len(cli._list_line("contact", {"key": KEY}, "-").split("\t"))
    return columns


# --- Home Assistant -------------------------------------------------------------------------------


def homeassistant() -> dict[str, Any]:
    topics = Topics(BASE, NODE)
    contact = contact_device_id(MAXIMAL, KEY)
    contacts: dict[str, dict[str, Any]] = {}
    for node_type in NodeType:  # the components of a contact depend on its type
        tracked = Tracked(Node(KEY, "Alice", node_type), fix=Fix(Position(1.0, 2.0), T, "advert"))
        found = _components(contact_payload(MAXIMAL, topics, tracked, SLUG)["cmps"], contact)
        for key, entry in found.items():
            known = contacts.setdefault(key, entry | {"node_types": []})
            if {name: value for name, value in known.items() if name != "node_types"} != entry:
                raise AssertionError(f"contact component {key}: other facts for {node_type.name}")
            known["node_types"].append(node_type.name.lower())
    return {
        "devices": {
            "gateway": {
                "id": DEVICE.replace(NODE, "<node_id>"),
                "topic": placeholders(discovery_topic(MAXIMAL)),
            },
            "manage_contacts": {"topic": placeholders(manage_topic(MAXIMAL))},
            "contact": {
                "id": placeholders(contact),
                "topic": placeholders(contact_discovery_topic(MAXIMAL, KEY)),
            },
        },
        "components": {
            "gateway": _components(discovery_payload(MAXIMAL, topics)["cmps"], DEVICE),
            "manage_contacts": _components(manage_payload(MAXIMAL, topics, [])["cmps"], DEVICE),
            "contact": contacts,
        },
    }


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
        "a major one):"
    ),
    tool.CHANGED: (
        "Changed - compatible, but users notice it (a default, a wider range); describe it "
        "under ### Changed:"
    ),
    tool.ADDED: "Added - describe it under ### Added and in the interface's documentation:",
}


def _findings(findings: Sequence[Finding]) -> list[str]:
    lines: list[str] = []
    for kind, advice in _ADVICE.items():
        if found := [f"  {finding}" for finding in findings if finding.kind == kind]:
            lines += [advice, *found]
    return lines


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
    except ValueError as exc:  # not JSON, or not this format (FormatError)
        return f"{_shown(SNAPSHOT)} cannot be compared with the code: {exc}"
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
