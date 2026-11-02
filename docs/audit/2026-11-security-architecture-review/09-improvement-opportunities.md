# 09 Improvement opportunities

Changes to what exists. Each item has a motivation and a design sketch concrete enough to start
from; where the sketch is already in a finding, the item summarises it and points there. Items
are `IMP-nnn`, grouped by area. No priority is implied by the order.

## A Security of the message and command paths

### IMP-001 Unguessable texts and a `suspect` flag for acknowledgements

| | |
| --- | --- |
| Related | SEC-001 (full design), DOC-008 |

**Motivation.** `confirmed` should mean that the recipient got the message; today it means that a
packet with a code anyone who knows the text could compute arrived.

**Design sketch.** `[messages] confirm_nonce = true` appends ` #xxxx` (16 bits from `secrets`) to
direct messages before the code is computed; the policy reserves the bytes; retries keep the
suffix; `delivery.text` carries the text as sent. Independently, a `confirmed` event gains
`suspect: true` when `rtt_ms` is below twice the message's time on air (the radio reports
`rtt_ms`; the airtime estimate exists) and the journal warns. Tests in SEC-001.

### IMP-002 An application-level second factor for commands

| | |
| --- | --- |
| Related | ADR 0009, SEC-001, SEC-003 (a forged frame over TCP), RES-003 |

**Motivation.** ADR 0009's authentication rests on the firmware's decryption plus the gateway's
prefix bookkeeping; it is sound but intricate, and it is defeated by whoever can hand the gateway
frames (a TCP impostor) or by a 48-bit collision the household then adds. A secret the gateway and
the sender share, used in the text, authenticates the *text* regardless of the transport or the
table.

