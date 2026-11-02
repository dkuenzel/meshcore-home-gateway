# 10 Extension opportunities

New capabilities, each with a motivation, a design sketch and the decisions it touches. The
reviewer does not say which are worth doing; it says what each would take and what it would
cost against the records the project has made. Items are `EXT-nnn`.

## A Reaching more households

### EXT-001 A Home Assistant add-on

| | |
| --- | --- |
| Touches | ADR 0002 (rejected "inside the Home Assistant virtual machine (add-on or custom integration)"), ADR 0005, ADR 0006 |

**Motivation.** Most Home Assistant users run Home Assistant OS on a small box or a VM, not
Proxmox. For them the Proxmox installer does nothing, and the "any systemd Linux" path means a
second machine. An add-on is a container that the Supervisor runs beside Home Assistant with a
USB device passed through (`uart: true` or `devices:`), configured through the UI, updated from
the add-on repository, and talking to the Mosquitto add-on on the same host.

**Why ADR 0002's reasons do not apply as they did.** ADR 0002 rejected the custom integration
because its code runs *inside* Home Assistant's process with dependencies installed at runtime
from PyPI. An add-on is a separate container with a pinned image: the gateway's supply chain
stays its own (the image is built by the project's workflow from the zipapp and Debian's
`python3-paho-mqtt`), the process is isolated (a crash does not touch Home Assistant), and the
USB device reaches the container the way Home Assistant OS passes devices to add-ons. ADR 0006's
"Docker needs a Docker host" objection does not apply on Home Assistant OS, which is one.

