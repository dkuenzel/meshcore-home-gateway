# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The MQTT API, the configuration
format and the command line are public interfaces.

## [Unreleased]

### Changed

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
- Removing components in Home Assistant also works after Home Assistant restarted (a disabled
  placeholder binds the old entity first) and after a run that reached neither broker nor Home
  Assistant (removals stay pending for 30 days).
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
  provenance and publishes them with `SHA256SUMS` on a GitHub release; `make release` is the local
  dry run.
- Continuous integration that runs the same `make` targets as a local check (`make check`,
  `make matrix`), with a pre-push hook, hash-pinned tools with a 14-day cooldown and SHA-pinned
  actions.
