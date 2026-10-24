---
status: accepted
date: 2026-10-23
decision-makers: maintainers
---

# Contacts live on the radio; each contact is a recipient with its own device

## Context and Problem Statement

A household adds a new handheld to the mesh. Today the gateway can only send to targets configured
by root in `config.toml` ([ADR 0008](0008-outbound-safety-targets-airtime-budget-deduplication.md)),
and contacts can only be managed with the phone app, which the dedicated radio cannot serve while
the gateway uses it. The handheld should appear in Home Assistant (or another front end) with its
own entities, to send to it, see its messages, its telemetry and its position, as soon as it is a
contact, and disappear when it is removed, without editing configuration. Who holds the contact
list, who may change it, and what follows from being a contact?

## Decision Drivers

* As little configuration as possible: detect and set up automatically.
* The radio decrypts and sends only for contacts in its own table: that table is what counts.
* Commands stay as safe as [ADR 0009](0009-inbound-commands-from-allowlisted-contacts.md) makes them:
  being a contact must grant no rights.
* Airtime limits of ADR 0008 keep holding for every recipient.
* Every front end sees the same contacts ([ADR 0015](0015-provider-neutral-documents-on-the-bus.md)).

## Considered Options

* The radio's table is the source of truth; every contact is a recipient with its own device.
* A contact list in `config.toml`, synchronised to the radio; only configured contacts get devices.
* Contacts in the radio, but devices and sending only for contacts selected in Home Assistant.

## Decision Outcome

Chosen option: "the radio's table is the source of truth; every contact is a recipient with its
own device", because it needs no configuration at all, matches what the radio can decrypt and send,
and keeps privileges where they are: in `config.toml`.

* **Membership** is the radio's contact table. The gateway adds contacts on request (a node it
  heard, a contact link or QR code, the command line); the managed profile keeps automatic adding
  off ([ADR 0020](0020-managed-radio-profile-for-the-observer.md)), so the table changes only
  through the gateway or a deliberate import. Contacts added by other means (a phone app on the
  radio before the gateway took over) are taken over as they are.
* **Every contact is a recipient**: `contacts/<key>/send` sends a direct message to it, through the
  same queue, airtime budget, duplicate suppression and retries as `send/<alias>`. This supersedes
  ADR 0008's rule "only aliases from the configuration can be sent to" for contacts; channels stay
  configuration-only, because a channel reaches everyone with its secret. Broker ACLs can still
  restrict who may publish to `contacts/+/send`.
* **Every contact gets its own device** in Home Assistant, linked to the gateway's device, with its
  entities: send, received messages, last heard, signal, and, as soon as the data exists, position
  and telemetry readings and the requests that apply to it. The device appears when the contact
  appears in the table and is removed when the contact leaves it. Configured targets keep their
  entities on the gateway device as before.
* **Being a contact grants nothing**: command senders stay a configured allowlist of full keys
  (ADR 0009). The gateway never sets the remote-CLI flag (0x10) on a contact and reports a contact
  that has it (`probe`, and a problem entity).
* **Protected contacts** are the keys of configured contact targets and command senders: they
  cannot be removed through the gateway, and the managed profile makes sure they exist on the
  radio as favourites (a replaced or reset radio heals itself).
* **No look-alikes by the gateway's hand**: adding a node whose 6-byte key prefix equals that of a
  contact or a protected key is refused, because messages name their sender by that prefix only.
* **Who may manage**: adding and removing contacts is on by default in Home Assistant and on the
  command line, because a contact grants no rights; an operator can switch it off in
  `config.toml`.
* **Firmware gate** (added with the implementation): contacts are written only on firmware the
  gateway is tested with, like the managed profile's settings ([ADR 0020](0020-managed-radio-profile-for-the-observer.md)):
  unknown firmware must not be written blindly. `radio.profile.manage = "always"` allows any.
* **One write, one result**: a request is written at most once (the radio rewrites its contacts
  file for every change and overwrites every field of an existing key), on the table as the radio
  has it at that moment, and ends in one `contact` event (none only for refusals beyond the
  rate limit and for requests still queued at a stop); a node heard advertising is
  added with the record the radio checked, a link or card only as a claim until the node's first
  advert ([mqtt-api.md](../mqtt-api.md#managing-contacts-add_contact-and-contactskeyremove)).

### Consequences

* Good, because a new handheld is a few clicks: it advertises, appears as a heard node, is added,
  and its device with all entities exists at once; removal cleans up the same way.
* Good, because sending to a person no longer needs root and a configuration change.
* Bad, because any client allowed to publish to `contacts/+/send` can message every contact; the
  budget bounds the airtime, and the broker ACL bounds who.
* Bad, because a large table means many devices; a radio with 350 contacts gives 350 devices. The
  household case is a handful; the documentation explains how to hide or disable devices, and the
  number of devices can be limited (for example to chat contacts and favourites) if needed.
* Neutral, because entity IDs are created from the contact's name when the device first appears
  and stay stable afterwards, like any Home Assistant entity; renaming the contact on the radio
  renames the device, not the entity IDs.

### Confirmation

Tests: a contact added to the emulated radio yields its device document; a removed one yields the
removal; sending to a contact goes through the budget and deduplication; a protected contact is
not removable; a look-alike is refused, of a contact and of a protected key not on the radio, and
the managed profile does not add a protected key next to one; the codec cannot set the remote-CLI
flag or a telemetry flag, and a contact that has one is reported. Contract
tests in Home Assistant create and remove a contact device with all its entities.

## Pros and Cons of the Options

### The radio's table, every contact a recipient with a device

* Good, because zero configuration and one truth.
* Bad, because the gateway must follow table changes it did not make (pushes, re-reads), which it
  already does for ADR 0009.

### A contact list in `config.toml`

* Good, because every change is reviewed by root.
* Bad, because every new handheld needs root, an edit and a restart, which the requirement rules
  out; and the list drifts from the radio's table.

### Devices only for contacts selected in Home Assistant

* Good, because fewer devices for large tables.
* Bad, because it is a manual step for the common case and a selection that lives only in one front
  end, against ADR 0015.

## More Information

* Contact table semantics, flags and pushes: [protocol.md](../protocol.md), sections 3–5.
* Revisit if tables with hundreds of contacts become common among users (device limits), or if
  MeshCore adds per-contact permissions that make some contacts privileged.
