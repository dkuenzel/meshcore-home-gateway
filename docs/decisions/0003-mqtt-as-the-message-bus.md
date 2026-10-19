---
status: accepted
date: 2026-10-18
decision-makers: maintainers
---

# MQTT as the message bus between the bridge and its users

## Context and Problem Statement

Automations and scripts need three connectors to the radio: *send* a message, *listen* to
received messages, and *trigger* on authorised commands. They also need to see whether the bridge
and the radio are up. Which interface does the bridge offer?

## Decision Drivers

* Works with Home Assistant without custom code in Home Assistant.
* Decouples the bridge from its consumers: no credentials for the automation system in the
  bridge, and consumers can come and go.
* Several consumers at once (automation system, scripts, monitoring).
* Visible availability, also when the bridge process dies.
* Access control per action (who may send to which target).

## Considered Options

* MQTT with a broker that the installation already runs
* An HTTP API in the bridge with webhooks to the consumers
* The bridge calls the Home Assistant API directly (REST or WebSocket)
* An own socket protocol

## Decision Outcome

Chosen option: "MQTT", because Home Assistant integrates MQTT natively (including discovery of
entities), publish/subscribe serves many consumers without coupling, retained messages and the
last-will message make availability visible, and broker ACLs can restrict each client to the
topics it needs.

### Consequences

* Good, because sending is a publish to `<base>/<node>/send/<target>` and listening is a
  subscription; `mosquitto_pub` and `mosquitto_sub` are enough for scripts.
* Good, because the bridge needs no knowledge of the automation system.
* Bad, because the broker becomes part of the message path. If it is down, nothing flows: the
  bridge keeps reconnecting, and requests published while the bridge is offline are lost (clean
  session, see the design document). Consumers see this through the retained availability topic.
* Bad, because the broker must be secured: authentication for every client, topic ACLs, and TLS
  if traffic leaves a trusted network.

### Confirmation

The MQTT API is specified in [`docs/mqtt-api.md`](../mqtt-api.md) and covered by integration tests
against a real broker (`mosquitto`), including the last-will message.

## Pros and Cons of the Options

### MQTT

* Good, because it is the common bus of home automation systems.
* Good, because QoS 1 gives at-least-once delivery between the bridge and the broker.
* Neutral, because it needs a broker; installations that use Home Assistant with MQTT already
  run one.

### HTTP API with webhooks

* Good, because no broker is needed.
* Bad, because the bridge must know every consumer's webhook and credentials, and must handle
  retries to each of them.

### Direct calls into the Home Assistant API

* Good, because events appear in Home Assistant without discovery.
* Bad, because the bridge needs a long-lived Home Assistant token, a credential with full access,
  and is useless to any other consumer.

### Own socket protocol

* Bad, because every consumer would need a client for it.

## More Information

* Home Assistant MQTT integration: <https://www.home-assistant.io/integrations/mqtt/>
* MQTT 3.1.1 (OASIS standard): <https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/mqtt-v3.1.1.html>
* Revisit if a deployment cannot run a broker; a local CLI connector would then be the fallback.
