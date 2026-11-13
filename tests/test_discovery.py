"""Home Assistant discovery document (rules from docs/home-assistant.md)."""

import copy
import re
from dataclasses import replace
from typing import Any

import pytest

from meshcore_home_gateway import __version__, i18n
from meshcore_home_gateway.config import DEFAULT_DEVICE_NAME, Config, parse_config
from meshcore_home_gateway.config_view import shown_settings, target_settings
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
    RADIO_CONNECTED,
    controls_off,
    device_name,
    discovery_payload,
    discovery_topic,
    never_available,
    placeholder,
)
from meshcore_home_gateway.mqtt.manage_entities import manage_payload
from meshcore_home_gateway.mqtt.payloads import rx_payload
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.ports import RadioInfo
from meshcore_home_gateway.protocol.codec import DeviceInfo, SelfInfo
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.views import (
    SELF_ADVERT_EVENTS,
    HistoryEntry,
    config_document,
    contacts_document,
    history_document,
    nodes_document,
    pending_document,
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
tcp_unauthenticated = true
host = "192.0.2.10"
[mqtt]
host = "broker.example.org"
node_id = "radio-1"
insecure = true
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
    "radio",
    "battery_v",
    "airtime_1h_s",
    "airtime_available_s",
    "airtime_budget_s_per_hour",
    "queue",
    "radio_name",
    "public_key",
    "model",
    "firmware",
    "frequency_mhz",
    "bandwidth_khz",
    "spreading_factor",
    "coding_rate",
    "tx_power_dbm",
    "contact_link",
    "version",
    "telemetry",
    "telemetry_reason",
    "telemetry_airtime_1h_s",
    "telemetry_mesh_airtime_1h_s",
    "radio_clock_offset_s",
    "radio_link_contested",
    "health",
}


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
        assert component["p"] in {"binary_sensor", "sensor", "event", "notify", "button", "text"}
        assert component["unique_id"] == f"meshcore_home_gateway_radio-1_{key}"
        # a draft's Send button `write_<alias>__send` keeps the entity ID of D-47; no other key
        # holds "__"
        object_id = key.replace("__", "_")
        assert object_id == key or (key.startswith("write_") and key.endswith("__send"))
        # Home Assistant turns the '-' into '_' in the entity ID (home-assistant/tests)
        assert (
            component["default_entity_id"]
            == f"{component['p']}.meshcore_home_gateway_radio-1_{object_id}"
        )


def test_entities_and_their_sources() -> None:
    cmps = discovery_payload(CONFIG, TOPICS)["cmps"]
    assert set(cmps) == {
        "radio",
        "battery",
        "airtime",
        "airtime_available",
        "queue",
        "message",
        "delivery",
        "command",
        "send_alerts",
        "send_alice",
        "write_alerts",
        "write_alerts__send",
        "write_alice",
        "write_alice__send",
        "noise_floor",
        "radio_airtime",
        "uptime",
        "packets_received",
        "packets_sent",
        "receive_errors",
        "contacts",
        "nodes",
        "remote_cli",
        "advert_position",
        "look_alike",
        "contact",
        "node",
        "show_hidden",
        "send_advert",
        "advert",
        "profile",
        "setting",
        "telemetry_status",
        "telemetry_airtime",
        "telemetry_mesh_airtime",
        "telemetry",
        "radio_clock_offset",
        "radio_link_contested",
        "degraded",
        "message_history",
        "pending_messages",
        "configuration",
    }
    assert cmps["radio"]["state_topic"] == "meshcore/radio-1/state"
    assert cmps["radio"]["payload_on"] == "connected"
    assert cmps["battery"]["unit_of_measurement"] == "V"  # "v" would be rejected
    assert cmps["battery"]["entity_category"] == "diagnostic"
    clock = cmps["radio_clock_offset"]  # SEC-002: the radio's clock cannot be set back
    assert (clock["entity_category"], clock["unit_of_measurement"]) == ("diagnostic", "s")
    # newer than the state documents of earlier versions: read without a warning there
    assert clock["value_template"] == "{{ value_json.get('radio_clock_offset_s') }}"
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
        TOPICS.history: history_document([]),
        TOPICS.pending: pending_document([]),
        TOPICS.config: config_document(0.0, target_settings(CONFIG, None), shown_settings(CONFIG)),
    }
    for topic, document in documents.items():
        assert _template_keys(cmps, topic) <= set(document), topic
    assert _template_keys(cmps, TOPICS.contacts) >= {"total", "remote_cli_total"}
    for counter in ("packets_received", "packets_sent", "receive_errors", "uptime"):
        assert cmps[counter]["enabled_by_default"] is False


