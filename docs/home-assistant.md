# Home Assistant

The gateway announces itself through MQTT device discovery
([ADR 0010](decisions/0010-home-assistant-through-mqtt-device-discovery.md)). Home Assistant
needs nothing besides its MQTT integration: no custom integration, no HACS repository, no app and
no YAML. Automations are built in the automation editor with the trigger *Event received* and the
action *Send a notification message*.

## Requirements

* Home Assistant 2026.8 or newer, tested with 2026.10.
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

A `-` in the node ID becomes `_` in the entity IDs, as in an alias: node `radio-1` gives
`sensor.meshcore_home_gateway_radio_1_battery`. The discovery topic, the device and the unique
IDs keep the node ID as it is, so two gateways `radio-1` and `radio_1` on one broker are two
devices, but the entity IDs of the one announced second end in `_2`; pick node IDs that differ in
more than that. Node IDs and aliases that Home Assistant would shorten further (a trailing `-` or
`_`, two of them in a row) are refused by the configuration check.

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

## First steps

Once the gateway runs, its device is under *Settings → Devices & services → MQTT*, named after
`homeassistant.device_name`.

**Read a message.** A received message is an event of *Message*
(`event.meshcore_home_gateway_radio1_message`). Its text, sender, signal and times are attributes
of that event; the logbook and the device's activity show only the event type (`contact` or
`channel`), not the attributes. *Developer tools → States*, filtered for the entity, shows them
all. On a dashboard, a Markdown card shows the last message:

```yaml
type: markdown
title: Last mesh message
content: |-
  {% set e = 'event.meshcore_home_gateway_radio1_message' %}
  {% set text = state_attr(e, 'text') %}
  {% if text is none %}none yet{% else %}<div><b>{{ (state_attr(e, 'sender_name') or '?') | e }}</b>: {{ text | e | replace('\r', ' ') | replace('\n', ' ') }}</div>{% endif %}
```

Anyone in radio range chooses a message's text and a channel sender's name, so the card escapes
them and keeps them in an HTML block on one line: in Markdown, even an escaped text such as
`![](https://example.org/pixel.png)` or `[here](https://example.org)` becomes an image that the
browser loads or a link, and a blank line would end the block
([platform-notes.md](platform-notes.md#home-assistant-dashboards)).

**Send a message.** Each target is a notify entity, *Send to <alias>*; it shows *unknown* until it
sends for the first time. It has no input field of its own: it sends with the action
`notify.send_message`. To try it, open *Developer tools → Actions*, switch to YAML mode and run:

```yaml
action: notify.send_message
target:
  entity_id: notify.meshcore_home_gateway_radio1_send_alerts
data:
  message: Test from Home Assistant
```

In an automation, the same action is *Notifications: Send a notification message* (examples
below). A dashboard button that sends a fixed text:

```yaml
type: button
name: Test message
icon: mdi:send
tap_action:
  action: perform-action
  perform_action: notify.send_message
  target:
    entity_id: notify.meshcore_home_gateway_radio1_send_alerts
  data:
    message: Test from Home Assistant
```

**See whether it arrived.** *Delivery* shows each step as an event: `queued`, then `sent` (final
for a channel) and `confirmed` when the recipient of a direct message acknowledged it, or `failed`
or `expired` with a `reason` attribute ([mqtt-api.md](mqtt-api.md#delivery-delivery)). The same
test without Home Assistant, in the gateway's container:
`meshcore-home-gateway send --wait 60 alerts "test"`.

## How discovery behaves

* The gateway publishes the discovery document when it connects to the broker, a random 0.5–2 s
  after Home Assistant's birth message (`online` on `status_topic`), and when the radio's model,
  firmware or key first becomes known or changes. The document is not retained; after a restart of
  Home Assistant the entities are back with the birth message, without any action.
* A component of an earlier configuration (a removed target, command words or the rx log switched
  off) is removed by the gateway, also when Home Assistant restarted in between: each announcement
  sends a disabled placeholder under the old ID, which Home Assistant binds, and then the removal.
  The state file keeps a removal pending for 30 days, since the gateway cannot see whether Home
  Assistant received it; Home Assistant must have run in that time. A leftover entity can be
  deleted under *Settings → Entities*. Delete only one that the gateway no longer announces:
  deleting a current entity clears the whole device's discovery topic, so all its entities go
  until the gateway announces again (restart the gateway;
  [platform-notes.md](platform-notes.md#home-assistant-mqtt)).
* To retire the gateway, stop it (or set `homeassistant.discovery = false`), then delete the device
  in Home Assistant.
* Events are never retained: Home Assistant ignores replayed retained events.
* `notify.send_message` publishes only `message`; `title` is dropped by Home Assistant. Send
  through the notify entities rather than `mqtt.publish`, which since 2026.10 needs an
  administrator when a user starts it.

These rules come from Home Assistant's MQTT discovery code (`homeassistant/components/mqtt/`:
`schemas.py`, `discovery.py`, `event.py`, `notify.py`). The contract tests in
[`home-assistant/tests/`](../home-assistant/tests/test_contract.py) check them with the real MQTT
integration of the release pinned in
[`home-assistant/requirements.in`](../home-assistant/requirements.in): the entity IDs above, the
device page, sensors and attributes, availability, the removal of components, the Markdown card
and the two actions of the first steps, and the examples below, exactly as written here
(`make ha-test`, [testing.md](testing.md)).

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
Assistant stores them in its history database, and the last event of each entity also in
`.storage/core.restore_state` (restored after a restart). To keep them out of the history,
exclude the entity from the recorder:

```yaml
recorder:
  exclude:
    entities:
      - event.meshcore_home_gateway_radio1_message
```
