---
status: accepted
date: 2026-10-18
decision-makers: maintainers
---

# Home Assistant integration through MQTT device discovery

## Context and Problem Statement

Home Assistant should show the radio's state, receive messages and commands as triggers, and send
messages, without custom code in Home Assistant. How does the bridge present itself?

## Decision Drivers

* No custom integration, no YAML on the Home Assistant side for the basics.
* Entities that fit Home Assistant's model for each kind of data.
* Robust against restarts of Home Assistant, the broker or the bridge.

## Considered Options

* Device-based MQTT discovery (one document per device) with sensor, binary sensor, event and
  notify entities
* Single-component discovery (one document per entity)
* No discovery; users write MQTT entities and triggers in YAML

## Decision Outcome

Chosen option: "device-based MQTT discovery", because one document describes the whole device
consistently, and Home Assistant offers fitting platforms: `event` entities for received messages,
deliveries and commands, `notify` entities for sending, sensors for the state.

* Discovery topic `<prefix>/device/meshcore_home_gateway_<node>/config`, published when the bridge
  connects and again shortly after Home Assistant's birth message; not retained.
* Every component has a `unique_id`, and the document has `origin`: without them Home Assistant
  rejects the whole document. `default_entity_id` sets stable entity IDs.
* Events are published without retain; notify publishes the plain text to `send/<alias>`.
* The shared state document is always complete, so no template renders against a missing key.

### Consequences

* Good, because the device appears with all entities as soon as the bridge connects.
* Good, because nothing is retained in the discovery topic: Home Assistant does not re-create the
  entities of a removed bridge after a restart (its device stays until deleted). Components that
  a new configuration no longer has are removed by the bridge with `{"p": <platform>}`.
* Bad, because device-based discovery needs Home Assistant 2024.11 or newer; the documented
  entities and automations need 2026.8 ([home-assistant.md](../home-assistant.md), Requirements).
* Neutral, because the bridge remembers the components it announced (in its state file), so that
  it can remove the ones a new configuration no longer has.

### Confirmation

Unit tests check the document against the rules above. The rules were verified against Home
Assistant 2026.10.2 (discovery schema in `homeassistant/components/mqtt/`) and a live run with
Mosquitto, in which all entity types were created, updated and removed.

## Pros and Cons of the Options

### Single-component discovery

* Good, because it works with older Home Assistant versions.
* Bad, because the device is split over many documents that must stay consistent.

### No discovery

* Good, because nothing is created implicitly.
* Bad, because every user has to write and maintain the same YAML.

## More Information

* MQTT discovery: <https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery>
* MQTT event: <https://www.home-assistant.io/integrations/event.mqtt/>
* MQTT notify: <https://www.home-assistant.io/integrations/notify.mqtt/>
* Revisit if Home Assistant changes the discovery schema or deprecates the event or notify platforms.
