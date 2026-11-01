# 04 Architecture and design review

This document judges the decisions (the 27 records in `docs/decisions/`) and the design document
against what the code does and what the review found elsewhere. It names tensions between
decisions, alternatives the records did not consider, and the places where the program has
outgrown the shape the records describe. Items are `ARC-nnn`; where a security consequence
exists, the `SEC` item is referenced instead of repeated.

## 1 The shape, in one paragraph

The program is a single asyncio process built as ports and adapters around a pure core
(ADR 0007). It started as a message bridge with three safety properties (configured targets,
an airtime budget, allowlisted commands: ADR 0008, 0009) and grew in one week (2026-10-23 to
2026-10-31) into an observer (ADR 0014) with seven more subsystems: documents and history
(0015, 0016), contact devices (0017), contact management, the managed radio profile (0020),
telemetry polling with lanes and expiry (0018, 0024), the advert check (0025), translations
(0019, 0027) and a dashboard generator (0021). The core stayed pure and tested; the application
layer (`bridge.py`, `observer.py`, `contact_manager.py`, `telemetry_poller.py`: 3,400 lines) is
where the growth landed. The shape held; the seams show in the application layer, in the state
file and in the security section of the design document, which was extended row by row rather
than redrawn (see `02-threat-model.md` §7).

Sizes for reference (lines of source, `main`): `core/` 2,710, `protocol/` 1,175, `radio/` 1,276,
`mqtt/` 1,816, top-level application and CLI 8,148 (of which `cli.py` 1,657, `bridge.py` 1,164,
`telemetry_poller.py` 896, `observer.py` 844, `config.py` 760, `state.py` 708); 15,125 in all.
Tests are about 43,000 lines, nearly three times the program.

## 2 Items

### ARC-001 The security section of the design did not grow with the observer

| | |
| --- | --- |
| Category | architecture |
| Component | `docs/design.md` §7; ADR 0014 |
| Related | `02-threat-model.md` §7, SEC-010 to SEC-016 |

