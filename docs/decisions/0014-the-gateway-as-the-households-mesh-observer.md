---
status: accepted
date: 2026-10-23
decision-makers: maintainers
---

# The gateway as the household's mesh observer: positions, telemetry, contacts and dashboards

## Context and Problem Statement

The gateway started as a message bridge: send to configured targets, publish what arrives, report
deliveries. Its non-goals excluded telemetry requests, maps and repeater administration
([design §3](../design.md#3-goals-and-non-goals)), and
[ADR 0002](0002-standalone-bridge-instead-of-home-assistant-integration.md) left "maps, repeater
administration, telemetry dashboards" to other projects. Users want more from the radio that is
always on at home: where the household's handhelds are, their batteries, which nodes are around,
contact management without plugging the radio into a PC, and a ready dashboard. The radio already
delivers most of this data and the gateway threw it away. Which of it is in scope, and under which
limits?

## Decision Drivers

* The gateway's radio is dedicated and always on: it is the household's natural **observer**, the
  one place where "all threads come together".
* Privacy: positions of the household's people must reach the observer and nobody else.
* Airtime: the mesh is shared; anything the gateway sends counts against the EU 10 % duty cycle
  and the network's capacity.
* Safety rules of [ADR 0008](0008-outbound-safety-targets-airtime-budget-deduplication.md) and
  [ADR 0009](0009-inbound-commands-from-allowlisted-contacts.md) must keep holding.
* As little manual configuration as possible: detect and set up automatically.

## Considered Options

* Stay a message bridge (status quo).
* Observer: passive data, contact management, telemetry of the household's own contacts, shipped
  dashboards; requests to other nodes (repeaters, sensors of others) later.
* Full node administration now, including repeaters and remote sensors.

## Decision Outcome

Chosen option: "observer", because it brings most of the value at little or no extra airtime and
keeps every new transmission bounded and attributable.

In scope from now on:

1. **Passive harvest** (no airtime): positions in contact records and adverts, nodes heard but not
   added, the radio's own statistics and measured time on air, contact flags, the radio's settings.
2. **Contact management** from Home Assistant and the command line: add heard nodes, remove,
   import; contacts get their own entities ([ADR 0017](0017-contacts-on-the-radio-each-with-its-own-device.md)).
3. **Telemetry pull from the household's companions**: the gateway requests battery, sensor and
   GPS readings from its own contacts, within an airtime budget; the companions grant this to the
   gateway's contact only ([ADR 0018](0018-telemetry-pulled-from-the-households-companions.md)).
4. **The observer reveals nothing**: the gateway's radio denies all telemetry requests and sends
   adverts without a position (managed radio profile, [ADR 0020](0020-managed-radio-profile-for-the-observer.md)).
5. **Views for every front end**: provider-neutral documents on the bus
   ([ADR 0015](0015-provider-neutral-documents-on-the-bus.md)) with a local history
   ([ADR 0016](0016-history-in-a-local-sqlite-database.md)); Home Assistant first, a terminal and
   a web dashboard later.
6. **Shipped dashboards** and translations ([ADR 0019](0019-internationalisation-with-gettext.md),
   [ADR 0021](0021-generated-dashboards-for-desktop-and-mobile.md)).

Still out of scope: repeater and room-server administration, requests to nodes that are not the
household's contacts, firmware updates, changing the radio's frequency or modulation.

This record supersedes the scope statements of ADR 0002 ("features of the existing integration
(maps, repeater administration, telemetry dashboards) are out of scope") for maps and telemetry
dashboards, and the non-goals "telemetry requests, maps" of the design document. Repeater
administration stays out of scope; ADR 0002's revisit condition for it is unchanged.

### Consequences

* Good, because the always-on radio becomes useful beyond messages: presence, batteries, a map,
  the radio's real duty cycle.
* Good, because passive data costs no airtime and is available at once.
* Bad, because the gateway grows: more protocol to follow, more state, more entities. Each part
  gets its own record, tests against the firmware layouts and contract tests against Home
  Assistant.
* Bad, because telemetry pulls transmit; the budget and the privacy model of ADR 0018 bound that.

### Confirmation

Each in-scope part is implemented under its own record; the design document lists the scope; the
architecture test keeps the core pure ([ADR 0007](0007-hexagonal-architecture-with-an-asyncio-core.md)).
A codec test fails for every encoder of a command on the deny list (frequency and modulation,
key export and import, factory reset, raw packets, the radio's command line).

## Pros and Cons of the Options

### Stay a message bridge

* Good, because the code stays small.
* Bad, because the data the radio delivers is lost, and users need a second tool (phone app, a
  custom integration) for everything else, which the dedicated radio cannot serve at the same time
  (design §6.1, "single client").

### Full node administration now

* Good, because one tool would do everything.
* Bad, because requests to repeaters and foreign sensors cost airtime of the whole network and
  need logins and their own security model. What aggressive polling does to a mesh is reported in
  [meshcore-ha issue 214](https://github.com/meshcore-dev/meshcore-ha/issues/214) ("Monitoring
  repeater telemetry – how often is too much?", fetched 2026-10-23): about half of the observed
  traffic were requests and replies, mostly flooded.

## More Information

* The data the radio delivers is documented in [protocol.md](../protocol.md), sections 3 and 4.
* Revisit when the household's own repeaters or sensors should be administered, or when MeshCore
  adds a push-based telemetry subscription that makes polling unnecessary.
