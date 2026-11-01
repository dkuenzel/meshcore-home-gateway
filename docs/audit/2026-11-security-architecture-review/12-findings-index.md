# 12 Index of every item

Every item of the review in one table, for import into the backlog (`findings.csv` holds the same
rows). No priority, severity or effort column: triage is the project team's. `Related` lists
the items, records and issues each one names. The security findings are grouped by boundary in
`03-security-findings.md` and by number here.

| Group | Items |
| --- | --- |
| Security findings (`SEC`) | 17 |
| Architecture and design (`ARC`) | 17 |
| Code quality (`CODE`) | 25 |
| Documentation (`DOC`) | 26 |
| Operations and deployment (`OPS`) | 22 |
| Supply chain and release (`SUP`) | 7 |
| Improvement opportunities (`IMP`) | 50 |
| Extension opportunities (`EXT`) | 20 |
| Research notes (`RES`) | 24 |
| **Total** | **208** |

## Security findings

| ID | Title | Component | Related |
| --- | --- | --- | --- |
| SEC-001 | Acknowledgements are forgeable from a guessed text: a false `confirmed` and suppressed retries | `src/meshcore_home_gateway/bridge.py:653-698` (`_attempt`), `bridge.py:161-199` (`AckWaiter`), `protocol/codec.py:207-210` (`expected_ack`); firmware `src/Me… | design §6.2 step 7, §7.4; RES-001; IMP-001; SEC-004 (the `delivery` topic reveals texts to broker readers) |
| SEC-002 | `set_clock` moves the radio's clock forward irreversibly from a wrong host clock | `src/meshcore_home_gateway/radio/session.py:587-597` (`_sync_clock`), `config.py:67` (`set_clock = True` default); firmware `examples/companion_radio/MyMesh.… | design §6.1 "Clock", `docs/deployment.md` troubleshooting rows `too_old` / `from_the_future`, DOC-002, IMP-006, RES-007 |
| SEC-003 | Over TCP the radio's private key is exportable and importable by anyone on the network (default build), and the gateway cannot tell a second client | `src/meshcore_home_gateway/radio/transport.py:139-181` (`TcpTransport`), `config.py:63-66`, `bridge.py:419-426` (the warning for commands over TCP); firmware… | design §7.2 "Unauthenticated TCP companion interface", §7.4; SEC-001; RES-008, RES-017, RES-018; OPS-010; DOC-009; IMP-041, IMP-042 |
| SEC-004 | An insecure broker configuration (no credentials, no TLS) is accepted without a word | `config.py:450-488` (`_mqtt`: `username`, `password`, `tls` all optional), `cli.py:447-486` (`_check_config`, `_login`: "no user name" printed as a fact), `m… | design §7.2 rows "Abuse of the send topic", "Forged events"; `docs/mqtt-api.md` "Securing the broker"; SEC-005, SEC-006, SEC-009; IMP-007 |
| SEC-005 | A forged or stale retained document is neither detected nor healed; a presence automation follows it | `observer.py:795-800` (`_publish`: compares with the last payload *sent*, never with the broker), `observer.py:396-404` (`publish_all` on reconnect), `mqtt/c… | SEC-004, ADR 0015 ("every front end reads the same documents"), ADR 0024, design §7.2 row "Forged events"; IMP-008 |
| SEC-006 | The MQTT client id is predictable; birth messages and discovery clears trigger unbounded re-announcements | `mqtt/client.py:77-83` (`make_paho_client`: `client_id = … or f"meshcore-home-gateway-{node_id}"`, `clean_session=True`), `bridge.py:1062-1072` (`_schedule_a… | SEC-004, SEC-005, design §6.5, `docs/platform-notes.md` Mosquitto ("The broker's log names the client id"), IMP-009 |
| SEC-007 | Names from the mesh reach the Home Assistant UI unflagged: a stranger can look like a contact or like the gateway | `mqtt/contact_devices.py:305-314` (device `name: node.name or key[:12]`), `core/contacts.node_label` (`<12 digits>…<8 digits> <name>`), `views.nodes_document… | SEC-017, ADR 0017, `docs/home-assistant.md` "Add a node that advertised", IMP-010 |
| SEC-008 | Every Home Assistant user can send, add and remove contacts, toggle anyone's telemetry and read every position | `mqtt/discovery.py` (notify per target, `show_hidden` button), `mqtt/manage_entities.py` (the select and the text), `mqtt/contact_devices.py` (`remove` butto… | SEC-016, SEC-013, ADR 0017 ("Who may manage: … on by default … because a contact grants no rights"), `docs/home-assistant.md` "Who may", "The dashboard has no control to remove a contact …: that does not keep other users from them", IMP-011 |
| SEC-009 | The broker password is a group-readable file, printed by `config answers`, never a systemd credential | `config.py:466-468,475-488` (`password` in `MqttConfig`), `config.config_answers` (prints it), `cli._render` (`--group`, mode 0640), `deploy/systemd/meshcore… | SEC-004, OPS-008, IMP-012 |
| SEC-010 | An advert flood with invented keys grows the history's `node` table without a cap | `src/meshcore_home_gateway/history.py:170-184` (`node_heard`), `history.py:186-212` (`purge`), `observer.py:605-656` (`_advert`), `observer.py:688-690` (`_st… | SEC-011; ADR 0016; DOC-004 (7 vs 30 days); CODE-013; IMP-019 |
| SEC-011 | An advert flood republishes a 15 KiB document per advert, evicts real heard nodes and keeps the gateway re-reading the contact table | `observer.py:605-656` (`_advert` → `_publish_nodes` on every advert), `views.py:246-267` (`nodes_document`, up to 15 KiB), `core/mesh.py:26,357-395` (`MAX_NO… | SEC-010, IMP-003 (incremental contact reads), RES-005 |
| SEC-012 | Messages on the public channel are published by default: every neighbour's chatter lands in Home Assistant's recorder and the history | `config.py:110,531` (`ignore_channels` default empty), `bridge.py:808-813,815-860` (`_ignored`, `_on_message`), `observer.py:359-378` (`message` → history), … | SEC-015, DOC-010, IMP-004, IMP-018, ADR 0017 (every contact a recipient) |
| SEC-013 | A contact can starve the radio's own duty cycle with direct messages the radio must acknowledge | firmware `src/helpers/BaseChatMesh.cpp:240-257` (every plain direct message from a contact is acknowledged), `src/Dispatcher.cpp:19-53` (the airtime factor d… | SEC-012, ADR 0008 (the budget does not see the radio's own transmissions), ADR 0020 |
| SEC-014 | Malformed frames over TCP produce one warning each, without a limit | `radio/session.py:666-671` (`_push`: `log.warning("radio: malformed push ignored: %s", exc)`), `session.py:663-664` (`unexpected frame … dropped` at debug), … | SEC-003, IMP-040, `bridge._report_unpublished` (the pattern to reuse) |
| SEC-015 | Bystanders' data (names, keys, signal, raw packets) is collected, retained and recorded without their knowledge | `views.nodes_document` (`nodes`, retained), `observer._advert` (`node` events, the `node` table), `bridge._event_loop` (`rx` when `publish_rx_log`), `mqtt/di… | SEC-010, SEC-012, SEC-016, ARC-002, DOC-010, RES-006 |
| SEC-016 | Telemetry is opt-out per contact: a newly added contact is asked within a minute, and consent is inferred from the companion's settings | `telemetry_poller.py` (candidates = every chat contact not excluded), `observer.on_contact_added` → `poller.contact_added` (due at once), `config.py:140` (`t… | SEC-008, SEC-015, ARC-002, IMP-005 |
| SEC-017 | The telemetry answer, the message and the add-contact paths accept names and texts without limits beyond the protocol's | `protocol/codec._cstring` (names: 32 bytes), `codec.decode_message` (texts ≤ 176 bytes), `core/lpp.decode` (total), `protocol/links.parse_contact_text` (link… | SEC-007, CODE-015, IMP-038, IMP-039 |

## Architecture and design

| ID | Title | Component | Related |
| --- | --- | --- | --- |
| ARC-001 | The security section of the design did not grow with the observer | `docs/design.md` §7; ADR 0014 | `02-threat-model.md` §7, SEC-010 to SEC-016 |
| ARC-002 | "A contact grants no rights" is true of the command path and not of the rest | ADR 0017 (Decision Drivers: "being a contact must grant no rights"; "Who may manage: … on by default … because a contact grants no rights") | SEC-008, SEC-013, SEC-016, SEC-012 |
| ARC-003 | Event types of the radio adapter are the application's vocabulary | `radio/session.py:101-197` (`MessageReceived`, `AckReceived`, `TelemetryReceived`, `PathChanged`, `PathReset`, `PacketLogged`, `AdvertHeard`, `ContactsRead`,… | ADR 0007; `tests/test_architecture.py` (guards only the core and the pure modules) |
| ARC-004 | The state file is one JSON blob with nine independent writers | `state.py` (`JsonStateFile` and nine classes that each `load()`, change one key, `save()`), `observer.py`, `bridge.py`, `contact_devices.py`, `telemetry_poll… | CODE-002 (blocking `fsync` on the loop), CODE-003, CODE-004, SEC-010 (disk full), IMP-021, `01-system-understanding.md` §7 |
| ARC-005 | The message budget starts full at every start; the telemetry lanes start empty and carry a debt | `core/budget.AirtimeBudget` (bridge: `tokens=None` → full), `core/telemetry` lanes (`tokens=0`, debt saved in `state.json` → `telemetry.own_debt_s`), ADR 000… | SEC-013, `docs/configuration.md` `[airtime]` ("each start begins with a full burst, so every restart within T adds up to burst_s") |
| ARC-006 | "Only the gateway writes its topics" is an assumption nothing in the system can verify | ADR 0015 ("every front end reads the same documents"), ADR 0003 ("broker ACLs can restrict each client") | SEC-004, SEC-005, IMP-008 |
| ARC-007 | Scope growth against the "small, reviewable" argument | ADR 0002 ("about 29,000 lines of Python with access to the radio and to the automation system" for the rejected alternative), ADR 0004 ("the subset is a few … | ARC-001, ARC-010 |
| ARC-008 | Amendments in place versus "records are never edited after acceptance" | ADR 0001 ("Accepted records are not edited; a new record supersedes an old one"), `CONTRIBUTING.md` ("Records are never edited after acceptance"); ADR 0006, … | none |
| ARC-009 | The application layer's orchestration lives in constructors and callback lists | `bridge.Bridge.__init__` (wires observer, devices, profile, contact manager, telemetry poller, manage entities; 19 constructor parameters), `observer.Observe… | ARC-003, CODE-005, IMP-022 |
| ARC-010 | The advert check's correctness rests on frame adjacency and the firmware's receive delay | ADR 0025, `core/adverts.py`, `observer._advert`, `observer._own_log`, `observer._check_adverts`, `session._dispatch` (frame numbering) | ADR 0026, RES-002, `docs/protocol.md` §4 |
| ARC-011 | Translations: a complete machinery for one language | ADR 0019, ADR 0027, `i18n.py` (≈400 lines), `catalog.py`, `texts.py`, `tools/i18n.py` (1,474 lines), `tests/test_i18n*.py`, `test_translations.py`, `test_add… | SUP-006 (contributor-supplied catalogs), EXT-015 |
| ARC-012 | Error handling philosophy: "never stop messages" hides degradation | `bridge._observe` (`except Exception: log.exception("observer failed")`), `observer.py` listeners (`except Exception` around every callback), `contact_manage… | OPS-011, CODE-008, IMP-013 |
| ARC-013 | No reload, no drain: every configuration change drops the queue | `cli._serve` (SIGTERM/SIGINT → stop), design §3 non-goal "Persisting the outgoing queue across restarts", `bridge.run` (the stop sequence) | OPS-006, IMP-014 |
| ARC-014 | MQTT 3.1.1 where MQTT 5 would carry the semantics the design wants | `mqtt/client.make_paho_client` (`MQTTProtocolVersion.MQTTv311`), design §6.5 ("clean session: requests published while the bridge is offline are lost, which … | SEC-005 (origin stamps), IMP-015 |
| ARC-015 | One radio per process, and the identity of "the gateway" in Home Assistant | design §3 non-goal "Several radios in one process"; `mqtt/discovery.device_id` (`meshcore_home_gateway_<node_id>`), the contact devices' `via_device`, the da… | EXT-014 |
| ARC-016 | The Home Assistant floor and the contract-test pin | `docs/home-assistant.md` Requirements (2026.8; `default_entity_id` since 2025.10, QoS at device level since 2026.5.2, `event.received` since 2026.7, per-even… | DOC-018, SUP-004 |
| ARC-017 | Decisions the records do not contain |  |  |

## Code quality

| ID | Title | Component | Related |
| --- | --- | --- | --- |
| CODE-001 | A stall of the event loop longer than 5 s during a radio command ends the session although the answer arrived | `radio/session.py:738-757` (`_next`: `asyncio.wait({closed, frame}, timeout=self._timeout)`), `session.py:44` (`COMMAND_TIMEOUT_S = 5.0`), everything that bl… | CODE-002, ARC-004, OPS-011 (CPU starvation in a container is an operational cause), IMP-020, RES-009 |
| CODE-002 | Blocking I/O on the event loop: `fsync` per state write, SQLite per event, the hourly purge | `state.JsonStateFile.save` (two `fsync`s, called by nine classes), `history.py` (every `add_message`, `add_position`, `add_readings`, `node_heard` is a synch… | CODE-001, ARC-004, SEC-010, ADR 0016 ("if they ever stall it, they move to a worker thread") |
| CODE-003 | Two state sections grow without bound: `slugs` and `radio_profile` | `state.ContactDeviceRecords.slugs` (`records.slugs[key] = slug` in `contact_devices._announce`, never removed: "also to contacts that left: they get theirs b… | ARC-004 |
| CODE-004 | A section of the state file is re-saved only when it changes, so a replaced file loses the others' sections until then | `state.JsonStateFile.load` (a corrupt file is moved aside, `{}` returned), every section's `save`/`replace` (loads from disk, writes its key, writes the file… | ARC-004, OPS-008 (restore from backup while running), IMP-021 |
| CODE-005 | Size and complexity hotspots | `cli.py` 1,657 lines (29 handlers, the probe report, the contact session helper, the dashboard, render/answers), `bridge.py` 1,164, `telemetry_poller.py` 896… | ARC-007, ARC-009 |
| CODE-006 | Comments cite design identifiers that are not in the repository | 34 comments and docstrings in `observer.py`, `core/adverts.py`, `core/mesh.py`, `views.py`, `telemetry_poller.py`, `state.py` of the form "design 6c §3.1", "… | DOC-011, IMP-030 |
| CODE-007 | `# fmt: skip` on 205 lines | `src/` (205 `# fmt: skip`, 49 `# noqa`), `pyproject.toml` (`ruff format`) | none |
| CODE-008 | Twenty-eight broad `except Exception` handlers | `bridge._observe`, `bridge._radio_loop`, `bridge._outbox_loop`, `bridge._message_loop`, `bridge._telemetry_state`, `observer` (listeners, four places), `cont… | ARC-012 |
| CODE-009 | The test strategy: what it covers well and what it does not | `tests/` (1,470 test functions in the main suite, 90 % branch coverage gate), `home-assistant/tests/` (contract), `tests/firmware/` (real firmware), `tests/m… | ADR 0026, IMP-017 |
| CODE-010 | Minor observations |  |  |
| CODE-011 | `session.refresh_channels` reads every channel slot (`max_channels`, up to 40 on some builds) with one command each at every connect | `session.py:521-530` | Acceptable (connects are rare); note the cost in the docstring |
| CODE-012 | `AckWaiter._recent` keeps late acknowledgements for 120 s without a count bound; only matched codes reach it (the radio pushes only matched ones), so it is bounded by the radio's 8-entry table | `bridge.py:161-199` | None; a comment that the bound is the radio's table |
| CODE-013 | `history.purge` runs six statements, two with correlated subqueries, inside autocommit (each its own transaction) | `history.py:186-212` | Wrap the purge in one transaction; run it in a thread (CODE-002) |
| CODE-014 | `config._convert` accepts `"nan"` and `"inf"` for floats and relies on the range check to refuse them (it does, since comparisons with NaN are false) | `config.py:716-730` | A test that `airtime.burst_s=nan` is refused with a readable message |
| CODE-015 | `config._toml_string` writes U+2028/U+2029 and other non-ASCII unescaped, which TOML allows; a `check-config` of such a file is fine; an editor may not show them | `config.py:751-760` | Escape `Zl`/`Zp` as `\uXXXX` for the operator's sake |
| CODE-016 | The CLI imports heavy modules inside handlers for a fast `--version` (`# noqa: PLC0415`) | `cli.py` | Fine; the pattern is documented in the comment |
| CODE-017 | `bridge._deliver`'s `while sent < attempts` loop `continue`s on `RadioError` without a counter; it is bounded by `_radio_until(deadline)` | `bridge.py:607-626` | A comment naming the bound |
| CODE-018 | `observer._advert` publishes the `nodes` document and the history row on every stranger's advert (SEC-011) | `observer.py:646-649` | As SEC-011 |
| CODE-019 | `mqtt/client.MqttBus.publish` logs a warning per refused publish with paho's numeric error; during a disconnect paho returns `MQTT_ERR_NO_CONN`, which is handled, but a full paho queue (`max_queued_messages_set(0)` = unlimited) cannot happen; the 1000-event bound is the adapter's own | `client.py:176-192` | None; the design is documented |
| CODE-020 | `cli.main` decides the language before parsing and catches `KeyboardInterrupt` as exit 1 | `cli.py:72-85` | None |
| CODE-021 | The installer builds `bash -c` scripts for the container by string interpolation of shell constants (`$STAGE`, `$LIB`, `$CONFIG`, `$ANSWERS_FILE`) and one validated variable (`$SOURCE`) | `meshcore-home-gateway-lxc.sh:476-500,786-810,1187-1192` | Fine; a comment that only constants may be interpolated would keep it so |
| CODE-022 | `tools/release_plan.py` and `tools/public_interface.py` (nearly 1,000 lines) are run with `python3 -I` in CI and tested; they are the only tooling that decides a release | `tools/` | A note in `docs/development.md` that they are release-critical and reviewed as such |
| CODE-023 | The firmware test shims (`tests/firmware/shim`, `board`) are C++ stand-ins the project maintains per firmware release | `tests/firmware/` | A `README` in the directory naming what each stand-in replaces and why (the ADR says it; the directory does not) |
| CODE-024 | Comments and docstrings carry design reasoning at length (a strength for context, a cost for reading a function's body) | throughout | Keep; prefer references to the design where the paragraph exists there already |
| CODE-025 | `MqttBus.stop` publishes `offline` and waits up to 2.5 s; the unit's `TimeoutStopSec=20` and the bridge's 2 s drain add up to about 5 s at stop, leaving 15 s unused | `client.py:194-208`, the unit | Use the room for ARC-013's drain of the queue |

## Documentation

| ID | Title | Component | Related |
| --- | --- | --- | --- |
| DOC-001 | The topic table gives the gateway's own discovery topic the direction "bridge →" | `docs/mqtt-api.md`, the topic table, row `<prefix>/device/meshcore_home_gateway_<node_id>/config` | issue #23 (the owner's comment of 2026-10-25 and 2026-11-01 names it); `docs/home-assistant.md` "How discovery behaves"; `mqtt/client.py` (`ha_devices` subscriptions), `bridge._ha_device_message` |
| DOC-002 | Command age checks are attributed to the radio's clock, which does not enter them | `docs/design.md` §6.1 "Clock" ("If the radio is ahead of the bridge by more than 60 s the bridge logs a warning; command age checks may then reject commands … | SEC-002 |
| DOC-003 | Stale statements after the first release | `README.md:79-81` ("Before the first release, use a pre-release's own installer … or `main`'s copy"), `docs/deployment.md:44-48` ("Before the first release t… | none |
| DOC-004 | Retention of heard nodes: 7 days in ADR 0016 and `mqtt-api.md`, 30 days in the history | ADR 0016 ("sightings 7 days"), `docs/mqtt-api.md` "Nodes heard" ("the last 50 of the last 7 days"), `history.Retention.nodes_days = 30.0` (not set by `cli._s… | SEC-010, SEC-015 |
| DOC-005 | The threat table and the residual risks do not cover the observer's new edges | `docs/design.md` §7.1–7.4 | ARC-001, `02-threat-model.md` §7 (the comparison), SEC-001 to SEC-016 |
| DOC-006 | The security policy's "latest release" with a stream of pre-releases | `SECURITY.md` ("Only the latest release receives fixes") | ADR 0022, `docs/development.md` Release |
| DOC-007 | Features the README states from the companion's side only | `README.md` Features: "Telemetry of the household's companions: … asked of each in turn, within an airtime budget of its own, only from those who allow the g… | SEC-016 |
| DOC-008 | The acknowledgement's meaning is not stated where users read `confirmed` | `docs/mqtt-api.md` "Delivery" (`confirmed`: "the recipient acknowledged; `rtt_ms` from the radio"), `docs/home-assistant.md` "See whether it arrived", `docs/… | SEC-001 |
| DOC-009 | What the TCP interface exposes | `SECURITY.md`, `docs/design.md` §7.2 and §7.4, `docs/configuration.md` `[radio] host`, `docs/protocol.md` §1 | SEC-003 |
| DOC-010 | Bystanders' data has no privacy paragraph | `README.md` (no privacy section), `docs/design.md` (Q6 "Privacy: Message texts are logged only at debug level" is the only privacy requirement), `docs/home-a… | SEC-012, SEC-015, SEC-016 |
| DOC-011 | The design document's size and the missing diagrams | `docs/design.md` (1,280 lines; §6.6 and §6.11 are 100 and 170 lines of prose each; §8's table has 42 rows); the code's references to a "design 6c" with decis… | CODE-005, CODE-006 |
| DOC-012 | "Bridge" and "gateway" | The documents use "the bridge" 78 times and "the gateway" 316 times for the same program; `bridge.py` is the module; the README's first sentence says "gatewa… | ADR 0012 |
| DOC-013 | A short way in is missing | `README.md` (good; 180 lines), then `docs/design.md` (1,280 lines) or `docs/development.md` (394 lines, with a 25-line layout) | DOC-011 |
| DOC-014 | Smaller accuracy notes |  |  |
| DOC-015 | `nodes_days` (30) is not documented and not settable (DOC-004) | `docs/configuration.md` `[history]` | Document or expose |
| DOC-016 | Says what `state.json` holds in one 15-line sentence; does not say the configuration backup contains the password, that `history.sqlite3` and `state.json` contain positions, or that a restored `state.json` replays command timestamps (that one is said) | `docs/deployment.md` §4 "State and backup" | Three short sentences (SEC-009, SEC-015) |
| DOC-017 | The ACL example is good; it does not say what happens without one in one sentence, nor that the gateway cannot tell (SEC-004) | `docs/mqtt-api.md` "Securing the broker" | One sentence |
| DOC-018 | "QoS 1 for every entity from the device level since 2026.5.2" and three other version facts: valuable; a table of "gateway version → Home Assistant versions tested" is missing (ARC-016) | `docs/home-assistant.md` Requirements | A table, kept per release |
| DOC-019 | Six records carry dated amendments (ARC-008) | `CONTRIBUTING.md` "Records are never edited after acceptance" | State the amendment practice |
| DOC-020 | Says the hardware checklist runs "before every release"; the changelog says it was not run for any 1.1 pre-release and 1.0.0 shipped rc1's program after the checklist: consistent, but a reader of the checklist alone does not learn that pre-releases are unchecked on hardware | `docs/testing.md` | A sentence at the top of the checklist |
| DOC-021 | Has `telemetry` commented (`# enabled = true`) and `history` with one commented key; `contacts` is missing entirely (the `[contacts] manage` default is the one a shared household may want to change, SEC-008) | `deploy/config.example.toml` | Add `[contacts]` with `# manage = true` and a comment |
| DOC-022 | "the answer is the push 0x8B" and §5 explains the tag; neither says the request's tag is derived from the radio's clock (if it is: the reviewer did not verify) | `docs/protocol.md` §2, `SEND_TELEMETRY_REQ` | Verify and state, since SEC-002 makes the clock matter |
| DOC-023 | "Prefer USB over the unauthenticated TCP companion interface" | `README.md` Security | Add "which exports the radio's private key to any client" (SEC-003) |
| DOC-024 | Lists the records; the index has no status column (all accepted; two amended by later records) | `docs/decisions/README.md` | A status column and "amended by" |
| DOC-025 | `bug_report.yml` asks for the version and the firmware; it does not ask for the transport (USB/TCP) or whether the broker has an ACL, which most security-relevant reports need | Issue forms | Two fields |
| DOC-026 | 1,555 lines of evidence; the "Home Assistant MQTT" section alone is 450 lines; there is no index of the facts | `docs/platform-notes.md` | A table of contents at the top with one line per fact |

## Operations and deployment

| ID | Title | Component | Related |
| --- | --- | --- | --- |
| OPS-001 | Security updates in the container are not automated | `install_service` (installs `python3 python3-paho-mqtt ca-certificates`, nothing else), `docs/deployment.md` (no mention of `apt upgrade` or `unattended-upgr… | SUP-005, ARC-007, IMP-023 |
| OPS-002 | The installer's integrity chain: checksums from the same release, no attestation in the container, an unverifiable `main` path | `stage_program` and `FETCH_PY` (download `SHA256SUMS` and the files from the release, compare), `pack_text` (the language pack from the release or `main`, ch… | SUP-003, ADR 0013, ADR 0022, IMP-023 |
| OPS-003 | The container does not start without the radio, and a re-plugged radio needs a container reboot | `create_container` (`pct set --dev0 path=…,gid=…,mode=0660`), ADR 0006 Consequences ("the container does not start while the radio is unplugged (also at host… | OPS-004 |
| OPS-004 | A crash loop ends in a permanently failed unit with no notification | `deploy/systemd/meshcore-home-gateway.service` (`Restart=on-failure`, `RestartSec=5`, `StartLimitIntervalSec=600`, `StartLimitBurst=20`, no `OnFailure=`), `W… | OPS-003 (the same silence), ARC-012 |
| OPS-005 | The unit's hardening: what else is available | `deploy/systemd/meshcore-home-gateway.service` | SEC-002, SEC-009, CODE-002 |
| OPS-006 | Restarts and updates drop the queue; the stop timeout is shorter than an acknowledgement wait | `update()` and `reconfigure()` (`systemctl restart`/`stop`), `TimeoutStopSec=20`, `bridge.run` (2 s drain of inbound, no drain of outbound), design §3 non-goal | ARC-013 |
| OPS-007 | A firmware update of the radio switches contact management off until the gateway is updated | `core/profile.TESTED_FIRMWARE = {14}`, `contact_manager._firmware_allowed`, `radio/profile` (`auto` writes only on tested firmware), `docs/configuration.md` … | ADR 0020, ARC-016 |
| OPS-008 | Backups carry the password and the household's positions; restore has three files that must stay together | `docs/deployment.md` §4 "State and backup" (names the four files and what `state.json` holds), `docs/configuration.md` `[history]` | SEC-009, SEC-015, CODE-004 |
| OPS-009 | Disk: the journal, the history and `state.json` share 2 GB with no limits set | `create_container` (`--rootfs …:2` GB), the container's `journald.conf` (Debian default: `SystemMaxUse` 10 % of the file system, 200 MB here), `history.py` c… | SEC-010, ARC-012 |
| OPS-010 | The TCP radio in the installer: asked for without a word | `ask_radio` (offers "TCP" beside the serial devices; asks host and port), `check_broker`, `summary_text` | SEC-003, SEC-004 |
| OPS-011 | Observability: what an operator sees and does not see | `systemd.notify` (`STATUS=radio …, queue …`), the `state` document, the journal, Home Assistant's diagnostic entities | ARC-012, CODE-001 |
| OPS-012 | Smaller operational notes |  |  |
| OPS-013 | `apt-get update && apt-get install` without `--no-install-recommends`'s companion `-o APT::Install-Suggests=false` (fine) and without `apt-get clean` (a few MB) | `install_service` | `apt-get clean` |
| OPS-014 | `--timezone host`; the journal shows local time; the gateway's documents are UTC: a reader comparing them needs to know | The container's clock | A sentence in `docs/deployment.md` §4 |
| OPS-015 | Stops the service for the duration of the dialogs; an interrupted run restarts it (good); the radio's inbound queue (16) may overflow in a long session | `--reconfigure` | A note; nothing to fix |
| OPS-016 | The unit file of the newer version stays (documented); a newer unit with `LoadCredential` or a new `After=` is harmless for an older program; a newer unit that drops a directive the older program needs is not (none today) | Rollback | Keep the note; version the unit file with the program (a comment line with the version) |
| OPS-017 | Logs message texts (documented); also every frame code and decision; a day of `debug` on a busy mesh is large | Logging at `debug` | A warning at start when `log_level = "debug"` is set, naming what it logs |
| OPS-018 | Reaches every serial device of the container; only `dev0` exists; another device passed later would be reachable too | The service user in `dialout` | None; documented in the unit's comments |
| OPS-019 | Documented with the full path in the final message | `pct exec` from the host uses the host's `PATH` | None |
| OPS-020 | The script checks `pve-container ≥ 5.3.1` and `pveversion ≥ 8`; good | Proxmox 8 "with current updates" | None |
| OPS-021 | Each its own container and radio; the installer picks the next free id; nothing prevents two containers claiming the same `dev0` (Proxmox allows it; the second gets `EBUSY` on open: `TIOCEXCL`) | Two gateways on one host | A check in `ask_radio` whether another container already passes the device through |
| OPS-022 | `pct destroy`; the broker's retained topics and Home Assistant's devices stay (documented with the commands) | Uninstall | A `meshcore-home-gateway retire` command that clears the topics and sends the removals, run before the destroy (IMP) |

## Supply chain and release

| ID | Title | Component | Related |
| --- | --- | --- | --- |
| SUP-001 | One identity writes, reviews, merges and releases; a mistake or an injected instruction becomes a signed release within the hour | ADR 0022; `.github/workflows/ci.yml` (`release` job on push to `main`); the merge history above; `CONTRIBUTING.md` ("For a larger change, open an issue first… | SUP-002, SUP-003, OPS-002 |
| SUP-002 | The build job runs the development tool chain before the artifact is attested | `.github/workflows/release.yml` (`build` job: `make check` then `make release`, then `actions/upload-artifact`; `publish` job: download, `sha256sum --check`,… | SUP-001, SUP-004, ADR 0013 |
| SUP-003 | Release assets can be replaced after publication without changing the tag; the installer trusts them | GitHub releases (assets are editable by anyone with write access; the tag ruleset protects the tag, not the assets), `deploy/proxmox/meshcore-home-gateway-lx… | OPS-002 (the mitigation is described there: pin the digests in the installer; make the verified path the first) |
| SUP-004 | Dependabot's lock files are not what `make lock` writes | `.github/dependabot.yml`, the open branches `dependabot/pip/requirements/dev-tools-601fa647f8` and `dependabot/pip/home-assistant/pytest-homeassistant-custom… | none |
| SUP-005 | No vulnerability scanning of the locked tool chains | `requirements/dev.txt` (≈40 packages), `home-assistant/requirements.txt` (≈200 packages, Home Assistant and its dependencies), `.github/workflows/ci.yml` (no… | SUP-002 |
| SUP-006 | Contributor-supplied translations are text shown in security-relevant dialogs | `src/meshcore_home_gateway/locale/*.po`, `deploy/proxmox/locale/*.po` and the packs, `docs/translations.md` ("A language comes as a contribution of its catal… | ARC-011 |
| SUP-007 | What could not be verified |  |  |

## Improvement opportunities

| ID | Title | Component | Related |
| --- | --- | --- | --- |
| IMP-001 | Unguessable texts and a `suspect` flag for acknowledgements |  | SEC-001 (full design), DOC-008 |
| IMP-002 | An application-level second factor for commands |  | ADR 0009, SEC-001, SEC-003 (a forged frame over TCP), RES-003 |
| IMP-003 | Incremental contact reads with `GET_CONTACTS since` |  | SEC-011, RES-015 |
| IMP-004 | Publish only the channels the household configured |  | SEC-012 |
| IMP-005 | Telemetry opt-in per contact ("household" contacts) |  | SEC-016, ARC-002 |
| IMP-006 | Clock plausibility, a radio clock offset in `state`, `set_clock = "always"` |  | SEC-002 |
| IMP-007 | Insecure-broker warnings and an anonymous-access probe |  | SEC-004 |
| IMP-008 | Self-healing documents and origin stamps |  | SEC-005, ARC-006 |
| IMP-009 | A random client-id suffix and bounded announcements |  | SEC-006 |
| IMP-010 | Name collision flags |  | SEC-007 |
| IMP-011 | Separate Home Assistant exposure from function |  | SEC-008 |
| IMP-012 | A password file and a systemd credential |  | SEC-009 |
| IMP-013 | A health section in `state` and a *Gateway degraded* sensor |  | ARC-012, CODE-008, OPS-011, OPS-009 |
| IMP-014 | Persist the outbox across restarts, drain on stop |  | ARC-013, OPS-006 |
| IMP-015 | MQTT 5: message expiry, correlation, user properties |  | ARC-014 |
| IMP-016 | Three CLI commands: `status`, `backup`, `retire` |  | OPS-011, OPS-008, OPS-022 |
| IMP-017 | Property-based tests of the parsers |  | CODE-009 |
| IMP-018 | Rate limits on `received` per sender and per channel |  | SEC-012, SEC-013 |
| IMP-019 | Bounded growth under advert floods |  | SEC-010, SEC-011 |
| IMP-020 | Loop-lag measurement and a grace in the radio timeout |  | CODE-001, CODE-002 |
| IMP-021 | One state store |  | ARC-004, CODE-003, CODE-004 |
| IMP-022 | Structure: ports, a wiring module, smaller modules |  | ARC-003, ARC-009, CODE-005 |
| IMP-023 | Installer: pinned digests, updates, a udev rule, warnings, journald limits |  | OPS-001, OPS-002, OPS-003, OPS-009, OPS-010 |
| IMP-024 | Unit: failure notification, memory limit, clock dependency |  | OPS-004, OPS-005 |
| IMP-025 | Guide examples for outages and shared households |  | OPS-003, OPS-004, SEC-008 |
| IMP-026 | A threat-model document and an ADR paragraph on boundaries |  | ARC-001, DOC-005 |
| IMP-027 | Release process: a second identity, a cooldown, code owners, a reviewer's checklist |  | SUP-001 |
| IMP-028 | Build isolation and a rebuild job |  | SUP-002 |
| IMP-029 | Vulnerability scanning of the lock files |  | SUP-005 |
| IMP-030 | Documentation structure |  | DOC-011, DOC-013, CODE-006 |
| IMP-031 | Persist the message airtime bucket's level across restarts (or document the asymmetry with the lanes) |  | ARC-005 |
| IMP-032 | Message priority: a JSON envelope on `send/<alias>` (`{"text": "…", "priority": "alert"}`) or `[targets.<alias>] priority`, with an alert overtaking queued status messages (design §12 names it) |  | design §12 |
| IMP-033 | `contacts.manage_reason` in the index (`untested_firmware`, `management_off`, `no_radio`) so Home Assistant and the dashboard can say why the controls are unavailable |  | OPS-007 |
| IMP-034 | A troubleshooting row for "controls unavailable after a radio firmware update" and a warning line at connect on untested firmware |  | OPS-007 |
| IMP-035 | `--update` waits for `state.queue == 0` (up to a minute) before restarting |  | OPS-006 |
| IMP-036 | A warning at start when `log_level = "debug"` (texts and positions go to the journal) |  | OPS-017 |
| IMP-037 | `nodes_days` as a setting or a documented constant, aligned with ADR 0016 |  | DOC-004 |
| IMP-038 | `[messages] clean_received` (apply the outbound cleaning to inbound texts) for operators who prefer it; default off to keep "texts are data" |  | SEC-017 |
| IMP-039 | Truncate the alias echoed in a rejected `delivery` event to 64 characters |  | SEC-017 |
| IMP-040 | Rate-limit the "malformed push" warning and end the session after N in a row |  | SEC-014 |
| IMP-041 | Detect a contested TCP link from a continuous radio uptime across a reconnect; publish `radio_link_contested` |  | SEC-003 |
| IMP-042 | `check-config` summary line for the radio names the transport's security ("tcp, unauthenticated") |  | SEC-003, OPS-010 |
| IMP-043 | An exported `.github/rulesets/*.json` of the branch and tag rulesets, kept in the repository |  | SUP-007 |
| IMP-044 | A `README` in `tests/firmware/` naming each stand-in |  | CODE-023 |
| IMP-045 | Remove `# fmt: skip` in one commit, or write the convention down |  | CODE-007 |
| IMP-046 | An index (table of contents of facts) at the top of `docs/platform-notes.md` |  | DOC-026 |
| IMP-047 | History export: `meshcore-home-gateway history export --since … --format jsonl |  | csv` (ADR 0016 anticipates it) |
| IMP-048 | `apt-get clean` after the install; `--offline` is out of scope |  | OPS-013 |
| IMP-049 | The dashboard's deferred items (reason codes as texts, remove and telemetry controls, heard repeaters on the map) as the project lists them in design §6.13 |  | design §6.13 |
| IMP-050 | `Disabled` frames from the radio (`writeDisabledFrame`, for commands compiled out) are mapped to `disabled` in `CommandFailedError`; a `settings` state `disabled_in_firmware` would tell "compiled out" from "unsupported" for the profile |  | `radio/profile.py` |

## Extension opportunities

| ID | Title | Component | Related |
| --- | --- | --- | --- |
| EXT-001 | A Home Assistant add-on |  | ADR 0002 (rejected "inside the Home Assistant virtual machine (add-on or custom integration)"), ADR 0005, ADR 0006 |
| EXT-002 | Packaging: PyPI, a Debian package, an OCI image |  | ADR 0005, ADR 0013 ("a PyPI release can be added later on top of this workflow"), ADR 0006 |
| EXT-003 | BLE as a radio transport |  | design §3 (non-goal "BLE as a transport (possible later as another adapter)"), §12, ADR 0005, ADR 0006, ARC-003 (the port) |
| EXT-004 | A machine-readable API description (AsyncAPI) |  | issue #23, ADR 0023 (the snapshot already computes the structure) |
| EXT-005 | Telemetry of the household's sensor nodes |  | ADR 0014 (in scope: "telemetry pull from the household's companions"; out: "requests to nodes that are not the household's contacts"), ADR 0018, `telemetry_poller` (candidates: chat contacts only) |
| EXT-006 | The household's own repeaters: login, status, telemetry |  | ADR 0014 (out of scope: "repeater and room-server administration"; revisit "when the household's own repeaters or sensors should be administered"), ADR 0002's revisit clause |
| EXT-007 | Mesh diagnostics: paths, traces and a neighbourhood map |  | ADR 0014, ADR 0021 ("the heard repeaters with known positions" on the map, deferred), `protocol.md` §4 (`PATH_DISCOVERY_RESPONSE`, `TRACE_DATA` are ignored today) |
| EXT-008 | Channels managed from the gateway |  | ADR 0017 ("channels stay configuration-only, because a channel reaches everyone with its secret"), the codec's deny list (channel secrets are never read: `protocol.md` §3) |
| EXT-009 | Built-in replies to commands |  | ADR 0008 (budget), ADR 0009, the guide's "Status on request" example |
| EXT-010 | Migration path from `SEND_TELEMETRY_REQ` (39) to `SEND_BINARY_REQ` (50) |  | ADR 0018 (command 39 "can deprecate"; command 50's answer carries no sender identity) |
| EXT-011 | A Homie adapter for openHAB and others |  | ADR 0015 ("Neutral: it can be added later as a further adapter fed by the same providers") |
| EXT-012 | A web dashboard over MQTT-over-WebSockets |  | ADR 0021 ("Later: … a web dashboard on the same documents"), ADR 0015 ("the web dashboard as a static page that connects to the broker over MQTT over WebSockets with its own broker user and ACL") |
| EXT-013 | A Prometheus exporter as a sidecar |  | ADR 0015 (no listener in the gateway) |
| EXT-014 | Two gateways, one household |  | design §3 (one radio per process), ARC-015 |
| EXT-015 | Translated user guides (German first) once the catalogs exist, generated from the English with a translation memory, reviewed by a speaker |  | ADR 0019 ("translated guides can follow") |
| EXT-016 | A `meshcore-home-gateway doctor` command that runs the checks of this review's operational findings (clock offset, anonymous broker, TCP posture, disk, updates pending, firmware tested) and prints a report |  | OPS-011, IMP-016 |
| EXT-017 | A per-target `template` setting that prefixes or suffixes texts (a site name, a timestamp) so that automations send bare texts |  | `[targets.<alias>]` |
| EXT-018 | Delivery receipts as Home Assistant notifications: a `notify` action that waits for `confirmed` and raises an error otherwise, through a script the guide ships (today the delivery is an event the automation has to correlate by `id`) |  | `docs/home-assistant.md` |
| EXT-019 | A simulated-air "playground": `make firmware-play` that starts a companion, a repeater and the gateway against a local broker for manual exploration, built from the existing fixtures |  | ADR 0026, `tests/firmware/` |
| EXT-020 | Support for firmware 1.19+ as it appears: the two-line bump in `sources.toml`, the firmware tests, the `TESTED_FIRMWARE` addition, with a documented turnaround target so that OPS-007's gap is short |  | ADR 0020, OPS-007 |

## Research notes

| ID | Title | Component | Related |
| --- | --- | --- | --- |
| RES-001 | The acknowledgement is a clear-text hash of (timestamp, attempt, text, sender key) |  |  |
| RES-002 | A contact's advert is dropped unless its timestamp is newer than the stored one |  |  |
| RES-003 | The cost of a 6-byte (48-bit) key prefix collision |  |  |
| RES-004 | Time on air at EU/UK Narrow of the packets that matter |  |  |
| RES-005 | Experiment: an advert flood with invented keys against the observer and the history |  |  |
| RES-006 | Data about bystanders and the household exemption |  |  |
| RES-007 | The radio's clock: forward-only over the protocol, bootstrapped from contacts at boot, persistent on RTC boards |  |  |
| RES-008 | Private key export and import are enabled in the release firmware's common build flags |  |  |
| RES-009 | Experiment: an event-loop stall longer than the command timeout ends the session |  |  |
| RES-010 | MQTT 3.1.1: a second connection with the same client id disconnects the first |  |  |
| RES-011 | Home Assistant's user model has no entity-level permissions in the UI |  |  |
| RES-012 | GitHub release assets can be replaced after publication |  |  |
| RES-013 | systemd features available on Debian 13 |  |  |
| RES-014 | MQTT 5 support in the project's platforms |  |  |
| RES-015 | `GET_CONTACTS` takes an optional `since` and lists contacts with `lastmod > since` |  |  |
| RES-016 | The telemetry request's tag is the radio's clock, made unique |  |  |
| RES-017 | No forward secrecy in MeshCore direct messages |  |  |
| RES-018 | `uptime_s` in the core statistics counts milliseconds since boot |  |  |
| RES-019 | The Dependabot-regenerated lock files differ in form from `make lock`'s |  |  |
| RES-020 | `journald` rate limits |  |  |
| RES-021 | Mosquitto facts the project relies on (confirmed where the reviewer could) |  |  |
| RES-022 | Python version floor and the installer's `tarfile` filter |  |  |
| RES-023 | Related projects, as the records name them, with the dates checked |  |  |
| RES-024 | The review's own method, for reproducibility |  |  |

## Coverage

| Area | Depth |
| --- | --- |
| `src/meshcore_home_gateway/` (45 modules) | read in full; the bridge, session, transport, observer, contact manager, poller, config, state, history, client, discovery, contact devices, manage entities, views, codec, packet, links, lpp, text, commands, contacts, budget, profile read line by line; i18n, catalog, texts, dashboard, cli read for structure and the security-relevant paths |
| `deploy/` | the installer read in full; the unit and the example configuration in full |
| `tools/` | `build_pyz.py` and `fetch_firmware.py` in full; `release_plan.py`, `public_interface.py`, `i18n.py`, `release_notes.py` for structure |
| `tests/` | the layout, the emulator, the architecture test, the docs test; individual tests where a claim needed one; the unit, component and emulator suites run |
| `home-assistant/tests/`, `tests/firmware/` | the layout and the fixtures; not run |
| `.github/` | workflows, Dependabot, templates in full |
| `docs/` | every guide and record in full |
| Open branches | `docs/usb-builds-without-usb-interface` (PR #10) and the two Dependabot branches: diffs read; nothing in them changes a finding (PR #10 improves the coverage of USB builds in the platform notes and the troubleshooting table; the Dependabot locks are RES-019 / SUP-004) |
| Firmware | the pinned sources for every claim marked verified; `platformio.ini` and `docs/` of the tag fetched separately |
| Not covered | hardware behaviour; a Proxmox host; a running Home Assistant; the broker, gettext, firmware and contract suites; GitHub settings that need credentials |
