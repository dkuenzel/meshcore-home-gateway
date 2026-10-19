---
status: accepted
date: 2026-10-18
decision-makers: maintainers
---

# Record architecture decisions as MADR files in the repository

## Context and Problem Statement

The bridge makes choices with long-lived consequences: protocol implementation, dependencies,
security model, deployment. Contributors and users who come later need to know why a choice was
made, which alternatives were rejected, and when to revisit it.

## Considered Options

* Markdown Architectural Decision Records (MADR 4.0) in `docs/decisions/`
* Decisions only in the design document
* Decisions in issues or pull requests

## Decision Outcome

Chosen option: "MADR 4.0 in `docs/decisions/`", because the records are versioned with the code,
reviewable in pull requests and readable without any tool.

### Consequences

* Good, because every significant change has a traceable reason and a rejected alternative.
* Good, because the design document stays a description of the current system and links to the
  records for the reasoning.
* Bad, because records must be kept short to stay useful.

### Confirmation

Pull requests that change behaviour, interfaces or dependencies add or reference a record
(`CONTRIBUTING.md`). Accepted records are not edited; a new record supersedes an old one and both
carry the link.

## More Information

* MADR 4.0.0, <https://adr.github.io/madr/> (file naming `NNNN-title-with-dashes.md`).
* Template for new records: [`template.md`](template.md).
