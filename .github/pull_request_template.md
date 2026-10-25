## What and why

<!-- What changes, and which problem it solves. Link the issue if there is one. -->

## How it was tested

<!-- Tests added or changed; results of `make check` and `make matrix`; for radio behaviour,
the steps of the hardware checklist (docs/testing.md) with radio models and firmware versions. -->

## Release

<!-- "None" (`__version__` unchanged), or the version this pull request releases (for example
1.1.0a2, 1.1.0rc1, 1.1.0): CI checks that it is the next one and tags the merge commit
(docs/development.md). -->

## Checklist

- [ ] Tests first: a new test fails without the change
- [ ] `make check` and `make matrix` pass (for a release also `make release-plan`)
- [ ] Documentation and `CHANGELOG.md` (*Unreleased*, or the section of the version released)
  updated for user-visible changes; an incompatible change starts with `**Breaking:**`
- [ ] Design document or a decision record updated for changes of behaviour, interfaces or
  dependencies
- [ ] No personal data, site names, keys or network details (CONTRIBUTING.md)
