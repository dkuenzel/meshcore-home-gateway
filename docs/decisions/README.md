# Architecture decision records

Decisions with lasting consequences, in the [MADR 4.0](https://adr.github.io/madr/) format. The
text of an accepted record stays as it was; a dated amendment at its end may correct or narrow it
or name a later record that supersedes a part of it, and a reversed decision needs a new record
([ADR 0001](0001-record-architecture-decisions.md)). New records start from
[`template.md`](template.md).

| No. | Decision | Status |
| --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions as MADR files in the repository | accepted; amended 2026-11-02 (the amendment practice) |
| [0002](0002-standalone-bridge-instead-of-home-assistant-integration.md) | A standalone bridge service instead of a Home Assistant integration | accepted; scope partly superseded by 0014; note 2026-11-02 (sizes) |
| [0003](0003-mqtt-as-the-message-bus.md) | MQTT as the message bus between the bridge and its users | accepted |
| [0004](0004-own-implementation-of-the-companion-protocol-subset.md) | Own implementation of the companion protocol subset | accepted; note 2026-11-02 (sizes) |
| [0005](0005-runtime-dependencies-from-the-distribution.md) | Runtime dependencies: the standard library and the distribution's paho-mqtt | accepted; amended 2026-11-03 (updates installed in the container; the tool chains of the build, the tests and the audit) |
| [0006](0006-deployment-in-an-unprivileged-proxmox-container.md) | Deployment in an unprivileged Proxmox container by a host script with dialogs | accepted; amended 2026-10-23 |
| [0007](0007-hexagonal-architecture-with-an-asyncio-core.md) | Ports and adapters around a pure domain core, one asyncio event loop | accepted; amended 2026-11-03 (the ports module; the parts of the bridge and their wiring) |
| [0008](0008-outbound-safety-targets-airtime-budget-deduplication.md) | Outbound safety: configured targets, an airtime budget and deduplication | accepted; targets partly superseded by 0017 (every contact a recipient); amended 2026-11-03 (the budget's level kept, 0028) |
| [0009](0009-inbound-commands-from-allowlisted-contacts.md) | Inbound commands only from allowlisted contacts, with replay protection | accepted; amended 2026-10-19, 2026-10-23 and 2026-11-04; amended 2026-11-07 (an ignored code secret; a code does not bind the text) |
| [0010](0010-home-assistant-through-mqtt-device-discovery.md) | Home Assistant integration through MQTT device discovery | accepted |
| [0011](0011-neutral-english-language-project-under-mit.md) | A neutral, English-language project under the MIT license | accepted |
| [0012](0012-project-name-meshcore-home-gateway.md) | The project is named meshcore-home-gateway | accepted; amended 2026-11-03 (the client identifier) |
| [0013](0013-signed-releases-with-github-artifact-attestations.md) | Signed releases with GitHub artifact attestations | accepted; amended by 0022; amended 2026-11-03 (the installer pins the program; build apart, rebuilt and compared) |
| [0014](0014-the-gateway-as-the-households-mesh-observer.md) | The gateway as the household's mesh observer: positions, telemetry, contacts and dashboards | accepted |
| [0015](0015-provider-neutral-documents-on-the-bus.md) | Provider-neutral documents on the bus; front ends are adapters | accepted; amended 2026-11-03 (the integrity model), 2026-11-04 (the Homie device) and 2026-11-07 (its gateway node); amended by 0031 (the drafts of Home Assistant's chats) |
| [0016](0016-history-in-a-local-sqlite-database.md) | History in a local SQLite database | accepted; amended 2026-11-03 (the purge in a thread; the state file held in memory) |
| [0017](0017-contacts-on-the-radio-each-with-its-own-device.md) | Contacts live on the radio; each contact is a recipient with its own device | accepted |
| [0018](0018-telemetry-pulled-from-the-households-companions.md) | Telemetry and positions pulled from the household's companions, in budget lanes | accepted; amended by 0025; amended 2026-11-04 (sensors may be asked) and 2026-11-05 (the household's companions asked by default) |
| [0019](0019-internationalisation-with-gettext.md) | Internationalisation with gettext catalogs; English source, German, Dutch, Polish, French | accepted; amended by 0027; amended 2026-11-11 (numbers in the dashboard's language; English and German offered, others when a translator offers one) |
| [0020](0020-managed-radio-profile-for-the-observer.md) | A managed radio profile for the observer, reconciled at every connect | accepted; amended 2026-11-06 (a restart sets back a radio clock that is ahead) |
| [0021](0021-generated-dashboards-for-desktop-and-mobile.md) | Generated dashboards for desktop and mobile | accepted; amended by 0031 |
| [0022](0022-releases-tagged-by-ci-on-main.md) | Releases tagged by CI on main, from the version a pull request writes | accepted; amended by 0023 and 0030; amended 2026-11-05 (a release tagged a day after its version), 2026-11-06 (a candidate tagged at once) and 2026-11-07 (a tag pushed by hand checked too) |
| [0023](0023-public-interfaces-kept-by-a-snapshot.md) | Four public interfaces, kept within a major version by a policy and a snapshot of the code | accepted |
| [0024](0024-telemetry-values-in-home-assistant-expire.md) | Telemetry values in Home Assistant expire when the companion stops answering | accepted |
| [0025](0025-positions-in-adverts-checked-from-the-packet-log.md) | Positions in adverts: checked from the packet log; the radio's record decides until then | accepted |
| [0026](0026-gateway-tested-against-the-real-firmware-built-natively.md) | The gateway is tested against the real firmware, built natively | accepted; amended by 0030 |
| [0027](0027-message-catalogs-compiled-when-loaded.md) | Message catalogs compiled when they are loaded | accepted |
| [0028](0028-outgoing-queue-kept-across-restarts.md) | The outgoing queue kept across restarts, alerts first | accepted; amended 2026-11-03 (where its tests are) and 2026-11-07 (the queue kept through a stop; an alert ends a wait) |
| [0029](0029-what-a-contact-is-trusted-with.md) | What a contact of the radio is trusted with | accepted; amended 2026-11-05 (the defaults of 2.0.0); the default of `heard_nodes` superseded by 0032 (2026-11-11) |
| [0030](0030-release-checked-by-ci-self-test-and-use.md) | A release checked by CI, the installer's self-test and the maintainer's use, not by a manual checklist | accepted; amended 2026-11-06 with 0022 (a candidate tagged once CI passed) |
| [0031](0031-household-chat-problems-and-settings-on-the-generated-dashboard.md) | Household chat, problems and settings on the generated dashboard | accepted; amended 2026-11-11 (the refused line above the thread) |
| [0032](0032-heard-nodes-kept-with-name-signal-and-time-by-default.md) | Heard nodes are kept with name, signal and time by default | accepted |
