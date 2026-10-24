"""Home Assistant device-based MQTT discovery (ADR 0010, docs/home-assistant.md).

One retained-free document on `<prefix>/device/<device id>/config` describes the device and all
its entities. Rules checked against Home Assistant 2026.10.2 (docs/home-assistant.md, sources):
`origin.name` and `unique_id` per component are mandatory, and a single null device field
rejects the whole document, so unknown radio fields are left out (Home Assistant keeps the stored
ones); events must not be retained; a component disappears only when it is sent once as
`{"p": <platform>}`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from meshcore_home_gateway import __version__
from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core.model import DeliveryState
from meshcore_home_gateway.mqtt.topics import Topics

if TYPE_CHECKING:
    from meshcore_home_gateway.radio.session import RadioInfo

DOCUMENTATION_URL = (
    "https://github.com/dkuenzel/meshcore-home-gateway/blob/main/docs/home-assistant.md"
)
# the static facts of the state document, as attributes of the radio link entity; the values that
# change by themselves have their own sensors (Home Assistant's guidance for attributes)
RADIO_ATTRIBUTES = (
    "{{ {'radio_name': value_json.radio_name, 'public_key': value_json.public_key, "
    "'model': value_json.model, 'firmware': value_json.firmware, "
    "'frequency_mhz': value_json.frequency_mhz, 'bandwidth_khz': value_json.bandwidth_khz, "
    "'spreading_factor': value_json.spreading_factor, 'coding_rate': value_json.coding_rate, "
    "'tx_power_dbm': value_json.tx_power_dbm, 'bridge_version': value_json.version} | tojson }}"
)
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

    def component(key: str, platform: str, name: str, **options: Any) -> dict[str, Any]:
        return {
            "p": platform,
            "unique_id": f"{uid}_{key}",
            "default_entity_id": f"{platform}.{uid}_{key}",
            "name": name,
            **options,
        }

    diagnostic = {"entity_category": "diagnostic", "state_topic": topics.state}
    components: dict[str, dict[str, Any]] = {
        "radio": component(
            "radio", "binary_sensor", "Radio link", device_class="connectivity",
            value_template="{{ value_json.radio }}", payload_on="connected",
            payload_off="disconnected", json_attributes_topic=topics.state,
            json_attributes_template=RADIO_ATTRIBUTES, **diagnostic,
        ),
        "battery": component(
            "battery", "sensor", "Battery", device_class="voltage", unit_of_measurement="V",
            state_class="measurement", suggested_display_precision=2,
            value_template="{{ value_json.battery_v }}", **diagnostic,
        ),
        "airtime": component(
            "airtime", "sensor", "Airtime last hour", device_class="duration",
            unit_of_measurement="s", state_class="measurement", suggested_display_precision=1,
            value_template="{{ value_json.airtime_1h_s }}", **diagnostic,
        ),
        "airtime_available": component(
            "airtime_available", "sensor", "Airtime available", device_class="duration",
            unit_of_measurement="s", state_class="measurement", suggested_display_precision=1,
            value_template="{{ value_json.airtime_available_s }}",
            json_attributes_topic=topics.state,
            json_attributes_template=(
                "{{ {'budget_s_per_hour': value_json.airtime_budget_s_per_hour} | tojson }}"
            ),
            **diagnostic,
        ),
        "queue": component(
            "queue", "sensor", "Queue", state_class="measurement", icon="mdi:tray-full",
            value_template="{{ value_json.queue }}", **diagnostic,
        ),
        "message": component(
            "message", "event", "Message", state_topic=topics.received,
            event_types=["contact", "channel"],
        ),
        "delivery": component(
            "delivery", "event", "Delivery", state_topic=topics.delivery,
            event_types=[state.value for state in DeliveryState],
        ),
    }  # fmt: skip
    if config.commands.words:
        components["command"] = component(
            "command", "event", "Command", state_topic=topics.command,
            event_types=sorted(config.commands.words),
        )  # fmt: skip
    components.update(_mesh_components(component, topics))
    if config.homeassistant.contact_devices:
        components["show_hidden"] = component(
            "show_hidden", "button", "Show hidden contacts", command_topic=topics.show_hidden,
            payload_press="show", entity_category="config", icon="mdi:eye",
        )  # fmt: skip
    if config.diagnostics.publish_rx_log:  # one recorder row per packet: off until enabled
        rx = {"entity_category": "diagnostic", "state_topic": topics.rx}
        components["rx_rssi"] = component(
            "rx_rssi",
            "sensor",
            "Last packet RSSI",
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
            "Last packet SNR",
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
        components[key] = component(
            key, "notify", f"Send to {target.alias}", command_topic=topics.send(target.alias)
        )
    for key, platform in (removed or {}).items():
        if key not in components:
            stand_in = placeholder(f"{uid}_{key}", platform, topics) if bind else None
            components[key] = stand_in or {"p": platform}
    device: dict[str, Any] = {
        "ids": [uid],
        "name": config.homeassistant.device_name,
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


def _mesh_components(
    component: Callable[..., dict[str, Any]], topics: Topics
) -> dict[str, dict[str, Any]]:
    """What the observer publishes (ADR 0015): the radio's measurements, the contact table, the
    nodes heard, and their events."""
    stats = {"entity_category": "diagnostic", "state_topic": topics.stats}
    counter = {"state_class": "total_increasing", "enabled_by_default": False, **stats}
    contacts = {"state_topic": topics.contacts, "json_attributes_topic": topics.contacts}
    return {
        "noise_floor": component(
            "noise_floor", "sensor", "Noise floor", device_class="signal_strength",
            unit_of_measurement="dBm", state_class="measurement",
            value_template="{{ value_json.noise_floor_dbm }}", **stats,
        ),
        "radio_airtime": component(
            "radio_airtime", "sensor", "Radio transmit time last hour", device_class="duration",
            unit_of_measurement="s", state_class="measurement", suggested_display_precision=1,
            value_template="{{ value_json.tx_airtime_1h_s }}", json_attributes_topic=topics.stats,
            json_attributes_template=STATS_ATTRIBUTES, **stats,
        ),
        "uptime": component(
            "uptime", "sensor", "Radio uptime", device_class="duration", unit_of_measurement="s",
            enabled_by_default=False, value_template="{{ value_json.uptime_s }}", **stats,
        ),
        "packets_received": component(
            "packets_received", "sensor", "Packets received", icon="mdi:download-network",
            value_template="{{ value_json.packets_received }}", **counter,
        ),
        "packets_sent": component(
            "packets_sent", "sensor", "Packets sent", icon="mdi:upload-network",
            value_template="{{ value_json.packets_sent }}", **counter,
        ),
        "receive_errors": component(
            "receive_errors", "sensor", "Receive errors", icon="mdi:alert-circle-outline",
            value_template="{{ value_json.receive_errors }}", **counter,
        ),
        "contacts": component(
            "contacts", "sensor", "Contacts", icon="mdi:account-multiple",
            state_class="measurement", value_template="{{ value_json.total }}",
            json_attributes_template="{{ {'max': value_json.max} | tojson }}", **contacts,
        ),
        "nodes": component(
            "nodes", "sensor", "Nodes heard", icon="mdi:radio-tower", state_class="measurement",
            state_topic=topics.nodes, value_template="{{ value_json.total }}",
        ),
        "remote_cli": component(
            "remote_cli", "binary_sensor", "Contact with remote access", device_class="problem",
            entity_category="diagnostic",
            value_template="{{ 'ON' if value_json.remote_cli_total else 'OFF' }}",
            json_attributes_template=(
                "{{ {'contacts': value_json.remote_cli, 'total': value_json.remote_cli_total}"
                " | tojson }}"
            ),
            **contacts,
        ),
        "advert_position": component(
            "advert_position", "binary_sensor", "Companion position in adverts",
            device_class="problem", entity_category="diagnostic",
            value_template="{{ 'ON' if value_json.advert_position_total else 'OFF' }}",
            json_attributes_template=(
                "{{ {'contacts': value_json.advert_position, "
                "'total': value_json.advert_position_total} | tojson }}"
            ),
            **contacts,
        ),
        "contact": component(
            "contact", "event", "Contact", state_topic=topics.contact_event,
            event_types=["added", "removed", "hidden", "shown"],
        ),
        "node": component(
            "node", "event", "New node", state_topic=topics.node_event, event_types=["new"],
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
}


def placeholder(unique_id: str, platform: str, topics: Topics) -> dict[str, Any] | None:
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
        "name": "Removed", **options,
    }  # fmt: skip