HISTORY_STATE = (  # the newest message's time; none in an empty history: unknown
    "{{ value_json.messages[0].at if value_json.messages else None }}"
)


def test_the_message_history_is_a_timestamp_sensor_that_outlives_the_gateway() -> None:
    """The `history` document as a sensor: its state the newest message's time, the document its
    attributes. Its own availability, on its own topic, keeps it available while the gateway is
    offline, so that the messages stay readable."""
    history = discovery_payload(CONFIG, TOPICS)["cmps"]["message_history"]
    assert history == {
        "p": "sensor",
        "unique_id": "meshcore_home_gateway_radio-1_message_history",
        "default_entity_id": "sensor.meshcore_home_gateway_radio-1_message_history",
        "name": "Message history",
        "device_class": "timestamp",
        "state_topic": "meshcore/radio-1/history",
        "value_template": HISTORY_STATE,
        "json_attributes_topic": "meshcore/radio-1/history",
        "availability": [{"topic": "meshcore/radio-1/history", "value_template": "{{ 'online' }}"}],
    }
    off = replace(CONFIG, homeassistant=replace(CONFIG.homeassistant, controls=False))
    assert discovery_payload(off, TOPICS)["cmps"]["message_history"] == history  # no control
    newest = history_document([HistoryEntry(1_800_000_000.0, "in", "channel", "Hi")])
    assert newest["messages"][0]["at"] == "2027-01-15T08:00:00+00:00"  # what the template reads


def test_the_messages_on_their_way_are_a_count_that_outlives_the_gateway() -> None:
    """The `pending` document as a sensor (#112): its state the number of messages accepted and
    not final, the one being sent included; the document its attributes. Its own availability
    keeps it readable while the gateway is offline, as the history's."""
    sensor = discovery_payload(CONFIG, TOPICS)["cmps"]["pending_messages"]
    assert sensor == {
        "p": "sensor",
        "unique_id": "meshcore_home_gateway_radio-1_pending_messages",
        "default_entity_id": "sensor.meshcore_home_gateway_radio-1_pending_messages",
        "name": "Messages on their way",
        "icon": "mdi:tray-arrow-up",
        "state_topic": "meshcore/radio-1/pending",
        "value_template": "{{ value_json.total }}",
        "json_attributes_topic": "meshcore/radio-1/pending",
        "availability": [{"topic": "meshcore/radio-1/pending", "value_template": "{{ 'online' }}"}],
    }
    off = replace(CONFIG, homeassistant=replace(CONFIG.homeassistant, controls=False))
    assert discovery_payload(off, TOPICS)["cmps"]["pending_messages"] == sensor  # no control


def test_the_configuration_is_a_diagnostic_timestamp_of_the_start() -> None:
    """The `config` document as a sensor: its state the time the gateway started with the
    settings, the settings its attributes. The gateway's availability: while it is offline its
    settings may be others by the next start. No control: also with `controls = false`."""
    sensor = discovery_payload(CONFIG, TOPICS)["cmps"]["configuration"]
    assert sensor == {
        "p": "sensor",
        "unique_id": "meshcore_home_gateway_radio-1_configuration",
        "default_entity_id": "sensor.meshcore_home_gateway_radio-1_configuration",
        "name": "Configuration",
        "device_class": "timestamp",
        "entity_category": "diagnostic",
        "state_topic": "meshcore/radio-1/config",
        "value_template": "{{ value_json.started_at }}",
        "json_attributes_topic": "meshcore/radio-1/config",
    }
    off = replace(CONFIG, homeassistant=replace(CONFIG.homeassistant, controls=False))
    assert discovery_payload(off, TOPICS)["cmps"]["configuration"] == sensor


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
    assert "availability" not in button  # the device's


