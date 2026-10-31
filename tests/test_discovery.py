"""Home Assistant discovery document (rules from docs/home-assistant.md)."""

import copy
import re
from dataclasses import replace
from typing import Any

import pytest

from meshcore_home_gateway import __version__, i18n
from meshcore_home_gateway.config import DEFAULT_DEVICE_NAME, Config, parse_config
from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.core.lpp import Named
from meshcore_home_gateway.core.mesh import (
    AirtimeMeter,
    Fix,
    Mesh,
    Node,
    Readings,
    Tracked,
    type_name,
)
from meshcore_home_gateway.core.model import Position
from meshcore_home_gateway.core.profile import ProfileReport
from meshcore_home_gateway.mqtt.contact_devices import contact_payload, removal_payload
from meshcore_home_gateway.mqtt.discovery import (
    device_name,
    discovery_payload,
    discovery_topic,
    placeholder,
)
from meshcore_home_gateway.mqtt.manage_entities import manage_payload
from meshcore_home_gateway.mqtt.payloads import rx_payload
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.protocol.codec import DeviceInfo, SelfInfo
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.radio.session import RadioInfo
from meshcore_home_gateway.views import (
    contacts_document,
    nodes_document,
    settings_document,
    stats_document,
)
from tests import public_interface as interface
from tests.i18n_helpers import pseudo, speaking
from tests.radio_emulator import LPP_ALL_READINGS

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
    "spreading_factor", "coding_rate", "tx_power_dbm", "contact_link", "version", "telemetry",
    "telemetry_reason", "telemetry_airtime_1h_s", "telemetry_mesh_airtime_1h_s",
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
                keys |= template_keys(component.get(option, ""))
    return keys


def template_keys(template: str) -> set[str]:
    """The keys a template reads: `value_json.key` and `value_json.get('key', …)`."""
    read = set(re.findall(r"value_json\.(\w+)", template)) - {"get"}
    return read | set(re.findall(r"value_json\.get\('(\w+)'", template))


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
        "advert_position", "look_alike", "contact", "node", "show_hidden", "profile", "setting",
        "telemetry_status", "telemetry_airtime", "telemetry_mesh_airtime", "telemetry",
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
    # newer than the state documents of earlier versions: read without a warning there
    assert "value_json.get('contact_link')" in cmps["radio"]["json_attributes_template"]


def test_a_look_alike_node_is_a_problem() -> None:
    sensor = discovery_payload(CONFIG, TOPICS)["cmps"]["look_alike"]
    assert (sensor["p"], sensor["device_class"]) == ("binary_sensor", "problem")
    assert sensor["entity_category"] == "diagnostic"
    assert sensor["state_topic"] == sensor["json_attributes_topic"] == TOPICS.nodes
    assert "look_alike_total is defined" in sensor["value_template"]  # earlier documents
    assert "value_json.get('look_alike', [])" in sensor["json_attributes_template"]


def test_the_companion_position_sensor_follows_the_households_companions() -> None:
    """On while a companion not excluded from telemetry sends its position in its newest advert,
    as the gateway checked it (design 6c D11). An index of 1.1.0a2 lacks the keys: `is number`
    and `.get` log nothing in Home Assistant (home-assistant/tests/test_adverts.py)."""
    sensor = discovery_payload(CONFIG, TOPICS)["cmps"]["advert_position"]
    assert (sensor["p"], sensor["name"], sensor["device_class"], sensor["entity_category"]) == (
        "binary_sensor", "Companion position in adverts", "problem", "diagnostic",
    )  # fmt: skip
    assert sensor["state_topic"] == sensor["json_attributes_topic"] == TOPICS.contacts
    assert sensor["value_template"] == (
        "{{ ('ON' if value_json.advert_carries_position_total > 0 else 'OFF') if value_json is "
        "defined and value_json.advert_carries_position_total is number else None }}"
    )
    assert sensor["json_attributes_template"] == (
        "{{ {'contacts': value_json.get('advert_carries_position', []), "
        "'total': value_json.get('advert_carries_position_total', 0)} | tojson }}"
    )
    read = template_keys(sensor["value_template"] + sensor["json_attributes_template"])
    assert read == {"advert_carries_position", "advert_carries_position_total"}


