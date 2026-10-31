---
status: accepted
date: 2026-10-31
decision-makers: maintainers
---

# Positions in adverts: checked from the packet log; the radio's record decides until then

## Context and Problem Statement

An advert reaches everyone in range, so a household companion should not send its position in
it. [ADR 0018](0018-telemetry-pulled-from-the-households-companions.md) asks the gateway to warn
when one does, and to read the advert itself, because the radio's record of a contact keeps the
position of its last advert that carried one, also after the companion stopped sharing it. But
the radio's report of an accepted advert (`ADVERT`) carries the key only, no content. The radio's
stored copy of the advert (`EXPORT_CONTACT`, command 17) is read into a 177-byte frame buffer
from up to 255 bytes on ESP32 radios, and a copy longer than a frame is never answered. And a
companion adverts only when someone presses *Advert* on it or sends one from its app: weeks can
pass between two adverts. How does the gateway learn what a companion's newest advert carries,
and what does it say before it knows?

## Decision Drivers

* Privacy: the household is warned when a companion's adverts carry its position; a warning
  missed is worse than one too many.
* The gateway sends nothing for it and never risks the radio: no command whose answer can
  overrun the radio's buffer or never come.
* A logged advert is unverified (the radio logs every packet before it checks the signature): a
  forged copy must not decide.
* No old position comes back on the map once a companion stopped sharing it.
* The public interfaces only grow ([ADR 0023](0023-public-interfaces-kept-by-a-snapshot.md)).

## Considered Options

* The packet log, confirmed by the radio's push and the next table read
* The radio's stored copy (`EXPORT_CONTACT`) after each push
* The packet log first, the stored copy where the log does not settle it
* Before a companion's newest advert is checked: `null` (no warning) instead of the radio's
  record

## Decision Outcome

Chosen option: "the packet log, confirmed by the radio's push and the next table read", with the
radio's record deciding until the newest advert is checked, because it is the only one that
sends nothing to the radio and does not turn the warning off for a companion that shares now.

* **The check.** The radio logs every received packet before it handles it, and the gateway
  parses the logged adverts. A logged advert counts once the radio pushed `ADVERT` for its key and
  the next table read shows the logged timestamp as the contact's `last_advert`. While the
  radio's receive delay is 0 (its default, read at each connect), the log that is the frame
  directly before the push decides: the radio logs and handles a packet in one pass, so a forged
  copy is never there. Otherwise every log of the key with that timestamp within 40 s before the
  push must agree. A flagged log must hold the position the record took. A new position in the
  record since the read before, with a newer `last_advert`, is evidence on its own. A table read
  that shows the record from before the pushed advert waits for the next one. A chat node
  the radio did not add is checked from its `NEW_ADVERT`, whose record the radio made from that
  advert alone; the gateway keeps the time and flag with the heard node and the table read that
  adds it takes them. The flag with 0, 0 (a companion that never had a position) counts as
  sharing.
* **Until the newest advert is checked, the radio's record decides, erring to warn.**
  `advert_carries_position` is the newest advert's flag once it is checked; before the first
  check, whether the record holds a position from an earlier advert; after a newer advert the
  gateway could not check (a dropped report over TCP, a log too long for a frame, an advert while
  the gateway was stopped), the last check's flag, or `true` when the record holds a position.
  `advert_checked_at` tells the cases apart. No event comes for the record's value.
* **A position from adverts follows the check.** It is shown only while the record holds one and
  the contact's last checked advert, if any, carried one. A check never drops a telemetry
  position; a changed advert position replaces it, as before, also at the check that shows the
  companion shares again. An advert that could not be checked changes nothing.
* The last check of each companion is kept in the state file, without a position. A logged
  position is held only to compare it, for at most about two minutes, and never stored,
  published or logged.

The documents keep growing only: `advert_position` keeps its meaning (the record holds a
position), and `advert_carries_position`, `advert_checked_at`, the index keys, the event
`position_in_advert` and *Position in adverts* are added
([mqtt-api.md](../mqtt-api.md#contacts-contacts-and-contactskey)).

### Consequences

* Good, because the gateway sends no command for the check; the radio's buffer and the session
  stay as they were.
* Good, because no warning that 1.0.0's `advert_position` gave turns off at the update: the
  values at the update equal it for the companions, so nothing flips and no event comes. After an
  advert the gateway could not check, the record errs to warn.
* Good, because a companion that stopped sharing leaves the map once a checked advert shows it,
  instead of keeping its last shared place for ever.
* Bad, because a companion that stopped sharing warns again after an advert the gateway could not
  check, until its next checked advert.
* Bad, because a companion whose record holds no position reads `false` until its next advert,
  also if it shares: one that never had a position (0, 0), one the gateway added before 1.1.0a3
  or after a restart between hearing and adding it, and one added from a key or a link before the
  gateway heard it.
* Bad, because a position in an advert can be one the companion saved long ago: the advert does
  not say when the fix was taken.
* Neutral, because a forged copy without the flag can still decide when the real advert's log was
  too long for a frame (non-default repeaters), or when another client raised the receive delay
  during a session: a wrong "no position" until the next advert.

### Confirmation

`tests/test_adverts.py` (the rules of the check, with forged and late copies, waits and the heard
check), `tests/test_mesh.py` (the position from adverts follows the check), `tests/test_views.py`
(the record's value while the newest advert is unchecked), the observer's component and emulator
tests in `tests/test_observer.py` and `tests/test_bridge.py` (the emulator never receives command
17), the Home Assistant contract tests `home-assistant/tests/test_adverts.py`; hardware steps
41–44 ([testing.md](../testing.md#hardware-checklist)).

## Pros and Cons of the Options

### The packet log, confirmed by the push and the table read

* Good, because the parser exists, every push already triggers a table read, and nothing is sent.
* Bad, because adverts whose report or log is lost are not checked: visible as
  `advert_checked_at` older than `last_advert_at` and as a debug line "not checked".

### The stored copy after each push

* Good, because it reads the advert the radio stored, also after the gateway was stopped.
* Bad, because a copy longer than a frame is never answered (the session would end on its
  timeout), an ESP32 or RP2040 radio reads it into its frame buffer past its end, and on nRF52 the
  copy can be older than the record.

### The packet log first, the stored copy where it does not settle

* Good, because it closes the gaps of the log.
* Bad, because it keeps every risk of the stored copy, and its guards need a new session method,
  a worker and writes of the state file per uncertain export.

### `null` until checked

* Good, because it never states what the gateway did not read.
* Bad, because without the stored copy a check can take weeks: the gateway's sensor would turn
  off at the update for a companion that shares right now.

## More Information

This record amends [ADR 0018](0018-telemetry-pulled-from-the-households-companions.md) (its
*Advert check*: the check reads the advert in the packet log, and the radio's record decides
until then) and relates to [ADR 0024](0024-telemetry-values-in-home-assistant-expire.md), whose
time rule for positions from adverts stays. The rules:
[design §6.6](../design.md#66-observer-documents-and-history); the firmware facts: [protocol.md](../protocol.md#4-push-frames) and
[platform-notes.md](../platform-notes.md#meshcore-companion-firmware). Revisit when hardware step
44 shows many adverts unchecked over TCP or behind a busy repeater (then the stored copy, once the
radio reads it safely), or when the firmware sends a report with the advert's content.
