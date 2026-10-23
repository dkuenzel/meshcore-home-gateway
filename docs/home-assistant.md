# Home Assistant

The gateway announces itself through MQTT device discovery
([ADR 0010](decisions/0010-home-assistant-through-mqtt-device-discovery.md)). Home Assistant
needs nothing besides its MQTT integration: no custom integration, no HACS repository, no app and
no YAML. Automations are built in the automation editor with the trigger *Event received* and the
action *Send a notification message*.

## Requirements

* Home Assistant 2026.8 or newer, tested with 2026.10.2.
* The MQTT integration connected to the same broker, with discovery and the birth message enabled
  (both are the defaults).
* In the gateway configuration: `homeassistant.discovery = true` (the default), `prefix` equal to
  the discovery prefix and `status_topic` equal to the birth message topic in the MQTT options of
  Home Assistant (defaults `homeassistant` and `homeassistant/status`).
* With a topic ACL on the broker, Home Assistant's user needs `readwrite` on `homeassistant/#`
  ([mqtt-api.md](mqtt-api.md), last section).

Why 2026.8: device-based discovery exists since 2024.11, the entity IDs below (from
`default_entity_id`) since 2025.10, QoS 1 for every entity from the device level since 2026.5.2,
the `event.received` trigger without the Labs setting since 2026.7, and since 2026.8 every event
is a state change of its own, also two events within the same millisecond.

## Entities

One device, named after `homeassistant.device_name`. Once the radio has connected, the device page
shows its model, firmware and public key (as serial number). The entities, with their IDs for
node `radio1`:

| Entity | Platform | Content |
| --- | --- | --- |
| `binary_sensor.meshcore_home_gateway_radio1_radio` | connectivity, diagnostic | radio link up; attributes below |
| `sensor.meshcore_home_gateway_radio1_battery` | voltage (V), diagnostic | radio battery |
| `sensor.meshcore_home_gateway_radio1_airtime` | duration (s), diagnostic | airtime of the last hour |
| `sensor.meshcore_home_gateway_radio1_airtime_available` | duration (s), diagnostic | airtime that may be spent now; attribute `budget_s_per_hour` |
| `sensor.meshcore_home_gateway_radio1_queue` | diagnostic | messages waiting for the radio |
| `event.meshcore_home_gateway_radio1_message` | event: `contact`, `channel` | every received message |
| `event.meshcore_home_gateway_radio1_delivery` | event: `queued` … `rejected` | delivery steps of outgoing messages |
| `event.meshcore_home_gateway_radio1_command` | event: the configured words | accepted commands (only with `commands.words`) |
| `notify.meshcore_home_gateway_radio1_send_<alias>` | notify, one per target | sends the message text to that target; `-` in an alias becomes `_` |
| `sensor.meshcore_home_gateway_radio1_rx_rssi`, `..._rx_snr` | signal strength, diagnostic | last received packet (only with `diagnostics.publish_rx_log`; disabled until enabled in Home Assistant) |

All entities share the gateway's availability topic and turn `unavailable` when it is offline.
The state values come from the retained state document, so they are back right after a restart of
either side.

## What reaches Home Assistant

| MQTT | In Home Assistant |
| --- | --- |
| `state`: `radio`, `battery_v`, `airtime_1h_s`, `airtime_available_s`, `queue` | the sensors above |
| `state`: `radio_name`, `public_key`, `model`, `firmware`, `frequency_mhz`, `bandwidth_khz`, `spreading_factor`, `coding_rate`, `tx_power_dbm`, `version` | attributes of the radio link (`version` as `bridge_version`) |
| `state`: `airtime_budget_s_per_hour` | attribute of *Airtime available* |
| `received`, `delivery`, `command` | every key becomes an attribute of the event entity, also the nested `command` object |
| `rx`: `rssi_dbm`, `snr_db` | the packet sensors; `raw`, `length` and `received_at` stay on MQTT |
| `send/<alias>` | the notify entities |

An entities card shows an attribute, such as the text of the last message, without YAML.

## How discovery behaves

* The gateway publishes the discovery document when it connects to the broker, a random 0.5–2 s
  after Home Assistant's birth message (`online` on `status_topic`), and when the radio's model,
  firmware or key first becomes known or changes. The document is not retained; after a restart of
  Home Assistant the entities are back with the birth message, without any action.
* A component of an earlier configuration (a removed target, command words or the rx log switched
  off) is removed by the gateway at its next start: it is sent as `{"p": <platform>}` in every
  announcement of that run. The state file remembers what was announced.
* To retire the gateway, stop it (or set `homeassistant.discovery = false`), then delete the device
  in Home Assistant.
* Events are never retained: Home Assistant ignores replayed retained events.
* `notify.send_message` publishes only `message`; `title` is dropped by Home Assistant. Send
  through the notify entities rather than `mqtt.publish`, which since 2026.10 needs an
  administrator when a user starts it.

These rules were checked against Home Assistant 2026.10.2: the MQTT discovery schema in
`homeassistant/components/mqtt/` (`schemas.py`, `discovery.py`, `event.py`, `notify.py`), and the
documents of this version run through the real MQTT integration
(`pytest-homeassistant-custom-component` 0.13.373): entity IDs, device page, attributes, removal,
and the examples below.

## Examples

Each example is one automation. Paste it in the automation editor (*Edit in YAML*), or add it
under the `automation:` key of an existing configuration.

Send an alert to a channel when the smoke detector fires:

```yaml
alias: Smoke alert over the mesh
triggers:
  - trigger: state
    entity_id: binary_sensor.kitchen_smoke
    to: "on"
actions:
  - action: notify.send_message
    target:
      entity_id: notify.meshcore_home_gateway_radio1_send_alerts
    data:
      message: "Smoke in the kitchen"
```

Answer the command `status` from an allowlisted handheld:

```yaml
alias: Status on request over the mesh
triggers:
  - trigger: event.received
    target:
      entity_id: event.meshcore_home_gateway_radio1_command
    options:
      event_type: [status]
actions:
  - action: notify.send_message
    target:
      entity_id: notify.meshcore_home_gateway_radio1_send_operator
    data:
      message: >-
        Doors {{ states('binary_sensor.doors') }},
        power {{ states('sensor.grid_power') }} W
mode: queued
```

Get told when a message could not be delivered:

```yaml
alias: Mesh delivery failed
triggers:
  - trigger: event.received
    target:
      entity_id: event.meshcore_home_gateway_radio1_delivery
    options:
      event_type: [failed, expired, rejected]
actions:
  - action: persistent_notification.create
    data:
      title: Mesh message not delivered
      message: >-
        {{ trigger.to_state.attributes.target }}:
        {{ trigger.to_state.attributes.event_type }}
        ({{ trigger.to_state.attributes.reason }})
mode: queued
```

Do not report a failed delivery over the mesh to the same target: when the radio cannot reach it,
the report fails too and uses airtime.

Use `event.received`: a state trigger on an event entity misses its first event (the state changes
from `unknown`), and a trigger on the attribute `event_type` misses the second of two equal events.
The event's keys are in `trigger.to_state.attributes`, for example `args` of a command.

## Privacy

Message texts, sender keys and sender names are attributes of the event entities, so Home
Assistant stores them in its history database. To keep them out, exclude the entity from the
recorder:

```yaml
recorder:
  exclude:
    entities:
      - event.meshcore_home_gateway_radio1_message
```
