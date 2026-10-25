# Development

How to work on meshcore-home-gateway. The rules (design first, test first, evidence for protocol
facts) are in [`CONTRIBUTING.md`](../CONTRIBUTING.md).

## Set up

```sh
make venv    # .venv with the hash-pinned tools from requirements/dev.txt and the package (editable)
make hooks   # pre-push hook: make check on the commit to push (refuses uncommitted changes)
make matrix  # tests with each Python from 3.11 to 3.14, each in its own .venv-python3.X
make help    # all targets
```

Requirements: Linux with systemd (for `make unit-check`), Python ≥ 3.11 with `venv`
(`python3-venv` on Debian and Ubuntu), GNU make, `mosquitto` for the integration tests, `uv` only
to update the lock file.

## Layout

```text
src/meshcore_home_gateway/
  core/          pure rules and calculations (no I/O): text, airtime, budget, contacts, commands, model
  protocol/      companion protocol: framing, codec (pure)
  radio/         transports (serial, TCP) and the companion session
  mqtt/          topics, payloads, Home Assistant discovery, paho adapter
  bridge.py      application service: outbox, inbox, status, supervision
  config.py      configuration loading, validation, rendering
  state.py       persistent state (command ledger)
  systemd.py     sd_notify
  cli.py         command line
deploy/          systemd unit, Proxmox installer, example configuration
docs/            design, decisions, protocol, API, operations
tests/           pytest suites, emulated radio, broker fixture; tests/manual: checks run by hand
tools/           build and release helpers: zipapp, release plan, public interface comparison
.github/         CI workflow, Dependabot, issue forms, pull request template
```

## Test-driven workflow

