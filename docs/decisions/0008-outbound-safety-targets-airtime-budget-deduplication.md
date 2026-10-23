---
status: accepted
date: 2026-10-18
decision-makers: maintainers
---

# Outbound safety: configured targets, an airtime budget and deduplication

## Context and Problem Statement

Any MQTT client that may publish to the send topics can make the radio transmit. A misbehaving
automation (a sensor that flaps, a loop) could flood a channel that many people share, exceed the
regulatory duty cycle, and drown the alerts that matter. How does the bridge limit what it sends?

## Decision Drivers

* Regulatory duty-cycle limits (EU 869.40–869.65 MHz: 10 % per device, ERC Recommendation 70-03).
* A shared channel: the bridge should use a small, predictable part of it.
* One alert per cause, not one per sensor flap.
* Simple to reason about and to test.

## Considered Options

* Free addressing plus a message-count rate limit
* Configured targets, a token bucket over estimated airtime, and duplicate suppression
* No limits in the bridge; leave it to the automations

## Decision Outcome

Chosen option: "configured targets, airtime token bucket, duplicate suppression", because airtime
is the quantity that regulation and channel load are about, and the three measures together bound
both the volume and the reach of what any client can trigger.

* **Targets:** only aliases from the configuration can be sent to (`send/<alias>`); contact targets
  are bound to a full public key. Broker ACLs can then restrict clients per alias.
* **Airtime budget:** each transmission's time on air is estimated from the radio's modulation
  settings and the packet size (SX126x datasheet formula, MeshCore packet format). A token bucket
  holds `burst_s` seconds and refills at `budget_s_per_hour`. Defaults: 10 s and 36 s per hour
  (1 % of the time), well inside the 10 % limit, leaving room for the radio's own traffic.
* **Duplicates:** the same final text to the same alias within `dedup_window_s` (default 300 s)
  is sent once; a duplicate does not extend the window, so a lasting condition is reported once per
  window.
* **Bounded queue and retries:** at most `queue_size` waiting messages, each with a time to live
  of `queue_ttl_s`; direct messages are retried at most `max_attempts` times. A packet that alone
  needs more airtime than `burst_s` is refused (`exceeds_airtime_burst`) instead of waiting
  forever; a request rejected because the queue is full does not count as sent for the
  duplicate check.

### Consequences

* Good, because the worst case is bounded: over any interval *T*, the bridge counts at most
  `burst_s + budget_s_per_hour × T / 3600` seconds per run of the service (each start begins
  with a full burst); a flooded attempt to a recipient more than 4 path bytes away takes somewhat
  more than counted (protocol.md §6).
* Good, because every refusal is reported on the delivery topic with a reason.
* Bad, because new destinations need a configuration change (on purpose).
* Bad, because the airtime estimate does not see the radio's own transmissions (acknowledgements,
  adverts); the default budget leaves margin for them, but it is not a duty-cycle meter.

### Confirmation

Unit tests check the time-on-air function against independent reference values, the bucket's
bound over a long greedy run, and duplicate suppression with an injected clock; component tests
check queue expiry and retries with an emulated radio.

## Pros and Cons of the Options

### Free addressing plus a message-count limit

* Good, because it is simple.
* Bad, because a count does not reflect airtime: a long direct message to a distant contact with
  retries costs many times a short channel message.
* Bad, because any client could address any contact or channel.

### No limits in the bridge

* Bad, because a single faulty automation can violate the duty cycle on behalf of the radio's owner.

## More Information

* Semtech SX1261/2 datasheet, section 6.1.4 "LoRa Time-on-Air".
* ERC Recommendation 70-03, Annex 1 (non-specific short range devices).
* Revisit if the firmware exposes its own airtime accounting over the companion protocol; the
  bridge could then use measured instead of estimated airtime.