def test_with_controls_off_the_show_hidden_button_is_unavailable() -> None:
    """SEC-008: `[homeassistant] controls = false` keeps the button, always unavailable: Home
    Assistant leaves it out of service calls, and its topic still works."""
    off = replace(CONFIG, homeassistant=replace(CONFIG.homeassistant, controls=False))
    button = discovery_payload(off, TOPICS)["cmps"]["show_hidden"]
    assert button["availability_mode"] == "all"
    assert button["availability"] == [
        {"topic": TOPICS.availability},
        {"topic": f"{TOPICS.root}/homeassistant/off", "value_template": "{{ 'offline' }}"},
    ]  # a topic nobody publishes: Home Assistant keys availability by topic (HA-02)


# --- advert nearby (ADR 0033) -------------------------------------------------------------------


def _homeassistant(**settings: Any) -> Config:
    return replace(CONFIG, homeassistant=replace(CONFIG.homeassistant, **settings))


def test_the_advert_button_publishes_send_on_the_request_topic() -> None:
    """*Send advert nearby* on the gateway's device: `send` on `advert/send` at QoS 0, as a
    draft's Send button (a press while Home Assistant's link to the broker is down is not sent
    after the reconnect); never retained; available while the gateway is online and its radio
    connected. A control, not a configuration: no entity category."""
    button = discovery_payload(CONFIG, TOPICS)["cmps"]["send_advert"]
    assert button == {
        "p": "button",
        "unique_id": "meshcore_home_gateway_radio-1_send_advert",
        "default_entity_id": "button.meshcore_home_gateway_radio-1_send_advert",
        "name": "Send advert nearby",
        "command_topic": "meshcore/radio-1/advert/send",
        "payload_press": "send",
        "qos": 0,
        "icon": "mdi:broadcast",
        "availability": [
            {"topic": TOPICS.availability},
            {"topic": TOPICS.state, "value_template": RADIO_CONNECTED},
        ],
        "availability_mode": "all",
    }
    assert TOPICS.advert_request == "meshcore/radio-1/advert/send"


@pytest.mark.parametrize(
    "settings",
    [
        {"radio_controls": False},
        {"controls": False},
        {"radio_controls": False, "controls": False},
    ],
)
def test_without_radio_controls_or_controls_the_advert_button_is_unavailable(
    settings: dict[str, bool],
) -> None:
    """ADR 0033: `radio_controls = false`, and `controls = false` as for every control (SEC-008),
    keep the button announced and always unavailable: a component of the gateway's device that
    goes can take the whole device away. One entry that is always offline, also with both."""
    on = discovery_payload(CONFIG, TOPICS)["cmps"]["send_advert"]
    off = discovery_payload(_homeassistant(**settings), TOPICS)["cmps"]["send_advert"]
    assert off == on | {"availability": [*on["availability"], never_available(TOPICS)]}


def test_radio_controls_off_leaves_every_other_control_as_it_is() -> None:
    """`radio_controls = false` takes only the button that makes the radio transmit away from
    Home Assistant: the contact controls stay as they are, which only `controls` turns off."""
    config = _homeassistant(radio_controls=False)
    assert controls_off(config, TOPICS) == []
    on = discovery_payload(CONFIG, TOPICS)["cmps"]
    off = discovery_payload(config, TOPICS)["cmps"]
    assert {k: v for k, v in off.items() if k != "send_advert"} == {
        k: v for k, v in on.items() if k != "send_advert"
    }


def test_the_advert_event_is_always_announced() -> None:
    """*Advert nearby* reports every request's outcome, from MQTT and the command line too, so
    it is there whatever the two settings say; never retained, as every event."""
    for config in (CONFIG, _homeassistant(radio_controls=False, controls=False)):
        event = discovery_payload(config, TOPICS)["cmps"]["advert"]
        assert event == {
            "p": "event",
            "unique_id": "meshcore_home_gateway_radio-1_advert",
            "default_entity_id": "event.meshcore_home_gateway_radio-1_advert",
            "name": "Advert nearby",
            "state_topic": "meshcore/radio-1/advert",
            "event_types": ["sent", "refused", "failed"],
        }
    assert list(SELF_ADVERT_EVENTS) == ["sent", "refused", "failed"]


def test_the_advert_entities_have_german_names() -> None:
    """The catalog's words (docs/translations.md): Advert stays Advert."""
    german = discovery_payload(_maximal("de"), TOPICS)["cmps"]
    assert (german["send_advert"]["name"], german["advert"]["name"]) == (
        "Advert in der Nähe senden",
        "Advert in der Nähe",
    )


