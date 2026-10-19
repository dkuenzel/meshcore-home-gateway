"""Home Assistant device-based MQTT discovery (ADR 0010, docs/home-assistant.md).

One retained-free document on `<prefix>/device/<device id>/config` describes the device and all
its entities. Rules checked against Home Assistant 2026.10.2 (docs/home-assistant.md, sources):
`origin.name` and `unique_id` per component are mandatory, otherwise the whole document is
rejected; events must not be retained; a component disappears only when it is sent once as
`{"p": <platform>}`.
"""

from __future__ import annotations

from typing import Any

from meshcore_bus import __version__
from meshcore_bus.config import Config
from meshcore_bus.core.model import DeliveryState
from meshcore_bus.mqtt.topics import Topics


def device_id(config: Config) -> str:
    return f"meshcore_bus_{config.mqtt.node_id.replace('-', '_')}"


def discovery_topic(config: Config) -> str:
    return f"{config.homeassistant.prefix}/device/{device_id(config)}/config"


def discovery_payload(config: Config, topics: Topics) -> dict[str, Any]:
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
            payload_off="disconnected", **diagnostic,
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
    for target in config.targets:
        key = f"send_{target.alias}"
        components[key] = component(
            key, "notify", f"Send to {target.alias}", command_topic=topics.send(target.alias)
        )
    return {
        "dev": {
            "ids": [uid],
            "name": config.homeassistant.device_name,
            "mf": "MeshCore",
            "mdl": "Companion radio via meshcore-bus",
            "sw": __version__,
        },
        "o": {"name": "meshcore-bus", "sw": __version__},
        "availability": [{"topic": topics.availability}],
        "qos": 1,
        "cmps": components,
    }
