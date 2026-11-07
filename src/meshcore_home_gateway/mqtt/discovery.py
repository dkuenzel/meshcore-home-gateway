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
    from meshcore_home_gateway.ports import RadioInfo

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
# the health section (ARC-012), which a document of an earlier version lacks
HEALTH_KEYS = (
    "history",
    "state_file",
    "exclusions",
    "errors_1h",
    "loop_lag_max_s",
    "events_dropped_1h",
    "disk_free_mb",
)
HEALTH_ATTRIBUTES = (
    "{% set h = value_json.get('health') or {} %}{{ {"
    + ", ".join(f"'{key}': h.get('{key}')" for key in HEALTH_KEYS)
    + "} | tojson }}"
)


def device_id(config: Config) -> str:
    """The node ID unchanged: node IDs that differ only in '-' and '_' are two devices (Home
    Assistant turns '-' into '_' only in the entity IDs)."""
    return f"meshcore_home_gateway_{config.mqtt.node_id}"


def discovery_topic(config: Config) -> str:
    return f"{config.homeassistant.prefix}/device/{device_id(config)}/config"


def controls_off(config: Config, topics: Topics) -> list[dict[str, str]]:
    """With `[homeassistant] controls = false`, an availability that is always offline, for the
    controls that change the radio's contacts or the telemetry: Home Assistant shows them
    unavailable and leaves them out of service calls, while MQTT and the command line keep
    working (SEC-008). Unavailable rather than removed: a disabled entity that goes takes its
    whole device away. Home Assistant keys availability by topic, so the entry has a topic of
    its own, which nobody publishes: on the gateway's availability topic it took effect only with
    the next message there (HA-02). A message another client writes there still reads offline."""
    if config.homeassistant.controls:
        return []
    return [{"topic": f"{topics.root}/homeassistant/off", "value_template": "{{ 'offline' }}"}]


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
        "device_class": "duration",
        "unit_of_measurement": "s",
        "state_class": "measurement",
        "suggested_display_precision": 1,
        "enabled_by_default": False,
        **diagnostic,
    }
    components: dict[str, dict[str, Any]] = {
        "radio": component(
            "radio",
            "binary_sensor",
            # Translators: binary sensor (connectivity) of the gateway: its USB or TCP link to
            # its own radio, not a link over the air. Home Assistant shows the device name first.
            ha_name("Radio link"),
            device_class="connectivity",
            value_template="{{ value_json.radio }}",
            payload_on="connected",
            payload_off="disconnected",
            json_attributes_topic=topics.state,
            json_attributes_template=RADIO_ATTRIBUTES,
            **diagnostic,
        ),
        "battery": component(
            "battery",
            "sensor",
            # Translators: sensor (voltage) of the gateway: the battery of its own radio
            ha_name("Battery"),
            device_class="voltage",
            unit_of_measurement="V",
            state_class="measurement",
            suggested_display_precision=2,
            value_template="{{ value_json.battery_v }}",
            **diagnostic,
        ),
        "radio_clock_offset": component(
            "radio_clock_offset",
            "sensor",
            # Translators: sensor (duration) of the gateway: how far its own radio's clock is
            # ahead of the gateway's clock (negative: behind)
            ha_name("Radio clock offset"),
            device_class="duration",
            unit_of_measurement="s",
            state_class="measurement",
            icon="mdi:clock-alert-outline",
            value_template="{{ value_json.get('radio_clock_offset_s') }}",
            **diagnostic,
        ),
        "airtime": component(
            "airtime",
            "sensor",
            # Translators: sensor (duration) of the gateway: how long it transmitted in the
            # last hour (airtime: the time on air, as in Home Assistant's own words)
            ha_name("Airtime last hour"),
            device_class="duration",
            unit_of_measurement="s",
            state_class="measurement",
            suggested_display_precision=1,
            value_template="{{ value_json.airtime_1h_s }}",
            **diagnostic,
        ),
        "airtime_available": component(
            "airtime_available",
            "sensor",
            # Translators: sensor (duration) of the gateway: the transmit time left in its
            # hourly budget
            ha_name("Airtime available"),
            device_class="duration",
            unit_of_measurement="s",
            state_class="measurement",
            suggested_display_precision=1,
            value_template="{{ value_json.airtime_available_s }}",
            json_attributes_topic=topics.state,
            json_attributes_template=(
                "{{ {'budget_s_per_hour': value_json.airtime_budget_s_per_hour} | tojson }}"
            ),
            **diagnostic,
        ),
        "queue": component(
            "queue",
            "sensor",
            # Translators: sensor of the gateway: the messages waiting to be sent
            ha_name("Queue"),
            state_class="measurement",
            icon="mdi:tray-full",
            value_template="{{ value_json.queue }}",
            **diagnostic,
        ),
        "degraded": component(
            "degraded",
            "binary_sensor",
            # Translators: binary sensor (problem) of the gateway: on while a part of it is in
            # trouble (its history, its state file, a bug it caught, a host that holds it up);
            # the log tells which
            ha_name("Gateway degraded"),
            device_class="problem",
            icon="mdi:heart-pulse",
            value_template=(
                "{{ ('ON' if value_json.health.degraded else 'OFF') "
                "if value_json.get('health') is not none else None }}"
            ),
            json_attributes_topic=topics.state,
            json_attributes_template=HEALTH_ATTRIBUTES,
            **diagnostic,
        ),
        "message": component(
            "message",
            "event",
            # Translators: event entity of the gateway: a message received from a contact or on
            # a channel
            ha_name("Message"),
            state_topic=topics.received,
            event_types=["contact", "channel"],
        ),
        "delivery": component(
            "delivery",
            "event",
            # Translators: event entity of the gateway: how far a message it sent has come
            # (queued, sent, confirmed, failed)
            ha_name("Delivery"),
            state_topic=topics.delivery,
            event_types=[state.value for state in DeliveryState],
        ),
        # the `history` document: the newest message's time (unknown while there is none), the
        # messages as attributes. Available while the document is there: the gateway never
        # clears it, so the messages stay readable while the gateway is offline.
        "message_history": component(
            "message_history",
            "sensor",
            # Translators: sensor (timestamp) of the gateway: the latest messages it received and
            # sent; its state is the time of the newest one
            ha_name("Message history"),
            device_class="timestamp",
            state_topic=topics.history,
            value_template="{{ value_json.messages[0].at if value_json.messages else None }}",
            json_attributes_topic=topics.history,
            availability=[{"topic": topics.history, "value_template": "{{ 'online' }}"}],
        ),
        # the `pending` document: how many messages are on their way, the one being sent
        # included (#112), the messages as attributes; available as the history is
        "pending_messages": component(
            "pending_messages",
            "sensor",
            # Translators: sensor of the gateway: how many messages it accepted and has not
            # delivered or given up yet, the one being sent included
            ha_name("Messages on their way"),
            icon="mdi:tray-arrow-up",
            state_topic=topics.pending,
            value_template="{{ value_json.total }}",
            json_attributes_topic=topics.pending,
            availability=[{"topic": topics.pending, "value_template": "{{ 'online' }}"}],
        ),
        # also with telemetry off: a disabled component that goes takes the whole device away
        "telemetry_status": component(
            "telemetry_status",
            "sensor",
            # Translators: sensor (enum) of the gateway: whether it asks the household's
            # companions for telemetry; keep it apart from "Telemetry state" of a contact
            ha_name("Telemetry status"),
            device_class="enum",
            options=list(telemetry.STATUSES),
            icon="mdi:satellite-uplink",
            value_template=TELEMETRY_STATUS,
            json_attributes_topic=topics.state,
            json_attributes_template=TELEMETRY_REASON,
            **diagnostic,
        ),
        "telemetry_airtime": component(
            "telemetry_airtime",
            "sensor",
            # Translators: sensor (duration) of the gateway: its own transmit time for
            # telemetry requests in the last hour
            ha_name("Telemetry airtime last hour"),
            value_template="{{ value_json.get('telemetry_airtime_1h_s') }}",
            **lane,
        ),
        "telemetry_mesh_airtime": component(
            "telemetry_mesh_airtime",
            "sensor",
            # Translators: sensor (duration) of the gateway: the airtime its telemetry requests
            # caused in the whole mesh in the last hour, as estimated
            ha_name("Telemetry mesh airtime last hour"),
            value_template="{{ value_json.get('telemetry_mesh_airtime_1h_s') }}",
            **lane,
        ),
        "telemetry": component(
            "telemetry",
            "event",
            # Translators: event entity of the gateway: a companion answered a telemetry
            # request, or did not
            ha_name("Telemetry"),
            state_topic=topics.telemetry_event,
            event_types=list(telemetry.EVENTS),
        ),
    }
    if config.commands.words:
        components["command"] = component(
            "command",
            "event",
            # Translators: event entity of the gateway: a command word from an allowed sender
            ha_name("Command"),
            state_topic=topics.command,
            event_types=sorted(config.commands.words),
        )
    if config.radio.transport == "tcp":  # USB has no other client
        components["radio_link_contested"] = component(
            "radio_link_contested",
            "binary_sensor",
            # Translators: binary sensor (problem) of the gateway: on for an hour after another
            # client took over the TCP link to its own radio, which lets that client control the
            # radio and read its private key
            ha_name("Radio link taken over"),
            device_class="problem",
            icon="mdi:lan-disconnect",
            value_template=(
                "{{ ('ON' if value_json.radio_link_contested else 'OFF') "
                "if value_json.get('radio_link_contested') is not none else None }}"
            ),
            **diagnostic,
        )
    components.update(_mesh_components(component, topics, ha_name))
    if config.homeassistant.contact_devices:
        off = controls_off(config, topics)
        components["show_hidden"] = component(
            "show_hidden",
            "button",
            # Translators: button of the gateway: brings back the contacts' devices the user
            # deleted in Home Assistant
            ha_name("Show hidden contacts"),
            command_topic=topics.show_hidden,
            payload_press="show",
            entity_category="config",
            icon="mdi:eye",
            **(
                {"availability": [{"topic": topics.availability}, *off], "availability_mode": "all"}
                if off
                else {}
            ),
        )
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
        if target.alias in config.homeassistant.hidden_targets:  # automations use MQTT
            continue
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


