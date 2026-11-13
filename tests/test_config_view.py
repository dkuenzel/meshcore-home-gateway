"""The `config` document (config_view.py, views.config_document, design §6.5): the settings in
effect by an allowlist, every key always present, bounded; published at the broker connect and at
the radio connect."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway.config import Config, load_config, parse_config
from meshcore_home_gateway.config_view import shown_settings, target_settings
from meshcore_home_gateway.mqtt.discovery import discovery_payload
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.ports import RadioInfo
from meshcore_home_gateway.protocol.codec import DeviceInfo, SelfInfo
from meshcore_home_gateway.views import ATTRIBUTE_LIMIT_BYTES, config_document, encode, iso
from tests.bridge_harness import (
    NOW,
    fast_timers,  # noqa: F401 - autouse
    make_config,
    running,
    scenario,
)
from tests.radio_emulator import EmulatedRadio

T = 1_800_000_000.0
KEY, SENDER = "0123456789abcdef" * 4, "5e" * 32  # a contact target; a command sender only
TOPICS = Topics("meshcore", "radio1")


def radio_named(name: str) -> RadioInfo:
    return RadioInfo(
        SelfInfo(bytes.fromhex("c3" * 32), name, 22, 22, 869_618, 62_500, 8, 8, True),
        DeviceInfo(9, 350, 40, "1 Oct 2026", "Example Radio", "v1.18.0", 0),
    )


def document(config: Config, radio: RadioInfo | None = None) -> dict[str, Any]:
    return config_document(T, target_settings(config, radio), shown_settings(config))


def key_paths(value: Any, prefix: str = "") -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            yield path
            yield from key_paths(item, path)
    elif isinstance(value, list):
        for item in value:
            yield from key_paths(item, f"{prefix}[]")


# the allowlist: a key added here is a decision, not a side effect
SHOWN = {
    "started_at",
    "targets",
    "targets.total",
    "targets.truncated",
    "targets.items",
    *(
        f"targets.items[].{key}"
        for key in (
            "alias",
            "kind",
            "channel_index",
            "key_start",
            "prefix",
            "suffix",
            "alert",
            "hidden",
            "room",
        )
    ),
    "messages",
    *(
        f"messages.{key}"
        for key in (
            "publish_channels",
            "ignore_channels",
            "confirm_nonce",
            "clean_received",
            "truncate",
            "ascii_only",
            "dedup_window_s",
            "queue_size",
            "queue_ttl_s",
            "max_attempts",
        )
    ),
    "contacts",
    "contacts.manage",
    "contacts.heard_nodes",
    "telemetry",
    "telemetry.enabled",
    "telemetry.ask",
    "telemetry.ask_types",
    "homeassistant",
    *(
        f"homeassistant.{key}"
        for key in (
            "controls",
            "radio_controls",
            "hidden_targets",
            "contact_entity_prefix",
            "language",
            "contact_devices",
        )
    ),
    "radio",
    "radio.transport",
    "radio.tcp_unauthenticated",
    "radio.set_clock",
    "radio.profile",
    "radio.profile.manage",
    "airtime",
    "airtime.budget_s_per_hour",
    "airtime.burst_s",
    "history",
    "history.enabled",
    "history.messages_days",
    "history.positions_days",
    "history.readings_days",
    "commands",
    "commands.senders",
    "commands.codes",
    "commands.reply",
}

# a value in every setting the document must never show: hosts, ports, device paths, user
# names, password files, client IDs, keys, the codes file, command words, and the rest that is
# not on the list
SENTINEL_TCP = f"""
[radio]
transport = "tcp"
tcp_unauthenticated = true
host = "qradiohostq.example.org"
port = 47011
set_clock_max_days = 3011
[radio.profile]
name = "qRadioNameq"
duty_cycle_percent = 47.25
protect_contacts = false
[mqtt]
host = "qbrokerhostq.example.org"
port = 47014
username = "qUserNameq"
password = "qPasswordq"
tls = true
ca_file = "/qcafileq/ca.pem"
cert_file = "/qcertfileq/cert.pem"
key_file = "/qkeyfileq/key.pem"
client_id = "qClientIdq"
keepalive_s = 3014
base_topic = "qBaseTopicq"
node_id = "qnodeidq"
protocol = "5"
[homeassistant]
prefix = "qDiscoveryPrefixq"
device_name = "qDeviceNameq"
status_topic = "qStatusTopicq/qBirthq"
[messages]
max_attempts = 4
flood_after = 3
[commands]
senders = ["{SENDER}"]
words = ["qCommandWordq", "qsecondwordq"]
max_age_s = 47016
codes_file = "/qcodesfileq/codes"
reply = "all"
[targets.family]
channel = 1
[targets.alice]
contact = "{KEY}"
[diagnostics]
publish_rx_log = true
log_level = "debug"
[homie]
enabled = true
contacts = 917
"""
SENTINEL_SERIAL = """
[radio]
transport = "serial"
device = "/dev/qttydeviceq"
[mqtt]
host = "192.0.2.47"
node_id = "radio1"
username = "qUserNameq"
password_file = "/qpasswordfileq/mqtt-password"
"""
NEVER = (
    "qradiohostq",
    "47011",
    "3011",
    "qRadioNameq",
    "47.25",
    "qbrokerhostq",
    "192.0.2.47",
    "47014",
    "qUserNameq",
    "qPasswordq",
    "qcafileq",
    "qcertfileq",
    "qkeyfileq",
    "qClientIdq",
    "3014",
    "qBaseTopicq",
    "qnodeidq",
    "qDiscoveryPrefixq",
    "qDeviceNameq",
    "qStatusTopicq",
    "qBirthq",
    "qcommandwordq",  # as the configuration takes command words
    "qCommandWordq",
    "qsecondwordq",
    "47016",
    "qcodesfileq",
    "917",
    "qttydeviceq",
    "qpasswordfileq",
    KEY,
    KEY[8:16],  # more of a target's key than its first 8 digits
    SENDER[:8],  # anything of a sender's key
)


@pytest.mark.parametrize("text", [SENTINEL_TCP, SENTINEL_SERIAL], ids=["tcp", "serial"])
def test_the_document_shows_only_the_allowed_settings(text: str) -> None:
    """Every setting not on the list holds a value of its own: none of them appears, and the
    document has exactly the keys of the list."""
    config = parse_config(text)
    shown = document(config, radio_named("qRadioNameq"))
    written = encode(shown).decode()
    assert [value for value in NEVER if value in written] == []
    assert set(key_paths(shown)) == (SHOWN if config.targets else SHOWN - _ITEMS)


_ITEMS = {path for path in SHOWN if path.startswith("targets.items[]")}


def test_the_codes_are_shown_only_as_whether_they_are_set(tmp_path: Path) -> None:
    """The codes file and the secrets in it never appear; `codes` says whether commands need
    them."""
    secret = base64.b32encode(bytes(range(20))).decode()
    codes = tmp_path / "codes"
    codes.write_text(f'{SENDER} = "{secret}"\n', encoding="utf-8")
    file = tmp_path / "config.toml"
    file.write_text(
        f"""
