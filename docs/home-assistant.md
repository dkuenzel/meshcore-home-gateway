# Home Assistant

The bridge announces itself through MQTT device discovery
([ADR 0010](decisions/0010-home-assistant-through-mqtt-device-discovery.md)). Requirements: the
MQTT integration with discovery enabled (the default), Home Assistant 2024.11 or newer for
device-based discovery, and `homeassistant.discovery = true` in the bridge configuration.

## Entities

One device, named after `homeassistant.device_name`, with these entities (entity IDs from
`default_entity_id`, here for node `radio1`):

| Entity | Platform | Content |
| --- | --- | --- |
| `binary_sensor.meshcore_home_gateway_radio1_radio` | connectivity, diagnostic | radio link up |
| `sensor.meshcore_home_gateway_radio1_battery` | voltage (V), diagnostic | radio battery |
| `sensor.meshcore_home_gateway_radio1_airtime` | duration (s), diagnostic | airtime of the last hour |
| `event.meshcore_home_gateway_radio1_message` | event: `contact`, `channel` | every received message; the payload keys are attributes |
| `event.meshcore_home_gateway_radio1_delivery` | event: `queued` … `rejected` | delivery steps of outgoing messages |
| `event.meshcore_home_gateway_radio1_command` | event: the configured words | accepted commands (only with `commands.words`) |
| `notify.meshcore_home_gateway_radio1_send_<alias>` | notify, one per target | sends the message text to that target |

All entities share the bridge's availability topic and turn `unavailable` when the bridge is
offline. The state values come from the retained state document, so they are back right after a
restart of either side.

## How discovery behaves

* The bridge publishes the discovery document when it connects to the broker and again a random
  0.5–2 s after Home Assistant announces itself on `homeassistant/status` (`online`). The document
  is not retained, so removing the bridge leaves no ghost entities behind.
* Events are never retained: Home Assistant ignores replayed retained events.
* `notify.send_message` publishes only `message`; `title` is dropped by Home Assistant.
* Removing a target from the configuration does not remove its notify entity; delete it in Home
  Assistant (or publish the component once as `{"p": "notify"}`, see design §12).

These rules were checked against Home Assistant 2026.10.2: the MQTT discovery schema in
`homeassistant/components/mqtt/` (`schemas.py`, `discovery.py`, `event.py`, `notify.py`) and a live
end-to-end run with Mosquitto.

## Examples

Send an alert to a channel when the smoke detector fires:

```yaml
automation:
  - alias: Smoke alert over the mesh
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

React to a command from an allowlisted handheld and answer the sender. An event entity's state is
the time of the last event, so every event changes it; `not_from` ignores the bridge coming back
online, and the condition picks the command word:

```yaml
automation:
  - alias: Status on request over the mesh
    triggers:
      - trigger: state
        entity_id: event.meshcore_home_gateway_radio1_command
        not_from: [unavailable, unknown]
    conditions:
      - condition: state
        entity_id: event.meshcore_home_gateway_radio1_command
        attribute: event_type
        state: status
    actions:
      - action: notify.send_message
        target:
          entity_id: notify.meshcore_home_gateway_radio1_send_operator
        data:
          message: >-
            Doors {{ states('binary_sensor.doors') }},
            power {{ states('sensor.grid_power') }} W
```

Get told when an alert could not be delivered:

```yaml
automation:
  - alias: Mesh delivery failed
    triggers:
      - trigger: state
        entity_id: event.meshcore_home_gateway_radio1_delivery
        not_from: [unavailable, unknown]
    conditions:
      - condition: state
        entity_id: event.meshcore_home_gateway_radio1_delivery
        attribute: event_type
        state: failed
    actions:
      - action: persistent_notification.create
        data:
          title: Mesh message failed
          message: >-
            {{ state_attr('event.meshcore_home_gateway_radio1_delivery', 'target') }}:
            {{ state_attr('event.meshcore_home_gateway_radio1_delivery', 'reason') }}
```

A trigger on the attribute `event_type` alone would miss the second of two equal events in a row,
because the attribute does not change.
