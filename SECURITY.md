# Security policy

## Supported versions

Fixes go into the next version of the current line. While a minor version is in pre-release (for
example 2.1.0a4), a fix comes with its next pre-release; the latest release (for example 2.0.0)
gets a patch release (2.0.1) unless a newer release already has the fix. Older versions get no
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
system. The threat model, with its assets, actors and trust boundaries, is
[`docs/threat-model.md`](docs/threat-model.md); the mitigations and the residual risks are in
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

What GitHub enforces for this repository, as set on 2026-11-03 and 2026-11-05 (SUP-007). The
rulesets are exported to [`.github/rulesets/`](.github/rulesets/); the settings themselves are
the authority (`gh api repos/dkuenzel/meshcore-home-gateway/rulesets` lists them).

- `main` (ruleset `main`): changes arrive by pull request and squash merge only, once the CI
  jobs passed; no force push, no deletion. No review is required: a second identity for the
  agent, whose approval would mean something, would make every change wait for the
  maintainer, who accepted the shared identity instead (SUP-001, 2026-11-05). The ruleset's
  bypass actor is a repository role that the maintainer's login has: the maintainer's login,
  and the agent through it, can bypass it at any time, and push to `main` directly, also by
  force (`current_user_can_bypass: always` in the API).
- A release is tagged a day after the date of the commit that wrote its version
  (`release-due.yml`): time to stop a merged change before it is signed and published. A squash
  merge dates that commit when it reaches `main`; a commit pushed past the ruleset carries the
  date its author gave it. It ships the program of a release candidate tagged before;
  pre-releases, candidates too, are tagged once CI passed on them and reach only installations
  that name them.
- `release.yml` publishes a release only where `release-due.yml` would tag it, also on a tag
  pushed by hand: on the commit that wrote the version `main` has, a day after that commit's
  date, with CI passed on it on `main`, and while `release-due.yml` is enabled, so that disabling
  it stops a release. A pre-release needs only a commit of `main` that carries its version: one
  tagged by hand is published also where CI has not passed. This check is part of the tagged
  commit: a tag on a commit off `main` brings that commit's `release.yml`, which can leave the
  check out. Such a tag is kept from being made only by who may create tags (below).
- Release tags `v*` (ruleset `v-star`): cannot be moved or deleted
  ([ADR 0022](docs/decisions/0022-releases-tagged-by-ci-on-main.md)). Its bypass actor is the
  same role: the maintainer's login, and the agent through it, can bypass it at any time, and
  move or delete a tag until its release is published, which then keeps it. Anyone with write
  access can create a tag: the maintainer's login and the workflows of this repository.
- Releases are immutable once published. Each carries Sigstore-signed build provenance (SLSA Build
  Level 2), its files are built twice, on x86-64 and on arm64 runners, and compared, and the
  published installer pins the program it installs ([install](docs/deployment.md#install)).
- Private vulnerability reporting, Dependabot alerts and security updates, secret scanning with
  push protection, and code scanning (CodeQL's default setup, for Python and the workflows) are
  on.
- [`.github/CODEOWNERS`](.github/CODEOWNERS) names the security-relevant paths; the pull request
  template's review checklist asks to read their diff.
- Most changes are written by an AI coding agent that works through the maintainer's GitHub login
  on the maintainer's computer; its commits name it as their author, and it merges them once the
  required jobs passed ([CONTRIBUTING.md](CONTRIBUTING.md#changes-written-by-an-agent)). The login's
  protection (two-factor authentication, the scope of its token) is the account's and not visible
  here.
