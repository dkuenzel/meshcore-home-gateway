---
status: accepted
date: 2026-10-18
decision-makers: maintainers
---

# Inbound commands only from allowlisted contacts, with replay protection

## Context and Problem Statement

People in the field should be able to trigger automations by radio ("status", "arm"). Anything the
bridge turns into a command event can switch things in a home. Which received messages become
commands, and how does the bridge make sure the sender is who it appears to be?

## Decision Drivers

* Authenticity: a command must come from a known person's device.
* Freshness: a recorded packet must not work twice.
* Least privilege: a small vocabulary, no free-form execution.
* Visibility: rejected attempts must be observable.

## Considered Options

* Any message whose text starts with a command word
* Direct messages from allowlisted 6-byte key prefixes
* Direct messages from allowlisted full public keys, with unique prefix resolution, a per-sender
  monotonic timestamp and a clock window

## Decision Outcome

Chosen option: "direct messages from allowlisted full public keys, with unique prefix resolution,
monotonic timestamps and a clock window", because it is the only option that resists both a
look-alike key and a replay, at negligible cost.

Rules, all of which must hold:

1. The message is a plain-text **direct** message. Channel messages never command: anyone with the
   channel secret can write there, and the sender name in a channel text is plain text.
2. The first word, case-insensitive, is in `commands.words`.
3. The 6-byte sender prefix in the message frame resolves to **exactly one** contact on the radio,
   and that contact's **full** 32-byte key is in `commands.senders`. A prefix that matches several
   contacts is rejected (`sender_ambiguous`). The bridge reads the radio's contact table again for
   every message that may be a command, after the message arrived: a look-alike contact that the
   radio added a moment ago, with or without telling the bridge, is then part of the table. The
   radio can only decrypt a direct message from a contact it knows, so a look-alike that sent the
   message is always in the table at that point. If the table cannot be read, the command is
   rejected (`sender_unverified`).
4. The sender timestamp is **greater** than the last accepted one of this sender (`replay`). The
   bridge stores it durably before publishing the event.
5. Unless `max_age_s` is 0, the timestamp lies within `max_age_s` of the bridge clock
   (`too_old`, `from_the_future`).

Accepted commands are published on the `command` topic with the word as `event_type` and the rest
of the text as `args`. Every received message, including rejected command attempts and the reason,
appears on `received`. The bridge executes nothing.

### Consequences

* Good, because direct messages are authenticated by the firmware with the key shared between the
  sender and the radio, so with full-key binding only the holder of an allowlisted key can command.
* Good, because a 48-bit prefix collision, which can be produced with enough computing effort,
  does not inherit permissions.
* Bad, because a look-alike contact that the radio adds automatically blocks the real sender's
  commands (`sender_ambiguous`) until it is removed; manual adding avoids this.
* Bad, because every possible command costs one extra read of the contact table (about a second
  for a full table over USB); ordinary messages do not.
* Good, because replays of captured packets fail, also across restarts.
* Bad, because senders need a correct clock (the phone apps set it; a handheld without time sync
  may be rejected until its clock is set). `max_age_s = 0` disables the window if needed.
* Bad, because the contact must be known to the radio with its full key (it has to have advertised
  itself or been added).

### Confirmation

Unit tests cover every rule and rejection, including a contact that only shares the prefix.
Component tests with the emulated radio check that a look-alike added without a push makes the
command ambiguous, that an unreadable contact table rejects it, and that the timestamp is persisted
before the command event is published.

## Pros and Cons of the Options

### Any message with a command word

* Bad, because anyone on a public or hashtag channel could switch the house.

### Allowlisted 6-byte prefixes

* Good, because the prefix is what the message frame carries.
* Bad, because a look-alike key with the same prefix would be accepted if the radio adds it as a
  contact.

## More Information

* Message and contact frame layouts: [`docs/protocol.md`](../protocol.md).
* Recommended radio setting: add contacts manually, so unknown nodes do not enter the contact table.
* Revisit if MeshCore adds signed commands or a challenge–response mechanism for companion clients.