[radio]
transport = "serial"
device = "/dev/ttyACM0"
[mqtt]
host = "broker.example.org"
node_id = "radio1"
insecure = true
[commands]
senders = ["{SENDER}", "{KEY}"]
words = ["status"]
codes_file = "{codes}"
""",
        encoding="utf-8",
    )
    config = load_config(file)
    assert config.commands.secrets  # read, as the service reads them
    shown = document(config)
    written = encode(shown).decode()
    for never in (secret, secret.lower(), str(codes), str(tmp_path), "status", SENDER[:8], KEY):
        assert never not in written
    assert shown["commands"] == {"senders": 2, "codes": True, "reply": "none"}
    without = parse_config(file.read_text(encoding="utf-8").replace(f'codes_file = "{codes}"', ""))
    assert document(without)["commands"] == {"senders": 2, "codes": False, "reply": "none"}


def test_the_document_holds_the_settings_in_effect() -> None:
    """Each value as the gateway uses it; lists sorted, the hidden targets counted (their items
    say which)."""
    config = parse_config(f"""
[radio]
transport = "serial"
device = "/dev/ttyACM0"
set_clock = false
[radio.profile]
manage = "observe"
[mqtt]
host = "broker.example.org"
node_id = "radio1"
[homeassistant]
contact_devices = 10
controls = false
radio_controls = false
hidden_targets = ["alarm"]
contact_entity_prefix = "mesh_2"
[messages]
ascii_only = true
truncate = false
dedup_window_s = 120
queue_size = 5
queue_ttl_s = 60
max_attempts = 4
ignore_channels = [7, 3]
publish_channels = "all"
clean_received = true
confirm_nonce = true
alert_targets = ["alarm", "alice"]
[airtime]
budget_s_per_hour = 72
burst_s = 12
[history]
enabled = false
messages_days = 7
positions_days = 90
readings_days = 14
[contacts]
manage = false
heard_nodes = "off"
[telemetry]
enabled = false
ask = "all"
ask_types = ["sensor", "chat"]
[targets.family]
channel = 1
prefix = "Home: "
[targets.alarm]
channel = 2
suffix = " !"
[targets.alice]
contact = "{KEY}"
""")
    shown = document(config)
    assert shown["started_at"] == iso(T) == "2027-01-15T08:00:00+00:00"
    assert shown["messages"] == {
        "publish_channels": "all",
        "ignore_channels": [3, 7],
        "confirm_nonce": True,
        "clean_received": True,
        "truncate": False,
        "ascii_only": True,
        "dedup_window_s": 120.0,
        "queue_size": 5,
        "queue_ttl_s": 60.0,
        "max_attempts": 4,
    }
    assert shown["contacts"] == {"manage": False, "heard_nodes": "off"}
    assert shown["telemetry"] == {"enabled": False, "ask": "all", "ask_types": ["chat", "sensor"]}
    assert shown["homeassistant"] == {
        "controls": False,
        "radio_controls": False,
        "hidden_targets": 1,
        "contact_entity_prefix": "mesh_2",
        "language": "en",
        "contact_devices": 10,
    }
    assert shown["radio"] == {
        "transport": "serial",
        "tcp_unauthenticated": False,
        "set_clock": False,
        "profile": {"manage": "observe"},
    }
    assert shown["airtime"] == {"budget_s_per_hour": 72.0, "burst_s": 12.0}
    assert shown["history"] == {
        "enabled": False,
        "messages_days": 7.0,
        "positions_days": 90.0,
        "readings_days": 14.0,
    }
    assert shown["commands"] == {"senders": 0, "codes": False, "reply": "none"}
    family, alarm, alice = shown["targets"]["items"]
    assert family == {
        "alias": "family",
        "kind": "channel",
        "channel_index": 1,
        "key_start": None,
        "prefix": "Home: ",
        "suffix": "",
        "alert": False,
        "hidden": False,
        "room": 158 - len("Home: "),
    }
    assert (alarm["alert"], alarm["hidden"], alarm["suffix"]) == (True, True, " !")
    assert alice | {"room": None} == {
        "alias": "alice",
        "kind": "contact",
        "channel_index": None,
        "key_start": KEY[:8],
        "prefix": "",
        "suffix": "",
        "alert": True,
        "hidden": False,
        "room": None,
    }
    assert shown["targets"] | {"items": None} == {"total": 3, "truncated": False, "items": None}


def test_a_minimal_configuration_has_every_key() -> None:
    config = parse_config("""
