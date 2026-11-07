---
status: accepted
date: 2026-11-03
decision-makers: maintainers
---

# The outgoing queue kept across restarts, alerts first

## Context and Problem Statement

The design listed "persisting the outgoing queue across restarts" as a non-goal: a stop or a
restart dropped the queued messages without a final `delivery` event, also the message whose
acknowledgement was awaited, since the unit gave the stop 20 s and an attempt waits up to 60 s.
Every update and every reconfiguration restarts the service, so "the update dropped the alarm"
is a plausible incident for an alert gateway (security and architecture review, ARC-013,
OPS-006, CODE-025, IMP-014). Messages also left strictly in arrival order: an alert waited
behind status texts, and behind every attempt of a message to an unreachable contact, up to
four times a minute (IMP-032, design §12). And the message budget started full at every start,
while the telemetry lanes keep their debt, so a crash loop could spend a burst per restart
(ARC-005, IMP-031). How does the queue survive a stop, and how does an alert get ahead?

## Decision Drivers

* A message that was accepted (`queued`) gets a final event, also across an update.
* No message arrives later than its time to live allows (`queue_ttl_s`).
* A retry stays recognisable to the recipient: the same timestamp, the next attempt number.
* The radio's airtime limits hold across restarts, without delaying the first alert after one.
* An alert is not held up by status messages.
* The installer's answers carry every setting ([ADR 0006](0006-deployment-in-an-unprivileged-proxmox-container.md)).

## Considered Options

* Keep the queue in the state file at the stop, and drain the acknowledgement in flight.
* Drain the whole queue at the stop, keep nothing.
* Make `--update` wait for an empty queue before it restarts (IMP-035).
* A priority per request (a JSON envelope on `send/<alias>`) instead of per target.

## Decision Outcome

Chosen option: keep the queue in the state file, because only it holds across every kind of
stop (an update, a reconfiguration, a reboot, a stop by hand) and needs neither a longer outage
nor the radio:

* **At the stop** no attempt starts any more; one that waits for its acknowledgement gets it
  for at most 60 s (`TimeoutStopSec` is 90 s). Then every message still queued, that one too
  if it needs another attempt, goes to `state.json` (`outbox`): its `id`, target, text, how long
  it had waited, its radio timestamp and the attempts sent. Requests are taken until then.
* **At the start** those with time left are queued again, in their order, and published
  `queued` again with their `id`; the others end as they would have (`expired` with
  `queue_ttl`, or `failed` with `no_acknowledgement` once one was sent; `failed` with
  `unknown_target` when the configuration no longer has the target). A message whose attempts
  began continues with the next attempt and the same timestamp. The kept texts count for the
  duplicate check, so a client that sends one again after the restart gets `duplicate`. The
  section is removed at once, so a crash after the start does not bring them back twice.
* **Alerts first.** `[messages] alert_targets` names the targets whose messages overtake the
  others; between two attempts of another message, an alert that waits goes first, and that
  message continues after it. A request chooses by its target: a second target for the same
  recipient, listed or not.
* **The budget's level** is written at once after every transmission and at the stop, and a
  start takes it back, refilled for the time stopped, but at least what a long direct message
  needs at the radio's settings, so that the first alert after a restart goes out at once
  ([ADR 0008](0008-outbound-safety-targets-airtime-budget-deduplication.md), amended).

### Consequences

* Good, because an update or a reconfiguration loses no accepted message, and every one ends
  with exactly one final event while the gateway is stopped and started.
* Good, because an alert overtakes status messages and is not held up by the retries of one.
* Good, because a crash loop no longer spends a burst of airtime per restart.
* Bad, because the texts waiting at a stop are on the disk until the next start, as the history
  holds them anyway ([privacy.md](../privacy.md)).
* Bad, because a stop can take up to a minute longer, while an acknowledgement is awaited.
* Bad, because a crash (not a stop) still drops the queue: it is written at the stop only.
* Neutral: the order between alerts and other messages is no longer strictly the arrival order.

### Confirmation

`tests/test_bridge.py` (the queue kept across a stop, its expiry at the start, the
acknowledgement awaited in the stop, the next attempt after it, alerts first, the budget's
level), `tests/test_outbox.py` (the order), `tests/test_state.py` (the records).

## Pros and Cons of the Options

### Keep the queue in the state file

* Good, because it holds for every stop, also one the installer makes.
* Bad, because it writes message texts to the disk.

### Drain the whole queue at the stop

* Good, because nothing is written.
* Bad, because a queue waiting for a radio that is away, or for airtime, cannot drain at all,
  and the stop would take up to `queue_ttl_s`.

### `--update` waits for an empty queue

* Good, because it is a few lines in the installer.
* Bad, because it covers only the update, not a reconfiguration, a reboot or a stop by hand.

### A priority per request

* Good, because one target serves both kinds of message.
* Bad, because the payload of `send/<alias>` is the text: a JSON envelope would make a text
  that looks like JSON mean something else, and Home Assistant's notify entities send the text
  only. A second target gives a request the choice without it.

## More Information

* The design's non-goal and future-work item are gone ([design.md §3, §6.2](../design.md));
  the delivery events in [mqtt-api.md](../mqtt-api.md#delivery-delivery).
* Revisit when crashes become a concern for the queue: the queue would then be written with the
  state file's tick, at the cost of a write per tick while messages wait.
* Amended on 2026-11-03 (security and architecture review, CODE-005): the tests that the
  confirmation names in `tests/test_bridge.py` are in `tests/test_outbox.py`, after the
  queue's own, since the bridge's tests follow its parts.
* Amended on 2026-11-07 (review of 2.0.0, #92): the queue was written once, after the wait for
  the acknowledgement and before the last received messages were handled. A stop cut short in
  the wait (a kill, a unit of 1.x with `TimeoutStopSec=20`) lost the queue, and a request or the
  answer to a command that came after the write was lost. The queue is now written at the stop's
  start, with the message in flight and its attempts, again at each change in the stop, and a
  last time as the stop's last step, after those messages and with the requests still waiting
  to be taken. A request after that is rejected with reason `stopping`. A crash still drops the
  queue.
* Amended on 2026-11-07 (review of 2.0.0, #93): an alert went only between two attempts of
  another message. One taken out of the queue that waited for the radio or for airtime before
  its first attempt held the alert up until it had used the airtime and its acknowledgement had
  come. Such a wait now ends when an alert comes, and the message goes back to the queue, first
  among its kind; so does one that waits for airtime before its next attempt.
