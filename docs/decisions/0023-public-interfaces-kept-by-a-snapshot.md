---
status: accepted
date: 2026-10-25
decision-makers: maintainers
---

# Four public interfaces, kept within a major version by a policy and a snapshot of the code

## Context and Problem Statement

From 1.0.0 on the project follows Semantic Versioning. The README, `CONTRIBUTING.md`, the
changelog and `docs/development.md` named three public interfaces (the MQTT API, the
configuration format and the command line), but nothing said what "compatible" means for each of
them. Home Assistant discovery was not named, although automations, dashboards and statistics
refer to its entity IDs. Nothing checked a change either: the release plan
([ADR 0022](0022-releases-tagged-by-ci-on-main.md)) takes the kind of change from the changelog,
which the author of the change writes. A removed topic, a narrowed range or a renamed option
would reach a minor release unnoticed. What does a major version promise, and how is that kept
without a radio, a broker or someone who remembers every interface?

## Decision Drivers

* Within a major version, what users built keeps working: automations, broker ACLs,
  configuration files, scripts and dashboards.
* A break shows in the pull request that makes it, not only at the release.
* Growth needs nothing beyond its documentation; false alarms are rare, so that nobody learns to
  wave the check through.
* 1.0.0 ships the program of 1.0.0rc1: the check must not change `src/`, `deploy/`,
  `pyproject.toml` or `tools/build_pyz.py`.
* The release tooling stays standard library only, run under `python3 -I`.

## Considered Options

* A written policy, and a snapshot of the interfaces computed from the code and compared with the
  snapshot of the last release on every change
* A written policy alone, kept by review
* Hand-written schemas of the interfaces (JSON Schema for the documents, lists of settings and
  options) as the reference, tested against the code
* The documentation's tables and examples as the reference, tested against the code

## Decision Outcome

Chosen option: "a written policy and a snapshot computed from the code", because it is the only
one that notices a break nobody declared without a second description of the interfaces that
could drift from the code.

* **Four public interfaces** ([docs/development.md](../development.md#compatibility)): the MQTT
  API, the configuration format, the command line and, newly, Home Assistant discovery with a
  narrow promise: unique IDs, default entity IDs, platforms, units, event types, options and
  attribute keys. Names (translated, [ADR 0019](0019-internationalisation-with-gettext.md)),
  icons, entity categories and which entities are enabled by default are not covered; device and
  state classes are left to review for now.
* **Within a major version each interface only grows.** The policy lists, for each one, what is
  compatible and what is breaking. Not interface: abbreviated options (a new option can make one
  ambiguous), texts for people, log messages, the Python modules and the state directory.
  Raising a platform floor is breaking: Python 3.11 with paho-mqtt 2.x (Debian 13 for the
  installer), Home Assistant 2026.8, MeshCore companion firmware 1.18.0. The Proxmox
  installer's options and its answers file only grow, like the configuration; the snapshot
  records the answer type of each setting, review keeps the rest.
* **The snapshot** `tests/public_interface.json` is computed by `tests/public_interface.py`
  without a radio or a broker: the MQTT adapter with a recording paho client below the bridge's
  publish paths, a recorder on the configuration parser, a walk of the argument parser, the
  commands' own output with the radio emulator for `probe`, and Home Assistant's discovery
  builders. It records structure only: topics with retained flag and QoS, subscriptions and the
  last will, key paths, value lists declared in one place or emitted where Home Assistant is told
  of them, settings with type, default, bounds and the choices the parser takes, commands,
  options and named positionals, columns and JSON keys of the machine-readable outputs, and the
  discovery facts above. `tests/test_public_interface.py` fails when snapshot and code differ;
  `make interface` writes it again and lists what changed.
* **The comparison** `tools/public_interface.py` classifies each difference as breaking, changed
  (compatible, but users notice it: a default, a wider range) or added. Attributes of a record are
  compared by a rule each, and an attribute without a rule, or a value of another shape, is an
  error, so recording a new fact is a deliberate change of the tool. A rename is a removal plus
  an addition. Each finding states one fact: a value removed from a list is a finding of its own.
* **The release plan** compares the snapshot at HEAD with that of the last release, on every pull
  request and push. A breaking finding needs a `**Breaking:**` entry or a `### Removed` section
  since that release, which makes the next release a major one. A release must contain the
  snapshot, so 1.0.0 becomes the first baseline. A false alarm is waived with a reason in
  `tests/public_interface_waivers.json`, for one release, by the finding as the refusal shows
  it; the waiver covers no other finding.

### Consequences

* Good, because a removal, a narrowed range, a renamed option or a changed entity ID can no
  longer reach a minor or patch release unnoticed.
* Good, because the snapshot's diff shows every change of a public interface in review, also the
  additions that need documentation.
* Good, because growth needs only `make interface`, and the check needs no tool besides git.
* Bad, because the extractor uses private parts of the code (argparse internals, steps of the
  bridge, private constants); a change there fails the test loudly and needs an edit of the
  extractor.
* Bad, because the snapshot does not see all that the policy promises; JSON types and
  nullability, the size limits and list bounds of MQTT, the value lists outside `mqtt > values`
  (the reasons among them), string formats, publishing after a reconnect, exit statuses,
  platform floors, meanings, units of values, rules between settings and the texts a request
  accepts stay with review and the written policy
  ([docs/development.md](../development.md#compatibility) lists them).
* Bad, because surface behind a setting that the fixture does not switch on is missing until the
  fixture gets it; a test that every topic of the layout is recorded closes that gap for MQTT.
* Neutral, because most feature pull requests touch the one snapshot file; a conflict is resolved
  by `make interface` after merging `main`.

### Confirmation

`tests/test_public_interface.py` checks the snapshot against the code, the topic coverage and the
comparator on small synthetic snapshots; `tests/test_release_plan.py` checks the comparison with
the last release in temporary repositories, also under `python3 -I`. The release of 1.0.0 must
contain the snapshot; the first change after it is compared with it.

## Pros and Cons of the Options

### A written policy and a snapshot computed from the code (chosen)

* Good, because the code is the only reference, so the check cannot drift from it.
* Good, because the structure covers most of what users depend on: names, flags, and the types
  and bounds of settings and options.
* Bad, because behaviour beyond the structure is not seen.

### A written policy alone

* Good, because no code to maintain.
* Bad, because the changelog entry that decides the version is written by the author of the
  change; a break that nobody notices is released as a minor version.

### Hand-written schemas as the reference

* Good, because schemas would document types and nullability precisely.
* Bad, because they are a second description beside the code, kept in step by hand; the check
  would compare schemas with each other, not what the code does.

### The documentation as the reference

* Good, because it is what users read.
* Bad, because tables and examples are prose: a check depends on their layout, and some facts
  (QoS, the last will, the columns of the lists) are not there in a form a test can read. The
  documentation's examples match the code today; checking them stays a separate test.

## More Information

Follows [ADR 0022](0022-releases-tagged-by-ci-on-main.md), whose consequences announced a check
of the public interfaces. Related: [ADR 0015](0015-provider-neutral-documents-on-the-bus.md)
(documents only grow). Revisit when the reason vocabularies become enums (they then join the
snapshot as additions), when device and state classes are decided, or when a release line besides
`main` is maintained.