def test_the_telemetry_entities_are_always_there() -> None:
    """Also with telemetry off: a disabled component that goes takes the whole device away."""
    off = parse_config(
        '[radio]\ntransport = "tcp"\nhost = "h"\n[mqtt]\nhost = "h"\nnode_id = "radio-1"\n'
        "[telemetry]\nenabled = false\n"
    )
    for config in (CONFIG, off):
        cmps = discovery_payload(config, TOPICS)["cmps"]
        status = cmps["telemetry_status"]
        assert (status["p"], status["name"], status["device_class"]) == (
            "sensor", "Telemetry status", "enum",
        )  # fmt: skip
        assert status["options"] == list(telemetry.STATUSES)
        assert status["entity_category"] == "diagnostic"
        assert status["state_topic"] == status["json_attributes_topic"] == TOPICS.state
        assert status["value_template"] == (
            "{{ value_json.telemetry if value_json is defined "
            "and value_json.telemetry is string else None }}"
        )
        assert status["json_attributes_template"] == (
            "{{ {'reason': value_json.get('telemetry_reason')} | tojson }}"
        )
        lanes = (("telemetry_airtime", "Telemetry airtime last hour"),
                 ("telemetry_mesh_airtime", "Telemetry mesh airtime last hour"))  # fmt: skip
        for key, name in lanes:
            lane = cmps[key]
            assert (lane["p"], lane["name"], lane["state_topic"]) == ("sensor", name, TOPICS.state)
            assert (lane["device_class"], lane["unit_of_measurement"]) == ("duration", "s")
            assert (lane["state_class"], lane["suggested_display_precision"]) == ("measurement", 1)
            assert lane["entity_category"] == "diagnostic"
            assert lane["enabled_by_default"] is False  # a row a minute: off until wanted
        assert cmps["telemetry_airtime"]["value_template"] == (
            "{{ value_json.get('telemetry_airtime_1h_s') }}"
        )
        assert cmps["telemetry_mesh_airtime"]["value_template"] == (
            "{{ value_json.get('telemetry_mesh_airtime_1h_s') }}"
        )
        event = cmps["telemetry"]
        assert (event["p"], event["name"], event["state_topic"]) == (
            "event", "Telemetry", "meshcore/radio-1/telemetry",
        )  # fmt: skip
        assert event["event_types"] == list(telemetry.EVENTS) == ["answered", "unanswered"]
        assert cmps["contact"]["event_types"] == [
            "added", "removed", "hidden", "shown", "refused", "failed", "excluded", "included",
            "position_in_advert",
        ]  # fmt: skip


def test_every_platform_announced_can_be_removed() -> None:
    """The gateway's, its controls' and every contact's components: each platform has a
    placeholder that binds what Home Assistant kept from an earlier run."""
    position = Position(52.1, 9.2)
    readings = Readings(0.0, Named(4.12, 23.4, {"ch2_temperature_c": 21.3}))
    found = [
        *discovery_payload(CONFIG, TOPICS)["cmps"].values(),
        *manage_payload(CONFIG, TOPICS, [])["cmps"].values(),
    ]
    for node_type in NodeType:
        tracked = Tracked(Node(KEY, "Alice", node_type), fix=Fix(position, 0.0, "telemetry"),
                          readings=readings)  # fmt: skip
        found += contact_payload(CONFIG, TOPICS, tracked, "alice")["cmps"].values()
    platforms = {component["p"] for component in found}
    assert {"switch", "device_tracker", "select", "text", "notify", "button"} <= platforms
    for platform in platforms:
        stand_in = placeholder(f"uid_{platform}", platform, TOPICS, i18n.ENGLISH.ha_name)
        assert stand_in is not None, platform
        assert stand_in["p"] == platform


def test_select_and_text_components_can_be_removed() -> None:
    """The controls that add contacts are removed like any component: placeholders bind what
    Home Assistant kept from an earlier run (home-assistant/tests)."""
    english = i18n.ENGLISH.ha_name
    for platform in ("select", "text"):
        stand_in = placeholder(f"uid_{platform}", platform, TOPICS, english)
        assert stand_in is not None
        assert stand_in["command_topic"] == "meshcore/radio-1/removed"
    select = placeholder("uid_select", "select", TOPICS, english)
    assert select is not None
    assert select["options"] == ["removed"]


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


# --- the language of the names (ADR 0019) --------------------------------------------------------

SPOKEN = ["xx", *(code for code in i18n.LANGUAGES if code != "en")]  # xx: tests/i18n_helpers.py
# a name as the pseudo-language shows it: the reading names add their channel and occurrence
MARKED = re.compile(r"‹[^‹›]+›(?: \d+(?: \(\d+\))?)?")
UNKNOWN_MODEL = type_name(NodeType.NONE)  # a node type without a model name of its own


