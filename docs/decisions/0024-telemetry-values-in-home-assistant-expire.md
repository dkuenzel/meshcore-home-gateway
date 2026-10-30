---
status: accepted
date: 2026-10-30
decision-makers: maintainers
---

# Telemetry values in Home Assistant expire when the companion stops answering

## Context and Problem Statement

The gateway asks the household's companions for their battery, their sensors and their position
([ADR 0018](0018-telemetry-pulled-from-the-households-companions.md)). In 1.1.0a1 a position
from telemetry stayed in Home Assistant as last received, also when nobody confirmed it any more.
A `person` reads only its device trackers, so a handheld that left range, ran flat or was
switched off kept its owner `home` for as long as the gateway ran: the presence trap. With the
readings, a frost warning could act on a temperature that is hours old. How does Home Assistant
learn that a value is no longer current?

## Decision Drivers

* Presence: a `person` must stop trusting a position nobody confirmed, without an automation of
  the household's own.
* No flapping: one lost answer, a request that waits for its airtime, or a restart of the gateway
  must not turn a present companion away.
* Every front end reads the same documents
  ([ADR 0015](0015-provider-neutral-documents-on-the-bus.md)): the rule belongs to the gateway.
* Few recorder rows: an expiry must not cost a state per answer.

## Considered Options

* Never expire: show the last values, and the time they came
* The tracker and the telemetry sensors turn `unavailable` while the answers are not current
* A freshness entity per contact (a timestamp or a state sensor), and templates that compare it
* Home Assistant's `expire_after`

## Decision Outcome

Chosen option: "the tracker and the telemetry sensors turn `unavailable` while the answers are not
current", because it is the only one a `person` follows without help: it skips a tracker that is
`unavailable`.

* **Current answers**: at most one request in a row went unanswered, and the last answer is at
  most `max(1 h, 3 × its interval)` old. Two failures catch a departure within about 16 minutes at
  rest next to the gateway; the time bound covers requests that are not sent (telemetry paused,
  the radio away, no path) and waits in the airtime lanes without flapping.
* **Current position**: the answers are current, and the newest answer, or one within the last
  hour, carried a position, so a handheld indoors without a fix stays present for an hour.
* **In the documents**: `contacts/<key>` carries `telemetry_state` (why the values are current or
  not), `telemetry_current` and `position_current`; the values stay in the document. Home
  Assistant's availability of the tracker reads `position_current`, that of *Battery*, *MCU
  temperature* and each reading sensor `telemetry_current`. The switch *Telemetry* and *Telemetry
  state* stay available. A position from adverts does not expire.
* **Events only for requests sent**: `unanswered` on the `telemetry` topic when a companion that
  answered within the day leaves its second request in a row unanswered, once per absence;
  `answered` when it answers after that, or for the first time. An expiry by the time bound alone
  (the radio away, telemetry paused, no path, the gateway stopped) raises none.
* The scheduler computes the state; it is saved with how long ago each answer came, so a restart
  of the gateway expires nothing.

### Consequences

* Good, because a `person` whose handheld left follows its other GPS trackers, or turns `unknown`
  with none, and its zone no longer counts it; the map drops the marker.
* Good, because the state names the cause (`away`, `no_answer`, `no_path`, `no_position`, …), and
  the events let an automation warn about a handheld that stopped answering.
* Bad, because presence triggers react differently to an expiry, which ends as `unknown`, not as
  another zone. For a `person` whose only GPS tracker is the mesh tracker:

  | Trigger | On expiry (`home` → `unknown`) | On return (`unknown` → `home`) |
  | --- | --- | --- |
  | legacy `zone` trigger, `event: leave` / `enter` | `leave` fires | `enter` fires |
  | state trigger on the person, `from: home` | fires | – |
  | `zone.left` / `zone.entered` | never | never |
  | `zone.occupancy_cleared` / `occupancy_detected` | fires if it was the last one in the zone | fires |

  "Never" includes a real departure. Every restart of the gateway makes every gateway entity
  `unavailable` for its seconds, trackers included; the guide recommends a state trigger with
  `for:` and the condition *Telemetry status* `on`
  ([home-assistant.md](../home-assistant.md#positions-and-presence)).
* Bad, because an outage of the gateway's radio longer than an hour expires every companion. The
  status says `paused` with the reason `no_radio` at once, and no event comes, so a guarded
  automation does not take it for everyone leaving.
* Bad, because each expiry and each return writes a recorder row per entity.
* Neutral, because coming back takes up to `max(30 min, its interval)`, the back-off of a
  companion that answered within the day.

### Confirmation

The Home Assistant contract tests (`home-assistant/tests/test_telemetry.py`):
`test_answers_that_are_not_current_make_the_sensors_and_tracker_unavailable`,
`test_a_person_ignores_an_expired_mesh_tracker` and the tests of the documented examples
(`test_the_left_home_example_skips_a_paused_telemetry_and_a_restart`,
`test_the_not_answering_example_posts_one_notification_per_event`); the scheduler's tests of the
state on a set clock (`tests/test_telemetry.py`); hardware steps 31, 37 and 38
([testing.md](../testing.md#hardware-checklist)).

## Pros and Cons of the Options

### Never expire

* Good, because nothing in Home Assistant changes.
* Bad, because the presence trap stays: a `person` reads only the tracker, never a time next to
  it.

### Unavailable while not current

* Good, because a `person`, the map and every automation see it without help.
* Bad, because `unavailable` hides the last value in Home Assistant's interface (the document
  keeps it), and the triggers above behave as the table says.

### A freshness entity and templates

* Good, because the trackers keep their last position.
* Bad, because a `person` ignores it: every household would have to build its own template
  tracker, and a timestamp entity writes a recorder row per answer.

### `expire_after`

* Good, because Home Assistant has it built in.
* Bad, because the MQTT device tracker does not offer it, and it counts from the last message:
  every republish of a document (a reconnect, a restart of the gateway) makes old values current
  again, and one bound for all contacts ignores their intervals.

## More Information

This record adds to [ADR 0018](0018-telemetry-pulled-from-the-households-companions.md), which
stays as accepted, and supersedes nothing. The documents: [mqtt-api.md](../mqtt-api.md#telemetry-states);
the guide: [home-assistant.md](../home-assistant.md#telemetry-of-the-households-companions).
Revisit when the hardware checklist or a week of use shows the time bound too short or too long
in a busy mesh, or when Home Assistant changes how a `person`, a zone or availability work.
