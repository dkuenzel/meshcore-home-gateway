# Development

How to work on meshcore-bus. The rules (design first, test first, evidence for protocol facts)
are in [`CONTRIBUTING.md`](../CONTRIBUTING.md).

## Set up

```sh
make venv    # .venv with the hash-pinned tools from requirements/dev.txt and the package (editable)
make hooks   # pre-push hook: make check
make matrix  # tests with every installed Python from 3.11 to 3.14, each in its own .venv-python3.X
make help    # all targets
```

Requirements: Linux with systemd (for `make unit-check`), Python ≥ 3.11, GNU make, `mosquitto` for
the integration tests, `uv` only to update the lock file.

## Layout

```text
src/meshcore_bus/
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

`config.py` (dataclass and validation), `docs/configuration.md`, `deploy/config.example.toml`, and
a test in `tests/test_config.py`. If the installer should ask for it, also the answer key in
`config.render_config` and the dialog in `deploy/proxmox/meshcore-bus-lxc.sh`.

## Tool chain updates

`make lock` re-resolves `requirements/dev.txt` from `requirements/dev.in` with hashes, admitting
only releases at least `COOLDOWN_DAYS` (14) old. Review the diff like code. GitHub Actions are
pinned to commit SHAs; Dependabot proposes updates with the same cooldown.

## Release

1. Run the hardware checklist in [`testing.md`](testing.md).
2. Set `__version__` in `src/meshcore_bus/__init__.py`; move the *Unreleased* entries in
   `CHANGELOG.md` under the new version with the date.
3. `make check`, then `make dist`; record `sha256sum dist/*` in the release notes.
4. Tag `vX.Y.Z` on the release commit and publish the artifacts with the checksums.
