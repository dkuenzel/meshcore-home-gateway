"""Home Assistant discovery document (rules from docs/home-assistant.md)."""

import re
from dataclasses import replace
from typing import Any

from meshcore_home_gateway import __version__
from meshcore_home_gateway.config import parse_config
from meshcore_home_gateway.core.mesh import AirtimeMeter, Mesh
from meshcore_home_gateway.core.profile import ProfileReport
from meshcore_home_gateway.mqtt.discovery import discovery_payload, discovery_topic
from meshcore_home_gateway.mqtt.payloads import rx_payload
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.protocol.codec import DeviceInfo, SelfInfo
from meshcore_home_gateway.radio.session import RadioInfo
from meshcore_home_gateway.views import (
    contacts_document,
    nodes_document,
    settings_document,
    stats_document,
)

KEY = "a1" * 32
CONFIG = parse_config(
    f"""
[radio]
transport = "tcp"
host = "192.0.2.10"
[mqtt]
host = "broker.example.org"
node_id = "radio-1"
[homeassistant]
prefix = "ha"
device_name = "Mesh radio"
[targets.alerts]
channel = 1
[targets.alice]
contact = "{KEY}"
[commands]
senders = ["{KEY}"]
words = ["status", "arm"]
"""
)
TOPICS = Topics(CONFIG.mqtt.base_topic, CONFIG.mqtt.node_id)
RADIO = RadioInfo(
    SelfInfo(bytes.fromhex("c3" * 32), "Gateway", 22, 22, 869_618, 62_500, 8, 8, True),
    DeviceInfo(9, 350, 40, "1 Oct 2026", "Heltec V3", "v1.18.0", 0),
)
STATE_KEYS = {
    "radio", "battery_v", "airtime_1h_s", "airtime_available_s", "airtime_budget_s_per_hour",
    "queue", "radio_name", "public_key", "model", "firmware", "frequency_mhz", "bandwidth_khz",
    "spreading_factor", "coding_rate", "tx_power_dbm", "version",
}  # fmt: skip


def _values(document: Any) -> list[Any]:
    if isinstance(document, dict):
        return [v for value in document.values() for v in [value, *_values(value)]]
    if isinstance(document, list):
        return [v for value in document for v in [value, *_values(value)]]
    return []


def _template_keys(components: dict[str, dict[str, Any]], topic: str) -> set[str]:
    keys = set()
    for component in components.values():
        if topic in (component.get("state_topic"), component.get("json_attributes_topic")):
            for option in ("value_template", "json_attributes_template"):
                keys |= set(re.findall(r"value_json\.(\w+)", component.get(option, "")))
    return keys


def test_topic_uses_the_device_path_and_the_node_id() -> None:
    assert discovery_topic(CONFIG) == "ha/device/meshcore_home_gateway_radio-1/config"


def test_node_ids_that_differ_only_in_hyphen_and_underscore_are_distinct_devices() -> None:
    other = replace(CONFIG, mqtt=replace(CONFIG.mqtt, node_id="radio_1"))
    assert discovery_topic(other) != discovery_topic(CONFIG)
    ours = discovery_payload(CONFIG, TOPICS)
    theirs = discovery_payload(other, Topics(other.mqtt.base_topic, other.mqtt.node_id))
    assert ours["dev"]["ids"] != theirs["dev"]["ids"]
    unique_ids = {c["unique_id"] for c in ours["cmps"].values()}
    assert unique_ids.isdisjoint(c["unique_id"] for c in theirs["cmps"].values())


def test_document_has_the_mandatory_parts() -> None:
    doc = discovery_payload(CONFIG, TOPICS)
    assert doc["dev"]["ids"] == ["meshcore_home_gateway_radio-1"]
    assert doc["dev"]["name"] == "Mesh radio"
    assert doc["o"]["name"] == "meshcore-home-gateway"
    assert doc["availability"] == [{"topic": "meshcore/radio-1/availability"}]
    assert doc["qos"] == 1
    for key, component in doc["cmps"].items():
        assert component["p"] in {"binary_sensor", "sensor", "event", "notify", "button"}, key
        assert component["unique_id"] == f"meshcore_home_gateway_radio-1_{key}"
        # Home Assistant turns the '-' into '_' in the entity ID (home-assistant/tests)
        assert (
            component["default_entity_id"]
            == f"{component['p']}.meshcore_home_gateway_radio-1_{key}"
        )


def test_entities_and_their_sources() -> None:
    cmps = discovery_payload(CONFIG, TOPICS)["cmps"]
    assert set(cmps) == {
        "radio", "battery", "airtime", "airtime_available", "queue", "message", "delivery",
        "command", "send_alerts", "send_alice", "noise_floor", "radio_airtime", "uptime",
        "packets_received", "packets_sent", "receive_errors", "contacts", "nodes", "remote_cli",
        "advert_position", "contact", "node", "show_hidden", "profile", "setting",
    }  # fmt: skip
    assert cmps["radio"]["state_topic"] == "meshcore/radio-1/state"
    assert cmps["radio"]["payload_on"] == "connected"
    assert cmps["battery"]["unit_of_measurement"] == "V"  # "v" would be rejected
    assert cmps["battery"]["entity_category"] == "diagnostic"
    assert cmps["message"]["event_types"] == ["contact", "channel"]
    assert "queued" in cmps["delivery"]["event_types"]
    assert cmps["command"]["event_types"] == ["arm", "status"]
    assert cmps["send_alerts"]["command_topic"] == "meshcore/radio-1/send/alerts"
    for event in ("message", "delivery", "command"):
        assert "device_class" not in cmps[event]  # only doorbell/button/motion would be valid


