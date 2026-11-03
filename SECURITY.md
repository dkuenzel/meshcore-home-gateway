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
  controls the radio, the release firmware hands it the radio's private key, which decrypts every
  direct message of the radio (there is no forward secrecy), and a host that takes the radio's
  network address can pose as the radio and forge commands. It can also lose push frames. Use USB,
  above all for commands, or else a network that only the gateway and the radio can reach; the
  configuration says so with `[radio] tcp_unauthenticated = true`
  ([configuration.md](docs/configuration.md#radio)).
- Runtime dependencies come from the Linux distribution only; the tools that build, test and
  audit the project are pinned by hash, at least 14 days old, and looked up in the Python
  Packaging Advisory Database by CI.

## How the repository and the releases are protected

What GitHub enforces for this repository, as set on 2026-11-03 (SUP-007). The tag ruleset is
exported to [`.github/rulesets/`](.github/rulesets/); the settings themselves are the authority
(`gh api repos/dkuenzel/meshcore-home-gateway/rulesets` lists them).

- `main` has no branch protection: changes arrive by pull request and squash merge, after CI, by
  practice. Whether a ruleset enforces that, and a review by a second identity, is the
  maintainer's to decide (SUP-001).
- Release tags `v*` (ruleset `v-star`): cannot be moved or deleted
  ([ADR 0022](docs/decisions/0022-releases-tagged-by-ci-on-main.md)).
- Releases are immutable once published. Each carries Sigstore-signed build provenance (SLSA Build
  Level 2), its files are built twice, on x86-64 and on arm64 runners, and compared, and the
  published installer pins the program it installs ([install](docs/deployment.md#install)).
- Private vulnerability reporting, Dependabot alerts and security updates, secret scanning with
  push protection, and code scanning (CodeQL's default setup, for Python and the workflows) are
  on.
- [`.github/CODEOWNERS`](.github/CODEOWNERS) names the security-relevant paths; the pull request
  template's review checklist asks to read their diff.
- Most changes are written by an AI coding agent that works through the maintainer's GitHub login
  on the maintainer's computer; its commits name it as their author, and the maintainer reviews
  and merges them ([CONTRIBUTING.md](CONTRIBUTING.md#changes-written-by-an-agent)). The login's
  protection (two-factor authentication, the scope of its token) is the account's and not visible
  here.
