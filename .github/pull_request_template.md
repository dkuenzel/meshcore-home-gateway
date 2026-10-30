## What and why

<!-- What changes, and which problem it solves. Link the issue if there is one. -->

## How it was tested

<!-- Tests added or changed; results of `make check` and `make matrix`; for radio behaviour,
the steps of the hardware checklist (docs/testing.md) with radio models and firmware versions. -->

## Compatibility

<!-- Does this change the MQTT API, the configuration format, the command line or Home Assistant
discovery (docs/development.md#compatibility)? "No"; or what grows (tests/public_interface.json
written again with `make interface`, an Added or Changed entry: the next release is then a minor
one); or what breaks (a `**Breaking:**` entry that says what users must change); a false alarm
has a waiver. -->

## Release

<!-- "None" (`__version__` unchanged), or the version this pull request releases (for example
1.1.0a2, 1.1.0rc1, 1.1.0): CI checks that it is the next one and tags the merge commit
(docs/development.md). -->

## Checklist

- [ ] Tests first: a new test fails without the change
- [ ] `make check` and `make matrix` pass; `git fetch --tags origin && make release-plan` passes
  for every change of a public interface (it compares with the last release) and for a release
- [ ] Documentation and `CHANGELOG.md` (*Unreleased*, or the section of the version released)
  updated for user-visible changes; an incompatible change starts with `**Breaking:**`
- [ ] Design document or a decision record updated for changes of behaviour, interfaces or
  dependencies
- [ ] Public interface: `make interface` if it changed; a `**Breaking:**` entry for a removal or
  narrowing of what the last release had, an Added or Changed entry for an addition or change
- [ ] No personal data, site names, keys or network details (CONTRIBUTING.md)
