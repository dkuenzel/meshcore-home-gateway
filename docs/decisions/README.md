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
