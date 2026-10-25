---
status: accepted
date: 2026-10-22
decision-makers: maintainers
---

# Signed releases with GitHub artifact attestations

## Context and Problem Statement

Users install the zipapp on a machine that controls a radio transmitter and talks to their home
automation. So far a release meant building the files on a maintainer's machine and publishing
them with checksums. A checksum shows that a download is complete, not who built it or from which
sources. How do users verify that a release file was built by this repository's workflow from the
tagged commit?

## Decision Drivers

* Verifiable origin: the file was built from a named commit by a named workflow.
* No long-lived signing key that has to be kept secret, rotated and published.
* No new runtime dependency and as few new tools as possible
  ([ADR 0005](0005-runtime-dependencies-from-the-distribution.md)).
* The same build steps as a local `make check`, so the release cannot differ from what was tested.
* Reproducible files, so anyone can rebuild and compare.

## Considered Options

* GitHub artifact attestations (Sigstore-signed SLSA build provenance), files and `SHA256SUMS` on a
  GitHub release
* Checksums signed with a maintainer's OpenPGP key
* Sigstore bundles per file made with `sigstore-python`
* Publishing to PyPI with trusted publishing (and its attestations)

## Decision Outcome

Chosen option: "GitHub artifact attestations", because they bind each file's digest to the
repository, the workflow and the commit that built it, signed with a short-lived certificate from
the public Sigstore instance, without any key to manage. `gh attestation verify` checks them, and
`sha256sum --check` still works without any tool.

The workflow `.github/workflows/release.yml` runs on a tag `vX.Y.Z`:

1. **check and build**, read-only: `make check` (everything CI runs), then `make release`, which
   refuses a tag that does not match `__version__`, builds the zipapp
   `meshcore-home-gateway-X.Y.Z.pyz` (interpreter `/usr/bin/python3 -I`, as installed), the wheel
   and the sdist with the commit time as timestamp, writes `SHA256SUMS`, and takes the release
   notes from the dated `CHANGELOG.md` section of the version;
2. **attest and publish**, the only job with write permissions (`contents`, `id-token`,
   `attestations`): checks the files against `SHA256SUMS`, attests every file listed there with
   `actions/attest`, and creates the GitHub release with the files, the attestation as a Sigstore
   bundle and the notes. PEP 440 pre-releases (`a`, `b`, `rc`, `dev`) are marked as
   pre-releases.

A manual run of the workflow is a dry run: it builds the files and keeps them as a workflow
artifact, without attestation or release. `make release` does the same locally.

### Consequences

* Good, because users can verify origin and integrity with one command, and the provenance names
  the exact commit and workflow run.
* Good, because there is no signing secret in the repository or with a maintainer.
* Good, because the release files are reproducible: a rebuild from the tag with the same tools
  gives the same digests.
* Neutral, because one more action (`actions/attest`, by GitHub) is pinned by commit SHA and
  updated by Dependabot with the cooldown.
* Neutral, because the check must name the release workflow and the tag (`--signer-workflow`,
  `--source-ref`; docs/deployment.md), and the release tags must be protected: with `--repo`
  alone, any workflow of the repository on any branch would pass.
* Bad, because the verification with `gh` depends on GitHub's attestation API; the Sigstore bundle
  can also be checked with other Sigstore clients, but that is more work.
* Bad, because releases need GitHub Actions to run; when the account cannot run workflows, no
  release can be published (the local dry run still works).

### Confirmation

`zizmor` audits the workflow in `make check`. `tests/test_release_notes.py` covers the release
notes. The workflow's dry run builds the files on every manual run. Before a release,
`docs/development.md` lists the steps; after it, `gh attestation verify` on a downloaded file must
pass.

## Pros and Cons of the Options

### Checksums signed with an OpenPGP key

* Good, because widely understood and independent of GitHub.
* Bad, because the key has to be kept secret, backed up, rotated and its fingerprint published;
  a stolen key signs anything.

### Sigstore bundles with `sigstore-python`

* Good, because the bundles can be verified with any Sigstore client.
* Bad, because it adds a tool to the release job for what `actions/attest` already produces in the
  same Sigstore bundle format.

### PyPI with trusted publishing

* Good, because `pip install` would work and PyPI shows attestations too.
* Bad, because the deployment model is the zipapp with the distribution's `paho-mqtt`, not a
  virtual environment from PyPI ([ADR 0005](0005-runtime-dependencies-from-the-distribution.md));
  a PyPI release can be added later on top of this workflow.

## More Information

* Amended on 2026-10-25 by [ADR 0022](0022-releases-tagged-by-ci-on-main.md): CI on `main` tags
  the commit that changed the version and starts the workflow on the tag (`workflow_dispatch`); a
  run on a tag releases, pushed or dispatched. The tag ruleset forbids moving and deleting tags.
* `actions/attest`: <https://github.com/actions/attest>
* Using artifact attestations:
  <https://docs.github.com/en/actions/security-guides/using-artifact-attestations-to-establish-provenance-for-builds>
* `gh attestation verify`: <https://cli.github.com/manual/gh_attestation_verify>
* SLSA build provenance: <https://slsa.dev/spec/v1.0/provenance>
