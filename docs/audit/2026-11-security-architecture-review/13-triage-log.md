# 13 Triage log

The decision the project team took on every item of the review, while working through
[issue #34](https://github.com/dkuenzel/meshcore-home-gateway/issues/34). One row per identifier,
so that no item is dropped silently. The review documents (`00-…` to `12-…`) stay as they were
written; this log is the only file of the folder that changes as the work goes on.

| Decision | Meaning |
| --- | --- |
| fix | Resolved by a change merged on `main`; *Reference* names the pull request |
| accept | The state is accepted as it is, with the reasoning; for a research note: acknowledged, and where its fact has lasting value, the document it moved to |
| reject | The suggested change is not taken, with the reasoning |
| defer | Not now; the reasoning names the condition or the milestone that brings it back |
| split | The item continues as its own issue |

*Reference* names the pull request that resolved the item once it is merged. While the work is
in progress it names the work package that will (`pending: <package>`); the packages are listed at
the end.

Decisions taken with the maintainer on 2026-11-02 that shape many rows:

* Hardening that the [compatibility policy](../../development.md#compatibility) counts as
  breaking (a changed default of `mqtt.client_id`, new rules between settings) is done now, with
  `**Breaking:**` entries in the changelog: **the next release is 2.0.0**.
* Defaults that touch privacy (published public channels, heard nodes, telemetry of every contact)
  get settings of their own; new installations get the privacy-friendly value from the installer;
  existing configurations keep their behaviour until the maintainer decides the default for 2.0.0.
* Improvement and extension opportunities are implemented where they can be built and verified
  here; what needs hardware, a platform or an account the project cannot test with, or a scope
  decision the records rule out, is deferred or rejected with the reason.
* Repository settings (immutable releases, code scanning, the description, labels) are changed
  where the review suggested it; a second GitHub identity for the agent and branch protection stay
  with the maintainer.

## Security findings

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| SEC-001 | fix | Document what `confirmed` proves; flag an acknowledgement whose round trip is shorter than the known path allows (`suspect`); an opt-in random suffix (`[messages] confirm_nonce`) makes the text, and so the code, unguessable. The protocol weakness itself is the firmware's (option 5: for the maintainer to raise upstream) | pending: acks |
| SEC-002 | fix | Host clock plausibility bounds before the write (the release date, ten years, and `[radio] set_clock_max_days`, default 30 days, 0 to force, instead of the planned `set_clock = "always"` and an hour: the firmware restarts a radio at its contacts' newest time, so an hour would refuse a radio unplugged for a night), the radio's clock offset in `state`, `After=time-sync.target`, recovery documented | [#39](https://github.com/dkuenzel/meshcore-home-gateway/pull/39) |
| SEC-003 | fix | Exact wording of the risk (key export and import, no forward secrecy) everywhere the TCP interface is mentioned; a TCP radio now needs `[radio] tcp_unauthenticated = true` (**Breaking**); detection of a contested link from the radio's uptime; the installer warns | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| SEC-004 | fix | Warnings at start and in `check-config` for a broker without credentials or TLS; an anonymous-access probe in `check-config --broker`; commands over such a broker need `[mqtt] insecure = true` (**Breaking**; a broker on the gateway's host or link needs no TLS, as the review proposed); the installer asks for TLS and confirms an empty user name | [#40](https://github.com/dkuenzel/meshcore-home-gateway/pull/40) |
| SEC-005 | fix | The gateway subscribes to its own documents and restores any it finds changed by another client (rate-limited, with a warning); the integrity model is stated. Stamps (`published_at`) are not added: they would change every document at every publish and a forger copies them | [#41](https://github.com/dkuenzel/meshcore-home-gateway/pull/41) |
| SEC-006 | fix | Announcements after a birth are never postponed past the first due time and come at most every 30 s; repeated births are logged; the default client id gets a random suffix per start (**Breaking**) | [#40](https://github.com/dkuenzel/meshcore-home-gateway/pull/40) |
| SEC-007 | fix | `name_taken` in the `nodes` document and the `node` event, a mark in the select's labels, contact devices of the same name disambiguated by their key digits; names compared after NFKC and case folding | pending: names |
| SEC-008 | fix | `[homeassistant] controls` separates Home Assistant's controls from the function (MQTT and the command line keep working); `[targets.<alias>] expose = false` hides a target's notify entity; the guide explains Home Assistant's user model | pending: ha-exposure |
| SEC-009 | fix | `[mqtt] password_file` keeps the secret out of `config.toml` and out of `config answers`; the installer writes it; a systemd credential is documented | pending: password |
| SEC-010 | fix | The history keeps node rows only for the contacts and the heard nodes the gateway keeps (the finding's option 3, tighter than a row cap), for 7 days as ADR 0016 says. No `[history] nodes_days` setting, as first planned: beyond the 7 days of the memory it would change nothing | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| SEC-011 | fix | The `nodes` document coalesced (at most every 30 s while only heard nodes change); no table read after a `NEW_ADVERT` on firmware that never uses it for a contact; once-heard nodes evicted before nodes heard again | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| SEC-012 | fix | `[messages] publish_channels = "targets"` publishes only the channels the household configured (new installations get it; the default stays `all` until the 2.0.0 decision); a rate limit per channel; documented | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43), pending: privacy |
| SEC-013 | fix | A rate limit per sender on what a message produces (events, history), commands still judged; the residual risk (the radio acknowledges before the gateway sees anything) in the threat model. Extending the acknowledgement wait by the radio's queue is not done: its statistics are read once a minute | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43), pending: threat-model |
| SEC-014 | fix | Malformed pushes logged at most once a minute with a count; a run of them ends the session, which the reconnect resynchronises | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43) |
| SEC-015 | fix | A privacy section on what is kept about bystanders and for how long; heard nodes 7 days; `[contacts] heard_nodes` minimises or switches off what is kept of strangers. Redacting positions from the opt-in `rx` diagnostics is rejected: it would make the diagnostics wrong; documented instead | pending: privacy |
| SEC-016 | fix | `[telemetry] ask = "household"` asks only contacts the household included (protected contacts are); new installations get it from the installer; README states the gateway's side | pending: privacy |
| SEC-017 | fix | A section "Handling text from the mesh" in `docs/development.md`; the alias echoed in a rejected `delivery` capped at 64 characters; an opt-in `[messages] clean_received` | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43) |

## Architecture and design

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| ARC-001 | fix | `docs/threat-model.md` with the seven boundaries and the actors, design §7 as its summary, a boundary paragraph required in the record template | pending: threat-model |
| ARC-002 | fix | ADR 0028 states what a contact is trusted with; the household mark (SEC-016) and the exposure setting (SEC-008) are its mechanisms | pending: threat-model |
| ARC-003 | fix | A `ports` module with the radio events and the radio port the application uses; the architecture test forbids the application modules to import the adapters | pending: structure |
| ARC-004 | fix | One state store for `state.json`, loaded once, every section in memory, written once per change set and at once for the ledger | pending: state-store |
| ARC-005 | fix | The message budget's level is kept across restarts, never below what one long message needs, so the first alert after a restart still goes out at once; ADR 0008 names the radio's own limiter as the regulatory backstop | pending: outbox |
| ARC-006 | fix | Through SEC-005; the integrity model in ADR 0015 (dated note) and the threat model | [#41](https://github.com/dkuenzel/meshcore-home-gateway/pull/41) |
| ARC-007 | fix | Dated notes with today's sizes in ADR 0002 and ADR 0004; a module size that asks for a split in `CONTRIBUTING.md` | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| ARC-008 | fix | The amendment practice stated in ADR 0001 and `CONTRIBUTING.md`: dated amendments may narrow or correct a record, a reversal needs a new one | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| ARC-009 | fix | Wiring in one module, `bridge.py` split into outbox, inbox and announcer | pending: structure |
| ARC-010 | fix | The firmware behaviours the advert check relies on, each with its line, in `docs/protocol.md` §4, as a checklist for a firmware update; the firmware tests stay the guard. A push carrying the advert's flags is for the maintainer to request upstream | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| ARC-011 | accept | The machinery stays; the roadmap names the first language as its proof and a review of the installer's packs after it. The parser risk is covered by SUP-006 | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| ARC-012 | fix | A `health` section in `state` and a *Gateway degraded* problem sensor; observer errors counted and their log rate-limited | pending: robustness |
| ARC-013 | fix | The outgoing queue is kept across restarts and drained on stop within a longer stop timeout (ADR 0029 supersedes the non-goal) | pending: outbox |
| ARC-014 | fix | `[mqtt] protocol = "5"` as an option: message expiry, correlation data and user properties; 3.1.1 stays the default | pending: mqtt5 |
| ARC-015 | fix | `[homeassistant] contact_entity_prefix` for a second gateway; the guide shows one `person` for two trackers | pending: ha-exposure |
| ARC-016 | fix | A "tested with" table in `docs/home-assistant.md`, kept per release, and a checklist step against the newest Home Assistant. A CI job against an unpinned Home Assistant is rejected: it would install releases inside the cooldown the dependency policy keeps out | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| ARC-017 | fix | The eight choices recorded as paragraphs of design §11 | pending: threat-model |

## Code quality

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| CODE-001 | fix | `_next` lets the reader and the getter run once after a timeout before it gives up; the loop's lateness is measured and published | pending: robustness |
| CODE-002 | fix | The hourly purge runs in a thread on a connection of its own, in one transaction; the state store (ARC-004) bounds the writes of `state.json`; the loop lag (CODE-001) shows whether more is needed | pending: robustness, state-store |
| CODE-003 | fix | `slugs` pruned to the contacts the gateway still knows plus the most recently seen, `radio_profile` to the current radio and the two before | pending: state-store |
| CODE-004 | fix | By the single store: a section never re-reads the file | pending: state-store |
| CODE-005 | fix | `cli.py` becomes a package of command modules, `bridge.py` is split (ARC-009); state and sequence diagrams for the telemetry subsystem | pending: structure, threat-model |
| CODE-006 | fix | Every reference to the sprint's "design 6c" decisions replaced by the section or record that holds the rule; a test that design references in the code resolve | pending: code-quality |
| CODE-007 | fix | The `# fmt: skip` markers removed in one mechanical commit; the formatter's output is the style | pending: code-quality |
| CODE-008 | fix | Through ARC-012 (counters, rate-limited logs) | pending: robustness |
| CODE-009 | fix | Property-based tests of the parsers (Hypothesis), a stall test (CODE-001), flood tests (SEC-010, SEC-011). A mutation run is deferred: a one-off calibration, not a gate | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38), pending: code-quality, robustness |
| CODE-010 | fix | The `Bus` protocol moves to the ports module and `MqttBus` declares it | pending: structure |
| CODE-011 | fix | The cost noted in `refresh_channels`' docstring | pending: code-quality |
| CODE-012 | fix | A comment naming the radio's table as the bound | pending: code-quality |
| CODE-013 | fix | The purge in one transaction | pending: robustness |
| CODE-014 | fix | A test that `nan` and `inf` are refused with a readable message | pending: code-quality |
| CODE-015 | fix | `_toml_string` escapes line and paragraph separators and other invisible format characters | pending: code-quality |
| CODE-016 | accept | The lazy imports keep `--version` and the light commands fast; documented where they are | – |
| CODE-017 | fix | A comment naming the bound of the retry loop | pending: code-quality |
| CODE-018 | fix | Through SEC-011 | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| CODE-019 | accept | The adapter's own bound is the documented design | – |
| CODE-020 | accept | Deciding the language before parsing is required for English service logs; Ctrl-C as exit 1 is the documented failure | – |
| CODE-021 | fix | A comment in the installer that only constants and validated values may be interpolated into `bash -c` | pending: ops |
| CODE-022 | fix | `docs/development.md` names the release-critical tools and that they are reviewed as such | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| CODE-023 | fix | `tests/firmware/README.md` names each stand-in and why it exists | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| CODE-024 | accept | The reasoning in comments stays; new code refers to the design where the paragraph exists | – |
| CODE-025 | fix | Through ARC-013: the stop timeout's room is used for the drain | pending: outbox |

## Documentation

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| DOC-001 | fix | The direction of the gateway's own discovery topic is "both" | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-002 | fix | Design §6.1, §8 and the troubleshooting row name the sender's and the host's clocks; the warning text names the adverts | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37), [#39](https://github.com/dkuenzel/meshcore-home-gateway/pull/39) |
| DOC-003 | fix | The "before the first release" paragraphs and the 0.1.0 example go; the repository description follows the README | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37); the description: repository settings, 2026-11-02 |
| DOC-004 | fix | Heard nodes are kept 7 days in the history too, as ADR 0016 says (SEC-010) | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| DOC-005 | fix | Through ARC-001 | pending: threat-model |
| DOC-006 | fix | `SECURITY.md` says which versions get fixes, how soon a report is acknowledged and that advisories are published | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-007 | fix | The README says whom the gateway asks, and that a companion answers only if allowed | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-008 | fix | Through SEC-001: one sentence wherever `confirmed` is explained | pending: acks |
| DOC-009 | fix | Through SEC-003 | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| DOC-010 | fix | A "What the gateway keeps" section (household and bystanders, where, how long, how to switch it off) | pending: threat-model |
| DOC-011 | fix | The design document split into a core and one document per subsystem, with state and sequence diagrams and a table of where each section moved | pending: threat-model |
| DOC-012 | fix | "Gateway" in prose, "bridge" for the module, a one-line glossary | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-013 | fix | `docs/architecture.md`: the short way in | pending: threat-model |
| DOC-014 | fix | Design §6.5's table says that it is a subset and points to `docs/mqtt-api.md` | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-015 | fix | Documented, not exposed: `[history]` in `configuration.md` says which node rows are kept and for how long, and why it is no setting (SEC-010) | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| DOC-016 | fix | The backup section says what each file holds, that the password is in the configuration, that the state files belong together and to stop the service first | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-017 | fix | "Securing the broker" says what happens without an ACL and that the gateway cannot tell | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-018 | fix | Through ARC-016 | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-019 | fix | Through ARC-008 | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-020 | fix | The checklist says that pre-releases are not checked on hardware | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-021 | fix | `[contacts]` in the example configuration | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-022 | fix | Verified in the firmware: the tag is the radio's clock, made unique; stated in `docs/protocol.md` (RES-016) | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-023 | fix | Through SEC-003 | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| DOC-024 | fix | A status column with "amended by" in the record index | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-025 | fix | The bug form asks for the transport and whether the broker has an ACL | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-026 | fix | A table of contents of the facts at the top of `docs/platform-notes.md` | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |

## Operations and deployment

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| OPS-001 | fix | The installer installs `unattended-upgrades`; the guide says that the container is a Debian system that needs updates | pending: ops |
| OPS-002 | fix | The published installer pins the digests of the zipapp and the unit; the verified path comes first in the README; examples pin `--ref` by commit | pending: installer-integrity |
| OPS-003 | fix | A Home Assistant example that notifies when the gateway is unavailable (IMP-025). The host-side udev rule is deferred: it cannot be verified without a Proxmox host, and ADR 0006 keeps the host free of the gateway's files | pending: ha-exposure |
| OPS-004 | fix | The unit restarts with a growing delay instead of giving up after 20 failures; the guide's example detects the outage. An `OnFailure=` unit is rejected: the gateway itself is the channel it would need | pending: ops |
| OPS-005 | fix | `After=time-sync.target`, `MemoryMax=`, `@obsolete` in the system call filter; `IPAddressAllow=` documented as an option | pending: ops |
| OPS-006 | fix | Through ARC-013 | pending: outbox |
| OPS-007 | fix | A troubleshooting row, a warning at every connect on untested firmware, `manage_reason` in the `contacts` index, a turnaround target for new firmware (EXT-020) | pending: ops |
| OPS-008 | fix | The backup section (DOC-016) and a `backup` command (IMP-016) | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37), pending: cli-ops |
| OPS-009 | fix | A journald limit from the installer, free disk space in the health section, the `node` cap (SEC-010) | pending: ops, robustness |
| OPS-010 | fix | Message boxes before a TCP radio and before a broker without credentials or TLS; the summary names the posture | pending: ops |
| OPS-011 | fix | The health section (ARC-012) and a `status` command (IMP-016) | pending: robustness, cli-ops |
| OPS-012 | accept | An installation needs Debian's mirror; an offline installation is a feature nobody asked for | – |
| OPS-013 | fix | `apt-get clean` after the installation | pending: ops |
| OPS-014 | fix | A sentence on time zones in the operations section | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| OPS-015 | accept | Nothing to fix; the reconfigure section notes the radio's queue | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| OPS-016 | fix | The released unit names its version in a comment | pending: ops |
| OPS-017 | fix | A warning at start when the log level is `debug` | pending: robustness |
| OPS-018 | accept | Documented in the unit; only the passed-through device exists in the container | – |
| OPS-019 | accept | Documented with the full path | – |
| OPS-020 | accept | Nothing to change | – |
| OPS-021 | fix | The installer refuses a device another container already passes through | pending: ops |
| OPS-022 | fix | A `retire` command (IMP-016) | pending: cli-ops |

## Supply chain and release

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| SUP-001 | fix | `CODEOWNERS`, a reviewer's checklist in the pull request template and a note on the agent's inputs. A separate GitHub identity for the agent and required reviews are deferred to the maintainer (an account and a ruleset only the maintainer can create); a release cooldown is deferred with them | pending: supply-chain |
| SUP-002 | fix | The release workflow builds in a job with a minimal, hash-pinned environment, a rebuild on another runner image must give the same `SHA256SUMS`, and only then is it attested | pending: supply-chain |
| SUP-003 | fix | The installer's pinned digests (OPS-002); immutable releases switched on for the repository | pending: installer-integrity, github |
| SUP-004 | fix | `make lock` and Dependabot write the same form | pending: supply-chain |
| SUP-005 | fix | `pip-audit` against both lock files in CI (the Home Assistant lock warns); code scanning switched on; the 43 open alerts of the Home Assistant lock resolved | pending: supply-chain, github |
| SUP-006 | fix | Translations reviewed by a second speaker; security-relevant texts carry a translators' note | pending: supply-chain |
| SUP-007 | fix | The rulesets exported to `.github/rulesets/`; `SECURITY.md` names the repository's security settings | pending: supply-chain |

## Improvement opportunities

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| IMP-001 | fix | Through SEC-001 | pending: acks |
| IMP-002 | fix | Optional per-sender TOTP codes for commands, with an enrolment command | pending: totp |
| IMP-003 | defer | Incremental table reads would touch the full reads ADR 0009's look-alike memory relies on; after SEC-011 strangers' adverts no longer cause reads, so the gain is small. Back when contact re-reads show up as a load | – |
| IMP-004 | fix | Through SEC-012 | pending: privacy |
| IMP-005 | fix | Through SEC-016 | pending: privacy |
| IMP-006 | fix | Through SEC-002 | [#39](https://github.com/dkuenzel/meshcore-home-gateway/pull/39) |
| IMP-007 | fix | Through SEC-004 | [#40](https://github.com/dkuenzel/meshcore-home-gateway/pull/40) |
| IMP-008 | fix | Through SEC-005 (healing); stamps rejected there, an HMAC signature deferred until a consumer can verify it (EXT-012) | [#41](https://github.com/dkuenzel/meshcore-home-gateway/pull/41) |
| IMP-009 | fix | Through SEC-006 | [#40](https://github.com/dkuenzel/meshcore-home-gateway/pull/40) |
| IMP-010 | fix | Through SEC-007 | pending: names |
| IMP-011 | fix | Through SEC-008 | pending: ha-exposure |
| IMP-012 | fix | Through SEC-009 | pending: password |
| IMP-013 | fix | Through ARC-012 | pending: robustness |
| IMP-014 | fix | Through ARC-013 | pending: outbox |
| IMP-015 | fix | Through ARC-014 | pending: mqtt5 |
| IMP-016 | fix | `status`, `backup` and `retire` commands | pending: cli-ops |
| IMP-017 | fix | Through CODE-009 | pending: code-quality |
| IMP-018 | fix | Through SEC-012 and SEC-013 | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43) |
| IMP-019 | fix | Through SEC-010 and SEC-011 | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| IMP-020 | fix | Through CODE-001 and CODE-002 | pending: robustness |
| IMP-021 | fix | Through ARC-004 | pending: state-store |
| IMP-022 | fix | Through ARC-003, ARC-009 and CODE-005 | pending: structure |
| IMP-023 | fix | Digests, updates, journald limit, warnings (OPS-001, -002, -009, -010); the udev rule deferred (OPS-003) | pending: ops, installer-integrity |
| IMP-024 | fix | Through OPS-004 and OPS-005 | pending: ops |
| IMP-025 | fix | Outage examples and a paragraph for shared households in the guide | pending: ha-exposure |
| IMP-026 | fix | Through ARC-001 | pending: threat-model |
| IMP-027 | fix | Through SUP-001 (code owners, checklist); identity and cooldown deferred to the maintainer | pending: supply-chain |
| IMP-028 | fix | Through SUP-002 | pending: supply-chain |
| IMP-029 | fix | Through SUP-005 | pending: supply-chain |
| IMP-030 | fix | Through DOC-011, DOC-013 and CODE-006 | pending: threat-model, code-quality |
| IMP-031 | fix | Through ARC-005 | pending: outbox |
| IMP-032 | fix | A priority on requests (`alert` overtakes `normal`), per request or per target | pending: outbox |
| IMP-033 | fix | Through OPS-007 | pending: ops |
| IMP-034 | fix | Through OPS-007 | pending: ops |
| IMP-035 | reject | Superseded by IMP-014: an update keeps the queue, waiting for it to empty is no longer needed | – |
| IMP-036 | fix | Through OPS-017 | pending: robustness |
| IMP-037 | fix | Through SEC-010 | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| IMP-038 | fix | Through SEC-017 (opt-in, default off: texts stay data) | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43) |
| IMP-039 | fix | Through SEC-017 | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43) |
| IMP-040 | fix | Through SEC-014 | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43) |
| IMP-041 | fix | Through SEC-003 | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| IMP-042 | fix | Through SEC-003 | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| IMP-043 | fix | Through SUP-007 | pending: supply-chain |
| IMP-044 | fix | Through CODE-023 | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| IMP-045 | fix | Through CODE-007 | pending: code-quality |
| IMP-046 | fix | Through DOC-026 | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| IMP-047 | fix | `history export` as JSON lines or CSV | pending: cli-ops |
| IMP-048 | fix | Through OPS-013 | pending: ops |
| IMP-049 | fix | Reason codes as texts and the remove and telemetry controls on the dashboard. Heard repeaters on the map are rejected: the gateway keeps no stranger's position (ADR 0016) | pending: extensions |
| IMP-050 | fix | A setting compiled out of the firmware is reported as `disabled`, apart from `unsupported` | pending: ops |

## Extension opportunities

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| EXT-001 | defer | An add-on is a second deployment path without the systemd sandbox; it cannot be tested here without Home Assistant OS, and it revisits ADR 0002 and ADR 0006. Back with a test host and a record | – |
| EXT-002 | defer | PyPI needs an account and a trusted publisher, a Debian package and an image each a channel to support; the maintainer decides which to open | – |
| EXT-003 | defer | BLE needs a new runtime dependency (a record under ADR 0005), BlueZ in the container and hardware to test | – |
| EXT-004 | split | Issue #23; implemented there from the interface snapshot | [#23](https://github.com/dkuenzel/meshcore-home-gateway/issues/23), pending: extensions |
| EXT-005 | fix | Sensor nodes of the household as telemetry candidates (`[telemetry] ask_types`), under the household mark | pending: extensions |
| EXT-006 | defer | Logging in to repeaters makes the gateway hold another node's administration password; ADR 0014 keeps administration out of scope. Back with a record and a threat model for it | – |
| EXT-007 | defer | Traces cost airtime and the map of repeaters needs strangers' positions, which the gateway does not keep; each needs a design of its own | – |
| EXT-008 | reject | ADR 0017 keeps channels configuration-only because a channel's secret reaches everyone who holds it; passing secrets over MQTT would undo that | – |
| EXT-009 | fix | `[commands] reply`: a short direct answer to accepted commands, optionally to safe rejections, through the outbox and the budget | pending: extensions |
| EXT-010 | defer | Command 39 works in every tested firmware; command 50 is taken up when a firmware release deprecates 39 (the firmware tests of each new release show it) | – |
| EXT-011 | fix | A Homie 4 adapter, opt-in, fed by the same documents | pending: extensions |
| EXT-012 | defer | A web front end with vendored libraries is a project of its own (ADR 0021 "later") | – |
| EXT-013 | fix | A configuration for `mqtt2prometheus` in `contrib/` | pending: extensions |
| EXT-014 | defer | Coordinating two gateways is distributed-systems work for a concrete household; the cheap part (an entity prefix, one `person` for two trackers) is done with ARC-015 | – |
| EXT-015 | defer | Translated guides follow the first translated catalogs (ADR 0019) and need a speaker's review | – |
| EXT-016 | fix | A `doctor` command that runs the checks of the review's operational findings | pending: cli-ops |
| EXT-017 | fix | `[targets.<alias>] prefix` and `suffix` | pending: extensions |
| EXT-018 | fix | A script in the guide that sends and waits for `confirmed` | pending: extensions |
| EXT-019 | fix | `make firmware-play` | pending: extensions |
| EXT-020 | fix | A documented turnaround for a new firmware release | pending: ops |

## Research notes

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| RES-001 | accept | Acknowledged; the consequence moves to `docs/protocol.md` §5 with SEC-001 | pending: acks |
| RES-002 | accept | Acknowledged; already in `docs/protocol.md` §5, the clock consequence added with SEC-002 | [#39](https://github.com/dkuenzel/meshcore-home-gateway/pull/39) |
| RES-003 | accept | Acknowledged; the scale of the cost goes to design §7.4 | pending: threat-model |
| RES-004 | accept | Acknowledged; the table goes to `docs/protocol.md` §7 | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| RES-005 | accept | Acknowledged; turned into flood tests (SEC-010, SEC-011) | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| RES-006 | accept | Acknowledged; the privacy section refers to it (not legal advice) | pending: threat-model |
| RES-007 | accept | Acknowledged; moves to `docs/protocol.md` with SEC-002 | [#39](https://github.com/dkuenzel/meshcore-home-gateway/pull/39) |
| RES-008 | accept | Acknowledged; moves to `docs/protocol.md` §1 with SEC-003 | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| RES-009 | accept | Acknowledged; turned into a test (CODE-001) | pending: robustness |
| RES-010 | accept | Acknowledged; moves to `docs/platform-notes.md` with SEC-006 | [#40](https://github.com/dkuenzel/meshcore-home-gateway/pull/40) |
| RES-011 | accept | Acknowledged; moves to `docs/platform-notes.md` with SEC-008 | pending: ha-exposure |
| RES-012 | accept | Acknowledged; moves to `docs/platform-notes.md` with SUP-003 | pending: installer-integrity |
| RES-013 | accept | Acknowledged; moves to `docs/platform-notes.md` with OPS-004 and OPS-005 | pending: ops |
| RES-014 | accept | Acknowledged; moves to `docs/platform-notes.md` with ARC-014 | pending: mqtt5 |
| RES-015 | accept | Acknowledged; moves to `docs/protocol.md` §2 (IMP-003 deferred) | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| RES-016 | accept | Acknowledged; verified, moves to `docs/protocol.md` (DOC-022) | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| RES-017 | accept | Acknowledged; moves to `docs/protocol.md` §1 with SEC-003 | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| RES-018 | accept | Acknowledged; moves to `docs/protocol.md` §3 with IMP-041 | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| RES-019 | accept | Acknowledged; resolved by SUP-004 | pending: supply-chain |
| RES-020 | accept | Acknowledged; moves to `docs/platform-notes.md` with OPS-009 | pending: ops |
| RES-021 | accept | Acknowledged; `docs/platform-notes.md` covers these | – |
| RES-022 | accept | Acknowledged; nothing to change | – |
| RES-023 | accept | Acknowledged; the firmware tests show at each firmware update whether the upstream documentation changed | – |
| RES-024 | accept | Acknowledged | – |

## Work packages

Each package is one pull request (or a few, where one would be too large to review), titled with
the item IDs and referencing issue #34.

| Package | Items |
| --- | --- |
| docs-accuracy | Documentation that the code already supports, the records' notes, the research facts that move into the guides |
| floods | SEC-010, SEC-011, CODE-018, DOC-004, DOC-015, IMP-019, IMP-037 |
| clock | SEC-002, IMP-006, DOC-002 |
| broker | SEC-004, SEC-006, IMP-007, IMP-009 |
| documents | SEC-005, ARC-006, IMP-008 |
| tcp | SEC-003, DOC-009, DOC-023, IMP-041, IMP-042 |
| limits | SEC-012 (rate), SEC-013, SEC-014, SEC-017, IMP-018, IMP-038, IMP-039, IMP-040 |
| privacy | SEC-012, SEC-015, SEC-016, IMP-004, IMP-005 |
| ha-exposure | SEC-008, ARC-015, OPS-003, IMP-011, IMP-025 |
| names | SEC-007, IMP-010 |
| password | SEC-009, IMP-012 |
| acks | SEC-001, DOC-008, IMP-001 |
| robustness | CODE-001, CODE-002, CODE-008, CODE-013, ARC-012, OPS-011, OPS-017, IMP-013, IMP-020, IMP-036 |
| state-store | ARC-004, CODE-003, CODE-004, IMP-021 |
| outbox | ARC-005, ARC-013, CODE-025, OPS-006, IMP-014, IMP-031, IMP-032 |
| ops | The unit and the installer: OPS-001, -004, -005, -007, -009, -010, -013, -016, -021, CODE-021, IMP-023, -024, -033, -034, -048, -050, EXT-020 |
| installer-integrity | OPS-002, SUP-003, IMP-023 |
| supply-chain | SUP-001, -002, -004, -005, -006, -007, IMP-027, -028, -029, -043 |
| code-quality | CODE-006, -007, -009, -011, -012, -014, -015, -017, IMP-017, IMP-045 |
| structure | ARC-003, ARC-009, CODE-005, CODE-010, IMP-022 |
| cli-ops | IMP-016, IMP-047, OPS-008, OPS-011, OPS-022, EXT-016 |
| totp | IMP-002 |
| mqtt5 | ARC-014, IMP-015 |
| threat-model | ARC-001, ARC-002, ARC-017, DOC-005, DOC-010, DOC-011, DOC-013, IMP-026, IMP-030 |
| extensions | EXT-004, -005, -009, -011, -013, -017, -018, -019, IMP-049 |
| github | Repository settings: the description, immutable releases, code scanning, the label `audit-2026-11` |
