---
status: accepted
date: 2026-11-11
decision-makers: maintainers
---

# Heard nodes are kept with name, signal and time by default

## Context and Problem Statement

The nodes the radio hears but did not add are other people's radios. The security review of
2026-11 found that the gateway keeps and publishes what it hears of them, without their knowledge:
their keys, the names their owners chose, when and how often each was heard, and the signal it
came with (SEC-015). Since 2.0.0 `[contacts] heard_nodes` chooses how much: `full` all of it,
`keys` the key and the type, `off` nothing but the keys of look-alikes and of nodes whose name is
taken. With the other defaults of 2.0.0 the default became `keys`
([ADR 0029](0029-what-a-contact-is-trusted-with.md), its amendment of 2026-11-05).

A name is not secret: every advert carries it in the clear, with the key, to every radio in range
([protocol.md](../protocol.md#4-push-frames)), so `keys` never made a stranger unknown to anyone
who listens. What is not public is the gateway's own record of when and how strongly a radio was
heard at the house. With `keys` the household sees digits where its neighbours' names are, and
nothing of the traffic around the house. After using 2.1 the maintainer found that time and
strength are the valuable part: they show whether a node is heard often and how far away it is.
Which level should a configuration that does not set `heard_nodes` get?

## Decision Drivers

* The household sees who is around the house, how often and how near, without a setting.
* What the gateway keeps of strangers stays bounded (the last 50 published, 7 days kept), never a
  position, and a household that wants less sets `keys` or `off`.
* A configuration keeps meaning what it meant: `keys`, `off` and `full` keep their meanings, and a
  configuration that sets one keeps it ([compatibility](../development.md#compatibility)).
* A node named like a member of the household is flagged also after a restart (SEC-007).

## Considered Options

* `full`: name, type, when and how each was heard, and its signal
* A new level `names`: name and type, no times and no signal
* `keys`, the default of 2.0 and 2.1

## Decision Outcome

Chosen option: `full`, because it is the only one that shows the household how often and how
near a node is heard, which is what the heard nodes are for; a name alone is what every radio in
range hears anyway.

* A configuration without `heard_nodes` gets `full` from 2.2 on. The levels keep their meanings;
  one that sets `heard_nodes` keeps its value, and the installer writes none, so a new
  installation follows the default.
* The gateway publishes and keeps of a heard node its key, type and name, when it was first and
  last heard and how, its hops, SNR and RSSI: in `nodes` (retained, the last 50), the `node`
  events, the history (7 days after the last time heard) and in Home Assistant the attributes of
  the *New node* event and the options of the select *Add heard node*. Never its position.
* What `keys` and `off` keep, and the look-alikes' and taken names' keys that every level keeps,
  stay as they were ([privacy.md](../privacy.md)).

### Consequences

* Good, because the household sees its neighbours' radios by name, and when and how strongly each
  was heard, without a setting; *Add heard node* offers them by name.
* Good, because the history keeps the name, so a node named like a contact or the gateway is
  flagged (`name_taken`) also after a restart, and an add after a restart takes its name. With
  `keys` both wait for the node's next advert.
* Bad, because strangers' names, signal and times are kept and published by default: on the
  broker in `nodes` and the `node` events, in the history, and unless the administrator leaves
  them out in Home Assistant's recorder, which keeps the *New node* event's attributes
  ([privacy.md](../privacy.md#other-people)). Home Assistant also keeps the last *New node*
  event and the last options of *Add heard node*, up to 20 names, in
  `.storage/core.restore_state` and its backups, which no recorder exclusion reaches: only
  `keys` or `off` keeps the names out of it.
* Bad, because a household that chose nothing now keeps more of the people around it than with
  2.0 and 2.1; the changelog says how to keep less.

### Trust boundaries

None moves and no entry point is added. B1 the air: a heard node's name and type are what its
advert claims, its signal comes from the radio's packet log, which is not authenticated; neither
grants anything, a name is compared only to flag a node that may pass for another (SEC-007). B3
the broker: `nodes` and the `node` events carry the names and signal to every client the ACL lets
read them. B4 Home Assistant and B5 its users: the *New node* event and *Add heard node* show
them to every user, the recorder keeps the events, and `.storage/core.restore_state` the last
event and the last options of *Add heard node*, also in Home Assistant's backups. B6 the
container: the history holds them for 7 days after the last time heard, its backups for as long
as they are kept. See the [threat model](../threat-model.md).

### Confirmation

`tests/test_config.py` (the default and the levels), `tests/test_observer.py` (what the default
publishes and keeps, a taken name flagged after a restart, a node kept under `keys` named again by
its next advert), `tests/test_contact_manager.py` (an add after a restart takes the name with the
default and the key's digits with `keys`), `tests/test_manage_entities.py` (the options name the
nodes), `tests/test_installer.py` (a new installation follows the default), the snapshot of the
public interfaces (the changed default), the contract test of the dashboard's *Settings* for each
level (`home-assistant/tests/test_gateway.py`), and the contract test that Home Assistant keeps
the options of *Add heard node* as restore data (`home-assistant/tests/test_manage_contacts.py`).

## Pros and Cons of the Options

### `full`

* Good, because it shows how often and how near a node is heard, which the maintainer uses.
* Good, because it needs no new level: the meanings of 2.0 stay.
* Bad, because it keeps and publishes the most of strangers by default.

### A new level `names`

* Good, because names are public, and the times and signal, the record of the house, would stay
  with `full`.
* Good, because the history would keep the names, so the restart effects of `keys` would go.
* Bad, because it leaves out what the household wants from the heard nodes: when and how strong.
* Bad, because a fourth level is one more meaning to document, test and keep within the major
  version.

### `keys`

* Good, because it keeps the least of strangers that still lets the household add them.
* Bad, because names are no secret, so it hides from the household only what everyone in range
  hears, and the times and signal with them.
* Bad, because after a restart a heard node has no name until its next advert: a node named like a
  contact is not flagged until then, and an add before it writes the first 12 digits of its key
  as the contact's name.

## More Information

This record supersedes the `heard_nodes` part of the amendment of 2026-11-05 to
[ADR 0029](0029-what-a-contact-is-trusted-with.md); `ask = "household"` and
`publish_channels = "targets"` stay the defaults. What is kept, where and for how long:
[privacy.md](../privacy.md); the setting: [configuration.md](../configuration.md#contacts); the
documents: [mqtt-api.md](../mqtt-api.md#nodes-heard-nodes). Revisit if the heard nodes come to
carry more than a name, a type and a signal (a position, a path), or if what the gateway keeps
of strangers leaves the household by another way than the broker, Home Assistant and backups.