**Design sketch.** A repository `meshcore-home-gateway-addon` (or a directory `addon/`) with
`config.yaml` (name, version, `arch: [amd64, aarch64]`, `uart: true`, `map: [config:rw]`,
options schema mirroring `config.toml` or a single "configuration file" text), a `Dockerfile`
from `ghcr.io/home-assistant/*-base-debian` installing `python3-paho-mqtt` and the zipapp
(verified against the release's digest at build time), a `run.sh` that writes `config.toml` from
the options (through `config render`, which exists for this purpose) and runs the gateway with
the add-on's `/data` as the state directory; the broker's credentials from the Mosquitto add-on's
service discovery (`bashio::services mqtt`). The add-on's image is built by a workflow in the
project (attested like the other files). The systemd sandbox does not apply inside the add-on's
container; the add-on's own isolation (AppArmor profile, no `host_network`, no privileged) takes
its place and the add-on documentation should say which.

**Cost.** A second deployment path to test (the Supervisor has a test harness, `devcontainer`),
a CI job that builds the image, and a maintained AppArmor profile. The payoff is the largest
audience the project can reach.

### EXT-002 Packaging: PyPI, a Debian package, an OCI image

| | |
| --- | --- |
| Touches | ADR 0005, ADR 0013 ("a PyPI release can be added later on top of this workflow"), ADR 0006 |

**Motivation.** The zipapp is the right artefact for the container; other users want `pip
install`, `apt install`, or `docker run`.

**Design sketch.** PyPI with trusted publishing from the release workflow (attestations are
supported by PyPI; the wheel already exists); a Debian source package in a separate repository
(`debian/` with `dh_python3`, depending on `python3-paho-mqtt`, installing the unit and the
example configuration; built by the release workflow as a `.deb` asset, attested); an OCI image
built from `debian:trixie-slim` with the two packages and the zipapp, published to `ghcr.io`,
with the documented `docker run --device /dev/serial/by-id/… -v state:/var/lib/…` and the note
that the systemd sandbox does not apply (the image runs the process directly; `--cap-drop ALL`,
`--read-only`, a non-root user). Each is a day of work and a CI job; each widens the installed
base and the support surface.

### EXT-003 BLE as a radio transport

| | |
| --- | --- |
| Touches | design §3 (non-goal "BLE as a transport (possible later as another adapter)"), §12, ADR 0005, ADR 0006, ARC-003 (the port) |

**Motivation.** Radios whose USB port is power-only or whose firmware has no USB interface (PR
#10 documents four such builds), and placements where the radio should sit on a window sill
away from the host.

**Design sketch.** The companion protocol over BLE uses the Nordic UART Service (two
characteristics, write and notify) with the same `<`/`>` framing, with a PIN pairing
(`DEVICE_INFO` carries the stored PIN; `CMD_SET_BLE_PIN` is on the deny list and stays there).
`bleak` is in Debian 13 (`python3-bleak`), which satisfies ADR 0005; it needs BlueZ over D-Bus,
which an unprivileged LXC container does not have by default (the host's Bluetooth adapter must
be passed through and `dbus` reachable), so the Proxmox path is harder than the USB one and
should be documented as "another systemd Linux with Bluetooth" first. The transport implements
`Transport` (`needs_flush = False`; a BLE connection starts the parser afresh? to be verified in
the firmware's `SerialBLEInterface`), with the notify callback feeding the same frame decoder.
The session's single-client assumption holds (BLE connects one central). Security: BLE pairing
with a PIN authenticates the client to the radio, unlike TCP; the PIN is in `DEVICE_INFO` only
after pairing.

**Cost.** A new adapter and its tests (the emulator's transport protocol makes unit tests easy;
hardware tests need a BLE host), plus ARC-003's port refactoring, which this would motivate.

### EXT-004 A machine-readable API description (AsyncAPI)

| | |
| --- | --- |
| Touches | issue #23, ADR 0023 (the snapshot already computes the structure) |

**Motivation.** The owner's own issue: consumers want to pin the MQTT API per release.

**Design sketch.** Generate `docs/mqtt-api.asyncapi.yaml` from `tests/public_interface.json`
(topics with retain and QoS, subscriptions, key paths, value lists) plus hand-written
descriptions and JSON schemas for the payloads (the views are the schema's source; a small
`tools/asyncapi.py` can derive types from the fixture documents the snapshot already builds,
with `null`able marked where the views use `None`). Validate in CI with the AsyncAPI CLI in a
job that may use `npx` (a development-only dependency; or with a JSON-schema check in Python
against the generated document's own meta-schema). Attach to the release. The direction fix of
DOC-001 goes in first.

## B More of the mesh

### EXT-005 Telemetry of the household's sensor nodes

| | |
| --- | --- |
| Touches | ADR 0014 (in scope: "telemetry pull from the household's companions"; out: "requests to nodes that are not the household's contacts"), ADR 0018, `telemetry_poller` (candidates: chat contacts only) |

**Motivation.** MeshCore's sensor firmware (type 4) answers the same `SEND_TELEMETRY_REQ`; a
household with a mesh sensor in the garden shed wants its readings in Home Assistant exactly as a
companion's. The poller, the LPP decoder, the readings' entities and the history all exist.

**Design sketch.** Admit `NodeType.SENSOR` (and perhaps room servers, which report battery) to
the candidate set under `[telemetry] ask_types = ["chat", "sensor"]`, with the same exclusion
mechanism and opt-in (IMP-005) so that a neighbour's sensor is not polled; sensors have no
"moving" state (fixed interval, say 15 minutes, configurable); no tracker unless the answer
carries a GPS reading; their `telemetry_state` vocabulary is the same. The firmware's sensor
example (`examples/simple_sensor`) is in the pinned source and the firmware tests could build it
too. Note ADR 0018's warning about polling load (meshcore-ha issue 214): sensors answer on a
path; the lanes still apply.

### EXT-006 The household's own repeaters: login, status, telemetry

| | |
| --- | --- |
| Touches | ADR 0014 (out of scope: "repeater and room-server administration"; revisit "when the household's own repeaters or sensors should be administered"), ADR 0002's revisit clause |

**Motivation.** A household that runs its own repeater on the roof wants its battery, uptime,
noise floor and packet counters in Home Assistant, and a warning when it goes silent. The
companion protocol supports it (`CMD_SEND_LOGIN` with the repeater's password, `CMD_GET_STATUS`
→ `STATUS_RESPONSE`, telemetry requests to repeaters, `CMD_SEND_TRACE_PATH`).

**Design sketch.** A `[repeaters.<alias>]` table with the key and a password file (the password
travels over the air encrypted to the repeater; it is a credential the gateway holds); the
poller gains a second schedule for repeaters (status every N minutes, in its own lane); a
`repeaters/<key>` document and a device per repeater with diagnostic sensors; the deny list keeps
the CLI command out (no remote administration, only reading). The security model changes a
little: the gateway holds a credential for another node; `ENABLE_PRIVATE_KEY_EXPORT`-style
concerns apply to the repeater's own interfaces, not the gateway's.

### EXT-007 Mesh diagnostics: paths, traces and a neighbourhood map

| | |
| --- | --- |
| Touches | ADR 0014, ADR 0021 ("the heard repeaters with known positions" on the map, deferred), `protocol.md` §4 (`PATH_DISCOVERY_RESPONSE`, `TRACE_DATA` are ignored today) |

**Motivation.** The observer already knows each contact's stored path (hops and repeater hashes)
and hears repeaters' adverts (with positions, which it does not keep for strangers by policy).
A household debugging its coverage wants to see which repeater carries which contact and how the
signal is.

**Design sketch.** Keep positions of *repeaters* the household marks as its own (an extension of
the household mark of IMP-005 to repeaters), draw them on the map (ADR 0021's deferred item),
show each contact's path as the list of repeater hashes resolved to names where known, and
offer `CMD_SEND_TRACE_PATH` on request (a transmission, within the lanes) for a per-hop SNR
trace shown in the dashboard. Privacy stays: strangers' positions are still not kept.

### EXT-008 Channels managed from the gateway

| | |
| --- | --- |
| Touches | ADR 0017 ("channels stay configuration-only, because a channel reaches everyone with its secret"), the codec's deny list (channel secrets are never read: `protocol.md` §3) |

**Motivation.** Adding a private channel to the radio needs the phone app and the radio
unplugged from the gateway. `CMD_SET_CHANNEL` exists.

**Design sketch.** A request `channels/<index>/set` with a name and either a secret (from a
`meshcore://channel/…` link) or "hashtag" (the secret derived from the name as the app does);
written like contacts (manage switch, tested firmware, one write, read back); never reading
secrets back (the `CHANNEL_INFO` frame carries them: the session already discards them). The
household's channel secrets would then pass over MQTT once: the ACL on the request topic decides
who may; document it as a credential.

### EXT-009 Built-in replies to commands

| | |
| --- | --- |
| Touches | ADR 0008 (budget), ADR 0009, the guide's "Status on request" example |

**Motivation.** A sender who sends `status` gets nothing unless an automation answers; a sender
whose command was rejected (`too_old`, `sender_unverified`) gets nothing at all and does not know
why. The guide says "the sender sends it again" without the sender knowing.

**Design sketch.** `[commands] reply = "none" | "accepted" | "all"`: with `accepted`, the gateway
sends a short direct message back ("ok: status") after publishing the command; with `all`, also
on rejections that are safe to reveal ("rejected: too_old"; never `sender_not_allowed` to an
unknown sender, which would confirm the key is known). Replies go through the outbox and the
budget as any message, deduplicated per sender and word within the dedup window, so a flood of
commands cannot make the gateway flood back. The reply's text is translatable? No: machine
output stays English (ADR 0019), or a configured template.

### EXT-010 Migration path from `SEND_TELEMETRY_REQ` (39) to `SEND_BINARY_REQ` (50)

| | |
| --- | --- |
| Touches | ADR 0018 (command 39 "can deprecate"; command 50's answer carries no sender identity) |

**Motivation.** The firmware marks 39 as deprecable; a release that drops it would switch
telemetry off (`ERR 1` → `unsupported`) for every gateway until a new version ships.

**Design sketch.** Implement 50 with a telemetry request type behind a firmware-version switch,
with the identity problem ADR 0018 names handled by the gateway: the answer's tag is the
request's, the gateway sends one request at a time and accepts one answer per tag, and the
contact is the one asked; a look-alike could still answer in its place, as with 39 (the 6-byte
prefix), so the risk is the same, not worse, when the gateway binds the answer to the tag it
issued rather than to a prefix the firmware reports. Verify against the firmware before relying
on this reading; keep 39 while it exists.

## C Beyond Home Assistant

### EXT-011 A Homie adapter for openHAB and others

| | |
| --- | --- |
| Touches | ADR 0015 ("Neutral: it can be added later as a further adapter fed by the same providers") |

**Motivation.** openHAB discovers Homie 4 devices with no configuration; the documents are the
source already.

**Design sketch.** `[homie] enabled = true` publishes `homie/<gateway>/$homie`, `$name`, `$nodes`
and one node per contact with properties (`battery`, `position` as `lat,lon`, `last-heard`,
`telemetry-state`) and a `send` settable property, all retained as the convention requires, fed
from the same `views` documents by an adapter class beside `ContactDevices`. The ACL grows by
`homie/<gateway>/#`. The retained-topic count is the cost ADR 0015 named (15 properties × 350
contacts); bound it to the contacts with devices (50).

### EXT-012 A web dashboard over MQTT-over-WebSockets

| | |
| --- | --- |
| Touches | ADR 0021 ("Later: … a web dashboard on the same documents"), ADR 0015 ("the web dashboard as a static page that connects to the broker over MQTT over WebSockets with its own broker user and ACL") |

**Motivation.** Households without Home Assistant, and a map and message view on a phone without
the companion app.

**Design sketch.** A static page (no build step: a single HTML file with a vendored MQTT.js and
Leaflet, hash-pinned by the release) that connects to the broker's WebSocket listener with a
read-only user (and `send/+` write if the household wants it), renders the documents, and never
evaluates mesh text as HTML (the same rules as the Markdown cards). Served by any web server, or
by the broker's `http_dir` (Mosquitto can serve static files on its WebSocket listener). The
gateway itself still has no listener (ADR 0015). Security: TLS on the WebSocket listener, the
ACL, and the page's own escaping; the documents' integrity caveat (SEC-005) applies.

### EXT-013 A Prometheus exporter as a sidecar

| | |
| --- | --- |
| Touches | ADR 0015 (no listener in the gateway) |

**Motivation.** Operators who run Grafana want the radio's counters and the gateway's health as
metrics.

**Design sketch.** Not in the gateway: a separate small program (or the generic `mqtt2prometheus`)
that subscribes to `state`, `stats` and `contacts/+` and exposes `/metrics`; the project could
ship a configuration for `mqtt2prometheus` in `contrib/`. Keeps ADR 0015 intact.

## D Operating several gateways

### EXT-014 Two gateways, one household

| | |
| --- | --- |
| Touches | design §3 (one radio per process), ARC-015 |

**Motivation.** A second radio for coverage (another floor, a holiday home) or redundancy. Today
each gateway is its own world: two devices per contact in Home Assistant, two polling schedules
asking the same companions (double airtime), two sets of positions.

**Design sketch.** A `[cluster] peers = ["radio2"]` setting: gateways subscribe to each other's
`contacts/<key>` documents on the same broker and (a) do not both poll a companion (the one with
the shorter path or the more recent answer asks; the other reads the document), (b) merge
"heard" information (a companion heard by either is heard), and (c) in Home Assistant, the
second gateway announces a contact device only if the first has none (or both announce and the
`person` holds both trackers). This is real distributed-system work (who leads, what happens
when one is down) and should wait for a concrete second-radio household; (c) alone (a
`contact_entity_prefix` and guidance on `person`) is cheap and covers the holiday-home case.

## E Smaller extensions

| ID | Extension | Touches |
| --- | --- | --- |
| EXT-015 | Translated user guides (German first) once the catalogs exist, generated from the English with a translation memory, reviewed by a speaker | ADR 0019 ("translated guides can follow") |
| EXT-016 | A `meshcore-home-gateway doctor` command that runs the checks of this review's operational findings (clock offset, anonymous broker, TCP posture, disk, updates pending, firmware tested) and prints a report | OPS-011, IMP-016 |
| EXT-017 | A per-target `template` setting that prefixes or suffixes texts (a site name, a timestamp) so that automations send bare texts | `[targets.<alias>]` |
| EXT-018 | Delivery receipts as Home Assistant notifications: a `notify` action that waits for `confirmed` and raises an error otherwise, through a script the guide ships (today the delivery is an event the automation has to correlate by `id`) | `docs/home-assistant.md` |
| EXT-019 | A simulated-air "playground": `make firmware-play` that starts a companion, a repeater and the gateway against a local broker for manual exploration, built from the existing fixtures | ADR 0026, `tests/firmware/` |
| EXT-020 | Support for firmware 1.19+ as it appears: the two-line bump in `sources.toml`, the firmware tests, the `TESTED_FIRMWARE` addition, with a documented turnaround target so that OPS-007's gap is short | ADR 0020, OPS-007 |
