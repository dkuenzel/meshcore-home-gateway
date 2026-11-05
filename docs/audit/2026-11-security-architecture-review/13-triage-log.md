# 13 Triage log

The decision the project team took on every item of the review, while working through
[issue #34](https://github.com/dkuenzel/meshcore-home-gateway/issues/34). One row per identifier,
so that no item is dropped silently. The review documents (`00-…` to `12-…`) stay as they were
written; this log is the only file of the folder that changes as the work goes on.

**State on 2026-11-04: complete.** All 208 items have a decision and none is pending: 162 fixed by
pull requests merged on `main` (#37 to #64), 34 accepted, 10 deferred with the condition that
brings each back, 2 rejected.

| Decision | Meaning |
| --- | --- |
| fix | Resolved by a change merged on `main`; *Reference* names the pull request |
| accept | The state is accepted as it is, with the reasoning; for a research note: acknowledged, and where its fact has lasting value, the document it moved to |
| reject | The suggested change is not taken, with the reasoning |
| defer | Not now; the reasoning names the condition or the milestone that brings it back |
| split | The item continues as its own issue; once the issue is resolved, the row becomes *fix* and names the issue beside the pull request (EXT-004) |

*Reference* names the pull request that resolved the item, merged on `main`; a dash means that
nothing had to change. While the work was in progress it named the work package that would
(`pending: <package>`); the packages and their pull requests are listed at the end.

Decisions taken with the maintainer on 2026-11-02 that shape many rows:

* Hardening that the [compatibility policy](../../development.md#compatibility) counts as
  breaking (a changed default of `mqtt.client_id`, new rules between settings) is done now, with
  `**Breaking:**` entries in the changelog: **the next release is 2.0.0**.
* Defaults that touch privacy (published public channels, heard nodes, telemetry of every contact)
  get settings of their own; new installations get the privacy-friendly value from the installer;
  existing configurations keep their behaviour until the maintainer decides the default for 2.0.0.
  Decided on 2026-11-05: the privacy-friendly values are the defaults of 2.0.0 for every
  configuration ([#66](https://github.com/dkuenzel/meshcore-home-gateway/pull/66)).
* Improvement and extension opportunities are implemented where they can be built and verified
  here; what needs hardware, a platform or an account the project cannot test with, or a scope
  decision the records rule out, is deferred or rejected with the reason.
* Repository settings (immutable releases, code scanning, the description, labels) are changed
  where the review suggested it; a second GitHub identity for the agent and branch protection stay
  with the maintainer. On 2026-11-05 the maintainer added a ruleset for `main` and accepted the
  shared identity ([#67](https://github.com/dkuenzel/meshcore-home-gateway/pull/67)).

## Security findings

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| SEC-001 | fix | Document what `confirmed` proves; flag an acknowledgement whose round trip is shorter than the known path allows (`suspect`); an opt-in random suffix (`[messages] confirm_nonce`, 8 hex digits: 32 bits, as many as the code) makes the text, and so the code, unguessable; a firmware test shows the forgery and both mitigations. The protocol weakness itself is the firmware's (option 5: for the maintainer to raise upstream) | [#48](https://github.com/dkuenzel/meshcore-home-gateway/pull/48) |
| SEC-002 | fix | Host clock plausibility bounds before the write (the release date, ten years, and `[radio] set_clock_max_days`, default 30 days, 0 to force, instead of the planned `set_clock = "always"` and an hour: the firmware restarts a radio at its contacts' newest time, so an hour would refuse a radio unplugged for a night), the radio's clock offset in `state`, `After=time-sync.target`, recovery documented | [#39](https://github.com/dkuenzel/meshcore-home-gateway/pull/39) |
| SEC-003 | fix | Exact wording of the risk (key export and import, no forward secrecy) everywhere the TCP interface is mentioned; a TCP radio now needs `[radio] tcp_unauthenticated = true` (**Breaking**); detection of a contested link from the radio's uptime; the installer warns | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| SEC-004 | fix | Warnings at start and in `check-config` for a broker without credentials or TLS; an anonymous-access probe in `check-config --broker`; commands over such a broker need `[mqtt] insecure = true` (**Breaking**; a broker on the gateway's host or link needs no TLS, as the review proposed); the installer asks for TLS and confirms an empty user name | [#40](https://github.com/dkuenzel/meshcore-home-gateway/pull/40) |
| SEC-005 | fix | The gateway subscribes to its own documents and restores any it finds changed by another client (rate-limited, with a warning); the integrity model is stated. Stamps (`published_at`) are not added: they would change every document at every publish and a forger copies them | [#41](https://github.com/dkuenzel/meshcore-home-gateway/pull/41) |
| SEC-006 | fix | Announcements after a birth are never postponed past the first due time and come at most every 30 s; repeated births are logged; the default client id gets a random suffix per start (**Breaking**) | [#40](https://github.com/dkuenzel/meshcore-home-gateway/pull/40) |
| SEC-007 | fix | `name_taken` in the `nodes` document and the `node` event, a mark in the select's labels, contact devices of the same name disambiguated by their key digits; names compared after NFKC and case folding, with invisible characters left out and Cyrillic and Greek look-alike letters read as the Latin ones | [#47](https://github.com/dkuenzel/meshcore-home-gateway/pull/47) |
| SEC-008 | fix | `[homeassistant] controls = false` separates Home Assistant's controls from the function (MQTT and the command line keep working): they stay announced but always unavailable, which Home Assistant leaves out of its actions, because removing a disabled entity takes its whole device away; `[homeassistant] hidden_targets` (instead of a per-target `expose`, which the installer's answers could not carry) hides a target's notify entity; the guide explains Home Assistant's user model | [#44](https://github.com/dkuenzel/meshcore-home-gateway/pull/44) |
| SEC-009 | fix | `[mqtt] password_file` keeps the secret out of `config.toml` and out of `config answers`; the installer writes it; a systemd credential is documented | [#46](https://github.com/dkuenzel/meshcore-home-gateway/pull/46) |
| SEC-010 | fix | The history keeps node rows only for the contacts and the heard nodes the gateway keeps (the finding's option 3, tighter than a row cap), for 7 days as ADR 0016 says. No `[history] nodes_days` setting, as first planned: beyond the 7 days of the memory it would change nothing | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| SEC-011 | fix | The `nodes` document coalesced (at most every 30 s while only heard nodes change); no table read after a `NEW_ADVERT` on firmware that never uses it for a contact; once-heard nodes evicted before nodes heard again | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| SEC-012 | fix | `[messages] publish_channels = "targets"` publishes only the channels the household configured (the default since 2.0.0; before, new installations got it from the installer); a rate limit per channel; documented | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43), [#45](https://github.com/dkuenzel/meshcore-home-gateway/pull/45), [#66](https://github.com/dkuenzel/meshcore-home-gateway/pull/66) |
| SEC-013 | fix | A rate limit per sender on what a message produces (events, history), commands still judged; the residual risk (the radio acknowledges before the gateway sees anything) in the threat model. Extending the acknowledgement wait by the radio's queue is not done: its statistics are read once a minute | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43), [#61](https://github.com/dkuenzel/meshcore-home-gateway/pull/61) |
| SEC-014 | fix | Malformed pushes logged at most once a minute with a count; a run of them ends the session, which the reconnect resynchronises | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43) |
| SEC-015 | fix | A privacy section on what is kept about bystanders and for how long; heard nodes 7 days; `[contacts] heard_nodes` minimises (`keys`, the default since 2.0.0) or switches off what is kept of strangers. Redacting positions from the opt-in `rx` diagnostics is rejected: it would make the diagnostics wrong; documented instead | [#45](https://github.com/dkuenzel/meshcore-home-gateway/pull/45), [#66](https://github.com/dkuenzel/meshcore-home-gateway/pull/66) |
| SEC-016 | fix | `[telemetry] ask = "household"` asks only contacts the household included (protected contacts are), the default since 2.0.0; README states the gateway's side | [#45](https://github.com/dkuenzel/meshcore-home-gateway/pull/45), [#66](https://github.com/dkuenzel/meshcore-home-gateway/pull/66) |
| SEC-017 | fix | A section "Handling text from the mesh" in `docs/development.md`; the alias echoed in a rejected `delivery` capped at 64 characters; an opt-in `[messages] clean_received` | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43) |

## Architecture and design

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| ARC-001 | fix | `docs/threat-model.md` with the seven boundaries and the actors, design §7 as its summary, a boundary paragraph required in the record template | [#61](https://github.com/dkuenzel/meshcore-home-gateway/pull/61) |
| ARC-002 | fix | ADR 0029 states what a contact is trusted with; the household mark (SEC-016) and the exposure setting (SEC-008) are its mechanisms | [#61](https://github.com/dkuenzel/meshcore-home-gateway/pull/61) |
| ARC-003 | fix | A `ports` module with the radio events and the radio port the application uses; the architecture test forbids the application modules to import the adapters | [#57](https://github.com/dkuenzel/meshcore-home-gateway/pull/57) |
| ARC-004 | fix | One state store for `state.json`, loaded once, every section in memory, written once per change set at the status tick and at once for what a crash must not lose (the ledger, the kept documents and advert checks, the schedule after a flood, the profile's attempts) | [#50](https://github.com/dkuenzel/meshcore-home-gateway/pull/50) |
| ARC-005 | fix | The message budget's level is kept across restarts, never below what one long message needs, so the first alert after a restart still goes out at once; ADR 0008 names the radio's own limiter as the regulatory backstop | [#51](https://github.com/dkuenzel/meshcore-home-gateway/pull/51) |
| ARC-006 | fix | Through SEC-005; the integrity model in ADR 0015 (dated note) and the threat model | [#41](https://github.com/dkuenzel/meshcore-home-gateway/pull/41) |
| ARC-007 | fix | Dated notes with today's sizes in ADR 0002 and ADR 0004; a module size that asks for a split in `CONTRIBUTING.md` | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| ARC-008 | fix | The amendment practice stated in ADR 0001 and `CONTRIBUTING.md`: dated amendments may narrow or correct a record, a reversal needs a new one | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| ARC-009 | fix | Wiring in one module, `bridge.py` split into outbox, inbox and announcer | [#57](https://github.com/dkuenzel/meshcore-home-gateway/pull/57) |
| ARC-010 | fix | The firmware behaviours the advert check relies on, each with its line, in `docs/protocol.md` §4, as a checklist for a firmware update; the firmware tests stay the guard. A push carrying the advert's flags is for the maintainer to request upstream | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| ARC-011 | accept | The machinery stays; the roadmap names the first language as its proof and a review of the installer's packs after it. The parser risk is covered by SUP-006 | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| ARC-012 | fix | A `health` section in `state` and a *Gateway degraded* problem sensor; observer errors counted and their log rate-limited | [#49](https://github.com/dkuenzel/meshcore-home-gateway/pull/49) |
| ARC-013 | fix | The outgoing queue is kept across restarts (written at the stop, queued again at the start with each message's `id`, attempts and timestamp) and the acknowledgement in flight is awaited for up to 60 s within a stop timeout of 90 s; ADR 0028 supersedes the non-goal (the next free number: the threat model's record became 0029) | [#51](https://github.com/dkuenzel/meshcore-home-gateway/pull/51) |
| ARC-014 | fix | `[mqtt] protocol = "5"` as an option: message expiry, correlation data and user properties; 3.1.1 stays the default | [#60](https://github.com/dkuenzel/meshcore-home-gateway/pull/60) |
| ARC-015 | fix | `[homeassistant] contact_entity_prefix` for a second gateway; the guide shows one `person` for two trackers | [#44](https://github.com/dkuenzel/meshcore-home-gateway/pull/44) |
| ARC-016 | fix | A "tested with" table in `docs/home-assistant.md`, kept per release, and a checklist step against the newest Home Assistant. A CI job against an unpinned Home Assistant is rejected: it would install releases inside the cooldown the dependency policy keeps out | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| ARC-017 | fix | The eight choices recorded as paragraphs of design §11 | [#61](https://github.com/dkuenzel/meshcore-home-gateway/pull/61) |

## Code quality

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| CODE-001 | fix | `_next` lets the reader and the getter run once after a timeout before it gives up; the loop's lateness is measured and published | [#49](https://github.com/dkuenzel/meshcore-home-gateway/pull/49) |
| CODE-002 | fix | The hourly purge runs in a thread on a connection of its own, in one transaction; the state store (ARC-004) bounds the writes of `state.json`; the loop lag (CODE-001) shows whether more is needed | [#49](https://github.com/dkuenzel/meshcore-home-gateway/pull/49), [#50](https://github.com/dkuenzel/meshcore-home-gateway/pull/50) |
| CODE-003 | fix | `slugs` of contacts that left the table kept for 90 days and at most 1 000 (the newest), `radio_profile` for the current radio and the two saved before it | [#50](https://github.com/dkuenzel/meshcore-home-gateway/pull/50) |
| CODE-004 | fix | By the single store: a section never re-reads the file | [#50](https://github.com/dkuenzel/meshcore-home-gateway/pull/50) |
| CODE-005 | fix | `cli.py` becomes a package of command modules, `bridge.py` is split (ARC-009) and its tests follow its parts; the tests of the observer and of the telemetry poller stay one file each, as each specifies one module; state and sequence diagrams for the telemetry subsystem | [#57](https://github.com/dkuenzel/meshcore-home-gateway/pull/57), [#61](https://github.com/dkuenzel/meshcore-home-gateway/pull/61) |
| CODE-006 | fix | Every reference to the sprint's "design 6c" decisions replaced by the section or record that holds the rule; a test that design references in the code resolve | [#55](https://github.com/dkuenzel/meshcore-home-gateway/pull/55) |
| CODE-007 | fix | The `# fmt: skip` markers removed in one mechanical commit; the formatter's output is the style | [#55](https://github.com/dkuenzel/meshcore-home-gateway/pull/55) |
| CODE-008 | fix | Through ARC-012 (counters, rate-limited logs) | [#49](https://github.com/dkuenzel/meshcore-home-gateway/pull/49) |
| CODE-009 | fix | Property-based tests of the parsers (Hypothesis), a stall test (CODE-001), flood tests (SEC-010, SEC-011). A mutation run is deferred: a one-off calibration, not a gate | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38), [#49](https://github.com/dkuenzel/meshcore-home-gateway/pull/49), [#55](https://github.com/dkuenzel/meshcore-home-gateway/pull/55) |
| CODE-010 | fix | The `Bus` protocol moved to the ports module and `MqttBus` declares it | [#57](https://github.com/dkuenzel/meshcore-home-gateway/pull/57) |
| CODE-011 | fix | The cost noted in `refresh_channels`' docstring | [#55](https://github.com/dkuenzel/meshcore-home-gateway/pull/55) |
| CODE-012 | fix | A comment naming the radio's table as the bound | [#55](https://github.com/dkuenzel/meshcore-home-gateway/pull/55) |
| CODE-013 | fix | The purge in one transaction, as it is since [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38); now also in a thread of its own (CODE-002) | [#49](https://github.com/dkuenzel/meshcore-home-gateway/pull/49) |
| CODE-014 | fix | A test that `nan` and `inf` are refused with a readable message | [#55](https://github.com/dkuenzel/meshcore-home-gateway/pull/55) |
| CODE-015 | fix | `_toml_string` escapes line and paragraph separators and other invisible format characters | [#55](https://github.com/dkuenzel/meshcore-home-gateway/pull/55) |
| CODE-016 | accept | The lazy imports keep `--version` and the light commands fast; documented where they are | – |
| CODE-017 | fix | A comment naming the bound of the retry loop | [#55](https://github.com/dkuenzel/meshcore-home-gateway/pull/55) |
| CODE-018 | fix | Through SEC-011 | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| CODE-019 | accept | The adapter's own bound is the documented design | – |
| CODE-020 | accept | Deciding the language before parsing is required for English service logs; Ctrl-C as exit 1 is the documented failure | – |
| CODE-021 | fix | A comment in the installer that only constants and validated values may be interpolated into `bash -c` | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| CODE-022 | fix | `docs/development.md` names the release-critical tools and that they are reviewed as such | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| CODE-023 | fix | `tests/firmware/README.md` names each stand-in and why it exists | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| CODE-024 | accept | The reasoning in comments stays; new code refers to the design where the paragraph exists | – |
| CODE-025 | fix | Through ARC-013: the stop timeout (now 90 s) leaves room for the acknowledgement in flight | [#51](https://github.com/dkuenzel/meshcore-home-gateway/pull/51) |

## Documentation

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| DOC-001 | fix | The direction of the gateway's own discovery topic is "both" | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-002 | fix | Design §6.1, §8 and the troubleshooting row name the sender's and the host's clocks; the warning text names the adverts | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37), [#39](https://github.com/dkuenzel/meshcore-home-gateway/pull/39) |
| DOC-003 | fix | The "before the first release" paragraphs and the 0.1.0 example go; the repository description follows the README | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37); the description: repository settings, 2026-11-02 |
| DOC-004 | fix | Heard nodes are kept 7 days in the history too, as ADR 0016 says (SEC-010) | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| DOC-005 | fix | Through ARC-001 | [#61](https://github.com/dkuenzel/meshcore-home-gateway/pull/61) |
| DOC-006 | fix | `SECURITY.md` says which versions get fixes, how soon a report is acknowledged and that advisories are published | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-007 | fix | The README says whom the gateway asks, and that a companion answers only if allowed | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-008 | fix | Through SEC-001: one sentence wherever `confirmed` is explained | [#48](https://github.com/dkuenzel/meshcore-home-gateway/pull/48) |
| DOC-009 | fix | Through SEC-003 | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| DOC-010 | fix | A "What the gateway keeps" section (household and bystanders, where, how long, how to switch it off): `docs/privacy.md`, with SEC-015 | [#45](https://github.com/dkuenzel/meshcore-home-gateway/pull/45) |
| DOC-011 | fix | The design document split into a core and one document per subsystem, with state and sequence diagrams and a table of where each section moved | [#61](https://github.com/dkuenzel/meshcore-home-gateway/pull/61) |
| DOC-012 | fix | "Gateway" in prose, "bridge" for the module, a one-line glossary | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| DOC-013 | fix | `docs/architecture.md`: the short way in | [#61](https://github.com/dkuenzel/meshcore-home-gateway/pull/61) |
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
| OPS-001 | fix | The installer installs `unattended-upgrades`; the guide says that the container is a Debian system that needs updates | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| OPS-002 | fix | The published installer pins the digests of the zipapp and the unit; the verified path comes first in the README; examples pin `--ref` by commit | [#53](https://github.com/dkuenzel/meshcore-home-gateway/pull/53) |
| OPS-003 | fix | A Home Assistant example that notifies when the gateway is unavailable (IMP-025). The host-side udev rule is deferred: it cannot be verified without a Proxmox host, and ADR 0006 keeps the host free of the gateway's files | [#44](https://github.com/dkuenzel/meshcore-home-gateway/pull/44) |
| OPS-004 | fix | The unit restarts with a growing delay instead of giving up after 20 failures; the guide's example detects the outage. An `OnFailure=` unit is rejected: the gateway itself is the channel it would need | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| OPS-005 | fix | `After=time-sync.target`, `MemoryMax=`, `@obsolete` in the system call filter; `IPAddressAllow=` documented as an option | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| OPS-006 | fix | Through ARC-013 | [#51](https://github.com/dkuenzel/meshcore-home-gateway/pull/51) |
| OPS-007 | fix | A troubleshooting row, a warning at every connect on untested firmware, `manage_reason` in the `contacts` index, a turnaround target for new firmware (EXT-020) | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| OPS-008 | fix | The backup section (DOC-016) and a `backup` command (IMP-016) | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37), [#58](https://github.com/dkuenzel/meshcore-home-gateway/pull/58) |
| OPS-009 | fix | A journald limit from the installer, free disk space in the health section, the `node` cap (SEC-010) | [#49](https://github.com/dkuenzel/meshcore-home-gateway/pull/49), [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| OPS-010 | fix | A radio over TCP and a broker without a user name need a confirmation, and the TLS question says what crosses the network in the clear (SEC-003, SEC-004); the final message names the posture: TLS or not, the user name or none, a TCP radio without authentication | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| OPS-011 | fix | The health section (ARC-012) and a `status` command (IMP-016) | [#49](https://github.com/dkuenzel/meshcore-home-gateway/pull/49), [#58](https://github.com/dkuenzel/meshcore-home-gateway/pull/58) |
| OPS-012 | accept | An installation needs Debian's mirror; an offline installation is a feature nobody asked for | – |
| OPS-013 | fix | `apt-get clean` after the installation | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| OPS-014 | fix | A sentence on time zones in the operations section | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| OPS-015 | accept | Nothing to fix; the reconfigure section notes the radio's queue | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| OPS-016 | fix | The released unit names its version in a comment | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| OPS-017 | fix | A warning at start when the log level is `debug` | [#49](https://github.com/dkuenzel/meshcore-home-gateway/pull/49) |
| OPS-018 | accept | Documented in the unit; only the passed-through device exists in the container | – |
| OPS-019 | accept | Documented with the full path | – |
| OPS-020 | accept | Nothing to change | – |
| OPS-021 | fix | The installer refuses a device another container already passes through | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| OPS-022 | fix | A `retire` command (IMP-016) | [#58](https://github.com/dkuenzel/meshcore-home-gateway/pull/58) |

## Supply chain and release

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| SUP-001 | fix | `CODEOWNERS`, a reviewer's checklist in the pull request template and a note on the agent's inputs; a release candidate or a release is tagged a day after its version reached `main` (`release-due.yml`). A separate GitHub identity for the agent with required reviews is not set up: on 2026-11-05 the maintainer accepted the shared identity, as a mandatory review would make every change wait | [#54](https://github.com/dkuenzel/meshcore-home-gateway/pull/54), [#67](https://github.com/dkuenzel/meshcore-home-gateway/pull/67) |
| SUP-002 | fix | The release workflow builds in a job with a minimal, hash-pinned environment, a rebuild on another runner image must give the same `SHA256SUMS`, and only then is it attested | [#54](https://github.com/dkuenzel/meshcore-home-gateway/pull/54) |
| SUP-003 | fix | The installer's pinned digests (OPS-002); immutable releases switched on for the repository | [#53](https://github.com/dkuenzel/meshcore-home-gateway/pull/53); immutable releases: repository settings, 2026-11-02 |
| SUP-004 | fix | `make lock` and Dependabot write the same form | [#54](https://github.com/dkuenzel/meshcore-home-gateway/pull/54) |
| SUP-005 | fix | `pip-audit` against every lock file in CI (`make audit`; the Home Assistant lock warns); code scanning switched on; the 43 open alerts of the Home Assistant lock resolved by overrides (Pillow 13.0.0, certifi 2026.10.19, uv 0.13.4) | [#54](https://github.com/dkuenzel/meshcore-home-gateway/pull/54); code scanning: repository settings, 2026-11-02 |
| SUP-006 | fix | Translations reviewed by a second speaker; security-relevant texts carry a translators' note | [#54](https://github.com/dkuenzel/meshcore-home-gateway/pull/54) |
| SUP-007 | fix | The tag ruleset exported to `.github/rulesets/`; `SECURITY.md` names the repository's security settings; `main` has a ruleset since 2026-11-05 (pull requests and squash merges only, the CI jobs required, no force push or deletion), exported beside it | [#54](https://github.com/dkuenzel/meshcore-home-gateway/pull/54), [#67](https://github.com/dkuenzel/meshcore-home-gateway/pull/67) |

## Improvement opportunities

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| IMP-001 | fix | Through SEC-001 | [#48](https://github.com/dkuenzel/meshcore-home-gateway/pull/48) |
| IMP-002 | fix | Optional per-sender TOTP codes for commands, with an enrolment command | [#59](https://github.com/dkuenzel/meshcore-home-gateway/pull/59) |
| IMP-003 | defer | Incremental table reads would touch the full reads ADR 0009's look-alike memory relies on; after SEC-011 strangers' adverts no longer cause reads, so the gain is small. Back when contact re-reads show up as a load | – |
| IMP-004 | fix | Through SEC-012 | [#45](https://github.com/dkuenzel/meshcore-home-gateway/pull/45) |
| IMP-005 | fix | Through SEC-016 | [#45](https://github.com/dkuenzel/meshcore-home-gateway/pull/45) |
| IMP-006 | fix | Through SEC-002 | [#39](https://github.com/dkuenzel/meshcore-home-gateway/pull/39) |
| IMP-007 | fix | Through SEC-004 | [#40](https://github.com/dkuenzel/meshcore-home-gateway/pull/40) |
| IMP-008 | fix | Through SEC-005 (healing); stamps rejected there, an HMAC signature deferred until a consumer can verify it (EXT-012) | [#41](https://github.com/dkuenzel/meshcore-home-gateway/pull/41) |
| IMP-009 | fix | Through SEC-006 | [#40](https://github.com/dkuenzel/meshcore-home-gateway/pull/40) |
| IMP-010 | fix | Through SEC-007 | [#47](https://github.com/dkuenzel/meshcore-home-gateway/pull/47) |
| IMP-011 | fix | Through SEC-008 | [#44](https://github.com/dkuenzel/meshcore-home-gateway/pull/44) |
| IMP-012 | fix | Through SEC-009 | [#46](https://github.com/dkuenzel/meshcore-home-gateway/pull/46) |
| IMP-013 | fix | Through ARC-012 | [#49](https://github.com/dkuenzel/meshcore-home-gateway/pull/49) |
| IMP-014 | fix | Through ARC-013 | [#51](https://github.com/dkuenzel/meshcore-home-gateway/pull/51) |
| IMP-015 | fix | Through ARC-014 | [#60](https://github.com/dkuenzel/meshcore-home-gateway/pull/60) |
| IMP-016 | fix | `status`, `backup` and `retire` commands | [#58](https://github.com/dkuenzel/meshcore-home-gateway/pull/58) |
| IMP-017 | fix | Through CODE-009 | [#55](https://github.com/dkuenzel/meshcore-home-gateway/pull/55) |
| IMP-018 | fix | Through SEC-012 and SEC-013 | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43) |
| IMP-019 | fix | Through SEC-010 and SEC-011 | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| IMP-020 | fix | Through CODE-001 and CODE-002 | [#49](https://github.com/dkuenzel/meshcore-home-gateway/pull/49) |
| IMP-021 | fix | Through ARC-004 | [#50](https://github.com/dkuenzel/meshcore-home-gateway/pull/50) |
| IMP-022 | fix | Through ARC-003, ARC-009 and CODE-005 | [#57](https://github.com/dkuenzel/meshcore-home-gateway/pull/57) |
| IMP-023 | fix | Digests, updates, journald limit, warnings (OPS-001, -002, -009, -010); the udev rule deferred (OPS-003) | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52), [#53](https://github.com/dkuenzel/meshcore-home-gateway/pull/53) |
| IMP-024 | fix | Through OPS-004 and OPS-005 | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| IMP-025 | fix | Outage examples and a paragraph for shared households in the guide | [#44](https://github.com/dkuenzel/meshcore-home-gateway/pull/44) |
| IMP-026 | fix | Through ARC-001 | [#61](https://github.com/dkuenzel/meshcore-home-gateway/pull/61) |
| IMP-027 | fix | Through SUP-001 (code owners, checklist, the release cooldown; the shared identity accepted) | [#54](https://github.com/dkuenzel/meshcore-home-gateway/pull/54), [#67](https://github.com/dkuenzel/meshcore-home-gateway/pull/67) |
| IMP-028 | fix | Through SUP-002 | [#54](https://github.com/dkuenzel/meshcore-home-gateway/pull/54) |
| IMP-029 | fix | Through SUP-005 | [#54](https://github.com/dkuenzel/meshcore-home-gateway/pull/54) |
| IMP-030 | fix | Through DOC-011, DOC-013 and CODE-006 | [#55](https://github.com/dkuenzel/meshcore-home-gateway/pull/55), [#61](https://github.com/dkuenzel/meshcore-home-gateway/pull/61) |
| IMP-031 | fix | Through ARC-005 | [#51](https://github.com/dkuenzel/meshcore-home-gateway/pull/51) |
| IMP-032 | fix | `[messages] alert_targets`: alerts overtake the queue and go between two attempts of a message in flight. Per target, not per request: a JSON envelope would change what a text that looks like JSON means, and the installer's answers carry no per-target setting (as for `hidden_targets`, SEC-008); a second target for the same recipient gives a request the choice | [#51](https://github.com/dkuenzel/meshcore-home-gateway/pull/51) |
| IMP-033 | fix | Through OPS-007 | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| IMP-034 | fix | Through OPS-007 | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| IMP-035 | reject | Superseded by IMP-014: an update keeps the queue, waiting for it to empty is no longer needed | – |
| IMP-036 | fix | Through OPS-017 | [#49](https://github.com/dkuenzel/meshcore-home-gateway/pull/49) |
| IMP-037 | fix | Through SEC-010 | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| IMP-038 | fix | Through SEC-017 (opt-in, default off: texts stay data) | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43) |
| IMP-039 | fix | Through SEC-017 | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43) |
| IMP-040 | fix | Through SEC-014 | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43) |
| IMP-041 | fix | Through SEC-003 | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| IMP-042 | fix | Through SEC-003 | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| IMP-043 | fix | Through SUP-007 | [#54](https://github.com/dkuenzel/meshcore-home-gateway/pull/54) |
| IMP-044 | fix | Through CODE-023 | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| IMP-045 | fix | Through CODE-007 | [#55](https://github.com/dkuenzel/meshcore-home-gateway/pull/55) |
| IMP-046 | fix | Through DOC-026 | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| IMP-047 | fix | `history export` as JSON lines or CSV | [#58](https://github.com/dkuenzel/meshcore-home-gateway/pull/58) |
| IMP-048 | fix | Through OPS-013 | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| IMP-049 | fix | Reason codes as texts and the remove and telemetry controls on the dashboard. Heard repeaters on the map are rejected: the gateway keeps no stranger's position (ADR 0016) | [#63](https://github.com/dkuenzel/meshcore-home-gateway/pull/63) |
| IMP-050 | fix | A setting compiled out of the firmware is reported as `disabled`, apart from `unsupported` | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |

## Extension opportunities

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| EXT-001 | defer | An add-on is a second deployment path without the systemd sandbox; it cannot be tested here without Home Assistant OS, and it revisits ADR 0002 and ADR 0006. Back with a test host and a record | – |
| EXT-002 | defer | PyPI needs an account and a trusted publisher, a Debian package and an image each a channel to support; on 2026-11-05 the maintainer decided to open none for now | – |
| EXT-003 | defer | BLE needs a new runtime dependency (a record under ADR 0005), BlueZ in the container and hardware to test | – |
| EXT-004 | fix | Split into issue #23 and resolved there: `docs/mqtt-api.asyncapi.yaml`, an AsyncAPI 3.1 document that `tools/asyncapi.py` generates from the interface snapshot (`make interface`), tested against the snapshot and published with each release | [#23](https://github.com/dkuenzel/meshcore-home-gateway/issues/23), [#64](https://github.com/dkuenzel/meshcore-home-gateway/pull/64) |
| EXT-005 | fix | Sensor nodes (MeshCore's sensor firmware) as telemetry candidates: with `"sensor"` in `[telemetry] ask_types` (companions only by default) they are asked as companions are, exclusions, inclusions, `ask`, switch and state alike; `docs/companions.md` (Sensors) says that a sensor answers only a radio in its access list | [#63](https://github.com/dkuenzel/meshcore-home-gateway/pull/63) |
| EXT-006 | defer | Logging in to repeaters makes the gateway hold another node's administration password; ADR 0014 keeps administration out of scope. Back with a record and a threat model for it | – |
| EXT-007 | defer | Traces cost airtime and the map of repeaters needs strangers' positions, which the gateway does not keep; each needs a design of its own | – |
| EXT-008 | reject | ADR 0017 keeps channels configuration-only because a channel's secret reaches everyone who holds it; passing secrets over MQTT would undo that | – |
| EXT-009 | fix | `[commands] reply`: a short direct answer to accepted commands, optionally to safe rejections, through the outbox and the budget | [#62](https://github.com/dkuenzel/meshcore-home-gateway/pull/62) |
| EXT-010 | defer | Command 39 works in every tested firmware; command 50 is taken up when a firmware release deprecates 39 (the firmware tests of each new release show it) | – |
| EXT-011 | fix | A Homie 4 adapter, opt-in (`[homie] enabled`), fed by the same documents: one device whose nodes are the contacts (at most `[homie] contacts`), on a connection of its own whose last will marks the device `lost` (ADR 0015 amended) | [#64](https://github.com/dkuenzel/meshcore-home-gateway/pull/64) |
| EXT-012 | defer | A web front end with vendored libraries is a project of its own (ADR 0021 "later") | – |
| EXT-013 | fix | Two configurations for `mqtt2prometheus` in `contrib/`: the gateway's `state` and `stats`, and each contact's document; `tests/test_contrib.py` checks every field they read against the gateway's documents | [#64](https://github.com/dkuenzel/meshcore-home-gateway/pull/64) |
| EXT-014 | defer | Coordinating two gateways is distributed-systems work for a concrete household; the cheap part (an entity prefix, one `person` for two trackers) is done with ARC-015 | – |
| EXT-015 | defer | Translated guides follow the first translated catalogs (ADR 0019) and need a speaker's review | – |
| EXT-016 | fix | A `doctor` command that runs the checks of the review's operational findings | [#58](https://github.com/dkuenzel/meshcore-home-gateway/pull/58) |
| EXT-017 | fix | `[targets.<alias>] prefix` and `suffix` | [#62](https://github.com/dkuenzel/meshcore-home-gateway/pull/62) |
| EXT-018 | fix | A script in the guide that sends and waits for `confirmed` | [#62](https://github.com/dkuenzel/meshcore-home-gateway/pull/62) |
| EXT-019 | fix | `make firmware-play`: the household of the hardware checklist on the simulated air, with a broker and the gateway, kept running to explore with an MQTT client | [#64](https://github.com/dkuenzel/meshcore-home-gateway/pull/64) |
| EXT-020 | fix | A documented turnaround for a new firmware release | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |

## Research notes

| ID | Decision | Reasoning | Reference |
| --- | --- | --- | --- |
| RES-001 | accept | Acknowledged; the consequence moved to `docs/protocol.md` §5 with SEC-001 | [#48](https://github.com/dkuenzel/meshcore-home-gateway/pull/48) |
| RES-002 | accept | Acknowledged; already in `docs/protocol.md` §5, the clock consequence added with SEC-002 | [#39](https://github.com/dkuenzel/meshcore-home-gateway/pull/39) |
| RES-003 | accept | Acknowledged; the scale of the cost went to design §7.4 | [#61](https://github.com/dkuenzel/meshcore-home-gateway/pull/61) |
| RES-004 | accept | Acknowledged; the table went to `docs/protocol.md` §7 | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| RES-005 | accept | Acknowledged; turned into flood tests (SEC-010, SEC-011) | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| RES-006 | accept | Acknowledged; the privacy section refers to it (not legal advice) | [#61](https://github.com/dkuenzel/meshcore-home-gateway/pull/61) |
| RES-007 | accept | Acknowledged; moved to `docs/protocol.md` with SEC-002 | [#39](https://github.com/dkuenzel/meshcore-home-gateway/pull/39) |
| RES-008 | accept | Acknowledged; moved to `docs/protocol.md` §1 with SEC-003 | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| RES-009 | accept | Acknowledged; turned into a test (CODE-001) | [#49](https://github.com/dkuenzel/meshcore-home-gateway/pull/49) |
| RES-010 | accept | Acknowledged; moved to `docs/platform-notes.md` with SEC-006 | [#40](https://github.com/dkuenzel/meshcore-home-gateway/pull/40) |
| RES-011 | accept | Acknowledged; moved to `docs/platform-notes.md` with SEC-008 | [#44](https://github.com/dkuenzel/meshcore-home-gateway/pull/44) |
| RES-012 | accept | Acknowledged; moved to `docs/platform-notes.md` with SUP-003 | [#53](https://github.com/dkuenzel/meshcore-home-gateway/pull/53) |
| RES-013 | accept | Acknowledged; moved to `docs/platform-notes.md` with OPS-004 and OPS-005 | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| RES-014 | accept | Acknowledged; moved to `docs/platform-notes.md` with ARC-014 | [#60](https://github.com/dkuenzel/meshcore-home-gateway/pull/60) |
| RES-015 | accept | Acknowledged; moved to `docs/protocol.md` §2 (IMP-003 deferred) | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| RES-016 | accept | Acknowledged; verified, moved to `docs/protocol.md` (DOC-022) | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| RES-017 | accept | Acknowledged; moved to `docs/protocol.md` §1 with SEC-003 | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| RES-018 | accept | Acknowledged; moved to `docs/protocol.md` §3 with IMP-041 | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| RES-019 | accept | Acknowledged; resolved by SUP-004 | [#54](https://github.com/dkuenzel/meshcore-home-gateway/pull/54) |
| RES-020 | accept | Acknowledged; moved to `docs/platform-notes.md` with OPS-009 | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| RES-021 | accept | Acknowledged; `docs/platform-notes.md` covers these | – |
| RES-022 | accept | Acknowledged; nothing to change | – |
| RES-023 | accept | Acknowledged; the firmware tests show at each firmware update whether the upstream documentation changed | – |
| RES-024 | accept | Acknowledged | – |

## Work packages

Each package was one pull request (or a few, where one would have been too large to review),
titled with the item IDs and referencing issue #34, and all of them are merged. *Items* is the
plan; where a row names a further pull request (a research fact that went into the same change,
an item that a later change touched again), the row is the record.

| Package | Items | Pull requests |
| --- | --- | --- |
| docs-accuracy | Documentation that the code already supports, the records' notes, the research facts that moved into the guides | [#37](https://github.com/dkuenzel/meshcore-home-gateway/pull/37) |
| floods | SEC-010, SEC-011, CODE-018, DOC-004, DOC-015, IMP-019, IMP-037 | [#38](https://github.com/dkuenzel/meshcore-home-gateway/pull/38) |
| clock | SEC-002, IMP-006, DOC-002 | [#39](https://github.com/dkuenzel/meshcore-home-gateway/pull/39) |
| broker | SEC-004, SEC-006, IMP-007, IMP-009 | [#40](https://github.com/dkuenzel/meshcore-home-gateway/pull/40) |
| documents | SEC-005, ARC-006, IMP-008 | [#41](https://github.com/dkuenzel/meshcore-home-gateway/pull/41) |
| tcp | SEC-003, DOC-009, DOC-023, IMP-041, IMP-042 | [#42](https://github.com/dkuenzel/meshcore-home-gateway/pull/42) |
| limits | SEC-012 (rate), SEC-013, SEC-014, SEC-017, IMP-018, IMP-038, IMP-039, IMP-040 | [#43](https://github.com/dkuenzel/meshcore-home-gateway/pull/43) |
| privacy | SEC-012, SEC-015, SEC-016, IMP-004, IMP-005, DOC-010 | [#45](https://github.com/dkuenzel/meshcore-home-gateway/pull/45) |
| ha-exposure | SEC-008, ARC-015, OPS-003, IMP-011, IMP-025 | [#44](https://github.com/dkuenzel/meshcore-home-gateway/pull/44) |
| names | SEC-007, IMP-010 | [#47](https://github.com/dkuenzel/meshcore-home-gateway/pull/47) |
| password | SEC-009, IMP-012 | [#46](https://github.com/dkuenzel/meshcore-home-gateway/pull/46) |
| acks | SEC-001, DOC-008, IMP-001 | [#48](https://github.com/dkuenzel/meshcore-home-gateway/pull/48) |
| robustness | CODE-001, CODE-002, CODE-008, CODE-013, ARC-012, OPS-011, OPS-017, IMP-013, IMP-020, IMP-036 | [#49](https://github.com/dkuenzel/meshcore-home-gateway/pull/49) |
| state-store | ARC-004, CODE-003, CODE-004, IMP-021 | [#50](https://github.com/dkuenzel/meshcore-home-gateway/pull/50) |
| outbox | ARC-005, ARC-013, CODE-025, OPS-006, IMP-014, IMP-031, IMP-032 | [#51](https://github.com/dkuenzel/meshcore-home-gateway/pull/51) |
| ops | The unit and the installer: OPS-001, -004, -005, -007, -009, -010, -013, -016, -021, CODE-021, IMP-023, -024, -033, -034, -048, -050, EXT-020 | [#52](https://github.com/dkuenzel/meshcore-home-gateway/pull/52) |
| installer-integrity | OPS-002, SUP-003, IMP-023 | [#53](https://github.com/dkuenzel/meshcore-home-gateway/pull/53) |
| supply-chain | SUP-001, -002, -004, -005, -006, -007, IMP-027, -028, -029, -043 | [#54](https://github.com/dkuenzel/meshcore-home-gateway/pull/54) |
| code-quality | CODE-006, -007, -009, -011, -012, -014, -015, -017, IMP-017, IMP-045 | [#55](https://github.com/dkuenzel/meshcore-home-gateway/pull/55) |
| structure | ARC-003, ARC-009, CODE-005, CODE-010, IMP-022 | [#57](https://github.com/dkuenzel/meshcore-home-gateway/pull/57) |
| cli-ops | IMP-016, IMP-047, OPS-008, OPS-011, OPS-022, EXT-016 | [#58](https://github.com/dkuenzel/meshcore-home-gateway/pull/58) |
| totp | IMP-002 | [#59](https://github.com/dkuenzel/meshcore-home-gateway/pull/59) |
| mqtt5 | ARC-014, IMP-015 | [#60](https://github.com/dkuenzel/meshcore-home-gateway/pull/60) |
| threat-model | ARC-001, ARC-002, ARC-017, DOC-005, DOC-011, DOC-013, IMP-026, IMP-030 | [#61](https://github.com/dkuenzel/meshcore-home-gateway/pull/61) |
| extensions | EXT-004, -005, -009, -011, -013, -017, -018, -019, IMP-049 | [#62](https://github.com/dkuenzel/meshcore-home-gateway/pull/62), [#63](https://github.com/dkuenzel/meshcore-home-gateway/pull/63), [#64](https://github.com/dkuenzel/meshcore-home-gateway/pull/64) |
| github | Repository settings: the description, immutable releases, code scanning, the label `audit-2026-11` | none: changed in the repository's settings, reported in [issue #34](https://github.com/dkuenzel/meshcore-home-gateway/issues/34#issuecomment-6580694408) |