**Description.** Design §7.1 draws one boundary chain (channel → radio → link → bridge → broker
→ automation) and §7.2 lists threats that are, with four late additions (telemetry of strangers,
presence on unconfirmed positions, positions in adverts, forged adverts in the log), those of the
message bridge. ADR 0014 changed what the gateway is: it now writes to the radio, polls other
people's devices, stores positions and names for weeks, exposes controls to every Home Assistant
user, and reacts to every advert in range. Each of those is a new attack surface or a new
privacy duty, and none has a row in §7.2. The gaps the review found are almost all at those new
edges (floods from the air, bystanders' data, Home Assistant users, the broker as a writer of
documents, the operator's clock).

**Evidence.** The comparison table in `02-threat-model.md` §7; the dates of the records.

**Motivation / tradeoff.** The team's attention went where the danger was most obvious (the
command path) and that part is excellent. The method ("design first, every protocol fact with a
citation") did not include "redraw the boundaries when the scope changes".

**Design sketch.** Make the threat model a document of its own (`docs/threat-model.md`) with the
boundaries and actors of `02-threat-model.md` (or the team's own), and make "which boundary does
this touch, which actor, what changes in the threat table" a required paragraph of every ADR
that adds a capability (the ADR template's "Consequences" could ask for it). Link design §7 to
it. Re-run the comparison at each minor release.

---

### ARC-002 "A contact grants no rights" is true of the command path and not of the rest

| | |
| --- | --- |
| Category | architecture |
| Component | ADR 0017 (Decision Drivers: "being a contact must grant no rights"; "Who may manage: … on by default … because a contact grants no rights") |
| Related | SEC-008, SEC-013, SEC-016, SEC-012 |

**Description.** ADR 0017 justifies three defaults with the claim: every contact is a recipient,
every contact gets a device, and anyone may add or remove contacts. The claim holds for ADR
0009's command path (only configured full keys command). It does not hold for what else being a
contact means in this system: the radio decrypts and acknowledges the contact's direct messages
(airtime and bus load, SEC-013), the gateway polls the contact's telemetry and records its
positions (SEC-016), its messages land in the recorder and the history (SEC-012's direct-message
cousin), and its name becomes a device name every user sees (SEC-007). A contact is therefore a
party the household **trusts with its attention and its storage**, and the right to make someone
a contact is a right.

**Motivation / tradeoff.** Zero configuration was the driver, and it works beautifully for the
common case. The cost is that the decision to trust is made by whoever can click.

**Design sketch.** Keep the mechanism, name the trust: (a) a per-contact `household` mark
(SEC-016's opt-in telemetry), (b) a Home Assistant exposure setting apart from the function
(SEC-008), (c) a sentence in ADR 0017's consequences that lists what a contact does get, so the
next reader does not inherit the simplification. A superseding record (ADR 0028, "What a contact
is trusted with") would fit the project's own rule that accepted records are not edited.

---

### ARC-003 Event types of the radio adapter are the application's vocabulary

| | |
| --- | --- |
| Category | architecture (layering) |
| Component | `radio/session.py:101-197` (`MessageReceived`, `AckReceived`, `TelemetryReceived`, `PathChanged`, `PathReset`, `PacketLogged`, `AdvertHeard`, `ContactsRead`, `StatsRead`, `Barrier`, `RadioEvent`), imported by `bridge.py`, `observer.py`, `contact_manager.py`, `telemetry_poller.py`, `mqtt/discovery.py` (`RadioInfo`), `cli.py` |
| Related | ADR 0007; `tests/test_architecture.py` (guards only the core and the pure modules) |

**Description.** ADR 0007 says the application "depends only on the ports" and the adapters are
replaceable ("a new transport or bus is a new adapter, not a change to the core"). The transport
is indeed a `Protocol`. The session is not a port: it is a concrete class, and the events it
emits are dataclasses defined in the adapter module, with a field `session: CompanionSession` in
`MessageReceived` so that the bridge can call `session.refresh_contacts` and read
`session.contacts`. The observer, the contact manager and the poller all import from
`radio.session`. A second radio adapter (BLE, named as future work in design §12) would have to
produce `radio.session`'s classes or the application would change. The architecture test does
not guard this direction (it checks that the core is pure; it does not check that the
application imports only ports).

**Motivation / tradeoff.** Pragmatism: the session *is* the only radio, and the event classes
are small. The cost is conceptual (the ADR overstates the isolation) and practical only when a
second adapter comes.

**Design sketch.** Move the event dataclasses and a `RadioPort` protocol (what the bridge calls:
`refresh_contacts`, `contacts`, `contact_records`, `send_text`, `send_channel_text`,
`reset_path`, `request_telemetry`, `write_contact`, `remove_contact`, `info`, `channels`,
`closed`, `commands_sent`, `contacts_changed_at`, `tuning`, `battery`, `stats`) into
`meshcore_home_gateway/ports.py` (or `core/events.py` for the data and `ports.py` for the
protocol); `radio/session.py` implements it. Extend `test_architecture.py` with a rule that
`bridge.py`, `observer.py`, `contact_manager.py`, `telemetry_poller.py` import no module below
`radio/` or `mqtt/client`. This is a refactoring without behaviour change; the emulator tests
cover it.

---

### ARC-004 The state file is one JSON blob with nine independent writers

| | |
| --- | --- |
| Category | architecture (persistence) |
| Component | `state.py` (`JsonStateFile` and nine classes that each `load()`, change one key, `save()`), `observer.py`, `bridge.py`, `contact_devices.py`, `telemetry_poller.py` |
| Related | CODE-00x (blocking `fsync` on the loop), SEC-010 (disk full), `01-system-understanding.md` §7 |

**Description.** `state.json` holds nine kinds of state, each owned by a class that reads the
whole file, replaces its key and writes the whole file atomically with two `fsync`s. All run on
the event loop, so writes never interleave, but: (a) each write re-reads the file from disk (the
in-memory copies are per class, there is no shared document), (b) a corrupt file is moved aside
by whichever class loads first and the others then start empty, (c) the file is rewritten in
full for a one-key change (the `current_contacts` documents and the telemetry schedule make it
tens of KiB), (d) the file's size grows with `slugs` without bound (CODE), and (e) two `fsync`s
per write on a container's LVM thin volume cost milliseconds to tens of milliseconds on the loop
that also serves the radio and the watchdog. The design keeps the history (bulk) apart from the
state (small, crash-critical) on purpose (ADR 0016), which is right; the state has since become
less small.

**Motivation / tradeoff.** Atomic replace of a small JSON is the simplest crash-safe store, and
the replay ledger needed exactly that. The growth came with the observer.

**Design sketch.** One `StateStore` object owned by the composition root (`cli._serve`), loaded
once, holding the document in memory; sections register with it; a `save(section)` marks the
section dirty and the store writes the whole document at most once per tick (or at once for the
ledger, which must be durable before the command is published: a `save_now()`). Keep the atomic
replace; drop the per-class re-read. Validation per section stays where it is. Alternatively a
second SQLite file for the state (`state.sqlite3`, one table `kv(section, json)`, WAL,
`synchronous=FULL` for the ledger's transaction): SQLite gives per-row writes and crash safety,
and the history already brings it in; the atomic-replace argument of ADR 0016 ("bulk history never
shares a file with [the ledger]") stays satisfied by using two files.

---

### ARC-005 The message budget starts full at every start; the telemetry lanes start empty and carry a debt

| | |
| --- | --- |
| Category | architecture (consistency of the airtime model) |
| Component | `core/budget.AirtimeBudget` (bridge: `tokens=None` → full), `core/telemetry` lanes (`tokens=0`, debt saved in `state.json` → `telemetry.own_debt_s`), ADR 0008, ADR 0018 |
| Related | SEC-013, `docs/configuration.md` `[airtime]` ("each start begins with a full burst, so every restart within T adds up to burst_s") |

**Description.** Two airtime accounts with opposite restart semantics. ADR 0008 accepts that a
restart adds a burst (10 s) to the message budget; ADR 0018 deliberately starts the telemetry
lanes empty and persists their debt so that "a restart adds no burst". The reason for the
difference is not recorded. A crash loop (`StartLimitBurst=20` in 600 s, `RestartSec=5`) could in
principle spend 20 bursts of message airtime in ten minutes if a client re-queues a message at
every `online` (an automation that sends on availability would do that), which is 200 s in 600 s,
a 33 % duty cycle, against a legal 10 %. The radio's own limiter (the profile's airtime factor 9)
caps the radio at 10 % anyway, so the regulatory bound holds through ADR 0020, not through ADR
0008; the queue and the budget then only shape what the gateway *asks* for.

**Motivation / tradeoff.** ADR 0008 predates the persistence of anything but the ledger;
ADR 0018 had `state.json` at hand. Starting full makes the first alert after a restart go out at
once, which is what an alert gateway should do.

**Design sketch.** Persist the message bucket's level with the state (the lanes already do) and
restore it at start, capped so that a restart never *reduces* the available airtime below what a
single alert needs (one burst's worth for the first message, as a floor), or document the
asymmetry in ADR 0008's consequences and name the radio's own limiter as the regulatory
backstop (it is, since ADR 0020; ADR 0008 still says "the bridge's budget only covers what the
bridge asks the radio to send").

---

### ARC-006 "Only the gateway writes its topics" is an assumption nothing in the system can verify

| | |
| --- | --- |
| Category | architecture (trust on the bus) |
| Component | ADR 0015 ("every front end reads the same documents"), ADR 0003 ("broker ACLs can restrict each client") |
| Related | SEC-004, SEC-005, IMP-008 |

**Description.** The document model is the right choice for the goals (one source of truth, no
listener in the gateway, Home Assistant without code). Its security premise, that the broker's
ACL makes the gateway the only writer, is outside the gateway's control and unverifiable by the
consumers. The records discuss ACLs as a feature ("access control per action") and not as a
dependency that, if absent, turns every document into untrusted input to a presence automation.

**Design sketch.** SEC-005's self-healing subscription and stamps; SEC-004's probes; and in ADR
0015 (or a superseding note) an explicit statement of the integrity model: "documents are
trusted because the broker's ACL allows only the gateway to write them; the gateway restores
documents it finds changed; a consumer that needs more than that uses the signature field".

---

### ARC-007 Scope growth against the "small, reviewable" argument

| | |
| --- | --- |
| Category | architecture |
| Component | ADR 0002 ("about 29,000 lines of Python with access to the radio and to the automation system" for the rejected alternative), ADR 0004 ("the subset is a few hundred lines of pure encode/decode functions that can be reviewed completely"), the current sizes (§1 above) |
| Related | ARC-001, ARC-010 |

**Description.** The decision to write a new bridge rested on reviewability and a small supply
chain. The supply chain stayed minimal (one runtime dependency; a real achievement). The
reviewability argument has to be restated: the program is 15,000 lines, the protocol layer 1,175
(not "a few hundred"), and the telemetry scheduler alone (`core/telemetry.py` 728 lines plus
the poller 896) implements rules that take design §6.11, four pages of dense prose, to state.
This review took a week of reading to cover. That is still far below the alternatives' size and
far better documented, but ADR 0002 and ADR 0004 now describe a smaller program than exists, and
a reader who checks the figures will doubt the rest.

**Design sketch.** Refresh the numbers in a dated note of ADR 0002 and 0004 (the project does
this elsewhere: "Amended on …"), and consider a line budget per module in `CONTRIBUTING.md` as a
review trigger (a module over 800 lines asks for a split), which `bridge.py`, `cli.py`,
`telemetry_poller.py` and `observer.py` would trip today.

---

### ARC-008 Amendments in place versus "records are never edited after acceptance"

| | |
| --- | --- |
| Category | architecture (decision process) |
| Component | ADR 0001 ("Accepted records are not edited; a new record supersedes an old one"), `CONTRIBUTING.md` ("Records are never edited after acceptance"); ADR 0006, 0009, 0013, 0018, 0019, 0022 each carry "Amended on …" paragraphs with changed substance (ADR 0009's amendments changed the rules of command acceptance twice) |
| Related | none |

**Description.** The rule and the practice differ. The amendments are honest and dated, and for
the pre-release ones ("before the first release") the rule arguably did not apply yet; ADR 0018's
amendment by ADR 0025 and ADR 0019's by ADR 0027 are the superseding form the rule asks for.
ADR 0009's two amendments changed the decision's rules in place.

**Design sketch.** Either state the practice in ADR 0001 ("a record may carry dated amendments
that narrow or correct it; a reversal needs a new record") or move the amended rules of ADR 0009
into a short ADR 0028 that supersedes the relevant parts. Minor; it matters for the trust that
the records are a reliable history.

---

### ARC-009 The application layer's orchestration lives in constructors and callback lists

| | |
| --- | --- |
| Category | architecture (composition) |
| Component | `bridge.Bridge.__init__` (wires observer, devices, profile, contact manager, telemetry poller, manage entities; 19 constructor parameters), `observer.Observer` (five callback lists: `on_contacts`, `on_nodes`, `on_contact_added`, `on_telemetry_exclusion`, plus `telemetry_state` as a pluggable function), `mqtt.client.MqttBus` (five callback lists), `cli._serve` (the composition root) |
| Related | ARC-003, CODE-00x |

**Description.** Subsystems talk through lists of callables that other subsystems append to in
constructors (`self.observer.on_contacts.append(devices.update)`,
`self.observer.on_telemetry_exclusion.append(poller.exclusion_changed)`, …). It works and is
testable (each list is a seam), but the wiring is spread over three constructors and `_serve`,
the order of appends matters (the comments say so: "before the listeners, the devices and the
documents of this read"), and reading who reacts to what means grepping for `.append(`. The
`Bridge` also owns behaviours that are not bridging (announcement scheduling, the state
document, protected-key helpers).

**Design sketch.** A small in-process event bus (a typed `Signal[T]` with `connect` and `emit`,
30 lines) and a `wiring.py` next to `cli.py` that connects everything in one place with the order
visible; or at least a docstring table in `bridge.py` listing every callback list and its
subscribers. Splitting `Bridge` into `outbox.py` (the delivery state machine), `inbox.py` (the
message and command path) and `announcer.py` (discovery scheduling) would bring each under 500
lines with no behaviour change; the emulator tests would carry over.

---

### ARC-010 The advert check's correctness rests on frame adjacency and the firmware's receive delay

| | |
| --- | --- |
| Category | architecture (robustness of a design to firmware change) |
| Component | ADR 0025, `core/adverts.py`, `observer._advert`, `observer._own_log`, `observer._check_adverts`, `session._dispatch` (frame numbering) |
| Related | RES-00x |

**Description.** The check decides what a companion's newest advert carried by matching the
radio's `ADVERT` push with the `LOG_RX_DATA` frame directly before it (when the receive delay is
0) or with all logs of the key within 40 s that agree, confirmed by the next table read's
`last_advert`. It is carefully reasoned and tested, including with forged copies. It depends on
three firmware behaviours that no interface promises: that a packet is logged and handled in one
pass (`Dispatcher.cpp`), that the push follows its log without another frame between
(`MyMesh.cpp:2388-2421`), and that the receive delay is readable and 0 by default. A firmware
release that reorders the loop, logs after handling, or adds a frame between, breaks the check
silently (the gateway then "errs to warn", which is the safe direction, but the feature degrades
to "the record holds a position", which is what 1.0.0 had). The firmware tests (ADR 0026) would
catch it at the next pinned release, which is the right place.

**Motivation / tradeoff.** The alternative (the radio's stored copy of the advert, `EXPORT_CONTACT`)
was rejected for a real reason (buffer overread on ESP32, unanswered frames). The chosen design is
the best available without a firmware change.

**Design sketch.** (a) Ask upstream for a push that carries the accepted advert's flags (a one-byte
extension of `ADVERT`), which removes the whole inference; (b) make the dependence explicit in
`docs/protocol.md` §4 as "facts the check relies on", each with its firmware line, so a release
bump has a checklist; (c) keep the firmware test `test_adverts_and_positions.py` as the guard it
already is. The team may also weigh whether the warning's precision is worth 530 lines of core
and observer logic; the reviewer thinks it is, given the privacy goal, but notes the cost.

---

### ARC-011 Translations: a complete machinery for one language

| | |
| --- | --- |
| Category | architecture (investment versus use) |
| Component | ADR 0019, ADR 0027, `i18n.py` (≈400 lines), `catalog.py`, `texts.py`, `tools/i18n.py` (1,474 lines), `tests/test_i18n*.py`, `test_translations.py`, `test_adding_a_language.py`, `test_cli_language.py`, `test_installer_language.py` (≈5,000 lines of tests), the installer's language packs and pins |
| Related | DOC-00x, SUP-00x (contributor-supplied catalogs) |

**Description.** About 7,000 lines (a sixth of the repository) implement, check and test
gettext for the command line, Home Assistant names, the dashboard and the installer, with a PO
reader and an MO writer of the project's own, byte-identical to GNU `msgfmt`, compiled at load.
Today English is the only language; ADR 0019 names German, Dutch, Polish and French as targets
and reasons about MeshCore's spread. The machinery is well made (the pseudo-language tests, the
strict catalog in tests, the guards against translations that break formatting are all good
engineering). The question for the team is sequencing: the languages do not exist yet, Weblate
is "not set up yet", and the machinery has to be carried through every change (every new text
needs `make translations`; the installer's packs are pinned by SHA-256 in the script and tested
for a size limit).

**Motivation / tradeoff.** ADR 0019's driver is real (households outside the English-speaking
world). Building the machinery before the first translation avoids retrofitting; it also means a
large surface that only tests exercise.

**Design sketch.** No change to the design; a note in the roadmap that the first language is the
proof of the machinery, and a review after it of whether the installer's pack mechanism (the
most intricate part: fetched from the release or `main`, pinned, size-limited, read without
`eval`) is worth keeping versus shipping the installer's texts in English and translating only
the program and Home Assistant. Also a security note in `08-…`: catalogs are contributor-supplied
data parsed at runtime by the project's own reader (the reader is small and tested; still a
parser of third-party input in the service).

---

### ARC-012 Error handling philosophy: "never stop messages" hides degradation

| | |
| --- | --- |
| Category | architecture (resilience versus observability) |
| Component | `bridge._observe` (`except Exception: log.exception("observer failed")`), `observer.py` listeners (`except Exception` around every callback), `contact_manager.run` (nested `except Exception`), `telemetry_poller` (`STATE_ERROR_LOG_S`: errors at most once a minute), `session._apply_profile` (a profile bug is logged and the connection goes on), `history._broke` (switches the history off until restart) |
| Related | OPS-00x, IMP-013 |

**Description.** The design states it (§6.6 "Failures stay local") and the code applies it
consistently: a bug in a subsystem is logged and the rest continues. This is the right priority
for an alert gateway. The cost is that the gateway can run for days in a degraded state — history
off, exclusions unsaved, the observer throwing on every tick, the profile not applied — with
`availability` `online`, `state.radio` `connected`, and the only evidence in the journal. Home
Assistant has no entity that says "degraded"; `state` has `telemetry_reason` for one subsystem.

**Design sketch.** A `health` section in the `state` document (or a `health` document):
`history: "ok" | "broken" | "off"`, `state_file: "ok" | "unwritable"`, `exclusions: "ok" |
"unsaved" | "unreadable"`, `observer_errors_1h: n`, `profile: "applied" | "failed"`, and a
diagnostic binary sensor *Gateway degraded* (problem class) in discovery. Each subsystem already
knows its state; the status loop publishes every 60 s anyway.

---

### ARC-013 No reload, no drain: every configuration change drops the queue

| | |
| --- | --- |
| Category | architecture (operations) |
| Component | `cli._serve` (SIGTERM/SIGINT → stop), design §3 non-goal "Persisting the outgoing queue across restarts", `bridge.run` (the stop sequence) |
| Related | OPS-00x, IMP-014 |

**Description.** The configuration is read once; a change (a new target, a new sender, a
changed budget) needs a restart, and a restart drops queued messages without a final `delivery`
event (documented). The installer's `--reconfigure` and `--update` restart the service. For an
alert gateway, "the update dropped the alarm" is a plausible incident; the non-goal was reasonable
for 0.1 and is worth revisiting now that `state.json` exists.

**Design sketch.** (a) Persist the outbox on stop (`Outgoing` entries with their creation time and
the elapsed time to live) and restore it at start within the time to live, publishing `queued`
again with the same `id`; the dedup memory need not be persisted. (b) A drain mode on SIGTERM:
stop accepting requests, finish the queue within `TimeoutStopSec` (20 s in the unit; a message
can take 60 s to be acknowledged, so either raise it or persist what is left). (c) SIGHUP reload
of the parts that can change at runtime (targets, words, senders, budgets) is more work and less
important than (a).

---

### ARC-014 MQTT 3.1.1 where MQTT 5 would carry the semantics the design wants

| | |
| --- | --- |
| Category | architecture (protocol choice) |
| Component | `mqtt/client.make_paho_client` (`MQTTProtocolVersion.MQTTv311`), design §6.5 ("clean session: requests published while the bridge is offline are lost, which is the safer choice"), §12 ("MQTT 5 features: message expiry for send requests, response topics for correlation") |
| Related | SEC-005 (origin stamps), IMP-015 |

**Description.** Three behaviours the design implements or documents by convention have native
MQTT 5 forms: a request that must not arrive late (clean session, QoS 0 for commands) is the
*message expiry interval*; the correlation of a request with its result (`id` in the JSON
envelope, generated when absent) is *correlation data* and *response topic*; a document's origin
and version (SEC-005) are *user properties*. paho-mqtt 2.1 supports MQTT 5; Mosquitto 2.0 does;
Home Assistant's MQTT integration speaks 3.1.1 and 5 (its own client setting), and consumers on
3.1.1 interoperate with a 5 publisher on a 5-capable broker (properties are dropped). The
compatibility policy lists "retained flags, QoS and the topic layout" as interface; the protocol
version of the gateway's own connection is not.

**Design sketch.** `[mqtt] protocol = "3.1.1" | "5"` (default 3.1.1 until tested), with: message
expiry on `send/*` requests honoured when present (the broker drops them, nothing to do in the
gateway) and set on the gateway's own events (`delivery`, `received`, `command`: an expiry of
`queue_ttl_s` makes "delivered after the reconnect, at an arbitrary later time" impossible,
which is the reason the command event is QoS 0 today); `response_topic`/`correlation_data` on
requests, echoed on the result; `user_properties` `gateway`, `version`, `published_at` on
documents. Brokers that do not speak 5 refuse the connection (`CONNACK` with an unsupported
protocol) and the gateway would say so.

---

### ARC-015 One radio per process, and the identity of "the gateway" in Home Assistant

| | |
| --- | --- |
| Category | architecture (scaling, naming) |
| Component | design §3 non-goal "Several radios in one process"; `mqtt/discovery.device_id` (`meshcore_home_gateway_<node_id>`), the contact devices' `via_device`, the dashboard's trackers ("the right tracker IDs when the contacts of two gateways share a name" is deferred) |
| Related | EXT-00x |

**Description.** One process per radio is a sound choice (isolation, simplicity). Two gateways on
one broker work, but Home Assistant's entity IDs of *contacts* are `mesh_<slug>` without the
gateway's node id: the same contact seen by two gateways yields `device_tracker.mesh_alice` and
`device_tracker.mesh_alice_2`, two devices for one person, and the dashboard of the second
gateway lists the first one's tracker (documented in `docs/home-assistant.md`). The design chose
short entity IDs for the household case; the cost appears with the second radio (a holiday home,
a club, a redundant radio).

**Design sketch.** A setting `[homeassistant] contact_entity_prefix` (default `mesh`), so that a
second gateway can use `mesh2_`; and, longer term, a way for two gateways to recognise the same
contact (the key is the identity; a `person`-like merge is Home Assistant's job, which the guide
could describe: assign both trackers to one `person`).

---

### ARC-016 The Home Assistant floor and the contract-test pin

| | |
| --- | --- |
| Category | architecture (platform coupling) |
| Component | `docs/home-assistant.md` Requirements (2026.8; `default_entity_id` since 2025.10, QoS at device level since 2026.5.2, `event.received` since 2026.7, per-event state changes since 2026.8), `home-assistant/requirements.in` (one pinned `pytest-homeassistant-custom-component`) |
| Related | SUP-00x |

**Description.** "Nothing to install in Home Assistant" is bought with a floor three months old
at release and features that Home Assistant added in four consecutive releases. The contract
tests pin one release and Dependabot proposes the next; a Home Assistant release that changes
discovery semantics (it has happened: the device-based discovery schema, the removal dance the
project documents) breaks every installation that updated Home Assistant before the gateway. The
project's "revisit" clauses name this. The alternative (a custom integration) was rejected for
good reasons (ADR 0002).

**Design sketch.** Keep as is; add to the release checklist "run `make ha-test` against the
*newest* Home Assistant, not only the pinned one" (a second environment, allowed to fail, as an
early warning), and publish in the README which Home Assistant versions each gateway release was
tested with (the changelog says it per release; a table would help).

---

### ARC-017 Decisions the records do not contain

For completeness, choices that the reviewer would have expected a record for and found decided
in code or prose only:

| Choice | Where it is decided | Why a record would help |
| --- | --- | --- |
| Telemetry on by default with a first-start hour | CHANGELOG 1.1.0a2, design §6.11 | It reversed ADR 0018's "off by default for now" and is the privacy decision of the project (SEC-016) |
| Every advert re-reads the contact table | `session._push` | A cost model decision with a denial-of-service side (SEC-011) |
| The state file as a multi-writer JSON blob | `state.py` | ARC-004 |
| QoS 0 for command events | design §6.5 | A deliberate loss-over-lateness tradeoff that consumers must know |
| Clean session, no persistence of requests | design §6.5 | Same |
| 50 contact devices, 50 heard nodes, 16 readings, 1024 exclusions | constants | Bounds that shape the product |
| The gateway writes the radio's clock | design §6.1 | SEC-002 |
| Mesh text is never cleaned on the way in | `bridge._on_message` | SEC-017's "data" rule, which consumers must know |

Each could be a short record or a paragraph in design §11 ("Alternatives considered").

## 3 What the architecture does well

Listed so that the critique above is read in proportion:

* The pure core with an injected clock is real: `core/` has no I/O, every policy is a function,
  and the tests show it (the telemetry scheduler is tested on a set clock through hundreds of
  cases).
* One event loop, one thread crossing, no locks beyond the session's command lock: the
  concurrency story is simple and the shutdown sequence is explicit and reasoned (the Python
  3.11 `wait_for` note is the kind of detail that shows real testing).
* Every bound has a number and the number has a reason (`docs/design.md` is full of "at most
  N, because").
* The protocol layer cites the firmware for every field; the firmware tests run the real
  firmware; the platform notes record evidence for every claim about the platforms. This is rare.
* Failure modes are enumerated (design §8) with detection, behaviour and visibility.
* The compatibility policy and the interface snapshot (ADR 0023) are a serious answer to
  "what does a major version promise", and the release plan enforces it.
* The supply chain discipline (one runtime dependency, hash-pinned tools, cooldowns, SHA-pinned
  actions, attestations) is better than most commercial projects'.