[radio]
transport = "serial"
device = "/dev/ttyACM0"
[mqtt]
host = "broker.example.org"
node_id = "radio1"
""")
    shown = document(config)
    assert set(key_paths(shown)) == SHOWN - _ITEMS
    assert shown["targets"] == {"total": 0, "truncated": False, "items": []}
    assert json.loads(encode(shown)) == shown


def test_the_room_of_a_target_is_the_max_of_its_draft() -> None:
    """The bytes a text has beside the prefix and suffix: a channel's follow the radio's name
    (without one, the shortest name's); a direct text's leave room for the delivery code."""
    config = parse_config(f"""
[radio]
transport = "serial"
device = "/dev/ttyACM0"
[mqtt]
host = "broker.example.org"
node_id = "radio1"
[messages]
confirm_nonce = true
[targets.family]
channel = 1
prefix = "Home: "
[targets.alice]
contact = "{KEY}"
""")
    for radio, family_room in ((None, 152), (radio_named("Bridge"), 146)):
        items = {i["alias"]: i["room"] for i in document(config, radio)["targets"]["items"]}
        assert items["family"] == family_room
        drafts = discovery_payload(config, TOPICS, radio)["cmps"]
        assert items == {alias: drafts[f"write_{alias}"]["max"] for alias in items}
    assert items["alice"] < 160  # the delivery code takes its room


def _many_targets(count: int) -> Config:
    """`count` targets, each as large as the configuration allows: a 32-character alias, a
    32-byte prefix and suffix of characters JSON escapes, every flag false (longer than true)."""
    quotes, backslashes = '"' * 32, "\\" * 32
    tables = []
    for i in range(count):
        alias = f"t{i:03d}" + "-x" * 14
        kind = f'contact = "{i:064x}"' if i % 2 else f"channel = {i % 256}"
        tables.append(f"[targets.{alias}]\n{kind}\nprefix = '{quotes}'\nsuffix = '{backslashes}'")
    return parse_config(
        """
[radio]
transport = "serial"
device = "/dev/ttyACM0"
[mqtt]
host = "broker.example.org"
node_id = "radio1"
"""
        + "\n".join(tables)
    )


def test_200_targets_stay_within_the_bound() -> None:
    """The configuration allows any number of targets: the list is cut from the end, so that the
    whole document stays within what Home Assistant records."""
    config = _many_targets(200)
    shown = document(config, radio_named("x" * 31))
    assert len(encode(shown)) <= ATTRIBUTE_LIMIT_BYTES
    targets = shown["targets"]
    assert (targets["total"], targets["truncated"]) == (200, True)
    kept = [item["alias"] for item in targets["items"]]
    assert kept == [t.alias for t in config.targets[: len(kept)]]
    assert 0 < len(kept) < 200
    assert all(len(item["alias"]) == 32 and len(item["prefix"]) == 32 for item in targets["items"])


def test_a_list_of_targets_near_the_bound_is_whole_or_cut() -> None:
    """`truncated` is true exactly when targets were left out, and the whole document stays
    within the bound either way."""
    seen = set()
    for count in range(30, 60):
        shown = document(_many_targets(count))
        targets = shown["targets"]
        assert len(encode(shown)) <= ATTRIBUTE_LIMIT_BYTES, count
        assert targets["total"] == count
        assert targets["truncated"] is (len(targets["items"]) < count)
        seen.add(targets["truncated"])
    assert seen == {True, False}


# --- publishing ----------------------------------------------------------------------------------

CONFIG_TOPIC = "meshcore/radio1/config"  # not the discovery documents, which end in /config too


def test_the_document_goes_out_at_the_start_the_broker_connect_and_each_radio_connect(
    tmp_path: Path,
) -> None:
    """Retained; before the radio connects with the room of the shortest name, then with the
    radio's; again at a connect of the broker; after a rename at the next connect with the new
    name's room."""
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):

            def rooms() -> list[int]:
                return [
                    next(i["room"] for i in d["targets"]["items"] if i["alias"] == "alerts")
                    for d in bus.payloads(CONFIG_TOPIC)
                ]

            await bus.wait_for(CONFIG_TOPIC, lambda d: rooms()[-1] == 158 - len("Bridge"))
            assert rooms()[0] == 158  # at the start: the radio's name not known yet
            assert all(retain for t, _, retain in bus.published if t == CONFIG_TOPIC)
            assert bus.qos[CONFIG_TOPIC] == {1}
            count = len(rooms())
            for connected in bus.on_connected:  # the broker connects again
                connected()
            assert len(rooms()) == count + 1
            assert bus.payloads(CONFIG_TOPIC)[-1] == bus.payloads(CONFIG_TOPIC)[-2]
            radio.name = "Shed radio"
            radio.disconnect()
            await bus.wait_for(CONFIG_TOPIC, lambda d: rooms()[-1] == 158 - len("Shed radio"))
            started = {d["started_at"] for d in bus.payloads(CONFIG_TOPIC)}
            assert started == {iso(NOW)}  # the gateway's start, at every publish

    scenario(main)
