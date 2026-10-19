"""Home Assistant discovery document (rules from docs/home-assistant.md)."""

from meshcore_bus.config import parse_config
from meshcore_bus.mqtt.discovery import discovery_payload, discovery_topic
from meshcore_bus.mqtt.topics import Topics

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


def test_topic_uses_the_device_path_and_a_safe_object_id() -> None:
    assert discovery_topic(CONFIG) == "ha/device/meshcore_bus_radio_1/config"


def test_document_has_the_mandatory_parts() -> None:
    doc = discovery_payload(CONFIG, TOPICS)
    assert doc["dev"]["ids"] == ["meshcore_bus_radio_1"]
    assert doc["dev"]["name"] == "Mesh radio"
    assert doc["o"]["name"] == "meshcore-bus"
    assert doc["availability"] == [{"topic": "meshcore/radio-1/availability"}]
    assert doc["qos"] == 1
    for key, component in doc["cmps"].items():
        assert component["p"] in {"binary_sensor", "sensor", "event", "notify"}, key
        assert component["unique_id"] == f"meshcore_bus_radio_1_{key}"
        assert component["default_entity_id"] == f"{component['p']}.meshcore_bus_radio_1_{key}"


def test_entities_and_their_sources() -> None:
    cmps = discovery_payload(CONFIG, TOPICS)["cmps"]
    assert set(cmps) == {
        "radio", "battery", "airtime", "message", "delivery", "command",
        "send_alerts", "send_alice",
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


def test_command_entity_only_with_command_words() -> None:
    plain = parse_config(
        '[radio]\ntransport = "tcp"\nhost = "h"\n[mqtt]\nhost = "h"\nnode_id = "n"\n'
    )
    assert "command" not in discovery_payload(plain, Topics("meshcore", "n"))["cmps"]