1. Write or extend a test that describes the behaviour and fails.
2. Implement until it passes; keep core modules free of I/O.
3. Refactor; `make lint test` stays green.
4. Update the documentation of the interface you touched (`mqtt-api.md`, `configuration.md`,
   `home-assistant.md`, `protocol.md`) and `CHANGELOG.md`; after a change of a public interface,
   `make interface` ([Compatibility](#compatibility)).

### Adding a protocol message

1. Find the command or response in the firmware (`examples/companion_radio/MyMesh.cpp` at the
   release named in [`protocol.md`](protocol.md)) and add its layout to `protocol.md` with the
   source lines.
2. Add a golden frame to `tests/test_codec.py`, derived from the source, not from our encoder.
3. Implement the encoder or decoder in `protocol/codec.py`.
4. Teach the emulated radio in `tests/radio_emulator.py` to answer it, then use it in the session.

### Adding a configuration setting

`config.py` (dataclass and validation), its answer type in `config._ANSWER_TYPES` (every setting
needs one: `--reconfigure` writes the configuration back from its answers, and
`tests/test_config.py` checks that no setting is missing), `docs/configuration.md`,
`deploy/config.example.toml`, a test in `tests/test_config.py`, and `make interface`. If the
installer should ask for it, also the dialog in `deploy/proxmox/meshcore-home-gateway-lxc.sh`.

## Tool chain updates

`make lock` re-resolves `requirements/dev.txt` from `requirements/dev.in` with hashes and takes
the newest release of every tool that is at least `COOLDOWN_DAYS` (14) old. `make ha-lock` does the
same for the Home Assistant test environment (`home-assistant/requirements.in`, one pinned
`pytest-homeassistant-custom-component`, which pins Home Assistant); after a Home Assistant update,
run `make ha-test` and adjust "tested with" in `docs/home-assistant.md`. Review the diff like code. GitHub Actions are
pinned to commit SHAs; Dependabot proposes updates with the same cooldown. `paho-mqtt` stays at the
version of Debian 13 (ADR 0005), and Dependabot ignores it: raise it with the deployment target.
A changed lock file gives `make venv` a fresh environment, so that no package the lock dropped
stays installed.

## Compatibility

From 1.0.0 on four interfaces are public: the MQTT API, the configuration format, the command
line and Home Assistant discovery
([ADR 0023](decisions/0023-public-interfaces-kept-by-a-snapshot.md)). Within a major version
each one only grows: what works with X.0.0 works the same with every X.Y.Z. An incompatible
change waits for the next major version, and its changelog entry starts with `**Breaking:**` and
says what users must change. Pre-releases promise nothing beyond the last release.

**MQTT API** ([mqtt-api.md](mqtt-api.md))

- Compatible: new topics; new keys in documents and events; new values in any list of values
  (`event_type`, `reason`, `state`, `status`, `type`, `flags`, …); new requests and new optional
  keys in a request; larger size limits and list bounds.
- Breaking:
  - a topic removed or renamed, or its retained flag or QoS changed;
  - a key removed or renamed, or its JSON type, unit or meaning changed;
  - a value removed or given another meaning;
  - a document no longer published again after a reconnect, or no longer cleared when its
    contact goes;
  - a request form that is no longer accepted;
  - smaller limits or bounds.
- Consumers ignore topics, keys and values they do not know.

**Configuration** ([configuration.md](configuration.md)). A file that loads with X.0.0 loads
with every X.Y.Z and means the same.

- Compatible: new optional settings and tables; wider ranges, formats and choices; a number
  where an integer was required; a changed default (listed under *Changed*).
- Breaking:
  - a setting or table removed or renamed (unknown keys are errors, so the service would not
    start);
  - a narrower type, range, format or choice;
  - a setting that becomes required;
  - a new rule between settings;
  - another meaning or unit for a value;
  - a changed default of `mqtt.base_topic`, `mqtt.client_id`, `homeassistant.prefix` or
    `homeassistant.status_topic`, because brokers, ACLs and Home Assistant refer to these names.
- A setting to be retired is deprecated first (*Deprecated*). It still loads, with a warning in
  the log, until the next major version.
- A file written for a newer version need not load with an older one.
- The Proxmox installer's options and its answers file
  ([unattended installation](deployment.md#unattended-installation)) only grow, like the
  configuration. Review keeps them; the snapshot below does not record them.

**Command line** (`meshcore-home-gateway --help`)

- Compatible: new commands, options and choices; new keys in JSON output; new line kinds in
  `probe --list profile`.
- Breaking:
  - a command, option or choice removed or renamed;
  - an option that switches between a flag and taking a value;
  - a new required option or argument, or arguments in another order;
  - another number of columns in a tab-separated list (`probe --list`, `contacts list`): scripts
    read them with `read`;
  - a key removed from JSON output;
  - another exit status for a documented case;
  - another `--version` output;
  - other default paths.
- Not interface: abbreviated options (write them in full, since a new option can make an
  abbreviation ambiguous); text meant for people (reports, summaries, messages on standard
  error), which may change and will be translated.

**Home Assistant discovery** ([home-assistant.md](home-assistant.md)). What automations,
dashboards and statistics refer to:

- Compatible: new entities, event types, options and attribute keys.
- Breaking: an entity removed (the gateway deletes it in Home Assistant); its unique ID, default
  entity ID, platform or unit changed; an event type, option or attribute key removed.
- Not interface: names (they will be translated), icons, entity categories, and which entities
  are enabled by default.
- Not recorded yet, so left to review: device and state classes (an entity without its state
  class keeps no long-term statistics).

**Platforms.** A major version keeps its floors: Python 3.11 with paho-mqtt 2.x (Debian 13 for
the installer), Home Assistant 2026.8, and MeshCore companion firmware 1.18.0 (protocol version
3). Raising a floor is breaking; supporting more is not. A firmware version tested in addition is
an addition; dropping a tested one is breaking.

**Not public:** the Python modules, the files in the state directory (the gateway migrates
them), and log messages.

**How it is checked.**

- `tests/public_interface.json` records the structural part of the four interfaces, computed from
  the code by `tests/public_interface.py`. `tests/test_public_interface.py` fails when the code
  and the snapshot differ.
- After a deliberate change, `make interface` writes the snapshot again and lists what changed:
  breaking, changed (a default, a wider range) or added. Review its diff like code.
- The CI job *release plan* (`make release-plan`) compares the snapshot with the one of the last
  release on every pull request and push. An entry that was removed or narrowed needs a
  `**Breaking:**` entry (or a `### Removed` section) since that release, which makes the next
  release a major one. A release must contain the snapshot, so the releases after it are compared
  with it.
- A false alarm (something recorded that was never interface) is waived with a reason in
  `tests/public_interface_waivers.json`, for that release only. The file maps the last release
  to the paths of the findings, as the refusal shows them, and a reason for each:
  `{"1.0.0": {"mqtt > publish > <base_topic>/<node_id>/rx": "never published"}}`.
- Behaviour beyond the structure (meanings, units of values, rules between settings, the texts a
  request accepts) is left to review.

## Release

Releases are built, signed and published by the workflow `.github/workflows/release.yml`
([ADR 0013](decisions/0013-signed-releases-with-github-artifact-attestations.md)); nobody builds
release files by hand.

Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) from 1.0.0 on, for
the four public interfaces of [Compatibility](#compatibility): an incompatible change raises the
major version, a feature (a slice of the roadmap,
[design §13](design.md#13-roadmap-the-mesh-observer)) the minor version, fixes the patch version.

Every finished increment is published as a pre-release, so each step is visible, signed and
installable: the increments of a feature as `X.Y.0aN` of the minor version it leads to (the
telemetry slices 6a, 6b and 6c as `1.1.0a1`, `1.1.0a2`, `1.1.0a3`), fixes as `X.Y.ZaN`. A
release candidate `X.Y.ZrcN` is what the hardware checklist runs with; the release follows when it
passes. `releases/latest` and the installer's default never pick a pre-release
([deployment.md](deployment.md)). Nobody tags by hand
([ADR 0022](decisions/0022-releases-tagged-by-ci-on-main.md)):

1. For a release (not a pre-release): run the [hardware checklist](testing.md#hardware-checklist)
   with the release candidate, the core steps always and the steps of the features that changed
   since the last release. The release ships the program of that candidate: only `__version__`
   may differ in `src/`, `deploy/`, `pyproject.toml` and `tools/build_pyz.py`.
2. The pull request that finishes the increment sets `__version__` in
   `src/meshcore_home_gateway/__init__.py` and moves the *Unreleased* entries of `CHANGELOG.md`
   under `## [X.Y.Z] - YYYY-MM-DD` (a pre-release's section says that the hardware checklist was
   not run for it). The version must be the next one, which `make release-plan` and the CI job
   *release plan* check: the kind of change comes from the changelog entries since the last
   release (an entry that starts with `**Breaking:**` or a `### Removed` section: major; Added,
   Changed or Deprecated: minor; Fixed or Security only: patch), the stage from the version
   written; pre-releases count up from 1. Only Keep a Changelog's six headings are allowed. The
   same check compares the public interface with that of the last release
   ([Compatibility](#compatibility)), also when the version does not change.
   `git fetch --tags origin && make release-plan` runs the check locally (it needs the tags);
   `RELEASE_TAG=vX.Y.Z make release` builds the release files into `dist/release/` as the
   workflow does. A pull request that keeps `__version__` releases nothing.
3. After the merge, CI on `main` tags the merge commit once every job passed on it, and starts
   `release.yml` on the tag (job *tag and release*); a push of several commits is tagged on its
   last one. A re-run, of the failed job or of all jobs, is safe: it finds the tag on the same
   commit and starts the release only if there is none and none is running.
4. The workflow runs `make check`, refuses a tag that differs from `__version__`, builds the
   zipapp, wheel and sdist reproducibly, adds the systemd unit and the Proxmox installer with the
   version written in (`RELEASE="X.Y.Z"`: run from the release's address, it installs this
   release), writes `SHA256SUMS`, attests every file (Sigstore-signed build provenance) and
   publishes the GitHub release with the changelog section as notes (its relative links point at
   the files of the tag).
5. Check the result like a user: download a file and run

   ```sh
   gh attestation verify meshcore-home-gateway-X.Y.Z.pyz --repo dkuenzel/meshcore-home-gateway \
     --signer-workflow dkuenzel/meshcore-home-gateway/.github/workflows/release.yml \
     --source-ref refs/tags/vX.Y.Z --deny-self-hosted-runners
   ```

The release tags are protected by a repository ruleset for `v*`: only its bypass actors (the
maintainer, for emergencies) can move or delete them, and by policy nobody does, so the
attestation's tag names one commit. Creating them is open to write access, which only the
maintainer and the workflows of this repository have (pull requests from forks get read-only
tokens). A release that failed is followed by the next version, never by a moved tag.

A manual run of the workflow ("Run workflow" in the Actions tab) is a dry run on a branch: it
builds the same files and keeps them as a workflow artifact, without attestation or release; on a
tag it releases that tag, as a pushed tag does.