def speak(language: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Offer the pseudo-language; a shipped language is offered already."""
    if language == "xx":
        speaking(monkeypatch, pseudo())


def _maximal(language: str, settings: str = "") -> Config:
    """Every component of the gateway (tests/public_interface.py) with names in `language`."""
    table = f'[homeassistant]\nlanguage = "{language}"\n{settings}'
    return parse_config(interface.MAXIMAL_TEXT.replace("[homeassistant]\n", table))


def _documents(config: Config) -> list[dict[str, Any]]:
    """What the gateway sends to Home Assistant: its document with removed components (as
    placeholders and as removed), the controls, a contact of every node type with every reading
    (two temperatures on one channel too), and a contact's removal."""
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    removed = {"send_old": "notify", "old_switch": "switch"}
    position = Position(52.1, 9.2)
    readings = Readings(0.0, Named(4.12, 23.4, LPP_ALL_READINGS))
    documents = [
        discovery_payload(config, topics, RADIO, removed, bind=True),
        discovery_payload(config, topics, RADIO, removed),
        manage_payload(config, topics, ["a1a1a1a1a1a1…a1a1a1a1 Alice"], removed, bind=True),
        manage_payload(config, topics, [], removed),
    ]
    for node_type in NodeType:
        node = Node(KEY, "Alice", node_type, position=position)
        tracked = Tracked(node, fix=Fix(position, 0.0, "telemetry"), readings=readings)
        documents.append(contact_payload(config, topics, tracked, "alice"))
    platforms = {"send": "notify", "ch2_temperature_2": "sensor", "position": "device_tracker"}
    documents.append(removal_payload(config, topics, KEY, platforms))
    return documents


def _named_fields(device: dict[str, Any]) -> list[str]:
    """The fields of a device that are names: the gateway's name, a contact's model. A
    contact's name, the radio's model and an unknown node type are data."""
    if "via_device" not in device:
        return ["name"]
    return ["mdl"] if device.get("mdl", UNKNOWN_MODEL) != UNKNOWN_MODEL else []


def _names(documents: list[dict[str, Any]]) -> list[str]:
    """Every name in the documents: of the components, the gateway and the contacts' models."""
    found = [c["name"] for d in documents for c in d["cmps"].values() if c.get("name")]
    return found + [d["dev"][field] for d in documents for field in _named_fields(d["dev"])]


def _without_names(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The documents with every name replaced; a component without a name keeps its None or
    its missing name, and data stays as it is."""
    found = copy.deepcopy(documents)
    for document in found:
        for component in document["cmps"].values():
            if isinstance(component.get("name"), str):
                component["name"] = "<name>"
        for field in _named_fields(document["dev"]):
            document["dev"][field] = "<name>"
    return found


@pytest.mark.parametrize("language", SPOKEN)
def test_only_names_change_with_the_language(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entity IDs, unique IDs, topics, templates, event types, options, units and classes are
    the same in every language; every name is translated, models and reading types included,
    and an entity without a name stays without one (ADR 0019)."""
    speak(language, monkeypatch)
    english, spoken = _documents(_maximal("en")), _documents(_maximal(language))
    assert _without_names(spoken) == _without_names(english)
    assert len(_names(spoken)) == len(_names(english)) > 60
    assert _names(spoken) != _names(english)
    if language == "xx":
        unmarked = [name for name in _names(spoken) if not MARKED.fullmatch(name)]
        assert unmarked == []


@pytest.mark.parametrize("language", ["en", *SPOKEN])
def test_default_device_name_follows_the_language(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default device name is the translation, also where the file names it (the installer
    writes it); a name of one's own stays. Both documents of the gateway's device name it alike,
    else Home Assistant would rename the device at each of them."""
    speak(language, monkeypatch)
    ha_name = i18n.translator(language).ha_name
    for settings, name in (
        ("", ha_name("MeshCore radio")),
        (f'device_name = "{DEFAULT_DEVICE_NAME}"\n', ha_name("MeshCore radio")),
        ('device_name = "Greenhouse"\n', "Greenhouse"),
    ):
        config = _maximal(language, settings)
        topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
        gateway = discovery_payload(config, topics, RADIO)["dev"]["name"]
        assert (gateway, manage_payload(config, topics, [])["dev"]["name"]) == (name, name)
        assert device_name(config, ha_name) == name
    if language != "en":
        assert ha_name("MeshCore radio") != DEFAULT_DEVICE_NAME


def test_data_in_names_stays_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """A target's alias is a value of its name; the default device name in English is the
    setting's default, so the literal the translators see is that default."""
    speaking(monkeypatch, pseudo())
    cmps = discovery_payload(_maximal("xx"), TOPICS)["cmps"]
    assert cmps[f"send_{interface.ALIAS}"]["name"] == f"‹Send to {interface.ALIAS}›"
    english = _maximal("en")
    assert discovery_payload(english, TOPICS)["dev"]["name"] == DEFAULT_DEVICE_NAME
    assert device_name(english, i18n.ENGLISH.ha_name) == DEFAULT_DEVICE_NAME