def test_a_hidden_target_has_no_notify_entity() -> None:
    """SEC-008: a target that only automations should use stays reachable on MQTT."""
    hidden = replace(
        CONFIG, homeassistant=replace(CONFIG.homeassistant, hidden_targets=frozenset({"alerts"}))
    )
    cmps = discovery_payload(hidden, TOPICS)["cmps"]
    assert "send_alerts" not in cmps
    assert "send_alice" in cmps
    assert "send_alerts" in discovery_payload(CONFIG, TOPICS)["cmps"]


def test_command_entity_only_with_command_words() -> None:
    plain = parse_config(
        '[radio]\ntransport = "tcp"\ntcp_unauthenticated = true\n'
        'host = "h"\n[mqtt]\nhost = "h"\nnode_id = "n"\n'
    )
    assert "command" not in discovery_payload(plain, Topics("meshcore", "n"))["cmps"]


def test_a_taken_tcp_link_is_a_problem_only_over_tcp() -> None:
    """SEC-003: another client took the radio's TCP link; over USB there is no other client."""
    sensor = discovery_payload(CONFIG, TOPICS)["cmps"]["radio_link_contested"]
    assert (sensor["p"], sensor["device_class"]) == ("binary_sensor", "problem")
    assert (sensor["entity_category"], sensor["state_topic"]) == ("diagnostic", TOPICS.state)
    # unknown (null, or a state document of an earlier version): neither on nor off
    assert (
        "value_json.get('radio_link_contested') is not none else None" in (sensor["value_template"])
    )
    usb = parse_config(
        '[radio]\ntransport = "serial"\ndevice = "/dev/ttyACM0"\n'
        '[mqtt]\nhost = "h"\nnode_id = "n"\n'
    )
    assert "radio_link_contested" not in discovery_payload(usb, Topics("meshcore", "n"))["cmps"]


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
    # the radio's name takes room from channel texts: only the channel draft's `max` follows it
    unknown_max = unknown["cmps"]["write_alerts"].pop("max")
    known_max = known["cmps"]["write_alerts"].pop("max")
    assert (unknown_max, known_max) == (158, 160 - len("Gateway") - 2)
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


def test_a_degraded_gateway_is_a_problem() -> None:
    """ARC-012: on while a part is not as it should be; the health section as attributes. A
    state document of an earlier version has none: unknown, without a warning."""
    sensor = discovery_payload(CONFIG, TOPICS)["cmps"]["degraded"]
    assert (sensor["p"], sensor["name"], sensor["device_class"], sensor["entity_category"]) == (
        "binary_sensor",
        "Gateway degraded",
        "problem",
        "diagnostic",
    )
    assert sensor["state_topic"] == sensor["json_attributes_topic"] == TOPICS.state
    assert "value_json.get('health') is not none" in sensor["value_template"]
    for key in (
        "history",
        "state_file",
        "exclusions",
        "errors_1h",
        "loop_lag_max_s",
        "events_dropped_1h",
        "disk_free_mb",
    ):
        assert f"'{key}': h.get('{key}')" in sensor["json_attributes_template"]


def test_the_contacts_sensor_says_why_requests_are_refused() -> None:
    """The guide and the changelog of 2.0.0 name `manage_reason` as an attribute of *Contacts*
    (#99): why add and remove requests are refused now, `null` while they are taken. An index of
    an earlier version lacks it: `.get` logs nothing in Home Assistant."""
    sensor = discovery_payload(CONFIG, TOPICS)["cmps"]["contacts"]
    assert sensor["state_topic"] == sensor["json_attributes_topic"] == TOPICS.contacts
    assert sensor["json_attributes_template"] == (
        "{{ {'max': value_json.max, 'manage_reason': value_json.get('manage_reason')} | tojson }}"
    )


def test_a_look_alike_node_is_a_problem() -> None:
    sensor = discovery_payload(CONFIG, TOPICS)["cmps"]["look_alike"]
    assert (sensor["p"], sensor["device_class"]) == ("binary_sensor", "problem")
    assert sensor["entity_category"] == "diagnostic"
    assert sensor["state_topic"] == sensor["json_attributes_topic"] == TOPICS.nodes
    assert "look_alike_total is defined" in sensor["value_template"]  # earlier documents
    assert "value_json.get('look_alike', [])" in sensor["json_attributes_template"]


