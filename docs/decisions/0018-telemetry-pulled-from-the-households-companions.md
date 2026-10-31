---
status: accepted
date: 2026-10-23
decision-makers: maintainers
---

# Telemetry and positions pulled from the household's companions, in budget lanes

## Context and Problem Statement

The household's handhelds carry the battery, sensor readings and, with GPS, the position of the
people who carry them. The observer ([ADR 0014](0014-the-gateway-as-the-households-mesh-observer.md))
should know them and keep positions current while a GPS is on, and the handhelds must give them to
the observer **only**. MeshCore companions do not push telemetry: a client requests it, and the
companion answers or stays silent. Every request transmits. How does the gateway get the data,
and how does it keep the cost to the mesh bounded?

## Decision Drivers

* Privacy: positions reach the observer and nobody else; the observer itself reveals nothing
  ([ADR 0020](0020-managed-radio-profile-for-the-observer.md)).
* Integrity: a position may drive presence or alarm automations; it must come from the contact it
  claims to come from.
* Airtime: the mesh is shared; floods cost every repeater in range
  ([meshcore-ha issue 214](https://github.com/meshcore-dev/meshcore-ha/issues/214)).
* Messages first: telemetry must never delay or starve alerts
  ([ADR 0008](0008-outbound-safety-targets-airtime-budget-deduplication.md)).
* No configuration per contact: detect who answers.

## Considered Options

* `SEND_TELEMETRY_REQ` (39) to each contact, scheduled, direct paths only, in its own airtime lanes
* `SEND_BINARY_REQ` (50) with a telemetry request type
* Positions from adverts only (no requests)
* No telemetry

## Decision Outcome

Chosen option: "`SEND_TELEMETRY_REQ` (39), scheduled, direct only, in its own lanes", because its
answer (`TELEMETRY_RESPONSE`, push 0x8B) names the 6-byte key prefix of the contact whose key
decrypted it, while the answer to command 50 (push 0x8C) carries only a tag
(`MyMesh.cpp:739-760` at companion-v1.18.0).

* **Who is polled**: every chat contact on the radio, without configuration. A contact that never
  answers is asked less and less often (back-off up to 6 h, reset by any sign of life: an advert, a
  message, a path update). A contact can be excluded from Home Assistant or the command line.
* **One request in flight** for the whole gateway: the firmware keeps a single pending request and
  every new one replaces it (`MyMesh.h:192-194`); it never times out a request itself. The gateway
  waits `max(5 s, 1.25 × est_timeout)` from `SENT`, accepts a 0x8B only when its prefix matches the
  contact asked, and drops unmatched 0x8C answers.
* **Direct only**: the radio floods when it knows no path to the contact. Polls are sent only with a
  known path; without one, a single repair flood is allowed when the contact was heard recently
  (at most 4 per hour for the gateway, 1 per contact per 30 min). A flooded request teaches both
  sides a route for the next polls.
* **Lanes**, separate from the message budget: telemetry may use 18 s per hour of the gateway's
  own transmit time (0.5 %) and an estimated 90 s per hour of mesh airtime (2.5 %, all hops both
  ways). A poll waits while a message waits for its acknowledgement.
* **Interval** from a channel share per companion: 1 % of channel time while it moves, 0.1 % while
  it rests (for a direct neighbour about every 90 s moving and every 15 min resting; longer with
  more hops). Moving means a displacement of 50 m or more since the last fix; two polls under
  50 m mean resting again. Without a position in the replies, once an hour.
* **Payload** is CayenneLPP (library 1.6.1, as the firmware pins it): decoded by a pure, total
  decoder that stops at channel 0 (the AES padding), keeps repeated (channel, type) pairs apart,
  treats voltage and current as signed, and drops impossible or (0, 0) positions. Positions are
  published centred in their 0.0001° cell with an accuracy of at least 15 m.
* **Denial is silence**: the companion answers only if the requester holds its *base* permission
  and is in its contact table. The gateway cannot tell "denied" from "unreachable" and reports
  "no answer" with both causes.
* **Household setup** (documented with the MeshCore app's own labels): on each companion,
  telemetry requests, location and environment "From/For Specific Contacts", and on its contact
  entry for the gateway "Allow Telemetry Requests?", location and environment "Yes" (flags 0x0E,
  0x0F with favourite), remote CLI **No**; GPS mode enabled; "Share Position in Advert" off.
* **Advert check**: a household contact whose advert carries its position is reported (event and
  problem entity): everyone in range can read it. The contact record's position is sticky, so the
  check reads the advert itself (the radio's packet log, or its stored copy of the advert).

### Consequences

* Good, because positions and batteries of the household arrive without anyone sharing them with
  the mesh, and without configuration in the gateway.
* Good, because the cost is bounded: three companions at home cost about 4.5 s/h of the gateway's
  transmit time and 11 s/h of mesh airtime *(calculated, EU/UK Narrow)*; five moving companions
  two hops away are throttled to the lanes.
* Bad, because a reply cannot show when the GPS fix was taken, and some tracker boards send their
  last or stored position even with GPS off; the gateway shows when it received a position and
  when it last changed, never a fix time.
* Bad, because the household must set the permissions on each companion; the gateway cannot read
  or set them remotely, and checks only what it observes.
* Neutral, because command 39 is marked "can deprecate" in the firmware; a codec test pins the
  layout, and the hardware checklist covers it.

### Confirmation

Pure tests for the decoder (negative coordinates, padding, duplicates, truncation, unknown types,
(0, 0)) and the scheduler (one in flight, timeouts, late answers, lanes, repair floods, movement
hysteresis, no burst after a restart); emulator tests with a remote companion that answers fully,
partly or not at all; airtime estimates against the formulas of protocol.md; hardware checklist
items for routes, permissions, GPS states and padding.

## Pros and Cons of the Options

### Command 39 with lanes

* Good, because the answer is bound to the contact.
* Bad, because the firmware may deprecate it in favour of command 50.

### Command 50

* Good, because it can ask for less (an inverse permission mask) and is the firmware's future path.
* Bad, because its answer carries no sender identity: any contact that guesses the clock-based tag
  could inject a position *(inferred)*.

### Positions from adverts only

* Good, because it costs no requests.
* Bad, because it requires the household to broadcast their positions to everyone, which is what
  the user rules out; companions also advert only when someone presses a button.

### No telemetry

* Bad, because the observer would not know where its people are or how their batteries are.

## More Information

* Amended on 2026-10-31 by [ADR 0025](0025-positions-in-adverts-checked-from-the-packet-log.md):
  until the gateway has checked a companion's newest advert, the radio's record decides, erring to
  warn; the check reads the packet log, not the stored copy.
* Frames, routes, permissions, payload and airtime with firmware citations: research for the
  design sprint (2026-10-23), summarised in [protocol.md](../protocol.md) with the implementation.
* Revisit when MeshCore offers pushed or subscribed telemetry, when command 39 is deprecated, or
  when measured airtime (`GET_STATS`) shows the estimates are off.