def gateway_names(config: Config) -> frozenset[str]:
    """The names the gateway goes by: its device in Home Assistant, and its radio's name when
    the profile sets it. A node or contact of such a name could pass for the gateway (SEC-007)."""
    names = {gateway_device_name(config), config.radio.profile.name}
    return frozenset(n for n in names if n)


def gateway_device_name(config: Config) -> str:
    """The name of the gateway's device in Home Assistant, in the language of its names; the
    Homie device has it too."""
    return device_name(config, i18n.translator(config.homeassistant.language).ha_name)


def with_removed(
    components: dict[str, dict[str, Any]],
    uid: str,
    topics: Topics,
    removed: Mapping[str, str] | None,
    ha_name: Callable[..., str],
    *,
    bind: bool,
) -> None:
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
            "noise_floor",
            "sensor",
            # Translators: sensor (signal strength) of the gateway: the noise its radio hears
            # when nobody transmits
            ha_name("Noise floor"),
            device_class="signal_strength",
            unit_of_measurement="dBm",
            state_class="measurement",
            value_template="{{ value_json.noise_floor_dbm }}",
            **stats,
        ),
        "radio_airtime": component(
            "radio_airtime",
            "sensor",
            # Translators: sensor (duration) of the gateway: what its radio itself counted as
            # sent in the last hour, adverts and acknowledgements included
            ha_name("Radio transmit time last hour"),
            device_class="duration",
            unit_of_measurement="s",
            state_class="measurement",
            suggested_display_precision=1,
            value_template="{{ value_json.tx_airtime_1h_s }}",
            json_attributes_topic=topics.stats,
            json_attributes_template=STATS_ATTRIBUTES,
            **stats,
        ),
        "uptime": component(
            "uptime",
            "sensor",
            # Translators: sensor (duration) of the gateway: the time since its radio started
            ha_name("Radio uptime"),
            device_class="duration",
            unit_of_measurement="s",
            enabled_by_default=False,
            value_template="{{ value_json.uptime_s }}",
            **stats,
        ),
        "packets_received": component(
            "packets_received",
            "sensor",
            # Translators: sensor of the gateway: a counter of the packets its radio received
            ha_name("Packets received"),
            icon="mdi:download-network",
            value_template="{{ value_json.packets_received }}",
            **counter,
        ),
        "packets_sent": component(
            "packets_sent",
            "sensor",
            # Translators: sensor of the gateway: a counter of the packets its radio sent
            ha_name("Packets sent"),
            icon="mdi:upload-network",
            value_template="{{ value_json.packets_sent }}",
            **counter,
        ),
        "receive_errors": component(
            "receive_errors",
            "sensor",
            # Translators: sensor of the gateway: a counter of the packets its radio could not
            # read
            ha_name("Receive errors"),
            icon="mdi:alert-circle-outline",
            value_template="{{ value_json.receive_errors }}",
            **counter,
        ),
        "contacts": component(
            "contacts",
            "sensor",
            # Translators: sensor of the gateway: the number of contacts on its radio
            ha_name("Contacts"),
            icon="mdi:account-multiple",
            state_class="measurement",
            value_template="{{ value_json.total }}",
            # why requests to add or remove contacts are refused now (#99)
            json_attributes_template=(
                "{{ {'max': value_json.max, 'manage_reason': value_json.get('manage_reason')}"
                " | tojson }}"
            ),
            **contacts,
        ),
        "nodes": component(
            "nodes",
            "sensor",
            # Translators: sensor of the gateway: the number of nodes its radio heard lately
            # (node: any MeshCore station)
            ha_name("Nodes heard"),
            icon="mdi:radio-tower",
            state_class="measurement",
            state_topic=topics.nodes,
            value_template="{{ value_json.total }}",
        ),
        "remote_cli": component(
            "remote_cli",
            "binary_sensor",
            # Translators: binary sensor (problem) of the gateway: on while a contact may use its
            # radio's command line over the air
            ha_name("Contact with remote access"),
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
            "advert_position",
            "binary_sensor",
            # Translators: binary sensor (problem) of the gateway: on while a companion of the
            # household sends its position in its adverts, which every node hears (companion: a
            # person's radio; advert: the packet a node announces itself with)
            ha_name("Companion position in adverts"),
            device_class="problem",
            entity_category="diagnostic",
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
            "look_alike",
            "binary_sensor",
            # Translators: binary sensor (problem) of the gateway: on while it hears a node
            # whose key begins like a protected contact's, which may pose as that contact
            ha_name("Look-alike node heard"),
            device_class="problem",
            entity_category="diagnostic",
            state_topic=topics.nodes,
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
            "contact",
            "event",
            # Translators: event entity of the gateway: a contact was added, removed, hidden,
            # shown, refused, excluded or included
            ha_name("Contact"),
            state_topic=topics.contact_event,
            event_types=[
                "added",
                "removed",
                "hidden",
                "shown",
                "refused",
                "failed",
                "excluded",
                "included",
                "position_in_advert",
            ],
        ),
        "node": component(
            "node",
            "event",
            # Translators: event entity of the gateway: its radio heard a node for the first
            # time
            ha_name("New node"),
            state_topic=topics.node_event,
            event_types=["new"],
        ),
        "profile": component(  # the whole settings document as attributes
            "profile",
            "sensor",
            # Translators: sensor (enum) of the gateway: whether its radio's settings match the
            # profile the gateway keeps on it
            ha_name("Radio profile"),
            device_class="enum",
            options=PROFILE_STATES,
            icon="mdi:shield-check-outline",
            entity_category="diagnostic",
            state_topic=topics.settings,
            value_template="{{ value_json.status }}",
            json_attributes_topic=topics.settings,
        ),
        "setting": component(
            "setting",
            "event",
            # Translators: event entity of the gateway: it wrote a setting of the profile to its
            # radio, or could not
            ha_name("Radio setting"),
            state_topic=topics.setting_event,
            event_types=["written", "failed", "not_taken", "not_converging", "drift"],
        ),
    }


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
        "p": platform,
        "unique_id": unique_id,
        "default_entity_id": f"{platform}.{unique_id}",
        # Translators: the name of an entity that exists only for a moment, while the gateway
        # removes it from Home Assistant
        "name": ha_name("Removed"),
        **options,
    }
