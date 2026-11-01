# 06 Documentation review

The documentation set is `README.md`, `docs/` (12 guides, 27 records, the template), `CHANGELOG.md`,
`SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, the issue forms, the pull request
template, the example configuration, the unit file's comments, and the module docstrings. It was
read in full and checked against the code for every claim the review touched. Items are
`DOC-nnn`; items that are consequences of findings elsewhere name them.

## 1 General impression

This is the best-documented small project the reviewer has seen: a design document with
numbered requirements and a failure-mode table; records for 27 decisions with alternatives and
revisit clauses; a protocol reference that cites firmware lines at a pinned tag and lists where
the firmware's own documentation is wrong; platform notes that record how each claim about
Proxmox, Mosquitto, paho, Home Assistant and whiptail was verified; a hardware checklist of 54
steps with the expected observation of each; a compatibility policy that says what each
interface promises. The prose style is unusual (long sentences of subordinate clauses, the
present tense, no bullets inside bullets), consistent, and dense: it rewards careful reading
and punishes skimming, which is the right tradeoff for a reference and the wrong one for a
first-steps guide. The weaknesses are staleness after the fast release cadence (seven releases
in eight days), a few statements the code does not support, and the absence of a short way in.

## 2 Items

### DOC-001 The topic table gives the gateway's own discovery topic the direction "bridge →"

| | |
| --- | --- |
| Category | documentation (accuracy) |
| Component | `docs/mqtt-api.md`, the topic table, row `<prefix>/device/meshcore_home_gateway_<node_id>/config` |
| Related | issue #23 (the owner's comment of 2026-10-25 and 2026-11-01 names it); `docs/home-assistant.md` "How discovery behaves"; `mqtt/client.py` (`ha_devices` subscriptions), `bridge._ha_device_message` |

**Description.** The gateway subscribes to its own discovery topic and acts on an empty message
there (Home Assistant clears it when it removes a disabled entity or the user deletes the device);
the table says "bridge →" while the two sibling rows say "both". The owner noted it in issue #23
twice; it is still so on `main` at 1.1.0a4.

**Suggested change.** "both", with the note the sibling rows have ("the gateway reads it to notice
when Home Assistant clears it").

---

### DOC-002 Command age checks are attributed to the radio's clock, which does not enter them

| | |
| --- | --- |
| Category | documentation (accuracy) |
| Component | `docs/design.md` §6.1 "Clock" ("If the radio is ahead of the bridge by more than 60 s the bridge logs a warning; command age checks may then reject commands until the clock is right (visible as `too_old` or `from_the_future`)"), §8 row "Radio clock wrong" ("command rejections `too_old` / `from_the_future`"), `docs/deployment.md` troubleshooting row "Commands rejected with `too_old` / `from_the_future`" ("Sender or bridge clock wrong; the phone apps set the radio clock"), `session._sync_clock`'s warning text |
| Related | SEC-002 |

**Description.** `core/commands._check_time` compares the *sender's* timestamp (set by the
sender's device) with the *gateway host's* wall clock (`bridge._decide` passes `self._wall()`).
The gateway's radio's clock is not an input. A radio that is ahead has no effect on the check; a
host that is wrong does. The troubleshooting row is half right ("bridge clock wrong") and half
misleading ("the phone apps set the radio clock" sets the *sender's* radio, which is right, but
the sentence is placed as if the gateway's radio mattered). The warning in `_sync_clock`
("command age checks may fail") repeats it in the journal.

**Suggested change.** In the three places: the age check uses the sender's clock (set by their
app) and the gateway host's clock; the gateway's radio's clock affects adverts and contact
ordering (SEC-002), not commands. Rewrite the warning: "radio clock is N s ahead and cannot be
set back: its adverts will carry that time".

---

### DOC-003 Stale statements after the first release

| | |
| --- | --- |
| Category | documentation (staleness) |
| Component | `README.md:79-81` ("Before the first release, use a pre-release's own installer … or `main`'s copy"), `docs/deployment.md:44-48` ("Before the first release there is nothing at that address; install the source of `main`"), `README.md:99-100` (the manual install names `meshcore-home-gateway-0.1.0.pyz`), the GitHub repository description ("A simple Meshcore producer for MQTT"), `README.md:61` ("Status: Version 1.0" while 1.1.0a4 is the newest pre-release: correct as a status, but the paragraph then lists what 1.1 adds) |
| Related | none |

**Description.** 1.0.0 was published on 2026-10-30; `releases/latest/download/…` serves it.
The two "before the first release" paragraphs now describe a situation that no longer exists and
send a reader to `main`'s unsigned installer instead of the signed release. The manual install
example's version (0.1.0) predates 1.0.0. The repository description on GitHub predates the
project's name and purpose.

**Suggested change.** Remove the two paragraphs (or turn them into "to install a pre-release
…"); write the example with `X.Y.Z`; update the GitHub description to the README's first
sentence; add "update the version in the README's manual install" to the release checklist in
`docs/development.md` (or use a placeholder and say so).

---

### DOC-004 Retention of heard nodes: 7 days in ADR 0016 and `mqtt-api.md`, 30 days in the history

| | |
| --- | --- |
| Category | documentation (accuracy; a hidden setting) |
| Component | ADR 0016 ("sightings 7 days"), `docs/mqtt-api.md` "Nodes heard" ("the last 50 of the last 7 days"), `history.Retention.nodes_days = 30.0` (not set by `cli._serve`, not in `[history]`, not in `docs/configuration.md`), `core/mesh.NODE_TTL_S = 7 days` |
| Related | SEC-010, SEC-015 |

**Description.** The in-memory heard nodes (what the `nodes` document shows) are forgotten after 7
days; their rows in the history stay 30 days and come back at a restart only while within 7 days
(`_take_heard_nodes` applies `forget_nodes`). So the document is right about 7 days, the ADR is
wrong about the file (30), and the 30 is a constant nobody can set or read about.

**Suggested change.** Decide (SEC-010 suggests 7 for both and a row cap), document `nodes_days`
in `[history]` if it stays a setting, and correct ADR 0016 with a dated note.

---

### DOC-005 The threat table and the residual risks do not cover the observer's new edges

| | |
| --- | --- |
| Category | documentation (completeness of the security section) |
| Component | `docs/design.md` §7.1–7.4 |
| Related | ARC-001, `02-threat-model.md` §7 (the comparison), SEC-001 to SEC-016 |

**Description.** The gaps are listed in the threat model's comparison table: forged
acknowledgements, the radio's clock, floods from the air (adverts, direct messages, public
channels), client-id squatting, Home Assistant's users as actors, mesh-controlled names, the
private key export over TCP, bystanders' data. §7.1's boundary list ("radio channel (untrusted)
→ radio (trusted firmware) → USB or TCP link → bridge → broker (authenticated clients) →
automation system") is the bridge's; the observer's boundaries (the broker as a writer, Home
Assistant's users, the operator's clock, the air as a source of load) are not drawn.

**Suggested change.** Adopt (or adapt) `02-threat-model.md` as `docs/threat-model.md`, keep §7 as
the summary, and add the missing rows to §7.2 with their mitigations as the team decides them.

---

### DOC-006 The security policy's "latest release" with a stream of pre-releases

| | |
| --- | --- |
| Category | documentation (policy clarity) |
| Component | `SECURITY.md` ("Only the latest release receives fixes") |
| Related | ADR 0022, `docs/development.md` Release |

**Description.** 1.0.0 is the latest release; 1.1.0a1–a4 are pre-releases that ship real
features and that the installer can install by `--release`. A fix for 1.0.0 would be 1.0.1; a fix
found in 1.1.0a4 would be 1.1.0a5. The sentence does not say which line gets fixes or whether a
pre-release user should expect them. The policy also does not say how long a report waits, or
whether the project publishes advisories (GitHub Security Advisories are available with private
vulnerability reporting, which the policy uses).

**Suggested change.** "Fixes go into the next pre-release of the current minor version and, for
the latest release, into a patch release when the fix is not already in a release; we aim to
acknowledge a report within N days; advisories are published on GitHub." Also name the versions
in scope explicitly at each release (the changelog could carry a "supported" line).

---

### DOC-007 Features the README states from the companion's side only

| | |
| --- | --- |
| Category | documentation (accuracy of a claim users rely on) |
| Component | `README.md` Features: "Telemetry of the household's companions: … asked of each in turn, within an airtime budget of its own, only from those who allow the gateway" |
| Related | SEC-016 |

**Description.** The gateway asks every chat contact that is not excluded; "only from those who
allow the gateway" is true of the answers, not of the asking, and the README's reader will not
guess that every neighbour they ever added is polled every 15 minutes (and tracked if their device
allows everyone). The paragraph does go on to mention exclusion and the first hour, so the fact is
there; the leading clause frames it the other way.

**Suggested change.** "asked of every companion on the radio that the household has not
excluded; a companion answers only if its owner allowed the gateway".

---

### DOC-008 The acknowledgement's meaning is not stated where users read `confirmed`

| | |
| --- | --- |
| Category | documentation (security consequence of a protocol fact) |
| Component | `docs/mqtt-api.md` "Delivery" (`confirmed`: "the recipient acknowledged; `rtt_ms` from the radio"), `docs/home-assistant.md` "See whether it arrived", `docs/protocol.md` §5 "ACK code" (states the formula, not the consequence) |
| Related | SEC-001 |

**Suggested change.** One sentence in each: what `confirmed` proves (a packet with the expected
code arrived) and when that is weaker than it sounds (a predictable text; SEC-001 option 1).

---

### DOC-009 What the TCP interface exposes

| | |
| --- | --- |
| Category | documentation (precision of a warning) |
| Component | `SECURITY.md`, `docs/design.md` §7.2 and §7.4, `docs/configuration.md` `[radio] host`, `docs/protocol.md` §1 |
| Related | SEC-003 |

**Suggested change.** Name the private key export and import in the release build, the absence of
forward secrecy, and the firmware's own advice to build without the flags; recommend a dedicated
network in words an operator can act on (a VLAN or a second SSID with client isolation; never the
household's WiFi).

---

### DOC-010 Bystanders' data has no privacy paragraph

| | |
| --- | --- |
| Category | documentation (privacy) |
| Component | `README.md` (no privacy section), `docs/design.md` (Q6 "Privacy: Message texts are logged only at debug level" is the only privacy requirement), `docs/home-assistant.md` "Privacy" (the household's data only) |
| Related | SEC-012, SEC-015, SEC-016 |

**Suggested change.** A "What the gateway collects" section in the README or `docs/design.md`:
for the household, for bystanders (names, keys, signal, public-channel texts), where each is
kept, for how long, how to turn it off; the same list in `docs/deployment.md`'s backup section
(what a backup contains).

---

### DOC-011 The design document's size and the missing diagrams

| | |
| --- | --- |
| Category | documentation (maintainability, readability) |
| Component | `docs/design.md` (1,280 lines; §6.6 and §6.11 are 100 and 170 lines of prose each; §8's table has 42 rows); the code's references to a "design 6c" with decision numbers that are not in the repository (CODE-006) |
| Related | CODE-005, CODE-006 |

**Description.** The document is the project's single source of truth and has grown by accretion
(each slice added a subsection). §6.11 (telemetry) describes a scheduler with lanes, routes,
back-off, states, events, persistence and the first-start hour in prose with numbers; there is
one sequence diagram and no state diagram. §6.6 (the observer and the advert check) is the same.
A contributor who must change the scheduler has the tests and this prose. The references in code
to "design 6c D13" show that a more structured document existed during the sprint.

**Suggested change.** Split `design.md` into a core document (goals, architecture, interfaces,
security, failure modes, testing) and one document per subsystem (`design-telemetry.md`,
`design-observer.md`, `design-home-assistant.md`) with the state and sequence diagrams; keep the
section numbers stable by a table of where each moved (the code and the ADRs cite them). Publish
the sprint design if it exists, or fold its numbered decisions into the subsystem documents so
that the code's references resolve.

---

### DOC-012 "Bridge" and "gateway"

| | |
| --- | --- |
| Category | documentation (terminology) |
| Component | The documents use "the bridge" 78 times and "the gateway" 316 times for the same program; `bridge.py` is the module; the README's first sentence says "gateway"; ADR 0012 renamed the project and kept "bridge" for the component |
| Related | ADR 0012 |

**Description.** A reader meets both words on the first page and has to learn that they are
one thing (the design even defines "the bridge is the piece in between"). ADR 0012's rationale
for "gateway" was the purpose; "bridge" survives as the older word.

**Suggested change.** Pick one for prose (gateway, the product's name) and keep "bridge" for the
module and class, with a one-line glossary in the README ("the gateway (its application module
is called the bridge)").

---

### DOC-013 A short way in is missing

| | |
| --- | --- |
| Category | documentation (onboarding) |
| Component | `README.md` (good; 180 lines), then `docs/design.md` (1,280 lines) or `docs/development.md` (394 lines, with a 25-line layout) |
| Related | DOC-011 |

**Description.** A contributor's path is README → `docs/development.md` → `docs/design.md`. There
is no 2–3 page "architecture overview" that names the eight tasks, the two queues, the event
types, the documents and where to look for what; `docs/development.md` "Layout" is a directory
listing. An operator's path (README → `docs/deployment.md`) is good. A security reviewer's path
(README → `SECURITY.md` → design §7) is good but stops short (DOC-005).

**Suggested change.** `docs/architecture.md` of about 150 lines: the diagram of `01-…` §2, the
flow of §5.1–5.2, the task list of design §5.2, the state inventory of §7, and a "where is …"
table. `01-system-understanding.md` of this review can serve as a draft.

---

### DOC-014 Smaller accuracy notes

| ID | Where | What | Suggested change |
| --- | --- | --- | --- |
| DOC-014 | `docs/design.md` §6.5 topic table | Lists 15 topics; `docs/mqtt-api.md` has 30 rows (settings, setting, `contacts/<key>/send`, `/received`, `add_contact`, the `homeassistant/*` requests, the discovery topics are missing in the design) | Either say "a subset; the full table is in mqtt-api.md" (it does say "The full API … is in mqtt-api.md" above the table) or regenerate the design's table from the same source |
| DOC-015 | `docs/configuration.md` `[history]` | `nodes_days` (30) is not documented and not settable (DOC-004) | Document or expose |
| DOC-016 | `docs/deployment.md` §4 "State and backup" | Says what `state.json` holds in one 15-line sentence; does not say the configuration backup contains the password, that `history.sqlite3` and `state.json` contain positions, or that a restored `state.json` replays command timestamps (that one is said) | Three short sentences (SEC-009, SEC-015) |
| DOC-017 | `docs/mqtt-api.md` "Securing the broker" | The ACL example is good; it does not say what happens without one in one sentence, nor that the gateway cannot tell (SEC-004) | One sentence |
| DOC-018 | `docs/home-assistant.md` Requirements | "QoS 1 for every entity from the device level since 2026.5.2" and three other version facts: valuable; a table of "gateway version → Home Assistant versions tested" is missing (ARC-016) | A table, kept per release |
| DOC-019 | `CONTRIBUTING.md` "Records are never edited after acceptance" | Six records carry dated amendments (ARC-008) | State the amendment practice |
| DOC-020 | `docs/testing.md` | Says the hardware checklist runs "before every release"; the changelog says it was not run for any 1.1 pre-release and 1.0.0 shipped rc1's program after the checklist: consistent, but a reader of the checklist alone does not learn that pre-releases are unchecked on hardware | A sentence at the top of the checklist |
| DOC-021 | `deploy/config.example.toml` | Has `telemetry` commented (`# enabled = true`) and `history` with one commented key; `contacts` is missing entirely (the `[contacts] manage` default is the one a shared household may want to change, SEC-008) | Add `[contacts]` with `# manage = true` and a comment |
| DOC-022 | `docs/protocol.md` §2, `SEND_TELEMETRY_REQ` | "the answer is the push 0x8B" and §5 explains the tag; neither says the request's tag is derived from the radio's clock (if it is: the reviewer did not verify) | Verify and state, since SEC-002 makes the clock matter |
| DOC-023 | `README.md` Security | "Prefer USB over the unauthenticated TCP companion interface" | Add "which exports the radio's private key to any client" (SEC-003) |
| DOC-024 | `docs/decisions/README.md` | Lists the records; the index has no status column (all accepted; two amended by later records) | A status column and "amended by" |
| DOC-025 | Issue forms | `bug_report.yml` asks for the version and the firmware; it does not ask for the transport (USB/TCP) or whether the broker has an ACL, which most security-relevant reports need | Two fields |
| DOC-026 | `docs/platform-notes.md` | 1,555 lines of evidence; the "Home Assistant MQTT" section alone is 450 lines; there is no index of the facts | A table of contents at the top with one line per fact |

## 3 What the documentation does well

* Every protocol fact has a firmware citation at a pinned tag, and a section lists where the
  firmware's own documentation is wrong (`docs/protocol.md` §8).
* Every platform claim has a "Verified / Here / Check" triple (`docs/platform-notes.md`): the
  reader can repeat the verification.
* The failure-mode table (design §8) and the hardware checklist (testing.md) are complete enough
  to be used as test plans.
* The compatibility policy says what is and is not interface, with examples of each.
* The changelog is written for users, with migration notes (telemetry after an update, the
  ACL line Home Assistant's user needs).
* The configuration reference documents every setting with default, range and meaning, and the
  example file matches it (tested).
* The guides say what the gateway *cannot* see (`docs/companions.md` "What the gateway cannot
  see"), which is rare and valuable.
