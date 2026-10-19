# Contributing

Thank you for helping. This project bridges a radio network and home automation, so correctness and
safety come before features. The rules below keep it that way.

## Ground rules

- **Design first.** Changes to behaviour, interfaces (MQTT topics and payloads, configuration, CLI) or
  dependencies start with an update to [`docs/design.md`](docs/design.md) and, for decisions with
  alternatives, a new record in [`docs/decisions/`](docs/decisions/README.md). Records are never
  edited after acceptance; a new record supersedes an old one.
- **Test first.** Write the failing test, make it pass, then refactor. Every bug fix comes with a
  test that fails without the fix.
- **Evidence for every protocol fact.** Statements about the MeshCore firmware cite the source file
  and line at a named release tag (see [`docs/protocol.md`](docs/protocol.md)). Calculations are
  checked against an independent source or method.
- **No new runtime dependencies** without a decision record. The runtime uses the Python standard
  library and `paho-mqtt` from the Linux distribution only
  ([ADR 0005](docs/decisions/0005-runtime-dependencies-from-the-distribution.md)).
- **Keep it neutral.** No personal data, site names, addresses, call signs, keys or network details
  in code, tests, examples, documentation or commit messages. Use the documentation ranges
  (`192.0.2.0/24`, `example.org`) and invented names.

## Development setup

Requirements: Linux with systemd, Python 3.11 or newer, GNU make, and `mosquitto` for the
integration tests (`apt install mosquitto`; the tests start their own broker on a free port).

```sh
make venv     # .venv from the hash-pinned lock file requirements/dev.txt
make hooks    # run `make check` before every push
make check    # everything CI runs: lint, shellcheck, workflow audit, tests, build, unit check
make matrix   # the tests with every installed Python from 3.11 to 3.14 (the CI matrix)
```

`make help` lists all targets. CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the
same targets, so a green `make check` plus `make matrix` locally means a green pipeline.

The integration tests need `mosquitto`. If it is missing they fail rather than silently skip;
set `MESHCORE_BUS_SKIP_BROKER_TESTS=1` to skip them deliberately (the summary shows the skip).

## Dependencies of the tool chain

Development tools are pinned with hashes in `requirements/dev.txt`, resolved from
`requirements/dev.in` by `make lock`. The lock only admits releases that are at least 14 days old
(`COOLDOWN_DAYS`), which keeps freshly published, possibly compromised releases out. Dependabot
proposes updates with the same cooldown; regenerate the lock in the pull request with `make lock`.
GitHub Actions are pinned to full commit SHAs with the version as a comment.

## Style

- Python: `ruff` (lint and format) and `mypy --strict` must pass; type hints everywhere.
- Small modules with one responsibility; the domain core (`meshcore_bus.core`) does no I/O.
- Shell: `shellcheck` clean, `set -Eeuo pipefail`, functions over long scripts.
- Documentation in English, plain and short sentences; Markdown, one sentence per idea.

## Commits and pull requests

- One logical change per commit; imperative subject line of at most 72 characters.
- Update `CHANGELOG.md` (section *Unreleased*) for every user-visible change.
- A pull request describes what changed, why, and how it was tested, including tests on real
  hardware where relevant (see the checklist in [`docs/testing.md`](docs/testing.md)).

## Releases

Semantic Versioning. The MQTT API, the configuration format and the CLI are public interfaces:
incompatible changes need a new major version (while in 0.x: a new minor version and a clear note
in the changelog). Release steps are in [`docs/development.md`](docs/development.md).
