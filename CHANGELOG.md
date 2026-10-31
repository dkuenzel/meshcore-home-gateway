# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The MQTT API, the configuration
format, the command line and Home Assistant discovery are public interfaces
([compatibility](docs/development.md#compatibility)).

## [Unreleased]

### Added

- Tests against the real firmware (`make firmware-test`, a CI job;
  [ADR 0026](docs/decisions/0026-gateway-tested-against-the-real-firmware-built-natively.md)):
  MeshCore's companion and repeater firmware of the pinned release (1.18.0), fetched by tag and
  checked against its commit, built for Linux unchanged and run on a simulated LoRa air with the
  EU/UK Narrow preset; the gateway's probe reads a companion through its serial transport.
  Scenarios of the hardware checklist's steps 29, 30, 33, 34, 35 and 39, and parts of steps 41
  and 43 (the check for positions in adverts), run the gateway's service against it, with the
  companion's phone app played on the gateway's own codec
  ([testing.md](docs/testing.md#steps-2945-on-the-simulated-air) lists what they show).
  `make check` still needs neither a compiler nor network.
- `[homeassistant] language` (default `en`): the language of the entity, device and model names
  the gateway sends to Home Assistant, also of the default device name `MeshCore radio`; one of
  the languages the gateway translates them into
  ([configuration.md](docs/configuration.md#homeassistant)). Entity IDs, event types, options,
  attributes and units stay the same in every language; a change renames only the display names
  at the next start, and names you changed stay
  ([home-assistant.md](docs/home-assistant.md#language-of-the-names)).
- The command line follows the language of its environment (`LANGUAGE`, `LC_ALL`, `LC_MESSAGES`,
  `LANG`; [configuration.md](docs/configuration.md#language-of-the-command-line)): help, errors,
  warnings, `check-config` and the `probe` report. Machine output (`--list`, `--json`, `listen`,
  `send --wait`, the results of `contacts add`, `remove` and `telemetry` with `--wait`,
  `contacts list`, `contacts link`, `config answers`, `--version`), `run` and the service's log
  stay English.

### Changed

- `check-config` writes "1 sender" or "2 senders" instead of "sender(s)", and names the language
  of the names in Home Assistant.

## [1.1.0a3] - 2026-10-31

A pre-release of 1.1.0, the third of the three telemetry slices: the gateway checks whether the
household's companions send their position in their adverts, and the guide shows their traces on
a map. After the update, press *Advert* once on each companion within range of the gateway's
radio (the button's advert crosses no repeater), or send a flood advert from its app, so that the
gateway can check it. The hardware checklist was not run for it.

### Added

- The check for positions in adverts: `contacts/<key>` gains `advert_carries_position` and
  `advert_checked_at`. For a companion, `advert_carries_position` says whether its newest advert
  carries its position (also 0, 0 from a companion that never had one), which everyone in range
  can read, as the gateway checked it; while the gateway has not checked its newest advert, the
  last check or, if the radio's record holds a position from an earlier advert, `true`; `null`
  for repeaters, room servers and sensors. `advert_checked_at` is the checked advert's time
  (`null` before a check). The gateway reads the advert in the radio's packet log once the radio
  has accepted it, also the advert of a node it adds afterwards, and sends nothing. The last
  check of each companion is kept in `state.json`, without a position. The `contacts` index gains
  `advert_carries_position` (the first 16 companions not excluded from telemetry for which it is
  `true`) and `advert_carries_position_total`.
- The event `position_in_advert` on `contact` when a companion that is not excluded from
  telemetry starts to send its position in its adverts, is added after the gateway heard its
  newest advert carrying it (in the same run of the gateway), or is included in telemetry again
  while its adverts do. Never a position. The event entity *Contact* lists it.
- *Position in adverts* (`binary_sensor.mesh_<name>_advert_position`, problem) on every
  companion's device.
- docs/home-assistant.md: a map card that draws a companion's trace from Home Assistant's
  history, what a trace shows across expiry and restarts, and an automation that tells the
  household when a handheld shares its position.
- [ADR 0025](docs/decisions/0025-positions-in-adverts-checked-from-the-packet-log.md): positions
  in adverts are checked from the radio's packet log; until the newest advert is checked, the
  radio's record decides, erring to warn. It amends ADR 0018.

### Changed

- *Companion position in adverts* on the gateway's device follows the check: on while a
  companion not excluded from telemetry has `advert_carries_position` `true`, also when it sends
  0, 0; its attributes `contacts` and `total` follow. It was on while the radio's record of any
  companion held a position, also long after the companion stopped sharing, and off for one that
  sends 0, 0 because it never had a position. `advert_position` keeps its meaning (the radio's
  record holds a position from one of the contact's adverts); the documentation now says so.
- A position from adverts is shown only while the radio's record holds one and the contact's last
  checked advert, if any, carried one: once a checked advert carries none (or 0, 0), its tracker
  turns `unavailable` unless telemetry gives a position, also after an exclusion and a restart (a
  crash included). A checked advert with a position brings it back. Until the gateway has checked
  one (after the update), and for repeaters, room servers and sensors, which it does not check,
  the record decides: an old position of a companion stays until the *Advert* asked for above. A
  record without a position now drops an advert position of every contact, also one never
  checked, repeaters included. Before, the tracker kept the last position the companion ever
  shared. A position in an advert can be one the companion saved long ago: the advert does not
  say when the fix was taken.

### Fixed

- While the radio's receive delay is 0 (its default), hops, SNR and RSSI of an advert come only
  from the log directly before the radio's report of it, also when the gateway reads the report
  more than 10 s after the log; for a contact, only when that log is newer than the record the
  gateway read; for a node the radio does not hold, only when it has the advert's time. An advert
  without such a log has none (`null`), as one that was not logged: before, any log of its key
  within 10 s gave them, such as a later copy of an older advert or a forged one. With a receive
  delay above 0, or one the radio does not report, a log of its key within 10 s gives them, as
  before.

## [1.1.0a2] - 2026-10-30

A pre-release of 1.1.0, the second of the three telemetry slices: the household's handhelds
report their battery and sensors to Home Assistant, their telemetry expires when they stop
answering, a contact can be excluded, and telemetry is on by default. The hardware checklist was
not run for it.

### Added

- Readings of each companion in `contacts/<key>`: `battery_v`, `mcu_temperature_c`, `readings`
  (keys `ch<channel>_<type>[_<n>][_<unit>]`, such as `ch2_temperature_c`; luminosity has no
  unit, `ch1_luminosity`, because the T1000-E sends 0–100 % in it) and `readings_at`;
  `telemetry_state` (`waiting`, `answering`, `no_position`, `away`, `no_answer`, `no_path`,
  `look_alike`, `excluded`, `off`), `telemetry_current`, `position_current` and
  `telemetry_excluded`. In Home Assistant, on the contact's device: *Battery* (V),
  *MCU temperature*, a sensor per reading (`sensor.mesh_<name>_ch2_temperature`, …),
  *Telemetry state* and the switch *Telemetry*.
- Excluding a contact from telemetry: the switch, `contacts/<key>/telemetry` (`on`/`off`, or JSON
  with `id` and `"forget": true`) and `meshcore-home-gateway contacts telemetry KEY on|off
  [--forget]`. `off` is taken at once, also for a contact that is not on the radio yet: the
  gateway stops asking, forgets the contact's current battery, readings and telemetry position,
  and keeps the choice in `telemetry-excluded.json` in its state directory (while that file
  cannot be read, telemetry pauses and no contact gets back its readings or a telemetry position
  from the history); `forget` also deletes the contact's readings and telemetry positions from
  the history. Results on `contact`: `excluded` and `included` with `request: "telemetry"`, the
  reasons `not_saved` and `not_forgotten` (`not_forgotten` first, when both apply), the refusals
  `not_a_companion` and `too_many` (and `rate_limited` for too many telemetry requests); the
  *Contact* event entity has the two new types. The `contacts` index gains `telemetry_excluded`
  (the first 16 keys) and `telemetry_excluded_total`.
- Events on `telemetry`: `unanswered` when a companion that answered within the day leaves two
  requests in a row unanswered, `answered` when it answers again or for the first time; the event
  entity *Telemetry* on the gateway's device. The sensor *Telemetry status* with the attribute
  `reason` (the new `state.telemetry_reason`: `first_start`, `no_radio`, `exclusions_unreadable`,
  `auto_add`, `radio_airtime`); the two telemetry airtime sensors, disabled until enabled.
- The history keeps the readings of every answer (a new table; `[history] readings_days`,
  30 days) and restores the newest at start, unless telemetry is on and its saved schedule knows a
  later answer the history missed; `position_received_at` also comes back after a restart.
- `listen --topic telemetry`.
- The interface snapshot records the device and state classes of the Home Assistant entities.

### Changed

- **`[telemetry] enabled` is `true` by default:** the gateway asks every companion of its radio
  that is not excluded. The first time telemetry runs on an installation, it waits an hour after
  the radio connects (*Telemetry status* `paused`, reason `first_start`, and a warning in the log
  with the number of companions it will ask): exclude every companion that is not the
  household's in that hour. A companion's firmware refuses telemetry to everyone until its owner
  allows it; one set to *Allow All* that has the gateway as a contact answers. A configuration
  copied from the 1.1.0a1 example says `enabled = false`: remove that line to follow the new
  default. To update from 1.0.0 without any request, see *Update* in docs/deployment.md. With a
  broker ACL, give Home Assistant's user `topic write <base_topic>/<node_id>/contacts/+/telemetry`
  ([docs/mqtt-api.md](docs/mqtt-api.md#securing-the-broker)), or the switch *Telemetry* does
  nothing.
- `state.telemetry` is `paused`, with the reason `no_radio`, while the radio is away (it stayed
  `on`); the state document follows each change of `state.telemetry` at once. It reads `on` again
  as soon as the radio is back, before the companions answer: the example in
  docs/home-assistant.md of a person leaving home waits for 10 minutes of `on`.
- A contact's tracker turns `unavailable` while its telemetry position is not current: after two
  unanswered requests in a row, after an hour (three of its intervals, if longer) without an
  answer, or an hour after the last answer with a position
  ([ADR 0024](docs/decisions/0024-telemetry-values-in-home-assistant-expire.md)). A `person`
  then follows its other trackers or turns `unknown` (docs/home-assistant.md lists which
  presence triggers fire). Positions from adverts are unchanged. This holds after a restart
  without the radio too: `state.json` keeps the last document of each companion whose answers
  are current, with its position and readings, also with the history off.
- A companion the radio's table adds again, within a day of its removal, is asked within a
  minute like a new one, and its first answer raises `answered`; it no longer gets back the
  schedule it had.
- A downgrade to 1.1.0a1 asks excluded contacts while it runs (it leaves their file alone),
  removes the four new gateway entities (Home Assistant then removes and restores the gateway's
  device once) and leaves a removed contact's *Telemetry* switch behind (delete it under
  *Settings → Entities*). Its history keeps working; the readings wait there, unpurged, until a
  later version runs again.

### Fixed

- A contact target or command sender that the radio holds with a position but without an advert
  time (written by another client of the radio, such as a phone app) gets its device at once; it
  waited for its first advert.
- Home Assistant no longer logs template errors when a contact leaves the radio: *Last heard*,
  *Hops*, *SNR* and the tracker's attributes read the empty message that clears the contact's
  document as unknown.

## [1.1.0a1] - 2026-10-30

A pre-release of 1.1.0, the first of the three telemetry slices: the gateway asks the household's
companions for their telemetry and puts their positions on the map. The hardware checklist was not
run for it; 1.1.0 follows its telemetry steps.

### Added

- Telemetry from the radio's companions ([ADR 0018](docs/decisions/0018-telemetry-pulled-from-the-households-companions.md),
  `[telemetry] enabled`, **off by default** for now): the gateway asks each companion (chat
  contact) of the radio for its battery, readings and position with `SEND_TELEMETRY_REQ`, one at
  a time and after the messages, and takes an answer only for the contact asked. Direct only: a
  companion without a path is flooded only when it was heard within 30 minutes when the flood is
  planned, at most 4 floods an hour; the gateway never resets or writes a path for it. Within
  18 s an hour of the gateway's own transmit time and an estimated 90 s an hour of mesh airtime,
  both starting empty at every start; about every 15 minutes at rest and 90 seconds while moving
  for a neighbour, once an hour without a position, backing off for one that does not answer.
  Positions go to `contacts/<key>` and the contact's tracker, with the new `position_accuracy_m`
  (15) and `position_received_at`; `state.telemetry`, `telemetry_airtime_1h_s` and
  `telemetry_mesh_airtime_1h_s`. A guide for setting up the companions:
  [docs/companions.md](docs/companions.md). **No contact can be excluded yet** (it follows with
  the next release, which turns telemetry on by default): switched on, it asks every chat contact.

### Changed

- `contacts/<key>.heard_via` can be `telemetry`: a companion heard only by its telemetry answers
  moves `heard_at` (and *Last heard*) at most once an hour, and `hops`, `snr_db` and `rssi_dbm`
  stay those of its last advert or message. `position_at` is when the position last changed. A
  contact's tracker carries `gps_accuracy` 15 for a position from telemetry, so a companion up to
  15 m outside a zone counts as inside.
- After a path reset (a message's flood fallback) the gateway's copy of the contact table and
  `contacts/<key>.path_known` show it at once, so the next message to that contact is budgeted
  as the flood it will be.

### Fixed

- The history's position cap (100 000) no longer removes a contact's newest position, which only
  `positions_days` removes: a companion at rest kept a single position, and lost it to the
  positions of a moving one.

## [1.0.0] - 2026-10-30

The first stable release. It ships the program of 1.0.0rc1, run on hardware with a LilyGo T-Echo
Lite on companion firmware 1.18.0 over USB, in a Proxmox container, with Home Assistant 2026.10.
From now on the four public interfaces follow Semantic Versioning, kept by the compatibility
policy and its snapshot of the code: an incompatible change raises the major version, a feature
the minor version, a fix the patch version. Releases are tagged by CI from the version a pull
request writes.

### Added

- The compatibility policy ([docs/development.md](docs/development.md#compatibility),
  [ADR 0023](docs/decisions/0023-public-interfaces-kept-by-a-snapshot.md)): what each public
  interface keeps within a major version, what is compatible and what is breaking. Home Assistant
  discovery is the fourth public interface: unique IDs, default entity IDs, platforms, units,
  event types, options and attribute keys (names, icons and categories are not). Abbreviated
  options, texts for people and log messages are not interface; raising a platform floor is
  breaking.
- A guard for it: `tests/public_interface.json` records the structure of the four interfaces,
  computed from the code without a radio or a broker; a test fails when the two differ, and
  `make interface` writes it again and lists what changed. The CI job *release plan* (and
  `make release-plan`) compares it with the last release on every change and refuses a removal or
  narrowing without a `**Breaking:**` entry or a `### Removed` section since, and an addition or
  change when the changelog since has no entry or only fixes; a release must contain it. A false
  alarm is waived for one release in `tests/public_interface_waivers.json`.

### Changed

- Releases are tagged by CI ([ADR 0022](docs/decisions/0022-releases-tagged-by-ci-on-main.md)):
  the pull request that finishes an increment writes the next version and its changelog section,
  the CI job *release plan* (and `make release-plan`) refuses one that is not the next (the kind
  of change comes from the changelog: `**Breaking:**` or Removed is major, Added, Changed or
  Deprecated minor, Fixed or Security patch), and after the merge CI tags the commit once its
  checks passed and starts the release workflow on the tag. A release ships the program of its
  release candidate. A run of the release workflow on a tag releases, also when started by hand.

## [1.0.0rc1] - 2026-10-24

The release candidate for 1.0.0: the gateway that has carried messages end to end since its first
version (the MeshCore app on a companion, the mesh, the gateway's radio, MQTT, Home Assistant and
back), with slices 1 to 5 of the mesh observer since. The code is that of 0.1.0a1; only the
version and the rules below changed. 1.0.0 follows once the core steps of the
[hardware checklist](docs/testing.md#hardware-checklist) pass with this candidate. From 1.0.0 on,
the MQTT API, the configuration format and the command line follow Semantic Versioning: an
incompatible change raises the major version, a feature the minor version, a fix the patch
version.

### Changed

- Versioning ([docs/development.md](docs/development.md#release)): every finished increment of a
  feature is a pre-release of the minor version it leads to (`1.1.0a1`, `1.1.0a2`, …); a release
  candidate is what the hardware checklist runs with, and the release follows when it passes.

## [0.1.0a1] - 2026-10-24

The first pre-release: the bridge (messages both ways, commands from allowlisted contacts,
Home Assistant discovery, the Proxmox installer) and slices 1 to 5 of the mesh observer
([design §13](docs/design.md#13-roadmap-the-mesh-observer)): what the radio reports, documents
and a local history, a Home Assistant device for every contact, the managed radio profile and
contact management. Built, signed and attested like a release; the
[hardware checklist](docs/testing.md#hardware-checklist) was not run for it.

### Changed

- `nodes` is published again after every table read; a contact that leaves the table is listed
  as a heard node again while it counts as heard; an older advert no longer replaces a heard
  node's record, in the history either: it keeps the advert time (history schema 2, migrated at
  start). The `contact` event comes after the contact's documents.
- The managed profile does not add a protected contact while another contact has its first
  6 bytes (`protected_contacts.failed`); `check-config` and the service warn about two protected
  keys with one prefix.
- The installer suggests no MQTT user name any more: the dialog's field starts empty, and an
  answers file without `mqtt.username` logs in without one (so `mqtt.password` alone is refused;
  before, both took the user name `meshcore-home-gateway`).
- The Proxmox installer runs straight from its address, like the Proxmox community scripts:
  `bash -c "$(wget -qO- URL)"`. Nothing is stored on the host; the program is downloaded inside
  the new container: from a release, its zipapp and unit, checked against its `SHA256SUMS` (the
  release publishes the installer with its version written in); before the first release, the
  source of `main` (`--ref` for another branch, tag or commit), built there. Run from a clone, it
  installs the clone, as before. Files go into the container through command input, no longer
  through temporary files on the host; when the program is downloaded, the settings are checked in
  the new container. `--release` installs another release, for example to go back to one.
- Settings that would have no effect are configuration errors: `mqtt.password` without
  `mqtt.username`, `radio.device` with `transport = "tcp"`, `radio.host` or `radio.port` with
  `transport = "serial"`. `radio.device` and the TLS files must be absolute paths (the service
  runs in `/`). Node IDs and aliases may not end with `-` or `_` or have two of them in a row
  (Home Assistant would merge their entity IDs). An update refuses such a configuration and keeps
  the running version; fix the named setting, for example with `--reconfigure`.
- The Home Assistant discovery topic, device identifier and unique IDs keep a `-` of
  `mqtt.node_id`, so node IDs that differ only in `-` and `_` are two devices. Entity IDs are
  unchanged; a gateway whose node ID contains `-` appears as a new device.
- The `rejected` and `duplicate` delivery events of send requests refused at once are limited to
  20 per second (bursts of 100); a log warning counts the rest at most once a minute, so a flood
  of bad requests cannot push accepted events out of a subscriber's queue on the broker.
- `make matrix` fails when a Python of `PYTHONS` is missing; the pre-push hook refuses a push
  with uncommitted changes or of a commit that is not checked out; `make check` builds the
  zipapp once; a changed lock file gives a fresh virtual environment.

### Fixed

- The broker login: the installer's dialogs run in the `C.UTF-8` locale (in a locale the host
  lacks, whiptail dropped every non-ASCII character typed, passwords included); the user name
  field starts empty (whiptail added what was typed to the suggested name); the password dialog
  names its user, and `--reconfigure` no longer keeps the old password for a new user name.
- The installer logs in to the broker before it reads the radio and, when the broker refuses,
  shows its answer and offers to enter the broker settings again; the final message shows the
  result. `check-config --broker` does the same login, `check-config` names the user.
- The gateway names the user in a refused login, and logs attempts that no broker answers
  (`cannot reach HOST:PORT: reason`) and connections closed before an answer; they went
  unlogged. `mqtt.host` and `radio.host` must be a host name or IP address: `mqtt://`, a port or
  brackets are refused with a hint.
- The service user could not read a configuration written under a strict umask; the program's
  directory and the configuration's are created readable whatever the host's umask. The final
  message names the program by its path, which `pct exec` needs when the host's `PATH` lacks
  `/usr/local/bin`.
- Commands: an identical copy of a command's packet, also of a rejected one, stays a duplicate
  while it could pass the age check (an hour, or twice `max_age_s`); after `broker_unavailable`
  the sender's timestamp is stored, so a copy is a `replay` also after a restart.
- On stop the gateway releases the radio first and still publishes the messages the radio has
  handed over, also one whose answer is on its way (for up to 2 s); they were lost.
- A direct message is not sent to a look-alike contact that the radio has just added: the gateway
  reads the contact table again before sending, also after waiting for airtime.
- Channel texts accepted before the radio's first connection are limited for its real name when
  they are sent, and deduplicated on the text that is sent.
- Truncated texts no longer end in half a character: flags, joined emoji, skin tones, keycaps,
  letters with their marks and the Thai, Lao and halfwidth kana signs that follow a letter stay
  whole or go before the `...`.
- A serial port whose terminal setup fails is closed again and reported as a connection error;
  it kept the exclusive lock until a restart.
- `listen` and `send --wait` survive payloads nested too deeply for the JSON parser; `config
  render` reports an output it cannot write, loads the TLS files like `check-config`, checks
  target aliases before writing them, and syncs the file to disk.
- Installer: an interrupted run (Ctrl-C, a closed SSH session, kill, a closed output pipe) starts
  a service stopped by `--reconfigure` again and names a new container to remove; target answers
  are checked before the container is created and count also when the radio offers no channel or
  chat contact; `--reconfigure` keeps every alias of a target; without a user name no password
  is asked; `--update` no longer needs whiptail; the settings check leaves no `__pycache__` in
  the checkout; a failed lookup of the next container ID or the storages stops the run.
- Documentation: the airtime bound is stated per run of the service and for the counted airtime
  (a flood to a far recipient takes more); retries, the time to live, `flood_after`, the
  connection handshake, resynchronisation, rollback, retained requests, node IDs in topics and
  the unauthenticated TCP link are described as the code behaves.
- Over TCP the session no longer sends the 256 zero bytes of the serial resynchronisation: they
  misaligned the WiFi firmware's frame parser, so no TCP session could start. The TCP emulator
  in the tests now parses like the WiFi firmware.
- After a long broker outage the reconnect no longer loses `online`, discovery and state behind a
  full message queue: only events are bounded (1000, the excess is dropped and logged), and
  retained documents are not queued while the broker is unreachable.
- Commands that arrive while the broker is unreachable are refused (`broker_unavailable`) instead
  of reaching the automations at an arbitrary later time.
- An acknowledgement that arrives while the next attempt waits for airtime ends the delivery as
  `confirmed`, without another transmission or path reset.
- A command queued behind a session that has just ended is no longer written to the radio; the
  error names the cause.
- A clean shutdown no longer logs a reconnect warning; an unconfirmed `offline` is logged.
- Text cleaning removes only control characters and direction overrides; joiners, variation
  selectors and other format characters stay, so emoji sequences survive. Vertical tab, form feed
  and Unicode line separators become line breaks; spaces from transliteration are collapsed.
- With `max_age_s = 0` a command dated more than a day ahead is refused (`from_the_future`)
  instead of blocking its sender's later commands as replays.
- `listen` ends when its reader goes away (`| head`) and prints at most `--count` events;
  `send` refuses aliases that are not configured and impossible `--wait` values.
- Configuration, answers and state files that are not valid UTF-8 are configuration errors (the
  state file is moved aside); blank strings are refused as settings.
- A TCP connect timeout names the radio; the hourly airtime budget in the state document no longer
  shows float artefacts; names from the mesh are quoted in the log.
- Command events are published with QoS 0: after a broker loss that the gateway had not noticed
  yet (one to two `keepalive_s`), a command could still reach the automations after the
  reconnect, at any later time; now it is lost and the sender can send it again.
- Removing components in Home Assistant also works after Home Assistant restarted (a
  placeholder binds the old entity first) and after a run that reached neither broker nor Home
  Assistant (removals stay pending for 30 days). The placeholder is enabled, and the gateway
  announces again when Home Assistant clears its device's topic: removing a disabled entity, such
  as the packet sensors after `diagnostics.publish_rx_log` was switched off, made Home Assistant
  clear the topic itself, which took every entity of the gateway away until its next start.
- Events queued while the broker is down are no longer logged as refused.
- Texts: in ASCII mode invisible format characters (soft hyphen, zero-width space) are dropped
  instead of becoming `?`; a text of only such characters is rejected as empty; `send` refuses a
  text that is not UTF-8 instead of printing a traceback.
- Installer: a malformed answers line is reported by its number also when the broker password
  contains `=`; an answers file that is not UTF-8 is reported by line, without a traceback.
- Releases: `make release` admits only version forms that the release workflow classifies right
  (no `1.0.0rc1.post1`, no local versions); the documented attestation check names the release
  workflow and the tag.
- Installer: `--update` refuses a container without an installation instead of installing
  packages, a user and a service into it; `--reconfigure` keeps an anonymous broker anonymous and
  a port left at its default, and refuses answers it cannot apply (`radio.choice`, `ct.*`); a
  malformed answers line is reported by its number, without its content (it may hold the
  password); container IDs below 100 are refused as invalid, not as taken.
- `make pyz` builds the zipapp with the isolated interpreter `/usr/bin/python3 -I`, as installed;
  `make lock` takes the newest releases within the cooldown; the sdist contains `.github/` and
  `.githooks/`, which its tests and `make check` need.
- Home Assistant's birth message is followed on its own topic (`homeassistant.status_topic`,
  default `homeassistant/status`) instead of below the discovery prefix: with a custom prefix the
  entities stayed unavailable after a restart of Home Assistant.
- Documentation matches the code: retained send requests are rejected, not ignored; the final
  delivery states; the `STATUS=` line; exit code 2; what Home Assistant's birth message
  republishes; topic ACLs are required with commands; manual installation steps, roll back, a
  renamed serial device and the limits of `--reconfigure` in the deployment guide.

### Added

- Contact management on request ([ADR 0017](docs/decisions/0017-contacts-on-the-radio-each-with-its-own-device.md),
  `[contacts] manage`, on by default): `add_contact` takes a public key, a
  `meshcore://contact/add` link or a contact card, `contacts/<key>/remove` removes a contact. A
  node the gateway heard is added with the record the radio checked; a link or card only claims
  name and type until the node's first advert (`contacts/<key>.verified`), and a card's
  signature is not checked. Contact targets and command senders are never removed; a key with
  the first 6 bytes of a contact or a protected key is refused; the gateway sets no flag but the
  favourite; nothing is written on untested firmware unless `radio.profile.manage = "always"`.
  A request ends in one `contact` event (none only for refusals beyond the rate limit and requests
  queued at a stop): `added` or `removed` with its `request` and `id`, the
  new `refused` (nothing was written) or `failed`; every `contact` event now carries `prefix`,
  `request`, `id` and `reason`. A queue of 8 requests and a budget of radio work (10, then 1 a
  minute) bound the radio's load; refusals are limited like those of send requests.
- Contact management in Home Assistant (docs/home-assistant.md): on the gateway's device the
  select *Add heard node* (the 20 most recently heard companions, room servers and sensors that
  are no look-alikes, as `<first 12 key digits>…<last 8> <name>`, rooms and sensors marked) and
  the text *Add contact (key or link)*, in a discovery document of their own
  (`…/manage_contacts/config`); *Remove from radio* on every contact's device; the problem sensor
  *Look-alike node heard*; the attribute `contact_link` of the radio link, with a QR card for the
  MeshCore app. The controls are available while the radio is connected and management is on (a
  remove button not for a protected contact); `contacts.manage` and `contacts/<key>.removable` are
  now `false` on firmware the gateway is not tested with, unless `radio.profile.manage =
  "always"`. The select's options change at most every 30 seconds (at once after an add or a
  removal) and 70 times an hour (2 more for each start of the gateway), below Home Assistant's
  warning limit. New request topic `homeassistant/add_node` (the select's label; refused
  `unknown_node` when it names no heard node or contact), subscribed with Home Assistant's
  discovery; `contacts add` and `remove` take a label with the name after it, as the select
  lists it. Examples: a dashboard row that asks
  before it removes, an automation that reports refused and failed requests.
- `meshcore-home-gateway contacts list|add|remove|link`: through the broker, with the full key
  shown to compare; a heard node is named by its first 12 and last 8 key digits, never by a bare
  prefix. `listen --topic contact|node`.
- `state.contact_link` (the gateway's own contact link, for a QR code), `contacts.manage`,
  `contacts/<key>.removable` and `.verified`, `nodes[].look_alike`, `nodes.look_alike` and
  `look_alike_total`, `node.look_alike`.
- Documents for every front end, retained on the broker and published when they change
  ([ADR 0015](docs/decisions/0015-provider-neutral-documents-on-the-bus.md), docs/mqtt-api.md):
  `stats` (the radio's own measurements: noise floor, its real time on air in the last hour,
  packet counters, uptime, error flags, its duty-cycle setting), `contacts` (index of the radio's
  contact table) and `contacts/<key>` (one per contact: when and how it was last heard, its path,
  its last known position), `nodes` (nodes heard but not added, without positions) and `history`
  (the latest messages in and out). Events `contact` (added, removed) and `node` (a node heard for
  the first time).
- A managed radio profile ([ADR 0020](docs/decisions/0020-managed-radio-profile-for-the-observer.md),
  `[radio.profile]`): at every connect the gateway sets its radio up as an observer, writing only
  what differs: contacts added manually only, telemetry denied to everyone, no position in
  adverts, the 10 % duty cycle in 869.40–869.65 MHz, the name if configured, and the protected
  contacts (targets, command senders) on the radio as favourites. A change made on the radio is
  set back with a warning, except the name; untested firmware is only observed. A value that does
  not take is no longer written after three connects in a row, per firmware version. The result is
  the `settings` document with `setting` events, and in Home Assistant *Radio profile* and
  *Radio setting*. `probe` shows what the service will do at its next connect, the protected
  contacts included (`--config` for the profile of a configuration, `--state-dir` for the
  service's stored state, read only); the installer asks for the radio's name in the advanced
  setup and shows that plan. A protected contact put on the radio before it was ever heard gets
  its Home Assistant device with its first advert.
- Every contact of the radio is a recipient: `contacts/<key>/send` sends a direct message to a
  companion in the contact table, through the same queue, airtime budget and duplicate check as a
  target ([ADR 0017](docs/decisions/0017-contacts-on-the-radio-each-with-its-own-device.md));
  rejected as `unknown_contact` or `not_a_companion`. Messages of a contact also arrive on
  `contacts/<key>/received`.
- A Home Assistant device for every contact, linked to the gateway's device, which appears when
  the contact is added to the radio and goes when it is removed (docs/home-assistant.md,
  *Contacts*): a notify entity to message it, its messages as events, when it was last heard, hops
  and signal, and a tracker once its position is known. Entity IDs `mesh_<name>_…`, kept when the
  contact is renamed. A device deleted in Home Assistant hides the contact there (event `hidden`)
  until *Show hidden contacts* is pressed. At most `homeassistant.contact_devices` contacts (default
  50) get a device, the household first.
- Entities on the gateway's device for the new documents: *Contacts*, *Nodes heard*, the events
  *Contact* and *New node*, *Noise floor*, *Radio transmit time last hour*, problem sensors for a
  contact with remote access and for a companion whose adverts carry its position, and the radio's
  uptime and packet counters (disabled until enabled).
- A local history in `/var/lib/meshcore-home-gateway/history.sqlite3` (SQLite, from the standard
  library; [ADR 0016](docs/decisions/0016-history-in-a-local-sqlite-database.md)): messages,
  positions of contacts, when nodes were last heard. Settings `[history]` `enabled`,
  `messages_days`, `positions_days` (30 days by default).
- `probe` reports what the radio tells about privacy and airtime: its position and whether its
  adverts carry it, who may request its telemetry (base, location, environment), the extra
  acknowledgements, its own duty-cycle factor (`airtime_factor`), and the flags of every contact;
  a contact that may run the radio's command line over the air (flag 0x10) is marked.
- Contract tests against the real Home Assistant MQTT integration (`make ha-test`, a CI job): the
  discovery document, the state and event payloads and the automations of the Home Assistant
  guide, with a hash-pinned Home Assistant release that Dependabot proposes updates for.
- Home Assistant: the device shows the radio's model, firmware and key (announced again when they
  become known or change); sensors *Airtime available* and *Queue*; the radio's static facts as
  attributes of *Radio link*; packet RSSI and SNR sensors with `publish_rx_log` (disabled by
  default). Components of an earlier configuration (a removed target) are removed by the gateway.
  The guide names the required version (2026.8), maps every MQTT value to its place in Home
  Assistant and uses the `event.received` trigger, which also fires on an entity's first event.
- The project is named meshcore-home-gateway (developed as meshcore-bus until 2026-10-22; ADR
  0012): command, package, unit, user, paths, MQTT client identifier and Home Assistant IDs.
- Design document, architecture decision records, protocol reference (MeshCore companion
  firmware 1.18.0), MQTT API, Home Assistant, configuration, deployment, testing and development
  documentation.
- Companion protocol subset: framing with resynchronisation, command encoders, decoders for
  responses, pushes and messages in the legacy and the version 3 layout; device information of
  older firmware (80 and 81 bytes) and the auto-add answer of firmware 1.12 and 1.13 (without the
  hop limit) are accepted.
- Radio session over USB serial (termios, DTR asserted, exclusive) or TCP: handshake, clock
  synchronisation, contact-adding settings, contacts and channels, message sync, acknowledgement
  events, heartbeat. A malformed or unexpected answer ends the session, so the bridge reconnects
  and resynchronises.
- Bridge service: configured targets, text rules with optional ASCII transliteration, duplicate
  suppression, bounded queue with time to live, airtime budget from the LoRa time-on-air formula,
  acknowledgement tracking with retries and path reset, delivery events with reasons (`expired`
  only for messages that never left), inbound deduplication, retained state document.
- Command policy with allowlisted full public keys and replay protection. The command word is the
  first word of the text, ended by any whitespace (also a line break or a tab). The sender must be
  the only contact with its 6-byte prefix in the table read after the message arrived, and no
  other key with that prefix may have been a contact of the radio in the last 24 hours (table
  reads, advert, path and overwrite pushes), so a look-alike that came and went makes the sender
  ambiguous, while one that the radio did not add does not count. Rejections by the cached table
  need no read, one read serves a batch, and a read in progress is reused. Commands fetched while
  the bridge connects are refused. The bridge warns at connect about contact types the radio adds
  on its own and about commands over TCP.
- MQTT adapter on paho-mqtt with last will, Home Assistant device discovery with sensor, event and
  notify entities. TLS files that cannot be loaded are configuration errors (exit code 78, no
  restart loop), and TLS file settings without `tls = true` or a key without a certificate are
  refused rather than ignored.
- Command line: `run`, `check-config` (also loads the TLS files), `probe` (also shows how the
  radio adds contacts, leaves waiting messages in the radio, and takes IPv6 addresses as
  `[ADDRESS]:PORT`), `send` (with `--wait` following its own request), `listen`, `config render`,
  `config answers`. Texts from the mesh reach the terminal without control or format characters;
  malformed arguments and unknown groups are reported without a traceback.
- systemd unit with notify, watchdog and sandboxing; reproducible zipapp build.
- Proxmox VE installer: default and advanced setup in dialogs or from an answers file (unknown
  installer answers refused, every configuration setting passed on, the radio never guessed),
  settings checked with the program's own code before the radio is read, unprivileged Debian 13
  container with nesting, radio passed through by its stable path, update that switches only to a
  version accepting the configuration (also with an unchanged version string), reconfigure that
  keeps the current settings and the targets the radio no longer offers.
- Release workflow (ADR 0013): a tag `vX.Y.Z` builds the zipapp, wheel and sdist reproducibly
  with the same `make` targets as a local check, attests them with Sigstore-signed build
  provenance and publishes them with `SHA256SUMS` on a GitHub release, the version's changelog
  section as its notes (links to the files of the tag); `make release` is the local dry run.
  Every finished increment is a pre-release `X.Y.ZaN` (docs/development.md).
- Continuous integration that runs the same `make` targets as a local check (`make check`,
  `make matrix`), with a pre-push hook, hash-pinned tools with a 14-day cooldown and SHA-pinned
  actions.
