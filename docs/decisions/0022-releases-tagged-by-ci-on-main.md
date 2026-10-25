---
status: accepted
date: 2026-10-25
decision-makers: maintainers
---

# Releases tagged by CI on main, from the version a pull request writes

## Context and Problem Statement

Releases are built, attested and published by `release.yml` when a tag `vX.Y.Z` arrives
([ADR 0013](0013-signed-releases-with-github-artifact-attestations.md)). The tag was pushed by
the maintainer after the merge of a separate release pull request. Changes are developed and
merged by an agent on the maintainer's behalf, which cannot push tags; the maintainer had to push
each one by hand, a step that is easy to get wrong and that holds every increment up. Every
finished increment is a pre-release (docs/development.md), so there are many. How can the merge
of a pull request release without a manual step, keeping the attested provenance and the
hardware checklist before a release?

## Decision Drivers

* No manual tag, and no maintainer step for pre-releases.
* The provenance stays as strong as before: the release is built by `release.yml` on the tag, and
  the documented `gh attestation verify --signer-workflow … --source-ref refs/tags/vX.Y.Z` holds.
* A tag only for a commit whose CI passed, and only for the right next version.
* The kind of change (major, minor, patch) is declared where it is reviewed: in the diff.
* No new long-lived credential, no new third-party tool, as little new code as possible.
* A release ships the program the hardware checklist ran with.

## Considered Options

* The pull request writes the version; a check on the pull request verifies it; CI on `main` tags
  the merge commit with the workflow's token and starts `release.yml` on the tag
* The same, with the tag made by a dedicated GitHub App, so the tag's push event starts
  `release.yml`
* A release tool (release-please, python-semantic-release, semantic-release, Changesets)
* The version decided at merge time from a label or the title, written by a bot commit
* Building and attesting in the run on `main` instead of on the tag

## Decision Outcome

Chosen option: "the pull request writes the version, CI on main tags it with the workflow's
token", because it needs no credential, no tool and no maintainer step, and the release is built
exactly as before.

* **The pull request** that finishes an increment sets `__version__` and the dated changelog
  section. `tools/release_plan.py check` (CI job *release plan*, `make release-plan`) refuses it
  unless it is the next version: the kind comes from the changelog entries since the last release
  (an entry starting with `**Breaking:**` or a `### Removed` section: major; Added, Changed or
  Deprecated: minor; Fixed or Security only: patch), the stage (`aN`, `bN`, `rcN` or none) from
  the version written, pre-releases count up from 1, and until 1.0.0 is out the next release is
  1.0.0. A release (no stage) needs a release candidate of the same version that ships the same
  program: in `src/`, `deploy/`, `pyproject.toml` and `tools/build_pyz.py` only `__version__` may
  differ.
* **CI on `main`** (job *tag and release*, after every other job passed on the commit): when the
  commit changed `__version__`, `tools/release_plan.py tag` checks it again, the job creates the
  annotated tag through the REST API with the workflow's token (`contents: write`) and starts
  `release.yml` on the tag with `workflow_dispatch` (`actions: write`): events of that token start
  no workflow, except `workflow_dispatch`. A re-run finds the tag on the same commit and starts the
  release only if there is none. A tag on another commit fails the job.
* **`release.yml`** releases on any run on a tag ref, pushed or dispatched; on a branch it stays a
  dry run. The certificate records `release.yml@refs/tags/vX.Y.Z` and the source ref
  `refs/tags/vX.Y.Z`, as for a pushed tag; only the trigger reads `workflow_dispatch`.
* **The tag ruleset** for `v*` forbids moving and deleting tags. It no longer restricts creating
  them, which the workflow's token needs; creating needs write access, which only the maintainer
  and this repository's workflows have.
* **A release** follows the hardware checklist with its candidate; the maintainer reports the
  result, and the agent opens the pull request that writes the release version.

### Consequences

* Good, because pre-releases go out on merge, without anyone pushing anything.
* Good, because the build, the attestation and the user's verification command do not change.
* Good, because the version and its kind are in the diff and checked before the merge, where a
  wrong bump can still be corrected.
* Good, because a release can only ship code that a release candidate shipped.
* Neutral, because every commit on `main` now runs CI to the end (a newer merge no longer cancels
  the run of an older one), so the tag job of each one runs.
* Bad, because a merged version is public within minutes; a wrong one is followed by the next
  version, never withdrawn by moving a tag.
* Bad, because tags can be created by any workflow of the repository that asks for
  `contents: write` and by anyone with write access; the ruleset keeps them from being moved, and
  `make release` refuses a tag that does not match the version it builds.
* Bad, because a meaning that changes (a value now refused, a different retain) is not detected;
  the author declares it, and a check of the public interfaces follows.

### Confirmation

`tests/test_release_plan.py` covers the rules on temporary repositories and that `__version__` is
the newest changelog section; `zizmor` audits the workflows in `make check`. The first release
through the pipeline is checked like a user: the tag by `github-actions[bot]`, `release.yml`
started by `workflow_dispatch` on the tag, and the documented `gh attestation verify` passing.

## Pros and Cons of the Options

### The workflow's token tags, then starts `release.yml` (chosen)

* Good, because nothing new to create, store or rotate.
* Bad, because the trigger in the certificate is `workflow_dispatch`, and tag creation cannot be
  restricted to one actor (GitHub does not reliably accept its Actions bot as a ruleset bypass).

### A dedicated GitHub App tags

* Good, because only the App could create tags, and its tag's push event starts `release.yml`.
* Bad, because the App and its private key are created in the maintainer's browser, the key is a
  long-lived secret to store and rotate, and the agent works as the same GitHub account as the
  maintainer, so an approval gate around it guards against mistakes, not against the agent.

### A release tool

* Good, because widely used.
* Bad, because none writes PEP 440 pre-releases and leaves a written Keep a Changelog file alone;
  they create tags and releases themselves, in the wrong order for the attestation, and decide
  the bump from commit titles that nothing checks before the merge here.

### A label or title at merge time, written by a bot commit

* Bad, because the released commit was never reviewed, the bot needs to push to `main`, and labels
  and squash titles are set by the same account that merges.

### Building and attesting in the run on `main`

* Bad, because the certificate's source ref would be `refs/heads/main`: the documented
  verification would fail, and ADR 0013's guarantee would get weaker.

## More Information

Amends [ADR 0013](0013-signed-releases-with-github-artifact-attestations.md) (who tags). Events
of `GITHUB_TOKEN`:
<https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow>.
Fulcio's certificate extensions:
<https://github.com/sigstore/fulcio/blob/main/docs/oid-info.md>. Revisit when the agent gets a
GitHub identity of its own (then an approval for releases can mean something), or when a
release line besides `main` is needed.