def test_the_companion_position_sensor_follows_the_households_companions() -> None:
    """On while a companion not excluded from telemetry sends its position in its newest advert,
    as the gateway checked it (ADR 0025). An index of 1.1.0a2 lacks the keys: `is number`
    and `.get` log nothing in Home Assistant (home-assistant/tests/test_adverts.py)."""
    sensor = discovery_payload(CONFIG, TOPICS)["cmps"]["advert_position"]
    assert (sensor["p"], sensor["name"], sensor["device_class"], sensor["entity_category"]) == (
        "binary_sensor",
        "Companion position in adverts",
        "problem",
        "diagnostic",
    )
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
        '[radio]\ntransport = "tcp"\ntcp_unauthenticated = true\n'
        'host = "h"\n[mqtt]\nhost = "h"\nnode_id = "radio-1"\n'
        "[telemetry]\nenabled = false\n"
    )
    for config in (CONFIG, off):
        cmps = discovery_payload(config, TOPICS)["cmps"]
        status = cmps["telemetry_status"]
        assert (status["p"], status["name"], status["device_class"]) == (
            "sensor",
            "Telemetry status",
            "enum",
        )
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
        lanes = (
            ("telemetry_airtime", "Telemetry airtime last hour"),
            ("telemetry_mesh_airtime", "Telemetry mesh airtime last hour"),
        )
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
            "event",
            "Telemetry",
            "meshcore/radio-1/telemetry",
        )
        assert event["event_types"] == list(telemetry.EVENTS) == ["answered", "unanswered"]
        assert cmps["contact"]["event_types"] == [
            "added",
            "removed",
            "hidden",
            "shown",
            "refused",
            "failed",
            "excluded",
            "included",
            "position_in_advert",
        ]


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
        tracked = Tracked(
            Node(KEY, "Alice", node_type), fix=Fix(position, 0.0, "telemetry"), readings=readings
        )
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
        '[radio]\ntransport = "tcp"\ntcp_unauthenticated = true\n'
        'host = "h"\n[mqtt]\nhost = "h"\nnode_id = "n"\n'
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


# --- the chats' drafts and their Send buttons (drafts.py, docs/mqtt-api.md) ---------------------

DRAFT_TEXT = "{{ value_json.text if value_json is defined and value_json.text is string else '' }}"


def _targets(text: str, **homeassistant: Any) -> Config:
    config = parse_config(
        '[radio]\ntransport = "tcp"\ntcp_unauthenticated = true\nhost = "192.0.2.10"\n'
        '[mqtt]\nhost = "broker.example.org"\nnode_id = "radio1"\ninsecure = true\n' + text
    )
    return replace(config, homeassistant=replace(config.homeassistant, **homeassistant))


def test_each_visible_target_has_a_draft_and_a_send_button_on_one_request_topic() -> None:
    """Both publish on the chat's request topic at QoS 0, over the device's shared QoS 1: the
    broker keeps the order of the text and the press, and a press made while Home Assistant's
    link to the broker is down is not sent after the reconnect (paho keeps QoS 1 for it)."""
    cmps = discovery_payload(CONFIG, TOPICS)["cmps"]
    request = "meshcore/radio-1/homeassistant/draft/target/alerts/request"
    assert cmps["write_alerts"] == {
        "p": "text",
        "unique_id": "meshcore_home_gateway_radio-1_write_alerts",
        "default_entity_id": "text.meshcore_home_gateway_radio-1_write_alerts",
        "name": "Write to alerts",
        "command_topic": request,
        "command_template": '{{ {"text": value} | tojson }}',
        "state_topic": "meshcore/radio-1/homeassistant/draft/target/alerts",
        "value_template": DRAFT_TEXT,
        "mode": "text",
        "max": 158,
        "icon": "mdi:message-draw",
        "qos": 0,
    }
    assert cmps["write_alerts__send"] == {
        "p": "button",
        "unique_id": "meshcore_home_gateway_radio-1_write_alerts__send",
        "default_entity_id": "button.meshcore_home_gateway_radio-1_write_alerts_send",
        "name": "Send what is written to alerts",
        "command_topic": request,
        "payload_press": '{"send": true}',
        "icon": "mdi:send",
        "qos": 0,
    }
    assert discovery_payload(CONFIG, TOPICS)["qos"] == 1  # the shared one, for the others
    alice = cmps["write_alice"]
    assert alice["command_topic"] == cmps["write_alice__send"]["command_topic"]
    assert alice["command_topic"] == "meshcore/radio-1/homeassistant/draft/target/alice/request"


