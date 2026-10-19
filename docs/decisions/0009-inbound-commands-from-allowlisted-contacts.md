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
look-alike key and a replay; it costs one read of the contact table per batch of messages that
could be accepted as commands.

Rules, all of which must hold:

1. The message is a plain-text **direct** message. Channel messages never command: anyone with the
   channel secret can write there, and the sender name in a channel text is plain text.
2. The first word, case-insensitive, is in `commands.words`.
3. The 6-byte sender prefix in the message frame resolves to **exactly one** contact, and that
   contact's **full** 32-byte key is in `commands.senders`. A prefix that matches several contacts
   is rejected (`sender_ambiguous`). The contacts are the radio's table as read **after** the
   message was fetched, plus every contact the radio reported as overwritten in the last 24 hours.
   The radio can only decrypt a direct message from a contact it holds, so a look-alike that sent
   the message was in the table when it arrived; until the bridge reads the table, the look-alike
   either stays there or, if the radio overwrites the oldest contact of a full table, is replaced,
   and the radio pushes its full key (`CONTACT_DELETED`) once to whichever client is connected.
   * A rejection by the table the bridge already holds stands: only a removal could turn it into
     an acceptance, and overwritten contacts still count. Otherwise the bridge reads the table
     again, unless a read started after the message was fetched; one read serves a whole batch.
   * A command that waited in the radio when the bridge connected is rejected
     (`sender_unverified`) if the radio adds contacts on its own and may overwrite them: an
     overwrite while no bridge listened was pushed to nobody. With other settings it is judged like
     any other command.
   * If the table cannot be read, the command is rejected (`sender_unverified`).
4. The sender timestamp is **greater** than the last accepted one of this sender (`replay`). The
   bridge stores it durably before publishing the event; if that fails, the command is rejected
   (`state_unwritable`).
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
  commands (`sender_ambiguous`) while it is in the table and for 24 hours after it was overwritten;
  manual adding without automatic contact types avoids this. The radio passes on direct messages
  from contacts of every type, so every type counts. The bridge reads the adding settings at
  connect and warns.
* Bad, because a command that could be accepted waits for a read of the contact table: about
  150 bytes per contact, 4.6 s for 350 contacts over a USB-UART bridge at 115200 baud, less over
  native USB. Ordinary messages and senders the cached table already rejects cost nothing, and one
  read serves every message fetched before it.
* Bad, because on a radio that may overwrite contacts, commands sent while the bridge was not
  connected (restart, update, unplugged radio) are rejected and must be sent again.
* Good, because replays of captured packets fail, also across restarts.
* Bad, because senders need a correct clock (the phone apps set it; a handheld without time sync
  may be rejected until its clock is set). `max_age_s = 0` disables the window if needed.
* Bad, because the contact must be known to the radio with its full key (it has to have advertised
  itself or been added).

### Confirmation

Unit tests cover every rule and rejection, including a contact that only shares the prefix.
Component tests with the emulated radio check that a look-alike added without a push makes the
command ambiguous, also after the radio overwrote it and across reconnects; that a command waiting
at connect is accepted with safe settings and rejected when the radio may overwrite contacts; that
an unreadable contact table rejects it; that cached rejections cost no read and one read serves a
batch; and that the timestamp is persisted before the command event is published, with an
unwritable state file rejecting the command. The hardware checklist repeats the restart case.

## Pros and Cons of the Options

### Any message with a command word

* Bad, because anyone on a public or hashtag channel could switch the house.

### Allowlisted 6-byte prefixes

* Good, because the prefix is what the message frame carries.
* Bad, because a look-alike key with the same prefix would be accepted if the radio adds it as a
  contact.

## More Information

* Message and contact frame layouts, contact-table behaviour of the firmware with source lines:
  [`docs/protocol.md`](../protocol.md).
* Recommended radio settings: add contacts manually, add no contact type automatically, do not
  overwrite the oldest contact.
* Amended on 2026-10-19, before the first release: a review showed that reading the table alone
  misses a look-alike the radio has overwritten in the meantime, and that a read per possible
  command is slow on large tables. Added the memory of overwritten contacts, the order of checks
  and the rule for commands waiting at connect.
* Revisit if MeshCore adds signed commands or a challenge–response mechanism for companion clients.
