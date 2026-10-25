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
tools/           build helpers (zipapp)
.github/         CI workflow, Dependabot, issue forms, pull request template
```

## Test-driven workflow

1. Write or extend a test that describes the behaviour and fails.
2. Implement until it passes; keep core modules free of I/O.
3. Refactor; `make lint test` stays green.
4. Update the documentation of the interface you touched (`mqtt-api.md`, `configuration.md`,
   `protocol.md`) and `CHANGELOG.md`.

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
`deploy/config.example.toml`, and a test in `tests/test_config.py`. If the installer should ask
for it, also the dialog in `deploy/proxmox/meshcore-home-gateway-lxc.sh`.

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

## Release

Releases are built, signed and published by the workflow `.github/workflows/release.yml`
([ADR 0013](decisions/0013-signed-releases-with-github-artifact-attestations.md)); nobody builds
release files by hand.

Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) from 1.0.0 on: the
MQTT API, the configuration format and the command line are the public interfaces; an
incompatible change raises the major version, a feature (a slice of the roadmap,
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
   written; pre-releases count up from 1. `RELEASE_TAG=vX.Y.Z make release` builds the release
   files into `dist/release/` as the workflow does. A pull request that keeps `__version__`
   releases nothing.
3. After the merge, CI on `main` tags the merge commit once every job passed on it, and starts
   `release.yml` on the tag (job *tag and release*). A re-run of a failed job is safe: it finds
   the tag on the same commit and starts the release only if there is none.
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

The release tags are protected by a repository ruleset for `v*`: they cannot be moved or
deleted, so the attestation's tag names one commit for good. Creating them is open to write
access, which only the maintainer and the workflows of this repository have (pull requests from
forks get read-only tokens). A release that failed is followed by the next version, never by a
moved tag.

A manual run of the workflow ("Run workflow" in the Actions tab) is a dry run on a branch: it
builds the same files and keeps them as a workflow artifact, without attestation or release; on a
tag it releases that tag, as a pushed tag does.
