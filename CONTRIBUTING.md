# Contributing

Thank you for helping. This project bridges a radio network and home automation, so correctness and
safety come before features. The rules below keep it that way. Everyone taking part follows the
[code of conduct](CODE_OF_CONDUCT.md).

Report bugs and propose features with the issue forms; report security problems privately, as
described in [`SECURITY.md`](SECURITY.md). For a larger change, open an issue first, so that the
design can be agreed on before the code is written.

## Ground rules

- **Design first.** Changes to behaviour, interfaces (MQTT topics and payloads, configuration, CLI,
  Home Assistant discovery) or dependencies start with an update to
  [`docs/design.md`](docs/design.md) and, for decisions with alternatives, a new record in
  [`docs/decisions/`](docs/decisions/README.md). Records are never edited after acceptance; a new
  record supersedes an old one.
- **Test first.** Write the failing test, make it pass, then refactor. Every bug fix comes with a
  test that fails without the fix.
- **Evidence for every protocol fact.** Statements about the MeshCore firmware cite the source file
  and line at a named release tag (see [`docs/protocol.md`](docs/protocol.md)). Calculations are
  checked against an independent source or method. Behaviour of the other software the project
  relies on (Proxmox VE, whiptail, the broker, Home Assistant) goes to
  [`docs/platform-notes.md`](docs/platform-notes.md) with the versions and the evidence.
- **No new runtime dependencies** without a decision record. The runtime uses the Python standard
  library and `paho-mqtt` from the Linux distribution only
  ([ADR 0005](docs/decisions/0005-runtime-dependencies-from-the-distribution.md)).
- **Keep it neutral.** No personal data, site names, addresses, call signs, keys or network details
  in code, tests, examples, documentation or commit messages. Use the documentation ranges
  (`192.0.2.0/24`, `example.org`) and invented names.

## Development setup

Requirements: Linux with systemd, Python 3.11 or newer with `venv` (on Debian and Ubuntu the
package `python3-venv`, or `python3.X-venv` for each Python that `make matrix` uses), GNU make,
`mosquitto` for the integration tests and GNU gettext for the tests of the message catalogs
(`apt install mosquitto gettext`; the tests start their own broker on a free port).

```sh
make venv     # .venv from the hash-pinned lock file requirements/dev.txt (fresh when it changes)
make hooks    # run `make check` before every push
make check    # everything CI runs: lint, shellcheck, workflow audit, tests, build, unit check
make matrix   # the tests with each Python from 3.11 to 3.14 (the CI matrix); all must be installed
```

`make help` lists all targets. CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the
same targets, so a green `make check`, `make matrix` and `make ha-test` locally mean a green
pipeline. `make matrix` fails when one of the Pythons is missing; `make matrix PYTHONS=python3.13`
tests only the ones named, and CI the rest.

The pre-push hook runs `make check` on the working tree, so it lets a push through only when that
is the commit being pushed: it refuses uncommitted changes, untracked files (commit, stash or
ignore them) and refs that point to another commit than the checked-out one.

The integration tests need `mosquitto`. If it is missing they fail rather than silently skip; set
`MESHCORE_HOME_GATEWAY_SKIP_BROKER_TESTS=1` to skip them deliberately (the summary shows the skip).
The same holds for the tests that compare our message catalogs with GNU gettext's `msgfmt`:
`MESHCORE_HOME_GATEWAY_SKIP_GETTEXT_TESTS=1` skips them deliberately.

## Dependencies of the tool chain

Development tools are pinned with hashes in `requirements/dev.txt`, resolved from
`requirements/dev.in` by `make lock`. The lock only admits releases that are at least 14 days old
(`COOLDOWN_DAYS`), which keeps freshly published, possibly compromised releases out. Dependabot
proposes updates with the same cooldown; regenerate the lock in the pull request with `make lock`.
The exception is `paho-mqtt`: `requirements/dev.in` pins the version of Debian 13, so that CI
tests what production runs
([ADR 0005](docs/decisions/0005-runtime-dependencies-from-the-distribution.md)). Dependabot leaves
it alone; it changes with the deployment target.
GitHub Actions are pinned to full commit SHAs with the version as a comment.

## Style

- Python: `ruff` (lint and format) and `mypy --strict` must pass; type hints everywhere.
- Small modules with one responsibility; the domain core (`meshcore_home_gateway.core`) does no I/O.
- Shell: `shellcheck` clean, `set -Eeuo pipefail`, functions over long scripts.
- Documentation in English, plain and short sentences; Markdown, one sentence per idea.

## Translations

The texts of the command line, the installer, the Home Assistant names and the dashboard are in
gettext catalogs: `src/meshcore_home_gateway/locale/` and `deploy/proxmox/locale/`. Edit a PO
file with any editor or with Poedit; switch off line wrapping and compiling on save (git ignores
the compiled `.mo`, but the wheel would pack it, so `make dist` refuses it). Weblate is not set
up yet. Run `make translations` before a pull request. A new or changed text needs its
translation in every shipped language in the same pull request; ask in the pull request if you
cannot write one. The files hold no names or addresses: the git history credits the translators.
[`docs/translations.md`](docs/translations.md) has the rules and how to add a language.

## Commits and pull requests

- One logical change per commit; imperative subject line of at most 72 characters.
- Update `CHANGELOG.md` (section *Unreleased*) for every user-visible change.
- A pull request describes what changed, why, and how it was tested, including tests on real
  hardware where relevant (see the checklist in [`docs/testing.md`](docs/testing.md)); the pull
  request template lists the points.

## Releases

Semantic Versioning from 1.0.0 on. The MQTT API, the configuration format, the CLI and Home
Assistant discovery are public interfaces
([compatibility](docs/development.md#compatibility)): incompatible changes need a new major
version, features a new minor version, with a clear note in the changelog (an incompatible change
starts with `**Breaking:**`). After a change of one, `make interface` writes
`tests/public_interface.json` again; CI compares it with the last release. A pull request
releases by writing the next version and its changelog section; CI checks it and tags the merge
commit, nobody tags by hand. Release steps are in [`docs/development.md`](docs/development.md).
