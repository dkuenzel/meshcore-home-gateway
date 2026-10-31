"""Home Assistant device-based MQTT discovery (ADR 0010, docs/home-assistant.md).

One retained-free document on `<prefix>/device/<device id>/config` describes the device and all
its entities. Rules checked against Home Assistant 2026.10.2 (docs/home-assistant.md, sources):
`origin.name` and `unique_id` per component are mandatory, and a single null device field
rejects the whole document, so unknown radio fields are left out (Home Assistant keeps the stored
ones); events must not be retained; a component disappears only when it is sent once as
`{"p": <platform>}`.

The names are in the language of `[homeassistant] language` (ADR 0019); entity IDs, unique IDs and
every other value stay the same in every language.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from meshcore_home_gateway import __version__, i18n
from meshcore_home_gateway.config import DEFAULT_DEVICE_NAME, Config
from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.core.model import DeliveryState
from meshcore_home_gateway.mqtt.topics import Topics

if TYPE_CHECKING:
    from meshcore_home_gateway.radio.session import RadioInfo

DOCUMENTATION_URL = (
    "https://github.com/dkuenzel/meshcore-home-gateway/blob/main/docs/home-assistant.md"
)
# the static facts of the state document, as attributes of the radio link entity; the values that
# change by themselves have their own sensors (Home Assistant's guidance for attributes). Keys
# that a document retained by an earlier version lacks are read with .get or `is defined`, which
# log no warning in Home Assistant (docs/platform-notes.md).
RADIO_ATTRIBUTES = (
    "{{ {'radio_name': value_json.radio_name, 'public_key': value_json.public_key, "
    "'model': value_json.model, 'firmware': value_json.firmware, "
    "'frequency_mhz': value_json.frequency_mhz, 'bandwidth_khz': value_json.bandwidth_khz, "
    "'spreading_factor': value_json.spreading_factor, 'coding_rate': value_json.coding_rate, "
    "'tx_power_dbm': value_json.tx_power_dbm, 'bridge_version': value_json.version, "
    "'contact_link': value_json.get('contact_link')} | tojson }}"
)
PROFILE_STATES = ["ok", "written", "drift", "differs", "problem"]  # views.settings_document
TELEMETRY_STATUS = (  # state.telemetry and state.telemetry_reason are newer than 1.0.0
    "{{ value_json.telemetry if value_json is defined "
    "and value_json.telemetry is string else None }}"
)
TELEMETRY_REASON = "{{ {'reason': value_json.get('telemetry_reason')} | tojson }}"
STATS_ATTRIBUTES = (
    "{{ {'window_s': value_json.tx_airtime_window_s, 'airtime_factor': value_json.airtime_factor, "
    "'duty_cycle_percent': value_json.duty_cycle_percent} | tojson }}"
)


def device_id(config: Config) -> str:
    """The node ID unchanged: node IDs that differ only in '-' and '_' are two devices (Home
    Assistant turns '-' into '_' only in the entity IDs)."""
    return f"meshcore_home_gateway_{config.mqtt.node_id}"


def discovery_topic(config: Config) -> str:
    return f"{config.homeassistant.prefix}/device/{device_id(config)}/config"


def discovery_payload(
    config: Config,
    topics: Topics,
    radio: RadioInfo | None = None,
    removed: Mapping[str, str] | None = None,
    *,
    bind: bool = False,
) -> dict[str, Any]:
    """The document; `radio` fills the device page, `removed` (key -> platform) are removed.

    Home Assistant removes a component only if it has discovered it since its own start. With
    `bind`, the removed components are sent as placeholders under their unique_id instead, which
    binds an entity left from an earlier run (or creates one for a moment); the document without
    `bind`, sent right after, then removes them.
    """
    uid = device_id(config)
    ha_name = i18n.translator(config.homeassistant.language).ha_name

    def component(key: str, platform: str, name: str, **options: Any) -> dict[str, Any]:
        return {
            "p": platform,
            "unique_id": f"{uid}_{key}",
            "default_entity_id": f"{platform}.{uid}_{key}",
            "name": name,
            **options,
        }

    diagnostic = {"entity_category": "diagnostic", "state_topic": topics.state}
    lane = {  # the telemetry lanes: each would write a row a minute, so off until wanted
        "device_class": "duration", "unit_of_measurement": "s", "state_class": "measurement",
        "suggested_display_precision": 1, "enabled_by_default": False, **diagnostic,
    }  # fmt: skip
    components: dict[str, dict[str, Any]] = {
        "radio": component(
            # Translators: binary sensor (connectivity) of the gateway: its USB or TCP link to
            # its own radio, not a link over the air. Home Assistant shows the device name first.
            "radio", "binary_sensor", ha_name("Radio link"), device_class="connectivity",
            value_template="{{ value_json.radio }}", payload_on="connected",
            payload_off="disconnected", json_attributes_topic=topics.state,
            json_attributes_template=RADIO_ATTRIBUTES, **diagnostic,
        ),
        "battery": component(
            # Translators: sensor (voltage) of the gateway: the battery of its own radio
            "battery", "sensor", ha_name("Battery"), device_class="voltage",
            unit_of_measurement="V",
            state_class="measurement", suggested_display_precision=2,
            value_template="{{ value_json.battery_v }}", **diagnostic,
        ),
        "airtime": component(
            # Translators: sensor (duration) of the gateway: how long it transmitted in the
            # last hour (airtime: the time on air, as in Home Assistant's own words)
            "airtime", "sensor", ha_name("Airtime last hour"), device_class="duration",
            unit_of_measurement="s", state_class="measurement", suggested_display_precision=1,
            value_template="{{ value_json.airtime_1h_s }}", **diagnostic,
        ),
        "airtime_available": component(
            # Translators: sensor (duration) of the gateway: the transmit time left in its
            # hourly budget
            "airtime_available", "sensor", ha_name("Airtime available"), device_class="duration",
            unit_of_measurement="s", state_class="measurement", suggested_display_precision=1,
            value_template="{{ value_json.airtime_available_s }}",
            json_attributes_topic=topics.state,
            json_attributes_template=(
                "{{ {'budget_s_per_hour': value_json.airtime_budget_s_per_hour} | tojson }}"
            ),
            **diagnostic,
        ),
        "queue": component(
            # Translators: sensor of the gateway: the messages waiting to be sent
            "queue", "sensor", ha_name("Queue"), state_class="measurement", icon="mdi:tray-full",
            value_template="{{ value_json.queue }}", **diagnostic,
        ),
        "message": component(
            # Translators: event entity of the gateway: a message received from a contact or on
            # a channel
            "message", "event", ha_name("Message"), state_topic=topics.received,
            event_types=["contact", "channel"],
        ),
        "delivery": component(
            # Translators: event entity of the gateway: how far a message it sent has come
            # (queued, sent, confirmed, failed)
            "delivery", "event", ha_name("Delivery"), state_topic=topics.delivery,
            event_types=[state.value for state in DeliveryState],
        ),
        # also with telemetry off: a disabled component that goes takes the whole device away
        "telemetry_status": component(
            # Translators: sensor (enum) of the gateway: whether it asks the household's
            # companions for telemetry; keep it apart from "Telemetry state" of a contact
            "telemetry_status", "sensor", ha_name("Telemetry status"), device_class="enum",
            options=list(telemetry.STATUSES), icon="mdi:satellite-uplink",
            value_template=TELEMETRY_STATUS, json_attributes_topic=topics.state,
            json_attributes_template=TELEMETRY_REASON, **diagnostic,
        ),
        "telemetry_airtime": component(
            # Translators: sensor (duration) of the gateway: its own transmit time for
            # telemetry requests in the last hour
            "telemetry_airtime", "sensor", ha_name("Telemetry airtime last hour"),
            value_template="{{ value_json.get('telemetry_airtime_1h_s') }}", **lane,
        ),
        "telemetry_mesh_airtime": component(
            # Translators: sensor (duration) of the gateway: the airtime its telemetry requests
            # caused in the whole mesh in the last hour, as estimated
            "telemetry_mesh_airtime", "sensor", ha_name("Telemetry mesh airtime last hour"),
            value_template="{{ value_json.get('telemetry_mesh_airtime_1h_s') }}", **lane,
        ),
        "telemetry": component(
            # Translators: event entity of the gateway: a companion answered a telemetry
            # request, or did not
            "telemetry", "event", ha_name("Telemetry"), state_topic=topics.telemetry_event,
            event_types=list(telemetry.EVENTS),
        ),
    }  # fmt: skip
    if config.commands.words:
        components["command"] = component(
            # Translators: event entity of the gateway: a command word from an allowed sender
            "command", "event", ha_name("Command"), state_topic=topics.command,
            event_types=sorted(config.commands.words),
        )  # fmt: skip
    components.update(_mesh_components(component, topics, ha_name))
    if config.homeassistant.contact_devices:
        components["show_hidden"] = component(
            # Translators: button of the gateway: brings back the contacts' devices the user
            # deleted in Home Assistant
            "show_hidden", "button", ha_name("Show hidden contacts"),
            command_topic=topics.show_hidden,
            payload_press="show", entity_category="config", icon="mdi:eye",
        )  # fmt: skip
    if config.diagnostics.publish_rx_log:  # one recorder row per packet: off until enabled
        rx = {"entity_category": "diagnostic", "state_topic": topics.rx}
        components["rx_rssi"] = component(
            "rx_rssi",
            "sensor",
            # Translators: sensor (signal strength) of the gateway: the received signal strength
            # (RSSI) of the last packet its radio heard
            ha_name("Last packet RSSI"),
            device_class="signal_strength",
            unit_of_measurement="dBm",
            state_class="measurement",
            value_template="{{ value_json.rssi_dbm }}",
            enabled_by_default=False,
            **rx,
        )
        components["rx_snr"] = component(
            "rx_snr",
            "sensor",
            # Translators: sensor (signal strength) of the gateway: the signal-to-noise ratio
            # (SNR) of the last packet its radio heard
            ha_name("Last packet SNR"),
            device_class="signal_strength",
            unit_of_measurement="dB",
            state_class="measurement",
            suggested_display_precision=2,
            value_template="{{ value_json.snr_db }}",
            enabled_by_default=False,
            **rx,
        )
    for target in config.targets:
        key = f"send_{target.alias}"
        # Translators: notify entity of the gateway: sends a message to a target of the
        # configuration; {alias} is the target's name there, as the user wrote it
        name = ha_name("Send to {alias}", alias=target.alias)
        components[key] = component(key, "notify", name, command_topic=topics.send(target.alias))
    with_removed(components, uid, topics, removed, ha_name, bind=bind)
    device: dict[str, Any] = {
        "ids": [uid],
        "name": device_name(config, ha_name),
        "mf": "MeshCore",
    }
    if radio is not None:
        identity = {"mdl": radio.device.model, "sw": radio.device.version, "sn": radio.public_key}
        device.update((field, value) for field, value in identity.items() if value)
    return {
        "dev": device,
        "o": origin(),
        "availability": [{"topic": topics.availability}],
        "qos": 1,
        "cmps": components,
    }


def origin() -> dict[str, str]:
    return {"name": "meshcore-home-gateway", "sw": __version__, "url": DOCUMENTATION_URL}


def device_name(config: Config, ha_name: Callable[..., str]) -> str:
    """The gateway's device name: the default in the language of the names, also when the file
    names it (the installer writes it); any other name as set (docs/configuration.md)."""
    name = config.homeassistant.device_name
    # Translators: the default name of the gateway's device in Home Assistant; MeshCore is the
    # name of the mesh firmware
    return ha_name("MeshCore radio") if name == DEFAULT_DEVICE_NAME else name


def with_removed(
    components: dict[str, dict[str, Any]], uid: str, topics: Topics,
    removed: Mapping[str, str] | None, ha_name: Callable[..., str], *, bind: bool,
) -> None:  # fmt: skip
    """Add the removed components (key -> platform) to a document: as placeholders with `bind`,
    else as `{"p": platform}`, which removes them."""
    for key, platform in (removed or {}).items():
        if key not in components:
            stand_in = placeholder(f"{uid}_{key}", platform, topics, ha_name) if bind else None
            components[key] = stand_in or {"p": platform}


def _mesh_components(
    component: Callable[..., dict[str, Any]], topics: Topics, ha_name: Callable[..., str]
) -> dict[str, dict[str, Any]]:
    """What the observer publishes (ADR 0015): the radio's measurements, the contact table, the
    nodes heard, the managed profile (ADR 0020), and their events."""
    stats = {"entity_category": "diagnostic", "state_topic": topics.stats}
    counter = {"state_class": "total_increasing", "enabled_by_default": False, **stats}
    contacts = {"state_topic": topics.contacts, "json_attributes_topic": topics.contacts}
    return {
        "noise_floor": component(
            # Translators: sensor (signal strength) of the gateway: the noise its radio hears
            # when nobody transmits
            "noise_floor", "sensor", ha_name("Noise floor"), device_class="signal_strength",
            unit_of_measurement="dBm", state_class="measurement",
            value_template="{{ value_json.noise_floor_dbm }}", **stats,
        ),
        "radio_airtime": component(
            # Translators: sensor (duration) of the gateway: what its radio itself counted as
            # sent in the last hour, adverts and acknowledgements included
            "radio_airtime", "sensor", ha_name("Radio transmit time last hour"),
            device_class="duration",
            unit_of_measurement="s", state_class="measurement", suggested_display_precision=1,
            value_template="{{ value_json.tx_airtime_1h_s }}", json_attributes_topic=topics.stats,
            json_attributes_template=STATS_ATTRIBUTES, **stats,
        ),
        "uptime": component(
            # Translators: sensor (duration) of the gateway: the time since its radio started
            "uptime", "sensor", ha_name("Radio uptime"), device_class="duration",
            unit_of_measurement="s",
            enabled_by_default=False, value_template="{{ value_json.uptime_s }}", **stats,
        ),
        "packets_received": component(
            # Translators: sensor of the gateway: a counter of the packets its radio received
            "packets_received", "sensor", ha_name("Packets received"),
            icon="mdi:download-network",
            value_template="{{ value_json.packets_received }}", **counter,
        ),
        "packets_sent": component(
            # Translators: sensor of the gateway: a counter of the packets its radio sent
            "packets_sent", "sensor", ha_name("Packets sent"), icon="mdi:upload-network",
            value_template="{{ value_json.packets_sent }}", **counter,
        ),
        "receive_errors": component(
            # Translators: sensor of the gateway: a counter of the packets its radio could not
            # read
            "receive_errors", "sensor", ha_name("Receive errors"),
            icon="mdi:alert-circle-outline",
            value_template="{{ value_json.receive_errors }}", **counter,
        ),
        "contacts": component(
            # Translators: sensor of the gateway: the number of contacts on its radio
            "contacts", "sensor", ha_name("Contacts"), icon="mdi:account-multiple",
            state_class="measurement", value_template="{{ value_json.total }}",
            json_attributes_template="{{ {'max': value_json.max} | tojson }}", **contacts,
        ),
        "nodes": component(
            # Translators: sensor of the gateway: the number of nodes its radio heard lately
            # (node: any MeshCore station)
            "nodes", "sensor", ha_name("Nodes heard"), icon="mdi:radio-tower",
            state_class="measurement",
            state_topic=topics.nodes, value_template="{{ value_json.total }}",
        ),
        "remote_cli": component(
            # Translators: binary sensor (problem) of the gateway: on while a contact may use its
            # radio's command line over the air
            "remote_cli", "binary_sensor", ha_name("Contact with remote access"),
            device_class="problem",
            entity_category="diagnostic",
            value_template="{{ 'ON' if value_json.remote_cli_total else 'OFF' }}",
            json_attributes_template=(
                "{{ {'contacts': value_json.remote_cli, 'total': value_json.remote_cli_total}"
                " | tojson }}"
            ),
            **contacts,
        ),
        # the household's companions whose newest advert carries their position (ADR 0018);
        # an index of 1.1.0a2 lacks the keys: `is number` and .get log nothing
        "advert_position": component(
            # Translators: binary sensor (problem) of the gateway: on while a companion of the
            # household sends its position in its adverts, which every node hears (companion: a
            # person's radio; advert: the packet a node announces itself with)
            "advert_position", "binary_sensor", ha_name("Companion position in adverts"),
            device_class="problem", entity_category="diagnostic",
            value_template=(
                "{{ ('ON' if value_json.advert_carries_position_total > 0 else 'OFF') if "
                "value_json is defined and value_json.advert_carries_position_total is number "
                "else None }}"
            ),
            json_attributes_template=(
                "{{ {'contacts': value_json.get('advert_carries_position', []), "
                "'total': value_json.get('advert_carries_position_total', 0)} | tojson }}"
            ),
            **contacts,
        ),
        "look_alike": component(
            # Translators: binary sensor (problem) of the gateway: on while it hears a node
            # whose key begins like a protected contact's, which may pose as that contact
            "look_alike", "binary_sensor", ha_name("Look-alike node heard"),
            device_class="problem",
            entity_category="diagnostic", state_topic=topics.nodes,
            value_template=(
                "{{ 'ON' if value_json.look_alike_total is defined "
                "and value_json.look_alike_total > 0 else 'OFF' }}"
            ),
            json_attributes_topic=topics.nodes,
            json_attributes_template=(
                "{{ {'nodes': value_json.get('look_alike', []), "
                "'total': value_json.get('look_alike_total', 0)} | tojson }}"
            ),
        ),
        "contact": component(
            # Translators: event entity of the gateway: a contact was added, removed, hidden,
            # shown, refused, excluded or included
            "contact", "event", ha_name("Contact"), state_topic=topics.contact_event,
            event_types=[
                "added", "removed", "hidden", "shown", "refused", "failed", "excluded", "included",
                "position_in_advert",
            ],
        ),
        "node": component(
            # Translators: event entity of the gateway: its radio heard a node for the first
            # time
            "node", "event", ha_name("New node"), state_topic=topics.node_event,
            event_types=["new"],
        ),
        "profile": component(  # the whole settings document as attributes
            # Translators: sensor (enum) of the gateway: whether its radio's settings match the
            # profile the gateway keeps on it
            "profile", "sensor", ha_name("Radio profile"), device_class="enum",
            options=PROFILE_STATES,
            icon="mdi:shield-check-outline", entity_category="diagnostic",
            state_topic=topics.settings, value_template="{{ value_json.status }}",
            json_attributes_topic=topics.settings,
        ),
        "setting": component(
            # Translators: event entity of the gateway: it wrote a setting of the profile to its
            # radio, or could not
            "setting", "event", ha_name("Radio setting"), state_topic=topics.setting_event,
            event_types=["written", "failed", "not_taken", "not_converging", "drift"],
        ),
    }  # fmt: skip


# the options each platform requires; nothing is ever published to or read from these topics
_REQUIRED: dict[str, dict[str, Any]] = {
    "binary_sensor": {"state_topic": "{root}/removed"},
    "sensor": {"state_topic": "{root}/removed"},
    "event": {"state_topic": "{root}/removed", "event_types": ["removed"]},
    "notify": {"command_topic": "{root}/removed"},
    "button": {"command_topic": "{root}/removed"},
    "device_tracker": {"state_topic": "{root}/removed"},
    "select": {"command_topic": "{root}/removed", "options": ["removed"]},
    "text": {"command_topic": "{root}/removed"},
    "switch": {"command_topic": "{root}/removed"},
}


def placeholder(
    unique_id: str, platform: str, topics: Topics, ha_name: Callable[..., str]
) -> dict[str, Any] | None:
    """A component under an earlier unique_id; None for a platform of another version.

    Enabled: when Home Assistant removes a disabled entity, it clears the whole device's
    discovery topic itself (docs/platform-notes.md), which would take every entity of the device
    away.
    """
    required = _REQUIRED.get(platform)
    if required is None:
        return None
    options = {
        name: value.format(root=topics.root) if isinstance(value, str) else value
        for name, value in required.items()
    }
    return {
        "p": platform, "unique_id": unique_id, "default_entity_id": f"{platform}.{unique_id}",
        # Translators: the name of an entity that exists only for a moment, while the gateway
        # removes it from Home Assistant
        "name": ha_name("Removed"), **options,
    }  # fmt: skip
