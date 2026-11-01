# Security and architecture review, November 2026: instructions and plan

| | |
| --- | --- |
| Subject | `dkuenzel/meshcore-home-gateway`, `main` at `d47e947` (version 1.1.0a4, released 2026-11-01), plus the open branches `docs/usb-builds-without-usb-interface` (PR #10) and the two Dependabot branches open on 2026-11-01 |
| Reviewer | An independent security and architecture auditor (an AI agent, Claude), working from a clone of the repository, the pinned MeshCore firmware source (`companion-v1.18.0`, commit `81dbae5`), the pinned CayenneLPP source (1.6.1), public documentation of the platforms the project relies on, and the repository's GitHub issues and pull requests |
| Started | 2026-11-01 |
| Branch | `claude/security-architecture-audit-01RMSbQ23kgFfJDGMGr7UxTk`, from `main` |
| Folder | `docs/audit/2026-11-security-architecture-review/` holds everything the review produced |
| Status of this folder | Working documents of a review, not a design decision. Nothing in it changes the program; every item is a proposal for the project team to triage |

## 1 The instruction

The review was commissioned with this instruction (quoted as given, 2026-11-01):

> I just opensourced this meshcore to mqtt bridge with home assistant entities. The project is still in develppment and not yet 100% finished but I want to do a surveillance audit and review to feed the results back into the ongoing agile develpment process. As this is a bridge from a publc mesh network onto a potentially unrestricted internal mqtt broker and a home assistant instance with far reaching abilities in one's home we need to do a thorough security review as well. Aside from this we should do a review of the overaching architecture, design, the decisions, docuentation, code and other repo contents. All findings, details, improvement opportunities, extension opportunieis, issues, should be fully documented. No triage yet, this is up to the project teams we want to be as verbose about our analysis and audit as possible to give full context and all perspectives. All reseach details that may be valuable should also be documented. Create the audit, review, ideas, opportunities, etc documentation as a dedicated folder in the repo, include all material and output in the folder. For all findings/opportunities/items give concrete mitigation or impementation suggestions and details if it makes sense.This output will be the full design doc for all suggested items.
> Do a first round and make yourself familiar with the code base, instruction, todos, etc, then let's align again on the final plan and it's contents before you put it in practice. Our documentation starts with this intruction, plan and planned initiative, all docs should be placed in the one dedicated folder which we create on a fresh branch and then later push once we are done. Commit regulary to keep progress transparent.

After the familiarisation round, the plan below was proposed and these points were settled:

1. The folder lives under `docs/` (`docs/audit/2026-11-security-architecture-review/`), not at the repository root.
2. The branch name was left to the reviewer: `claude/security-architecture-audit-<session id>`.
3. Every finding states its impact and its attacker preconditions in full detail. There is **no severity rating, no priority and no effort estimate**: triage belongs to the project team.
4. The reviewer may fetch every source it deems valuable (the pinned firmware, library documentation, platform documentation) and is explicitly encouraged to cross-check best practices and the latest documentation of the libraries and platforms.
5. Everything is in scope: the program, the installer, the tooling, the translation machinery, the dashboard template, the firmware test harness, the documentation, the repository settings as far as they are visible, and the open feature branches, which are frozen until the following Friday so that the review can cover them.

The operating constraints of the reviewer, for transparency: it works in a sandbox with the clone, Python 3.11, `ruff`, `mypy` and `paho-mqtt` 2.1.0; without `mosquitto`, GNU gettext, a C++ compiler or Home Assistant, so the broker, catalog, firmware and contract test suites were not run here (the unit, component and emulator suites were: about 900 tests pass on `main`). It has no credentials for the repository: pushing waits for an instruction and a token, and GitHub repository settings (rulesets, branch protection, secret scanning, private vulnerability reporting) could only be checked through what the public API and the documents say.

## 2 Scope

| Area | What is reviewed | Where the results go |
| --- | --- | --- |
| Security | The trust boundary between the public mesh and the internal broker and Home Assistant; input validation at every entry point (mesh, radio link, broker, Home Assistant controls, configuration, state files, installer answers); authentication and authorisation of commands and requests; injection (JSON, Jinja templates in discovery and the dashboard, Markdown, terminal escapes, TOML, shell); denial of service (airtime, memory, disk, the event loop, the broker); credential handling; what Home Assistant exposes and to whom; the radio protocol's own weaknesses as they affect the gateway; the installer and the service sandbox; the release and supply chain | `02-threat-model.md`, `03-security-findings.md`, `07-…`, `08-…` |
| Architecture and design | The decisions (27 ADRs) and the design document: whether the reasoning holds, the tensions between decisions, the alternatives not taken, the tradeoffs the team accepted, and what the growth from a bridge to an observer did to the original shape | `04-architecture-and-design-review.md` |
| Code | Every module of `src/`, the tools, the tests and the installer: correctness as far as reading and targeted experiments show it, maintainability, consistency, error handling, concurrency, resource bounds, the test strategy | `05-code-quality-review.md` |
| Documentation | Accuracy against the code, gaps, inconsistencies, the path a newcomer takes, what an operator needs and does not find | `06-documentation-review.md` |
| Operations and deployment | The Proxmox installer, the systemd unit, the container, update and rollback, backup and restore, observability, failure behaviour | `07-operations-and-deployment-review.md` |
| Supply chain and release | CI, the release workflow, attestations, the dependency policy, reproducibility, the GitHub settings as far as visible | `08-supply-chain-and-release-review.md` |
| Improvements | Changes to what exists, each with a design sketch | `09-improvement-opportunities.md` |
| Extensions | New capabilities, each with a design sketch | `10-extension-opportunities.md` |
| Research | Facts established during the review that the project may want to keep: firmware behaviour verified in the source, platform behaviour, regulation, related projects, references | `11-research-notes.md` |
| Index | Every item in one table and one CSV for import into the backlog | `12-findings-index.md`, `findings.csv` |

Out of scope: nothing was excluded. Where the reviewer could not verify something (no hardware, no Home Assistant, no Proxmox host), the finding says so and names what would verify it.

## 3 Method

1. **Read everything.** All of `src/`, `deploy/`, `tools/`, `tests/` (structure and the parts that matter for a claim), `home-assistant/`, `.github/`, `docs/`, the changelog, the issues and pull requests, and the open branches.
2. **Model the system independently** before judging it: components, data flows, trust boundaries, state, and the attacker roles (`01-…`, `02-…`). The project's own threat table (design §7) is compared with that model, not taken as the starting point.
3. **Verify claims at the source.** Statements about the firmware are checked in the pinned MeshCore and CayenneLPP sources fetched with the project's own `tools/fetch_firmware.py` (commit `81dbae5`). Statements about libraries and platforms are checked in their current documentation. Where a claim could not be verified, the item says so.
4. **Experiment where reading is not enough.** The radio emulator and the unit suites are used for targeted checks (resource growth under floods, clock behaviour, parsing of hostile input). Each experiment is described so that the team can repeat it.
5. **Write every item in full**, with the template of section 5, in the document of its category. Items that belong to several categories are written once and cross-referenced.
6. **Commit after every document** (and after substantial additions), so the progress is visible in the history. Push only when instructed.

## 4 Identifiers

Each item has an identifier that is stable across the documents and the index:

| Prefix | Category | Document |
| --- | --- | --- |
| `SEC-nnn` | Security finding | `03-security-findings.md` |
| `ARC-nnn` | Architecture and design | `04-architecture-and-design-review.md` |
| `CODE-nnn` | Code quality, correctness, tests | `05-code-quality-review.md` |
| `DOC-nnn` | Documentation | `06-documentation-review.md` |
| `OPS-nnn` | Operations and deployment | `07-operations-and-deployment-review.md` |
| `SUP-nnn` | Supply chain and release | `08-supply-chain-and-release-review.md` |
| `IMP-nnn` | Improvement opportunity | `09-improvement-opportunities.md` |
| `EXT-nnn` | Extension opportunity | `10-extension-opportunities.md` |
| `RES-nnn` | Research note | `11-research-notes.md` |

Numbers are assigned in the order of writing and never reused. An item withdrawn during the review keeps its number and says why.

## 5 The template of an item

Every finding, opportunity and note uses these headings, in this order; a heading that does not apply says "none" rather than being left out, so that nothing is silently skipped.

```text
### <ID> <title>

| | |
| --- | --- |
| Category | security / architecture / code / documentation / operations / supply chain / improvement / extension / research |
| Component | the files and lines, or the document and section |
| Related | other item IDs, ADRs, issues |

**Description.** What the reviewer found, in enough detail that a reader who has not seen the
code understands it.

**Evidence.** The code, document, firmware source or experiment that shows it; file:line
citations; what was run and what came back.

**Preconditions and attacker model.** Who has to be where, with what access, for this to matter
(a node in radio range, a broker client with which ACL, a Home Assistant user of which role, the
host, the supply chain); or "none" for items that are not about an adversary.

**Impact.** What can happen, to which asset, in full: the chain from the precondition to the
outcome, and what the outcome means for the household, the mesh and the operator. Factual, no
rating.

**Existing mitigations.** What the project already does about it, with references, so the team
can see what the finding adds.

**Suggested mitigation or implementation.** Concrete: which module changes how, which setting or
document, a sketch of the code where it helps, which test would show the change holds, and the
alternatives with their tradeoffs. Where several options exist, all are given.

**References.** Standards, documentation, sources outside the repository.
```

Items in `09-…` and `10-…` (opportunities) replace the two attacker-oriented headings by **Motivation** and **Design sketch**; items in `11-…` (research) by **Fact**, **How it was verified** and **Where it matters**.

## 6 What the review does not do

* It does not rate, rank or triage. The team decides what to do, in which order, and whether at all.
* It does not change the program, the documentation outside this folder, or the configuration. Where a fix is small and obvious, the item still only describes it.
* It does not claim to be complete. A review is a sample of attention; the index says what was covered with which depth.
* It does not replace the project's own hardware checklist, contract tests or firmware tests, none of which the reviewer could run.

## 7 The documents, in reading order

| File | Content | State |
| --- | --- | --- |
| `00-instructions-and-plan.md` | This document | written |
| `01-system-understanding.md` | The reviewer's model of the system: components, flows, boundaries, state, timing | written |
| `02-threat-model.md` | Assets, actors, entry points, STRIDE per boundary, attack trees, the project's threat table compared | written |
| `03-security-findings.md` | SEC items | written |
| `04-architecture-and-design-review.md` | ARC items | written |
| `05-code-quality-review.md` | CODE items | written |
| `06-documentation-review.md` | DOC items | written |
| `07-operations-and-deployment-review.md` | OPS items | written |
| `08-supply-chain-and-release-review.md` | SUP items | written |
| `09-improvement-opportunities.md` | IMP items | written |
| `10-extension-opportunities.md` | EXT items | written |
| `11-research-notes.md` | RES items | written |
| `12-findings-index.md`, `findings.csv` | The index of every item | written |

(The state column is updated as the documents are committed.)

## 8 How the familiarisation round went

Before this plan was agreed, the reviewer read the README, the design document, all 27 decision records, the protocol reference, the MQTT API, the configuration, deployment, Home Assistant, companions, testing, development and translation guides, the changelog, the security policy, the contributing guide, every module of `src/`, the installer, the workflows, the Makefile, the test layout, the issues and the pull requests, and ran the unit, component and emulator suites locally. The summary given to the project owner at that point, and the observations that calibrated the plan, are kept in section 9 so that the starting point of the review is on record.

## 9 The starting point: summary of the familiarisation round

**System.** A Python 3.11+ asyncio service (standard library plus `paho-mqtt`) that holds one MeshCore companion radio (USB serial or unauthenticated TCP), bridges messages to and from an MQTT broker, and announces itself and every contact of the radio to Home Assistant through device-based MQTT discovery. Since 1.1 it is also the household's "mesh observer": contact management, telemetry polling of the household's companions (positions, battery, sensors), a packet-log-based check for position-leaking adverts, a SQLite history, a managed radio profile (which writes settings to the radio), internationalisation machinery and a generated Home Assistant dashboard. About 59,000 lines including tests; 1,609 test functions; a Proxmox LXC installer of 1,373 lines of bash that runs as root on the host from `bash -c "$(wget …)"`.

**Trust boundaries as the project sees them:** mesh (untrusted) → radio firmware (trusted, holds keys) → USB or TCP → gateway → broker (authenticated clients) → Home Assistant. Commands into Home Assistant are gated by ADR 0009: direct messages only, full-key allowlist, a 24-hour memory of keys sharing an allowed sender's 6-byte prefix, monotonic per-sender timestamps persisted before publishing, a clock window, rejection of anything fetched while connecting. Outbound: configured targets only, an airtime token bucket, deduplication, a bounded queue, rate-limited refusal events. Service: a strong systemd sandbox (exposure score 1.6), an unprivileged container, a `python3 -I` zipapp. Supply chain: hash-pinned development tools with a 14-day cooldown, SHA-pinned actions, zizmor, Sigstore attestations, CI-tagged releases, a reproducible zipapp.

**Maturity.** Unusually well engineered for a two-week-old project: 27 MADR records, a 1,280-line design document with a threat table and residual risks, a protocol document citing firmware file and line at a pinned tag, 1,555 lines of evidence-backed platform notes, contract tests against the real Home Assistant, and the real firmware compiled natively on a simulated air. A prior audit already happened (PRs #2 and #3: 125 findings re-checked, 65 fixed). No TODO or FIXME marker anywhere. `ruff` clean; the local suites pass. 39 of 50 commits are authored by an AI agent working for the maintainer, which is relevant context: consistency is high, but the decision records and the code are written in one voice, so an independent challenge of the assumptions is where a review adds most.

**Open on GitHub:** issue #23 (an AsyncAPI document, with a documentation inconsistency the owner noted in a comment), PR #10 (documentation of USB builds without a USB interface), two Dependabot pull requests.

**Preliminary observations that calibrated the plan** (each is verified and written up in full in the documents that follow; this list is the record of what was seen at the start, not a result):

* The MeshCore acknowledgement code is `SHA256(timestamp ‖ attempt ‖ text ‖ sender's public key)[:4]`, sent in the clear: whoever can guess the text of a message (alert texts are fixed strings) can forge a `confirmed` and suppress the retries.
* `set_clock` only moves the radio's clock forward, and a host with a wrong future clock moves it irreversibly (until the radio restarts), with no plausibility bound before the write.
* The history's `node` table has no row cap, unlike the other tables: an advert flood with invented keys grows the database until the purge by age; the in-memory heard-node list holds 50 entries, so such a flood evicts the real nodes from the *Add heard node* select.
* Broker security is entirely external to the gateway: a configuration without credentials or TLS is accepted silently, the password lives in a file rather than a systemd credential, and any broker client that knows the client id can disconnect the gateway.
* The installer is the `curl | bash` pattern run as root on the Proxmox host (with a documented attestation path), and `--ref` installs any branch over HTTPS alone.
* The release build job runs third-party development tooling before the artifact is attested, and the reproducibility the project relies on is not demonstrated by an independent rebuild in CI.
* The state file is written by about eight components with load-modify-save each; one map in it (`slugs`) grows without bound; blocking `fsync` and SQLite purges run on the event loop that also feeds a 60-second watchdog.
* Strings from the mesh reach Home Assistant device names: any node in range can advertise as "MeshCore radio" or under an existing contact's name.
* Several documentation inconsistencies (the topic direction row issue #23 names, retention days, the stale repository description).
