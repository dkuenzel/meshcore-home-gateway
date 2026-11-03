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
| [0005](0005-runtime-dependencies-from-the-distribution.md) | Runtime dependencies: standard library and the distribution's paho-mqtt | accepted |
| [0006](0006-deployment-in-an-unprivileged-proxmox-container.md) | Deployment in an unprivileged Proxmox container by a host script with dialogs | accepted; amended 2026-10-23 |
| [0007](0007-hexagonal-architecture-with-an-asyncio-core.md) | Ports and adapters around a pure domain core, one asyncio event loop | accepted |
| [0008](0008-outbound-safety-targets-airtime-budget-deduplication.md) | Outbound safety: configured targets, an airtime budget and deduplication | accepted; targets partly superseded by 0017 (every contact a recipient) |
| [0009](0009-inbound-commands-from-allowlisted-contacts.md) | Inbound commands only from allowlisted contacts, with replay protection | accepted; amended 2026-10-19 and 2026-10-23 |
| [0010](0010-home-assistant-through-mqtt-device-discovery.md) | Home Assistant integration through MQTT device discovery | accepted |
| [0011](0011-neutral-english-language-project-under-mit.md) | A neutral, English-language project under the MIT license | accepted |
| [0012](0012-project-name-meshcore-home-gateway.md) | The project is named meshcore-home-gateway | accepted; amended 2026-11-03 (the client identifier) |
| [0013](0013-signed-releases-with-github-artifact-attestations.md) | Signed releases with GitHub artifact attestations | accepted; amended by 0022 |
| [0014](0014-the-gateway-as-the-households-mesh-observer.md) | The gateway as the household's mesh observer: positions, telemetry, contacts and dashboards | accepted |
| [0015](0015-provider-neutral-documents-on-the-bus.md) | Provider-neutral documents on the bus; front ends are adapters | accepted |
| [0016](0016-history-in-a-local-sqlite-database.md) | History in a local SQLite database | accepted |
| [0017](0017-contacts-on-the-radio-each-with-its-own-device.md) | Contacts live on the radio; each contact is a recipient with its own device | accepted |
| [0018](0018-telemetry-pulled-from-the-households-companions.md) | Telemetry and positions pulled from the household's companions, in budget lanes | accepted; amended by 0025 |
| [0019](0019-internationalisation-with-gettext.md) | Internationalisation with gettext catalogs; English source, German, Dutch, Polish, French | accepted; amended by 0027 |
| [0020](0020-managed-radio-profile-for-the-observer.md) | A managed radio profile for the observer, reconciled at every connect | accepted |
| [0021](0021-generated-dashboards-for-desktop-and-mobile.md) | Generated dashboards for desktop and mobile | accepted |
| [0022](0022-releases-tagged-by-ci-on-main.md) | Releases tagged by CI on main, from the version a pull request writes | accepted; amended by 0023 |
| [0023](0023-public-interfaces-kept-by-a-snapshot.md) | Four public interfaces, kept within a major version by a policy and a snapshot of the code | accepted |
| [0024](0024-telemetry-values-in-home-assistant-expire.md) | Telemetry values in Home Assistant expire when the companion stops answering | accepted |
| [0025](0025-positions-in-adverts-checked-from-the-packet-log.md) | Positions in adverts: checked from the packet log; the radio's record decides until then | accepted |
| [0026](0026-gateway-tested-against-the-real-firmware-built-natively.md) | The gateway is tested against the real firmware, built natively | accepted |
| [0027](0027-message-catalogs-compiled-when-loaded.md) | Message catalogs compiled when they are loaded | accepted |