**Design sketch.** Per sender an optional TOTP secret (`[commands.totp] <key> = "base32…"`, or a
file): a command is accepted only if its first word is a word and its last token is a valid
6-digit TOTP for that sender's secret within ±1 step (30 s, RFC 6238; `hmac` and `hashlib` are
in the standard library; the sender uses any authenticator app or a phone's clock). The code
also serves as the replay nonce (a code is accepted once: the ledger already stores the
timestamp; store the last code too). Senders without a secret keep today's rules. `args` is the
text between the word and the code. The guide shows how to enrol (a QR code for the
authenticator from the CLI: `meshcore-home-gateway commands enrol KEY` prints an `otpauth://`
URI as text; the dashboard's QR card can show it). Cost: six characters per command typed on a
handheld. For a household that uses commands for anything that matters, this is the measure
that makes the gateway independent of the prefix problem and of the transport.

### IMP-003 Incremental contact reads with `GET_CONTACTS since`

| | |
| --- | --- |
| Related | SEC-011, RES-015 |

**Motivation.** Every advert or path push re-reads the whole table (4.6 s and 52 KB for 350
contacts over USB-UART); the firmware can list only contacts changed since a time.

**Design sketch.** `session.refresh_contacts(incremental=True)` sends `GET_CONTACTS since =
newest last_modified seen` (the `END_OF_CONTACTS` frame carries it) and merges the records into
`contact_records`; removals are not listed, so a full read follows any `CONTACT_DELETED`,
`CONTACTS_FULL` or write by the gateway, and a full read runs at a floor rate (every 10 minutes)
and before any command decision (ADR 0009 relies on full reads for the look-alike memory; keep
that path full). The emulator implements `since` already (it answers `GET_CONTACTS`; check
`tests/radio_emulator.py` for the filter) and the firmware tests can verify the semantics
(`MyMesh.cpp:2397`, `lastmod > since`; note that `lastmod` is the radio's clock, SEC-002).

### IMP-004 Publish only the channels the household configured

| | |
| --- | --- |
| Related | SEC-012 |

**Design sketch.** `[messages] publish_channels = "targets" | "all"`; the installer's channel
checklist; a per-channel token bucket for `received`; see SEC-012.

### IMP-005 Telemetry opt-in per contact ("household" contacts)

| | |
| --- | --- |
| Related | SEC-016, ARC-002 |

**Design sketch.** `[telemetry] ask = "household" | "all"`; the *Telemetry* switch off by default
for new contacts; protected contacts household by default; the installer asks which contacts are
the household's; see SEC-016.

### IMP-006 Clock plausibility, a radio clock offset in `state`, `set_clock = "always"`

| | |
| --- | --- |
| Related | SEC-002 |

**Design sketch.** As SEC-002: a release-date constant as the lower bound, an hour as the jump
bound from a set clock, `always` to force, `radio_clock_offset_s` in the state and a diagnostic
sensor, the clock read at every heartbeat, `After=time-sync.target`.

### IMP-007 Insecure-broker warnings and an anonymous-access probe

| | |
| --- | --- |
| Related | SEC-004 |

**Design sketch.** Warnings at start and in `check-config` for no credentials, no TLS, commands
with either; `check-config --broker` opens a second, anonymous connection and reports whether the
broker accepted it, and whether an anonymous client could subscribe to `<root>/command`; the
installer's dialogs make the insecure choices explicit; `[mqtt] insecure = true` as a rule in the
next major version.

### IMP-008 Self-healing documents and origin stamps

| | |
| --- | --- |
| Related | SEC-005, ARC-006 |

**Design sketch.** Subscribe to the gateway's own document topics; on a payload that differs from
the last published one, republish and warn (rate-limited); `published_at` and `gateway` fields in
every document; an optional HMAC signature for consumers that can verify.

### IMP-009 A random client-id suffix and bounded announcements

| | |
| --- | --- |
| Related | SEC-006 |

**Design sketch.** `meshcore-home-gateway-<node_id>-<8 hex>` when `client_id` is unset (a
compatibility note: ACL patterns on `%c` should use the prefix); announcements never postponed
past the first due time and at most one per 30 s after the first; a warning on repeated births.

### IMP-010 Name collision flags

| | |
| --- | --- |
| Related | SEC-007 |

**Design sketch.** `name_taken` in `nodes` and the `node` event (NFKC + casefold comparison with
contacts, the radio, the device name and other nodes); the select's label gains ` (name taken)`;
a second contact of the same name gets `(c3d4…)` in its device name.

### IMP-011 Separate Home Assistant exposure from function

| | |
| --- | --- |
| Related | SEC-008 |

**Design sketch.** `[homeassistant] controls = "all" | "none"` (or three booleans), `[targets.<alias>]
expose = false`; removal of the components through the existing placeholder mechanism; a guide
paragraph on Home Assistant's user model and its policy file.

### IMP-012 A password file and a systemd credential

| | |
| --- | --- |
| Related | SEC-009 |

**Design sketch.** `[mqtt] password_file`, `LoadCredential=` in the unit, the installer writing the
credential file, `config answers` printing the path; see SEC-009.

## B Robustness and operations

### IMP-013 A health section in `state` and a *Gateway degraded* sensor

| | |
| --- | --- |
| Related | ARC-012, CODE-008, OPS-011, OPS-009 |

**Design sketch.** In `state`: `health: {history: "ok"|"broken"|"off", state_file: "ok"|"unwritable",
exclusions: "ok"|"unsaved"|"unreadable", observer_errors_1h: n, profile: "applied"|"failed"|
"observing", loop_lag_max_s: x, disk_free_mb: n, radio_clock_offset_s: n, radio_link_contested:
bool, events_dropped_1h: n, requests_refused_1h: n}`; a `binary_sensor` (problem) *Gateway
degraded* that is on when any field is not its good value, with the section as attributes. Each
subsystem already knows its state; the status loop publishes every 60 s.

### IMP-014 Persist the outbox across restarts, drain on stop

| | |
| --- | --- |
| Related | ARC-013, OPS-006 |

**Design sketch.** On stop, write the queued `Outgoing` entries (id, target, text, remaining time
to live) into `state.json` (`outbox`); on start, re-queue those with time left and publish
`queued` again with the same `id`; `TimeoutStopSec=90` and a drain that finishes the message in
flight (its acknowledgement wait) before the radio closes. The dedup memory is not persisted (a
duplicate after a restart is acceptable). Compatibility: a changed behaviour that the
documentation describes as a non-goal; a *Changed* entry.

### IMP-015 MQTT 5: message expiry, correlation, user properties

| | |
| --- | --- |
| Related | ARC-014 |

**Design sketch.** `[mqtt] protocol = "5"`: expiry of `queue_ttl_s` on `delivery`, `received` and
`command` (the command event could then be QoS 1 without arriving late); `response_topic` and
`correlation_data` honoured on requests and echoed on results; `user_properties` with the
gateway's name, version and `published_at` on documents. Keep 3.1.1 the default until the contract
tests cover Home Assistant on a 5-capable broker.

### IMP-016 Three CLI commands: `status`, `backup`, `retire`

| | |
| --- | --- |
| Related | OPS-011, OPS-008, OPS-022 |

**Design sketch.** `status` reads the retained `state`, `stats`, `contacts` and prints a human
summary (link, battery, airtime, queue, telemetry, profile, health, contacts count, last message
time). `backup FILE` writes a tarball of `config.toml` (with a `--with-password` switch;
without it the line is replaced by a comment), `state.json`, `telemetry-excluded.json` and a
consistent copy of `history.sqlite3` (`sqlite3.Connection.backup`), with a manifest naming the
version. `retire` publishes the removals of every announced Home Assistant device and component
(the placeholder dance), clears every retained document under `<root>/`, and publishes
`availability offline`; to be run before `pct destroy`.

### IMP-017 Property-based tests of the parsers

| | |
| --- | --- |
| Related | CODE-009 |

**Design sketch.** `hypothesis` in `requirements/dev.in`; `tests/test_fuzz.py` with strategies
`binary(max_size=176)` for `codec.decode`, `codec.decode_message`, `packet.parse_packet`,
`parse_advert`, `lpp.decode`; `text(max_size=1024)` for `links.parse_contact_text`,
`contacts.key_from_label`, `text.prepare_text`, `cut_utf8` (invariant: the result's UTF-8 length
is within the limit and is a prefix of the input modulo the ellipsis); `catalog` with generated
PO entries; `config.render_config` with generated `key=value` lines. The invariant for the
decoders: returns a value or raises the declared error type, within a deadline. Run in `make
check` with a fixed example database (`hypothesis`'s `.hypothesis/` directory ignored) and a
modest `max_examples` so CI stays fast.

### IMP-018 Rate limits on `received` per sender and per channel

| | |
| --- | --- |
| Related | SEC-012, SEC-013 |

**Design sketch.** `RateLimit` instances keyed by sender key and by channel index (a dict with
eviction of idle keys); beyond the limit the message is still judged (a command may be in it)
and recorded in a counter, but not published or stored; the log counts at most once a minute.
Defaults: 1/2 s with a burst of 10 per sender, 1/s with a burst of 20 per channel. Documented in
`docs/mqtt-api.md` next to the refusal limits.

### IMP-019 Bounded growth under advert floods

| | |
| --- | --- |
| Related | SEC-010, SEC-011 |

**Design sketch.** A row cap for the `node` table; the `nodes` document published at most every
10–30 s when only heard nodes changed; no table re-read on `NEW_ADVERT` for firmware ≥ 1.12;
once-heard nodes evicted before twice-heard ones.

### IMP-020 Loop-lag measurement and a grace in the radio timeout

| | |
| --- | --- |
| Related | CODE-001, CODE-002 |

**Design sketch.** The status loop measures its own lateness and records the maximum per hour
(`loop_lag_max_s` in health); `_next` yields twice after a timeout and re-checks for a frame;
the history's purge runs in a thread with its own connection and `busy_timeout`.

### IMP-021 One state store

| | |
| --- | --- |
| Related | ARC-004, CODE-003, CODE-004 |

**Design sketch.** `StateStore` loaded once in `cli._serve`, sections registered by name with a
validator each, `mark_dirty(section)` and one write per tick, `save_now()` for the ledger; prune
`slugs` and `radio_profile`; the per-class `load()` calls disappear.

### IMP-022 Structure: ports, a wiring module, smaller modules

| | |
| --- | --- |
| Related | ARC-003, ARC-009, CODE-005 |

**Design sketch.** `ports.py` with the radio event dataclasses and a `RadioPort` protocol;
`wiring.py` with the callback connections in one place (or a small typed signal class);
`bridge.py` split into `outbox.py`, `inbox.py`, `announcer.py`; `cli/` as a package; the
architecture test extended with "application modules import no adapter".

### IMP-023 Installer: pinned digests, updates, a udev rule, warnings, journald limits

| | |
| --- | --- |
| Related | OPS-001, OPS-002, OPS-003, OPS-009, OPS-010 |

**Design sketch.** `make release` writes `PYZ_SHA256` and `UNIT_SHA256` into the published
installer and `FETCH_PY` checks them; `install_service` installs `unattended-upgrades` and a
`journald.conf.d` limit; an optional `--host-udev-rule` writes a rule that re-passes the device
and starts the container on plug events; message boxes before a TCP radio, an empty user name
and no TLS; the summary names the posture.

### IMP-024 Unit: failure notification, memory limit, clock dependency

| | |
| --- | --- |
| Related | OPS-004, OPS-005 |

**Design sketch.** `OnFailure=meshcore-home-gateway-failed.service` (a unit that logs at `crit`
and runs an optional hook); `RestartSteps=`/`RestartMaxDelaySec=` instead of a hard start limit;
`MemoryMax=192M`; `After=time-sync.target`; `@obsolete` in the syscall filter.

### IMP-025 Guide examples for outages and shared households

| | |
| --- | --- |
| Related | OPS-003, OPS-004, SEC-008 |

**Design sketch.** In `docs/home-assistant.md` *Examples*: "notify when the gateway is
unavailable for 5 minutes" (state trigger on the radio link sensor to `unavailable` with `for`),
"notify when the radio is disconnected for 10 minutes", and a paragraph on dashboards per user
and the policy file for households that want to limit who sees the map.

## C Process and documentation

### IMP-026 A threat-model document and an ADR paragraph on boundaries

| | |
| --- | --- |
| Related | ARC-001, DOC-005 |

**Design sketch.** `docs/threat-model.md` from `02-threat-model.md` (adapted by the team); the
ADR template's *Consequences* gains "Boundaries and actors this touches; rows added to the threat
table"; design §7 becomes the summary that links to it.

### IMP-027 Release process: a second identity, a cooldown, code owners, a reviewer's checklist

| | |
| --- | --- |
| Related | SUP-001 |

**Design sketch.** As SUP-001: the agent as its own GitHub identity with required reviews on
`main`; releases (not pre-releases) tagged only from commits at least 24 hours old; `CODEOWNERS`
on the security-relevant paths; a checklist in the pull request template for agent-authored
changes; the agent's standing instructions in the repository.

### IMP-028 Build isolation and a rebuild job

| | |
| --- | --- |
| Related | SUP-002 |

**Design sketch.** `release.yml`: `check` (the tool chain, read-only) → `build` (a minimal
environment: `build`, `flit_core`, the standard library) → `rebuild` (another runner image,
compares `SHA256SUMS`) → `publish` (attests the `build` artifact only when `rebuild` agrees).

### IMP-029 Vulnerability scanning of the lock files

| | |
| --- | --- |
| Related | SUP-005 |

**Design sketch.** A CI job running `pip-audit --require-hashes -r requirements/dev.txt` (fails)
and the same for `home-assistant/requirements.txt` (warns); CodeQL default setup.

### IMP-030 Documentation structure

| | |
| --- | --- |
| Related | DOC-011, DOC-013, CODE-006 |

**Design sketch.** `docs/architecture.md` (short); `design.md` split per subsystem with diagrams;
the sprint design's numbered decisions published or folded in so that the code's references
resolve; a test that design references in docstrings resolve to headings.

## D Smaller improvements

| ID | Improvement | Related |
| --- | --- | --- |
| IMP-031 | Persist the message airtime bucket's level across restarts (or document the asymmetry with the lanes) | ARC-005 |
| IMP-032 | Message priority: a JSON envelope on `send/<alias>` (`{"text": "…", "priority": "alert"}`) or `[targets.<alias>] priority`, with an alert overtaking queued status messages (design §12 names it) | design §12 |
| IMP-033 | `contacts.manage_reason` in the index (`untested_firmware`, `management_off`, `no_radio`) so Home Assistant and the dashboard can say why the controls are unavailable | OPS-007 |
| IMP-034 | A troubleshooting row for "controls unavailable after a radio firmware update" and a warning line at connect on untested firmware | OPS-007 |
| IMP-035 | `--update` waits for `state.queue == 0` (up to a minute) before restarting | OPS-006 |
| IMP-036 | A warning at start when `log_level = "debug"` (texts and positions go to the journal) | OPS-017 |
| IMP-037 | `nodes_days` as a setting or a documented constant, aligned with ADR 0016 | DOC-004 |
| IMP-038 | `[messages] clean_received` (apply the outbound cleaning to inbound texts) for operators who prefer it; default off to keep "texts are data" | SEC-017 |
| IMP-039 | Truncate the alias echoed in a rejected `delivery` event to 64 characters | SEC-017 |
| IMP-040 | Rate-limit the "malformed push" warning and end the session after N in a row | SEC-014 |
| IMP-041 | Detect a contested TCP link from a continuous radio uptime across a reconnect; publish `radio_link_contested` | SEC-003 |
| IMP-042 | `check-config` summary line for the radio names the transport's security ("tcp, unauthenticated") | SEC-003, OPS-010 |
| IMP-043 | An exported `.github/rulesets/*.json` of the branch and tag rulesets, kept in the repository | SUP-007 |
| IMP-044 | A `README` in `tests/firmware/` naming each stand-in | CODE-023 |
| IMP-045 | Remove `# fmt: skip` in one commit, or write the convention down | CODE-007 |
| IMP-046 | An index (table of contents of facts) at the top of `docs/platform-notes.md` | DOC-026 |
| IMP-047 | History export: `meshcore-home-gateway history export --since … --format jsonl|csv` (ADR 0016 anticipates it) | ADR 0016 |
| IMP-048 | `apt-get clean` after the install; `--offline` is out of scope | OPS-013 |
| IMP-049 | The dashboard's deferred items (reason codes as texts, remove and telemetry controls, heard repeaters on the map) as the project lists them in design §6.13 | design §6.13 |
| IMP-050 | `Disabled` frames from the radio (`writeDisabledFrame`, for commands compiled out) are mapped to `disabled` in `CommandFailedError`; a `settings` state `disabled_in_firmware` would tell "compiled out" from "unsupported" for the profile | `radio/profile.py` |
