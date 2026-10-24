---
status: accepted
date: 2026-10-23
decision-makers: maintainers
---

# Provider-neutral documents on the bus; front ends are adapters

## Context and Problem Statement

The observer ([ADR 0014](0014-the-gateway-as-the-households-mesh-observer.md)) collects data about
the radio, its contacts, the nodes around, positions, telemetry and messages. Home Assistant shall
show it first; a terminal dashboard and a web dashboard come later; other home automation systems
(openHAB, ioBroker, Node-RED, FHEM, Domoticz) should be able to use it without code in the
gateway, where that costs little. How does the gateway offer its data so that one implementation
serves all of them?

## Decision Drivers

* One source of truth for every value; no front end computes what another one cannot see.
* Home Assistant without custom code ([ADR 0010](0010-home-assistant-through-mqtt-device-discovery.md)).
* Other frameworks through their standard MQTT bindings.
* No new runtime dependency ([ADR 0005](0005-runtime-dependencies-from-the-distribution.md)), no
  new network listener in the gateway (attack surface, authentication).
* Bounded sizes: Home Assistant does not record attributes over 16 KiB, the documented broker
  setting allows packets up to 64 KiB ([mqtt-api.md](../mqtt-api.md), "Broker").
* KISS: few, stable shapes that are easy to test.

## Considered Options

* **Documents:** retained JSON documents per object, events and requests on MQTT; front ends map
  them (Home Assistant through discovery, the others by subscribing).
* **Per front end:** each front end gets its own adapter in the gateway with its own topics or
  protocol (Home Assistant-shaped topics, an in-process terminal view, a web server).
* **Homie convention:** one retained topic per property under `homie/<device>/…`, which openHAB
  discovers on its own.
* **HTTP API** in the gateway: REST for snapshots, server-sent events or WebSockets for changes.

## Decision Outcome

Chosen option: "documents", because the broker already is the one interface every candidate front
end speaks, retained documents give each new subscriber the current state at once, and Home
Assistant's discovery only needs to point entities at fields of them.

The rules (the "provider API", part of the public MQTT API under semantic versioning):

* **Providers are pure functions** (module `views`, checked as pure like the core) of the
  gateway's state, which the core holds: each builds one document (radio state, radio
  statistics, contact index, one contact, heard nodes, recent history). An adapter publishes a
  document when it changed, retained, and clears the topic of an object that is gone with an
  empty retained message.
* **Documents** are JSON objects with every key always present (`null` when unknown), times in
  ISO 8601 UTC, units in the key name (`_v`, `_dbm`, `_s`, `_m`), machine values in lower case
  (`chat`, `repeater`, `telemetry`) and no translated text: presentation belongs to the front end.
  Texts from the mesh are data and are never markup. Within a major version keys are only added;
  consumers ignore keys they do not know.
* **Sizes:** a document that Home Assistant reads into attributes stays below 16 KiB, every
  document below 64 KiB; lists are bounded and carry `total` and `truncated`.
* **Events** are not retained and carry `event_type` (as `received`, `delivery`, `command` today).
* **Requests** are not retained; every request ends in an event with the outcome, refusals with a
  reason, as for sending today.
* **Per-object topics** below `<base>/<node>/`: `contacts/<key>` for each contact, so that a front
  end subscribes to what it shows and no single document grows with the table.
* **Home Assistant** is an adapter: discovery documents whose entities read fields of the documents
  with templates and send requests; nothing in the documents exists only for Home Assistant.
* **Later front ends** use the same topics: the terminal dashboard as an MQTT client (or reading the
  local history directly on the gateway host), the web dashboard as a static page that connects to
  the broker over MQTT over WebSockets with its own broker user and ACL. History beyond the recent
  lists will be offered as a request with a result event, when the web dashboard needs it.
* **Other frameworks** subscribe and extract fields (openHAB JSONPATH transformation, ioBroker's
  MQTT adapter, Node-RED's JSON node); the MQTT API documentation shows the topics and fields, and
  examples follow when users ask for them.

### Consequences

* Good, because every value is computed once, in the core, and tested there; front ends cannot
  drift apart.
* Good, because a new front end needs no change in the gateway, and the broker's ACLs decide who
  may read and request what.
* Good, because a retained document is a cache: a dashboard that connects sees the current state
  without asking the gateway.
* Bad, because consumers need JSON extraction instead of reading a plain value; every candidate
  framework has it built in.
* Bad, because retained topics outlive the gateway: after an uninstall they stay on the broker
  until cleared (`mosquitto_pub -r -n -t …`), which the deployment guide documents.
* Neutral, because the gateway remembers which per-object topics it published, so that it can
  clear those of objects that disappeared while it was stopped.

### Confirmation

Unit tests build every document from core state and check the rules above (keys always present,
sizes under worst-case input, no markup interpretation needed). The Home Assistant contract tests
render the entities from the documents in a real Home Assistant. The architecture test keeps the
providers pure.

## Pros and Cons of the Options

### Documents

* Good, because one mechanism serves all front ends.
* Good, because atomic: a document is consistent in itself, unlike many property topics updated one
  after another.
* Bad, because a large object must be split by hand (per contact).

### Per front end

* Good, because each front end gets exactly its shape.
* Bad, because the gateway would grow with every front end, and the same value would be computed
  several times; a web server would add a listener, authentication and TLS to the gateway.

### Homie convention

* Good, because openHAB discovers Homie devices without configuration.
* Bad, because Home Assistant does not use it, so the gateway would publish everything twice; one
  retained topic per property multiplies retained messages (a contact with 15 properties, 350
  contacts: over 5000 topics); values of one object are not updated atomically.
* Neutral: it can be added later as a further adapter fed by the same providers.

### HTTP API

* Good, because a web dashboard could query history directly.
* Bad, because it is a new listener with its own authentication, TLS and hardening, inside a
  sandboxed service that today only connects outwards
  ([ADR 0006](0006-deployment-in-an-unprivileged-proxmox-container.md)).
* Bad, because changes still need a push channel; the broker already is one.

## More Information

* Home Assistant drops recorded attributes over 16 KiB: `MAX_STATE_ATTRS_BYTES = 16384` in
  `homeassistant/components/recorder/db_schema.py:90, 590-596` of Home Assistant 2026.10.0.
* Mosquitto serves MQTT over WebSockets with `listener <port>` and `protocol websockets`
  (mosquitto.conf(5), Mosquitto 2.0.18).
* Revisit when a front end needs queries that request/result events cannot serve with acceptable
  latency, or when Homie support is requested.