def test_every_template_reads_a_key_of_its_document() -> None:
    """The observer's documents (views.py) have every key the templates read, also when
    nothing is known yet."""
    cmps = discovery_payload(CONFIG, TOPICS)["cmps"]
    mesh = Mesh()
    documents = {
        TOPICS.stats: stats_document(None, AirtimeMeter(), None),
        TOPICS.contacts: contacts_document(mesh),
        TOPICS.nodes: nodes_document(mesh),
        TOPICS.settings: settings_document(ProfileReport("auto", 14, True, False, {}, {}), 0.0),
    }
    for topic, document in documents.items():
        assert _template_keys(cmps, topic) <= set(document), topic
    assert _template_keys(cmps, TOPICS.contacts) >= {"total", "remote_cli_total"}
    for counter in ("packets_received", "packets_sent", "receive_errors", "uptime"):
        assert cmps[counter]["enabled_by_default"] is False


def test_the_profile_sensor_names_every_status_the_document_has() -> None:
    sensor = discovery_payload(CONFIG, TOPICS)["cmps"]["profile"]
    assert sensor["device_class"] == "enum"
    assert set(sensor["options"]) == {"ok", "written", "drift", "differs", "problem"}
    assert "json_attributes_template" not in sensor  # the whole document: no coordinates in it


def test_show_hidden_only_with_contact_devices() -> None:
    none = replace(CONFIG, homeassistant=replace(CONFIG.homeassistant, contact_devices=0))
    assert "show_hidden" not in discovery_payload(none, TOPICS)["cmps"]
    button = discovery_payload(CONFIG, TOPICS)["cmps"]["show_hidden"]
    assert button["command_topic"] == "meshcore/radio-1/homeassistant/show_hidden"
    assert button["entity_category"] == "config"


def test_command_entity_only_with_command_words() -> None:
    plain = parse_config(
        '[radio]\ntransport = "tcp"\nhost = "h"\n[mqtt]\nhost = "h"\nnode_id = "n"\n'
    )
    assert "command" not in discovery_payload(plain, Topics("meshcore", "n"))["cmps"]


def test_the_device_shows_the_radio_once_it_is_known() -> None:
    """Home Assistant rejects the whole document for one null device field, so unknown ones are
    left out; it keeps the stored values then."""
    unknown = discovery_payload(CONFIG, TOPICS)
    assert {"mdl", "sw", "sn"}.isdisjoint(unknown["dev"])
    assert None not in _values(unknown)
    known = discovery_payload(CONFIG, TOPICS, RADIO)
    assert (known["dev"]["mdl"], known["dev"]["sw"]) == ("Heltec V3", "v1.18.0")
    assert known["dev"]["sn"] == "c3" * 32
    assert known["o"]["sw"] == __version__
    assert known["o"]["url"].startswith("https://github.com/")
    assert known["cmps"] == unknown["cmps"]


def test_every_state_value_reaches_home_assistant() -> None:
    """A new key in the state document must get an entity or an attribute."""
    cmps = discovery_payload(CONFIG, TOPICS)["cmps"]
    assert _template_keys(cmps, TOPICS.state) == STATE_KEYS
    assert cmps["queue"]["value_template"] == "{{ value_json.queue }}"
    assert cmps["airtime_available"]["unit_of_measurement"] == "s"
    assert "budget_s_per_hour" in cmps["airtime_available"]["json_attributes_template"]
    assert "radio_name" in cmps["radio"]["json_attributes_template"]


def test_packet_sensors_only_with_the_rx_log_and_disabled_by_default() -> None:
    assert not {"rx_rssi", "rx_snr"} & set(discovery_payload(CONFIG, TOPICS)["cmps"])
    rx = parse_config(
        '[radio]\ntransport = "tcp"\nhost = "h"\n[mqtt]\nhost = "h"\nnode_id = "n"\n'
        "[diagnostics]\npublish_rx_log = true\n"
    )
    topics = Topics("meshcore", "n")
    cmps = discovery_payload(rx, topics)["cmps"]
    for key in ("rx_rssi", "rx_snr"):
        assert cmps[key]["state_topic"] == "meshcore/n/rx"
        assert cmps[key]["enabled_by_default"] is False
    sample = set(rx_payload(5.25, -90, b"\x01", 0.0)) - {"raw", "length", "received_at"}
    assert _template_keys(cmps, topics.rx) == sample


def test_removed_components_are_sent_once_as_their_platform() -> None:
    """Home Assistant removes a component only when it is sent as {"p": <platform>}."""
    removed = {"send_old": "notify", "send_alerts": "notify"}
    doc = discovery_payload(CONFIG, TOPICS, removed=removed)
    assert doc["cmps"]["send_old"] == {"p": "notify"}
    assert doc["cmps"]["send_alerts"]["command_topic"] == "meshcore/radio-1/send/alerts"
