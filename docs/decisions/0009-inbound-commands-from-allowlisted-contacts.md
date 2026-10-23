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
   contact's **full** 32-byte key is in `commands.senders`. The contact must be in the radio's
   table as read **after** the message was fetched, and no other key with the same prefix may have
   been a contact of the radio in the last 24 hours (`sender_ambiguous`).
   The radio can only decrypt a direct message from a contact it holds, so a look-alike that sent
   the message was in the table when it arrived. It can leave the table before the bridge reads
   it: overwritten by a newer contact, or, in firmware 1.18, cleared by two adverts of its own (the
   first claims type 0), without any removal push ([`protocol.md`](../protocol.md#5-semantics)).
   But every advert, path update and overwrite is pushed with the contact's full key, and every
   table read lists the keys. The bridge therefore remembers each key that shares the prefix of an
   allowed sender and that the radio named as its contact, for 24 hours after it last saw it; such
   keys only add doubt, never a sender. A node the radio reported but did not add (`NEW_ADVERT`)
   cannot have sent a message the radio decrypted; it only renews a key already remembered.
   * A rejection by the table the bridge already holds stands, unless the sender was unknown:
     nothing the radio does can turn it into an acceptance, because keys that leave the table are
     remembered. Otherwise the bridge reads the table again, unless a read started after the
     message was fetched; one read serves a whole batch, and a read in progress is awaited
     instead of repeated.
   * A command fetched while the bridge connects, because it waited in the radio or arrived
     during the connection setup, is rejected (`sender_unverified`): a look-alike may have come
     and gone while nobody listened. The sender sends it again.
   * If the table cannot be read, also because the radio connection ended first, the command is
     rejected (`sender_unverified`).
4. The sender timestamp is **greater** than the last accepted one of this sender (`replay`). The
   bridge stores it durably before publishing the event; if that fails, the command is rejected
   (`state_unwritable`). It also stores the timestamp of a command it refuses because the broker
   is unreachable (`broker_unavailable`): that sender was verified.
5. Unless `max_age_s` is 0, the timestamp lies within `max_age_s` of the bridge clock
   (`too_old`, `from_the_future`).

Accepted commands are published on the `command` topic with the word as `event_type` and the rest
of the text as `args`. Every received message, including rejected command attempts and the reason,
appears on `received`. The bridge executes nothing.

### Consequences

* Good, because direct messages are authenticated by the firmware with the key shared between the
  sender and the radio, so with full-key binding only the holder of an allowlisted key can command.
* Good, because a 48-bit prefix collision, which can be produced with enough computing effort,
  does not inherit permissions, as long as the bridge is the radio's only client and sees its
  pushes (USB; for TCP see below).
* Bad, because a look-alike that the radio adds blocks the real sender's commands
  (`sender_ambiguous`) for 24 hours after the bridge last saw it. With manual adding and no
  contact type added automatically, it reaches the table only if someone adds it. The radio
  passes on direct messages from contacts of every type, so every type counts; the bridge reads
  the adding settings at connect and warns.
* Bad, because a command that could be accepted waits for a read of the contact table: about
  150 bytes per contact, 4.6 s for 350 contacts over a USB-UART bridge at 115200 baud, less over
  native USB. Ordinary messages and senders the cached table already rejects cost nothing, and one
  read serves every message fetched before it.
* Bad, because commands sent while the bridge was not connected (restart, update, unplugged
  radio) are rejected and must be sent again.
* Bad, because over TCP the radio drops push frames while its 4-frame send queue is full; a
  look-alike that enters and leaves the table between two reads can then go unseen. Commands
  should come in over USB; the bridge warns when commands are configured with TCP.
* Good, because replays of accepted commands fail, also across restarts.
* Bad, because a command rejected as `sender_unverified` or `state_unwritable` stores no
  timestamp (a look-alike's must not block the real sender, or the file cannot be written). A
  replayed copy of its packet is dropped as an exact repeat while it could pass the age check
  (an hour, or twice `max_age_s` and a minute); after a restart it is accepted if it is still
  within `max_age_s`, and with `max_age_s = 0` also after the hour.
* Bad, because senders need a correct clock (the phone apps set it; a handheld without time sync
  may be rejected until its clock is set). `max_age_s = 0` disables the window if needed.
* Bad, because the contact must be known to the radio with its full key (it has to have advertised
  itself or been added).

### Confirmation

Unit tests cover every rule and rejection, including a contact that only shares the prefix and
keys that are only remembered. Component tests with the emulated radio check that a look-alike
makes the command ambiguous when it was added without a push, overwritten, cleared silently after
its adverts, or seen only in an earlier table read, also across reconnects; that a sender known
only from memory is not accepted; that a command waiting at connect is rejected and its repetition
accepted, while an identical copy of its packet, or of one refused for an unreachable broker (also
after a restart), is not; that an unreadable contact table rejects it; that cached rejections cost
no read, one read serves a batch and a read in progress is reused; and that the timestamp is
persisted before the command event is published, with an unwritable state file rejecting the
command. The hardware checklist repeats the restart case.

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
* Recommended for a radio that takes commands: USB, contacts added manually, no contact type
  added automatically.
* Amended on 2026-10-19, before the first release, after two reviews: reading the table alone
  misses a look-alike that has left it again (overwritten, or cleared by its own adverts without
  a push), and a read per possible command is slow on large tables. Added the memory of keys with
  a sender's prefix, the order of checks and the rejection of commands waiting at connect.
* Amended on 2026-10-23, before the first release: a rejected command stays an exact repeat
  instead of being judged again (a retry after a lost acknowledgement and a replay look the same;
  the sender sends a rejected command anew), the repeat memory covers the age window, and
  `broker_unavailable` stores the timestamp; the remaining gap is listed under Consequences.
* Revisit if MeshCore adds signed commands or a challenge–response mechanism for companion clients.
