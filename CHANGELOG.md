# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The MQTT API, the configuration
format and the command line are public interfaces.

## [Unreleased]

### Fixed

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
  settings checked before anything is created, unprivileged Debian 13 container with nesting, radio
  passed through by its stable path, update that switches only to a version accepting the
  configuration (also with an unchanged version string), reconfigure that keeps the current
  settings and the targets the radio no longer offers.
- Release workflow (ADR 0013): a tag `vX.Y.Z` builds the zipapp, wheel and sdist reproducibly
  with the same `make` targets as a local check, attests them with Sigstore-signed build
  provenance and publishes them with `SHA256SUMS` on a GitHub release; `make release` is the local
  dry run.
- Continuous integration that runs the same `make` targets as a local check (`make check`,
  `make matrix`), with a pre-push hook, hash-pinned tools with a 14-day cooldown and SHA-pinned
  actions.