def test_targets_x_and_x_send_keep_each_its_draft_and_its_send_button() -> None:
    """The button of `family` and the text of `family_send` must not share a key: the one
    written second would replace the other in the document, and one chat would lose an entity.
    No alias holds `__`, so the button's key `write_<alias>__send` is no text's key. The default
    entity IDs stay `text.…_write_<alias>` and `button.…_write_<alias>_send`: they differ by
    platform."""
    uid, topics = "meshcore_home_gateway_radio1", Topics("meshcore", "radio1")
    for first, second in (("family", "family_send"), ("family_send", "family")):
        config = _targets(f"[targets.{first}]\nchannel = 1\n[targets.{second}]\nchannel = 2\n")
        drafts = {
            key: component
            for key, component in discovery_payload(config, topics)["cmps"].items()
            if key.startswith("write_")
        }
        assert len(drafts) == 4, (first, second)
        assert len({c["unique_id"] for c in drafts.values()}) == 4
        for alias in ("family", "family_send"):
            request = topics.target_draft_request(alias)
            chat = {c["p"]: c for c in drafts.values() if c["command_topic"] == request}
            assert chat["text"]["default_entity_id"] == f"text.{uid}_write_{alias}"
            assert chat["button"]["default_entity_id"] == f"button.{uid}_write_{alias}_send"
            assert drafts[f"write_{alias}__send"] is chat["button"]


def test_the_drafts_max_is_the_room_of_the_targets_texts() -> None:
    """In bytes: the radio's 160, less a channel's radio name and ": " (the shortest name while
    it is not known), a direct message's nonce, and the target's prefix and suffix."""
    affixed = _targets(
        '[targets.family]\nchannel = 1\nprefix = "Home: "\n'
        '[targets.alice]\ncontact = "' + KEY + '"\nsuffix = " (home)"\n'
    )
    nonce = _targets(
        '[targets.alice]\ncontact = "' + KEY + '"\n[messages]\nconfirm_nonce = true\n'
        "ascii_only = true\n"
        '[targets.carol]\ncontact = "' + "c4" * 32 + '"\nprefix = "° "\n'
    )
    cases = [
        (CONFIG, None, "alerts", 158),  # a channel, the radio's name unknown
        (CONFIG, RADIO, "alerts", 160 - len("Gateway") - 2),
        (CONFIG, RADIO, "alice", 160),  # a direct message
        (affixed, RADIO, "family", 160 - 9 - 6),
        (affixed, None, "alice", 160 - 7),
        (nonce, RADIO, "alice", 150),
        (nonce, RADIO, "carol", 150 - len("deg ")),  # transliterated, as it is sent
    ]
    for config, radio, alias, room in cases:
        cmps = discovery_payload(config, TOPICS, radio)["cmps"]
        assert cmps[f"write_{alias}"]["max"] == room, (alias, room)


def test_a_hidden_target_has_no_draft() -> None:
    hidden = replace(
        CONFIG,
        homeassistant=replace(CONFIG.homeassistant, hidden_targets=frozenset({"alerts", "alice"})),
    )
    cmps = discovery_payload(hidden, TOPICS)["cmps"]
    assert not [key for key in cmps if key.startswith("write_")]


def test_controls_off_leaves_the_drafts_available() -> None:
    """Sending is no control: every user can send with the notify entities anyway."""
    off = replace(CONFIG, homeassistant=replace(CONFIG.homeassistant, controls=False))
    for key in ("write_alerts", "write_alerts__send", "write_alice", "write_alice__send"):
        assert (
            discovery_payload(off, TOPICS)["cmps"][key]
            == discovery_payload(CONFIG, TOPICS)["cmps"][key]
        )
        assert "availability" not in discovery_payload(off, TOPICS)["cmps"][key]
