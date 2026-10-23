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

1. Run the [hardware checklist](testing.md#hardware-checklist) with the release candidate.
2. In a pull request: set `__version__` in `src/meshcore_home_gateway/__init__.py` (PEP 440, for
   example `0.1.0` or `0.2.0rc1`) and move the *Unreleased* entries of `CHANGELOG.md` under
   `## [X.Y.Z] - YYYY-MM-DD`. `make release` builds the release files into `dist/release/` as a
   dry run and only warns about a missing section or an unsupported version form;
   `RELEASE_TAG=vX.Y.Z make release`, as the workflow runs it, fails then. Run that before you
   tag.
3. After the merge, tag the merge commit and push the tag:
   `git tag -a vX.Y.Z -m "meshcore-home-gateway X.Y.Z" && git push origin vX.Y.Z`.
4. The workflow runs `make check`, refuses a tag that differs from `__version__`, builds the
   zipapp, wheel and sdist reproducibly, adds the systemd unit and the Proxmox installer with the
   version written in (`RELEASE="X.Y.Z"`: run from the release's address, it installs this
   release), writes `SHA256SUMS`, attests every file (Sigstore-signed build provenance) and
   publishes the GitHub release with the changelog section as notes.
5. Check the result like a user: download a file and run

   ```sh
   gh attestation verify meshcore-home-gateway-X.Y.Z.pyz --repo dkuenzel/meshcore-home-gateway \
     --signer-workflow dkuenzel/meshcore-home-gateway/.github/workflows/release.yml \
     --source-ref refs/tags/vX.Y.Z --deny-self-hosted-runners
   ```

Protect the release tags (a repository ruleset for `v*` that only maintainers may create): the
attestation proves which workflow built a file from which tag, so a tag that anyone with write
access can push proves little.

A manual run of the workflow ("Run workflow" in the Actions tab) is a dry run on any branch: it
builds the same files and keeps them as a workflow artifact, without attestation or release.
