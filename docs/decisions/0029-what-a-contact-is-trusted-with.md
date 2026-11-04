---
status: accepted
date: 2026-11-04
decision-makers: maintainers
---

# What a contact of the radio is trusted with

## Context and Problem Statement

The radio's contact table holds the household's companions and anyone else the household added:
a neighbour, a friend's handheld, a former member, a node added from Home Assistant's select. The
records decided each permission on its own: commands from an allowlist of full keys
([ADR 0009](0009-inbound-commands-from-allowlisted-contacts.md)), telemetry from every chat contact
([ADR 0018](0018-telemetry-pulled-from-the-households-companions.md)), a device in Home Assistant
for each contact ([ADR 0017](0017-contacts-on-the-radio-each-with-its-own-device.md)). None said
what being a contact means as such, so telemetry treated every contact as household, and every
Home Assistant user could act on every contact (security review 2026-11, ARC-002, SEC-008,
SEC-016). What is a contact trusted with, and what does the household grant per key?

## Decision Drivers

* Being on the radio's table is needed for a direct message both ways: it must not grant more.
* The household decides, per companion and at runtime, without editing the configuration.
* A grant names a full public key, never a name or a 6-byte prefix.
* Existing installations keep their behaviour until the maintainer decides the defaults of 2.0.0.

## Considered Options

* A contact is trusted as household: what the radio holds, the gateway serves.
* A contact is trusted with being heard and reached; everything else is granted per key.
* A list of household keys in the configuration.

## Decision Outcome

Chosen option: a contact is trusted with being heard and reached, because it is the least that a
direct message needs, and every further grant then has an owner and a place:

* **Every contact** may send the gateway direct messages, which are published (`received`, the
  history) as data, never acted on, and rate-limited per sender; it may be sent to over
  `contacts/<key>/send` by whoever the broker's ACL lets write that topic; it gets a device in
  Home Assistant ([ADR 0017](0017-contacts-on-the-radio-each-with-its-own-device.md)).
* **The household mark** grants telemetry: with `[telemetry] ask = "household"`, which the
  installer gives new installations, only the contacts the household included are asked, and the
  protected ones (contact targets and command senders); with `ask = "all"`, every chat contact
  the household did not exclude. The marks are kept in `telemetry-excluded.json` (`excluded`,
  `included`) and set from Home Assistant, MQTT or the command line (SEC-016).
* **The allowlist** grants commands: `[commands] senders`, full keys, with the checks of
  [ADR 0009](0009-inbound-commands-from-allowlisted-contacts.md) and, per sender, a code secret.
* **A target** grants a name for sending: `[targets.<alias>] contact`, which Home Assistant offers
  as a notify entity unless `[homeassistant] hidden_targets` hides it.
* **Home Assistant's users** get the controls (send, add, remove, telemetry) unless
  `[homeassistant] controls = false` (SEC-008); Home Assistant has no per-user rights for them.

### Consequences

* Good, because adding a contact no longer starts tracking it with `ask = "household"`, and a
  neighbour's handheld stays a contact the household can write to and nothing more.
* Good, because each grant is visible where it is made: the configuration (allowlist, targets,
  exposure) or the marks file, which a backup keeps.
* Bad, because a household on `ask = "all"` (the default of existing configurations) still asks
  every chat contact until it excludes the strangers or switches.
* Bad, because Home Assistant's users still share one set of controls: who may act on contacts is
  decided per gateway, not per user.

### Trust boundaries

B1 (the air): a contact's messages and telemetry answers are data, bound to it by its full key
where the radio names it, by its 6-byte prefix only where the firmware does (answers of a
look-alike are never taken). B3 (the broker): requests on the contact topics are trusted as far as
the ACL is. B5 (Home Assistant's users): the marks and the controls are theirs unless switched
off. See the [threat model](../threat-model.md).

### Confirmation

`tests/test_telemetry_poller.py` (who is asked under each `ask`), `tests/test_contact_manager.py`
(marks, also before a contact is on the radio), `tests/test_commands.py` (the allowlist and
codes), `tests/test_announcer.py` and `tests/test_manage_entities.py` (the exposure settings).

## Pros and Cons of the Options

### Trusted as household

* Good, because nothing needs to be marked.
* Bad, because adding a neighbour to write to them starts tracking their position (SEC-016).

### Heard and reached; the rest granted per key

* Good, because a contact costs the household nothing it did not choose.
* Bad, because the household marks its companions once (the installer asks).

### A list of household keys in the configuration

* Good, because one file holds every grant.
* Bad, because a mark would need an edit and a restart, and Home Assistant's switch could not set
  it.

## More Information

The privacy consequences are in [privacy.md](../privacy.md); the actors and boundaries in the
[threat model](../threat-model.md). Revisit if Home Assistant gains per-user permissions for
entities, or when the defaults of 2.0.0 are decided (`ask`, `publish_channels`, `heard_nodes`).
