# Security policy

## Supported versions

The project is in its 0.x phase. Only the latest release receives fixes.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private vulnerability
reporting for this repository ("Security" tab, "Report a vulnerability"). If that is not available,
contact the maintainers privately and allow time for a fix before disclosure.

Please include the version, the configuration that matters (without secrets) and the steps to
reproduce.

## Scope and threat model

The bridge sits between an untrusted radio channel, a local MQTT broker and a home automation
system. The threat model, the trust boundaries and the mitigations are described in
[`docs/design.md`](docs/design.md#7-security). In short:

- Commands from the radio are accepted only as direct messages from allowlisted contacts, never
  from channels, and are protected against replay
  ([ADR 0009](docs/decisions/0009-inbound-commands-from-allowlisted-contacts.md)).
- The bridge never executes anything itself; it publishes events that the home automation
  evaluates.
- Outbound traffic is limited to configured targets and an airtime budget
  ([ADR 0008](docs/decisions/0008-outbound-safety-targets-airtime-budget-deduplication.md)).
- The MeshCore TCP companion interface has no authentication; use USB or an isolated network.
- Runtime dependencies come from the Linux distribution only; development tools are hash-pinned.
