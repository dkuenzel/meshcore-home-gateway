# Security policy

## Supported versions

Fixes go into the next version of the current line. While a minor version is in pre-release (for
example 1.1.0a4), a fix comes with its next pre-release; the latest release (for example 1.0.0)
gets a patch release (1.0.1) unless a newer release already has the fix. Older versions get no
fixes: update to the latest release. The changelog names the versions in scope with each release.

We aim to acknowledge a report within seven days, to agree on a date for the disclosure with the
reporter, and to publish a GitHub security advisory when the fix is released.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private vulnerability
reporting for this repository ("Security" tab, "Report a vulnerability"); only the maintainers can
read it. If the form is not available, open an issue that asks for a private contact, without any
details of the problem. Please allow time for a fix before disclosure.

Please include the version, the configuration that matters (without secrets) and the steps to
reproduce.

## Scope and threat model

The gateway sits between an untrusted radio channel, a local MQTT broker and a home automation
system. The threat model, the trust boundaries and the mitigations are described in
[`docs/design.md`](docs/design.md#7-security). In short:

- Commands from the radio are accepted only as direct messages from allowlisted contacts, never
  from channels, and are protected against replay and against look-alike keys that share an
  allowed sender's 6-byte prefix
  ([ADR 0009](docs/decisions/0009-inbound-commands-from-allowlisted-contacts.md)).
- The gateway never executes anything itself; it publishes events that the home automation
  evaluates.
- Its events and documents carry no origin: consumers trust them because the broker's ACL lets
  only the gateway write its topics, which the gateway cannot check
  ([securing the broker](docs/mqtt-api.md#securing-the-broker)). It undoes what another client
  writes on its retained documents, but cannot keep a forged event from being delivered.
- Outbound traffic is limited to configured targets and an airtime budget
  ([ADR 0008](docs/decisions/0008-outbound-safety-targets-airtime-budget-deduplication.md)).
- The MeshCore TCP companion interface has no authentication in either direction: any client
  controls the radio, and a host that takes the radio's network address can pose as the radio and
  forge commands. It can also lose push frames. Use USB, above all for commands, or else an
  isolated network that only the gateway and the radio can reach.
- Runtime dependencies come from the Linux distribution only; development tools are hash-pinned.
