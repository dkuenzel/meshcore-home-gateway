# Architecture decision records

Decisions with lasting consequences, in the [MADR 4.0](https://adr.github.io/madr/) format. Records
are not edited after acceptance; a new record supersedes an old one, and both link to each other.
New records start from [`template.md`](template.md).

| No. | Decision | Status |
| --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions as MADR files in the repository | accepted |
| [0002](0002-standalone-bridge-instead-of-home-assistant-integration.md) | A standalone bridge service instead of a Home Assistant integration | accepted |
| [0003](0003-mqtt-as-the-message-bus.md) | MQTT as the message bus between the bridge and its users | accepted |
| [0004](0004-own-implementation-of-the-companion-protocol-subset.md) | Own implementation of the companion protocol subset | accepted |
| [0005](0005-runtime-dependencies-from-the-distribution.md) | Runtime dependencies: standard library and the distribution's paho-mqtt | accepted |
| [0006](0006-deployment-in-an-unprivileged-proxmox-container.md) | Deployment in an unprivileged Proxmox container by a host script with dialogs | accepted |
| [0007](0007-hexagonal-architecture-with-an-asyncio-core.md) | Ports and adapters around a pure domain core, one asyncio event loop | accepted |
| [0008](0008-outbound-safety-targets-airtime-budget-deduplication.md) | Outbound safety: configured targets, an airtime budget and deduplication | accepted |
| [0009](0009-inbound-commands-from-allowlisted-contacts.md) | Inbound commands only from allowlisted contacts, with replay protection | accepted |
| [0010](0010-home-assistant-through-mqtt-device-discovery.md) | Home Assistant integration through MQTT device discovery | accepted |
| [0011](0011-neutral-english-language-project-under-mit.md) | A neutral, English-language project under the MIT license | accepted |
| [0012](0012-project-name-meshcore-home-gateway.md) | The project is named meshcore-home-gateway | accepted |
| [0013](0013-signed-releases-with-github-artifact-attestations.md) | Signed releases with GitHub artifact attestations | accepted |
| [0014](0014-the-gateway-as-the-households-mesh-observer.md) | The gateway as the household's mesh observer: positions, telemetry, contacts and dashboards | accepted |
| [0015](0015-provider-neutral-documents-on-the-bus.md) | Provider-neutral documents on the bus; front ends are adapters | accepted |
| [0016](0016-history-in-a-local-sqlite-database.md) | History in a local SQLite database | accepted |
| [0017](0017-contacts-on-the-radio-each-with-its-own-device.md) | Contacts live on the radio; each contact is a recipient with its own device | accepted |
| [0018](0018-telemetry-pulled-from-the-households-companions.md) | Telemetry and positions pulled from the household's companions, in budget lanes | accepted |
| [0019](0019-internationalisation-with-gettext.md) | Internationalisation with gettext catalogs; English source, German, Dutch, Polish, French | accepted |
| [0020](0020-managed-radio-profile-for-the-observer.md) | A managed radio profile for the observer, reconciled at every connect | accepted |
| [0021](0021-generated-dashboards-for-desktop-and-mobile.md) | Generated dashboards for desktop and mobile | accepted |
| [0022](0022-releases-tagged-by-ci-on-main.md) | Releases tagged by CI on main, from the version a pull request writes | accepted |
